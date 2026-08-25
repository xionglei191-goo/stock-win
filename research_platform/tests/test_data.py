from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.data import ResearchDataHub
from research_platform.data_plan import required_bar_lookback
from research_platform.models import DataStatus
from research_platform.strategies.weekly_triangle import WeeklyTriangleStrategy
from research_platform.tests.helpers import temporary_config


class DataHealthTests(unittest.TestCase):
    def test_strategy_bar_requirement_controls_scan_lookback(self) -> None:
        self.assertEqual(
            required_bar_lookback([WeeklyTriangleStrategy.metadata], minimum=120),
            180,
        )

    def test_intraday_before_expected_day_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = ResearchDataHub(temporary_config(Path(directory)))
            bars = {"600000.SH": pd.DataFrame({"Close": [10.0]}, index=[pd.Timestamp("2026-01-05 15:00")])}
            health = hub.assess_intraday(bars, pd.Timestamp("2026-01-06"))
        self.assertEqual(health.status, DataStatus.STALE)

    def test_partial_daily_coverage_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = ResearchDataHub(temporary_config(Path(directory)))
            index = pd.DataFrame({"Close": [1.0]}, index=[pd.Timestamp("2026-01-06")])
            bars = {
                "A": pd.DataFrame({"Close": [1.0]}, index=[pd.Timestamp("2026-01-06")]),
                "B": pd.DataFrame({"Close": [1.0]}, index=[pd.Timestamp("2026-01-05")]),
            }
            health = hub.assess_daily(bars, index)
        self.assertEqual(health.status, DataStatus.PARTIAL)

    def test_missing_requested_symbols_are_counted_in_daily_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = ResearchDataHub(temporary_config(Path(directory)))
            index = pd.DataFrame({"Close": [1.0]}, index=[pd.Timestamp("2026-01-06")])
            bars = {
                f"{index:06d}.SZ": pd.DataFrame(
                    {"Close": [1.0]},
                    index=[pd.Timestamp("2026-01-06")],
                )
                for index in range(90)
            }

            health = hub.assess_daily(
                bars,
                index,
                expected_symbol_count=100,
            )

        self.assertEqual(health.status, DataStatus.READY)
        incomplete = hub.assess_daily(
            dict(list(bars.items())[:89]),
            index,
            expected_symbol_count=100,
        )
        self.assertEqual(incomplete.status, DataStatus.PARTIAL)


if __name__ == "__main__":
    unittest.main()
