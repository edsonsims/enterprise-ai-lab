"""Local append-only hash-linked audit, without tokens or document bodies.

This detects ordinary row alteration and accidental corruption against the
retained chain. A host administrator can replace the database and all hashes;
this is not an externally anchored or tamper-proof audit service.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from ..approval.contracts import format_utc, identifier, parse_utc
from ..publication.contracts import digest
from ..publication.storage import canonical

ZERO_HASH = "0" * 64
MAX_AUDIT_EVENTS = 100_000


class AuditIntegrityError(RuntimeError):
    """The local audit chain is inconsistent or has reached its bounded limit."""


class AuditLog:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def connect(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("""CREATE TABLE IF NOT EXISTS review_audit (
            sequence INTEGER PRIMARY KEY,
            event_key TEXT UNIQUE NOT NULL,
            event_json TEXT NOT NULL,
            previous_sha256 TEXT NOT NULL,
            event_sha256 TEXT NOT NULL
        )""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS review_audit_no_update
            BEFORE UPDATE ON review_audit BEGIN SELECT RAISE(ABORT, 'append-only audit'); END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS review_audit_no_delete
            BEFORE DELETE ON review_audit BEGIN SELECT RAISE(ABORT, 'append-only audit'); END""")
        return connection

    @staticmethod
    def _read_chain(connection):
        rows = connection.execute("SELECT sequence,event_key,event_json,previous_sha256,event_sha256 "
                                  "FROM review_audit ORDER BY sequence LIMIT ?", (MAX_AUDIT_EVENTS + 1,)).fetchall()
        if len(rows) > MAX_AUDIT_EVENTS:
            raise AuditIntegrityError("audit exceeds its local event bound")
        previous = ZERO_HASH
        values = []
        for sequence, key, encoded, previous_hash, event_hash in rows:
            try:
                if not isinstance(encoded, str) or len(encoded.encode()) > 8192:
                    raise ValueError("invalid event size")
                value = json.loads(encoded)
                expected = hashlib.sha256((previous + "\n" + encoded).encode()).hexdigest()
                if (sequence != len(values) + 1 or not isinstance(value, dict)
                        or canonical(value) != encoded or value.get("sequence") != sequence
                        or value.get("event_key") != key or previous_hash != previous
                        or event_hash != expected):
                    raise ValueError("audit chain differs")
            except (ValueError, TypeError, UnicodeError) as exc:
                raise AuditIntegrityError("local audit chain failed verification") from exc
            values.append({**value, "previous_sha256": previous_hash, "event_sha256": event_hash})
            previous = event_hash
        return values

    def events(self, tenant_id: str | None = None):
        if tenant_id is not None:
            identifier(tenant_id)
        connection = self.connect()
        try:
            values = self._read_chain(connection)
        finally:
            connection.close()
        return [value for value in values if tenant_id is None or value["tenant_id"] == tenant_id]

    def log(self, **fields):
        """Named alias used by the web boundary for login/logout/PDF observations."""
        return self.append(**fields)

    def append(self, *, tenant_id: str, actor_id: str, action: str, result: str,
               request_id: str | None = None, candidate_sha256: str | None = None,
               snapshot_sha256: str | None = None, command_id: str | None = None,
               identity_kind: str = "user", event_key: str | None = None,
               reason_code: str | None = None, workflow_accepted_at: str | None = None,
               document_id: str | None = None):
        for value in (tenant_id, actor_id, action, result):
            identifier(value)
        for value in (request_id, command_id, reason_code, document_id):
            if value is not None:
                identifier(value)
        for value in (candidate_sha256, snapshot_sha256):
            if value is not None:
                digest(value)
        if identity_kind not in ("user", "service_or_unknown", "system"):
            raise ValueError("unsupported audit subject kind")
        if workflow_accepted_at is not None:
            parse_utc(workflow_accepted_at)
        if event_key is None:
            event_key = uuid.uuid4().hex
        if not isinstance(event_key, str) or not 1 <= len(event_key) <= 256:
            raise ValueError("invalid audit idempotency key")
        payload = dict(tenant_id=tenant_id, actor_id=actor_id, identity_kind=identity_kind,
                       action=action, result=result, request_id=request_id,
                       candidate_sha256=candidate_sha256, snapshot_sha256=snapshot_sha256,
                       command_id=command_id, reason_code=reason_code, event_key=event_key,
                       workflow_accepted_at=workflow_accepted_at, document_id=document_id)
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            chain = self._read_chain(connection)
            prior = next((row for row in chain if row["event_key"] == event_key), None)
            if prior is not None:
                if any(prior.get(key) != value for key, value in payload.items()):
                    raise AuditIntegrityError("audit event key conflicts with different content")
                connection.commit()
                return prior
            if len(chain) >= MAX_AUDIT_EVENTS:
                raise AuditIntegrityError("audit has reached its local event bound")
            sequence = len(chain) + 1
            previous = chain[-1]["event_sha256"] if chain else ZERO_HASH
            value = {**payload, "sequence": sequence,
                     "at": format_utc(datetime.now(timezone.utc))}
            encoded = canonical(value)
            event_hash = hashlib.sha256((previous + "\n" + encoded).encode()).hexdigest()
            connection.execute("INSERT INTO review_audit VALUES (?,?,?,?,?)",
                               (sequence, event_key, encoded, previous, event_hash))
            connection.commit()
            return {**value, "previous_sha256": previous, "event_sha256": event_hash}
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
