"""Account-scoped review of immutable candidates over the existing workflow.

The local catalog records submission/decision intents before network calls.
Temporal remains authoritative for acceptance. A later read reconciles accepted
receipts after a crash between workflow acceptance and the audit append; there
is deliberately no cross-database transaction claim.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from ..approval.contracts import DecisionCommand, DecisionReceipt, identifier, parse_utc, validate_decision
from ..publication.contracts import make_request
from ..publication.gateway import PublicationConflict, PublicationNotFound
from ..publication.storage import CandidateRepository, canonical, document_dict
from ..rag.auth import AuthorizationError
from ..rag.store import chunk_document
from .audit import AuditIntegrityError, AuditLog
from .auth import HumanPrincipal

MAX_CATALOG_REQUESTS = 1000


class ReviewNotFound(LookupError):
    """A request or document does not exist in this account's permitted view."""


def _hash(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class HumanReviewService:
    def __init__(self, candidates: CandidateRepository, gateway, audit_database: str | Path):
        self.candidates, self.gateway = candidates, gateway
        self.audit = AuditLog(audit_database)
        connection = self._connect()
        connection.close()

    def _connect(self):
        connection = self.audit.connect()
        connection.execute("""CREATE TABLE IF NOT EXISTS review_requests (
            tenant_id TEXT NOT NULL, request_id TEXT NOT NULL,
            request_json TEXT NOT NULL, PRIMARY KEY(tenant_id,request_id)
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS review_decision_intents (
            intent_sha256 TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            request_id TEXT NOT NULL, command_json TEXT NOT NULL
        )""")
        return connection

    @staticmethod
    def _authorize(principal, scopes, required, role=None):
        if (not isinstance(principal, HumanPrincipal) or required not in scopes
                or (role is not None and role not in principal.roles)):
            raise AuthorizationError("This account cannot perform the requested review action.")

    @staticmethod
    def _readable(principal, document):
        return (principal.tenant_id == document.tenant_id
                and bool(set(principal.roles).intersection(document.allowed_roles)))

    def _intent(self, principal, request_id):
        identifier(request_id)
        connection = self._connect()
        try:
            row = connection.execute("SELECT request_json FROM review_requests WHERE tenant_id=? AND request_id=?",
                                     (principal.tenant_id, request_id)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ReviewNotFound("review request not found")
        try:
            if not isinstance(row[0], str) or len(row[0].encode()) > 4096:
                raise ValueError("invalid catalog size")
            value = json.loads(row[0])
            request = make_request(value["request_id"], value["tenant_id"], value["requested_by"],
                                   value["candidate_sha256"], value["expected_revision"], value["expires_at"])
            if (asdict(request) != value or canonical(value) != row[0]
                    or request.tenant_id != principal.tenant_id or request.request_id != request_id):
                raise ValueError("catalog identity differs")
            return request
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise AuditIntegrityError("review catalog failed immutable snapshot validation") from exc

    def _save_request(self, request):
        encoded = canonical(asdict(request))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT request_json FROM review_requests WHERE tenant_id=? AND request_id=?",
                                     (request.tenant_id, request.request_id)).fetchone()
            if row is not None and row[0] != encoded:
                raise PublicationConflict("request ID already binds a different review submission")
            if row is None:
                count = connection.execute("SELECT count(*) FROM review_requests WHERE tenant_id=?",
                                           (request.tenant_id,)).fetchone()[0]
                if count >= MAX_CATALOG_REQUESTS:
                    raise ValueError("local review catalog has reached its tenant request bound")
                connection.execute("INSERT INTO review_requests VALUES (?,?,?)",
                                   (request.tenant_id, request.request_id, encoded))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def create_request(self, principal, scopes, request_id, candidate_sha256,
                             expected_revision, expires_at):
        self._authorize(principal, scopes, "review:request", "requester")
        request = make_request(request_id, principal.tenant_id, principal.subject,
                               candidate_sha256, expected_revision, expires_at)
        # Tenant-scoped immutable read before recording anything. Requesting
        # review grants no additional document-reading or decision permission.
        self.candidates.load_documents(principal.tenant_id, candidate_sha256)
        self._save_request(request)
        self.audit.append(tenant_id=principal.tenant_id, actor_id=principal.subject,
                          action="request_intent", result="recorded", request_id=request_id,
                          candidate_sha256=candidate_sha256, snapshot_sha256=request.snapshot_sha256,
                          event_key="request-intent/" + request.snapshot_sha256)
        snapshot = await self.gateway.submit(principal.publication_principal(), request_id,
                                              candidate_sha256, expected_revision, expires_at)
        self._validate_snapshot(request, snapshot)
        self._reconcile(request, snapshot)
        return self._view(principal, scopes, request, snapshot, include_text=True)

    @staticmethod
    def _validate_snapshot(request, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("request") != asdict(request):
            raise AuditIntegrityError("workflow snapshot differs from the immutable review catalog")

    def _reconcile(self, request, snapshot):
        self.audit.append(tenant_id=request.tenant_id, actor_id=request.requested_by,
                          action="request_accepted", result="accepted", request_id=request.request_id,
                          candidate_sha256=request.candidate_sha256, snapshot_sha256=request.snapshot_sha256,
                          event_key="request-accepted/" + request.snapshot_sha256)
        receipt_value = snapshot.get("receipt")
        if receipt_value is None:
            return
        try:
            command_value = dict(receipt_value["command"])
            command_value["actor_roles"] = tuple(command_value["actor_roles"])
            command = DecisionCommand(**command_value)
            receipt = DecisionReceipt(command, receipt_value["accepted_at"])
            validate_decision(request, command, parse_utc(receipt.accepted_at))
        except (ValueError, TypeError, KeyError) as exc:
            raise AuditIntegrityError("workflow returned an invalid review receipt") from exc
        command_json = canonical(asdict(command))
        connection = self._connect()
        try:
            known = connection.execute("SELECT command_json FROM review_decision_intents "
                                       "WHERE intent_sha256=? AND tenant_id=? AND request_id=?",
                                       (_hash(asdict(command)), request.tenant_id, request.request_id)).fetchone()
        finally:
            connection.close()
        is_user = known is not None and known[0] == command_json
        # An approval submitted outside this browser boundary remains visible,
        # but is never relabelled as a signed-in human action.
        self.audit.append(tenant_id=request.tenant_id, actor_id=command.actor_id,
                          action="decision_accepted" if is_user else "decision_observed",
                          result=command.decision, request_id=request.request_id,
                          candidate_sha256=request.candidate_sha256, snapshot_sha256=request.snapshot_sha256,
                          command_id=command.command_id,
                          identity_kind="user" if is_user else "service_or_unknown",
                          workflow_accepted_at=receipt.accepted_at,
                          event_key="decision-receipt/" + _hash([request.tenant_id, request.request_id, asdict(receipt)]))
        if snapshot.get("publication_status") == "published":
            if not isinstance(snapshot.get("effect"), dict):
                raise AuditIntegrityError("published workflow lacks its effect receipt")
            self.audit.append(tenant_id=request.tenant_id, actor_id="publication-worker",
                              action="publication_observed", result="published",
                              request_id=request.request_id, candidate_sha256=request.candidate_sha256,
                              snapshot_sha256=request.snapshot_sha256, identity_kind="system",
                              event_key="publication-observed/" + request.snapshot_sha256)

    async def _load(self, principal, request_id):
        request = self._intent(principal, request_id)
        try:
            snapshot = await self.gateway.status(principal.publication_principal(), request_id)
        except PublicationNotFound:
            # A process can stop after its persisted intent but before Temporal
            # accepts the start. An exact create-request retry resumes admission.
            return request, {"request": asdict(request), "status": "submission_pending",
                             "publication_status": "not_applicable", "receipt": None, "effect": None}
        self._validate_snapshot(request, snapshot)
        self._reconcile(request, snapshot)
        return request, snapshot

    def _view(self, principal, scopes, request, snapshot, *, include_text):
        documents = self.candidates.load_documents(principal.tenant_id, request.candidate_sha256)
        permitted = [document for document in documents
                     if "review:read" in scopes and self._readable(principal, document)]
        denied_count = len(documents) - len(permitted)
        visible = []
        for document in permitted:
            value = document_dict(document)
            if include_text:
                value["chunks"] = chunk_document(document)
            else:
                value.pop("text")
            visible.append(value)
        # Never return PublicationGateway's complete candidate metadata: a
        # reviewer role alone must not disclose an unreadable page's title,
        # source URL, PDF hash, OCR geometry or document identifier.
        response = {key: snapshot.get(key) for key in ("request", "status", "publication_status", "receipt", "effect")}
        response.update(documents=visible, denied_document_count=denied_count,
                        can_decide=(denied_count == 0 and "approver" in principal.roles
                                    and "review:decide" in scopes and "review:read" in scopes
                                    and principal.subject != request.requested_by
                                    and parse_utc(request.expires_at) > datetime.now(timezone.utc)
                                    and snapshot.get("status") == "pending"))
        return response

    async def list_requests(self, principal, scopes):
        self._authorize(principal, scopes, "review:read")
        connection = self._connect()
        try:
            rows = connection.execute("SELECT request_id FROM review_requests WHERE tenant_id=? ORDER BY request_id LIMIT ?",
                                      (principal.tenant_id, MAX_CATALOG_REQUESTS + 1)).fetchall()
        finally:
            connection.close()
        if len(rows) > MAX_CATALOG_REQUESTS:
            raise AuditIntegrityError("review catalog exceeds its tenant request bound")
        result = []
        for (request_id,) in rows:
            request, snapshot = await self._load(principal, request_id)
            view = self._view(principal, scopes, request, snapshot, include_text=False)
            # A wholly hidden request is omitted rather than leaking a title or
            # encouraging a blind decision. A requester can still see its ID.
            if view["documents"] or request.requested_by == principal.subject:
                result.append(view)
        return result

    async def detail(self, principal, scopes, request_id):
        self._authorize(principal, scopes, "review:read")
        request, snapshot = await self._load(principal, request_id)
        view = self._view(principal, scopes, request, snapshot, include_text=True)
        if not view["documents"] and request.requested_by != principal.subject:
            raise ReviewNotFound("review request not found")
        self.audit.append(tenant_id=principal.tenant_id, actor_id=principal.subject,
                          action="request_view", result="permitted", request_id=request_id,
                          candidate_sha256=request.candidate_sha256, snapshot_sha256=request.snapshot_sha256)
        return view

    async def get_document(self, principal, scopes, request_id, document_id):
        self._authorize(principal, scopes, "review:read")
        identifier(document_id)
        request, _snapshot = await self._load(principal, request_id)
        documents = self.candidates.load_documents(principal.tenant_id, request.candidate_sha256)
        for document in documents:
            if document.document_id == document_id and self._readable(principal, document):
                return document
        raise ReviewNotFound("review document not found")

    async def decide(self, principal, scopes, request_id, command_id, snapshot_sha256, decision):
        self._authorize(principal, scopes, "review:decide", "approver")
        self._authorize(principal, scopes, "review:read")
        request, _snapshot = await self._load(principal, request_id)
        documents = self.candidates.load_documents(principal.tenant_id, request.candidate_sha256)
        if not all(self._readable(principal, document) for document in documents):
            self.audit.append(tenant_id=principal.tenant_id, actor_id=principal.subject,
                              action="decision_denied", result="denied", request_id=request_id,
                              candidate_sha256=request.candidate_sha256, snapshot_sha256=request.snapshot_sha256,
                              reason_code="unreadable_batch")
            raise AuthorizationError("Every document must be readable before deciding this batch.")
        command = DecisionCommand(command_id, principal.tenant_id, snapshot_sha256,
                                  decision, principal.subject, principal.roles)
        command.validate()
        command_hash, command_json = _hash(asdict(command)), canonical(asdict(command))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR IGNORE INTO review_decision_intents VALUES (?,?,?,?)",
                               (command_hash, principal.tenant_id, request_id, command_json))
            prior = connection.execute("SELECT tenant_id,request_id,command_json FROM review_decision_intents WHERE intent_sha256=?",
                                       (command_hash,)).fetchone()
            if prior != (principal.tenant_id, request_id, command_json):
                raise AuditIntegrityError("review decision intent conflicts")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.audit.append(tenant_id=principal.tenant_id, actor_id=principal.subject,
                          action="decision_intent", result=decision, request_id=request_id,
                          candidate_sha256=request.candidate_sha256, snapshot_sha256=snapshot_sha256,
                          command_id=command_id, event_key="decision-intent/" + command_hash)
        try:
            snapshot = await self.gateway.decide(principal.publication_principal(), request_id,
                                                 command_id, snapshot_sha256, decision)
        except PublicationConflict:
            self.audit.append(tenant_id=principal.tenant_id, actor_id=principal.subject,
                              action="decision_denied", result="conflict", request_id=request_id,
                              candidate_sha256=request.candidate_sha256, snapshot_sha256=snapshot_sha256,
                              command_id=command_id, event_key="decision-conflict/" + command_hash)
            raise
        self._validate_snapshot(request, snapshot)
        self._reconcile(request, snapshot)
        return self._view(principal, scopes, request, snapshot, include_text=True)

    async def audit_events(self, principal, scopes):
        self._authorize(principal, scopes, "review:read", "approver")
        readable_requests = set()
        for view in await self.list_requests(principal, scopes):
            if view["denied_document_count"] == 0:
                readable_requests.add(view["request"]["request_id"])
        events = self.audit.events(principal.tenant_id)
        # Login/logout observations are private to the actor. Review events
        # require the same complete-batch reading policy as a decision.
        return [event for event in events
                if (event["request_id"] in readable_requests
                    or (event["request_id"] is None and event["actor_id"] == principal.subject))]
