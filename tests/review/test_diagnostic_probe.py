"""Actual opt-in missing-worker experiment plus bounded report metadata checks."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

from enterprise_ai_lab.review.diagnostic_probe import (
    _save_report, request_fingerprint, source_fingerprints, verify_public_manifest,
)
from enterprise_ai_lab.approval.contracts import format_utc


class DiagnosticMetadataTests(unittest.TestCase):
    def test_exact_request_fingerprint_is_order_independent_but_binds_every_field(self):
        body = {"request_id": "probe", "candidate_sha256": "1" * 64, "expected_revision": 0,
                "expires_at": format_utc(datetime.now(timezone.utc) + timedelta(minutes=10))}
        first = request_fingerprint(body)
        self.assertEqual(first, request_fingerprint(dict(reversed(list(body.items())))))
        for name, change in (("request_id", "other"), ("candidate_sha256", "2" * 64),
                             ("expected_revision", 1), ("expires_at", format_utc(datetime.now(timezone.utc)
                                                                              + timedelta(minutes=11)))):
            with self.subTest(field=name):
                self.assertNotEqual(first, request_fingerprint({**body, name: change}))
        with self.assertRaises(ValueError): request_fingerprint({**body, "password": "do-not-copy"})
        with self.assertRaises(ValueError): request_fingerprint({**body, "expected_revision": False})

    def test_source_fingerprints_export_only_fixed_repository_sources(self):
        root = Path(__file__).resolve().parents[2]
        result = source_fingerprints(root)
        self.assertIn("src/enterprise_ai_lab/review/diagnostic_probe.py", result)
        for name, value in result.items():
            self.assertFalse(Path(name).is_absolute())
            self.assertEqual(value, hashlib.sha256((root / name).read_bytes()).hexdigest())

    def test_report_creation_is_private_exclusive_and_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            _save_report(path, {"controlled_fault": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): _save_report(path, {})
            link = Path(temporary) / "link.json"; link.symlink_to(path)
            with self.assertRaises(FileExistsError): _save_report(link, {})
            self.assertEqual(json.loads(path.read_text()), {"controlled_fault": True})

    def test_export_probe_rejects_private_or_modified_manifest_bytes(self):
        public = Path(__file__).resolve().parents[2] / "public-data/pdf-v1/ingestion.json"
        self.assertEqual(verify_public_manifest(public), hashlib.sha256(public.read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as temporary:
            changed = Path(temporary) / "private-manifest.json"
            changed.write_bytes(public.read_bytes() + b"\n")
            with self.assertRaises(ValueError): verify_public_manifest(changed)


@unittest.skipUnless(os.environ.get("RUN_REVIEW_DIAGNOSTIC") == "1", "Enable the actual controlled review diagnostic explicitly.")
class ActualReviewDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from enterprise_ai_lab.review.diagnostic_probe import run_probe
        cls.temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        # Missing configured runtimes raise; enabling this test never silently skips.
        cls.report = asyncio.run(run_probe(argparse.Namespace(work_dir=str(directory / "private"),
            out=str(directory / "safe-report.json"), identity_dir=os.environ["IDENTITY_LAB_DIR"],
            node=os.environ["NODE_BINARY"], model_dir=os.environ["RAG_MODEL_DIR"],
            temporal_cli=os.environ["TEMPORAL_CLI"],
            manifest=str(Path(__file__).resolve().parents[2] / "public-data/pdf-v1/ingestion.json"))))

    def test_actual_503_records_rpc_stage_and_accepted_unprocessed_workflow(self):
        report = self.report
        self.assertTrue(report["controlled_fault"])
        self.assertEqual(report["original_ci_failure_cause"], "unknown")
        initial = report["initial"]
        self.assertEqual(initial["http_status"], 503)
        self.assertEqual(initial["workflow_status"], "RUNNING")
        self.assertEqual(initial["history"]["workflow_start_count"], 1)
        self.assertEqual(initial["history"]["workflow_task_completed_count"], 0)
        self.assertEqual(initial["failure"]["http"]["error_code"], "unavailable")
        diagnostic = initial["failure"]["diagnostics"][0]
        self.assertEqual(diagnostic["stage"], "temporal_query")
        self.assertIn(diagnostic["rpc_status"], ("DEADLINE_EXCEEDED", "CANCELLED"))

    def test_one_identical_retry_after_real_worker_readiness_never_publishes(self):
        report = self.report; recovery = report["recovery"]
        self.assertEqual(report["submission"]["total_post_attempts"], 2)
        self.assertTrue(report["submission"]["identical_retry"])
        self.assertEqual(report["submission"]["rpc_timeout_seconds"], 3)
        self.assertEqual(report["submission"]["api_operation_timeout_seconds"], 12)
        self.assertEqual(recovery["http_status"], 200)
        self.assertEqual(recovery["status"], "pending")
        self.assertTrue(recovery["same_workflow_run"])
        self.assertGreaterEqual(recovery["worker_ready_workflow_task_count"], 1)
        self.assertEqual(recovery["history"]["workflow_start_count"], 1)
        self.assertEqual(recovery["history"]["activity_scheduled_count"], 0)
        self.assertEqual(recovery["publication_effect_count"], 0)
        self.assertEqual(recovery["tenant_revision"], 0)
        self.assertEqual(recovery["rag"], {"http_status": 200, "abstained": True, "citation_count": 0})

    def test_public_pdf_origins_and_no_credentials_or_person_review_claim(self):
        report = self.report
        self.assertFalse(report["human_review_performed"])
        documents = report["public_candidate"]["documents"]
        self.assertEqual({d["provenance"]["method"] for d in documents}, {"native", "ocr"})
        self.assertEqual({d["tenant_id"] for d in documents}, {"alpha"})
        secret_keys = {"password", "access_token", "id_token", "csrf_token", "code_verifier", "session_cookie", "text"}
        def check(value):
            if type(value) is dict:
                self.assertFalse(secret_keys.intersection(value))
                for child in value.values(): check(child)
            elif type(value) is list:
                for child in value: check(child)
        check(report)
        self.assertIsNone(re.search(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|Bearer\s+[A-Za-z0-9_.-]{20,}", json.dumps(report)))
