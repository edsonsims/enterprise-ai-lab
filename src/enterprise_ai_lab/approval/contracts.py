"""Bounded immutable approval snapshots; no tokens or external destinations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
MAX_LIFETIME_SECONDS = 86_400


class ApprovalValidationError(ValueError):
    """A malformed request, unauthorised decision, or conflicting replay."""


def identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ApprovalValidationError("invalid identifier")
    return value


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise ApprovalValidationError("timestamp must be UTC with second precision")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ApprovalValidationError("invalid UTC timestamp") from exc


def format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalValidationError("timestamp requires a timezone")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ApprovalValidationError("invalid note text")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ApprovalValidationError("invalid note text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ApprovalValidationError("invalid note text") from exc
    return value


@dataclass(frozen=True)
class LocalNote:
    title: str
    body: str

    def validate(self) -> None:
        _bounded_text(self.title, 160)
        _bounded_text(self.body, 4_000)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def snapshot_digest(
    request_id: str, tenant_id: str, requested_by: str, note: LocalNote, expires_at: str
) -> str:
    """Bind authority, content and deadline, rather than hashing only note text."""
    identifier(request_id)
    identifier(tenant_id)
    identifier(requested_by)
    if not isinstance(note, LocalNote):
        raise ApprovalValidationError("invalid local note")
    note.validate()
    parse_utc(expires_at)
    canonical = _canonical({
        "schema": 1,
        "action": "publish_local_note",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "requested_by": requested_by,
        "payload": asdict(note),
        "expires_at": expires_at,
    })
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    tenant_id: str
    requested_by: str
    payload: LocalNote
    expires_at: str
    snapshot_sha256: str

    def validate(self) -> None:
        expected = snapshot_digest(
            self.request_id, self.tenant_id, self.requested_by, self.payload, self.expires_at
        )
        if self.snapshot_sha256 != expected:
            raise ApprovalValidationError("snapshot digest mismatch")


def make_request(
    request_id: str, tenant_id: str, requested_by: str, note: LocalNote, expires_at: str
) -> ApprovalRequest:
    return ApprovalRequest(
        request_id, tenant_id, requested_by, note, expires_at,
        snapshot_digest(request_id, tenant_id, requested_by, note, expires_at),
    )


@dataclass(frozen=True)
class DecisionCommand:
    """Constructed only from gateway-verified identity, never from body identity."""

    command_id: str
    tenant_id: str
    snapshot_sha256: str
    decision: str
    actor_id: str
    actor_roles: tuple[str, ...]

    def validate(self) -> None:
        identifier(self.command_id)
        identifier(self.tenant_id)
        identifier(self.actor_id)
        if not isinstance(self.snapshot_sha256, str) or not _DIGEST.fullmatch(self.snapshot_sha256):
            raise ApprovalValidationError("invalid snapshot digest")
        if self.decision not in ("approve", "reject"):
            raise ApprovalValidationError("decision must be approve or reject")
        if (
            not isinstance(self.actor_roles, tuple)
            or not 1 <= len(self.actor_roles) <= 16
            or not all(isinstance(role, str) for role in self.actor_roles)
            or len(set(self.actor_roles)) != len(self.actor_roles)
        ):
            raise ApprovalValidationError("invalid actor roles")
        for role in self.actor_roles:
            identifier(role)


@dataclass(frozen=True)
class DecisionReceipt:
    command: DecisionCommand
    accepted_at: str


@dataclass(frozen=True)
class ApplyApprovedNote:
    request: ApprovalRequest
    receipt: DecisionReceipt


@dataclass(frozen=True)
class EffectReceipt:
    effect_key: str
    tenant_id: str
    request_id: str
    snapshot_sha256: str
    note_sha256: str


@dataclass(frozen=True)
class ApprovalSnapshot:
    request: ApprovalRequest
    status: str
    publication_status: str
    receipt: DecisionReceipt | None = None
    effect: EffectReceipt | None = None


def validate_decision(
    request: ApprovalRequest,
    command: DecisionCommand,
    now: datetime,
    existing: DecisionReceipt | None = None,
) -> None:
    request.validate()
    command.validate()
    if command.tenant_id != request.tenant_id or command.snapshot_sha256 != request.snapshot_sha256:
        raise ApprovalValidationError("decision does not match the immutable request")
    if "approver" not in command.actor_roles or command.actor_id == request.requested_by:
        raise ApprovalValidationError("independent approver required")
    if existing is not None:
        if command != existing.command:
            raise ApprovalValidationError("a different decision was already accepted")
        # An exact accepted command remains a replay after the deadline.
        return
    if now >= parse_utc(request.expires_at):
        raise ApprovalValidationError("approval request expired")


def validate_effect(value: ApplyApprovedNote) -> None:
    """The sink also checks the recorded receipt before touching SQLite."""
    if not isinstance(value, ApplyApprovedNote):
        raise ApprovalValidationError("invalid approved note")
    request, receipt = value.request, value.receipt
    request.validate()
    validate_decision(request, receipt.command, parse_utc(receipt.accepted_at))
    if receipt.command.decision != "approve":
        raise ApprovalValidationError("only approved notes may be applied")


def note_digest(note: LocalNote) -> str:
    note.validate()
    return hashlib.sha256(_canonical(asdict(note)).encode("utf-8")).hexdigest()
