"""A replay-safe approval wait. All I/O belongs to the local sink activity."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from .contracts import (
        MAX_LIFETIME_SECONDS, ApplyApprovedNote, ApprovalRequest, ApprovalSnapshot,
        ApprovalValidationError, DecisionCommand, DecisionReceipt, EffectReceipt,
        format_utc, parse_utc, validate_decision,
    )


@workflow.defn
class ApprovalWorkflow:
    @workflow.init
    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        self.status = "pending"
        self.publication_status = "not_started"
        self.receipt: DecisionReceipt | None = None
        self.effect: EffectReceipt | None = None

    @workflow.run
    async def run(self, request: ApprovalRequest) -> ApprovalSnapshot:
        try:
            request.validate()
            remaining = (parse_utc(request.expires_at) - workflow.now()).total_seconds()
            if remaining > MAX_LIFETIME_SECONDS:
                raise ApprovalValidationError("approval lifetime exceeds 24 hours")
        except (ApprovalValidationError, AttributeError, TypeError) as exc:
            raise ApplicationError("invalid approval request", non_retryable=True) from exc

        if self.receipt is None and remaining > 0:
            try:
                await workflow.wait_condition(lambda: self.receipt is not None, timeout=remaining)
            except TimeoutError:
                pass
        if self.receipt is None:
            self.status = "expired"
            self.publication_status = "not_applicable"
        elif self.receipt.command.decision == "reject":
            self.status = "rejected"
            self.publication_status = "not_applicable"
        else:
            self.status = "approved"
            self.publication_status = "pending"
            try:
                self.effect = await workflow.execute_activity(
                    "publish_approved_note",
                    ApplyApprovedNote(self.request, self.receipt),
                    result_type=EffectReceipt,
                    start_to_close_timeout=timedelta(seconds=20),
                    schedule_to_close_timeout=timedelta(seconds=90),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=5),
                        maximum_attempts=5,
                        non_retryable_error_types=["ApprovalValidationError", "SinkConflict"],
                    ),
                )
            except Exception:
                self.publication_status = "failed"
                raise
            self.publication_status = "published"
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.snapshot()

    @workflow.update
    def decide(self, command: DecisionCommand) -> DecisionReceipt:
        # Repeat validation because another handler can change state after admission.
        self._validate(command)
        if self.receipt is None:
            self.receipt = DecisionReceipt(command, format_utc(workflow.now()))
            self.status = "approved" if command.decision == "approve" else "rejected"
        return self.receipt

    @decide.validator
    def validate_decide(self, command: DecisionCommand) -> None:
        self._validate(command)

    def _validate(self, command: DecisionCommand) -> None:
        validate_decision(self.request, command, workflow.now(), self.receipt)
        if self.status == "expired":
            raise ApprovalValidationError("approval request expired")

    @workflow.query
    def snapshot(self) -> ApprovalSnapshot:
        return ApprovalSnapshot(
            self.request, self.status, self.publication_status, self.receipt, self.effect
        )
