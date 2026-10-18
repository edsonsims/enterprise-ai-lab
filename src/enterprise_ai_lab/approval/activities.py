"""A local notification is one SQLite row, never an email or external action."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .contracts import (
    ApplyApprovedNote, ApprovalValidationError, EffectReceipt, format_utc,
    note_digest, validate_effect,
)


class SinkConflict(ApprovalValidationError):
    """An existing effect key was presented with different immutable content."""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class LocalNotifier:
    """At-least-once activity delivery, with one transactional local effect.

    Fault options are trusted worker configuration used by the crash demo. They
    are not request fields and cannot change the note, destination, or authority.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        pause_after_commit_seconds: float = 0,
        fault_once_after_commit: bool = False,
    ) -> None:
        if isinstance(pause_after_commit_seconds, bool) or not isinstance(pause_after_commit_seconds, (int, float)):
            raise ValueError("invalid post-commit pause")
        if not 0 <= pause_after_commit_seconds <= 15:
            raise ValueError("post-commit pause must be between 0 and 15 seconds")
        if not isinstance(fault_once_after_commit, bool):
            raise ValueError("invalid fault switch")
        self.database_path = Path(database_path)
        self.pause_after_commit_seconds = pause_after_commit_seconds
        self.fault_once_after_commit = fault_once_after_commit

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                effect_key TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                note_sha256 TEXT NOT NULL,
                note_json TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                published_at TEXT NOT NULL,
                UNIQUE(tenant_id, request_id)
            )
        """)
        return connection

    @activity.defn(name="publish_approved_note")
    def publish_approved_note(self, value: ApplyApprovedNote) -> EffectReceipt:
        try:
            validate_effect(value)
        except ApprovalValidationError as exc:
            raise ApplicationError("invalid approved note", type="ApprovalValidationError", non_retryable=True) from exc
        request = value.request
        # Identifiers cannot contain '/', so this is unambiguous and request-scoped.
        effect_key = f"approval/{request.tenant_id}/{request.request_id}"
        digest = note_digest(request.payload)
        note_json, receipt_json = _json(asdict(request.payload)), _json(asdict(value.receipt))
        immutable = (
            request.tenant_id, request.request_id, request.snapshot_sha256,
            digest, note_json, receipt_json,
        )
        connection = self._connect()
        inserted = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT tenant_id,request_id,snapshot_sha256,note_sha256,note_json,receipt_json FROM notifications WHERE effect_key=?",
                (effect_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO notifications VALUES (?,?,?,?,?,?,?,?)",
                    (effect_key, *immutable, format_utc(datetime.now(timezone.utc))),
                )
                inserted = True
            elif existing != immutable:
                raise SinkConflict("effect key already contains different immutable content")
            connection.commit()
        except SinkConflict as exc:
            connection.rollback()
            raise ApplicationError("local effect conflicts with prior content", type="SinkConflict", non_retryable=True) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        # A killed process after commit loses the activity acknowledgement. Retry
        # returns the same receipt only after checking the exact committed content.
        if inserted and self.pause_after_commit_seconds:
            time.sleep(self.pause_after_commit_seconds)
        if inserted and self.fault_once_after_commit:
            raise RuntimeError("injected lost acknowledgement after durable local commit")
        return EffectReceipt(effect_key, request.tenant_id, request.request_id, request.snapshot_sha256, digest)

    def list_notifications(self, tenant_id: str | None = None) -> list[dict]:
        """Trusted local inspection for demos; this is not a public data API."""
        connection = self._connect()
        try:
            connection.row_factory = sqlite3.Row
            if tenant_id is None:
                rows = connection.execute("SELECT * FROM notifications ORDER BY effect_key").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM notifications WHERE tenant_id=? ORDER BY effect_key", (tenant_id,)
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
