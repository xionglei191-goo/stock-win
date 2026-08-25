from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi.testclient import TestClient

from research_platform.api import create_app
from research_platform.backtest_engine import BacktestService
from research_platform.__main__ import (
    _paper_corporate_action_records,
    _paper_non_session_result,
    build_parser,
    main,
)
from research_platform.service import PlatformService
from research_platform.tests.helpers import temporary_config


class USRuntimeIntegrationTests(unittest.TestCase):
    def test_tick_passes_current_admitted_release_actions_to_runtime(self) -> None:
        fixed_now = datetime(2026, 8, 13, 9, 20, tzinfo=ZoneInfo("America/New_York"))
        release_id = "a" * 64
        action = {
            "action_id": "split-aapl-20260813",
            "security_id": "USISIN-AAPL",
            "action_type": "SPLIT",
            "announced_at": "2026-08-01T12:00:00Z",
            "effective_at": "2026-08-13T00:00:00-04:00",
            "pay_date": None,
            "terms_verified": True,
            "source_id": "exchange_notice",
            "evidence_sha256": "e" * 64,
            "split_ratio": 2.0,
        }

        class FakeDataset:
            calendar = pd.DataFrame(
                {"session_date": ["2026-08-13", "2026-08-31"]}
            )

            @staticmethod
            def actions_on(session: object) -> pd.DataFrame:
                self.assertEqual(fixed_now.date(), session)
                return pd.DataFrame([action])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_path = root / "release"
            release_path.mkdir()
            manifest_path = release_path / "manifest.json"
            manifest_path.write_text(json.dumps({"release_id": release_id}), encoding="utf-8")
            manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            config = SimpleNamespace(
                repository_root=root,
                runtime_dir=root / "runtime",
                us_paper_database_path=root / "paper.sqlite3",
                us_program_database_path=root / "program.sqlite3",
                us_paper_runtime_database_path=root / "runtime.sqlite3",
                us_pit_dir=root / "pit",
                us_paper_decision_archive_dir=root / "decision-archive",
            )
            release = SimpleNamespace(
                path=release_path,
                to_backtest_dataset=Mock(return_value=FakeDataset()),
            )
            pit = Mock()
            pit.store.load_release.return_value = release
            program = Mock()
            program.status.return_value = {
                "state": "PAPER_COLLECTING",
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
                "paper_decision_release_id": release_id,
                "paper_decision_manifest_sha256": manifest_sha256,
            }
            paper = Mock()
            runtime = Mock()
            runtime.config.market_close = time(16, 0)
            runtime.schedule.contains.return_value = True
            runtime.current_decision_binding.return_value = {
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
            }
            runtime.tick.return_value = {"runtime": {"status": "RUNNING"}}

            with (
                patch.object(sys, "argv", ["research_platform", "us-paper", "tick"]),
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.__main__.USMomentumPaperService",
                    return_value=paper,
                ),
                patch("research_platform.__main__.USMomentumProgram", return_value=program),
                patch("research_platform.__main__.USPITService", return_value=pit),
                patch("research_platform.__main__.datetime") as mocked_datetime,
                patch(
                    "research_platform.us_paper_runtime.USPaperRuntime.open_existing",
                    return_value=runtime,
                ),
                patch("research_platform.__main__._print"),
            ):
                mocked_datetime.now.return_value = fixed_now
                self.assertEqual(0, main())

        kwargs = runtime.tick.call_args.kwargs
        self.assertEqual(fixed_now, kwargs["now"])
        self.assertEqual([], kwargs["daily_bars"])
        self.assertEqual("split-aapl-20260813", kwargs["corporate_actions"][0]["action_id"])

    def test_close_tick_builds_replayable_tdx_raw_and_biltr_provenance(self) -> None:
        fixed_now = datetime(2026, 8, 13, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        release_id = "a" * 64

        class FakeDataset:
            calendar = pd.DataFrame(
                {"session_date": ["2026-08-13", "2026-08-31"]}
            )

            @staticmethod
            def actions_on(session: object) -> pd.DataFrame:
                return pd.DataFrame()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_path = root / "release"
            release_path.mkdir()
            manifest_path = release_path / "manifest.json"
            manifest_path.write_text(json.dumps({"release_id": release_id}), encoding="utf-8")
            manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            config = SimpleNamespace(
                repository_root=root,
                runtime_dir=root / "runtime",
                us_paper_database_path=root / "paper.sqlite3",
                us_program_database_path=root / "program.sqlite3",
                us_paper_runtime_database_path=root / "runtime.sqlite3",
                us_pit_dir=root / "pit",
                us_paper_decision_archive_dir=root / "decision-archive",
            )
            release = SimpleNamespace(
                path=release_path,
                to_backtest_dataset=Mock(return_value=FakeDataset()),
            )
            pit = Mock()
            pit.store.load_release.return_value = release
            program = Mock()
            program.status.return_value = {
                "state": "PAPER_COLLECTING",
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
                "paper_decision_release_id": release_id,
                "paper_decision_manifest_sha256": manifest_sha256,
            }
            paper = Mock()
            paper.status.return_value = {"positions": []}
            runtime = Mock()
            runtime.config.market_close = time(16, 0)
            runtime.schedule.contains.return_value = True
            runtime.current_decision_binding.return_value = {
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
            }
            runtime.tick.return_value = {"runtime": {"status": "RUNNING"}}
            raw_frame = pd.DataFrame(
                {"Open": [91.0], "High": [91.1], "Low": [90.9], "Close": [91.05]},
                index=pd.to_datetime(["2026-08-13"]),
            )
            front_frame = pd.DataFrame(
                {"Close": [90.99, 91.07]},
                index=pd.to_datetime(["2026-08-12", "2026-08-13"]),
            )
            provider = MagicMock()
            provider.__enter__.return_value = provider
            provider.fetch_bars.side_effect = [
                {"BIL.US": raw_frame},
                {"BIL.US": front_frame},
            ]

            with (
                patch.object(sys, "argv", ["research_platform", "us-paper", "tick"]),
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.__main__.USMomentumPaperService",
                    return_value=paper,
                ),
                patch("research_platform.__main__.USMomentumProgram", return_value=program),
                patch("research_platform.__main__.USPITService", return_value=pit),
                patch("research_platform.__main__.datetime") as mocked_datetime,
                patch(
                    "research_platform.us_paper_runtime.USPaperRuntime.open_existing",
                    return_value=runtime,
                ),
                patch("research_platform.data.TdxProvider", return_value=provider),
                patch("research_platform.__main__._print"),
            ):
                mocked_datetime.now.return_value = fixed_now
                self.assertEqual(0, main())

        bars = {item["code"]: item for item in runtime.tick.call_args.kwargs["daily_bars"]}
        self.assertEqual("TDX", bars["BIL.US"]["source"])
        self.assertEqual("us-paper-tdx-daily-v1", bars["BIL.US"]["source_schema"])
        self.assertEqual("BIL.US", bars["BIL.US"]["source_code"])
        self.assertEqual("1d", bars["BIL.US"]["frequency"])
        self.assertEqual("none", bars["BIL.US"]["adjustment"])
        self.assertEqual(
            [
                {
                    "session_date": "2026-08-13",
                    "Open": 91.0,
                    "High": 91.1,
                    "Low": 90.9,
                    "Close": 91.05,
                }
            ],
            bars["BIL.US"]["source_rows"],
        )
        self.assertEqual("front", bars["BILTR.US"]["adjustment"])
        self.assertEqual(
            [
                {"session_date": "2026-08-12", "Close": 90.99},
                {"session_date": "2026-08-13", "Close": 91.07},
            ],
            bars["BILTR.US"]["source_rows"],
        )
        for bar in bars.values():
            canonical = {
                "source_schema": bar["source_schema"],
                "source": bar["source"],
                "source_code": bar["source_code"],
                "frequency": bar["frequency"],
                "adjustment": bar["adjustment"],
                "source_rows": bar["source_rows"],
            }
            self.assertEqual(
                hashlib.sha256(
                    json.dumps(
                        canonical,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                bar["source_sha256"],
            )

    def test_non_session_worker_is_clean_but_schedule_exhaustion_fails_closed(self) -> None:
        runtime = Mock()
        runtime.schedule.sessions = (date(2026, 8, 12), date(2026, 8, 14))
        runtime.schedule.contains.return_value = False
        runtime.status.return_value = {"runtime": {"status": "RUNNING"}}

        holiday = _paper_non_session_result(runtime, date(2026, 8, 13))
        exhausted = _paper_non_session_result(runtime, date(2026, 8, 15))

        self.assertIsNotNone(holiday)
        self.assertEqual(0, holiday[0])
        self.assertEqual("MARKET_CLOSED", holiday[1]["status"])
        self.assertIsNotNone(exhausted)
        self.assertEqual(2, exhausted[0])
        self.assertEqual("FROZEN_XNYS_SCHEDULE_EXHAUSTED", exhausted[1]["reason"])

    def test_corporate_action_converter_uses_dataset_session_filter(self) -> None:
        frame = pd.DataFrame([{"action_id": "a1", "terms_verified": True}])
        dataset = Mock()
        dataset.actions_on.return_value = frame

        result = _paper_corporate_action_records(dataset, "2026-08-13")

        dataset.actions_on.assert_called_once_with("2026-08-13")
        self.assertEqual([{"action_id": "a1", "terms_verified": True}], result)

    def test_manual_resume_command_is_fail_closed_and_does_not_mutate_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                repository_root=root,
                runtime_dir=root / "runtime",
                us_paper_database_path=root / "paper.sqlite3",
                us_program_database_path=root / "program.sqlite3",
            )
            paper = Mock()
            paper.status.return_value = {
                "account": {"status": "DATA_DEGRADED"},
                "paper_only": True,
            }
            with (
                patch.object(
                    sys,
                    "argv",
                    ["research_platform", "us-paper", "resume", "--note", "looks fine"],
                ),
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.__main__.USMomentumPaperService",
                    return_value=paper,
                ),
                patch("research_platform.__main__.USMomentumProgram"),
                patch("research_platform.__main__._print") as output,
            ):
                self.assertEqual(2, main())

        paper.acknowledge_data_recovery.assert_not_called()
        payload = output.call_args.args[0]
        self.assertEqual(
            "MANUAL_RESUME_DISABLED_USE_EVIDENCE_DRIVEN_TICK", payload["reason"]
        )

    def test_rolling_release_requires_explicit_cli_admission(self) -> None:
        parsed = build_parser().parse_args(
            ["us-paper", "admit-release", "--release", "a" * 64]
        )
        self.assertEqual("us-paper", parsed.command)
        self.assertEqual("admit-release", parsed.us_paper_command)
        self.assertEqual("a" * 64, parsed.release)

    def test_read_only_catalog_and_paper_status_start_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            releases = client.get("/api/data/us-pit/releases")
            paper = client.get("/api/us-paper/status")

        self.assertEqual(releases.status_code, 200)
        self.assertEqual(releases.json(), [])
        self.assertEqual(paper.status_code, 200)
        self.assertTrue(paper.json()["paper_only"])
        self.assertFalse(paper.json()["broker_writes_enabled"])
        self.assertEqual(paper.json()["qualification"], "DATA_BLOCKED")
        self.assertEqual(paper.json()["program"]["state"], "DATA_BLOCKED")

    def test_strict_platform_route_rejects_missing_or_unknown_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            platform = PlatformService(config)
            backtests = BacktestService(config, platform.database)

            with self.assertRaisesRegex(ValueError, "pit_release_id"):
                backtests.run(
                    "us_momentum_v1",
                    universe="sp500_ivv_proxy_v1",
                )
            with self.assertRaisesRegex(ValueError, "not found"):
                backtests.run(
                    "us_momentum_v1",
                    universe="sp500_ivv_proxy_v1",
                    pit_release_id="a" * 64,
                )
            with self.assertRaisesRegex(ValueError, "prohibit custom"):
                backtests.run(
                    "us_momentum_v1",
                    universe="sp500_ivv_proxy_v1",
                    pit_release_id="a" * 64,
                    stock_codes=["AAPL.US"],
                )

    def test_api_accepts_strict_request_but_job_fails_closed_without_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            response = client.post(
                "/api/backtests",
                json={
                    "strategy_id": "us_momentum_v1",
                    "universe": "sp500_ivv_proxy_v1",
                    "pit_release_id": "a" * 64,
                    "sampling_mode": "full",
                },
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["job_id"]
            for _ in range(200):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] not in {"QUEUED", "RUNNING"}:
                    break
            self.assertEqual(job["status"], "FAILED")
            self.assertIn("PIT release not found", job["error"])


if __name__ == "__main__":
    unittest.main()
