"""Controlled boundary tests; process/OIDC/browser evidence is a separate suite."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest

from enterprise_ai_lab.approval.contracts import DecisionCommand, DecisionReceipt, format_utc, validate_decision
from enterprise_ai_lab.publication.contracts import make_request
from enterprise_ai_lab.publication.gateway import PublicationConflict, PublicationNotFound
from enterprise_ai_lab.publication.storage import CandidateRepository
from enterprise_ai_lab.rag.auth import AuthenticationError, AuthorizationError
from enterprise_ai_lab.rag.contracts import Document
from enterprise_ai_lab.review.audit import AuditIntegrityError, AuditLog
from enterprise_ai_lab.review.auth import HumanPrincipal, HumanTokenVerifier, REVIEW_AUDIENCE
from enterprise_ai_lab.review.core import HumanReviewService, ReviewNotFound


@pytest.fixture
def signing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public.update(kid="review-key", alg="RS256", use="sig")
    return key, HumanTokenVerifier("http://127.0.0.1:15551", {"keys": [public]})


def token(signing, **changes):
    key, _verifier = signing
    now = int(time.time())
    claims = dict(iss="http://127.0.0.1:15551", aud=REVIEW_AUDIENCE, iat=now, exp=now + 300,
                  sub="alice", client_id="review-portal", jti="safe-unique-id", identity_kind="user",
                  tenant_id="alpha", roles=["approver", "reader"], scope="review:read review:decide")
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "review-key", "typ": "at+jwt"})


def test_account_subject_survives_shared_browser_client(signing):
    alice, scopes = signing[1].verify(token(signing))
    bob, _ = signing[1].verify(token(signing, sub="bob"))
    assert alice.client_id == bob.client_id == "review-portal"
    assert alice.publication_principal().client_id == "alice"
    assert bob.publication_principal().client_id == "bob"
    assert alice.expires_at > time.time()
    assert scopes == ("review:read", "review:decide")


@pytest.mark.parametrize("changes", [
    {"identity_kind": "service"}, {"identity_kind": None}, {"sub": "review-portal"},
    {"client_id": "approver-alpha"}, {"aud": "urn:identity-boundary:approval"},
    {"aud": [REVIEW_AUDIENCE]}, {"sub": "a/b"}, {"tenant_id": "alpha beta"},
    {"roles": ["approver", "approver"]}, {"roles": []}, {"roles": "approver"},
    {"scope": "review:read review:read"}, {"scope": ["review:read"]},
    {"iat": lambda: int(time.time()) + 30}, {"exp": int(time.time()) - 1},
    {"exp": int(time.time()) + 3600}, {"exp": True},
])
def test_user_token_boundary_rejects_service_or_malformed_claims(signing, changes):
    changes = {key: value() if callable(value) else value for key, value in changes.items()}
    with pytest.raises(AuthenticationError):
        signing[1].verify(token(signing, **changes))


def test_id_token_nonce_subject_and_access_audience_are_distinct(signing):
    key, verifier = signing
    now = int(time.time())
    claims = dict(iss=verifier.issuer, aud="review-portal", iat=now, exp=now + 300,
                  sub="alice", nonce="a" * 32, auth_time=now)
    encoded = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "review-key", "typ": "JWT"})
    assert verifier.verify_id_token(encoded, "a" * 32, "alice")["sub"] == "alice"
    for nonce, subject in [("b" * 32, "alice"), ("a" * 32, "bob")]:
        with pytest.raises(AuthenticationError):
            verifier.verify_id_token(encoded, nonce, subject)
    with pytest.raises(AuthenticationError):
        verifier.verify(encoded)
    with pytest.raises(AuthenticationError):
        verifier.verify_id_token(token(signing), "a" * 32, "alice")


@pytest.mark.parametrize("auth_time", [None, True, "123", 0, "future"])
def test_id_token_requires_recent_integer_authentication_time(signing, auth_time):
    key, verifier = signing
    now = int(time.time())
    claims = dict(iss=verifier.issuer, aud="review-portal", iat=now, exp=now + 300,
                  sub="alice", nonce="a" * 32)
    if auth_time is not None:
        claims["auth_time"] = now + 5 if auth_time == "future" else auth_time
    encoded = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "review-key", "typ": "JWT"})
    with pytest.raises(AuthenticationError):
        verifier.verify_id_token(encoded, "a" * 32, "alice")


def test_audit_chain_idempotency_append_only_and_tamper_detection(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite")
    fields = dict(tenant_id="alpha", actor_id="alice", action="login", result="accepted", event_key="login-1")
    first = log.log(**fields)
    assert log.log(**fields) == first
    log.log(tenant_id="beta", actor_id="bob", action="logout", result="accepted")
    assert [row["actor_id"] for row in log.events("alpha")] == ["alice"]
    assert len(log.events()) == 2
    with pytest.raises(AuditIntegrityError):
        log.log(**{**fields, "actor_id": "bob"})
    with sqlite3.connect(log.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM review_audit")
        # Deliberate administrator corruption is a test hook, not an API.
        connection.execute("DROP TRIGGER review_audit_no_update")
        connection.execute("UPDATE review_audit SET event_json='{}' WHERE sequence=1")
    with pytest.raises(AuditIntegrityError):
        log.events()


class WorkflowDouble:
    """Controlled Temporal admission/receipt for crash-window unit tests only."""
    def __init__(self):
        self.values = {}
        self.decisions = 0

    async def submit(self, principal, request_id, candidate_sha256, expected_revision, expires_at):
        request = make_request(request_id, principal.tenant_id, principal.client_id,
                               candidate_sha256, expected_revision, expires_at)
        key = (principal.tenant_id, request_id)
        if key in self.values and self.values[key]["request"] != asdict(request):
            raise PublicationConflict("different immutable snapshot")
        return self.values.setdefault(key, {"request": asdict(request), "status": "pending",
                                            "publication_status": "not_applicable", "receipt": None, "effect": None})

    async def status(self, principal, request_id):
        try:
            return self.values[(principal.tenant_id, request_id)]
        except KeyError:
            raise PublicationNotFound("missing")

    async def decide(self, principal, request_id, command_id, snapshot_sha256, decision):
        value = await self.status(principal, request_id)
        request = make_request(**{key: child for key, child in value["request"].items() if key != "snapshot_sha256"})
        command = DecisionCommand(command_id, principal.tenant_id, snapshot_sha256,
                                  decision, principal.client_id, principal.roles)
        existing = None
        if value["receipt"]:
            raw = value["receipt"]["command"]
            existing = DecisionReceipt(DecisionCommand(**{**raw, "actor_roles": tuple(raw["actor_roles"])}),
                                       value["receipt"]["accepted_at"])
        now = datetime.now(timezone.utc)
        try:
            validate_decision(request, command, now, existing)
        except ValueError:
            raise PublicationConflict("rejected")
        if not existing:
            self.decisions += 1
            value.update(status="approved" if decision == "approve" else "rejected",
                         publication_status="pending" if decision == "approve" else "not_applicable",
                         receipt=asdict(DecisionReceipt(command, format_utc(now))))
        return value


@pytest.fixture
def review(tmp_path):
    candidates = CandidateRepository(tmp_path / "candidate.sqlite")
    mixed = candidates.stage([
        Document("public", "alpha", ("reader",), "Visible native page", "Public exact source text."),
        Document("private", "alpha", ("analyst",), "HIDDEN SECRET TITLE", "HIDDEN SECRET TEXT."),
    ])
    beta = candidates.stage([Document("beta-page", "beta", ("reader",), "BETA SECRET", "BETA SECRET TEXT")])
    workflow = WorkflowDouble()
    service = HumanReviewService(candidates, workflow, tmp_path / "review.sqlite")
    return service, workflow, mixed, beta


async def create(service, candidate, *, principal=None, request_id="request-1"):
    principal = principal or HumanPrincipal("alpha", ("requester", "reader"), "requester-user")
    deadline = format_utc(datetime.now(timezone.utc) + timedelta(minutes=5))
    return await service.create_request(principal, ("review:request", "review:read"), request_id,
                                        candidate.candidate_sha256, 0, deadline)


@pytest.mark.asyncio
async def test_partial_candidate_view_hides_metadata_and_blocks_both_decisions(review):
    service, workflow, mixed, _beta = review
    await create(service, mixed)
    reviewer = HumanPrincipal("alpha", ("approver", "reader"), "alice")
    scopes = ("review:read", "review:decide")
    view = await service.detail(reviewer, scopes, "request-1")
    encoded = json.dumps(view)
    assert "Visible native page" in encoded
    assert "HIDDEN SECRET" not in encoded and '"document_id": "private"' not in encoded
    assert view["denied_document_count"] == 1 and not view["can_decide"]
    assert view["documents"][0]["chunks"][0]["text"] == "Public exact source text."
    for decision in ("approve", "reject"):
        with pytest.raises(AuthorizationError):
            await service.decide(reviewer, scopes, "request-1", "cmd-1", view["request"]["snapshot_sha256"], decision)
    assert workflow.decisions == 0
    with pytest.raises(ReviewNotFound):
        await service.get_document(reviewer, scopes, "request-1", "private")
    assert (await service.get_document(reviewer, scopes, "request-1", "public")).document_id == "public"


@pytest.mark.asyncio
async def test_tenant_and_reader_permissions_are_required_separately(review):
    service, workflow, mixed, beta = review
    await create(service, mixed)
    beta_user = HumanPrincipal("beta", ("approver", "reader"), "beta-reviewer")
    assert await service.list_requests(beta_user, ("review:read",)) == []
    with pytest.raises(ReviewNotFound):
        await service.detail(beta_user, ("review:read",), "request-1")
    approver_only = HumanPrincipal("alpha", ("approver",), "no-reader")
    assert await service.list_requests(approver_only, ("review:read",)) == []
    with pytest.raises(ReviewNotFound):
        await service.detail(approver_only, ("review:read",), "request-1")
    requester = HumanPrincipal("alpha", ("requester",), "requester-user")
    with pytest.raises(LookupError):
        await create(service, beta, principal=requester, request_id="cross-tenant")
    assert workflow.decisions == 0


@pytest.mark.asyncio
async def test_crash_after_workflow_acceptance_reconciles_one_user_receipt(review, monkeypatch):
    service, workflow, mixed, _beta = review
    view = await create(service, mixed)
    reviewer = HumanPrincipal("alpha", ("approver", "reader", "analyst"), "alice")
    scopes = ("review:read", "review:decide")
    append = service.audit.append
    failed = False
    def stop_after_accept(**fields):
        nonlocal failed
        if fields["action"] == "decision_accepted" and not failed:
            failed = True
            raise OSError("controlled crash before receipt audit commit")
        return append(**fields)
    monkeypatch.setattr(service.audit, "append", stop_after_accept)
    with pytest.raises(OSError):
        await service.decide(reviewer, scopes, "request-1", "cmd-1", view["request"]["snapshot_sha256"], "approve")
    assert workflow.decisions == 1
    assert not any(row["action"] == "decision_accepted" for row in service.audit.events())
    reopened = HumanReviewService(service.candidates, workflow, service.audit.database_path)
    restored = await reopened.detail(reviewer, scopes, "request-1")
    assert restored["status"] == "approved"
    await reopened.decide(reviewer, scopes, "request-1", "cmd-1", view["request"]["snapshot_sha256"], "approve")
    accepted = [row for row in reopened.audit.events() if row["action"] == "decision_accepted"]
    assert len(accepted) == 1 and workflow.decisions == 1
    assert accepted[0]["actor_id"] == "alice" and accepted[0]["identity_kind"] == "user"
    assert accepted[0]["workflow_accepted_at"] == restored["receipt"]["accepted_at"]


@pytest.mark.asyncio
async def test_external_service_receipt_is_never_relabelled_as_human(review):
    service, workflow, mixed, _beta = review
    view = await create(service, mixed)
    from enterprise_ai_lab.rag.contracts import RagPrincipal
    await workflow.decide(RagPrincipal("alpha", ("approver",), "service-reviewer"), "request-1",
                          "external-command", view["request"]["snapshot_sha256"], "reject")
    reviewer = HumanPrincipal("alpha", ("approver", "reader", "analyst"), "alice")
    await service.detail(reviewer, ("review:read",), "request-1")
    events = await service.audit_events(reviewer, ("review:read",))
    receipts = [row for row in events if row["action"] == "decision_observed"]
    assert len(receipts) == 1 and receipts[0]["identity_kind"] == "service_or_unknown"
    assert not any(row["action"] == "decision_accepted" for row in events)


@pytest.mark.asyncio
async def test_self_review_snapshot_changes_and_missing_scope_cannot_decide(review):
    service, workflow, mixed, _beta = review
    requester = HumanPrincipal("alpha", ("requester", "approver", "reader", "analyst"), "same-person")
    view = await create(service, mixed, principal=requester)
    with pytest.raises(PublicationConflict):
        await service.decide(requester, ("review:decide", "review:read"), "request-1", "self", view["request"]["snapshot_sha256"], "approve")
    reviewer = HumanPrincipal("alpha", ("approver", "reader", "analyst"), "alice")
    with pytest.raises(AuthorizationError):
        await service.decide(reviewer, ("review:read",), "request-1", "missing-scope", view["request"]["snapshot_sha256"], "approve")
    with pytest.raises(PublicationConflict):
        await service.decide(reviewer, ("review:decide", "review:read"), "request-1", "wrong-hash", "0" * 64, "approve")
    assert workflow.decisions == 0


@pytest.mark.asyncio
async def test_request_or_decision_scope_never_substitutes_for_read_scope(review):
    service, workflow, mixed, _beta = review
    requester = HumanPrincipal("alpha", ("requester", "reader", "analyst"), "requester-user")
    deadline = format_utc(datetime.now(timezone.utc) + timedelta(minutes=5))
    view = await service.create_request(requester, ("review:request",), "request-1", mixed.candidate_sha256, 0, deadline)
    assert view["documents"] == [] and view["denied_document_count"] == 2
    reviewer = HumanPrincipal("alpha", ("approver", "reader", "analyst"), "alice")
    with pytest.raises(AuthorizationError):
        await service.decide(reviewer, ("review:decide",), "request-1", "blind-scope", view["request"]["snapshot_sha256"], "approve")
    assert workflow.decisions == 0
