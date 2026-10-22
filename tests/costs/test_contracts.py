"""Literal fictional arithmetic and input contracts; no provider/model calls."""

import copy
import unittest
from decimal import ROUND_UP, Decimal, Inexact, localcontext

from enterprise_ai_lab.costs.contracts import (
    MAX_INPUT,
    CostError,
    charge,
    event,
    money,
    period,
    rate_card,
    strict_json,
    totals,
)


def usage(**changes):
    return {
        "event_id": "event-1",
        "request_id": "request-1",
        "metric": "input_tokens",
        "quantity": 1001,
        "rate_version": "fictional-v1",
        "occurred_at": "2026-10-22T10:00:00Z",
        **changes,
    }


class ArithmeticTests(unittest.TestCase):
    def test_literal_charges_each_metric_zero_and_maximum(self):
        card, _ = rate_card()
        for metric, quantity, expected in [
            ("input_tokens", 1001, "0.5005"),
            ("output_tokens", 7, "0.0105"),
            ("embedding_tokens", 1, "0.005"),
            ("ocr_pages", 3, "0.045"),
            ("input_tokens", 0, "0"),
            ("input_tokens", 1_000_000_000, "500000"),
        ]:
            with self.subTest(metric=metric, quantity=quantity):
                self.assertEqual(
                    charge(usage(metric=metric, quantity=quantity), card), expected
                )

    def test_half_even_settlement_is_aggregate_first(self):
        self.assertEqual(
            totals(["1.005"]),
            {
                "exact_total": "1.005",
                "settled_total": "1.00",
                "rounding_delta": "-0.005",
            },
        )
        self.assertEqual(
            totals(["1.015"]),
            {
                "exact_total": "1.015",
                "settled_total": "1.02",
                "rounding_delta": "0.005",
            },
        )
        self.assertEqual(
            totals(["0.005", "0.005"]),
            {"exact_total": "0.01", "settled_total": "0.01", "rounding_delta": "0"},
        )
        self.assertEqual(
            totals([]),
            {"exact_total": "0", "settled_total": "0.00", "rounding_delta": "0"},
        )

    def test_maximum_coefficient_quantity_scale_and_aggregate_carry(self):
        card, _ = rate_card()
        card["rates"]["input_tokens"] = {
            "unit_price": "999999999999",
            "unit_exponent": 0,
        }
        validated, _ = rate_card(card)
        line = charge(usage(quantity=1_000_000_000), validated)
        self.assertEqual(line, "999999999999000000000")
        self.assertEqual(
            totals([line] * 1000)["exact_total"], "999999999999000000000000"
        )
        card["rates"]["input_tokens"] = {"unit_price": "0.000001", "unit_exponent": 6}
        self.assertEqual(
            charge(usage(quantity=1), rate_card(card)[0]), "0.000000000001"
        )

    def test_hostile_caller_decimal_context_and_flags_are_unchanged(self):
        card, _ = rate_card()
        with localcontext() as caller:
            caller.prec = 2
            caller.rounding = ROUND_UP
            caller.Emin = -2
            caller.Emax = 2
            for signal in caller.traps:
                caller.traps[signal] = False
            caller.flags[Inexact] = True
            before = (repr(caller), dict(caller.traps), dict(caller.flags))
            self.assertEqual(charge(usage(), card), "0.5005")
            self.assertEqual(
                totals(["0.5005", "0.0105", "0.005", "0.005", "0.045"])["exact_total"],
                "0.566",
            )
            with self.assertRaises(CostError):
                money("1e3")
            self.assertEqual(
                (repr(caller), dict(caller.traps), dict(caller.flags)), before
            )

    def test_canonical_exact_and_settled_grammars_are_distinct(self):
        for value in [
            "-0",
            "-0.00",
            "+1",
            " 1",
            "1 ",
            "01",
            "1.0",
            "0.00",
            "1.",
            ".1",
            "1e3",
            "NaN",
            "Infinity",
            "0.0000000000001",
            "9" * 37,
            0.1,
            True,
        ]:
            with self.subTest(value=value), self.assertRaises(CostError):
                money(value)
        self.assertEqual(money("0"), Decimal(0))
        self.assertEqual(money("0.00", settled=True), Decimal(0))
        for value in ["0", "1.2", "01.00", "-0.00", "1.000"]:
            with self.subTest(value=value), self.assertRaises(CostError):
                money(value, settled=True)

    def test_quantity_and_identity_overrides_are_rejected(self):
        for changes in [
            {"quantity": True},
            {"quantity": 1.0},
            {"quantity": -1},
            {"quantity": 1_000_000_001},
            {"tenant_id": "beta"},
            {"roles": ["operator"]},
            {"event_id": "x';DROP TABLE usage;--"},
            {"rate_version": "live-pricing"},
            {"metric": "unknown"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(CostError):
                event(usage(**changes))

    def test_rate_card_detaches_caller_and_checks_bounds(self):
        original, _ = rate_card()
        detached, _ = rate_card(original)
        original["rates"]["input_tokens"]["unit_price"] = "9"
        self.assertEqual(detached["rates"]["input_tokens"]["unit_price"], "0.5")
        for row in [
            {"unit_price": "0", "unit_exponent": 0},
            {"unit_price": "0.0000001", "unit_exponent": 0},
            {"unit_price": "1000000000000", "unit_exponent": 0},
            {"unit_price": "1", "unit_exponent": True},
            {"unit_price": "1", "unit_exponent": 7},
        ]:
            value = copy.deepcopy(detached)
            value["rates"]["input_tokens"] = row
            with self.subTest(row=row), self.assertRaises(CostError):
                rate_card(value)

    def test_strict_json_and_periods_reject_ambiguous_input(self):
        for raw in [
            b'{"quantity":1,"quantity":2}',
            b'{"quantity":1.0}',
            b'{"q":NaN}',
            b'"\xff"',
            b" " * (MAX_INPUT + 1),
        ]:
            with self.subTest(raw=raw[:40]), self.assertRaises(CostError):
                strict_json(raw)
        for start, end in [
            ("2026-02-30T00:00:00Z", "2026-03-01T00:00:00Z"),
            ("2026-10-22T00:00:00+00:00", "2026-10-23T00:00:00Z"),
            ("2026-10-22T00:00:00.000Z", "2026-10-23T00:00:00Z"),
            ("2026-10-22T00:00:00Z", "2026-10-22T00:00:00Z"),
        ]:
            with self.subTest(start=start), self.assertRaises(CostError):
                period(start, end)
