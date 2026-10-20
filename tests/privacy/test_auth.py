"""Dedicated privacy audience with actual RS256 tokens and controlled fixture keys."""
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from enterprise_ai_lab.rag import auth


RESOURCE = 'urn:identity-boundary:privacy'


class PrivacyJwtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = {**jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True),
                      'kid': 'privacy-controlled-key', 'alg': 'RS256', 'use': 'sig'}
        cls.jwks = {'keys': [cls.public]}

    def setUp(self):
        self.issuer = 'http://127.0.0.1:8181'

    def claims(self, **changes):
        now = int(time.time())
        return {'iss': self.issuer, 'aud': RESOURCE, 'iat': now, 'exp': now + 60,
                'sub': 'privacy-alpha', 'client_id': 'privacy-alpha', 'jti': 'privacy-auth-case',
                'tenant_id': 'alpha', 'roles': ['redactor', 'restorer'],
                'scope': 'privacy:redact privacy:restore', **changes}

    def sign(self, claims=None, *, key=None, headers=None):
        return jwt.encode(self.claims() if claims is None else claims,
                          self.key if key is None else key, algorithm='RS256',
                          headers={'kid': 'privacy-controlled-key', 'typ': 'at+jwt', **(headers or {})})

    def verifier(self, *, audience=RESOURCE):
        return auth.JwtVerifier(self.issuer, self.jwks, audience=audience)

    def test_privacy_audience_requires_explicit_opt_in_and_verifies_signed_identity(self):
        self.assertEqual(getattr(auth, 'PRIVACY_AUDIENCE', None), RESOURCE)
        verifier = self.verifier()
        for tenant, client, roles, scope in [
            ('alpha', 'privacy-alpha', ['redactor', 'restorer'], 'privacy:redact privacy:restore'),
            ('beta', 'privacy-beta', ['redactor', 'restorer'], 'privacy:redact privacy:restore'),
            ('alpha', 'privacy-other-alpha', ['redactor', 'restorer'], 'privacy:redact privacy:restore'),
            ('alpha', 'privacy-redactor-alpha', ['redactor'], 'privacy:redact'),
        ]:
            with self.subTest(client=client):
                principal, scopes = verifier.verify(self.sign(self.claims(
                    tenant_id=tenant, client_id=client, sub=client, roles=roles, scope=scope)))
                self.assertEqual(principal.tenant_id, tenant)
                self.assertEqual(principal.client_id, client)
                self.assertEqual(principal.roles, tuple(roles))
                self.assertEqual(scopes, tuple(scope.split()))

    def test_default_rag_and_explicit_approval_verifiers_reject_privacy_and_reverse(self):
        privacy = self.sign()
        default = auth.JwtVerifier(self.issuer, self.jwks)
        self.assertEqual(default.audience, auth.RAG_AUDIENCE)
        for other in [default, self.verifier(audience=auth.APPROVAL_AUDIENCE)]:
            with self.subTest(audience=other.audience):
                with self.assertRaises(auth.AuthenticationError):
                    other.verify(privacy)
                token = self.sign(self.claims(aud=other.audience))
                with self.assertRaises(auth.AuthenticationError):
                    self.verifier().verify(token)
                self.assertEqual(other.verify(token)[0].client_id, 'privacy-alpha')

    def test_strict_single_resource_audience_and_subject_identity(self):
        verifier = self.verifier()
        for change in [{'aud': [RESOURCE]}, {'aud': [RESOURCE, auth.RAG_AUDIENCE]},
                       {'aud': 'urn:identity-boundary:reports:jwt'},
                       {'iss': 'http://127.0.0.1:8182'}, {'sub': 'privacy-other-alpha'}]:
            with self.subTest(change=change), self.assertRaises(auth.AuthenticationError):
                verifier.verify(self.sign(self.claims(**change)))
        with self.assertRaises(ValueError):
            self.verifier(audience='urn:identity-boundary:privacy:unknown')

    def test_expired_future_and_over_sixty_second_privacy_tokens_fail_closed(self):
        verifier = self.verifier()
        now = int(time.time())
        for change in [{'iat': now - 61, 'exp': now + 1}, {'exp': now + 61},
                       {'iat': now - 60, 'exp': now}, {'iat': now + 1, 'exp': now + 60},
                       {'iat': float(now)}, {'exp': str(now + 60)}, {'exp': True}]:
            with self.subTest(change=change), self.assertRaises(auth.AuthenticationError):
                verifier.verify(self.sign(self.claims(**change)))

    def test_signature_and_algorithm_cannot_be_replaced(self):
        verifier = self.verifier()
        unrelated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(auth.AuthenticationError):
            verifier.verify(self.sign(key=unrelated))
        token = jwt.encode(self.claims(), b'controlled-hmac-key-32-bytes-long!', algorithm='HS256',
                           headers={'kid': 'privacy-controlled-key', 'typ': 'at+jwt'})
        with self.assertRaises(auth.AuthenticationError):
            verifier.verify(token)
        with self.assertRaises(auth.AuthenticationError):
            verifier.verify(self.sign(headers={'typ': 'JWT'}))

    def test_roles_tenant_and_scope_have_strict_types(self):
        verifier = self.verifier()
        for change in [{'roles': []}, {'roles': ['redactor', 'redactor']},
                       {'roles': 'redactor'}, {'roles': ['redactor', 1]},
                       {'tenant_id': ['alpha']}, {'client_id': False},
                       {'scope': ['privacy:restore']}]:
            with self.subTest(change=change), self.assertRaises(auth.AuthenticationError):
                verifier.verify(self.sign(self.claims(**change)))

    def test_authentication_does_not_grant_resource_operation_or_rag_permission(self):
        principal, scopes = self.verifier().verify(self.sign(self.claims(roles=['observer'], scope='')))
        self.assertEqual(principal.roles, ('observer',))
        self.assertEqual(scopes, ())
        # Resource-specific privacy role/scope decisions belong to its edge.
        with self.assertRaises(auth.AuthorizationError):
            auth.authorize(principal, scopes, 'alpha')


if __name__ == '__main__':
    unittest.main()
