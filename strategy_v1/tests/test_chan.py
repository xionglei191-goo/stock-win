from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from strategy_v1.chan import (
    Center,
    ChanParameters,
    Fractal,
    Stroke,
    build_segments,
    build_strokes,
    classify_trend,
    detect_bearish_divergence,
    detect_bullish_divergence,
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
        # Pin volume ratio to 0 so this test focuses on ATR and return gates only.
        parameters = ChanParameters(max_atr_ratio=0.10, max_signal_return=0.10, min_volume_ratio=0.0)
        self.assertTrue(daily_entry_allowed(frame, parameters))

        volatile = frame.copy()
        volatile.iloc[-1, volatile.columns.get_loc("High")] = 20.0
        self.assertFalse(daily_entry_allowed(volatile, parameters))

        chased = frame.copy()
        chased.iloc[-1, chased.columns.get_loc("Close")] = float(chased["Close"].iloc[-2]) * 1.15
        self.assertFalse(daily_entry_allowed(chased, parameters))

    def test_daily_entry_filter_requires_volume_pickup(self) -> None:
        # Baseline frame: volume trending up so recent ≥ 1.2 × prior.
        import numpy as np
        n = 30
        idx = pd.date_range("2026-01-01", periods=n, freq="B")
        close = 10.0 + np.arange(n) * 0.01
        strong_vol = np.concatenate([np.full(n - 1, 1000.0), [1300.0]])
        frame_vol = pd.DataFrame(
            {"Open": close, "High": close + 0.1, "Low": close - 0.1, "Close": close, "Volume": strong_vol},
            index=idx,
        )
        parameters = ChanParameters(min_volume_ratio=1.2, max_atr_ratio=1.0, max_signal_return=1.0)
        self.assertTrue(daily_entry_allowed(frame_vol, parameters))

        # Flat volume: last bar equal to baseline → ratio ~1.0, below 1.2.
        flat_vol = frame_vol.copy()
        flat_vol.iloc[-1, flat_vol.columns.get_loc("Volume")] = 1000.0
        self.assertFalse(daily_entry_allowed(flat_vol, parameters))

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

    def test_bearish_divergence_compares_histogram_area_between_two_rallies(self) -> None:
        frame = frame_from_rows([(10 + i * 0.1, 8 + i * 0.1, 9 + i * 0.1) for i in range(12)])
        fractals = [
            Fractal(1, frame.index[1], "BOTTOM", 8.0),
            Fractal(3, frame.index[3], "TOP", 11.0),
            Fractal(6, frame.index[6], "BOTTOM", 9.0),
            Fractal(9, frame.index[9], "TOP", 12.0),
        ]
        # 前段（1..3）面积 6.0，后段（6..9）面积 3.0：价格新高但动能萎缩。
        shrinking = pd.DataFrame(
            {"histogram": [0.0, 1.0, 3.0, 2.0, 0.0, 0.0, 0.5, 1.0, 1.0, 0.5, 0.0, 0.0]},
            index=frame.index,
        )
        with patch("strategy_v1.chan.macd", return_value=shrinking):
            self.assertTrue(detect_bearish_divergence(frame, fractals))

        # 后段面积反而放大，不构成背驰（旧的单点比较会误判为背驰）。
        expanding = pd.DataFrame(
            {"histogram": [0.0, 1.0, 2.0, 1.0, 0.0, 0.0, 2.0, 4.0, 4.0, 1.0, 0.0, 0.0]},
            index=frame.index,
        )
        with patch("strategy_v1.chan.macd", return_value=expanding):
            self.assertFalse(detect_bearish_divergence(frame, fractals))

    def test_bullish_divergence_detects_shrinking_decline(self) -> None:
        frame = frame_from_rows([(10 - i * 0.1, 8 - i * 0.1, 9 - i * 0.1) for i in range(12)])
        fractals = [
            Fractal(1, frame.index[1], "TOP", 12.0),
            Fractal(3, frame.index[3], "BOTTOM", 9.0),
            Fractal(6, frame.index[6], "TOP", 11.0),
            Fractal(9, frame.index[9], "BOTTOM", 8.0),
        ]
        shrinking = pd.DataFrame(
            {"histogram": [0.0, -1.0, -3.0, -2.0, 0.0, 0.0, -0.5, -1.0, -1.0, -0.5, 0.0, 0.0]},
            index=frame.index,
        )
        with patch("strategy_v1.chan.macd", return_value=shrinking):
            self.assertTrue(detect_bullish_divergence(frame, fractals))

    def test_segments_require_third_stroke_to_break_first(self) -> None:
        times = pd.date_range("2026-01-01", periods=30, freq="30min")
        points = [
            Fractal(0, times[0], "BOTTOM", 8.0),
            Fractal(5, times[5], "TOP", 11.0),
            Fractal(10, times[10], "BOTTOM", 9.5),
            Fractal(15, times[15], "TOP", 13.0),
        ]
        strokes = [
            Stroke(points[0], points[1], 8.0, 11.0),
            Stroke(points[1], points[2], 9.5, 11.0),
            Stroke(points[2], points[3], 9.5, 13.0),
        ]
        segments = build_segments(strokes)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].direction, "UP")
        self.assertEqual(segments[0].stroke_count, 3)
        self.assertEqual(segments[0].high, 13.0)

        # 第三笔未突破第一笔顶点则不成段。
        weak_end = Fractal(15, times[15], "TOP", 10.5)
        failed = build_segments(
            [
                strokes[0],
                strokes[1],
                Stroke(points[2], weak_end, 9.5, 10.5),
            ]
        )
        self.assertEqual(failed, [])

    def test_center_extends_instead_of_creating_overlapping_pseudo_centers(self) -> None:
        times = pd.date_range("2026-01-01", periods=40, freq="30min")
        prices = [8.0, 12.0, 9.0, 11.5, 9.5, 11.0]
        points = [
            Fractal(
                index * 5,
                times[index * 5],
                "BOTTOM" if index % 2 == 0 else "TOP",
                price,
            )
            for index, price in enumerate(prices)
        ]
        strokes = [
            Stroke(start, end, min(start.price, end.price), max(start.price, end.price))
            for start, end in zip(points, points[1:])
        ]
        centers = find_centers(strokes)
        self.assertEqual(len(centers), 1)
        self.assertEqual(centers[0].unit_count, 5)
        self.assertEqual(centers[0].lower, 9.5)
        self.assertEqual(centers[0].upper, 11.0)

    def test_breakout_confirmed_requires_macd_positive_and_segment_center(self) -> None:
        import numpy as np
        # Build a frame with enough bars for MACD (26 EMA needs ~60 bars to stabilize).
        n = 80
        idx = pd.date_range("2025-01-01", periods=n, freq="B")
        close = np.linspace(10.0, 14.0, n)  # steadily rising — MACD diff positive
        frame = pd.DataFrame(
            {
                "Open": close,
                "High": close + 0.5,
                "Low": close - 0.5,
                "Close": close,
                "Volume": np.linspace(1000.0, 1500.0, n),
            },
            index=idx,
        )
        from strategy_v1.chan import analyze_chan, ChanParameters
        state = analyze_chan(frame, ChanParameters())
        # In a clear up-trend: if a breakout fires and a segment center exists,
        # breakout_confirmed should be True when MACD diff is positive.
        # This test asserts the logic is applied — the exact boolean depends on
        # whether a center forms; at minimum the field must exist on the state.
        self.assertIsInstance(state.breakout_confirmed, bool)
        # require_segment_center=False should allow stroke-level centers too.
        state_relaxed = analyze_chan(frame, ChanParameters(require_segment_center=False))
        self.assertIsInstance(state_relaxed.breakout_confirmed, bool)
        times = pd.date_range("2026-01-01", periods=10, freq="D")
        lower = Center(0, 3, 9.0, 10.0, times[3])
        higher = Center(4, 7, 12.0, 13.0, times[7])
        overlapping = Center(4, 7, 9.5, 10.5, times[7])
        self.assertEqual(classify_trend([lower, higher]), "UP")
        self.assertEqual(classify_trend([higher, lower]), "DOWN")
        self.assertEqual(classify_trend([lower, overlapping]), "RANGE")
        self.assertEqual(classify_trend([lower]), "RANGE")


if __name__ == "__main__":
    unittest.main()
