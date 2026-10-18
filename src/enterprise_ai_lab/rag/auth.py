"""Pinned local issuer discovery and offline RS256 resource-token verification."""
from __future__ import annotations

import base64
import json
import math
import re
import time
from urllib.parse import urlsplit

import httpx
import jwt

RAG_AUDIENCE = 'urn:identity-boundary:rag'
RAG_SCOPE = 'rag:query'
APPROVAL_AUDIENCE = 'urn:identity-boundary:approval'
_NAME = re.compile(r'[a-z][a-z0-9:_-]{0,63}\Z')
_COMPACT = re.compile(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z')


class AuthenticationError(ValueError):
    """An access token cannot establish a principal for this resource."""


class AuthorizationError(ValueError):
    """A verified principal cannot request this tenant's knowledge resource."""


def strict_json(raw: bytes, *, max_depth: int = 32):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field.')
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Nonfinite JSON number.')
        return number

    def constant(_value):
        raise ValueError('Nonfinite JSON number.')

    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                       parse_float=finite, parse_constant=constant)
    def check(item, depth):
        if depth > max_depth:
            raise ValueError('JSON nesting is too deep.')
        if isinstance(item, str):
            item.encode('utf-8')
        elif isinstance(item, dict):
            for key, child in item.items():
                key.encode('utf-8')
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
    check(value, 0)
    return value


def local_origin(value: str) -> str:
    """Only an explicitly configured literal loopback origin is trusted."""
    url = urlsplit(value)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1'
            or url.username or url.password or url.query or url.fragment
            or url.path not in ('', '/') or not url.port or not 1 <= url.port <= 65535):
        raise ValueError('Expected an explicit http://127.0.0.1:port issuer origin.')
    return f'http://127.0.0.1:{url.port}'


def fetch_object(url: str, *, timeout: float = 1.0, max_bytes: int = 32768) -> dict:
    # Ignore inherited proxies; credentials never go through an environment proxy.
    deadline = time.monotonic() + 2.0
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
        with client.stream('GET', url, headers={'accept': 'application/json', 'accept-encoding': 'identity'}) as response:
            if response.status_code != 200:
                raise ValueError('Issuer metadata is unavailable.')
            content_type = response.headers.get('content-type', '').split(';')[0].strip()
            if content_type not in ('application/json', 'application/jwk-set+json'):
                raise ValueError('Issuer metadata must be JSON.')
            data = bytearray()
            for chunk in response.iter_raw():
                if time.monotonic() > deadline:
                    raise ValueError('Issuer metadata exceeded its total deadline.')
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError('Issuer metadata exceeds its byte limit.')
    if time.monotonic() > deadline:
        raise ValueError('Issuer metadata exceeded its total deadline.')
    value = strict_json(bytes(data))
    if not isinstance(value, dict):
        raise ValueError('Issuer metadata must be an object.')
    return value


class JwtVerifier:
    """Startup JWKS snapshot. It never follows URLs supplied in a token."""

    def __init__(self, issuer: str, jwks: dict, *, audience: str = RAG_AUDIENCE):
        if audience not in (RAG_AUDIENCE, APPROVAL_AUDIENCE):
            raise ValueError('Unsupported local resource audience.')
        self.audience = audience
        self.issuer = local_origin(issuer)
        keys = jwks.get('keys')
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
            raise ValueError('Expected one to eight issuer signing keys.')
        self._keys = {}
        for key in keys:
            if (not isinstance(key, dict) or key.get('kty') != 'RSA'
                    or key.get('alg') != 'RS256' or key.get('use') != 'sig'
                    or not isinstance(key.get('kid'), str) or not 1 <= len(key['kid']) <= 128
                    or key['kid'] in self._keys
                    or any(name in key for name in ('d', 'p', 'q', 'dp', 'dq', 'qi', 'oth'))
                    or ('key_ops' in key and key['key_ops'] != ['verify'])):
                raise ValueError('Unsupported or ambiguous public signing key.')
            public = jwt.PyJWK.from_dict(key, algorithm='RS256').key
            if public.key_size < 2048:
                raise ValueError('RSA signing keys must have at least 2048 bits.')
            self._keys[key['kid']] = public

    @classmethod
    def discover(cls, issuer: str, *, audience: str = RAG_AUDIENCE) -> 'JwtVerifier':
        origin = local_origin(issuer)
        metadata = fetch_object(origin + '/.well-known/openid-configuration')
        if metadata.get('issuer') != origin or not isinstance(metadata.get('jwks_uri'), str):
            raise ValueError('Issuer discovery mismatch.')
        target = urlsplit(metadata['jwks_uri'])
        if (target.scheme != 'http' or target.hostname != '127.0.0.1'
                or target.port != urlsplit(origin).port or target.username or target.password
                or target.query or target.fragment):
            raise ValueError('Issuer keys must stay on the configured origin.')
        return cls(origin, fetch_object(metadata['jwks_uri']), audience=audience)

    def verify(self, token: str):
        from .contracts import RagPrincipal
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 4096 or not _COMPACT.fullmatch(token):
                raise ValueError('Unsupported compact token.')
            segments = token.split('.')
            decoded = [strict_json(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
                       for part in segments[:2]]
            header, proposed = decoded
            if (not isinstance(header, dict) or not isinstance(proposed, dict)
                    or header.get('alg') != 'RS256' or header.get('typ') != 'at+jwt'
                    or not isinstance(header.get('kid'), str) or header['kid'] not in self._keys
                    or 'crit' in header or 'b64' in header):
                raise ValueError('Unsupported token header.')
            claims = jwt.decode(token, self._keys[header['kid']], algorithms=['RS256'],
                issuer=self.issuer, audience=self.audience, leeway=0,
                options={'require': ['iss', 'aud', 'exp', 'iat', 'sub', 'client_id', 'jti'],
                         'strict_aud': True})
            now = time.time()
            if (claims != proposed or claims.get('iss') != self.issuer or claims.get('aud') != self.audience
                    or type(claims['exp']) is not int or type(claims['iat']) is not int
                    or claims['iat'] > now or claims['exp'] <= now
                    or not 0 < claims['exp'] - claims['iat'] <= 60 or now - claims['iat'] >= 60
                    or ('nbf' in claims and (type(claims['nbf']) is not int or claims['nbf'] > now))
                    or not isinstance(claims['jti'], str) or not 1 <= len(claims['jti']) <= 128
                    or not isinstance(claims['client_id'], str) or not _NAME.fullmatch(claims['client_id'])
                    or claims['sub'] != claims['client_id']
                    or not isinstance(claims.get('tenant_id'), str) or not _NAME.fullmatch(claims['tenant_id'])
                    or not isinstance(claims.get('roles'), list) or not 1 <= len(claims['roles']) <= 8
                    or not all(isinstance(role, str) and _NAME.fullmatch(role) for role in claims['roles'])
                    or len(set(claims['roles'])) != len(claims['roles'])
                    or not isinstance(claims.get('scope', ''), str)):
                raise ValueError('Invalid principal claims.')
            principal = RagPrincipal(tenant_id=claims['tenant_id'], roles=tuple(claims['roles']),
                                     client_id=claims['client_id'])
            return principal, tuple(claims.get('scope', '').split())
        except (ValueError, TypeError, KeyError, jwt.PyJWTError, UnicodeError, RecursionError) as exc:
            raise AuthenticationError('The token is invalid for this resource.') from exc


def authorize(principal, scopes: tuple[str, ...], requested_tenant: str) -> None:
    if (principal.tenant_id != requested_tenant or RAG_SCOPE not in scopes
            or not set(principal.roles).intersection(('reader', 'analyst'))):
        raise AuthorizationError('This principal cannot query the requested tenant.')
