"""Finite diagnostic privacy and actual loopback HTTP status preservation.

The HTTP adapters below are controlled; real Temporal/issuer evidence is a
separate probe. These tests never claim owner review or authentication coverage.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import asyncio
import fcntl
import json
import os
from pathlib import Path
import queue
import socket
import sqlite3
import stat
from threading import Event, Thread
import time

import httpx
import pytest
from temporalio.service import RPCError, RPCStatusCode
import uvicorn

from enterprise_ai_lab.diagnostics import (
    DiagnosticRecorder, MAX_BYTES, diagnostic_scope, diagnostic_stage,
    mark_rpc_failure, read_diagnostics, validate_record,
)
from enterprise_ai_lab.publication.gateway import PublicationConflict, _rpc_stage
from enterprise_ai_lab.review.api import COOKIE, create_app
from enterprise_ai_lab.review.auth import HumanPrincipal
from enterprise_ai_lab.review.sessions import Sessions

SECRET = "private-token-document-path-tenant-SECRET"


def safe_record(**changes):
    value = {"schema_version": 1, "operation": "review_create", "stage": "review_operation",
             "category": "runtime", "exception_type": "runtime_error", "rpc_status": None, "sqlite_error": None}
    value.update(changes)
    return value


def write_owned(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)


def test_optional_sink_is_silent_and_finite_sink_never_stringifies_errors(tmp_path, capsys):
    class Hostile(Exception):
        def __str__(self):
            raise AssertionError("exception text must not be inspected")
        __repr__ = __str__
        @property
        def status(self):
            raise AssertionError("unknown status must not be inspected")
        @property
        def sqlite_errorname(self):
            raise AssertionError("unknown attributes must not be inspected")
    Hostile.__name__ = SECRET
    failure = Hostile(SECRET)
    path = tmp_path / "diagnostics.jsonl"
    recorder = DiagnosticRecorder(path)
    with diagnostic_scope("review_create"):
        assert DiagnosticRecorder().failure("review_create", failure) is False
        try:
            with diagnostic_stage("review_candidate_load"):
                raise failure
        except Hostile as exc:
            assert recorder.failure("review_create", exc)
    record = read_diagnostics(path)[0]
    assert record == safe_record(stage="review_candidate_load", category="other", exception_type="other")
    assert SECRET not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 1.0}, {"tenant_id": SECRET}, {"stage": SECRET},
    {"exception_type": SECRET}, {"rpc_status": SECRET}, {"sqlite_error": SECRET},
    {"category": "rpc"}, {"category": "capacity"}, {"exception_type": "rpc_error"},
])
def test_export_rejects_unknown_fields_values_and_inconsistent_pairs(change):
    with pytest.raises(ValueError):
        validate_record(safe_record(**change))


@pytest.mark.parametrize("raw", [
    b"SQLite format 3\x00private source bytes", b'{"schema_version":1}\n', b'{}',
    b'{"schema_version":1,"schema_version":1}\n', b'\xff\n', b'\n',
])
def test_existing_corrupt_sink_is_never_appended_or_exported(tmp_path, raw):
    path = tmp_path / "existing"
    write_owned(path, raw)
    assert DiagnosticRecorder(path).record(safe_record()) is False
    assert path.read_bytes() == raw
    with pytest.raises((ValueError, UnicodeError)):
        read_diagnostics(path)


def test_full_and_oversized_sink_preserve_existing_bytes(tmp_path):
    path = tmp_path / "full"
    line = (json.dumps(safe_record(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    raw = line * (MAX_BYTES // len(line))
    write_owned(path, raw)
    assert DiagnosticRecorder(path).record(safe_record()) is False
    assert path.read_bytes() == raw
    assert len(read_diagnostics(path)) == MAX_BYTES // len(line)
    write_owned(path, b"x" * (MAX_BYTES + 1))
    assert DiagnosticRecorder(path).record(safe_record()) is False
    assert path.stat().st_size == MAX_BYTES + 1
    with pytest.raises(ValueError):
        read_diagnostics(path)


def test_sink_refuses_symlink_hardlink_wrong_mode_directory_fifo_and_lock(tmp_path):
    target = tmp_path / "target"
    write_owned(target, b"")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target)
    assert DiagnosticRecorder(symlink).record(safe_record()) is False
    with pytest.raises(OSError):
        read_diagnostics(symlink)
    hardlink = tmp_path / "hardlink"
    os.link(target, hardlink)
    assert DiagnosticRecorder(hardlink).record(safe_record()) is False
    with pytest.raises(ValueError):
        read_diagnostics(hardlink)
    hardlink.unlink()
    target.chmod(0o644)
    assert DiagnosticRecorder(target).record(safe_record()) is False
    with pytest.raises(ValueError):
        read_diagnostics(target)
    assert DiagnosticRecorder(tmp_path).record(safe_record()) is False
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    assert DiagnosticRecorder(fifo).record(safe_record()) is False
    with pytest.raises(ValueError):
        read_diagnostics(fifo)
    target.chmod(0o600)
    with target.open("rb") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert DiagnosticRecorder(target).record(safe_record()) is False
        with pytest.raises(BlockingIOError):
            read_diagnostics(target)
    assert target.read_bytes() == b""
    with pytest.raises(FileNotFoundError):
        read_diagnostics(tmp_path / "absent")


@pytest.mark.parametrize("raises", [False, True])
def test_partial_append_is_best_effort_restored_without_http_exception(tmp_path, monkeypatch, raises):
    path = tmp_path / "diagnostics"
    recorder = DiagnosticRecorder(path)
    assert recorder.record(safe_record())
    before = path.read_bytes()
    original = os.write
    def partial(fd, raw):
        count = original(fd, raw[:10])
        if raises:
            raise OSError(SECRET)
        return count
    monkeypatch.setattr(os, "write", partial)
    assert recorder.record(safe_record()) is False
    assert path.read_bytes() == before
    assert len(read_diagnostics(path)) == 1


def test_deepest_stage_success_reset_and_handled_not_found_do_not_bleed(tmp_path):
    recorder = DiagnosticRecorder(tmp_path / "diagnostics")
    with diagnostic_scope("review_create"):
        try:
            with diagnostic_stage("review_submit"):
                with _rpc_stage("temporal_query"):
                    raise RPCError(SECRET, RPCStatusCode.NOT_FOUND, SECRET.encode())
        except RPCError:
            pass  # Expected absence is handled, then a different stage fails.
        try:
            with diagnostic_stage("review_request_save"):
                with diagnostic_stage("review_view"):
                    pass
                raise RuntimeError(SECRET)
        except RuntimeError as exc:
            assert recorder.failure("review_create", exc)
    assert read_diagnostics(recorder.path) == [safe_record(stage="review_request_save")]
    assert recorder.failure("review_create", RuntimeError(SECRET)) is False
    with diagnostic_scope("review_list"):
        assert recorder.failure("review_list", RuntimeError(SECRET))
    assert read_diagnostics(recorder.path)[1] == safe_record(operation="review_list")


def test_rpc_translation_uses_final_exception_identity_not_private_cause(tmp_path):
    recorder = DiagnosticRecorder(tmp_path / "diagnostics")
    with diagnostic_scope("review_create"):
        try:
            with diagnostic_stage("review_submit"):
                try:
                    with _rpc_stage("temporal_query"):
                        raise RPCError(SECRET, RPCStatusCode.UNAVAILABLE, SECRET.encode())
                except RPCError as cause:
                    raise RuntimeError(SECRET) from cause
        except RuntimeError as exc:
            assert recorder.failure("review_create", exc)
    assert read_diagnostics(recorder.path) == [safe_record(stage="review_submit")]


def test_sqlite_error_attributes_are_filtered_to_real_codes(tmp_path):
    recorder = DiagnosticRecorder(tmp_path / "diagnostics")
    connection = sqlite3.connect(":memory:")
    try:
        with diagnostic_scope("review_create"):
            try:
                with diagnostic_stage("review_request_save"):
                    connection.execute("SELECT missing FROM unknown_table")
            except sqlite3.OperationalError as exc:
                assert recorder.failure("review_create", exc)
            error = sqlite3.OperationalError(SECRET)
            error.sqlite_errorname = SECRET
            assert recorder.failure("review_create", error)
    finally:
        connection.close()
    records = read_diagnostics(recorder.path)
    assert records[0] == safe_record(stage="review_request_save", category="sqlite",
        exception_type="sqlite_operational_error", sqlite_error="SQLITE_ERROR")
    assert records[1]["sqlite_error"] is None
    assert SECRET not in recorder.path.read_text()


def test_concurrent_contexts_keep_operation_stage_and_rpc_status_separate(tmp_path):
    recorder = DiagnosticRecorder(tmp_path / "diagnostics")
    async def check():
        first, second = asyncio.Event(), asyncio.Event()
        async def rpc():
            with diagnostic_scope("review_create"):
                try:
                    with _rpc_stage("temporal_start"):
                        first.set()
                        await second.wait()
                        raise RPCError(SECRET, RPCStatusCode.DEADLINE_EXCEEDED, SECRET.encode())
                except RPCError as exc:
                    assert recorder.failure("review_create", exc)
        async def local():
            await first.wait()
            with diagnostic_scope("review_audit"):
                try:
                    with diagnostic_stage("review_reconcile"):
                        second.set()
                        await asyncio.sleep(0)
                        raise RuntimeError(SECRET)
                except RuntimeError as exc:
                    assert recorder.failure("review_audit", exc)
        await asyncio.wait_for(asyncio.gather(rpc(), local()), timeout=2)
    asyncio.run(check())
    records = {row["operation"]: row for row in read_diagnostics(recorder.path)}
    assert records["review_create"] == safe_record(stage="temporal_start", category="rpc",
        exception_type="rpc_error", rpc_status="DEADLINE_EXCEEDED")
    assert records["review_audit"] == safe_record(operation="review_audit", stage="review_reconcile")


class ControlledVerifier:
    """Fixed local principal; not an OIDC/signature test."""
    def verify(self, _token):
        return HumanPrincipal("alpha", ("reader",), "fixture-user"), ("review:read",)


@contextmanager
def live_portal(service, recorder):
    sessions = Sessions()
    cookie = sessions.create(SECRET, int(time.time()) + 300)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = create_app(origin=origin, verifier=ControlledVerifier(), service=service,
                     assets=None, sessions=sessions, diagnostics=recorder)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical"))
    thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline
        time.sleep(.005)
    client = httpx.Client(base_url=origin, trust_env=False, timeout=3)
    client.cookies.set(COOKIE, cookie)
    try:
        yield client, sessions
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


class ControlledFailureService:
    def __init__(self, error):
        self.error = error
    async def list_requests(self, _principal, _scopes):
        with _rpc_stage("temporal_query"):
            raise self.error


def test_actual_http_rpc_503_records_only_finite_deepest_metadata(tmp_path):
    recorder = DiagnosticRecorder(tmp_path / "diagnostics")
    error = RPCError(SECRET, RPCStatusCode.DEADLINE_EXCEEDED, SECRET.encode())
    with live_portal(ControlledFailureService(error), recorder) as (client, _sessions):
        response = client.get("/api/reviews")
    assert response.status_code == 503 and response.json()["error"]["code"] == "unavailable"
    assert SECRET not in response.text + recorder.path.read_text()
    assert read_diagnostics(recorder.path) == [safe_record(operation="review_list", stage="temporal_query",
        category="rpc", exception_type="rpc_error", rpc_status="DEADLINE_EXCEEDED")]


@pytest.mark.parametrize("error,status", [(PermissionError(SECRET), 403), (LookupError(SECRET), 404),
    (PublicationConflict(SECRET), 409), (ValueError(SECRET), 422)])
def test_actual_http_expected_4xx_have_no_failure_events(tmp_path, error, status):
    recorder = DiagnosticRecorder(tmp_path / "diagnostics")
    with live_portal(ControlledFailureService(error), recorder) as (client, _sessions):
        assert client.get("/api/reviews").status_code == status
        client.cookies.clear()
        assert client.get("/api/reviews").status_code == 401
    assert not recorder.path.exists()


def test_actual_http_sink_failure_does_not_change_503_or_corrupt_existing_file(tmp_path):
    path = tmp_path / "existing.sqlite"
    write_owned(path, b"SQLite format 3\x00private source bytes")
    before = path.read_bytes()
    with live_portal(ControlledFailureService(RuntimeError(SECRET)), DiagnosticRecorder(path)) as (client, _sessions):
        assert client.get("/api/reviews").status_code == 503
    assert path.read_bytes() == before


def test_actual_http_inflight_capacity_is_separate_safe_metadata(tmp_path):
    entered, release = queue.Queue(), Event()
    class WaitingService:
        async def list_requests(self, _principal, _scopes):
            entered.put_nowait(True)
            while not release.is_set():
                await asyncio.sleep(.001)
            return []
    recorder = DiagnosticRecorder(tmp_path / "diagnostics")
    with live_portal(WaitingService(), recorder) as (client, _sessions):
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(client.get, "/api/reviews") for _ in range(16)]
            try:
                for _ in range(16):
                    entered.get(timeout=2)
                assert client.get("/api/reviews").status_code == 503
            finally:
                release.set()
            assert all(future.result(timeout=3).status_code == 200 for future in futures)
    assert read_diagnostics(recorder.path) == [safe_record(operation="review_list", stage="admission",
        category="capacity", exception_type="none")]


def test_actual_http_login_capacity_is_safe_and_does_not_echo_exception(tmp_path):
    recorder = DiagnosticRecorder(tmp_path / "diagnostics")
    with live_portal(ControlledFailureService(RuntimeError(SECRET)), recorder) as (client, sessions):
        for _ in range(64):
            sessions.login()
        response = client.get("/login")
    assert response.status_code == 503
    assert SECRET not in response.text + recorder.path.read_text()
    assert read_diagnostics(recorder.path) == [safe_record(operation="review_login", stage="admission",
        category="capacity", exception_type="none")]
