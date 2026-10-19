"""Official-client adapter checks, distinct from the real stdio demo/integration.

These controlled services test wire-adapter preservation/sanitization. They do not
stand in for the SQLite/process evidence recorded by the integration harness.
"""

import copy
import json
from importlib.metadata import version as package_version
import platform
import sqlite3

from jsonschema import Draft202012Validator
from mcp import Client
from mcp.shared.exceptions import MCPError
import pytest

from enterprise_ai_lab.mcp_gateway.server import build_server
from enterprise_ai_lab.mcp_gateway.service import GatewayError, GatewayService
from enterprise_ai_lab.mcp_gateway.audit import GatewayAudit, canonical
from enterprise_ai_lab.mcp_gateway.store import ReadOnlyStore, initialize_fixture, principal_for_profile


class ControlledService:
    def __init__(self, allowed=("list_work_orders", "get_work_order"), response=None, error=None):
        self.allowed = list(allowed)
        self.received = []
        self.response = response
        self.error = error

    def allowed_tools(self):
        return self.allowed

    def call(self, name, arguments):
        self.received.append((name, copy.deepcopy(arguments)))
        if self.error is not None:
            raise self.error
        return self.response


def successful_order():
    return {
        "data": {"item": {
            "work_order_id": "WO-0001", "asset_id": "AS-001", "tenant_id": "alpha",
            "title": "Synthetic fixture", "status": "open", "summary": "Authored study data",
        }},
        "meta": {"operation": "get_work_order", "tenant_id": "alpha"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, version", [("auto", "2026-07-28"), ("legacy", "2025-11-25")])
@pytest.mark.parametrize("names", [
    [], ["list_work_orders", "get_work_order"],
    ["list_work_orders", "get_work_order", "get_asset_diagnostics"],
])
async def test_catalog_exact_schemas_and_read_only_annotations(names, mode, version):
    async with Client(build_server(ControlledService(names)), mode=mode) as client:
        result = await client.list_tools()
        assert [tool.name for tool in result.tools] == names
        assert result.next_cursor is None
        assert client.protocol_version == version
        assert client.server_info.name == "enterprise-legacy-fixture"
        assert client.server_info.version
        marker = "\nRuntime fingerprint: "
        assert client.instructions.count(marker) == 1
        fingerprint = json.loads(client.instructions.split(marker, 1)[1])
        assert fingerprint == {
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "mcp": package_version("mcp"), "mcp_types": package_version("mcp-types"),
        }
        for tool in result.tools:
            assert tool.input_schema["additionalProperties"] is False
            assert not ({"tenant_id", "roles", "sql", "database", "path"} & tool.input_schema["properties"].keys())
            Draft202012Validator.check_schema(tool.input_schema)
            Draft202012Validator.check_schema(tool.output_schema)
            assert tool.output_schema["additionalProperties"] is False
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
            assert "audit event" in tool.description


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"limit": True}, {"limit": "3"}, {"limit": 3.0},
    {"tenant_id": "beta"}, {"roles": ["analyst"]}, {"sql": "DROP TABLE work_orders"},
])
async def test_raw_arguments_reach_service_without_coercion_or_dropped_fields(arguments):
    service = ControlledService(error=GatewayError("invalid_request", "Invalid request"))
    async with Client(build_server(service)) as client:
        result = await client.call_tool("list_work_orders", arguments)
        assert result.is_error is True
        assert result.structured_content is None
        assert json.loads(result.content[0].text)["error"]["code"] == "invalid_request"
    assert service.received == [("list_work_orders", arguments)]
    received = service.received[0][1]
    for key in arguments:
        assert type(received[key]) is type(arguments[key])


@pytest.mark.asyncio
async def test_success_content_and_structured_output_match_and_validate():
    envelope = successful_order()
    service = ControlledService(response=envelope)
    async with Client(build_server(service)) as client:
        catalog = await client.list_tools()
        result = await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
        assert result.is_error is False
        assert result.structured_content == envelope
        assert json.loads(result.content[0].text) == envelope
        schema = next(tool.output_schema for tool in catalog.tools if tool.name == "get_work_order")
        Draft202012Validator(schema).validate(result.structured_content)
        wire = result.model_dump(by_alias=True, mode="json")
        assert "structuredContent" in wire and wire["isError"] is False
        assert wire["resultType"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_asset_diagnostics", "execute_sql"])
async def test_hidden_or_unknown_calls_are_still_refused_by_service(name):
    service = ControlledService(error=GatewayError("forbidden", "Operation is unavailable"))
    async with Client(build_server(service)) as client:
        result = await client.call_tool(name, {"asset_id": "AS-001"})
        assert result.is_error is True
        assert json.loads(result.content[0].text)["error"]["code"] == "forbidden"
    assert service.received == [(name, {"asset_id": "AS-001"})]


@pytest.mark.asyncio
async def test_unexpected_error_is_sanitized_before_result():
    secret = "/private/local-store.sqlite SQL hidden fixture text"
    service = ControlledService(error=RuntimeError(secret))
    async with Client(build_server(service)) as client:
        result = await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
        assert result.is_error is True
        assert result.structured_content is None
        assert secret not in result.content[0].text
        assert json.loads(result.content[0].text)["error"]["code"] == "internal_error"


@pytest.mark.asyncio
async def test_invalid_success_payload_is_not_returned():
    response = successful_order()
    response["data"]["item"]["unexpected"] = "private implementation detail"
    service = ControlledService(response=response)
    async with Client(build_server(service)) as client:
        result = await client.call_tool("get_work_order", {"work_order_id": "WO-0001"})
        assert result.is_error is True
        assert result.structured_content is None
        assert "private implementation detail" not in result.content[0].text


@pytest.mark.asyncio
async def test_empty_catalog_cursor_is_not_mistaken_for_no_cursor():
    async with Client(build_server(ControlledService())) as client:
        with pytest.raises(MCPError) as failure:
            await client.list_tools(cursor="")
        assert failure.value.code == -32602


def test_startup_refuses_unknown_or_duplicate_tool_catalog():
    for names in [["execute_sql"], ["get_work_order", "get_work_order"]]:
        with pytest.raises(ValueError, match="unsupported tool catalog"):
            build_server(ControlledService(names))


@pytest.mark.asyncio
async def test_real_sqlite_service_rejects_and_audits_unchanged_extra_arguments(tmp_path):
    import hashlib

    database = tmp_path / "source.sqlite"
    initialize_fixture(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    store = ReadOnlyStore(database)
    audit = GatewayAudit(tmp_path / "audit.sqlite")
    service = GatewayService(store, audit, principal_for_profile("alpha-reader"))
    arguments = {"limit": 3, "tenant_id": "beta", "roles": ["analyst"], "sql": "SELECT * FROM work_orders"}
    try:
        async with Client(build_server(service)) as client:
            rejected = await client.call_tool("list_work_orders", arguments)
            assert rejected.is_error is True
            assert rejected.structured_content is None
            assert json.loads(rejected.content[0].text)["error"]["code"] == "invalid_request"
            accepted = await client.call_tool("list_work_orders", {"limit": 2})
            assert accepted.is_error is False
            assert [row["work_order_id"] for row in accepted.structured_content["data"]["items"]] == ["WO-0001", "WO-0003"]
        events = audit.events()
        assert len(events) == 4
        assert events[0]["input_sha256"] == hashlib.sha256(canonical(
            {"tool": "list_work_orders", "arguments": arguments},
        ).encode("utf-8")).hexdigest()
        assert events[1]["outcome"] == "error"
        assert events[1]["error_code"] == "invalid_request"
        assert events[3]["outcome"] == "success"
        assert "SELECT *" not in canonical(events)
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    finally:
        store.close()


@pytest.mark.asyncio
async def test_advertised_nullable_filters_match_actual_service_defaults(tmp_path):
    database = tmp_path / "source.sqlite"
    initialize_fixture(database)
    store = ReadOnlyStore(database)
    service = GatewayService(store, GatewayAudit(tmp_path / "audit.sqlite"), principal_for_profile("alpha-reader"))
    try:
        async with Client(build_server(service)) as client:
            catalog = await client.list_tools()
            schema = next(tool.input_schema for tool in catalog.tools if tool.name == "list_work_orders")
            arguments = {"status": None, "after_id": None}
            Draft202012Validator(schema).validate(arguments)
            explicit = await client.call_tool("list_work_orders", arguments)
            omitted = await client.call_tool("list_work_orders", {})
            assert explicit.is_error is False
            assert omitted.is_error is False
            assert explicit.structured_content == omitted.structured_content
    finally:
        store.close()
