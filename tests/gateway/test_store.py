"""Actual SQLite mode/authorizer/SQL filtering and bounded pagination."""
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from enterprise_ai_lab.mcp_gateway.store import (
    DATA, GatewayError, Principal, ReadOnlyStore, fixture_source,
    initialize_fixture, principal_for_profile,
)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.sqlite"
        self.metadata = initialize_fixture(self.path)
        self.store = ReadOnlyStore(self.path)
        self.addCleanup(self.store.close)
        self.reader = principal_for_profile("alpha-reader")
        self.analyst = principal_for_profile("alpha-analyst")

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(GatewayError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(str(self.path), str(caught.exception))
        return caught.exception

    def test_fixture_source_and_fresh_no_replace_initialization(self):
        value, digest = fixture_source()
        self.assertEqual(digest, hashlib.sha256(DATA.read_bytes()).hexdigest())
        self.assertEqual(len(value["work_orders"]), 18)
        self.assertEqual(self.metadata["fixture_sha256"], self.store.fixture_sha256)
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            initialize_fixture(self.path)
        self.assertEqual(before, self.path.read_bytes())
        missing = self.path.parent / "absent.sqlite"
        with self.assertRaises(FileNotFoundError):
            ReadOnlyStore(missing)
        self.assertFalse(missing.exists())

    def test_tenant_collision_and_analyst_only_rows(self):
        alpha = self.store.get_work_order(self.reader, "WO-0001")["item"]
        beta = self.store.get_work_order(principal_for_profile("beta-reader"), "WO-0001")["item"]
        self.assertEqual(alpha["work_order_id"], beta["work_order_id"])
        self.assertIn("ALPHA_READER_01", alpha["summary"])
        self.assertIn("BETA_READER_01", beta["summary"])
        hidden = self.assertCode("not_found", self.store.get_work_order, self.reader, "WO-0002")
        absent = self.assertCode("not_found", self.store.get_work_order, self.reader, "WO-9999")
        self.assertEqual(hidden.safe_message, absent.safe_message)
        self.assertIn("ALPHA_ANALYST_02", self.store.get_work_order(self.analyst, "WO-0002")["item"]["summary"])
        self.assertEqual(self.store.list_work_orders(Principal("local:other", "other", ("reader",)))["items"], [])

    def test_engine_prefilter_fills_pages_and_cursor_reveals_only_visible_ids(self):
        first = self.store.list_work_orders(self.reader)
        self.assertEqual([r["work_order_id"] for r in first["items"]], ["WO-0001", "WO-0003", "WO-0005"])
        self.assertEqual(first["next_after"], "WO-0005")
        second = self.store.list_work_orders(self.reader, after_id=first["next_after"])
        self.assertEqual([r["work_order_id"] for r in second["items"]], ["WO-0006", "WO-0008", "WO-0009"])
        self.assertIsNone(second["next_after"])
        self.assertCode("not_found", self.store.list_work_orders, self.reader, after_id="WO-0002")
        self.assertEqual([r["work_order_id"] for r in self.store.list_work_orders(self.analyst)["items"]],
                         ["WO-0001", "WO-0002", "WO-0003"])
        self.assertEqual([r["work_order_id"] for r in self.store.list_work_orders(self.reader, status="open")["items"]], ["WO-0001"])

    def test_analyst_diagnostics_keep_tenant_and_row_grants(self):
        self.assertCode("forbidden", self.store.get_asset_diagnostics, self.reader, "AS-001")
        alpha = self.store.get_asset_diagnostics(self.analyst, "AS-001")["item"]
        beta = self.store.get_asset_diagnostics(principal_for_profile("beta-analyst"), "AS-001")["item"]
        self.assertIn("ALPHA_DIAGNOSTIC_001", alpha["observation"])
        self.assertIn("BETA_DIAGNOSTIC_001", beta["observation"])
        self.assertCode("not_found", self.store.get_asset_diagnostics, self.analyst, "AS-999")
        self.assertCode("forbidden", self.store.list_work_orders, principal_for_profile("alpha-observer"))

    def test_read_only_authorizer_denies_mutations_metadata_attach_and_functions(self):
        statements = [
            "DELETE FROM work_orders", "UPDATE work_orders SET summary='changed'",
            "INSERT INTO work_orders VALUES ('x','WO-0001','AS-001','x','open','x')",
            "DROP TABLE work_orders", "CREATE TABLE surprise(x)", "ATTACH DATABASE ':memory:' AS other",
            "PRAGMA query_only=OFF", "SELECT * FROM fixture_metadata", "SELECT * FROM sqlite_master",
            "SELECT load_extension('not-a-library')", "SELECT sqlite_version()",
        ]
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.store._connection.execute(sql)
        self.assertIn("ALPHA_READER_01", self.store.get_work_order(self.reader, "WO-0001")["item"]["summary"])

    def test_uri_read_only_remains_when_trusted_test_removes_other_guards(self):
        self.store._connection.set_authorizer(None)
        self.store._connection.execute("PRAGMA query_only=OFF")
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            self.store._connection.execute("DELETE FROM work_orders")

    def test_actual_exclusive_lock_returns_bounded_busy_without_sql_or_path(self):
        writer = sqlite3.connect(self.path, timeout=.1)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        self.assertCode("resource_busy", self.store.list_work_orders, self.reader)
        self.assertLess(time.monotonic() - started, 1)
        writer.rollback()
        self.assertEqual(len(self.store.list_work_orders(self.reader)["items"]), 3)

    def test_progress_deadline_interrupts_an_actual_sql_read(self):
        # A controlled clock jump exercises SQLite's actual progress handler.
        with mock.patch("enterprise_ai_lab.mcp_gateway.store.time.monotonic", side_effect=[0, 1, 1, 1, 1]):
            self.assertCode("resource_busy", self.store.list_work_orders, self.reader)
        self.assertEqual(len(self.store.list_work_orders(self.reader)["items"]), 3)

    def test_wrong_schema_nonregular_and_symlink_refused(self):
        wrong = self.path.parent / "wrong.sqlite"
        with sqlite3.connect(wrong) as connection:
            connection.execute("CREATE TABLE arbitrary(x)")
        with self.assertRaises(ValueError): ReadOnlyStore(wrong)
        linked = self.path.parent / "linked.sqlite"; linked.symlink_to(self.path)
        with self.assertRaises(ValueError): ReadOnlyStore(linked)
        if hasattr(os, "mkfifo"):
            fifo = self.path.parent / "fifo.sqlite"; os.mkfifo(fifo)
            with self.assertRaises(ValueError): ReadOnlyStore(fifo)

    def test_strict_scalar_and_identifier_bounds(self):
        for limit in (True, False, 0, 11, 3.0, "3"):
            with self.subTest(limit=limit): self.assertCode("invalid_request", self.store.list_work_orders, self.reader, limit=limit)
        for status in (True, "OPEN", "pending", 1, ["open"]):
            with self.subTest(status=status): self.assertCode("invalid_request", self.store.list_work_orders, self.reader, status=status)
        for identifier in (1, True, "WO-0001' OR 1=1", "WO-00001", "x" * 10000):
            with self.subTest(identifier=identifier): self.assertCode("invalid_request", self.store.get_work_order, self.reader, identifier)
        self.store.close()
        self.assertCode("internal_error", self.store.list_work_orders, self.reader)
