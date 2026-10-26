"""Local fictional configuration, atomic receipts and tenant-filtered work orders.

Only two view settings can change. This store cannot execute a command, edit an
ACL, restart a process or repair another study's storage. The local filesystem
and operator remain trusted; the hash chain has no external administrator-proof
anchor. Config, mutation audit and exact replay receipt share one SQLite commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path

from ..mcp_gateway.store import STATUSES, GatewayError, Principal, ReadOnlyStore

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
ZERO_HASH = "0" * 64
MAX_CHANGES = 1000
MAX_DB_BYTES = 16 * 1024 * 1024
DEFAULT_CONFIG = {"work_order_page_size": 3, "default_status": "open"}
SCHEMA = {
    "meta": "CREATE TABLE meta (version INTEGER NOT NULL, source_sha256 TEXT NOT NULL, audit_count INTEGER NOT NULL, audit_tip TEXT NOT NULL)",
    "changes": "CREATE TABLE changes (sequence INTEGER PRIMARY KEY, tenant TEXT NOT NULL, revision INTEGER NOT NULL, subject TEXT NOT NULL, command_id TEXT NOT NULL, command_json TEXT NOT NULL, config_json TEXT NOT NULL, receipt_json TEXT NOT NULL, previous_sha TEXT NOT NULL, entry_sha TEXT NOT NULL, UNIQUE(tenant,revision), UNIQUE(tenant,command_id))",
}
MESSAGES = {
    "invalid_request": "Only the documented bounded configuration command is accepted.",
    "unauthenticated": "A current dedicated admin user identity is required.",
    "forbidden": "The signed role and granted scope do not permit this operation.",
    "conflict": "The command conflicts with an existing receipt or configuration revision.",
    "busy": "The local configuration store is busy; retry the same command explicitly.",
    "storage_unavailable": "The configuration and audit state could not be confirmed.",
    "capacity": "The finite configuration history is full; operator action is required.",
}


class AdminError(ValueError):
    def __init__(self, code):
        if code not in MESSAGES:
            raise ValueError("unsupported admin error")
        self.code, self.safe_message = code, MESSAGES[code]
        super().__init__(self.safe_message)


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def configuration(value):
    if (
        type(value) is not dict
        or set(value) != set(DEFAULT_CONFIG)
        or type(value["work_order_page_size"]) is not int
        or not 1 <= value["work_order_page_size"] <= 10
        or type(value["default_status"]) is not str
        or value["default_status"] not in STATUSES
    ):
        raise AdminError("invalid_request")
    return dict(value)


def command(value):
    if type(value) is not dict or value.get("action") not in ("update", "rollback"):
        raise AdminError("invalid_request")
    expected = {
        "command_id",
        "expected_revision",
        "action",
        "config" if value["action"] == "update" else "target_revision",
    }
    if (
        set(value) != expected
        or type(value["command_id"]) is not str
        or not NAME.fullmatch(value["command_id"])
        or type(value["expected_revision"]) is not int
        or not 0 <= value["expected_revision"] <= MAX_CHANGES
    ):
        raise AdminError("invalid_request")
    result = {key: value[key] for key in ("command_id", "expected_revision", "action")}
    if value["action"] == "update":
        result["config"] = configuration(value["config"])
    else:
        if (
            type(value["target_revision"]) is not int
            or not 0 <= value["target_revision"] <= MAX_CHANGES
        ):
            raise AdminError("invalid_request")
        result["target_revision"] = value["target_revision"]
    return result


def _failure(exc):
    code = getattr(exc, "sqlite_errorcode", None)
    busy = type(code) is int and code & 255 in (5, 6)
    if code is None and type(exc) is sqlite3.OperationalError:
        # Python3.10 does not expose native sqlite_errorcode. Keep only these
        # exact legacy messages as a finite fallback; never reflect their text.
        busy = exc.args in (("database is locked",), ("database table is locked",))
    return AdminError("busy" if busy else "storage_unavailable")


class AdminStore:
    """One private local DB, fixed read-only source, bounded append-only history.

    fault_hook is a trusted in-process experiment seam. It is never selected by
    a browser request. A post-commit fault can lose the response, not the commit;
    callers must retry the exact immutable command ID/body after recovery.
    """

    def __init__(self, path, source_store, *, fault_hook=None, clock=time.time):
        if (
            not isinstance(source_store, ReadOnlyStore)
            or not callable(clock)
            or (fault_hook is not None and not callable(fault_hook))
        ):
            raise ValueError("validated read-only source and clock required")
        self.source_store, self.clock, self.fault_hook = source_store, clock, fault_hook
        self.path = Path(path).absolute()
        self.lock = threading.Lock()
        self._db = self._dir_fd = self._file_identity = None
        try:
            self._distinct_files()
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            self._dir_fd = os.open(
                self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            self._check_parent()
            try:
                new_fd = os.open(
                    self.path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=self._dir_fd,
                )
                created = True
            except FileExistsError:
                created = False
            else:
                # No SQLite handle can reference this newly created inode yet.
                # Existing databases are never opened by a manual descriptor:
                # closing one could drop another connection's POSIX locks.
                os.close(new_fd)
            current = self.path.lstat()
            self._file_identity = current.st_dev, current.st_ino
            self._check_files()
            self._db = sqlite3.connect(
                self.path, timeout=0.05, isolation_level=None, check_same_thread=False
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA busy_timeout=50")
            self._db.execute("PRAGMA trusted_schema=OFF")
            if created:
                self._db.execute("PRAGMA journal_mode=DELETE")
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    for sql in SCHEMA.values():
                        self._db.execute(sql)
                    self._db.execute(
                        "INSERT INTO meta VALUES(1,?,0,?)",
                        (self.source_store.fixture_sha256, ZERO_HASH),
                    )
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            if self._db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise AdminError("storage_unavailable")
            self._db.execute("PRAGMA max_page_count=4096")
            self._validate()
        except BaseException as exc:
            self.close()
            if isinstance(exc, (AdminError, KeyboardInterrupt, SystemExit)):
                raise
            raise _failure(exc) from None

    def _check_parent(self):
        info, current = os.fstat(self._dir_fd), self.path.parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise AdminError("storage_unavailable")

    def _distinct_files(self):
        def family(path):
            return [
                Path(str(path) + suffix) for suffix in ("", "-wal", "-shm", "-journal")
            ]

        source, target = family(self.source_store.database_path), family(self.path)
        if {x.resolve(strict=False) for x in source} & {
            x.resolve(strict=False) for x in target
        }:
            raise AdminError("storage_unavailable")

        def inodes(paths):
            result = set()
            for p in paths:
                try:
                    info = p.stat()
                except FileNotFoundError:
                    continue
                result.add((info.st_dev, info.st_ino))
            return result

        if inodes(source) & inodes(target):
            raise AdminError("storage_unavailable")

    def _check_files(self):
        self._check_parent()
        self._distinct_files()
        current = self.path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_size > MAX_DB_BYTES
            or self._file_identity != (current.st_dev, current.st_ino)
        ):
            raise AdminError("storage_unavailable")
        for suffix in ("-journal", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            try:
                side = p.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(side.st_mode)
                or side.st_uid != os.getuid()
                or side.st_nlink != 1
                or stat.S_IMODE(side.st_mode) != 0o600
                or side.st_size > MAX_DB_BYTES
                or suffix in ("-wal", "-shm")
            ):
                raise AdminError("storage_unavailable")

    def _authorize(self, principal, scopes, operation):
        from .auth import AdminPrincipal

        if (
            not isinstance(principal, AdminPrincipal)
            or principal.client_id != "admin-portal"
            or type(principal.expires_at) is not int
            or principal.expires_at <= self.clock()
        ):
            raise AdminError("unauthenticated")
        if (
            not isinstance(scopes, tuple)
            or not all(type(x) is str for x in scopes)
            or len(scopes) != len(set(scopes))
            or len(scopes) > 16
        ):
            raise AdminError("forbidden")
        role = (
            {"config_admin"}
            if operation == "configure"
            else {"config_admin", "config_auditor"}
        )
        if not role.intersection(principal.roles) or "admin:" + operation not in scopes:
            raise AdminError("forbidden")

    @staticmethod
    def _event(sequence, tenant, subject, body, config, revision, previous_sha):
        return {
            "sequence": sequence,
            "tenant_id": tenant,
            "subject": subject,
            "command_id": body["command_id"],
            "action": body["action"],
            "command_sha256": digest(
                {
                    "tenant_id": tenant,
                    "subject": subject,
                    "client_id": "admin-portal",
                    "command": body,
                }
            ),
            "revision": revision,
            "previous_revision": revision - 1,
            "configuration_sha256": digest(config),
            "previous_audit_hash": previous_sha,
        }

    @staticmethod
    def _receipt(event, entry_sha):
        return {
            key: event[key]
            for key in (
                "tenant_id",
                "command_id",
                "action",
                "revision",
                "previous_revision",
                "configuration_sha256",
            )
        } | {"audit_sequence": event["sequence"], "audit_hash": entry_sha}

    def _validate(self):
        self._check_files()
        schema = self._db.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        if {
            x["name"]: x["sql"] for x in schema if x["type"] == "table"
        } != SCHEMA or any(
            x["type"] not in ("table", "index")
            or (x["type"] == "index" and not x["name"].startswith("sqlite_autoindex_"))
            for x in schema
        ):
            raise AdminError("storage_unavailable")
        meta = self._db.execute("SELECT * FROM meta").fetchall()
        if (
            len(meta) != 1
            or type(meta[0]["version"]) is not int
            or tuple(meta[0])[:2] != (1, self.source_store.fixture_sha256)
            or type(meta[0]["audit_count"]) is not int
        ):
            raise AdminError("storage_unavailable")
        count = self._db.execute("SELECT count(*) FROM changes").fetchone()[0]
        if (
            type(count) is not int
            or not 0 <= count <= MAX_CHANGES
            or meta[0]["audit_count"] != count
        ):
            raise AdminError("storage_unavailable")
        rows = self._db.execute("SELECT * FROM changes ORDER BY sequence").fetchall()
        configs, receipts, events, previous = {}, {}, [], ZERO_HASH
        for index, row in enumerate(rows, 1):
            tenant, subject = row["tenant"], row["subject"]
            if (
                type(tenant) is not str
                or not NAME.fullmatch(tenant)
                or type(subject) is not str
                or not NAME.fullmatch(subject)
                or type(row["sequence"]) is not int
                or type(row["revision"]) is not int
                or row["sequence"] != index
            ):
                raise AdminError("storage_unavailable")
            try:
                body = command(json.loads(row["command_json"]))
                config = configuration(json.loads(row["config_json"]))
                receipt = json.loads(row["receipt_json"])
            except (ValueError, TypeError, RecursionError):
                raise AdminError("storage_unavailable") from None
            history = configs.setdefault(tenant, [dict(DEFAULT_CONFIG)])
            if body["expected_revision"] != len(history) - 1 or row["revision"] != len(
                history
            ):
                raise AdminError("storage_unavailable")
            target = body.get("target_revision")
            if body["action"] == "rollback" and not 0 <= target < len(history):
                raise AdminError("storage_unavailable")
            expected = body["config"] if body["action"] == "update" else history[target]
            event = self._event(
                index, tenant, subject, body, config, row["revision"], previous
            )
            entry_sha = digest(event)
            key = (tenant, body["command_id"])
            if (
                config != expected
                or row["command_id"] != body["command_id"]
                or key in receipts
                or row["previous_sha"] != previous
                or row["entry_sha"] != entry_sha
                or canonical(receipt) != canonical(self._receipt(event, entry_sha))
                or row["command_json"] != canonical(body)
                or row["config_json"] != canonical(config)
                or row["receipt_json"] != canonical(receipt)
            ):
                raise AdminError("storage_unavailable")
            history.append(config)
            receipts[key] = (event["command_sha256"], receipt)
            events.append(event | {"audit_hash": entry_sha})
            previous = entry_sha
        if meta[0]["audit_tip"] != previous:
            raise AdminError("storage_unavailable")
        return configs, receipts, events

    def _enter(self, principal, scopes, operation, *, write=False):
        self._authorize(principal, scopes, operation)
        if not self.lock.acquire(blocking=False):
            raise AdminError("busy")
        try:
            if self._db is None:
                raise AdminError("storage_unavailable")
            self._authorize(principal, scopes, operation)
            self._check_files()
            self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        except BaseException as exc:
            self.lock.release()
            if isinstance(exc, AdminError):
                raise
            raise _failure(exc) from None

    def _abort(self):
        if self._db is not None and self._db.in_transaction:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                raise AdminError("storage_unavailable") from None

    def _read(self, principal, scopes, operation):
        self._enter(principal, scopes, operation)
        try:
            data = self._validate()
            self._authorize(principal, scopes, operation)
            self._db.execute("COMMIT")
            return data
        except BaseException as exc:
            self._abort()
            if isinstance(exc, (AdminError, KeyboardInterrupt, SystemExit)):
                raise
            raise _failure(exc) from None
        finally:
            self.lock.release()

    def snapshot(self, principal, scopes):
        configs, _, _ = self._read(principal, scopes, "read")
        history = configs.get(principal.tenant_id, [dict(DEFAULT_CONFIG)])
        return {
            "tenant_id": principal.tenant_id,
            "revision": len(history) - 1,
            "config": dict(history[-1]),
        }

    def work_orders(self, principal, scopes):
        snapshot = self.snapshot(principal, scopes)
        config = snapshot["config"]
        identity = Principal(principal.subject, principal.tenant_id, principal.roles)
        try:
            self._authorize(principal, scopes, "read")
            data = self.source_store.list_work_orders(
                identity,
                status=config["default_status"],
                limit=config["work_order_page_size"],
            )
            self._authorize(principal, scopes, "read")
            return {"configuration": snapshot, **data}
        except GatewayError as exc:
            raise AdminError(
                "forbidden" if exc.code == "forbidden" else "storage_unavailable"
            ) from None

    def audit(self, principal, scopes):
        _, _, events = self._read(principal, scopes, "audit")
        selected = [x for x in events if x["tenant_id"] == principal.tenant_id]
        return {
            "tenant_id": principal.tenant_id,
            "events": selected[-20:],
            "total": len(selected),
        }

    def apply(self, principal, scopes, value):
        # Validate without mutating the caller's object. Authorization precedes
        # replay lookup too: a receipt is never a way around current permission.
        self._authorize(principal, scopes, "configure")
        body = command(value)
        self._enter(principal, scopes, "configure", write=True)
        try:
            configs, receipts, events = self._validate()
            request_sha = digest(
                {
                    "tenant_id": principal.tenant_id,
                    "subject": principal.subject,
                    "client_id": "admin-portal",
                    "command": body,
                }
            )
            prior = receipts.get((principal.tenant_id, body["command_id"]))
            if prior is not None:
                if prior[0] != request_sha:
                    raise AdminError("conflict")
                self._authorize(principal, scopes, "configure")
                self._db.execute("COMMIT")
                return dict(prior[1])
            history = configs.get(principal.tenant_id, [dict(DEFAULT_CONFIG)])
            revision = len(history)
            if body["expected_revision"] != revision - 1:
                raise AdminError("conflict")
            if len(events) >= MAX_CHANGES:
                raise AdminError("capacity")
            if body["action"] == "rollback":
                if body["target_revision"] >= len(history):
                    raise AdminError("conflict")
                config = history[body["target_revision"]]
            else:
                config = body["config"]
            previous = events[-1]["audit_hash"] if events else ZERO_HASH
            event = self._event(
                len(events) + 1,
                principal.tenant_id,
                principal.subject,
                body,
                config,
                revision,
                previous,
            )
            entry_sha = digest(event)
            receipt = self._receipt(event, entry_sha)
            self._db.execute(
                "INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event["sequence"],
                    principal.tenant_id,
                    revision,
                    principal.subject,
                    body["command_id"],
                    canonical(body),
                    canonical(config),
                    canonical(receipt),
                    previous,
                    entry_sha,
                ),
            )
            self._db.execute(
                "UPDATE meta SET audit_count=?,audit_tip=?",
                (event["sequence"], entry_sha),
            )
            if self.fault_hook is not None:
                self.fault_hook("before_commit")
            self._authorize(principal, scopes, "configure")
            self._check_files()
            self._db.execute("COMMIT")
            if self.fault_hook is not None:
                self.fault_hook("after_commit")
            return receipt
        except BaseException as exc:
            self._abort()
            if isinstance(exc, (AdminError, KeyboardInterrupt, SystemExit)):
                raise
            raise _failure(exc) from None
        finally:
            self.lock.release()

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        for name in ("_dir_fd",):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
