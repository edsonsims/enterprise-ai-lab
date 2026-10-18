"""Opt-in actual Temporal/OAuth/HTTP process and persistence integration.

The shared demo starts pinned real local processes and exports their histories.
There are no HTTP, identity, workflow or activity doubles in this suite. A role-
bearing fixture client still does not establish an actual human review event.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest


_TOKEN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|Bearer\s+[A-Za-z0-9_.-]{20,}")


def readable_payloads(value):
    """Inspect exported protobuf payload bytes as well as the JSON envelope."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "data" and isinstance(child, str):
                try:
                    yield base64.b64decode(child, validate=True).decode("utf-8")
                except (ValueError, UnicodeError, binascii.Error):
                    pass
            yield from readable_payloads(child)
    elif isinstance(value, list):
        for child in value:
            yield from readable_payloads(child)


@unittest.skipUnless(os.environ.get("RUN_APPROVAL_INTEGRATION") == "1",
                     "Actual Temporal integration requires explicit RUN_APPROVAL_INTEGRATION=1.")
class ActualApprovalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from enterprise_ai_lab.approval.integration import run_demo

        # Enabling integration without the declared local runtime must fail,
        # rather than silently turn a missing real dependency into a pass.
        cls.cli = Path(os.environ["TEMPORAL_CLI"])
        cls.identity_dir = Path(os.environ["IDENTITY_LAB_DIR"])
        cls.node = os.environ["NODE_BINARY"]
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.out = cls.directory / "report.json"
        cls.report = asyncio.run(run_demo(
            temporal_cli=cls.cli, identity_dir=cls.identity_dir, node=cls.node,
            work_dir=cls.directory / "runtime", out=cls.out,
        ))

    def test_real_process_crash_and_persistence_evidence(self):
        durability = self.report["durability"]
        self.assertIs(durability["worker_killed_after_commit"], True)
        self.assertIs(durability["server_restarted"], True)
        self.assertIs(durability["edge_restarted"], True)
        self.assertEqual(durability["notification_rows_for_primary"], 1)
        self.assertEqual(durability["total_notifications"], 2)
        self.assertIs(durability["pending_request_preserved"], True)
        self.assertIs(durability["expiry_while_worker_absent"], True)
        self.assertIs(durability["before_crash_activity_completed"], False)

    def test_http_denials_and_terminal_replay_cases_are_preserved(self):
        cases = self.report["observations"]
        by_name = {item["case"]: item for item in cases}
        self.assertEqual(len(by_name), len(cases))
        expected = {
            "request-created": 200,
            "exact-create-replay": 200,
            "changed-create-conflict": 409,
            "forged-token-denied": 401,
            "rag-audience-denied": 401,
            "cross-tenant-denied": 403,
            "wrong-role-denied": 403,
            "missing-scope-denied": 403,
            "tampered-snapshot-denied": 409,
            "body-actor-spoof-denied": 422,
            "unknown-request": 404,
            "self-approval-denied": 409,
            "pending-after-server-worker-edge-restart": 200,
            "approve-before-effect-ack": 200,
            "published-once-after-worker-kill-and-server-restart": 200,
            "exact-decision-replay": 200,
            "changed-command-id-payload-conflict": 409,
            "changed-actor-same-command-conflict": 409,
            "changed-digest-same-command-conflict": 409,
            "same-request-id-other-tenant": 200,
            "beta-approved": 200,
            "rejected": 200,
            "expired-while-worker-absent": 200,
            "late-decision-denied": 409,
            "published-after-edge-restart": 200,
        }
        self.assertTrue(set(expected).issubset(by_name))
        for name, status in expected.items():
            with self.subTest(case=name):
                self.assertEqual(by_name[name]["http_status"], status)
                if status >= 400:
                    self.assertEqual(set(by_name[name]["response"]), {"error"})

    def test_identity_content_and_receipt_survive_all_process_restarts(self):
        cases = {item["case"]: item["response"] for item in self.report["observations"]}
        original = cases["request-created"]
        self.assertEqual(original["status"], "pending")
        self.assertEqual(original["request"]["requested_by"], "requester-alpha")
        for name in ["exact-create-replay", "pending-after-server-worker-edge-restart",
                     "approve-before-effect-ack", "published-once-after-worker-kill-and-server-restart",
                     "exact-decision-replay", "published-after-edge-restart"]:
            with self.subTest(case=name):
                self.assertEqual(cases[name]["request"], original["request"])
        published = cases["published-once-after-worker-kill-and-server-restart"]
        self.assertEqual((published["status"], published["publication_status"]), ("approved", "published"))
        self.assertEqual(published["receipt"]["command"]["actor_id"], "approver-alpha")
        self.assertEqual(cases["exact-decision-replay"]["receipt"], published["receipt"])
        self.assertEqual(cases["published-after-edge-restart"]["effect"], published["effect"])
        self.assertEqual(cases["expired-while-worker-absent"]["status"], "expired")
        self.assertEqual(cases["expired-while-worker-absent"]["publication_status"], "not_applicable")
        self.assertIsNone(cases["expired-while-worker-absent"]["receipt"])
        self.assertIsNone(cases["expired-while-worker-absent"]["effect"])

    def test_same_request_id_remains_independent_across_tenants_and_effects(self):
        cases = {item["case"]: item["response"] for item in self.report["observations"]}
        alpha = cases["request-created"]["request"]
        beta = cases["same-request-id-other-tenant"]["request"]
        self.assertEqual(alpha["request_id"], beta["request_id"])
        self.assertEqual((alpha["tenant_id"], beta["tenant_id"]), ("alpha", "beta"))
        self.assertNotEqual(alpha["snapshot_sha256"], beta["snapshot_sha256"])
        effects = self.report["local_notifications"]
        self.assertEqual(len(effects), 2)
        self.assertEqual({item["tenant_id"] for item in effects}, {"alpha", "beta"})
        self.assertEqual({item["request_id"] for item in effects}, {alpha["request_id"]})
        self.assertEqual(len({item["effect_key"] for item in effects}), 2)

    def test_exported_histories_are_actual_files_with_recorded_hashes(self):
        histories = self.report["histories"]
        self.assertGreaterEqual(len(histories), 2)
        self.assertEqual(len({item["filename"] for item in histories}), len(histories))
        for item in histories:
            with self.subTest(request_id=item["request_id"]):
                path = self.out.parent / item["filename"]
                raw = path.read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
                history = json.loads(raw)
                events = history["events"]
                self.assertEqual(len(events), item["event_count"])
                self.assertGreater(len(events), 1)

    def test_every_recorded_history_replays_without_nondeterminism(self):
        replay = self.report["replay"]
        self.assertIs(replay["all_passed"], True)
        self.assertEqual(replay["histories"], len(self.report["histories"]))
        self.assertIs(replay["no_new_local_effects"], True)

    def test_primary_history_records_a_retried_activity_and_one_completion(self):
        item = next(item for item in self.report["histories"]
                    if item["tenant_id"] == "alpha" and item["request_id"] == "publish-study-note")
        events = json.loads((self.out.parent / item["filename"]).read_text())["events"]
        started = [event["activityTaskStartedEventAttributes"] for event in events
                   if "activityTaskStartedEventAttributes" in event]
        self.assertTrue(any(int(event.get("attempt", 1)) >= 2 for event in started))
        completed = [event for event in events if event["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED"]
        self.assertEqual(len(completed), 1)

    def test_report_and_decoded_history_payloads_exclude_bearer_tokens(self):
        self.assertFalse(bool(_TOKEN.search(json.dumps(self.report))),
                         "The report contains token-like material.")
        for item in self.report["histories"]:
            path = self.out.parent / item["filename"]
            raw = path.read_text()
            history = json.loads(raw)
            self.assertFalse(bool(_TOKEN.search(raw)), "An exported history contains token-like material.")
            for text in readable_payloads(history):
                self.assertFalse(bool(_TOKEN.search(text)), "A history payload contains token-like material.")

    def test_artifact_is_reproducible_report_and_not_human_approval_evidence(self):
        self.assertEqual(json.loads(self.out.read_text()), self.report)
        self.assertIs(self.report["human_review_performed"], False)


if __name__ == "__main__":
    unittest.main()
