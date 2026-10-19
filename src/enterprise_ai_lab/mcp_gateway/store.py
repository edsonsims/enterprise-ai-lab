"""Allowlisted read-only SQLite access to an authored synthetic legacy fixture."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock
import time
from types import MappingProxyType

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
WORK_ORDER_ID = re.compile(r"WO-[0-9]{4}\Z")
ASSET_ID = re.compile(r"AS-[0-9]{3}\Z")
STATUSES = ("open", "in_progress", "closed")
QUERY_SECONDS = 0.1
BUSY_MILLISECONDS = 50
MAX_DATABASE_BYTES = 5_000_000
MAX_ROWS = 1000
DATA = Path(__file__).parent / "data" / "work-orders-v1.json"


class GatewayError(ValueError):
    """A bounded public error without SQL, paths or supplied input values."""
    def __init__(self, code: str, safe_message: str):
        self.code, self.safe_message = code, safe_message
        super().__init__(safe_message)


def _name(value):
    return isinstance(value, str) and bool(NAME.fullmatch(value))


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: tuple[str, ...]

    def __post_init__(self):
        if (not _name(self.subject) or not _name(self.tenant_id)
                or not isinstance(self.roles, tuple) or not 1 <= len(self.roles) <= 8
                or not all(_name(role) for role in self.roles)
                or len(set(self.roles)) != len(self.roles)):
            raise ValueError("invalid trusted launcher principal")


PROFILES = MappingProxyType({
    name: Principal("local:" + name, tenant, roles)
    for name, tenant, roles in (
        ("alpha-reader", "alpha", ("reader",)),
        ("alpha-analyst", "alpha", ("reader", "analyst")),
        ("beta-reader", "beta", ("reader",)),
        ("beta-analyst", "beta", ("reader", "analyst")),
        ("alpha-observer", "alpha", ("observer",)),
    )
})


def principal_for_profile(name: str) -> Principal:
    if not isinstance(name, str) or name not in PROFILES:
        raise GatewayError("invalid_request", "Unknown trusted launcher profile.")
    return PROFILES[name]


SCHEMAS = {
    "fixture_metadata": "CREATE TABLE fixture_metadata (schema_version INTEGER NOT NULL, source_sha256 TEXT NOT NULL)",
    "work_orders": "CREATE TABLE work_orders (tenant_id TEXT NOT NULL, work_order_id TEXT NOT NULL, asset_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL, PRIMARY KEY(tenant_id, work_order_id))",
    "work_order_roles": "CREATE TABLE work_order_roles (tenant_id TEXT NOT NULL, work_order_id TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY(tenant_id, work_order_id, role), FOREIGN KEY(tenant_id,work_order_id) REFERENCES work_orders(tenant_id,work_order_id))",
    "asset_diagnostics": "CREATE TABLE asset_diagnostics (tenant_id TEXT NOT NULL, asset_id TEXT NOT NULL, code TEXT NOT NULL, observation TEXT NOT NULL, PRIMARY KEY(tenant_id, asset_id))",
    "diagnostic_roles": "CREATE TABLE diagnostic_roles (tenant_id TEXT NOT NULL, asset_id TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY(tenant_id, asset_id, role), FOREIGN KEY(tenant_id,asset_id) REFERENCES asset_diagnostics(tenant_id,asset_id))",
}
READ_COLUMNS = {
    "work_orders": {"tenant_id", "work_order_id", "asset_id", "title", "status", "summary"},
    "work_order_roles": {"tenant_id", "work_order_id", "role"},
    "asset_diagnostics": {"tenant_id", "asset_id", "code", "observation"},
    "diagnostic_roles": {"tenant_id", "asset_id", "role"},
}


def _text(value, limit):
    return (isinstance(value, str) and 1 <= len(value) <= limit and bool(value.strip())
            and not any(ord(char) < 32 and char not in "\n\t" for char in value))


def _validate_row(row, *, diagnostics=False):
    expected = {"tenant_id", "asset_id", "code", "observation"} if diagnostics else {
        "tenant_id", "work_order_id", "asset_id", "title", "status", "summary"}
    if (not isinstance(row, dict) or set(row) != expected or not _name(row["tenant_id"])
            or not isinstance(row["asset_id"], str) or not ASSET_ID.fullmatch(row["asset_id"])):
        raise ValueError("invalid fixture row")
    if diagnostics:
        valid = _name(row["code"]) and _text(row["observation"], 2000)
    else:
        valid = (isinstance(row["work_order_id"], str) and WORK_ORDER_ID.fullmatch(row["work_order_id"])
                 and _text(row["title"], 160) and row["status"] in STATUSES and _text(row["summary"], 2000))
    if not valid:
        raise ValueError("invalid bounded fixture content")
    json.dumps(row, ensure_ascii=False).encode("utf-8")


def fixture_source() -> tuple[dict, str]:
    raw = DATA.read_bytes()
    if len(raw) > 256_000:
        raise ValueError("fixture source exceeds byte bound")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate fixture key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "work_orders", "asset_diagnostics"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("invalid fixture source schema")
    for key in ("work_orders", "asset_diagnostics"):
        if not isinstance(value[key], list) or not 1 <= len(value[key]) <= MAX_ROWS:
            raise ValueError("invalid fixture row count")
        seen = set()
        for original in value[key]:
            if not isinstance(original, dict) or "allowed_roles" not in original:
                raise ValueError("fixture needs authored role grants")
            row = {k: v for k, v in original.items() if k != "allowed_roles"}
            _validate_row(row, diagnostics=key == "asset_diagnostics")
            identity = (row["tenant_id"], row.get("work_order_id", row["asset_id"]))
            roles = original["allowed_roles"]
            if (identity in seen or not isinstance(roles, list) or not 1 <= len(roles) <= 8
                    or not all(_name(role) for role in roles) or len(set(roles)) != len(roles)):
                raise ValueError("invalid fixture identity or grant")
            seen.add(identity)
    return value, hashlib.sha256(raw).hexdigest()


def initialize_fixture(database_path) -> dict:
    """Explicit no-replace initializer; serving never seeds this database."""
    value, source_sha256 = fixture_source()
    path = Path(database_path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    connection = None
    try:
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA foreign_keys=ON")
        for schema in SCHEMAS.values():
            connection.execute(schema)
        connection.execute("INSERT INTO fixture_metadata VALUES (?,?)", (1, source_sha256))
        for table, grants, identity in (("work_orders", "work_order_roles", "work_order_id"),
                                        ("asset_diagnostics", "diagnostic_roles", "asset_id")):
            fields = tuple(name for name in ("tenant_id", "work_order_id", "asset_id", "title", "status", "summary")
                           if name in READ_COLUMNS[table]) if table == "work_orders" else ("tenant_id", "asset_id", "code", "observation")
            for row in value[table]:
                connection.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in fields)})",
                                   tuple(row[name] for name in fields))
                connection.executemany(f"INSERT INTO {grants} VALUES (?,?,?)",
                                       [(row["tenant_id"], row[identity], role) for role in row["allowed_roles"]])
        connection.commit()
    except BaseException:
        if connection is not None:
            connection.close(); connection = None
        path.unlink(missing_ok=True)
        raise
    finally:
        if connection is not None:
            connection.close()
    return {"schema_version": 1, "fixture_sha256": source_sha256,
            "work_order_count": len(value["work_orders"]),
            "diagnostic_count": len(value["asset_diagnostics"])}


class ReadOnlyStore:
    def __init__(self, database_path):
        path = Path(database_path).absolute()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_DATABASE_BYTES:
            raise ValueError("existing bounded regular fixture database required")
        self.database_path = path.resolve(strict=True)
        self.lock, self._closed = RLock(), False
        self._connection = sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True,
                                           timeout=BUSY_MILLISECONDS / 1000, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA query_only=ON")
            self._connection.execute(f"PRAGMA busy_timeout={BUSY_MILLISECONDS}")
            self._connection.execute("PRAGMA trusted_schema=OFF")
            try:
                self._connection.enable_load_extension(False)
            except AttributeError:
                pass  # This SQLite build has no extension-loading API.
            self._validate_schema()
            self.sqlite_version = self._connection.execute("SELECT sqlite_version()").fetchone()[0]
            self._connection.set_authorizer(self._authorizer)
        except BaseException:
            self._connection.close()
            raise

    def _validate_schema(self):
        rows = self._connection.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        actual = {row["name"]: row["sql"] for row in rows if row["type"] == "table"}
        if (actual != SCHEMAS or any(row["type"] not in ("table", "index")
                                    or (row["type"] == "index" and not row["name"].startswith("sqlite_autoindex_")) for row in rows)):
            raise ValueError("fixture database schema differs from the allowlist")
        for table in SCHEMAS:
            count = self._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            bound = 1 if table == "fixture_metadata" else MAX_ROWS * 8 if table.endswith("roles") else MAX_ROWS
            if not 1 <= count <= bound:
                raise ValueError("fixture database exceeds row bounds")
        metadata = self._connection.execute("SELECT schema_version,source_sha256 FROM fixture_metadata").fetchall()
        _, expected_sha256 = fixture_source()
        if len(metadata) != 1 or tuple(metadata[0]) != (1, expected_sha256):
            raise ValueError("fixture provenance differs from the packaged source")
        self.fixture_sha256 = expected_sha256

    @staticmethod
    def _authorizer(action, first, second, database, _trigger):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if (action == sqlite3.SQLITE_READ and database == "main"
                and first in READ_COLUMNS and second in READ_COLUMNS[first]):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    @staticmethod
    def _principal(principal, *, analyst=False):
        if (not isinstance(principal, Principal)
                or not set(principal.roles).intersection(("reader", "analyst"))
                or (analyst and "analyst" not in principal.roles)):
            raise GatewayError("forbidden", "The trusted profile does not allow this operation.")

    def _query(self, sql, parameters):
        with self.lock:
            if self._closed:
                raise GatewayError("internal_error", "The local resource is unavailable.")
            deadline = time.monotonic() + QUERY_SECONDS
            self._connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            try:
                return [dict(row) for row in self._connection.execute(sql, parameters).fetchall()]
            except sqlite3.Error as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 255
                if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT):
                    raise GatewayError("resource_busy", "The local resource is busy; retry explicitly.") from None
                raise GatewayError("internal_error", "The local read could not be confirmed.") from None
            finally:
                self._connection.set_progress_handler(None, 0)

    @staticmethod
    def _id(value, pattern):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise GatewayError("invalid_request", "Expected a bounded resource identifier.")

    @staticmethod
    def _items(rows, principal, *, diagnostics=False):
        try:
            for row in rows:
                _validate_row(row, diagnostics=diagnostics)
                if row["tenant_id"] != principal.tenant_id:
                    raise ValueError("resource tenant differs")
            return rows
        except (ValueError, TypeError, UnicodeError):
            raise GatewayError("internal_error", "The local read failed its result validation.") from None

    def list_work_orders(self, principal, status=None, after_id=None, limit=3):
        self._principal(principal)
        if status is not None and (not isinstance(status, str) or status not in STATUSES):
            raise GatewayError("invalid_request", "Unsupported work-order status.")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise GatewayError("invalid_request", "The page limit must be between one and ten.")
        if after_id is not None:
            self.get_work_order(principal, after_id)  # Hidden IDs cannot become cursors.
        predicates = ["w.tenant_id=?", "EXISTS (SELECT 1 FROM work_order_roles AS r WHERE r.tenant_id=w.tenant_id AND r.work_order_id=w.work_order_id AND r.role IN (" + ",".join("?" for _ in principal.roles) + "))"]
        parameters = [principal.tenant_id, *principal.roles]
        if status is not None:
            predicates.append("w.status=?"); parameters.append(status)
        if after_id is not None:
            predicates.append("w.work_order_id>?"); parameters.append(after_id)
        parameters.append(limit + 1)
        rows = self._query("SELECT w.tenant_id,w.work_order_id,w.asset_id,w.title,w.status,w.summary FROM work_orders AS w WHERE " + " AND ".join(predicates) + " ORDER BY w.work_order_id LIMIT ?", parameters)
        self._items(rows, principal)
        return {"items": rows[:limit], "next_after": rows[limit - 1]["work_order_id"] if len(rows) > limit else None}

    def get_work_order(self, principal, work_order_id):
        self._principal(principal); self._id(work_order_id, WORK_ORDER_ID)
        sql = "SELECT w.tenant_id,w.work_order_id,w.asset_id,w.title,w.status,w.summary FROM work_orders AS w WHERE w.tenant_id=? AND w.work_order_id=? AND EXISTS (SELECT 1 FROM work_order_roles AS r WHERE r.tenant_id=w.tenant_id AND r.work_order_id=w.work_order_id AND r.role IN (" + ",".join("?" for _ in principal.roles) + ")) LIMIT 1"
        rows = self._query(sql, (principal.tenant_id, work_order_id, *principal.roles))
        if not rows:
            raise GatewayError("not_found", "No permitted resource exists for that identifier.")
        return {"item": self._items(rows, principal)[0]}

    def get_asset_diagnostics(self, principal, asset_id):
        self._principal(principal, analyst=True); self._id(asset_id, ASSET_ID)
        sql = "SELECT d.tenant_id,d.asset_id,d.code,d.observation FROM asset_diagnostics AS d WHERE d.tenant_id=? AND d.asset_id=? AND EXISTS (SELECT 1 FROM diagnostic_roles AS r WHERE r.tenant_id=d.tenant_id AND r.asset_id=d.asset_id AND r.role IN (" + ",".join("?" for _ in principal.roles) + ")) LIMIT 1"
        rows = self._query(sql, (principal.tenant_id, asset_id, *principal.roles))
        if not rows:
            raise GatewayError("not_found", "No permitted resource exists for that identifier.")
        return {"item": self._items(rows, principal, diagnostics=True)[0]}

    def close(self):
        with self.lock:
            if not self._closed:
                self._connection.close(); self._closed = True
