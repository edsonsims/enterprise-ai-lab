"""Actual local-process fictional accounting, with fixed experiment-only faults."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .. import __version__
from .contracts import DATA, MAX_OUTPUT, CostError, canonical, now, strict_json, totals


def run_demo(work_dir):
    work = Path(work_dir).absolute()
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    scenario = strict_json((DATA / "scenario-v1.json").read_bytes())
    database = work / "usage.sqlite"
    observations = []
    process_count = 0
    input_count = 0
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}
    }

    def command(operation, profile=None, value=None, **options):
        nonlocal input_count
        args = [
            sys.executable,
            "-m",
            "enterprise_ai_lab.costs",
            operation,
            "--ledger",
            str(database),
        ]
        if profile is not None:
            args += ["--profile", profile]
        if value is not None:
            path = work / ("input-" + str(input_count) + ".json")
            input_count += 1
            with path.open("x") as stream:
                stream.write(canonical(value))
            args += ["--input", str(path)]
        for key, item in options.items():
            args += ["--" + key.replace("_", "-"), str(item)]
        return args

    def run(label, operation, profile=None, value=None, expected=None, **options):
        nonlocal process_count
        completed = subprocess.run(
            command(operation, profile, value, **options),
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
        process_count += 1
        if len(completed.stdout) > MAX_OUTPUT or completed.stderr:
            raise CostError("demo_failed")
        result = json.loads(completed.stdout)
        if expected is None:
            if completed.returncode != 0 or result["ok"] is not True:
                raise CostError("demo_failed")
        elif completed.returncode != 2 or result != {"ok": False, "error": expected}:
            raise CostError("demo_failed")
        observations.append(
            {"case": label, "process_exit": completed.returncode, "response": result}
        )
        return result.get("result")

    run("initialize", "init")
    for entry in scenario["events"]:
        run(
            entry["profile"] + ":" + entry["event"]["event_id"],
            "append",
            entry["profile"],
            entry["event"],
        )
    original = observations[1]["response"]["result"]
    replay = run(
        "same-event-new-process",
        "append",
        "alpha-operator2",
        scenario["events"][0]["event"],
    )
    if {k: v for k, v in original.items() if k != "replayed"} != {
        k: v for k, v in replay.items() if k != "replayed"
    } or not replay["replayed"]:
        raise CostError("demo_failed")
    conflict = copy.deepcopy(scenario["events"][0]["event"])
    conflict["quantity"] += 1
    run(
        "conflicting-event",
        "append",
        "alpha-operator",
        conflict,
        "idempotency_conflict",
    )
    for tenant in ("alpha", "beta"):
        result = run(
            tenant + "-summary-after-reopen",
            "summary",
            tenant + "-auditor",
            **scenario["period"],
        )
        if any(
            result[key] != value
            for key, value in scenario["literal_expected_totals"][tenant].items()
        ):
            raise CostError("demo_failed")
    for case in scenario["statements"]:
        expected = (
            "duplicate_statement_line"
            if case["expected"] == "duplicate_statement_line"
            else None
        )
        result = run(
            case["label"], "reconcile", case["profile"], case["statement"], expected
        )
        if result is not None and not result["result"][case["expected"]]:
            raise CostError("demo_failed")
    clean = next(
        row["response"]["result"]
        for row in observations
        if row["case"] == "alpha-clean"
    )
    later = copy.deepcopy(scenario["events"][1]["event"])
    later["event_id"] = "later-event"
    run("later-usage", "append", "alpha-operator", later)
    frozen = run(
        "statement-frozen-after-later-append",
        "reconcile",
        "alpha-auditor",
        scenario["statements"][0]["statement"],
    )
    if {k: v for k, v in frozen.items() if k != "replayed"} != {
        k: v for k, v in clean.items() if k != "replayed"
    }:
        raise CostError("demo_failed")
    run("observer-denied", "list", "alpha-observer", expected="forbidden")
    run("auditor-cannot-append", "append", "alpha-auditor", later, "forbidden")
    override = {**later, "tenant_id": "beta"}
    run("body-tenant-override", "append", "alpha-operator", override, "invalid_input")
    run(
        "unreadable-foreign-id",
        "get",
        "beta-auditor",
        expected="not_found",
        event_id="tiny-001",
    )
    run(
        "absent-id-same-error",
        "get",
        "beta-auditor",
        expected="not_found",
        event_id="never-seen",
    )

    # Fixed experiment-only wrapper blocks after actual durable commit, before stdout.
    lost = {**later, "event_id": "lost-receipt"}
    marker = work / "committed.json"
    code = "from pathlib import Path;import json,sys;from enterprise_ai_lab.costs import CostLedger,principal_for_profile; r=CostLedger(sys.argv[1]).append(principal_for_profile('alpha-operator'),json.loads(sys.argv[2]));Path(sys.argv[3]).write_text(json.dumps(r));sys.stdin.read(1)"
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(database), canonical(lost), str(marker)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process_count += 1
    try:
        deadline = time.monotonic() + 5
        while (
            not marker.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not marker.exists() or child.poll() is not None:
            raise CostError("demo_failed")
        committed = strict_json(marker.read_bytes())
        child.kill()
        child.wait(timeout=2)
        stdout, stderr = child.communicate(timeout=2)
        if stdout or stderr or child.returncode >= 0:
            raise CostError("demo_failed")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
    recovered = run("lost-receipt-replay", "append", "alpha-operator", lost)
    if recovered["receipt_id"] != committed["receipt_id"] or not recovered["replayed"]:
        raise CostError("demo_failed")

    race = {**later, "event_id": "concurrent-duplicate"}
    children = []
    try:
        for _ in range(2):
            children.append(
                subprocess.Popen(
                    command("append", "alpha-operator", race),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
            process_count += 1
        results = []
        for process in children:
            out, err = process.communicate(timeout=10)
            if process.returncode != 0 or err or len(out) > MAX_OUTPUT:
                raise CostError("demo_failed")
            results.append(json.loads(out)["result"])
        if results[0]["receipt_id"] != results[1]["receipt_id"] or sorted(
            row["replayed"] for row in results
        ) != [False, True]:
            raise CostError("demo_failed")
        observations.append(
            {"case": "concurrent-identical-processes", "responses": results}
        )
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    final = run("alpha-final-summary", "summary", "alpha-auditor", **scenario["period"])
    if final["events"] != 8:
        raise CostError("demo_failed")
    source = Path(__file__).parent
    hashes = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(source.rglob("*"))
        if p.is_file() and p.suffix in (".py", ".json")
    }
    hashes["package-init.py"] = hashlib.sha256(
        (source.parent / "__init__.py").read_bytes()
    ).hexdigest()
    try:
        installed = version("enterprise-ai-lab")
    except PackageNotFoundError:
        installed = None
    return {
        "schema_version": 1,
        "study": "fictional-tenant-cost-ledger-v1",
        "recorded_at": now(),
        "data_kind": scenario["data_kind"],
        "identity_kind": "trusted_local_launcher_not_authentication",
        "human_review_performed": False,
        "base_head": "8f6652279d976d9b29de9b81124960bda18e0f05",
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "installed_distribution": installed,
            "source_version": __version__,
        },
        "source_sha256": hashes,
        "observations": observations,
        "actual_processes": process_count,
        "all_owned_processes_reaped": True,
        "lost_receipt": {
            "committed_receipt_id": committed["receipt_id"],
            "killed_process_exit": child.returncode,
            "empty_stdout": True,
        },
        "rounding_control": {
            "aggregate_first": totals(["0.005", "0.005"]),
            "incorrect_sum_of_individually_rounded_lines": "0.00",
        },
        "limits": [
            "Authored fictional rates/usage/statements; not provider telemetry, prices, invoices or billing.",
            "One trusted local host and launcher; no authentication or administrator-proof audit.",
            "No payments, model/provider calls, ROI, tax, FX, credits or production deployment.",
            "Independent usage and statement input; original RAG/OCR/MCP results are not billed.",
        ],
    }
