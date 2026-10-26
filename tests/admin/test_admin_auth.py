"""Real signatures and bounded loopback discovery for the separate admin resource."""

from contextlib import contextmanager
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import jwt

from enterprise_ai_lab.admin.auth import (
    ADMIN_AUDIENCE,
    ADMIN_CLIENT,
    AdminPrincipal,
    AdminTokenVerifier,
)
from enterprise_ai_lab.rag.auth import AuthenticationError


def encoded(value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@contextmanager
def discovery(jwks, mode="valid"):
    visits = []
    origin = ""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            visits.append(self.path)
            if self.path == "/.well-known/openid-configuration":
                body = dict(
                    issuer=origin,
                    jwks_uri=origin + "/jwks",
                    authorization_endpoint=origin + "/auth",
                    token_endpoint=origin + "/token",
                )
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("location", origin + "/untrusted")
                    self.end_headers()
                    return
                if mode == "foreign-keys":
                    body["jwks_uri"] = "http://127.0.0.1:1/jwks"
                if mode == "foreign-token":
                    body["token_endpoint"] = "http://127.0.0.1:1/token"
                if mode == "query-authorization":
                    body["authorization_endpoint"] += "?resource=review"
                if mode == "issuer":
                    body["issuer"] += "/other"
            elif self.path == "/jwks":
                body = jwks
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield origin, visits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class AdminTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True)
        cls.jwks = {
            "keys": [{**public, "kid": "admin-key", "alg": "RS256", "use": "sig"}]
        }

    def setUp(self):
        self.verifier = AdminTokenVerifier("http://127.0.0.1:19991", self.jwks)
        now = int(time.time())
        # Freeze each test's baseline once: clock crossings cannot turn a 601s
        # mutation into an accidentally valid 600s lifetime.
        self.claims = dict(
            iss=self.verifier.issuer,
            aud=ADMIN_AUDIENCE,
            iat=now,
            exp=now + 300,
            sub="admin-alpha",
            client_id=ADMIN_CLIENT,
            jti="signed-test-request",
            tenant_id="alpha",
            roles=["config_admin", "reader"],
            identity_kind="user",
            scope="admin:read admin:configure admin:audit",
        )

    def token(self, changes=None, headers=None, claims=None):
        value = {**(self.claims if claims is None else claims), **(changes or {})}
        return jwt.encode(
            value,
            self.key,
            algorithm="RS256",
            headers={"kid": "admin-key", "typ": "at+jwt", **(headers or {})},
        )

    def test_subject_tenant_and_scopes_are_preserved(self):
        principal, scopes = self.verifier.verify(self.token())
        self.assertEqual(
            principal,
            AdminPrincipal(
                "alpha",
                ("config_admin", "reader"),
                "admin-alpha",
                expires_at=self.claims["exp"],
            ),
        )
        self.assertEqual(scopes, ("admin:read", "admin:configure", "admin:audit"))
        beta, _ = self.verifier.verify(
            self.token({"sub": "admin-beta", "tenant_id": "beta"})
        )
        self.assertEqual(beta.tenant_id, "beta")

    def test_auditor_and_observer_have_only_their_signed_permissions(self):
        principal, scopes = self.verifier.verify(
            self.token(
                {
                    "sub": "auditor-alpha",
                    "roles": ["config_auditor", "reader"],
                    "scope": "admin:read admin:audit",
                }
            )
        )
        self.assertEqual(principal.roles, ("config_auditor", "reader"))
        self.assertNotIn("admin:configure", scopes)
        principal, scopes = self.verifier.verify(
            self.token({"roles": ["observer"], "scope": ""})
        )
        self.assertEqual((principal.roles, scopes), (("observer",), ()))

    def test_other_resources_clients_and_service_tokens_are_rejected(self):
        for changes in [
            {"aud": value}
            for value in [
                "urn:identity-boundary:review",
                "urn:identity-boundary:rag",
                "urn:identity-boundary:approval",
                "urn:identity-boundary:privacy",
                [ADMIN_AUDIENCE],
            ]
        ] + [
            {"client_id": "review-portal"},
            {"client_id": "reader-alpha"},
            {"identity_kind": "service"},
            {"sub": ADMIN_CLIENT},
            {"identity_kind": None},
        ]:
            with self.subTest(changes=changes), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.token(changes))

    def test_missing_or_ambiguous_identity_claims_fail_closed(self):
        for name in [
            "iss",
            "aud",
            "exp",
            "iat",
            "sub",
            "client_id",
            "jti",
            "tenant_id",
            "roles",
        ]:
            claims = dict(self.claims)
            del claims[name]
            with self.subTest(missing=name), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.token(claims=claims))
        for changes in [
            {"roles": []},
            {"roles": "reader"},
            {"roles": ["config_admin", "config_admin"]},
            {"roles": ["config_admin", 2]},
            {"roles": ["approver", "reader"]},
            {"tenant_id": ["alpha"]},
            {"tenant_id": "alpha beta"},
            {"sub": "../alice"},
            {"scope": ["admin:read"]},
            {"scope": "admin:read admin:read"},
            {"scope": "review:read"},
            {"roles": ["config_auditor"], "scope": "admin:configure"},
            {"roles": ["reader"], "scope": "admin:read"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.token(changes))

    def test_timestamp_types_expiry_and_maximum_lifetime(self):
        now = self.claims["iat"]
        for changes in [
            {"iat": True},
            {"exp": True},
            {"iat": float(now)},
            {"exp": str(now + 300)},
            {"exp": now - 1},
            {"iat": now + 60},
            {"exp": now + 601},
            {"nbf": now + 60},
            {"nbf": False},
            {"iat": now - 601, "exp": now + 1},
        ]:
            with self.subTest(changes=changes), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.token(changes))
        self.assertEqual(
            self.verifier.verify(self.token({"exp": now + 600}))[0].expires_at,
            now + 600,
        )

    def test_critical_headers_key_urls_wrong_types_and_unknown_keys_rejected(self):
        for headers in [
            {"typ": "JWT"},
            {"kid": "unknown"},
            {"jku": "http://127.0.0.1:1/key"},
            {"jwk": {}},
            {"x5u": "http://127.0.0.1:1/key"},
            {"x5c": []},
            {"crit": ["roles"]},
            {"b64": True},
        ]:
            with self.subTest(headers=headers), self.assertRaises(AuthenticationError):
                token = self.token(headers=headers)
                if "b64" in headers:
                    # PyJWT removes explicit b64:true during encode; sign the
                    # actual forbidden header instead of testing a stripped one.
                    head = {
                        "alg": "RS256",
                        "typ": "at+jwt",
                        "kid": "admin-key",
                        **headers,
                    }
                    message = (encoded(head) + "." + encoded(self.claims)).encode()
                    signature = self.key.sign(
                        message, padding.PKCS1v15(), hashes.SHA256()
                    )
                    token = message.decode() + "." + encoded(signature)
                self.verifier.verify(token)

    def test_tampered_signature_and_algorithm_confusion_rejected(self):
        original = self.token().split(".")
        tampered = ".".join(
            [original[0], encoded({**self.claims, "tenant_id": "beta"}), original[2]]
        )
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(tampered)
        wrong = jwt.encode(
            self.claims,
            "synthetic-hmac-secret-at-least-thirty-two-bytes",
            algorithm="HS256",
            headers={"kid": "admin-key", "typ": "at+jwt"},
        )
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(wrong)

    def test_signed_duplicate_json_and_surrogate_claims_rejected(self):
        for raw in [
            json.dumps(self.claims).encode()[:-1] + b',"tenant_id":"beta"}',
            json.dumps({**self.claims, "sub": "\ud800"}).encode(),
        ]:
            message = (
                encoded({"alg": "RS256", "typ": "at+jwt", "kid": "admin-key"})
                + "."
                + encoded(raw)
            ).encode()
            signature = self.key.sign(message, padding.PKCS1v15(), hashes.SHA256())
            with self.assertRaises(AuthenticationError):
                self.verifier.verify(message.decode() + "." + encoded(signature))

    def test_id_token_binds_nonce_user_client_and_recent_authentication(self):
        now = self.claims["iat"]
        claims = dict(
            iss=self.verifier.issuer,
            aud=ADMIN_CLIENT,
            sub="admin-alpha",
            iat=now,
            exp=now + 300,
            auth_time=now,
            nonce="a" * 32,
        )
        token = self.token(claims=claims, headers={"typ": "JWT"})
        self.assertEqual(
            self.verifier.verify_id_token(token, "a" * 32, "admin-alpha")["sub"],
            "admin-alpha",
        )
        for changes in [
            {"aud": "review-portal"},
            {"auth_time": True},
            {"auth_time": now + 1},
            {"auth_time": now - 600},
            {"auth_time": None},
            {"exp": now + 601},
        ]:
            with self.subTest(changes=changes), self.assertRaises(AuthenticationError):
                self.verifier.verify_id_token(
                    self.token(changes, {"typ": "JWT"}, claims), "a" * 32, "admin-alpha"
                )
        for nonce, subject in [("b" * 32, "admin-alpha"), ("a" * 32, "admin-beta")]:
            with self.subTest(subject=subject), self.assertRaises(AuthenticationError):
                self.verifier.verify_id_token(token, nonce, subject)
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(token)
        with self.assertRaises(AuthenticationError):
            self.verifier.verify_id_token(self.token(), "a" * 32, "admin-alpha")

    def test_discovery_uses_actual_http_then_frozen_keys_offline(self):
        with discovery(self.jwks) as (origin, visits):
            verifier = AdminTokenVerifier.discover(origin)
            token = self.token({"iss": origin})
            self.assertEqual(verifier.verify(token)[0].subject, "admin-alpha")
            self.assertEqual(visits, ["/.well-known/openid-configuration", "/jwks"])
        self.assertEqual(verifier.verify(token)[0].tenant_id, "alpha")

    def test_discovery_refuses_redirect_and_foreign_or_ambiguous_endpoints(self):
        for mode in [
            "redirect",
            "foreign-keys",
            "foreign-token",
            "query-authorization",
            "issuer",
        ]:
            with (
                self.subTest(mode=mode),
                discovery(self.jwks, mode) as (origin, visits),
            ):
                with self.assertRaises(ValueError):
                    AdminTokenVerifier.discover(origin)
                self.assertNotIn("/untrusted", visits)

    def test_direct_principal_cannot_relabel_review_or_accept_mutable_roles(self):
        for changes in [
            {"client_id": "review-portal"},
            {"roles": ["config_admin"]},
            {"roles": ("approver",)},
            {"roles": ("config_admin", "config_admin")},
            {"expires_at": True},
            {"tenant_id": "../alpha"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                AdminPrincipal(
                    **{
                        **dict(
                            tenant_id="alpha",
                            roles=("config_admin", "reader"),
                            subject="admin-alpha",
                        ),
                        **changes,
                    }
                )


if __name__ == "__main__":
    unittest.main()
