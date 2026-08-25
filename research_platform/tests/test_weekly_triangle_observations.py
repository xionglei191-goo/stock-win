from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config
from research_platform.weekly_triangle_observations import (
    WeeklyTriangleObservationService,
)


def candidate(
    code: str = "000001.SZ",
    *,
    signal_asof: str = "2026-01-02",
    stage: str = "BREAKOUT",
    price_location: float = 0.75,
) -> dict[str, object]:
    return {
        "code": code,
        "name": "Test Security",
        "asof": signal_asof,
        "stage": stage,
        "breakout": stage == "BREAKOUT",
        "score": 0.82,
        "entry_allowed": True,
        "observation_only": True,
        "close": 10.0,
        "upper_boundary": 11.0,
        "lower_boundary": 9.0,
        "price_location": price_location,
    }


def bars(periods: int = 26) -> pd.DataFrame:
    index = pd.bdate_range("2026-01-05", periods=periods)
    values = [10.0 + 0.05 * offset for offset in range(periods)]
    return pd.DataFrame(
        {
            "Open": values,
            "High": [value + 0.2 for value in values],
            "Low": [value - 0.2 for value in values],
            "Close": [value + 0.05 for value in values],
        },
        index=index,
    )


class WeeklyTriangleObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = temporary_config(Path(self.temp.name))
        self.database = Database(self.config)
        self.database.initialize()
        self.database.create_run("scan-1", "scan", "research", ["weekly_triangle_v1"])
        self.service = WeeklyTriangleObservationService(self.config, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_capture_is_idempotent_and_entry_waits_for_first_seen_scan(self) -> None:
        inserted = self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-09",
            candidates=[candidate()],
            target_weight=0.20,
        )
        duplicate = self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-12",
            candidates=[candidate()],
            target_weight=0.20,
        )

        result = self.service.refresh(bars={"000001.SZ": bars()})
        row = self.database.query("SELECT * FROM strategy_observations")[0]

        self.assertEqual(inserted, 1)
        self.assertEqual(duplicate, 0)
        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(row["status"], "COMPLETE")
        self.assertEqual(pd.Timestamp(row["entry_time"]).date().isoformat(), "2026-01-12")
        self.assertGreater(pd.Timestamp(row["entry_time"]), pd.Timestamp(row["observed_at"]))
        self.assertIsNotNone(row["return_5d"])
        self.assertIsNotNone(row["return_20d"])

    def test_partial_observation_matures_without_changing_entry(self) -> None:
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-09",
            candidates=[candidate("000002.SZ")],
            target_weight=0.20,
        )

        self.service.refresh(bars={"000002.SZ": bars(10)})
        partial = self.database.query("SELECT * FROM strategy_observations")[0]
        partial_entry = partial["entry_time"]
        self.service.refresh(bars={"000002.SZ": bars(26)})
        complete = self.database.query("SELECT * FROM strategy_observations")[0]

        self.assertEqual(partial["status"], "PARTIAL")
        self.assertIsNotNone(partial["return_5d"])
        self.assertIsNone(partial["return_20d"])
        self.assertEqual(complete["status"], "COMPLETE")
        self.assertEqual(complete["entry_time"], partial_entry)
        self.assertIsNotNone(complete["mae_20d"])
        self.assertIsNotNone(complete["mfe_20d"])

    def test_limit_up_next_open_is_unfilled_and_never_skipped(self) -> None:
        code = "600000.SH"
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-09",
            candidates=[candidate(code, signal_asof="2026-01-09")],
            target_weight=0.20,
        )
        index = pd.bdate_range("2026-01-09", periods=3)
        frame = pd.DataFrame(
            {
                "Open": [10.0, 11.0, 10.5],
                "High": [10.1, 11.0, 10.8],
                "Low": [9.9, 11.0, 10.4],
                "Close": [10.0, 11.0, 10.7],
            },
            index=index,
        )

        self.service.refresh(bars={code: frame})
        row = self.database.query("SELECT * FROM strategy_observations")[0]

        self.assertEqual(row["status"], "UNFILLED")
        self.assertEqual(row["block_reason"], "NEXT_OPEN_NOT_TRADABLE")
        self.assertIsNone(row["entry_time"])

    def test_summary_keeps_live_entry_disabled_while_sample_is_small(self) -> None:
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-09",
            candidates=[candidate(stage="SETUP")],
            target_weight=0.20,
        )

        summary = self.service.summary()

        self.assertEqual(summary["policy_status"], "HISTORICAL_REJECTED")
        self.assertEqual(summary["counts"]["pending"], 1)
        self.assertEqual(summary["forward_gate"]["status"], "COLLECTING")
        self.assertFalse(summary["forward_gate"]["automatic_live_entry"])

    def test_capture_freezes_only_twenty_policy_breakouts_per_cohort(self) -> None:
        candidates = [
            candidate(f"{offset:06d}.SZ") | {"score": 1.0 - offset / 100.0}
            for offset in range(25)
        ]

        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-09",
            candidates=candidates,
            target_weight=0.20,
            maximum_entries=20,
        )
        rows = self.database.query(
            "SELECT candidate_json FROM strategy_observations ORDER BY score DESC"
        )
        payloads = [json.loads(str(row["candidate_json"])) for row in rows]

        self.assertEqual(sum(bool(item["policy_selected"]) for item in payloads), 20)
        self.assertEqual(payloads[0]["policy_rank"], 1)
        self.assertEqual(payloads[19]["policy_rank"], 20)
        self.assertFalse(payloads[20]["policy_selected"])

    def test_setup_hypothesis_ranks_episode_starts_by_price_location(self) -> None:
        candidates = [
            candidate(
                f"{offset:06d}.SZ",
                stage="SETUP",
                price_location=0.50 + offset / 100.0,
            )
            | {"score": 1.0 - offset / 100.0}
            for offset in range(25)
        ]

        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-02",
            candidates=candidates,
            target_weight=0.20,
        )
        rows = self.database.query(
            """SELECT code, hypothesis_rank, hypothesis_selected, candidate_json
            FROM strategy_observations ORDER BY hypothesis_rank"""
        )

        self.assertEqual(rows[0]["code"], "000024.SZ")
        self.assertEqual(rows[0]["hypothesis_rank"], 1)
        self.assertEqual(sum(bool(row["hypothesis_selected"]) for row in rows), 20)
        payload = json.loads(str(rows[0]["candidate_json"]))
        self.assertTrue(payload["hypothesis_episode_start"])
        self.assertEqual(payload["hypothesis_id"], "price_location_high_v1")

    def test_setup_episode_requires_gap_from_previous_setup_over_fourteen_days(self) -> None:
        for signal_asof in ("2026-01-02", "2026-01-09", "2026-01-30"):
            self.service.capture(
                run_id="scan-1",
                strategy_version="1.2.0",
                observed_at=signal_asof,
                candidates=[candidate(signal_asof=signal_asof, stage="SETUP")],
                target_weight=0.20,
            )

        rows = self.database.query(
            """SELECT signal_asof, status, conversion_status, candidate_json
            FROM strategy_observations WHERE stage='SETUP' ORDER BY signal_asof"""
        )
        starts = [
            json.loads(str(row["candidate_json"]))["hypothesis_episode_start"]
            for row in rows
        ]

        self.assertEqual(starts, [True, False, True])
        self.assertEqual(rows[1]["status"], "EXCLUDED")
        self.assertEqual(rows[1]["conversion_status"], "NOT_APPLICABLE")

    def test_setup_converts_only_on_strictly_later_breakout(self) -> None:
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-02",
            candidates=[candidate(signal_asof="2026-01-02", stage="SETUP")],
            target_weight=0.20,
        )
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-02",
            candidates=[candidate(signal_asof="2026-01-02", stage="BREAKOUT")],
            target_weight=0.20,
        )
        same_day = self.database.query(
            "SELECT * FROM strategy_observations WHERE stage='SETUP'"
        )[0]

        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-30",
            candidates=[candidate(signal_asof="2026-01-30", stage="BREAKOUT")],
            target_weight=0.20,
        )
        converted = self.database.query(
            "SELECT * FROM strategy_observations WHERE stage='SETUP'"
        )[0]

        self.assertEqual(same_day["conversion_status"], "PENDING")
        self.assertEqual(converted["conversion_status"], "CONVERTED")
        self.assertEqual(converted["converted_at"], "2026-01-30")
        self.assertEqual(converted["conversion_days"], 28)
        self.assertEqual(converted["status"], "COMPLETE")
        self.assertIsNone(converted["entry_time"])
        self.assertIsNone(converted["return_20d"])

    def test_setup_expires_on_day_thirty_five_without_breakout(self) -> None:
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-02",
            candidates=[candidate(signal_asof="2026-01-02", stage="SETUP")],
            target_weight=0.20,
        )

        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-02-06",
            candidates=[],
            target_weight=0.20,
        )
        row = self.database.query(
            "SELECT * FROM strategy_observations WHERE stage='SETUP'"
        )[0]

        self.assertEqual(row["status"], "COMPLETE")
        self.assertEqual(row["conversion_status"], "NOT_CONVERTED")
        self.assertEqual(row["conversion_days"], 35)
        self.assertIsNone(row["entry_price"])
        self.assertIsNone(row["return_5d"])

    def test_setup_refresh_never_creates_a_hypothetical_trade(self) -> None:
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-02",
            candidates=[candidate(signal_asof="2026-01-02", stage="SETUP")],
            target_weight=0.20,
        )

        self.service.refresh(bars={"000001.SZ": bars(26)})
        row = self.database.query(
            "SELECT * FROM strategy_observations WHERE stage='SETUP'"
        )[0]

        self.assertEqual(row["status"], "PARTIAL")
        self.assertEqual(row["conversion_status"], "PENDING")
        self.assertEqual(row["executable"], 0)
        self.assertIsNone(row["entry_time"])
        self.assertIsNone(row["entry_price"])
        self.assertIsNone(row["return_5d"])
        self.assertIsNone(row["return_20d"])

    def test_duplicate_capture_backfills_legacy_setup_without_trade_fields(self) -> None:
        setup = candidate(signal_asof="2026-01-02", stage="SETUP")
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-02",
            candidates=[setup],
            target_weight=0.20,
        )
        legacy_payload = dict(setup)
        legacy_payload.pop("price_location")
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE strategy_observations SET status='COMPLETE', executable=1,
                entry_time='2026-01-05', entry_price=10.1, return_5d=0.05,
                return_20d=0.10, candidate_json=?, hypothesis_id='',
                hypothesis_rank=NULL, hypothesis_selected=0,
                conversion_status='NOT_APPLICABLE'""",
                (json.dumps(legacy_payload),),
            )

        duplicate = self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-09",
            candidates=[setup],
            target_weight=0.20,
        )
        row = self.database.query("SELECT * FROM strategy_observations")[0]
        payload = json.loads(str(row["candidate_json"]))

        self.assertEqual(duplicate, 0)
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["conversion_status"], "PENDING")
        self.assertEqual(row["hypothesis_rank"], 1)
        self.assertTrue(row["hypothesis_selected"])
        self.assertAlmostEqual(payload["price_location"], 0.5)
        self.assertIsNone(row["entry_time"])
        self.assertIsNone(row["return_20d"])

    def test_summary_compares_selected_conversion_with_score_baseline(self) -> None:
        setups = [
            candidate(
                f"{offset:06d}.SZ",
                stage="SETUP",
                price_location=0.50 + offset / 100.0,
            )
            | {"score": 1.0 - offset / 100.0}
            for offset in range(21)
        ]
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-02",
            candidates=setups,
            target_weight=0.20,
        )
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-01-16",
            candidates=[
                candidate(
                    "000020.SZ",
                    signal_asof="2026-01-16",
                    stage="BREAKOUT",
                )
            ],
            target_weight=0.20,
        )
        self.service.capture(
            run_id="scan-1",
            strategy_version="1.2.0",
            observed_at="2026-02-06",
            candidates=[],
            target_weight=0.20,
        )

        hypothesis = self.service.summary()["setup_hypothesis"]

        self.assertEqual(hypothesis["selected"]["resolved_samples"], 20)
        self.assertEqual(hypothesis["score_baseline"]["resolved_samples"], 20)
        self.assertAlmostEqual(hypothesis["selected"]["conversion_rate"], 0.05)
        self.assertEqual(hypothesis["score_baseline"]["conversion_rate"], 0.0)
        self.assertAlmostEqual(hypothesis["conversion_rate_lift"], 0.05)
        self.assertEqual(hypothesis["status"], "COLLECTING")
        self.assertFalse(hypothesis["automatic_live_entry"])


if __name__ == "__main__":
    unittest.main()
