from __future__ import annotations

import struct
import unittest

import pandas as pd

from research_platform.reverse_repo_sweep_research import (
    BASE_COMMISSION_RATE,
    TARGET_WEIGHTED_ANNUALIZED_RETURN,
    assess_reverse_repo_sweep,
    decode_repo_day_bytes,
    protocol_manifest,
    simulate_repo_sweep,
)


def _day_record(
    date_value: int,
    open_rate: int,
    high_rate: int,
    low_rate: int,
    close_rate: int,
) -> bytes:
    return struct.pack(
        "<IIIIIfII",
        date_value,
        open_rate,
        high_rate,
        low_rate,
        close_rate,
        0.0,
        0,
        0,
    )


class ReverseRepoSweepResearchTests(unittest.TestCase):
    def test_decoder_uses_repo_rate_scale(self) -> None:
        payload = _day_record(20240102, 15_000, 18_000, 12_000, 14_900)
        rates = decode_repo_day_bytes(payload)
        self.assertEqual(rates.loc[0, "timestamp"], pd.Timestamp("2024-01-02"))
        self.assertAlmostEqual(rates.loc[0, "Open"], 1.5)
        self.assertAlmostEqual(rates.loc[0, "Close"], 1.49)

    def test_sweep_uses_prior_cash_lot_and_one_day_interest(self) -> None:
        equity = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
                "equity": [50_000.0, 50_000.0, 50_000.0],
                "cash": [50_000.0, 12_345.0, 12_345.0],
            }
        )
        rates = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "Close": [3.65, 3.65],
                "Low": [3.0, 3.0],
            }
        )
        result = simulate_repo_sweep(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=BASE_COMMISSION_RATE,
        )
        frame = result["equity"]
        first = 50_000.0 * 0.0365 / 365.0 - 50_000.0 * BASE_COMMISSION_RATE
        second = 12_000.0 * 0.0365 / 365.0 - 12_000.0 * BASE_COMMISSION_RATE
        self.assertEqual(frame.loc[0, "repo_principal"], 50_000.0)
        self.assertEqual(frame.loc[1, "repo_principal"], 12_000.0)
        self.assertAlmostEqual(frame.loc[1, "accrued_repo_interest"], first)
        self.assertAlmostEqual(frame.loc[2, "accrued_repo_interest"], first + second)
        self.assertAlmostEqual(result["net_interest"], first + second)

    def test_unprofitable_quote_is_skipped(self) -> None:
        equity = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "equity": [50_000.0, 50_000.0],
                "cash": [50_000.0, 50_000.0],
            }
        )
        rates = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-02"]),
                "Close": [0.10],
                "Low": [0.10],
            }
        )
        result = simulate_repo_sweep(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=BASE_COMMISSION_RATE,
        )
        self.assertEqual(result["repo_trades"], 0)
        self.assertEqual(result["skipped_unprofitable"], 1)
        self.assertEqual(result["net_interest"], 0.0)

    def test_appended_future_rate_does_not_change_history(self) -> None:
        equity = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "equity": [50_000.0, 50_000.0],
                "cash": [50_000.0, 50_000.0],
            }
        )
        rates = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-02"]),
                "Close": [2.0],
                "Low": [1.5],
            }
        )
        first = simulate_repo_sweep(
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
                        "timestamp": pd.to_datetime(["2024-01-04"]),
                        "Close": [99.0],
                        "Low": [99.0],
                    }
                ),
            ],
            ignore_index=True,
        )
        second = simulate_repo_sweep(
            equity,
            appended,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertEqual(first["net_interest"], second["net_interest"])
        self.assertEqual(first["final_equity"], second["final_equity"])

    def test_assessment_never_authorizes_production(self) -> None:
        baseline = {
            "weighted_annualized_return": 0.38,
            "exact_baseline_reproduction": True,
        }
        good_window = {
            "incremental_total_return": 0.01,
            "max_drawdown": -0.05,
            "drawdown_degradation": 0.0,
            "rate_coverage": 1.0,
            "net_interest": 100.0,
        }
        base = {
            "weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
            "windows": [good_window],
        }
        stress = {"weighted_annualized_return": 0.39, "windows": [good_window]}
        decision = assess_reverse_repo_sweep(baseline, base, stress)
        self.assertEqual(decision["decision"], "REQUIRE_BROKER_EXECUTION_AUDIT")
        self.assertTrue(decision["retrospective_qualified"])
        self.assertFalse(decision["production_authorized"])

        rejected = assess_reverse_repo_sweep(
            baseline, {**base, "weighted_annualized_return": 0.399}, stress
        )
        self.assertEqual(rejected["decision"], "REJECT")

    def test_protocol_is_research_only_and_conservative(self) -> None:
        protocol = protocol_manifest()
        self.assertTrue(protocol["invariants"]["v9_trade_dates_unchanged"])
        self.assertTrue(protocol["invariants"]["no_production_registration"])
        self.assertEqual(protocol["execution"]["credited_days_per_roll"], 1)
        self.assertIn("ignored", protocol["execution"]["weekend_and_holiday_extra_days"])
        self.assertTrue(
            protocol["decision_rule"]["passing_is_not_production_authorization"]
        )


if __name__ == "__main__":
    unittest.main()
