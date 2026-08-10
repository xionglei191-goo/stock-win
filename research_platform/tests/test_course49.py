from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from research_platform.lhb import normalize_lhb_history
from research_platform.models import SignalStatus
from research_platform.strategies.course49 import (
    Course49Market,
    Course49Strategy,
    infer_market_phase,
    infer_theme_phase,
)


def make_bars(code_index: int, limit_streak: int = 0) -> pd.DataFrame:
    dates = pd.date_range("2025-10-01", periods=75, freq="B")
    close = [10.0 + code_index * 0.2]
    for offset in range(1, len(dates)):
        change = 0.002 if offset % 2 else -0.001
        close.append(close[-1] * (1 + change))
    if limit_streak:
        start = len(close) - limit_streak
        for offset in range(start, len(close)):
            close[offset] = close[offset - 1] * 1.10
    series = pd.Series(close, index=dates)
    volume = pd.Series([1_000_000.0] * len(dates), index=dates)
    return pd.DataFrame(
        {
            "Open": series,
            "High": series * 1.01,
            "Low": series * 0.99,
            "Close": series,
            "Volume": volume,
            "Amount": series * volume,
        }
    )


class Course49Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = Course49Strategy()
        self.names = {f"00000{i}.SZ": f"样本{i}" for i in range(1, 7)}
        self.raw = {
            code: make_bars(index, 2 if index <= 4 else 0)
            for index, code in enumerate(self.names, start=1)
        }
        self.front = {code: frame.copy() for code, frame in self.raw.items()}
        self.sectors = {"S1": {"name": "主流题材", "members": list(self.names)}}

    def test_climax_market_keeps_leaders_but_does_not_chase(self) -> None:
        result = self.strategy.scan(
            run_id="run",
            front_bars=self.front,
            raw_bars=self.raw,
            names=self.names,
            sector_members=self.sectors,
            positions=[],
        )
        self.assertEqual(result.state["market_regime"], "STRONG")
        self.assertEqual(result.state["market_phase"], "CLIMAX")
        self.assertTrue(result.candidates)
        self.assertFalse(result.signals)

    def test_ferment_market_and_capital_confirmation_produce_signal(self) -> None:
        event_date = self.raw["000003.SZ"].index[-1].strftime("%Y%m%d")
        lhb = normalize_lhb_history(
            {
                "000003.SZ": {
                    "GP02": [{"Date": event_date, "Value": [1000, 200]}],
                    "GP09": [{"Date": event_date, "Value": [2, 500]}],
                    "GP37": [{"Date": event_date, "Value": [2, 0]}],
                }
            },
            self.raw,
        )
        market = Course49Market(
            asof=self.raw["000003.SZ"].index[-1],
            score=0.70,
            regime="STRONG",
            phase="FERMENT",
            score_change_3d=0.02,
            entry_allowed=True,
            advance_percentile=0.70,
            limit_strength_percentile=0.80,
            premium_percentile=0.70,
            streak_percentile=0.80,
        )
        with patch.object(self.strategy, "analyze_market", return_value=market):
            result = self.strategy.scan(
                run_id="run",
                front_bars=self.front,
                raw_bars=self.raw,
                names=self.names,
                sector_members=self.sectors,
                positions=[],
                lhb_history=lhb,
            )
        self.assertTrue(result.signals)
        signal = result.signals[0]
        self.assertEqual(signal.status, SignalStatus.PROPOSED)
        self.assertEqual(signal.evidence["setup"], "SECOND_BOARD_CAPITAL_CONFIRMED")
        self.assertIn("LHB_NET_BUY", signal.reason_codes)
        self.assertTrue(signal.evidence["lhb"]["listed"])

    def test_phase_classification_separates_entry_and_exit_states(self) -> None:
        self.assertEqual(infer_market_phase(0.20, -0.05), "ICE")
        self.assertEqual(infer_market_phase(0.58, 0.12), "RECOVERY")
        self.assertEqual(infer_market_phase(0.70, 0.04), "ACCELERATION")
        self.assertEqual(infer_market_phase(0.85, 0.01), "CLIMAX")
        self.assertEqual(infer_market_phase(0.60, -0.15), "DIVERGENCE")
        self.assertEqual(infer_theme_phase(4, 1, 4, 0.50, 1.0), "START")
        self.assertEqual(infer_theme_phase(6, 4, 6, 0.70, 1.2), "ACCELERATION")
        self.assertEqual(infer_theme_phase(4, 6, 6, 0.60, 1.0), "DIVERGENCE")

    def test_board_quality_confirms_entry_and_late_weak_seal_blocks_it(self) -> None:
        event_date = next(iter(self.raw.values())).index[-1].strftime("%Y%m%d")
        quality_raw = {}
        for code in list(self.names)[:4]:
            quality_raw[code] = {
                "GP14": [{"Date": event_date, "Value": [5000, 0]}],
                "GP22": [{"Date": event_date, "Value": [12, 1.5]}],
                "GP24": [{"Date": event_date, "Value": [94000, 5000]}],
                "GP36": [{"Date": event_date, "Value": [1000, 0]}],
                "GP39": [{"Date": event_date, "Value": [70, 70]}],
            }
        market = Course49Market(
            asof=next(iter(self.raw.values())).index[-1],
            score=0.70,
            regime="STRONG",
            phase="FERMENT",
            score_change_3d=0.02,
            entry_allowed=True,
            advance_percentile=0.70,
            limit_strength_percentile=0.80,
            premium_percentile=0.70,
            streak_percentile=0.80,
        )
        with patch.object(self.strategy, "analyze_market", return_value=market):
            confirmed = self.strategy.scan(
                run_id="run",
                front_bars=self.front,
                raw_bars=self.raw,
                names=self.names,
                sector_members=self.sectors,
                positions=[],
                lhb_history=normalize_lhb_history(quality_raw, self.raw),
            )
        self.assertTrue(confirmed.signals)
        self.assertEqual(
            confirmed.signals[0].evidence["setup"], "SECOND_BOARD_QUALITY_CONFIRMED"
        )
        self.assertIn("EARLY_SEAL", confirmed.signals[0].reason_codes)

        weak_raw = {
            code: {
                "GP14": [{"Date": event_date, "Value": [10, 0]}],
                "GP24": [{"Date": event_date, "Value": [145500, 10]}],
            }
            for code in list(self.names)[:4]
        }
        with patch.object(self.strategy, "analyze_market", return_value=market):
            blocked = self.strategy.scan(
                run_id="run",
                front_bars=self.front,
                raw_bars=self.raw,
                names=self.names,
                sector_members=self.sectors,
                positions=[],
                lhb_history=normalize_lhb_history(weak_raw, self.raw),
            )
        self.assertFalse(blocked.signals)
        self.assertTrue(
            all(item["board_risk"] == "LATE_WEAK_SEAL" for item in blocked.candidates)
        )

    def test_future_bar_does_not_change_prior_market_score(self) -> None:
        first = self.strategy.analyze_market(self.raw, self.names)
        changed = {code: frame.copy() for code, frame in self.raw.items()}
        for code, frame in changed.items():
            future = frame.iloc[-1].copy()
            future["Close"] = float(future["Close"]) * (2 if code.endswith("1.SZ") else 0.5)
            frame.loc[frame.index[-1] + pd.offsets.BDay(1)] = future
        sliced = {code: frame.iloc[:-1] for code, frame in changed.items()}
        second = self.strategy.analyze_market(sliced, self.names)
        self.assertAlmostEqual(first.score, second.score)


if __name__ == "__main__":
    unittest.main()
