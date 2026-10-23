"""Real-ledger snapshot boundaries and independently checkable rational examples."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import ROUND_DOWN, localcontext
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from enterprise_ai_lab.costs import (
    CostError,
    CostLedger,
    initialize,
    principal_for_profile,
)
from enterprise_ai_lab.costs.contracts import Principal, canonical, digest
from enterprise_ai_lab.health import build_report, render_html, rounded, scenario


def authored_scenario(**changes):
    value = {
        "schema_version": 1,
        "data_kind": "fictional_roi_assumptions",
        "scenario_id": "fictional-test-v1",
        "previous_start": "2026-10-01T00:00:00Z",
        "current_start": "2026-10-08T00:00:00Z",
        "current_end": "2026-10-15T00:00:00Z",
        "baseline_minutes": {"low": "5", "base": "15", "high": "30"},
        "automation_share": {"low": "0", "base": "0.5", "high": "0.8"},
        "realization_share": {"low": "0.25", "base": "0.5", "high": "0.75"},
        "hourly_rate": "40",
        "allocated_fixed_cost": "3",
        "period_operating_cost": "1",
        "minimum_requests": 3,
    }
    value.update(changes)
    return value


def event(
    key,
    request="shared-request",
    *,
    time="2026-10-08T00:00:00Z",
    metric="embedding_tokens",
    quantity=1,
):
    return {
        "event_id": key,
        "request_id": request,
        "occurred_at": time,
        "metric": metric,
        "quantity": quantity,
        "rate_version": "fictional-v1",
    }


def fraction(value):
    return Fraction(int(value["numerator"]), int(value["denominator"]))


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ledger.sqlite"
        initialize(self.path)
        self.ledger = CostLedger(self.path)
        self.operator = principal_for_profile("alpha-operator")
        self.auditor = principal_for_profile("alpha-auditor")
        self.assumptions = authored_scenario()

    def append(self, value, profile=None):
        return self.ledger.append(self.operator if profile is None else profile, value)

    def error(self, code, operation, *args):
        with self.assertRaises(CostError) as caught:
            operation(*args)
        self.assertEqual(caught.exception.code, code)

    def test_one_snapshot_half_open_periods_tenant_collisions_and_receipt_hash(self):
        before = self.append(event("too-early", time="2026-09-30T23:59:59Z"))
        previous = self.append(
            event(
                "collision",
                request="previous-a",
                time=self.assumptions["previous_start"],
            )
        )
        current = self.append(event("current", time=self.assumptions["current_start"]))
        end = self.append(event("too-late", time=self.assumptions["current_end"]))
        self.append(
            event("collision", quantity=1000), principal_for_profile("beta-operator")
        )
        with patch.object(self.ledger, "_read", wraps=self.ledger._read) as read:
            report = build_report(self.ledger, self.auditor, self.assumptions)
        self.assertEqual(read.call_count, 1)
        snapshot = report["snapshot"]
        self.assertEqual(snapshot["watermark"], 4)
        self.assertEqual(
            [row["event_id"] for row in snapshot["events"]], ["collision", "current"]
        )
        full = [
            {k: v for k, v in row.items() if k != "replayed"}
            for row in [previous, current]
        ]
        self.assertEqual(snapshot["snapshot_sha256"], digest(full))
        self.assertNotEqual(snapshot["snapshot_sha256"], digest(snapshot["events"]))
        self.assertNotIn("first_actor", snapshot["events"][0])
        self.assertEqual(
            (report["previous"]["event_count"], report["current"]["event_count"]),
            (1, 1),
        )
        self.assertEqual(snapshot["currency"], "USD")
        self.assertNotIn("beta", canonical(report))
        self.assertNotIn(before["receipt_id"], canonical(report))
        self.assertNotIn(end["receipt_id"], canonical(report))

    def test_append_after_validated_read_does_not_mix_periods_or_watermarks(self):
        self.append(event("first"))
        original = self.ledger._read
        reads = []

        def read_then_append(principal):
            rows = original(principal)
            reads.append(len(rows))
            CostLedger(self.path).append(
                self.operator, event("later", request="later-request")
            )
            return rows

        with patch.object(self.ledger, "_read", side_effect=read_then_append):
            report = build_report(self.ledger, self.auditor, self.assumptions)
        self.assertEqual(reads, [1])
        self.assertEqual(report["snapshot"]["watermark"], 1)
        self.assertEqual(report["current"]["usage_identified_requests"], 1)
        self.assertEqual(
            CostLedger(self.path).snapshot(
                self.auditor,
                self.assumptions["previous_start"],
                self.assumptions["current_end"],
            )["watermark"],
            2,
        )

    def test_exact_base_case_and_all_27_combinations(self):
        self.append(event("a", request="r1"))
        self.append(event("b", request="r1", metric="output_tokens", quantity=10))
        self.append(event("c", request="r2", metric="ocr_pages", quantity=2))
        report = build_report(self.ledger, self.auditor, self.assumptions)
        self.assertEqual(report["current"]["usage_identified_requests"], 2)
        self.assertEqual(report["current"]["event_count"], 3)
        self.assertEqual(
            fraction(report["modeled_cost"]["ledger"]["exact"]), Fraction(1, 20)
        )
        base = report["modeled_base"]
        self.assertEqual(fraction(base["gross_benefit"]["exact"]), Fraction(5))
        self.assertEqual(fraction(base["total_cost"]["exact"]), Fraction(81, 20))
        self.assertEqual(fraction(base["net_benefit"]["exact"]), Fraction(19, 20))
        self.assertEqual(fraction(base["roi_percent"]["exact"]), Fraction(1900, 81))
        self.assertEqual(base["roi_percent"]["display"], "23.46")
        cells = report["sensitivity"]
        self.assertEqual(len(cells), 27)
        self.assertEqual(len({tuple(row["levels"].values()) for row in cells}), 27)
        self.assertEqual(
            report["modeled_range"]["low"]["gross_benefit"]["display_usd"], "0.00"
        )
        self.assertEqual(
            report["modeled_range"]["high"]["gross_benefit"]["display_usd"], "24.00"
        )

    def test_hostile_decimal_context_cannot_change_results_or_context(self):
        self.append(event("one", metric="input_tokens", quantity=333))
        normal = build_report(self.ledger, self.auditor, self.assumptions)
        with localcontext() as context:
            context.prec = 1
            context.rounding = ROUND_DOWN
            context.Emax, context.Emin = 1, -1
            for signal in context.traps:
                context.traps[signal] = True
            context.clear_flags()
            hostile = build_report(self.ledger, self.auditor, self.assumptions)
            self.assertEqual(context.prec, 1)
            self.assertFalse(any(context.flags.values()))
        self.assertEqual(normal, hostile)

    def test_half_even_signed_fraction_rounding_and_zero(self):
        for amount, expected in [
            (Fraction(201, 200), "1.00"),
            (Fraction(203, 200), "1.02"),
            (Fraction(-201, 200), "-1.00"),
            (Fraction(-203, 200), "-1.02"),
            (Fraction(-1, 1000), "0.00"),
            (Fraction(1, 3), "0.33"),
        ]:
            with self.subTest(amount=amount):
                self.assertEqual(rounded(amount), expected)
        self.error("invalid_input", rounded, 1.005)

    def test_zero_cost_roi_undefined_and_negative_modeled_roi(self):
        empty = build_report(
            self.ledger,
            self.auditor,
            authored_scenario(allocated_fixed_cost="0", period_operating_cost="0"),
        )
        self.assertTrue(all(row["roi_percent"] is None for row in empty["sensitivity"]))
        self.assertIsNone(empty["activity"]["ratio_change"])
        negative = build_report(self.ledger, self.auditor, self.assumptions)
        self.assertEqual(
            negative["modeled_base"]["net_benefit"]["display_usd"], "-4.00"
        )
        self.assertEqual(negative["modeled_base"]["roi_percent"]["display"], "-100.00")
        self.assertEqual(
            negative["activity"]["flags"],
            {
                "no_current_usage": True,
                "below_authored_minimum": True,
                "declining_usage": False,
            },
        )

    def test_repeated_request_ids_and_zero_quantity_are_not_completion_counts(self):
        self.append(
            event("previous-a", request="a", time=self.assumptions["previous_start"])
        )
        self.append(
            event("previous-b", request="b", time=self.assumptions["previous_start"])
        )
        self.append(event("current-a", request="a", quantity=0))
        self.append(event("current-a2", request="a", metric="ocr_pages", quantity=0))
        report = build_report(self.ledger, self.auditor, self.assumptions)
        self.assertEqual(report["current"]["usage_identified_requests"], 1)
        self.assertEqual(report["current"]["event_count"], 2)
        self.assertEqual(report["current"]["ledger_cost"]["display_usd"], "0.00")
        self.assertEqual(report["activity"]["percent_change_display"], "-50.00")
        self.assertTrue(report["activity"]["flags"]["declining_usage"])

    def test_snapshot_operator_auditor_allowed_and_forged_observer_denied(self):
        for profile in ["alpha-operator", "alpha-operator2", "alpha-auditor"]:
            self.ledger.snapshot(
                principal_for_profile(profile),
                self.assumptions["previous_start"],
                self.assumptions["current_end"],
            )
        for principal in [
            principal_for_profile("alpha-observer"),
            Principal("invented", "alpha", "auditor"),
        ]:
            self.error(
                "forbidden",
                self.ledger.snapshot,
                principal,
                self.assumptions["previous_start"],
                self.assumptions["current_end"],
            )

    def test_snapshot_validates_corrupted_statement_not_only_selected_usage(self):
        self.append(event("one"))
        value = {
            "statement_id": "prior",
            "currency": "USD",
            "start": self.assumptions["previous_start"],
            "end": self.assumptions["current_end"],
            "lines": [],
            "settled_total": "0.00",
        }
        self.ledger.reconcile(self.auditor, value)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='statements_no_update'"
            ).fetchone()[0]
            record = json.loads(
                connection.execute(
                    "SELECT record FROM statements WHERE tenant_id='alpha'"
                ).fetchone()[0]
            )
            record["result"]["matched"] = True
            connection.execute("DROP TRIGGER statements_no_update")
            connection.execute(
                "UPDATE statements SET record=?,digest=? WHERE tenant_id='alpha'",
                (canonical(record), digest(record)),
            )
            connection.execute(trigger)
        self.error(
            "storage_corrupt", build_report, self.ledger, self.auditor, self.assumptions
        )

    def test_strict_scenario_rejects_identity_numbers_windows_and_ranges(self):
        bad = [
            {"tenant_id": "beta"},
            {"currency": "EUR"},
            {"schema_version": True},
            {"minimum_requests": True},
            {"minimum_requests": -1},
            {"minimum_requests": 1001},
            {"hourly_rate": "1000.000000000001"},
            {"allocated_fixed_cost": "1000001"},
            {"period_operating_cost": "-0"},
            {"hourly_rate": "1.00"},
            {"hourly_rate": 40},
            {"hourly_rate": "1e2"},
            {"hourly_rate": "0.0000000000001"},
            {"current_start": "2026-10-09T00:00:00Z"},
            {"current_end": "2026-10-15T00:00:00+00:00"},
            {"baseline_minutes": {"low": "1", "base": "0", "high": "2"}},
            {"baseline_minutes": {"low": "0", "base": "2", "high": "1441"}},
            {"automation_share": {"low": "0", "base": "0.5", "high": "1.1"}},
            {"realization_share": ["0", "0.5", "1"]},
        ]
        for changes in bad:
            with self.subTest(changes=changes):
                self.error("invalid_input", scenario, authored_scenario(**changes))

    def test_scenario_detaches_mutable_ranges_and_includes_upper_bounds(self):
        value = authored_scenario(
            hourly_rate="1000",
            allocated_fixed_cost="1000000",
            period_operating_cost="1000000",
            minimum_requests=1000,
        )
        detached = scenario(value)
        value["baseline_minutes"]["base"] = "999"
        self.assertEqual(detached["baseline_minutes"]["base"], "15")

    def test_html_escapes_display_and_inert_json_without_external_execution(self):
        report = build_report(self.ledger, self.auditor, self.assumptions)
        hostile = '</script><img src="https://example.invalid/x" onerror="alert(1)">&\u2028\u2029'
        report["scenario_id"] = hostile
        document = render_html(report)
        self.assertNotIn("<img src=", document)
        self.assertIn("&lt;/script&gt;", document)
        embedded = re.search(
            r'<script id="report-data" type="application/json">(.*?)</script>',
            document,
            re.DOTALL,
        ).group(1)
        self.assertNotIn("<", embedded)
        self.assertNotIn("\u2028", embedded)
        self.assertEqual(json.loads(embedded), report)
        self.assertIn("default-src 'none'", document)
        self.assertEqual(document.count("<script"), 1)
        self.assertEqual(document.count('tabindex="0"'), 3)
        self.assertNotIn("<script src=", document)

    def test_source_hashes_are_relative_and_match_current_runtime(self):
        report = build_report(self.ledger, self.auditor, self.assumptions)
        package = Path(__file__).resolve().parents[2] / "src/enterprise_ai_lab"
        self.assertEqual(len(report["source_sha256"]), 7)
        for path, checksum in report["source_sha256"].items():
            self.assertFalse(Path(path).is_absolute())
            self.assertEqual(
                hashlib.sha256((package / path).read_bytes()).hexdigest(), checksum
            )
