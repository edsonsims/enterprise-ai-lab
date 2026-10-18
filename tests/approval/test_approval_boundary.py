"""Independent signed-JWT admission tests over actual loopback HTTP.

The gateway below is a controlled probe. These tests establish that rejected
requests cannot reach it; they are not Temporal durability or human-review tests.
Real Temporal restart/replay experiments live in the separate integration suite.
"""
from __future__ import annotations

import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
import json
import socket
import threading
import time
import unittest

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa

from enterprise_ai_lab.approval.api import create_app
from enterprise_ai_lab.rag.auth import AuthenticationError, JwtVerifier, RAG_AUDIENCE


APPROVAL_AUDIENCE = "urn:identity-boundary:approval"


class GatewayProbe:
    """Records server-derived identity; deliberately contains no workflow engine."""

    def __init__(self):
        self.calls = []
        self.failure = None

    def record(self, name, *arguments):
        self.calls.append((name, arguments))
        if self.failure is not None:
            raise self.failure
        return {"probe": name, "accepted_by_controlled_probe": True}

    async def submit(self, principal, request_id, note, expires_at):
        return self.record("submit", principal, request_id, note, expires_at)

    async def status(self, principal, request_id):
        return self.record("status", principal, request_id)

    async def decide(self, principal, request_id, command_id, snapshot_sha256, decision):
        return self.record("decide", principal, request_id, command_id, snapshot_sha256, decision)


class ApprovalHttpAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = {**jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True),
                      "kid": "approval-review", "alg": "RS256", "use": "sig"}

    def setUp(self):
        self.issuer = "http://127.0.0.1:8181"
        self.gateway = GatewayProbe()
        self.verifier = JwtVerifier(self.issuer, {"keys": [self.public]}, audience=APPROVAL_AUDIENCE)
        app = create_app(self.verifier, self.gateway)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 3
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            self.fail("Actual loopback approval HTTP server did not start.")

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=3)
        self.socket.close()
        self.assertFalse(self.thread.is_alive())

    def claims(self, *, role="requester", scope="approval:request", tenant="alpha", audience=APPROVAL_AUDIENCE,
               client=None):
        now = int(time.time())
        client = client or f"{role}-{tenant}"
        return {"iss": self.issuer, "aud": audience, "iat": now, "exp": now + 60,
                "sub": client, "client_id": client, "jti": "approval-http-review",
                "tenant_id": tenant, "roles": [role], "scope": scope}

    def token(self, **options):
        claims = options.pop("claims", None)
        return jwt.encode(self.claims(**options) if claims is None else claims, self.key, algorithm="RS256",
                          headers={"kid": "approval-review", "typ": "at+jwt"})

    def request_body(self):
        return {"request_id": "review-request", "note": {"title": "Synthetic note", "body": "Local-only fixture."},
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}

    @staticmethod
    def decision_body():
        return {"command_id": "review-command", "snapshot_sha256": "a" * 64, "decision": "approve"}

    def call(self, body=None, *, method="POST", path="/v1/tenants/alpha/approvals", token=None,
             with_token=True, headers=None, raw=None):
        if raw is None:
            raw = b"" if method == "GET" else json.dumps(self.request_body() if body is None else body).encode()
        pairs = [("content-type", "application/json")] if method == "POST" else []
        if with_token:
            pairs.append(("authorization", "Bearer " + (token or self.token())))
        if headers is not None:
            pairs = headers
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest(method, path)
            for key, value in pairs:
                connection.putheader(key, value)
            connection.putheader("content-length", str(len(raw)))
            connection.endheaders(raw)
            response = connection.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            connection.close()

    def test_valid_dedicated_signed_identity_reaches_controlled_backend(self):
        status, _body, headers = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(len(self.gateway.calls), 1)
        name, arguments = self.gateway.calls[0]
        principal = arguments[0]
        self.assertEqual((name, principal.client_id, principal.tenant_id, principal.roles),
                         ("submit", "requester-alpha", "alpha", ("requester",)))

    def test_no_token_expired_and_changed_signature_fail_before_backend(self):
        self.assertEqual(self.call(with_token=False)[0], 401)
        now = int(time.time())
        expired = self.token(claims={**self.claims(), "iat": now - 50, "exp": now - 1})
        self.assertEqual(self.call(token=expired)[0], 401)
        token = self.token()
        header, payload, signature = token.split(".")
        forged = ".".join((header, payload, ("A" if signature[0] != "A" else "B") + signature[1:]))
        status, body, headers = self.call(token=forged)
        self.assertEqual(status, 401)
        self.assertIn("www-authenticate", headers)
        self.assertNotIn(forged, body)
        self.assertEqual(self.gateway.calls, [])

    def test_unsigned_payload_changes_cannot_forge_approver_identity(self):
        token = self.token()
        header, _payload, signature = token.split(".")
        forged_claims = self.claims(role="approver", scope="approval:decide")
        encoded = base64.urlsafe_b64encode(json.dumps(forged_claims).encode()).rstrip(b"=").decode()
        forged = ".".join((header, encoded, signature))
        status, _body, _headers = self.call(self.decision_body(), token=forged,
                                          path="/v1/tenants/alpha/approvals/review-request/decisions")
        self.assertEqual(status, 401)
        self.assertEqual(self.gateway.calls, [])

    def test_rag_audience_cannot_be_reused_even_with_approval_scope_and_role(self):
        token = self.token(role="approver", scope="approval:decide", audience=RAG_AUDIENCE)
        self.assertEqual(self.call(self.decision_body(), token=token,
                                  path="/v1/tenants/alpha/approvals/review-request/decisions")[0], 401)
        self.assertEqual(self.gateway.calls, [])

    def test_cross_tenant_and_wrong_role_or_scope_fail_before_backend(self):
        variants = [{"tenant": "beta"}, {"role": "reader"}, {"role": "approver"},
                    {"scope": "approval:decide"}, {"scope": ""}, {"scope": "rag:query"}]
        for options in variants:
            with self.subTest(options=options):
                self.assertEqual(self.call(token=self.token(**options))[0], 403)
        self.assertEqual(self.gateway.calls, [])

    def test_decision_identity_is_verified_and_body_never_supplies_actor(self):
        token = self.token(role="approver", scope="approval:decide")
        path = "/v1/tenants/alpha/approvals/review-request/decisions"
        self.assertEqual(self.call(self.decision_body(), token=token, path=path)[0], 200)
        name, arguments = self.gateway.calls.pop()
        self.assertEqual(name, "decide")
        self.assertEqual((arguments[0].client_id, arguments[0].tenant_id, arguments[0].roles),
                         ("approver-alpha", "alpha", ("approver",)))
        self.assertEqual(arguments[1:], ("review-request", "review-command", "a" * 64, "approve"))
        for field in ["actor_id", "actor_roles", "tenant_id", "principal", "accepted_at", "token"]:
            with self.subTest(field=field):
                body = {**self.decision_body(), field: "private-sentinel"}
                status, response, _headers = self.call(body, token=token, path=path)
                self.assertEqual(status, 422)
                self.assertNotIn("private-sentinel", response)
        self.assertEqual(self.gateway.calls, [])

    def test_requester_cannot_decide_by_adding_approval_scope(self):
        token = self.token(scope="approval:decide")
        self.assertEqual(self.call(self.decision_body(), token=token,
                                  path="/v1/tenants/alpha/approvals/review-request/decisions")[0], 403)
        self.assertEqual(self.gateway.calls, [])

    def test_read_scope_and_role_are_independently_required(self):
        path = "/v1/tenants/alpha/approvals/review-request"
        for options in [{"role": "reader", "scope": "approval:read"}, {"scope": "approval:request"},
                        {"tenant": "beta", "scope": "approval:read"}]:
            with self.subTest(options=options):
                self.assertEqual(self.call(method="GET", path=path, token=self.token(**options))[0], 403)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.call(method="GET", path=path, token=self.token(scope="approval:read"))[0], 200)
        self.assertEqual(self.gateway.calls[0][0], "status")

    def test_duplicate_bearers_and_identity_override_headers_fail_closed(self):
        token = self.token()
        base = [("content-type", "application/json"), ("authorization", "Bearer " + token)]
        self.assertEqual(self.call(headers=[*base, ("Authorization", "Bearer " + token)])[0], 401)
        for name in ["x-tenant-id", "x-roles", "x-role", "x-user-id"]:
            with self.subTest(header=name):
                self.assertEqual(self.call(headers=[*base, (name, "beta")])[0], 400)
        self.assertEqual(self.gateway.calls, [])

    def test_query_string_cannot_override_tenant_or_identity(self):
        self.assertEqual(self.call(path="/v1/tenants/alpha/approvals?tenant=beta")[0], 400)
        self.assertEqual(self.gateway.calls, [])

    def test_request_body_cannot_override_trusted_snapshot_authority(self):
        for field in ["tenant_id", "requested_by", "roles", "snapshot_sha256", "actor_id", "action", "target"]:
            with self.subTest(field=field):
                status, body, _headers = self.call({**self.request_body(), field: "private-sentinel"})
                self.assertEqual(status, 422)
                self.assertNotIn("private-sentinel", body)
        self.assertEqual(self.gateway.calls, [])

    def test_malformed_ambiguous_and_oversized_json_never_reaches_backend(self):
        cases = [(b'{"request_id":"a","request_id":"b"}', 400),
                 (b'{"expires_at":NaN}', 400), (b'{"request_id":"\\ud800"}', 400),
                 (b" " * 16385, 413), (b"[]", 400)]
        for raw, expected in cases:
            with self.subTest(payload=raw[:40]):
                self.assertEqual(self.call(raw=raw)[0], expected)
        self.assertEqual(self.gateway.calls, [])

    def test_schema_rejects_invalid_decision_and_noncanonical_digest(self):
        token = self.token(role="approver", scope="approval:decide")
        path = "/v1/tenants/alpha/approvals/review-request/decisions"
        for mutation in [{"decision": "APPROVE"}, {"decision": True}, {"snapshot_sha256": "A" * 64},
                         {"snapshot_sha256": "a" * 63}, {"command_id": "../elsewhere"}, {"command_id": 2}]:
            with self.subTest(mutation=mutation):
                self.assertEqual(self.call({**self.decision_body(), **mutation}, token=token, path=path)[0], 422)
        self.assertEqual(self.gateway.calls, [])

    def test_wrong_media_type_and_duplicate_media_type_are_rejected(self):
        token = self.token()
        headers = [("authorization", "Bearer " + token)]
        self.assertEqual(self.call(headers=[*headers, ("content-type", "text/plain")])[0], 415)
        self.assertEqual(self.call(headers=[*headers, ("content-type", "application/json"),
                                            ("Content-Type", "application/json")])[0], 415)
        self.assertEqual(self.gateway.calls, [])

    def test_backend_failures_are_sanitized_and_do_not_reflect_note_or_token(self):
        token = self.token()
        self.gateway.failure = RuntimeError("private-backend-sentinel " + token)
        status, body, headers = self.call(token=token)
        self.assertEqual(status, 503)
        self.assertNotIn("private-backend-sentinel", body)
        self.assertNotIn(token, body)
        self.assertNotIn("Local-only fixture", body)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_approval_tokens_are_not_accepted_by_default_rag_verifier(self):
        rag_verifier = JwtVerifier(self.issuer, {"keys": [self.public]})
        with self.assertRaises(AuthenticationError):
            rag_verifier.verify(self.token())

    def test_capacity_rejects_excess_work_and_recovers_after_inflight_requests(self):
        # A controlled paused gateway keeps actual HTTP requests in flight. This
        # proves admission/release at the edge, not workflow throughput.
        release = threading.Event()

        async def paused_submit(principal, request_id, note, expires_at):
            result = self.gateway.record("submit", principal, request_id, note, expires_at)
            while not release.is_set():
                await asyncio.sleep(0.01)
            return result

        self.gateway.submit = paused_submit
        with ThreadPoolExecutor(max_workers=16) as executor:
            pending = [executor.submit(self.call) for _ in range(16)]
            try:
                deadline = time.monotonic() + 2
                while len(self.gateway.calls) < 16 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(self.gateway.calls), 16)
                status, body, _headers = self.call()
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(body)["error"]["code"], "approval_busy")
                self.assertEqual(len(self.gateway.calls), 16)
            finally:
                release.set()
            for future in pending:
                self.assertEqual(future.result()[0], 200)
        self.assertEqual(self.call()[0], 200)


if __name__ == "__main__":
    unittest.main()
