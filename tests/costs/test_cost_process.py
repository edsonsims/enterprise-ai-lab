"""Actual offline CLI processes and the fixed synthetic experiment."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enterprise_ai_lab.costs.experiment import run_demo

ROOT = Path(__file__).resolve().parents[2]


class ProcessTests(unittest.TestCase):
    def test_second_spawn_failure_reaps_first_actual_child(self):
        original = subprocess.Popen
        children = []

        def spawn(args, *positional, **keywords):
            if "--input" in args:
                body = json.loads(Path(args[args.index("--input") + 1]).read_text())
                if body.get("event_id") == "concurrent-duplicate":
                    if children:
                        raise OSError("controlled second spawn failure")
                    child = original(args, *positional, **keywords)
                    children.append(child)
                    return child
            return original(args, *positional, **keywords)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "enterprise_ai_lab.costs.experiment.subprocess.Popen",
                side_effect=spawn,
            ),
            self.assertRaises(OSError),
        ):
            run_demo(Path(directory) / "work")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stderr.closed)

    def run_cli(self, *args):
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "TMPDIR"}
        }
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "enterprise_ai_lab.costs", *map(str, args)],
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.stderr, b"")
        return result.returncode, json.loads(result.stdout)

    def test_actual_full_process_demo_and_no_existing_work_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_demo(root / "work")
            self.assertEqual(len(report["observations"]), 31)
            self.assertEqual(report["actual_processes"], 33)
            self.assertTrue(report["all_owned_processes_reaped"])
            self.assertLess(report["lost_receipt"]["killed_process_exit"], 0)
            self.assertTrue(report["lost_receipt"]["empty_stdout"])
            final = report["observations"][-1]["response"]["result"]
            self.assertEqual((final["events"], final["exact_total"]), (8, "0.581"))
            with self.assertRaises(FileExistsError):
                run_demo(root / "work")

    def test_existing_report_is_refused_before_work_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            report.write_text("keep")
            code, response = self.run_cli(
                "demo", "--work-dir", root / "work", "--out", report
            )
            self.assertEqual(code, 2)
            self.assertFalse(response["ok"])
            self.assertEqual(report.read_text(), "keep")
            self.assertFalse((root / "work").exists())

    def test_duplicate_keys_unsafe_file_and_errors_do_not_echo_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "usage.sqlite"
            raw = root / "event.json"
            self.assertEqual(self.run_cli("init", "--ledger", database)[0], 0)
            for content in [
                '{"PRIVATE":1,"PRIVATE":2}',
                '{"private":"SECRET"}',
                '"\u0000"',
            ]:
                raw.write_text(content)
                code, response = self.run_cli(
                    "append",
                    "--ledger",
                    database,
                    "--profile",
                    "alpha-operator",
                    "--input",
                    raw,
                )
                self.assertEqual(code, 2)
                self.assertEqual(response, {"ok": False, "error": "invalid_input"})
            link = root / "link.json"
            link.symlink_to(raw)
            self.assertEqual(
                self.run_cli(
                    "append",
                    "--ledger",
                    database,
                    "--profile",
                    "alpha-operator",
                    "--input",
                    link,
                )[0],
                2,
            )
            fifo = root / "pipe"
            os.mkfifo(fifo)
            self.assertEqual(
                self.run_cli(
                    "append",
                    "--ledger",
                    database,
                    "--profile",
                    "alpha-operator",
                    "--input",
                    fifo,
                )[0],
                2,
            )
