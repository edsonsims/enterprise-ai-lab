"""Finite contracts for a bounded, trusted-local privacy study."""
from __future__ import annotations

from dataclasses import dataclass
import re

MAX_TEXT_BYTES = 32768
TOKEN_PREFIX = "[[PII:"
TOKEN_RE = re.compile(r"\[\[PII:([0-9a-f]{32})\]\]")
HEX_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
KINDS = ("EMAIL", "PAN", "PHONE")
ERROR_CODES = frozenset({"invalid_input", "invalid_scope", "invalid_token", "context_unavailable",
                         "capacity_exceeded", "busy", "storage_unavailable"})
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


class PrivacyError(Exception):
    """Expose a finite code only, including when constructed incorrectly."""
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in ERROR_CODES else "storage_unavailable"
        super().__init__(self.code)

    def __repr__(self):
        return f"PrivacyError({self.code!r})"


@dataclass(frozen=True)
class Span:
    kind: str
    start: int
    end: int


def validate_text(text, *, original: bool = False) -> str:
    if type(text) is not str:
        raise PrivacyError("invalid_input")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise PrivacyError("invalid_input") from None
    if len(encoded) > MAX_TEXT_BYTES or (original and TOKEN_PREFIX in text):
        raise PrivacyError("invalid_input")
    return text


def validate_scope(tenant, client) -> None:
    if any(type(value) is not str or _SCOPE.fullmatch(value) is None for value in (tenant, client)):
        raise PrivacyError("invalid_scope")


def validate_id(value) -> str:
    if type(value) is not str or HEX_ID_RE.fullmatch(value) is None:
        raise PrivacyError("invalid_token")
    return value
