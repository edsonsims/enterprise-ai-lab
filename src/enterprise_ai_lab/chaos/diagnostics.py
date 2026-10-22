"""Bounded finite experiment metadata; never exception text or business records."""

from __future__ import annotations

import json
import sqlite3

CASES = frozenset(
    {
        "cli",
        "setup",
        "finalize",
        "healthy_primary",
        "primary_killed_after_durable_outcome",
        "primary_disconnect_before_read",
        "primary_killed_after_intent",
        "primary_stalled_after_read",
        "both_transports_disconnect",
        "total_work_deadline",
        "terminal_permission_denial",
        "terminal_hidden_row",
        "terminal_invalid_tenant_argument",
        "terminal_source_busy",
        "terminal_audit_unavailable",
        "terminal_wire_provenance_mismatch",
        "explicit_fresh_primary_recovery",
        "caller_cancellation_after_durable_outcome",
    }
)
STAGES = frozenset(
    {
        "output_refusal",
        "demo_run",
        "report_write",
        "work_directory",
        "fixture_initialize",
        "source_snapshot",
        "case_setup",
        "before_audit",
        "admission",
        "read",
        "outcome_check",
        "attempt_check",
        "after_audit",
        "response_check",
        "case_record",
        "case_check",
        "cancellation_marker",
        "cancellation_request",
        "cancellation_completion",
        "cancellation_check",
        "source_integrity",
        "final_audit",
        "report_metadata",
    }
)
READ_CODES = frozenset(
    {
        "transport_timeout",
        "transport_closed",
        "total_deadline",
        "attempt_deadline",
        "attempts_exhausted",
        "provenance_mismatch",
        "endpoint_alias",
        "invalid_request",
        "invalid_payload",
        "invalid_catalog",
        "tool_error",
        "cleanup_unconfirmed",
        "internal_error",
    }
)
TOOL_CODES = frozenset(
    {
        "invalid_request",
        "forbidden",
        "not_found",
        "resource_busy",
        "audit_unavailable",
        "internal_error",
    }
)
FAILURE_CODES = READ_CODES | TOOL_CODES | {"unknown"}
OUTCOMES = READ_CODES | {"success", "caller_cancelled", "unconfirmed", "unknown"}
ATTEMPT_OUTCOMES = READ_CODES | {
    "response",
    "caller_cancelled",
    "interrupted",
    "unconfirmed",
    "unknown",
}
EXCEPTION_TYPES = frozenset(
    {
        "assertion_error",
        "timeout_error",
        "os_error",
        "sqlite_error",
        "value_error",
        "runtime_error",
        "read_failure",
        "gateway_error",
        "exception_group",
        "other",
    }
)
FIELDS = frozenset(
    {
        "schema_version",
        "case",
        "stage",
        "exception_type",
        "failure_code",
        "expected_outcome",
        "observed_outcome",
        "attempt_count",
        "last_attempt_outcome",
        "tool_error_code",
    }
)
MAX_BYTES = 1023


def _member(value, choices):
    return type(value) is str and len(value) <= 64 and value in choices


def validate_failure(record):
    """Return a fresh finite flat record; reject arbitrary fields and values."""
    if (
        type(record) is not dict
        or any(type(key) is not str for key in record)
        or set(record) != FIELDS
    ):
        raise ValueError("invalid diagnostic fields")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("invalid diagnostic version")
    for key, choices in (
        ("case", CASES),
        ("stage", STAGES),
        ("exception_type", EXCEPTION_TYPES),
        ("failure_code", FAILURE_CODES),
        ("expected_outcome", OUTCOMES),
        ("observed_outcome", OUTCOMES),
        ("last_attempt_outcome", ATTEMPT_OUTCOMES),
    ):
        if not _member(record[key], choices):
            raise ValueError("invalid diagnostic enum")
    count = record["attempt_count"]
    if count is not None and (type(count) is not int or not 0 <= count <= 2):
        raise ValueError("invalid diagnostic attempt count")
    tool = record["tool_error_code"]
    if tool is not None and not _member(tool, TOOL_CODES):
        raise ValueError("invalid diagnostic tool code")
    if count in (None, 0) and (
        record["last_attempt_outcome"] != "unconfirmed" or tool is not None
    ):
        raise ValueError("inconsistent diagnostic attempt")
    return {key: record[key] for key in sorted(FIELDS)}


def format_failure(record):
    result = json.dumps(
        validate_failure(record),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if len(result.encode("ascii")) > MAX_BYTES:
        raise ValueError("diagnostic exceeds bound")
    return result


def _exception_type(exc):
    kind = type(exc)
    for known, label in (
        (AssertionError, "assertion_error"),
        (TimeoutError, "timeout_error"),
        (ValueError, "value_error"),
        (RuntimeError, "runtime_error"),
    ):
        if kind is known:
            return label
    if kind is ExceptionGroup:
        return "exception_group"
    if isinstance(exc, sqlite3.Error):
        return "sqlite_error"
    if isinstance(exc, OSError):
        return "os_error"
    return "other"


class DemoDiagnostics:
    """Per-invocation state. No global scope, file sink or original error storage."""

    def __init__(self):
        self.begin("cli", "demo_run")

    def begin(self, case, stage, *, expected="unconfirmed"):
        if not _member(case, CASES) or not _member(expected, OUTCOMES):
            raise ValueError("invalid static diagnostic case")
        self.case, self.expected, self.observed, self.records = (
            case,
            expected,
            "unconfirmed",
            None,
        )
        self.stage(stage)

    def stage(self, stage):
        if not _member(stage, STAGES):
            raise ValueError("invalid static diagnostic stage")
        self.current_stage = stage

    def observe(self, outcome):
        self.observed = outcome if _member(outcome, OUTCOMES) else "unknown"

    def bind(self, records):
        self.records = records

    def failure(self, exc, *, known_kind=None, code=None):
        count, last, tool = 0, "unconfirmed", None
        if self.records is not None:
            count = (
                len(self.records)
                if type(self.records) is list and len(self.records) <= 2
                else None
            )
            if count and type(self.records[-1]) is dict:
                value = self.records[-1].get("outcome")
                last = value if _member(value, ATTEMPT_OUTCOMES) else "unknown"
                value = self.records[-1].get("tool_error_code")
                tool = value if _member(value, TOOL_CODES) else None
        kind = (
            known_kind
            if _member(known_kind, {"read_failure", "gateway_error"})
            else _exception_type(exc)
        )
        return validate_failure(
            {
                "schema_version": 1,
                "case": self.case,
                "stage": self.current_stage,
                "exception_type": kind,
                "failure_code": code if _member(code, FAILURE_CODES) else "unknown",
                "expected_outcome": self.expected,
                "observed_outcome": self.observed,
                "attempt_count": count,
                "last_attempt_outcome": last,
                "tool_error_code": tool,
            }
        )
