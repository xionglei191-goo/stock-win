from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from research_platform.ai_research import (
    AIResearchService,
    DailyBriefOutput,
    EvidenceClaim,
    SignalReviewOutput,
)
from research_platform.models import PlatformSignal, RunStatus, SignalStatus
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config


class FakeResponses:
    def __init__(self, outputs: list[DailyBriefOutput]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        output = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        return SimpleNamespace(
            id=f"resp_{len(self.calls)}",
            model="test-model",
            output_parsed=output,
            usage={"input_tokens": 10, "output_tokens": 20},
        )


def make_signal(run_id: str, generated_at: datetime) -> PlatformSignal:
    return PlatformSignal(
        run_id=run_id,
        strategy_id="course49_v3",
        strategy_version="3.0.0",
        generated_at=generated_at,
        available_at=generated_at,
        code="600000.SH",
        side="BUY",
        strength=0.8,
        target_weight=0.2,
        horizon="daily-short",
        valid_until=generated_at + timedelta(days=2),
        stop_price=9.2,
        status=SignalStatus.PROPOSED,
        reason_codes=("TEST",),
        evidence={"market_phase": "BULL", "price": 10.0},
    )


class AIResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = temporary_config(Path(self.temp.name))
        self.database = Database(self.config)
        self.database.initialize()
        self.database.create_run("run-ai", "scan", "research", ["course49_v3"])
        self.signal = make_signal("run-ai", datetime.now().astimezone())
        self.database.save_signals([self.signal])
        self.database.update_run("run-ai", RunStatus.SUCCEEDED, metadata={"signal_count": 1})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def output(self, refs: list[str]) -> DailyBriefOutput:
        return DailyBriefOutput(
            headline="测试简报",
            market_summary=EvidenceClaim(text="扫描完成", evidence_refs=["run:run-ai"], confidence=1),
            signal_reviews=[
                SignalReviewOutput(
                    signal_id=self.signal.signal_id,
                    recommendation="SUPPORT",
                    confidence=0.7,
                    summary="证据一致",
                    evidence_refs=refs,
                )
            ],
        )

    def test_structured_result_is_persisted_without_storage(self) -> None:
        responses = FakeResponses([self.output([f"signal:{self.signal.signal_id}"])])
        result = AIResearchService(
            self.config, self.database, client=SimpleNamespace(responses=responses)
        ).generate_brief("run-ai")

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["reviews"][0]["recommendation"], "SUPPORT")
        self.assertFalse(responses.calls[0]["store"])

    def test_invalid_evidence_retries_once_then_fails_whole_brief(self) -> None:
        responses = FakeResponses([
            self.output(["signal:not-real"]),
            self.output(["signal:not-real"]),
        ])
        result = AIResearchService(
            self.config, self.database, client=SimpleNamespace(responses=responses)
        ).generate_brief("run-ai")

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["reviews"], [])
        self.assertEqual(len(responses.calls), 2)
        self.assertIn("must cite its signal", result["error"])

    def test_validation_retry_can_recover(self) -> None:
        responses = FakeResponses([
            self.output(["signal:not-real"]),
            self.output([f"signal:{self.signal.signal_id}"]),
        ])
        result = AIResearchService(
            self.config, self.database, client=SimpleNamespace(responses=responses)
        ).generate_brief("run-ai")

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(responses.calls), 2)


if __name__ == "__main__":
    unittest.main()
