"""Narrow deterministic detectors; this is not universal PII identification."""
from __future__ import annotations

import re

from .contracts import Span, validate_text

# Unicode-aware boundaries stop ASCII-looking suffixes of non-ASCII names from
# being presented as whole email addresses. The address itself remains ASCII.
_LOCAL = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL = re.compile(r"(?<![\w.!#$%&'*+/=?^_`{|}~@-])(" + _LOCAL + r"@(?:" + _LABEL
                    + r"\.)+[A-Za-z]{2,63})(?![\w@-]|\.[\w-])")
_PHONE = re.compile(r"(?<![\w+])\+1(?:[ .-]?[0-9]{3}|[ .-]?\([0-9]{3}\))"
                    r"[ .-]?[0-9]{3}[ .-]?[0-9]{4}(?!\w)")
_PAN = re.compile(r"(?<!\w)(?<![0-9][ -])(?:[0-9][ -]?){12,18}[0-9](?!\w|[ -]?[0-9])")


def _luhn(value: str) -> bool:
    digits = [int(char) for char in value if "0" <= char <= "9"]
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return 13 <= len(digits) <= 19 and total % 10 == 0


def detect(text: str) -> tuple[Span, ...]:
    """Return non-overlapping Python-code-point spans, EMAIL then PAN then PHONE."""
    validate_text(text, original=True)
    candidates = []
    for match in _EMAIL.finditer(text):
        value = match.group()
        local, domain = value.rsplit("@", 1)
        if len(local) <= 64 and len(domain) <= 253 and not local.startswith(".") and not local.endswith(".") and ".." not in local:
            candidates.append((0, Span("EMAIL", match.start(), match.end())))
    for match in _PAN.finditer(text):
        if _luhn(match.group()):
            candidates.append((1, Span("PAN", match.start(), match.end())))
    candidates.extend((2, Span("PHONE", match.start(), match.end())) for match in _PHONE.finditer(text))
    accepted = []
    occupied = bytearray(len(text))
    for _, span in sorted(candidates, key=lambda item: (item[0], -(item[1].end-item[1].start), item[1].start)):
        if not any(occupied[span.start:span.end]):
            accepted.append(span)
            occupied[span.start:span.end] = b"\x01" * (span.end-span.start)
    return tuple(sorted(accepted, key=lambda span: (span.start, span.end, span.kind)))
