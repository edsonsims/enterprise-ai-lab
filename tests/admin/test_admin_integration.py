"""Opt-in actual dedicated OIDC, portal HTTP, SQLite faults and Chromium."""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(
    os.environ.get("RUN_ADMIN_INTEGRATION") == "1",
    "Enable actual local admin integration explicitly.",
)
class AdminProcessIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from enterprise_ai_lab.admin.integration import run_demo

        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.browser = os.environ.get("RUN_ADMIN_BROWSER") == "1"
        cls.report = run_demo(
            argparse.Namespace(
                identity_dir=os.environ["IDENTITY_LAB_DIR"],
                node=os.environ["NODE_BINARY"],
                work_dir=cls.root / "runtime",
                out=cls.root / "report.json",
                browser_test=cls.browser,
                browser_script=Path(__file__).parent / "browser.mjs",
            )
        )
        cls.cases = {row["case"]: row for row in cls.report["observations"]}

    def test_real_user_login_and_permissions(self):
        self.assertEqual(self.report["oidc"]["pkce"], "S256")
        self.assertEqual(
            self.cases["login-admin-alpha"]["response"]["user"]["subject"],
            "admin-alpha",
        )
        for case in (
            "auditor-write-denied",
            "observer-denied",
            "csrf-denied",
            "origin-denied",
        ):
            self.assertEqual(self.cases[case]["http_status"], 403)

    def test_actual_config_reads_and_tenant_ids(self):
        alpha = self.cases["configured-read"]["response"]
        self.assertEqual(len(alpha["items"]), 1)
        self.assertEqual({row["tenant_id"] for row in alpha["items"]}, {"alpha"})
        beta = self.cases["beta-filtered-read"]["response"]
        self.assertTrue(all(row["tenant_id"] == "beta" for row in beta["items"]))
        self.assertEqual(
            self.cases["beta-independent-same-id"]["response"]["revision"], 1
        )

    def test_replay_conflict_and_append_only_rollback(self):
        self.assertEqual(
            self.cases["alpha-update"]["response"],
            self.cases["alpha-exact-replay"]["response"],
        )
        for case in ("immutable-command-conflict", "stale-revision-conflict"):
            self.assertEqual(self.cases[case]["http_status"], 409)
        self.assertEqual(self.cases["rollback-new-revision"]["response"]["revision"], 2)
        self.assertEqual(self.cases["auditor-tenant-history"]["response"]["total"], 2)

    def test_actual_kill_before_and_after_commit_same_body_recovery(self):
        self.assertEqual(len(self.report["faults"]), 2)
        before, after = self.report["faults"]
        self.assertEqual(
            (
                before["revision_after_reopen_before_retry"],
                before["revision_after_explicit_retry"],
            ),
            (2, 3),
        )
        self.assertEqual(
            (
                after["revision_after_reopen_before_retry"],
                after["revision_after_explicit_retry"],
            ),
            (4, 4),
        )
        for fault in self.report["faults"]:
            self.assertTrue(
                fault["controlled_fault"]
                and fault["response_lost"]
                and fault["same_id_and_body"]
            )
            self.assertFalse(fault["automatic_retry"])
        self.assertEqual(self.cases["final-alpha-audit"]["response"]["total"], 4)

    def test_restart_sessions_reaped_children_and_finite_safe_report(self):
        self.assertEqual(self.cases["restart-requires-login"]["http_status"], 401)
        self.assertTrue(self.report["processes"]["all_reaped"])
        self.assertFalse(self.report["human_review_performed"])
        raw = json.dumps(self.report)
        self.assertNotIn("access_token", raw)
        self.assertNotIn("csrf_token", raw)
        self.assertNotIn("password", raw)
        self.assertNotIn(str(self.root), raw)

    def test_actual_browser_when_explicitly_enabled(self):
        if not self.browser:
            self.skipTest("Actual rendered-browser checks disabled explicitly.")
        self.assertTrue(self.report["browser"]["passed"])
        self.assertTrue(all(row["passed"] for row in self.report["browser"]["checks"]))

    def test_offline_verifier_recomputes_actual_receipts_and_rejects_tampering(self):
        from enterprise_ai_lab.admin.integration import verify_report

        result = verify_report(self.report, identity_dir=os.environ["IDENTITY_LAB_DIR"])
        self.assertEqual(result["recomputed_committed_receipts"], 5)
        for field, value in (
            ("human_review_performed", True),
            ("schema_version", True),
            ("source_sha256", {}),
            ("identity_dist_sha256", {}),
        ):
            with self.subTest(field=field):
                corrupt = deepcopy(self.report)
                corrupt[field] = value
                with self.assertRaises(ValueError):
                    verify_report(corrupt, identity_dir=os.environ["IDENTITY_LAB_DIR"])
        corrupt = deepcopy(self.report)
        corrupt["faults"][0]["response_lost"] = 1
        with self.assertRaises(ValueError):
            verify_report(corrupt, identity_dir=os.environ["IDENTITY_LAB_DIR"])
        corrupt = deepcopy(self.report)
        corrupt["observations"][11]["response"]["audit_hash"] = "0" * 64
        with self.assertRaises(ValueError):
            verify_report(corrupt, identity_dir=os.environ["IDENTITY_LAB_DIR"])


class AdminReportAdmissionTests(unittest.TestCase):
    def test_existing_report_is_refused_before_starting_services(self):
        from enterprise_ai_lab.admin.integration import run_demo

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "report.json"
            out.write_text("preserve original")
            with patch("enterprise_ai_lab.admin.integration._JsonProcess") as process:
                with self.assertRaises(ValueError):
                    run_demo(argparse.Namespace(work_dir=root / "work", out=out))
                process.assert_not_called()
            self.assertEqual(out.read_text(), "preserve original")

    def test_bounded_no_follow_report_read_rejects_duplicate_keys_and_symlinks(self):
        from enterprise_ai_lab.admin.integration import load_report

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "report.json"
            source.write_text('{"same":1,"same":2}')
            with self.assertRaises(ValueError):
                load_report(source)
            link = root / "link.json"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                load_report(link)
            source.write_bytes(b" " * 1_000_001)
            with self.assertRaises(ValueError):
                load_report(source)
