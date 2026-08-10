from __future__ import annotations

import unittest
from dataclasses import replace

import pandas as pd

from strategy_v1.backtest import build_daily_schedule
from strategy_v1.config import StrategyConfig


def make_bars(values: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(values), freq="B")
    close = pd.Series(values, index=index, dtype=float)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": 1_000_000.0,
            "Amount": close * 100.0,
        },
        index=index,
    )


class NoLookaheadTests(unittest.TestCase):
    def test_future_price_change_does_not_change_prior_schedule(self) -> None:
        config = replace(
            StrategyConfig(),
            minimum_listing_bars=20,
            minimum_average_turnover=0,
            min_sector_members=1,
            top_sector_count=1,
            leaders_per_sector=1,
        )
        base = [10 + index * 0.1 for index in range(32)]
        changed = base.copy()
        changed[-1] = 1000.0
        index_bars = make_bars([3000 + index for index in range(32)])
        sectors = {"S1": {"name": "sector", "members": ["000001.SZ"]}}
        first, _ = build_daily_schedule(
            index_bars, {"000001.SZ": make_bars(base)}, {"000001.SZ": "A"}, sectors, config
        )
        second, _ = build_daily_schedule(
            index_bars, {"000001.SZ": make_bars(changed)}, {"000001.SZ": "A"}, sectors, config
        )
        compare_date = pd.date_range("2026-01-01", periods=31, freq="B")[-1].date().isoformat()
        self.assertEqual(set(first.get(compare_date, {})), set(second.get(compare_date, {})))


if __name__ == "__main__":
    unittest.main()
