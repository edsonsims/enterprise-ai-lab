"""Bounded SQLite mapping vault for a trusted local filesystem and operator.

Original documents are never stored. Mapped substrings are plaintext and must
remain in this private database. ID tombstones prevent reuse after expiry.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time

from .contracts import (HEX_ID_RE, KINDS, MAX_TEXT_BYTES, TOKEN_PREFIX, TOKEN_RE,
                        PrivacyError, validate_id, validate_scope, validate_text)
from .detector import detect

MAX_DB_BYTES = 32 * 1024 * 1024
_TABLES = {
    "meta": ("version",),
    "ids": ("id", "kind"),
    "contexts": ("context_id", "tenant", "client", "created_at", "expires_at", "mapping_count"),
    "mappings": ("token_id", "context_id", "kind", "original"),
    "audit": ("sequence", "action", "outcome", "count"),
}
_SCHEMA = (
    "CREATE TABLE meta(version INTEGER NOT NULL)",
    "INSERT INTO meta VALUES(1)",
    "CREATE TABLE ids(id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('context','token')))",
    "CREATE TABLE contexts(context_id TEXT PRIMARY KEY REFERENCES ids(id), tenant TEXT NOT NULL, "
    "client TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, mapping_count INTEGER NOT NULL)",
    "CREATE INDEX expiry ON contexts(expires_at,context_id)",
    "CREATE TABLE mappings(token_id TEXT PRIMARY KEY REFERENCES ids(id), context_id TEXT NOT NULL "
    "REFERENCES contexts(context_id) ON DELETE CASCADE, kind TEXT NOT NULL, original TEXT NOT NULL)",
    "CREATE INDEX context_mappings ON mappings(context_id)",
    "CREATE TABLE audit(sequence INTEGER PRIMARY KEY, action TEXT NOT NULL, outcome TEXT NOT NULL, count INTEGER NOT NULL)",
)


def _finite_error(exc) -> PrivacyError:
    if type(exc) in (sqlite3.OperationalError, sqlite3.IntegrityError, sqlite3.DatabaseError,
                     sqlite3.ProgrammingError, sqlite3.DataError, sqlite3.InterfaceError,
                     sqlite3.InternalError, sqlite3.NotSupportedError, sqlite3.Error):
        code = getattr(exc, "sqlite_errorcode", None)
        if type(code) is int and (code & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return PrivacyError("busy")
    return PrivacyError("storage_unavailable")


class Vault:
    """Each redact creates a new context; restore may repeat until wall-clock expiry.

    Capacity is finite, including non-reusable ID tombstones and audit rows.
    No automatic reset, log truncation or deletion of active mappings occurs.
    """
    def __init__(self, path, *, ttl_seconds=120, max_contexts=256, max_mappings=4096,
                 max_ids=131072, max_audit_rows=10000, purge_limit=32,
                 clock=time.time, random_hex=None):
        limits = ((ttl_seconds, 600), (max_contexts, 4096), (max_mappings, 65536),
                  (max_ids, 262144), (max_audit_rows, 65536), (purge_limit, 64))
        if any(type(value) is not int or not 1 <= value <= maximum for value, maximum in limits):
            raise PrivacyError("invalid_input")
        if not callable(clock) or (random_hex is not None and not callable(random_hex)):
            raise PrivacyError("invalid_input")
        self.ttl_seconds, self.max_contexts, self.max_mappings = ttl_seconds, max_contexts, max_mappings
        self.max_ids, self.max_audit_rows, self.purge_limit = max_ids, max_audit_rows, purge_limit
        self.clock = clock
        self.random_hex = random_hex or (lambda: secrets.token_hex(16))
        self._lock = threading.RLock()
        self._conn = None
        self._file_fd = self._dir_fd = None
        try:
            self.path = Path(path).absolute()
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            self._dir_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._check_parent()
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                self._file_fd = os.open(self.path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._dir_fd)
                created = True
            except FileExistsError:
                self._file_fd = os.open(self.path.name, flags, dir_fd=self._dir_fd)
                created = False
            self._check_files()
            self._conn = sqlite3.connect(self.path, timeout=0.05, isolation_level=None, check_same_thread=False)
            self._conn.execute("PRAGMA busy_timeout=50")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA trusted_schema=OFF")
            if created:
                self._conn.execute("PRAGMA journal_mode=DELETE")
                self._conn.execute("PRAGMA max_page_count=8192")
                self._conn.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    self._conn.execute(statement)
                self._conn.execute("COMMIT")
            self._validate_schema()
            if self._conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise PrivacyError("storage_unavailable")
            self._conn.execute("PRAGMA max_page_count=8192")
            self._check_files()
        except PrivacyError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise _finite_error(exc) from None

    def _check_parent(self):
        info = os.fstat(self._dir_fd)
        current = os.stat(self.path.parent, follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid() or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise PrivacyError("storage_unavailable")

    def _safe_file(self, name, *, optional=False, maximum=MAX_DB_BYTES):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._dir_fd)
        except FileNotFoundError:
            if optional:
                return None
            raise
        try:
            info = os.fstat(fd)
            current = os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > maximum
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise PrivacyError("storage_unavailable")
            return info
        finally:
            os.close(fd)

    def _check_files(self):
        self._check_parent()
        db = self._safe_file(self.path.name)
        opened = os.fstat(self._file_fd)
        if (db.st_dev, db.st_ino) != (opened.st_dev, opened.st_ino):
            raise PrivacyError("storage_unavailable")
        journal = self._safe_file(self.path.name + "-journal", optional=True, maximum=MAX_DB_BYTES + 1048576)
        if journal is not None and (journal.st_dev, journal.st_ino) == (db.st_dev, db.st_ino):
            raise PrivacyError("storage_unavailable")
        # These sidecars are never part of the fixed DELETE-journal format.
        for suffix in ("-wal", "-shm"):
            if os.path.lexists(self.path.parent / (self.path.name + suffix)):
                raise PrivacyError("storage_unavailable")

    def _validate_schema(self):
        names = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names != set(_TABLES):
            raise PrivacyError("storage_unavailable")
        for name, columns in _TABLES.items():
            found = tuple(row[1] for row in self._conn.execute(f"PRAGMA table_info({name})"))
            if found != columns:
                raise PrivacyError("storage_unavailable")
        if self._conn.execute("SELECT version FROM meta").fetchall() != [(1,)]:
            raise PrivacyError("storage_unavailable")
        if self._conn.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
            raise PrivacyError("storage_unavailable")
        if self._conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise PrivacyError("storage_unavailable")

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2**53 - 601:
            raise PrivacyError("storage_unavailable")
        return int(value)

    def _count(self, table):
        # Only static internal table names call this method.
        return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _identifier(self, kind):
        if self._count("ids") >= self.max_ids:
            raise PrivacyError("capacity_exceeded")
        for _ in range(8):
            value = self.random_hex()
            if type(value) is not str or HEX_ID_RE.fullmatch(value) is None:
                raise PrivacyError("storage_unavailable")
            if self._conn.execute("SELECT 1 FROM ids WHERE id=?", (value,)).fetchone() is None:
                self._conn.execute("INSERT INTO ids VALUES(?,?)", (value, kind))
                return value
        raise PrivacyError("capacity_exceeded")

    def _audit(self, action, outcome, count, *, optional=False):
        if self._count("audit") >= self.max_audit_rows:
            if optional:
                return
            raise PrivacyError("capacity_exceeded")
        self._conn.execute("INSERT INTO audit(action,outcome,count) VALUES(?,?,?)", (action, outcome, count))

    def _operation(self, action, operation):
        with self._lock:
            if self._conn is None:
                raise PrivacyError("storage_unavailable")
            try:
                self._check_files()
                now = self._now()
                self._conn.execute("BEGIN IMMEDIATE")
                # Expiry cleanup is bounded and may commit even when the caller's
                # subsequent action is denied. Tombstones are never recycled.
                self._conn.execute("DELETE FROM contexts WHERE context_id IN "
                    "(SELECT context_id FROM contexts WHERE expires_at<=? ORDER BY expires_at,context_id LIMIT ?)",
                    (now, self.purge_limit))
                self._conn.execute("SAVEPOINT action")
                try:
                    if self._count("audit") >= self.max_audit_rows:
                        raise PrivacyError("capacity_exceeded")
                    result, count = operation(now)
                    self._audit(action, "success", count)
                except PrivacyError as exc:
                    self._conn.execute("ROLLBACK TO action")
                    self._conn.execute("RELEASE action")
                    outcome = "capacity" if exc.code == "capacity_exceeded" else "denied"
                    self._audit(action, outcome, 0, optional=True)
                    self._conn.execute("COMMIT")
                    self._check_files()
                    raise
                self._conn.execute("RELEASE action")
                self._conn.execute("COMMIT")
                self._check_files()
                return result
            except PrivacyError:
                try:
                    if self._conn.in_transaction:
                        self._conn.rollback()
                except sqlite3.Error as exc:
                    raise _finite_error(exc) from None
                raise
            except Exception as exc:
                try:
                    if self._conn.in_transaction:
                        self._conn.rollback()
                except sqlite3.Error:
                    pass
                raise _finite_error(exc) from None

    def redact(self, tenant, client, text) -> dict:
        def operation(now):
            validate_scope(tenant, client)
            validate_text(text, original=True)
            spans = detect(text)
            expanded = len(text.encode("utf-8")) + sum(40-len(text[s.start:s.end].encode("utf-8")) for s in spans)
            if (expanded > MAX_TEXT_BYTES or self._count("contexts") >= self.max_contexts
                    or self._count("mappings") + len(spans) > self.max_mappings):
                raise PrivacyError("capacity_exceeded")
            context_id = self._identifier("context")
            expires_at = now + self.ttl_seconds
            self._conn.execute("INSERT INTO contexts VALUES(?,?,?,?,?,?)",
                               (context_id, tenant, client, now, expires_at, len(spans)))
            parts, detections, position = [], [], 0
            for span in spans:
                token_id = self._identifier("token")
                token = f"[[PII:{token_id}]]"
                self._conn.execute("INSERT INTO mappings VALUES(?,?,?,?)",
                                   (token_id, context_id, span.kind, text[span.start:span.end]))
                parts.extend((text[position:span.start], token))
                detections.append({"kind": span.kind, "start": span.start, "end": span.end, "token": token})
                position = span.end
            parts.append(text[position:])
            return {"context_id": context_id, "redacted_text": "".join(parts),
                    "expires_at": expires_at, "detections": detections}, len(spans)
        return self._operation("redact", operation)

    def restore(self, tenant, client, context_id, text) -> dict:
        def operation(now):
            validate_scope(tenant, client)
            validate_id(context_id)
            validate_text(text)
            row = self._conn.execute("SELECT tenant,client,created_at,expires_at,mapping_count FROM contexts WHERE context_id=?",
                                     (context_id,)).fetchone()
            if row is None or row[:2] != (tenant, client):
                raise PrivacyError("context_unavailable")
            created, expires, expected = row[2:]
            if (type(created) is not int or type(expires) is not int or not 0 <= created < expires <= 2**53
                    or not 1 <= expires-created <= 600 or type(expected) is not int or not 0 <= expected <= self.max_mappings):
                raise PrivacyError("storage_unavailable")
            if not created <= now < expires:
                raise PrivacyError("context_unavailable")
            rows = self._conn.execute("SELECT token_id,kind,original FROM mappings WHERE context_id=? LIMIT ?",
                                      (context_id, self.max_mappings+1)).fetchall()
            if len(rows) != expected:
                raise PrivacyError("storage_unavailable")
            values = {}
            for token_id, kind, original in rows:
                if (type(token_id) is not str or HEX_ID_RE.fullmatch(token_id) is None or kind not in KINDS
                        or type(original) is not str or TOKEN_PREFIX in original):
                    raise PrivacyError("storage_unavailable")
                try:
                    validate_text(original)
                except PrivacyError:
                    raise PrivacyError("storage_unavailable") from None
                values[token_id] = original
            parts, position, count = [], 0, 0
            while True:
                start = text.find(TOKEN_PREFIX, position)
                if start < 0:
                    parts.append(text[position:])
                    break
                match = TOKEN_RE.match(text, start)
                if match is None or match[1] not in values:
                    raise PrivacyError("invalid_token")
                parts.extend((text[position:start], values[match[1]]))
                position, count = match.end(), count+1
            restored = "".join(parts)
            if len(restored.encode("utf-8")) > MAX_TEXT_BYTES:
                raise PrivacyError("capacity_exceeded")
            return {"restored_text": restored}, count
        return self._operation("restore", operation)

    def audit_events(self, *, limit=1000) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 65536:
            raise PrivacyError("invalid_input")
        with self._lock:
            if self._conn is None:
                raise PrivacyError("storage_unavailable")
            try:
                self._check_files()
                rows = self._conn.execute("SELECT sequence,action,outcome,count FROM audit ORDER BY sequence LIMIT ?",
                                          (limit+1,)).fetchall()
                if len(rows) > limit:
                    raise PrivacyError("capacity_exceeded")
                result = []
                for sequence, action, outcome, count in rows:
                    if (type(sequence) is not int or sequence < 1 or action not in ("redact", "restore")
                            or outcome not in ("success", "denied", "capacity")
                            or type(count) is not int or not 0 <= count <= MAX_TEXT_BYTES):
                        raise PrivacyError("storage_unavailable")
                    result.append({"sequence": sequence, "action": action, "outcome": outcome, "count": count})
                self._check_files()
                return result
            except PrivacyError:
                raise
            except Exception as exc:
                raise _finite_error(exc) from None

    def close(self):
        with getattr(self, "_lock", threading.RLock()):
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                finally:
                    self._conn = None
            for attr in ("_file_fd", "_dir_fd"):
                fd = getattr(self, attr, None)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    setattr(self, attr, None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
