"""An authenticated RAG resource; identity comes only from a verified access token."""
from __future__ import annotations

import asyncio
import time
import re
import threading
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import AuthenticationError, AuthorizationError, JwtVerifier, authorize, strict_json
from .contracts import QueryLimitError

MAX_BODY = 16384


def error(status: int, code: str, message: str):
    headers = {'cache-control': 'no-store'}
    if status == 401:
        headers['www-authenticate'] = 'Bearer realm="enterprise-ai-lab", error="invalid_token"'
    return JSONResponse(status_code=status, content={'error': {'code': code, 'message': message}}, headers=headers)


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    top_k: Annotated[int, Field(ge=1, le=5)] = 3

    @field_validator('query')
    @classmethod
    def meaningful_query(cls, value):
        if not value.strip() or len(value.encode('utf-8')) > 6000:
            raise ValueError('A nonblank, bounded question is required.')
        return value


class JsonBoundary:
    """Bound and disambiguate HTTP input before schema coercion or retrieval."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = scope.get('headers', [])
        authorization = [value for key, value in headers if key.lower() == b'authorization']
        if len(authorization) > 1:
            return await error(401, 'invalid_token', 'Exactly one Bearer header is required.')(scope, receive, send)
        if any(key.lower() in (b'x-tenant-id', b'x-roles', b'x-role', b'x-user-id') for key, _ in headers):
            return await error(400, 'invalid_request', 'Identity override headers are not accepted.')(scope, receive, send)
        if scope.get('query_string'):
            return await error(400, 'invalid_request', 'Query-string parameters are not accepted.')(scope, receive, send)
        if scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        types = [value.decode('latin1').split(';')[0].strip().lower()
                 for key, value in headers if key.lower() == b'content-type']
        if types != ['application/json']:
            return await error(415, 'unsupported_media_type', 'One application/json content type is required.')(scope, receive, send)
        body = bytearray()
        deadline = time.monotonic() + 3.0
        while True:
            try:
                message = await asyncio.wait_for(receive(), timeout=min(2.0, max(0.001, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                return await error(408, 'request_timeout', 'The request body deadline expired.')(scope, receive, send)
            if time.monotonic() > deadline:
                return await error(408, 'request_timeout', 'The request body deadline expired.')(scope, receive, send)
            if message['type'] == 'http.disconnect':
                return
            if message['type'] != 'http.request':
                continue
            body.extend(message.get('body', b''))
            if len(body) > MAX_BODY:
                return await error(413, 'body_too_large', 'The request body exceeds its byte limit.')(scope, receive, send)
            if not message.get('more_body'):
                break
        try:
            value = strict_json(bytes(body))
            if not isinstance(value, dict):
                raise ValueError('Expected a JSON object.')
        except (ValueError, UnicodeError, RecursionError):
            return await error(400, 'invalid_json', 'The JSON body is malformed or ambiguous.')(scope, receive, send)
        consumed = False
        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()
        return await self.app(scope, replay, send)


def create_app(verifier: JwtVerifier, pipeline) -> FastAPI:
    app = FastAPI(title='Enterprise AI Lab — permission-aware RAG', version='0.2.0',
                  docs_url=None, redoc_url=None)
    app.add_middleware(JsonBoundary)
    query_lock = threading.Lock()

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exception):
        # Do not echo submitted query text, tokens or unknown identity fields.
        return error(422, 'invalid_request', 'Expected only a bounded query and integer top_k from 1 to 5.')

    @app.get('/healthz')
    def health():
        return JSONResponse({'status': 'ok', 'authentication': 'verified-jwt', 'vector_backend': 'qdrant-local'},
                            headers={'cache-control': 'no-store'})

    @app.post('/v1/tenants/{tenant_id}/rag')
    def rag(tenant_id: str, body: Query, request: Request):
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', tenant_id):
            return error(422, 'invalid_request', 'The tenant path is malformed.')
        raw = request.headers.get('authorization', '')
        match = re.fullmatch(r'Bearer ([A-Za-z0-9_.-]+)', raw, flags=re.IGNORECASE)
        if not match or len(match[1]) > 4096:
            return error(401, 'invalid_token', 'A bounded Bearer token is required.')
        try:
            principal, scopes = verifier.verify(match[1])
            authorize(principal, scopes, tenant_id)
        except AuthenticationError:
            return error(401, 'invalid_token', 'The token is invalid for this resource.')
        except AuthorizationError:
            return error(403, 'forbidden', 'This principal cannot query the requested tenant.')
        # Admission is bounded; the model and local vector client are serialized.
        if not query_lock.acquire(blocking=False):
            return error(503, 'resource_busy', 'The local RAG worker is busy; retry explicitly.')
        try:
            # A token could expire while waiting on unrelated application work.
            principal, scopes = verifier.verify(match[1])
            authorize(principal, scopes, tenant_id)
            result = pipeline.query(body.query, principal, top_k=body.top_k)
            return JSONResponse(result, headers={'cache-control': 'no-store'})
        except AuthenticationError:
            return error(401, 'invalid_token', 'The token is invalid for this resource.')
        except AuthorizationError:
            return error(403, 'forbidden', 'This principal cannot query the requested tenant.')
        except QueryLimitError:
            return error(422, 'query_limit', 'The question exceeds the embedding input limits; shorten it.')
        except Exception:
            # Generation/storage errors must not become an apparently successful answer.
            return error(503, 'rag_unavailable', 'The local retrieval or generation step could not complete.')
        finally:
            query_lock.release()

    return app
