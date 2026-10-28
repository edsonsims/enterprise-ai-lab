"""Audit a fixed historical evidence inventory without executing experiments."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_PATH = "docs/case-studies/historical-v1.json"
MANIFEST_SHA256 = "1b1a78873902eddb09d7bfa4fbd872fbe39d5e98327394441fb9014e24fbd835"
MAX_MANIFEST_BYTES = 16_384
MAX_REPORT_BYTES = 1_048_576
CASES = {
    "permission-aware-knowledge": "reports/publication-v1/demo.json",
    "reliable-legacy-automation": "reports/chaos-v1/demo.json",
    "operable-local-ai": "reports/edge-v1/cpu-qwen-v1.json",
}


class AuditError(Exception):
    """A finite error code; untrusted paths and exception text are not exported."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError("duplicate_key")
        result[key] = value
    return result


def _constant(_value):
    raise AuditError("invalid_manifest")


def _read(root_fd: int, relative: str, limit: int) -> bytes:
    """Read bounded, stable regular bytes through directory handles, not links."""
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AuditError("unsafe_path")
    parent = os.dup(root_fd)
    file_fd = None
    try:
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
            os.close(parent)
            parent = child
        file_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise AuditError("unsafe_artifact")
        if before.st_size > limit:
            raise AuditError("artifact_too_large")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(file_fd, min(32_768, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(file_fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            len(data) != before.st_size
            or len(data) > limit
            or any(getattr(before, field) != getattr(after, field) for field in fields)
        ):
            raise AuditError("artifact_changed")
        return bytes(data)
    except FileNotFoundError:
        raise AuditError("artifact_missing") from None
    except OSError:
        raise AuditError("artifact_unavailable") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent)


def _manifest(data: bytes) -> dict:
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise AuditError("invalid_manifest") from None
    if type(value) is not dict or set(value) != {
        "schema_version", "manifest_id", "scope", "cases"
    }:
        raise AuditError("invalid_manifest")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["manifest_id"] != "historical-v1"
        or value["scope"] != "historical_evidence_only"
        or type(value["cases"]) is not list
        or len(value["cases"]) != 3
    ):
        raise AuditError("invalid_manifest")
    identifiers = set()
    paths = set()
    keys = {
        "id", "title", "report", "sha256", "bytes", "recorded_at",
        "archive_source_revision", "evidence_state", "narrative",
        "historical_scope", "limitations",
    }
    for case in value["cases"]:
        if type(case) is not dict or set(case) != keys:
            raise AuditError("invalid_manifest")
        if any(type(case[key]) is not str for key in keys - {"bytes", "limitations"}):
            raise AuditError("invalid_manifest")
        if case["id"] in identifiers or case["report"] in paths:
            raise AuditError("duplicate_case_or_path")
        identifiers.add(case["id"])
        paths.add(case["report"])
        if (
            case["id"] not in CASES
            or case["report"] != CASES[case["id"]]
            or case["narrative"] != f"docs/case-studies/{case['id']}.md"
        ):
            raise AuditError("unsafe_path")
        if case["evidence_state"] != "recorded_historical":
            raise AuditError("unknown_evidence_state")
        if (
            type(case["bytes"]) is not int
            or not 0 < case["bytes"] <= MAX_REPORT_BYTES
            or re.fullmatch("[0-9a-f]{64}", case["sha256"]) is None
            or re.fullmatch("[0-9a-f]{40}", case["archive_source_revision"]) is None
            or type(case["limitations"]) is not list
            or not 1 <= len(case["limitations"]) <= 8
            or any(type(item) is not str or not 1 <= len(item) <= 400 for item in case["limitations"])
        ):
            raise AuditError("invalid_manifest")
    # The pin makes metadata trusted inventory, never arbitrary report/user text.
    if hashlib.sha256(data).hexdigest() != MANIFEST_SHA256:
        raise AuditError("manifest_mismatch")
    return value


def inspect(repository: Path | str) -> dict:
    """Hash the fixed three archived reports; do not parse or execute their content."""
    root_fd = None
    try:
        root_fd = os.open(
            repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        manifest = _manifest(_read(root_fd, MANIFEST_PATH, MAX_MANIFEST_BYTES))
        cases = []
        for case in manifest["cases"]:
            result = {
                "id": case["id"],
                "title": case["title"],
                "report": case["report"],
                "narrative": case["narrative"],
                "historical_recorded_at": case["recorded_at"],
                "archive_source_revision": case["archive_source_revision"],
                "historical_scope": case["historical_scope"],
                "limitations": list(case["limitations"]),
                "expected_sha256": case["sha256"],
                "expected_bytes": case["bytes"],
                "observed_sha256": None,
                "observed_bytes": None,
                "status": "failed",
                "error": None,
            }
            try:
                data = _read(root_fd, case["report"], MAX_REPORT_BYTES)
                result["observed_sha256"] = hashlib.sha256(data).hexdigest()
                result["observed_bytes"] = len(data)
                if result["observed_sha256"] != case["sha256"] or len(data) != case["bytes"]:
                    result["error"] = "report_mismatch"
                else:
                    result["status"] = "byte_verified_historical"
            except AuditError as exc:
                result["error"] = exc.code
            cases.append(result)
        verified = sum(case["status"] == "byte_verified_historical" for case in cases)
        return {
            "schema_version": 1,
            "audit_kind": "historical_bytes_only",
            "inspected_at_utc": datetime.now(timezone.utc).isoformat(),
            "ok": verified == 3,
            "manifest_id": manifest["manifest_id"],
            "manifest_sha256": MANIFEST_SHA256,
            "cases": cases,
            "counts": {"byte_verified_historical": verified, "failed": 3 - verified},
            "fresh_execution_performed": False,
            "semantic_report_verification_performed": False,
            "archive_revision_verification_performed": False,
            "owner_review_or_study_asserted": False,
            "roadmap_item_15_complete": False,
            "limits": [
                "Matching archived bytes does not reproduce or semantically verify an experiment.",
                "Archive revisions identify published source context, not a proven execution checkout.",
                "Fixed metadata comes from the pinned inventory; raw report contents are not exported.",
                "Fresh end-to-end reproductions and the full three-case-study milestone remain pending.",
            ],
        }
    except OSError:
        raise AuditError("repository_unavailable") from None
    finally:
        if root_fd is not None:
            os.close(root_fd)


def write_report(report: dict, path: Path | str) -> None:
    """Create an owned 0600 output; never replace an existing file or symlink."""
    destination = Path(path)
    parent = None
    descriptor = None
    try:
        parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(
            destination.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        data = (json.dumps(report, indent=2, allow_nan=False) + "\n").encode()
        while data:
            count = os.write(descriptor, data)
            if count == 0:
                raise AuditError("output_unavailable")
            data = data[count:]
        os.fsync(descriptor)
    except FileExistsError:
        raise AuditError("output_exists") from None
    except OSError:
        raise AuditError("output_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)
