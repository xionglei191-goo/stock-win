from __future__ import annotations

import unittest

import pandas as pd
import pyarrow as pa

from research_platform.backtest_engine import _lhb_snapshot_schema
from research_platform.lhb import (
    flatten_lhb_history,
    inflate_lhb_history,
    latest_lhb_features,
    latest_limit_features,
    normalize_lhb_history,
)


class LhbFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        dates = pd.to_datetime(["2026-07-20", "2026-07-21", "2026-07-22"])
        self.bars = {
            "000037.SZ": pd.DataFrame(
                {
                    "Close": [10.0, 10.8, 11.2],
                    "Amount": [100_000_000.0, 120_000_000.0, 130_000_000.0],
                    "Volume": [20_000_000.0, 22_000_000.0, 24_000_000.0],
                },
                index=dates,
            )
        }

    def test_normalizes_amounts_and_capital_structure(self) -> None:
        history = normalize_lhb_history(
            {
                "000037.SZ": {
                    "GP02": [{"Date": "20260720", "Value": [9000, 3000]}],
                    "GP08": [{"Date": "20260720", "Value": [1, 500]}],
                    "GP09": [{"Date": "20260720", "Value": [3, 2500]}],
                    "GP17": [{"Date": "20260720", "Value": [4500, 1800]}],
                    "GP18": [{"Date": "20260720", "Value": [800, 100]}],
                    "GP37": [{"Date": "20260720", "Value": [2, 0]}],
                }
            },
            self.bars,
        )
        feature = history["000037.SZ"]["2026-07-20"]
        self.assertEqual(feature.total_net, 60_000_000.0)
        self.assertAlmostEqual(feature.net_buy_ratio or 0.0, 0.60)
        self.assertEqual(feature.institution_net, 20_000_000.0)
        self.assertIn("LHB_NET_BUY", feature.confirmations)
        self.assertIn("INSTITUTION_BUY", feature.confirmations)
        self.assertIn("NORTHBOUND_BUY", feature.confirmations)
        self.assertIn("REPEATED_LIST", feature.confirmations)
        self.assertEqual(feature.risk, "")

    def test_point_in_time_lookup_never_uses_future_event(self) -> None:
        history = normalize_lhb_history(
            {
                "000037.SZ": {
                    "GP02": [
                        {"Date": "20260720", "Value": [3000, 1000]},
                        {"Date": "20260722", "Value": [1000, 6000]},
                    ]
                }
            },
            self.bars,
        )
        visible = latest_lhb_features(history, "000037.SZ", "2026-07-21")
        self.assertIsNotNone(visible)
        self.assertEqual(visible.event_date, "2026-07-20")
        self.assertGreater(visible.total_net, 0)
        future = latest_lhb_features(history, "000037.SZ", "2026-07-19")
        self.assertIsNone(future)

    def test_missing_listing_is_distinct_from_outflow(self) -> None:
        history = normalize_lhb_history({}, self.bars)
        self.assertIsNone(latest_lhb_features(history, "000037.SZ", "2026-07-22"))

    def test_limit_behavior_uses_historical_seal_and_auction_fields(self) -> None:
        history = normalize_lhb_history(
            {
                "000037.SZ": {
                    "GP14": [{"Date": "20260720", "Value": [5000, 0]}],
                    "GP22": [{"Date": "20260720", "Value": [12, 1.5]}],
                    "GP24": [{"Date": "20260720", "Value": [94000, 8000]}],
                    "GP25": [{"Date": "20260720", "Value": [50000, 0]}],
                    "GP36": [{"Date": "20260720", "Value": [3000, 0]}],
                    "GP38": [{"Date": "20260720", "Value": [8, 5]}],
                    "GP39": [{"Date": "20260720", "Value": [70, 68]}],
                    "GP40": [{"Date": "20260720", "Value": [35, 145000]}],
                }
            },
            self.bars,
        )

        feature = latest_limit_features(history, "000037.SZ", "2026-07-20")

        self.assertIsNotNone(feature)
        assert feature is not None
        self.assertFalse(feature.listed)
        self.assertEqual(feature.first_limit_time, "094000")
        self.assertEqual(feature.open_board_count, 0)
        self.assertEqual(feature.limit_amount, 50_000_000.0)
        self.assertAlmostEqual(feature.max_seal_turnover_ratio or 0.0, 0.80)
        self.assertAlmostEqual(feature.auction_volume_ratio or 0.0, 0.25)
        self.assertAlmostEqual(feature.auction_limit_buy_ratio or 0.0, 0.30)
        self.assertGreaterEqual(feature.board_quality_score, 0.70)
        self.assertIn("EARLY_SEAL", feature.board_confirmations)
        self.assertIn("STRONG_SEAL", feature.board_confirmations)
        self.assertIn("AUCTION_STRENGTH", feature.board_confirmations)
        self.assertIsNone(latest_limit_features(history, "000037.SZ", "2026-07-21"))

    def test_zero_daily_fields_are_not_events(self) -> None:
        history = normalize_lhb_history(
            {
                "000037.SZ": {
                    "GP14": [{"Date": "20260720", "Value": [0, 0]}],
                    "GP24": [{"Date": "20260720", "Value": [0, 0]}],
                    "GP37": [{"Date": "20260720", "Value": [0, 0]}],
                }
            },
            self.bars,
        )
        self.assertNotIn("000037.SZ", history)
        self.assertIsNone(latest_limit_features(history, "000037.SZ", "2026-07-20"))
        self.assertIsNone(latest_lhb_features(history, "000037.SZ", "2026-07-20"))

    def test_flatten_filters_do_not_duplicate_unrelated_events(self) -> None:
        history = normalize_lhb_history(
            {
                "000037.SZ": {
                    "GP02": [{"Date": "20260720", "Value": [3000, 1000]}],
                    "GP14": [{"Date": "20260721", "Value": [5000, 0]}],
                    "GP24": [{"Date": "20260721", "Value": [94000, 8000]}],
                }
            },
            self.bars,
        )

        listed = flatten_lhb_history(history, listed_only=True)
        limits = flatten_lhb_history(history, limit_only=True)

        self.assertEqual([row["event_date"] for row in listed], ["2026-07-20"])
        self.assertEqual([row["event_date"] for row in limits], ["2026-07-21"])

        restored = inflate_lhb_history(flatten_lhb_history(history))
        self.assertEqual(
            restored["000037.SZ"]["2026-07-20"],
            history["000037.SZ"]["2026-07-20"],
        )
        self.assertEqual(
            restored["000037.SZ"]["2026-07-21"],
            history["000037.SZ"]["2026-07-21"],
        )
        table = pa.Table.from_pylist(
            flatten_lhb_history(history), schema=_lhb_snapshot_schema()
        )
        self.assertEqual(table.num_rows, 2)


if __name__ == "__main__":
    unittest.main()
