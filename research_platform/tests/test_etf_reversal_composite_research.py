from __future__ import annotations

import unittest

import pandas as pd

from research_platform.config import PortfolioConfig
from research_platform.etf_reversal_composite_research import (
    assess_replication,
    protocol_manifest,
)
from research_platform.etf_trend_overlay_research import simulate_v9_overlay


class EtfReversalCompositeResearchTests(unittest.TestCase):
    def test_protocol_reuses_original_rules_and_seals_holdout(self) -> None:
        protocol = protocol_manifest()
        self.assertEqual(
            protocol["components"]["etf_reversal"]["entry_and_exit"],
            "unchanged original 1.0.0 protocol",
        )
        self.assertEqual(protocol["components"]["etf_reversal"]["maximum_positions"], 3)
        self.assertTrue(protocol["data"]["holdout_remains_sealed_until_replication_passes"])
        self.assertTrue(protocol["invariants"]["no_production_promotion"])

    def test_shared_cash_simulator_can_honor_original_three_position_cap(self) -> None:
        dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
        events = pd.DataFrame(
            [
                {
                    "code": code,
                    "entry_date": dates[0],
                    "entry_open": 10.0,
                    "exit_date": dates[1],
                    "exit_open": 10.1,
                    "score": float(4 - index),
                }
                for index, code in enumerate(("510001.SH", "510002.SH", "510003.SH"), 1)
            ]
        )
        etf_bars = pd.DataFrame(
            [
                {"code": code, "timestamp": day, "Close": 10.0}
                for code in events["code"]
                for day in dates
            ]
        )
        v9_bars = pd.DataFrame(columns=["code", "timestamp", "Close"])
        v9_trades = pd.DataFrame(columns=["timestamp", "side", "code", "quantity", "price", "fees"])
        result = simulate_v9_overlay(
            v9_trades,
            v9_bars,
            events,
            etf_bars,
            dates,
            initial_cash=50_000.0,
            config=PortfolioConfig(),
            maximum_etf_positions=3,
        )
        self.assertEqual(result["etf_trades"], 3)
        self.assertEqual(result["v9_cash_blocked"], 0)

    def test_replication_gate_requires_robust_base_and_stress_increment(self) -> None:
        report = {
            "portfolio_trades": 20,
            "portfolio_total_return": 0.01,
            "median_trade_return": 0.01,
            "ex_top3_contribution": 0.005,
            "fill_rate": 0.9,
            "portfolio_max_drawdown": -0.03,
        }
        bundle = {
            "incremental_total_return": 0.003,
            "daily_return_correlation": 0.2,
            "max_drawdown": -0.08,
            "v9_reproduction_match": True,
            "repo_control_match": True,
            "v9_cash_blocked": 0,
        }
        decision = assess_replication(report, report, bundle, bundle)
        self.assertTrue(decision["replication_qualified"])
        failed_stress = dict(bundle, incremental_total_return=-0.001)
        rejected = assess_replication(report, report, bundle, failed_stress)
        self.assertFalse(rejected["replication_qualified"])
        self.assertEqual(rejected["decision"], "REJECT")


if __name__ == "__main__":
    unittest.main()
