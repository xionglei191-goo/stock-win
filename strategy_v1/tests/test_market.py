from __future__ import annotations

import unittest
from dataclasses import replace

import pandas as pd

from strategy_v1.config import StrategyConfig
from strategy_v1.market import evaluate_market_regime, rank_leaders, rank_sectors


def bars(start: float, step: float, periods: int = 30, volume: float = 1_000_000) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=periods, freq="B")
    close = pd.Series([start + step * index for index in range(periods)], index=index)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": volume,
            "Amount": close * volume / 10_000,
        },
        index=index,
    )


class MarketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = replace(StrategyConfig(), min_sector_members=1, minimum_average_turnover=0)

    def test_market_normal_when_two_conditions_pass(self) -> None:
        universe = {"000001.SZ": bars(10, 0.1), "600000.SH": bars(10, -0.01)}
        state = evaluate_market_regime(bars(3000, 5), universe, self.config)
        self.assertEqual(state.regime, "NORMAL")
        self.assertGreaterEqual(state.passed_conditions, 2)

    def test_sector_and_leader_ranking(self) -> None:
        daily = {"000001.SZ": bars(10, 0.2), "600000.SH": bars(10, 0.01)}
        sectors = {
            "S1": {"name": "强势", "members": ["000001.SZ"]},
            "S2": {"name": "弱势", "members": ["600000.SH"]},
        }
        ranked = rank_sectors(sectors, daily, self.config)
        self.assertEqual(ranked[0].code, "S1")
        leaders = rank_leaders(ranked, sectors, daily, {"000001.SZ": "A", "600000.SH": "B"}, self.config)
        self.assertEqual(leaders[0].code, "000001.SZ")


if __name__ == "__main__":
    unittest.main()
