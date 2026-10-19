"""Strict named reads with trusted launcher identity and fail-closed audit."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from threading import RLock
import uuid

from .audit import GatewayAudit, TOOLS, canonical
from .store import GatewayError, Principal, ReadOnlyStore

MAX_INPUT_BYTES = 16_384


def _input_digest(tool_name, arguments):
    """Hash bounded JSON exactly, or identify a safe rejected-input descriptor.

    An oversized/non-JSON object is not silently called an exact input digest.
    The metadata records the descriptor kind and never stores supplied values.
    """
    count = 0
    def check(value, depth=0):
        nonlocal count
        count += 1
        if count > 128 or depth > 8:
            raise ValueError("input exceeds structural bounds")
        if value is None or type(value) in (bool, int):
            if type(value) is int and value.bit_length() > 64:
                raise ValueError("input integer exceeds bound")
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("nonfinite input")
        elif type(value) is str:
            if len(value) > 8192 or len(value.encode("utf-8")) > MAX_INPUT_BYTES:
                raise ValueError("input string exceeds bound")
        elif type(value) is list:
            for child in value: check(child, depth + 1)
        elif type(value) is dict:
            for key, child in value.items():
                if type(key) is not str: raise ValueError("JSON keys must be strings")
                check(key, depth + 1); check(child, depth + 1)
        else:
            raise ValueError("input is not plain JSON")
    try:
        value = {"tool": tool_name, "arguments": arguments}
        check(value)
        encoded = canonical(value).encode("utf-8")
        if len(encoded) > MAX_INPUT_BYTES:
            raise ValueError("input exceeds byte bound")
        return hashlib.sha256(encoded).hexdigest(), "canonical_json", None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        descriptor = {"rejected_input": True, "tool_type": type(tool_name).__name__,
                      "argument_type": type(arguments).__name__}
        return hashlib.sha256(canonical(descriptor).encode()).hexdigest(), "rejected_descriptor", GatewayError(
            "invalid_request", "Expected bounded plain JSON tool arguments.")


def _family(path):
    path = Path(path).resolve(strict=False)
    return {path, *(Path(str(path) + suffix).resolve(strict=False) for suffix in ("-wal", "-shm", "-journal"))}


def _inodes(paths):
    result = set()
    for path in paths:
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        result.add((info.st_dev, info.st_ino))
    return result


class GatewayService:
    def __init__(self, store: ReadOnlyStore, audit: GatewayAudit, principal: Principal):
        if not isinstance(store, ReadOnlyStore) or not isinstance(audit, GatewayAudit) or not isinstance(principal, Principal):
            raise ValueError("validated local store, audit and launcher principal required")
        self.store, self.audit, self.principal = store, audit, principal
        self.lock = RLock()
        self._distinct_files()

    def _distinct_files(self):
        try:
            source, audit = _family(self.store.database_path), _family(self.audit.database_path)
            conflict = source & audit or _inodes(source) & _inodes(audit)
        except OSError:
            raise GatewayError("audit_unavailable", "Database-file separation could not be confirmed.") from None
        if conflict:
            raise GatewayError("invalid_request", "Source and audit database files must be distinct.")

    def allowed_tools(self) -> list[str]:
        if not set(self.principal.roles).intersection(("reader", "analyst")):
            return []
        return [*TOOLS[:2], *([TOOLS[2]] if "analyst" in self.principal.roles else [])]

    def _validate(self, tool_name, arguments):
        if not isinstance(tool_name, str) or tool_name not in TOOLS:
            raise GatewayError("invalid_request", "Unknown named read operation.")
        if tool_name not in self.allowed_tools():
            raise GatewayError("forbidden", "The trusted profile does not allow this operation.")
        if type(arguments) is not dict:
            raise GatewayError("invalid_request", "Tool arguments must be one strict JSON object.")
        allowed = {"list_work_orders": {"status", "after_id", "limit"},
                   "get_work_order": {"work_order_id"}, "get_asset_diagnostics": {"asset_id"}}[tool_name]
        if not set(arguments) <= allowed:
            raise GatewayError("invalid_request", "Only documented operation fields are accepted.")
        if tool_name != "list_work_orders" and set(arguments) != allowed:
            raise GatewayError("invalid_request", "The operation requires its resource identifier.")
        if tool_name == "list_work_orders":
            if (any(key in arguments and arguments[key] is not None and type(arguments[key]) is not str for key in ("status", "after_id"))
                    or ("limit" in arguments and type(arguments["limit"]) is not int)):
                raise GatewayError("invalid_request", "Operation fields have the wrong scalar type.")
        elif any(type(value) is not str for value in arguments.values()):
            raise GatewayError("invalid_request", "The operation requires a string identifier.")

    def call(self, tool_name, arguments):
        with self.lock:
            # SQLite may open its -wal/-shm/-journal files as well as its main
            # file. Reject aliases across both entire families before audit I/O.
            self._distinct_files()
            input_sha256, digest_kind, input_error = _input_digest(tool_name, arguments)
            operation = tool_name if isinstance(tool_name, str) and tool_name in TOOLS else "unknown_tool"
            metadata = dict(call_id=uuid.uuid4().hex, subject=self.principal.subject,
                            tenant_id=self.principal.tenant_id, operation=operation,
                            input_sha256=input_sha256, input_digest_kind=digest_kind)
            # Audit errors deliberately take precedence over all results. If the
            # intent is unavailable, no source SQL is attempted at all.
            self.audit.append(**metadata, phase="intent", outcome="intent")
            error, response, count = None, None, 0
            try:
                if input_error is not None:
                    raise input_error
                self._validate(tool_name, arguments)
                data = getattr(self.store, operation)(self.principal, **arguments)
                response = {"data": data, "meta": {"operation": operation,
                                                    "tenant_id": self.principal.tenant_id}}
                count = len(data["items"]) if operation == "list_work_orders" else 1
            except GatewayError as exc:
                error = exc
            except Exception:
                error = GatewayError("internal_error", "The local operation could not be confirmed.")
            result = response if error is None else {"error": {"code": error.code, "message": error.safe_message}}
            output_sha256 = hashlib.sha256(canonical(result).encode("utf-8")).hexdigest()
            # A source read may already have happened if this append fails.
            # Its data is withheld; the surviving intent is honest gap evidence.
            self.audit.append(**metadata, phase="outcome", outcome="success" if error is None else "error",
                              output_sha256=output_sha256, error_code=None if error is None else error.code,
                              row_count=count)
            if error is not None:
                raise error
            return response
