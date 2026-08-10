from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.backtest_engine import _performance_metrics
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config
from research_platform.validation import validate_course49_v3


class Course49ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(temporary_config(Path(self.temp.name)))
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_gate_requires_same_snapshot_double_cost_and_forward_evidence(self) -> None:
        days = pd.date_range("2026-01-02", periods=300, freq="B")
        values = 50_000.0 * (1.001 ** pd.Series(range(len(days))))
        equity = pd.DataFrame({"timestamp": days, "equity": values, "cash": values, "positions": 0})
        trade_days = days[5::7]
        trades = pd.DataFrame(
            {
                "timestamp": trade_days,
                "code": "600000.SH",
                "side": "SELL",
                "quantity": 100,
                "price": 10.0,
                "fees": 5.0,
                "pnl": 100.0,
                "reason": "TEST",
                "evidence": "{}",
            }
        )
        metrics = _performance_metrics(equity, trades, 50_000.0)
        self._insert_backtest(
            "baseline",
            "snapshot",
            {"stock_pool_hash": "hash", "execution_cost_multiplier": 1.0},
            metrics,
        )
        self._insert_backtest(
            "stress",
            "snapshot",
            {
                "stock_pool_hash": "hash",
                "execution_cost_multiplier": 2.0,
                "snapshot_replay": True,
            },
            {**metrics, "total_return": metrics["total_return"] - 0.02},
        )
        self._insert_backtest(
            "holdout",
            "holdout-snapshot",
            {"stock_pool_hash": "other-hash", "execution_cost_multiplier": 1.0},
            metrics,
            start_date="2024-01-02",
            end_date="2025-02-25",
        )
        with self.database.connect() as connection:
            connection.executemany(
                """INSERT INTO backtest_equity
                (backtest_id, strategy_id, timestamp, equity, cash, positions)
                VALUES ('baseline', 'course49_v3', ?, ?, ?, 0)""",
                [
                    (row.timestamp.isoformat(), float(row.equity), float(row.cash))
                    for row in equity.itertuples()
                ],
            )
            connection.executemany(
                """INSERT INTO backtest_trades
                (backtest_id, strategy_id, timestamp, code, side, quantity, price, fees, pnl,
                 reason, evidence, group_key, leg_id)
                VALUES ('baseline', 'course49_v3', ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')""",
                [
                    (
                        pd.Timestamp(row.timestamp).isoformat(),
                        row.code,
                        row.side,
                        int(row.quantity),
                        float(row.price),
                        float(row.fees),
                        float(row.pnl),
                        row.reason,
                        row.evidence,
                    )
                    for row in trades.itertuples()
                ],
            )

        verified = validate_course49_v3(
            self.database,
            "baseline",
            stress_backtest_id="stress",
            historical_holdout_backtest_id="holdout",
        )
        missing_stress = validate_course49_v3(self.database, "baseline")

        self.assertEqual(verified["status"], "VERIFIED")
        self.assertTrue(verified["checks"]["forward_60_days"])
        self.assertTrue(verified["cost_stress"]["same_snapshot"])
        self.assertTrue(verified["historical_holdout"]["non_overlapping"])
        self.assertGreater(verified["trade_concentration"]["pnl_without_top3_winners"], 0)
        self.assertEqual(missing_stress["status"], "UNVERIFIED")
        self.assertIn("double_cost_stress_positive", missing_stress["failed_checks"])

    def _insert_backtest(
        self,
        backtest_id: str,
        snapshot_id: str,
        parameters: dict[str, object],
        metrics: dict[str, object],
        *,
        start_date: str = "2026-01-02",
        end_date: str = "2027-02-25",
    ) -> None:
        self.database.execute(
            """INSERT INTO backtests
            (backtest_id, strategy_id, status, started_at, finished_at, start_date, end_date,
             snapshot_id, parameters_json, metrics_json)
            VALUES (?, 'course49_v3', 'SUCCEEDED', '2026-01-01', '2027-03-01',
                    ?, ?, ?, ?, ?)""",
            (
                backtest_id,
                start_date,
                end_date,
                snapshot_id,
                json.dumps(parameters),
                json.dumps(metrics),
            ),
        )


if __name__ == "__main__":
    unittest.main()
