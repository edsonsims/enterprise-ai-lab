"""Authenticated application edge to a trusted local Temporal namespace."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from .contracts import (ApprovalSnapshot, DecisionCommand, LocalNote, MAX_LIFETIME_SECONDS,
    identifier, make_request, parse_utc, validate_decision)
from .workflow import ApprovalWorkflow

RPC_WAIT = timedelta(seconds=3)


class ApprovalConflict(ValueError):
    """The existing immutable request or terminal decision differs."""


class ApprovalNotFound(ValueError):
    """No request exists in this tenant's workflow scope."""


def workflow_id(tenant_id: str, request_id: str) -> str:
    identifier(tenant_id); identifier(request_id)
    # Hash a framed identity; punctuation inside either ID cannot alias a peer.
    return "approval-" + hashlib.sha256(json.dumps([tenant_id, request_id], separators=(",", ":")).encode()).hexdigest()


class ApprovalGateway:
    def __init__(self, client: Client, task_queue: str):
        identifier(task_queue)
        self.client, self.task_queue = client, task_queue

    async def _snapshot(self, principal, request_id: str) -> ApprovalSnapshot:
        handle = self.client.get_workflow_handle(workflow_id(principal.tenant_id, request_id))
        try:
            snapshot = await handle.query(ApprovalWorkflow.snapshot, rpc_timeout=RPC_WAIT)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise ApprovalNotFound("request does not exist") from None
            raise
        if (not isinstance(snapshot, ApprovalSnapshot) or snapshot.request.tenant_id != principal.tenant_id
                or snapshot.request.request_id != request_id):
            raise RuntimeError("workflow returned inconsistent identity")
        snapshot.request.validate()
        return snapshot

    async def submit(self, principal, request_id, note, expires_at) -> dict:
        proposed = make_request(request_id, principal.tenant_id, principal.client_id,
            LocalNote(**note), expires_at)
        try:
            existing = await self._snapshot(principal, request_id)
        except ApprovalNotFound:
            existing = None
        if existing is not None:
            if existing.request != proposed:
                raise ApprovalConflict("request ID already binds different immutable content")
            return asdict(existing)
        remaining = (parse_utc(expires_at) - datetime.now(timezone.utc)).total_seconds()
        if not 0 < remaining <= MAX_LIFETIME_SECONDS:
            raise ValueError("new request deadline must be within 24 hours")
        try:
            await self.client.start_workflow(ApprovalWorkflow.run, proposed,
                id=workflow_id(principal.tenant_id, request_id), task_queue=self.task_queue,
                execution_timeout=timedelta(days=2), id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=RPC_WAIT)
        except WorkflowAlreadyStartedError:
            # Two concurrent creates can both observe NOT_FOUND. Re-read and
            # compare every immutable field; never silently join other content.
            pass
        existing = await self._snapshot(principal, request_id)
        if existing.request != proposed:
            raise ApprovalConflict("request ID already binds different immutable content")
        return asdict(existing)

    async def status(self, principal, request_id) -> dict:
        return asdict(await self._snapshot(principal, request_id))

    async def decide(self, principal, request_id, command_id, snapshot_sha256, decision) -> dict:
        command = DecisionCommand(command_id, principal.tenant_id, snapshot_sha256,
            decision, principal.client_id, principal.roles)
        snapshot = await self._snapshot(principal, request_id)
        try:
            validate_decision(snapshot.request, command, datetime.now(timezone.utc), snapshot.receipt)
            if snapshot.status == "expired":
                raise ValueError("request expired")
        except ValueError:
            raise ApprovalConflict("decision is expired, conflicting or does not match the request") from None
        if snapshot.receipt is not None:
            return asdict(snapshot)
        # Temporal deduplicates IDs BEFORE running validators. Bind the Update
        # ID to the complete server-derived command, then verify its receipt too.
        update_id = hashlib.sha256(json.dumps(asdict(command), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        handle = self.client.get_workflow_handle(workflow_id(principal.tenant_id, request_id))
        try:
            receipt = await handle.execute_update(ApprovalWorkflow.decide, command, id=update_id, rpc_timeout=RPC_WAIT)
        except WorkflowUpdateFailedError:
            raise ApprovalConflict("decision was rejected by the durable workflow") from None
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                # The timer/another decision may have closed the workflow.
                latest = await self._snapshot(principal, request_id)
                if latest.receipt is not None and latest.receipt.command == command:
                    return asdict(latest)
                raise ApprovalConflict("request is already terminal") from None
            raise
        if receipt.command != command:
            raise ApprovalConflict("returned receipt differs from the proposed command")
        latest = await self._snapshot(principal, request_id)
        if latest.receipt is None or latest.receipt.command != command:
            raise ApprovalConflict("durable decision receipt differs")
        return asdict(latest)
