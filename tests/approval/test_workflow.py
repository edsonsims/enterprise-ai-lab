"""Core boundaries plus actual Temporal updates, timers and deterministic replay."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from enterprise_ai_lab.approval.activities import LocalNotifier
from enterprise_ai_lab.approval.contracts import (
    ApplyApprovedNote, ApprovalValidationError, DecisionCommand, DecisionReceipt,
    LocalNote, format_utc, make_request, parse_utc, validate_decision,
)
from enterprise_ai_lab.approval.workflow import ApprovalWorkflow


NOW = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)


def request_at(now=NOW, *, request_id="r1", tenant="alpha", lifetime=60):
    return make_request(request_id, tenant, "requester-alpha", LocalNote("Review summary", "Synthetic local note."), format_utc(now + timedelta(seconds=lifetime)))


def command_for(request, **kwargs):
    value = DecisionCommand("command-1", request.tenant_id, request.snapshot_sha256, "approve", "reviewer-alpha", ("approver",))
    return replace(value, **kwargs)


def test_digest_binds_tenant_requester_deadline_content_and_request_id():
    original = request_at()
    variants = [
        replace(original, tenant_id="beta"), replace(original, request_id="r2"),
        replace(original, requested_by="someone-else"),
        replace(original, expires_at=format_utc(NOW + timedelta(seconds=61))),
        replace(original, payload=LocalNote("Review summary", "A different note.")),
    ]
    original.validate()
    for changed in variants:
        with pytest.raises(ApprovalValidationError, match="digest"):
            changed.validate()


@pytest.mark.parametrize("change", [
    {"tenant_id": "beta"}, {"snapshot_sha256": "0" * 64},
    {"actor_roles": ("reader",)}, {"actor_id": "requester-alpha"},
    {"decision": "published"}, {"actor_roles": ("approver", "approver")},
    {"actor_roles": (["approver"],)}, {"command_id": "../escape"},
])
def test_decision_rejects_forged_or_malformed_authority(change):
    request = request_at()
    with pytest.raises(ApprovalValidationError):
        validate_decision(request, command_for(request, **change), NOW)


def test_expiry_is_strict_and_exact_accepted_duplicate_retains_receipt():
    request = request_at()
    command = command_for(request)
    accepted = DecisionReceipt(command, format_utc(NOW))
    with pytest.raises(ApprovalValidationError, match="expired"):
        validate_decision(request, command, parse_utc(request.expires_at))
    validate_decision(request, command, NOW + timedelta(days=1), accepted)
    for altered in (replace(command, command_id="other"), replace(command, decision="reject"), replace(command, actor_id="other")):
        with pytest.raises(ApprovalValidationError, match="already accepted"):
            validate_decision(request, altered, NOW, accepted)


@pytest.mark.parametrize("timestamp", ["2026-10-18", "2026-10-18T14:00:00+00:00", "2026-99-99T14:00:00Z"])
def test_timestamps_have_one_canonical_form(timestamp):
    with pytest.raises(ApprovalValidationError):
        parse_utc(timestamp)


@pytest.mark.parametrize("title,body", [("", "hello"), ("x" * 161, "hello"), ("hello", "x" * 4001), ("hello", "bad\x00text"), ("hello", "\ud800")])
def test_notes_are_bounded(title, body):
    with pytest.raises(ApprovalValidationError):
        make_request("r1", "alpha", "requester", LocalNote(title, body), format_utc(NOW))


def test_local_sink_lost_ack_retries_without_duplicate_and_rejects_conflicting_content(tmp_path):
    request = request_at()
    command = command_for(request)
    value = ApplyApprovedNote(request, DecisionReceipt(command, format_utc(NOW)))
    notifier = LocalNotifier(tmp_path / "sink.sqlite", fault_once_after_commit=True)
    environment = ActivityEnvironment()
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        environment.run(notifier.publish_approved_note, value)
    assert len(notifier.list_notifications()) == 1
    receipt = environment.run(notifier.publish_approved_note, value)
    assert environment.run(notifier.publish_approved_note, value) == receipt
    # The original row is not silently overwritten under the same effect key.
    altered_request = replace(request, payload=LocalNote("Changed", "Different content"))
    altered_request = make_request(altered_request.request_id, altered_request.tenant_id, altered_request.requested_by, altered_request.payload, altered_request.expires_at)
    altered_value = ApplyApprovedNote(altered_request, DecisionReceipt(command_for(altered_request), format_utc(NOW)))
    with pytest.raises(ApplicationError, match="conflicts"):
        environment.run(notifier.publish_approved_note, altered_value)
    assert len(notifier.list_notifications()) == 1
    assert notifier.list_notifications()[0]["snapshot_sha256"] == request.snapshot_sha256


@pytest.mark.parametrize("decision", ["reject", "approve"])
def test_sink_rejects_unapproved_or_late_receipts_before_any_row(tmp_path, decision):
    request = request_at()
    accepted_at = format_utc(NOW) if decision == "reject" else request.expires_at
    value = ApplyApprovedNote(request, DecisionReceipt(command_for(request, decision=decision), accepted_at))
    notifier = LocalNotifier(tmp_path / "sink.sqlite")
    with pytest.raises(ApplicationError, match="invalid approved"):
        ActivityEnvironment().run(notifier.publish_approved_note, value)
    assert notifier.list_notifications() == []


@pytest.mark.asyncio
async def test_actual_temporal_updates_timer_and_history_replay(tmp_path):
    cli = os.environ.get("TEMPORAL_CLI")
    if not cli:
        pytest.skip("set TEMPORAL_CLI to the explicitly bootstrapped pinned local executable")
    assert Path(cli).is_file()
    environment = await WorkflowEnvironment.start_local(
        dev_server_existing_path=cli,
        dev_server_database_filename=str(tmp_path / "temporal.sqlite"),
        ui=False,
    )
    queue = f"core-{uuid.uuid4()}"
    notifier = LocalNotifier(tmp_path / "notifications.sqlite", fault_once_after_commit=True)
    histories = []
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(environment.client, task_queue=queue, workflows=[ApprovalWorkflow], activities=[notifier.publish_approved_note], activity_executor=executor):
                now = datetime.now(timezone.utc)
                request = request_at(now, request_id="approved", lifetime=40)
                handle = await environment.client.start_workflow(ApprovalWorkflow.run, request, id=f"approval-{uuid.uuid4()}", task_queue=queue)
                # The query waits for first task without changing approval state.
                snapshot = await handle.query(ApprovalWorkflow.snapshot)
                assert snapshot.status == "pending"
                assert notifier.list_notifications() == []
                for change in ({"tenant_id": "beta"}, {"actor_roles": ("reader",)}, {"actor_id": "requester-alpha"}, {"snapshot_sha256": "0" * 64}):
                    with pytest.raises(WorkflowUpdateFailedError):
                        await handle.execute_update(ApprovalWorkflow.decide, command_for(request, **change))
                assert (await handle.query(ApprovalWorkflow.snapshot)).status == "pending"
                assert notifier.list_notifications() == []
                receipt = await handle.execute_update(ApprovalWorkflow.decide, command_for(request), id="exact-approved-command")
                result = await handle.result()
                assert result.status == "approved" and result.publication_status == "published"
                assert result.receipt == receipt
                assert len(notifier.list_notifications()) == 1
                # Temporal's identical Update ID returns its recorded result even after closure.
                duplicate = await handle.execute_update(ApprovalWorkflow.decide, command_for(request), id="exact-approved-command")
                assert duplicate == receipt
                # Server Update-ID deduplication happens before our validator.
                # The gateway must compare the returned command, not mistake
                # this recorded approval for approval of an altered request.
                altered_command = command_for(request, decision="reject")
                deduplicated = await handle.execute_update(ApprovalWorkflow.decide, altered_command, id="exact-approved-command")
                assert deduplicated == receipt and deduplicated.command != altered_command
                histories.append(await handle.fetch_history())

                rejected_request = request_at(datetime.now(timezone.utc), request_id="rejected", lifetime=20)
                rejected = await environment.client.start_workflow(ApprovalWorkflow.run, rejected_request, id=f"approval-{uuid.uuid4()}", task_queue=queue)
                await rejected.execute_update(ApprovalWorkflow.decide, command_for(rejected_request, decision="reject"))
                result = await rejected.result()
                assert result.status == "rejected" and result.publication_status == "not_applicable"
                histories.append(await rejected.fetch_history())

                expired_request = request_at(datetime.now(timezone.utc), request_id="expired", lifetime=2)
                expired = await environment.client.start_workflow(ApprovalWorkflow.run, expired_request, id=f"approval-{uuid.uuid4()}", task_queue=queue)
                result = await expired.result()
                assert result.status == "expired" and result.effect is None
                assert len(notifier.list_notifications()) == 1
                histories.append(await expired.fetch_history())
        for history in histories:
            await Replayer(workflows=[ApprovalWorkflow]).replay_workflow(history)
    finally:
        await environment.shutdown()
