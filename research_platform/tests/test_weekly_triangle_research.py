from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.tests.test_weekly_triangle import CODE, weekly_pattern
from research_platform.weekly_triangle_research import (
    _prepare_setup_episodes,
    analyze_weekly_triangle_feature_stability,
    analyze_weekly_triangle_setup_stability,
    evaluate_weekly_triangle_events,
    persist_weekly_triangle_research,
    persist_weekly_triangle_setup_stability,
)


class WeeklyTriangleResearchTests(unittest.TestCase):
    def test_event_study_uses_next_week_open_and_future_week_closes(self) -> None:
        frame = weekly_pattern(breakout=True)
        last = frame.iloc[-1]
        future = pd.DataFrame(
            [
                {
                    **last.to_dict(),
                    "Open": 13.00,
                    "High": 13.00 * (1.02 + index * 0.01),
                    "Low": 13.00 * 0.98,
                    "Close": 13.00 * (1.01 + index * 0.01),
                }
                for index in range(8)
            ],
            index=pd.date_range(frame.index[-1] + pd.Timedelta(days=7), periods=8, freq="W-FRI"),
        )
        bars = pd.concat([frame, future])

        result = evaluate_weekly_triangle_events(
            {CODE: bars},
            start=frame.index[-1],
            end=future.index[-1],
        )

        event = next(
            item
            for item in result["events"]
            if item["asof"] == frame.index[-1].date().isoformat()
        )
        self.assertEqual(event["stage"], "BREAKOUT")
        self.assertAlmostEqual(event["entry_price"], 13.00)
        self.assertAlmostEqual(event["return_1w"], 0.01)
        self.assertAlmostEqual(event["return_4w"], 0.04)
        self.assertAlmostEqual(event["return_8w"], 0.08)

    def test_setup_conversion_counts_only_later_breakouts(self) -> None:
        setup = weekly_pattern(breakout=False)
        breakout = weekly_pattern(breakout=True).iloc[[-1]]
        breakout.index = [setup.index[-1] + pd.Timedelta(days=7)]
        future = pd.concat(
            [breakout] * 9,
            ignore_index=True,
        )
        future.index = pd.date_range(breakout.index[-1], periods=9, freq="W-FRI")
        bars = pd.concat([setup, future])

        result = evaluate_weekly_triangle_events(
            {CODE: bars},
            start=setup.index[-1],
            end=future.index[-1],
        )

        self.assertGreaterEqual(result["setup_events"], 1)
        self.assertGreaterEqual(result["converted_4w"], 1)

    def test_market_filter_preserves_raw_breakout_audit(self) -> None:
        frame = weekly_pattern(breakout=True)
        future = pd.concat([frame.iloc[[-1]]] * 9, ignore_index=True)
        future.index = pd.date_range(
            frame.index[-1] + pd.Timedelta(days=7), periods=9, freq="W-FRI"
        )
        bars = pd.concat([frame, future])
        market_values = pd.Series(
            range(50, 10, -1),
            index=pd.date_range("2024-12-06", periods=40, freq="W-FRI"),
            dtype=float,
        )
        market = pd.DataFrame(
            {
                "Open": market_values,
                "High": market_values * 1.01,
                "Low": market_values * 0.99,
                "Close": market_values,
                "Volume": 1_000_000.0,
            }
        )

        result = evaluate_weekly_triangle_events(
            {CODE: bars},
            start=frame.index[-1],
            end=future.index[-1],
            market_index=market,
        )

        self.assertEqual(result["raw_breakout_events"], 1)
        self.assertEqual(result["market_blocked_events"], 1)
        self.assertEqual(result["breakout_events"], 0)

    def test_daily_execution_simulation_applies_time_exit_and_costs(self) -> None:
        frame = weekly_pattern(breakout=True)
        weekly_future = pd.concat([frame.iloc[[-1]]] * 9, ignore_index=True)
        weekly_future.index = pd.date_range(
            frame.index[-1] + pd.Timedelta(days=7), periods=9, freq="W-FRI"
        )
        front = pd.concat([frame, weekly_future])
        raw_index = pd.DatetimeIndex(
            [frame.index[-1], *pd.date_range(frame.index[-1] + pd.Timedelta(days=3), periods=21, freq="B")]
        )
        raw = pd.DataFrame(
            {
                "Open": 12.78,
                "High": 12.90,
                "Low": 12.70,
                "Close": 12.78,
                "Volume": 1_000_000.0,
            },
            index=raw_index,
        )

        result = evaluate_weekly_triangle_events(
            {CODE: front},
            start=frame.index[-1],
            end=weekly_future.index[-1],
            raw_bars={CODE: raw},
        )

        event = next(
            item
            for item in result["events"]
            if item["asof"] == frame.index[-1].date().isoformat()
        )
        self.assertEqual(event["exit_reason"], "WEEKLY_TIME_EXIT")
        self.assertEqual(event["holding_days"], 20)
        self.assertLess(event["net_return"], 0.0)
        self.assertLess(event["net_return_2x"], event["net_return"])
        self.assertAlmostEqual(event["entry_gap"], 0.0)
        self.assertEqual(result["exit_reasons"], {"WEEKLY_TIME_EXIT": 1})

    def test_cross_section_ranking_matches_strategy_entry_limit(self) -> None:
        frame = weekly_pattern(breakout=True)
        future = pd.concat([frame.iloc[[-1]]] * 9, ignore_index=True)
        future.index = pd.date_range(
            frame.index[-1] + pd.Timedelta(days=7), periods=9, freq="W-FRI"
        )
        history = pd.concat([frame, future])
        bars = {f"{index:06d}.SZ": history for index in range(21)}

        result = evaluate_weekly_triangle_events(
            bars,
            start=frame.index[-1],
            end=future.index[-1],
        )

        signal_day = frame.index[-1].date().isoformat()
        ranked = [
            event
            for event in result["events"]
            if event["asof"] == signal_day and event["stage"] == "BREAKOUT"
        ]
        selected = [event for event in ranked if event["entry_selected"]]
        excluded = [event for event in ranked if not event["entry_selected"]]
        self.assertEqual(result["eligible_breakout_events"], 21)
        self.assertEqual(result["breakout_events"], 20)
        self.assertEqual(result["rank_blocked_events"], 1)
        self.assertEqual(len(selected), 20)
        self.assertEqual(excluded[0]["code"], "000020.SZ")
        self.assertEqual(excluded[0]["cross_section_rank"], 21)

    def test_research_artifact_round_trips_features_and_summary(self) -> None:
        frame = weekly_pattern(breakout=True)
        future = pd.concat([frame.iloc[[-1]]] * 9, ignore_index=True)
        future.index = pd.date_range(
            frame.index[-1] + pd.Timedelta(days=7), periods=9, freq="W-FRI"
        )
        result = evaluate_weekly_triangle_events(
            {CODE: pd.concat([frame, future])},
            start=frame.index[-1],
            end=future.index[-1],
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = persist_weekly_triangle_research(result, directory, "sample")
            events = pd.read_parquet(paths["events"])
            summary = json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))

        self.assertIn("breakout_extension", events.columns)
        self.assertIn("prior_return_12w", events.columns)
        self.assertIn("cross_section_rank", events.columns)
        self.assertNotIn("events", summary)
        self.assertEqual(summary["breakout_events"], 1)

    def test_feature_direction_is_frozen_on_development_windows(self) -> None:
        def frame(positive_for_low: bool) -> pd.DataFrame:
            low_return, high_return = (
                (0.02, -0.01) if positive_for_low else (-0.03, 0.01)
            )
            return pd.DataFrame(
                {
                    "stage": ["BREAKOUT", "BREAKOUT"],
                    "entry_allowed": [True, True],
                    "net_return": [low_return, high_return],
                    "net_return_2x": [low_return - 0.002, high_return - 0.002],
                    "asof": ["2026-01-02", "2026-01-02"],
                    "code": ["000001.SZ", "000002.SZ"],
                    "probe": [0.1, 0.9],
                    "upper_touches": [2, 2],
                    "lower_touches": [2, 2],
                    "median_amount_4w": [1_000_000.0, 1_000_000.0],
                }
            )

        result = analyze_weekly_triangle_feature_stability(
            {
                "development_a": frame(True),
                "development_b": frame(True),
                "validation": frame(False),
            },
            development_windows=("development_a", "development_b"),
            maximum_entries=1,
            features=("probe",),
        )

        self.assertEqual(len(result["qualified"]), 1)
        candidate = result["qualified"][0]
        self.assertEqual(candidate["feature"], "probe")
        self.assertEqual(candidate["direction"], "low")
        self.assertLess(candidate["windows"]["validation"]["mean_2x"], 0.0)

    def test_setup_episode_conversion_is_strictly_future_and_right_censored(self) -> None:
        frame = pd.DataFrame(
            {
                "code": ["A", "A", "B", "C", "C", "Z"],
                "asof": [
                    "2026-01-02",
                    "2026-01-09",
                    "2026-03-20",
                    "2026-01-02",
                    "2026-01-02",
                    "2026-03-31",
                ],
                "stage": [
                    "SETUP",
                    "BREAKOUT",
                    "SETUP",
                    "SETUP",
                    "BREAKOUT",
                    "OTHER",
                ],
                "score": [0.8, 0.9, 0.7, 0.8, 0.9, 0.0],
            }
        )

        episodes = _prepare_setup_episodes(
            frame,
            episode_gap_days=14,
            conversion_days=35,
        )

        self.assertEqual(set(episodes["code"]), {"A", "C"})
        self.assertTrue(bool(episodes.loc[episodes["code"] == "A", "converted_4w"].iloc[0]))
        self.assertFalse(bool(episodes.loc[episodes["code"] == "C", "converted_4w"].iloc[0]))

    def test_setup_feature_must_pass_validation_before_promotion(self) -> None:
        def frame(validation: bool) -> pd.DataFrame:
            setup_codes = ["A", "B", "C", "D"]
            converted_codes = ["C", "D"] if validation else ["A", "B"]
            rows = [
                {
                    "code": code,
                    "asof": "2026-01-02",
                    "stage": "SETUP",
                    "score": 0.1 if code in {"A", "B"} else 0.9,
                    "price_location": 0.9 if code in {"A", "B"} else 0.1,
                    "entry_allowed": True,
                    "net_return": None,
                    "net_return_2x": None,
                }
                for code in setup_codes
            ]
            rows.extend(
                {
                    "code": code,
                    "asof": "2026-01-09",
                    "stage": "BREAKOUT",
                    "score": 0.8,
                    "price_location": 1.1,
                    "entry_allowed": True,
                    "net_return": -0.03 if validation else 0.03,
                    "net_return_2x": -0.032 if validation else 0.028,
                }
                for code in converted_codes
            )
            rows.append(
                {
                    "code": "Z",
                    "asof": "2026-03-06",
                    "stage": "OTHER",
                    "score": 0.0,
                    "price_location": 0.0,
                    "entry_allowed": False,
                    "net_return": None,
                    "net_return_2x": None,
                }
            )
            return pd.DataFrame(rows)

        result = analyze_weekly_triangle_setup_stability(
            {
                "development_a": frame(False),
                "development_b": frame(False),
                "validation_a": frame(True),
                "validation_b": frame(True),
            },
            development_windows=("development_a", "development_b"),
            validation_windows=("validation_a", "validation_b"),
            maximum_setups=2,
            maximum_entries=2,
            minimum_development_samples=2,
            minimum_trade_samples=2,
            features=("price_location",),
        )

        qualified = result["development_conversion_qualified"]
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["direction"], "high")
        self.assertTrue(qualified[0]["development_trade_qualified"])
        self.assertFalse(qualified[0]["validation_conversion_confirmed"])
        self.assertFalse(qualified[0]["validation_trade_confirmed"])
        self.assertEqual(result["promotion_qualified"], [])

        with tempfile.TemporaryDirectory() as directory:
            path = persist_weekly_triangle_setup_stability(result, directory)
            persisted = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(persisted["promotion_qualified"], [])


if __name__ == "__main__":
    unittest.main()
