"""Adversarial checks of recorded data, without starting its three runtimes."""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_reliability_case", ROOT / "scripts/verify_reliability_case.py")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)
FIXTURE = Path(os.environ.get("RELIABILITY_CASE_FIXTURE", ROOT / "reports/case-reliability-v1"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class ReliabilityCaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "case"
        shutil.copytree(FIXTURE, self.directory)
        self.reports = {kind: json.loads((self.directory / kind / "demo.json").read_text()) for kind in ["chaos", "approval", "privacy"]}
        self.provenance = json.loads((self.directory / "provenance.json").read_text())

    def observation(self, kind, name):
        return next(x for x in self.reports[kind]["observations"] if x["case"] == name)

    def reseal(self, kind):
        path = self.directory / kind / "demo.json"
        write_json(path, self.reports[kind])
        self.provenance["experiments"][kind].update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        write_json(self.directory / "provenance.json", self.provenance)

    def rejected(self, kind=None):
        if kind is not None:
            self.reseal(kind)
        result = VERIFIER.verify(self.directory)
        self.assertFalse(result["passed"])
        return result

    def history_change(self, filename, mutate):
        path = self.directory / "approval/histories" / filename
        history = json.loads(path.read_text())
        mutate(history)
        write_json(path, history)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        for meta in self.reports["approval"]["histories"]:
            if meta["filename"] == "histories/" + filename:
                meta.update(sha256=sha, event_count=len(history["events"]), event_types=[e["eventType"] for e in history["events"]])
        self.provenance["histories_replayed_by_actual_SDK"] = [{**h, "filename": "approval/" + h["filename"]} for h in self.reports["approval"]["histories"]]
        self.reseal("approval")

    def payload_change(self, filename, attribute, keys, mutate):
        def change(history):
            event = next(e[attribute] for e in history["events"] if attribute in e)
            for key in keys:
                event = event[key]
            payload = event["payloads"][0]
            value = json.loads(base64.b64decode(payload["data"]))
            mutate(value)
            payload["data"] = base64.b64encode(json.dumps(value).encode()).decode()
        self.history_change(filename, change)

    def reseal_chaos_audits(self):
        report = self.reports["chaos"]
        for endpoint in ["primary", "secondary"]:
            previous, events = "0" * 64, []
            for observation in report["observations"]:
                for event in observation["audit_events_added"][endpoint]:
                    event.update(sequence=len(events) + 1, previous_sha256=previous)
                    body = {k: v for k, v in event.items() if k not in ["previous_sha256", "event_sha256"]}
                    previous = hashlib.sha256((previous + "\n" + VERIFIER.canonical(body)).encode()).hexdigest()
                    event["event_sha256"] = previous
                    events.append(event)
            report["audits"][endpoint] = copy.deepcopy(events)
        self.reseal("chaos")

    def test_fixed_recorded_bundle_passes_without_reexecution_claim(self):
        result = VERIFIER.verify(self.directory)
        self.assertTrue(result["passed"], [x for x in result["checks"] if not x["passed"]])
        self.assertEqual(result["denominators"], {"chaos_observations": 15, "approval_observations": 29, "privacy_observations": 25, "approval_histories": 5})
        self.assertFalse(result["runtime_reexecuted"])
        self.assertFalse(result["SDK_replay_performed"])
        self.assertFalse(result["connected_write_pipeline_verified"])

    def test_each_component_retains_omitted_failure_in_denominator(self):
        for kind in self.reports:
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.reports[kind])
                self.reports[kind]["observations"].pop()
                self.rejected(kind)
                self.reports[kind] = original
                self.reseal(kind)

    def test_duplicate_cases_cannot_replace_missing_cases(self):
        for kind in self.reports:
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.reports[kind])
                self.reports[kind]["observations"][-1] = copy.deepcopy(self.reports[kind]["observations"][0])
                self.rejected(kind)
                self.reports[kind] = original
                self.reseal(kind)

    def test_resealed_foreign_tenant_read_and_rehashed_audit_still_fail(self):
        item = self.observation("chaos", "healthy_primary")
        item["response"]["data"]["item"]["tenant_id"] = "beta"
        item["response"]["meta"]["tenant_id"] = "beta"
        item["audit_events_added"]["primary"][1]["output_sha256"] = VERIFIER.digest(item["response"])
        self.reseal_chaos_audits()
        self.rejected()

    def test_terminal_permission_error_cannot_fail_over_for_data(self):
        item = self.observation("chaos", "terminal_permission_denial")
        item["response"] = copy.deepcopy(VERIFIER.READ)
        item["outcome"] = "success"
        item["attempts"].append(copy.deepcopy(self.observation("chaos", "primary_killed_after_intent")["attempts"][1]))
        self.rejected("chaos")

    def test_lost_response_does_not_erase_completed_primary_read(self):
        self.observation("chaos", "primary_killed_after_durable_outcome")["audit_events_added"]["primary"] = []
        self.reseal_chaos_audits()
        self.rejected()

    def test_resealed_unfinished_intent_cannot_be_dropped(self):
        self.observation("chaos", "primary_killed_after_intent")["audit_events_added"]["primary"] = []
        self.reseal_chaos_audits()
        self.rejected()

    def test_resealed_audit_outcome_needs_its_actual_intent(self):
        self.observation("chaos", "healthy_primary")["audit_events_added"]["primary"][1]["call_id"] = "a" * 32
        self.reseal_chaos_audits()
        self.rejected()

    def test_refusal_audit_cannot_claim_a_returned_row(self):
        self.observation("chaos", "terminal_permission_denial")["audit_events_added"]["primary"][1]["row_count"] = 1
        self.reseal_chaos_audits()
        self.rejected()

    def test_boolean_is_not_an_audit_count(self):
        self.observation("chaos", "terminal_permission_denial")["audit_events_added"]["primary"][1]["row_count"] = False
        self.reseal_chaos_audits()
        self.rejected()

    def test_audit_fence_is_terminal_not_permission_to_try_secondary(self):
        self.observation("chaos", "terminal_audit_unavailable")["attempts"] *= 2
        self.rejected("chaos")

    def test_cancellation_does_not_claim_read_rollback(self):
        self.observation("chaos", "caller_cancellation_after_durable_outcome")["cancellation_did_not_rollback_read"] = False
        self.rejected("chaos")

    def test_total_deadline_cannot_invent_third_attempt(self):
        self.observation("chaos", "total_work_deadline")["attempts"] *= 3
        self.rejected("chaos")

    def test_pending_approval_is_not_a_committed_note(self):
        self.observation("approval", "approve-before-effect-ack")["response"]["effect"] = self.observation("approval", "published-after-edge-restart")["response"]["effect"]
        self.rejected("approval")

    def test_replay_must_retain_exact_original_receipt(self):
        self.observation("approval", "exact-decision-replay")["response"]["receipt"]["command"]["actor_id"] = "approver-beta"
        self.rejected("approval")

    def test_beta_approval_cannot_substitute_primary_snapshot(self):
        self.observation("approval", "beta-approved")["response"]["request"] = copy.deepcopy(self.observation("approval", "request-created")["response"]["request"])
        self.rejected("approval")

    def test_third_local_effect_cannot_hide_behind_same_key(self):
        self.reports["approval"]["local_notifications"].append(copy.deepcopy(self.reports["approval"]["local_notifications"][0]))
        self.rejected("approval")

    def test_rejected_request_cannot_publish_an_effect(self):
        self.observation("approval", "rejected")["response"]["effect"] = self.observation("approval", "published-after-edge-restart")["response"]["effect"]
        self.rejected("approval")

    def test_self_request_requires_an_independent_rejector_too(self):
        self.observation("approval", "independent-reviewer-rejects-self-request")["response"]["receipt"]["command"]["actor_id"] = "dual-alpha"
        self.rejected("approval")

    def test_local_note_cannot_grow_a_corpus_revision(self):
        self.observation("approval", "published-after-edge-restart")["response"]["revision"] = 1
        self.rejected("approval")

    def test_conflicting_same_command_must_stay_denied(self):
        value = self.observation("approval", "changed-actor-same-command-conflict")
        value["http_status"] = 200
        value["response"] = copy.deepcopy(self.observation("approval", "published-after-edge-restart")["response"])
        self.rejected("approval")

    def test_resealed_history_start_cannot_change_tenant(self):
        self.payload_change("alpha-publish-study-note.json", "workflowExecutionStartedEventAttributes", ["input"], lambda v: v.update(tenant_id="beta"))
        self.rejected()

    def test_resealed_Update_payload_must_match_accepted_command(self):
        self.payload_change("alpha-publish-study-note.json", "workflowExecutionUpdateAcceptedEventAttributes", ["acceptedRequest", "input", "args"], lambda v: v.update(actor_id="approver-beta"))
        self.rejected()

    def test_resealed_activity_ack_cannot_substitute_other_tenant_effect(self):
        self.payload_change("alpha-publish-study-note.json", "activityTaskCompletedEventAttributes", ["result"], lambda v: v.update(tenant_id="beta"))
        self.rejected()

    def test_resealed_history_cannot_hide_attempt_one_as_retry(self):
        def change(history):
            for event in history["events"]:
                if "activityTaskStartedEventAttributes" in event:
                    event["activityTaskStartedEventAttributes"]["attempt"] = 1
        self.history_change("alpha-publish-study-note.json", change)
        self.rejected()

    def test_resealed_rejected_history_cannot_schedule_effect(self):
        self.history_change("alpha-reject-study-note.json", lambda h: h["events"][1].update(eventType="EVENT_TYPE_ACTIVITY_TASK_SCHEDULED", activityTaskScheduledEventAttributes={}))
        self.rejected()

    def test_missing_history_cannot_become_a_smaller_replay_denominator(self):
        (self.directory / "approval/histories/alpha-self-review.json").unlink()
        self.reports["approval"]["histories"].pop(3)
        self.rejected("approval")

    def test_other_client_denial_cannot_return_plaintext(self):
        value = self.observation("privacy", "other_client_same_tenant")
        value["response"]["restored_text"] = VERIFIER.TEXT
        self.rejected("privacy")

    def test_restoration_cannot_swap_context_failure_for_success(self):
        value = self.observation("privacy", "copied_other_context_token")
        value.update(http_status=200, response={"restored_text": VERIFIER.TEXT})
        self.rejected("privacy")

    def test_same_input_must_mint_another_context_and_tokens(self):
        self.observation("privacy", "same_values_new_context")["response"] = copy.deepcopy(self.observation("privacy", "redact")["response"])
        self.rejected("privacy")

    def test_processor_cannot_receive_original_supported_value(self):
        self.reports["privacy"]["processor_requests"][0]["text"] = VERIFIER.TEXT
        self.rejected("privacy")

    def test_processor_cannot_receive_authorization_or_context(self):
        self.reports["privacy"]["processor_requests"][0].update(authorization_present=True, context_id="b" * 32)
        self.rejected("privacy")

    def test_processor_calls_cannot_reuse_one_redaction_map(self):
        self.reports["privacy"]["processor_requests"][1] = copy.deepcopy(self.reports["privacy"]["processor_requests"][0])
        self.rejected("privacy")

    def test_privacy_audit_cannot_contain_plaintext_metadata(self):
        self.reports["privacy"]["audit"][0]["text"] = "PRIVATE-CONTENT-CANARY"
        result = self.rejected("privacy")
        self.assertNotIn("PRIVATE-CONTENT-CANARY", json.dumps(result))

    def test_log_scan_cannot_hide_a_known_match(self):
        self.reports["privacy"]["log_scan"]["matched_supported_values_or_bearer_tokens"] = 1
        self.rejected("privacy")

    def test_fixed_source_inventory_cannot_be_resealed_to_other_code(self):
        path = self.directory / "wheel-source-inventory.json"
        value = json.loads(path.read_text())
        value["package_files"][0]["sha256"] = "a" * 64
        write_json(path, value)
        self.rejected()

    def test_partial_runtime_lock_inventory_is_not_full_binding(self):
        self.provenance["all_existing_requirement_locks_preserved"].pop("requirements-generator.lock")
        write_json(self.directory / "provenance.json", self.provenance)
        self.rejected()

    def test_resealed_partial_source_fingerprints_do_not_bind_the_whole_component(self):
        self.reports["chaos"]["source_sha256"].pop("chaos/client.py")
        self.rejected("chaos")

    def test_resealed_terminal_event_cannot_keep_success_attributes_with_failed_type(self):
        self.history_change("alpha-publish-study-note.json", lambda h: h["events"][-1].update(eventType="EVENT_TYPE_WORKFLOW_EXECUTION_FAILED"))
        self.rejected()

    def test_resealed_expired_history_cannot_invent_a_completed_decision(self):
        def change(history):
            event = history["events"][1]
            for key in list(event):
                if key.endswith("EventAttributes"):
                    del event[key]
            event.update(eventType="EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_COMPLETED", workflowExecutionUpdateCompletedEventAttributes={"meta": {"updateId": "invented"}})
        self.history_change("alpha-expire-study-note.json", change)
        self.rejected()

    def test_resealed_activity_type_requires_its_corresponding_attributes(self):
        def change(history):
            event = next(e for e in history["events"] if "activityTaskStartedEventAttributes" in e)
            del event["activityTaskStartedEventAttributes"]
        self.history_change("beta-publish-study-note.json", change)
        self.rejected()

    def test_resealed_audit_phase_cannot_substitute_an_outcome_label(self):
        self.observation("chaos", "healthy_primary")["audit_events_added"]["primary"][-1]["phase"] = "read-completed"
        self.reseal_chaos_audits()
        self.rejected()

    def test_resealed_success_audit_cannot_retain_a_permission_error(self):
        self.observation("chaos", "healthy_primary")["audit_events_added"]["primary"][-1]["error_code"] = "forbidden"
        self.reseal_chaos_audits()
        self.rejected()

    def test_huge_recorded_timing_is_refused_without_integer_overflow(self):
        self.observation("chaos", "healthy_primary")["attempts"][0]["wall_seconds"] = 10**400
        self.rejected("chaos")

    def test_artifact_hashes_do_not_turn_recorded_evidence_into_a_pipeline(self):
        self.provenance["connected_privacy_to_MCP_write_pipeline"] = True
        write_json(self.directory / "provenance.json", self.provenance)
        self.rejected()

    def test_private_content_inside_resealed_base64_payload_is_not_echoed(self):
        canary = "/Users/PRIVATE-PATH-CANARY/note"
        self.payload_change("alpha-publish-study-note.json", "workflowExecutionStartedEventAttributes", ["input"], lambda v: v["payload"].update(body=canary))
        result = self.rejected()
        self.assertNotIn(canary, json.dumps(result))
        self.assertNotIn(str(self.directory), json.dumps(result))

    def test_duplicate_JSON_members_are_refused_after_hash_reseal(self):
        path = self.directory / "privacy/demo.json"
        path.write_text(path.read_text().replace('"synthetic_only": true', '"synthetic_only": true, "synthetic_only": true', 1))
        self.provenance["experiments"]["privacy"].update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        write_json(self.directory / "provenance.json", self.provenance)
        self.rejected()

    def test_artifact_symlink_is_refused(self):
        path = self.directory / "privacy/demo.json"
        target = path.with_name("real.json")
        path.rename(target)
        path.symlink_to(target.name)
        self.rejected()

    def test_history_hardlink_is_refused(self):
        path = self.directory / "approval/histories/alpha-self-review.json"
        os.link(path, self.directory / "alias.json")
        self.rejected()

    def test_intermediate_directory_symlink_is_refused(self):
        path = self.directory / "approval/histories"
        path.rename(self.directory / "actual-histories")
        path.symlink_to("../actual-histories")
        self.rejected()

    def test_report_directed_history_traversal_is_never_opened_or_echoed(self):
        self.reports["approval"]["histories"][0]["filename"] = "../../PRIVATE-PATH-CANARY.json"
        result = self.rejected("approval")
        self.assertNotIn("PRIVATE-PATH-CANARY", json.dumps(result))

    def test_provenance_cannot_redirect_inventory_read(self):
        self.provenance["wheel_inventory"] = "../../PRIVATE-PATH-CANARY.json"
        write_json(self.directory / "provenance.json", self.provenance)
        result = self.rejected()
        self.assertNotIn("PRIVATE-PATH-CANARY", json.dumps(result))

    def test_root_symlink_and_parent_traversal_are_refused(self):
        alias = self.root / "alias"
        alias.symlink_to("case", target_is_directory=True)
        self.assertFalse(VERIFIER.verify(alias)["passed"])
        self.assertFalse(VERIFIER.verify(self.directory / ".." / "case")["passed"])

    def test_FIFO_is_refused_without_reading_it(self):
        path = self.directory / "privacy/demo.json"
        path.unlink()
        os.mkfifo(path)
        self.rejected()

    def test_oversize_is_refused_before_hash_or_content_export(self):
        path = self.directory / "chaos/demo.json"
        path.write_bytes(b"PRIVATE-CONTENT-CANARY" * 100000)
        result = self.rejected()
        self.assertNotIn("chaos/demo.json", result["artifact_sha256"])
        self.assertNotIn("PRIVATE-CONTENT-CANARY", json.dumps(result))

    def test_CLI_exit_and_scope_do_not_import_optional_runtime(self):
        result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/verify_reliability_case.py"), "--directory", str(self.directory)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["scope"], "recorded_three_component_reliability_contract_only")


if __name__ == "__main__":
    unittest.main()
