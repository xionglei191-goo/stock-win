from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.models import SignalStatus
from research_platform.strategies.weekly_triangle import (
    WeeklyTriangleStrategy,
    analyze_weekly_triangle,
    completed_precomputed_weekly_bars,
    completed_weekly_bars,
    resample_weekly_bars,
)


CODE = "600000.SH"


def weekly_pattern(*, breakout: bool) -> pd.DataFrame:
    pre_close = np.linspace(11.0, 11.6, 20)
    pre = pd.DataFrame(
        {
            "Open": pre_close * 0.995,
            "High": pre_close * 1.03,
            "Low": pre_close * 0.97,
            "Close": pre_close,
            "Volume": np.full(20, 1_500_000.0),
        }
    )
    upper = np.linspace(13.4, 12.6, 10)
    lower = np.linspace(10.7, 12.05, 10)
    triangle_close = lower + (upper - lower) * 0.70
    triangle = pd.DataFrame(
        {
            "Open": triangle_close * 0.995,
            "High": upper,
            "Low": lower,
            "Close": triangle_close,
            "Volume": np.linspace(1_800_000.0, 1_000_000.0, 10),
        }
    )
    frames = [pre, triangle]
    if breakout:
        frames.append(
            pd.DataFrame(
                {
                    "Open": [12.55],
                    "High": [12.95],
                    "Low": [12.42],
                    "Close": [12.78],
                    "Volume": [1_800_000.0],
                }
            )
        )
    result = pd.concat(frames, ignore_index=True)
    result["Amount"] = result["Close"] * result["Volume"]
    result.index = pd.date_range("2025-01-10", periods=len(result), freq="W-FRI")
    return result


def market_pattern(end: pd.Timestamp, *, rising: bool = True) -> pd.DataFrame:
    values = np.linspace(100.0, 140.0, 40)
    if not rising:
        values = values[::-1]
    index = pd.date_range(end=end, periods=len(values), freq="W-FRI")
    return pd.DataFrame(
        {
            "Open": values,
            "High": values * 1.01,
            "Low": values * 0.99,
            "Close": values,
            "Volume": np.full(len(values), 10_000_000.0),
        },
        index=index,
    )


class WeeklyTriangleTests(unittest.TestCase):
    def test_weekly_resampler_does_not_truncate_historical_input(self) -> None:
        index = pd.date_range("2020-01-03", periods=220, freq="W-FRI")
        values = np.linspace(10.0, 20.0, len(index))
        frame = pd.DataFrame(
            {
                "Open": values,
                "High": values * 1.01,
                "Low": values * 0.99,
                "Close": values,
                "Volume": np.full(len(index), 1_000_000.0),
            },
            index=index,
        )

        weekly = resample_weekly_bars(frame)

        self.assertEqual(len(weekly), 220)
        self.assertEqual(weekly.index[0], index[0])

    def test_shorter_valid_convergence_window_is_detected(self) -> None:
        close = np.linspace(40.0, 43.0, 25)
        weekly = pd.DataFrame(
            {
                "Open": close * 0.995,
                "High": close * 1.03,
                "Low": close * 0.97,
                "Close": close,
                "Volume": np.full(25, 2_000_000.0),
            }
        )
        recent = pd.DataFrame(
            {
                "Open": [47.0, 42.0, 45.0, 43.0, 45.8],
                "High": [50.70, 49.46, 46.58, 48.25, 46.44],
                "Low": [39.21, 40.73, 40.80, 42.41, 42.47],
                "Close": [48.47, 42.50, 45.81, 43.18, 46.42],
                "Volume": [617, 423, 538, 490, 369],
            }
        )
        weekly = pd.concat([weekly, recent], ignore_index=True)
        weekly.index = pd.date_range("2025-09-19", periods=30, freq="W-FRI")

        analysis = analyze_weekly_triangle(weekly)

        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["stage"], "SETUP")
        self.assertEqual(analysis["triangle_weeks"], 5)

    def test_converging_triangle_is_a_setup_without_entry(self) -> None:
        weekly = weekly_pattern(breakout=False)

        analysis = analyze_weekly_triangle(weekly)
        result = WeeklyTriangleStrategy().scan(
            run_id="setup",
            asof=weekly.index[-1],
            front_bars={CODE: weekly},
            raw_bars={CODE: weekly},
            names={CODE: "test"},
            positions=[],
            index_bars=market_pattern(weekly.index[-1]),
        )

        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["stage"], "SETUP")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.signals, ())

    def test_breakout_requires_volume_and_emits_proposed_entry(self) -> None:
        weekly = weekly_pattern(breakout=True)

        result = WeeklyTriangleStrategy().scan(
            run_id="breakout",
            asof=weekly.index[-1],
            front_bars={CODE: weekly},
            raw_bars={CODE: weekly},
            names={CODE: "test"},
            positions=[],
            index_bars=market_pattern(weekly.index[-1]),
            backtest_mode=True,
        )

        self.assertEqual(result.candidates[0]["stage"], "BREAKOUT")
        self.assertEqual(len(result.signals), 1)
        signal = result.signals[0]
        self.assertEqual(signal.side, "BUY")
        self.assertEqual(signal.status, SignalStatus.PROPOSED)
        self.assertLess(signal.stop_price, float(weekly["Close"].iloc[-1]))
        self.assertEqual(
            signal.generated_at.date().isoformat(),
            weekly.index[-1].date().isoformat(),
        )

    def test_live_breakout_is_observation_only_after_historical_rejection(self) -> None:
        weekly = weekly_pattern(breakout=True)

        result = WeeklyTriangleStrategy().scan(
            run_id="live_observation",
            asof=weekly.index[-1],
            front_bars={CODE: weekly},
            raw_bars={CODE: weekly},
            names={CODE: "test"},
            positions=[],
            index_bars=market_pattern(weekly.index[-1]),
        )

        self.assertEqual(result.candidates[0]["stage"], "BREAKOUT")
        self.assertTrue(result.candidates[0]["observation_only"])
        self.assertEqual(result.signals, ())
        self.assertFalse(result.state["entry_signals_enabled"])
        self.assertEqual(result.state["qualified_breakout_count"], 1)
        self.assertEqual(result.state["entry_ready_count"], 0)
        self.assertEqual(WeeklyTriangleStrategy.metadata.lifecycle, "HISTORICAL_REJECTED")

    def test_weak_market_preserves_breakout_candidate_but_blocks_entry(self) -> None:
        weekly = weekly_pattern(breakout=True)

        result = WeeklyTriangleStrategy().scan(
            run_id="weak_market",
            asof=weekly.index[-1],
            front_bars={CODE: weekly},
            raw_bars={CODE: weekly},
            names={CODE: "test"},
            positions=[],
            index_bars=market_pattern(weekly.index[-1], rising=False),
        )

        self.assertEqual(result.candidates[0]["stage"], "BREAKOUT")
        self.assertFalse(result.candidates[0]["market_above_ma"])
        self.assertFalse(result.candidates[0]["entry_allowed"])
        self.assertEqual(result.state["breakout_count"], 1)
        self.assertEqual(result.state["entry_ready_count"], 0)
        self.assertEqual(result.signals, ())

    def test_partial_week_is_excluded_from_confirmation(self) -> None:
        weekly = weekly_pattern(breakout=False)
        monday = weekly.index[-1] + pd.Timedelta(days=3)
        partial = pd.DataFrame(
            {
                "Open": [12.60],
                "High": [13.10],
                "Low": [12.50],
                "Close": [13.00],
                "Volume": [2_000_000.0],
                "Amount": [26_000_000.0],
            },
            index=[monday],
        )
        daily = pd.concat([weekly, partial])

        completed = completed_weekly_bars(daily, monday)
        result = WeeklyTriangleStrategy().scan(
            run_id="partial",
            asof=monday,
            front_bars={CODE: daily},
            raw_bars={CODE: daily},
            names={CODE: "test"},
            positions=[],
        )

        self.assertEqual(completed.index[-1], weekly.index[-1])
        self.assertEqual(result.candidates[0]["stage"], "SETUP")
        self.assertEqual(result.signals, ())

    def test_precomputed_weekly_data_is_still_cut_off_at_asof(self) -> None:
        setup = weekly_pattern(breakout=False)
        future = weekly_pattern(breakout=True).iloc[[-1]].copy()
        future.index = [setup.index[-1] + pd.Timedelta(days=7)]
        complete_history = pd.concat([setup, future])
        strategy = WeeklyTriangleStrategy()
        prepared = strategy.prepare_backtest_data(front_bars={CODE: complete_history})

        visible = completed_precomputed_weekly_bars(
            prepared["weekly_front"][CODE],
            setup.index[-1],
        )
        result = strategy.scan(
            run_id="precomputed_no_future",
            asof=setup.index[-1],
            front_bars={CODE: setup},
            raw_bars={CODE: setup},
            names={CODE: "test"},
            positions=[],
            prepared_backtest_data=prepared,
            index_bars=market_pattern(setup.index[-1]),
        )

        self.assertEqual(visible.index[-1], setup.index[-1])
        self.assertEqual(result.candidates[0]["stage"], "SETUP")
        self.assertEqual(result.signals, ())

    def test_stale_symbol_is_not_returned_as_a_current_candidate(self) -> None:
        weekly = weekly_pattern(breakout=False)
        market_day = weekly.index[-1] + pd.Timedelta(days=7)

        result = WeeklyTriangleStrategy().scan(
            run_id="stale",
            asof=market_day,
            front_bars={CODE: weekly},
            raw_bars={CODE: weekly},
            names={CODE: "suspended"},
            positions=[],
        )

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.signals, ())

    def test_completed_week_candidate_is_reused_during_next_week(self) -> None:
        weekly = weekly_pattern(breakout=False)
        strategy = WeeklyTriangleStrategy()
        friday = strategy.scan(
            run_id="friday",
            asof=weekly.index[-1],
            front_bars={CODE: weekly},
            raw_bars={CODE: weekly},
            names={CODE: "test"},
            positions=[],
        )
        monday = weekly.index[-1] + pd.Timedelta(days=3)
        partial = pd.DataFrame(
            {
                "Open": [12.40],
                "High": [12.50],
                "Low": [12.30],
                "Close": [12.45],
                "Volume": [200_000.0],
                "Amount": [2_490_000.0],
            },
            index=[monday],
        )
        daily = pd.concat([weekly, partial])

        reused = strategy.scan(
            run_id="monday",
            asof=monday,
            front_bars={CODE: daily},
            raw_bars={CODE: daily},
            names={CODE: "test"},
            positions=[],
            runtime_state=friday.state["runtime_state"],
        )

        self.assertEqual(reused.candidates, friday.candidates)

    def test_position_fixed_stop_is_approved_exit(self) -> None:
        weekly = weekly_pattern(breakout=False)
        raw = weekly.copy()
        raw.loc[raw.index[-1], "Close"] = 9.0

        result = WeeklyTriangleStrategy().scan(
            run_id="exit",
            asof=raw.index[-1],
            front_bars={CODE: weekly},
            raw_bars={CODE: raw},
            names={CODE: "test"},
            positions=[
                {
                    "code": CODE,
                    "stop_price": 10.0,
                    "entry_time": weekly.index[-5].date().isoformat(),
                    "average_price": 11.0,
                }
            ],
        )

        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].side, "SELL")
        self.assertEqual(result.signals[0].status, SignalStatus.APPROVED)
        self.assertEqual(result.signals[0].reason_codes, ("FIXED_STOP",))

    def test_position_exits_after_twenty_observed_trading_bars(self) -> None:
        weekly = weekly_pattern(breakout=False)

        result = WeeklyTriangleStrategy().scan(
            run_id="time_exit",
            asof=weekly.index[-1],
            front_bars={CODE: weekly},
            raw_bars={CODE: weekly},
            names={CODE: "test"},
            positions=[
                {
                    "code": CODE,
                    "stop_price": 1.0,
                    "entry_time": weekly.index[-20].date().isoformat(),
                    "average_price": 13.0,
                }
            ],
        )

        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].reason_codes, ("WEEKLY_TIME_EXIT",))
        self.assertEqual(result.signals[0].evidence["holding_days"], 20)


if __name__ == "__main__":
    unittest.main()
