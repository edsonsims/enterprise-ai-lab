"""Real SDK timeout, plus cancellation with controlled pre-query scheduling.

Cancellation requests stop waiting; they do not claim rollback or forced SQLite
interruption. The worker is bounded independently and may finish an audited read.
"""
from contextlib import asynccontextmanager, contextmanager
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


# This bootstrap exists only in the test, with no production flag or runtime
# limit change. It pauses the genuine service after its committed audit intent
# but before the genuine SQL call. Parent release makes cancellation observation
# independent of a busy runner happening to schedule us inside a 50ms window.
_PRE_QUERY_BARRIER = """
from pathlib import Path
import runpy
import sys
import time
from enterprise_ai_lab.mcp_gateway.store import ReadOnlyStore

gate = Path(sys.argv[1])
original_query = ReadOnlyStore._query
used = False
def controlled_query(self, sql, parameters):
    global used
    if not used and (gate / 'armed').exists():
        used = True
        (gate / 'entered').write_text('after committed intent, before real SQL')
        deadline = time.monotonic() + 5
        while not (gate / 'release').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('Controlled test barrier exceeded its bound')
            time.sleep(.001)
    return original_query(self, sql, parameters)
ReadOnlyStore._query = controlled_query
sys.argv = sys.argv[2:]
runpy.run_module('enterprise_ai_lab.mcp_gateway', run_name='__main__')
"""


@contextmanager
def audit_observer(audit):
    """Committed read-only snapshots, without writer-side audit initialization."""
    observer = sqlite3.connect(audit.as_uri() + "?mode=ro", uri=True,
                               isolation_level=None, timeout=.1)
    statements = []
    observer.set_trace_callback(statements.append)
    try:
        yield lambda: GatewayAudit._chain(observer), statements
    finally:
        observer.close()
        assert statements and all(sql.startswith("SELECT ") for sql in statements)


@asynccontextmanager
async def connected(database, audit, *, pre_query_gate=None):
    cli = ["enterprise_ai_lab.mcp_gateway", "serve", "--database", str(database),
        "--audit", str(audit), "--profile", "alpha-reader",
    ]
    # repr keeps bootstrap newlines escaped in argv, preserving the real child
    # PID observer on both procps/Linux and macOS. The actual CLI args follow.
    args = (["-m", *cli] if pre_query_gate is None else
            ["-c", "exec(" + repr(_PRE_QUERY_BARRIER) + ")", str(pre_query_gate), *cli])
    parameters = StdioServerParameters(command=sys.executable, args=args)
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
    gate = tmp_path / "controlled-pre-query"; gate.mkdir()
    initialize_fixture(database)
    source_hash = hashlib.sha256(database.read_bytes()).hexdigest()

    async def check():
        async with connected(database, audit, pre_query_gate=gate) as (client, ready):
            audit_log = GatewayAudit(audit)
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
                with audit_observer(audit) as (snapshot, _statements):
                    previous_sequence = snapshot()[-1]["sequence"]
                    (gate / "armed").touch()
                    async with anyio.create_task_group() as group:
                        group.start_soon(in_flight_read)
                        await entered.wait()
                        # A real committed new intent plus the child barrier
                        # proves RPC handling began. Source SQL is deliberately
                        # paused, not claimed interrupted by cancellation.
                        with anyio.fail_after(2):
                            while not (gate / "entered").exists():
                                await anyio.sleep(.001)
                            # Deliberately observe later than the ordinary 50ms
                            # SQLite busy window. Only this controlled test gate
                            # keeps the request pending; runner speed is irrelevant.
                            await anyio.sleep(.1)
                            events = snapshot()
                            pending = [event for event in events
                                       if event["sequence"] > previous_sequence and event["phase"] == "intent"]
                            assert len(pending) == 1
                            intent = pending[0]
                            outcomes = [event for event in events
                                        if event["call_id"] == intent["call_id"] and event["phase"] == "outcome"]
                            assert not outcomes and not finished.is_set()
                        scopes[0].cancel()
                        with anyio.fail_after(1):
                            await finished.wait()
                        assert scopes[0].cancel_called and scopes[0].cancelled_caught
                        assert results == []
            finally:
                # Always unblock the test child, including failed assertions or
                # parent cancellation. The child also has its own five-second cap.
                (gate / "release").touch()
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
