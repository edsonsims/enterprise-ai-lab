from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

PREVIOUS = "2026-01-01T00:00:00Z"
CURRENT = "2026-01-02T00:00:00Z"
END = "2026-01-03T00:00:00Z"
SOURCE = Path(__file__).resolve().parents[2] / "src"


def scenario():
    return {
        "schema_version": 1,
        "data_kind": "fictional_roi_assumptions",
        "scenario_id": "process-fixture",
        "previous_start": PREVIOUS,
        "current_start": CURRENT,
        "current_end": END,
        "baseline_minutes": {"low": "1", "base": "2", "high": "3"},
        "automation_share": {"low": "0.1", "base": "0.5", "high": "1"},
        "realization_share": {"low": "0.1", "base": "0.5", "high": "1"},
        "hourly_rate": "30",
        "allocated_fixed_cost": "0",
        "period_operating_cost": "0",
        "minimum_requests": 1,
    }


def event(key, *, request="request-1", occurred=CURRENT, metric="embedding_tokens"):
    return {
        "event_id": key,
        "request_id": request,
        "metric": metric,
        "quantity": 1,
        "rate_version": "fictional-v1",
        "occurred_at": occurred,
    }


def checksum(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.in_script = False
        self.script_text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, text):
        if self.in_script:
            self.script_text.append(text)


class HealthProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.ledger = self.directory / "ledger.sqlite"
        self.scenario_path = self.directory / "scenario.json"
        self.write_scenario(scenario())
        self.environment = dict(os.environ)
        self.environment["PYTHONPATH"] = str(SOURCE)
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"
        self.cost("init", "--ledger", self.ledger)

    def run_cli(self, module, *arguments, success=True, timeout=8):
        result = subprocess.run(
            [sys.executable, "-m", module, *map(str, arguments)],
            cwd=self.directory,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.returncode, 0 if success else 2, result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        value = json.loads(result.stdout)
        self.assertIs(value["ok"], success)
        if not success:
            self.assertEqual(set(value), {"ok", "error"})
            self.assertNotIn(str(self.directory), result.stdout)
        return value

    def cost(self, *arguments, success=True):
        return self.run_cli("enterprise_ai_lab.costs", *arguments, success=success)

    def write_scenario(self, value):
        self.scenario_path.write_text(json.dumps(value))

    def append(self, value, profile="alpha-operator"):
        path = self.directory / "event.json"
        path.write_text(json.dumps(value))
        return self.cost(
            "append", "--ledger", self.ledger, "--profile", profile, "--input", path
        )["result"]

    def render(self, output, *, profile="alpha-auditor", input_path=None, success=True):
        return self.run_cli(
            "enterprise_ai_lab.health",
            "render",
            "--ledger",
            self.ledger,
            "--profile",
            profile,
            "--scenario",
            input_path or self.scenario_path,
            "--out-dir",
            output,
            success=success,
        )

    def test_real_ledger_export_is_tenant_scoped_and_hashes_full_receipts(self):
        receipts = [
            self.append(event("previous-1", occurred=PREVIOUS)),
            self.append(event("previous-2", occurred=PREVIOUS)),
            self.append(event("current-1", request="current-request")),
            self.append(
                event("current-2", request="current-request", metric="ocr_pages")
            ),
        ]
        self.append(event("beta-only", request="beta-only-request"), "beta-operator")
        output = self.directory / "alpha-report"
        value = self.render(output)["result"]
        raw = (output / "report.json").read_bytes()
        report = json.loads(raw)
        self.assertEqual(report["snapshot"]["tenant_id"], "alpha")
        self.assertEqual(report["snapshot"]["watermark"], 4)
        self.assertEqual(report["previous"]["event_count"], 2)
        self.assertEqual(report["previous"]["usage_identified_requests"], 1)
        self.assertEqual(report["current"]["event_count"], 2)
        self.assertEqual(report["current"]["usage_identified_requests"], 1)
        full_rows = [
            {k: v for k, v in row.items() if k != "replayed"} for row in receipts
        ]
        self.assertEqual(report["snapshot"]["snapshot_sha256"], checksum(full_rows))
        self.assertEqual(value["snapshot_sha256"], checksum(full_rows))
        self.assertNotIn("beta-only", raw.decode())
        self.assertEqual(value["report_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            value["html_sha256"],
            hashlib.sha256((output / "index.html").read_bytes()).hexdigest(),
        )
        self.assertEqual(value["cells"], 27)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        self.assertEqual(
            {v.name for v in output.iterdir()}, {"report.json", "index.html"}
        )
        for file in output.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual(file.stat().st_nlink, 1)

    def test_static_html_contains_only_inert_report_and_local_download(self):
        self.append(event("event-1"))
        output = self.directory / "html-report"
        self.render(output)
        document = (output / "index.html").read_text()
        report = json.loads((output / "report.json").read_text())
        parsed = Document()
        parsed.feed(document)
        policies = [
            v["content"]
            for tag, v in parsed.tags
            if tag == "meta" and v.get("http-equiv") == "Content-Security-Policy"
        ]
        self.assertEqual(len(policies), 1)
        for rule in ("default-src 'none'", "base-uri 'none'", "form-action 'none'"):
            self.assertIn(rule, policies[0])
        scripts = [attrs for tag, attrs in parsed.tags if tag == "script"]
        self.assertEqual(scripts, [{"id": "report-data", "type": "application/json"}])
        self.assertEqual(json.loads("".join(parsed.script_text)), report)
        self.assertEqual(document.count("</script>"), 1)
        self.assertNotIn("url(", document.lower())
        for tag, attrs in parsed.tags:
            self.assertNotIn(
                tag,
                {"iframe", "object", "embed", "img", "link", "audio", "video", "form"},
            )
            self.assertFalse(any(key.lower().startswith("on") for key in attrs))
            self.assertNotIn("src", attrs)
            if "href" in attrs:
                self.assertEqual(attrs["href"], "report.json")
        self.assertIn("not observed savings", document)
        self.assertIn("not statistical confidence intervals", document)
        self.assertEqual(len(report["sensitivity"]), 27)

    def test_bad_json_and_identity_overrides_create_no_export(self):
        base = json.dumps(scenario())
        malformed = {
            "duplicate": base.replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
            ),
            "float_schema": base.replace(
                '"schema_version": 1', '"schema_version": 1.0'
            ),
            "float_rate": base.replace('"hourly_rate": "30"', '"hourly_rate": 30.0'),
            "nan": base.replace('"hourly_rate": "30"', '"hourly_rate": NaN'),
            "identity": json.dumps({**scenario(), "tenant_id": "beta"}),
            "role": json.dumps({**scenario(), "roles": ["auditor"]}),
            "boolean": json.dumps({**scenario(), "minimum_requests": True}),
            "unequal_windows": json.dumps(
                {**scenario(), "current_end": "2026-01-04T00:00:00Z"}
            ),
            "markup": json.dumps(
                {**scenario(), "scenario_id": "</title><script>SECRET</script>"}
            ),
        }
        for key, text in malformed.items():
            with self.subTest(case=key):
                self.scenario_path.write_text(text)
                output = self.directory / key
                self.assertEqual(
                    self.render(output, success=False)["error"], "invalid_input"
                )
                self.assertFalse(output.exists())

    def test_profiles_are_server_policy_and_observer_cannot_export(self):
        self.append(event("alpha-only"))
        for profile, error in (
            ("alpha-observer", "forbidden"),
            ("not-a-profile", "invalid_profile"),
        ):
            with self.subTest(profile=profile):
                output = self.directory / profile
                self.assertEqual(
                    self.render(output, profile=profile, success=False)["error"], error
                )
                self.assertFalse(output.exists())
        output = self.directory / "beta-report"
        self.render(output, profile="beta-auditor")
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["snapshot"]["tenant_id"], "beta")
        self.assertEqual(report["snapshot"]["events"], [])
        self.assertNotIn("alpha-only", (output / "index.html").read_text())

    def test_existing_output_paths_are_never_overwritten(self):
        directory = self.directory / "already-directory"
        directory.mkdir()
        sentinel = directory / "report.json"
        sentinel.write_bytes(b"preserve existing output")
        plain = self.directory / "already-file"
        plain.write_bytes(b"preserve file")
        link = self.directory / "already-link"
        link.symlink_to(directory, target_is_directory=True)
        for output in (directory, plain, link):
            with self.subTest(kind=output.name):
                self.assertEqual(
                    self.render(output, success=False)["error"], "invalid_input"
                )
        self.assertEqual(sentinel.read_bytes(), b"preserve existing output")
        self.assertEqual(plain.read_bytes(), b"preserve file")
        self.assertTrue(link.is_symlink())
        self.assertEqual({v.name for v in directory.iterdir()}, {"report.json"})

    def test_scenario_file_boundaries_reject_aliases_nonregular_and_oversize(self):
        link = self.directory / "scenario-link.json"
        link.symlink_to(self.scenario_path)
        hardlink = self.directory / "scenario-hardlink.json"
        os.link(self.scenario_path, hardlink)
        oversized = self.directory / "oversized.json"
        oversized.write_bytes(b" " * (256 * 1024 + 1))
        fifo = self.directory / "scenario.fifo"
        os.mkfifo(fifo)
        for number, path in enumerate(
            (link, hardlink, self.directory, oversized, fifo)
        ):
            with self.subTest(kind=path.name):
                output = self.directory / f"invalid-file-{number}"
                self.assertEqual(
                    self.render(output, input_path=path, success=False)["error"],
                    "invalid_input",
                )
                self.assertFalse(output.exists())

    def test_empty_windows_leave_growth_and_zero_cost_roi_undefined(self):
        output = self.directory / "empty-report"
        self.render(output)
        report = json.loads((output / "report.json").read_text())
        self.assertIsNone(report["activity"]["ratio_change"])
        self.assertIsNone(report["activity"]["percent_change_display"])
        self.assertIsNone(report["modeled_base"]["roi_percent"])
        self.assertTrue(report["activity"]["flags"]["no_current_usage"])
        self.assertIn(
            "Undefined: previous count is zero", (output / "index.html").read_text()
        )
        self.assertIn(
            "Undefined: zero modeled cost", (output / "index.html").read_text()
        )

    def test_export_is_immutable_while_later_ledger_append_changes_new_snapshot(self):
        self.append(event("first"))
        old = self.directory / "old-report"
        self.render(old)
        original = {p.name: p.read_bytes() for p in old.iterdir()}
        self.append(event("later", request="later-request"))
        new = self.directory / "new-report"
        self.render(new)
        before, after = (
            json.loads(original["report.json"]),
            json.loads((new / "report.json").read_text()),
        )
        self.assertEqual(
            (before["snapshot"]["watermark"], after["snapshot"]["watermark"]), (1, 2)
        )
        self.assertEqual(
            (
                before["current"]["usage_identified_requests"],
                after["current"]["usage_identified_requests"],
            ),
            (1, 2),
        )
        self.assertNotEqual(
            before["snapshot"]["snapshot_sha256"], after["snapshot"]["snapshot_sha256"]
        )
        self.assertEqual(original, {p.name: p.read_bytes() for p in old.iterdir()})

    def test_corrupt_stored_usage_fails_before_any_export(self):
        self.append(event("stored-event"))
        connection = sqlite3.connect(self.ledger)
        try:
            original_trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='usage_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER usage_no_update")
            connection.execute("UPDATE usage SET digest=?", ("0" * 64,))
            connection.execute(original_trigger)
            connection.commit()
        finally:
            connection.close()
        output = self.directory / "corrupt-report"
        self.assertEqual(self.render(output, success=False)["error"], "storage_corrupt")
        self.assertFalse(output.exists())

    def test_missing_parent_and_missing_ledger_do_not_create_partial_exports(self):
        output = self.directory / "missing-parent" / "report"
        self.assertEqual(
            self.render(output, success=False)["error"], "storage_unavailable"
        )
        self.assertFalse(output.parent.exists())
        self.ledger.unlink()
        output = self.directory / "missing-ledger-report"
        self.assertEqual(
            self.render(output, success=False)["error"], "storage_unavailable"
        )
        self.assertFalse(output.exists())
        self.assertFalse(self.ledger.exists())

    def test_demo_bootstraps_nested_parents_and_refuses_existing_report_before_work(
        self,
    ):
        script = SOURCE.parent / "scripts" / "health_demo.py"
        work = self.directory / "new-work-parent" / "nested" / "demo"
        report_path = self.directory / "new-output-parent" / "reports" / "story.json"
        self.assertFalse(work.parent.exists())
        self.assertFalse(report_path.parent.exists())

        def invoke(work_path):
            return subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--work-dir",
                    str(work_path),
                    "--out",
                    str(report_path),
                ],
                cwd=self.directory,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        completed = invoke(work)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(json.loads(completed.stdout)["observations"], 15)
        report_bytes = report_path.read_bytes()
        report = json.loads(report_bytes)
        self.assertEqual(report["actual_CLI_processes"], 15)
        self.assertEqual(len(report["observations"]), 15)
        self.assertIs(report["every_subprocess_reaped"], True)
        self.assertIs(report["subprocess_socket_connect_and_DNS_audit_denial"], True)
        self.assertEqual(
            report["data_kind"], "authored_fictional_usage_and_scenario_model"
        )
        self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
        for name in ("alpha", "beta", "repeat", "later"):
            self.assertEqual(stat.S_IMODE((work / name).stat().st_mode), 0o700)
            for artifact in (work / name).iterdir():
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        refused_work = self.directory / "never-created-parent" / "demo"
        refused = invoke(refused_work)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("existing output", refused.stderr)
        self.assertFalse(refused_work.parent.exists())
        self.assertEqual(report_path.read_bytes(), report_bytes)


if __name__ == "__main__":
    unittest.main()
