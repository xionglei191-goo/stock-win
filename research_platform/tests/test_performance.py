from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

import pandas as pd

from research_platform.backtest_engine import BacktestService, _required_daily_bars
from research_platform.data_cache import DataCacheManager
from research_platform.data_plan import build_data_plan
from research_platform.jobs import JobManager
from research_platform.storage import Database
from research_platform.strategies.pairs_arbitrage import PairsArbitrageStrategy
from research_platform.tests.helpers import temporary_config
from strategy_v1.config import StrategyConfig
from strategy_v1.tdx_adapter import TdxAdapter


class _FakeTq:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_market_data(self, **kwargs):
        self.calls.append(kwargs)
        code = str(kwargs["stock_list"][0])
        index = pd.to_datetime(["2022-04-28", "2022-04-29"])
        return {"Close": pd.DataFrame({code: [10.0, 10.2]}, index=index)}


class _AdaptiveFakeTq:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def get_market_data(self, **kwargs):
        codes = list(kwargs["stock_list"])
        self.batch_sizes.append(len(codes))
        if len(codes) > 100:
            return {}
        index = pd.to_datetime(["2022-04-29"])
        return {"Close": pd.DataFrame({code: [10.0] for code in codes}, index=index)}


class PerformanceInfrastructureTests(unittest.TestCase):
    def test_historical_bar_count_ends_at_requested_date(self) -> None:
        count = _required_daily_bars(180, "2021-04-01", "2022-04-29")
        self.assertLess(count, _required_daily_bars(180, "2021-04-01", "2026-08-10"))
        self.assertGreaterEqual(count, 360)

    def test_fixed_pair_strategy_data_plan_avoids_market_wide_dependencies(self) -> None:
        plan = build_data_plan([PairsArbitrageStrategy.metadata])
        self.assertFalse(plan.require_sectors)
        self.assertFalse(plan.require_style_benchmarks)
        self.assertFalse(plan.require_course49_events)
        self.assertEqual(plan.front_fields, ("Close",))
        self.assertEqual(plan.raw_fields, ("Open", "Close"))

    def test_course49_v2_defaults_event_coverage_to_first_board(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = BacktestService(config, database)
            self.assertEqual(service._candidate_minimum_streak("course49_v2"), 1)

    def test_tdx_market_request_receives_historical_end_time(self) -> None:
        adapter = TdxAdapter(
            StrategyConfig(batch_size=800),
            __file__,
            transform_workers=2,
            minimum_batch_size=100,
        )
        fake = _FakeTq()
        adapter._tq = fake
        bars = adapter.fetch_bars(
            ["600000.SH"],
            "1d",
            300,
            fields=("Close",),
            start_time="2021-04-01",
            end_time="2022-04-29",
            warmup_bars=90,
        )
        self.assertEqual(fake.calls[0]["end_time"], "20220429")
        self.assertEqual(fake.calls[0]["count"], 300)
        self.assertIn("600000.SH", bars)

    def test_failed_large_batch_halves_and_keeps_symbol_order(self) -> None:
        codes = [f"{index:06d}.SZ" for index in range(250)]
        adapter = TdxAdapter(
            StrategyConfig(batch_size=800),
            __file__,
            transform_workers=8,
            minimum_batch_size=100,
        )
        fake = _AdaptiveFakeTq()
        adapter._tq = fake
        bars = adapter.fetch_bars(codes, "1d", 90, fields=("Close",))
        self.assertEqual(list(bars), codes)
        self.assertEqual(fake.batch_sizes[0], 250)
        self.assertTrue(any(size == 100 for size in fake.batch_sizes))
        self.assertTrue(all(size <= 100 for size in adapter.successful_batch_sizes))

    def test_failed_refresh_does_not_replace_ready_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            cache = DataCacheManager(config, database)
            identity = {"universe": "all_a"}
            coverage = {
                "start_date": "2021-01-01",
                "end_date": "2022-01-01",
                "datasets": ["daily_front"],
                "event_minimum_streak": 1,
            }
            query = {"identity": identity, "coverage": coverage, "data_asof": "2022-01-01"}
            key = cache.key(query)
            old_snapshot = "old_snapshot"
            (config.snapshot_dir / old_snapshot).mkdir(parents=True)
            cache.begin_snapshot("old_build", old_snapshot, "2022-01-01", query, coverage)
            cache.commit_snapshot("old_build", key, old_snapshot)

            new_snapshot = "new_snapshot"
            (config.snapshot_dir / new_snapshot).mkdir(parents=True)
            cache.begin_snapshot("new_build", new_snapshot, "2022-01-01", query, coverage)
            cache.fail("new_build", "incomplete")

            match = cache.find(key, identity=identity, coverage=coverage)
            self.assertIsNotNone(match)
            self.assertEqual(match.snapshot_id, old_snapshot)

    def test_covering_snapshot_matches_narrower_range_and_event_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            cache = DataCacheManager(config, database)
            identity = {"universe": "all_a"}
            coverage = {
                "start_date": "2021-01-01",
                "end_date": "2022-12-31",
                "datasets": ["daily_front", "dragon_tiger"],
                "event_minimum_streak": 1,
            }
            query = {"identity": identity, "coverage": coverage, "data_asof": "2022-12-31"}
            key = cache.key(query)
            snapshot_id = "covering"
            (config.snapshot_dir / snapshot_id).mkdir(parents=True)
            cache.begin_snapshot("covering_build", snapshot_id, "2022-12-31", query, coverage)
            cache.commit_snapshot("covering_build", key, snapshot_id)
            requested = {
                "start_date": "2021-06-01",
                "end_date": "2022-06-30",
                "datasets": ["daily_front", "dragon_tiger"],
                "event_minimum_streak": 2,
            }
            match = cache.find("different-key", identity=identity, coverage=requested)
            self.assertIsNotNone(match)
            self.assertEqual(match.hit_type, "superset_hit")

    def test_prune_keeps_referenced_snapshot_and_rebuilds_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = temporary_config(Path(directory))
            config = replace(
                base,
                performance=replace(base.performance, disk_cache_bytes=1),
            )
            database = Database(config)
            database.initialize()
            cache = DataCacheManager(config, database)
            snapshot_id = "protected"
            snapshot_dir = config.snapshot_dir / snapshot_id
            snapshot_dir.mkdir(parents=True)
            (snapshot_dir / "evidence.bin").write_bytes(b"evidence")
            coverage = {
                "start_date": "2021-01-01",
                "end_date": "2021-02-01",
                "datasets": [],
                "event_minimum_streak": 1,
            }
            query = {"identity": {}, "coverage": coverage}
            cache.begin_snapshot("protected_build", snapshot_id, "2021-02-01", query, coverage)
            cache.commit_snapshot("protected_build", cache.key(query), snapshot_id)
            database.execute(
                """INSERT INTO backtests
                (backtest_id, strategy_id, status, started_at, snapshot_id, parameters_json)
                VALUES ('protected_bt', 'test', 'SUCCEEDED', '2021-02-01', ?, '{}')""",
                (snapshot_id,),
            )
            feature_key = cache.feature_key(snapshot_id, "matrix", "1")
            builds = 0

            def build() -> pd.DataFrame:
                nonlocal builds
                builds += 1
                return pd.DataFrame({"value": [1, 2, 3]})

            cache.get_or_build_feature_frames(feature_key, build)
            cache.memory.clear()
            result = cache.prune()
            self.assertTrue(snapshot_dir.exists())
            self.assertTrue(result["size_bytes"] > result["limit_bytes"])
            cache.get_or_build_feature_frames(feature_key, build)
            self.assertEqual(builds, 2)

    def test_three_jobs_run_concurrently_and_duplicates_share_one_flight(self) -> None:
        manager = JobManager(max_workers=3)
        gate = threading.Barrier(3)
        counter_lock = threading.Lock()
        active = 0
        peak = 0

        def work(value: int) -> int:
            nonlocal active, peak
            with counter_lock:
                active += 1
                peak = max(peak, active)
            gate.wait(timeout=2)
            time.sleep(0.02)
            with counter_lock:
                active -= 1
            return value

        ids = [manager.submit("backtest", work, value) for value in range(3)]
        deadline = time.time() + 3
        while time.time() < deadline and any(
            manager.get(job_id)["status"] in {"QUEUED", "RUNNING"} for job_id in ids
        ):
            time.sleep(0.01)
        self.assertEqual(peak, 3)
        self.assertTrue(all(manager.get(job_id)["status"] == "SUCCEEDED" for job_id in ids))

        release = threading.Event()

        def blocked(value: int) -> int:
            release.wait(timeout=2)
            return value

        first = manager.submit("backtest", blocked, 49)
        second = manager.submit("backtest", blocked, 49)
        self.assertEqual(first, second)
        release.set()
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
