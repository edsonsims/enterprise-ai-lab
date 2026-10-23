"""Finite authored assumptions and exact rational sensitivity calculations."""

from __future__ import annotations

import hashlib
import itertools
import json
import platform
import sqlite3
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from ..costs.contracts import METRICS, CostError, bounded, digest, money, name, period

LEVELS = ("low", "base", "high")
FIELDS = {
    "schema_version",
    "data_kind",
    "scenario_id",
    "previous_start",
    "current_start",
    "current_end",
    "baseline_minutes",
    "automation_share",
    "realization_share",
    "hourly_rate",
    "allocated_fixed_cost",
    "period_operating_cost",
    "minimum_requests",
}
LIMITATIONS = [
    "Usage and rates are fictional fixtures, not provider or customer telemetry.",
    "Usage-identified requests are distinct request IDs, not completed or successful tasks.",
    "Benefits and ROI are modeled assumptions, not observed savings or business outcomes.",
    "Low/base/high ranges are authored sensitivity ranges, not statistical confidence intervals.",
    "Activity flags are declared count comparisons, not predictive customer health scores.",
    "Launcher profiles are trusted local policy selection, not authentication.",
    "Recomputing the selected full-row digest requires preserved ledger receipts; compact references alone are insufficient.",
]


def _amount(value, maximum):
    money(value)
    result = Fraction(value)
    if result > maximum:
        raise CostError("invalid_input")
    return result


def _range(value, maximum):
    if type(value) is not dict or set(value) != set(LEVELS):
        raise CostError("invalid_input")
    values = [_amount(value[key], maximum) for key in LEVELS]
    if values != sorted(values):
        raise CostError("invalid_input")


def scenario(value):
    """Validate a bounded development scenario; identity never comes from it."""
    if (
        type(value) is not dict
        or set(value) != FIELDS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["data_kind"] != "fictional_roi_assumptions"
        or not name(value["scenario_id"])
        or type(value["minimum_requests"]) is not int
        or not 0 <= value["minimum_requests"] <= 1000
    ):
        raise CostError("invalid_input")
    period(value["previous_start"], value["current_start"])
    period(value["current_start"], value["current_end"])
    times = [
        datetime.strptime(value[key], "%Y-%m-%dT%H:%M:%SZ")
        for key in ("previous_start", "current_start", "current_end")
    ]
    if times[1] - times[0] != times[2] - times[1]:
        raise CostError("invalid_input")
    _range(value["baseline_minutes"], 1440)
    _range(value["automation_share"], 1)
    _range(value["realization_share"], 1)
    _amount(value["hourly_rate"], 1000)
    _amount(value["allocated_fixed_cost"], 1_000_000)
    _amount(value["period_operating_cost"], 1_000_000)
    return json.loads(json.dumps(value))


def exact(value):
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def rounded(value):
    """Round a Fraction to two places using integer half-even, including negatives."""
    if type(value) is not Fraction:
        raise CostError("invalid_input")
    cents, remainder = divmod(abs(value.numerator) * 100, value.denominator)
    if remainder * 2 > value.denominator or (
        remainder * 2 == value.denominator and cents % 2
    ):
        cents += 1
    sign = "-" if value < 0 and cents else ""
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _money_result(value):
    return {"exact": exact(value), "display_usd": rounded(value)}


def _usage(rows, start, end):
    selected = [row for row in rows if start <= row["occurred_at"] < end]
    metrics = {}
    total = Fraction(0)
    for metric in METRICS:
        matches = [row for row in selected if row["metric"] == metric]
        charge = sum((Fraction(row["charge"]) for row in matches), Fraction(0))
        total += charge
        metrics[metric] = {
            "event_count": len(matches),
            "quantity": sum(row["quantity"] for row in matches),
            "charge": _money_result(charge),
        }
    return {
        "start": start,
        "end": end,
        "event_count": len(selected),
        "usage_identified_requests": len({row["request_id"] for row in selected}),
        "metrics": metrics,
        "ledger_cost": _money_result(total),
    }, total


def _sources():
    package = Path(__file__).resolve().parent.parent
    paths = [
        package / "health" / path
        for path in ("__init__.py", "core.py", "view.py", "__main__.py")
    ]
    paths += [
        package / "costs" / path
        for path in ("__init__.py", "ledger.py", "contracts.py")
    ]
    return {
        path.relative_to(package).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


def build_report(ledger, principal, value):
    assumptions = scenario(value)
    snapshot = ledger.snapshot(
        principal, assumptions["previous_start"], assumptions["current_end"]
    )
    previous, _ = _usage(
        snapshot["events"], assumptions["previous_start"], assumptions["current_start"]
    )
    current, ledger_cost = _usage(
        snapshot["events"], assumptions["current_start"], assumptions["current_end"]
    )
    before, count = (
        previous["usage_identified_requests"],
        current["usage_identified_requests"],
    )
    ratio = None if before == 0 else Fraction(count - before, before)
    fixed = Fraction(assumptions["allocated_fixed_cost"])
    operating = Fraction(assumptions["period_operating_cost"])
    cost = ledger_cost + fixed + operating
    cells = []
    for minutes, automation, realization in itertools.product(LEVELS, repeat=3):
        gross = (
            count
            * Fraction(assumptions["baseline_minutes"][minutes])
            * Fraction(assumptions["automation_share"][automation])
            * Fraction(assumptions["realization_share"][realization])
            * Fraction(assumptions["hourly_rate"])
            / 60
        )
        net = gross - cost
        roi = None if cost == 0 else net / cost * 100
        cells.append(
            {
                "levels": {
                    "baseline_minutes": minutes,
                    "automation_share": automation,
                    "realization_share": realization,
                },
                "gross_benefit": _money_result(gross),
                "net_benefit": _money_result(net),
                "total_cost": _money_result(cost),
                "roi_percent": None
                if roi is None
                else {"exact": exact(roi), "display": rounded(roi)},
            }
        )
    seconds = datetime.strptime(
        assumptions["current_end"], "%Y-%m-%dT%H:%M:%SZ"
    ) - datetime.strptime(assumptions["current_start"], "%Y-%m-%dT%H:%M:%SZ")
    return bounded(
        {
            "schema_version": 1,
            "data_kind": "fictional_usage_and_modeled_roi",
            "scenario_id": assumptions["scenario_id"],
            "scenario_sha256": digest(assumptions),
            "assumptions": assumptions,
            "snapshot": snapshot,
            "period_seconds": seconds.days * 86400 + seconds.seconds,
            "previous": previous,
            "current": current,
            "activity": {
                "request_count_change": count - before,
                "ratio_change": None if ratio is None else exact(ratio),
                "percent_change_display": None
                if ratio is None
                else rounded(ratio * 100),
                "flags": {
                    "no_current_usage": count == 0,
                    "below_authored_minimum": count < assumptions["minimum_requests"],
                    "declining_usage": count < before,
                },
            },
            "modeled_cost": {
                "ledger": _money_result(ledger_cost),
                "allocated_fixed": _money_result(fixed),
                "period_operating": _money_result(operating),
                "total": _money_result(cost),
            },
            "modeled_base": cells[13],
            "modeled_range": {"low": cells[0], "high": cells[-1]},
            "sensitivity": cells,
            "rounding": "Aggregate display only: integer rational half-even cents; ROI percent two places.",
            "formula": "request_count * baseline_minutes * automation_share * realization_share * hourly_rate / 60; net = gross - cost; ROI_percent = net / cost * 100 when cost > 0",
            "limitations": LIMITATIONS.copy(),
            "runtime": {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
            },
            "source_sha256": _sources(),
        }
    )
