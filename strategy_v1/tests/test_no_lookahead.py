from __future__ import annotations

import unittest
from dataclasses import replace

import pandas as pd

from strategy_v1.backtest import (
    _build_daily_schedule_reference,
    _slice_bars,
    build_daily_schedule,
)
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
    def test_vectorized_schedule_matches_reference_implementation(self) -> None:
        config = replace(
            StrategyConfig(),
            daily_lookback=60,
            minimum_listing_bars=25,
            minimum_average_turnover=0,
            min_sector_members=2,
            top_sector_count=2,
            leaders_per_sector=2,
        )
        codes = ["000001.SZ", "000002.SZ", "600001.SH", "600002.SH"]
        bars = {
            code: make_bars(
                [
                    10.0
                    + code_index
                    + day * (0.025 + code_index * 0.004)
                    + ((day + code_index) % 7) * 0.03
                    for day in range(72)
                ]
            )
            for code_index, code in enumerate(codes)
        }
        names = {code: f"stock-{index}" for index, code in enumerate(codes)}
        sectors = {
            "S1": {"name": "sector-1", "members": codes[:3]},
            "S2": {"name": "sector-2", "members": codes[1:]},
        }
        index_bars = make_bars([3000.0 + day * 1.5 for day in range(72)])

        expected_leaders, expected_markets = _build_daily_schedule_reference(
            index_bars, bars, names, sectors, config
        )
        actual_leaders, actual_markets = build_daily_schedule(
            index_bars, bars, names, sectors, config
        )

        self.assertEqual(set(actual_markets), set(expected_markets))
        self.assertEqual(set(actual_leaders), set(expected_leaders))
        for day, expected in expected_markets.items():
            actual = actual_markets[day]
            self.assertEqual(actual.regime, expected.regime)
            self.assertEqual(actual.index_above_ma20, expected.index_above_ma20)
            self.assertEqual(actual.passed_conditions, expected.passed_conditions)
            self.assertAlmostEqual(actual.breadth, expected.breadth, places=12)
            self.assertAlmostEqual(
                actual.average_return_5d,
                expected.average_return_5d,
                places=12,
            )
        for day, expected in expected_leaders.items():
            actual = actual_leaders[day]
            self.assertEqual(list(actual), list(expected))
            for code, expected_candidate in expected.items():
                actual_candidate = actual[code]
                self.assertEqual(actual_candidate.sector_code, expected_candidate.sector_code)
                self.assertEqual(actual_candidate.leader_rank, expected_candidate.leader_rank)
                self.assertAlmostEqual(
                    actual_candidate.sector_score,
                    expected_candidate.sector_score,
                    places=12,
                )
                self.assertAlmostEqual(
                    actual_candidate.leader_score,
                    expected_candidate.leader_score,
                    places=12,
                )

    def test_symbol_without_current_bar_is_excluded_from_schedule(self) -> None:
        config = replace(
            StrategyConfig(),
            minimum_listing_bars=20,
            minimum_average_turnover=0,
            min_sector_members=1,
            top_sector_count=1,
            leaders_per_sector=2,
        )
        index_bars = make_bars([3000.0 + day for day in range(32)])
        current = make_bars([10.0 + day * 0.1 for day in range(32)])
        stale = make_bars([20.0 + day * 0.2 for day in range(32)]).drop(
            index_bars.index[-2]
        )
        sectors = {
            "S1": {
                "name": "sector",
                "members": ["000001.SZ", "000002.SZ"],
            }
        }

        schedule, _ = build_daily_schedule(
            index_bars,
            {"000001.SZ": current, "000002.SZ": stale},
            {"000001.SZ": "A", "000002.SZ": "B"},
            sectors,
            config,
        )

        final_date = index_bars.index[-1].date().isoformat()
        self.assertNotIn("000002.SZ", schedule.get(final_date, {}))

    def test_daily_schedule_input_is_bounded_to_declared_lookback(self) -> None:
        bars = make_bars([float(index) for index in range(150)])
        asof = bars.index[130]

        sliced = _slice_bars({"000001.SZ": bars}, asof, 120)["000001.SZ"]

        self.assertEqual(len(sliced), 120)
        self.assertEqual(sliced.index[-1], asof)
        self.assertEqual(sliced.index[0], bars.index[11])

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
