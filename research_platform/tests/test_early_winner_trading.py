from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from research_platform.early_winner_research import (
    EarlyWinnerResearchService,
    _historical_security_status,
    _select_validation_champion,
    _weekly_decision_dates,
)
from research_platform.early_winner_trading import (
    EarlyWinnerTradingService,
    TdxTradingHttpClient,
    TradingSafetyError,
    controlled_limit_price,
)
from research_platform.storage import Database
from research_platform.strategies.early_winner_trade import EarlyWinnerTradeStrategy
from research_platform.tests.helpers import temporary_config


TZ = ZoneInfo("Asia/Shanghai")


class _Broker:
    def __init__(self) -> None:
        self.snapshot = {"asset": [], "positions": [], "orders": []}
        self.submitted: list[dict[str, object]] = []
        self.canceled: list[dict[str, str]] = []

    def read_snapshot(self) -> dict[str, object]:
        return self.snapshot

    def submit_limit_order(
        self, *, code: str, side: str, quantity: int, price: float
    ) -> dict[str, object]:
        order = {"code": code, "side": side, "quantity": quantity, "price": price}
        self.submitted.append(order)
        return {"Value": 2, "Wtbh": f"broker-{len(self.submitted)}"}

    def cancel_order(self, *, code: str, broker_order_id: str) -> dict[str, object]:
        self.canceled.append({"code": code, "broker_order_id": broker_order_id})
        return {"Value": 1, "Msg": "accepted"}


class EarlyWinnerTradingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = temporary_config(Path(self.temporary.name))
        self.database = Database(self.config)
        self.database.initialize()
        EarlyWinnerResearchService(self.config, self.database)
        self.database.register_strategy(EarlyWinnerTradeStrategy.metadata, "builtin")
        self.broker = _Broker()
        self.service = EarlyWinnerTradingService(
            self.config,
            self.database,
            broker=self.broker,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_repository_profile_has_no_real_broker_write_transport(self) -> None:
        self.assertFalse(self.config.live_trading_enabled)
        client = TdxTradingHttpClient(self.config)
        with self.assertRaisesRegex(TradingSafetyError, "not compiled"):
            client.submit_limit_order(
                code="600000.SH", side="BUY", quantity=100, price=10.0
            )
        with self.assertRaisesRegex(TradingSafetyError, "not compiled"):
            client.cancel_order(code="600000.SH", broker_order_id="never-sent")

    def _save_passing_validation(self) -> None:
        self.database.save_research_validation(
            {
                "validation_id": "validation-1",
                "project_id": "early_winner_v1",
                "status": "OBSERVATION_ONLY",
                "created_at": "2026-01-02T15:00:00+08:00",
                "finished_at": "2026-01-02T15:01:00+08:00",
                "snapshot_id": "snapshot-frozen",
                "rule_metrics": {},
                "ml_metrics": {},
                "baseline_metrics": {},
                "stress_metrics": {},
                "gates": {"rule": {"passed": True}, "ml": {"passed": False}},
                "champion": {
                    "method": "rule",
                    "strategy_id": "early_winner_rule_v1",
                    "artifact_hash": "a" * 64,
                    "feature_schema_hash": "b" * 64,
                    "snapshot_id": "snapshot-frozen",
                    "validation_id": "validation-1",
                },
                "error": "",
            }
        )

    def _save_rejected_validation(self) -> None:
        self.database.save_research_validation(
            {
                "validation_id": "validation-rejected",
                "project_id": "early_winner_v1",
                "status": "VALIDATION_REJECTED",
                "created_at": "2026-01-03T15:00:00+08:00",
                "finished_at": "2026-01-03T15:01:00+08:00",
                "snapshot_id": "snapshot-rejected",
                "rule_metrics": {},
                "ml_metrics": {},
                "baseline_metrics": {},
                "stress_metrics": {},
                "gates": {
                    "rule": {"passed": False},
                    "ml": {"passed": False},
                },
                "champion": {},
                "error": "both methods failed the frozen validation gates",
            }
        )

    @staticmethod
    def _candidates() -> list[dict[str, object]]:
        return [
            {
                "code": f"60000{index}.SH",
                "name": f"样本{index}",
                "industry": "行业甲",
                "rank": index + 1,
                "close": 10.0,
                "adv20": 100_000_000.0,
                "rule_score": 90 - index,
            }
            for index in range(7)
        ]

    def test_shadow_requires_frozen_validation_and_batch_is_idempotent(self) -> None:
        with self.assertRaises(TradingSafetyError):
            self.service.activate_shadow()
        self._save_passing_validation()
        detail = self.service.activate_shadow()
        self.assertEqual(detail["state"], "SHADOW")
        self.assertEqual(detail["champion"]["method"], "rule")
        batch = self.service.create_order_batch(
            rebalance_date="2099-01-02",
            execution_date="2099-01-05",
            candidates=self._candidates(),
            positions=[],
            equity=1_000_000,
            account_equity=1_000_000,
            market_health={},
        )
        duplicate = self.service.create_order_batch(
            rebalance_date="2099-01-02",
            execution_date="2099-01-05",
            candidates=list(reversed(self._candidates())),
            positions=[],
            equity=1_000_000,
            account_equity=1_000_000,
            market_health={},
        )
        self.assertEqual(batch["batch_id"], duplicate["batch_id"])
        self.assertEqual(len(batch["intents"]), 5)
        self.assertIn("confirmation_code", batch)
        self.assertNotIn("confirmation_code", duplicate)
        challenge = self.service.order_batch_confirmation_challenge(batch["batch_id"])
        self.assertEqual(challenge["confirmation_code"], batch["confirmation_code"])

    def test_rejected_validation_cannot_activate_shadow(self) -> None:
        self._save_rejected_validation()

        with self.assertRaisesRegex(
            TradingSafetyError,
            "research validation has not passed",
        ):
            self.service.activate_shadow()

        self.assertEqual(self.service.detail()["state"], "VALIDATION_REQUIRED")

    def test_batch_approval_shadow_fill_and_t_plus_one_sellable_quantity(self) -> None:
        self._save_passing_validation()
        self.service.activate_shadow()
        batch = self.service.create_order_batch(
            rebalance_date="2099-01-02",
            execution_date="2099-01-05",
            candidates=[],
            positions=[
                {
                    "code": "600000.SH",
                    "quantity": 1_000,
                    "can_use_volume": 300,
                    "adv20": 100_000_000,
                }
            ],
            equity=1_000_000,
            account_equity=1_000_000,
            market_health={},
        )
        self.assertEqual(batch["intents"][0]["requested_quantity"], 300)
        approved = self.service.decide_order_batch(
            batch["batch_id"],
            decision="APPROVED",
            confirmation_code=batch["confirmation_code"],
            now=datetime(2099, 1, 5, 9, 19, tzinfo=TZ),
        )
        intent = approved["intents"][0]
        result = self.service.submit_intent(
            intent["intent_id"],
            bid=10.00,
            ask=10.03,
            limit_up=11.00,
            limit_down=9.00,
            quote_age_seconds=0,
            clock_skew_seconds=0,
            calendar_match=True,
            now=datetime(2099, 1, 5, 9, 30, 5, tzinfo=TZ),
        )
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(self.broker.submitted, [])

    def test_stale_quote_and_loss_limits_fail_closed(self) -> None:
        self._save_passing_validation()
        self.service.activate_shadow()
        batch = self.service.create_order_batch(
            rebalance_date="2099-01-02",
            execution_date="2099-01-05",
            candidates=self._candidates()[:1],
            positions=[],
            equity=1_000_000,
            account_equity=1_000_000,
            market_health={},
        )
        approved = self.service.decide_order_batch(
            batch["batch_id"],
            decision="APPROVED",
            confirmation_code=batch["confirmation_code"],
            now=datetime(2099, 1, 5, 9, 19, tzinfo=TZ),
        )
        with self.assertRaises(TradingSafetyError):
            self.service.submit_intent(
                approved["intents"][0]["intent_id"],
                bid=10,
                ask=10.03,
                limit_up=11,
                limit_down=9,
                quote_age_seconds=6,
                clock_skew_seconds=0,
                calendar_match=True,
                now=datetime(2099, 1, 5, 9, 30, 5, tzinfo=TZ),
            )
        self.service.record_sleeve_equity(1_000)
        self.service.record_sleeve_equity(970)
        self.assertEqual(self.service.detail()["state"], "RISK_HALTED")

    def test_reconciliation_blocks_unknown_open_broker_order(self) -> None:
        self._save_passing_validation()
        self.service.activate_shadow()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE trading_deployments SET state='LIVE_PILOT', max_capital_cny=10000,
                max_account_fraction=0.1 WHERE deployment_id='early_winner_trade_v1'"""
            )
        self.broker.snapshot = {
            "asset": {"Value": [{"Asset": "100000"}]},
            "positions": {"Value": []},
            "orders": {
                "Value": [
                    {
                        "Wtbh": "external-1",
                        "Code": "600000.SH",
                        "WtDate": 0,
                        "WtVol": "100",
                        "CJVol": "0",
                    }
                ]
            },
        }
        result = self.service.reconcile()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.service.detail()["state"], "RECONCILIATION_BLOCKED")

    def test_funding_has_no_default_and_cannot_skip_paper_gate(self) -> None:
        self.assertIsNone(self.service.detail()["max_capital_cny"])
        with self.assertRaises(TradingSafetyError):
            self.service.configure_pilot(max_capital_cny=10_000, max_account_fraction=0.1)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE trading_deployments SET state='PAPER_QUALIFIED' WHERE deployment_id=?",
                ("early_winner_trade_v1",),
            )
        result = self.service.configure_pilot(
            max_capital_cny=10_000,
            max_account_fraction=0.1,
        )
        self.assertEqual(result["state"], "LIVE_APPROVAL_REQUIRED")

    def test_controlled_price_respects_offset_tick_and_exchange_limits(self) -> None:
        self.assertEqual(
            controlled_limit_price(side="BUY", bid=10, ask=10.03, limit_up=10.04, limit_down=9),
            10.04,
        )
        self.assertEqual(
            controlled_limit_price(side="SELL", bid=10, ask=10.03, limit_up=11, limit_down=9.99),
            9.99,
        )
        reasons = self.service.risk_exit_reasons(
            [
                {"code": "A", "close": 9, "ma60": 10, "holding_peak": 12},
                {"code": "B", "close": 9, "ma60": 8, "holding_peak": 12},
                {"code": "C", "close": 9, "ma60": 8, "holding_peak": 10, "event_type": "REDUCTION"},
            ]
        )
        self.assertEqual(reasons, {"A": "BELOW_MA60", "B": "DRAWDOWN_25_PERCENT", "C": "MAJOR_NEGATIVE_EVENT"})

    def test_schema_contains_v13_trading_audit_tables(self) -> None:
        names = {
            row["name"]
            for row in self.database.query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'trading_%'"
            )
        }
        self.assertTrue(
            {
                "trading_deployments",
                "trading_order_batches",
                "trading_order_intents",
                "trading_broker_orders",
                "trading_broker_fills",
                "trading_reconciliations",
                "trading_risk_events",
                "trading_scheduler_heartbeats",
            }.issubset(names)
        )


class EarlyWinnerFreezeTests(unittest.TestCase):
    def test_champion_selection_is_unique_and_rule_preferred(self) -> None:
        common = {
            "model_row": {"model_id": "m1", "artifact_hash": "m" * 64, "feature_schema_hash": "f" * 64},
            "snapshot_id": "s1",
            "validation_id": "v1",
            "selected_at": "2026-01-01T15:00:00+08:00",
            "rule_artifact_hash": "r" * 64,
        }
        champion = _select_validation_champion(
            gates={"rule": {"passed": True}, "ml": {"passed": True}},
            **common,
        )
        self.assertEqual(champion["method"], "rule")
        rejected = _select_validation_champion(
            gates={"rule": {"passed": False}, "ml": {"passed": False}},
            **common,
        )
        self.assertEqual(rejected, {})

    def test_weekly_calendar_and_historical_st_state_are_point_in_time(self) -> None:
        weeks = _weekly_decision_dates(
            ["2024-01-02", "2024-01-03", "2024-01-05", "2024-01-08", "2024-01-12"],
            2024,
            2024,
        )
        self.assertEqual([item.strftime("%Y-%m-%d") for item in weeks], ["2024-01-05", "2024-01-12"])
        status_payload = {
            "GP29": {
                "600000.SH": {
                    "index": ["2024-01-03", "2024-02-03"],
                    "data": [[0, 2], [0, 4]],
                }
            }
        }
        self.assertTrue(
            _historical_security_status(status_payload, "600000.SH", datetime(2024, 1, 31))["is_st"]
        )
        self.assertFalse(
            _historical_security_status(status_payload, "600000.SH", datetime(2024, 2, 29))["is_st"]
        )


if __name__ == "__main__":
    unittest.main()
