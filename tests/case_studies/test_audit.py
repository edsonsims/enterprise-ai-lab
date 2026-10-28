"""Real local files exercise integrity, finite evidence states and safe outputs."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from enterprise_ai_lab.case_studies import (
    AuditError,
    CASES,
    MANIFEST_PATH,
    MAX_REPORT_BYTES,
    inspect,
    write_report,
)
from enterprise_ai_lab.case_studies.__main__ import main

REPOSITORY = Path(__file__).resolve().parents[2]


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repository"
        for relative in [MANIFEST_PATH, *CASES.values()]:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY / relative, target)

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(args))
        return code, json.loads(output.getvalue())

    def edit_manifest(self, change):
        path = self.root / MANIFEST_PATH
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    def test_matching_bytes_remain_historical(self):
        result = inspect(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["counts"], {"byte_verified_historical": 3, "failed": 0})
        for case in result["cases"]:
            self.assertEqual(case["status"], "byte_verified_historical")
            self.assertEqual(case["expected_sha256"], case["observed_sha256"])
        for field in [
            "fresh_execution_performed", "semantic_report_verification_performed",
            "archive_revision_verification_performed", "owner_review_or_study_asserted",
            "roadmap_item_15_complete",
        ]:
            self.assertIs(result[field], False)

    def test_mixed_report_failure_retains_matching_cases_and_safe_output(self):
        path = self.root / CASES["reliable-legacy-automation"]
        path.write_text("PRIVATE_SENTINEL bearer secret tenant content")
        destination = Path(self.temporary.name) / "failed-audit.json"
        code, result = self.cli("inspect", "--repository", str(self.root), "--out", str(destination))
        self.assertEqual(code, 1)
        self.assertEqual(result["counts"], {"byte_verified_historical": 2, "failed": 1})
        self.assertEqual(result["cases"][1]["error"], "report_mismatch")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(json.loads(destination.read_text()), result)

    def test_missing_report_is_visible_and_nonzero(self):
        (self.root / CASES["operable-local-ai"]).unlink()
        code, result = self.cli("inspect", "--repository", str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(result["cases"][2]["error"], "artifact_missing")
        self.assertEqual(result["counts"]["byte_verified_historical"], 2)

    def test_unknown_manifest_fields_are_refused_before_output(self):
        self.edit_manifest(lambda data: data.update(command="PRIVATE_COMMAND"))
        destination = Path(self.temporary.name) / "not-created.json"
        code, result = self.cli("inspect", "--repository", str(self.root), "--out", str(destination))
        self.assertEqual((code, result), (2, {"ok": False, "error": "invalid_manifest"}))
        self.assertFalse(destination.exists())

    def test_duplicate_json_keys_are_rejected(self):
        path = self.root / MANIFEST_PATH
        path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(AuditError) as caught:
            inspect(self.root)
        self.assertEqual(caught.exception.code, "duplicate_key")

    def test_unknown_fresh_evidence_state_is_not_promoted(self):
        self.edit_manifest(lambda data: data["cases"][0].update(evidence_state="executed_fresh"))
        with self.assertRaises(AuditError) as caught:
            inspect(self.root)
        self.assertEqual(caught.exception.code, "unknown_evidence_state")

    def test_duplicate_cases_and_report_paths_are_rejected(self):
        for change in [
            lambda data: data["cases"][1].update(id=data["cases"][0]["id"]),
            lambda data: data["cases"][1].update(report=data["cases"][0]["report"]),
        ]:
            with self.subTest(change=change):
                shutil.copyfile(REPOSITORY / MANIFEST_PATH, self.root / MANIFEST_PATH)
                self.edit_manifest(change)
                with self.assertRaises(AuditError) as caught:
                    inspect(self.root)
                self.assertEqual(caught.exception.code, "duplicate_case_or_path")

    def test_path_escape_absolute_and_private_paths_are_rejected(self):
        for relative in ["../../private.db", "/etc/passwd", "reports/local/private.json"]:
            with self.subTest(relative=relative):
                shutil.copyfile(REPOSITORY / MANIFEST_PATH, self.root / MANIFEST_PATH)
                self.edit_manifest(lambda data: data["cases"][0].update(report=relative))
                with self.assertRaises(AuditError) as caught:
                    inspect(self.root)
                self.assertEqual(caught.exception.code, "unsafe_path")

    def test_stale_metadata_cannot_be_exported_by_repinning_a_report(self):
        self.edit_manifest(lambda data: data["cases"][0].update(title="PRIVATE_SENTINEL"))
        code, result = self.cli("inspect", "--repository", str(self.root))
        self.assertEqual((code, result), (2, {"ok": False, "error": "manifest_mismatch"}))
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_nonstandard_json_and_boolean_schema_are_rejected(self):
        for text in ["NaN", "[]", '{"schema_version":true}', "{"]:
            with self.subTest(text=text):
                (self.root / MANIFEST_PATH).write_text(text)
                code, result = self.cli("inspect", "--repository", str(self.root))
                self.assertEqual(code, 2)
                self.assertEqual(result["error"], "invalid_manifest")

    def test_report_symlinks_and_symlink_directories_are_not_followed(self):
        path = self.root / CASES["reliable-legacy-automation"]
        target = Path(self.temporary.name) / "private.json"
        target.write_text("PRIVATE_SENTINEL")
        path.unlink()
        path.symlink_to(target)
        result = inspect(self.root)
        self.assertEqual(result["cases"][1]["status"], "failed")
        self.assertIsNone(result["cases"][1]["observed_sha256"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(target.parent, target_is_directory=True)
        result = inspect(self.root)
        self.assertEqual(result["cases"][1]["status"], "failed")
        self.assertIsNone(result["cases"][1]["observed_sha256"])

    def test_hardlinked_report_is_refused(self):
        path = self.root / CASES["reliable-legacy-automation"]
        os.link(path, Path(self.temporary.name) / "second-link.json")
        result = inspect(self.root)
        self.assertEqual(result["cases"][1]["error"], "unsafe_artifact")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_nonregular_report_does_not_block(self):
        path = self.root / CASES["reliable-legacy-automation"]
        path.unlink()
        os.mkfifo(path)
        result = inspect(self.root)
        self.assertEqual(result["cases"][1]["error"], "unsafe_artifact")

    def test_oversized_report_is_refused(self):
        path = self.root / CASES["reliable-legacy-automation"]
        with path.open("wb") as stream:
            stream.truncate(MAX_REPORT_BYTES + 1)
        result = inspect(self.root)
        self.assertEqual(result["cases"][1]["error"], "artifact_too_large")

    def test_manifest_change_during_read_is_refused(self):
        real_read = os.read
        changed = False

        def read_then_change(descriptor, size):
            nonlocal changed
            data = real_read(descriptor, size)
            if not changed:
                changed = True
                with (self.root / MANIFEST_PATH).open("ab") as stream:
                    stream.write(b"\n")
            return data

        with mock.patch("enterprise_ai_lab.case_studies.os.read", side_effect=read_then_change):
            with self.assertRaises(AuditError) as caught:
                inspect(self.root)
        self.assertEqual(caught.exception.code, "artifact_changed")

    def test_output_is_fresh_private_file_and_never_overwritten(self):
        destination = Path(self.temporary.name) / "audit.json"
        report = inspect(self.root)
        write_report(report, destination)
        original = destination.read_bytes()
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with self.assertRaises(AuditError) as caught:
            write_report(report, destination)
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(destination.read_bytes(), original)

    def test_output_symlink_and_symlink_parent_are_refused(self):
        target = Path(self.temporary.name) / "existing.json"
        target.write_text("original")
        destination = Path(self.temporary.name) / "audit.json"
        destination.symlink_to(target)
        code, result = self.cli("inspect", "--repository", str(self.root), "--out", str(destination))
        self.assertEqual((code, result), (2, {"ok": False, "error": "output_exists"}))
        self.assertEqual(target.read_text(), "original")
        alias = Path(self.temporary.name) / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(AuditError):
            write_report(inspect(self.root), alias / "not-created.json")
        self.assertFalse((self.root / "not-created.json").exists())

    def test_cli_rejects_missing_or_execution_arguments_without_path_echo(self):
        for args in [[], ["run"], ["inspect"], ["inspect", "--repository", str(self.root), "--execute"]]:
            with self.subTest(args=args):
                code, result = self.cli(*args)
                self.assertEqual((code, result), (2, {"ok": False, "error": "invalid_arguments"}))

    def test_no_services_subprocesses_or_optional_dependencies_needed(self):
        program = (
            "import json,sys;"
            f"sys.path.insert(0,{str(REPOSITORY / 'src')!r});"
            "from enterprise_ai_lab.case_studies import inspect;"
            f"report=inspect({str(self.root)!r});"
            "blocked={'torch','transformers','numpy','mcp','temporalio','fastapi','onnxruntime','qdrant_client'};"
            "print(json.dumps({'ok':report['ok'],'optional':sorted(blocked.intersection(sys.modules))}))"
        )
        result = subprocess.run([sys.executable, "-S", "-c", program], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "optional": []})

    def test_inspection_starts_no_network_or_child_process(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")):
            with mock.patch("subprocess.Popen", side_effect=AssertionError("process forbidden")):
                self.assertTrue(inspect(self.root)["ok"])

    def test_missing_manifest_or_repository_is_a_finite_cli_failure(self):
        (self.root / MANIFEST_PATH).unlink()
        code, result = self.cli("inspect", "--repository", str(self.root))
        self.assertEqual((code, result), (2, {"ok": False, "error": "artifact_missing"}))
        code, result = self.cli("inspect", "--repository", str(self.root / "PRIVATE_MISSING"))
        self.assertEqual((code, result), (2, {"ok": False, "error": "repository_unavailable"}))


if __name__ == "__main__":
    unittest.main()
