from __future__ import annotations

import unittest

import pandas as pd

from research_platform.cash_sweep_research import (
    TARGET_WEIGHTED_ANNUALIZED_RETURN,
    accrue_idle_cash_yield,
    assess_cash_yield_feasibility,
    protocol_manifest,
)


class CashSweepResearchTests(unittest.TestCase):
    def test_zero_yield_reproduces_equity_exactly(self) -> None:
        equity = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-05"]),
                "equity": [100_000.0, 101_000.0, 100_500.0],
                "cash": [80_000.0, 60_000.0, 100_500.0],
            }
        )
        result = accrue_idle_cash_yield(
            equity, initial_cash=100_000.0, annual_cash_yield=0.0
        )
        adjusted = result["equity"]
        self.assertEqual(result["accrued_interest"], 0.0)
        pd.testing.assert_series_equal(
            adjusted["adjusted_equity"], equity["equity"], check_names=False
        )
        expected_annualized = (100_500.0 / 100_000.0) ** (252.0 / 2.0) - 1.0
        self.assertAlmostEqual(result["annualized_return"], expected_annualized, places=12)

    def test_interest_uses_prior_cash_and_elapsed_calendar_days(self) -> None:
        equity = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-05", "2024-01-08", "2024-01-09"]),
                "equity": [100_000.0, 100_000.0, 100_000.0],
                "cash": [100_000.0, 20_000.0, 20_000.0],
            }
        )
        rate = 0.036525
        result = accrue_idle_cash_yield(
            equity, initial_cash=100_000.0, annual_cash_yield=rate
        )
        frame = result["equity"]
        three_day_growth = (1.0 + rate) ** (3.0 / 365.25) - 1.0
        one_day_growth = (1.0 + rate) ** (1.0 / 365.25) - 1.0
        first_interest = 100_000.0 * three_day_growth
        expected = first_interest + (20_000.0 + first_interest) * one_day_growth
        self.assertAlmostEqual(frame.loc[1, "accrued_interest"], first_interest, places=10)
        self.assertAlmostEqual(frame.loc[2, "accrued_interest"], expected, places=10)

    def test_feasibility_never_authorizes_production(self) -> None:
        baseline = {"exact_baseline_reproduction": True}
        required = {
            "weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
            "annual_cash_yield": 0.015,
            "windows": [{"max_drawdown": -0.05}],
        }
        decision = assess_cash_yield_feasibility(baseline, required)
        self.assertEqual(decision["decision"], "REQUIRE_INSTRUMENT_VALIDATION")
        self.assertTrue(decision["feasibility_qualified"])
        self.assertFalse(decision["production_authorized"])

        rejected = assess_cash_yield_feasibility(
            baseline, {**required, "annual_cash_yield": 0.025}
        )
        self.assertEqual(rejected["decision"], "REJECT")

    def test_protocol_preserves_v9_and_only_runs_sensitivity(self) -> None:
        protocol = protocol_manifest()
        self.assertEqual(protocol["research_status"], "sensitivity_only")
        self.assertTrue(protocol["invariants"]["v9_trade_dates_unchanged"])
        self.assertTrue(protocol["invariants"]["no_production_registration"])
        self.assertTrue(protocol["decision_rule"]["passing_is_not_promotion"])


if __name__ == "__main__":
    unittest.main()
