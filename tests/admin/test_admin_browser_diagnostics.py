"""Controlled driver results and real private-file admission; no browser claim."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from enterprise_ai_lab.admin import integration

PRIVATE = "PRIVATE-URL-TOKEN-PASSWORD-MARKER"


def report_file(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def invoke(tmp_path):
    return integration._run_browser(
        node="controlled-node",
        script=tmp_path / "driver.mjs",
        origin="http://127.0.0.1:8129",
        accounts=tmp_path / "accounts.private.json",
        output=tmp_path / "browser.json",
        cwd=tmp_path,
    )


def result(monkeypatch, code):
    observed = []

    def controlled(command, **kwargs):
        observed.append((command, kwargs))
        return SimpleNamespace(
            returncode=code, stdout=PRIVATE.encode(), stderr=PRIVATE.encode()
        )

    monkeypatch.setattr(integration.subprocess, "run", controlled)
    return observed


@pytest.mark.parametrize(
    "stage",
    [
        "launch",
        "alpha_login",
        "login_navigation",
        "login_form",
        "login_redirect",
        "login_view",
        "login_session",
        "initial_view_guard",
        "keyboard_update",
        "rollback",
        "saved_command_recovery",
        "desktop_pixels",
        "auditor",
        "beta",
        "mobile",
    ],
)
def test_nonzero_driver_exposes_only_fixed_stage_code_and_observed_returncode(
    tmp_path, monkeypatch, stage
):
    report_file(
        tmp_path / "browser.json",
        {
            "schema_version": 1,
            "passed": False,
            "failure_stage": stage,
            "raw_failure": PRIVATE,
        },
    )
    observed = result(monkeypatch, 2)
    with pytest.raises(integration.BrowserFailure) as caught:
        invoke(tmp_path)
    error = caught.value
    assert (error.code, error.failure_stage, error.returncode) == (
        "browser_nonzero",
        stage,
        2,
    )
    assert PRIVATE not in str(error) and PRIVATE not in repr(error)
    assert not hasattr(error, "stdout") and not hasattr(error, "stderr")
    assert observed[0][1]["timeout"] == 90 and observed[0][1]["capture_output"] is True


@pytest.mark.parametrize("stage", [PRIVATE, {"secret": PRIVATE}, None, 1])
def test_unknown_or_private_stage_is_never_reflected(tmp_path, monkeypatch, stage):
    report_file(tmp_path / "browser.json", {"failure_stage": stage, "passed": True})
    result(monkeypatch, 2)
    with pytest.raises(integration.BrowserFailure) as caught:
        invoke(tmp_path)
    assert caught.value.failure_stage == "unknown"
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("code", [True, None, -256, 65536])
def test_only_bounded_exact_integer_returncode_is_exposed(tmp_path, monkeypatch, code):
    result(monkeypatch, code)
    with pytest.raises(integration.BrowserFailure) as caught:
        invoke(tmp_path)
    assert caught.value.returncode is None
    assert caught.value.code == "browser_nonzero"


def test_timeout_exposes_existing_safe_stage_without_raw_exception_output(
    tmp_path, monkeypatch
):
    report_file(tmp_path / "browser.json", {"failure_stage": "mobile", "passed": False})

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(PRIVATE, 90, output=PRIVATE, stderr=PRIVATE)

    monkeypatch.setattr(integration.subprocess, "run", timeout)
    with pytest.raises(integration.BrowserFailure) as caught:
        invoke(tmp_path)
    assert (caught.value.code, caught.value.failure_stage, caught.value.returncode) == (
        "browser_timeout",
        "mobile",
        None,
    )
    assert caught.value.__suppress_context__ is True
    assert PRIVATE not in str(caught.value) and PRIVATE not in repr(caught.value)


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "malformed",
        "duplicate",
        "symlink",
        "hardlink",
        "oversize",
        "mode",
        "fifo",
    ],
)
def test_unsafe_or_unavailable_report_cannot_supply_diagnostic_fields(
    tmp_path, monkeypatch, kind
):
    path = tmp_path / "browser.json"
    if kind in ("symlink", "hardlink"):
        target = report_file(tmp_path / "target.json", {"failure_stage": "mobile"})
        if kind == "symlink":
            path.symlink_to(target)
        else:
            os.link(target, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind != "missing":
        report_file(path, {"failure_stage": "mobile"})
        if kind == "malformed":
            path.write_text(PRIVATE)
        elif kind == "duplicate":
            path.write_text('{"failure_stage":"mobile","failure_stage":"beta"}')
        elif kind == "oversize":
            path.write_bytes(b" " * 32769)
        elif kind == "mode":
            path.chmod(0o644)
    result(monkeypatch, 2)
    with pytest.raises(integration.BrowserFailure) as caught:
        invoke(tmp_path)
    assert caught.value.code == "browser_nonzero"
    assert caught.value.failure_stage == "unknown"
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"schema_version": 1, "passed": False, "failure_stage": "rollback"},
        {"schema_version": True, "passed": True},
        {"schema_version": 1, "passed": 1},
    ],
)
def test_zero_exit_without_actual_success_report_has_no_pass_claim(
    tmp_path, monkeypatch, value
):
    if value is not None:
        report_file(tmp_path / "browser.json", value)
    result(monkeypatch, 0)
    with pytest.raises(integration.BrowserFailure) as caught:
        invoke(tmp_path)
    assert caught.value.code == "report_unavailable" and caught.value.returncode == 0


def test_launch_oserror_is_sanitized(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError(PRIVATE)

    monkeypatch.setattr(integration.subprocess, "run", fail)
    with pytest.raises(integration.BrowserFailure) as caught:
        invoke(tmp_path)
    assert (caught.value.code, caught.value.failure_stage, caught.value.returncode) == (
        "report_unavailable",
        "unknown",
        None,
    )
    assert PRIVATE not in str(caught.value)


def test_controlled_success_preserves_existing_report(tmp_path, monkeypatch):
    value = {"schema_version": 1, "passed": True, "checks": []}
    report_file(tmp_path / "browser.json", value)
    result(monkeypatch, 0)
    assert invoke(tmp_path) == value


def test_diagnostic_constructor_never_converts_hostile_dynamic_values():
    class Hostile:
        def __str__(self):
            raise AssertionError(PRIVATE)

    value = Hostile()
    error = integration.BrowserFailure(value, value, value)
    assert (error.code, error.failure_stage, error.returncode) == (
        "report_unavailable",
        "unknown",
        None,
    )
    assert PRIVATE not in str(error)
