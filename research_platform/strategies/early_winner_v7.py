from __future__ import annotations

from typing import Any

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


PROJECT_ID = "early_winner_v7"
STRATEGY_ID = "early_winner_event_quiet_v7"


class EarlyWinnerV7Strategy:
    """Catalog-only V7 preregistration; it cannot scan, backtest, or trade."""

    metadata = StrategyMetadata(
        strategy_id=STRATEGY_ID,
        version="7.0.0-preregistered",
        name="早期强势股识别 V7 · 点时数据密封验证",
        description=(
            "V6 因 V4 与退市历史审计协议升级而自锁后建立的全新预注册。"
            "V7 独立复核当前历史证券主表与退市历史质量门禁，并保留一次性揭盲、"
            "不可变结果和逐行逐周期证据重算。V7 永久保持 RESEARCH_ONLY。"
        ),
        frequency="1w",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        strategy_family=PROJECT_ID,
        scan_enabled=False,
        backtest_enabled=False,
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement("historical_universe_master", "event", "none", 0, True),
            DataRequirement("delisted_history_quality", "event", "none", 0, True),
            DataRequirement("bars", "1d", "front", 180, True),
            DataRequirement("bars", "1d", "none", 60, True),
            DataRequirement("cninfo_events", "event", "none", 30, True),
            DataRequirement("trading_calendar", "1d", "none", 0, True),
        ),
    )

    def scan(self, **context: Any) -> StrategyScanResult:
        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=(),
            state={
                "asof": context.get("asof"),
                "status": "BLOCKED_DATA",
                "candidate_count": 0,
                "lifecycle": "RESEARCH_ONLY",
                "candidate_generation_enabled": False,
                "trade_signals_enabled": False,
                "frozen_validation_opened": False,
                "promotion_allowed": False,
                "supersedes_v6_status": "PROTOCOL_CHANGED_REQUIRES_V7",
            },
        )


__all__ = ["EarlyWinnerV7Strategy", "PROJECT_ID", "STRATEGY_ID"]
