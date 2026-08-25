from __future__ import annotations

import unittest

import pandas as pd

from research_platform.cross_market_tenor_ladder_research import (
    SIMULATION_TENORS,
    protocol_manifest,
    simulate_cross_market_tenor_ladder,
)


def _rates(dates: pd.DatetimeIndex, default_rate: float = 1.0) -> pd.DataFrame:
    rows = []
    for tenor in SIMULATION_TENORS:
        for day in dates:
            rows.append(
                {
                    "timestamp": day,
                    "code": tenor.code,
                    "tenor_days": tenor.tenor_days,
                    "Close": default_rate,
                    "Low": default_rate,
                    "Volume": 1_000.0,
                }
            )
    return pd.DataFrame(rows)


class CrossMarketTenorLadderResearchTests(unittest.TestCase):
    def test_shanghai_tenors_are_blocked_before_effective_date(self) -> None:
        dates = pd.to_datetime(
            ["2022-05-12", "2022-05-13", "2022-05-16", "2022-05-17"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": 50_000.0, "cash": 50_000.0}
        )
        rates = _rates(dates)
        rates.loc[rates["code"].str.endswith(".SH"), "Close"] = 99.0
        result = simulate_cross_market_tenor_ladder(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertTrue(result["equity"].loc[:1, "repo_code"].str.endswith(".SZ").all())

    def test_friday_can_select_best_three_day_tenor(self) -> None:
        dates = pd.to_datetime(
            ["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": 50_000.0, "cash": 50_000.0}
        )
        rates = _rates(dates)
        rates.loc[
            rates["timestamp"].eq(dates[0]) & rates["code"].eq("204003.SH"),
            "Close",
        ] = 5.0
        result = simulate_cross_market_tenor_ladder(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertEqual(result["equity"].loc[0, "repo_code"], "204003.SH")

    def test_regular_day_allows_only_one_day_tenor(self) -> None:
        dates = pd.to_datetime(
            ["2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": 50_000.0, "cash": 50_000.0}
        )
        rates = _rates(dates)
        rates.loc[rates["tenor_days"].gt(1), "Close"] = 99.0
        result = simulate_cross_market_tenor_ladder(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertTrue(result["equity"].loc[:1, "repo_tenor_days"].eq(1).all())

    def test_future_quote_does_not_change_completed_interest(self) -> None:
        dates = pd.to_datetime(
            ["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": 50_000.0, "cash": 50_000.0}
        )
        rates = _rates(dates)
        first = simulate_cross_market_tenor_ladder(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        future = rates.copy()
        future.loc[future["timestamp"].eq(dates[-1]), "Close"] = 99.0
        second = simulate_cross_market_tenor_ladder(
            equity,
            future,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertEqual(first["net_interest"], second["net_interest"])

    def test_protocol_never_authorizes_production(self) -> None:
        protocol = protocol_manifest()
        self.assertFalse(
            protocol["post_hoc_disclosure"][
                "longer_shanghai_tenor_history_seen_before_freeze"
            ]
        )
        self.assertTrue(protocol["historical_eligibility"]["no_backfill"])
        self.assertTrue(protocol["invariants"]["no_production_promotion"])
        self.assertTrue(protocol["invariants"]["no_parameter_scan_after_result"])


if __name__ == "__main__":
    unittest.main()
