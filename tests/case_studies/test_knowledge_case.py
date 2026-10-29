"""Negative contracts for the fixed recorded publication-case verifier."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_knowledge_case", ROOT / "scripts/verify_knowledge_case.py")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class KnowledgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "case"
        shutil.copytree(ROOT / "reports/case-knowledge-v1", self.directory)
        self.path = self.directory / "demo.json"
        self.report = json.loads(self.path.read_text())

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2) + "\n")

    def observation(self, name):
        return next(item for item in self.report["observations"] if item["case"] == name)

    def rejected(self):
        self.save()
        result = VERIFIER.verify(self.path)
        self.assertFalse(result["passed"])
        return result

    def history_change(self, name, mutate):
        metadata = next(item for item in self.report["histories"] if item["filename"] == "histories/" + name)
        path = self.directory / metadata["filename"]
        value = json.loads(path.read_text())
        mutate(value)
        raw = (json.dumps(value, indent=2) + "\n").encode()
        path.write_bytes(raw)
        metadata["sha256"] = hashlib.sha256(raw).hexdigest()

    def test_retained_fresh_report_contract_passes(self):
        result = VERIFIER.verify(self.path)
        self.assertTrue(result["passed"])
        self.assertEqual(result["scope"], "recorded_publication_contract_only")
        self.assertEqual(result["report_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_missing_observation_cannot_hide_a_failure(self):
        self.report["observations"].pop()
        self.rejected()

    def test_duplicate_case_cannot_replace_a_missing_case(self):
        self.report["observations"][-1] = self.report["observations"][0]
        self.rejected()

    def test_wrong_denial_status_fails(self):
        self.observation("forged-approval-denied")["http_status"] = 200
        self.rejected()

    def test_boolean_is_not_an_integer_status(self):
        self.observation("forged-approval-denied")["http_status"] = True
        self.rejected()

    def test_preapproval_data_cannot_become_visible(self):
        source = self.observation("approved-native-reader")["response"]
        self.observation("staged-not-searchable")["response"] = source
        self.rejected()

    def test_approved_acl_must_remove_reader(self):
        source = self.observation("approved-native-reader")["response"]
        self.observation("approved-acl-removes-reader")["response"] = source
        self.rejected()

    def test_missing_preapproval_query_marker_cannot_drop_visibility_check(self):
        source = self.observation("approved-native-reader")["response"]
        target = self.observation("staged-not-searchable")
        target.pop("query")
        target["response"] = source
        self.rejected()

    def test_missing_acl_query_marker_cannot_drop_visibility_check(self):
        source = self.observation("approved-native-reader")["response"]
        target = self.observation("approved-acl-removes-reader")
        target.pop("query")
        target["response"] = source
        self.rejected()

    def test_extra_query_marker_on_non_query_case_is_refused(self):
        self.observation("reject-created")["query"] = VERIFIER.NATIVE_QUESTION
        self.rejected()

    def test_query_literal_must_match_fixed_case_question(self):
        self.observation("approved-native-reader")["query"] = "a different question"
        self.rejected()

    def test_query_tenant_and_client_cannot_be_consistently_relabelled(self):
        item = self.observation("approved-native-reader")
        item["tenant"], item["client"] = "beta", "reader-beta"
        item["response"]["identity"] = {"tenant_id": "beta", "client_id": "reader-beta"}
        beta_document = next(d for d in self.report["candidates"][1]["documents"] if d["provenance"]["method"] == "native")
        for citation in item["response"]["citations"]:
            old_id = citation["document_id"]
            citation["tenant_id"] = "beta"
            citation["document_id"] = beta_document["document_id"]
            citation["chunk_id"] = citation["chunk_id"].replace(old_id, beta_document["document_id"])
            for evidence in item["response"]["evidence"]:
                evidence["chunk_id"] = evidence["chunk_id"].replace(old_id, beta_document["document_id"])
        self.rejected()

    def test_cross_tenant_denial_keeps_fixed_query_metadata(self):
        item = self.observation("cross-tenant-query-denied")
        item["client"], item["tenant"] = "reader-alpha", "alpha"
        self.rejected()

    def test_denied_query_response_cannot_return_authorized_data(self):
        self.observation("cross-tenant-query-denied")["response"] = self.observation("approved-native-reader")["response"]
        self.rejected()

    def test_denied_operation_response_cannot_append_candidate_data(self):
        self.observation("foreign-candidate-denied")["response"]["candidate"] = self.report["candidates"][0]
        self.rejected()

    def test_denial_error_code_must_match_the_fixed_case(self):
        self.observation("wrong-role-denied")["response"]["error"]["code"] = "invalid_token"
        self.rejected()

    def test_citation_wrong_tenant_fails(self):
        self.observation("approved-native-reader")["response"]["citations"][0]["tenant_id"] = "beta"
        self.rejected()

    def test_citation_roles_must_match_reviewed_candidate(self):
        self.observation("approved-native-reader")["response"]["citations"][0]["allowed_roles"].append("analyst")
        self.rejected()

    def test_citation_source_cannot_be_relabelled(self):
        self.observation("approved-native-reader")["response"]["citations"][0]["provenance"]["source_page"] = 16
        self.rejected()

    def test_empty_quote_cannot_satisfy_literal_evidence(self):
        response = self.observation("approved-native-reader")["response"]
        response["citations"][0]["quote"] = ""
        response["evidence"][0]["quote"] = ""
        self.rejected()

    def test_changed_candidate_source_fails_even_if_citation_matches(self):
        for candidate in self.report["candidates"]:
            for document in candidate["documents"]:
                document["provenance"]["source_uri"] = "https://example.invalid/replacement"
        for observation in self.report["observations"]:
            for citation in observation["response"].get("citations", []):
                citation["provenance"]["source_uri"] = "https://example.invalid/replacement"
        self.rejected()

    def test_restart_must_preserve_immutable_request(self):
        self.observation("pending-after-all-services-restart")["response"]["request"]["expected_revision"] = 1
        self.rejected()

    def test_approval_is_not_a_completed_publication(self):
        self.observation("approval-accepted")["response"]["effect"] = self.report["effects"][0]
        self.rejected()

    def test_fourth_publication_is_not_ignored(self):
        self.report["effects"].append(self.report["effects"][0])
        self.rejected()

    def test_wrong_committed_revision_fails(self):
        self.report["effects"][0]["revision"] = 3
        self.rejected()

    def test_stale_approval_must_fail_without_effect(self):
        response = self.observation("stale-revision-publication-failed")["response"]
        response["publication_status"] = "published"
        response["effect"] = self.report["effects"][0]
        self.rejected()

    def test_primary_and_old_replay_cannot_share_another_tenants_effect(self):
        beta = next(effect for effect in self.report["effects"] if effect["tenant_id"] == "beta")
        self.observation("published-once-after-kill-and-server-restart")["response"]["effect"] = beta
        self.observation("old-decision-replay")["response"]["effect"] = beta
        self.rejected()

    def test_primary_final_request_remains_the_exact_created_request(self):
        self.observation("published-once-after-kill-and-server-restart")["response"]["request"]["expected_revision"] = 1
        self.rejected()

    def test_old_replay_request_remains_the_exact_created_request(self):
        self.observation("old-decision-replay")["response"]["request"]["request_id"] = "narrow-native-role"
        self.rejected()

    def test_old_replay_retains_the_exact_accepted_receipt(self):
        self.observation("old-decision-replay")["response"]["receipt"]["command"]["actor_id"] = "approver-beta"
        self.rejected()

    def test_human_review_claim_cannot_be_inferred(self):
        self.report["human_review_performed"] = True
        self.rejected()

    def test_actor_must_differ_from_requester(self):
        response = self.observation("approval-accepted")["response"]
        response["receipt"]["command"]["actor_id"] = response["request"]["requested_by"]
        self.rejected()

    def test_booleans_do_not_stand_in_for_vector_counts(self):
        self.report["index"]["before_any_approval"] = False
        self.rejected()

    def test_failed_replay_is_not_ignored(self):
        self.report["replay"]["all_passed"] = False
        self.rejected()

    def test_missing_history_fails(self):
        (self.directory / self.report["histories"][0]["filename"]).unlink()
        self.rejected()

    def test_history_paths_are_fixed_not_report_directed(self):
        self.report["histories"][0]["filename"] = "../../private.json"
        self.rejected()

    def test_changed_history_bytes_fail_hash_check(self):
        with (self.directory / self.report["histories"][0]["filename"]).open("ab") as stream:
            stream.write(b" ")
        self.rejected()

    def test_resealed_history_input_must_match_request(self):
        def mutate(history):
            payload = history["events"][0]["workflowExecutionStartedEventAttributes"]["input"]["payloads"][0]
            value = json.loads(base64.b64decode(payload["data"]))
            value["expected_revision"] = 99
            payload["data"] = base64.b64encode(json.dumps(value).encode()).decode()
        self.history_change("alpha-publish-public-pages.json", mutate)
        self.rejected()

    def test_resealed_history_cannot_substitute_another_effect(self):
        beta = next(e for e in self.report["effects"] if e["tenant_id"] == "beta")
        def mutate(history):
            payload = history["events"][-1]["workflowExecutionCompletedEventAttributes"]["result"]["payloads"][0]
            value = json.loads(base64.b64decode(payload["data"]))
            value["effect"] = beta
            payload["data"] = base64.b64encode(json.dumps(value).encode()).decode()
        self.history_change("alpha-publish-public-pages.json", mutate)
        self.rejected()

    def test_history_must_record_actual_attempt_two(self):
        def mutate(history):
            for event in history["events"]:
                if "activityTaskStartedEventAttributes" in event:
                    event["activityTaskStartedEventAttributes"]["attempt"] = 1
        self.history_change("alpha-publish-public-pages.json", mutate)
        self.rejected()

    def test_rejected_history_must_not_schedule_an_activity(self):
        self.history_change("alpha-reject-pages.json", lambda h: h["events"][1].update(eventType="EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"))
        # Keep even the reported event-type list consistent: semantics still fail.
        metadata = next(h for h in self.report["histories"] if h["request_id"] == "reject-pages")
        metadata["event_types"][1] = "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
        self.rejected()

    def test_private_canary_is_not_echoed_in_failure_codes(self):
        canary = "PRIVATE-CONTENT-CANARY"
        self.report["effects"][0]["request_id"] = canary
        result = self.rejected()
        self.assertNotIn(canary, json.dumps(result))
        self.assertNotIn(str(self.directory), json.dumps(result))

    def test_duplicate_JSON_members_are_refused(self):
        self.path.write_text(self.path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1))
        self.assertFalse(VERIFIER.verify(self.path)["passed"])

    def test_symlink_report_is_refused(self):
        target = self.directory / "actual.json"
        self.path.rename(target)
        self.path.symlink_to(target.name)
        self.assertFalse(VERIFIER.verify(self.path)["passed"])

    def test_hardlinked_history_is_refused(self):
        path = self.directory / self.report["histories"][0]["filename"]
        os.link(path, self.directory / "alias.json")
        self.rejected()

    def test_symlink_history_directory_is_refused(self):
        target = self.directory / "actual-histories"
        (self.directory / "histories").rename(target)
        (self.directory / "histories").symlink_to(target.name)
        self.rejected()

    def test_oversize_report_is_refused(self):
        self.path.write_bytes(b"x" * (1024 * 1024 + 1))
        result = VERIFIER.verify(self.path)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["report_sha256"])


if __name__ == "__main__":
    unittest.main()
