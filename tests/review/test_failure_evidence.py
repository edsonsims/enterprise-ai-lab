"""Safe failure export survives private cleanup, without raw HTTP/process logs."""
import argparse
import asyncio
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from enterprise_ai_lab.diagnostics import DiagnosticRecorder, diagnostic_scope, diagnostic_stage
from enterprise_ai_lab.review.failure import (capture_failure, http_summary, phase_for_case,
    read_failure, save_failure, validate_failure)


class FailureEvidenceTests(unittest.TestCase):
    def envelope(self):
        return {"schema_version": 1, "phase": "create_browser_main",
            "http": {"status": 503, "format": "json", "error_code": "unavailable"},
            "diagnostics_status": "absent", "diagnostics_complete": False, "diagnostics": [],
            "children": {"portal": "running", "resource": "running"}}

    def exporter(self):
        path = Path(__file__).resolve().parents[2]/"scripts/export_review_failure.py"
        spec = importlib.util.spec_from_file_location("safe_export", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.export

    def test_http_messages_headers_and_unknown_fields_are_not_exported(self):
        secret = "private-token-and-source-text"
        response = httpx.Response(503, json={"error": {"code": "unavailable", "message": secret},
            "tenant": secret}, headers={"set-cookie": secret, "x-debug": secret})
        self.assertEqual(http_summary(response), self.envelope()["http"])
        self.assertNotIn(secret, json.dumps(http_summary(response)))
        for response in (httpx.Response(503, text=secret),
                         httpx.Response(503, json={"error": {"code": secret}}),
                         httpx.Response(503, content=secret.encode(), headers={"content-type": secret})):
            with self.subTest(response=response.status_code):
                self.assertNotIn(secret, json.dumps(http_summary(response)))

    def test_non_json_and_malformed_oversized_json_use_fixed_metadata(self):
        self.assertEqual(http_summary(httpx.Response(503, text="Service Unavailable")),
                         {"status": 503, "format": "text_plain", "error_code": None})
        for raw in (b"[", b'"' + b"x"*3000 + b'"', b'{"error":{"code":[]}}'):
            result = http_summary(httpx.Response(503, content=raw, headers={"content-type":"application/json"}))
            self.assertIn(result["error_code"], (None, "unrecognized"))

    def test_dynamic_case_values_map_to_static_phases(self):
        self.assertEqual(phase_for_case("create-browser-main"), "create_browser_main")
        self.assertEqual(phase_for_case("login-private-account"), "login")
        self.assertEqual(phase_for_case("private-account"), "other")
        self.assertEqual(phase_for_case([]), "other")

    def test_capture_and_export_survive_private_temporary_directory_cleanup(self):
        with tempfile.TemporaryDirectory() as public, tempfile.TemporaryDirectory() as private:
            sink = Path(private)/"diagnostics.jsonl"
            recorder = DiagnosticRecorder(sink)
            with diagnostic_scope("review_create"):
                try:
                    with diagnostic_stage("review_intent_audit"):
                        raise RuntimeError("private-source-and-token")
                except RuntimeError as exc:
                    self.assertTrue(recorder.failure("review_create", exc))
            child = Mock(); child._child.poll.return_value = None
            result = capture_failure(phase="create_browser_main", http=self.envelope()["http"],
                diagnostics_path=sink, portal=child)
            self.assertTrue(result["diagnostics_complete"])
            self.assertEqual(result["diagnostics"][0]["stage"], "review_intent_audit")
            self.assertEqual(result["children"], {"portal":"running", "resource":"unavailable"})
            path = Path(public)/"failure.json"
            save_failure(path, result)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-source", path.read_text())
            # Destroy source evidence, retaining only the validated export.
            sink.unlink()
            self.assertEqual(read_failure(path), result)
            exported = Path(public)/"upload.json"
            self.assertTrue(self.exporter()(path, exported))
            self.assertEqual(read_failure(exported), result)

    def test_absent_invalid_and_truncated_snapshots_are_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"diagnostics"
            result = capture_failure(phase=[], http=None, diagnostics_path=path)
            self.assertEqual(result["phase"], "other")
            self.assertEqual(result["diagnostics_status"], "absent")
            self.assertFalse(result["diagnostics_complete"])
            path.write_text("private-text"); path.chmod(0o600)
            result = capture_failure(phase="setup", http=None, diagnostics_path=path)
            self.assertEqual(result["diagnostics_status"], "invalid")
            self.assertFalse(result["diagnostics_complete"])
            path.unlink()
            recorder = DiagnosticRecorder(path)
            for _ in range(17):
                with diagnostic_scope("review_create"):
                    self.assertTrue(recorder.failure("review_create", RuntimeError("private")))
            result = capture_failure(phase="setup", http=None, diagnostics_path=path)
            self.assertEqual(len(result["diagnostics"]), 16)
            self.assertFalse(result["diagnostics_complete"])

    def test_unknown_nested_fields_and_non_string_enums_are_rejected(self):
        for key, value in (("http", {**self.envelope()["http"], "message":"private"}),
                           ("phase", []), ("diagnostics_status", []),
                           ("children", {"portal":[], "resource":"running"}),
                           ("diagnostics", [{"private": ["source"]*10000}]),
                           ("diagnostics_complete", True)):
            bad = self.envelope(); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_failure(bad)
        bad = self.envelope(); bad["unknown"] = "private"
        with self.assertRaises(ValueError): validate_failure(bad)
        result = validate_failure(self.envelope())
        result["children"]["portal"] = "exited"
        self.assertEqual(self.envelope()["children"]["portal"], "running")

    def test_export_rejects_corrupt_oversized_duplicate_or_unsafe_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp)/"input", Path(tmp)/"output"
            export = self.exporter()
            for raw in (b'{"private":"token"}', b"x"*16385, b'{"phase":"setup","phase":"other"}'):
                source.write_bytes(raw); source.chmod(0o600)
                self.assertFalse(export(source, destination))
                self.assertFalse(destination.exists())
            source.unlink(); save_failure(source, self.envelope())
            source.chmod(0o644)
            self.assertFalse(export(source, destination))
            source.chmod(0o600)
            alias = Path(tmp)/"alias"; os.link(source, alias)
            self.assertFalse(export(source, destination))
            alias.unlink(); alias.symlink_to(source)
            self.assertFalse(export(alias, destination))
            self.assertFalse(export(Path(tmp), destination))

    def test_export_and_save_never_replace_existing_owner_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp)/"input", Path(tmp)/"owner"
            save_failure(source, self.envelope())
            destination.write_bytes(b"owner bytes")
            self.assertFalse(self.exporter()(source, destination))
            with self.assertRaises(FileExistsError): save_failure(destination, self.envelope())
            self.assertEqual(destination.read_bytes(), b"owner bytes")

    def test_demo_failure_is_exported_before_private_cleanup_without_hiding_error(self):
        # Deliberate startup double; actual accepted-start/503 recovery is covered
        # separately by the opt-in Temporal/issuer/worker process experiment.
        from enterprise_ai_lab.review import integration
        with tempfile.TemporaryDirectory() as retained:
            output = Path(retained)/"failure.json"
            with tempfile.TemporaryDirectory() as private, ExitStack() as stack:
                for name in ("verify_cli", "verify_model", "load_pdf_documents", "CandidateRepository"):
                    stack.enter_context(patch.object(integration, name, return_value=Mock()))
                stack.enter_context(patch.object(integration, "load_pdf_documents", return_value=[]))
                identity = stack.enter_context(patch.object(integration, "IdentityFixture"))
                human = stack.enter_context(patch.object(integration, "HumanIssuer"))
                failure = RuntimeError("private-runtime-path-and-token")
                stack.enter_context(patch.object(integration.WorkflowEnvironment, "start_local",
                                                new=AsyncMock(side_effect=failure)))
                args = argparse.Namespace(work_dir=str(Path(private)/"work"), out=str(Path(private)/"demo.json"),
                    failure_out=output, identity_dir=private, manifest=private, model_dir=private,
                    temporal_cli=private, node="node", browser_test=False, stay_open=False)
                stdout = io.StringIO()
                with redirect_stdout(stdout), self.assertRaises(RuntimeError) as caught:
                    asyncio.run(integration.run_demo(args))
                self.assertIs(caught.exception, failure)
                self.assertNotIn("private-runtime", stdout.getvalue())
                self.assertTrue(json.loads(stdout.getvalue())["saved"])
                identity.return_value.close.assert_called_once()
                human.return_value.close.assert_called_once()
            self.assertEqual(read_failure(output)["phase"], "setup")
            self.assertFalse(read_failure(output)["diagnostics_complete"])


if __name__ == "__main__":
    unittest.main()
