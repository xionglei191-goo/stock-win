from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from research_platform.api import create_app
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
            },
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(len(dashboard.json()["accounts"]), 14)

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
            {"chan_v1", "course49_system", "pairs_arbitrage_v1"},
        )
        self.assertEqual(len(catalog.json()["archived_strategies"]), 11)
        self.assertEqual(catalog.json()["frameworks"][0]["framework_id"], "course49")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.json()["plugin_issues"], [])
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
            experiments = client.get("/api/research/experiments")
            schema = client.get("/openapi.json").json()

        self.assertEqual(briefs.json(), [])
        self.assertEqual(feedback.json(), {"rows": [], "aggregates": []})
        self.assertEqual(experiments.json(), [])
        paths = schema["paths"]
        self.assertIn("/api/research/daily", paths)
        self.assertIn("/api/research/experiments/{experiment_id}/promote", paths)
        scan_schema = schema["components"]["schemas"]["ScanRequest"]["properties"]
        self.assertNotIn("execution_cost_multiplier", scan_schema)

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
