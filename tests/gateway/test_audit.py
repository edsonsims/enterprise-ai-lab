"""Actual durable audit chain, metadata bounds and source-file protection."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from enterprise_ai_lab.mcp_gateway.audit import GatewayAudit, TRIGGERS, canonical
from enterprise_ai_lab.mcp_gateway.store import GatewayError, initialize_fixture


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit.sqlite"
        self.audit = GatewayAudit(self.path)
        self.metadata = dict(call_id="1" * 32, subject="local:alpha-reader", tenant_id="alpha",
                             operation="get_work_order", input_sha256="2" * 64)

    def test_metadata_chain_is_durable_and_locally_verifiable(self):
        self.assertFalse(self.path.exists())  # Constructor performs no writes.
        first = self.audit.append(**self.metadata, phase="intent")
        last = self.audit.append(**self.metadata, phase="outcome", outcome="success",
                                 output_sha256="3" * 64, row_count=1)
        self.assertEqual([first, last], GatewayAudit(self.path).events())
        previous = "0" * 64
        for event in [first, last]:
            payload = {k: v for k, v in event.items() if k not in ("previous_sha256", "event_sha256")}
            expected = hashlib.sha256((previous + "\n" + canonical(payload)).encode()).hexdigest()
            self.assertEqual(event["event_sha256"], expected)
            self.assertEqual(event["previous_sha256"], previous)
            previous = expected

    def test_append_only_triggers_reject_updates_and_deletes(self):
        self.audit.append(**self.metadata, phase="intent")
        with sqlite3.connect(self.path) as connection:
            for statement in ("UPDATE gateway_audit SET event_json='{}'", "DELETE FROM gateway_audit"):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
        self.assertEqual(len(self.audit.events()), 1)

    def test_corrupt_row_or_removed_protection_fails_verification(self):
        self.audit.append(**self.metadata, phase="intent")
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TRIGGER gateway_audit_no_update")
        with self.assertRaises(GatewayError) as caught: self.audit.events()
        self.assertEqual(caught.exception.code, "audit_unavailable")
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE gateway_audit SET previous_sha256=?", ("9" * 64,))
            connection.execute(TRIGGERS["gateway_audit_no_update"])
        with self.assertRaises(GatewayError): self.audit.events()

    def test_existing_business_schema_is_refused_without_mutating_its_bytes(self):
        source = self.path.parent / "business.sqlite"
        initialize_fixture(source); before = source.read_bytes()
        wrong = GatewayAudit(source)
        with self.assertRaises(GatewayError) as caught: wrong.events()
        self.assertEqual(caught.exception.code, "audit_unavailable")
        self.assertEqual(before, source.read_bytes())
        self.assertFalse(Path(str(source) + "-wal").exists())
        self.assertFalse(Path(str(source) + "-shm").exists())

    def test_outcome_requires_exact_intent_and_cannot_be_duplicated(self):
        outcome = dict(phase="outcome", outcome="success", output_sha256="3" * 64, row_count=1)
        with self.assertRaises(GatewayError): self.audit.append(**self.metadata, **outcome)
        self.assertEqual(self.audit.events(), [])
        self.audit.append(**self.metadata, phase="intent")
        with self.assertRaises(GatewayError): self.audit.append(**self.metadata, phase="intent")
        changed = {**self.metadata, "tenant_id": "beta"}
        with self.assertRaises(GatewayError): self.audit.append(**changed, **outcome)
        self.audit.append(**self.metadata, **outcome)
        with self.assertRaises(GatewayError): self.audit.append(**self.metadata, **outcome)
        self.assertEqual(len(self.audit.events()), 2)

    def test_event_bound_fails_closed_without_truncating_history(self):
        self.audit.append(**self.metadata, phase="intent")
        self.audit.append(**self.metadata, phase="outcome", outcome="error", error_code="not_found",
                          output_sha256="3" * 64, row_count=0)
        with mock.patch("enterprise_ai_lab.mcp_gateway.audit.MAX_EVENTS", 2):
            with self.assertRaises(GatewayError): self.audit.append(**{**self.metadata, "call_id": "4" * 32}, phase="intent")
            self.assertEqual(len(self.audit.events()), 2)

    def test_invalid_metadata_is_rejected_without_persisting_values(self):
        for changed in ({"subject": "raw password with spaces"}, {"operation": "arbitrary sql"},
                        {"input_sha256": "not-a-digest"}, {"input_digest_kind": "unlabelled"}):
            with self.subTest(changed=changed), self.assertRaises(GatewayError):
                self.audit.append(**{**self.metadata, **changed}, phase="intent")
        self.assertEqual(self.audit.events(), [])
