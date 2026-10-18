"""Durable document approvals with a separately versioned workflow type."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from .contracts import (
        MAX_LIFETIME_SECONDS, ApplyApprovedDocuments, ApprovalValidationError,
        DecisionCommand, DecisionReceipt, PublicationEffect, PublicationRequest,
        PublicationSnapshot, format_utc, parse_utc, validate_decision,
    )


@workflow.defn
class DocumentPublicationWorkflow:
    @workflow.init
    def __init__(self, request: PublicationRequest) -> None:
        self.request = request
        self.status = "pending"
        self.publication_status = "not_started"
        self.receipt: DecisionReceipt | None = None
        self.effect: PublicationEffect | None = None

    @workflow.run
    async def run(self, request: PublicationRequest) -> PublicationSnapshot:
        try:
            request.validate()
            remaining = (parse_utc(request.expires_at) - workflow.now()).total_seconds()
            if remaining > MAX_LIFETIME_SECONDS:
                raise ApprovalValidationError("publication lifetime exceeds 24 hours")
        except (ApprovalValidationError, AttributeError, TypeError) as exc:
            raise ApplicationError("invalid publication request", non_retryable=True) from exc

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
                    "publish_approved_documents",
                    ApplyApprovedDocuments(self.request, self.receipt),
                    result_type=PublicationEffect,
                    start_to_close_timeout=timedelta(seconds=30),
                    schedule_to_close_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=5), maximum_attempts=5,
                        non_retryable_error_types=["ApprovalValidationError",
                            "PublicationValidationError", "CandidateConflict",
                            "PublicationConflict"],
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
            raise ApprovalValidationError("publication request expired")

    @workflow.query
    def snapshot(self) -> PublicationSnapshot:
        return PublicationSnapshot(self.request, self.status, self.publication_status,
                                   self.receipt, self.effect)
