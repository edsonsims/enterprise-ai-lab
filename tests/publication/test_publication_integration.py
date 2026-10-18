"""Opt-in real Temporal/OAuth/ONNX/Qdrant/native PDF/OCR process tests."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest


def decoded_payloads(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "data" and isinstance(child, str):
                try:
                    yield base64.b64decode(child, validate=True).decode()
                except (ValueError, UnicodeError):
                    pass
            yield from decoded_payloads(child)
    elif isinstance(value, list):
        for child in value:
            yield from decoded_payloads(child)


@unittest.skipUnless(os.environ.get("RUN_PUBLICATION_INTEGRATION") == "1",
    "Enable RUN_PUBLICATION_INTEGRATION=1 with the actual pinned local runtimes.")
class ActualPublicationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from enterprise_ai_lab.publication.integration import run_demo
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        cls.out = directory / "demo.json"
        cls.report = asyncio.run(run_demo(temporal_cli=Path(os.environ["TEMPORAL_CLI"]),
            identity_dir=Path(os.environ["IDENTITY_LAB_DIR"]), node=os.environ["NODE_BINARY"],
            model_dir=Path(os.environ["RAG_MODEL_DIR"]),
            manifest_path=Path(__file__).resolve().parents[2] / "public-data/pdf-v1/ingestion.json",
            work_dir=directory / "runtime", out=cls.out))
        cls.cases = {item["case"]: item for item in cls.report["observations"]}

    def body(self, case):
        return self.cases[case]["response"]

    def test_unapproved_candidates_are_absent_from_actual_vector_index(self):
        self.assertEqual(self.report["index"]["before_any_approval"], 0)
        for case in ("staged-not-searchable", "rejected-not-searchable", "expired-not-searchable",
                     "pending-not-searchable", "pending-after-restart-not-searchable"):
            with self.subTest(case=case):
                self.assertTrue(self.body(case)["abstained"])
                self.assertEqual(self.body(case)["citations"], [])

    def test_signed_tenant_and_role_boundaries_precede_retrieval_and_publication(self):
        expected = {"forged-approval-denied": 401, "rag-audience-denied": 401,
            "cross-tenant-approval-denied": 403, "wrong-role-denied": 403,
            "missing-scope-denied": 403, "tampered-snapshot-denied": 409,
            "spoofed-actor-denied": 422, "foreign-candidate-denied": 404,
            "candidate-missing": 404, "cross-tenant-query-denied": 403}
        for case, code in expected.items():
            with self.subTest(case=case):
                self.assertEqual(self.cases[case]["http_status"], code)
                self.assertEqual(set(self.body(case)), {"error"})
        self.assertTrue(self.body("ocr-reader-denied")["abstained"])
        self.assertTrue(self.body("beta-still-unpublished")["abstained"])
        self.assertFalse(self.body("beta-ocr-reader")["abstained"])

    def test_approved_native_and_ocr_citations_keep_original_artifact_and_page_identity(self):
        for case, method, source_page, artifact_page in [
            ("approved-native-reader", "native", 15, 15),
            ("approved-ocr-analyst", "ocr", 25, 1),
            ("beta-ocr-reader", "ocr", 25, 1)]:
            citations = self.body(case)["citations"]
            self.assertTrue(citations)
            for citation in citations:
                with self.subTest(case=case):
                    span = citation["provenance"]
                    self.assertEqual((span["method"], span["source_page"], span["artifact_page"]),
                        (method, source_page, artifact_page))
                    self.assertEqual(span["end"] - span["start"], len(citation["text"]))
                    self.assertIn(citation["quote"], citation["text"])
                    self.assertTrue(span["source_uri"].startswith("https://"))
                    if method == "ocr":
                        self.assertTrue(span["ocr_words"])
                        self.assertNotEqual(span["source_sha256"], span["artifact_sha256"])
                    else:
                        self.assertEqual(span["source_sha256"], span["artifact_sha256"])

    def test_pending_approval_and_exact_snapshot_survive_all_service_restarts(self):
        created = self.body("publication-created")
        restored = self.body("pending-after-all-services-restart")
        self.assertEqual(created["request"], restored["request"])
        self.assertEqual(restored["status"], "pending")
        self.assertEqual(created["request"]["requested_by"], "requester-alpha")
        self.assertEqual(self.cases["changed-revision-conflict"]["http_status"], 409)
        self.assertEqual(self.body("approval-accepted")["receipt"]["command"]["actor_id"], "approver-alpha")

    def test_actual_crash_retry_has_one_effect_and_a_retried_temporal_activity(self):
        self.assertTrue(self.report["durability"]["worker_killed_after_commit"])
        self.assertFalse(self.report["durability"]["before_crash_activity_completed"])
        self.assertEqual(self.report["durability"]["primary_effect_count"], 1)
        primary = next(item for item in self.report["histories"] if item["tenant_id"] == "alpha"
            and item["request_id"] == "publish-public-pages")
        history = json.loads((self.out.parent / primary["filename"]).read_text())
        attempts = [int(event["activityTaskStartedEventAttributes"].get("attempt", 1))
            for event in history["events"] if "activityTaskStartedEventAttributes" in event]
        self.assertTrue(any(attempt >= 2 for attempt in attempts))
        self.assertEqual(sum(event["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED"
            for event in history["events"]), 1)
        self.assertEqual(len(self.report["effects"]), 3)
        self.assertEqual(self.report["index"]["after_publications"], 28)

    def test_role_revision_waits_for_approval_and_old_approval_cannot_reopen_access(self):
        self.assertFalse(self.body("pending-acl-keeps-approved-version")["abstained"])
        self.assertFalse(self.body("approved-acl-preserves-analyst")["abstained"])
        for case in ("approved-acl-removes-reader", "old-replay-cannot-restore-reader",
                     "stale-approval-cannot-restore-reader", "narrowed-acl-after-restart"):
            self.assertTrue(self.body(case)["abstained"])
            self.assertEqual(self.body(case)["citations"], [])
        failed = self.body("stale-revision-publication-failed")
        self.assertEqual((failed["status"], failed["publication_status"]), ("approved", "failed"))
        self.assertIsNone(failed["effect"])

    def test_rejection_expiry_and_stale_approval_produce_no_publication(self):
        self.assertEqual(self.body("expired-while-worker-absent")["status"], "expired")
        self.assertEqual(self.cases["expired-approval-denied"]["http_status"], 409)
        self.assertEqual(self.report["durability"]["rejected_expired_stale_effect_count"], 0)
        self.assertEqual(self.body("rejected")["receipt"]["command"]["decision"], "reject")

    def test_actual_histories_are_hashed_and_replay_without_republishing(self):
        self.assertEqual(len(self.report["histories"]), 6)
        self.assertTrue(self.report["replay"]["all_passed"])
        self.assertTrue(self.report["replay"]["no_new_publications"])
        for item in self.report["histories"]:
            raw = (self.out.parent / item["filename"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
            self.assertEqual(len(json.loads(raw)["events"]), item["event_count"])

    def test_source_citations_survive_resource_restart(self):
        self.assertEqual(self.body("ocr-before-resource-restart")["citations"],
            self.body("ocr-after-resource-restart")["citations"])

    def test_public_artifacts_exclude_tokens_and_do_not_claim_human_approval(self):
        pattern = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|Bearer\s+[A-Za-z0-9_.-]{20,}")
        self.assertFalse(self.report["human_review_performed"])
        self.assertEqual(json.loads(self.out.read_text()), self.report)
        self.assertIsNone(pattern.search(json.dumps(self.report)))
        for item in self.report["histories"]:
            raw = (self.out.parent / item["filename"]).read_text()
            self.assertIsNone(pattern.search(raw))
            for payload in decoded_payloads(json.loads(raw)):
                self.assertIsNone(pattern.search(payload))
