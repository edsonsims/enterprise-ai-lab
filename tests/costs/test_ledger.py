from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from decimal import Inexact, ROUND_DOWN, localcontext
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import enterprise_ai_lab.costs as costs_package
from enterprise_ai_lab.costs import CostError, CostLedger, initialize, principal_for_profile


START = "2026-01-01T00:00:00Z"
END = "2026-02-01T00:00:00Z"


def event(key="event-1", **changes):
    value = {"event_id": key, "request_id": "request-1", "metric": "embedding_tokens",
             "quantity": 1, "rate_version": "fictional-v1", "occurred_at": START}
    value.update(changes)
    return value


def statement(key="statement-1", *, lines=None, settled="0.00", **changes):
    value = {"statement_id": key, "currency": "USD", "start": START, "end": END,
             "lines": [{"event_id": "event-1", "amount": "0.005"}] if lines is None else lines,
             "settled_total": settled}
    value.update(changes)
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def checksum(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


CHILD = r'''
import json, sys, time
from pathlib import Path
from enterprise_ai_lab.costs import CostError, CostLedger, principal_for_profile
path,ready,gate,profile,value,mode,operation=sys.argv[1:]
ledger=CostLedger(path)
if mode == 'race':
    Path(ready).write_text('ready')
    deadline=time.monotonic()+10
    while not Path(gate).exists():
        if time.monotonic()>=deadline: raise RuntimeError('test gate deadline')
        time.sleep(.005)
try:
    result=getattr(ledger,operation)(principal_for_profile(profile),json.loads(value))
except CostError as exc:
    result={'error':exc.code}
if mode == 'lost':
    Path(ready).write_text('committed')
    deadline=time.monotonic()+10
    while not Path(gate).exists():
        if time.monotonic()>=deadline: raise RuntimeError('test gate deadline')
        time.sleep(.005)
print(json.dumps(result),flush=True)
'''


class CostLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "ledger.sqlite"
        initialize(self.path)
        self.ledger = CostLedger(self.path)
        self.operator = principal_for_profile("alpha-operator")
        self.other_operator = principal_for_profile("alpha-operator2")
        self.auditor = principal_for_profile("alpha-auditor")
        self.beta = principal_for_profile("beta-operator")
        self.beta_auditor = principal_for_profile("beta-auditor")
        self.observer = principal_for_profile("alpha-observer")

    def error(self, code, operation, *args, **kwargs):
        with self.assertRaises(CostError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def counts(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("usage", "statements"))

    def test_append_reopen_replay_keeps_first_actor_and_receipt(self):
        first = self.ledger.append(self.operator, event())
        self.assertIs(first["replayed"], False)
        self.assertEqual(first["charge"], "0.005")
        self.assertEqual(first["first_actor"], self.operator.subject)
        replay = CostLedger(self.path).append(self.other_operator, event())
        self.assertEqual(replay, {**first, "replayed": True})
        self.assertEqual(self.counts(), (1, 0))

    def test_changed_immutable_fields_conflict_without_mutation(self):
        original = self.ledger.append(self.operator, event())
        for changes in ({"request_id": "another-request"}, {"metric": "input_tokens"},
                        {"quantity": 2}, {"occurred_at": "2026-01-02T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.error("idempotency_conflict", self.ledger.append, self.operator, event(**changes))
                self.assertEqual(self.ledger.get(self.auditor, "event-1"), {**original, "replayed": True})
                self.assertEqual(self.counts(), (1, 0))
        self.error("invalid_input", self.ledger.append, self.operator, event(rate_version="fictional-v2"))

    def test_caller_mutation_cannot_change_stored_input_or_receipt(self):
        value = event()
        receipt = self.ledger.append(self.operator, value)
        value["quantity"] = 999
        receipt["event"]["quantity"] = 888
        stored = self.ledger.get(self.auditor, "event-1")
        self.assertEqual(stored["event"]["quantity"], 1)
        self.assertEqual(stored["charge"], "0.005")

    def test_tenant_colliding_ids_and_pagination_do_not_leak_other_activity(self):
        alpha = self.ledger.append(self.operator, event())
        for i in range(4):
            self.ledger.append(self.beta, event(f"beta-{i}", quantity=20))
        beta = self.ledger.append(self.beta, event(quantity=10))
        second = self.ledger.append(self.operator, event("event-2"))
        self.assertEqual((alpha["ordinal"], second["ordinal"]), (1, 2))
        self.assertNotEqual(alpha["charge"], beta["charge"])
        page = self.ledger.list(self.auditor, limit=1)
        self.assertEqual([v["event"]["event_id"] for v in page["events"]], ["event-1"])
        self.assertEqual(page["next_after"], 1)
        last = self.ledger.list(self.auditor, after=page["next_after"], limit=1)
        self.assertEqual([v["event"]["event_id"] for v in last["events"]], ["event-2"])
        self.assertIsNone(last["next_after"])
        self.error("not_found", self.ledger.get, self.auditor, "beta-0")
        self.error("not_found", self.ledger.get, self.auditor, "unknown-id")
        encoded = canonical(self.ledger.summary(self.auditor, START, END))
        self.assertNotIn("beta", encoded)
        self.assertEqual(self.ledger.summary(self.auditor, START, END)["exact_total"], "0.01")

    def test_role_matrix_and_body_overrides_are_rejected(self):
        self.ledger.append(self.operator, event())
        for principal in (self.auditor, self.observer):
            with self.subTest(operation="append", role=principal.role):
                self.error("forbidden", self.ledger.append, principal, event("new"))
        for principal in (self.operator, self.observer):
            with self.subTest(operation="reconcile", role=principal.role):
                self.error("forbidden", self.ledger.reconcile, principal, statement())
        for operation, args in ((self.ledger.get, ("event-1",)),
                                (self.ledger.list, ()),
                                (self.ledger.summary, (START, END))):
            self.error("forbidden", operation, self.observer, *args)
        self.error("invalid_input", self.ledger.append, self.operator, event(tenant_id="beta"))
        self.error("invalid_input", self.ledger.append, self.operator, event(role="auditor"))
        self.assertEqual(self.counts(), (1, 0))

    def test_period_is_half_open_and_requires_canonical_utc(self):
        self.ledger.append(self.operator, event("before", occurred_at="2025-12-31T23:59:59Z"))
        self.ledger.append(self.operator, event("start", quantity=2))
        self.ledger.append(self.operator, event("end", occurred_at=END, quantity=100))
        result = self.ledger.summary(self.auditor, START, END)
        self.assertEqual((result["events"], result["exact_total"]), (1, "0.01"))
        for start, end in ((START, START), (END, START),
                           ("2026-01-01T00:00:00+00:00", END),
                           ("2026-01-01T00:00:00.000Z", END),
                           ("2026-02-30T00:00:00Z", END)):
            with self.subTest(start=start, end=end):
                self.error("invalid_input", self.ledger.summary, self.auditor, start, end)

    def test_exact_aggregate_uses_local_decimal_context(self):
        with localcontext() as caller:
            caller.prec = 2
            caller.rounding = ROUND_DOWN
            caller.traps[Inexact] = True
            caller.flags[Inexact] = True
            before = (caller.prec, caller.rounding, dict(caller.traps), dict(caller.flags))
            self.ledger.append(self.operator, event("tiny-1"))
            self.ledger.append(self.operator, event("tiny-2"))
            self.assertEqual(self.ledger.summary(self.auditor, START, END), {
                "tenant_id": "alpha", "currency": "USD", "start": START, "end": END,
                "events": 2, "exact_total": "0.01", "settled_total": "0.01", "rounding_delta": "0"})
            self.error("idempotency_conflict", self.ledger.append, self.operator, event("tiny-1", quantity=2))
            self.assertEqual(before, (caller.prec, caller.rounding, dict(caller.traps), dict(caller.flags)))

    def test_statement_replay_freezes_snapshot_after_more_usage(self):
        self.ledger.append(self.operator, event())
        first = self.ledger.reconcile(self.auditor, statement())
        self.assertIs(first["result"]["matched"], True)
        self.assertEqual(first["watermark"], 1)
        self.ledger.append(self.beta, event("foreign", quantity=100))
        self.ledger.append(self.operator, event("event-2"))
        replay = CostLedger(self.path).reconcile(self.auditor, statement())
        self.assertEqual(replay, {**first, "replayed": True})
        later = self.ledger.reconcile(self.auditor, statement("later"))
        self.assertEqual(later["watermark"], 2)
        self.assertEqual(later["result"]["missing_events"], ["event-2"])
        self.assertEqual(later["result"]["settled_total"], "0.01")
        self.assertNotEqual(later["snapshot_sha256"], first["snapshot_sha256"])

    def test_statement_ids_are_tenant_scoped_and_conflicts_are_atomic(self):
        self.ledger.append(self.operator, event())
        self.ledger.append(self.beta, event(quantity=2))
        alpha = self.ledger.reconcile(self.auditor, statement())
        beta = self.ledger.reconcile(self.beta_auditor,
            statement(lines=[{"event_id": "event-1", "amount": "0.01"}], settled="0.01"))
        self.assertIs(alpha["result"]["matched"], True)
        self.assertIs(beta["result"]["matched"], True)
        self.assertNotEqual(alpha["receipt_id"], beta["receipt_id"])
        changed = statement(lines=[{"event_id": "event-1", "amount": "0.006"}])
        self.error("idempotency_conflict", self.ledger.reconcile, self.auditor, changed)
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.ledger.reconcile(self.auditor, statement()), {**alpha, "replayed": True})

    def test_reconciliation_retains_distinct_authored_anomalies(self):
        self.ledger.append(self.operator, event(quantity=2))
        self.ledger.append(self.operator, event("event-2"))
        clean = [{"event_id": "event-1", "amount": "0.01"},
                 {"event_id": "event-2", "amount": "0.005"}]
        cases = [
            ("clean", clean, "USD", "0.02", {"matched": True}),
            ("missing", clean[:1], "USD", "0.02", {"missing_events": ["event-2"]}),
            ("extra", clean + [{"event_id": "absent", "amount": "0.005"}], "USD", "0.02", {"extra_events": ["absent"]}),
            ("amount", [clean[0], {"event_id": "event-2", "amount": "0.006"}], "USD", "0.02",
             {"amount_mismatches": [{"event_id": "event-2", "expected": "0.005", "declared": "0.006"}]}),
            ("total", clean, "USD", "0.03", {"settled_total_mismatch": True}),
            ("currency", clean, "EUR", "0.02", {"currency_mismatch": True, "amount_mismatches": [], "settled_total_mismatch": None}),
        ]
        for key, lines, currency, settled, expected in cases:
            with self.subTest(case=key):
                receipt = self.ledger.reconcile(self.auditor,
                    statement(key, lines=lines, settled=settled, currency=currency))
                for field, value in expected.items():
                    self.assertEqual(receipt["result"][field], value)
                self.assertEqual(receipt["result"]["currency"], "USD")
                self.assertEqual(receipt["result"]["exact_total"], "0.015")
                self.assertEqual(receipt["result"]["settled_total"], "0.02")
                self.assertEqual(receipt["result"]["rounding_delta"], "0.005")
                self.assertEqual(receipt["result"]["matched"], key == "clean")
        self.assertEqual(self.counts(), (2, len(cases)))

    def test_duplicate_statement_lines_leave_no_receipt(self):
        self.ledger.append(self.operator, event())
        line = {"event_id": "event-1", "amount": "0.005"}
        self.error("duplicate_statement_line", self.ledger.reconcile, self.auditor,
                   statement(lines=[line, deepcopy(line)]))
        self.assertEqual(self.counts(), (1, 0))

    def test_catalog_binding_and_unrelated_database_are_not_adopted(self):
        card = json.loads(files("enterprise_ai_lab.costs").joinpath("data/rates-v1.json").read_text())
        card["rates"]["embedding_tokens"]["unit_price"] = "0.006"
        before = self.path.read_bytes()
        self.error("storage_corrupt", CostLedger, self.path, card=card)
        self.assertEqual(before, self.path.read_bytes())
        unrelated = self.directory / "unrelated.sqlite"
        with closing(sqlite3.connect(unrelated)) as connection, connection:
            connection.execute("CREATE TABLE owner_record (value TEXT)")
            connection.execute("INSERT INTO owner_record VALUES ('preserve-me')")
        unrelated.chmod(0o600)
        before = unrelated.read_bytes()
        self.error("storage_corrupt", CostLedger, unrelated)
        self.assertEqual(before, unrelated.read_bytes())
        self.error("storage_unavailable", initialize, unrelated)
        self.assertEqual(before, unrelated.read_bytes())

    def test_usage_and_statement_records_reject_updates_and_deletes(self):
        self.ledger.append(self.operator, event())
        self.ledger.reconcile(self.auditor, statement())
        for table in ("usage", "statements", "metadata"):
            for verb in ("UPDATE", "DELETE"):
                with self.subTest(table=table, verb=verb), closing(sqlite3.connect(self.path)) as connection, connection:
                    sql = f"DELETE FROM {table}" if verb == "DELETE" else f"UPDATE {table} SET " + (
                        "schema_version=schema_version" if table == "metadata" else "record=record")
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(sql)
        self.assertEqual(self.counts(), (1, 1))

    def rewrite_record(self, table, key_field, key, mutate, *, repair_digest):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            original, old_digest = connection.execute(
                f"SELECT record,digest FROM {table} WHERE tenant_id=? AND {key_field}=?", ("alpha", key)).fetchone()
            value = json.loads(original)
            self.assertEqual(checksum(value), old_digest)
            mutate(value)
            triggers = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall()
            for name, _ in triggers:
                connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            connection.execute(f"UPDATE {table} SET record=?,digest=? WHERE tenant_id=? AND {key_field}=?",
                               (canonical(value), checksum(value) if repair_digest else old_digest, "alpha", key))
            for _, sql in triggers:
                connection.execute(sql)

    def test_row_digest_and_recomputed_wrong_charge_are_both_rejected(self):
        for repair_digest in (False, True):
            with self.subTest(repair_digest=repair_digest):
                path = self.directory / ("tamper-" + str(repair_digest) + ".sqlite")
                initialize(path)
                old_path, self.path = self.path, path
                try:
                    ledger = CostLedger(path)
                    ledger.append(self.operator, event())
                    self.rewrite_record("usage", "event_id", "event-1",
                                        lambda v: v.update(charge="999"), repair_digest=repair_digest)
                    self.error("storage_corrupt", ledger.summary, self.auditor, START, END)
                    self.error("storage_corrupt", ledger.append, self.operator, event("another"))
                    self.assertEqual(self.counts(), (1, 0))
                finally:
                    self.path = old_path

    def test_reconciliation_result_tampering_is_recomputed(self):
        self.ledger.append(self.operator, event())
        self.ledger.reconcile(self.auditor, statement())
        self.rewrite_record("statements", "statement_id", "statement-1",
                            lambda v: v["result"].update(exact_total="999"), repair_digest=True)
        self.error("storage_corrupt", self.ledger.reconcile, self.auditor, statement())
        self.error("storage_corrupt", self.ledger.summary, self.auditor, START, END)

    def test_missing_append_only_trigger_and_invalid_bytes_fail_closed(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            name = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone()[0]
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        before = self.path.read_bytes()
        self.error("storage_corrupt", CostLedger, self.path)
        self.assertEqual(before, self.path.read_bytes())
        corrupted = self.directory / "corrupted.sqlite"
        corrupted.write_bytes(b"not-a-database-private-fixture")
        corrupted.chmod(0o600)
        before = corrupted.read_bytes()
        with self.assertRaises(CostError):
            CostLedger(corrupted)
        self.assertEqual(before, corrupted.read_bytes())

    def test_rehashed_boolean_result_cannot_be_replaced_with_equal_integer(self):
        for field in ("matched", "currency_mismatch", "settled_total_mismatch"):
            with self.subTest(field=field):
                path = self.directory / ("bool-" + field + ".sqlite")
                initialize(path)
                old_path, self.path = self.path, path
                try:
                    ledger = CostLedger(path)
                    ledger.append(self.operator, event())
                    ledger.reconcile(self.auditor, statement())
                    self.rewrite_record("statements", "statement_id", "statement-1",
                        lambda v: v["result"].update({field: int(v["result"][field])}), repair_digest=True)
                    self.error("storage_corrupt", ledger.summary, self.auditor, START, END)
                finally:
                    self.path = old_path

    def test_every_reopened_writer_applies_actual_sqlite_page_limit(self):
        observations = []
        real_connect = sqlite3.connect

        class ObservedConnection(sqlite3.Connection):
            def execute(connection, sql, *args, **kwargs):
                if sql.upper().startswith(("INSERT INTO USAGE ", "INSERT INTO STATEMENTS ")):
                    limit = sqlite3.Connection.execute(connection, "PRAGMA max_page_count").fetchone()[0]
                    page_size = sqlite3.Connection.execute(connection, "PRAGMA page_size").fetchone()[0]
                    observations.append((limit, page_size))
                return sqlite3.Connection.execute(connection, sql, *args, **kwargs)

        def connect(*args, **kwargs):
            kwargs["factory"] = ObservedConnection
            return real_connect(*args, **kwargs)

        with patch("enterprise_ai_lab.costs.ledger.sqlite3.connect", side_effect=connect):
            CostLedger(self.path).append(self.operator, event())
            CostLedger(self.path).reconcile(self.auditor, statement())
            CostLedger(self.path).append(self.operator, event("event-2"))
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(limit * page_size <= 32 * 1024 * 1024 for limit, page_size in observations), observations)

    def test_actual_sqlite_writer_lock_is_bounded_and_rollback_preserves_rows(self):
        self.ledger.append(self.operator, event())
        blocker = sqlite3.connect(self.path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            self.error("resource_busy", self.ledger.append, self.operator, event("locked"))
            self.assertLess(time.monotonic() - started, 2)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(self.counts(), (1, 0))
        self.assertIs(self.ledger.append(self.operator, event("locked"))["replayed"], False)

    def start_child(self, index, value, *, mode="race", profile="alpha-operator", operation="append"):
        ready, gate = self.directory / f"ready-{index}", self.directory / "gate"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(costs_package.__file__).resolve().parents[2])
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(self.path), str(ready),
                                    str(gate), profile, canonical(value), mode, operation],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=environment, cwd=self.directory)
        self.addCleanup(self.reap_child, process)
        return process, ready

    @staticmethod
    def reap_child(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def wait_ready(self, children):
        deadline = time.monotonic() + 10
        while not all(path.exists() for _, path in children):
            if any(process.poll() is not None for process, _ in children):
                details = [process.communicate(timeout=2) for process, _ in children if process.poll() is not None]
                self.fail("test child exited before barrier: " + repr(details))
            if time.monotonic() >= deadline:
                self.fail("test child barrier deadline")
            time.sleep(.005)

    def test_actual_process_duplicate_writers_commit_only_once(self):
        children = [self.start_child(0, event()),
                    self.start_child(1, event(), profile="alpha-operator2")]
        self.wait_ready(children)
        (self.directory / "gate").write_text("go")
        results = []
        for process, _ in children:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        accepted = [r for r in results if "error" not in r]
        self.assertEqual(sum(not r["replayed"] for r in accepted), 1)
        self.assertTrue(all(r.get("error") in (None, "resource_busy") for r in results))
        original = accepted[0]
        replay = CostLedger(self.path).append(self.other_operator, event())
        self.assertEqual(replay["receipt_id"], original["receipt_id"])
        self.assertEqual(self.counts(), (1, 0))

    def test_actual_process_conflicting_writers_preserve_one_winner(self):
        children = [self.start_child(0, event(quantity=1)), self.start_child(1, event(quantity=2))]
        self.wait_ready(children)
        (self.directory / "gate").write_text("go")
        results = []
        for process, _ in children:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sum("error" not in r for r in results), 1)
        self.assertTrue(all(r.get("error") in (None, "idempotency_conflict", "resource_busy") for r in results))
        stored = self.ledger.get(self.auditor, "event-1")
        loser_quantity = 2 if stored["event"]["quantity"] == 1 else 1
        self.error("idempotency_conflict", self.ledger.append, self.operator, event(quantity=loser_quantity))
        self.assertEqual(self.counts(), (1, 0))

    def test_nested_reads_preserve_managed_and_unmanaged_posix_writer_locks(self):
        child = r"""
import json, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=.1, isolation_level=None)
try:
    connection.execute('BEGIN IMMEDIATE')
    print(json.dumps({'acquired': True, 'busy': False}))
except sqlite3.OperationalError as exc:
    code = getattr(exc, 'sqlite_errorcode', None)
    busy = (type(code) is int and code & 255 in (5, 6)) or exc.args in (
        ('database is locked',), ('database table is locked',))
    print(json.dumps({'acquired': False, 'busy': busy}))
finally:
    connection.rollback()
    connection.close()
"""

        def foreign_writer():
            result = subprocess.run([sys.executable, "-c", child, str(self.path)],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            return json.loads(result.stdout)

        for managed in (True, False):
            with self.subTest(managed=managed):
                context = (self.ledger._transaction(write=True) if managed else
                           closing(sqlite3.connect(self.path, isolation_level=None)))
                with context as outer:
                    if not managed:
                        outer.execute("BEGIN IMMEDIATE")
                    try:
                        self.assertEqual(foreign_writer(), {"acquired": False, "busy": True})
                        other = CostLedger(self.path)
                        self.assertEqual(other.summary(self.auditor, START, END)["events"], 0)
                        self.assertTrue(outer.in_transaction)
                        self.assertEqual(foreign_writer(), {"acquired": False, "busy": True})
                        self.assertTrue(outer.in_transaction)
                    finally:
                        if not managed:
                            outer.rollback()
                self.assertEqual(foreign_writer(), {"acquired": True, "busy": False})

    def test_actual_event_capacity_is_per_tenant_and_existing_replay_still_works(self):
        first = self.ledger.append(self.operator, event())
        stored = {key: value for key, value in first.items() if key != "replayed"}
        rows = []
        for ordinal in range(2, 1001):
            value = deepcopy(stored)
            value.update(receipt_id=f"{ordinal:032x}", ordinal=ordinal,
                         event=event(f"event-{ordinal}", request_id=f"request-{ordinal}"))
            value["event_sha256"] = checksum({"tenant_id": "alpha", "event": value["event"],
                                              "rate_card_sha256": value["rate_card_sha256"]})
            rows.append(("alpha", value["event"]["event_id"], ordinal, canonical(value), checksum(value)))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany("INSERT INTO usage VALUES (?,?,?,?,?)", rows)
        self.error("capacity_exceeded", self.ledger.append, self.operator, event("overflow"))
        self.assertEqual(self.counts(), (1000, 0))
        replay = self.ledger.append(self.other_operator, event())
        self.assertEqual(replay["receipt_id"], first["receipt_id"])
        self.assertIs(replay["replayed"], True)
        total = self.ledger.summary(self.auditor, START, END)
        self.assertEqual((total["events"], total["exact_total"], total["settled_total"]), (1000, "5", "5.00"))
        self.assertIs(self.ledger.append(self.beta, event())["replayed"], False)
        self.assertEqual(self.counts(), (1001, 0))

    def test_actual_statement_capacity_preserves_original_receipts(self):
        self.ledger.append(self.operator, event())
        first = self.ledger.reconcile(self.auditor, statement("statement-0"))
        for number in range(1, 100):
            self.ledger.reconcile(self.auditor, statement(f"statement-{number}"))
        self.error("capacity_exceeded", self.ledger.reconcile, self.auditor, statement("overflow"))
        replay = self.ledger.reconcile(self.auditor, statement("statement-0"))
        self.assertEqual(replay, {**first, "replayed": True})
        self.assertEqual(self.counts(), (1, 100))

    def test_concurrent_append_and_statement_produce_one_consistent_snapshot(self):
        self.ledger.append(self.operator, event())
        children = [self.start_child(0, event("event-2")),
                    self.start_child(1, statement(), profile="alpha-auditor", operation="reconcile")]
        self.wait_ready(children)
        (self.directory / "gate").write_text("go")
        for process, _ in children:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            result = json.loads(stdout)
            self.assertIn(result.get("error"), (None, "resource_busy"))
        self.ledger.append(self.operator, event("event-2"))
        receipt = self.ledger.reconcile(self.auditor, statement())
        if receipt["watermark"] == 1:
            self.assertEqual(receipt["result"]["exact_total"], "0.005")
            self.assertEqual(receipt["result"]["missing_events"], [])
            self.assertIs(receipt["result"]["matched"], True)
        else:
            self.assertEqual(receipt["watermark"], 2)
            self.assertEqual(receipt["result"]["exact_total"], "0.01")
            self.assertEqual(receipt["result"]["missing_events"], ["event-2"])
            self.assertIs(receipt["result"]["matched"], False)
        self.assertEqual(self.counts(), (2, 1))

    def test_killed_client_after_commit_replays_original_receipt(self):
        process, ready = self.start_child(0, event(), mode="lost")
        self.wait_ready([(process, ready)])
        before = self.ledger.get(self.auditor, "event-1")
        process.kill()
        stdout, _ = process.communicate(timeout=3)
        self.assertEqual(stdout, "")
        self.assertIsNotNone(process.returncode)
        replay = CostLedger(self.path).append(self.other_operator, event())
        self.assertEqual(replay, before)
        self.assertEqual(self.counts(), (1, 0))


if __name__ == "__main__":
    unittest.main()
