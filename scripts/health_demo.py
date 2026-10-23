"""Exercise fictional activity and modeled ROI through actual offline CLI processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "public-data" / "health-v1"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_demo(work_dir, out):
    if out.exists() or out.is_symlink():
        raise ValueError("existing output")
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(mode=0o700)
    observations = []
    env = {
        k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TMPDIR"}
    }
    env["PYTHONPATH"] = str(ROOT / "src")
    ledger = work_dir / "ledger.sqlite"
    wrapper = (
        "import sys,runpy; "
        "sys.addaudithook(lambda event,args: "
        '(_ for _ in ()).throw(RuntimeError("network refused")) '
        'if event in ("socket.connect","socket.getaddrinfo") else None); '
        'module=sys.argv.pop(1);runpy.run_module(module,run_name="__main__")'
    )

    def cli(module, *args, expected=0):
        command = [
            sys.executable,
            "-c",
            wrapper,
            "enterprise_ai_lab." + module,
            *map(str, args),
        ]
        result = subprocess.run(
            command,
            check=False,
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        value = json.loads(result.stdout)
        if result.returncode != expected or value.get("ok") != (expected == 0):
            raise RuntimeError("unexpected finite CLI result")
        observations.append(
            {
                "module": module,
                "operation": args[0],
                "returncode": result.returncode,
                "response": value,
            }
        )
        return value

    def put(value, name):
        path = work_dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        return path

    def render(profile, name, expected=0):
        return cli(
            "health",
            "render",
            "--ledger",
            ledger,
            "--profile",
            profile,
            "--scenario",
            DATA / "scenario.json",
            "--out-dir",
            work_dir / name,
            expected=expected,
        )

    scenario = json.loads((DATA / "scenario.json").read_text())
    fixture = json.loads((DATA / "events.json").read_text())
    cli("costs", "init", "--ledger", ledger)
    for i, row in enumerate(fixture["events"]):
        path = put(row["event"], f"event-{i}.json")
        cli(
            "costs",
            "append",
            "--ledger",
            ledger,
            "--profile",
            row["profile"],
            "--input",
            path,
        )
    render("alpha-auditor", "alpha")
    render("beta-auditor", "beta")
    denied = render("alpha-observer", "denied", expected=2)
    assert denied["error"] == "forbidden" and not (work_dir / "denied").exists()
    render("alpha-auditor", "repeat")
    alpha = json.loads((work_dir / "alpha" / "report.json").read_text())
    beta = json.loads((work_dir / "beta" / "report.json").read_text())
    assert (
        alpha["snapshot"]["tenant_id"] == "alpha"
        and beta["snapshot"]["tenant_id"] == "beta"
    )
    assert alpha["previous"]["usage_identified_requests"] == 3
    assert (
        alpha["current"]["usage_identified_requests"] == 2
        and alpha["current"]["event_count"] == 3
    )
    assert alpha["current"]["ledger_cost"]["display_usd"] == "0.83"
    assert beta["current"]["ledger_cost"]["display_usd"] == "50.00"
    assert alpha["modeled_base"]["gross_benefit"]["display_usd"] == "5.00"
    assert alpha["modeled_base"]["net_benefit"]["display_usd"] == "0.17"
    assert alpha["modeled_base"]["roi_percent"]["display"] == "3.52"
    assert len(alpha["sensitivity"]) == 27
    assert "beta-only" not in json.dumps(alpha)
    assert (work_dir / "alpha" / "report.json").read_bytes() == (
        work_dir / "repeat" / "report.json"
    ).read_bytes()
    original_json, original_html = (
        sha(work_dir / "alpha" / "report.json"),
        sha(work_dir / "alpha" / "index.html"),
    )
    late = fixture["late_event"]
    cli(
        "costs",
        "append",
        "--ledger",
        ledger,
        "--profile",
        late["profile"],
        "--input",
        put(late["event"], "late-event.json"),
    )
    render("alpha-auditor", "later")
    later = json.loads((work_dir / "later" / "report.json").read_text())
    assert later["current"]["usage_identified_requests"] == 3
    assert later["snapshot"]["watermark"] == alpha["snapshot"]["watermark"] + 1
    assert later["snapshot"]["snapshot_sha256"] != alpha["snapshot"]["snapshot_sha256"]
    refused = render("alpha-auditor", "alpha", expected=2)
    assert refused["error"] == "invalid_input"
    assert original_json == sha(work_dir / "alpha" / "report.json")
    assert original_html == sha(work_dir / "alpha" / "index.html")
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "data_kind": "authored_fictional_usage_and_scenario_model",
        "observations": observations,
        "actual_CLI_processes": len(observations),
        "every_subprocess_reaped": True,
        "subprocess_socket_connect_and_DNS_audit_denial": True,
        "reports": {"alpha": alpha, "beta": beta, "later": later},
        "artifacts": {
            "alpha_report_sha256": original_json,
            "alpha_html_sha256": original_html,
        },
        "fixture_sha256": {
            p.name: sha(p) for p in [DATA / "events.json", DATA / "scenario.json"]
        },
        "source_sha256": alpha["source_sha256"],
        "scenario_id": scenario["scenario_id"],
        "script_sha256": sha(Path(__file__)),
        "limitations": [
            "All usage, rates and assumptions are fictional; no actual customer, telemetry, savings or ROI.",
            "Request IDs identify usage, not successful/completed tasks; all benefit is modeled.",
            "Trusted launcher profiles are not authentication. Exported artifacts are plaintext and inherit no live authorization.",
            "Socket audit denial is a Python subprocess guard, not an OS network sandbox.",
            "Process tests and snapshot consistency do not establish production throughput or availability.",
            "No owner study, human review, registry release or statistical confidence interval is inferred.",
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    return {
        "observations": len(observations),
        "fictional_only": True,
        "owner_human_review_performed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_demo(args.work_dir.absolute(), args.out.absolute())))


if __name__ == "__main__":
    main()
