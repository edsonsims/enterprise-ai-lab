"""Versioned publication snapshots, separate from the original note workflow."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from ..approval.contracts import (
    ApprovalValidationError, DecisionCommand, DecisionReceipt, MAX_LIFETIME_SECONDS,
    format_utc, identifier, parse_utc, validate_decision,
)


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_REVISION = (1 << 63) - 2


class PublicationValidationError(ApprovalValidationError):
    """A publication's immutable content or approval receipt is invalid."""


def digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise PublicationValidationError("invalid publication digest")
    return value


def revision(value: object) -> int:
    # SQLite stores signed 64-bit revisions; leave room for the next commit.
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise PublicationValidationError("invalid publication revision")
    return value


@dataclass(frozen=True)
class CandidateMetadata:
    """Reviewer metadata derived from the complete immutable stored documents.

    These summaries never contain document text. Their candidate digest binds
    the full documents, including text, ACLs and native/OCR provenance.
    """

    tenant_id: str
    candidate_sha256: str
    document_count: int
    document_ids: tuple[str, ...]
    documents: tuple[dict, ...]


def snapshot_digest(
    request_id: str, tenant_id: str, requested_by: str, candidate_sha256: str,
    expected_revision: int, expires_at: str,
) -> str:
    identifier(request_id)
    identifier(tenant_id)
    identifier(requested_by)
    digest(candidate_sha256)
    revision(expected_revision)
    parse_utc(expires_at)
    value = {
        "schema": 1,
        "action": "publish_rag_documents",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "requested_by": requested_by,
        "candidate_sha256": candidate_sha256,
        "expected_revision": expected_revision,
        "expires_at": expires_at,
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PublicationRequest:
    request_id: str
    tenant_id: str
    requested_by: str
    candidate_sha256: str
    expected_revision: int
    expires_at: str
    snapshot_sha256: str

    def validate(self) -> None:
        expected = snapshot_digest(
            self.request_id, self.tenant_id, self.requested_by,
            self.candidate_sha256, self.expected_revision, self.expires_at,
        )
        if self.snapshot_sha256 != expected:
            raise PublicationValidationError("publication snapshot digest mismatch")


def make_request(
    request_id: str, tenant_id: str, requested_by: str, candidate_sha256: str,
    expected_revision: int, expires_at: str,
) -> PublicationRequest:
    return PublicationRequest(
        request_id, tenant_id, requested_by, candidate_sha256, expected_revision,
        expires_at, snapshot_digest(request_id, tenant_id, requested_by,
                                    candidate_sha256, expected_revision, expires_at),
    )


@dataclass(frozen=True)
class ApplyApprovedDocuments:
    request: PublicationRequest
    receipt: DecisionReceipt


@dataclass(frozen=True)
class PublicationEffect:
    effect_key: str
    tenant_id: str
    request_id: str
    snapshot_sha256: str
    candidate_sha256: str
    revision: int
    document_count: int
    chunk_count: int


@dataclass(frozen=True)
class PublicationSnapshot:
    request: PublicationRequest
    status: str
    publication_status: str
    receipt: DecisionReceipt | None = None
    effect: PublicationEffect | None = None


def validate_effect(value: ApplyApprovedDocuments) -> None:
    if (not isinstance(value, ApplyApprovedDocuments)
            or not isinstance(value.request, PublicationRequest)
            or not isinstance(value.receipt, DecisionReceipt)
            or not isinstance(value.receipt.command, DecisionCommand)):
        raise PublicationValidationError("invalid approved document publication")
    request, receipt = value.request, value.receipt
    request.validate()
    # The shared validator checks signed tenant, exact snapshot, approver role,
    # a distinct requester and acceptance strictly before the fixed deadline.
    validate_decision(request, receipt.command, parse_utc(receipt.accepted_at))
    if receipt.command.decision != "approve":
        raise PublicationValidationError("only approved documents may be published")
