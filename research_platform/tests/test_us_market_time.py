from __future__ import annotations

import unittest

import pandas as pd

from research_platform.us_market_time import ny_session_date, ny_session_dates


class USMarketTimeTests(unittest.TestCase):
    def test_timezone_aware_instant_uses_new_york_market_date(self) -> None:
        self.assertEqual(
            pd.Timestamp("2023-01-03"),
            ny_session_date("2023-01-03T23:30:00-05:00"),
        )
        self.assertEqual(
            pd.Timestamp("2023-01-03"),
            ny_session_date("2023-01-04T04:30:00Z"),
        )

    def test_date_label_is_not_timezone_shifted(self) -> None:
        self.assertEqual(pd.Timestamp("2023-01-03"), ny_session_date("2023-01-03"))

    def test_naive_intraday_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            ny_session_date("2023-01-03T23:30:00")

    def test_series_conversion_preserves_missing_values(self) -> None:
        result = ny_session_dates(
            pd.Series(["2023-01-04T04:30:00Z", None, "2023-01-04"])
        )
        self.assertEqual(pd.Timestamp("2023-01-03"), result.iloc[0])
        self.assertTrue(pd.isna(result.iloc[1]))
        self.assertEqual(pd.Timestamp("2023-01-04"), result.iloc[2])


if __name__ == "__main__":
    unittest.main()
