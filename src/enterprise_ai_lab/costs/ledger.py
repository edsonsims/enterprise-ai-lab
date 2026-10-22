"""Tenant-scoped append-only local SQLite ledger and frozen reconciliation receipts."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from .contracts import (
    MAX_DATABASE,
    MAX_EVENTS,
    MAX_STATEMENTS,
    PROFILES,
    CostError,
    authorize,
    bounded,
    canonical,
    charge,
    digest,
    event,
    name,
    now,
    period,
    rate_card,
    statement,
    timestamp,
    totals,
)

TABLES = {
    "metadata": "CREATE TABLE metadata (schema_version INTEGER NOT NULL, rate_card_sha256 TEXT NOT NULL)",
    "usage": "CREATE TABLE usage (tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, ordinal INTEGER NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(tenant_id,event_id), UNIQUE(tenant_id,ordinal))",
    "statements": "CREATE TABLE statements (tenant_id TEXT NOT NULL, statement_id TEXT NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(tenant_id,statement_id))",
}
TRIGGERS = {
    f"{table}_no_{operation}": f"CREATE TRIGGER {table}_no_{operation} BEFORE {operation.upper()} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable ledger'); END"
    for table in TABLES
    for operation in ("update", "delete")
}
SCHEMA = {
    **{("table", key): value for key, value in TABLES.items()},
    **{("trigger", key): value for key, value in TRIGGERS.items()},
    ("index", "sqlite_autoindex_usage_1"): None,
    ("index", "sqlite_autoindex_usage_2"): None,
    ("index", "sqlite_autoindex_statements_1"): None,
}
HEX = re.compile(r"[0-9a-f]{64}\Z")
RECEIPT = re.compile(r"[0-9a-f]{32}\Z")


def _stored(function):
    @wraps(function)
    def validate(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CostError:
            raise CostError("storage_corrupt") from None

    return validate


def _admit(path):
    members = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        member = Path(str(path) + suffix)
        if not (member.exists() or member.is_symlink()):
            if not suffix:
                raise CostError("storage_unavailable")
            continue
        try:
            info = member.lstat()
        except FileNotFoundError:
            if suffix:  # A concurrent SQLite commit may naturally unlink its journal.
                continue
            raise CostError("storage_unavailable") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_DATABASE
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise CostError("storage_unavailable")
        members.append((info.st_dev, info.st_ino))
    if len(set(members)) != len(members):
        raise CostError("storage_unavailable")


def _same_file(path, reserved):
    _admit(path)
    current = path.lstat()
    if (reserved.st_dev, reserved.st_ino) != (current.st_dev, current.st_ino):
        raise CostError("storage_unavailable")


def initialize(path, card=None):
    _, card_hash = rate_card(card)
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Refuse sidecars before reserving any path or creating a schema.
    if any(
        Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
        for suffix in ("", "-wal", "-shm", "-journal")
    ):
        raise CostError("storage_unavailable")
    connection, fd = None, None
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        reserved = os.fstat(fd)
        # Raw closes cancel process-wide POSIX locks; finish reservation before SQL.
        os.close(fd)
        fd = None
        connection = sqlite3.connect(path)
        _same_file(path, reserved)
        if connection.execute("SELECT name FROM sqlite_master").fetchall():
            raise CostError("storage_unavailable")
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA max_page_count=8192")
        connection.execute("BEGIN IMMEDIATE")
        for sql in (*TABLES.values(), *TRIGGERS.values()):
            connection.execute(sql)
        connection.execute("INSERT INTO metadata VALUES (?,?)", (1, card_hash))
        _same_file(path, reserved)
        connection.commit()
    except Exception:  # noqa: BLE001 - refuse unexpected storage failures without raw details
        # Preserve incomplete state for inspection; opening it must fail closed.
        raise CostError("storage_unavailable") from None
    finally:
        if connection is not None:
            connection.close()
        if fd is not None:
            os.close(fd)


class CostLedger:
    def __init__(self, path, card=None):
        self.path = Path(path).absolute()
        self._card, self.card_hash = rate_card(card)
        with self._transaction():
            pass

    @contextmanager
    def _transaction(self, write=False):
        connection = None
        try:
            _admit(self.path)
            reserved = self.path.lstat()
            _same_file(self.path, reserved)
            if digest(self._card) != self.card_hash:
                raise CostError("storage_corrupt")
            uri = "file:" + quote(str(self.path), safe="/")
            probe = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=0.1)
            try:
                _same_file(self.path, reserved)
                actual = {
                    (kind, key): sql
                    for kind, key, sql in probe.execute(
                        "SELECT type,name,sql FROM sqlite_master"
                    )
                }
                if actual != SCHEMA or probe.execute(
                    "SELECT * FROM metadata"
                ).fetchall() != [(1, self.card_hash)]:
                    raise CostError("storage_corrupt")
            finally:
                probe.close()
            connection = sqlite3.connect(
                "file:" + quote(str(self.path), safe="/") + "?mode=rw",
                uri=True,
                timeout=0.1,
                isolation_level=None,
            )
            _same_file(self.path, reserved)
            actual = {
                (kind, key): sql
                for kind, key, sql in connection.execute(
                    "SELECT type,name,sql FROM sqlite_master"
                )
            }
            if actual != SCHEMA or connection.execute(
                "SELECT * FROM metadata"
            ).fetchall() != [(1, self.card_hash)]:
                raise CostError("storage_corrupt")
            # Only a validated, owned schema reaches persistent storage settings.
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA page_size").fetchone() != (4096,):
                raise CostError("storage_corrupt")
            if write and connection.execute(
                "PRAGMA max_page_count=8192"
            ).fetchone() != (8192,):
                raise CostError("storage_unavailable")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            _same_file(self.path, reserved)
            connection.commit()
        except CostError:
            if connection is not None:
                connection.rollback()
            raise
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.rollback()
            native = type(exc) is sqlite3.OperationalError
            number = getattr(exc, "sqlite_errorcode", None) if native else None
            busy = (type(number) is int and number & 255 in (5, 6)) or (
                native
                and number is None
                and exc.args in (("database is locked",), ("database table is locked",))
            )
            full = (type(number) is int and number & 255 == 13) or (
                native and number is None and exc.args == ("database or disk is full",)
            )
            code = (
                "resource_busy"
                if busy
                else "capacity_exceeded"
                if full
                else "storage_unavailable"
            )
            raise CostError(code) from None
        except Exception:  # noqa: BLE001 - refuse unexpected storage failures without raw details
            if connection is not None:
                connection.rollback()
            raise CostError("storage_corrupt") from None
        finally:
            if connection is not None:
                connection.close()

    def _decode(self, encoded, checksum):
        if not isinstance(encoded, str) or len(encoded.encode()) > 512 * 1024:
            raise CostError("storage_corrupt")
        # Stored receipt input is bounded separately from CLI input.
        value = json.loads(encoded)
        if (
            canonical(value) != encoded
            or not isinstance(checksum, str)
            or digest(value) != checksum
        ):
            raise CostError("storage_corrupt")
        return value

    def _base(self, value, tenant, role):
        if (
            value["tenant_id"] != tenant
            or value["rate_card_sha256"] != self.card_hash
            or not isinstance(value["receipt_id"], str)
            or not RECEIPT.fullmatch(value["receipt_id"])
            or not any(
                p.subject == value["first_actor"]
                and p.tenant_id == tenant
                and p.role == role
                for p in PROFILES.values()
            )
        ):
            raise CostError("storage_corrupt")
        timestamp(value["created_at"])

    @_stored
    def _rows(self, connection, tenant):
        rows = connection.execute(
            "SELECT event_id,ordinal,record,digest FROM usage WHERE tenant_id=? ORDER BY ordinal LIMIT ?",
            (tenant, MAX_EVENTS + 1),
        ).fetchall()
        if len(rows) > MAX_EVENTS:
            raise CostError("storage_corrupt")
        result = []
        for key, ordinal, encoded, checksum in rows:
            value = self._decode(encoded, checksum)
            if set(value) != {
                "receipt_id",
                "tenant_id",
                "first_actor",
                "created_at",
                "event",
                "event_sha256",
                "rate_card_sha256",
                "ordinal",
                "charge",
            }:
                raise CostError("storage_corrupt")
            self._base(value, tenant, "operator")
            original = event(value["event"])
            expected = digest(
                {
                    "tenant_id": tenant,
                    "event": original,
                    "rate_card_sha256": self.card_hash,
                }
            )
            if (
                ordinal != len(result) + 1
                or type(value["ordinal"]) is not int
                or value["ordinal"] != ordinal
                or original["event_id"] != key
                or value["event_sha256"] != expected
                or original != value["event"]
                or value["charge"] != charge(original, self._card)
            ):
                raise CostError("storage_corrupt")
            result.append(value)
        return result

    def _snapshot(self, rows, start, end, watermark):
        return [
            row
            for row in rows
            if row["ordinal"] <= watermark
            and start <= row["event"]["occurred_at"] < end
        ]

    def _reconcile(self, rows, value):
        expected = {row["event"]["event_id"]: row["charge"] for row in rows}
        declared = {row["event_id"]: row["amount"] for row in value["lines"]}
        currency = value["currency"] != self._card["currency"]
        result = {
            "currency": self._card["currency"],
            "missing_events": sorted(expected.keys() - declared.keys()),
            "extra_events": sorted(declared.keys() - expected.keys()),
            "amount_mismatches": (
                []
                if currency
                else [
                    {
                        "event_id": key,
                        "expected": expected[key],
                        "declared": declared[key],
                    }
                    for key in sorted(expected.keys() & declared.keys())
                    if expected[key] != declared[key]
                ]
            ),
            "currency_mismatch": currency,
            **totals(expected.values()),
        }
        result["settled_total_mismatch"] = (
            None if currency else value["settled_total"] != result["settled_total"]
        )
        result["matched"] = not (
            result["missing_events"]
            or result["extra_events"]
            or result["amount_mismatches"]
            or currency
            or result["settled_total_mismatch"]
        )
        return result

    @_stored
    def _statements(self, connection, tenant, rows):
        stored = connection.execute(
            "SELECT statement_id,record,digest FROM statements WHERE tenant_id=? ORDER BY statement_id LIMIT ?",
            (tenant, MAX_STATEMENTS + 1),
        ).fetchall()
        if len(stored) > MAX_STATEMENTS:
            raise CostError("storage_corrupt")
        result = []
        for key, encoded, checksum in stored:
            value = self._decode(encoded, checksum)
            if set(value) != {
                "receipt_id",
                "tenant_id",
                "first_actor",
                "created_at",
                "statement",
                "statement_sha256",
                "rate_card_sha256",
                "watermark",
                "snapshot_sha256",
                "result",
            }:
                raise CostError("storage_corrupt")
            self._base(value, tenant, "auditor")
            original = statement(value["statement"])
            mark = value["watermark"]
            if type(mark) is not int or not 0 <= mark <= len(rows):
                raise CostError("storage_corrupt")
            selected = self._snapshot(rows, original["start"], original["end"], mark)
            if (
                original["statement_id"] != key
                or value["statement_sha256"] != digest(original)
                or original != value["statement"]
                or value["snapshot_sha256"] != digest(selected)
                or canonical(value["result"])
                != canonical(self._reconcile(selected, original))
            ):
                raise CostError("storage_corrupt")
            result.append(value)
        return result

    def append(self, principal, value):
        authorize(principal, ("operator",))
        value = event(value)
        identity = digest(
            {
                "tenant_id": principal.tenant_id,
                "event": value,
                "rate_card_sha256": self.card_hash,
            }
        )
        with self._transaction(True) as connection:
            rows = self._rows(connection, principal.tenant_id)
            self._statements(connection, principal.tenant_id, rows)
            previous = next(
                (row for row in rows if row["event"]["event_id"] == value["event_id"]),
                None,
            )
            if previous:
                if previous["event_sha256"] != identity:
                    raise CostError("idempotency_conflict")
                return bounded({**previous, "replayed": True})
            if len(rows) >= MAX_EVENTS:
                raise CostError("capacity_exceeded")
            result = {
                "receipt_id": uuid.uuid4().hex,
                "tenant_id": principal.tenant_id,
                "first_actor": principal.subject,
                "created_at": now(),
                "event": value,
                "event_sha256": identity,
                "rate_card_sha256": self.card_hash,
                "ordinal": len(rows) + 1,
                "charge": charge(value, self._card),
            }
            connection.execute(
                "INSERT INTO usage VALUES (?,?,?,?,?)",
                (
                    principal.tenant_id,
                    value["event_id"],
                    result["ordinal"],
                    canonical(result),
                    digest(result),
                ),
            )
            return bounded({**result, "replayed": False})

    def _read(self, principal):
        authorize(principal, ("operator", "auditor"))
        with self._transaction() as connection:
            rows = self._rows(connection, principal.tenant_id)
            self._statements(connection, principal.tenant_id, rows)
            return rows

    def get(self, principal, event_id):
        if not name(event_id):
            raise CostError("invalid_input")
        rows = self._read(principal)
        result = next(
            (row for row in rows if row["event"]["event_id"] == event_id), None
        )
        if result is None:
            raise CostError("not_found")
        return bounded({**result, "replayed": True})

    def list(self, principal, *, after=0, limit=100):
        if (
            type(after) is not int
            or not 0 <= after <= MAX_EVENTS
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise CostError("invalid_input")
        rows = [row for row in self._read(principal) if row["ordinal"] > after]
        selected = rows[:limit]
        return bounded(
            {
                "events": selected,
                "next_after": selected[-1]["ordinal"] if len(rows) > limit else None,
            }
        )

    def summary(self, principal, start, end):
        period(start, end)
        selected = self._snapshot(self._read(principal), start, end, MAX_EVENTS)
        return {
            "tenant_id": principal.tenant_id,
            "currency": self._card["currency"],
            "start": start,
            "end": end,
            "events": len(selected),
            **totals(row["charge"] for row in selected),
        }

    def reconcile(self, principal, value):
        authorize(principal, ("auditor",))
        value = statement(value)
        identity = digest(value)
        with self._transaction(True) as connection:
            rows = self._rows(connection, principal.tenant_id)
            receipts = self._statements(connection, principal.tenant_id, rows)
            previous = next(
                (
                    row
                    for row in receipts
                    if row["statement"]["statement_id"] == value["statement_id"]
                ),
                None,
            )
            if previous:
                if previous["statement_sha256"] != identity:
                    raise CostError("idempotency_conflict")
                return bounded({**previous, "replayed": True})
            if len(receipts) >= MAX_STATEMENTS:
                raise CostError("capacity_exceeded")
            selected = self._snapshot(rows, value["start"], value["end"], len(rows))
            result = {
                "receipt_id": uuid.uuid4().hex,
                "tenant_id": principal.tenant_id,
                "first_actor": principal.subject,
                "created_at": now(),
                "statement": value,
                "statement_sha256": identity,
                "rate_card_sha256": self.card_hash,
                "watermark": len(rows),
                "snapshot_sha256": digest(selected),
                "result": self._reconcile(selected, value),
            }
            bounded({**result, "replayed": False})
            connection.execute(
                "INSERT INTO statements VALUES (?,?,?,?)",
                (
                    principal.tenant_id,
                    value["statement_id"],
                    canonical(result),
                    digest(result),
                ),
            )
            return {**result, "replayed": False}
