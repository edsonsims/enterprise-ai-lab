"""Verified approval identity to immutable candidates and a trusted namespace."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Protocol

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from ..diagnostics import diagnostic_stage, mark_rpc_failure

from .contracts import (
    CandidateMetadata, DecisionCommand, MAX_LIFETIME_SECONDS, PublicationSnapshot,
    digest, identifier, make_request, parse_utc, validate_decision,
)
from .workflow import DocumentPublicationWorkflow

RPC_WAIT = timedelta(seconds=3)


@contextmanager
def _rpc_stage(stage):
    with diagnostic_stage(stage):
        try:
            yield
        except RPCError as exc:
            if type(exc) is RPCError:
                status = exc.status
                mark_rpc_failure(exc, status.name if type(status) is RPCStatusCode else None)
            raise


class CandidateReader(Protocol):
    def get_candidate(self, tenant_id: str, candidate_sha256: str) -> CandidateMetadata: ...


class PublicationConflict(ValueError):
    """An immutable request or terminal decision differs from the replay."""


class PublicationNotFound(LookupError):
    """The request or staged candidate does not exist in this tenant scope."""


def workflow_id(tenant_id: str, request_id: str) -> str:
    identifier(tenant_id)
    identifier(request_id)
    framed = json.dumps([tenant_id, request_id], separators=(",", ":"))
    return "publication-" + hashlib.sha256(framed.encode()).hexdigest()


class PublicationGateway:
    def __init__(self, client: Client, task_queue: str, candidates: CandidateReader):
        identifier(task_queue)
        self.client, self.task_queue, self.candidates = client, task_queue, candidates

    def _candidate(self, tenant_id: str, candidate_sha256: str) -> CandidateMetadata:
        with diagnostic_stage("publication_candidate"):
            identifier(tenant_id)
            digest(candidate_sha256)
            try:
                value = self.candidates.get_candidate(tenant_id, candidate_sha256)
            except LookupError:
                raise PublicationNotFound("candidate does not exist in this tenant scope") from None
            except ValueError as exc:
                # Corrupt immutable content is a backend integrity failure, not a
                # public client validation error with potentially private details.
                raise RuntimeError("candidate integrity check failed") from exc
            if (not isinstance(value, CandidateMetadata) or value.tenant_id != tenant_id
                    or value.candidate_sha256 != candidate_sha256
                    or type(value.document_count) is not int
                    or not 1 <= value.document_count <= 1000
                    or len(value.document_ids) != value.document_count
                    or len(value.documents) != value.document_count
                    or len(set(value.document_ids)) != value.document_count):
                raise RuntimeError("candidate returned inconsistent metadata")
            return value

    async def _snapshot(self, principal, request_id: str) -> PublicationSnapshot:
        handle = self.client.get_workflow_handle(workflow_id(principal.tenant_id, request_id))
        try:
            with _rpc_stage("temporal_query"):
                snapshot = await handle.query(DocumentPublicationWorkflow.snapshot, rpc_timeout=RPC_WAIT)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise PublicationNotFound("request does not exist") from None
            raise
        if (not isinstance(snapshot, PublicationSnapshot)
                or snapshot.request.tenant_id != principal.tenant_id
                or snapshot.request.request_id != request_id):
            raise RuntimeError("workflow returned inconsistent publication identity")
        snapshot.request.validate()
        return snapshot

    def _response(self, snapshot: PublicationSnapshot) -> dict:
        candidate = self._candidate(snapshot.request.tenant_id, snapshot.request.candidate_sha256)
        return {**asdict(snapshot), "candidate": asdict(candidate)}

    async def submit(self, principal, request_id, candidate_sha256, expected_revision, expires_at) -> dict:
        proposed = make_request(request_id, principal.tenant_id, principal.client_id,
                                candidate_sha256, expected_revision, expires_at)
        self._candidate(principal.tenant_id, candidate_sha256)
        try:
            existing = await self._snapshot(principal, request_id)
        except PublicationNotFound:
            existing = None
        if existing is not None:
            if existing.request != proposed:
                raise PublicationConflict("request ID already binds different immutable content")
            return self._response(existing)
        remaining = (parse_utc(expires_at) - datetime.now(timezone.utc)).total_seconds()
        if not 0 < remaining <= MAX_LIFETIME_SECONDS:
            raise ValueError("new publication deadline must be within 24 hours")
        try:
            with _rpc_stage("temporal_start"):
                await self.client.start_workflow(
                    DocumentPublicationWorkflow.run, proposed,
                    id=workflow_id(principal.tenant_id, request_id), task_queue=self.task_queue,
                    execution_timeout=timedelta(days=2),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    rpc_timeout=RPC_WAIT,
                )
        except WorkflowAlreadyStartedError:
            # Two admissions can observe NOT_FOUND; compare every bound field
            # after the winning start rather than joining unrelated content.
            pass
        existing = await self._snapshot(principal, request_id)
        if existing.request != proposed:
            raise PublicationConflict("request ID already binds different immutable content")
        return self._response(existing)

    async def status(self, principal, request_id) -> dict:
        return self._response(await self._snapshot(principal, request_id))

    async def decide(self, principal, request_id, command_id, snapshot_sha256, decision) -> dict:
        command = DecisionCommand(command_id, principal.tenant_id, snapshot_sha256,
                                  decision, principal.client_id, principal.roles)
        snapshot = await self._snapshot(principal, request_id)
        # Verify candidate availability/integrity before accepting any decision.
        self._candidate(principal.tenant_id, snapshot.request.candidate_sha256)
        try:
            validate_decision(snapshot.request, command, datetime.now(timezone.utc), snapshot.receipt)
            if snapshot.status == "expired":
                raise ValueError("request expired")
        except ValueError:
            raise PublicationConflict("decision is expired, conflicting or does not match the request") from None
        if snapshot.receipt is not None:
            return self._response(snapshot)
        # Temporal deduplicates Update IDs before validators. Bind every command
        # field, including the verified actor and roles, then verify its receipt.
        canonical = json.dumps(asdict(command), sort_keys=True, separators=(",", ":"))
        update_id = hashlib.sha256(canonical.encode()).hexdigest()
        handle = self.client.get_workflow_handle(workflow_id(principal.tenant_id, request_id))
        try:
            with _rpc_stage("temporal_update"):
                receipt = await handle.execute_update(DocumentPublicationWorkflow.decide, command,
                                                      id=update_id, rpc_timeout=RPC_WAIT)
        except WorkflowUpdateFailedError:
            raise PublicationConflict("decision was rejected by the durable workflow") from None
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                latest = await self._snapshot(principal, request_id)
                if latest.receipt is not None and latest.receipt.command == command:
                    return self._response(latest)
                raise PublicationConflict("request is already terminal") from None
            raise
        if receipt.command != command:
            raise PublicationConflict("returned receipt differs from the proposed command")
        latest = await self._snapshot(principal, request_id)
        if latest.receipt is None or latest.receipt.command != command:
            raise PublicationConflict("durable decision receipt differs")
        return self._response(latest)
