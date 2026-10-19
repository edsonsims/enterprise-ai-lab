"""Official MCP2 stdio adapter for the bounded, trusted-launcher SQLite study.

The low-level official Server API keeps arguments intact until GatewayService
validates and audits them. No decorator coercion may discard tenant/role/SQL
injection attempts. This module does not authenticate a remote caller.
"""

from __future__ import annotations

import copy
import json
import logging
from functools import partial
from importlib.metadata import version
import platform
import sqlite3
from typing import Any

import anyio
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from enterprise_ai_lab import __version__

from .service import GatewayError, GatewayService

_LOG = logging.getLogger(__name__)
_ID = {"type": "string", "minLength": 1, "maxLength": 64,
       "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"}
_WORK_ORDER_ID = {"type": "string", "minLength": 7, "maxLength": 7,
                  "pattern": "^WO-[0-9]{4}$"}
_ASSET_ID = {"type": "string", "minLength": 6, "maxLength": 6,
             "pattern": "^AS-[0-9]{3}$"}
_STATUS = {"type": "string", "enum": ["open", "in_progress", "closed"]}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


_WORK_ORDER = _object({
    "work_order_id": _WORK_ORDER_ID,
    "asset_id": _ASSET_ID,
    "tenant_id": _ID,
    "title": {"type": "string", "minLength": 1, "maxLength": 160},
    "status": _STATUS,
    "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
}, ["work_order_id", "asset_id", "tenant_id", "title", "status", "summary"])
_DIAGNOSTIC = _object({
    "asset_id": _ASSET_ID,
    "tenant_id": _ID,
    "code": _ID,
    "observation": {"type": "string", "minLength": 1, "maxLength": 2000},
}, ["asset_id", "tenant_id", "code", "observation"])

_INPUTS = {
    "list_work_orders": _object({
        "status": {"anyOf": [_STATUS, {"type": "null"}]},
        "after_id": {"anyOf": [_WORK_ORDER_ID, {"type": "null"}]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
    }, []),
    "get_work_order": _object({"work_order_id": _WORK_ORDER_ID}, ["work_order_id"]),
    "get_asset_diagnostics": _object({"asset_id": _ASSET_ID}, ["asset_id"]),
}
_DATA = {
    "list_work_orders": _object({
        "items": {"type": "array", "items": _WORK_ORDER, "maxItems": 10},
        "next_after": {"anyOf": [_WORK_ORDER_ID, {"type": "null"}]},
    }, ["items", "next_after"]),
    "get_work_order": _object({"item": _WORK_ORDER}, ["item"]),
    "get_asset_diagnostics": _object({"item": _DIAGNOSTIC}, ["item"]),
}
_DESCRIPTIONS = {
    "list_work_orders": (
        "List visible synthetic work orders in ID order. Trusted launcher policy "
        "filters tenant and row roles before the bounded page. after_id is a "
        "visible row ID, not an opaque protocol cursor or authorization token."
    ),
    "get_work_order": (
        "Read one visible synthetic work order. Missing and unreadable IDs have "
        "the same not_found result. Tenant and roles come only from the launcher."
    ),
    "get_asset_diagnostics": (
        "Read synthetic diagnostics for an asset; requires the launcher's analyst "
        "role and the same tenant. No live enterprise system is connected."
    ),
}


def _output_schema(name: str) -> dict[str, Any]:
    return _object({
        "data": _DATA[name],
        "meta": _object({
            "operation": {"type": "string", "const": name},
            "tenant_id": _ID,
        }, ["operation", "tenant_id"]),
    }, ["data", "meta"])


def _error_result(code: str, message: str) -> CallToolResult:
    # An error is a tool result, never a success-shaped structured envelope.
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(
            {"error": {"code": code, "message": message}}, sort_keys=True,
        ))],
        is_error=True,
    )


def build_server(service: GatewayService) -> Server:
    """Build a genuine official SDK server with a fixed, least-privilege catalog.

    The service owns authorization, strict validation and durable audit. A source
    read is idempotent with respect to business data; every attempt appends an
    audit event, so repeated calls do not mean identical audit state.
    """
    allowed = tuple(service.allowed_tools())
    if len(allowed) != len(set(allowed)) or any(name not in _INPUTS for name in allowed):
        raise ValueError("Gateway service exposed an unsupported tool catalog")
    outputs = {name: _output_schema(name) for name in allowed}
    validators = {name: Draft202012Validator(schema) for name, schema in outputs.items()}
    for schema in [*(_INPUTS[name] for name in allowed), *outputs.values()]:
        Draft202012Validator.check_schema(schema)
    # Store queries have independent SQLite busy/progress deadlines. Waiting for
    # this worker on cancellation avoids abandoning live DB/audit work in a thread.
    limiter = anyio.CapacityLimiter(1)

    async def list_tools(_context: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
        if params is not None and params.cursor is not None:
            raise MCPError(code=-32602, message="The tool catalog has no continuation page")
        return ListToolsResult(tools=[
            Tool(
                name=name,
                description=_DESCRIPTIONS[name] + (
                    " Source business data is read-only; each attempt writes a local audit event."
                ),
                input_schema=copy.deepcopy(_INPUTS[name]),
                output_schema=copy.deepcopy(outputs[name]),
                annotations=ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            ) for name in allowed
        ])

    async def call_tool(_context: Any, params: CallToolRequestParams) -> CallToolResult:
        try:
            envelope = await anyio.to_thread.run_sync(
                partial(service.call, params.name, params.arguments if params.arguments is not None else {}),
                abandon_on_cancel=False,
                limiter=limiter,
            )
            # Low-level handlers supply their own output validation. Never return
            # partial data if an implementation error violates the advertised schema.
            validator = validators.get(params.name)
            if validator is None:
                return _error_result("internal_error", "The gateway could not complete the request")
            validator.validate(envelope)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(
                    envelope, ensure_ascii=False, sort_keys=True, allow_nan=False,
                ))],
                structured_content=envelope,
                is_error=False,
            )
        except GatewayError as exc:
            return _error_result(exc.code, exc.safe_message)
        except Exception:
            # Client-visible details must not contain DB paths, SQL or input
            # values. A trusted local operator may inspect private stderr logs.
            _LOG.exception("Gateway adapter failed")
            return _error_result("internal_error", "The gateway could not complete the request")

    def input_schema(name: str) -> dict[str, Any] | None:
        # This SDK hook supports protocol schema/header validation; service still
        # validates the raw arguments on every call, including hidden/unknown tools.
        return copy.deepcopy(_INPUTS[name]) if name in allowed else None

    runtime = json.dumps({
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "mcp": version("mcp"),
        "mcp_types": version("mcp-types"),
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return Server(
        "enterprise-legacy-fixture",
        version=__version__,
        instructions=(
            "Synthetic local work-order fixture. The trusted launcher assigns tenant and roles; "
            "this stdio process does not authenticate a remote user. No SQL, write, Oracle, SAP, "
            "model, or remote-IAM tool exists. All tool calls are locally audited."
            "\nRuntime fingerprint: " + runtime
        ),
        get_tool_input_schema=input_schema,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio_async(service: GatewayService) -> None:
    """Serve only official MCP JSON-RPC on stdout until stdin EOF/cancellation."""
    server = build_server(service)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio(service: GatewayService) -> None:
    """Synchronous CLI entry point; process lifecycle is owned by the launcher."""
    anyio.run(run_stdio_async, service)
