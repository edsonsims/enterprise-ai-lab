"""Bounded authenticated text transformations; no request or bearer-token logging."""
from __future__ import annotations

import asyncio
import re
import threading
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..rag.auth import AuthenticationError, PRIVACY_AUDIENCE, local_origin, strict_json
from .contracts import PrivacyError

MAX_BODY = 65536
_NAME = re.compile(r"[a-z][a-z0-9:_-]{0,63}\Z")
_CONTEXT = re.compile(r"[0-9a-f]{32}\Z")
_MESSAGES = {
    "invalid_request": "The request does not match this operation.",
    "invalid_json": "The JSON body is malformed or ambiguous.",
    "invalid_token": "A valid token for this resource is required.",
    "forbidden": "This principal cannot perform this operation.",
    "body_too_large": "The body exceeds the fixed byte limit.",
    "request_timeout": "The body deadline expired.",
    "unsupported_media_type": "One application/json content type is required.",
    "invalid_input": "The text or configuration exceeds the supported contract.",
    "invalid_scope": "The context is unavailable to this principal.",
    "context_unavailable": "The context is missing, expired or unavailable.",
    "invalid_token_reference": "The response contains an unavailable placeholder.",
    "capacity_exceeded": "The bounded local vault has reached capacity.",
    "resource_busy": "The local resource is busy; retry explicitly.",
    "storage_unavailable": "The local vault cannot complete this operation.",
    "processor_unavailable": "The configured local processor did not complete safely.",
}


def failure(status: int, code: str):
    headers = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
    if status == 401:
        headers["www-authenticate"] = 'Bearer realm="privacy-proxy", error="invalid_token"'
    return JSONResponse({"error": {"code": code, "message": _MESSAGES[code]}},
                        status_code=status, headers=headers)


def success(value):
    return JSONResponse(value, headers={"cache-control": "no-store", "x-content-type-options": "nosniff"})


class JsonBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        if len(auth) > 1:
            return await failure(401, "invalid_token")(scope, receive, send)
        overrides = {b"x-tenant-id", b"x-roles", b"x-role", b"x-user-id", b"x-client-id"}
        if scope.get("query_string") or any(k.lower() in overrides for k, _ in headers):
            return await failure(400, "invalid_request")(scope, receive, send)
        if scope["method"] == "POST":
            kinds = [v.decode("latin1").split(";")[0].strip().lower()
                     for k, v in headers if k.lower() == b"content-type"]
            if kinds != ["application/json"]:
                return await failure(415, "unsupported_media_type")(scope, receive, send)
            raw = bytearray()
            deadline = time.monotonic() + 3
            while True:
                try:
                    msg = await asyncio.wait_for(receive(), timeout=max(.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    return await failure(408, "request_timeout")(scope, receive, send)
                if time.monotonic() > deadline:
                    return await failure(408, "request_timeout")(scope, receive, send)
                if msg["type"] == "http.disconnect":
                    return
                if msg["type"] != "http.request":
                    continue
                raw.extend(msg.get("body", b""))
                if len(raw) > MAX_BODY:
                    return await failure(413, "body_too_large")(scope, receive, send)
                if not msg.get("more_body"):
                    break
            try:
                value = strict_json(bytes(raw), max_depth=8)
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError):
                return await failure(400, "invalid_json")(scope, receive, send)
            scope["privacy_json"] = value
        return await self.app(scope, receive, send)


class LocalProcessor:
    """One operator-configured loopback target; no URLs, headers or credentials from callers."""
    def __init__(self, origin: str):
        self.origin = local_origin(origin)

    def process(self, text: str) -> str:
        deadline = time.monotonic() + 3
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
            with client.stream("POST", self.origin + "/process", json={"text": text},
                               headers={"accept": "application/json", "accept-encoding": "identity"}) as response:
                if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise ValueError()
                data = bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic() > deadline:
                        raise ValueError()
                    data.extend(chunk)
                    if len(data) > MAX_BODY:
                        raise ValueError()
        value = strict_json(bytes(data), max_depth=4)
        if (time.monotonic() > deadline or not isinstance(value, dict) or set(value) != {"text"}
                or not isinstance(value["text"], str) or len(value["text"].encode("utf-8")) > 32768):
            raise ValueError()
        return value["text"]


def create_app(verifier, vault, processor=None):
    if verifier.audience != PRIVACY_AUDIENCE:
        raise ValueError("A dedicated privacy verifier is required.")
    app = FastAPI(title="Local privacy boundary", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(JsonBoundary)
    admission = threading.Lock()

    def principal(request, tenant, operations):
        if not _NAME.fullmatch(tenant):
            raise PermissionError()
        raw = request.headers.get("authorization", "")
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_.-]{1,4096})", raw, re.IGNORECASE)
        if not match:
            raise AuthenticationError()
        identity, scopes = verifier.verify(match[1])
        if identity.tenant_id != tenant or any(
                "privacy:" + op not in scopes or ("redactor" if op == "redact" else "restorer") not in identity.roles
                for op in operations):
            raise PermissionError()
        return identity

    def execute(request, tenant, operation):
        operations = ("redact", "restore") if operation == "process" else (operation,)
        try:
            principal(request, tenant, operations)
        except AuthenticationError:
            return failure(401, "invalid_token")
        except PermissionError:
            return failure(403, "forbidden")
        body = request.scope.get("privacy_json", {})
        expected = {"context_id", "text"} if operation == "restore" else {"text"}
        if set(body) != expected or not isinstance(body.get("text"), str):
            return failure(400, "invalid_request")
        if operation == "restore" and (not isinstance(body["context_id"], str) or not _CONTEXT.fullmatch(body["context_id"])):
            return failure(400, "invalid_request")
        if not admission.acquire(blocking=False):
            return failure(503, "resource_busy")
        try:
            identity = principal(request, tenant, operations)
            if operation == "restore":
                result = vault.restore(identity.tenant_id, identity.client_id, body["context_id"], body["text"])
            else:
                if operation == "process" and processor is None:
                    return failure(502, "processor_unavailable")
                result = vault.redact(identity.tenant_id, identity.client_id, body["text"])
                if operation == "process":
                    try:
                        output = processor.process(result["redacted_text"])
                    except Exception:
                        return failure(502, "processor_unavailable")
                    # Recheck a short-lived credential after outbound work, before releasing plaintext.
                    identity = principal(request, tenant, operations)
                    restored = vault.restore(identity.tenant_id, identity.client_id, result["context_id"], output)
                    result = {**result, "processed_text": output, **restored}
            return success(result)
        except AuthenticationError:
            return failure(401, "invalid_token")
        except PermissionError:
            return failure(403, "forbidden")
        except PrivacyError as exc:
            code = exc.code
            status = {"invalid_input": 400, "invalid_scope": 404, "invalid_token": 400,
                      "context_unavailable": 404, "capacity_exceeded": 429, "busy": 503,
                      "storage_unavailable": 503}.get(code, 503)
            public = {"invalid_token": "invalid_token_reference", "busy": "resource_busy"}.get(code, code)
            return failure(status, public if public in _MESSAGES else "storage_unavailable")
        except Exception:
            return failure(503, "storage_unavailable")
        finally:
            admission.release()

    @app.get("/healthz")
    def health():
        return success({"status": "ok", "authentication": "verified-privacy-jwt", "storage": "private-local-sqlite"})

    @app.post("/v1/tenants/{tenant_id}/privacy/redactions")
    def redact(tenant_id: str, request: Request):
        return execute(request, tenant_id, "redact")

    @app.post("/v1/tenants/{tenant_id}/privacy/restorations")
    def restore(tenant_id: str, request: Request):
        return execute(request, tenant_id, "restore")

    @app.post("/v1/tenants/{tenant_id}/privacy/process")
    def process(tenant_id: str, request: Request):
        return execute(request, tenant_id, "process")

    return app
