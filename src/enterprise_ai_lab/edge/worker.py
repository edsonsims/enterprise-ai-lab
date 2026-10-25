"""Trusted-launcher JSONL inference node. No network/download or caller config."""

from __future__ import annotations

import os
import platform
import sys
import time

from .contracts import (
    MAX_FRAME,
    SCHEMA,
    EdgeError,
    canonical,
    parse,
    request,
    source_hashes,
)


def peak_rss():
    system = platform.system()
    if system not in {"Darwin", "Linux"}:
        raise EdgeError("unsupported_platform")
    import resource

    native = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    unit = "bytes" if system == "Darwin" else "KiB"
    return {
        "native": native,
        "native_unit": unit,
        "bytes": native * (1 if unit == "bytes" else 1024),
        "scope": "process_high_water_all_threads",
    }


def emit(value, stream):
    raw = canonical(value)
    if len(raw) > MAX_FRAME:
        raise EdgeError("worker_failed")
    stream.write(raw + b"\n")
    stream.flush()


class ModelMeter:
    """Count/time actual model.generate invocations without replacing inference."""

    def __init__(self, generator):
        self.calls = self.elapsed_ns = self.total_calls = 0
        underlying = generator.model.generate

        def measured(*args, **kwargs):
            start = time.perf_counter_ns()
            self.calls += 1
            self.total_calls += 1
            try:
                return underlying(*args, **kwargs)
            finally:
                self.elapsed_ns += max(1, time.perf_counter_ns() - start)

        generator.model.generate = measured

    def reset(self):
        self.calls = self.elapsed_ns = 0


def serve(model_dir, threads, *, stdin=None, stdout=None, generator_factory=None):
    input_stream = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout.buffer if stdout is None else stdout
    try:
        if type(threads) is not int or threads not in {1, 4}:
            raise EdgeError("invalid_input")
        if generator_factory is None:
            # Local-only adapter calls are supplemented by library offline flags.
            # The recorded experiment separately demonstrates OS network denial.
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
            os.environ["TOKENIZERS_PARALLELISM"] = "false"
            from ..rag.neural import LocalQwenGenerator

            generator_factory = LocalQwenGenerator
        started = time.perf_counter_ns()
        generator = generator_factory(model_dir, max_new_tokens=128, threads=threads)
        meter = ModelMeter(generator)
        emit(
            {
                "type": "ready",
                "schema_version": SCHEMA,
                "metadata": generator.metadata(),
                "startup_elapsed_ns": max(1, time.perf_counter_ns() - started),
                "rss": peak_rss(),
                "source_sha256": source_hashes(),
                "python": platform.python_version(),
                "system": platform.system(),
                "machine": platform.machine(),
            },
            output_stream,
        )
    except Exception:  # noqa: BLE001 - do not log private exception text.
        emit({"type": "fatal", "error": "startup_failed"}, output_stream)
        return 2
    received = 0
    while True:
        raw = input_stream.readline(MAX_FRAME + 1)
        if not raw:
            emit(
                {
                    "type": "stopped",
                    "requests": received,
                    "model_generate_calls": meter.total_calls,
                    "rss": peak_rss(),
                },
                output_stream,
            )
            return 0
        try:
            if len(raw) > MAX_FRAME or not raw.endswith(b"\n") or received >= 32:
                raise EdgeError("invalid_input")
            value = request(parse(raw))
        except Exception:  # noqa: BLE001 - do not log private exception text.
            emit({"type": "fatal", "error": "invalid_input"}, output_stream)
            return 2
        received += 1
        meter.reset()
        # Input validation in the reused adapter can fail before its own reset.
        # A failed current request must never inherit a prior request's trace.
        generator.last_trace = None
        started = time.perf_counter_ns()
        answer, ok = None, False
        try:
            answer = generator.generate(value["question"], value["chunks"])
            ok = True
        except Exception:  # noqa: BLE001, S110 - do not log private exception text.
            # Text/classes/tracebacks can contain private paths or user input.
            # The raw model trace is separately retained only in explicit reports.
            pass
        emit(
            {
                "type": "result",
                "id": value["id"],
                "ok": ok,
                "error": None if ok else "generation_failed",
                "answer": answer,
                "elapsed_ns": max(1, time.perf_counter_ns() - started),
                "model_generate_calls": meter.calls,
                "model_generate_elapsed_ns": meter.elapsed_ns,
                "trace": generator.last_trace,
                "rss": peak_rss(),
            },
            output_stream,
        )
