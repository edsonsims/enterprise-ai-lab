"""Metadata-only local audit, validated before reads and before responses.

The chain and SQLite append-only triggers detect accidental corruption and
ordinary row edits. They are not an external anchor against a host administrator
who can replace the file, schema and every hash.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock

from .store import GatewayError, NAME

MAX_EVENTS = 10_000
ZERO_HASH = "0" * 64
HEX = re.compile(r"[0-9a-f]{64}\Z")
CALL_ID = re.compile(r"[0-9a-f]{32}\Z")
TOOLS = ("list_work_orders", "get_work_order", "get_asset_diagnostics")
ERRORS = ("invalid_request", "forbidden", "not_found", "resource_busy", "audit_unavailable", "internal_error")
TABLE = "CREATE TABLE gateway_audit (sequence INTEGER PRIMARY KEY, event_json TEXT NOT NULL, previous_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL)"
TRIGGERS = {
    "gateway_audit_no_update": "CREATE TRIGGER gateway_audit_no_update BEFORE UPDATE ON gateway_audit BEGIN SELECT RAISE(ABORT, 'append-only audit'); END",
    "gateway_audit_no_delete": "CREATE TRIGGER gateway_audit_no_delete BEFORE DELETE ON gateway_audit BEGIN SELECT RAISE(ABORT, 'append-only audit'); END",
}
FIELDS = {"sequence", "at", "call_id", "phase", "subject", "tenant_id", "operation", "input_sha256",
          "input_digest_kind", "output_sha256", "outcome", "error_code", "row_count"}


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _validate(value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value["sequence"]) is not int or not 1 <= value["sequence"] <= MAX_EVENTS
            or not isinstance(value["at"], str) or len(value["at"]) > 40
            or not isinstance(value["call_id"], str) or not CALL_ID.fullmatch(value["call_id"])
            or value["phase"] not in ("intent", "outcome")
            or not all(isinstance(value[key], str) and NAME.fullmatch(value[key]) for key in ("subject", "tenant_id"))
            or value["operation"] not in (*TOOLS, "unknown_tool")
            or not isinstance(value["input_sha256"], str) or not HEX.fullmatch(value["input_sha256"])
            or value["input_digest_kind"] not in ("canonical_json", "rejected_descriptor")
            or (value["output_sha256"] is not None and (not isinstance(value["output_sha256"], str) or not HEX.fullmatch(value["output_sha256"])))
            or (value["error_code"] is not None and value["error_code"] not in ERRORS)
            or (value["row_count"] is not None and (type(value["row_count"]) is not int or not 0 <= value["row_count"] <= 10))):
        raise ValueError("invalid audit metadata")
    timestamp = datetime.fromisoformat(value["at"].replace("Z", "+00:00"))
    if timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
        raise ValueError("audit timestamps require UTC")
    if value["phase"] == "intent":
        if (value["outcome"] != "intent" or any(value[key] is not None for key in ("output_sha256", "error_code", "row_count"))):
            raise ValueError("intent must not claim a result")
    elif (value["outcome"] not in ("success", "error") or value["output_sha256"] is None
          or value["row_count"] is None
          or (value["outcome"] == "success" and value["error_code"] is not None)
          or (value["outcome"] == "error" and value["error_code"] is None)):
        raise ValueError("outcome needs a bounded result")


class GatewayAudit:
    """Lazy schema initialization allows the service to reject source aliases first."""
    def __init__(self, database_path):
        self.database_path = Path(database_path).absolute()
        self.lock = RLock()

    def _connect(self):
        path = self.database_path
        existed = path.exists() or path.is_symlink()
        if existed:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64_000_000:
                raise ValueError("audit needs a bounded regular file")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=.05)
        try:
            rows = connection.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
            if rows:
                actual = {row[1]: row[2] for row in rows}
                if actual != {"gateway_audit": TABLE, **TRIGGERS}:
                    # Crucially this occurs before journal-mode or schema writes:
                    # an accidental audit path cannot modify a business DB.
                    raise ValueError("existing database is not this audit schema")
            elif existed and path.stat().st_size:
                raise ValueError("existing database lacks its audit schema")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=50")
            if not rows:
                connection.execute(TABLE)
                for sql in TRIGGERS.values():
                    connection.execute(sql)
                connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _chain(connection):
        rows = connection.execute("SELECT sequence,event_json,previous_sha256,event_sha256 "
                                  "FROM gateway_audit ORDER BY sequence LIMIT ?", (MAX_EVENTS + 1,)).fetchall()
        if len(rows) > MAX_EVENTS:
            raise ValueError("audit event limit exceeded")
        previous, result, intents, outcomes = ZERO_HASH, [], {}, set()
        for sequence, encoded, prior_hash, event_hash in rows:
            if not isinstance(encoded, str) or len(encoded.encode()) > 4096:
                raise ValueError("audit event exceeds bound")
            value = json.loads(encoded)
            _validate(value)
            expected_hash = hashlib.sha256((previous + "\n" + encoded).encode()).hexdigest()
            if (value["sequence"] != sequence or sequence != len(result) + 1
                    or canonical(value) != encoded or prior_hash != previous or event_hash != expected_hash):
                raise ValueError("audit chain differs")
            call = value["call_id"]
            if value["phase"] == "intent":
                if call in intents:
                    raise ValueError("duplicate audit call intent")
                intents[call] = value
            else:
                intent = intents.get(call)
                if (intent is None or call in outcomes or any(value[key] != intent[key] for key in
                        ("subject", "tenant_id", "operation", "input_sha256", "input_digest_kind"))):
                    raise ValueError("audit outcome differs from its intent")
                outcomes.add(call)
            result.append({**value, "previous_sha256": prior_hash, "event_sha256": event_hash})
            previous = event_hash
        return result

    def append(self, *, call_id, phase, subject, tenant_id, operation, input_sha256,
               input_digest_kind="canonical_json", output_sha256=None, outcome="intent",
               error_code=None, row_count=None):
        with self.lock:
            connection = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                chain = self._chain(connection)
                if len(chain) >= MAX_EVENTS:
                    raise ValueError("audit event limit reached")
                value = dict(sequence=len(chain) + 1, at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                             call_id=call_id, phase=phase, subject=subject, tenant_id=tenant_id,
                             operation=operation, input_sha256=input_sha256, input_digest_kind=input_digest_kind,
                             output_sha256=output_sha256, outcome=outcome, error_code=error_code, row_count=row_count)
                _validate(value)
                # Validate correlations before committing this row, not only on
                # the next read. Unfinished intents are legitimate crash evidence.
                prior = [row for row in chain if row["call_id"] == call_id]
                if phase == "intent":
                    if prior:
                        raise ValueError("duplicate audit call")
                elif (len(prior) != 1 or prior[0]["phase"] != "intent" or any(value[key] != prior[0][key] for key in
                        ("subject", "tenant_id", "operation", "input_sha256", "input_digest_kind"))):
                    raise ValueError("outcome needs its exact intent")
                previous = chain[-1]["event_sha256"] if chain else ZERO_HASH
                encoded = canonical(value)
                event_hash = hashlib.sha256((previous + "\n" + encoded).encode()).hexdigest()
                connection.execute("INSERT INTO gateway_audit VALUES (?,?,?,?)",
                                   (value["sequence"], encoded, previous, event_hash))
                connection.commit()
                return {**value, "previous_sha256": previous, "event_sha256": event_hash}
            except Exception:
                if connection is not None:
                    connection.rollback()
                raise GatewayError("audit_unavailable", "The local audit could not be confirmed; no data is returned.") from None
            finally:
                if connection is not None:
                    connection.close()

    def events(self):
        """Trusted local inspection; this is deliberately not an MCP tool."""
        with self.lock:
            connection = None
            try:
                connection = self._connect()
                return self._chain(connection)
            except Exception:
                raise GatewayError("audit_unavailable", "The local audit failed verification.") from None
            finally:
                if connection is not None:
                    connection.close()
