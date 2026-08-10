from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from research_platform.composition import (
    CompositionEngine,
    CompositionMode,
    ConflictPolicy,
    StrategyCatalog,
    StrategyGroupDefinition,
    StrategyGroupMember,
)
from research_platform.models import PlatformSignal, SignalStatus, StrategyScanResult
from research_platform.strategies import (
    ChanStrategy,
    Course49Strategy,
    Course49SystemStrategy,
    Course49V2Strategy,
    Course49V3Strategy,
    Course49V4Strategy,
    Course49V5Strategy,
    Course49V6Strategy,
    Course49V7Strategy,
    Course49V8Strategy,
    Course49V9Strategy,
    Course49V10Strategy,
    Course49V11Strategy,
    PairsArbitrageStrategy,
)


def signal(strategy_id: str, side: str, strength: float) -> PlatformSignal:
    now = datetime.now().astimezone()
    return PlatformSignal(
        run_id="run",
        strategy_id=strategy_id,
        strategy_version="1.0",
        generated_at=now,
        available_at=now,
        code="600000.SH",
        side=side,  # type: ignore[arg-type]
        strength=strength,
        target_weight=0.20 if side == "BUY" else 0.0,
        horizon="test",
        valid_until=now + timedelta(days=1),
        stop_price=None,
        status=SignalStatus.APPROVED,
        reason_codes=("TEST",),
    )


class CompositionTests(unittest.TestCase):
    def test_chan_strategy_is_daily_only(self) -> None:
        metadata = self.strategies["chan_v1"].metadata

        self.assertEqual(metadata.frequency, "1d")
        self.assertEqual(metadata.version, "2.0.0")
        self.assertTrue(metadata.data_requirements)
        self.assertEqual(
            {requirement.frequency for requirement in metadata.data_requirements if requirement.dataset == "bars"},
            {"1d"},
        )

    def setUp(self) -> None:
        self.strategies = {
            "chan_v1": ChanStrategy(),
            "course49_v1": Course49Strategy(),
            "course49_v2": Course49V2Strategy(),
            "course49_v3": Course49V3Strategy(),
            "course49_v4": Course49V4Strategy(),
            "course49_v5": Course49V5Strategy(),
            "course49_v6": Course49V6Strategy(),
            "course49_v7": Course49V7Strategy(),
            "course49_v8": Course49V8Strategy(),
            "course49_v9": Course49V9Strategy(),
            "course49_v10": Course49V10Strategy(),
            "course49_v11": Course49V11Strategy(),
            "course49_system": Course49SystemStrategy(),
            "pairs_arbitrage_v1": PairsArbitrageStrategy(),
        }

    def test_catalog_exposes_plugin_and_builtin_group_capabilities(self) -> None:
        records = StrategyCatalog(self.strategies).as_records()
        pairs = next(item for item in records["strategies"] if item["strategy_id"] == "pairs_arbitrage_v1")
        adaptive = next(item for item in records["groups"] if item["group_id"] == "adaptive_multi_strategy")
        self.assertEqual(pairs["execution_model"], "MULTI_LEG")
        self.assertTrue(pairs["supports_short"])
        self.assertTrue(adaptive["backtest_supported"])
        reward_compare = next(
            item for item in records["groups"] if item["group_id"] == "course49_v9_compare"
        )
        self.assertTrue(reward_compare["backtest_supported"])
        self.assertFalse(reward_compare["scan_supported"])
        v10_compare = next(
            item for item in records["groups"] if item["group_id"] == "course49_v10_compare"
        )
        self.assertTrue(v10_compare["backtest_supported"])
        self.assertFalse(v10_compare["scan_supported"])
        v11_compare = next(
            item for item in records["groups"] if item["group_id"] == "course49_v11_compare"
        )
        self.assertTrue(v11_compare["backtest_supported"])
        self.assertFalse(v11_compare["scan_supported"])

    def test_capital_sleeves_must_sum_to_one(self) -> None:
        group = StrategyGroupDefinition(
            "invalid", "1.0", "invalid", "", CompositionMode.CAPITAL_SLEEVES,
            ConflictPolicy.RISK_FIRST,
            (StrategyGroupMember("chan_v1", 0.4), StrategyGroupMember("course49_v2", 0.4)),
        )
        with self.assertRaisesRegex(ValueError, "sum to 1"):
            group.validate(self.strategies)

    def test_score_fusion_uses_risk_first_for_sell_conflict(self) -> None:
        group = StrategyGroupDefinition(
            "fusion", "1.0", "fusion", "", CompositionMode.SCORE_FUSION,
            ConflictPolicy.RISK_FIRST,
            (StrategyGroupMember("chan_v1", 0.5), StrategyGroupMember("course49_v2", 0.5)),
        )
        results = [
            StrategyScanResult(self.strategies["chan_v1"].metadata, (signal("chan_v1", "BUY", 0.9),), (), {}),
            StrategyScanResult(self.strategies["course49_v2"].metadata, (signal("course49_v2", "SELL", 0.4),), (), {}),
        ]
        composed = CompositionEngine().compose(group, results, "run")
        self.assertEqual(len(composed), 1)
        self.assertEqual(composed[0].signals[0].side, "SELL")
        self.assertEqual(composed[0].signals[0].strategy_id, "fusion")

    def test_priority_conflict_uses_highest_priority_member(self) -> None:
        group = StrategyGroupDefinition(
            "priority_fusion", "1.0", "priority", "", CompositionMode.SCORE_FUSION,
            ConflictPolicy.PRIORITY,
            (StrategyGroupMember("chan_v1", 0.5, priority=20), StrategyGroupMember("course49_v2", 0.5, priority=10)),
        )
        results = [
            StrategyScanResult(self.strategies["chan_v1"].metadata, (signal("chan_v1", "BUY", 1.0),), (), {}),
            StrategyScanResult(self.strategies["course49_v2"].metadata, (signal("course49_v2", "SELL", 0.2),), (), {}),
        ]

        composed = CompositionEngine().compose(group, results, "run")

        self.assertEqual(composed[0].signals[0].side, "SELL")
        self.assertEqual(
            composed[0].signals[0].evidence["components"][0]["strategy_id"],
            "course49_v2",
        )

    def test_signal_composition_rejects_invalid_roles(self) -> None:
        group = StrategyGroupDefinition(
            "invalid_overlay", "1.0", "invalid", "", CompositionMode.RISK_OVERLAY,
            ConflictPolicy.RISK_FIRST,
            (StrategyGroupMember("chan_v1", 0.5), StrategyGroupMember("course49_v2", 0.5)),
        )
        with self.assertRaisesRegex(ValueError, "one alpha and one risk"):
            group.validate(self.strategies)

    def test_catalog_isolates_saved_group_with_missing_plugin(self) -> None:
        stale = StrategyGroupDefinition(
            "stale_group", "1.0", "stale", "", CompositionMode.CAPITAL_SLEEVES,
            ConflictPolicy.RISK_FIRST,
            (StrategyGroupMember("removed_plugin", 1.0),),
        )

        catalog = StrategyCatalog(self.strategies, (stale,))

        self.assertNotIn("stale_group", catalog.groups)
        self.assertEqual(catalog.group_issues[0]["code"], "INVALID_GROUP")


if __name__ == "__main__":
    unittest.main()
