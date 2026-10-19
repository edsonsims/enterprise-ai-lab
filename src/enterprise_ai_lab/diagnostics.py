"""Finite failure metadata for a trusted local operator, never exception text.

This optional best-effort sink is separate from the durable audit. A host or
filesystem failure can lose diagnostic events; it must not change HTTP results.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat

MAX_BYTES = 64 * 1024
FIELDS = frozenset({"schema_version", "operation", "stage", "category", "exception_type", "rpc_status", "sqlite_error"})
OPERATIONS = frozenset({"review_create", "review_list", "review_detail", "review_decide", "review_pdf",
                        "review_preview", "review_audit", "review_logout", "review_login"})
STAGES = frozenset({"admission", "review_operation", "review_authorize", "review_make_request",
                    "review_candidate_load", "review_request_save", "review_intent_audit", "review_submit",
                    "review_snapshot_validate", "review_reconcile", "review_view", "publication_candidate",
                    "temporal_query", "temporal_start", "temporal_update"})
CATEGORIES = frozenset({"capacity", "timeout", "rpc", "sqlite", "io", "runtime", "other"})
EXCEPTION_TYPES = frozenset({"none", "timeout_error", "rpc_error", "os_error", "runtime_error", "other",
    "sqlite_error", "sqlite_operational_error", "sqlite_integrity_error", "sqlite_database_error",
    "sqlite_programming_error", "sqlite_data_error", "sqlite_interface_error", "sqlite_internal_error",
    "sqlite_not_supported_error"})
RPC_STATUSES = frozenset({"OK", "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED", "NOT_FOUND",
    "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "FAILED_PRECONDITION", "ABORTED",
    "OUT_OF_RANGE", "UNIMPLEMENTED", "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED"})
# Only real SQLite result-code constants may be emitted, never arbitrary attrs.
SQLITE_ERRORS = frozenset(name for name, value in vars(sqlite3).items()
    if name.startswith("SQLITE_") and type(value) is int and name.split("_")[1] in {
        "OK", "ERROR", "INTERNAL", "PERM", "ABORT", "BUSY", "LOCKED", "NOMEM", "READONLY", "INTERRUPT",
        "IOERR", "CORRUPT", "NOTFOUND", "FULL", "CANTOPEN", "PROTOCOL", "EMPTY", "SCHEMA", "TOOBIG",
        "CONSTRAINT", "MISMATCH", "MISUSE", "NOLFS", "AUTH", "FORMAT", "RANGE", "NOTADB", "NOTICE",
        "WARNING", "ROW", "DONE"})
_SQLITE_TYPES = (
    (sqlite3.OperationalError, "sqlite_operational_error"), (sqlite3.IntegrityError, "sqlite_integrity_error"),
    (sqlite3.DatabaseError, "sqlite_database_error"), (sqlite3.ProgrammingError, "sqlite_programming_error"),
    (sqlite3.DataError, "sqlite_data_error"), (sqlite3.InterfaceError, "sqlite_interface_error"),
    (sqlite3.InternalError, "sqlite_internal_error"), (sqlite3.NotSupportedError, "sqlite_not_supported_error"),
    (sqlite3.Error, "sqlite_error"),
)


@dataclass
class _Failure:
    exception: BaseException
    stage: str
    category: str
    exception_type: str
    rpc_status: str | None = None
    sqlite_error: str | None = None


@dataclass
class _Scope:
    operation: str
    failure: _Failure | None = None


_scope: ContextVar[_Scope | None] = ContextVar("failure_diagnostic_scope", default=None)
_stage: ContextVar[str] = ContextVar("failure_diagnostic_stage", default="review_operation")


def _member(value, choices):
    return type(value) is str and len(value) <= 64 and value in choices


def _classification(exc):
    kind = type(exc)
    for known, name in _SQLITE_TYPES:
        if kind is known:
            value = getattr(exc, "sqlite_errorname", None)
            return "sqlite", name, value if _member(value, SQLITE_ERRORS) else None
    if kind is TimeoutError:
        return "timeout", "timeout_error", None
    if kind is OSError:
        return "io", "os_error", None
    if kind is RuntimeError:
        return "runtime", "runtime_error", None
    return "other", "other", None


@contextmanager
def diagnostic_scope(operation: str):
    """Isolate a static operation and its propagating failure from other requests."""
    if not _member(operation, OPERATIONS):
        raise ValueError("unknown static diagnostic operation")
    token = _scope.set(_Scope(operation))
    stage_token = _stage.set("review_operation")
    try:
        yield
    finally:
        _stage.reset(stage_token)
        _scope.reset(token)


@contextmanager
def diagnostic_stage(stage: str):
    """Retain the deepest stage only for the same escaping exception object."""
    if not _member(stage, STAGES):
        raise ValueError("unknown static diagnostic stage")
    token = _stage.set(stage)
    try:
        yield
    except BaseException as exc:
        scope = _scope.get()
        try:
            if scope is not None and (scope.failure is None or scope.failure.exception is not exc):
                category, kind, sqlite_error = _classification(exc)
                scope.failure = _Failure(exc, stage, category, kind, sqlite_error=sqlite_error)
        except Exception:
            pass  # Diagnostics never replace the original escaping exception.
        raise
    finally:
        _stage.reset(token)


def mark_rpc_failure(exc: BaseException, rpc_status: str | None):
    """Mark a caught exact trusted SDK RPCError using only a fixed status name.

    The publication adapter checks the concrete SDK class and status enum before
    calling this helper. No exception attribute, name, message or cause is read.
    """
    scope = _scope.get()
    if scope is not None:
        scope.failure = _Failure(exc, _stage.get(), "rpc", "rpc_error",
                                rpc_status if _member(rpc_status, RPC_STATUSES) else None)


def validate_record(record) -> dict:
    """Return a fresh finite flat record, or reject every unknown field/value."""
    if type(record) is not dict or len(record) != len(FIELDS):
        raise ValueError("invalid diagnostic record")
    if any(type(key) is not str for key in record) or set(record) != FIELDS:
        raise ValueError("invalid diagnostic fields")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("invalid diagnostic version")
    for key, choices in (("operation", OPERATIONS), ("stage", STAGES), ("category", CATEGORIES),
                         ("exception_type", EXCEPTION_TYPES)):
        if not _member(record[key], choices):
            raise ValueError("invalid diagnostic enum")
    for key, choices in (("rpc_status", RPC_STATUSES), ("sqlite_error", SQLITE_ERRORS)):
        if record[key] is not None and not _member(record[key], choices):
            raise ValueError("invalid diagnostic status")
    kind = record["exception_type"]
    expected = ("capacity" if kind == "none" else "rpc" if kind == "rpc_error" else
                "sqlite" if kind.startswith("sqlite_") else "timeout" if kind == "timeout_error" else
                "io" if kind == "os_error" else "runtime" if kind == "runtime_error" else "other")
    if (record["category"] != expected
            or (record["rpc_status"] is not None and kind != "rpc_error")
            or (record["sqlite_error"] is not None and not kind.startswith("sqlite_"))):
        raise ValueError("inconsistent diagnostic classification")
    return {key: record[key] for key in sorted(FIELDS)}


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate diagnostic field")
        value[key] = item
    return value


def _decode(raw):
    if len(raw) > MAX_BYTES or (raw and not raw.endswith(b"\n")):
        raise ValueError("invalid diagnostic length")
    result = []
    for line in raw.splitlines():
        if not line or len(line) > 1024:
            raise ValueError("invalid diagnostic line")
        result.append(validate_record(json.loads(line.decode("utf-8"), object_pairs_hook=_pairs)))
    return result


def _safe_file(fd, path):
    info = os.fstat(fd)
    current = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > MAX_BYTES
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
        raise ValueError("unsafe diagnostic file")
    return info


def _read(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, size = [], 0
    while True:
        chunk = os.read(fd, min(8192, MAX_BYTES + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BYTES:
            raise ValueError("diagnostic file exceeds limit")


def read_diagnostics(path) -> list[dict]:
    """Strictly validate a bounded owned JSONL sink; never return raw fallback."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(Path(path), flags)
    try:
        _safe_file(fd, path)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        _safe_file(fd, path)
        result = _decode(_read(fd))
        _safe_file(fd, path)
        return result
    finally:
        os.close(fd)


class DiagnosticRecorder:
    """Optional bounded metadata sink; None means no logging or file access."""
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None

    def record(self, record) -> bool:
        """Append only validated safe JSON; an unavailable sink cannot fail HTTP."""
        if self.path is None:
            return False
        fd = None
        try:
            encoded = (json.dumps(validate_record(record), sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
            flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(self.path, flags)
            _safe_file(fd, self.path)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = _safe_file(fd, self.path)
            raw = _read(fd)
            _decode(raw)
            if len(raw) != info.st_size or info.st_size + len(encoded) > MAX_BYTES:
                return False
            if _safe_file(fd, self.path).st_size != info.st_size:
                return False
            try:
                written = os.write(fd, encoded)
                if written != len(encoded):
                    raise OSError("incomplete diagnostic append")
            except Exception:
                # Best effort restore under the same lock. A failing filesystem
                # can still lose events; strict export rejects a torn suffix.
                try:
                    _safe_file(fd, self.path)
                    os.ftruncate(fd, info.st_size)
                except Exception:
                    pass
                return False
            return True
        except Exception:
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def failure(self, operation: str, exc: BaseException | None = None, *, category: str | None = None) -> bool:
        """Record only static metadata; never stringify or introspect unknown errors."""
        try:
            scope = _scope.get()
            if not _member(operation, OPERATIONS) or scope is None or scope.operation != operation:
                return False
            if category is not None and not _member(category, {"capacity"}):
                return False
            if category == "capacity" and exc is None:
                failure = _Failure(Exception(), "admission", "capacity", "none")
            elif category is None and isinstance(exc, BaseException):
                if scope.failure is not None and scope.failure.exception is exc:
                    failure = scope.failure
                else:
                    group, kind, sqlite_error = _classification(exc)
                    failure = _Failure(exc, "review_operation", group, kind, sqlite_error=sqlite_error)
            else:
                return False
            return self.record({"schema_version": 1, "operation": operation, "stage": failure.stage,
                "category": failure.category, "exception_type": failure.exception_type,
                "rpc_status": failure.rpc_status, "sqlite_error": failure.sqlite_error})
        except Exception:
            return False
