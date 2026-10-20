"""Real TCP/issuer/process observations, separate from TestClient and processor doubles."""
import os
from pathlib import Path

import pytest


def test_demo_refuses_existing_report_before_starting_services(tmp_path):
    from enterprise_ai_lab.privacy_proxy.integration import run_demo
    report = tmp_path / "report.json"
    report.write_text("preserved")
    with pytest.raises(ValueError, match="prior reports"):
        run_demo(tmp_path / "absent-issuer", "absent-node", tmp_path / "work", report)
    assert report.read_text() == "preserved"
    assert not (tmp_path / "work").exists()


@pytest.mark.skipif(os.environ.get("RUN_PRIVACY_INTEGRATION") != "1", reason="Explicit real local issuer run")
def test_actual_identity_http_restart_expiry_and_log_boundaries(tmp_path):
    from enterprise_ai_lab.privacy_proxy.integration import run_demo

    identity = Path(os.environ.get("IDENTITY_LAB_DIR", "../identity-boundary-lab")).resolve()
    report = run_demo(identity, os.environ.get("NODE_BINARY", "node"), tmp_path / "work", tmp_path / "report.json")
    observations = {case["case"]: case for case in report["observations"]}
    assert len(observations) == len(report["observations"]) == 25
    assert observations["restore_after_resource_restart"]["response"]["restored_text"] == report["synthetic_input"]
    assert observations["actual_context_expiry"]["http_status"] == 404
    assert observations["actual_sqlite_lock"]["http_status"] == 503
    assert report["actual_sqlite_busy_seconds"] < 2
    assert report["processes_reaped"] is True
    assert report["log_scan"]["matched_supported_values_or_bearer_tokens"] == 0
    assert report["log_scan"]["resource_processes"] == 3
    assert len(report["processor_requests"]) == 3
    assert all(call["authorization_present"] is False for call in report["processor_requests"])
    assert report["limitations"]["unsupported_name_was_forwarded"] is True
    assert report["limitations"]["external_processor_or_neural_inference"] is False
    assert all(set(row) == {"sequence", "action", "outcome", "count"} for row in report["audit"])
    # An explicit retry cannot silently overwrite a prior private vault or its evidence.
    with pytest.raises(ValueError, match="new work directory"):
        run_demo(identity, os.environ.get("NODE_BINARY", "node"), tmp_path / "work", tmp_path / "other.json")
