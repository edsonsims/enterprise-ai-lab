"""Verify fixed recorded reliability artifacts using only the standard library.

This reads three separate experiments. It starts no service, performs no SDK
replay and cannot authenticate an execution claim. A matching hash binds bytes;
it does not establish fresh execution, human review or a connected write path.
The root directory is a trusted operator input; every descendant read is fixed,
bounded, regular, no-follow and single-link. Input bodies/paths are never echoed.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

SOURCE_REVISION = "d4fa3236e047d63ea7d34f6f26debc31513520e6"
IDENTITY_REVISION = "92a6688e6df862934d53201a6ac39d8fba410a52"
SOURCE_MAP_SHA256 = "45177951318758c6cb7b77c510f0d125ead3303af874cebdbe5262a6f8c18bbe"
LOCKS = {
    "requirements-mcp.lock": "f4cc23099d77c6eb3cb71f0645f0e6c804231046ceea3fe69a1e53785580ea10",
    "requirements-approval.lock": "ac5f71d1fc73e220abbb4e3f826b5b9749555504dbb278f0a54d52ac91c48702",
    "requirements-rag.lock": "d5d7efe222ad4fef0753b6f60223cf49f21ecfafef88d1f9665ed3604bb7552b",
    "requirements-ingestion.lock": "dc28732351792eee4cea6dd3cbe6b2f8bd1f09cca32e9e7638799c577f4a3752",
    "requirements-generator.lock": "3073a0b004579f674f1eacc524e47c2499332ebbb9fd3d5b0b3702113f0505fe",
}
HEX = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[0-9a-f]{32}\Z")
TOKEN = re.compile(r"\[\[PII:([0-9a-f]{32})\]\]")
PRIVATE = re.compile(r"/Users/|/home/|/private/|Bearer\s|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
NOTE = {"title": "Synthetic study note", "body": "Publish this fixture only to the local SQLite notifier."}
TEXT = "Fictional note for Mara Vale: nina@privacy.example, +1 202-555-0114, card 4242 4242 4242 4242."
ROW = {"asset_id": "AS-001", "status": "open", "summary": "ALPHA_READER_01: Authored example work order; inspect the demonstration asset and record a local observation. This is fictional study data.", "tenant_id": "alpha", "title": "Alpha synthetic inspection 1", "work_order_id": "WO-0001"}
READ = {"data": {"item": ROW}, "meta": {"operation": "get_work_order", "tenant_id": "alpha"}}
# outcome, ordered attempt outcomes, expected primary/secondary durable phases
CHAOS = {
    "healthy_primary": ("success", ["response"], ["intent", "success"], []),
    "primary_killed_after_durable_outcome": ("success", ["transport_closed", "response"], ["intent", "success"], ["intent", "success"]),
    "primary_disconnect_before_read": ("success", ["transport_closed", "response"], [], ["intent", "success"]),
    "primary_killed_after_intent": ("success", ["transport_closed", "response"], ["intent"], ["intent", "success"]),
    "primary_stalled_after_read": ("success", ["transport_timeout", "response"], ["intent", "success"], ["intent", "success"]),
    "both_transports_disconnect": ("attempts_exhausted", ["transport_closed", "transport_closed"], [], []),
    "total_work_deadline": ("total_deadline", ["transport_timeout"], ["intent", "success"], []),
    "terminal_permission_denial": ("tool_error", ["tool_error"], ["intent", "forbidden"], []),
    "terminal_hidden_row": ("tool_error", ["tool_error"], ["intent", "not_found"], []),
    "terminal_invalid_tenant_argument": ("tool_error", ["tool_error"], ["intent", "invalid_request"], []),
    "terminal_source_busy": ("tool_error", ["tool_error"], ["intent", "resource_busy"], []),
    "terminal_audit_unavailable": ("tool_error", ["tool_error"], [], []),
    "terminal_wire_provenance_mismatch": ("provenance_mismatch", ["response"], ["intent", "success"], []),
    "explicit_fresh_primary_recovery": ("success", ["response"], ["intent", "success"], []),
    "caller_cancellation_after_durable_outcome": ("caller_cancelled", ["caller_cancelled"], ["intent", "success"], []),
}
APPROVAL_STATUS = dict.fromkeys([
    "request-created", "exact-create-replay", "dual-role-request-created",
    "pending-after-server-worker-edge-restart", "approve-before-effect-ack",
    "published-once-after-worker-kill-and-server-restart", "exact-decision-replay",
    "same-request-id-other-tenant", "beta-approved", "rejection-request-created",
    "rejected", "independent-reviewer-rejects-self-request", "expiry-request-created",
    "expired-while-worker-absent", "published-after-edge-restart"], 200)
APPROVAL_STATUS.update(dict.fromkeys([
    "changed-create-conflict", "tampered-snapshot-denied", "self-approval-denied",
    "changed-command-id-payload-conflict", "changed-actor-same-command-conflict",
    "changed-digest-same-command-conflict", "late-decision-denied"], 409))
APPROVAL_STATUS.update({"forged-token-denied": 401, "rag-audience-denied": 401,
    "cross-tenant-denied": 403, "wrong-role-denied": 403, "missing-scope-denied": 403,
    "body-actor-spoof-denied": 422, "unknown-request": 404})
A_ERRORS = {
    401: ("invalid_token", "Token invalid for the approval resource."),
    403: ("forbidden", "This principal cannot perform that tenant's approval operation."),
    404: ("not_found", "No approval request exists in this tenant scope."),
    409: ("approval_conflict", "Request or decision conflicts, is expired, or has a stale snapshot."),
    422: ("invalid_request", "Only the bounded approval request fields are accepted."),
}
CREATED = {
    ("alpha", "publish-study-note"): "request-created",
    ("beta", "publish-study-note"): "same-request-id-other-tenant",
    ("alpha", "reject-study-note"): "rejection-request-created",
    ("alpha", "self-review"): "dual-role-request-created",
    ("alpha", "expire-study-note"): "expiry-request-created",
}
HISTORIES = {
    "alpha-publish-study-note.json": ("alpha", "publish-study-note", "approved"),
    "beta-publish-study-note.json": ("beta", "publish-study-note", "approved"),
    "alpha-reject-study-note.json": ("alpha", "reject-study-note", "rejected"),
    "alpha-self-review.json": ("alpha", "self-review", "rejected"),
    "alpha-expire-study-note.json": ("alpha", "expire-study-note", "expired"),
}
PRIVACY_STATUS = {"redact": 200, "restore": 200, "cross_tenant_path": 403,
    "other_tenant_context": 404, "other_client_same_tenant": 404,
    "redactor_cannot_restore": 403, "missing_restore_scope": 403,
    "wrong_audience": 401, "missing_token": 401, "identity_override": 400,
    "extra_body_identity": 400, "duplicate_json": 400, "invalid_utf8": 400,
    "body_limit": 413, "reserved_literal": 400, "same_values_new_context": 200,
    "copied_other_context_token": 400, "actual_http_processor": 200,
    "processor_injected_token": 400, "processor_failure": 502,
    "actual_sqlite_lock": 503, "after_lock_released": 200,
    "restore_after_resource_restart": 200, "short_context_created": 200,
    "actual_context_expiry": 404}
P_ERRORS = {
    "cross_tenant_path": ("forbidden", "This principal cannot perform this operation."),
    "other_tenant_context": ("context_unavailable", "The context is missing, expired or unavailable."),
    "other_client_same_tenant": ("context_unavailable", "The context is missing, expired or unavailable."),
    "redactor_cannot_restore": ("forbidden", "This principal cannot perform this operation."),
    "missing_restore_scope": ("forbidden", "This principal cannot perform this operation."),
    "wrong_audience": ("invalid_token", "A valid token for this resource is required."),
    "missing_token": ("invalid_token", "A valid token for this resource is required."),
    "identity_override": ("invalid_request", "The request does not match this operation."),
    "extra_body_identity": ("invalid_request", "The request does not match this operation."),
    "duplicate_json": ("invalid_json", "The JSON body is malformed or ambiguous."),
    "invalid_utf8": ("invalid_json", "The JSON body is malformed or ambiguous."),
    "body_limit": ("body_too_large", "The body exceeds the fixed byte limit."),
    "reserved_literal": ("invalid_input", "The text or configuration exceeds the supported contract."),
    "copied_other_context_token": ("invalid_token_reference", "The response contains an unavailable placeholder."),
    "processor_injected_token": ("invalid_token_reference", "The response contains an unavailable placeholder."),
    "processor_failure": ("processor_unavailable", "The configured local processor did not complete safely."),
    "actual_sqlite_lock": ("resource_busy", "The local resource is busy; retry explicitly."),
    "actual_context_expiry": ("context_unavailable", "The context is missing, expired or unavailable."),
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _object(pairs):
    value = {}
    for k, v in pairs:
        if k in value:
            raise ValueError("duplicate member")
        value[k] = v
    return value


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _utc(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.utcoffset() != timezone.utc.utcoffset(date):
        raise ValueError("UTC required")
    return date


def _finite(value):
    return type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value)


def _envelope(pair):
    return {"error": {"code": pair[0], "message": pair[1]}}


def _payload(value):
    payloads = value["payloads"]
    if len(payloads) != 1 or base64.b64decode(payloads[0]["metadata"]["encoding"], validate=True) != b"json/plain":
        raise ValueError("history encoding")
    return _json(base64.b64decode(payloads[0]["data"], validate=True))


def _scan(value, depth=0):
    """Check known public-pattern canaries, including every JSON/plain payload."""
    if depth > 80:
        raise ValueError("structure bound")
    count = 0
    if type(value) is str and PRIVATE.search(value):
        raise ValueError("private content")
    if isinstance(value, list):
        count += sum(_scan(child, depth + 1) for child in value)
    elif isinstance(value, dict):
        count += sum(_scan(k, depth + 1) + _scan(v, depth + 1) for k, v in value.items())
        if "metadata" in value and "data" in value and isinstance(value["metadata"], dict) and "encoding" in value["metadata"]:
            decoded = _payload({"payloads": [value]})
            count += 1 + _scan(decoded, depth + 1)
    return count


class Reader:
    """Only callers' fixed relative names, never JSON-supplied filesystem paths."""
    def __init__(self, directory):
        path = Path(directory)
        if ".." in path.parts:
            raise ValueError("traversal")
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.seen = set()
        self.hashes = {}
        self.sizes = {}
        self.decoded_payloads = 0

    def read(self, parts, limit=1024 * 1024):
        current = os.dup(self.fd)
        file_fd = None
        try:
            for part in parts[:-1]:
                current_next = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = current_next
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            before = os.fstat(file_fd)
            inode = (before.st_dev, before.st_ino)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit or inode in self.seen:
                raise ValueError("artifact refused")
            raw = os.read(file_fd, limit + 1)
            after = os.fstat(file_fd)
            admitted = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
            if len(raw) != before.st_size or len(raw) > limit or identity(before) != identity(after) or identity(before) != identity(admitted):
                raise ValueError("artifact changed")
            self.seen.add(inode)
            self.hashes["/".join(parts)] = hashlib.sha256(raw).hexdigest()
            self.sizes["/".join(parts)] = len(raw)
            value = _json(raw)
            self.decoded_payloads += _scan(value)
            return value
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(current)

    def close(self):
        os.close(self.fd)


def _cases(report, expected, check, prefix):
    values = report["observations"]
    names = [x["case"] for x in values]
    valid = len(names) == len(expected) and len(set(names)) == len(names) and set(names) == set(expected)
    check(prefix + ":complete-case-denominator", valid)
    if not valid:
        raise ValueError("case inventory")
    return {x["case"]: x for x in values}


def _chaos(report, check):
    check("chaos:report-shape", set(report) == {"schema_version", "study", "recorded_at", "data_kind", "identity_kind", "profile", "human_review_performed", "runtime", "source_sha256", "requirements_mcp_lock_sha256", "source_databases_before", "source_databases_after", "source_unchanged", "observations", "actual_processes", "all_observed_children_reaped", "audits", "audit_chains_verified", "observed_degradation", "limits"})
    check("chaos:identity-and-study", report["schema_version"] == 1 and type(report["schema_version"]) is int and report["study"] == "fixture-bound-local-read-failover-v1" and report["data_kind"] == "authored_synthetic" and report["identity_kind"] == "trusted_local_launcher_profile_not_authentication" and report["profile"] == "alpha-reader" and report["human_review_performed"] is False)
    by = _cases(report, CHAOS, check, "chaos")
    concatenated = {"primary": [], "secondary": []}
    pids = []
    for name, (outcome, attempts, primary, secondary) in CHAOS.items():
        value = by[name]
        fields = {"case", "kind", "test_only_injections", "operation", "arguments", "budget", "admission_seconds", "whole_read_seconds", "outcome", "response", "attempts", "audit_events_added"}
        if name == "caller_cancellation_after_durable_outcome":
            fields = fields - {"admission_seconds"} | {"cancellation_did_not_rollback_read"}
            check("chaos:cancellation-keeps-completed-read-audit", value["cancellation_did_not_rollback_read"] is True)
        check("chaos:" + name + ":observation-shape", set(value) == fields)
        operation, arguments = "get_work_order", {"work_order_id": "WO-0001"}
        if name == "terminal_permission_denial": operation, arguments = "get_asset_diagnostics", {"asset_id": "AS-001"}
        if name == "terminal_hidden_row": arguments = {"work_order_id": "WO-0002"}
        if name == "terminal_invalid_tenant_argument": operation, arguments = "list_work_orders", {"tenant_id": "beta"}
        check("chaos:" + name + ":operation", value["kind"] == "actual_official_mcp_stdio" and value["operation"] == operation and value["arguments"] == arguments)
        check("chaos:" + name + ":attempts-terminal-policy", value["outcome"] == outcome and [x["outcome"] for x in value["attempts"]] == attempts and [x["endpoint"] for x in value["attempts"]] == ["primary", "secondary"][:len(attempts)] and value["budget"]["maximum_attempts"] == 2)
        check("chaos:" + name + ":authorized-data", value["response"] == (READ if outcome == "success" else None))
        total, per_attempt = {"primary_stalled_after_read": (4, 1), "total_work_deadline": (1, 3), "caller_cancellation_after_durable_outcome": (5, 3)}.get(name, (4, 2))
        check("chaos:" + name + ":fixed-work-budget", value["budget"] == {"total_work_seconds": total, "per_attempt_work_seconds": per_attempt, "maximum_attempts": 2} and all(_finite(x) for x in value["budget"].values()))
        for attempt in value["attempts"]:
            check("chaos:" + name + ":owned-reaped", attempt["owned_child_observed"] is True and attempt["process_reaped"] is True and type(attempt["pid"]) is int and attempt["pid"] > 0 and all(_finite(attempt[k]) for k in ["work_budget_seconds", "work_seconds", "cleanup_seconds", "wall_seconds"]))
            pids.append(attempt["pid"])
        check("chaos:" + name + ":audit-endpoints", set(value["audit_events_added"]) == {"primary", "secondary"})
        for endpoint, phases in [("primary", primary), ("secondary", secondary)]:
            events = value["audit_events_added"][endpoint]
            check("chaos:" + name + ":" + endpoint + ":durable-phases", [x["outcome"] if x["outcome"] != "error" else x["error_code"] for x in events] == phases)
            for event in events:
                check("chaos:" + name + ":audit-input", event["operation"] == operation and event["tenant_id"] == "alpha" and event["subject"] == "local:alpha-reader" and event["input_digest_kind"] == "canonical_json" and event["input_sha256"] == digest({"tool": operation, "arguments": arguments}))
                if event["phase"] == "outcome" and event["outcome"] == "success":
                    check("chaos:" + name + ":successful-read-audit", event["output_sha256"] == digest(READ) and event["row_count"] == 1 and type(event["row_count"]) is int)
                elif event["phase"] == "outcome":
                    messages = {"forbidden": "The trusted profile does not allow this operation.", "not_found": "No permitted resource exists for that identifier.", "invalid_request": "Only documented operation fields are accepted.", "resource_busy": "The local resource is busy; retry explicitly."}
                    code = event["error_code"]
                    check("chaos:" + name + ":refusal-is-not-a-read-result", type(event["row_count"]) is int and event["row_count"] == 0 and event["outcome"] == "error" and event["output_sha256"] == digest(_envelope((code, messages[code]))))
            concatenated[endpoint].extend(events)
    check("chaos:complete-process-denominator", type(report["actual_processes"]) is int and report["actual_processes"] == len(pids) == len(set(pids)) == 20 and report["all_observed_children_reaped"] is True)
    check("chaos:immutable-source-copies", report["source_unchanged"] is True and report["source_databases_before"] == report["source_databases_after"] and set(report["source_databases_before"]) == {"primary", "secondary"} and len(set(report["source_databases_before"].values())) == 1)
    check("chaos:complete-durable-audits", report["audits"] == concatenated and [len(concatenated[x]) for x in ["primary", "secondary"]] == [23, 8] and report["audit_chains_verified"] is True)
    fields = {"sequence", "at", "call_id", "phase", "subject", "tenant_id", "operation", "input_sha256", "input_digest_kind", "output_sha256", "outcome", "error_code", "row_count"}
    for endpoint, events in concatenated.items():
        previous, intents, outcomes = "0" * 64, {}, set()
        for i, event in enumerate(events, 1):
            body = {k: v for k, v in event.items() if k not in {"previous_sha256", "event_sha256"}}
            valid = set(body) == fields and type(body["sequence"]) is int and body["sequence"] == i and ID.fullmatch(body["call_id"]) and event["previous_sha256"] == previous and event["event_sha256"] == hashlib.sha256((previous + "\n" + canonical(body)).encode()).hexdigest()
            if body["phase"] == "intent":
                valid = valid and body["call_id"] not in intents and body["outcome"] == "intent" and all(body[k] is None for k in ["output_sha256", "row_count", "error_code"])
                intents[body["call_id"]] = body
            else:
                intent = intents.get(body["call_id"])
                valid = valid and body["phase"] == "outcome" and intent is not None and body["call_id"] not in outcomes and all(body[k] == intent[k] for k in ["subject", "tenant_id", "operation", "input_sha256", "input_digest_kind"]) and body["outcome"] in {"success", "error"} and (body["error_code"] is None if body["outcome"] == "success" else body["error_code"] in {"forbidden", "not_found", "invalid_request", "resource_busy"})
                outcomes.add(body["call_id"])
            check("chaos:" + endpoint + ":audit-chain-" + str(i), valid)
            _utc(body["at"])
            previous = event["event_sha256"]
        check("chaos:" + endpoint + ":unfinished-intents-preserved", len(set(intents) - outcomes) == (1 if endpoint == "primary" else 0))


def _approval(report, reader, check):
    check("approval:report-shape", set(report) == {"schema_version", "recorded_at", "experiment", "runtime", "observations", "histories", "durability", "local_notifications", "replay", "human_review_performed", "limitations"})
    check("approval:fixture-study", type(report["schema_version"]) is int and report["schema_version"] == 1 and report["human_review_performed"] is False)
    by = _cases(report, APPROVAL_STATUS, check, "approval")
    for name, status in APPROVAL_STATUS.items():
        value = by[name]
        check("approval:" + name + ":HTTP", type(value["http_status"]) is int and value["http_status"] == status and set(value) == {"case", "http_status", "response"})
        if status != 200:
            check("approval:" + name + ":denial", value["response"] == _envelope(A_ERRORS[status]))
    requests = {}
    for key, name in CREATED.items():
        tenant, request_id = key
        request = by[name]["response"]["request"]
        check("approval:" + name + ":request", set(request) == {"request_id", "tenant_id", "requested_by", "payload", "expires_at", "snapshot_sha256"} and request["request_id"] == request_id and request["tenant_id"] == tenant and request["requested_by"] == ("dual-alpha" if request_id == "self-review" else "requester-" + tenant) and request["payload"] == NOTE)
        expected = {"schema": 1, "action": "publish_local_note", **{k: v for k, v in request.items() if k != "snapshot_sha256"}}
        check("approval:" + name + ":immutable-digest", request["snapshot_sha256"] == digest(expected))
        _utc(request["expires_at"])
        requests[key] = request
    response = lambda name: by[name]["response"]
    scope = {
        "request-created": ("alpha", "publish-study-note"), "exact-create-replay": ("alpha", "publish-study-note"),
        "pending-after-server-worker-edge-restart": ("alpha", "publish-study-note"),
        "approve-before-effect-ack": ("alpha", "publish-study-note"),
        "published-once-after-worker-kill-and-server-restart": ("alpha", "publish-study-note"),
        "exact-decision-replay": ("alpha", "publish-study-note"), "published-after-edge-restart": ("alpha", "publish-study-note"),
        "same-request-id-other-tenant": ("beta", "publish-study-note"), "beta-approved": ("beta", "publish-study-note"),
        "dual-role-request-created": ("alpha", "self-review"), "independent-reviewer-rejects-self-request": ("alpha", "self-review"),
        "rejection-request-created": ("alpha", "reject-study-note"), "rejected": ("alpha", "reject-study-note"),
        "expiry-request-created": ("alpha", "expire-study-note"), "expired-while-worker-absent": ("alpha", "expire-study-note"),
    }
    for name, key in scope.items():
        value = response(name)
        check("approval:" + name + ":exact-scoped-snapshot", set(value) == {"request", "status", "publication_status", "receipt", "effect"} and value["request"] == requests[key])
        receipt = value["receipt"]
        if receipt is not None:
            reject = name in {"rejected", "independent-reviewer-rejects-self-request"}
            command_id = ("dual-reject" if key[1] == "self-review" else "reject-1") if reject else "review-1"
            check("approval:" + name + ":exact-independent-command", set(receipt) == {"accepted_at", "command"} and receipt["command"] == {"command_id": command_id, "tenant_id": key[0], "snapshot_sha256": requests[key]["snapshot_sha256"], "decision": "reject" if reject else "approve", "actor_id": "approver-" + key[0], "actor_roles": ["approver"]} and _utc(receipt["accepted_at"]) < _utc(requests[key]["expires_at"]))
        if name in CREATED.values():
            check("approval:" + name + ":created-without-decision-effect", value["status"] == "pending" and value["publication_status"] == "not_started" and receipt is None and value["effect"] is None)
    primary = response("request-created")
    check("approval:immutable-create-retry-restart", primary == response("exact-create-replay") == response("pending-after-server-worker-edge-restart") and primary["status"] == "pending" and primary["publication_status"] == "not_started" and primary["effect"] is None and primary["receipt"] is None)
    note_effects = {}
    notes = report["local_notifications"]
    check("approval:exactly-two-local-effects", len(notes) == 2 and {(x["tenant_id"], x["request_id"]) for x in notes} == {("alpha", "publish-study-note"), ("beta", "publish-study-note")})
    for note in notes:
        key = (note["tenant_id"], note["request_id"])
        request = requests[key]
        effect = {k: note[k] for k in ["effect_key", "tenant_id", "request_id", "snapshot_sha256", "note_sha256"]}
        receipt = _json(note["receipt_json"])
        check("approval:" + key[0] + ":local-effect", effect["effect_key"] == "approval/" + key[0] + "/publish-study-note" and effect["snapshot_sha256"] == request["snapshot_sha256"] and effect["note_sha256"] == digest(NOTE) and _json(note["note_json"]) == NOTE and set(note) == set(effect) | {"note_json", "receipt_json", "published_at"})
        check("approval:" + key[0] + ":independent-receipt", receipt["command"] == {"command_id": "review-1", "tenant_id": key[0], "snapshot_sha256": request["snapshot_sha256"], "decision": "approve", "actor_id": "approver-" + key[0], "actor_roles": ["approver"]} and _utc(receipt["accepted_at"]) < _utc(request["expires_at"]))
        check("approval:" + key[0] + ":ledger-retains-accepted-receipt", receipt == response("approve-before-effect-ack" if key[0] == "alpha" else "beta-approved")["receipt"])
        _utc(note["published_at"])
        note_effects[key] = effect
    approved = response("approve-before-effect-ack")
    check("approval:decision-before-local-effect-ack", approved["request"] == requests[("alpha", "publish-study-note")] and approved["status"] == "approved" and approved["publication_status"] == "pending" and approved["effect"] is None)
    check("approval:beta-decision-before-ack", response("beta-approved")["status"] == "approved" and response("beta-approved")["publication_status"] == "pending" and response("beta-approved")["effect"] is None)
    final = response("published-once-after-worker-kill-and-server-restart")
    check("approval:primary-exact-retry-and-effect", final == response("exact-decision-replay") == response("published-after-edge-restart") and final["request"] == primary["request"] and final["receipt"] == approved["receipt"] and final["effect"] == note_effects[("alpha", "publish-study-note")] and final["status"] == "approved" and final["publication_status"] == "published")
    for name, key, status in [("rejected", ("alpha", "reject-study-note"), "rejected"), ("independent-reviewer-rejects-self-request", ("alpha", "self-review"), "rejected"), ("expired-while-worker-absent", ("alpha", "expire-study-note"), "expired")]:
        result = response(name)
        check("approval:" + name + ":terminal-without-effect", result["request"] == requests[key] and result["status"] == status and result["publication_status"] == "not_applicable" and result["effect"] is None)
        if status == "expired": check("approval:expiry-has-no-decision", result["receipt"] is None)
    check("approval:durability-counts", report["durability"] == {"worker_killed_after_commit": True, "server_restarted": True, "edge_restarted": True, "notification_rows_for_primary": 1, "total_notifications": 2, "pending_request_preserved": True, "expiry_while_worker_absent": True, "before_crash_activity_completed": False} and type(report["durability"]["notification_rows_for_primary"]) is int and type(report["durability"]["total_notifications"]) is int)
    check("approval:recorded-SDK-replay-not-verifier-replay", report["replay"] == {"histories": 5, "all_passed": True, "no_new_local_effects": True} and type(report["replay"]["histories"]) is int)
    histories = report["histories"]
    valid = len(histories) == 5 and {h["filename"] for h in histories} == {"histories/" + n for n in HISTORIES} and len({h["filename"] for h in histories}) == 5
    check("approval:all-five-histories-once", valid)
    if not valid: raise ValueError("history inventory")
    for filename, (tenant, request_id, outcome) in HISTORIES.items():
        history = reader.read(("approval", "histories", filename), 512 * 1024)
        meta = next(x for x in histories if x["filename"] == "histories/" + filename)
        events = history["events"]
        check("approval:" + filename + ":recorded-hash-and-inventory", reader.hashes["approval/histories/" + filename] == meta["sha256"] and type(meta["event_count"]) is int and len(events) == meta["event_count"] and [x["eventType"] for x in events] == meta["event_types"] and meta["tenant_id"] == tenant and meta["request_id"] == request_id)
        event_attributes = {"workflowExecutionStartedEventAttributes": "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED", "workflowExecutionCompletedEventAttributes": "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED", "workflowExecutionUpdateAcceptedEventAttributes": "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED", "workflowExecutionUpdateCompletedEventAttributes": "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_COMPLETED", "activityTaskScheduledEventAttributes": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED", "activityTaskStartedEventAttributes": "EVENT_TYPE_ACTIVITY_TASK_STARTED", "activityTaskCompletedEventAttributes": "EVENT_TYPE_ACTIVITY_TASK_COMPLETED"}
        event_attributes.update({"workflowTaskScheduledEventAttributes": "EVENT_TYPE_WORKFLOW_TASK_SCHEDULED", "workflowTaskStartedEventAttributes": "EVENT_TYPE_WORKFLOW_TASK_STARTED", "workflowTaskCompletedEventAttributes": "EVENT_TYPE_WORKFLOW_TASK_COMPLETED", "workflowTaskTimedOutEventAttributes": "EVENT_TYPE_WORKFLOW_TASK_TIMED_OUT", "timerStartedEventAttributes": "EVENT_TYPE_TIMER_STARTED", "timerFiredEventAttributes": "EVENT_TYPE_TIMER_FIRED", "timerCanceledEventAttributes": "EVENT_TYPE_TIMER_CANCELED"})
        check("approval:" + filename + ":event-identities-and-terminal", [e["eventId"] for e in events] == [str(i) for i in range(1, len(events) + 1)] and events[0]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED" and events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED" and all([k for k in e if k.endswith("EventAttributes")] == [attribute] and e["eventType"] == event_type for e in events for attribute, event_type in event_attributes.items() if attribute in e) and all(any(attribute in e for attribute in event_attributes) for e in events))
        request = requests[(tenant, request_id)]
        start = events[0]["workflowExecutionStartedEventAttributes"]
        result = _payload(events[-1]["workflowExecutionCompletedEventAttributes"]["result"])
        check("approval:" + filename + ":immutable-start", start["workflowType"]["name"] == "ApprovalWorkflow" and _payload(start["input"]) == request)
        check("approval:" + filename + ":terminal-result", result["request"] == request and result["status"] == outcome and set(result) == {"request", "status", "publication_status", "receipt", "effect"})
        terminal_case = {("alpha", "publish-study-note"): "published-after-edge-restart", ("beta", "publish-study-note"): "beta-approved", ("alpha", "reject-study-note"): "rejected", ("alpha", "self-review"): "independent-reviewer-rejects-self-request", ("alpha", "expire-study-note"): "expired-while-worker-absent"}[(tenant, request_id)]
        check("approval:" + filename + ":same-accepted-receipt", result["receipt"] == response(terminal_case)["receipt"])
        activities = [x for x in events if x["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"]
        updates = [x for x in events if x["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED"]
        completed_updates = [x for x in events if x["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_COMPLETED"]
        if outcome != "expired":
            update = updates[0]["workflowExecutionUpdateAcceptedEventAttributes"]
            completed = completed_updates[0]["workflowExecutionUpdateCompletedEventAttributes"]
            command = result["receipt"]["command"]
            check("approval:" + filename + ":whole-command-Update-identity", len(updates) == len(completed_updates) == 1 and update["acceptedRequest"]["input"]["name"] == "decide" and _payload(update["acceptedRequest"]["input"]["args"]) == command and update["protocolInstanceId"] == update["acceptedRequest"]["meta"]["updateId"] == completed["meta"]["updateId"] == digest(command) and _payload(completed["outcome"]["success"]) == result["receipt"])
        if outcome == "approved":
            check("approval:" + filename + ":one-effect-not-repeated", len(activities) == 1 and len(updates) == 1 and result["publication_status"] == "published" and result["effect"] == note_effects[(tenant, request_id)])
            check("approval:" + filename + ":activity-input", _payload(activities[0]["activityTaskScheduledEventAttributes"]["input"]) == {"request": request, "receipt": result["receipt"]})
            completed_activities = [x for x in events if x["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED"]
            check("approval:" + filename + ":activity-ack-exact-effect", len(completed_activities) == 1 and _payload(completed_activities[0]["activityTaskCompletedEventAttributes"]["result"]) == note_effects[(tenant, request_id)])
            if tenant == "alpha":
                starts = [x["activityTaskStartedEventAttributes"] for x in events if "activityTaskStartedEventAttributes" in x]
                check("approval:actual-retry-attempt-two", len(starts) == 1 and type(starts[0]["attempt"]) is int and starts[0]["attempt"] == 2 and starts[0]["lastFailure"]["timeoutFailureInfo"]["timeoutType"] == "TIMEOUT_TYPE_START_TO_CLOSE")
        else:
            check("approval:" + filename + ":no-activity-or-effect", not any(e["eventType"].startswith("EVENT_TYPE_ACTIVITY_") for e in events) and result["effect"] is None and result["publication_status"] == "not_applicable" and len(updates) == len(completed_updates) == (0 if outcome == "expired" else 1))


def _privacy(report, check):
    check("privacy:report-shape", set(report) == {"schema", "recorded_at", "synthetic_only", "processor", "runtime", "source_sha256", "identity_dist_sha256", "synthetic_input", "observations", "processor_requests", "limitations", "actual_sqlite_busy_seconds", "log_scan", "processes_reaped", "audit"})
    check("privacy:fixed-synthetic-contract", report["schema"] == "privacy-process-v1" and report["synthetic_only"] is True and report["synthetic_input"] == TEXT and report["processes_reaped"] is True)
    by = _cases(report, PRIVACY_STATUS, check, "privacy")
    for name, status in PRIVACY_STATUS.items():
        value = by[name]
        check("privacy:" + name + ":HTTP", set(value) == {"case", "http_status", "response"} and type(value["http_status"]) is int and value["http_status"] == status)
        if name in P_ERRORS: check("privacy:" + name + ":withheld-denial", value["response"] == _envelope(P_ERRORS[name]))
    tokens, contexts = set(), set()
    redactions = ["redact", "same_values_new_context", "actual_http_processor", "short_context_created"]
    for name in redactions:
        value = by[name]["response"]
        valid = type(value["context_id"]) is str and ID.fullmatch(value["context_id"]) and value["context_id"] not in contexts and type(value["expires_at"]) is int
        contexts.add(value["context_id"])
        detections = value["detections"]
        valid = valid and len(detections) == 3 and [(d["kind"], d["start"], d["end"]) for d in detections] == [("EMAIL", 30, 50), ("PHONE", 52, 67), ("PAN", 74, 93)] and all(type(d["start"]) is type(d["end"]) is int and set(d) == {"kind", "start", "end", "token"} for d in detections)
        text, cursor = "", 0
        for detection in detections:
            token = detection["token"]
            valid = valid and TOKEN.fullmatch(token) and token not in tokens
            tokens.add(token)
            text += TEXT[cursor:detection["start"]] + token
            cursor = detection["end"]
        text += TEXT[cursor:]
        check("privacy:" + name + ":context-token-span-binding", valid and value["redacted_text"] == text)
        if name == "actual_http_processor":
            check("privacy:processor-restores-same-context", set(value) == {"context_id", "redacted_text", "expires_at", "detections", "processed_text", "restored_text"} and value["processed_text"] == "Processed: " + text and value["restored_text"] == "Processed: " + TEXT)
        else:
            check("privacy:" + name + ":redaction-shape", set(value) == {"context_id", "redacted_text", "expires_at", "detections"})
    for name in ["restore", "after_lock_released", "restore_after_resource_restart"]:
        check("privacy:" + name + ":authorized-restoration", by[name]["response"] == {"restored_text": TEXT})
    processor = report["processor_requests"]
    check("privacy:three-processor-requests-no-credentials", len(processor) == 3 and all(set(x) == {"text", "authorization_present"} and x["authorization_present"] is False and len(TOKEN.findall(x["text"])) == 3 and all(v not in x["text"] for v in ["nina@privacy.example", "+1 202-555-0114", "4242 4242 4242 4242"]) for x in processor) and processor[0]["text"] == by["actual_http_processor"]["response"]["redacted_text"])
    processor_tokens = [t for request in processor for t in TOKEN.findall(request["text"])]
    check("privacy:process-calls-do-not-reuse-context-tokens", len(set(processor_tokens)) == 9)
    check("privacy:short-expiry-remains-short", by["short_context_created"]["response"]["expires_at"] < by["redact"]["response"]["expires_at"])
    expected = [("redact", "success", 3), ("restore", "success", 3), ("restore", "denied", 0), ("restore", "denied", 0), ("redact", "denied", 0), ("redact", "success", 3), ("restore", "denied", 0), ("redact", "success", 3), ("restore", "success", 3), ("redact", "success", 3), ("restore", "denied", 0), ("redact", "success", 3), ("restore", "success", 3), ("restore", "success", 3)]
    check("privacy:metadata-only-audit-denominator", len(report["audit"]) == 14 and all(set(v) == {"sequence", "action", "outcome", "count"} and type(v["sequence"]) is int and v["sequence"] == i and type(v["count"]) is int and (v["action"], v["outcome"], v["count"]) == e for i, (v, e) in enumerate(zip(report["audit"], expected), 1)))
    check("privacy:bounded-known-log-scan", report["log_scan"] == {"resource_processes": 3, "bytes": 156, "matched_supported_values_or_bearer_tokens": 0, "raw_private_issuer_ipc_included": False} and all(type(report["log_scan"][k]) is int for k in ["resource_processes", "bytes", "matched_supported_values_or_bearer_tokens"]))


def _provenance(value, inventory, runtime, reports, reader, check):
    check("provenance:case-and-source", type(value["schema_version"]) is int and value["schema_version"] == 1 and value["case_id"] == "reliable-legacy-automation" and value["runtime_source_revision"] == SOURCE_REVISION and value["runtime_source_clean_at_start"] is True and value["runtime_package"] == "enterprise-ai-lab==0.14.1" and value["human_review_performed"] is False and value["owner_personal_study_recorded"] is False and value["complete_three_case_milestone"] is False)
    check("provenance:separate-not-connected-write-case", value["three_separate_experiments"] is True and value["connected_privacy_to_MCP_write_pipeline"] is False)
    check("provenance:fixed-inventory-paths", value["wheel_inventory"] == "wheel-source-inventory.json" and value["runtime_inventory"] == "runtime-inventory.json")
    files = inventory["package_files"]
    file_map = {x["file"]: x["sha256"] for x in files}
    check("provenance:113-fixed-source-files", type(inventory["matched_package_entries"]) is int and inventory["matched_package_entries"] == value["wheel_package_entries_matched_source"] == len(files) == len(file_map) == 113 and inventory["source_revision"] == SOURCE_REVISION and digest(file_map) == SOURCE_MAP_SHA256 and inventory["all_package_bytes_match_committed_source"] is True)
    check("provenance:wheel-byte-binding-not-execution-proof", HEX.fullmatch(value["wheel_sha256"]) and inventory["wheel_sha256"] == value["wheel_sha256"] and inventory["wheel"] == "enterprise_ai_lab-0.14.1-py3-none-any.whl" and value["installed_module_outside_checkout"] is True and value["isolated_main_python"] is True)
    fingerprints = {k.removeprefix("enterprise_ai_lab/"): v for k, v in file_map.items()}
    expected_sources = {
        "chaos": {"chaos/__init__.py", "chaos/__main__.py", "chaos/client.py", "chaos/contract.py", "chaos/diagnostics.py", "chaos/experiment.py", "chaos/transport.py", "mcp_gateway/__init__.py", "mcp_gateway/__main__.py", "mcp_gateway/audit.py", "mcp_gateway/data/work-orders-v1.json", "mcp_gateway/integration.py", "mcp_gateway/server.py", "mcp_gateway/service.py", "mcp_gateway/store.py"},
        "privacy": {"privacy_proxy/__init__.py", "privacy_proxy/__main__.py", "privacy_proxy/api.py", "privacy_proxy/contracts.py", "privacy_proxy/detector.py", "privacy_proxy/evaluation.py", "privacy_proxy/integration.py", "privacy_proxy/vault.py", "rag/auth.py"},
    }
    check("provenance:source-fingerprints", all(set(reports[kind]["source_sha256"]) == expected_sources[kind] and all(fingerprints[k] == v for k, v in reports[kind]["source_sha256"].items()) for kind in expected_sources))
    check("provenance:exact-unchanged-locks", value["exact_runtime_locks"] == {k: LOCKS[k] for k in ["requirements-mcp.lock", "requirements-approval.lock"]} and value["all_existing_requirement_locks_preserved"] == LOCKS and reports["chaos"]["requirements_mcp_lock_sha256"] is None)
    envs = runtime["environments"]
    check("provenance:two-recorded-installed-environments", runtime["schema_version"] == 1 and type(runtime["schema_version"]) is int and set(envs) == {"mcp", "approval"} and all(e["installed_module_outside_checkout"] is True and e["isolated_main_python"] is True and e["packages"]["enterprise-ai-lab"] == "0.14.1" and HEX.fullmatch(e["private_runtime_readback_receipt_sha256"]) for e in envs.values()))
    check("provenance:recorded-runtime-versions", envs["mcp"]["python"].startswith(reports["chaos"]["runtime"]["python"] + " ") and envs["approval"]["python"].startswith(reports["approval"]["runtime"]["python"] + " ") and reports["approval"]["runtime"]["python"] == reports["privacy"]["runtime"]["python"] and envs["mcp"]["packages"]["mcp"] == reports["chaos"]["runtime"]["mcp"] == "2.3.0" and envs["mcp"]["packages"]["mcp-types"] == reports["chaos"]["runtime"]["mcp_types"] == "2.3.0" and envs["approval"]["packages"]["temporalio"] == reports["approval"]["runtime"]["temporalio"] == "1.34.0" and envs["approval"]["packages"]["httpx"] == reports["privacy"]["runtime"]["httpx"] == "0.28.1" and envs["approval"]["packages"]["PyJWT"] == reports["privacy"]["runtime"]["PyJWT"] == "2.15.1")
    check("provenance:identity-runtime-binding", value["identity_source_revision"] == IDENTITY_REVISION and value["identity_bridge_sha256"] == reports["approval"]["runtime"]["identity_bridge_sha256"] == reports["privacy"]["identity_dist_sha256"]["bridge.js"] and value["identity_issuer_sha256"] == reports["privacy"]["identity_dist_sha256"]["issuer.js"] and value["node_version"] == reports["approval"]["runtime"]["node"] == reports["privacy"]["runtime"]["node"] == "v24.19.0" and HEX.fullmatch(value["identity_package_lock_sha256"]) and HEX.fullmatch(value["node_binary_sha256"]))
    cli = value["temporal_cli"]
    check("provenance:pinned-Temporal-binary", cli == reports["approval"]["runtime"]["cli"] and cli["cli_version"] == "1.9.1" and cli["server_version"] == "1.32.0" and cli["sha256"] == "33c298d41f6eefebf01a28a3511f444480e939e21d3518cb0c12a4abad467c4b")
    experiments = value["experiments"]
    check("provenance:three-separate-experiment-denominator", set(experiments) == {"chaos", "approval", "privacy"})
    previous_end, receipts = None, set()
    for name in ["chaos", "approval", "privacy"]:
        e = experiments[name]
        start, end = _utc(e["execution_started_at"]), _utc(e["execution_finished_at"])
        check("provenance:" + name + ":recorded-artifact", e["report"] == name + "/demo.json" and e["sha256"] == reader.hashes[name + "/demo.json"] and type(e["bytes"]) is int and e["bytes"] == reader.sizes[name + "/demo.json"] and type(e["execution_exit_code"]) is int and e["execution_exit_code"] == 0 and type(e["experiment_attempts"]) is int and e["experiment_attempts"] == 1 and e["raw_private_and_candidate_bytes_identical"] is True)
        check("provenance:" + name + ":recorded-time-receipt", start < end and (previous_end is None or previous_end <= start) and start <= _utc(reports[name]["recorded_at"]) <= end and _finite(e["execution_elapsed_s"]) and abs(e["execution_elapsed_s"] - (end - start).total_seconds()) < .1 and HEX.fullmatch(e["private_exact_command_receipt_sha256"]) and e["private_exact_command_receipt_sha256"] not in receipts and e["separate_new_work_directory"] is True and e["installed_wheel_and_isolated_main_python"] is True and e["runtime_asset_downloads_disabled"] is True)
        previous_end = end
        receipts.add(e["private_exact_command_receipt_sha256"])
    histories = [{**h, "filename": "approval/" + h["filename"]} for h in reports["approval"]["histories"]]
    check("provenance:recorded-SDK-histories-not-new-replay", value["histories_replayed_by_actual_SDK"] == histories and value["raw_private_and_candidate_artifacts_byte_identical"] is True)
    check("provenance:known-safe-scan-denominator", value["public_safe_scan"]["files"] == 8 and type(value["public_safe_scan"]["files"]) is int and value["public_safe_scan"]["decoded_history_payloads"] == reader.decoded_payloads == 22 and value["public_safe_scan"]["matches"] == {"absolute_home_or_private_path": 0, "bearer_credential": 0, "JWT": 0} and value["public_safe_scan"]["raw_and_decoded_history_payloads_checked"] is True)


def verify(directory):
    checks, reader = [], None
    def check(name, condition): checks.append({"check": name, "passed": bool(condition)})
    try:
        reader = Reader(directory)
        reports = {name: reader.read((name, "demo.json")) for name in ["chaos", "approval", "privacy"]}
        provenance = reader.read(("provenance.json",), 512 * 1024)
        inventory = reader.read(("wheel-source-inventory.json",), 512 * 1024)
        runtime = reader.read(("runtime-inventory.json",), 512 * 1024)
        _chaos(reports["chaos"], check)
        _approval(reports["approval"], reader, check)
        _privacy(reports["privacy"], check)
        _provenance(provenance, inventory, runtime, reports, reader, check)
        for value in [*reports.values(), provenance, inventory]:
            check("public-artifacts:no-known-private-path-or-credential", not PRIVATE.search(canonical(value)))
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, UnicodeError):
        check("complete-readable-recorded-contract", False)
    finally:
        if reader is not None: reader.close()
    return {"schema_version": 1, "scope": "recorded_three_component_reliability_contract_only", "passed": bool(checks) and all(x["passed"] for x in checks), "checks": checks,
        "artifact_sha256": {} if reader is None else reader.hashes,
        "denominators": {"chaos_observations": 15, "approval_observations": 29, "privacy_observations": 25, "approval_histories": 5},
        "runtime_reexecuted": False, "SDK_replay_performed": False, "connected_write_pipeline_verified": False,
        "limitations": ["This verifies recorded data and provenance consistency, not fresh execution or execution provenance authenticity.", "Three independent experiments are not an approved MCP write pipeline or a cross-system transaction.", "Local note approval has no RAG corpus revision; no exactly-once external-effect, owner review or study is established.", "Privacy cases record denials and context/token results, not exported JWT claims or comprehensive leak detection."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args(argv)
    result = verify(args.directory)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
