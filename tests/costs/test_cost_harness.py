"""Controlled real SQLite/CLI schedules; synthetic wire failures are explicit doubles."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from enterprise_ai_lab.costs import CostError
from enterprise_ai_lab.costs.__main__ import main
from enterprise_ai_lab.costs.experiment import DemoFailure, _response, run_demo

PAUSED_WRITER = r"""
import sqlite3,sys,time
from pathlib import Path
ready,gate=sys.argv[1:3]
arguments=sys.argv[3:]
path=arguments[arguments.index('--ledger')+1]
original=sqlite3.connect
class Paused(sqlite3.Connection):
    def commit(self):
        if self.in_transaction and Path(path+'-journal').exists():
            Path(ready).write_text('holding-real-write')
            end=time.monotonic()+5
            while not Path(gate).exists():
                if time.monotonic()>=end:raise RuntimeError('test barrier deadline')
                time.sleep(.005)
        return super().commit()
def connect(*args,**kwargs):
    kwargs['factory']=Paused
    return original(*args,**kwargs)
sqlite3.connect=connect
from enterprise_ai_lab.costs.__main__ import main
raise SystemExit(main(arguments))
"""

PARTIAL_MARKER = r"""
import sys,time
from pathlib import Path
marker=Path(sys.argv[3]);temporary=Path(sys.argv[3]+'.tmp')
ready=Path(sys.argv[3]+'.ready');gate=Path(sys.argv[3]+'.gate')
original=Path.write_text
def write(path,data,*args,**kwargs):
    if path in (marker,temporary):
        cut=MARKER_CUT
        with path.open('w',encoding='utf-8') as stream:
            stream.write(data[:cut]);stream.flush()
            original(ready,'partial-written')
            end=time.monotonic()+5
            while not gate.exists():
                if time.monotonic()>=end:raise RuntimeError('test publication deadline')
                time.sleep(.005)
            stream.write(data[cut:])
        return len(data)
    return original(path,data,*args,**kwargs)
Path.write_text=write
"""


def busy_schedule(work, *, terminal_double=False):
    """Pause a real writer until its real contending CLI has finished."""
    work.mkdir(mode=0o700)
    ready, gate = work / "ready", work / "gate"
    native = subprocess.Popen
    children, threads, errors = [], [], []

    def spawn(args, *positional, **keywords):
        if "--input" not in args:
            return native(args, *positional, **keywords)
        value = json.loads(Path(args[args.index("--input") + 1]).read_text())
        if value.get("event_id") != "concurrent-duplicate":
            return native(args, *positional, **keywords)
        if not children:
            child = native(
                [args[0], "-c", PAUSED_WRITER, str(ready), str(gate), *args[3:]],
                *positional,
                **keywords,
            )
            children.append(child)
            deadline = time.monotonic() + 5
            while not ready.exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("real writer did not reach barrier")
                time.sleep(0.005)
        else:
            # This optional branch is an explicit wire double, not a real role denial.
            command = (
                [
                    args[0],
                    "-c",
                    "import json;print(json.dumps({'ok':False,'error':'forbidden'}));raise SystemExit(2)",
                ]
                if terminal_double
                else args
            )
            child = native(command, *positional, **keywords)
            children.append(child)

            def release():
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    errors.append("child_deadline")
                finally:
                    gate.write_text("release")

            thread = threading.Thread(target=release)
            threads.append(thread)
            thread.start()
        return child

    try:
        with patch(
            "enterprise_ai_lab.costs.experiment.subprocess.Popen", side_effect=spawn
        ):
            try:
                result, failure = run_demo(work / "demo"), None
            except DemoFailure as exc:
                result, failure = None, exc.diagnostic
    finally:
        gate.write_text("release")
        for thread in threads:
            thread.join(timeout=3)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert all(child.poll() is not None for child in children)
    return result, failure, len(children)


def marker_schedule(work, cut):
    """A real child stalls with empty/partial temporary bytes before publication."""
    native, original_exists = subprocess.Popen, Path.exists
    marker = work / "committed.json"
    ready, gate = Path(str(marker) + ".ready"), Path(str(marker) + ".gate")
    children, observations = [], []

    def spawn(args, *positional, **keywords):
        if len(args) == 6 and args[1] == "-c" and Path(args[5]) == marker:
            args = [*args]
            args[2] = PARTIAL_MARKER.replace("MARKER_CUT", str(cut)) + "\n" + args[2]
            child = native(args, *positional, **keywords)
            children.append(child)
            return child
        return native(args, *positional, **keywords)

    def exists(path):
        result = original_exists(path)
        if path == marker and original_exists(ready) and not observations:
            temporary = Path(str(marker) + ".tmp")
            observations.append(
                {
                    "published": result,
                    "temporary_bytes": temporary.stat().st_size
                    if original_exists(temporary)
                    else None,
                }
            )
            if not result:
                gate.write_text("publish")
        return result

    try:
        with (
            patch(
                "enterprise_ai_lab.costs.experiment.subprocess.Popen", side_effect=spawn
            ),
            patch.object(Path, "exists", exists),
        ):
            result = run_demo(work)
    finally:
        if original_exists(work):
            gate.write_text("release")
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
    assert len(children) == 1 and children[0].poll() is not None
    return result, observations


class DemoHarnessTests(unittest.TestCase):
    def test_hostile_error_subclasses_and_tampered_diagnostics_do_not_reflect_text(
        self,
    ):
        class HostileCode(CostError):
            def __init__(self):
                ValueError.__init__(self, "PRIVATE-TEXT")

            @property
            def code(self):
                raise CostError("PRIVATE-TEXT")

        tampered = DemoFailure("setup", "other")
        tampered.diagnostic = {"PRIVATE-TEXT": "PRIVATE-TEXT"}
        for exception in (HostileCode(), CostError(["PRIVATE-TEXT"]), tampered):
            with (
                self.subTest(exception_type=type(exception).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                output = io.StringIO()
                with (
                    patch(
                        "enterprise_ai_lab.costs.experiment._run_demo",
                        side_effect=exception,
                    ),
                    redirect_stdout(output),
                ):
                    code = main(
                        [
                            "demo",
                            "--work-dir",
                            str(Path(directory) / "work"),
                            "--out",
                            str(Path(directory) / "out.json"),
                        ]
                    )
                self.assertEqual(code, 2)
                body = json.loads(output.getvalue())
                self.assertEqual(body["error"], "demo_failed")
                self.assertEqual(
                    set(body["diagnostic"]),
                    {"stage", "category", "error_codes", "returncodes"},
                )
                self.assertNotIn("PRIVATE", output.getvalue())

    def test_real_busy_refusal_is_retained_then_replayed_once_after_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            report, failure, attempts = busy_schedule(Path(directory) / "case")
        self.assertIsNone(failure)
        self.assertEqual(attempts, 3)  # two initial CLI children and one exact replay
        race = next(
            row
            for row in report["observations"]
            if row["case"] == "concurrent-identical-processes"
        )
        self.assertEqual(
            race["initial_attempts"][1],
            {"process_exit": 2, "response": {"ok": False, "error": "resource_busy"}},
        )
        self.assertEqual((race["attempt_count"], race["explicit_replay_count"]), (3, 1))
        self.assertEqual(
            sorted(row["replayed"] for row in race["responses"]), [False, True]
        )
        self.assertEqual(len({row["receipt_id"] for row in race["responses"]}), 1)
        self.assertEqual(
            (len(report["observations"]), report["actual_processes"]), (32, 34)
        )
        self.assertTrue(report["all_owned_processes_reaped"])

    def test_nonbusy_wire_double_is_terminal_and_has_finite_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            report, failure, attempts = busy_schedule(
                Path(directory) / "case", terminal_double=True
            )
        self.assertIsNone(report)
        self.assertEqual(attempts, 2)
        self.assertEqual(
            failure,
            {
                "stage": "duplicate_writers",
                "category": "child_error",
                "error_codes": ["forbidden"],
                "returncodes": [2],
            },
        )

    def test_empty_and_partial_marker_bytes_are_not_published_readiness(self):
        for cut in (0, 19):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as directory:
                report, observed = marker_schedule(Path(directory) / "demo", cut)
                self.assertEqual(
                    observed, [{"published": False, "temporary_bytes": cut}]
                )
                self.assertTrue(report["lost_receipt"]["empty_stdout"])
                self.assertLess(report["lost_receipt"]["killed_process_exit"], 0)
                self.assertTrue(report["all_owned_processes_reaped"])

    def test_unexpected_response_and_exception_never_reflect_private_text(self):
        for raw, stderr in [
            (b'{"ok":false,"error":"PRIVATE-TEXT"}', b""),
            (b"PRIVATE-TEXT", b""),
            (b"{}", b"PRIVATE-TEXT"),
        ]:
            with self.subTest(raw=raw), self.assertRaises(DemoFailure) as caught:
                _response(raw, stderr, 2, "duplicate_writers")
            self.assertNotIn("PRIVATE", json.dumps(caught.exception.diagnostic))
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "enterprise_ai_lab.costs.experiment._run_demo",
                side_effect=RuntimeError("PRIVATE-TEXT"),
            ),
            redirect_stdout(output),
        ):
            code = main(
                [
                    "demo",
                    "--work-dir",
                    str(Path(directory) / "work"),
                    "--out",
                    str(Path(directory) / "out.json"),
                ]
            )
        self.assertEqual(code, 2)
        body = json.loads(output.getvalue())
        self.assertEqual(
            body,
            {
                "ok": False,
                "error": "demo_failed",
                "diagnostic": {
                    "stage": "setup",
                    "category": "other",
                    "error_codes": [],
                    "returncodes": [],
                },
            },
        )
        self.assertNotIn("PRIVATE", output.getvalue())
