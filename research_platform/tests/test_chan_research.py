from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from research_platform.chan_research import analyze_chan_validation
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config


class ChanResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(temporary_config(Path(self.temp.name)))
        self.database.initialize()
        self.protocol = self._protocol()
        self.baseline_ids = tuple(f"chan-base-{index}" for index in range(5))
        self.stress_ids = tuple(f"chan-stress-{index}" for index in range(5))
        self._insert_runs()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_retrospective_pass_does_not_authorize_promotion(self) -> None:
        result = self._analyze()

        self.assertTrue(result["historical_gates_passed"])
        self.assertFalse(result["promotion_qualified"])
        self.assertEqual(result["decision"], "HISTORICAL_RESEARCH_CANDIDATE")
        self.assertEqual(result["aggregate"]["baseline_completed_trades"], 50)
        self.assertTrue(result["aggregate"]["same_snapshot_replay"])
        self.assertTrue(all(result["gates"].values()))

    def test_unstable_windows_are_rejected(self) -> None:
        for backtest_id in self.baseline_ids[:2]:
            self.database.execute(
                "UPDATE backtests SET metrics_json=? WHERE backtest_id=?",
                (json.dumps(self._metrics(-0.03)), backtest_id),
            )

        result = self._analyze()

        self.assertFalse(result["historical_gates_passed"])
        self.assertEqual(result["decision"], "HISTORICAL_REJECTED")
        self.assertFalse(result["gates"]["baseline_window_stability"])

    def test_frozen_replay_contract_is_required(self) -> None:
        parameters = self._parameters(0, 1.0)
        parameters["chan_replay_contract_version"] = "1.0.0"
        self.database.execute(
            "UPDATE backtests SET parameters_json=? WHERE backtest_id=?",
            (json.dumps(parameters), self.baseline_ids[0]),
        )

        with self.assertRaisesRegex(ValueError, "incompatible Chan replay contract"):
            self._analyze()

    def _analyze(self) -> dict[str, object]:
        return analyze_chan_validation(
            self.database,
            self.protocol,
            protocol_hash="frozen-protocol",
            baseline_backtest_ids=self.baseline_ids,
            stress_backtest_ids=self.stress_ids,
        )

    def _insert_runs(self) -> None:
        with self.database.connect() as connection:
            for index, window in enumerate(self.protocol["windows"]):
                snapshot_id = window["source_snapshot_id"]
                for backtest_id, multiplier in (
                    (self.baseline_ids[index], 1.0),
                    (self.stress_ids[index], 2.0),
                ):
                    connection.execute(
                        """INSERT INTO backtests
                        (backtest_id, strategy_id, status, started_at, finished_at,
                         start_date, end_date, snapshot_id, parameters_json, metrics_json)
                        VALUES (?, 'chan_v1', 'SUCCEEDED', '2026-08-11', '2026-08-11',
                                ?, ?, ?, ?, ?)""",
                        (
                            backtest_id,
                            window["start_date"],
                            window["end_date"],
                            snapshot_id,
                            json.dumps(self._parameters(index, multiplier)),
                            json.dumps(self._metrics(0.02)),
                        ),
                    )
                    start = date.fromisoformat(window["start_date"])
                    for trade_index in range(10):
                        connection.execute(
                            """INSERT INTO backtest_trades
                            (backtest_id, strategy_id, timestamp, code, side, quantity,
                             price, fees, pnl, reason)
                            VALUES (?, 'chan_v1', ?, ?, 'SELL', 100, 10, 1, 5,
                                    'CHAN_EXIT')""",
                            (
                                backtest_id,
                                (start + timedelta(days=trade_index)).isoformat(),
                                f"{trade_index:06d}.SZ",
                            ),
                        )

    def _parameters(self, index: int, multiplier: float) -> dict[str, object]:
        window = self.protocol["windows"][index]
        return {
            "components": ["chan_v1"],
            "strategy_versions": {"chan_v1": "2.0.0"},
            "chan_replay_contract_version": "2.0.0",
            "source_backtest_id": window["source_backtest_id"],
            "source_snapshot_id": window["source_snapshot_id"],
            "stock_pool_hash": f"pool-{index}",
            "execution_cost_multiplier": multiplier,
            "sector_membership_quality": "LIMITED",
        }

    @staticmethod
    def _metrics(total_return: float) -> dict[str, object]:
        return {
            "trading_days": 250,
            "closed_trades": 10,
            "total_return": total_return,
            "annualized_return": total_return,
            "max_drawdown": -0.05,
            "win_rate": 0.6,
            "profit_factor": 1.5,
        }

    @staticmethod
    def _protocol() -> dict[str, object]:
        windows = (
            ("2021-01-01", "2021-12-31"),
            ("2022-01-01", "2022-12-31"),
            ("2023-01-01", "2023-12-31"),
            ("2024-01-01", "2024-12-31"),
            ("2025-01-01", "2025-12-31"),
        )
        return {
            "strategy_id": "chan_v1",
            "strategy_version": "2.0.0",
            "chan_replay_contract_version": "2.0.0",
            "study_type": "RETROSPECTIVE_FROZEN_AUDIT",
            "promotion_use": "NOT_ELIGIBLE",
            "windows": [
                {
                    "start_date": start,
                    "end_date": end,
                    "source_backtest_id": f"source-{index}",
                    "source_snapshot_id": f"snapshot-{index}",
                }
                for index, (start, end) in enumerate(windows)
            ],
            "cost_multipliers": {"baseline": 1.0, "stress": 2.0},
            "historical_gates": {
                "minimum_positive_baseline_windows": 4,
                "minimum_positive_stress_windows": 3,
                "minimum_completed_trades": 50,
                "minimum_windows_with_five_completed_trades": 4,
                "maximum_absolute_window_drawdown": 0.15,
                "minimum_worst_window_total_return": -0.1,
            },
            "known_limitations": ["retrospective"],
        }


if __name__ == "__main__":
    unittest.main()
