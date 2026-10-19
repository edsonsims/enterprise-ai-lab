"""Real SQLite service calls, strict arguments and fail-closed audit windows."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from enterprise_ai_lab.mcp_gateway.audit import GatewayAudit, TRIGGERS, canonical
from enterprise_ai_lab.mcp_gateway.service import GatewayService
from enterprise_ai_lab.mcp_gateway.store import GatewayError, ReadOnlyStore, initialize_fixture, principal_for_profile


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / "source.sqlite"
        initialize_fixture(self.source)
        self.store = ReadOnlyStore(self.source); self.addCleanup(self.store.close)
        self.audit = GatewayAudit(self.root / "audit.sqlite")
        self.service = GatewayService(self.store, self.audit, principal_for_profile("alpha-reader"))

    def assertCode(self, code, service, name, arguments):
        with self.assertRaises(GatewayError) as caught:
            service.call(name, arguments)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(str(self.root), str(caught.exception))
        return caught.exception

    def test_profile_tool_surface_and_actual_result_envelope(self):
        self.assertEqual(self.service.allowed_tools(), ["list_work_orders", "get_work_order"])
        analyst = GatewayService(self.store, self.audit, principal_for_profile("alpha-analyst"))
        observer = GatewayService(self.store, self.audit, principal_for_profile("alpha-observer"))
        self.assertEqual(len(analyst.allowed_tools()), 3); self.assertEqual(observer.allowed_tools(), [])
        value = self.service.call("list_work_orders", {})
        self.assertEqual(value["meta"], {"operation": "list_work_orders", "tenant_id": "alpha"})
        self.assertEqual([row["work_order_id"] for row in value["data"]["items"]], ["WO-0001", "WO-0003", "WO-0005"])
        self.assertCode("forbidden", self.service, "get_asset_diagnostics", {"asset_id": "AS-001"})
        self.assertCode("forbidden", observer, "get_work_order", {"work_order_id": "WO-0001"})
        self.assertIn("ALPHA_DIAGNOSTIC_001", analyst.call("get_asset_diagnostics", {"asset_id": "AS-001"})["data"]["item"]["observation"])

    def test_omitted_and_explicit_null_optional_filters_agree(self):
        self.assertEqual(self.service.call("list_work_orders", {}),
                         self.service.call("list_work_orders", {"status": None, "after_id": None}))

    def test_strict_arguments_and_unknown_tool_are_audited_without_values(self):
        cases = [("list_work_orders", {"tenant_id": "BETA_ARGUMENT_SECRET"}),
                 ("list_work_orders", {"roles": ["analyst"]}),
                 ("list_work_orders", {"sql": "SELECT PRIVATE_CANARY"}),
                 ("list_work_orders", {"limit": True}), ("list_work_orders", {"limit": 3.0}),
                 ("list_work_orders", {"status": False}), ("list_work_orders", {"after_id": 1}),
                 ("get_work_order", {}), ("get_work_order", {"work_order_id": True}),
                 ("get_work_order", {"work_order_id": "WO-0001", "subject": "forged"}),
                 ("arbitrary_sql_PRIVATE_TOOL", {"sql": "DROP TABLE work_orders"}),
                 ("get_work_order", []), ("get_work_order", None)]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments): self.assertCode("invalid_request", self.service, name, arguments)
        events = self.audit.events(); self.assertEqual(len(events), 2 * len(cases))
        encoded = json.dumps(events)
        for secret in ("BETA_ARGUMENT_SECRET", "PRIVATE_CANARY", "PRIVATE_TOOL", "DROP TABLE", "forged"):
            self.assertNotIn(secret, encoded)
        self.assertIn("unknown_tool", encoded)
        self.assertTrue(all(row["error_code"] == "invalid_request" for row in events if row["phase"] == "outcome"))

    def test_oversized_and_non_json_inputs_use_labelled_rejection_digest(self):
        for arguments in ({"extra": "PRIVATE_TOO_LARGE" * 10000}, {"extra": object()}, {"extra": float("nan")}, {1: "value"}):
            self.assertCode("invalid_request", self.service, "list_work_orders", arguments)
        events = self.audit.events()
        self.assertTrue(all(row["input_digest_kind"] == "rejected_descriptor" for row in events))
        self.assertNotIn("PRIVATE_TOO_LARGE", json.dumps(events))

    def test_intent_and_outcome_hashes_correlate_and_persist(self):
        arguments = {"work_order_id": "WO-0001"}
        value = self.service.call("get_work_order", arguments)
        reopened = GatewayAudit(self.audit.database_path)
        first, last = reopened.events()
        self.assertEqual(first["phase"], "intent"); self.assertEqual(last["phase"], "outcome")
        self.assertEqual(first["call_id"], last["call_id"])
        self.assertEqual(first["input_sha256"], hashlib.sha256(canonical({"tool": "get_work_order", "arguments": arguments}).encode()).hexdigest())
        self.assertEqual(last["output_sha256"], hashlib.sha256(canonical(value).encode()).hexdigest())
        self.assertEqual(last["row_count"], 1)
        self.assertNotIn("ALPHA_READER", json.dumps([first, last]))

    def test_fixture_aliases_and_sqlite_sidecar_hardlinks_cannot_be_audit_files(self):
        before = self.source.read_bytes()
        paths = [self.source, self.root / "same-inode.sqlite", self.root / "source-link.sqlite"]
        os.link(self.source, paths[1]); paths[2].symlink_to(self.source)
        for path in paths:
            with self.subTest(path=path), self.assertRaises(GatewayError):
                GatewayService(self.store, GatewayAudit(path), principal_for_profile("alpha-reader"))
        for suffix in ("-wal", "-shm", "-journal"):
            audit_path = self.root / ("sidecar-" + suffix[1:] + ".sqlite")
            alias = Path(str(audit_path) + suffix); os.link(self.source, alias)
            with self.subTest(suffix=suffix), self.assertRaises(GatewayError) as caught:
                GatewayService(self.store, GatewayAudit(audit_path), principal_for_profile("alpha-reader"))
            self.assertEqual(caught.exception.code, "invalid_request")
            self.assertFalse(audit_path.exists()); self.assertEqual(before, self.source.read_bytes())
        # Recheck before each call as well as at service construction.
        late = Path(str(self.audit.database_path) + "-shm"); os.link(self.source, late)
        with self.assertRaises(GatewayError): self.service.call("list_work_orders", {})
        self.assertEqual(before, self.source.read_bytes())

    def test_corrupt_or_unavailable_audit_stops_source_sql(self):
        self.service.call("list_work_orders", {})
        with sqlite3.connect(self.audit.database_path) as connection:
            connection.execute("DROP TRIGGER gateway_audit_no_update")
            connection.execute("UPDATE gateway_audit SET event_json='{}' WHERE sequence=1")
            connection.execute(TRIGGERS["gateway_audit_no_update"])
        with mock.patch.object(self.store, "_query", wraps=self.store._query) as query:
            self.assertCode("audit_unavailable", self.service, "list_work_orders", {})
            query.assert_not_called()
        bad = self.root / "directory.sqlite"; bad.mkdir()
        service = GatewayService(self.store, GatewayAudit(bad), principal_for_profile("alpha-reader"))
        with mock.patch.object(self.store, "_query", wraps=self.store._query) as query:
            self.assertCode("audit_unavailable", service, "list_work_orders", {})
            query.assert_not_called()

    def test_outcome_failure_withholds_read_result_and_leaves_honest_intent(self):
        append = self.audit.append
        def fail_outcome(**metadata):
            if metadata["phase"] == "outcome":
                raise GatewayError("audit_unavailable", "Controlled audit write failure.")
            return append(**metadata)
        with mock.patch.object(self.audit, "append", side_effect=fail_outcome), mock.patch.object(self.store, "_query", wraps=self.store._query) as query:
            error = self.assertCode("audit_unavailable", self.service, "get_work_order", {"work_order_id": "WO-0001"})
            query.assert_called_once()
            self.assertNotIn("ALPHA_READER", error.safe_message)
        self.assertEqual([row["phase"] for row in self.audit.events()], ["intent"])
        restarted = GatewayService(self.store, GatewayAudit(self.audit.database_path), principal_for_profile("alpha-reader"))
        self.assertIn("ALPHA_READER", restarted.call("get_work_order", {"work_order_id": "WO-0001"})["data"]["item"]["summary"])
        self.assertEqual([row["phase"] for row in self.audit.events()], ["intent", "intent", "outcome"])

    def test_real_busy_source_produces_durable_error_not_fake_success(self):
        writer = sqlite3.connect(self.source); self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        self.assertCode("resource_busy", self.service, "list_work_orders", {})
        writer.rollback()
        self.assertEqual(self.audit.events()[-1]["error_code"], "resource_busy")
        self.assertEqual(self.audit.events()[-1]["row_count"], 0)

    def test_unverifiable_file_separation_is_sanitized_before_reads(self):
        with mock.patch("enterprise_ai_lab.mcp_gateway.service._inodes", side_effect=PermissionError("PRIVATE_PATH")), mock.patch.object(self.store, "_query", wraps=self.store._query) as query:
            error = self.assertCode("audit_unavailable", self.service, "list_work_orders", {})
            self.assertNotIn("PRIVATE_PATH", str(error)); query.assert_not_called()
        self.assertFalse(self.audit.database_path.exists())
