"""Loopback browser review portal with server-side OAuth sessions and CSRF."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode, parse_qs

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated, Literal

from ..rag.auth import AuthenticationError, AuthorizationError, local_origin, strict_json
from ..publication.gateway import PublicationConflict
from .sessions import Sessions

COOKIE = "lab_review_session"
BINDING = "lab_review_login"
MAX_BODY = 16_384


def error(code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code={
        "unauthorized": 401, "forbidden": 403, "not_found": 404, "conflict": 409,
        "invalid_request": 422, "unavailable": 503}.get(code, 400))


class PortalBoundary:
    def __init__(self, app, origin):
        self.app, self.origin = app, origin
        self.host = origin.removeprefix("http://").encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        def values(name):
            return [v for k,v in headers if k.lower() == name]
        if values(b"host") != [self.host]:
            return await error("invalid_request", "Use the configured literal loopback origin.")(scope, receive, send)
        if (values(b"authorization") or any(values(k) for k in (b"x-tenant-id", b"x-roles", b"x-role", b"x-user-id",
                b"x-client-id", b"x-actor-id", b"x-subject", b"x-tenant", b"forwarded", b"x-forwarded-host", b"x-forwarded-proto"))
                or len(values(b"origin")) > 1 or len(values(b"x-csrf-token")) > 1):
            return await error("invalid_request", "Ambiguous identity or browser headers.")(scope, receive, send)
        cookies = b";".join(values(b"cookie")).decode("latin1")
        if any(sum(piece.strip().startswith(name + "=") for piece in cookies.split(";")) > 1
               for name in (COOKIE, BINDING)):
            return await error("unauthorized", "Ambiguous browser session.")(scope, receive, send)
        raw_query = scope.get("query_string", b"")
        if len(raw_query) > 8192:
            return await error("invalid_request", "Query exceeds its limit.")(scope, receive, send)
        if raw_query:
            try:
                query = parse_qs(raw_query.decode("ascii"), keep_blank_values=True, strict_parsing=True, max_num_fields=5)
                allowed = {"code", "state", "iss", "error", "error_description"} if scope["path"] == "/auth/callback" else {"variant"} if scope["path"].endswith(("/pdf", "/preview.png")) else set()
                if not set(query) <= allowed or any(len(v) != 1 for v in query.values()):
                    raise ValueError("ambiguous query")
            except (ValueError, UnicodeError):
                return await error("invalid_request", "Only the documented query fields are accepted.")(scope, receive, send)
        if scope["method"] != "POST":
            return await self.app(scope, receive, send)
        if values(b"origin") != [self.origin.encode()]:
            return await error("forbidden", "A same-origin browser request is required.")(scope, receive, send)
        if [v.decode("latin1").split(";")[0].strip().lower() for v in values(b"content-type")] != ["application/json"]:
            return await error("invalid_request", "One JSON content type is required.")(scope, receive, send)
        data = bytearray()
        try:
            async with asyncio.timeout(3):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        continue
                    data.extend(message.get("body", b""))
                    if len(data) > MAX_BODY:
                        raise ValueError("body exceeds limit")
                    if not message.get("more_body"):
                        break
            if not isinstance(strict_json(bytes(data)), dict):
                raise ValueError("expected JSON object")
        except (ValueError, UnicodeError, RecursionError, TimeoutError):
            return await error("invalid_request", "Malformed, ambiguous or unbounded JSON body.")(scope, receive, send)
        consumed = False
        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()
        return await self.app(scope, replay, send)


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")]
    candidate_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    expected_revision: Annotated[int, Field(ge=0, le=(1 << 63)-2)]
    expires_at: Annotated[str, Field(max_length=20)]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")]
    snapshot_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    decision: Literal["approve", "reject"]


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def create_app(*, origin, verifier, service, assets, sessions=None):
    origin = local_origin(origin)
    sessions = sessions or Sessions()
    app = FastAPI(title="Enterprise AI Lab — human review", version="0.6.0",
        docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(PortalBoundary, origin=origin)
    inflight = 0

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"cache-control": "no-store", "x-content-type-options": "nosniff",
            "cross-origin-resource-policy": "same-origin",
            "referrer-policy": "no-referrer", "x-frame-options": "SAMEORIGIN",
            "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"})
        return response

    @app.exception_handler(RequestValidationError)
    async def malformed(_request, _exception):
        return error("invalid_request", "Only the documented strict fields are accepted.")

    def authenticated(request, *, csrf=False):
        session = sessions.get(request.cookies.get(COOKIE))
        principal, scopes = verifier.verify(session.token)
        supplied_csrf = request.headers.get("x-csrf-token", "")
        if csrf and (not supplied_csrf.isascii() or not secrets.compare_digest(supplied_csrf, session.csrf)):
            raise PermissionError("CSRF token required")
        return session, principal, scopes

    def log(principal, action, result, **kwargs):
        service.audit.log(tenant_id=principal.tenant_id, actor_id=principal.subject,
            identity_kind="user", action=action, result=result, **kwargs)

    async def invoke(request, operation, *, csrf=False):
        nonlocal inflight
        try:
            session, principal, scopes = authenticated(request, csrf=csrf)
        except (AuthenticationError, ValueError):
            return error("unauthorized", "Sign in again; this local session is missing or expired.")
        except (PermissionError, AuthorizationError):
            return error("forbidden", "The browser CSRF check failed.")
        if inflight >= 16:
            return error("unavailable", "The local review service is busy; retry explicitly.")
        inflight += 1
        try:
            async with asyncio.timeout(12):
                return await operation(principal, scopes)
        except (PermissionError, AuthorizationError):
            return error("forbidden", "Your tenant, role or document grant does not allow this operation.")
        except LookupError:
            return error("not_found", "No allowed review or document exists in this tenant.")
        except PublicationConflict:
            return error("conflict", "The immutable review or decision conflicts with durable state.")
        except ValueError:
            return error("invalid_request", "Invalid bounded review input.")
        except Exception:
            return error("unavailable", "Review state could not be confirmed; refresh before retrying the same command.")
        finally:
            inflight -= 1

    @app.get("/healthz")
    def health():
        return {"status": "ok", "login": "oidc-code-pkce", "session": "server-side"}

    @app.get("/")
    def index():
        return Response((Path(__file__).parent/"static/index.html").read_bytes(), media_type="text/html")

    @app.get("/static/{filename}")
    def static(filename):
        if filename not in ("app.js", "app.css"):
            return error("not_found", "Unknown static asset.")
        return Response((Path(__file__).parent/"static"/filename).read_bytes(),
            media_type="text/javascript" if filename.endswith(".js") else "text/css")

    @app.get("/login")
    def login():
        try:
            state, binding, code_verifier, nonce = sessions.login()
        except RuntimeError:
            return error("unavailable", "Local login capacity is full.")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
        target = verifier.authorization_endpoint + "?" + urlencode({"client_id": "review-portal",
            "redirect_uri": origin + "/auth/callback", "response_type": "code", "scope": "openid review:read review:request review:decide",
            "resource": "urn:identity-boundary:review", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "login"})
        response = RedirectResponse(target, status_code=302)
        response.set_cookie(BINDING, binding, max_age=180, httponly=True, samesite="lax", secure=False, path="/auth/callback")
        return response

    @app.get("/auth/callback")
    async def callback(request: Request):
        try:
            query = request.query_params
            if "error" in query or set(query) not in ({"code", "state", "iss"}, {"code", "state"}):
                raise ValueError("invalid OAuth callback")
            if query.get("iss", verifier.issuer) != verifier.issuer:
                raise ValueError("issuer mismatch")
            pending = sessions.consume_login(query["state"], request.cookies.get(BINDING))
            code = query["code"]
            if not re.fullmatch(r"[A-Za-z0-9_.~-]{1,1024}", code):
                raise ValueError("invalid code")
            async with asyncio.timeout(6):
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=3) as client:
                    async with client.stream("POST", verifier.token_endpoint, data={"grant_type": "authorization_code",
                            "client_id": "review-portal", "code": code, "redirect_uri": origin + "/auth/callback",
                            "code_verifier": pending["verifier"], "resource": "urn:identity-boundary:review"}) as token_response:
                        data = bytearray()
                        async for chunk in token_response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 24_576:
                                raise ValueError("unbounded token response")
                        if token_response.status_code != 200:
                            raise ValueError("code exchange failed")
            result = strict_json(bytes(data))
            if result.get("token_type", "").lower() != "bearer":
                raise ValueError("unsupported token type")
            principal, scopes = verifier.verify(result["access_token"])
            verifier.verify_id_token(result["id_token"], pending["nonce"], principal.subject)
            sessions.remove(request.cookies.get(COOKIE))
            identifier = sessions.create(result["access_token"], principal.expires_at)
            log(principal, "login", "authenticated")
            response = RedirectResponse("/", status_code=302)
            response.set_cookie(COOKIE, identifier, max_age=max(1, principal.expires_at-int(time.time())),
                httponly=True, samesite="lax", secure=False, path="/")
            response.delete_cookie(BINDING, path="/auth/callback")
            return response
        except Exception:
            # Never include a token, authorization code, password or IdP error.
            return error("unauthorized", "Login could not be completed. Start a new sign-in.")

    @app.get("/api/session")
    def session(request: Request):
        try:
            value, principal, _ = authenticated(request)
            return {"authenticated": True, "user": {"subject": principal.subject,
                "tenant_id": principal.tenant_id, "roles": list(principal.roles)},
                "csrf_token": value.csrf, "expires_at": value.expires_at}
        except (ValueError, AuthenticationError):
            return error("unauthorized", "Sign in to review documents.")

    @app.post("/logout")
    async def logout(body: Empty, request: Request):
        async def action(principal, scopes):
            log(principal, "logout", "session_removed")
            sessions.remove(request.cookies.get(COOKIE))
            response = JSONResponse({"logged_out": True})
            response.delete_cookie(COOKIE, path="/")
            return response
        return await invoke(request, action, csrf=True)

    @app.get("/api/reviews")
    async def reviews(request: Request):
        async def action(principal, scopes):
            value = await service.list_requests(principal, scopes)
            return JSONResponse(value if isinstance(value, dict) else {"reviews": value})
        return await invoke(request, action)

    def urls(detail):
        for document in detail.get("documents", []):
            base = f"/api/reviews/{detail['request']['request_id']}/documents/{document['document_id']}/pdf"
            document.update(pdf_url=base, original_pdf_url=base+"?variant=original")
            document.update(preview_url=base.removesuffix("pdf")+"preview.png",
                original_preview_url=base.removesuffix("pdf")+"preview.png?variant=original")
        return detail

    @app.post("/api/reviews")
    async def submit(body: Submit, request: Request):
        async def action(principal, scopes):
            return JSONResponse(urls(await service.create_request(principal, scopes, **body.model_dump())))
        return await invoke(request, action, csrf=True)

    @app.get("/api/reviews/{request_id}")
    async def detail(request_id: str, request: Request):
        async def action(principal, scopes):
            return JSONResponse(urls(await service.detail(principal, scopes, request_id)))
        return await invoke(request, action)

    @app.post("/api/reviews/{request_id}/decisions")
    async def decide(request_id: str, body: Decision, request: Request):
        async def action(principal, scopes):
            return JSONResponse(urls(await service.decide(principal, scopes, request_id, **body.model_dump())))
        return await invoke(request, action, csrf=True)

    @app.get("/api/reviews/{request_id}/documents/{document_id}/pdf")
    async def pdf(request_id: str, document_id: str, request: Request):
        async def action(principal, scopes):
            try:
                document = await service.get_document(principal, scopes, request_id, document_id)
            except (LookupError, AuthorizationError):
                log(principal, "pdf_view", "denied", reason_code="not_permitted")
                raise
            variant = request.query_params.get("variant", "artifact")
            data, metadata = await asyncio.to_thread(assets.preview, document, variant=variant)
            log(principal, "pdf_view", "allowed", request_id=request_id, document_id=document_id)
            return Response(data, media_type="application/pdf", headers={
                "content-disposition": 'inline; filename="authorized-page-preview.pdf"',
                "x-preview-sha256": metadata["preview_sha256"], "x-preview-page-count": "1",
                "x-original-sha256": metadata["original_sha256"], "x-artifact-sha256": metadata["artifact_sha256"]})
        return await invoke(request, action)

    @app.get("/api/audit")
    async def audit(request: Request):
        async def action(principal, scopes):
            return JSONResponse({"events": await service.audit_events(principal, scopes), "chain_valid": True})
        return await invoke(request, action)

    @app.get("/api/reviews/{request_id}/documents/{document_id}/preview.png")
    async def page_image(request_id: str, document_id: str, request: Request):
        async def action(principal, scopes):
            try:
                document = await service.get_document(principal, scopes, request_id, document_id)
            except (LookupError, AuthorizationError):
                log(principal, "page_preview", "denied", reason_code="not_permitted")
                raise
            data, metadata = await asyncio.to_thread(assets.image, document,
                variant=request.query_params.get("variant", "artifact"))
            log(principal, "page_preview", "allowed", request_id=request_id, document_id=document_id)
            return Response(data, media_type="image/png", headers={"x-preview-page-count": "1",
                "x-render-sha256": metadata["image_sha256"], "x-preview-sha256": metadata["preview_sha256"],
                "x-render-width": str(metadata["width_px"]), "x-render-height": str(metadata["height_px"])})
        return await invoke(request, action)
    return app
