"""Actual OIDC code+PKCE, portal, PDF/OCR, Temporal, RAG and optional Chromium."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest


def payloads(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "data" and isinstance(child, str):
                try: yield base64.b64decode(child, validate=True).decode()
                except (ValueError, UnicodeError): pass
            yield from payloads(child)
    elif isinstance(value, list):
        for child in value: yield from payloads(child)


@unittest.skipUnless(os.environ.get("RUN_REVIEW_INTEGRATION") == "1", "Enable actual local review integration explicitly.")
class ActualHumanReviewIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from enterprise_ai_lab.review.integration import run_demo
        cls.temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name); cls.out = directory/"demo.json"
        cls.browser = os.environ.get("RUN_REVIEW_BROWSER") == "1"
        cls.report = asyncio.run(run_demo(argparse.Namespace(work_dir=str(directory/"runtime"), out=str(cls.out),
            identity_dir=os.environ["IDENTITY_LAB_DIR"], node=os.environ["NODE_BINARY"],
            model_dir=os.environ["RAG_MODEL_DIR"], temporal_cli=os.environ["TEMPORAL_CLI"],
            manifest=str(Path(__file__).resolve().parents[2]/"public-data/pdf-v1/ingestion.json"),
            browser_test=cls.browser, stay_open=False,
            failure_out=os.environ.get("REVIEW_FAILURE_REPORT"))))
        cls.cases = {item["case"]: item for item in cls.report["observations"]}

    def body(self, name): return self.cases[name]["response"]

    def test_actual_signed_in_subject_is_an_account_not_the_shared_application(self):
        login = self.body("login-analyst-reviewer-alpha")
        self.assertEqual(login["user"]["subject"], "analyst-reviewer-alpha")
        self.assertNotEqual(login["user"]["subject"], "review-portal")
        receipt = self.body("human-account-approved-publication")["receipt"]
        self.assertEqual(receipt["command"]["actor_id"], login["user"]["subject"])
        self.assertEqual(self.report["oidc"]["flow"], "authorization_code")
        self.assertEqual(self.report["oidc"]["pkce"], "S256")

    def test_reviewer_role_does_not_bypass_the_document_role(self):
        view = self.body("partial-role-view")
        self.assertEqual(len(view["documents"]), 1)
        self.assertEqual(view["denied_document_count"], 1)
        self.assertFalse(view["can_decide"])
        self.assertEqual(self.cases["hidden-ocr-pdf-denied"]["http_status"], 404)
        self.assertEqual(self.cases["blind-approval-denied"]["http_status"], 403)

    def test_complete_reviewer_sees_exact_source_spans_and_only_single_page_previews(self):
        view = self.body("complete-role-view")
        self.assertEqual(len(view["documents"]), 2)
        self.assertTrue(view["can_decide"])
        for document in view["documents"]:
            for chunk in document["chunks"]:
                span = chunk["provenance"]
                self.assertEqual(document["text"][span["start"]:span["end"]], chunk["text"])
        self.assertEqual(len(self.report["previews"]), 4)
        self.assertTrue(all(p["page_count"] == 1 for p in self.report["previews"]))
        self.assertTrue(any(p["variant"] == "original" and p["provenance"]["source_page"] == 25
            for p in self.report["previews"]))

    def test_signed_tenant_isolation_and_observer_denial(self):
        self.assertEqual(self.cases["cross-tenant-detail-denied"]["http_status"], 404)
        self.assertEqual(self.cases["observer-role-denied"]["http_status"], 403)
        reviews = self.body("beta-queue-isolated")["reviews"]
        self.assertEqual({v["request"]["tenant_id"] for v in reviews}, {"beta"})
        self.assertEqual({v["request"]["request_id"] for v in reviews}, {"beta-public-pages"})

    def test_cookie_mutations_need_csrf_and_same_origin(self):
        for name in ("csrf-denied", "cross-origin-denied"):
            self.assertEqual(self.cases[name]["http_status"], 403)

    def test_pending_empty_and_approved_ocr_only_after_durable_decision(self):
        self.assertTrue(self.body("pending-rag-empty")["abstained"])
        self.assertEqual(self.body("pending-rag-empty")["citations"], [])
        self.assertFalse(self.body("approved-ocr-rag")["abstained"])
        self.assertTrue(any(c["provenance"]["method"] == "ocr" for c in self.body("approved-ocr-rag")["citations"]))
        self.assertEqual(len(self.report["effects"]), 4 if self.browser else 2)

    def test_audit_persists_while_server_sessions_do_not(self):
        self.assertEqual(self.cases["old-session-denied-after-portal-restart"]["http_status"], 401)
        before = {v["event_sha256"] for v in self.body("audit-before-restart")["events"]}
        after = {v["event_sha256"] for v in self.body("audit-after-restart")["events"]}
        self.assertTrue(before <= after)
        self.assertEqual(self.cases["logged-out-denied"]["http_status"], 401)
        self.assertTrue(self.report["audit_chain_valid"])
        self.assertTrue(any(v["action"] == "decision_accepted" and v["identity_kind"] == "user" for v in self.report["audit"]))
        self.assertTrue(any(v["action"] == "publication_observed" and v["identity_kind"] == "system" for v in self.report["audit"]))

    def test_histories_replay_and_rejection_never_runs_publication(self):
        self.assertEqual(len(self.report["histories"]), 6 if self.browser else 3)
        self.assertTrue(self.report["replay"]["all_passed"])
        self.assertTrue(self.report["replay"]["no_new_effects"])
        for item in self.report["histories"]:
            raw = (self.out.parent/item["filename"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
            if "reject" in item["request_id"]:
                self.assertNotIn("EVENT_TYPE_ACTIVITY_TASK_SCHEDULED", item["event_types"])

    def test_public_artifacts_exclude_credentials_and_do_not_claim_a_person_reviewed(self):
        expression = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|Bearer\s+[A-Za-z0-9_.-]{20,}")
        self.assertFalse(self.report["human_review_performed"])
        self.assertIsNone(expression.search(json.dumps(self.report)))
        secret_keys = {"password", "access_token", "id_token", "csrf_token", "code_verifier", "session_cookie"}
        def check(value):
            if isinstance(value, dict):
                self.assertFalse(secret_keys.intersection(value))
                for child in value.values(): check(child)
            elif isinstance(value, list):
                for child in value: check(child)
        check(self.report)
        for item in self.report["histories"]:
            value = json.loads((self.out.parent/item["filename"]).read_text())
            for text in payloads(value): self.assertIsNone(expression.search(text))

    def test_actual_chromium_checks_when_enabled(self):
        if not self.browser:
            self.skipTest("Actual Chromium UI explicitly disabled; HTTP protocol integration still ran.")
        browser = self.report["browser"]
        self.assertTrue(browser["passed"])
        self.assertEqual(browser["browser"]["name"], "Chromium")
        self.assertTrue(browser["checks"])
        self.assertTrue(all(item["passed"] for item in browser["checks"]))
