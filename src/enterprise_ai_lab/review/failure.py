"""Strictly allowlisted failure evidence; never export private process logs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

PHASES = frozenset({"setup", "login", "create_initial", "create_browser_main",
    "create_browser_reject", "create_browser_beta", "create_interactive", "decision",
    "detail", "permissions", "audit", "session", "preview", "browser", "replay", "other"})
ERRORS = frozenset({"invalid_request", "unauthorized", "forbidden", "not_found", "conflict", "unavailable"})


def phase_for_case(case):
    # Caller values are mapped to constants, never copied into the artifact.
    exact = {"create-browser-main": "create_browser_main", "create-browser-reject": "create_browser_reject",
             "create-browser-beta": "create_browser_beta", "create-review-public-pages": "create_initial",
             "create-reject-public-pages": "create_initial", "create-beta-public-pages": "create_initial",
             "create-hands-on-review": "create_interactive", "create-hands-on-reject": "create_interactive"}
    if type(case) is not str:
        return "other"
    if case in exact:
        return exact[case]
    for prefix, phase in (("login-", "login"), ("approve-", "decision"), ("reject-", "decision"),
                          ("audit-", "audit"), ("logout", "session"), ("logged-out", "session")):
        if case.startswith(prefix):
            return phase
    return "other"


def http_summary(response):
    """Retain only a status, normalized format and a finite public error code."""
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    format_name = {"application/json": "json", "text/plain": "text_plain"}.get(content_type, "other")
    code = None
    if format_name == "json" and len(response.content) <= 2048:
        try:
            data = json.loads(response.content)
            error = data.get("error") if type(data) is dict else None
            candidate = error.get("code") if type(error) is dict else None
            code = candidate if type(candidate) is str and candidate in ERRORS else "unrecognized"
        except (ValueError, UnicodeError, RecursionError):
            code = "unrecognized"
    return {"status": response.status_code, "format": format_name, "error_code": code}


def process_state(process):
    if process is None:
        return "unavailable"
    try:
        return "running" if process._child.poll() is None else "exited"
    except Exception:
        return "unavailable"


def capture_failure(*, phase, http, diagnostics_path, portal=None, resource=None):
    from ..diagnostics import read_diagnostics
    try:
        records = read_diagnostics(diagnostics_path)
        status = "available"
    except FileNotFoundError:
        records, status = [], "absent"
    except Exception:
        records, status = [], "invalid"
    result = {"schema_version": 1, "phase": phase if type(phase) is str and phase in PHASES else "other",
              "http": http, "diagnostics_status": status,
              "diagnostics_complete": status == "available" and len(records) <= 16, "diagnostics": records[-16:],
              "children": {"portal": process_state(portal), "resource": process_state(resource)}}
    return validate_failure(result)


def validate_failure(value):
    from ..diagnostics import validate_record
    if (type(value) is not dict or set(value) != {"schema_version", "phase", "http", "diagnostics_status",
            "diagnostics_complete", "diagnostics", "children"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["phase"]) is not str or value["phase"] not in PHASES
            or type(value["diagnostics_status"]) is not str
            or value["diagnostics_status"] not in ("available", "absent", "invalid")
            or type(value["diagnostics_complete"]) is not bool
            or type(value["diagnostics"]) is not list or len(value["diagnostics"]) > 16
            or type(value["children"]) is not dict or set(value["children"]) != {"portal", "resource"}
            or any(type(v) is not str or v not in ("running", "exited", "unavailable") for v in value["children"].values())
            or (value["diagnostics_status"] != "available" and
                (value["diagnostics_complete"] or value["diagnostics"]))):
        raise ValueError("Invalid bounded failure evidence")
    if value["http"] is not None:
        http = value["http"]
        if (type(http) is not dict or set(http) != {"status", "format", "error_code"}
                or type(http["status"]) is not int or not 100 <= http["status"] <= 599
                or type(http["format"]) is not str or http["format"] not in ("json", "text_plain", "other")
                or (http["error_code"] is not None and (type(http["error_code"]) is not str
                    or http["error_code"] not in (*ERRORS, "unrecognized")))):
            raise ValueError("Invalid HTTP failure evidence")
    return {"schema_version": 1, "phase": value["phase"],
            "http": None if value["http"] is None else dict(value["http"]),
            "diagnostics_status": value["diagnostics_status"], "diagnostics_complete": value["diagnostics_complete"],
            "diagnostics": [validate_record(record) for record in value["diagnostics"]],
            "children": dict(value["children"])}


def read_failure(path):
    """Read at most 16 KiB from an owned regular file, with strict JSON fields."""
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate failure field")
            result[key] = item
        return result
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 16_384 or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("Unsafe failure evidence file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Failure evidence exceeds its byte bound")
        return validate_failure(json.loads(raw, object_pairs_hook=pairs))
    finally:
        if fd is not None:
            os.close(fd)


def save_failure(path, value):
    """Create one bounded artifact without replacing an existing record/file."""
    value = validate_failure(value)
    encoded = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(encoded) > 16_384:
        raise ValueError("Failure evidence exceeds its byte bound")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Failure evidence needs a regular file")
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    return value
