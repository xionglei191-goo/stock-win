from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.strategies.weekly_bull_platform import (
    WeeklyBullPlatformParameters,
    WeeklyBullPlatformStrategy,
    analyze_weekly_bull_platform,
    is_a_share_stock_code,
)


def bottom_base_rising_pattern() -> pd.DataFrame:
    pre_base = np.linspace(20.0, 16.0, 12)
    base = 16.5 + np.array(
        [
            -0.3,
            0.1,
            -0.5,
            0.3,
            -0.2,
            0.4,
            -0.1,
            0.5,
            0.0,
            0.3,
            -0.2,
            0.4,
            -0.1,
            0.6,
            0.1,
            0.5,
            0.0,
            0.7,
            0.2,
            0.6,
        ]
    )
    rise = np.linspace(18.2, 39.0, 18) + np.resize(
        np.array([0.0, 0.5, -0.2, 0.7, -0.4, 0.6, -0.3, 0.9, -0.5]),
        18,
    )
    upper_trend = np.linspace(39.5, 48.5, 22) + np.resize(
        np.array([0.0, 0.8, -0.6, 1.0, -0.8, 0.7, -0.4, 0.9, -0.7]),
        22,
    )
    upper_trend[-1] = 48.2
    close = np.r_[pre_base, base, rise, upper_trend]
    candle_range = np.r_[
        np.full(len(pre_base), 0.07),
        np.full(len(base), 0.06),
        np.full(len(rise), 0.10),
        np.full(len(upper_trend), 0.09),
    ]
    volume = np.r_[
        np.full(len(pre_base), 1_000_000.0),
        np.full(len(base), 800_000.0),
        np.full(len(rise), 1_800_000.0),
        np.full(len(upper_trend), 1_400_000.0),
    ]
    return pd.DataFrame(
        {
            "Open": close * 0.99,
            "High": close * (1.0 + candle_range / 2.0),
            "Low": close * (1.0 - candle_range / 2.0),
            "Close": close,
            "Volume": volume,
        },
        index=pd.date_range("2025-01-03", periods=len(close), freq="W-FRI"),
    )


def bottom_base_breakout_pattern() -> pd.DataFrame:
    setup = bottom_base_rising_pattern()
    breakout = pd.DataFrame(
        {
            "Open": [49.0],
            "High": [55.0],
            "Low": [48.5],
            "Close": [53.0],
            "Volume": [2_800_000.0],
        },
        index=[setup.index[-1] + pd.Timedelta(days=7)],
    )
    return pd.concat([setup, breakout])


def post_breakout_extension_pattern() -> pd.DataFrame:
    breakout = bottom_base_breakout_pattern()
    extension = pd.DataFrame(
        {
            "Open": [53.5],
            "High": [57.0],
            "Low": [52.5],
            "Close": [56.0],
            "Volume": [2_200_000.0],
        },
        index=[breakout.index[-1] + pd.Timedelta(days=7)],
    )
    return pd.concat([breakout, extension])


def single_week_spike_pattern() -> pd.DataFrame:
    pre_base = np.linspace(20.0, 16.0, 10)
    base = 16.5 + np.resize(np.array([-0.3, 0.2, -0.2, 0.3]), 20)
    flat = 17.0 + np.resize(np.array([-0.2, 0.1, 0.2, -0.1]), 19)
    close = np.r_[pre_base, base, flat, 30.0]
    return pd.DataFrame(
        {
            "Open": close * 0.99,
            "High": close * 1.04,
            "Low": close * 0.96,
            "Close": close,
            "Volume": np.full(len(close), 1_000_000.0),
        },
        index=pd.date_range("2025-01-03", periods=len(close), freq="W-FRI"),
    )


class WeeklyBullPlatformTests(unittest.TestCase):
    def test_bottom_base_rise_and_bull_alignment_are_detected(self) -> None:
        analysis = analyze_weekly_bull_platform(bottom_base_rising_pattern())

        self.assertIsNotNone(analysis)
        assert analysis is not None
        self.assertEqual(analysis["stage"], "SETUP")
        self.assertGreaterEqual(analysis["advance_from_base"], 0.35)
        self.assertLessEqual(analysis["base_close_band"], 0.18)
        self.assertLessEqual(analysis["pre_base_position"], 0.50)
        self.assertLessEqual(analysis["structure_base_position"], 0.30)
        self.assertGreater(analysis["ma5"], analysis["ma10"])
        self.assertGreater(analysis["ma10"], analysis["ma20"])
        self.assertGreater(analysis["ma20"], analysis["ma30"])
        self.assertGreaterEqual(analysis["ma20_slope"], 0.005)
        self.assertGreaterEqual(analysis["ma30_slope"], 0.005)

    def test_single_week_spike_is_not_a_rising_weekly_trend(self) -> None:
        analysis = analyze_weekly_bull_platform(single_week_spike_pattern())

        self.assertIsNone(analysis)

    def test_post_breakout_extension_is_not_mislabeled_as_setup(self) -> None:
        analysis = analyze_weekly_bull_platform(post_breakout_extension_pattern())

        self.assertIsNone(analysis)

    def test_etf_codes_are_excluded_from_strategy(self) -> None:
        strategy = WeeklyBullPlatformStrategy()

        self.assertTrue(strategy._eligible_code("600000.SH", "浦发银行"))
        self.assertTrue(strategy._eligible_code("300001.SZ", "特锐德"))
        self.assertFalse(strategy._eligible_code("510300.SH", "沪深300ETF"))
        self.assertFalse(strategy._eligible_code("159915.SZ", "创业板ETF"))
        self.assertFalse(strategy._eligible_code("511880.SH", "货币ETF"))
        self.assertFalse(is_a_share_stock_code("000300.SH"))

    def test_live_scan_is_observation_only(self) -> None:
        weekly = bottom_base_rising_pattern()
        result = WeeklyBullPlatformStrategy().scan(
            run_id="platform",
            asof=weekly.index[-1],
            front_bars={"600000.SH": weekly, "510300.SH": weekly},
            raw_bars={"600000.SH": weekly, "510300.SH": weekly},
            names={"600000.SH": "test", "510300.SH": "沪深300ETF"},
            positions=[],
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0]["code"], "600000.SH")
        self.assertEqual(result.signals, ())
        self.assertTrue(result.candidates[0]["observation_only"])

    def test_breakout_requires_price_and_volume_confirmation(self) -> None:
        analysis = analyze_weekly_bull_platform(bottom_base_breakout_pattern())

        self.assertIsNotNone(analysis)
        assert analysis is not None
        self.assertEqual(analysis["stage"], "BREAKOUT")
        self.assertGreater(analysis["close"], analysis["upper_boundary"] * 1.02)
        self.assertGreaterEqual(analysis["volume_ratio"], 1.20)

    def test_precomputed_weekly_history_is_cut_off_at_asof(self) -> None:
        complete = bottom_base_breakout_pattern()
        setup = complete.iloc[:-1]
        strategy = WeeklyBullPlatformStrategy()
        prepared = strategy.prepare_backtest_data(front_bars={"600000.SH": complete})

        result = strategy.scan(
            run_id="no_future",
            asof=setup.index[-1],
            front_bars={"600000.SH": setup},
            raw_bars={"600000.SH": setup},
            names={"600000.SH": "test"},
            positions=[],
            prepared_backtest_data=prepared,
            backtest_mode=True,
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0]["stage"], "SETUP")
        self.assertEqual(result.signals, ())

    def test_parameter_change_invalidates_same_week_candidate_cache(self) -> None:
        weekly = bottom_base_rising_pattern()
        strict = WeeklyBullPlatformStrategy(
            WeeklyBullPlatformParameters(minimum_advance_from_base=3.0)
        ).scan(
            run_id="strict",
            asof=weekly.index[-1],
            front_bars={"600000.SH": weekly},
            raw_bars={"600000.SH": weekly},
            names={"600000.SH": "test"},
            positions=[],
        )
        self.assertEqual(strict.candidates, ())

        relaxed = WeeklyBullPlatformStrategy().scan(
            run_id="relaxed",
            asof=weekly.index[-1],
            front_bars={"600000.SH": weekly},
            raw_bars={"600000.SH": weekly},
            names={"600000.SH": "test"},
            positions=[],
            runtime_state=strict.state["runtime_state"],
        )

        self.assertEqual(len(relaxed.candidates), 1)


if __name__ == "__main__":
    unittest.main()
