"""Model-free contracts and real process/pipe checks; no neural inference."""

from __future__ import annotations

import copy
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from enterprise_ai_lab.edge import contracts as c
from enterprise_ai_lab.edge import experiment as e
from enterprise_ai_lab.edge.worker import serve
from enterprise_ai_lab.rag.neural import (
    MODEL_FILES,
    LocalQwenGenerator,
    validate_generation,
)


def fixture_metadata(threads):
    generator = object.__new__(LocalQwenGenerator)
    generator.file_hashes = MODEL_FILES
    generator.threads = threads
    generator.max_new_tokens = 128
    return generator.metadata()


def memory():
    return {
        "native": 1024,
        "native_unit": "bytes" if platform.system() == "Darwin" else "KiB",
        "bytes": 1024 if platform.system() == "Darwin" else 1048576,
        "scope": "process_high_water_all_threads",
    }


def ready(threads):
    return {
        "type": "ready",
        "schema_version": 1,
        "metadata": fixture_metadata(threads),
        "startup_elapsed_ns": 1,
        "rss": memory(),
        "source_sha256": c.source_hashes(),
        "python": "3.13.1",
        "system": platform.system(),
        "machine": platform.machine(),
    }


def response(request, *, valid=True):
    raw = (
        json.dumps(
            {
                "answer": "Fixture-only response",
                "evidence": [
                    {
                        "chunk_id": request["chunks"][0]["chunk_id"],
                        "quote": request["chunks"][0]["text"],
                    }
                ],
            }
        )
        if valid
        else "not JSON"
    )
    trace = {
        "input_tokens": 20,
        "generated_tokens": 10,
        "prompt_sha256": "1" * 64,
        "raw_output_sha256": c.sha(raw.encode()),
        "raw_output": raw,
        "validated": valid,
    }
    answer = None
    if valid:
        answer = validate_generation(raw, request["chunks"])
        answer.update(
            generator="qwen3-0.6b-cpu",
            generation={
                name: trace[name]
                for name in (
                    "input_tokens",
                    "generated_tokens",
                    "prompt_sha256",
                    "raw_output_sha256",
                )
            },
        )
    return {
        "type": "result",
        "id": request["id"],
        "ok": valid,
        "error": None if valid else "generation_failed",
        "answer": answer,
        "elapsed_ns": 3,
        "model_generate_calls": 1,
        "model_generate_elapsed_ns": 1,
        "trace": trace,
        "rss": memory(),
    }


class FakeGenerator:
    """Explicit deterministic test double, never a recorded neural measurement."""

    def __init__(self, _path, *, max_new_tokens, threads):
        assert max_new_tokens == 128
        self.threads = threads
        self.last_trace = None
        self.model = SimpleNamespace(generate=lambda: None)

    def metadata(self):
        return fixture_metadata(self.threads)

    def generate(self, question, chunks):
        if question == "fail before trace reset":
            raise ValueError("PRIVATE input")
        self.model.generate()
        row = response(
            {"id": "stub", "chunks": chunks}, valid=question != "invalid output"
        )
        self.last_trace = row["trace"]
        if row["ok"]:
            return row["answer"]
        raise ValueError("PRIVATE generation error")


class ScriptedClient:
    """Report recomputation test double, not process or model evidence."""

    def __init__(self, command):
        self.threads = int(command[-1])
        self.started_ns = time.perf_counter_ns()
        self.done = False
        self.count = 0

    def read(self):
        if not self.done:
            return ready(self.threads)
        return {
            "type": "stopped",
            "requests": self.count,
            "model_generate_calls": self.count,
            "rss": memory(),
        }

    def call(self, request):
        self.count += 1
        return response(request, valid=self.count != 2)

    def finish_input(self):
        self.done = True

    def close(self, *, abort=False):
        return {
            "spawned": True,
            "reaped": True,
            "returncode": 0,
            "forced_kill": False,
            "elapsed_ns": 1,
            "stderr_bytes": 0,
            "stderr_sha256": c.sha(b""),
        }


class ContractTests(unittest.TestCase):
    def test_frozen_fixture_has_no_answers_and_all_calls_are_scheduled(self):
        p = c.protocol()
        self.assertEqual([1, 4], p["threads"])
        self.assertEqual(7, len(c.schedule()))
        for case in p["cases"]:
            self.assertEqual({"id", "question", "chunks"}, set(case))
        self.assertEqual("first_request_after_load", c.schedule()[0][0])

    def test_duplicate_nonfinite_float_extra_and_overlong_inputs_fail(self):
        for raw in [b'{"id":1,"id":2}', b'{"a":NaN}', b'{"a":1.0}', b'"\\ud800"']:
            with self.subTest(raw=raw), self.assertRaises((c.EdgeError, UnicodeError)):
                value = c.parse(raw)
                c.text(value, 100)
        original = c.protocol()["cases"][0]
        for mutation in [
            {**original, "tenant": "alpha"},
            {**original, "question": "x" * 2049},
            {**original, "chunks": original["chunks"] * 2},
        ]:
            with self.assertRaises(c.EdgeError):
                c.request(mutation)

    def test_token_trace_and_literal_evidence_are_rechecked(self):
        case = c.protocol()["cases"][0]
        row = response(case)
        c.result(row, case)
        for change in [
            lambda x: x["trace"].update(generated_tokens=129),
            lambda x: x["trace"].update(prompt_sha256=[]),
            lambda x: x["answer"]["evidence"][0].update(quote="invented"),
            lambda x: x.update(model_generate_calls=True),
            lambda x: (
                x.update(ok=False, error="generation_failed", answer=None)
                or x["trace"].update(validated=False)
            ),
        ]:
            bad = copy.deepcopy(row)
            change(bad)
            with self.assertRaises(c.EdgeError):
                c.result(bad, case)
        c.result(response(case, valid=False), case)

    def test_report_recomputes_failures_calls_medians_and_rejects_tampering(self):
        with patch.object(e, "WorkerClient", ScriptedClient):
            report = e.measure("/unused/test-double")
        self.assertEqual(
            {
                "configurations": 2,
                "requests_with_receipts": 14,
                "observed_model_generate_calls": 14,
                "terminal_errors": 0,
            },
            e.verify_report(report),
        )
        self.assertEqual(
            1, report["configurations"][0]["summary"]["generation_failures"]
        )
        for mutate in [
            lambda r: r.update(worker_lifetime_seconds=True),
            lambda r: r.update(configuration_order=[True, 4]),
            lambda r: r["configurations"][0]["summary"].update(
                observed_model_generate_calls=6
            ),
            lambda r: r["configurations"][0]["samples"][1]["response"]["rss"].update(
                native_unit="KiB" if platform.system() == "Darwin" else "bytes",
                bytes=1048576 if platform.system() == "Darwin" else 1024,
            ),
            lambda r: r["configurations"][0]["samples"].pop(),
        ]:
            bad = copy.deepcopy(report)
            mutate(bad)
            with self.assertRaises(c.EdgeError):
                e.verify_report(bad)

    def test_spawn_failure_is_retained_without_losing_other_configuration(self):
        calls = []

        def factory(command):
            calls.append(command)
            if len(calls) == 2:
                raise OSError("PRIVATE path")
            return ScriptedClient(command)

        with patch.object(e, "WorkerClient", factory):
            report = e.measure("/unused/test-double")
        self.assertEqual(7, len(report["configurations"][0]["samples"]))
        self.assertEqual("worker_failed", report["configurations"][1]["terminal_error"])
        self.assertFalse(report["configurations"][1]["cleanup"]["spawned"])
        self.assertNotIn("PRIVATE", json.dumps(report))
        report["configurations"][1]["stopped"] = {
            "type": "stopped",
            "requests": 0,
            "model_generate_calls": 0,
            "rss": memory(),
        }
        with self.assertRaises(c.EdgeError):
            e.verify_report(report)


class WorkerTests(unittest.TestCase):
    def test_test_double_worker_preserves_failed_current_request_and_call_counts(self):
        cases = [
            c.protocol()["cases"][0],
            {
                **c.protocol()["cases"][0],
                "id": "prevalidation",
                "question": "fail before trace reset",
            },
            {**c.protocol()["cases"][1], "question": "invalid output"},
        ]
        output = io.BytesIO()
        self.assertEqual(
            0,
            serve(
                "unused",
                1,
                stdin=io.BytesIO(b"".join(c.canonical(x) + b"\n" for x in cases)),
                stdout=output,
                generator_factory=FakeGenerator,
            ),
        )
        frames = [c.parse(x) for x in output.getvalue().splitlines()]
        self.assertEqual(
            ["ready", "result", "result", "result", "stopped"],
            [x["type"] for x in frames],
        )
        self.assertTrue(frames[1]["ok"])
        self.assertIsNone(frames[2]["trace"])
        self.assertEqual(0, frames[2]["model_generate_calls"])
        self.assertFalse(frames[3]["trace"]["validated"])
        self.assertEqual(2, frames[4]["model_generate_calls"])
        for frame, case in zip(frames[1:4], cases):
            c.result(frame, case)

    def test_invalid_wire_fails_without_raw_input_disclosure(self):
        output = io.BytesIO()
        self.assertEqual(
            2,
            serve(
                "unused",
                1,
                stdin=io.BytesIO(b'{"PRIVATE":"token"}\n'),
                stdout=output,
                generator_factory=FakeGenerator,
            ),
        )
        self.assertEqual(
            {"type": "fatal", "error": "invalid_input"},
            c.parse(output.getvalue().splitlines()[-1]),
        )
        self.assertNotIn(b"PRIVATE", output.getvalue())

    def test_startup_failure_is_finite(self):
        output = io.BytesIO()

        def broken(*_args, **_kwargs):
            raise ValueError("PRIVATE path")

        self.assertEqual(
            2,
            serve(
                "unused", 1, stdin=io.BytesIO(), stdout=output, generator_factory=broken
            ),
        )
        self.assertEqual(
            b'{"error":"startup_failed","type":"fatal"}\n', output.getvalue()
        )


class RealProcessTests(unittest.TestCase):
    def child(self, script, *, lifetime=1):
        return e.WorkerClient([sys.executable, "-c", script], lifetime=lifetime)

    def test_real_pipe_roundtrip_and_clean_eof(self):
        client = self.child(
            'import sys; print("{}",flush=True); line=sys.stdin.readline(); print(line,flush=True); sys.stdin.read()'
        )
        try:
            self.assertEqual({}, client.read())
            self.assertEqual(
                {"value": "unicode é"}, client.call({"value": "unicode é"})
            )
        finally:
            outcome = client.close()
        self.assertTrue(outcome["reaped"])
        self.assertEqual(0, outcome["returncode"])
        self.assertFalse(outcome["forced_kill"])

    def test_real_stderr_is_drained_without_retaining_text(self):
        client = self.child(
            'import sys; sys.stderr.write("PRIVATE"*10000);sys.stderr.flush();print("{}",flush=True)'
        )
        try:
            self.assertEqual({}, client.read())
        finally:
            outcome = client.close()
        self.assertEqual(70000, outcome["stderr_bytes"])
        self.assertEqual(c.sha(b"PRIVATE" * 10000), outcome["stderr_sha256"])
        self.assertNotIn("PRIVATE", json.dumps(outcome))

    def test_real_eof_malformed_oversize_and_unknown_fatal_are_bounded(self):
        for script in [
            "pass",
            'print("not JSON")',
            'print("x"*70000)',
            'print(\'{"type":"fatal","error":["PRIVATE"]}\')',
        ]:
            with self.subTest(script=script):
                client = self.child(script)
                try:
                    with self.assertRaises(c.EdgeError):
                        client.read()
                finally:
                    self.assertTrue(client.close(abort=True)["reaped"])

    def test_real_backpressure_cannot_block_past_lifetime(self):
        client = self.child("import time; time.sleep(10)", lifetime=0.12)
        start = time.monotonic()
        try:
            with self.assertRaises(c.EdgeError) as error:
                client.call({"value": "x" * 64000})
            self.assertEqual("worker_timeout", error.exception.code)
        finally:
            outcome = client.close(abort=True)
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(outcome["reaped"])
        self.assertTrue(outcome["forced_kill"])

    def test_buffered_frame_is_not_accepted_after_deadline(self):
        client = self.child('import time; print("{}",flush=True);time.sleep(10)')
        try:
            client._buffer = b"{}\n"
            client.deadline = time.monotonic() - 1
            with self.assertRaises(c.EdgeError) as error:
                client.read()
            self.assertEqual("worker_timeout", error.exception.code)
        finally:
            client.close(abort=True)

    def test_registration_failure_after_real_spawn_still_reaps(self):
        selector = e.selectors.DefaultSelector()
        selector_type = type(selector)
        selector.close()
        with patch.object(selector_type, "register", side_effect=OSError("PRIVATE")):
            client = self.child("import time;time.sleep(10)")
        self.assertTrue(client.cleanup["reaped"])
        self.assertTrue(client.cleanup["spawned"])
        self.assertIsNotNone(client.process.poll())
        with self.assertRaises(c.EdgeError):
            client.read()


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "enterprise_ai_lab.edge", *map(str, args)],
            capture_output=True,
            timeout=15,
            check=False,
        )

    def test_existing_output_refused_before_model_start(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text("owner bytes")
            proc = self.invoke("demo", "--model-dir", "missing", "--out", path)
            self.assertEqual(2, proc.returncode)
            self.assertEqual(
                {"ok": False, "error": "output_exists"}, json.loads(proc.stdout)
            )
            self.assertEqual("owner bytes", path.read_text())

    def test_real_missing_model_retains_failure_report_and_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            proc = self.invoke(
                "demo", "--model-dir", Path(folder) / "missing", "--out", path
            )
            self.assertEqual(2, proc.returncode, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertFalse(result["ok"])
            self.assertEqual(2, result["result"]["terminal_errors"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            report = c.parse(path.read_bytes(), limit=c.MAX_REPORT)
            self.assertEqual(
                0, e.verify_report(report)["observed_model_generate_calls"]
            )
            self.assertEqual(
                ["startup_failed"] * 2,
                [x["terminal_error"] for x in report["configurations"]],
            )
            verified = self.invoke("verify-report", "--report", path)
            self.assertEqual(0, verified.returncode, verified.stderr)
            self.assertEqual(
                2, json.loads(verified.stdout)["result"]["terminal_errors"]
            )

    def test_report_input_refuses_symlink_hardlink_fifo_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "original"
            original.write_bytes(b'{"untrusted": NaN}')
            for name, setup in [
                ("symbolic", lambda p: p.symlink_to(original)),
                ("hard", lambda p: os.link(original, p)),
                ("fifo", lambda p: os.mkfifo(p)),
            ]:
                path = root / name
                setup(path)
                with self.assertRaises(c.EdgeError):
                    e.read_report(path)
                path.unlink()
            with self.assertRaises(c.EdgeError):
                e.read_report(original)


if __name__ == "__main__":
    unittest.main()
