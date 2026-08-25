from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from research_platform import early_winner_v6_research as early_winner_v6_module
from research_platform.api import create_app
from research_platform.early_winner_v4_research import EarlyWinnerV4ResearchService
from research_platform.service import PlatformService
from research_platform.tests.helpers import temporary_config


class ApiTests(unittest.TestCase):
    def test_core_read_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            sources = client.get("/api/sources")
            strategies = client.get("/api/strategies")
            dashboard = client.get("/api/dashboard")
        self.assertEqual(sources.status_code, 200)
        self.assertEqual(strategies.status_code, 200)
        self.assertEqual(
            {item["strategy_id"] for item in strategies.json()},
            {
                "chan_v1", "course49_v1", "course49_v2", "course49_v3", "course49_v4", "course49_v5", "course49_v6",
                "course49_v7", "course49_v8", "course49_v9", "course49_v10", "course49_v11",
                "course49_system",
                "pairs_arbitrage_v1",
                "weekly_triangle_v1",
                "weekly_bull_platform_v1",
                "early_winner_rule_v1",
                "early_winner_ml_v1",
                "early_winner_trade_v1",
                "early_winner_ml_v2",
                "early_winner_ml_v3",
                "early_winner_ml_v4",
                "early_winner_event_quiet_v5",
                "early_winner_event_quiet_v6",
                "us_momentum_v1",
                "qqq_vol_dca_v1",
                "qqq_treasury_rotation_v1",
            },
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(len(dashboard.json()["accounts"]), 16)

    def test_early_winner_history_and_trading_endpoints_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            history = client.get("/api/research/early-winner/history/status")
            refresh = client.post("/api/research/early-winner/refresh")
            train = client.post("/api/research/early-winner/train")
            validate = client.post("/api/research/early-winner/validate")
            history_build = client.post(
                "/api/research/early-winner/history/build",
                json={"start_year": 2018, "end_year": 2025},
            )
            trading = client.get("/api/trading/early-winner")
            activation = client.post("/api/trading/early-winner/activate-shadow")
            batches = client.get("/api/trading/order-batches")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["status"], "BLOCKED_DATA")
        self.assertEqual(history.json()["artifact_status"], "NOT_STARTED")
        self.assertEqual(
            history.json()["trust_policy"]["status"],
            "SUPERSEDED_DATA_QUALITY_REJECTED",
        )
        self.assertEqual(refresh.status_code, 410)
        self.assertEqual(train.status_code, 410)
        self.assertEqual(validate.status_code, 410)
        self.assertEqual(history_build.status_code, 410)
        self.assertEqual(trading.status_code, 404)
        self.assertEqual(activation.status_code, 404)
        self.assertEqual(batches.status_code, 404)

    def test_early_winner_v2_starts_sealed_and_has_no_trade_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            detail = client.get("/api/research/early-winner-v2")
            submitted = client.post(
                "/api/research/early-winner-v2/development-audit"
            )
            job = client.get(f"/api/jobs/{submitted.json()['job_id']}").json()
            for _ in range(100):
                if job["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
                job = client.get(f"/api/jobs/{submitted.json()['job_id']}").json()

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "DEVELOPMENT_AUDIT_REQUIRED")
        self.assertFalse(detail.json()["forward_validation_opened"])
        self.assertFalse(detail.json()["candidate_generation_enabled"])
        self.assertFalse(detail.json()["trade_signals_enabled"])
        self.assertEqual(submitted.status_code, 202)
        self.assertEqual(job["status"], "SUCCEEDED")
        self.assertEqual(job["result"]["status"], "BLOCKED_DATA")

    def test_early_winner_v3_starts_sealed_and_has_no_trade_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            detail = client.get("/api/research/early-winner-v3")
            submitted = client.post(
                "/api/research/early-winner-v3/development-audit"
            )
            job = client.get(f"/api/jobs/{submitted.json()['job_id']}").json()
            for _ in range(100):
                if job["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
                job = client.get(f"/api/jobs/{submitted.json()['job_id']}").json()

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "DATA_BUILDING")
        self.assertFalse(detail.json()["frozen_validation_opened"])
        self.assertFalse(detail.json()["candidate_generation_enabled"])
        self.assertFalse(detail.json()["trade_signals_enabled"])
        self.assertEqual(submitted.status_code, 202)
        self.assertEqual(job["status"], "SUCCEEDED")
        self.assertEqual(job["result"]["status"], "BLOCKED_DATA")

    def test_early_winner_v4_starts_sealed_and_has_no_trade_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            initial_detail = client.get("/api/research/early-winner-v4")
            submitted = client.post(
                "/api/research/early-winner-v4/development-audit"
            )
            job = client.get(f"/api/jobs/{submitted.json()['job_id']}").json()
            for _ in range(100):
                if job["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
                job = client.get(f"/api/jobs/{submitted.json()['job_id']}").json()
            blocked_detail = client.get("/api/research/early-winner-v4")

        self.assertEqual(initial_detail.status_code, 200)
        self.assertEqual(initial_detail.json()["status"], "BLOCKED_DATA")
        self.assertEqual(
            initial_detail.json()["data_gates"]["historical_universe_master"]["status"],
            "NOT_BUILT",
        )
        self.assertEqual(initial_detail.json()["protocol"]["holding_trading_days"], 40)
        self.assertFalse(initial_detail.json()["frozen_validation_opened"])
        self.assertFalse(initial_detail.json()["candidate_generation_enabled"])
        self.assertFalse(initial_detail.json()["trade_signals_enabled"])
        self.assertEqual(submitted.status_code, 202)
        self.assertEqual(job["status"], "FAILED")
        self.assertIsNone(job["result"])
        self.assertTrue(job["error"])
        self.assertEqual(blocked_detail.json()["status"], "BLOCKED_DATA")

    def test_early_winner_v4_unexpected_job_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                EarlyWinnerV4ResearchService,
                "build_label_snapshot",
                side_effect=RuntimeError("unexpected-v4-test-error"),
            ):
                client = TestClient(create_app(temporary_config(Path(directory))))
                submitted = client.post(
                    "/api/research/early-winner-v4/build-labels"
                )
                job = client.get(f"/api/jobs/{submitted.json()['job_id']}").json()
                for _ in range(100):
                    if job["status"] not in {"QUEUED", "RUNNING"}:
                        break
                    time.sleep(0.01)
                    job = client.get(f"/api/jobs/{submitted.json()['job_id']}").json()
                blocked_detail = client.get("/api/research/early-winner-v4")

        self.assertEqual(submitted.status_code, 202)
        self.assertEqual(job["status"], "FAILED")
        self.assertIsNone(job["result"])
        self.assertEqual(job["error"], "unexpected-v4-test-error")
        self.assertEqual(blocked_detail.json()["status"], "BLOCKED_DATA")

    def test_early_winner_v5_is_read_only_preregistered_and_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            detail = client.get("/api/research/early-winner-v5")
            unavailable_actions = [
                client.post("/api/research/early-winner-v5/train"),
                client.post("/api/research/early-winner-v5/validate"),
                client.post("/api/research/early-winner-v5/trade"),
            ]

        self.assertEqual(detail.status_code, 200)
        payload = detail.json()
        self.assertEqual(payload["status"], "BLOCKED_DATA")
        self.assertEqual(payload["lifecycle"], "RESEARCH_ONLY")
        self.assertTrue(payload["data_gates"]["preregistration"]["ready"])
        self.assertFalse(payload["frozen_validation_opened"])
        self.assertFalse(payload["candidate_generation_enabled"])
        self.assertFalse(payload["trade_signals_enabled"])
        self.assertFalse(payload["promotion_allowed"])
        self.assertTrue(all(response.status_code == 404 for response in unavailable_actions))

    def test_early_winner_v6_is_read_only_and_never_opens_frozen_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(early_winner_v6_module, "seal_v6_frozen_validation") as seal,
                patch.object(early_winner_v6_module, "run_v6_frozen_validation_once") as run,
                patch.object(early_winner_v6_module, "assess_v6_frozen_result") as assess,
            ):
                client = TestClient(create_app(temporary_config(Path(directory))))
                detail = client.get("/api/research/early-winner-v6")
                repeated = client.get("/api/research/early-winner-v6")
                unavailable_actions = [
                    client.post(f"/api/research/early-winner-v6/{action}")
                    for action in (
                        "seal",
                        "open",
                        "consume",
                        "run",
                        "assess",
                        "validate",
                        "train",
                        "trade",
                    )
                ]
                schema = client.get("/openapi.json").json()

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        payload = detail.json()
        self.assertEqual(repeated.json()["frozen_open_state"], "NOT_SEALED")
        self.assertEqual(payload["project_id"], "early_winner_v6")
        self.assertEqual(payload["status"], "BLOCKED_DATA")
        self.assertEqual(payload["lifecycle"], "RESEARCH_ONLY")
        self.assertEqual(payload["frozen_open_state"], "NOT_SEALED")
        self.assertFalse(payload["frozen_validation_opened"])
        self.assertFalse(payload["candidate_generation_enabled"])
        self.assertFalse(payload["trade_signals_enabled"])
        self.assertFalse(payload["promotion_allowed"])
        self.assertEqual(
            payload["v5_disposition"]["status"], "PREREGISTRATION_REJECTED"
        )
        dependency_lock = payload["protocol"]["dependency_lock"]
        for value in (
            payload["protocol_hash"],
            dependency_lock["evaluator_bundle_hash"],
            dependency_lock["label_schema_hash"],
            dependency_lock["dependency_lock_hash"],
        ):
            self.assertEqual(len(value), 64)
        self.assertTrue(all(response.status_code == 404 for response in unavailable_actions))
        self.assertEqual(set(schema["paths"]["/api/research/early-winner-v6"]), {"get"})
        self.assertFalse(
            any(
                path.startswith("/api/research/early-winner-v6/")
                for path in schema["paths"]
            )
        )
        seal.assert_not_called()
        run.assert_not_called()
        assess.assert_not_called()

    def test_strategy_catalog_and_custom_group_crud(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            catalog = client.get("/api/strategy-catalog")
            reloaded = client.post("/api/strategy-catalog/reload")
            created = client.post(
                "/api/strategy-groups",
                json={
                    "group_id": "test_sleeves",
                    "version": "1.0.0",
                    "name": "测试分舱",
                    "composition_mode": "capital_sleeves",
                    "members": [
                        {"strategy_id": "chan_v1", "weight": 0.4},
                        {"strategy_id": "pairs_arbitrage_v1", "weight": 0.6},
                    ],
                },
            )
            deleted = client.delete("/api/strategy-groups/test_sleeves")
        self.assertEqual(catalog.status_code, 200)
        self.assertIn("adaptive_multi_strategy", {item["group_id"] for item in catalog.json()["groups"]})
        self.assertEqual(
            {item["strategy_id"] for item in catalog.json()["strategies"]},
            {
                "chan_v1",
                "course49_system",
                "pairs_arbitrage_v1",
                "weekly_triangle_v1",
                "weekly_bull_platform_v1",
                "early_winner_rule_v1",
                "early_winner_ml_v1",
                "early_winner_trade_v1",
                "early_winner_ml_v2",
                "early_winner_ml_v3",
                "early_winner_ml_v4",
                "early_winner_event_quiet_v5",
                "early_winner_event_quiet_v6",
                "us_momentum_v1",
                "qqq_vol_dca_v1",
                "qqq_treasury_rotation_v1",
            },
        )
        self.assertEqual(len(catalog.json()["archived_strategies"]), 11)
        self.assertEqual(catalog.json()["frameworks"][0]["framework_id"], "course49")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.json()["plugin_issues"], [])
        self.assertIn(
            "early_winner_event_quiet_v6",
            {item["strategy_id"] for item in reloaded.json()["strategies"]},
        )
        self.assertEqual(
            next(
                item
                for item in reloaded.json()["strategies"]
                if item["strategy_id"] == "pairs_arbitrage_v1"
            )["runtime_adapter"],
            "generic_daily",
        )
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.json()["backtest_supported"])
        self.assertEqual(deleted.status_code, 204)

    def test_frontend_routes_fall_back_to_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            config.frontend_dist.mkdir(parents=True)
            (config.frontend_dist / "index.html").write_text("<h1>research-app</h1>", encoding="utf-8")
            client = TestClient(create_app(config))

            response = client.get("/backtests")
            missing_api = client.get("/api/not-real")

        self.assertEqual(response.status_code, 200)
        self.assertIn("research-app", response.text)
        self.assertEqual(missing_api.status_code, 404)

    def test_research_read_endpoints_and_openapi_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(temporary_config(Path(directory))))
            briefs = client.get("/api/research/briefs")
            feedback = client.get("/api/research/feedback/summary")
            observations = client.get("/api/research/weekly-triangle/observations")
            setup_stability = client.get("/api/research/weekly-triangle/setup-stability")
            pairs_validation = client.get("/api/research/pairs-arbitrage/validation")
            chan_validation = client.get("/api/research/chan/validation")
            experiments = client.get("/api/research/experiments")
            early_winner = client.get("/api/research/early-winner")
            early_candidates = client.get(
                "/api/research/early-winner/candidates?method=rule"
            )
            schema = client.get("/openapi.json").json()

        self.assertEqual(briefs.json(), [])
        self.assertEqual(feedback.json(), {"rows": [], "aggregates": []})
        self.assertEqual(observations.status_code, 200)
        self.assertEqual(observations.json()["counts"]["total"], 0)
        self.assertEqual(observations.json()["forward_gate"]["status"], "COLLECTING")
        self.assertEqual(
            observations.json()["setup_hypothesis"]["lifecycle"],
            "RESEARCH_ONLY",
        )
        self.assertEqual(
            observations.json()["setup_hypothesis"]["selected"]["resolved_samples"],
            0,
        )
        self.assertFalse(
            observations.json()["setup_hypothesis"]["automatic_live_entry"]
        )
        self.assertEqual(setup_stability.status_code, 200)
        self.assertEqual(setup_stability.json()["status"], "NOT_AVAILABLE")
        self.assertEqual(pairs_validation.status_code, 200)
        self.assertEqual(pairs_validation.json()["status"], "NOT_AVAILABLE")
        self.assertEqual(chan_validation.status_code, 200)
        self.assertEqual(chan_validation.json()["status"], "NOT_AVAILABLE")
        self.assertEqual(experiments.json(), [])
        self.assertEqual(early_winner.status_code, 200)
        self.assertEqual(early_winner.json()["project_id"], "early_winner_v1")
        self.assertFalse(early_winner.json()["trade_signals_enabled"])
        self.assertEqual(early_candidates.json(), [])
        paths = schema["paths"]
        self.assertIn("/api/research/daily", paths)
        self.assertIn("/api/research/weekly-triangle/observations", paths)
        self.assertIn("/api/research/weekly-triangle/setup-stability", paths)
        self.assertIn("/api/research/pairs-arbitrage/validation", paths)
        self.assertIn("/api/research/chan/validation", paths)
        self.assertIn("/api/research/experiments/{experiment_id}/promote", paths)
        self.assertIn("/api/research/early-winner", paths)
        self.assertIn("/api/research/early-winner-v6", paths)
        self.assertIn("/api/research/early-winner/refresh", paths)
        self.assertIn("/api/research/early-winner/train", paths)
        self.assertIn("/api/research/early-winner/validate", paths)
        scan_schema = schema["components"]["schemas"]["ScanRequest"]["properties"]
        self.assertNotIn("execution_cost_multiplier", scan_schema)

    def test_weekly_triangle_setup_stability_endpoint_reads_persisted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = (root / "data" / "research" / "weekly_triangle_v1")
            artifact.mkdir(parents=True)
            (artifact / "setup_stability.json").write_text(
                json.dumps({"promotion_qualified": [], "development_conversion_qualified": [1]}),
                encoding="utf-8",
            )
            client = TestClient(create_app(temporary_config(root)))
            response = client.get("/api/research/weekly-triangle/setup-stability")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "RESEARCH_ONLY")
        self.assertEqual(response.json()["development_conversion_qualified"], [1])

    def test_pairs_validation_endpoint_reads_persisted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "data" / "research" / "pairs_arbitrage_v1"
            artifact.mkdir(parents=True)
            (artifact / "historical_validation.json").write_text(
                json.dumps(
                    {
                        "decision": "HISTORICAL_REJECTED",
                        "promotion_qualified": False,
                    }
                ),
                encoding="utf-8",
            )
            client = TestClient(create_app(temporary_config(root)))
            response = client.get("/api/research/pairs-arbitrage/validation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "HISTORICAL_REJECTED")
        self.assertFalse(response.json()["promotion_qualified"])

    def test_chan_validation_endpoint_reads_persisted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "data" / "research" / "chan_v1"
            artifact.mkdir(parents=True)
            (artifact / "historical_validation.json").write_text(
                json.dumps(
                    {
                        "decision": "HISTORICAL_REJECTED",
                        "promotion_qualified": False,
                    }
                ),
                encoding="utf-8",
            )
            client = TestClient(create_app(temporary_config(root)))
            response = client.get("/api/research/chan/validation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "HISTORICAL_REJECTED")
        self.assertFalse(response.json()["promotion_qualified"])

    def test_scan_api_calls_run_scan_without_backtest_cost_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            PlatformService, "run_scan", autospec=True, return_value={"status": "SUCCEEDED"}
        ) as run_scan:
            client = TestClient(create_app(temporary_config(Path(directory))))
            submitted = client.post(
                "/api/runs/scan",
                json={"strategies": ["combined"], "mode": "research", "push_tdx": False},
            )
            job_id = submitted.json()["job_id"]
            job = client.get(f"/api/jobs/{job_id}").json()
            for _ in range(50):
                if job["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
                job = client.get(f"/api/jobs/{job_id}").json()

        self.assertEqual(job["status"], "SUCCEEDED")
        kwargs = run_scan.call_args.kwargs
        self.assertNotIn("execution_cost_multiplier", kwargs)
        self.assertFalse(kwargs["push_tdx"])


if __name__ == "__main__":
    unittest.main()
