from __future__ import annotations

import unittest

import pandas as pd

from research_platform.reverse_repo_actual_days_research import (
    TARGET_WEIGHTED_ANNUALIZED_RETURN,
    _bisect_maximum_passing_commission,
    protocol_manifest,
    simulate_actual_days_repo_sweep,
)


class ReverseRepoActualDaysResearchTests(unittest.TestCase):
    def test_weekend_uses_settlement_interval_and_delayed_credit(self) -> None:
        dates = pd.to_datetime(
            ["2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 4, "cash": [50_000.0] * 4}
        )
        rates = pd.DataFrame(
            {"timestamp": dates, "Close": [3.65] * 4, "Low": [3.0] * 4}
        )
        result = simulate_actual_days_repo_sweep(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        frame = result["equity"]
        thursday_interest = 50_000.0 * 0.0365 * 3.0 / 365.0
        friday_interest = 50_000.0 * 0.0365 * 1.0 / 365.0
        self.assertEqual(frame.loc[0, "actual_occupied_days"], 3)
        self.assertEqual(frame.loc[1, "actual_occupied_days"], 1)
        self.assertEqual(frame.loc[1, "settled_repo_pnl"], 0.0)
        self.assertAlmostEqual(frame.loc[2, "settled_repo_pnl"], thursday_interest)
        self.assertAlmostEqual(
            frame.loc[3, "settled_repo_pnl"], thursday_interest + friday_interest
        )

    def test_last_two_sessions_do_not_add_unsettled_interest(self) -> None:
        dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 3, "cash": [50_000.0] * 3}
        )
        rates = pd.DataFrame(
            {"timestamp": dates, "Close": [3.65] * 3, "Low": [3.0] * 3}
        )
        result = simulate_actual_days_repo_sweep(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertEqual(result["repo_trades"], 1)
        self.assertEqual(result["equity"].loc[1, "settled_repo_pnl"], 0.0)
        self.assertGreater(result["equity"].loc[2, "settled_repo_pnl"], 0.0)

    def test_missing_rate_on_eligible_day_fails(self) -> None:
        dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 3, "cash": [50_000.0] * 3}
        )
        rates = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-03"]),
                "Close": [3.65],
                "Low": [3.0],
            }
        )
        with self.assertRaisesRegex(ValueError, "Missing R-001 rates"):
            simulate_actual_days_repo_sweep(
                equity,
                rates,
                initial_cash=50_000.0,
                rate_field="Close",
                commission_rate=0.0,
            )

    def test_future_rate_does_not_change_completed_settlements(self) -> None:
        dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 3, "cash": [50_000.0] * 3}
        )
        rates = pd.DataFrame(
            {"timestamp": dates, "Close": [2.0] * 3, "Low": [1.5] * 3}
        )
        first = simulate_actual_days_repo_sweep(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        appended = pd.concat(
            [
                rates,
                pd.DataFrame(
                    {
                        "timestamp": pd.to_datetime(["2024-01-05"]),
                        "Close": [99.0],
                        "Low": [99.0],
                    }
                ),
            ],
            ignore_index=True,
        )
        second = simulate_actual_days_repo_sweep(
            equity,
            appended,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertEqual(first["net_interest"], second["net_interest"])

    def test_protocol_discloses_post_hoc_status_and_no_promotion(self) -> None:
        protocol = protocol_manifest()
        self.assertTrue(protocol["post_hoc_disclosure"]["prior_result_seen"])
        self.assertEqual(
            protocol["evaluation"]["target_weighted_annualized_return"],
            TARGET_WEIGHTED_ANNUALIZED_RETURN,
        )
        self.assertTrue(
            protocol["decision_rule"]["passing_is_not_production_authorization"]
        )
        self.assertTrue(protocol["invariants"]["no_production_registration"])

    def test_commission_solver_returns_maximum_passing_rate(self) -> None:
        result = _bisect_maximum_passing_commission(
            lambda fee: 0.41 - fee * 1_000.0,
            target=0.40,
            lower=0.0,
            upper=0.00002,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(float(result), 0.00001, places=10)
        impossible = _bisect_maximum_passing_commission(
            lambda fee: 0.39 - fee,
            target=0.40,
            lower=0.0,
            upper=0.00002,
        )
        self.assertIsNone(impossible)


if __name__ == "__main__":
    unittest.main()
