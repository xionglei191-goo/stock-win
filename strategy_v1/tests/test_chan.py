from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from strategy_v1.chan import (
    Center,
    ChanParameters,
    Fractal,
    Stroke,
    build_strokes,
    detect_bearish_divergence,
    detect_center_cross,
    daily_entry_allowed,
    daily_trailing_exit,
    find_centers,
    find_fractals,
    merge_inclusions,
)


def frame_from_rows(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01 09:30", periods=len(rows), freq="30min")
    return pd.DataFrame(
        {
            "Open": [row[2] for row in rows],
            "High": [row[0] for row in rows],
            "Low": [row[1] for row in rows],
            "Close": [row[2] for row in rows],
            "Volume": [1000.0] * len(rows),
        },
        index=index,
    )


class ChanTests(unittest.TestCase):
    def test_daily_entry_filter_rejects_excessive_volatility_and_chasing(self) -> None:
        frame = frame_from_rows([(10 + i * 0.1, 9 + i * 0.1, 9.5 + i * 0.1) for i in range(22)])
        parameters = ChanParameters(max_atr_ratio=0.10, max_signal_return=0.10)
        self.assertTrue(daily_entry_allowed(frame, parameters))

        volatile = frame.copy()
        volatile.iloc[-1, volatile.columns.get_loc("High")] = 20.0
        self.assertFalse(daily_entry_allowed(volatile, parameters))

        chased = frame.copy()
        chased.iloc[-1, chased.columns.get_loc("Close")] = float(chased["Close"].iloc[-2]) * 1.15
        self.assertFalse(daily_entry_allowed(chased, parameters))

    def test_daily_trailing_exit_uses_only_prices_since_entry(self) -> None:
        frame = frame_from_rows([(10, 9, 10), (12, 10, 11.5), (11.5, 10.5, 10.7)])
        parameters = ChanParameters(trailing_activation=0.10, trailing_drawdown=0.06)

        self.assertTrue(daily_trailing_exit(frame, frame.index[0], 10.0, parameters))
        self.assertFalse(daily_trailing_exit(frame, frame.index[1], 11.5, parameters))

    def test_upward_inclusion_uses_higher_low(self) -> None:
        frame = frame_from_rows([(10, 8, 9), (12, 9, 11), (11, 10, 10.5)])
        merged = merge_inclusions(frame)
        self.assertEqual(len(merged), 2)
        self.assertEqual(float(merged.iloc[-1]["High"]), 12.0)
        self.assertEqual(float(merged.iloc[-1]["Low"]), 10.0)
        self.assertEqual(int(merged.iloc[-1]["SourceCount"]), 2)

    def test_confirmed_fractals_require_left_and_right_bar(self) -> None:
        merged = frame_from_rows([(9, 7, 8), (12, 10, 11), (10, 8, 9), (8, 6, 7), (11, 9, 10)])
        fractals = find_fractals(merged)
        self.assertEqual([(item.kind, item.position) for item in fractals], [("TOP", 1), ("BOTTOM", 3)])

    def test_strokes_alternate_and_respect_minimum_distance(self) -> None:
        times = pd.date_range("2026-01-01", periods=20, freq="30min")
        fractals = [
            Fractal(1, times[1], "BOTTOM", 8.0),
            Fractal(3, times[3], "TOP", 11.0),
            Fractal(6, times[6], "TOP", 12.0),
            Fractal(11, times[11], "BOTTOM", 9.0),
            Fractal(16, times[16], "TOP", 13.0),
        ]
        strokes = build_strokes(fractals)
        self.assertEqual(len(strokes), 3)
        self.assertEqual(strokes[0].start.position, 1)
        self.assertEqual(strokes[0].end.position, 6)

    def test_three_overlapping_strokes_form_center(self) -> None:
        times = pd.date_range("2026-01-01", periods=20, freq="30min")
        points = [
            Fractal(0, times[0], "BOTTOM", 8.0),
            Fractal(5, times[5], "TOP", 12.0),
            Fractal(10, times[10], "BOTTOM", 9.0),
            Fractal(15, times[15], "TOP", 11.0),
        ]
        strokes = [
            Stroke(points[0], points[1], 8.0, 12.0),
            Stroke(points[1], points[2], 9.0, 12.0),
            Stroke(points[2], points[3], 9.0, 11.0),
        ]
        centers = find_centers(strokes)
        self.assertEqual(len(centers), 1)
        self.assertEqual(centers[0].lower, 9.0)
        self.assertEqual(centers[0].upper, 11.0)

    def test_center_breakout_and_breakdown_are_close_confirmed(self) -> None:
        frame = frame_from_rows([(10, 8, 9.5), (11, 9, 10.0), (12, 10, 11.2)])
        center = Center(0, 1, 9.0, 10.5, frame.index[1])
        self.assertEqual(detect_center_cross(frame, center), (True, False))
        falling = frame.copy()
        falling.loc[falling.index[-2], "Close"] = 9.5
        falling.loc[falling.index[-1], "Close"] = 8.5
        self.assertEqual(detect_center_cross(falling, center), (False, True))

    def test_bearish_divergence_uses_only_confirmed_latest_top(self) -> None:
        frame = frame_from_rows([(10 + i * 0.1, 8 + i * 0.1, 9 + i * 0.1) for i in range(10)])
        tops = [
            Fractal(3, frame.index[3], "TOP", 11.0),
            Fractal(8, frame.index[8], "TOP", 12.0),
        ]
        fake_macd = pd.DataFrame(
            {"histogram": [0.0, 0.0, 0.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]},
            index=frame.index,
        )
        with patch("strategy_v1.chan.macd", return_value=fake_macd):
            self.assertTrue(detect_bearish_divergence(frame, tops))


if __name__ == "__main__":
    unittest.main()
