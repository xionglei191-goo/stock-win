from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research_platform.early_winner_research import ResearchDataBlockedError
from research_platform.early_winner_v3_research import (
    EarlyWinnerV3ResearchService,
    _point_in_time_ttm_profit,
)
from research_platform.storage import Database
from research_platform.strategies.early_winner import mark_research_universe_eligibility
from research_platform.tests.helpers import temporary_config


class EarlyWinnerV3Tests(unittest.TestCase):
    def test_ttm_profit_uses_only_announcements_available_at_decision(self) -> None:
        events = [
            (pd.Timestamp("2018-03-15"), pd.Timestamp("2017-12-31"), 100.0, 0),
            (pd.Timestamp("2018-04-20"), pd.Timestamp("2018-03-31"), 30.0, 1),
            (pd.Timestamp("2018-08-16"), pd.Timestamp("2018-06-30"), 70.0, 2),
            (pd.Timestamp("2019-03-15"), pd.Timestamp("2018-12-31"), 140.0, 3),
        ]

        values = _point_in_time_ttm_profit(
            events,
            pd.Series(
                ["2018-03-14", "2018-03-16", "2018-04-19", "2018-08-17", "2019-03-16"]
            ),
        )

        self.assertTrue(np.isnan(values[0]))
        self.assertEqual(values[1], 100.0)
        self.assertEqual(values[2], 100.0)
        self.assertTrue(np.isnan(values[3]))  # 2017H1 was not supplied; fail closed.
        self.assertEqual(values[4], 140.0)

    def test_ttm_profit_builds_latest_quarter_from_prior_annual_and_same_period(self) -> None:
        events = [
            (pd.Timestamp("2018-03-15"), pd.Timestamp("2017-12-31"), 100.0, 0),
            (pd.Timestamp("2018-04-20"), pd.Timestamp("2017-03-31"), 20.0, 1),
            (pd.Timestamp("2018-04-21"), pd.Timestamp("2018-03-31"), 30.0, 2),
        ]

        values = _point_in_time_ttm_profit(events, pd.Series(["2018-04-21"]))

        self.assertEqual(values[0], 110.0)

    def test_missing_supplemental_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV3ResearchService(config, database)

            with self.assertRaises(ResearchDataBlockedError):
                service._v3_batches()

    def test_strategy_remains_research_only_and_emits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV3ResearchService(config, database)

            result = service.strategy.scan(asof="2026-08-12")

        self.assertEqual(result.signals, ())
        self.assertEqual(result.candidates, ())
        self.assertFalse(result.state["trade_signals_enabled"])
        self.assertFalse(result.state["frozen_validation_opened"])

    def test_restored_turnover_activates_declared_extreme_heat_entry_gate(self) -> None:
        rows = []
        for index in range(100):
            rows.append(
                {
                    "code": f"600{index:03d}.SH",
                    "listed_days": 300,
                    "valid_days_20": 20,
                    "adv20": 200_000_000.0,
                    "suspended": False,
                    "is_st": False,
                    "is_quit": False,
                    "relative_return_60": float(index),
                    "return_60": float(index),
                    "turnover_20": float(index),
                    "price_to_ma60": 2.0 if index == 99 else 1.1,
                }
            )

        marked = mark_research_universe_eligibility(rows)
        hottest = next(item for item in marked if item["code"] == "600099.SH")

        self.assertTrue(hottest["universe_gate"])
        self.assertTrue(hottest["extreme_heat"])
        self.assertFalse(hottest["eligible"])

    def test_matching_audit_snapshot_identity_uses_feature_batch_hashes(self) -> None:
        from research_platform.early_winner_v3_research import _hash_payload

        records = [
            {"batch_id": "a", "content_hash": "one"},
            {"batch_id": "b", "content_hash": "two"},
        ]
        source_hash = _hash_payload(
            [(item["batch_id"], item["content_hash"]) for item in records]
        )

        self.assertEqual(source_hash, _hash_payload([("a", "one"), ("b", "two")]))


if __name__ == "__main__":
    unittest.main()
