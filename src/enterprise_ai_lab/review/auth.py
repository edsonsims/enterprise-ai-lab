"""Dedicated user access tokens; service credentials cannot establish a person."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hmac
import re
import time
from urllib.parse import urlsplit

import jwt

from ..rag.auth import (
    AuthenticationError, JwtVerifier, fetch_object, local_origin, strict_json,
)
from ..rag.contracts import RagPrincipal, valid_identifier

REVIEW_AUDIENCE = "urn:identity-boundary:review"
REVIEW_CLIENT = "review-portal"
_COMPACT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True)
class HumanPrincipal:
    tenant_id: str
    roles: tuple[str, ...]
    subject: str
    client_id: str = REVIEW_CLIENT
    expires_at: int | None = None

    def __post_init__(self):
        if self.client_id != REVIEW_CLIENT or not valid_identifier(self.subject):
            raise ValueError("invalid review user identity")
        # Preserve the signed account subject as the workflow actor. Using the
        # shared OAuth application client ID would collapse all users into one.
        self.publication_principal()

    def publication_principal(self) -> RagPrincipal:
        return RagPrincipal(self.tenant_id, self.roles, self.subject)


class HumanTokenVerifier:
    """Startup public keys, strict short-lived RS256 user-token verification.

    The existing client-credentials verifier remains unchanged. Its public-key
    parser is reused, while this audience has separate account-claim rules.
    """

    audience = REVIEW_AUDIENCE

    def __init__(self, issuer: str, jwks: dict):
        parser = JwtVerifier(issuer, jwks)
        self.issuer, self._keys = parser.issuer, parser._keys
        self.authorization_endpoint = self.issuer + "/auth"
        self.token_endpoint = self.issuer + "/token"

    @classmethod
    def discover(cls, issuer: str) -> "HumanTokenVerifier":
        origin = local_origin(issuer)
        metadata = fetch_object(origin + "/.well-known/openid-configuration")
        if metadata.get("issuer") != origin or not isinstance(metadata.get("jwks_uri"), str):
            raise ValueError("issuer discovery mismatch")
        target = urlsplit(metadata["jwks_uri"])
        if (target.scheme != "http" or target.hostname != "127.0.0.1"
                or target.port != urlsplit(origin).port or target.username or target.password
                or target.query or target.fragment):
            raise ValueError("issuer keys must stay on the configured origin")
        instance = cls(origin, fetch_object(metadata["jwks_uri"]))
        for name in ("authorization_endpoint", "token_endpoint"):
            endpoint = metadata.get(name)
            if not isinstance(endpoint, str):
                raise ValueError("issuer omitted its browser authentication endpoint")
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                    or parsed.port != urlsplit(origin).port or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError("browser authentication endpoints must stay on the issuer origin")
            setattr(instance, name, endpoint)
        return instance

    def verify_id_token(self, token: str, nonce: str, subject: str) -> dict:
        """Bind an OIDC callback to the browser's state/nonce and access user.

        PKCE and state verification remain the caller's responsibility. ID
        tokens are accepted only here; API sessions require the separate user
        access token on every request.
        """
        try:
            if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                    or not _COMPACT.fullmatch(token) or not valid_identifier(subject)
                    or not isinstance(nonce, str) or not 16 <= len(nonce) <= 256):
                raise ValueError("unsupported ID token")
            header, proposed = [strict_json(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                                for part in token.split(".")[:2]]
            if (not isinstance(header, dict) or not isinstance(proposed, dict)
                    or header.get("alg") != "RS256" or header.get("typ") not in (None, "JWT")
                    or header.get("kid") not in self._keys
                    or any(name in header for name in ("crit", "b64", "jku", "jwk", "x5u", "x5c"))):
                raise ValueError("unsupported ID token header")
            claims = jwt.decode(token, self._keys[header["kid"]], algorithms=["RS256"],
                                issuer=self.issuer, audience=REVIEW_CLIENT, leeway=0,
                                options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce", "auth_time"],
                                         "strict_aud": True})
            now = time.time()
            if (claims != proposed or claims.get("iss") != self.issuer or claims.get("aud") != REVIEW_CLIENT
                    or claims["sub"] != subject or not isinstance(claims["nonce"], str)
                    or not hmac.compare_digest(claims["nonce"], nonce)
                    or type(claims["iat"]) is not int or type(claims["exp"]) is not int
                    or claims["iat"] > now or claims["exp"] <= now
                    or not 0 < claims["exp"] - claims["iat"] <= 600 or now - claims["iat"] >= 600
                    or type(claims["auth_time"]) is not int
                    or not claims["iat"] - 600 < claims["auth_time"] <= claims["iat"]
                    or claims["auth_time"] > now or now - claims["auth_time"] >= 600
                    or ("nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now))):
                raise ValueError("invalid ID token callback binding")
            return claims
        except (ValueError, TypeError, KeyError, jwt.PyJWTError, UnicodeError, RecursionError) as exc:
            raise AuthenticationError("The ID token does not match this browser login.") from exc

    def verify(self, token: str) -> tuple[HumanPrincipal, tuple[str, ...]]:
        try:
            if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                    or not _COMPACT.fullmatch(token)):
                raise ValueError("unsupported compact token")
            header, proposed = [strict_json(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                                for part in token.split(".")[:2]]
            if (not isinstance(header, dict) or not isinstance(proposed, dict)
                    or header.get("alg") != "RS256" or header.get("typ") != "at+jwt"
                    or header.get("kid") not in self._keys
                    or any(name in header for name in ("crit", "b64", "jku", "jwk", "x5u", "x5c"))):
                raise ValueError("unsupported user token header")
            claims = jwt.decode(token, self._keys[header["kid"]], algorithms=["RS256"],
                                issuer=self.issuer, audience=self.audience, leeway=0,
                                options={"require": ["iss", "aud", "exp", "iat", "sub", "client_id", "jti"],
                                         "strict_aud": True})
            now = time.time()
            if (claims != proposed or claims.get("iss") != self.issuer
                    or claims.get("aud") != self.audience
                    or claims.get("identity_kind") != "user"
                    or claims.get("client_id") != REVIEW_CLIENT
                    or type(claims["exp"]) is not int or type(claims["iat"]) is not int
                    or claims["iat"] > now or claims["exp"] <= now
                    or not 0 < claims["exp"] - claims["iat"] <= 600 or now - claims["iat"] >= 600
                    or ("nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now))
                    or not isinstance(claims["jti"], str) or not 1 <= len(claims["jti"]) <= 128
                    or not valid_identifier(claims["sub"]) or claims["sub"] == REVIEW_CLIENT
                    or not isinstance(claims.get("roles"), list)
                    or not 1 <= len(claims["roles"]) <= 8
                    or not all(valid_identifier(role) for role in claims["roles"])
                    or len(set(claims["roles"])) != len(claims["roles"])
                    or not isinstance(claims.get("scope", ""), str)
                    or len(claims.get("scope", "")) > 1024):
                raise ValueError("invalid review account claims")
            principal = HumanPrincipal(claims["tenant_id"], tuple(claims["roles"]), claims["sub"], expires_at=claims["exp"])
            scopes = tuple(claims.get("scope", "").split())
            if (len(scopes) != len(set(scopes)) or len(scopes) > 16
                    or any(not valid_identifier(scope) for scope in scopes)):
                raise ValueError("invalid review scopes")
            return principal, scopes
        except (ValueError, TypeError, KeyError, jwt.PyJWTError, UnicodeError, RecursionError) as exc:
            raise AuthenticationError("The token is invalid for the human review resource.") from exc
