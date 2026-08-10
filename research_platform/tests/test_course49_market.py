from __future__ import annotations

import unittest

import pandas as pd

from research_platform.course49_market import normalize_market_activity
from research_platform.strategies.course49 import Course49Strategy
from research_platform.tests.test_course49 import make_bars


class Course49MarketActivityTests(unittest.TestCase):
    def test_normalizes_market_ecology_fields(self) -> None:
        frame = normalize_market_activity(
            {
                "SC15": [{"Date": "20260807", "Value": [80, 20]}],
                "SC24": [{"Date": "20260807", "Value": [42, 3]}],
                "SC30": [{"Date": "20260807", "Value": [5, 12]}],
                "SC31": [{"Date": "20260807", "Value": [3100, 1900]}],
                "SC35": [{"Date": "20260807", "Value": [4, 65]}],
            }
        )

        self.assertEqual(frame.loc[pd.Timestamp("2026-08-07"), "limit_up"], 42)
        self.assertEqual(frame.loc[pd.Timestamp("2026-08-07"), "max_streak"], 5)
        self.assertAlmostEqual(frame.loc[pd.Timestamp("2026-08-07"), "reseal_rate"], 0.65)
        self.assertAlmostEqual(
            frame.loc[pd.Timestamp("2026-08-07"), "seal_fund_success_ratio"], 0.80
        )

    def test_future_market_activity_does_not_change_prior_score(self) -> None:
        names = {f"00000{i}.SZ": f"样本{i}" for i in range(1, 7)}
        raw = {code: make_bars(index, 2 if index <= 4 else 0) for index, code in enumerate(names, 1)}
        asof = max(frame.index[-1] for frame in raw.values())
        dates = pd.date_range(asof - pd.offsets.BDay(29), periods=30, freq="B")
        activity = pd.DataFrame(
            {
                "advance_count": range(2400, 2430),
                "decline_count": range(1800, 1830),
                "limit_up": range(25, 55),
                "limit_down": [5] * 30,
                "max_streak": [2 + index // 10 for index in range(30)],
                "reseal_rate": [0.60] * 30,
                "seal_fund_success_ratio": [0.70] * 30,
            },
            index=dates,
        )
        strategy = Course49Strategy()
        first = strategy.analyze_market(raw, names, activity)
        activity.loc[asof + pd.offsets.BDay(1)] = [1, 5000, 0, 100, 0, 0.0, 0.0]
        second = strategy.analyze_market(raw, names, activity)

        self.assertEqual(first.data_source, "tdx_market_activity")
        self.assertAlmostEqual(first.score, second.score)


if __name__ == "__main__":
    unittest.main()
