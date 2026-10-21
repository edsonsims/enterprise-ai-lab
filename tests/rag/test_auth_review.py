"""Independent signed-token and actual-loopback discovery boundary probes."""
import base64
import hashlib
import hmac
import http.client
import json
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import uvicorn
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from enterprise_ai_lab.rag.auth import AuthenticationError, JwtVerifier, RAG_AUDIENCE
from enterprise_ai_lab.rag.api import create_app


def encoded(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@contextmanager
def issuer_http(jwks, *, mode="normal"):
    visits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            visits.append(self.path)
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/.well-known/openid-configuration":
                body = {"issuer": origin, "jwks_uri": origin + "/jwks"}
                if mode == "foreign-jwks":
                    body["jwks_uri"] = "http://127.0.0.2:9/jwks"
                if mode == "wrong-issuer":
                    body["issuer"] = "http://127.0.0.1:9"
            elif self.path == "/jwks":
                body = jwks
            else:
                self.send_error(404)
                return
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.end_headers()
                return
            raw = json.dumps(body).encode()
            if mode == "duplicate-json":
                raw = b'{"issuer":"x","issuer":"y"}'
            if mode == "oversized":
                raw = b" " * 32769
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            try:
                if mode == "slow-stream":
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.2)
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", visits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class SignedIdentityReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = {**jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True),
                      "kid": "review-key", "alg": "RS256", "use": "sig"}
        cls.jwks = {"keys": [cls.public]}

    def setUp(self):
        self.issuer = "http://127.0.0.1:8181"
        self.verifier = JwtVerifier(self.issuer, self.jwks)

    def claims(self):
        now = int(time.time())
        return {"iss": self.issuer, "aud": RAG_AUDIENCE, "iat": now, "exp": now + 60,
                "sub": "reader-alpha", "client_id": "reader-alpha", "jti": "test-request",
                "tenant_id": "alpha", "roles": ["reader"], "scope": "rag:query"}

    def sign(self, claims=None, headers=None):
        return jwt.encode(self.claims() if claims is None else claims, self.key, algorithm="RS256",
                          headers={"kid": "review-key", "typ": "at+jwt", **(headers or {})})

    def sign_raw(self, payload):
        header = encoded({"alg": "RS256", "typ": "at+jwt", "kid": "review-key"})
        message = f"{header}.{encoded(payload)}".encode()
        signature = self.key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return message.decode() + "." + encoded(signature)

    def test_signature_verified_claims_form_principal(self):
        principal, scopes = self.verifier.verify(self.sign())
        self.assertEqual((principal.tenant_id, principal.roles, scopes), ("alpha", ("reader",), ("rag:query",)))

    def test_missing_required_claims_and_identity_fields_fail_closed(self):
        for field in ["iss", "aud", "exp", "iat", "sub", "client_id", "jti", "tenant_id", "roles"]:
            claims = self.claims()
            del claims[field]
            with self.subTest(field=field), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.sign(claims))

    def test_algorithm_confusion_cannot_reuse_rsa_public_key_as_hmac_secret(self):
        header = encoded({"alg": "HS256", "typ": "at+jwt", "kid": "review-key"})
        message = f"{header}.{encoded(self.claims())}".encode()
        public_der = self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
        forged = message.decode() + "." + encoded(hmac.new(public_der, message, hashlib.sha256).digest())
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(forged)

    def test_access_token_type_critical_extension_and_unknown_key_rejected(self):
        for header in [{"typ": "JWT"}, {"crit": ["tenant_id"]}, {"b64": False}, {"kid": "unknown-key"}]:
            with self.subTest(header=header), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.sign(headers=header))

    def test_audience_and_subject_must_match_this_client_credentials_resource(self):
        for field, value in [("aud", [RAG_AUDIENCE]), ("aud", "urn:identity-boundary:reports:jwt"),
                             ("iss", "http://127.0.0.1:9"), ("sub", "another-client")]:
            claims = {**self.claims(), field: value}
            with self.subTest(field=field, value=value), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.sign(claims))

    def test_strict_integer_timestamps_and_short_lifetime(self):
        claims = self.claims()
        now = claims["iat"]
        mutations = [{"iat": True}, {"exp": True}, {"iat": float(now)}, {"exp": str(now + 60)},
                     {"exp": now - 1}, {"iat": now + 10}, {"exp": now + 61},
                     {"nbf": now + 10}, {"nbf": False}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.sign({**claims, **mutation}))

    def test_mixed_or_ambiguous_principal_claim_types_rejected(self):
        mutations = [{"roles": "reader"}, {"roles": []}, {"roles": ["reader", "reader"]},
                     {"roles": ["reader", 1]}, {"tenant_id": ["alpha"]}, {"tenant_id": "alpha beta"},
                     {"client_id": 4}, {"scope": ["rag:query"]}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.sign({**self.claims(), **mutation}))

    def test_signed_duplicate_json_claims_still_rejected(self):
        raw = json.dumps(self.claims()).encode()
        raw = raw[:-1] + b',"tenant_id":"beta"}'
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(self.sign_raw(raw))

    def test_signed_escaped_surrogate_claim_rejected(self):
        raw = json.dumps({**self.claims(), "tenant_id": "\ud800"}).encode()
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(self.sign_raw(raw))

    def test_forged_signature_cannot_elevate_roles(self):
        original = self.sign().split(".")
        forged = ".".join([original[0], encoded({**self.claims(), "roles": ["analyst"]}), original[2]])
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(forged)

    def test_token_supplied_jku_does_not_make_network_request(self):
        # A real listening server would record a fetch. An unknown claim URL is
        # ignored while the configured startup key snapshot verifies the token.
        with issuer_http(self.jwks) as (origin, visits):
            token = self.sign(headers={"jku": origin + "/untrusted-keys"})
            self.verifier.verify(token)
            self.assertEqual(visits, [])

    def test_discovery_and_jwks_actual_http_requests_then_snapshot_validation(self):
        with issuer_http(self.jwks) as (origin, visits):
            verifier = JwtVerifier.discover(origin)
            token = self.sign({**self.claims(), "iss": origin})
            principal, _scopes = verifier.verify(token)
            self.assertEqual(principal.tenant_id, "alpha")
            self.assertEqual(visits, ["/.well-known/openid-configuration", "/jwks"])
        # Issuer is now stopped; offline JWT validation still uses the snapshot.
        self.assertEqual(verifier.verify(token)[0].tenant_id, "alpha")

    def test_metadata_redirects_and_other_origins_not_followed(self):
        for mode in ["redirect", "foreign-jwks", "wrong-issuer", "duplicate-json", "oversized"]:
            with self.subTest(mode=mode), issuer_http(self.jwks, mode=mode) as (origin, visits):
                with self.assertRaises(ValueError):
                    JwtVerifier.discover(origin)
                self.assertEqual(visits, ["/.well-known/openid-configuration"])

    def test_slow_trickle_metadata_has_total_deadline(self):
        with issuer_http(self.jwks, mode="slow-stream") as (origin, _visits):
            start = time.monotonic()
            with self.assertRaises((ValueError, TimeoutError)):
                JwtVerifier.discover(origin)
            self.assertLess(time.monotonic() - start, 4.5)


class RagHttpAdmissionReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = {**jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True),
                      "kid": "http-review-key", "alg": "RS256", "use": "sig"}

    def setUp(self):
        self.issuer = "http://127.0.0.1:8181"
        self.calls = []
        calls = self.calls

        class MustNotRun:
            def query(self, *args, **kwargs):
                calls.append((args, kwargs))
                raise RuntimeError("private fixture failure detail must not reach HTTP response")

        verifier = JwtVerifier(self.issuer, {"keys": [self.public]})
        app = create_app(verifier, MustNotRun())
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
            self.fail("Actual loopback RAG HTTP server did not start.")

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=3)
        self.socket.close()
        self.assertFalse(self.thread.is_alive())

    def token(self, *, tenant="alpha", roles=None, scope="rag:query"):
        now = int(time.time())
        return jwt.encode({"iss": self.issuer, "aud": RAG_AUDIENCE, "iat": now, "exp": now + 60,
                           "sub": "reader-alpha", "client_id": "reader-alpha", "jti": "http-review",
                           "tenant_id": tenant, "roles": ["reader"] if roles is None else roles, "scope": scope},
                          self.key, algorithm="RS256", headers={"kid": "http-review-key", "typ": "at+jwt"})

    def post(self, body=b'{"query":"What is the synthetic limit?","top_k":1}', *, headers=None,
             path="/v1/tenants/alpha/rag", with_token=True):
        pairs = [("content-type", "application/json")]
        if with_token:
            pairs.append(("authorization", "Bearer " + self.token()))
        if headers is not None:
            pairs = headers
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest("POST", path)
            for name, value in pairs:
                connection.putheader(name, value)
            connection.putheader("content-length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            connection.close()

    def test_unauthenticated_and_cross_tenant_requests_never_reach_retrieval(self):
        status, body, headers = self.post(with_token=False)
        self.assertEqual(status, 401)
        self.assertIn("www-authenticate", headers)
        self.assertNotIn("synthetic limit", body)
        status, _body, _headers = self.post(path="/v1/tenants/beta/rag")
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_denied_role_and_missing_scope_never_reach_retrieval(self):
        for token in [self.token(roles=["observer"]), self.token(scope="reports:read")]:
            status, _body, _headers = self.post(headers=[("content-type", "application/json"),
                                                        ("authorization", "Bearer " + token)])
            self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_duplicate_authorization_header_is_rejected(self):
        token = self.token()
        status, _body, _headers = self.post(headers=[("content-type", "application/json"),
                                                    ("authorization", "Bearer " + token),
                                                    ("Authorization", "Bearer " + token)])
        self.assertEqual(status, 401)
        self.assertEqual(self.calls, [])

    def test_identity_override_headers_and_fields_are_rejected(self):
        for name in ["X-Tenant-Id", "X-Roles", "X-Role", "X-User-Id"]:
            status, _body, _headers = self.post(headers=[("content-type", "application/json"),
                ("authorization", "Bearer " + self.token()), (name, "beta")])
            self.assertEqual(status, 400)
        for field in ["tenant_id", "roles", "principal"]:
            status, body, _headers = self.post(json.dumps({"query": "sentinel-private-query", field: "beta"}).encode())
            self.assertEqual(status, 422)
            self.assertNotIn("sentinel-private-query", body)
        self.assertEqual(self.calls, [])

    def test_malformed_ambiguous_or_oversized_json_never_reaches_retrieval(self):
        cases = [(b'{"query":"a","query":"b"}', 400),
                 (b'{"query":NaN}', 400), (b'{"query":"\\ud800"}', 400),
                 (b'{"query":"x","top_k":true}', 422), (b'{"query":"x","top_k":6}', 422),
                 (b" " * 16385, 413), (b"[]", 400)]
        for payload, expected in cases:
            with self.subTest(payload=payload[:40]):
                self.assertEqual(self.post(payload)[0], expected)
        self.assertEqual(self.calls, [])

    def test_unsupported_content_type_and_query_parameters_never_reach_retrieval(self):
        self.assertEqual(self.post(headers=[("content-type", "text/plain"),
                                           ("authorization", "Bearer " + self.token())])[0], 415)
        self.assertEqual(self.post(path="/v1/tenants/alpha/rag?tenant_id=beta")[0], 400)
        self.assertEqual(self.calls, [])

    def test_backend_failure_is_explicit_and_does_not_echo_private_details(self):
        status, body, headers = self.post()
        self.assertEqual(status, 503)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("rag_unavailable", body)
        self.assertNotIn("private fixture failure", body)
        self.assertEqual(headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
