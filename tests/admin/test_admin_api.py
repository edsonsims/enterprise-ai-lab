"""Signed RSA sessions plus actual local SQLite; OAuth exchange is controlled here."""

import base64
from copy import deepcopy
import hashlib
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

from enterprise_ai_lab.admin import api
from enterprise_ai_lab.admin.auth import ADMIN_AUDIENCE, AdminTokenVerifier
from enterprise_ai_lab.admin.core import AdminStore
from enterprise_ai_lab.mcp_gateway.store import ReadOnlyStore, initialize_fixture
from enterprise_ai_lab.review.sessions import Sessions

ORIGIN = "http://127.0.0.1:8129"
ISSUER = "http://127.0.0.1:8130"


@pytest.fixture
def portal(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public.update(kid="admin-test-key", alg="RS256", use="sig")
    verifier = AdminTokenVerifier(ISSUER, {"keys": [public]})
    source_path = tmp_path / "source.sqlite3"
    initialize_fixture(source_path)
    source = ReadOnlyStore(source_path)
    store = AdminStore(tmp_path / "config.sqlite3", source)
    sessions = Sessions()
    with TestClient(
        api.create_app(
            origin=ORIGIN, verifier=verifier, service=store, sessions=sessions
        ),
        base_url=ORIGIN,
    ) as client:

        def sign(
            subject="alice",
            tenant="alpha",
            roles=("config_admin", "reader"),
            scopes="admin:read admin:configure admin:audit",
            **extra,
        ):
            now = int(time.time())
            claims = dict(
                iss=ISSUER,
                aud=ADMIN_AUDIENCE,
                iat=now,
                exp=now + 300,
                sub=subject,
                client_id="admin-portal",
                jti="controlled-admin-token",
                identity_kind="user",
                tenant_id=tenant,
                roles=list(roles),
                scope=scopes,
            )
            claims.update(extra)
            return jwt.encode(
                claims,
                key,
                algorithm="RS256",
                headers={"kid": "admin-test-key", "typ": "at+jwt"},
            )

        def login(**kwargs):
            token = sign(**kwargs)
            identifier = sessions.create(token, int(time.time()) + 300)
            client.cookies.set(api.COOKIE, identifier, domain="127.0.0.1", path="/")
            return {
                "origin": ORIGIN,
                "x-csrf-token": sessions.get(identifier).csrf,
            }, token

        yield dict(
            client=client,
            store=store,
            sessions=sessions,
            verifier=verifier,
            key=key,
            sign=sign,
            login=login,
            source=source,
        )
    store.close()
    source.close()


def body(**extra):
    return {
        "command_id": "same-command",
        "expected_revision": 0,
        "action": "update",
        "config": {"work_order_page_size": 1, "default_status": "open"},
        **extra,
    }


def test_signed_session_and_page_cannot_release_bearer_or_mutate_without_confirm(
    portal,
):
    client = portal["client"]
    assert client.get("/api/config").status_code == 401
    headers, token = portal["login"]()
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.json()["user"] == {
        "subject": "alice",
        "tenant_id": "alpha",
        "roles": ["config_admin", "reader"],
    }
    assert response.json()["can_configure"]
    assert token not in response.text
    assert "access_token" not in response.text and "id_token" not in response.text
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/config").json()["revision"] == 0
    assert client.post("/api/commands", json=body(), headers=headers).status_code == 200


def test_exact_replay_conflict_rollback_and_tenant_local_ids_use_actual_sqlite(portal):
    client = portal["client"]
    headers, _ = portal["login"]()
    first = client.post("/api/commands", json=body(), headers=headers).json()
    assert first["revision"] == 1 and first["tenant_id"] == "alpha"
    assert client.post("/api/commands", json=body(), headers=headers).json() == first
    assert (
        client.post(
            "/api/commands",
            json=body(config={"work_order_page_size": 2, "default_status": "open"}),
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/commands", json=body(command_id="new-id"), headers=headers
        ).status_code
        == 409
    )
    rollback = {
        "command_id": "restore",
        "expected_revision": 1,
        "action": "rollback",
        "target_revision": 0,
    }
    assert (
        client.post("/api/commands", json=rollback, headers=headers).json()["revision"]
        == 2
    )
    assert client.get("/api/config").json()["config"] == {
        "work_order_page_size": 3,
        "default_status": "open",
    }
    history = client.get("/api/audit").json()
    assert history["total"] == 2
    assert {row["tenant_id"] for row in history["events"]} == {"alpha"}
    headers, _ = portal["login"](tenant="beta", subject="bob")
    assert client.get("/api/config").json()["revision"] == 0
    assert client.get("/api/audit").json()["events"] == []
    second = client.post("/api/commands", json=body(), headers=headers).json()
    assert second["tenant_id"] == "beta" and second["revision"] == 1
    rows = client.get("/api/work-orders").json()
    assert len(rows["items"]) == 1 and {r["tenant_id"] for r in rows["items"]} == {
        "beta"
    }


def test_configured_source_read_keeps_reader_acl_and_sql_limit(portal):
    client = portal["client"]
    headers, _ = portal["login"]()
    client.post("/api/commands", json=body(), headers=headers)
    response = client.get("/api/work-orders")
    assert response.status_code == 200
    value = response.json()
    assert value["configuration"]["revision"] == 1
    assert len(value["items"]) == 1
    assert value["items"][0]["work_order_id"] == "WO-0001"
    assert value["items"][0]["tenant_id"] == "alpha"


@pytest.mark.parametrize(
    "roles,scopes,read,write,audit",
    [
        (
            ("config_admin", "reader"),
            "admin:read admin:configure admin:audit",
            200,
            200,
            200,
        ),
        (("config_admin", "reader"), "admin:read", 200, 403, 403),
        (("config_admin", "reader"), "admin:configure", 403, 200, 403),
        (("config_auditor", "reader"), "admin:read admin:audit", 200, 403, 200),
        (("observer",), "", 403, 403, 403),
    ],
)
def test_scope_and_role_required_independently(
    portal, roles, scopes, read, write, audit
):
    headers, _ = portal["login"](roles=roles, scopes=scopes)
    client = portal["client"]
    assert client.get("/api/config").status_code == read
    assert (
        client.post("/api/commands", json=body(), headers=headers).status_code == write
    )
    assert client.get("/api/audit").status_code == audit


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "urn:identity-boundary:review"},
        {"client_id": "review-portal"},
        {"identity_kind": "service"},
        {"sub": "admin-portal"},
        {"scope": "admin:configure", "roles": ["config_auditor", "reader"]},
        {"exp": 1},
        {"tenant_id": "alpha, beta"},
    ],
)
def test_wrong_resource_or_forged_identity_is_not_a_human_admin(portal, claims):
    portal["login"](**claims)
    assert portal["client"].get("/api/config").status_code == 401


@pytest.mark.parametrize(
    "header,value",
    [
        ("authorization", "Bearer PRIVATE-MARKER"),
        ("x-tenant-id", "beta"),
        ("x-roles", "config_admin"),
        ("x-client-id", "other"),
        ("x-forwarded-host", "evil.example"),
        ("host", "evil.example"),
    ],
)
def test_browser_identity_and_host_headers_are_rejected(portal, header, value):
    portal["login"]()
    response = portal["client"].get("/api/config", headers={header: value})
    assert response.status_code == 400 and "PRIVATE-MARKER" not in response.text


def test_duplicate_session_and_authorization_headers_refused(portal):
    portal["login"]()
    client = portal["client"]
    assert (
        client.get(
            "/api/config", headers={"cookie": f"{api.COOKIE}=a; {api.COOKIE}=b"}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/config", headers=[("authorization", "a"), ("authorization", "b")]
        ).status_code
        == 400
    )
    assert client.get("/api/config?tenant_id=beta").status_code == 400
    assert client.get("/api/work-orders?limit=10").status_code == 400
    assert client.get("/auth/callback?state=a&state=b&code=c").status_code == 400


@pytest.mark.parametrize("spacing", ["", " ", "\t"])
@pytest.mark.parametrize("valid_first", [False, True])
def test_cookie_parser_whitespace_does_not_hide_duplicate_session(
    portal, spacing, valid_first
):
    portal["login"]()
    valid = portal["client"].cookies.get(api.COOKIE)
    pieces = [f"{api.COOKIE}{spacing}={valid}", f"{api.COOKIE}=invalid"]
    if not valid_first:
        pieces.reverse()
    response = portal["client"].get(
        "/api/config", headers={"cookie": "; ".join(pieces)}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "origin,csrf",
    [(False, True), (True, False), ("null", True), ("http://127.0.0.1:1", True)],
)
def test_mutations_require_exact_origin_and_csrf(portal, origin, csrf):
    headers, _ = portal["login"]()
    if not csrf:
        headers.pop("x-csrf-token")
    if origin is False:
        headers.pop("origin")
    elif origin is not True:
        headers["origin"] = origin
    assert (
        portal["client"].post("/api/commands", json=body(), headers=headers).status_code
        == 403
    )
    assert portal["client"].get("/api/config").json()["revision"] == 0


@pytest.mark.parametrize(
    "content",
    [
        b'{"action":"update","action":"rollback"}',
        b"\xff",
        b"[]",
        b'{"page_size":NaN}',
        b"{" + b" " * 4096 + b"}",
    ],
)
def test_strict_bounded_json_refuses_ambiguous_or_malformed_body(portal, content):
    headers, _ = portal["login"]()
    response = portal["client"].post(
        "/api/commands",
        content=content,
        headers={**headers, "content-type": "application/json"},
    )
    assert response.status_code == 400
    assert portal["client"].get("/api/config").json()["revision"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "beta"},
        {"action": "restart"},
        {"expected_revision": True},
        {"expected_revision": "0"},
        {"expected_revision": 1001},
        {"command_id": "../command"},
        {"config": {"work_order_page_size": True, "default_status": "open"}},
        {"config": {"work_order_page_size": 11, "default_status": "open"}},
        {
            "config": {
                "work_order_page_size": 2,
                "default_status": "open",
                "roles": ["admin"],
            }
        },
        {"config": {"work_order_page_size": 2, "default_status": "PRIVATE-MARKER"}},
    ],
)
def test_exact_command_schema_cannot_become_general_admin_api(portal, change):
    headers, _ = portal["login"]()
    response = portal["client"].post(
        "/api/commands", json=body(**deepcopy(change)), headers=headers
    )
    assert response.status_code == 400 and "PRIVATE-MARKER" not in response.text
    assert portal["client"].get("/api/config").json()["revision"] == 0


def test_logout_needs_json_and_removes_server_session(portal):
    headers, _ = portal["login"]()
    cookie = portal["client"].cookies.get(api.COOKIE)
    assert (
        portal["client"]
        .post("/logout", json={"unexpected": True}, headers=headers)
        .status_code
        == 400
    )
    response = portal["client"].post("/logout", json={}, headers=headers)
    assert response.status_code == 200 and response.json() == {"logged_out": True}
    assert (
        portal["client"]
        .get("/api/config", headers={"cookie": f"{api.COOKIE}={cookie}"})
        .status_code
        == 401
    )


def test_pkce_nonce_state_browser_binding_and_single_use_code_exchange(
    portal, monkeypatch
):
    client, sessions = portal["client"], portal["sessions"]
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    cookie = response.headers["set-cookie"].lower()
    assert (
        "httponly" in cookie
        and "samesite=lax" in cookie
        and "path=/auth/callback" in cookie
    )
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["client_id"] == ["admin-portal"] and query["resource"] == [
        ADMIN_AUDIENCE
    ]
    assert query["code_challenge_method"] == ["S256"] and query["prompt"] == ["login"]
    state, nonce = query["state"][0], query["nonce"][0]
    pending = sessions.logins[state]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [expected]
    calls = []
    now = int(time.time())
    id_token = jwt.encode(
        dict(
            iss=ISSUER,
            aud="admin-portal",
            sub="alice",
            nonce=nonce,
            auth_time=now,
            iat=now,
            exp=now + 300,
        ),
        portal["key"],
        algorithm="RS256",
        headers={"kid": "admin-test-key", "typ": "JWT"},
    )

    def exchange(request):
        form = parse_qs(request.content.decode())
        calls.append(form)
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": portal["sign"](),
                "id_token": id_token,
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(exchange), **kwargs),
    )
    callback = "/auth/callback?" + urlencode(
        {"code": "controlled-code", "state": state, "iss": ISSUER}
    )
    binding = client.cookies.get(api.BINDING)
    client.cookies.delete(api.BINDING)
    assert client.get(callback).status_code == 401
    assert not calls and state in sessions.logins
    client.cookies.set(api.BINDING, binding, domain="127.0.0.1", path="/auth/callback")
    accepted = client.get(callback, follow_redirects=False)
    assert accepted.status_code == 302
    assert calls[0]["code_verifier"] == [pending["verifier"]]
    assert calls[0]["client_id"] == ["admin-portal"]
    assert client.get("/api/session").json()["user"]["subject"] == "alice"
    assert client.get(callback).status_code == 401 and len(calls) == 1


def test_unexpected_core_failure_is_finite_and_responses_never_cache(
    portal, monkeypatch
):
    portal["login"]()

    def failure(*_args):
        raise RuntimeError("PRIVATE-EXCEPTION-MARKER")

    monkeypatch.setattr(portal["store"], "snapshot", failure)
    response = portal["client"].get("/api/config")
    assert (
        response.status_code == 503 and "PRIVATE-EXCEPTION-MARKER" not in response.text
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_socket_startup_failure_closes_actual_readonly_source(
    portal, monkeypatch, tmp_path
):
    import argparse
    import asyncio
    import socket
    from enterprise_ai_lab.admin import __main__ as command
    from enterprise_ai_lab.mcp_gateway import store as source_module

    sources = []
    actual_source = source_module.ReadOnlyStore
    actual_socket = socket.socket

    class ObservedSource(actual_source):
        def __init__(self, path):
            super().__init__(path)
            sources.append(self)

    def refused_socket(family=socket.AF_INET, *args, **kwargs):
        if family == socket.AF_INET:
            raise OSError("controlled local socket admission failure")
        return actual_socket(family, *args, **kwargs)

    monkeypatch.setattr(source_module, "ReadOnlyStore", ObservedSource)
    monkeypatch.setattr(command.socket, "socket", refused_socket)
    monkeypatch.setattr(
        AdminTokenVerifier, "discover", lambda _issuer: portal["verifier"]
    )
    try:
        with pytest.raises(OSError):
            asyncio.run(
                command.serve(
                    argparse.Namespace(
                        port=0,
                        issuer=ISSUER,
                        source=portal["source"].database_path,
                        database=tmp_path / "startup.sqlite3",
                    )
                )
            )
        assert len(sources) == 1 and sources[0]._closed
    finally:
        for source in sources:
            source.close()
