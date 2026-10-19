"""Actual official MCP stdio, separate processes, persistent SQLite and faults."""
from pathlib import Path
import sqlite3
import sys
import time

import anyio
from mcp import Client, StdioServerParameters
import pytest

from enterprise_ai_lab.mcp_gateway.audit import GatewayAudit
from enterprise_ai_lab.mcp_gateway.integration import _server_pid, run_demo
from enterprise_ai_lab.mcp_gateway.store import initialize_fixture


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    return anyio.run(run_demo, tmp_path_factory.mktemp("gateway") / "demo")


def by_case(report, case):
    return next(row for row in report["observations"] if row["case"] == case)


def test_actual_modern_and_legacy_protocols_and_reaped_processes(report):
    assert report["protocol_versions"] == ["2025-11-25", "2026-07-28"]
    assert len(report["sessions"]) == 8
    assert all(session["process_reaped"] for session in report["sessions"])


def test_prefilter_before_page_limit_across_real_wire(report):
    pages = [by_case(report, f"reader_page_{page}")["response"]["data"] for page in (1, 2, 3)]
    assert [len(page["items"]) for page in pages] == [2, 2, 2]
    assert [item["work_order_id"] for page in pages for item in page["items"]] == [
        "WO-0001", "WO-0003", "WO-0005", "WO-0006", "WO-0008", "WO-0009"]
    assert pages[-1]["next_after"] is None


def test_colliding_ids_keep_tenant_and_role_boundaries(report):
    alpha, beta = (by_case(report, case)["response"]["data"]["item"]
                   for case in ("alpha_colliding_id", "beta_colliding_id"))
    assert alpha["work_order_id"] == beta["work_order_id"]
    assert alpha["tenant_id"] == "alpha" and beta["tenant_id"] == "beta"
    assert alpha["summary"] != beta["summary"]
    assert by_case(report, "hidden_row")["response"] == by_case(report, "missing_row")["response"]
    assert by_case(report, "alpha-analyst_diagnostics")["response"]["data"]["item"]["tenant_id"] == "alpha"
    assert by_case(report, "beta-analyst_diagnostics")["response"]["data"]["item"]["tenant_id"] == "beta"


@pytest.mark.parametrize("case,code", [
    ("reader_no_diagnostics", "forbidden"), ("observer_has_no_tools", "forbidden"),
    ("hidden_cursor", "not_found"), ("tenant_override", "invalid_request"),
    ("role_override", "invalid_request"), ("sql_argument", "invalid_request"),
    ("bool_limit", "invalid_request"), ("string_limit", "invalid_request"),
    ("oversized_limit", "invalid_request"), ("invalid_status", "invalid_request"),
    ("identifier_injection", "invalid_request"), ("no_sql_tool", "invalid_request"),
    ("source_locked", "resource_busy"), ("audit_write_locked", "audit_unavailable"),
    ("corrupt_chain_fails_closed", "audit_unavailable"),
])
def test_real_tool_errors_never_return_success_content(report, case, code):
    observation = by_case(report, case)
    assert observation["is_error"] is True
    assert set(observation["response"]) == {"error"}
    assert observation["response"]["error"]["code"] == code


def test_audit_and_source_survive_process_restart(report):
    assert report["source_unchanged"]
    assert report["source_database_sha256_before"] == report["source_database_sha256_after"]
    assert report["audit"]["restart_preserved_prefix"]
    events = report["audit"]["events"]
    assert len({event["call_id"] for event in events}) == len(events) // 2
    assert all(events[index]["phase"] == "intent" and events[index + 1]["phase"] == "outcome"
               for index in range(0, len(events), 2))
    assert by_case(report, "fresh_process_same_read")["response"] == by_case(report, "alpha_colliding_id")["response"]


def test_total_caller_timeout_is_not_a_read_rollback_and_connection_recovers(tmp_path):
    """An actual locked SQLite read exceeds caller budget; no write is involved.

    The timeout says nothing about whether the audited source read already ran.
    Release the lock and explicitly retry; do not infer cancellation rolled back
    work or caused a durable transaction. Official transport owns process exit.
    """
    database, audit = tmp_path / "source.sqlite", tmp_path / "audit.sqlite"
    initialize_fixture(database)

    async def check():
        parameters = StdioServerParameters(command=sys.executable, args=[
            "-m", "enterprise_ai_lab.mcp_gateway", "serve", "--database", str(database),
            "--audit", str(audit), "--profile", "alpha-reader"])
        with anyio.fail_after(12):
            async with Client(parameters, mode="auto", read_timeout_seconds=1, cache=None) as client:
                pid = _server_pid(database)
                ready = await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
                assert not ready.is_error
                lock = sqlite3.connect(database, timeout=.1)
                try:
                    lock.execute("BEGIN EXCLUSIVE")
                    started = time.monotonic()
                    with pytest.raises(TimeoutError):
                        with anyio.fail_after(.005):
                            await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
                    assert time.monotonic() - started < 2
                finally:
                    lock.rollback(); lock.close()
                recovered = await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
                assert not recovered.is_error and recovered.structured_content == ready.structured_content
        import os
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    anyio.run(check)
    assert GatewayAudit(audit).events()  # Still a verified, honest local chain.
