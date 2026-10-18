"""Loopback approval HTTP API. Only verified JWT claims establish authority."""
from __future__ import annotations

import asyncio
import re
from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..rag.api import JsonBoundary, error
from ..rag.auth import APPROVAL_AUDIENCE, AuthenticationError, JwtVerifier
from .gateway import ApprovalConflict, ApprovalNotFound

ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$"


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: Annotated[str, Field(min_length=1, max_length=160)]
    body: Annotated[str, Field(min_length=1, max_length=4000)]


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, Field(pattern=ID)]
    note: NoteBody
    expires_at: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: Annotated[str, Field(pattern=ID)]
    snapshot_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    decision: Literal["approve", "reject"]


def create_app(verifier: JwtVerifier, gateway) -> FastAPI:
    if verifier.audience != APPROVAL_AUDIENCE:
        raise ValueError("approval resource requires its dedicated audience")
    app = FastAPI(title="Enterprise AI Lab — durable local approval", version="0.4.0",
        docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(JsonBoundary)
    inflight = 0

    @app.exception_handler(RequestValidationError)
    async def malformed(_request, _exception):
        return error(422, "invalid_request", "Only the bounded approval request fields are accepted.")

    @app.get("/healthz")
    def health():
        return JSONResponse({"status": "ok", "authentication": "verified-jwt", "engine": "temporal-local"},
            headers={"cache-control": "no-store"})

    async def invoke(request, tenant, operation, role, callback):
        nonlocal inflight
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", tenant):
            return error(422, "invalid_request", "Malformed tenant path.")
        token = re.fullmatch(r"Bearer ([A-Za-z0-9_.-]{1,4096})", request.headers.get("authorization", ""), re.IGNORECASE)
        if not token:
            return error(401, "invalid_token", "A bounded Bearer token is required.")
        try:
            principal, scopes = verifier.verify(token[1])
        except AuthenticationError:
            return error(401, "invalid_token", "Token invalid for the approval resource.")
        roles = (role,) if isinstance(role, str) else role
        if principal.tenant_id != tenant or operation not in scopes or not set(roles).intersection(principal.roles):
            return error(403, "forbidden", "This principal cannot perform that tenant's approval operation.")
        if inflight >= 16:
            return error(503, "approval_busy", "Local approval capacity is full; retry the same IDs explicitly.")
        inflight += 1
        try:
            result = await asyncio.wait_for(callback(principal), timeout=12)
            return JSONResponse(result, headers={"cache-control": "no-store"})
        except ApprovalNotFound:
            return error(404, "not_found", "No approval request exists in this tenant scope.")
        except ApprovalConflict:
            return error(409, "approval_conflict", "Request or decision conflicts, is expired, or has a stale snapshot.")
        except ValueError:
            return error(422, "invalid_request", "Invalid bounded approval request.")
        except Exception:
            return error(503, "approval_unavailable", "The workflow could not reply; read status or retry the same IDs.")
        finally:
            inflight -= 1

    @app.post("/v1/tenants/{tenant}/approvals")
    async def submit(tenant: str, body: SubmitBody, request: Request):
        return await invoke(request, tenant, "approval:request", "requester",
            lambda p: gateway.submit(p, body.request_id, body.note.model_dump(), body.expires_at))

    @app.get("/v1/tenants/{tenant}/approvals/{request_id}")
    async def status(tenant: str, request_id: str, request: Request):
        if not re.fullmatch(ID, request_id):
            return error(422, "invalid_request", "Malformed request ID.")
        return await invoke(request, tenant, "approval:read", ("requester", "approver"),
            lambda p: gateway.status(p, request_id))

    @app.post("/v1/tenants/{tenant}/approvals/{request_id}/decisions")
    async def decide(tenant: str, request_id: str, body: DecideBody, request: Request):
        if not re.fullmatch(ID, request_id):
            return error(422, "invalid_request", "Malformed request ID.")
        return await invoke(request, tenant, "approval:decide", "approver",
            lambda p: gateway.decide(p, request_id, body.command_id, body.snapshot_sha256, body.decision))

    return app
