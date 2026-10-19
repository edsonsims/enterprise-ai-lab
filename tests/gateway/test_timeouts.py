"""Real official SDK timeout/cancel observations against a locked SQLite source.

Cancellation requests stop waiting; they do not claim rollback or forced SQLite
interruption. The worker is bounded independently and may finish an audited read.
"""
from contextlib import asynccontextmanager
import hashlib
import os
import sqlite3
import sys
import time

import anyio
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT
import pytest

from enterprise_ai_lab.mcp_gateway.audit import GatewayAudit
from enterprise_ai_lab.mcp_gateway.integration import _server_pid
from enterprise_ai_lab.mcp_gateway.store import initialize_fixture


@asynccontextmanager
async def connected(database, audit):
    parameters = StdioServerParameters(command=sys.executable, args=[
        "-m", "enterprise_ai_lab.mcp_gateway", "serve", "--database", str(database),
        "--audit", str(audit), "--profile", "alpha-reader",
    ])
    pid = None
    try:
        with anyio.fail_after(20):
            async with Client(parameters, mode="auto", read_timeout_seconds=2, cache=None) as client:
                pid = _server_pid(database)
                assert client.protocol_version == "2026-07-28"
                await client.list_tools()
                ready = await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
                assert ready.is_error is False
                yield client, ready
    finally:
        # Official stdio transport has exited before this check, even on failure.
        if pid is not None:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)


def test_official_sdk_read_timeout_and_same_connection_recovery(tmp_path):
    database, audit = tmp_path / "source.sqlite", tmp_path / "audit.sqlite"
    initialize_fixture(database)
    source_hash = hashlib.sha256(database.read_bytes()).hexdigest()

    async def check():
        async with connected(database, audit) as (client, ready):
            lock = sqlite3.connect(database, timeout=.1)
            try:
                lock.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                with anyio.fail_after(2):
                    with pytest.raises(MCPError) as failure:
                        await client.call_tool("get_work_order", {"work_order_id": "WO-0001"},
                                               read_timeout_seconds=.005)
                assert failure.value.code == REQUEST_TIMEOUT
                assert time.monotonic() - started < 2
            finally:
                lock.rollback()
                lock.close()
            with anyio.fail_after(3):
                recovered = await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
            assert recovered.is_error is False
            assert recovered.structured_content == ready.structured_content

    anyio.run(check)
    events = GatewayAudit(audit).events()
    assert events[-1]["phase"] == "outcome" and events[-1]["outcome"] == "success"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == source_hash


def test_explicit_cancellation_after_durable_intent_and_same_connection_recovery(tmp_path):
    database, audit = tmp_path / "source.sqlite", tmp_path / "audit.sqlite"
    initialize_fixture(database)
    source_hash = hashlib.sha256(database.read_bytes()).hexdigest()

    async def check():
        async with connected(database, audit) as (client, ready):
            audit_log = GatewayAudit(audit)
            previous_sequence = audit_log.events()[-1]["sequence"]
            scopes, results = [], []
            entered, finished = anyio.Event(), anyio.Event()

            async def in_flight_read():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    entered.set()
                    results.append(await client.call_tool(
                        "get_work_order", {"work_order_id": "WO-0001"}, read_timeout_seconds=2,
                    ))
                finished.set()

            lock = sqlite3.connect(database, timeout=.1)
            try:
                lock.execute("BEGIN EXCLUSIVE")
                async with anyio.create_task_group() as group:
                    group.start_soon(in_flight_read)
                    await entered.wait()
                    # A genuine persisted new intent proves the remote handler
                    # began work. Do not confuse task creation with RPC start.
                    with anyio.fail_after(1):
                        while True:
                            events = audit_log.events()
                            pending = [event for event in events
                                       if event["sequence"] > previous_sequence and event["phase"] == "intent"]
                            if pending:
                                intent = pending[0]
                                outcomes = [event for event in events
                                            if event["call_id"] == intent["call_id"] and event["phase"] == "outcome"]
                                assert not outcomes and not finished.is_set(), (
                                    "The controlled read completed before an in-flight cancel could be measured"
                                )
                                break
                            await anyio.sleep(.001)
                    scopes[0].cancel()
                    with anyio.fail_after(1):
                        await finished.wait()
                    assert scopes[0].cancel_called and scopes[0].cancelled_caught
                    assert results == []
            finally:
                lock.rollback()
                lock.close()
            with anyio.fail_after(3):
                recovered = await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
            assert recovered.is_error is False
            assert recovered.structured_content == ready.structured_content
            # The cancelled read's worker may have produced an outcome despite
            # suppressing its wire response. The local chain must remain valid.
            events = audit_log.events()
            assert any(event["call_id"] == intent["call_id"] and event["phase"] == "outcome" for event in events)
            assert events[-1]["outcome"] == "success"

    anyio.run(check)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == source_hash
