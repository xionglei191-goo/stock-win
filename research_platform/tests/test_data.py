from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.data import ResearchDataHub
from research_platform.models import DataStatus
from research_platform.tests.helpers import temporary_config


class DataHealthTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
