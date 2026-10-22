"""Finite demo failure metadata never renders exception or business content."""

import json

import anyio
import pytest

from enterprise_ai_lab.chaos.__main__ import main


def test_original_error_and_failing_sink_are_preserved(tmp_path, monkeypatch):
    from enterprise_ai_lab.chaos import experiment

    failure = RuntimeError("PRIVATE exception args and path /private/owner")
    records = []

    def fail(path):
        raise failure

    def sink(record):
        records.append(record)
        raise KeyboardInterrupt("PRIVATE callback failure")

    monkeypatch.setattr(experiment, "initialize_fixture", fail)
    with pytest.raises(RuntimeError) as caught:
        anyio.run(lambda: experiment.run_demo(tmp_path / "fresh", _on_failure=sink))
    assert caught.value is failure
    assert len(records) == 1
    assert records[0]["case"] == "setup" and records[0]["stage"] == "fixture_initialize"
    assert records[0]["exception_type"] == "runtime_error"
    assert "PRIVATE" not in json.dumps(records)


def test_unexpected_read_outcome_has_case_stage_and_safe_attempt_metadata(
    tmp_path, monkeypatch
):
    from enterprise_ai_lab.chaos import experiment
    from enterprise_ai_lab.chaos.contract import ReadFailure

    class Reader:
        def __init__(self, *args, _driver, **kwargs):
            self.driver = _driver

        async def read(self, *args):
            self.driver.records.append(
                {
                    "outcome": "transport_timeout",
                    "tool_error_code": None,
                    "pid": "PRIVATE PID",
                    "response": "PRIVATE BODY",
                }
            )
            raise ReadFailure("total_deadline")

    monkeypatch.setattr(
        experiment,
        "initialize_fixture",
        lambda path: path.write_bytes(b"controlled fixture double"),
    )
    monkeypatch.setattr(experiment, "_audit", lambda endpoint: [])
    monkeypatch.setattr(experiment, "FailoverReader", Reader)
    records = []
    with pytest.raises(AssertionError) as caught:
        anyio.run(
            lambda: experiment.run_demo(tmp_path / "fresh", _on_failure=records.append)
        )
    assert caught.value.args == (("healthy_primary", "total_deadline", None),)
    record = records[0]
    assert record["case"] == "healthy_primary" and record["stage"] == "outcome_check"
    assert (
        record["expected_outcome"] == "success"
        and record["observed_outcome"] == "total_deadline"
    )
    assert (
        record["exception_type"] == "assertion_error"
        and record["failure_code"] == "unknown"
    )
    assert (
        record["attempt_count"] == 1
        and record["last_attempt_outcome"] == "transport_timeout"
    )
    assert record["tool_error_code"] is None and "PRIVATE" not in json.dumps(record)


@pytest.mark.parametrize(
    "code,expected",
    [("audit_unavailable", "audit_unavailable"), ("PRIVATE RAW CODE", "unknown")],
)
def test_known_gateway_codes_are_allowlisted(tmp_path, monkeypatch, code, expected):
    from enterprise_ai_lab.chaos import experiment
    from enterprise_ai_lab.mcp_gateway.store import GatewayError

    error = GatewayError(code, "PRIVATE error text that must not be serialized")

    def fail(path):
        raise error

    monkeypatch.setattr(experiment, "initialize_fixture", fail)
    records = []
    with pytest.raises(GatewayError) as caught:
        anyio.run(
            lambda: experiment.run_demo(tmp_path / "fresh", _on_failure=records.append)
        )
    assert caught.value is error
    assert records[0]["exception_type"] == "gateway_error"
    assert records[0]["failure_code"] == expected and "PRIVATE" not in json.dumps(
        records
    )


def test_caller_cancellation_is_not_converted_or_reported(tmp_path, monkeypatch):
    from enterprise_ai_lab.chaos import experiment

    records = []

    async def check():
        started = anyio.Event()

        async def waiting(work, diagnostic):
            started.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(experiment, "_run_demo", waiting)
        async with anyio.create_task_group() as group:
            group.start_soon(
                lambda: experiment.run_demo(
                    tmp_path / "unused", _on_failure=records.append
                )
            )
            with anyio.fail_after(1):
                await started.wait()
            group.cancel_scope.cancel()

    anyio.run(check)
    assert not records and not (tmp_path / "unused").exists()


def test_admission_read_failure_keeps_only_its_finite_code(tmp_path, monkeypatch):
    from enterprise_ai_lab.chaos import experiment
    from enterprise_ai_lab.chaos.contract import ReadFailure

    error = ReadFailure("provenance_mismatch")

    def reader(*args, **kwargs):
        raise error

    monkeypatch.setattr(
        experiment,
        "initialize_fixture",
        lambda path: path.write_bytes(b"controlled fixture double"),
    )
    monkeypatch.setattr(experiment, "_audit", lambda endpoint: [])
    monkeypatch.setattr(experiment, "FailoverReader", reader)
    records = []
    with pytest.raises(ReadFailure) as caught:
        anyio.run(
            lambda: experiment.run_demo(tmp_path / "fresh", _on_failure=records.append)
        )
    assert caught.value is error
    assert (
        records[0]["case"] == "healthy_primary" and records[0]["stage"] == "admission"
    )
    assert records[0]["exception_type"] == "read_failure"
    assert (
        records[0]["failure_code"] == "provenance_mismatch"
        and records[0]["attempt_count"] == 0
    )


def test_concurrent_demo_failures_keep_separate_stages(tmp_path, monkeypatch):
    """Controlled interleaving; no real protocol or inference claim."""
    from enterprise_ai_lab.chaos import experiment
    from enterprise_ai_lab.chaos.contract import ReadFailure

    records_a, records_b = [], []

    async def check():
        reading, release = anyio.Event(), anyio.Event()

        def initialize(path):
            if path.parent.name == "run-b":
                raise RuntimeError("PRIVATE B")
            path.write_bytes(b"controlled fixture double")

        class Reader:
            def __init__(self, *args, _driver, **kwargs):
                self.driver = _driver

            async def read(self, *args):
                reading.set()
                await release.wait()
                self.driver.records.append({"outcome": "transport_timeout"})
                raise ReadFailure("total_deadline")

        monkeypatch.setattr(experiment, "initialize_fixture", initialize)
        monkeypatch.setattr(experiment, "_audit", lambda endpoint: [])
        monkeypatch.setattr(experiment, "FailoverReader", Reader)

        async def a():
            with pytest.raises(AssertionError):
                await experiment.run_demo(
                    tmp_path / "run-a", _on_failure=records_a.append
                )

        async def b():
            await reading.wait()
            try:
                with pytest.raises(RuntimeError):
                    await experiment.run_demo(
                        tmp_path / "run-b", _on_failure=records_b.append
                    )
            finally:
                release.set()

        with anyio.fail_after(1):
            async with anyio.create_task_group() as group:
                group.start_soon(a)
                group.start_soon(b)

    anyio.run(check)
    assert (records_a[0]["case"], records_a[0]["stage"]) == (
        "healthy_primary",
        "outcome_check",
    )
    assert (records_b[0]["case"], records_b[0]["stage"]) == (
        "setup",
        "fixture_initialize",
    )
    assert "PRIVATE" not in json.dumps([records_a, records_b])


def test_untrusted_attempt_container_has_no_invented_count():
    from enterprise_ai_lab.chaos.diagnostics import DemoDiagnostics, format_failure

    tracker = DemoDiagnostics()
    tracker.bind([{"outcome": "response"}] * 3)
    record = tracker.failure(Exception("PRIVATE"))
    assert (
        record["attempt_count"] is None
        and record["last_attempt_outcome"] == "unconfirmed"
    )
    assert "PRIVATE" not in format_failure(record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("case", "PRIVATE"),
        ("stage", "PRIVATE"),
        ("exception_type", "SecretClass"),
        ("failure_code", "PRIVATE"),
        ("expected_outcome", float("nan")),
        ("observed_outcome", "PRIVATE"),
        ("attempt_count", True),
        ("attempt_count", 3),
        ("attempt_count", -1),
        ("last_attempt_outcome", "PRIVATE"),
        ("tool_error_code", "PRIVATE"),
    ],
)
def test_strict_failure_schema_rejects_unknown_values(field, value):
    from enterprise_ai_lab.chaos.diagnostics import DemoDiagnostics, validate_failure

    record = DemoDiagnostics().failure(Exception("PRIVATE"))
    record[field] = value
    with pytest.raises(ValueError):
        validate_failure(record)


def test_formatter_ignores_raw_records_and_is_bounded():
    from enterprise_ai_lab.chaos.diagnostics import (
        DemoDiagnostics,
        format_failure,
        validate_failure,
    )

    diagnostic = DemoDiagnostics()
    diagnostic.begin("healthy_primary", "attempt_check", expected="success")
    diagnostic.bind(
        [{"outcome": "PRIVATE", "tool_error_code": "PRIVATE", "args": "PRIVATE"}]
    )
    record = diagnostic.failure(Exception("PRIVATE"))
    assert (
        record["last_attempt_outcome"] == "unknown"
        and record["tool_error_code"] is None
    )
    encoded = format_failure(record)
    assert len(encoded.encode("ascii")) < 1024 and "PRIVATE" not in encoded
    assert validate_failure(record) is not record
    record["extra"] = "PRIVATE"
    with pytest.raises(ValueError):
        format_failure(record)


def test_cli_output_refusal_and_report_write_failures_are_safe(
    tmp_path, monkeypatch, capsys
):
    existing = tmp_path / "owner.json"
    existing.write_text("PRIVATE owner data")
    assert (
        main(["demo", "--work-dir", str(tmp_path / "fresh"), "--out", str(existing)])
        == 2
    )
    record = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert record["case"] == "cli" and record["stage"] == "output_refusal"
    assert (
        existing.read_text() == "PRIVATE owner data"
        and not (tmp_path / "fresh").exists()
    )

    monkeypatch.setattr(anyio, "run", lambda *args: {"observations": []})
    parent = tmp_path / "not-a-directory"
    parent.write_text("PRIVATE owner file")
    assert (
        main(
            [
                "demo",
                "--work-dir",
                str(tmp_path / "unused"),
                "--out",
                str(parent / "result.json"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    record = json.loads(captured.err.splitlines()[-1])
    assert record["stage"] == "report_write" and record["exception_type"] == "os_error"
    assert "PRIVATE" not in captured.err and "not-a-directory" not in captured.err


def test_cli_runtime_failure_emits_finite_metadata(tmp_path, monkeypatch, capsys):
    class SecretNamedFailure(Exception):
        def __str__(self):
            raise AssertionError("exception rendering must never be used")

    failure = SecretNamedFailure("PRIVATE_BODY /private/secret owner-id password")

    def fail(*args):
        raise failure

    monkeypatch.setattr(anyio, "run", fail)
    assert (
        main(
            [
                "demo",
                "--work-dir",
                str(tmp_path / "fresh"),
                "--out",
                str(tmp_path / "report.json"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert len(lines) == 2
    record = json.loads(lines[-1])
    assert record["case"] == "cli" and record["stage"] == "demo_run"
    assert record["exception_type"] == "other"
    assert not captured.out and not (tmp_path / "fresh").exists()
    assert len(lines[-1].encode()) < 1024
    assert all(
        secret not in captured.err
        for secret in (
            "PRIVATE_BODY",
            "/private/secret",
            "owner-id",
            "password",
            "SecretNamedFailure",
        )
    )
