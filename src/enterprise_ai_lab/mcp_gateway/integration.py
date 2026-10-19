"""Actual official-client/stdio/SQLite observations, with labelled local faults.

No model or remote system is called. All returned business records are authored
synthetic data. This demo does not establish authentication or production safety.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time

import anyio
from mcp import Client, StdioServerParameters

from .audit import GatewayAudit, TRIGGERS
from .store import ReadOnlyStore, initialize_fixture


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _server_pid(database: Path) -> int:
    """Observe a real direct child without using private SDK transport fields."""
    # procps may truncate a captured command column to terminal width. Explicit
    # unlimited width is necessary for Python/fixture paths on Linux runners.
    result = subprocess.run(["ps", "-ww", "-ax", "-o", "pid=,ppid=,args="],
                            capture_output=True, text=True, check=True, timeout=2)
    matches = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if (len(parts) == 3 and parts[1] == str(os.getpid())
                and "enterprise_ai_lab.mcp_gateway serve" in parts[2]
                and str(database) in parts[2]):
            matches.append(int(parts[0]))
    if len(matches) != 1:
        raise AssertionError("Expected exactly one real gateway child process")
    return matches[0]


async def run_demo(work_dir: Path) -> dict:
    """Run the complete offline story in a fresh local directory; fail on drift."""
    work_dir = Path(work_dir).absolute()
    work_dir.mkdir(parents=True, exist_ok=False)
    database, audit_path = work_dir / "fixture.sqlite", work_dir / "audit.sqlite"
    fixture = initialize_fixture(database)
    original_sha = _sha(database)
    observations, sessions, protocols, catalogs = [], [], set(), {}

    @asynccontextmanager
    async def connected(profile, *, audit=audit_path, mode="auto"):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "enterprise_ai_lab.mcp_gateway", "serve", "--database", str(database),
                  "--audit", str(audit), "--profile", profile],
        )
        pid = None
        started = time.monotonic()
        # Independent total session budget includes discovery, all operations and
        # context exit; individual calls also have their own total budgets below.
        with anyio.fail_after(20):
            async with Client(parameters, mode=mode, read_timeout_seconds=2, cache=None) as client:
                pid = _server_pid(database)
                negotiated_version = client.protocol_version
                protocols.add(negotiated_version)
                server_runtime = json.loads(client.instructions.rsplit("\nRuntime fingerprint: ", 1)[1])
                server_info = client.server_info.model_dump(mode="json", by_alias=True, exclude_none=True)
                assert set(server_runtime) == {"python", "sqlite", "mcp", "mcp_types"}
                assert server_runtime["mcp"] == "2.3.0" and server_runtime["sqlite"] == sqlite3.sqlite_version
                with anyio.fail_after(3):
                    tools = await client.list_tools()
                names = [tool.name for tool in tools.tools]
                expected = ([] if profile.endswith("observer") else
                            ["list_work_orders", "get_work_order"] +
                            (["get_asset_diagnostics"] if profile.endswith("analyst") else []))
                assert names == expected and tools.next_cursor is None
                catalogs[profile] = [tool.model_dump(mode="json", by_alias=True,
                                                     exclude_none=True) for tool in tools.tools]
                yield client
        assert pid is not None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("Official stdio context leaked an unreaped server process")
        sessions.append({"profile": profile, "mode": mode, "process_reaped": True,
                         "protocol_version": negotiated_version,
                         "server_info": server_info, "server_runtime": server_runtime,
                         "elapsed_seconds": round(time.monotonic() - started, 6)})

    async def call(client, case, tool, arguments, error=None, *, fault=False):
        started = time.monotonic()
        with anyio.fail_after(3):
            result = await client.call_tool(tool, arguments)
        text = [part.text for part in result.content if part.type == "text"]
        assert len(text) == 1
        decoded = json.loads(text[0])
        if error is None:
            assert result.is_error is not True and result.structured_content == decoded
            assert set(decoded) == {"data", "meta"}
        else:
            assert result.is_error is True and result.structured_content is None
            assert decoded["error"]["code"] == error
            assert set(decoded) == {"error"}
        observations.append({"case": case, "kind": "controlled_fault" if fault else "actual_stdio",
                             "tool": tool, "arguments": arguments, "is_error": bool(result.is_error),
                             "response": decoded,
                             "elapsed_seconds": round(time.monotonic() - started, 6)})
        return decoded

    async with connected("alpha-reader") as client:
        cursor, ids = None, []
        for page in range(1, 5):
            body = await call(client, f"reader_page_{page}", "list_work_orders",
                              {"limit": 2, "after_id": cursor})
            ids.extend(row["work_order_id"] for row in body["data"]["items"])
            assert all(row["tenant_id"] == "alpha" for row in body["data"]["items"])
            cursor = body["data"]["next_after"]
            if cursor is None:
                break
        assert cursor is None and ids == ["WO-0001", "WO-0003", "WO-0005", "WO-0006", "WO-0008", "WO-0009"]
        alpha = await call(client, "alpha_colliding_id", "get_work_order", {"work_order_id": "WO-0001"})
        hidden = await call(client, "hidden_row", "get_work_order", {"work_order_id": "WO-0002"}, "not_found")
        missing = await call(client, "missing_row", "get_work_order", {"work_order_id": "WO-9999"}, "not_found")
        assert hidden == missing
        await call(client, "hidden_cursor", "list_work_orders", {"after_id": "WO-0002"}, "not_found")
        await call(client, "reader_no_diagnostics", "get_asset_diagnostics", {"asset_id": "AS-001"}, "forbidden")
        filtered = await call(client, "status_filter", "list_work_orders", {"status": "open", "limit": 10})
        assert [row["work_order_id"] for row in filtered["data"]["items"]] == ["WO-0001"]
        for case, arguments in (
            ("tenant_override", {"tenant_id": "beta"}),
            ("role_override", {"roles": ["analyst"]}),
            ("sql_argument", {"sql": "SELECT * FROM work_orders"}),
            ("bool_limit", {"limit": True}),
            ("string_limit", {"limit": "2"}),
            ("oversized_limit", {"limit": 11}),
            ("invalid_status", {"status": "OPEN"}),
        ):
            await call(client, case, "list_work_orders", arguments, "invalid_request")
        await call(client, "identifier_injection", "get_work_order",
                   {"work_order_id": "WO-0001' OR 1=1 --"}, "invalid_request")
        await call(client, "no_sql_tool", "execute_sql", {"sql": "DELETE FROM work_orders"}, "invalid_request")
        # Real SQLite file lock, not a mocked query failure.
        blocker = sqlite3.connect(database, timeout=.1)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            await call(client, "source_locked", "get_work_order", {"work_order_id": "WO-0001"},
                       "resource_busy", fault=True)
        finally:
            blocker.rollback(); blocker.close()
        await call(client, "source_lock_recovered", "get_work_order", {"work_order_id": "WO-0001"})
        blocker = sqlite3.connect(audit_path, timeout=.1)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            await call(client, "audit_write_locked", "get_work_order", {"work_order_id": "WO-0001"},
                       "audit_unavailable", fault=True)
        finally:
            blocker.rollback(); blocker.close()
        await call(client, "audit_lock_recovered", "get_work_order", {"work_order_id": "WO-0001"})

    before_restart = GatewayAudit(audit_path).events()
    async with connected("alpha-reader") as client:
        repeated = await call(client, "fresh_process_same_read", "get_work_order", {"work_order_id": "WO-0001"})
        assert repeated == alpha
    after_restart = GatewayAudit(audit_path).events()
    assert after_restart[:len(before_restart)] == before_restart and len(after_restart) == len(before_restart) + 2

    async with connected("beta-reader") as client:
        beta = await call(client, "beta_colliding_id", "get_work_order", {"work_order_id": "WO-0001"})
        assert beta["data"]["item"]["tenant_id"] == "beta"
        assert "ALPHA_" not in json.dumps(beta) and beta["data"] != alpha["data"]
    for profile in ("alpha-analyst", "beta-analyst"):
        async with connected(profile) as client:
            tenant = profile.split("-")[0]
            restricted = await call(client, profile + "_restricted", "get_work_order", {"work_order_id": "WO-0002"})
            diagnostic = await call(client, profile + "_diagnostics", "get_asset_diagnostics", {"asset_id": "AS-001"})
            assert restricted["data"]["item"]["tenant_id"] == tenant
            assert diagnostic["data"]["item"]["tenant_id"] == tenant
    async with connected("alpha-observer") as client:
        await call(client, "observer_has_no_tools", "list_work_orders", {}, "forbidden")
    async with connected("alpha-reader", mode="legacy") as client:
        await call(client, "legacy_handshake", "get_work_order", {"work_order_id": "WO-0001"})

    # Direct engine defence-in-depth probe, not a public arbitrary-SQL operation.
    store = ReadOnlyStore(database)
    try:
        try:
            store._connection.execute("DELETE FROM work_orders")
        except sqlite3.DatabaseError:
            observations.append({"case": "engine_denies_write", "kind": "controlled_engine_probe",
                                 "write_denied": True})
        else:
            raise AssertionError("Read-only engine accepted a write")
    finally:
        store.close()

    # An administrator-level fault bypasses append-only triggers on a COPY only.
    # Recreate exact schema so this tests chain validation rather than schema drift.
    corrupt_path = work_dir / "corrupt-audit.sqlite"
    source, target = sqlite3.connect(audit_path), sqlite3.connect(corrupt_path)
    try:
        source.backup(target)
        target.execute("DROP TRIGGER gateway_audit_no_update")
        target.execute("UPDATE gateway_audit SET event_sha256=? WHERE sequence=1", ("f" * 64,))
        target.execute(TRIGGERS["gateway_audit_no_update"])
        target.commit()
    finally:
        source.close(); target.close()
    async with connected("alpha-reader", audit=corrupt_path) as client:
        await call(client, "corrupt_chain_fails_closed", "get_work_order", {"work_order_id": "WO-0001"},
                   "audit_unavailable", fault=True)

    audit = GatewayAudit(audit_path).events()
    # Bodies and caller arguments are absent even for rejected injection attempts.
    encoded_audit = json.dumps(audit)
    assert "ALPHA_" not in encoded_audit and "BETA_" not in encoded_audit
    assert "SELECT *" not in encoded_audit and "DELETE FROM" not in encoded_audit
    assert _sha(database) == original_sha
    assert protocols == {"2026-07-28", "2025-11-25"}
    return {
        "schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "study": "local-sqlite-mcp-gateway-v1", "data_kind": "authored_synthetic",
        "identity_kind": "trusted_local_launcher_profiles_not_authentication",
        "human_review_performed": False,
        "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                    "sqlite": sqlite3.sqlite_version, "mcp": version("mcp"),
                    "mcp_types": version("mcp-types")},
        "source_files": {path.name: _sha(path) for path in sorted(Path(__file__).parent.glob("*.py"))},
        "protocol_versions": sorted(protocols), "fixture": fixture,
        "source_database_sha256_before": original_sha, "source_database_sha256_after": _sha(database),
        "source_unchanged": True, "catalogs": catalogs, "sessions": sessions,
        "observations": observations,
        "audit": {"events": audit, "event_count": len(audit), "chain_verified": True,
                  "restart_preserved_prefix": True, "last_sha256": audit[-1]["event_sha256"]},
        "limits": ["Local stdio and trusted launcher, not remote authentication or production IAM",
                   "Synthetic SQLite, no Oracle/SAP, model or external customer data",
                   "Audit and source are separate databases, not a distributed transaction",
                   "Local hash chain has no external anchor against a host administrator",
                   "Read-only fixture pagination is not a mutable-database snapshot guarantee"],
    }
