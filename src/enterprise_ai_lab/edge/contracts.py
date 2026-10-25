"""Strict bounded JSONL and source-bound measurement contracts; no model imports."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
from pathlib import Path

SCHEMA = 1
MAX_FRAME = 65_536
MAX_REPORT = 2_000_000
CODES = frozenset(
    {
        "invalid_input",
        "invalid_protocol",
        "invalid_report",
        "generation_failed",
        "startup_failed",
        "worker_failed",
        "worker_timeout",
        "output_exists",
        "storage_unavailable",
        "unsupported_platform",
    }
)
ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}\Z")
PROTOCOL_SHA256 = "f1e31ca721496b9c3d4ac0b8feff33d450807309e61ecef200935a76caaad57c"


class EdgeError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in CODES else "worker_failed"
        super().__init__(self.code)


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def keys(value, expected, code="invalid_input"):
    if type(value) is not dict or set(value) != set(expected):
        raise EdgeError(code)


def integer(value, low, high, code="invalid_report"):
    if type(value) is not int or not low <= value <= high:
        raise EdgeError(code)
    return value


def text(value, limit, *, blank=False, code="invalid_input"):
    if (
        type(value) is not str
        or (not blank and not value.strip())
        or len(value) > limit
    ):
        raise EdgeError(code)
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise EdgeError(code) from exc
    return value


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise EdgeError("invalid_input")
        result[name] = value
    return result


def _number(_value):
    raise EdgeError("invalid_input")


def parse(raw, *, limit=MAX_FRAME, code="invalid_input"):
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= limit:
            raise EdgeError(code)
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_number
        )

        # JSON float values are not used anywhere in this study's wire schema.
        def finite(item):
            if type(item) is float:
                raise EdgeError(code)
            if isinstance(item, (dict, list)):
                for child in item.values() if type(item) is dict else item:
                    finite(child)

        finite(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise EdgeError(code) from exc


def request(value):
    keys(value, {"id", "question", "chunks"})
    if not ID.fullmatch(text(value["id"], 64)):
        raise EdgeError("invalid_input")
    text(value["question"], 2048)
    rows = value["chunks"]
    if type(rows) is not list or not 1 <= len(rows) <= 8:
        raise EdgeError("invalid_input")
    seen = set()
    for row in rows:
        keys(row, {"chunk_id", "text"})
        name = text(row["chunk_id"], 128)
        text(row["text"], 24_000)
        if name in seen:
            raise EdgeError("invalid_input")
        seen.add(name)
    if len(canonical(rows)) > 24_000 or len(canonical(value)) > MAX_FRAME:
        raise EdgeError("invalid_input")
    return value


def protocol():
    raw = (
        importlib.resources.files("enterprise_ai_lab.edge")
        .joinpath("data/requests-v1.json")
        .read_bytes()
    )
    if sha(raw) != PROTOCOL_SHA256:
        raise EdgeError("invalid_protocol")
    value = parse(raw, code="invalid_protocol")
    keys(
        value,
        {
            "schema_version",
            "data_kind",
            "protocol_id",
            "threads",
            "max_new_tokens",
            "repetitions",
            "cases",
        },
        "invalid_protocol",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["data_kind"] != "authored_fictional_inference_measurement"
        or value["protocol_id"] != "cpu-qwen-v1"
        or value["threads"] != [1, 4]
        or any(type(x) is not int for x in value["threads"])
        or type(value["max_new_tokens"]) is not int
        or value["max_new_tokens"] != 128
        or type(value["repetitions"]) is not int
        or value["repetitions"] != 2
        or type(value["cases"]) is not list
        or len(value["cases"]) != 3
    ):
        raise EdgeError("invalid_protocol")
    for case in value["cases"]:
        request(case)
    if len({case["id"] for case in value["cases"]}) != 3:
        raise EdgeError("invalid_protocol")
    return value


def schedule():
    p = protocol()
    result = [("first_request_after_load", 0, p["cases"][0])]
    for repetition in range(1, p["repetitions"] + 1):
        result.extend(("warm", repetition, case) for case in p["cases"])
    return result


def source_hashes():
    own = Path(__file__).parent
    files = {
        "edge/" + name: own / name
        for name in [
            "__init__.py",
            "contracts.py",
            "worker.py",
            "experiment.py",
            "__main__.py",
        ]
    }
    files["rag/neural.py"] = own.parent / "rag/neural.py"
    return {name: sha(path.read_bytes()) for name, path in files.items()}


def rss(value):
    keys(value, {"native", "native_unit", "bytes", "scope"}, "invalid_report")
    native = integer(value["native"], 1, 2**63 - 1)
    if (
        type(value["native_unit"]) is not str
        or value["native_unit"] not in {"bytes", "KiB"}
        or value["scope"] != "process_high_water_all_threads"
    ):
        raise EdgeError("invalid_report")
    if integer(value["bytes"], 1, 2**63 - 1) != native * (
        1 if value["native_unit"] == "bytes" else 1024
    ):
        raise EdgeError("invalid_report")
    return value


def metadata(value, threads):
    from ..rag.neural import MODEL_FILES, MODEL_ID, MODEL_REVISION, RUNTIME

    # Compare to the adapter's exact published metadata, not caller-selected versions.
    expected = {
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "license": "Apache-2.0",
        "files_sha256": dict(MODEL_FILES),
        "runtime": dict(RUNTIME),
        "device": "cpu",
        "dtype": "float32",
        "cpu_threads": threads,
        "sampling": False,
        "thinking": False,
        "max_input_tokens": 4096,
        "max_generated_tokens": 128,
        "soft_decoding_deadline_seconds": 10,
        "evidence_validation": "exact chunk ID and literal quote, not semantic entailment",
    }
    if canonical(value) != canonical(expected):
        raise EdgeError("invalid_report")
    return value


def ready(value, threads):
    keys(
        value,
        {
            "type",
            "schema_version",
            "metadata",
            "startup_elapsed_ns",
            "rss",
            "source_sha256",
            "python",
            "system",
            "machine",
        },
        "invalid_report",
    )
    if (
        value["type"] != "ready"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA
    ):
        raise EdgeError("invalid_report")
    metadata(value["metadata"], threads)
    integer(value["startup_elapsed_ns"], 1, 2**63 - 1)
    rss(value["rss"])
    if value["source_sha256"] != source_hashes():
        raise EdgeError("invalid_report")
    for name in ["python", "system", "machine"]:
        text(value[name], 64, code="invalid_report")
    if value["system"] not in {"Darwin", "Linux"}:
        raise EdgeError("invalid_report")
    if value["rss"]["native_unit"] != (
        "bytes" if value["system"] == "Darwin" else "KiB"
    ):
        raise EdgeError("invalid_report")
    return value


def result(value, expected_request):
    keys(
        value,
        {
            "type",
            "id",
            "ok",
            "error",
            "answer",
            "elapsed_ns",
            "model_generate_calls",
            "model_generate_elapsed_ns",
            "trace",
            "rss",
        },
        "invalid_report",
    )
    if (
        value["type"] != "result"
        or value["id"] != expected_request["id"]
        or type(value["ok"]) is not bool
    ):
        raise EdgeError("invalid_report")
    integer(value["elapsed_ns"], 1, 2**63 - 1)
    calls = integer(value["model_generate_calls"], 0, 1)
    model_time = integer(value["model_generate_elapsed_ns"], 0, 2**63 - 1)
    if model_time > value["elapsed_ns"] or bool(calls) != bool(model_time):
        raise EdgeError("invalid_report")
    rss(value["rss"])
    trace = value["trace"]
    if trace is not None:
        keys(
            trace,
            {
                "input_tokens",
                "generated_tokens",
                "prompt_sha256",
                "raw_output_sha256",
                "raw_output",
                "validated",
            },
            "invalid_report",
        )
        integer(trace["input_tokens"], 1, 4096)
        integer(trace["generated_tokens"], 0, 128)
        raw = text(trace["raw_output"], 16_384, blank=True, code="invalid_report")
        if (
            calls != 1
            or type(trace["validated"]) is not bool
            or trace["validated"] != value["ok"]
        ):
            raise EdgeError("invalid_report")
        if (
            type(trace["prompt_sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", trace["prompt_sha256"])
            or sha(raw.encode()) != trace["raw_output_sha256"]
        ):
            raise EdgeError("invalid_report")
    if value["ok"]:
        if trace is None or value["error"] is not None:
            raise EdgeError("invalid_report")
        from ..rag.neural import GenerationError, validate_generation

        try:
            expected = validate_generation(
                trace["raw_output"], expected_request["chunks"]
            )
        except GenerationError as exc:
            raise EdgeError("invalid_report") from exc
        answer = value["answer"]
        keys(
            answer,
            {
                "answer",
                "evidence",
                "human_review_required",
                "legal_advice",
                "generator",
                "generation",
            },
            "invalid_report",
        )
        expected.update(
            generator="qwen3-0.6b-cpu",
            generation={
                key: trace[key]
                for key in [
                    "input_tokens",
                    "generated_tokens",
                    "prompt_sha256",
                    "raw_output_sha256",
                ]
            },
        )
        if canonical(answer) != canonical(expected):
            raise EdgeError("invalid_report")
    else:
        if value["error"] != "generation_failed" or value["answer"] is not None:
            raise EdgeError("invalid_report")
        if trace is not None:
            from ..rag.neural import GenerationError, validate_generation

            try:
                validate_generation(trace["raw_output"], expected_request["chunks"])
            except GenerationError:
                pass
            else:
                raise EdgeError("invalid_report")
    return value
