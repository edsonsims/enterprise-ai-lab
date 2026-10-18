"""Real loopback HTTP with signed user tokens; workflow/IdP adapters controlled.

The separate review process integration uses the actual oidc-provider login and
Temporal publication owner. These tests isolate the browser/session boundary.
"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket
from threading import Thread
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import jwt
from PIL import Image, ImageChops
import pytest
import uvicorn

from enterprise_ai_lab.approval.contracts import DecisionCommand, DecisionReceipt, format_utc
from enterprise_ai_lab.publication.contracts import make_request
from enterprise_ai_lab.publication.gateway import PublicationNotFound
from enterprise_ai_lab.publication.storage import CandidateRepository
from enterprise_ai_lab.rag.pdf_corpus import load_pdf_documents
from enterprise_ai_lab.review.api import BINDING, COOKIE, create_app
from enterprise_ai_lab.review.assets import PdfAssets
from enterprise_ai_lab.review.auth import HumanPrincipal, HumanTokenVerifier, REVIEW_AUDIENCE
from enterprise_ai_lab.review.core import HumanReviewService
from enterprise_ai_lab.review.sessions import Sessions

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "public-data/pdf-v1/ingestion.json"


@contextmanager
def live_server(factory):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0)); sock.listen()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = factory(origin)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("loopback test server did not start")
        time.sleep(.01)
    client = httpx.Client(base_url=origin, trust_env=False, timeout=3, follow_redirects=False)
    try:
        yield origin, client
    finally:
        client.close(); server.should_exit = True; thread.join(timeout=5); sock.close()
        assert not thread.is_alive(), "loopback test server did not stop"


class ControlledWorkflow:
    """Explicit gateway test double; no claim of Temporal coverage here."""
    def __init__(self):
        self.requests, self.decisions = {}, []

    async def submit(self, principal, request_id, candidate_sha256, expected_revision, expires_at):
        request = make_request(request_id, principal.tenant_id, principal.client_id,
                               candidate_sha256, expected_revision, expires_at)
        value = {"request": asdict(request), "status": "pending", "publication_status": "not_applicable",
                 "receipt": None, "effect": None}
        return self.requests.setdefault((principal.tenant_id, request_id), value)

    async def status(self, principal, request_id):
        try:
            return self.requests[(principal.tenant_id, request_id)]
        except KeyError:
            raise PublicationNotFound("missing controlled workflow")

    async def decide(self, principal, request_id, command_id, snapshot_sha256, decision):
        value = await self.status(principal, request_id)
        command = DecisionCommand(command_id, principal.tenant_id, snapshot_sha256, decision,
                                  principal.client_id, principal.roles)
        self.decisions.append(command)
        value.update(status="approved" if decision == "approve" else "rejected",
                     publication_status="pending" if decision == "approve" else "not_applicable",
                     receipt=asdict(DecisionReceipt(command, format_utc(datetime.now(timezone.utc)))))
        return value


@pytest.fixture(scope="module")
def public_documents():
    return load_pdf_documents(MANIFEST)


@pytest.fixture
def portal(tmp_path, public_documents):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public.update(kid="test-review", alg="RS256", use="sig")
    exchange = {}
    issuer = FastAPI()
    @issuer.post("/token")
    async def token_endpoint(request: Request):
        form = parse_qs((await request.body()).decode())
        exchange["last_form"] = form
        if form.get("code") != ["controlled-code"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return exchange["response"]
    with live_server(lambda _origin: issuer) as (issuer_origin, _issuer_client):
        verifier = HumanTokenVerifier(issuer_origin, {"keys": [public]})
        sessions = Sessions()
        repository = CandidateRepository(tmp_path / "candidate.sqlite")
        candidate = repository.stage([document for document in public_documents if document.tenant_id == "alpha"])
        gateway = ControlledWorkflow()
        service = HumanReviewService(repository, gateway, tmp_path / "audit.sqlite")
        def factory(origin):
            return create_app(origin=origin, verifier=verifier, service=service, assets=PdfAssets(MANIFEST), sessions=sessions)
        with live_server(factory) as (origin, client):
            def signed(subject="alice", tenant="alpha", roles=("approver", "reader", "analyst"),
                       scopes="review:read review:decide review:request", ttl=300, **extra):
                now = int(time.time())
                claims = dict(iss=issuer_origin, aud=REVIEW_AUDIENCE, iat=now, exp=now+ttl, sub=subject,
                              client_id="review-portal", jti="controlled-user-jti", identity_kind="user",
                              tenant_id=tenant, roles=list(roles), scope=scopes)
                claims.update(extra)
                return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-review", "typ": "at+jwt"})
            def sign_in(**kwargs):
                token = signed(**kwargs)
                principal, scopes = verifier.verify(token)
                identifier = sessions.create(token, principal.expires_at)
                client.cookies.set(COOKIE, identifier, domain="127.0.0.1", path="/")
                return {"Origin": origin, "X-CSRF-Token": sessions.get(identifier).csrf,
                        "Content-Type": "application/json"}, token
            yield dict(client=client, origin=origin, verifier=verifier, sessions=sessions,
                       service=service, gateway=gateway, candidate=candidate, signed=signed,
                       sign_in=sign_in, exchange=exchange, key=key, documents=public_documents)


def request_body(portal):
    return dict(request_id="human-http", candidate_sha256=portal["candidate"].candidate_sha256,
                expected_revision=0, expires_at=format_utc(datetime.now(timezone.utc)+timedelta(minutes=5)))


def create_review(portal):
    headers, _ = portal["sign_in"](subject="requester-user", roles=("requester", "reader"))
    response = portal["client"].post("/api/reviews", json=request_body(portal), headers=headers)
    assert response.status_code == 200
    return response.json()


def test_real_loopback_session_hides_tokens_and_sets_browser_policy(portal):
    client = portal["client"]
    assert client.get("/api/session").status_code == 401
    _, token = portal["sign_in"]()
    response = client.get("/api/session")
    assert response.status_code == 200 and response.json()["user"]["subject"] == "alice"
    assert token not in response.text and "access_token" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]


def test_login_pkce_nonce_cookie_callback_binding_and_single_use(portal):
    client, sessions = portal["client"], portal["sessions"]
    login = client.get("/login")
    assert login.status_code == 302
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/auth/callback" in cookie
    query = parse_qs(urlsplit(login.headers["location"]).query)
    assert query["response_type"] == ["code"] and query["code_challenge_method"] == ["S256"]
    assert query["prompt"] == ["login"]
    state, nonce = query["state"][0], query["nonce"][0]
    pending = sessions.logins[state]
    expected = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == [expected]
    callback = "/auth/callback?" + urlencode({"code": "controlled-code", "state": state, "iss": portal["verifier"].issuer})
    # Correct state without its browser-bound opaque cookie cannot be exchanged.
    isolated = httpx.Client(base_url=portal["origin"], trust_env=False, timeout=3)
    try:
        assert isolated.get(callback).status_code == 401
    finally:
        isolated.close()
    assert state in sessions.logins and "last_form" not in portal["exchange"]
    now = int(time.time())
    id_token = jwt.encode(dict(iss=portal["verifier"].issuer, aud="review-portal", iat=now, exp=now+300,
                               sub="alice", nonce=nonce, auth_time=now), portal["key"], algorithm="RS256",
                          headers={"kid": "test-review", "typ": "JWT"})
    portal["exchange"]["response"] = dict(access_token=portal["signed"](), id_token=id_token, token_type="Bearer")
    accepted = client.get(callback)
    assert accepted.status_code == 302 and accepted.headers["location"] == "/"
    assert "httponly" in accepted.headers.get_list("set-cookie")[0].lower()
    assert client.get("/api/session").json()["user"]["subject"] == "alice"
    assert portal["exchange"]["last_form"]["code_verifier"] == [pending["verifier"]]
    assert portal["exchange"]["last_form"]["client_id"] == ["review-portal"]
    assert client.get(callback).status_code == 401
    assert len([event for event in portal["service"].audit.events() if event["action"] == "login"]) == 1


@pytest.mark.parametrize("header,value", [("Authorization", "Bearer ignored"), ("X-Tenant-ID", "beta"),
                                           ("X-Roles", "approver"), ("X-Role", "approver"), ("X-User-ID", "bob")])
def test_browser_boundary_refuses_bearer_and_identity_spoofing(portal, header, value):
    portal["sign_in"]()
    assert portal["client"].get("/api/reviews", headers={header: value}).status_code == 422


def test_host_duplicate_cookie_and_query_identity_are_refused(portal):
    portal["sign_in"]()
    client = portal["client"]
    assert client.get("/api/session", headers={"Host": "attacker.example"}).status_code == 422
    assert client.get("/api/session", headers={"Cookie": f"{COOKIE}=a; {COOKIE}=b"}).status_code == 401
    assert client.get("/api/reviews?tenant_id=beta").status_code == 422
    assert client.get("/auth/callback?state=a&state=b&code=c").status_code == 422


def test_csrf_requires_origin_and_bound_session_token(portal):
    headers, _ = portal["sign_in"](subject="requester-user", roles=("requester", "reader"))
    client = portal["client"]
    for altered in ({"Content-Type": "application/json"},
                    {**headers, "Origin": "http://127.0.0.1:1"},
                    {key: value for key,value in headers.items() if key.lower() != "x-csrf-token"},
                    {**headers, "X-CSRF-Token": "wrong"}):
        assert client.post("/api/reviews", json=request_body(portal), headers=altered).status_code == 403
    assert portal["gateway"].requests == {}
    assert client.post("/api/reviews", json=request_body(portal), headers=headers).status_code == 200


def test_json_extra_duplicate_and_unbounded_fields_are_rejected(portal):
    headers, _ = portal["sign_in"](subject="requester-user", roles=("requester", "reader"))
    body = request_body(portal)
    client = portal["client"]
    assert client.post("/api/reviews", json={**body, "tenant_id": "beta"}, headers=headers).status_code == 422
    assert client.post("/api/reviews", content='{"request_id":"a","request_id":"b"}', headers=headers).status_code == 422
    assert client.post("/api/reviews", content=json.dumps({"padding": "a"*17000}), headers=headers).status_code == 422
    assert client.post("/api/reviews", json={**body, "expected_revision": True}, headers=headers).status_code == 422
    assert portal["gateway"].requests == {}


def test_page_acl_is_enforced_before_pdf_assets_and_audit(portal):
    created = create_review(portal)
    assert created["denied_document_count"] == 1
    portal["sign_in"](roles=("approver", "reader"))
    client = portal["client"]
    visible = client.get("/api/reviews/human-http").json()
    assert visible["denied_document_count"] == 1 and not visible["can_decide"]
    native = next(document for document in portal["documents"] if document.tenant_id == "alpha" and document.provenance.method == "native")
    hidden = next(document for document in portal["documents"] if document.tenant_id == "alpha" and document.provenance.method == "ocr")
    response = client.get(f"/api/reviews/human-http/documents/{native.document_id}/pdf")
    assert response.status_code == 200 and response.headers["x-preview-page-count"] == "1"
    assert response.headers["content-type"] == "application/pdf"
    assert client.get(f"/api/reviews/human-http/documents/{hidden.document_id}/pdf").status_code == 404
    assert client.get(f"/api/reviews/human-http/documents/{native.document_id}/pdf?variant=all").status_code == 422
    events = portal["service"].audit.events("alpha")
    allowed = [row for row in events if row["action"] == "pdf_view" and row["result"] == "allowed"]
    assert len(allowed) == 1 and allowed[0]["document_id"] == native.document_id
    denied = [row for row in events if row["action"] == "pdf_view" and row["result"] == "denied"]
    assert len(denied) == 1 and denied[0]["document_id"] is None
    portal["sign_in"](subject="beta-reviewer", tenant="beta")
    assert client.get(f"/api/reviews/human-http/documents/{native.document_id}/pdf").status_code == 404


def test_signed_user_decision_and_logout_are_csrf_bound_json_operations(portal):
    created = create_review(portal)
    headers, _ = portal["sign_in"]()
    body = dict(command_id="human-decision", snapshot_sha256=created["request"]["snapshot_sha256"], decision="reject")
    client = portal["client"]
    assert client.post("/api/reviews/human-http/decisions", json={**body, "actor_id": "forged"}, headers=headers).status_code == 422
    response = client.post("/api/reviews/human-http/decisions", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert portal["gateway"].decisions[0].actor_id == "alice"
    assert client.post("/logout", json={}, headers={**headers, "X-CSRF-Token": "wrong"}).status_code == 403
    logged_out = client.post("/logout", json={}, headers=headers)
    assert logged_out.status_code == 200 and logged_out.json() == {"logged_out": True}
    assert client.get("/api/session").status_code == 401
    assert len([row for row in portal["service"].audit.events() if row["action"] == "logout"]) == 1


def test_real_clock_expiry_requires_new_authentication(portal):
    portal["sign_in"](ttl=2)
    client = portal["client"]
    assert client.get("/api/session").status_code == 200
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and client.get("/api/session").status_code == 200:
        time.sleep(.05)
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/reviews").status_code == 401


def test_png_page_preview_uses_live_user_acl_and_never_discloses_hidden_pages(portal):
    create_review(portal)
    client = portal["client"]
    native = next(document for document in portal["documents"] if document.tenant_id == "alpha" and document.provenance.method == "native")
    hidden = next(document for document in portal["documents"] if document.tenant_id == "alpha" and document.provenance.method == "ocr")
    native_url = f"/api/reviews/human-http/documents/{native.document_id}/preview.png"
    hidden_url = f"/api/reviews/human-http/documents/{hidden.document_id}/preview.png"
    client.cookies.clear()
    assert client.get(native_url).status_code == 401
    _, token = portal["sign_in"](roles=("approver", "reader"))
    detail = client.get("/api/reviews/human-http").json()
    assert detail["documents"][0]["preview_url"] == native_url
    assert detail["documents"][0]["original_preview_url"] == native_url + "?variant=original"
    image_response = client.get(native_url)
    assert image_response.status_code == 200 and image_response.headers["content-type"] == "image/png"
    assert image_response.headers["x-preview-page-count"] == "1"
    assert image_response.headers["x-render-sha256"] == hashlib.sha256(image_response.content).hexdigest()
    pdf_response = client.get(native_url.removesuffix("preview.png") + "pdf")
    assert image_response.headers["x-preview-sha256"] == pdf_response.headers["x-preview-sha256"]
    assert image_response.headers["cache-control"] == "no-store"
    assert token.encode() not in image_response.content
    with Image.open(BytesIO(image_response.content)) as image:
        assert image.size == (int(image_response.headers["x-render-width"]), int(image_response.headers["x-render-height"]))
        assert image.width * image.height <= 15_000_000
        with Image.new("RGB", image.size, "white") as white, image.convert("RGB") as rgb:
            with ImageChops.difference(rgb, white) as difference:
                assert difference.getbbox() is not None
    denied = client.get(hidden_url)
    assert denied.status_code == 404 and hidden.provenance.source_uri not in denied.text
    assert hidden.title not in denied.text and not denied.content.startswith(b"\x89PNG")
    assert client.get(native_url + "?variant=all").status_code == 422
    assert client.get(native_url + "?tenant_id=beta").status_code == 422
    portal["sign_in"](subject="beta-user", tenant="beta")
    assert client.get(native_url).status_code == 404
    portal["sign_in"]()  # A signed analyst role can independently read the OCR page.
    ocr_image = client.get(hidden_url)
    assert ocr_image.status_code == 200
    assert ocr_image.headers["x-render-sha256"] != image_response.headers["x-render-sha256"]
    original = client.get(hidden_url + "?variant=original")
    assert original.status_code == 200 and original.headers["x-preview-page-count"] == "1"
    events = portal["service"].audit.events()
    views = [event for event in events if event["action"] == "page_preview"]
    assert len([event for event in views if event["result"] == "allowed"]) == 3
    assert len([event for event in views if event["result"] == "denied"]) == 2
    assert all(event["document_id"] is None and event["request_id"] is None
               for event in views if event["result"] == "denied")
