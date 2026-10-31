"""Check six fixed recorded operability artifacts using only the standard library.

This starts no model, browser, issuer or accounting process. It verifies recorded
contracts and byte bindings, not execution authenticity, model quality, owner
review, production readiness or a connected inference/billing/admin system.
The root is a trusted operator input; descendant reads are fixed, bounded,
regular, no-follow and single-link. Failure output never echoes input bodies.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

SOURCE = "f8afd5e2e3104147a14dfabb7d9e575140a1c211"
IDENTITY = "92a6688e6df862934d53201a6ac39d8fba410a52"
SOURCE_MAP = "45177951318758c6cb7b77c510f0d125ead3303af874cebdbe5262a6f8c18bbe"
PROTOCOL = "f1e31ca721496b9c3d4ac0b8feff33d450807309e61ecef200935a76caaad57c"
HEX = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[0-9a-f]{32}\Z")
PRIVATE = re.compile(r"/Users/|/home/|/private/|Bearer\s|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
KINDS = ("offline", "edge", "costs", "admin")
LOCKS = {
    "requirements-mcp.lock": "f4cc23099d77c6eb3cb71f0645f0e6c804231046ceea3fe69a1e53785580ea10",
    "requirements-approval.lock": "ac5f71d1fc73e220abbb4e3f826b5b9749555504dbb278f0a54d52ac91c48702",
    "requirements-rag.lock": "d5d7efe222ad4fef0753b6f60223cf49f21ecfafef88d1f9665ed3604bb7552b",
    "requirements-ingestion.lock": "dc28732351792eee4cea6dd3cbe6b2f8bd1f09cca32e9e7638799c577f4a3752",
    "requirements-generator.lock": "3073a0b004579f674f1eacc524e47c2499332ebbb9fd3d5b0b3702113f0505fe",
}
BROWSER_CHECKS = [
    "Initial desk stays hidden until the real configuration response and initial view load complete; controlled response delay",
    "Actual Code+PKCE login uses a user account and an HttpOnly opaque cookie",
    "Keyboard review and confirmation change the real configured view only after confirmation",
    "Rollback appends a revision and the configuration audit remains visible",
    "Tab reload preserves only the account-bound immutable command without automatically submitting it",
    "Auditor inspects this tenant and audit while mutation controls stay disabled",
    "Beta work orders and audit remain tenant-local; a query-string tenant is rejected",
    "The rendered desk stays within a 390-pixel viewport",
    "No uncaught application errors in the actual browser",
]
ADMIN_CASES = [
    "unauthenticated", "login-admin-alpha", "login-admin-beta", "login-auditor-alpha",
    "login-observer-alpha", "observer-denied", "alpha-initial-config", "initial-work-orders",
    "auditor-write-denied", "csrf-denied", "origin-denied", "alpha-update", "alpha-exact-replay",
    "immutable-command-conflict", "stale-revision-conflict", "configured-read",
    "beta-independent-same-id", "beta-filtered-read", "rollback-new-revision",
    "rollback-restored-defaults", "auditor-tenant-history", "restart-requires-login",
    "login-admin-alpha", "restart-exact-replay", "restart-audit-preserved", "login-admin-alpha",
    "before_commit-old-session-denied", "login-admin-alpha", "before_commit-durable-before-retry",
    "before_commit-exact-retry", "before_commit-second-exact-replay", "login-admin-alpha",
    "after_commit-old-session-denied", "login-admin-alpha", "after_commit-durable-before-retry",
    "after_commit-exact-retry", "after_commit-second-exact-replay", "final-alpha-audit",
    "logout", "logged-out-denied",
]
COST_CASES = [
    "initialize", "alpha-operator:shared-001", "alpha-operator:tiny-001", "alpha-operator:tiny-002",
    "alpha-operator:pages-001", "alpha-operator:output-001", "beta-operator:shared-001",
    "beta-operator:output-001", "beta-operator:pages-001", "same-event-new-process",
    "conflicting-event", "alpha-summary-after-reopen", "beta-summary-after-reopen", "alpha-clean",
    "beta-clean", "missing", "extra", "wrong-amount", "currency", "rounded-total", "duplicate",
    "later-usage", "statement-frozen-after-later-append", "observer-denied", "auditor-cannot-append",
    "body-tenant-override", "unreadable-foreign-id", "absent-id-same-error", "lost-receipt-replay",
    "concurrent-identical-processes", "alpha-final-summary",
]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def same(left, right):
    """JSON equality distinguishes a boolean from an integer count."""
    return canonical(left) == canonical(right)


def keys(value, expected):
    return type(value) is dict and set(value) == set(expected)


def integer(value, low=0, high=2**63 - 1):
    return type(value) is int and low <= value <= high


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("UTC required")
    return parsed


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def scan(value, depth=0):
    if depth > 60:
        raise ValueError("structure bound")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("nonfinite number")
    if type(value) is str and (PRIVATE.search(value) or len(value) > 100_000):
        raise ValueError("private or oversized text")
    if isinstance(value, (dict, list)):
        if len(value) > 10_000:
            raise ValueError("container bound")
        for item in value.values() if type(value) is dict else value:
            scan(item, depth + 1)
        if type(value) is dict:
            for key in value:
                scan(key, depth + 1)


def parse(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    scan(value)
    return value


class Reader:
    """Read only fixed names supplied by this verifier, never JSON path fields."""
    def __init__(self, directory):
        path = Path(directory)
        if ".." in path.parts:
            raise ValueError("traversal")
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.seen, self.hashes, self.sizes = set(), {}, {}

    def read(self, parts, limit=512 * 1024):
        current, file_fd = os.dup(self.fd), None
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            before = os.fstat(file_fd)
            inode = before.st_dev, before.st_ino
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit or inode in self.seen:
                raise ValueError("artifact refused")
            blocks, remaining = [], limit + 1
            while remaining:
                block = os.read(file_fd, min(remaining, 65_536))
                if not block:
                    break
                blocks.append(block)
                remaining -= len(block)
            raw = b"".join(blocks)
            after = os.fstat(file_fd)
            admitted = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
            if len(raw) != before.st_size or len(raw) > limit or identity(before) != identity(after) or identity(before) != identity(admitted):
                raise ValueError("artifact changed")
            self.seen.add(inode)
            name = "/".join(parts)
            self.hashes[name], self.sizes[name] = hashlib.sha256(raw).hexdigest(), len(raw)
            return parse(raw)
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(current)

    def close(self):
        os.close(self.fd)


def offline(report, check, wheel):
    check("offline:report-fields", keys(report, "elapsed_seconds manifest manifest_sha256 network_control network_policy no_sandbox_delivery_observed observations platform python recorded_at same_frozen_wheel_repeat_manifest schema_version scope source_sha256 study".split()))
    check("offline:core-scope-only", report["study"] == "core-wheel-process-network-denial" and same(report["schema_version"], 1) and same(report["scope"], {"container_image_exercised": False, "core_lexical_demo": True, "filesystem_isolated": False, "human_review_performed": False, "optional_integrations_exercised": False, "physical_air_gap": False}))
    check("offline:unconfined-control-and-confined-probes", report["network_control"] == {"TCP": "delivered", "UDP": "delivered"} and report["network_policy"] == "(version 1) (allow default) (deny network*)" and report["no_sandbox_delivery_observed"] is True)
    m, o = report["manifest"], report["observations"]
    check("offline:manifest-core-not-model-delivery", keys(m, "artifacts bundle_kind container_images dependencies models project schema_version".split()) and m["bundle_kind"] == "enterprise_ai_lab_core_wheel" and m["models"] == m["dependencies"] == m["container_images"] == [] and same(m["project"], {"name": "enterprise-ai-lab", "version": "0.14.1"}) and same(m["schema_version"], 1) and report["manifest_sha256"] == hashlib.sha256((canonical(m) + "\n").encode()).hexdigest() and report["same_frozen_wheel_repeat_manifest"] is True)
    a = m["artifacts"]
    check("offline:exact-wheel-and-verifier-artifacts", len(a) == 2 and keys(a[0], "filename kind path sha256 size_bytes".split()) and a[0]["kind"] == "wheel" and a[0]["filename"] == "enterprise_ai_lab-0.14.1-py3-none-any.whl" and a[0]["sha256"] == wheel and a[0]["path"] == "artifacts/" + wheel + "/" + a[0]["filename"] and integer(a[0]["size_bytes"], 1, 2_000_000) and keys(a[1], "kind path sha256 size_bytes".split()) and a[1]["kind"] == "verifier" and a[1]["path"] == "verify_bundle.py" and a[1]["sha256"] == report["source_sha256"]["src/enterprise_ai_lab/offline_bundle.py"] and integer(a[1]["size_bytes"], 1))
    check("offline:observation-fields", keys(o, "demo demo_file_sha256 descendant_network_errno fresh_runtime import_inside_fresh_runtime installed_from_bundle installed_project_version network_probes pip_stdout_sha256 runtime_distributions system_site_packages".split()))
    check("offline:fresh-core-venv-install", o["fresh_runtime"] is o["import_inside_fresh_runtime"] is o["installed_from_bundle"] is True and o["system_site_packages"] is False and o["installed_project_version"] == "0.14.1" and o["runtime_distributions"] == ["enterprise-ai-lab", "pip"] and HEX.fullmatch(o["pip_stdout_sha256"]))
    check("offline:actual-child-denial", integer(o["descendant_network_errno"], 1, 13) and o["descendant_network_errno"] in (1, 13) and len(o["network_probes"]) == 2 and all(same(p, {"errno": p["errno"], "family": "IPv4", "permission_denied": True, "stage": stage, "transport": transport}) and type(p["errno"]) is int and p["errno"] in (1, 13) for p, transport, stage in zip(o["network_probes"], ["TCP", "UDP"], ["connect", "sendto"])))
    check("offline:lexical-safe-and-unsafe-control", same(o["demo"], {"safe_query_leakage": 0.0, "unsafe_query_leakage": 0.9}) and finite(report["elapsed_seconds"]))
    names = [prefix + file for prefix in ("", "unsafe_global_baseline/") for file in ("cases.jsonl", "documents.jsonl", "manifest.json", "predictions.jsonl", "report.json", "results.jsonl")]
    check("offline:complete-twelve-demo-files", set(o["demo_file_sha256"]) == set(names) and all(HEX.fullmatch(v) for v in o["demo_file_sha256"].values()) and all(o["demo_file_sha256"][f] == o["demo_file_sha256"]["unsafe_global_baseline/" + f] for f in ["cases.jsonl", "documents.jsonl"]))


def rss(value):
    return keys(value, "bytes native native_unit scope".split()) and integer(value["bytes"], 1, 2**50) and integer(value["native"], 1, 2**50) and value["native_unit"] == "bytes" and value["native"] == value["bytes"] and value["scope"] == "process_high_water_all_threads"


def edge(report, check):
    prompts = {"processing-limit": (184, 54, "49b1f773dcdca40e662f59c6458e3707bbfe72ad29d604e41246810c73894da8"), "maintenance-window": (186, 72, "f0465c9213fd1ff0332a0f203f60cf93a7e53b0ec5415be1d7d83253c1d7999f"), "archive-retention": (183, 63, "5397727049e6c926ea1edc42968541473033064e7af757d37b0233048aa2c759")}
    check("edge:report-fields", keys(report, "schema_version study protocol_sha256 source_sha256 protocol runtime_package_version recorded_at_utc host worker_lifetime_seconds cleanup_grace_seconds configuration_order configurations limitations".split()))
    p = report["protocol"]
    check("edge:frozen-protocol", same(report["schema_version"], 1) and report["study"] == "local-cpu-inference-v1" and report["protocol_sha256"] == PROTOCOL and keys(p, "schema_version data_kind protocol_id threads max_new_tokens repetitions cases".split()) and same(p["schema_version"], 1) and p["data_kind"] == "authored_fictional_inference_measurement" and p["protocol_id"] == "cpu-qwen-v1" and same(p["threads"], [1, 4]) and same(p["max_new_tokens"], 128) and same(p["repetitions"], 2) and report["runtime_package_version"] == "0.14.1" and same(report["worker_lifetime_seconds"], 180) and same(report["cleanup_grace_seconds"], 5) and same(report["configuration_order"], [1, 4]))
    # Bind the parsed protocol to its packaged bytes through the wheel inventory below.
    check("edge:three-fictional-cases", [c["id"] for c in p["cases"]] == ["processing-limit", "maintenance-window", "archive-retention"] and all(keys(c, "id question chunks".split()) and len(c["chunks"]) == 1 and all(keys(x, ["chunk_id", "text"]) for x in c["chunks"]) for c in p["cases"]))
    expected_schedule = [("first_request_after_load", 0, p["cases"][0])] + [("warm", rep, case) for rep in (1, 2) for case in p["cases"]]
    check("edge:two-fixed-configurations", len(report["configurations"]) == 2)
    for n, config in enumerate(report["configurations"]):
        prefix = "edge:threads-" + str([1, 4][n]) + ":"
        check(prefix + "fields-and-thread-order", keys(config, "threads ready parent_ready_elapsed_ns samples stopped terminal_error cleanup summary".split()) and same(config["threads"], [1, 4][n]))
        ready, meta = config["ready"], config["ready"]["metadata"]
        check(prefix + "ready", keys(ready, "machine metadata python rss schema_version source_sha256 startup_elapsed_ns system type".split()) and ready["type"] == "ready" and same(ready["schema_version"], 1) and ready["system"] == "Darwin" and ready["machine"] == "arm64" and rss(ready["rss"]) and integer(ready["startup_elapsed_ns"], 1) and integer(config["parent_ready_elapsed_ns"], ready["startup_elapsed_ns"]) and same(ready["source_sha256"], report["source_sha256"]))
        check(prefix + "actual-pinned-CPU-model", keys(meta, "cpu_threads device dtype evidence_validation files_sha256 license max_generated_tokens max_input_tokens model revision runtime sampling soft_decoding_deadline_seconds thinking".split()) and same(meta["cpu_threads"], config["threads"]) and meta["device"] == "cpu" and meta["dtype"] == "float32" and meta["model"] == "Qwen/Qwen3-0.6B" and meta["revision"] == "c1899de289a04d12100db370d81485cdf75e47ca" and meta["license"] == "Apache-2.0" and meta["sampling"] is meta["thinking"] is False and same(meta["max_generated_tokens"], 128) and same(meta["max_input_tokens"], 4096) and same(meta["soft_decoding_deadline_seconds"], 10) and meta["evidence_validation"] == "exact chunk ID and literal quote, not semantic entailment")
        samples = config["samples"]
        check(prefix + "seven-receipts-no-errors-dropped", len(samples) == 7 and config["terminal_error"] is None)
        totals = {"requests_with_receipts": len(samples), "warm_requests_with_receipts": 0, "accepted_literal_evidence": 0, "generation_failures": 0, "observed_model_generate_calls": 0, "observed_generated_tokens": 0, "requests_without_token_trace": 0}
        warm_times, highwater = [], ready["rss"]["bytes"]
        for i, (sample, (phase, repetition, case)) in enumerate(zip(samples, expected_schedule)):
            label = prefix + str(i) + ":"
            check(label + "fixed-input-order", keys(sample, "phase repetition case_id response".split()) and same([sample["phase"], sample["repetition"], sample["case_id"]], [phase, repetition, case["id"]]))
            r, trace = sample["response"], sample["response"]["trace"]
            check(label + "receipt-shape-time-and-call", keys(r, "answer elapsed_ns error id model_generate_calls model_generate_elapsed_ns ok rss trace type".split()) and r["type"] == "result" and r["id"] == case["id"] and type(r["ok"]) is bool and integer(r["elapsed_ns"], 1) and integer(r["model_generate_calls"], 0, 1) and integer(r["model_generate_elapsed_ns"], 0, r["elapsed_ns"]) and bool(r["model_generate_calls"]) == bool(r["model_generate_elapsed_ns"]) and rss(r["rss"]) and r["rss"]["bytes"] >= highwater)
            highwater = r["rss"]["bytes"]
            totals["observed_model_generate_calls"] += r["model_generate_calls"]
            totals["accepted_literal_evidence"] += r["ok"]
            totals["generation_failures"] += not r["ok"]
            totals["requests_without_token_trace"] += trace is None
            if phase == "warm":
                totals["warm_requests_with_receipts"] += 1
                warm_times.append(r["elapsed_ns"])
            if trace is not None:
                check(label + "raw-token-trace", keys(trace, "input_tokens generated_tokens prompt_sha256 raw_output_sha256 raw_output validated".split()) and integer(trace["input_tokens"], 1, 4096) and integer(trace["generated_tokens"], 0, 128) and type(trace["validated"]) is bool and trace["validated"] is r["ok"] and same(r["model_generate_calls"], 1) and HEX.fullmatch(trace["prompt_sha256"]) and len(trace["raw_output"].encode()) <= 16384 and hashlib.sha256(trace["raw_output"].encode()).hexdigest() == trace["raw_output_sha256"])
                totals["observed_generated_tokens"] += trace["generated_tokens"]
                check(label + "recorded-token-prompt-fingerprint-not-retokenization", same([trace["input_tokens"], trace["generated_tokens"], trace["prompt_sha256"]], list(prompts[case["id"]])))
            if r["ok"]:
                generated, answer = parse(trace["raw_output"].encode()), r["answer"]
                chunks = {row["chunk_id"]: row["text"] for row in case["chunks"]}
                evidence = generated["evidence"]
                check(label + "literal-check-not-entailment", keys(generated, ["answer", "evidence"]) and type(generated["answer"]) is str and 1 <= len(generated["answer"].strip()) <= 2000 and 1 <= len(evidence) <= len(chunks) and len({e["chunk_id"] for e in evidence}) == len(evidence) and all(keys(e, ["chunk_id", "quote"]) and e["chunk_id"] in chunks and type(e["quote"]) is str and e["quote"].strip() and len(e["quote"]) <= 2000 and e["quote"] in chunks[e["chunk_id"]] for e in evidence))
                expected = {**generated, "generator": "qwen3-0.6b-cpu", "human_review_required": True, "legal_advice": False, "generation": {k: trace[k] for k in ["generated_tokens", "input_tokens", "prompt_sha256", "raw_output_sha256"]}}
                check(label + "answer-linked-to-complete-trace", r["error"] is None and same(answer, expected))
            else:
                check(label + "failure-retained", r["answer"] is None and r["error"] == "generation_failed")
        times = sorted(warm_times)
        totals["warm_median_elapsed_ns_times_two"] = times[2] + times[3]
        check(prefix + "recomputed-all-error-denominators-and-median", same(config["summary"], totals) and totals["requests_with_receipts"] == 7 and totals["warm_requests_with_receipts"] == 6 and totals["observed_model_generate_calls"] == 7)
        check(prefix + "recorded-outcome-not-quality-score", totals["accepted_literal_evidence"] == 7 and totals["generation_failures"] == totals["requests_without_token_trace"] == 0 and totals["observed_generated_tokens"] == 432)
        stop = config["stopped"]
        check(prefix + "worker-accounting-highwater", keys(stop, "model_generate_calls requests rss type".split()) and stop["type"] == "stopped" and same(stop["requests"], 7) and same(stop["model_generate_calls"], totals["observed_model_generate_calls"]) and rss(stop["rss"]) and stop["rss"]["bytes"] >= highwater)
        cleanup = config["cleanup"]
        check(prefix + "actual-owned-worker-reaped", keys(cleanup, "spawned reaped returncode forced_kill elapsed_ns stderr_bytes stderr_sha256".split()) and cleanup["spawned"] is cleanup["reaped"] is True and cleanup["forced_kill"] is False and same(cleanup["returncode"], 0) and integer(cleanup["elapsed_ns"], 1) and integer(cleanup["stderr_bytes"]) and HEX.fullmatch(cleanup["stderr_sha256"]))


def decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def totals(values):
    exact = sum((Decimal(v) for v in values), Decimal(0))
    settled = exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return {"exact_total": decimal_text(exact), "settled_total": format(settled, ".2f"), "rounding_delta": decimal_text(settled - exact)}


def costs(report, check):
    check("costs:report-fields", keys(report, "schema_version study recorded_at data_kind identity_kind human_review_performed base_head runtime source_sha256 observations actual_processes all_owned_processes_reaped lost_receipt rounding_control limits".split()))
    check("costs:fictional-not-authentication-or-billing", same(report["schema_version"], 1) and report["study"] == "fictional-tenant-cost-ledger-v1" and report["data_kind"] == "authored_synthetic_usage_not_measurement" and report["identity_kind"] == "trusted_local_launcher_not_authentication" and report["human_review_performed"] is False and report["base_head"] == "8f6652279d976d9b29de9b81124960bda18e0f05")
    observations = report["observations"]
    check("costs:31-ordered-observations", [o["case"] for o in observations] == COST_CASES)
    by = {o["case"]: o for o in observations}
    failures = {"conflicting-event": "idempotency_conflict", "duplicate": "duplicate_statement_line", "observer-denied": "forbidden", "auditor-cannot-append": "forbidden", "body-tenant-override": "invalid_input", "unreadable-foreign-id": "not_found", "absent-id-same-error": "not_found"}
    for index, (name, o) in enumerate(by.items()):
        if name == "concurrent-identical-processes":
            check("costs:concurrent-pair-fields", keys(o, "case responses initial_attempts explicit_replay_count attempt_count".split()) and len(o["responses"]) == 2 and len(o["initial_attempts"]) == 2 and same(o["explicit_replay_count"], 0) and same(o["attempt_count"], 2) and all(keys(a, ["process_exit", "response"]) and same(a["process_exit"], 0) and same(a["response"], {"ok": True, "result": r}) for a, r in zip(o["initial_attempts"], o["responses"])))
            continue
        check("costs:observation-" + str(index) + ":envelope", keys(o, ["case", "process_exit", "response"]) and same(o["process_exit"], 2 if name in failures else 0) and (same(o["response"], {"ok": False, "error": failures[name]}) if name in failures else keys(o["response"], ["ok", "result"]) and o["response"]["ok"] is True))
    check("costs:initialize", same(by["initialize"]["response"]["result"], {"initialized": True}))
    result = lambda name: by[name]["response"]["result"]
    prices = {"input_tokens": Decimal("0.0005"), "output_tokens": Decimal("0.0015"), "embedding_tokens": Decimal("0.005"), "ocr_pages": Decimal("0.015")}
    rate_hash = "d0c9725a1eae84a7f6dec653586b1343f3099293e36dca398df7da9e9b67be93"
    definitions = [("alpha", "shared-001", "input_tokens", 1001), ("alpha", "tiny-001", "embedding_tokens", 1), ("alpha", "tiny-002", "embedding_tokens", 1), ("alpha", "pages-001", "ocr_pages", 3), ("alpha", "output-001", "output_tokens", 7), ("beta", "shared-001", "input_tokens", 2000), ("beta", "output-001", "output_tokens", 2), ("beta", "pages-001", "ocr_pages", 1), ("alpha", "later-event", "embedding_tokens", 1), ("alpha", "lost-receipt", "embedding_tokens", 1), ("alpha", "concurrent-duplicate", "embedding_tokens", 1)]
    receipts, tenant_rows, receipt_ids = {}, {"alpha": [], "beta": []}, set()
    for tenant, event_id, metric, quantity in definitions:
        label = tenant + "-operator:" + event_id
        r = (by["concurrent-identical-processes"]["responses"][0] if event_id == "concurrent-duplicate" else result("lost-receipt-replay") if event_id == "lost-receipt" else result("later-usage") if event_id == "later-event" else result(label))
        e = {"event_id": event_id, "metric": metric, "occurred_at": "2026-10-22T10:00:00Z", "quantity": quantity, "rate_version": "fictional-v1", "request_id": "request-shared"}
        check("costs:" + event_id + tenant + ":exact-tenant-event-charge", keys(r, "receipt_id tenant_id first_actor created_at event event_sha256 rate_card_sha256 ordinal charge replayed".split()) and ID.fullmatch(r["receipt_id"]) and r["receipt_id"] not in receipt_ids and r["tenant_id"] == tenant and r["first_actor"] == "local:" + tenant + "-operator" and same(r["event"], e) and same(r["ordinal"], len(tenant_rows[tenant]) + 1) and r["charge"] == decimal_text(Decimal(quantity) * prices[metric]) and r["rate_card_sha256"] == rate_hash and r["event_sha256"] == digest({"tenant_id": tenant, "event": e, "rate_card_sha256": rate_hash}) and type(r["replayed"]) is bool)
        utc(r["created_at"])
        receipt_ids.add(r["receipt_id"])
        receipts[(tenant, event_id)] = r
        tenant_rows[tenant].append({k: v for k, v in r.items() if k != "replayed"})
    first = receipts[("alpha", "shared-001")]
    check("costs:exact-replay-survives-new-process", first["replayed"] is False and same(result("same-event-new-process"), {**first, "replayed": True}))
    check("costs:initial-events-new-not-replays", all(receipts[(t, e)]["replayed"] is False for t, e, _, _ in definitions[:9]))
    for name, tenant, count in [("alpha-summary-after-reopen", "alpha", 5), ("beta-summary-after-reopen", "beta", 3), ("alpha-final-summary", "alpha", 8)]:
        expected = {"tenant_id": tenant, "currency": "USD", "start": "2026-10-22T00:00:00Z", "end": "2026-10-23T00:00:00Z", "events": count, **totals(r["charge"] for r in tenant_rows[tenant][:count])}
        check("costs:" + name + ":exact-aggregate-first", same(result(name), expected))
    for name in ["alpha-clean", "beta-clean", "missing", "extra", "wrong-amount", "currency", "rounded-total"]:
        r = result(name)
        tenant = "beta" if name == "beta-clean" else "alpha"
        count = 3 if tenant == "beta" else 5
        selected = tenant_rows[tenant][:count]
        statement = r["statement"]
        expected_lines = {row["event"]["event_id"]: row["charge"] for row in selected}
        declared = dict(expected_lines)
        if name == "missing": declared.pop("shared-001")
        if name == "extra": declared["absent-001"] = "0.1"
        if name == "wrong-amount": declared["shared-001"] = "0.5"
        expected_statement = {"currency": "EUR" if name == "currency" else "USD", "end": "2026-10-23T00:00:00Z", "start": "2026-10-22T00:00:00Z", "lines": [{"amount": v, "event_id": k} for k, v in sorted(declared.items())], "settled_total": "0.56" if name == "rounded-total" else "1.02" if tenant == "beta" else "0.57", "statement_id": tenant + "-" + (name.removeprefix(tenant + "-") if name in ["alpha-clean", "beta-clean"] else name)}
        currency = name == "currency"
        expected_result = {"currency": "USD", "missing_events": sorted(expected_lines.keys() - declared.keys()), "extra_events": sorted(declared.keys() - expected_lines.keys()), "amount_mismatches": [] if currency else [{"event_id": k, "expected": expected_lines[k], "declared": declared[k]} for k in sorted(expected_lines.keys() & declared.keys()) if expected_lines[k] != declared[k]], "currency_mismatch": currency, **totals(expected_lines.values()), "settled_total_mismatch": None if currency else name == "rounded-total", "matched": name in ["alpha-clean", "beta-clean"]}
        check("costs:" + name + ":independent-statement-anomaly-and-snapshot", keys(r, "receipt_id tenant_id first_actor created_at statement statement_sha256 rate_card_sha256 watermark snapshot_sha256 result replayed".split()) and ID.fullmatch(r["receipt_id"]) and r["receipt_id"] not in receipt_ids and r["tenant_id"] == tenant and r["first_actor"] == "local:" + tenant + "-auditor" and same(statement, expected_statement) and r["statement_sha256"] == digest(statement) and r["rate_card_sha256"] == rate_hash and same(r["watermark"], count) and r["snapshot_sha256"] == digest(selected) and same(r["result"], expected_result) and r["replayed"] is False)
        receipt_ids.add(r["receipt_id"])
    check("costs:statement-replay-keeps-original-watermark", same(result("statement-frozen-after-later-append"), {**result("alpha-clean"), "replayed": True}))
    lost = result("lost-receipt-replay")
    check("costs:lost-ACK-exact-durable-receipt", lost["replayed"] is True and same(report["lost_receipt"], {"committed_receipt_id": lost["receipt_id"], "killed_process_exit": -9, "empty_stdout": True}))
    concurrent = by["concurrent-identical-processes"]["responses"]
    check("costs:concurrent-writers-one-primary-effect", sorted(r["replayed"] for r in concurrent) == [False, True] and all(type(r["replayed"]) is bool for r in concurrent) and same({k: v for k, v in concurrent[0].items() if k != "replayed"}, {k: v for k, v in concurrent[1].items() if k != "replayed"}))
    check("costs:rounding-counterexample", same(report["rounding_control"], {"aggregate_first": {"exact_total": "0.01", "settled_total": "0.01", "rounding_delta": "0"}, "incorrect_sum_of_individually_rounded_lines": "0.00"}))
    check("costs:all-33-owned-processes-reaped", same(report["actual_processes"], 33) and report["all_owned_processes_reaped"] is True)


def admin(report, check):
    check("admin:report-fields", keys(report, "schema_version recorded_at experiment data runtime oidc fixture source_database_sha256_before_and_after source_sha256 identity_dist_sha256 browser_script_sha256 observations faults browser processes human_review_performed registry_release limitations".split()))
    check("admin:OIDC-not-owner-login-or-production", same(report["schema_version"], 1) and same(report["oidc"], {"flow": "authorization_code", "pkce": "S256", "audience": "urn:identity-boundary:admin", "client_id": "admin-portal"}) and report["human_review_performed"] is report["registry_release"] is False and same(report["fixture"], {"schema_version": 1, "fixture_sha256": "7f37ae73b4c670f22276e43170517b5c30a6ecaadbb7aba07231d20e271def56", "work_order_count": 18, "diagnostic_count": 6}) and HEX.fullmatch(report["source_database_sha256_before_and_after"]))
    observations = report["observations"]
    check("admin:40-ordered-HTTP-observations", [o["case"] for o in observations] == ADMIN_CASES)
    by = {o["case"]: o for o in observations}
    denies = {"unauthenticated": 401, "observer-denied": 403, "auditor-write-denied": 403, "csrf-denied": 403, "origin-denied": 403, "immutable-command-conflict": 409, "stale-revision-conflict": 409, "restart-requires-login": 401, "before_commit-old-session-denied": 401, "after_commit-old-session-denied": 401, "logged-out-denied": 401}
    messages = {401: ("unauthenticated", "Sign in again; this local session is missing or expired."), 403: ("forbidden", "The signed role, scope or browser CSRF check forbids this operation."), 409: ("conflict", "Refresh durable state; the revision or immutable command conflicts.")}
    response = lambda name: by[name]["response"]
    for index, o in enumerate(observations):
        name, status = o["case"], denies.get(o["case"], 200)
        check("admin:" + str(index) + ":HTTP-fields-status", keys(o, ["case", "http_status", "response"]) and same(o["http_status"], status))
        if name in denies:
            code, message = messages[status]
            check("admin:" + name + ":denial-withholds-data", same(o["response"], {"error": {"code": code, "message": message}}))
        if name.startswith("login-"):
            subject = name.removeprefix("login-")
            role, tenant = subject.split("-")
            role_name = {"admin": "config_admin", "auditor": "config_auditor", "observer": "observer"}[role]
            roles = [role_name] + ([] if role == "observer" else ["reader"])
            check("admin:" + str(index) + ":signed-user-role-scope", same(o["response"], {"authenticated": True, "user": {"subject": subject, "tenant_id": tenant, "roles": roles}, "can_read": role != "observer", "can_configure": role == "admin", "can_audit": role != "observer"}))
    default = {"default_status": "open", "work_order_page_size": 3}
    configs = [default, {"default_status": "open", "work_order_page_size": 1}, default, {"default_status": "open", "work_order_page_size": 4}, {"default_status": "open", "work_order_page_size": 5}]
    beta_config = {"default_status": "closed", "work_order_page_size": 2}
    for name, revision in [("alpha-initial-config", 0), ("rollback-restored-defaults", 2), ("before_commit-durable-before-retry", 2), ("after_commit-durable-before-retry", 4)]:
        check("admin:" + name + ":durable-CAS-state", same(response(name), {"tenant_id": "alpha", "revision": revision, "config": configs[revision]}))
    event_specs = [("alpha-update", "alpha", 1, "shared-command", "update", configs[1], None), ("beta-independent-same-id", "beta", 1, "shared-command", "update", beta_config, None), ("rollback-new-revision", "alpha", 2, "restore-zero", "rollback", configs[2], 0), ("before_commit-exact-retry", "alpha", 3, "fault-before_commit", "update", configs[3], None), ("after_commit-exact-retry", "alpha", 4, "fault-after_commit", "update", configs[4], None)]
    previous, events = "0" * 64, []
    for seq, (name, tenant, revision, command_id, action, config, target) in enumerate(event_specs, 1):
        command = {"command_id": command_id, "expected_revision": revision - 1, "action": action, **({"target_revision": target} if action == "rollback" else {"config": config})}
        event = {"sequence": seq, "tenant_id": tenant, "subject": "admin-" + tenant, "command_id": command_id, "action": action, "command_sha256": digest({"tenant_id": tenant, "subject": "admin-" + tenant, "client_id": "admin-portal", "command": command}), "revision": revision, "previous_revision": revision - 1, "configuration_sha256": digest(config), "previous_audit_hash": previous}
        previous = digest(event)
        expected_receipt = {k: event[k] for k in ["tenant_id", "command_id", "action", "revision", "previous_revision", "configuration_sha256"]} | {"audit_sequence": seq, "audit_hash": previous}
        check("admin:" + name + ":atomic-receipt-chain-CAS", same(response(name), expected_receipt))
        events.append({**event, "audit_hash": previous})
    for replay, original in [("alpha-exact-replay", "alpha-update"), ("restart-exact-replay", "rollback-new-revision"), ("before_commit-second-exact-replay", "before_commit-exact-retry"), ("after_commit-second-exact-replay", "after_commit-exact-retry")]:
        check("admin:" + replay + ":exact-receipt-not-new-revision", same(response(replay), response(original)))
    alpha_events = [e for e in events if e["tenant_id"] == "alpha"]
    for name, expected in [("auditor-tenant-history", alpha_events[:2]), ("restart-audit-preserved", alpha_events[:2]), ("final-alpha-audit", alpha_events)]:
        check("admin:" + name + ":tenant-audit-no-foreign-events", same(response(name), {"tenant_id": "alpha", "events": expected, "total": len(expected)}))
    for name, tenant, revision, config, numbers, next_after in [("initial-work-orders", "alpha", 0, default, [1], None), ("configured-read", "alpha", 1, configs[1], [1], None), ("beta-filtered-read", "beta", 1, beta_config, [3, 6], "WO-0006")]:
        rows = [{"tenant_id": tenant, "work_order_id": f"WO-{n:04}", "asset_id": f"AS-{((n-1)%3)+1:03}", "title": tenant.title() + f" synthetic inspection {n}", "status": "closed" if tenant == "beta" else "open", "summary": tenant.upper() + f"_READER_{n:02}: Authored example work order; inspect the demonstration asset and record a local observation. This is fictional study data."} for n in numbers]
        check("admin:" + name + ":source-read-filtered-tenant-role-config", same(response(name), {"configuration": {"tenant_id": tenant, "revision": revision, "config": config}, "items": rows, "next_after": next_after}))
    check("admin:two-distinct-commit-faults", len(report["faults"]) == 2 and [f["phase"] for f in report["faults"]] == ["before_commit", "after_commit"])
    for phase, f, before, after, page in zip(["before_commit", "after_commit"], report["faults"], [2, 4], [3, 4], [4, 5]):
        immutable = {"command_id": "fault-" + phase, "expected_revision": after - 1, "action": "update", "config": {"work_order_page_size": page, "default_status": "open"}}
        check("admin:" + phase + ":explicit-identical-retry-after-lost-response", keys(f, "phase controlled_fault response_lost immutable_command_sha256 revision_after_reopen_before_retry revision_after_explicit_retry receipt same_id_and_body automatic_retry".split()) and f["controlled_fault"] is f["response_lost"] is f["same_id_and_body"] is True and f["automatic_retry"] is False and same(f["revision_after_reopen_before_retry"], before) and same(f["revision_after_explicit_retry"], after) and f["immutable_command_sha256"] == digest(immutable) and same(f["receipt"], response(phase + "-exact-retry")))
    check("admin:logout-clears-account-session", same(response("logout"), {"logged_out": True}))
    browser = report["browser"]
    check("admin:actual-browser-checks-not-human-review", keys(browser, "schema_version recorded_at passed experiment checks human_review_performed limitations browser screenshots".split()) and same(browser["schema_version"], 1) and browser["passed"] is True and browser["human_review_performed"] is False and [x["name"] for x in browser["checks"]] == BROWSER_CHECKS and all(x["passed"] is True and (same(x, {"name": x["name"], "passed": True, "viewport_width": 390}) if x["name"] == BROWSER_CHECKS[7] else keys(x, ["name", "passed"])) for x in browser["checks"]) and browser["browser"]["name"] == "Chromium" and browser["screenshots"] == ["admin-desktop.png", "admin-mobile.png"])
    check("admin:seven-owned-service-children-reaped", same(report["processes"], {"count": 7, "all_reaped": True, "scope": "issuer-and-portal-service-children"}))


def provenance(value, inventory, reports, reader, check):
    check("provenance:fixed-fields", keys(value, "schema_version case_id runtime_source_revision runtime_source_clean_at_start runtime_package wheel_inventory wheel_sha256 four_separate_experiments core_bundle_includes_optional_runtime_or_model costs_are_inference_telemetry admin_controls_inference human_review_performed owner_personal_study_recorded full_roadmap_15_complete exact_unchanged_locks runtime_environments identity model_assets edge_network_denial experiments public_safe_scan development_browser".split()))
    check("provenance:case-source-and-limits", same(value["schema_version"], 1) and value["case_id"] == "operable-local-ai" and value["runtime_source_revision"] == SOURCE and value["runtime_source_clean_at_start"] is True and value["runtime_package"] == "enterprise-ai-lab==0.14.1" and value["four_separate_experiments"] is True and all(value[k] is False for k in ["core_bundle_includes_optional_runtime_or_model", "costs_are_inference_telemetry", "admin_controls_inference", "human_review_performed", "owner_personal_study_recorded", "full_roadmap_15_complete"]))
    check("provenance:fixed-wheel-inventory-path", value["wheel_inventory"] == "wheel-source-inventory.json")
    files = inventory["package_files"]
    check("provenance:113-source-bound-wheel-files", keys(inventory, "source_revision wheel wheel_sha256 package_files all_package_bytes_match_committed_source".split()) and inventory["source_revision"] == SOURCE and inventory["wheel"] == "enterprise_ai_lab-0.14.1-py3-none-any.whl" and HEX.fullmatch(inventory["wheel_sha256"]) and inventory["wheel_sha256"] == value["wheel_sha256"] and inventory["all_package_bytes_match_committed_source"] is True and len(files) == 113 and digest(files) == SOURCE_MAP and all(k.startswith("enterprise_ai_lab/") and HEX.fullmatch(v) for k, v in files.items()))
    fingerprints = {k.removeprefix("enterprise_ai_lab/"): v for k, v in files.items()}
    expected_sources = {
        "edge": {"edge/__init__.py", "edge/__main__.py", "edge/contracts.py", "edge/experiment.py", "edge/worker.py", "rag/neural.py"},
        "costs": {"__init__.py", "__main__.py", "contracts.py", "data/rates-v1.json", "data/scenario-v1.json", "experiment.py", "ledger.py", "package-init.py"},
        "admin": {"enterprise_ai_lab/" + k for k in ["admin/__init__.py", "admin/__main__.py", "admin/api.py", "admin/auth.py", "admin/core.py", "admin/integration.py", "admin/static/app.css", "admin/static/app.js", "admin/static/index.html", "review/sessions.py", "rag/auth.py", "rag/contracts.py", "rag/integration.py", "mcp_gateway/store.py", "mcp_gateway/data/work-orders-v1.json"]},
    }
    check("provenance:offline-script-and-wheel-verifier-source", same(reports["offline"]["source_sha256"], {"scripts/offline_bundle_demo.py": "d7b5a7f5a2a1a7c36b4976a793c550a47a33c4821ace1e5b720c1e1d3f11136b", "src/enterprise_ai_lab/offline_bundle.py": fingerprints["offline_bundle.py"]}))
    for kind, expected in expected_sources.items():
        source = reports[kind]["source_sha256"]
        if kind == "edge": bound = all(fingerprints[k] == v for k, v in source.items())
        elif kind == "admin": bound = all(files[k] == v for k, v in source.items())
        else: bound = all(fingerprints["__init__.py" if k == "package-init.py" else "costs/" + k] == v for k, v in source.items())
        check("provenance:" + kind + ":all-package-source-fingerprints", set(source) == expected and bound)
    check("provenance:frozen-input-protocol-bytes-and-values", files["enterprise_ai_lab/edge/data/requests-v1.json"] == PROTOCOL and digest(reports["edge"]["protocol"]) == "7fa69f978e83f56d29532c5c9ce97b08a2a550eb109ef7431e22fcde94309944")
    check("provenance:five-exact-unchanged-locks", same(value["exact_unchanged_locks"], LOCKS))
    environments = value["runtime_environments"]
    check("provenance:two-reused-dependency-environments", keys(environments, ["generator", "approval"]))
    package_digests = {"generator": "01e6d85e8d4e36a0be96dd3f8344a5b834cf7d1d73f4df62fd5113de097609ff", "approval": "be90d31e62d92c48fb015a6fbfa64226e95a393969af3f93c1f4711f82f028c5"}
    for kind in ["generator", "approval"]:
        e = environments[kind]
        check("provenance:" + kind + ":reused-runtime-new-wheel-target", keys(e, "python packages dependency_environment package_target installed_module_outside_checkout private_runtime_readback_receipt_sha256".split()) and e["python"].startswith("3.13.1 ") and e["dependency_environment"] == "reused_unchanged" and e["package_target"] == "new_installed_wheel_target" and e["installed_module_outside_checkout"] is True and HEX.fullmatch(e["private_runtime_readback_receipt_sha256"]) and digest(e["packages"]) == package_digests[kind])
    check("provenance:recorded-interpreters-and-installed-distribution", all(c["ready"]["python"] == "3.13.1" for c in reports["edge"]["configurations"]) and reports["offline"]["python"] == reports["costs"]["runtime"]["python"] == reports["admin"]["runtime"]["python"] == "3.13.1" and reports["costs"]["runtime"]["installed_distribution"] == reports["admin"]["runtime"]["package"] == "0.14.1")
    check("provenance:reported-historical-cost-source-version-not-new-wheel-version", reports["costs"]["runtime"]["source_version"] == "0.11.0")
    identity = value["identity"]
    check("provenance:Identity-source-node-dist-binding", keys(identity, "source_revision dist_sha256 package_lock_sha256 node_version node_binary_sha256".split()) and identity["source_revision"] == IDENTITY and identity["node_version"] == "v24.19.0" and identity["node_binary_sha256"] == "060b970693b04c9b5664c68014c6ece7c30ec7968d55c8764776d8a07e154056" and digest(identity["dist_sha256"]) == "db28b0307cc336a01c23653945d809774aefe7fb0c78494a8e3eee4f6d19d864" and set(identity["dist_sha256"]) == {"dist/admin-bridge.js", "dist/admin-issuer.js", "dist/http.js", "package.json", "package-lock.json"} and all(HEX.fullmatch(v) for v in identity["dist_sha256"].values()) and same(identity["dist_sha256"], reports["admin"]["identity_dist_sha256"]) and identity["package_lock_sha256"] == identity["dist_sha256"]["package-lock.json"])
    check("provenance:pinned-local-model-assets", digest(value["model_assets"]) == "23c4dcaba31a9da67809aba2ab74b05a9e8a832e3cbad4144e024e399d50fd78" and all(same(c["ready"]["metadata"]["files_sha256"], value["model_assets"]) and all(c["ready"]["metadata"]["runtime"][k] == environments["generator"]["packages"][k] for k in ["safetensors", "tokenizers", "torch", "transformers"]) for c in reports["edge"]["configurations"]))
    browser = value["development_browser"]
    observed = reports["admin"]["browser"]["browser"]
    check("provenance:locked-development-browser-launcher-only", keys(browser, "package_lock_sha256 locked_playwright_version observed_playwright_version observed_chromium_version chromium_launcher_sha256 chromium_launcher_bytes chromium_fingerprint_scope private_post_execution_readback_receipt_sha256".split()) and browser["package_lock_sha256"] == "5ef046ba8d7d00bc1be39d923790ea86b433cb421bd2ce9c2404568320dffe89" and browser["locked_playwright_version"] == browser["observed_playwright_version"] == observed["playwright"] == "1.62.1" and browser["observed_chromium_version"] == observed["version"] and browser["chromium_launcher_sha256"] == "a596b1cfc6353e987fcec8d71a23a28cd6a9e7a6b4e20b908e4c4fcffe51158e" and same(browser["chromium_launcher_bytes"], 52112) and browser["chromium_fingerprint_scope"] == "launcher_only_not_entire_distribution" and HEX.fullmatch(browser["private_post_execution_readback_receipt_sha256"]))
    network = value["edge_network_denial"]
    check("provenance:edge-process-network-denial", keys(network, "TCP UDP policy scope private_command_stdout_sha256".split()) and all(keys(network[t], ["errno", "permission_denied"]) and type(network[t]["errno"]) is int and network[t]["errno"] in (1, 13) and network[t]["permission_denied"] is True for t in ["TCP", "UDP"]) and network["policy"] == "(version 1) (allow default) (deny network*)" and network["scope"] == "process_network_denial_not_physical_air_gap" and HEX.fullmatch(network["private_command_stdout_sha256"]))
    experiments = value["experiments"]
    check("provenance:four-separate-recorded-experiments", set(experiments) == set(KINDS))
    previous_end, receipts = None, set()
    for name in KINDS:
        e = experiments[name]
        start, end = utc(e["execution_started_at"]), utc(e["execution_finished_at"])
        recorded = utc(reports[name]["recorded_at_utc" if name == "edge" else "recorded_at"])
        check("provenance:" + name + ":exact-artifact-and-one-attempt", keys(e, "report sha256 bytes execution_started_at execution_finished_at execution_elapsed_s execution_exit_code experiment_attempts private_exact_command_receipt_sha256 raw_private_and_candidate_bytes_identical runtime_asset_downloads_disabled".split()) and e["report"] == name + "/demo.json" and e["sha256"] == reader.hashes[name + "/demo.json"] and same(e["bytes"], reader.sizes[name + "/demo.json"]) and same(e["execution_exit_code"], 0) and same(e["experiment_attempts"], 1) and e["raw_private_and_candidate_bytes_identical"] is e["runtime_asset_downloads_disabled"] is True)
        check("provenance:" + name + ":time-and-distinct-private-receipt", start < end and (previous_end is None or previous_end <= start) and -1 < (recorded - start).total_seconds() <= (end - start).total_seconds() and finite(e["execution_elapsed_s"]) and abs(e["execution_elapsed_s"] - (end - start).total_seconds()) < .1 and HEX.fullmatch(e["private_exact_command_receipt_sha256"]) and e["private_exact_command_receipt_sha256"] not in receipts)
        previous_end = end
        receipts.add(e["private_exact_command_receipt_sha256"])
    check("provenance:recorded-public-safe-scan-is-bounded-pattern-check", same(value["public_safe_scan"], {"files": 6, "matches": {"absolute_home_or_private_path": 0, "bearer_credential": 0, "JWT": 0}}))


def verify(directory):
    checks, reader = [], None
    def check(name, condition):
        checks.append({"check": name, "passed": bool(condition)})
    try:
        reader = Reader(directory)
        reports = {name: reader.read((name, "demo.json")) for name in KINDS}
        p = reader.read(("provenance.json",))
        inventory = reader.read(("wheel-source-inventory.json",))
        offline(reports["offline"], check, p["wheel_sha256"])
        edge(reports["edge"], check)
        costs(reports["costs"], check)
        admin(reports["admin"], check)
        provenance(p, inventory, reports, reader, check)
        for kind, key, expected in [("edge", "limitations", "417a5ba0f82337374e8b570d34c274a175bf2a057654b1b06677343895b5b741"), ("costs", "limits", "56e566dd5b06d7b8da59dc6bb7131a5d4152d729f9ca8e32d6842daabfa49f93"), ("admin", "limitations", "d8acbf62fe5c08a8b4d28faef9396c4ad533eeb2368222fd18f4298eea6c6ed2")]:
            check(kind + ":recorded-limitations-retained", digest(reports[kind][key]) == expected)
        check("admin:recorded-browser-limitations-retained", digest(reports["admin"]["browser"]["limitations"]) == "60be400a058445949a136d663dca1c90f103bad58027f0265d0f014cf3d09061")
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, UnicodeError, ArithmeticError):
        check("complete-readable-recorded-contract", False)
    finally:
        if reader is not None:
            reader.close()
    return {"schema_version": 1, "scope": "recorded_four_separate_operability_contracts_only", "passed": bool(checks) and all(c["passed"] for c in checks), "checks": checks, "artifact_sha256": {} if reader is None else reader.hashes,
        "denominators": {"separate_experiments": 4, "model_requests": 14, "model_generate_calls": 14, "warm_requests": 12, "cost_observations": 31, "admin_HTTP_observations": 40, "admin_browser_checks": 9},
        "runtime_reexecuted": False, "model_inference_performed": False, "browser_started": False, "connected_inference_cost_admin_system_verified": False, "owner_review_or_study_established": False,
        "limitations": ["Matching hashes and recorded contracts do not authenticate execution or prove current behavior.", "Four separate experiments are not integrated billing telemetry or inference administration.", "Core packaging creates a fresh venv; optional runtimes reuse dependency environments with a new wheel target.", "Literal evidence acceptance is not semantic entailment or a model-quality benchmark; timing/RSS are small-sample process observations.", "Recorded process network denial is not a physical air gap or filesystem isolation; browser launcher hash is not the entire distribution.", "Tokenization, signatures, OIDC and command execution are not rerun; recorded token/prompt counters and metadata are checked against this frozen case."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    result = verify(parser.parse_args(argv).directory)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
