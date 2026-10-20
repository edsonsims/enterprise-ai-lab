"""Trusted-local synthetic redaction and scoped restoration primitives."""
from .contracts import PrivacyError, Span
from .detector import detect
from .vault import Vault

__all__ = ["PrivacyError", "Span", "detect", "Vault"]
