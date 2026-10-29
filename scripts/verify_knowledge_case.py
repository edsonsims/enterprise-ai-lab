"""Check the fixed recorded publication case without starting its services.

This checks recorded observations and exported Temporal history, not the truth of
an execution claim. A passing artifact is neither a new run nor human review.
Only standard-library modules are imported. Directory paths are trusted local
operator inputs; report/history files must be bounded, regular, unaliased files.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


EXPECTED_STATUS = {
    "staged-not-searchable": 200, "reject-created": 200, "rejected": 200,
    "rejected-not-searchable": 200, "expiry-created": 200,
    "expired-while-worker-absent": 200, "expired-approval-denied": 409,
    "expired-not-searchable": 200, "publication-created": 200,
    "exact-create-replay": 200, "changed-revision-conflict": 409,
    "foreign-candidate-denied": 404, "candidate-missing": 404,
    "forged-approval-denied": 401, "rag-audience-denied": 401,
    "cross-tenant-approval-denied": 403, "wrong-role-denied": 403,
    "missing-scope-denied": 403, "tampered-snapshot-denied": 409,
    "spoofed-actor-denied": 422, "pending-not-searchable": 200,
    "pending-after-all-services-restart": 200,
    "pending-after-restart-not-searchable": 200, "approval-accepted": 200,
    "published-once-after-kill-and-server-restart": 200,
    "approved-native-reader": 200, "approved-ocr-analyst": 200,
    "ocr-reader-denied": 200, "beta-still-unpublished": 200,
    "cross-tenant-query-denied": 403, "changed-decision-conflict": 409,
    "beta-created": 200, "beta-approved": 200, "beta-ocr-reader": 200,
    "acl-revision-created": 200, "pending-acl-keeps-approved-version": 200,
    "acl-revision-approved": 200, "approved-acl-removes-reader": 200,
    "approved-acl-preserves-analyst": 200, "old-decision-replay": 200,
    "old-replay-cannot-restore-reader": 200, "stale-revision-created": 200,
    "stale-revision-approval": 200, "stale-revision-publication-failed": 200,
    "stale-approval-cannot-restore-reader": 200,
    "ocr-before-resource-restart": 200, "ocr-after-resource-restart": 200,
    "narrowed-acl-after-restart": 200,
}
VISIBLE = {
    "approved-native-reader": "native", "approved-ocr-analyst": "ocr",
    "beta-ocr-reader": "ocr", "pending-acl-keeps-approved-version": "native",
    "approved-acl-preserves-analyst": "native",
    "ocr-before-resource-restart": "ocr", "ocr-after-resource-restart": "ocr",
}
NATIVE_QUESTION = "How is access to individual enterprise resources granted?"
OCR_QUESTION = "What functions is the Core composed of?"
QUERIES = {
    "staged-not-searchable": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "rejected-not-searchable": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "expired-not-searchable": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "pending-not-searchable": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "pending-after-restart-not-searchable": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "approved-native-reader": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "approved-ocr-analyst": (OCR_QUESTION, "analyst-alpha", "alpha"),
    "ocr-reader-denied": (OCR_QUESTION, "reader-alpha", "alpha"),
    "beta-still-unpublished": (OCR_QUESTION, "reader-beta", "beta"),
    "cross-tenant-query-denied": (NATIVE_QUESTION, "analyst-alpha", "beta"),
    "beta-ocr-reader": (OCR_QUESTION, "reader-beta", "beta"),
    "pending-acl-keeps-approved-version": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "approved-acl-removes-reader": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "approved-acl-preserves-analyst": (NATIVE_QUESTION, "analyst-alpha", "alpha"),
    "old-replay-cannot-restore-reader": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "stale-approval-cannot-restore-reader": (NATIVE_QUESTION, "reader-alpha", "alpha"),
    "ocr-before-resource-restart": (OCR_QUESTION, "analyst-alpha", "alpha"),
    "ocr-after-resource-restart": (OCR_QUESTION, "analyst-alpha", "alpha"),
    "narrowed-acl-after-restart": (NATIVE_QUESTION, "reader-alpha", "alpha"),
}
HISTORIES = {
    "histories/alpha-reject-pages.json": ("alpha", "reject-pages", "rejected"),
    "histories/alpha-expire-pages.json": ("alpha", "expire-pages", "expired"),
    "histories/alpha-publish-public-pages.json": ("alpha", "publish-public-pages", "published"),
    "histories/beta-publish-public-pages.json": ("beta", "publish-public-pages", "published"),
    "histories/alpha-narrow-native-role.json": ("alpha", "narrow-native-role", "published"),
    "histories/alpha-stale-publication.json": ("alpha", "stale-publication", "failed"),
}
CREATED = {
    ("alpha", "reject-pages"): "reject-created",
    ("alpha", "expire-pages"): "expiry-created",
    ("alpha", "publish-public-pages"): "publication-created",
    ("beta", "publish-public-pages"): "beta-created",
    ("alpha", "narrow-native-role"): "acl-revision-created",
    ("alpha", "stale-publication"): "stale-revision-created",
}
SOURCES = {
    "native": {
        "source_id": "nist-zero-trust",
        "source_uri": "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-207.pdf",
        "source_sha256": "0290d6ece24874287316f4bf430fef770aa4ec08a2227c8f2c1e5b2ff975e03d",
        "source_page": 15,
        "artifact_sha256": "0290d6ece24874287316f4bf430fef770aa4ec08a2227c8f2c1e5b2ff975e03d",
        "artifact_page": 15,
    },
    "ocr": {
        "source_id": "nist-ai-rmf", "source_uri": "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf",
        "source_sha256": "7576edb531d9848825814ee88e28b1795d3a84b435b4b797d3670eafdc4a89f1",
        "source_page": 25,
        "artifact_sha256": "d3d3c6d13229f9d68e5cefadac414f5ff4247556e536ff83c021496f96b698b2",
        "artifact_page": 1,
    },
}
ERRORS = {
    401: {"error": {"code": "invalid_token", "message": "Token invalid for the publication approval resource."}},
    403: {"error": {"code": "forbidden", "message": "This principal cannot perform that tenant's publication operation."}},
    404: {"error": {"code": "not_found", "message": "No publication request or candidate exists in this tenant scope."}},
    409: {"error": {"code": "publication_conflict", "message": "Request or decision conflicts, is expired, or has a stale snapshot."}},
    422: {"error": {"code": "invalid_request", "message": "Only the bounded publication request fields are accepted."}},
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _read(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
            raise ValueError("refused artifact")
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(raw) != before.st_size or len(raw) > limit or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("changed artifact")
        return raw
    finally:
        os.close(fd)


def _payload(value):
    payloads = value["payloads"]
    if len(payloads) != 1 or base64.b64decode(payloads[0]["metadata"]["encoding"], validate=True) != b"json/plain":
        raise ValueError("unsupported history payload")
    return _json(base64.b64decode(payloads[0]["data"], validate=True))


def verify(report_path):
    """Return finite checks; never echo input bodies, exception text or paths."""
    checks = []

    def check(code, condition):
        checks.append({"check": code, "passed": bool(condition)})

    directory = history_dir = None
    digest = None
    try:
        report_path = Path(report_path)
        directory = os.open(report_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        raw = _read(directory, report_path.name, 1024 * 1024)
        digest = hashlib.sha256(raw).hexdigest()
        report = _json(raw)
        check("schema", type(report["schema_version"]) is int and report["schema_version"] == 1)
        check("fixture-review-only", report["human_review_performed"] is False)
        observations = report["observations"]
        names = [item["case"] for item in observations]
        check("all-48-case-ids-once", len(names) == 48 and len(set(names)) == 48 and set(names) == set(EXPECTED_STATUS))
        by_name = {item["case"]: item for item in observations}
        if set(names) != set(EXPECTED_STATUS) or len(names) != 48:
            raise ValueError("case inventory differs")
        check("HTTP-results", all(type(by_name[name]["http_status"]) is int and
              by_name[name]["http_status"] == status for name, status in EXPECTED_STATUS.items()))
        for name, status in EXPECTED_STATUS.items():
            if status != 200:
                error = ({"error": {"code": "forbidden", "message": "This principal cannot query the requested tenant."}}
                         if name == "cross-tenant-query-denied" else ERRORS[status])
                check(name + ":error-envelope", by_name[name]["response"] == error)
        candidates = report["candidates"]
        check("candidate-inventory", len(candidates) == 3 and [c["tenant_id"] for c in candidates] == ["alpha", "beta", "alpha"])
        candidate_by_hash = {c["candidate_sha256"]: c for c in candidates}
        check("distinct-candidates", len(candidate_by_hash) == 3)
        check("two-native-OCR-pages-per-candidate", all(c["document_count"] == 2 and
              len(c["documents"]) == 2 and set(c["document_ids"]) == {d["document_id"] for d in c["documents"]} and
              {d["provenance"]["method"] for d in c["documents"]} == {"native", "ocr"} for c in candidates))
        roles = [[d["allowed_roles"] for d in sorted(c["documents"], key=lambda d: d["provenance"]["method"])] for c in candidates]
        check("authored-role-grants", roles == [[["reader"], ["analyst"]], [["reader"], ["reader"]], [["analyst"], ["analyst"]]])
        check("fixed-public-source-provenance", all(
            all(d["provenance"][k] == v and type(d["provenance"][k]) is type(v)
                for k, v in SOURCES[d["provenance"]["method"]].items())
            for c in candidates for d in c["documents"]))
        check("fixed-query-case-inventory", {item["case"] for item in observations if "query" in item} == set(QUERIES))
        for name, (question, client, tenant) in QUERIES.items():
            item = by_name[name]
            check(name + ":query-metadata", (item.get("query"), item.get("client"), item.get("tenant")) == (question, client, tenant))
            if EXPECTED_STATUS[name] != 200:
                continue
            response = item["response"]
            visible = name in VISIBLE
            check(name + ":visibility", response["abstained"] is (not visible) and
                  response["generation_performed"] is visible and bool(response["citations"]) is visible and
                  bool(response["evidence"]) is visible and response["generator"] == "extractive-v1")
            check(name + ":identity", response["identity"] == {"tenant_id": tenant, "client_id": client})
            role = client.split("-", 1)[0]
            candidate_index = 1 if tenant == "beta" else (
                2 if name in {"approved-acl-preserves-analyst", "ocr-before-resource-restart", "ocr-after-resource-restart"} else 0)
            documents = {d["document_id"]: d for d in candidates[candidate_index]["documents"]}
            for citation in response["citations"]:
                provenance = citation["provenance"]
                document = documents[citation["document_id"]]
                check(name + ":citation", citation["tenant_id"] == tenant and role in citation["allowed_roles"] and
                      citation["allowed_roles"] == document["allowed_roles"] and
                      all(provenance[k] == v for k, v in document["provenance"].items()) and
                      provenance["method"] == VISIBLE[name] and type(citation["quote"]) is str and
                      bool(citation["quote"]) and citation["quote"] in citation["text"] and
                      citation["chunk_id"].rsplit(":", 1)[1] == hashlib.sha256(citation["text"].encode()).hexdigest()[:12] and
                      type(provenance["start"]) is int and type(provenance["end"]) is int and
                      provenance["end"] - provenance["start"] == len(citation["text"]) and
                      {"chunk_id": citation["chunk_id"], "quote": citation["quote"]} in response["evidence"])
        response = lambda name: by_name[name]["response"]
        primary = response("publication-created")
        check("immutable-create-replay-restart", primary == response("exact-create-replay") == response("pending-after-all-services-restart"))
        check("pending-has-no-effect", primary["status"] == "pending" and primary["publication_status"] == "not_started" and primary["effect"] is None)
        check("decision-before-publication", response("approval-accepted")["status"] == "approved" and
              response("approval-accepted")["effect"] is None and response("approval-accepted")["publication_status"] == "pending")
        for name, status in [("rejected", "rejected"), ("expired-while-worker-absent", "expired"), ("stale-revision-publication-failed", "approved")]:
            check(name + ":terminal", response(name)["status"] == status and response(name)["effect"] is None)
        check("stale-approval-fails-publication", response("stale-revision-publication-failed")["publication_status"] == "failed")
        effects = report["effects"]
        expected_effects = {("alpha", "publish-public-pages", 1), ("beta", "publish-public-pages", 1), ("alpha", "narrow-native-role", 2)}
        check("three-unique-publications", len(effects) == 3 and len({e["effect_key"] for e in effects}) == 3 and
              {(e["tenant_id"], e["request_id"], e["revision"]) for e in effects} == expected_effects)
        for ordinal, effect in enumerate(effects):
            request = response(CREATED[(effect["tenant_id"], effect["request_id"] )])["request"]
            candidate = candidate_by_hash[effect["candidate_sha256"]]
            check("effect-" + str(ordinal),
                  effect["effect_key"] == "publication/" + effect["tenant_id"] + "/" + effect["request_id"] and
                  effect["snapshot_sha256"] == request["snapshot_sha256"] and effect["candidate_sha256"] == request["candidate_sha256"] and
                  candidate["tenant_id"] == effect["tenant_id"] and type(effect["revision"]) is int and
                  request["expected_revision"] + 1 == effect["revision"] and effect["document_count"] == 2 and effect["chunk_count"] == 14)
        final_primary = response("published-once-after-kill-and-server-restart")
        replayed_primary = response("old-decision-replay")
        primary_effects = [effect for effect in effects if effect["tenant_id"] == "alpha" and effect["request_id"] == "publish-public-pages"]
        check("primary-effect-and-old-decision-replay", len(primary_effects) == 1 and
              final_primary["effect"] == primary_effects[0] == replayed_primary["effect"] and
              final_primary["request"] == primary["request"] == replayed_primary["request"] and
              final_primary["receipt"] == response("approval-accepted")["receipt"] == replayed_primary["receipt"] and
              final_primary["status"] == replayed_primary["status"] == "approved" and
              final_primary["publication_status"] == replayed_primary["publication_status"] == "published")
        for name in ["approval-accepted", "beta-approved", "acl-revision-approved", "stale-revision-approval"]:
            value = response(name)
            command = value["receipt"]["command"]
            request = value["request"]
            check(name + ":independent-actor", command["actor_id"] == "approver-" + request["tenant_id"] and
                  command["actor_id"] != request["requested_by"] and command["actor_roles"] == ["approver"] and
                  command["snapshot_sha256"] == request["snapshot_sha256"] and command["tenant_id"] == request["tenant_id"])
        check("zero-before-approval-28-after", report["index"] == {"before_any_approval": 0, "after_publications": 28} and
              all(type(v) is int for v in report["index"].values()))
        check("OCR-citations-preserved-after-restart", response("ocr-before-resource-restart")["citations"] == response("ocr-after-resource-restart")["citations"])
        durability = report["durability"]
        expected_durability = {"temporal_resource_edge_restarted_while_pending": True, "identity_issuer_kept_running": True,
            "worker_killed_after_commit": True, "before_crash_activity_completed": False, "temporal_server_restarted": True,
            "primary_effect_count": 1, "rejected_expired_stale_effect_count": 0, "old_approval_cannot_restore_acl": True,
            "citations_identical_after_resource_restart": True}
        check("recorded-durability-controls", durability == expected_durability and
              all(type(durability[k]) is type(v) for k, v in expected_durability.items()))
        check("recorded-replay-result", report["replay"] == {"histories": 6, "all_passed": True, "no_new_publications": True} and
              type(report["replay"]["histories"]) is int and report["replay"]["all_passed"] is True and report["replay"]["no_new_publications"] is True)
        histories = report["histories"]
        check("all-six-history-ids-once", len(histories) == 6 and len({h["filename"] for h in histories}) == 6 and
              {h["filename"] for h in histories} == set(HISTORIES))
        if {h["filename"] for h in histories} != set(HISTORIES) or len(histories) != 6:
            raise ValueError("history inventory differs")
        history_dir = os.open("histories", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        for item in histories:
            filename = item["filename"]
            tenant, request_id, outcome = HISTORIES[filename]
            raw_history = _read(history_dir, filename.split("/")[1], 1024 * 1024)
            check(filename + ":hash", hashlib.sha256(raw_history).hexdigest() == item["sha256"])
            events = _json(raw_history)["events"]
            check(filename + ":events", type(item["event_count"]) is int and len(events) == item["event_count"] and
                  [e["eventType"] for e in events] == item["event_types"] and item["tenant_id"] == tenant and item["request_id"] == request_id)
            start = events[0]["workflowExecutionStartedEventAttributes"]
            request = response(CREATED[(tenant, request_id)])["request"]
            check(filename + ":immutable-input", _payload(start["input"]) == request and start["workflowType"]["name"] == "DocumentPublicationWorkflow")
            if outcome == "failed":
                check(filename + ":failed-without-effect", events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED" and
                      all(not (e["tenant_id"] == tenant and e["request_id"] == request_id) for e in effects))
            else:
                result = _payload(events[-1]["workflowExecutionCompletedEventAttributes"]["result"])
                expected_status = "approved" if outcome == "published" else outcome
                matching_effects = [effect for effect in effects if effect["tenant_id"] == tenant and effect["request_id"] == request_id]
                check(filename + ":outcome", result["request"] == request and result["status"] == expected_status and
                      ((result["publication_status"] == "published" and len(matching_effects) == 1 and result["effect"] == matching_effects[0]) if outcome == "published" else
                       (result["effect"] is None and not any(e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED" for e in events))))
            if filename == "histories/alpha-publish-public-pages.json":
                starts = [e["activityTaskStartedEventAttributes"] for e in events if "activityTaskStartedEventAttributes" in e]
                check("primary-history-attempt2-after-StartToClose", len(starts) == 1 and starts[0]["attempt"] == 2 and
                      starts[0]["lastFailure"]["timeoutFailureInfo"]["timeoutType"] == "TIMEOUT_TYPE_START_TO_CLOSE")
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        check("complete-readable-contract", False)
    finally:
        if history_dir is not None:
            os.close(history_dir)
        if directory is not None:
            os.close(directory)
    return {"schema_version": 1, "scope": "recorded_publication_contract_only", "report_sha256": digest,
            "passed": bool(checks) and all(x["passed"] for x in checks), "checks": checks,
            "limitations": ["This verifier starts no services, performs no SDK replay and proves no new execution.",
                            "Recorded fixture decisions do not establish human review or production security.",
                            "Literal citation and metadata checks are not source transcription, entailment or answer-quality evaluation."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    result = verify(args.report)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
