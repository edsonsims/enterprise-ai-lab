"""Bounded approval-only HTTP edge; document staging is a trusted local tool."""
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
from .contracts import MAX_REVISION
from .gateway import PublicationConflict, PublicationNotFound

ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$"
SHA256 = r"^[a-f0-9]{64}$"


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, Field(pattern=ID)]
    candidate_sha256: Annotated[str, Field(pattern=SHA256)]
    expected_revision: Annotated[int, Field(ge=0, le=MAX_REVISION)]
    expires_at: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")]


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: Annotated[str, Field(pattern=ID)]
    snapshot_sha256: Annotated[str, Field(pattern=SHA256)]
    decision: Literal["approve", "reject"]


def create_app(verifier: JwtVerifier, gateway) -> FastAPI:
    if verifier.audience != APPROVAL_AUDIENCE:
        raise ValueError("publication approval requires its dedicated audience")
    app = FastAPI(title="Enterprise AI Lab — approved RAG publication", version="0.5.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(JsonBoundary)
    inflight = 0

    @app.exception_handler(RequestValidationError)
    async def malformed(_request, _exception):
        return error(422, "invalid_request", "Only the bounded publication request fields are accepted.")

    @app.get("/healthz")
    def health():
        return JSONResponse({"status": "ok", "authentication": "verified-jwt",
                             "engine": "temporal-local", "action": "publish_rag_documents"},
                            headers={"cache-control": "no-store"})

    async def invoke(request, tenant, operation, role, callback):
        nonlocal inflight
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", tenant):
            return error(422, "invalid_request", "Malformed tenant path.")
        token = re.fullmatch(r"Bearer ([A-Za-z0-9_.-]{1,4096})",
                            request.headers.get("authorization", ""), re.IGNORECASE)
        if not token:
            return error(401, "invalid_token", "A bounded Bearer token is required.")
        try:
            principal, scopes = verifier.verify(token[1])
        except AuthenticationError:
            return error(401, "invalid_token", "Token invalid for the publication approval resource.")
        roles = (role,) if isinstance(role, str) else role
        if (principal.tenant_id != tenant or operation not in scopes
                or not set(roles).intersection(principal.roles)):
            return error(403, "forbidden", "This principal cannot perform that tenant's publication operation.")
        if inflight >= 16:
            return error(503, "publication_busy", "Local publication capacity is full; retry the same IDs explicitly.")
        inflight += 1
        try:
            result = await asyncio.wait_for(callback(principal), timeout=12)
            return JSONResponse(result, headers={"cache-control": "no-store"})
        except PublicationNotFound:
            return error(404, "not_found", "No publication request or candidate exists in this tenant scope.")
        except PublicationConflict:
            return error(409, "publication_conflict", "Request or decision conflicts, is expired, or has a stale snapshot.")
        except ValueError:
            return error(422, "invalid_request", "Invalid bounded publication request.")
        except Exception:
            return error(503, "publication_unavailable", "The workflow could not reply; read status or retry the same IDs.")
        finally:
            inflight -= 1

    @app.post("/v1/tenants/{tenant}/publications")
    async def submit(tenant: str, body: SubmitBody, request: Request):
        return await invoke(request, tenant, "approval:request", "requester",
            lambda p: gateway.submit(p, body.request_id, body.candidate_sha256,
                                     body.expected_revision, body.expires_at))

    @app.get("/v1/tenants/{tenant}/publications/{request_id}")
    async def status(tenant: str, request_id: str, request: Request):
        if not re.fullmatch(ID, request_id):
            return error(422, "invalid_request", "Malformed request ID.")
        return await invoke(request, tenant, "approval:read", ("requester", "approver"),
                            lambda p: gateway.status(p, request_id))

    @app.post("/v1/tenants/{tenant}/publications/{request_id}/decisions")
    async def decide(tenant: str, request_id: str, body: DecideBody, request: Request):
        if not re.fullmatch(ID, request_id):
            return error(422, "invalid_request", "Malformed request ID.")
        return await invoke(request, tenant, "approval:decide", "approver",
            lambda p: gateway.decide(p, request_id, body.command_id, body.snapshot_sha256, body.decision))

    return app
