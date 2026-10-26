"""Loopback configuration portal; signed identity stays behind opaque cookies."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
import re
import secrets
import time
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..rag.auth import (
    AuthenticationError,
    AuthorizationError,
    local_origin,
    strict_json,
)
from ..review.sessions import Sessions
from .auth import ADMIN_AUDIENCE, ADMIN_CLIENT
from .core import AdminError

COOKIE = "lab_admin_session"
BINDING = "lab_admin_login"
MAX_BODY = 4096
ERRORS = {
    "unauthenticated": (
        401,
        "Sign in again; this local session is missing or expired.",
    ),
    "forbidden": (
        403,
        "The signed role, scope or browser CSRF check forbids this operation.",
    ),
    "invalid_request": (
        400,
        "Only documented bounded inputs and the literal loopback origin are accepted.",
    ),
    "conflict": (
        409,
        "Refresh durable state; the revision or immutable command conflicts.",
    ),
    "busy": (
        503,
        "The local store is busy. Inspect state before explicitly retrying the same command.",
    ),
    "storage_unavailable": (
        503,
        "Durable state could not be confirmed. Inspect state before retrying the same command.",
    ),
    "capacity": (503, "The bounded local service has reached capacity."),
    "not_found": (404, "No documented resource exists."),
}


def error(code):
    status, message = ERRORS.get(code, ERRORS["storage_unavailable"])
    return JSONResponse(
        {
            "error": {
                "code": code if code in ERRORS else "storage_unavailable",
                "message": message,
            }
        },
        status_code=status,
    )


class PortalBoundary:
    def __init__(self, app, origin):
        self.app, self.origin = app, origin
        self.host = origin.removeprefix("http://").encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])

        def values(name):
            return [v for k, v in headers if k.lower() == name]

        if values(b"host") != [self.host]:
            return await error("invalid_request")(scope, receive, send)
        forbidden = (
            b"authorization",
            b"x-tenant-id",
            b"x-roles",
            b"x-role",
            b"x-user-id",
            b"x-client-id",
            b"x-actor-id",
            b"x-subject",
            b"x-tenant",
            b"forwarded",
            b"x-forwarded-host",
            b"x-forwarded-proto",
        )
        if (
            any(values(k) for k in forbidden)
            or len(values(b"origin")) > 1
            or len(values(b"x-csrf-token")) > 1
        ):
            return await error("invalid_request")(scope, receive, send)
        cookies = b";".join(values(b"cookie")).decode("latin1")
        if any(
            sum(piece.partition("=")[0].strip() == name for piece in cookies.split(";"))
            > 1
            for name in (COOKIE, BINDING)
        ):
            return await error("unauthenticated")(scope, receive, send)
        raw_query = scope.get("query_string", b"")
        if len(raw_query) > 4096:
            return await error("invalid_request")(scope, receive, send)
        if raw_query:
            try:
                query = parse_qs(
                    raw_query.decode("ascii"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=5,
                )
                allowed = (
                    {"code", "state", "iss", "error", "error_description"}
                    if scope["path"] == "/auth/callback"
                    else set()
                )
                if not set(query) <= allowed or any(
                    len(v) != 1 for v in query.values()
                ):
                    raise ValueError("ambiguous query")
            except (ValueError, UnicodeError):
                return await error("invalid_request")(scope, receive, send)
        if scope["method"] != "POST":
            return await self.app(scope, receive, send)
        if values(b"origin") != [self.origin.encode("ascii")]:
            return await error("forbidden")(scope, receive, send)
        types = [
            v.decode("latin1").split(";")[0].strip().lower()
            for v in values(b"content-type")
        ]
        if types != ["application/json"]:
            return await error("invalid_request")(scope, receive, send)
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
                raise ValueError("expected object")
        except (ValueError, UnicodeError, RecursionError, TimeoutError):
            return await error("invalid_request")(scope, receive, send)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    work_order_page_size: Annotated[int, Field(ge=1, le=10)]
    default_status: Literal["open", "in_progress", "closed"]


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")]
    expected_revision: Annotated[int, Field(ge=0, le=1000)]
    action: Literal["update"]
    config: Config


class Rollback(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")]
    expected_revision: Annotated[int, Field(ge=0, le=1000)]
    action: Literal["rollback"]
    target_revision: Annotated[int, Field(ge=0, le=1000)]


Command = Annotated[Update | Rollback, Field(discriminator="action")]


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def create_app(*, origin, verifier, service, sessions=None):
    origin = local_origin(origin)
    sessions = sessions or Sessions()
    app = FastAPI(
        title="Enterprise AI Lab local administration",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(PortalBoundary, origin=origin)
    inflight = 0

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "cache-control": "no-store",
                "x-content-type-options": "nosniff",
                "cross-origin-resource-policy": "same-origin",
                "referrer-policy": "no-referrer",
                "x-frame-options": "DENY",
                "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            }
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def malformed(_request, _exception):
        return error("invalid_request")

    def authenticated(request, *, csrf=False):
        session = sessions.get(request.cookies.get(COOKIE))
        principal, scopes = verifier.verify(session.token)
        token = request.headers.get("x-csrf-token", "")
        if csrf and (
            not token.isascii() or not secrets.compare_digest(token, session.csrf)
        ):
            raise PermissionError("csrf")
        return session, principal, scopes

    async def invoke(request, operation, *, csrf=False):
        nonlocal inflight
        try:
            session, principal, scopes = authenticated(request, csrf=csrf)
        except (AuthenticationError, ValueError):
            return error("unauthenticated")
        except (PermissionError, AuthorizationError):
            return error("forbidden")
        if inflight >= 8:
            return error("capacity")
        inflight += 1
        try:
            # Cancellation of a waiting HTTP caller is not transaction rollback.
            # A caller must inspect durable state and retry the same immutable ID.
            result = await asyncio.to_thread(operation, principal, scopes)
            return JSONResponse(result)
        except AdminError as exc:
            return error(exc.code)
        except (PermissionError, AuthorizationError):
            return error("forbidden")
        except Exception:
            return error("storage_unavailable")
        finally:
            inflight -= 1

    @app.get("/healthz")
    async def health():
        return {
            "status": "ok",
            "login": "oidc-code-pkce",
            "session": "server-side",
            "operations": "fictional-config-only",
        }

    @app.get("/")
    async def index():
        return Response(
            (Path(__file__).parent / "static/index.html").read_bytes(),
            media_type="text/html",
        )

    @app.get("/static/{filename}")
    async def static(filename):
        if filename not in ("app.js", "app.css"):
            return error("not_found")
        return Response(
            (Path(__file__).parent / "static" / filename).read_bytes(),
            media_type="text/javascript" if filename.endswith(".js") else "text/css",
        )

    @app.get("/login")
    async def login():
        try:
            state, binding, code_verifier, nonce = sessions.login()
        except RuntimeError:
            return error("capacity")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        target = (
            verifier.authorization_endpoint
            + "?"
            + urlencode(
                {
                    "client_id": ADMIN_CLIENT,
                    "redirect_uri": origin + "/auth/callback",
                    "response_type": "code",
                    "scope": "openid admin:read admin:configure admin:audit",
                    "resource": ADMIN_AUDIENCE,
                    "state": state,
                    "nonce": nonce,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "login",
                }
            )
        )
        response = RedirectResponse(target, status_code=302)
        response.set_cookie(
            BINDING,
            binding,
            max_age=180,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/auth/callback",
        )
        return response

    @app.get("/auth/callback")
    async def callback(request: Request):
        try:
            query = request.query_params
            if "error" in query or set(query) not in (
                {"code", "state", "iss"},
                {"code", "state"},
            ):
                raise ValueError("callback")
            if query.get("iss", verifier.issuer) != verifier.issuer:
                raise ValueError("issuer")
            pending = sessions.consume_login(
                query["state"], request.cookies.get(BINDING)
            )
            code = query["code"]
            if not re.fullmatch(r"[A-Za-z0-9_.~-]{1,1024}", code):
                raise ValueError("code")
            async with asyncio.timeout(6):
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, timeout=3
                ) as client:
                    async with client.stream(
                        "POST",
                        verifier.token_endpoint,
                        data={
                            "grant_type": "authorization_code",
                            "client_id": ADMIN_CLIENT,
                            "code": code,
                            "redirect_uri": origin + "/auth/callback",
                            "code_verifier": pending["verifier"],
                            "resource": ADMIN_AUDIENCE,
                        },
                    ) as result:
                        data = bytearray()
                        async for chunk in result.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 24_576:
                                raise ValueError("response bound")
                        if result.status_code != 200:
                            raise ValueError("exchange")
            value = strict_json(bytes(data))
            if (
                not isinstance(value, dict)
                or value.get("token_type", "").lower() != "bearer"
            ):
                raise ValueError("token type")
            principal, _scopes = verifier.verify(value["access_token"])
            verifier.verify_id_token(
                value["id_token"], pending["nonce"], principal.subject
            )
            sessions.remove(request.cookies.get(COOKIE))
            identifier = sessions.create(value["access_token"], principal.expires_at)
            response = RedirectResponse("/", status_code=302)
            response.set_cookie(
                COOKIE,
                identifier,
                max_age=max(1, principal.expires_at - int(time.time())),
                httponly=True,
                samesite="lax",
                secure=False,
                path="/",
            )
            response.delete_cookie(BINDING, path="/auth/callback")
            return response
        except Exception:
            return error("unauthenticated")

    @app.get("/api/session")
    async def session(request: Request):
        try:
            value, principal, scopes = authenticated(request)
            return {
                "authenticated": True,
                "user": {
                    "subject": principal.subject,
                    "tenant_id": principal.tenant_id,
                    "roles": list(principal.roles),
                },
                "csrf_token": value.csrf,
                "expires_at": value.expires_at,
                "can_configure": "config_admin" in principal.roles
                and "admin:configure" in scopes,
                "can_read": bool(
                    set(principal.roles) & {"config_admin", "config_auditor"}
                )
                and "admin:read" in scopes,
                "can_audit": bool(
                    set(principal.roles) & {"config_admin", "config_auditor"}
                )
                and "admin:audit" in scopes,
            }
        except (ValueError, AuthenticationError):
            return error("unauthenticated")

    @app.post("/logout")
    async def logout(body: Empty, request: Request):
        try:
            authenticated(request, csrf=True)
        except (ValueError, AuthenticationError):
            return error("unauthenticated")
        except (PermissionError, AuthorizationError):
            return error("forbidden")
        sessions.remove(request.cookies.get(COOKIE))
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/config")
    async def configuration(request: Request):
        return await invoke(request, service.snapshot)

    @app.get("/api/work-orders")
    async def work_orders(request: Request):
        return await invoke(request, service.work_orders)

    @app.get("/api/audit")
    async def audit(request: Request):
        return await invoke(request, service.audit)

    @app.post("/api/commands")
    async def command(body: Command, request: Request):
        return await invoke(
            request,
            lambda principal, scopes: service.apply(
                principal, scopes, body.model_dump()
            ),
            csrf=True,
        )

    return app
