from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from research_platform.pairs_arbitrage_research import (
    VALIDATION_WINDOWS,
    analyze_pairs_arbitrage_validation,
    persist_pairs_arbitrage_validation,
)
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config


class PairsArbitrageResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(temporary_config(Path(self.temp.name)))
        self.database.initialize()
        self.baseline_ids = tuple(f"base-{index}" for index in range(5))
        self.stress_ids = tuple(f"stress-{index}" for index in range(5))
        self._insert_validation_runs()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_retrospective_gates_do_not_authorize_promotion(self) -> None:
        result = self._analyze()

        self.assertTrue(result["historical_gates_passed"])
        self.assertFalse(result["promotion_qualified"])
        self.assertEqual(result["decision"], "HISTORICAL_RESEARCH_CANDIDATE")
        self.assertEqual(result["aggregate"]["baseline_completed_pair_groups"], 50)
        self.assertTrue(result["aggregate"]["same_snapshot_replay"])
        self.assertTrue(all(result["gates"].values()))

    def test_unstable_returns_and_snapshot_mismatch_are_rejected(self) -> None:
        self.database.execute(
            "UPDATE backtests SET metrics_json=? WHERE backtest_id=?",
            (json.dumps(self._metrics(-0.01)), self.baseline_ids[0]),
        )
        self.database.execute(
            "UPDATE backtests SET metrics_json=? WHERE backtest_id=?",
            (json.dumps(self._metrics(-0.01)), self.baseline_ids[1]),
        )
        self.database.execute(
            "UPDATE backtests SET snapshot_id='different' WHERE backtest_id=?",
            (self.stress_ids[1],),
        )

        result = self._analyze()

        self.assertFalse(result["historical_gates_passed"])
        self.assertEqual(result["decision"], "HISTORICAL_REJECTED")
        self.assertFalse(result["gates"]["same_snapshot_cost_replay"])
        self.assertFalse(result["gates"]["baseline_window_stability"])

    def test_frozen_version_and_standalone_run_are_required(self) -> None:
        parameters = self._parameters("snapshot-0", 1.0)
        parameters["strategy_versions"] = {"pairs_arbitrage_v1": "1.0.1"}
        self.database.execute(
            "UPDATE backtests SET parameters_json=? WHERE backtest_id=?",
            (json.dumps(parameters), self.baseline_ids[0]),
        )

        with self.assertRaisesRegex(ValueError, "not frozen"):
            self._analyze()

    def test_persistence_round_trip(self) -> None:
        result = self._analyze()
        output = Path(self.temp.name) / "artifact"

        path = persist_pairs_arbitrage_validation(result, output)
        persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "historical_validation.json")
        self.assertEqual(persisted["strategy_id"], "pairs_arbitrage_v1")
        self.assertFalse(persisted["promotion_qualified"])

    def _analyze(self) -> dict[str, object]:
        return analyze_pairs_arbitrage_validation(
            self.database,
            baseline_backtest_ids=self.baseline_ids,
            stress_backtest_ids=self.stress_ids,
        )

    def _insert_validation_runs(self) -> None:
        with self.database.connect() as connection:
            for index, (start_date, end_date) in enumerate(VALIDATION_WINDOWS):
                snapshot_id = f"snapshot-{index}"
                for backtest_id, multiplier in (
                    (self.baseline_ids[index], 1.0),
                    (self.stress_ids[index], 2.0),
                ):
                    connection.execute(
                        """INSERT INTO backtests
                        (backtest_id, strategy_id, status, started_at, finished_at,
                         start_date, end_date, snapshot_id, parameters_json, metrics_json)
                        VALUES (?, 'pairs_arbitrage_v1', 'SUCCEEDED', '2021-01-01',
                                '2026-08-08', ?, ?, ?, ?, ?)""",
                        (
                            backtest_id,
                            start_date,
                            end_date,
                            snapshot_id,
                            json.dumps(self._parameters(snapshot_id, multiplier)),
                            json.dumps(self._metrics(0.01)),
                        ),
                    )
                    start = date.fromisoformat(start_date)
                    for group_index in range(10):
                        timestamp = (start + timedelta(days=group_index)).isoformat()
                        group_key = f"pair-{group_index}"
                        for code, side in (("LEFT.SH", "SELL"), ("RIGHT.SH", "COVER")):
                            connection.execute(
                                """INSERT INTO backtest_trades
                                (backtest_id, strategy_id, timestamp, code, side, quantity,
                                 price, fees, pnl, reason, group_key)
                                VALUES (?, 'pairs_arbitrage_v1', ?, ?, ?, 100, 10, 1, 5,
                                        'PAIR_MEAN_REVERTED', ?)""",
                                (backtest_id, timestamp, code, side, group_key),
                            )

    @staticmethod
    def _parameters(snapshot_id: str, multiplier: float) -> dict[str, object]:
        return {
            "components": ["pairs_arbitrage_v1"],
            "strategy_versions": {"pairs_arbitrage_v1": "1.0.0"},
            "source_snapshot_id": snapshot_id,
            "stock_pool_hash": "fixed-universe",
            "execution_cost_multiplier": multiplier,
        }

    @staticmethod
    def _metrics(total_return: float) -> dict[str, object]:
        return {
            "trading_days": 250,
            "closed_trades": 20,
            "total_return": total_return,
            "annualized_return": total_return,
            "max_drawdown": -0.01,
            "components": {
                "pairs_arbitrage_v1": {
                    "average_gross_exposure": 0.3,
                    "average_net_exposure": 0.0,
                }
            },
        }


if __name__ == "__main__":
    unittest.main()
