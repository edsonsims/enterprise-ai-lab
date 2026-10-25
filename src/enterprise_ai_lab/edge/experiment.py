"""Actual subprocess ownership, bounded framing and model-free report verification."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .contracts import (
    MAX_FRAME,
    MAX_REPORT,
    PROTOCOL_SHA256,
    EdgeError,
    canonical,
    integer,
    keys,
    parse,
    protocol,
    ready,
    result,
    rss,
    schedule,
    sha,
    source_hashes,
)

LIFETIME_SECONDS = 180
CLEANUP_SECONDS = 5
LIMITATIONS = [
    "Authored fictional inputs; no answer-quality, semantic-entailment or real customer measurement.",
    "First request follows a fresh worker/model load; OS filesystem caches are not cold-controlled.",
    "Warm request work includes formatting, tokenization, generation, decoding and validation.",
    "RSS is whole-process/all-thread lifetime highwater, not incremental model/request allocation or GPU memory.",
    "Sequential small sample; no p95/p99, capacity, energy, acceleration or model-superiority claim.",
    "CPU-only trusted local launcher/stdio; no network identity, hosted endpoint, production or NVIDIA deployment.",
    "The 10-second model decoding budget is soft; parent lifetime/cleanup budgets are separately observed.",
    "Raw model output may contain fixture text. Reports of private-document runs must remain private.",
]


class WorkerClient:
    """Own one POSIX process group; all pipe I/O shares one absolute deadline."""

    def __init__(self, command, *, lifetime=LIFETIME_SECONDS):
        self.started_ns = time.perf_counter_ns()
        self.deadline = time.monotonic() + lifetime
        self.stderr_size = 0
        self._stderr_hash = hashlib.sha256()
        self._buffer = b""
        self._selector = selectors.DefaultSelector()
        self.process = None
        self.cleanup = None
        self._stdout_eof = False
        self._setup_error = False
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                bufsize=0,
            )
            for name in ("stdin", "stdout", "stderr"):
                os.set_blocking(getattr(self.process, name).fileno(), False)
            self._selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            self._selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        except Exception:  # noqa: BLE001 - do not log private exception text.
            # Even selector/nonblocking setup can fail after a successful spawn.
            self.close(abort=True)
            if self.process is None:
                raise EdgeError("startup_failed") from None
            self._setup_error = True
        except BaseException:
            self.close(abort=True)
            raise

    def _deadline(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EdgeError("worker_timeout")
        return remaining

    def _pump(self, timeout):
        writable = False
        for key, _ in self._selector.select(max(0, timeout)):
            if key.data == "stdin":
                writable = True
                continue
            try:
                block = os.read(key.fileobj.fileno(), 4096)
            except BlockingIOError:
                continue
            if not block:
                self._selector.unregister(key.fileobj)
                if key.data == "stdout":
                    self._stdout_eof = True
            elif key.data == "stderr":
                self.stderr_size += len(block)
                self._stderr_hash.update(block)
            else:
                self._buffer += block
                if len(self._buffer) > MAX_FRAME:
                    raise EdgeError("worker_failed")
        return writable

    def read(self):
        if self._setup_error:
            raise EdgeError("startup_failed")
        while b"\n" not in self._buffer:
            self._deadline()
            if self._stdout_eof:
                raise EdgeError("worker_failed")
            self._pump(self._deadline())
        self._deadline()  # A buffered frame is not accepted after the budget.
        raw, self._buffer = self._buffer.split(b"\n", 1)
        value = parse(raw, code="worker_failed")
        if type(value) is dict and value.get("type") == "fatal":
            keys(value, {"type", "error"}, "worker_failed")
            code = value["error"]
            if type(code) is not str or code not in {"startup_failed", "invalid_input"}:
                raise EdgeError("worker_failed")
            raise EdgeError(code)
        return value

    def call(self, value):
        self._deadline()
        raw = canonical(value) + b"\n"
        if len(raw) > MAX_FRAME:
            raise EdgeError("invalid_input")
        self._selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
        try:
            pending = memoryview(raw)
            while pending:
                self._deadline()
                if self._pump(self._deadline()):
                    try:
                        written = os.write(self.process.stdin.fileno(), pending)
                    except BlockingIOError:
                        continue
                    pending = pending[written:]
        except (OSError, ValueError) as exc:
            if type(exc) is EdgeError:
                raise
            raise EdgeError("worker_failed") from exc
        finally:
            try:
                self._selector.unregister(self.process.stdin)
            except (KeyError, ValueError):
                pass
        return self.read()

    def finish_input(self):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass

    def _kill_group(self):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self, *, abort=False):
        if self.cleanup is not None:
            return self.cleanup
        started = time.monotonic()
        deadline = started + CLEANUP_SECONDS
        forced = False
        if self.process is not None:
            self.finish_input()
            try:
                # Reserve half the SINGLE cleanup budget for forced termination.
                while (
                    self.process.poll() is None
                    and not abort
                    and time.monotonic() < deadline - CLEANUP_SECONDS / 2
                ):
                    self._pump(
                        min(
                            0.02,
                            max(0, deadline - CLEANUP_SECONDS / 2 - time.monotonic()),
                        )
                    )
            except Exception:  # noqa: BLE001 - do not log private exception text.
                abort = True
            if self.process.poll() is None:
                forced = True
                self._kill_group()
            else:
                # A descendant must not keep this owned group's pipes alive.
                self._kill_group()
            try:
                self.process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise EdgeError("worker_failed") from None
            # Drain what is already observable, bounded by the same deadline.
            try:
                while self._selector.get_map() and time.monotonic() < deadline:
                    before = (
                        self.stderr_size,
                        len(self._buffer),
                        len(self._selector.get_map()),
                    )
                    self._pump(0)
                    if before == (
                        self.stderr_size,
                        len(self._buffer),
                        len(self._selector.get_map()),
                    ):
                        break
            except Exception:  # noqa: BLE001, S110 - do not log private exception text.
                pass
            finally:
                for name in ("stdin", "stdout", "stderr"):
                    try:
                        getattr(self.process, name).close()
                    except (OSError, ValueError):
                        pass
        self._selector.close()
        self.cleanup = {
            "spawned": self.process is not None,
            "reaped": self.process is None or self.process.poll() is not None,
            "returncode": None if self.process is None else self.process.returncode,
            "forced_kill": forced,
            "elapsed_ns": max(1, int((time.monotonic() - started) * 1_000_000_000)),
            "stderr_bytes": self.stderr_size,
            "stderr_sha256": self._stderr_hash.hexdigest(),
        }
        return self.cleanup


def _summary(samples):
    warm = [sample["response"] for sample in samples if sample["phase"] == "warm"]
    times = sorted(row["elapsed_ns"] for row in warm)
    median_twice = (
        0
        if not times
        else times[len(times) // 2] * 2
        if len(times) % 2
        else times[len(times) // 2 - 1] + times[len(times) // 2]
    )
    return {
        "requests_with_receipts": len(samples),
        "warm_requests_with_receipts": len(warm),
        "accepted_literal_evidence": sum(row["response"]["ok"] for row in samples),
        "generation_failures": sum(not row["response"]["ok"] for row in samples),
        "observed_model_generate_calls": sum(
            row["response"]["model_generate_calls"] for row in samples
        ),
        "observed_generated_tokens": sum(
            row["response"]["trace"]["generated_tokens"]
            for row in samples
            if row["response"]["trace"] is not None
        ),
        "requests_without_token_trace": sum(
            row["response"]["trace"] is None for row in samples
        ),
        "warm_median_elapsed_ns_times_two": median_twice,
    }


def _rss_progress(previous, current):
    rss(current)
    if (
        current["native_unit"] != previous["native_unit"]
        or current["bytes"] < previous["bytes"]
    ):
        raise EdgeError("invalid_report")


def measure(model_dir):
    # Protocol is hash-frozen before any worker/model starts.
    protocol()
    configs = []
    for threads in [1, 4]:
        command = [
            sys.executable,
            "-m",
            "enterprise_ai_lab.edge",
            "serve",
            "--model-dir",
            str(Path(model_dir).resolve()),
            "--threads",
            str(threads),
        ]
        client = None
        record = {
            "threads": threads,
            "ready": None,
            "parent_ready_elapsed_ns": None,
            "samples": [],
            "stopped": None,
            "terminal_error": None,
            "cleanup": None,
            "summary": None,
        }
        try:
            # Construct inside the boundary: a spawn failure retains this and prior configurations.
            client = WorkerClient(command)
            record["ready"] = ready(client.read(), threads)
            record["parent_ready_elapsed_ns"] = max(
                1, time.perf_counter_ns() - client.started_ns
            )
            for phase, repeat, case in schedule():
                response = result(client.call(case), case)
                previous = (
                    record["samples"][-1]["response"]["rss"]
                    if record["samples"]
                    else record["ready"]["rss"]
                )
                _rss_progress(previous, response["rss"])
                record["samples"].append(
                    {
                        "phase": phase,
                        "repetition": repeat,
                        "case_id": case["id"],
                        "response": response,
                    }
                )
            client.finish_input()
            stopped = client.read()
            keys(
                stopped,
                {"type", "requests", "model_generate_calls", "rss"},
                "invalid_report",
            )
            if (
                stopped["type"] != "stopped"
                or type(stopped["requests"]) is not int
                or stopped["requests"] != len(record["samples"])
                or type(stopped["model_generate_calls"]) is not int
                or stopped["model_generate_calls"]
                != sum(x["response"]["model_generate_calls"] for x in record["samples"])
            ):
                raise EdgeError("invalid_report")
            rss(stopped["rss"])
            _rss_progress(record["samples"][-1]["response"]["rss"], stopped["rss"])
            record["stopped"] = stopped
        except Exception as exc:  # noqa: BLE001 - retain only fixed finite metadata.
            record["terminal_error"] = (
                exc.code if type(exc) is EdgeError else "worker_failed"
            )
        finally:
            if client is not None:
                record["cleanup"] = client.close(
                    abort=record["terminal_error"] is not None
                )
            else:
                record["cleanup"] = {
                    "spawned": False,
                    "reaped": True,
                    "returncode": None,
                    "forced_kill": False,
                    "elapsed_ns": 1,
                    "stderr_bytes": 0,
                    "stderr_sha256": sha(b""),
                }
        record["summary"] = _summary(record["samples"])
        if record["terminal_error"] is None and (
            record["cleanup"]["returncode"] != 0 or record["cleanup"]["forced_kill"]
        ):
            record["terminal_error"] = "worker_failed"
        configs.append(record)
    value = {
        "schema_version": 1,
        "study": "local-cpu-inference-v1",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": source_hashes(),
        "protocol": protocol(),
        "runtime_package_version": importlib.metadata.version("enterprise-ai-lab"),
        "recorded_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "os_release": platform.release(),
            "logical_cpus": os.cpu_count(),
        },
        "worker_lifetime_seconds": LIFETIME_SECONDS,
        "cleanup_grace_seconds": CLEANUP_SECONDS,
        "configuration_order": [1, 4],
        "configurations": configs,
        "limitations": LIMITATIONS,
    }
    verify_report(value)
    return value


def verify_report(value):
    keys(
        value,
        {
            "schema_version",
            "study",
            "protocol_sha256",
            "source_sha256",
            "protocol",
            "runtime_package_version",
            "worker_lifetime_seconds",
            "cleanup_grace_seconds",
            "configuration_order",
            "configurations",
            "limitations",
            "recorded_at_utc",
            "host",
        },
        "invalid_report",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["study"] != "local-cpu-inference-v1"
        or value["protocol_sha256"] != PROTOCOL_SHA256
        or canonical(value["protocol"]) != canonical(protocol())
        or value["source_sha256"] != source_hashes()
        or value["limitations"] != LIMITATIONS
        or type(value["worker_lifetime_seconds"]) is not int
        or value["worker_lifetime_seconds"] != LIFETIME_SECONDS
        or type(value["cleanup_grace_seconds"]) is not int
        or value["cleanup_grace_seconds"] != CLEANUP_SECONDS
        or canonical(value["configuration_order"]) != canonical([1, 4])
        or type(value["configurations"]) is not list
        or len(value["configurations"]) != 2
    ):
        raise EdgeError("invalid_report")
    if type(value["runtime_package_version"]) is not str or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", value["runtime_package_version"]
    ):
        raise EdgeError("invalid_report")
    if type(value["recorded_at_utc"]) is not str:
        raise EdgeError("invalid_report")
    try:
        datetime.strptime(value["recorded_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise EdgeError("invalid_report") from exc
    host = value["host"]
    keys(host, {"system", "machine", "os_release", "logical_cpus"}, "invalid_report")
    from .contracts import text

    for name in ("system", "machine", "os_release"):
        text(host[name], 64, code="invalid_report")
    if host["system"] not in {"Darwin", "Linux"}:
        raise EdgeError("invalid_report")
    integer(host["logical_cpus"], 1, 4096)
    prompts = {}
    for config, threads in zip(value["configurations"], [1, 4]):
        keys(
            config,
            {
                "threads",
                "ready",
                "parent_ready_elapsed_ns",
                "samples",
                "stopped",
                "terminal_error",
                "cleanup",
                "summary",
            },
            "invalid_report",
        )
        if (
            type(config["threads"]) is not int
            or config["threads"] != threads
            or type(config["samples"]) is not list
            or len(config["samples"]) > 7
        ):
            raise EdgeError("invalid_report")
        if config["ready"] is not None:
            ready(config["ready"], threads)
            if (
                config["ready"]["system"] != host["system"]
                or config["ready"]["machine"] != host["machine"]
            ):
                raise EdgeError("invalid_report")
            integer(config["parent_ready_elapsed_ns"], 1, 2**63 - 1)
        elif (
            config["parent_ready_elapsed_ns"] is not None
            or config["samples"]
            or config["stopped"] is not None
        ):
            raise EdgeError("invalid_report")
        for sample, (phase, repeat, case) in zip(config["samples"], schedule()):
            keys(
                sample, {"phase", "repetition", "case_id", "response"}, "invalid_report"
            )
            if (
                sample["phase"] != phase
                or type(sample["repetition"]) is not int
                or sample["repetition"] != repeat
                or sample["case_id"] != case["id"]
            ):
                raise EdgeError("invalid_report")
            result(sample["response"], case)
            trace = sample["response"]["trace"]
            if trace is not None:
                signature = (trace["prompt_sha256"], trace["input_tokens"])
                if case["id"] in prompts and prompts[case["id"]] != signature:
                    raise EdgeError("invalid_report")
                prompts[case["id"]] = signature
        if config["terminal_error"] is None:
            if len(config["samples"]) != 7 or config["stopped"] is None:
                raise EdgeError("invalid_report")
        elif type(config["terminal_error"]) is not str or config[
            "terminal_error"
        ] not in {
            "startup_failed",
            "worker_failed",
            "worker_timeout",
            "invalid_input",
            "invalid_report",
        }:
            raise EdgeError("invalid_report")
        if config["stopped"] is not None:
            stopped = config["stopped"]
            keys(
                stopped,
                {"type", "requests", "model_generate_calls", "rss"},
                "invalid_report",
            )
            if (
                stopped["type"] != "stopped"
                or type(stopped["requests"]) is not int
                or stopped["requests"] != len(config["samples"])
                or type(stopped["model_generate_calls"]) is not int
                or stopped["model_generate_calls"]
                != sum(x["response"]["model_generate_calls"] for x in config["samples"])
            ):
                raise EdgeError("invalid_report")
            rss(stopped["rss"])
        cleanup = config["cleanup"]
        keys(
            cleanup,
            {
                "spawned",
                "reaped",
                "returncode",
                "forced_kill",
                "elapsed_ns",
                "stderr_bytes",
                "stderr_sha256",
            },
            "invalid_report",
        )
        if (
            cleanup["reaped"] is not True
            or type(cleanup["spawned"]) is not bool
            or type(cleanup["forced_kill"]) is not bool
            or (
                type(cleanup["returncode"]) is not int
                if cleanup["spawned"]
                else cleanup["returncode"] is not None
            )
        ):
            raise EdgeError("invalid_report")
        integer(cleanup["elapsed_ns"], 1, 2**63 - 1)
        integer(cleanup["stderr_bytes"], 0, 2**63 - 1)
        if type(cleanup["stderr_sha256"]) is not str or not re.fullmatch(
            r"[0-9a-f]{64}", cleanup["stderr_sha256"]
        ):
            raise EdgeError("invalid_report")
        if not cleanup["spawned"] and (
            config["ready"] is not None
            or config["terminal_error"] is None
            or cleanup["forced_kill"]
            or cleanup["stderr_bytes"]
            or cleanup["stderr_sha256"] != sha(b"")
        ):
            raise EdgeError("invalid_report")
        if config["ready"] is not None:
            records = (
                [config["ready"]]
                + [s["response"] for s in config["samples"]]
                + ([config["stopped"]] if config["stopped"] is not None else [])
            )
            previous = 0
            for row in records:
                if (
                    row["rss"]["native_unit"] != config["ready"]["rss"]["native_unit"]
                    or row["rss"]["bytes"] < previous
                ):
                    raise EdgeError("invalid_report")
                previous = row["rss"]["bytes"]
        if config["terminal_error"] is None and (
            cleanup["returncode"] != 0 or cleanup["forced_kill"]
        ):
            raise EdgeError("invalid_report")
        if canonical(config["summary"]) != canonical(_summary(config["samples"])):
            raise EdgeError("invalid_report")
    return {
        "configurations": 2,
        "requests_with_receipts": sum(
            c["summary"]["requests_with_receipts"] for c in value["configurations"]
        ),
        "observed_model_generate_calls": sum(
            c["summary"]["observed_model_generate_calls"]
            for c in value["configurations"]
        ),
        "terminal_errors": sum(
            c["terminal_error"] is not None for c in value["configurations"]
        ),
    }


def read_report(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 1 <= before.st_size <= MAX_REPORT
            ):
                raise EdgeError("invalid_report")
            raw = stream.read(MAX_REPORT + 1)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or len(raw) != before.st_size:
                raise EdgeError("invalid_report")
        return parse(raw, limit=MAX_REPORT, code="invalid_report")
    except OSError as exc:
        raise EdgeError("invalid_report") from exc


def write_report(path, value):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise EdgeError("output_exists")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise EdgeError("storage_unavailable")
    raw = (
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False).encode()
        + b"\n"
    )
    if len(raw) > MAX_REPORT:
        raise EdgeError("invalid_report")
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
    except FileExistsError as exc:
        raise EdgeError("output_exists") from exc
    except OSError as exc:
        raise EdgeError("storage_unavailable") from exc
    return sha(raw)
