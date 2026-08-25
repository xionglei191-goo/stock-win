from __future__ import annotations

from typing import Any

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


PROJECT_ID = "early_winner_v6"
STRATEGY_ID = "early_winner_event_quiet_v6"


class EarlyWinnerV6Strategy:
    """Catalog-only V6 preregistration; it cannot scan, backtest, or trade."""

    metadata = StrategyMetadata(
        strategy_id=STRATEGY_ID,
        version="6.0.0-preregistered",
        name="早期强势股识别 V6 · 密封事件验证",
        description=(
            "修复 V5 冻结揭盲闭环的预注册版本：分片逐项绑定、数据库一次性消费、"
            "结果审计绑定，并冻结复用的 V4 标签与八相位账簿实现。"
            "V6 永久保持 RESEARCH_ONLY。"
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
                "supersedes_v5_status": "PREREGISTRATION_REJECTED",
            },
        )


__all__ = ["EarlyWinnerV6Strategy", "PROJECT_ID", "STRATEGY_ID"]
