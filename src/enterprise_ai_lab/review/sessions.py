"""Ephemeral opaque browser sessions. Tokens never enter browser JavaScript."""
from dataclasses import dataclass
import hashlib
import secrets
import time


@dataclass
class BrowserSession:
    token: str
    csrf: str
    expires_at: int


class Sessions:
    def __init__(self):
        self.sessions, self.logins = {}, {}

    def _prune(self):
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v.expires_at > now}
        self.logins = {k: v for k, v in self.logins.items() if v["expires_at"] > now}

    @staticmethod
    def key(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def login(self):
        self._prune()
        if len(self.logins) >= 64:
            raise RuntimeError("local login capacity exceeded")
        state, binding, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
        self.logins[state] = {"binding": self.key(binding), "verifier": verifier,
            "nonce": nonce, "expires_at": time.time() + 180}
        return state, binding, verifier, nonce

    def consume_login(self, state, binding):
        self._prune()
        if not isinstance(state, str) or not isinstance(binding, str):
            raise ValueError("invalid login binding")
        value = self.logins.get(state)
        if value is None or not secrets.compare_digest(value["binding"], self.key(binding)):
            raise ValueError("invalid login binding")
        del self.logins[state]
        return value

    def create(self, token, expires_at):
        self._prune()
        if len(self.sessions) >= 64 or type(expires_at) is not int or not time.time() < expires_at <= time.time() + 600:
            raise ValueError("invalid bounded session")
        identifier = secrets.token_urlsafe(32)
        self.sessions[self.key(identifier)] = BrowserSession(token, secrets.token_urlsafe(32), expires_at)
        return identifier

    def get(self, identifier):
        self._prune()
        if not isinstance(identifier, str) or not 20 <= len(identifier) <= 128:
            raise ValueError("missing session")
        value = self.sessions.get(self.key(identifier))
        if value is None:
            raise ValueError("expired or unknown session")
        return value

    def remove(self, identifier):
        if isinstance(identifier, str):
            self.sessions.pop(self.key(identifier), None)
