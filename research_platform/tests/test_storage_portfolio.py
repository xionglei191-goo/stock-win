from __future__ import annotations

import tempfile
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa

from research_platform.models import (
    OrderGroupAction,
    OrderGroupIntent,
    OrderLegIntent,
    PlatformSignal,
    SignalStatus,
)
from research_platform.portfolio import PaperPortfolio
from research_platform.storage import Database, ParquetSnapshotStore
from research_platform.tests.helpers import temporary_config


def signal(side: str, generated_at: datetime, status: SignalStatus) -> PlatformSignal:
    return PlatformSignal(
        run_id="run",
        strategy_id="course49_v1",
        strategy_version="1.0.0",
        generated_at=generated_at,
        available_at=generated_at,
        code="600000.SH",
        side=side,  # type: ignore[arg-type]
        strength=0.9,
        target_weight=0.4 if side == "BUY" else 0.0,
        horizon="daily-short",
        valid_until=generated_at + timedelta(days=2),
        stop_price=9.5 if side == "BUY" else None,
        status=status,
        reason_codes=("TEST",),
        evidence={"price": 10.0, "sector_code": "S1"},
    )


class StoragePortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = temporary_config(Path(self.temp.name))
        self.database = Database(self.config)
        self.database.initialize()
        self.database.create_run("run", "scan", "research", ["course49_v1"])
        self.portfolio = PaperPortfolio(self.config, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_approval_is_audited_and_queues_order(self) -> None:
        proposed = signal("BUY", datetime.now().astimezone(), SignalStatus.PROPOSED)
        self.database.save_signals([proposed])
        result = self.database.decide_signal(proposed.signal_id, SignalStatus.APPROVED, "确认题材")
        decisions = self.database.query("SELECT * FROM signal_decisions")
        orders = self.database.query("SELECT * FROM paper_orders")
        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(decisions[0]["note"], "确认题材")
        self.assertEqual(orders[0]["status"], "PENDING")

    def test_schema_migration_is_serialized_across_instances(self) -> None:
        config = temporary_config(Path(self.temp.name) / "parallel")
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: Database(config).initialize(), range(4)))
        columns = {
            row["name"]
            for row in Database(config).query("PRAGMA table_info(paper_group_positions)")
        }
        self.assertIn("entry_fees", columns)

    def test_v5_database_migrates_to_v8_cache_schema(self) -> None:
        config = temporary_config(Path(self.temp.name) / "v5")
        config.database_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(config.database_path)
        try:
            connection.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO schema_migrations VALUES (5, '2026-01-01T00:00:00')")
            connection.execute(
                """CREATE TABLE strategies (
                strategy_id TEXT PRIMARY KEY, version TEXT, name TEXT, description TEXT,
                frequency TEXT, enabled INTEGER, metadata_json TEXT)"""
            )
            connection.execute(
                """CREATE TABLE signal_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, decision TEXT,
                note TEXT DEFAULT '', decided_at TEXT)"""
            )
            connection.commit()
        finally:
            connection.close()

        database = Database(config)
        database.initialize()

        strategy_columns = {row["name"] for row in database.query("PRAGMA table_info(strategies)")}
        decision_columns = {row["name"] for row in database.query("PRAGMA table_info(signal_decisions)")}
        self.assertEqual(
            database.query("SELECT MAX(version) AS version FROM schema_migrations")[0]["version"], 8
        )
        self.assertIn("scan_enabled", strategy_columns)
        self.assertIn("runtime_adapter", strategy_columns)
        self.assertIn("ai_alignment", decision_columns)
        self.assertTrue(database.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_experiments'"
        ))
        self.assertTrue(database.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='data_cache_entries'"
        ))

    def test_position_cap_and_t_plus_one(self) -> None:
        buy = signal("BUY", datetime(2026, 1, 5, 15, 0).astimezone(), SignalStatus.APPROVED)
        self.database.save_signals([buy])
        self.portfolio.queue_approved([buy])
        bars = {
            "600000.SH": pd.DataFrame(
                {"Open": [10.0, 10.1], "Close": [10.0, 10.2]},
                index=[pd.Timestamp("2026-01-05 15:00"), pd.Timestamp("2026-01-06 09:30")],
            )
        }
        self.portfolio.process_pending(bars, {"600000.SH": "浦发银行"})
        positions = self.portfolio.positions("course49_v1")
        self.assertEqual(len(positions), 1)
        self.assertLessEqual(positions[0]["quantity"] * positions[0]["last_price"], 20_000.01)

        sell = signal("SELL", datetime(2026, 1, 6, 10, 0).astimezone(), SignalStatus.APPROVED)
        self.database.save_signals([sell])
        self.portfolio.queue_approved([sell])
        sell_bars = {
            "600000.SH": pd.DataFrame(
                {"Open": [10.2, 10.3], "Close": [10.2, 10.3]},
                index=[pd.Timestamp("2026-01-06 13:00"), pd.Timestamp("2026-01-07 09:30")],
            )
        }
        self.portfolio.process_pending(sell_bars, {"600000.SH": "浦发银行"})
        self.assertFalse(self.portfolio.positions("course49_v1"))

    def test_course49_limit_up_buy_does_not_fill_on_a_later_day(self) -> None:
        buy = signal("BUY", datetime(2026, 1, 5, 15, 0).astimezone(), SignalStatus.APPROVED)
        self.database.save_signals([buy])
        self.portfolio.queue_approved([buy])
        bars = {
            "600000.SH": pd.DataFrame(
                {
                    "Open": [10.0, 11.0, 10.5],
                    "Close": [10.0, 11.0, 10.6],
                },
                index=[
                    pd.Timestamp("2026-01-05 15:00"),
                    pd.Timestamp("2026-01-06 09:30"),
                    pd.Timestamp("2026-01-07 09:30"),
                ],
            )
        }

        self.portfolio.process_pending(bars, {"600000.SH": "浦发银行"})

        order = self.database.query("SELECT * FROM paper_orders WHERE signal_id=?", (buy.signal_id,))[0]
        self.assertEqual(order["status"], "CANCELED")
        self.assertEqual(order["block_reason"], "NEXT_OPEN_NOT_TRADABLE")
        self.assertFalse(self.portfolio.positions("course49_v1"))

    def test_sector_membership_uses_latest_snapshot_not_after_asof(self) -> None:
        snapshots = ParquetSnapshotStore(self.config, self.database)
        snapshots.write_records(
            "old",
            "sector_membership",
            [{"sector_code": "S1", "sector_name": "旧题材", "member_code": "600000.SH"}],
            {"asof": "2026-01-05", "quality": "CURRENT"},
        )
        snapshots.write_records(
            "future",
            "sector_membership",
            [{"sector_code": "S2", "sector_name": "未来题材", "member_code": "000001.SZ"}],
            {"asof": "2026-01-10", "quality": "CURRENT"},
        )
        loaded = snapshots.load_sector_membership("2026-01-07")
        self.assertIsNotNone(loaded)
        sectors, metadata = loaded or ({}, {})
        self.assertEqual(list(sectors), ["S1"])
        self.assertEqual(metadata["effective_asof"], "2026-01-05")

    def test_snapshot_replay_loader_verifies_hash_and_restores_bar_metadata(self) -> None:
        snapshots = ParquetSnapshotStore(self.config, self.database)
        frame = pd.DataFrame(
            {
                "Open": [10.0, 10.1],
                "Close": [10.1, 10.2],
                "Volume": [1_000.0, 2_000.0],
                "Amount": [10_000.0, 20_000.0],
            },
            index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
        )
        path = snapshots.write_bars(
            "replay",
            "daily_front",
            {"600000.SH": frame},
            {"adjustment": "front"},
        )

        loaded = snapshots.load_bars("replay", "daily_front")

        self.assertEqual(list(loaded), ["600000.SH"])
        self.assertEqual(loaded["600000.SH"].attrs["amount_unit"], "CNY")
        self.assertEqual(loaded["600000.SH"].attrs["adjustment"], "front")
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            snapshots.load_bars("replay", "daily_front")

    def test_snapshot_stream_writer_appends_batches_and_registers_hash(self) -> None:
        snapshots = ParquetSnapshotStore(self.config, self.database)
        schema = pa.schema(
            [pa.field("code", pa.string()), pa.field("score", pa.float64())]
        )
        writer = snapshots.open_record_writer(
            "stream", "events", {"scope": "test"}, schema=schema
        )
        with writer:
            writer.append([{"code": "600000.SH", "score": 0.5}])
            writer.append([{"code": "000001.SZ", "score": 0.7}])

        loaded = snapshots.load_records("stream", "events")

        self.assertEqual(writer.row_count, 2)
        self.assertEqual(loaded["code"].tolist(), ["600000.SH", "000001.SZ"])

    def test_sector_membership_preserves_limited_root_provenance(self) -> None:
        snapshots = ParquetSnapshotStore(self.config, self.database)
        rows = [{"sector_code": "S1", "sector_name": "当前题材", "member_code": "600000.SH"}]
        snapshots.write_records(
            "root",
            "sector_membership",
            rows,
            {
                "asof": "2026-01-05",
                "quality": "LIMITED",
                "source": "current_fallback",
            },
        )
        snapshots.write_records(
            "copy",
            "sector_membership",
            rows,
            {
                "asof": "2026-01-06",
                "quality": "HISTORICAL_SNAPSHOT",
                "source": "root",
            },
        )

        loaded = snapshots.load_sector_membership("2026-01-07")

        self.assertIsNotNone(loaded)
        _, metadata = loaded or ({}, {})
        self.assertEqual(metadata["quality"], "LIMITED")
        self.assertEqual(metadata["source"], "current_fallback")
        self.assertEqual(metadata["effective_asof"], "2026-01-05")

    def test_multi_leg_open_is_atomic(self) -> None:
        generated = pd.Timestamp.now(tz="Asia/Shanghai").normalize().replace(hour=15)
        next_day = generated + pd.offsets.BDay(1)
        intent = OrderGroupIntent(
            run_id="run",
            strategy_id="pairs_arbitrage_v1",
            strategy_version="1.0.0",
            generated_at=generated.to_pydatetime(),
            available_at=generated.to_pydatetime(),
            valid_until=(generated + pd.offsets.BDay(2)).to_pydatetime(),
            group_key="600036.SH|601166.SH",
            action=OrderGroupAction.OPEN,
            strength=0.8,
            gross_target_weight=0.4,
            status=SignalStatus.PROPOSED,
            reason_codes=("PAIR_ZSCORE_ENTRY",),
            legs=(
                OrderLegIntent("600036.SH", "BUY", 1.0, 0.5),
                OrderLegIntent("601166.SH", "SHORT", 1.0, 0.5),
            ),
        )
        self.database.save_order_groups([intent])
        self.database.decide_order_group(intent.intent_id, SignalStatus.APPROVED, "approve pair")
        index = [generated.tz_localize(None), next_day.replace(hour=9, minute=30).tz_localize(None)]
        bars = {
            "600036.SH": pd.DataFrame({"Open": [10.0, 10.1], "Close": [10.0, 10.2]}, index=index),
            "601166.SH": pd.DataFrame({"Open": [20.0, 19.9], "Close": [20.0, 19.8]}, index=index),
        }
        fills = self.portfolio.process_pending_groups(
            bars, {"600036.SH": "招商银行", "601166.SH": "兴业银行"}
        )
        self.assertEqual(len(fills), 2)
        self.assertEqual(len(self.database.group_positions("pairs_arbitrage_v1")), 2)
        self.assertEqual(self.database.get_order_group(intent.intent_id)["status"], "EXECUTED")

    def test_multi_leg_open_does_not_partially_fill(self) -> None:
        generated = pd.Timestamp.now(tz="Asia/Shanghai").normalize().replace(hour=15)
        next_day = generated + pd.offsets.BDay(1)
        intent = OrderGroupIntent(
            run_id="run",
            strategy_id="pairs_arbitrage_v1",
            strategy_version="1.0.0",
            generated_at=generated.to_pydatetime(),
            available_at=generated.to_pydatetime(),
            valid_until=(generated + pd.offsets.BDay(2)).to_pydatetime(),
            group_key="missing-leg",
            action=OrderGroupAction.OPEN,
            strength=0.8,
            gross_target_weight=0.4,
            status=SignalStatus.PROPOSED,
            reason_codes=("PAIR_ZSCORE_ENTRY",),
            legs=(
                OrderLegIntent("600036.SH", "BUY", 1.0, 0.5),
                OrderLegIntent("601166.SH", "SHORT", 1.0, 0.5),
            ),
        )
        self.database.save_order_groups([intent])
        self.database.decide_order_group(intent.intent_id, SignalStatus.APPROVED)
        bars = {
            "600036.SH": pd.DataFrame(
                {"Open": [10.0, 10.1], "Close": [10.0, 10.2]},
                index=[generated.tz_localize(None), next_day.replace(hour=9, minute=30).tz_localize(None)],
            )
        }
        fills = self.portfolio.process_pending_groups(bars, {"600036.SH": "招商银行"})
        self.assertEqual(fills, [])
        self.assertEqual(self.database.group_positions("pairs_arbitrage_v1"), [])
        self.assertEqual(self.database.query("SELECT * FROM paper_group_fills"), [])


if __name__ == "__main__":
    unittest.main()
