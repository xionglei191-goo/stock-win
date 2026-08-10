from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from research_platform.feedback import FeedbackService
from research_platform.models import PlatformSignal, SignalStatus
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config


def make_signal(code: str, generated_at: datetime) -> PlatformSignal:
    return PlatformSignal(
        run_id="feedback-run",
        strategy_id="course49_v3",
        strategy_version="3.0.0",
        generated_at=generated_at,
        available_at=generated_at,
        code=code,
        side="BUY",
        strength=0.8,
        target_weight=0.2,
        horizon="daily-short",
        valid_until=generated_at + timedelta(days=2),
        stop_price=9.0,
        status=SignalStatus.PROPOSED,
        reason_codes=("TEST",),
        evidence={"market_phase": "BULL"},
    )


class FeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = temporary_config(Path(self.temp.name))
        self.database = Database(self.config)
        self.database.initialize()
        self.database.create_run("feedback-run", "scan", "research", ["course49_v3"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rejected_signal_never_skips_untradable_next_open(self) -> None:
        generated = datetime.now().astimezone()
        signal = make_signal("600000.SH", generated)
        self.database.save_signals([signal])
        self.database.decide_signal(
            signal.signal_id,
            SignalStatus.REJECTED,
            reason_tags=("风险过高",),
            confidence=80,
        )
        today = pd.Timestamp.now().normalize()
        index = pd.DatetimeIndex([
            today - pd.offsets.BDay(1),
            today + pd.offsets.BDay(1),
            today + pd.offsets.BDay(2),
            today + pd.offsets.BDay(3),
        ])
        bars = {
            signal.code: pd.DataFrame(
                {
                    "Open": [10.0, 11.0, 10.2, 10.3],
                    "Close": [10.0, 11.0, 10.3, 10.4],
                    "Low": [9.8, 11.0, 10.1, 10.2],
                    "High": [10.1, 11.0, 10.4, 10.5],
                    "Volume": [1000] * 4,
                },
                index=index,
            )
        }

        result = FeedbackService(self.config, self.database).refresh(
            bars=bars, names={signal.code: "浦发银行"}
        )
        outcome = self.database.query("SELECT * FROM decision_outcomes")[0]

        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(outcome["status"], "UNFILLED")
        self.assertEqual(outcome["block_reason"], "NEXT_OPEN_NOT_TRADABLE")
        self.assertIsNone(outcome["entry_time"])

    def test_feedback_marks_small_samples_insufficient(self) -> None:
        generated = datetime.now().astimezone()
        signal = make_signal("000001.SZ", generated)
        self.database.save_signals([signal])
        self.database.decide_signal(signal.signal_id, SignalStatus.REJECTED)
        today = pd.Timestamp.now().normalize()
        dates = pd.bdate_range(today - pd.offsets.BDay(1), periods=7)
        bars = {
            signal.code: pd.DataFrame(
                {
                    "Open": [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6],
                    "Close": [10.0, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7],
                    "Low": [9.9, 10.0, 10.1, 10.2, 10.3, 10.4, 10.5],
                    "High": [10.1, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8],
                    "Volume": [1000] * 7,
                },
                index=dates,
            )
        }
        service = FeedbackService(self.config, self.database)
        service.refresh(bars=bars, names={signal.code: "平安银行"})

        summary = service.summary()

        self.assertFalse(summary["aggregates"][0]["sufficient_sample"])
        self.assertEqual(summary["aggregates"][0]["reason_tag"], "UNSPECIFIED")
        self.assertIsNotNone(summary["rows"][0]["return_5d"])


if __name__ == "__main__":
    unittest.main()
