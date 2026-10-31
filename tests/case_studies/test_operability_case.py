"""Corrupt recorded contracts without starting any of the four runtimes."""
from __future__ import annotations

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
SCRIPT = Path(os.environ.get("OPERABILITY_CASE_VERIFIER", ROOT / "scripts/verify_operability_case.py"))
FIXTURE = Path(os.environ.get("OPERABILITY_CASE_FIXTURE", ROOT / "reports/case-operability-v1"))
SPEC = importlib.util.spec_from_file_location("operability_verifier", SCRIPT)
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class OperabilityCaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "case"
        shutil.copytree(FIXTURE, self.directory)
        self.reports = {k: json.loads((self.directory / k / "demo.json").read_text()) for k in VERIFIER.KINDS}
        self.provenance = json.loads((self.directory / "provenance.json").read_text())

    def observation(self, kind, name):
        return next(o for o in self.reports[kind]["observations"] if o["case"] == name)

    def result(self, name):
        return self.observation("costs", name)["response"]["result"]

    def response(self, name):
        return self.observation("admin", name)["response"]

    def sample(self):
        return self.reports["edge"]["configurations"][0]["samples"][0]["response"]

    def reseal(self, kind):
        path = self.directory / kind / "demo.json"
        write_json(path, self.reports[kind])
        self.provenance["experiments"][kind].update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        write_json(self.directory / "provenance.json", self.provenance)

    def rejected(self, kind=None, contract=None):
        if kind:
            self.reseal(kind)
        result = VERIFIER.verify(self.directory)
        self.assertFalse(result["passed"])
        if contract:
            self.assertTrue(any(not x["passed"] and contract in x["check"] for x in result["checks"]), result["checks"])
        return result

    def provenance_change(self):
        write_json(self.directory / "provenance.json", self.provenance)
        return self.rejected()

    def test_frozen_bundle_passes_without_live_or_owner_claim(self):
        result = VERIFIER.verify(self.directory)
        self.assertTrue(result["passed"], [c for c in result["checks"] if not c["passed"]])
        self.assertEqual(result["denominators"], {"separate_experiments": 4, "model_requests": 14, "model_generate_calls": 14, "warm_requests": 12, "cost_observations": 31, "admin_HTTP_observations": 40, "admin_browser_checks": 9})
        for key in ["runtime_reexecuted", "model_inference_performed", "browser_started", "connected_inference_cost_admin_system_verified", "owner_review_or_study_established"]:
            self.assertIs(result[key], False)

    def test_cli_works_outside_checkout_with_isolated_stdlib(self):
        completed = subprocess.run([sys.executable, "-I", str(SCRIPT.resolve()), "--directory", str(self.directory)], cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["passed"])

    def test_report_omission_keeps_all_denominators(self):
        for kind in VERIFIER.KINDS:
            with self.subTest(kind=kind):
                path = self.directory / kind / "demo.json"
                raw = path.read_bytes()
                path.unlink()
                result = self.rejected()
                self.assertEqual(result["denominators"]["model_requests"], 14)
                path.write_bytes(raw)

    def test_missing_case_cannot_be_replaced_by_duplicate(self):
        for kind in ["costs", "admin"]:
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.reports[kind])
                self.reports[kind]["observations"][-1] = copy.deepcopy(self.reports[kind]["observations"][0])
                self.rejected(kind, "ordered-")
                self.reports[kind] = original
                self.reseal(kind)

    def test_resealed_core_cannot_be_called_a_full_optional_bundle(self):
        self.reports["offline"]["scope"]["optional_integrations_exercised"] = True
        self.rejected("offline", "core-scope-only")

    def test_fresh_optional_environment_cannot_be_inferred(self):
        self.provenance["runtime_environments"]["generator"]["dependency_environment"] = "fresh_virtual_environment"
        self.provenance_change()

    def test_core_fresh_runtime_requires_isolated_import_and_no_system_packages(self):
        for key, value in [("fresh_runtime", False), ("import_inside_fresh_runtime", False), ("system_site_packages", True)]:
            with self.subTest(key=key):
                original = self.reports["offline"]["observations"][key]
                self.reports["offline"]["observations"][key] = value
                self.rejected("offline", "fresh-core-venv")
                self.reports["offline"]["observations"][key] = original
                self.reseal("offline")

    def test_core_denial_needs_permission_error_not_refused_port(self):
        self.reports["offline"]["observations"]["network_probes"][0]["errno"] = 61
        self.rejected("offline", "actual-child-denial")

    def test_core_unconfined_delivery_control_is_required(self):
        self.reports["offline"]["network_control"]["UDP"] = "not_attempted"
        self.rejected("offline", "unconfined-control")

    def test_unsafe_lexical_baseline_cannot_be_removed_or_claimed_safe(self):
        self.reports["offline"]["observations"]["demo"]["unsafe_query_leakage"] = 0.0
        self.rejected("offline", "lexical-safe-and-unsafe")

    def test_fake_wheel_digest_with_resealed_manifest_is_not_shared_wheel(self):
        m = self.reports["offline"]["manifest"]
        m["artifacts"][0]["sha256"] = "f" * 64
        self.reports["offline"]["manifest_sha256"] = hashlib.sha256((VERIFIER.canonical(m) + "\n").encode()).hexdigest()
        self.rejected("offline", "exact-wheel")

    def test_core_manifest_cannot_gain_unexercised_models(self):
        m = self.reports["offline"]["manifest"]
        m["models"] = [{"model": "claimed-local-model"}]
        self.reports["offline"]["manifest_sha256"] = hashlib.sha256((VERIFIER.canonical(m) + "\n").encode()).hexdigest()
        self.rejected("offline", "manifest-core-not-model")

    def test_model_request_error_cannot_leave_the_denominator(self):
        self.reports["edge"]["configurations"][0]["samples"].pop()
        self.rejected("edge", "seven-receipts")

    def test_model_failure_cannot_be_hidden_by_success_summary(self):
        r = self.sample()
        r.update(ok=False, error="generation_failed", answer=None)
        r["trace"]["validated"] = False
        self.rejected("edge", "recomputed-all-error-denominators")

    def test_false_generation_call_and_coherently_faked_summary_are_rejected(self):
        c = self.reports["edge"]["configurations"][0]
        c["samples"][0]["response"]["model_generate_calls"] = 0
        c["summary"]["observed_model_generate_calls"] = 6
        c["stopped"]["model_generate_calls"] = 6
        self.rejected("edge", "receipt-shape-time-and-call")

    def test_bool_is_not_a_model_call_counter(self):
        self.sample()["model_generate_calls"] = True
        self.rejected("edge", "receipt-shape-time-and-call")

    def test_resealed_raw_output_needs_its_own_hash(self):
        self.sample()["trace"]["raw_output"] += " "
        self.rejected("edge", "raw-token-trace")

    def test_nonliteral_quote_fails_after_all_output_hashes_are_updated(self):
        r = self.sample()
        output = json.loads(r["trace"]["raw_output"])
        output["evidence"][0]["quote"] = "The fictional Alpha team may process 999 documents."
        raw = json.dumps(output)
        r["trace"]["raw_output"] = raw
        r["trace"]["raw_output_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        r["answer"]["evidence"] = copy.deepcopy(output["evidence"])
        r["answer"]["generation"]["raw_output_sha256"] = r["trace"]["raw_output_sha256"]
        self.rejected("edge", "literal-check-not-entailment")

    def test_recorded_tokens_cannot_change_with_coherent_totals(self):
        c = self.reports["edge"]["configurations"][0]
        r = self.sample()
        r["trace"]["generated_tokens"] -= 1
        r["answer"]["generation"]["generated_tokens"] -= 1
        c["summary"]["observed_generated_tokens"] -= 1
        self.rejected("edge", "recorded-token-prompt")

    def test_model_time_cannot_exceed_whole_request(self):
        self.sample()["model_generate_elapsed_ns"] = self.sample()["elapsed_ns"] + 1
        self.rejected("edge", "receipt-shape-time-and-call")

    def test_median_uses_all_six_warm_requests(self):
        self.reports["edge"]["configurations"][0]["summary"]["warm_median_elapsed_ns_times_two"] += 1
        self.rejected("edge", "recomputed-all-error-denominators")

    def test_first_request_cannot_be_relabelled_warm(self):
        self.reports["edge"]["configurations"][0]["samples"][0]["phase"] = "warm"
        self.rejected("edge", "fixed-input-order")

    def test_RSS_is_process_highwater_not_incremental_weights_memory(self):
        self.sample()["rss"]["scope"] = "incremental_model_weights"
        self.rejected("edge", "receipt-shape-time-and-call")

    def test_RSS_unit_mismatch_and_decreasing_highwater_are_rejected(self):
        r = self.sample()["rss"]
        r["native"] //= 1024
        r["native_unit"] = "KiB"
        self.rejected("edge", "receipt-shape-time-and-call")

    def test_actual_model_worker_reaping_is_required(self):
        self.reports["edge"]["configurations"][0]["cleanup"]["reaped"] = False
        self.rejected("edge", "actual-owned-worker-reaped")

    def test_fictional_event_quantity_and_hash_do_not_override_exact_charge(self):
        r = self.result("alpha-operator:shared-001")
        r["charge"] = "0.5"
        self.rejected("costs", "exact-tenant-event-charge")

    def test_malformed_decimal_fails_closed_without_echo(self):
        self.result("alpha-operator:shared-001")["charge"] = "PRIVATE_CANARY_INVALID_DECIMAL"
        result = self.rejected("costs")
        self.assertNotIn("PRIVATE_CANARY", json.dumps(result))

    def test_binary_float_or_bool_is_not_an_exact_usage_quantity(self):
        r = self.result("alpha-operator:shared-001")
        r["event"]["quantity"] = True
        r["event_sha256"] = VERIFIER.digest({"tenant_id": r["tenant_id"], "event": r["event"], "rate_card_sha256": r["rate_card_sha256"]})
        self.rejected("costs", "exact-tenant-event-charge")

    def test_same_event_in_two_tenants_does_not_merge_receipts(self):
        self.result("beta-operator:shared-001")["receipt_id"] = self.result("alpha-operator:shared-001")["receipt_id"]
        self.rejected("costs", "exact-tenant-event-charge")

    def test_exact_replay_does_not_allow_another_receipt_or_actor(self):
        self.result("same-event-new-process")["first_actor"] = "local:beta-operator"
        self.rejected("costs", "exact-replay-survives")

    def test_aggregate_first_total_cannot_be_sum_of_rounded_lines(self):
        self.result("alpha-summary-after-reopen")["settled_total"] = "0.56"
        self.rejected("costs", "exact-aggregate-first")

    def test_statement_anomaly_cannot_be_erased_after_hash_reseal(self):
        for name, field, value in [("missing", "missing_events", []), ("extra", "extra_events", []), ("wrong-amount", "amount_mismatches", []), ("currency", "currency_mismatch", False), ("rounded-total", "settled_total_mismatch", False)]:
            with self.subTest(name=name):
                original = copy.deepcopy(self.reports["costs"])
                self.result(name)["result"][field] = value
                self.rejected("costs", "independent-statement-anomaly")
                self.reports["costs"] = original
                self.reseal("costs")

    def test_old_statement_cannot_advance_watermark_on_replay(self):
        self.result("statement-frozen-after-later-append")["watermark"] = 6
        self.rejected("costs", "statement-replay-keeps")

    def test_foreign_and_absent_ids_keep_the_same_withheld_response(self):
        self.observation("costs", "unreadable-foreign-id")["response"] = {"ok": True, "result": {"tenant_id": "beta"}}
        self.rejected("costs", "observation-26:envelope")

    def test_lost_receipt_needs_the_same_committed_identity(self):
        self.reports["costs"]["lost_receipt"]["committed_receipt_id"] = "f" * 32
        self.rejected("costs", "lost-ACK-exact")

    def test_two_concurrent_primary_writes_are_not_one_effect(self):
        o = self.observation("costs", "concurrent-identical-processes")
        for response in o["responses"]:
            response["replayed"] = False
        for attempt in o["initial_attempts"]:
            attempt["response"]["result"]["replayed"] = False
        self.rejected("costs", "concurrent-writers-one-primary")

    def test_busy_initial_concurrent_attempt_cannot_be_hidden(self):
        o = self.observation("costs", "concurrent-identical-processes")
        o["initial_attempts"][0] = {"process_exit": 2, "response": {"ok": False, "error": "resource_busy"}}
        self.rejected("costs", "concurrent-pair-fields")

    def test_actual_ledger_process_count_and_cleanup_are_not_bools(self):
        self.reports["costs"]["actual_processes"] = True
        self.rejected("costs", "all-33-owned-processes")

    def test_admin_user_identity_cannot_be_replaced_with_client_credentials(self):
        self.reports["admin"]["oidc"]["flow"] = "client_credentials"
        self.rejected("admin", "OIDC-not-owner")

    def test_observer_role_cannot_gain_read_or_configuration(self):
        self.response("login-observer-alpha")["can_configure"] = True
        self.rejected("admin", "signed-user-role-scope")

    def test_admin_denial_cannot_include_data_even_with_right_error(self):
        self.response("csrf-denied")["items"] = [{"tenant_id": "alpha"}]
        self.rejected("admin", "denial-withholds-data")

    def test_receipt_replay_cannot_create_another_revision(self):
        self.response("alpha-exact-replay")["revision"] = 2
        self.rejected("admin", "exact-receipt-not-new-revision")

    def test_revision_CAS_conflict_must_remain_409(self):
        self.observation("admin", "stale-revision-conflict")["http_status"] = 200
        self.rejected("admin", "HTTP-fields-status")

    def test_rollback_appends_revision_instead_of_erasing_history(self):
        self.response("rollback-new-revision")["revision"] = 0
        self.rejected("admin", "atomic-receipt-chain-CAS")

    def test_cross_tenant_configuration_read_is_not_allowed(self):
        self.response("configured-read")["items"][0]["tenant_id"] = "beta"
        self.rejected("admin", "source-read-filtered-tenant")

    def test_audit_hash_reseal_does_not_admit_foreign_tenant(self):
        event = self.response("final-alpha-audit")["events"][0]
        event["tenant_id"] = "beta"
        event["audit_hash"] = VERIFIER.digest({k: v for k, v in event.items() if k != "audit_hash"})
        self.rejected("admin", "tenant-audit-no-foreign")

    def test_after_commit_lost_response_cannot_be_counted_twice(self):
        self.reports["admin"]["faults"][1]["revision_after_explicit_retry"] = 5
        self.rejected("admin", "explicit-identical-retry")

    def test_retry_does_not_imply_automatic_replacement_command(self):
        self.reports["admin"]["faults"][0]["automatic_retry"] = True
        self.rejected("admin", "explicit-identical-retry")

    def test_faults_cannot_share_one_command_body(self):
        self.reports["admin"]["faults"][1]["immutable_command_sha256"] = self.reports["admin"]["faults"][0]["immutable_command_sha256"]
        self.rejected("admin", "explicit-identical-retry")

    def test_browser_summary_cannot_hide_missing_loading_guard(self):
        self.reports["admin"]["browser"]["checks"].pop(0)
        self.rejected("admin", "actual-browser-checks")

    def test_browser_success_is_not_owner_review(self):
        self.reports["admin"]["browser"]["human_review_performed"] = True
        self.rejected("admin", "actual-browser-checks")

    def test_browser_mobile_viewport_cannot_be_wider_than_claimed(self):
        self.reports["admin"]["browser"]["checks"][7]["viewport_width"] = 1200
        self.rejected("admin", "actual-browser-checks")

    def test_admin_service_children_must_be_reaped(self):
        self.reports["admin"]["processes"]["all_reaped"] = False
        self.rejected("admin", "seven-owned-service")

    def test_browser_lock_and_observed_version_must_agree(self):
        self.provenance["development_browser"]["locked_playwright_version"] = "0.0.0"
        self.provenance_change()

    def test_launcher_hash_cannot_claim_entire_browser_distribution(self):
        self.provenance["development_browser"]["chromium_fingerprint_scope"] = "entire_distribution"
        self.provenance_change()

    def test_optional_report_source_drift_cannot_match_same_wheel(self):
        self.reports["admin"]["source_sha256"]["enterprise_ai_lab/admin/core.py"] = "f" * 64
        self.rejected("admin", "all-package-source-fingerprints")

    def test_package_inventory_cannot_be_rehashed_to_another_source(self):
        path = self.directory / "wheel-source-inventory.json"
        x = json.loads(path.read_text())
        x["package_files"]["enterprise_ai_lab/admin/core.py"] = "f" * 64
        write_json(path, x)
        self.rejected()

    def test_tokenizer_runtime_drift_cannot_be_hidden_in_one_config(self):
        self.reports["edge"]["configurations"][1]["ready"]["metadata"]["runtime"]["tokenizers"] = "0.0.0"
        self.rejected("edge", "pinned-local-model-assets")

    def test_protocol_payload_change_cannot_hide_behind_original_raw_hash(self):
        self.reports["edge"]["protocol"]["cases"][0]["question"] = "Another question"
        self.rejected("edge", "frozen-input-protocol-bytes-and-values")

    def test_costs_cannot_be_relabelled_as_inference_billing(self):
        self.provenance["costs_are_inference_telemetry"] = True
        self.provenance_change()

    def test_full_roadmap_completion_or_owner_study_is_not_inferred(self):
        self.provenance["full_roadmap_15_complete"] = True
        self.provenance["owner_personal_study_recorded"] = True
        self.provenance_change()

    def test_actual_edge_network_control_cannot_be_a_refused_port(self):
        self.provenance["edge_network_denial"]["TCP"]["errno"] = 61
        self.provenance_change()

    def test_report_fingerprint_does_not_accept_mutated_bytes(self):
        path = self.directory / "costs/demo.json"
        path.write_bytes(path.read_bytes() + b" ")
        self.rejected(contract="exact-artifact")

    def test_same_command_receipt_cannot_cover_two_experiments(self):
        self.provenance["experiments"]["admin"]["private_exact_command_receipt_sha256"] = self.provenance["experiments"]["edge"]["private_exact_command_receipt_sha256"]
        self.provenance_change()

    def test_report_date_is_bound_to_actual_command_window(self):
        self.reports["costs"]["recorded_at"] = "2000-01-01T00:00:00Z"
        self.rejected("costs", "time-and-distinct")

    def test_plaintext_case_phase_or_environment_canary_never_enters_diagnostics(self):
        canary = "PRIVATE_RESULT_BODY_CANARY"
        baseline_reports, baseline_provenance = copy.deepcopy(self.reports), copy.deepcopy(self.provenance)
        for target in ["cost-case", "admin-case", "admin-fault-phase", "runtime-environment"]:
            with self.subTest(target=target):
                self.reports, self.provenance = copy.deepcopy(baseline_reports), copy.deepcopy(baseline_provenance)
                if target == "cost-case":
                    self.reports["costs"]["observations"][0]["case"] = canary
                elif target == "admin-case":
                    self.reports["admin"]["observations"][0]["case"] = canary
                elif target == "admin-fault-phase":
                    self.reports["admin"]["faults"][0]["phase"] = canary
                else:
                    self.provenance["runtime_environments"][canary] = self.provenance["runtime_environments"].pop("generator")
                for kind in VERIFIER.KINDS:
                    self.reseal(kind)
                result = self.rejected()
                self.assertNotIn(canary, json.dumps(result))

    def test_known_private_path_or_credential_is_not_echoed(self):
        path = self.directory / "admin/demo.json"
        raw = path.read_bytes().replace(b'"experiment":', b'"private": "/Users/PRIVATE_CANARY/token", "experiment":', 1)
        path.write_bytes(raw)
        result = self.rejected()
        self.assertNotIn("PRIVATE_CANARY", json.dumps(result))

    def test_duplicate_JSON_members_are_rejected(self):
        path = self.directory / "provenance.json"
        path.write_text('{"schema_version":1,' + path.read_text()[1:])
        self.rejected()

    def test_nonfinite_constants_and_overflowing_exponent_are_rejected(self):
        path = self.directory / "provenance.json"
        raw = path.read_bytes()
        for value in [b"NaN", b"Infinity", b"-Infinity", b"1e999"]:
            with self.subTest(value=value):
                path.write_bytes(b'{"nonfinite":' + value + b"," + raw[1:])
                self.rejected()
        path.write_bytes(raw)

    def test_invalid_UTF8_and_deeply_nested_JSON_are_rejected(self):
        path = self.directory / "provenance.json"
        path.write_bytes(b"\xff")
        self.rejected()
        path.write_bytes(b"[" * 100 + b"0" + b"]" * 100)
        self.rejected()

    def test_report_file_symlink_is_refused(self):
        path = self.directory / "admin/demo.json"
        target = self.root / "outside.json"
        path.rename(target)
        path.symlink_to(target)
        self.rejected()

    def test_component_directory_symlink_is_refused(self):
        path = self.directory / "edge"
        target = self.root / "outside"
        path.rename(target)
        path.symlink_to(target, target_is_directory=True)
        self.rejected()

    def test_hardlinked_report_is_refused_even_if_bytes_match(self):
        os.link(self.directory / "edge/demo.json", self.root / "second-link.json")
        self.rejected()

    def test_nonblocking_FIFO_is_refused_without_waiting(self):
        path = self.directory / "edge/demo.json"
        path.unlink()
        os.mkfifo(path)
        self.rejected()

    def test_empty_oversized_and_directory_artifacts_are_refused(self):
        path = self.directory / "provenance.json"
        for raw in [b"", b" " * (512 * 1024 + 1)]:
            path.write_bytes(raw)
            self.rejected()
        path.unlink()
        path.mkdir()
        self.rejected()

    def test_json_paths_never_choose_files_to_open(self):
        self.provenance["wheel_inventory"] = "../outside.json"
        self.provenance_change()

    def test_limitations_cannot_be_replaced_with_quality_claim(self):
        self.reports["edge"]["limitations"] = ["Production quality and capacity proven"]
        self.rejected("edge", "recorded-limitations-retained")


if __name__ == "__main__":
    unittest.main()
