"""Finite fictional accounting contracts; no provider prices or measured usage."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from types import MappingProxyType

MAX_QUANTITY = 1_000_000_000
MAX_EVENTS = 1000
MAX_STATEMENTS = 100
MAX_INPUT = 256 * 1024
MAX_OUTPUT = 512 * 1024
MAX_DATABASE = 32 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
MONEY = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
SETTLED = re.compile(r"(?:0|[1-9][0-9]*)\.[0-9]{2}\Z")
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
METRICS = ("input_tokens", "output_tokens", "embedding_tokens", "ocr_pages")
DATA = Path(__file__).parent / "data"


class CostError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def strict_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_INPUT:
        raise CostError("invalid_input")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CostError("invalid_input")
            result[key] = value
        return result

    def number(_):
        raise CostError("invalid_input")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=number,
            parse_constant=number,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise CostError("invalid_input") from None


def bounded(value):
    if len(canonical(value).encode()) > MAX_OUTPUT:
        raise CostError("capacity_exceeded")
    return value


def name(value):
    return isinstance(value, str) and bool(NAME.fullmatch(value))


def timestamp(value):
    if not isinstance(value, str) or not TIME.fullmatch(value):
        raise CostError("invalid_input")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise CostError("invalid_input") from None
    return value


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def period(start, end):
    timestamp(start)
    timestamp(end)
    if start >= end:
        raise CostError("invalid_input")


def exact_context():
    # <=12 price digits * <=10 quantity digits; <=12 fractional places.
    # At most1000 events need <=36 common-scale digits, including carry.
    context = Context(
        prec=50, rounding=ROUND_HALF_EVEN, Emin=-60, Emax=60, capitals=1, clamp=0
    )
    for signal in context.traps:
        context.traps[signal] = True
    context.clear_flags()
    return context


def money(value, *, price=False, settled=False):
    grammar = SETTLED if settled else MONEY
    if not isinstance(value, str) or len(value) > 50 or not grammar.fullmatch(value):
        raise CostError("invalid_input")
    coefficient = value.replace(".", "").lstrip("0") or "0"
    scale = len(value.partition(".")[2])
    if len(coefficient) > (12 if price else 36) or scale > (6 if price else 12):
        raise CostError("invalid_input")
    result = Decimal(value)
    if price and result <= 0:
        raise CostError("invalid_input")
    return result


def decimal_text(value):
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if value == 0 else result


def totals(amounts):
    with localcontext(exact_context()) as context:
        total = Decimal(0)
        for index, value in enumerate(amounts):
            if index >= MAX_EVENTS:
                raise CostError("capacity_exceeded")
            total += money(value)
        context.traps[Inexact] = False
        context.traps[Rounded] = False
        settled = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        context.clear_flags()
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        delta = settled - total
        return {
            "exact_total": decimal_text(total),
            "settled_total": format(settled, ".2f"),
            "rounding_delta": decimal_text(delta),
        }


def rate_card(value=None):
    if value is None:
        value = strict_json((DATA / "rates-v1.json").read_bytes())
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "data_kind", "version", "currency", "rates"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["data_kind"] != "fictional_scenario_rates"
        or value["version"] != "fictional-v1"
        or value["currency"] != "USD"
        or not isinstance(value["rates"], dict)
        or set(value["rates"]) != set(METRICS)
    ):
        raise CostError("invalid_input")
    for row in value["rates"].values():
        if not isinstance(row, dict) or set(row) != {"unit_price", "unit_exponent"}:
            raise CostError("invalid_input")
        money(row["unit_price"], price=True)
        if type(row["unit_exponent"]) is not int or not 0 <= row["unit_exponent"] <= 6:
            raise CostError("invalid_input")
    # Detach mutable caller data, including nested rate entries.
    return json.loads(canonical(value)), digest(value)


def event(value):
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "event_id",
            "request_id",
            "metric",
            "quantity",
            "rate_version",
            "occurred_at",
        }
        or not name(value["event_id"])
        or not name(value["request_id"])
        or value["metric"] not in METRICS
        or value["rate_version"] != "fictional-v1"
        or type(value["quantity"]) is not int
        or not 0 <= value["quantity"] <= MAX_QUANTITY
    ):
        raise CostError("invalid_input")
    timestamp(value["occurred_at"])
    return json.loads(canonical(value))


def charge(value, card):
    value = event(value)
    row = card["rates"][value["metric"]]
    with localcontext(exact_context()):
        return decimal_text(
            Decimal(value["quantity"])
            * money(row["unit_price"], price=True)
            / Decimal(10 ** row["unit_exponent"])
        )


def statement(value):
    if (
        not isinstance(value, dict)
        or set(value)
        != {"statement_id", "currency", "start", "end", "lines", "settled_total"}
        or not name(value["statement_id"])
        or not isinstance(value["currency"], str)
        or not re.fullmatch(r"[A-Z]{3}", value["currency"])
        or not isinstance(value["lines"], list)
        or len(value["lines"]) > MAX_EVENTS
    ):
        raise CostError("invalid_input")
    period(value["start"], value["end"])
    money(value["settled_total"], settled=True)
    seen = set()
    for line in value["lines"]:
        if (
            not isinstance(line, dict)
            or set(line) != {"event_id", "amount"}
            or not name(line["event_id"])
        ):
            raise CostError("invalid_input")
        money(line["amount"])
        if line["event_id"] in seen:
            raise CostError("duplicate_statement_line")
        seen.add(line["event_id"])
    result = json.loads(canonical(value))
    result["lines"].sort(key=lambda line: line["event_id"])
    return result


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    role: str


PROFILES = MappingProxyType(
    {
        name: Principal("local:" + name, tenant, role)
        for name, tenant, role in (
            ("alpha-operator", "alpha", "operator"),
            ("alpha-operator2", "alpha", "operator"),
            ("alpha-auditor", "alpha", "auditor"),
            ("beta-operator", "beta", "operator"),
            ("beta-auditor", "beta", "auditor"),
            ("alpha-observer", "alpha", "observer"),
        )
    }
)


def principal_for_profile(profile):
    if not isinstance(profile, str) or profile not in PROFILES:
        raise CostError("invalid_profile")
    return PROFILES[profile]


def authorize(principal, roles):
    if (
        not isinstance(principal, Principal)
        or principal not in PROFILES.values()
        or principal.role not in roles
    ):
        raise CostError("forbidden")
