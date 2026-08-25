from __future__ import annotations

from typing import Any

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


PROJECT_ID = "early_winner_v5"
STRATEGY_ID = "early_winner_event_quiet_v5"


class EarlyWinnerV5Strategy:
    """Catalog-only V5 preregistration; it cannot scan or trade."""

    metadata = StrategyMetadata(
        strategy_id=STRATEGY_ID,
        version="5.0.0-preregistered",
        name="早期强势股识别 V5 · 事件静默量价",
        description=(
            "预注册的两键事件规则；历史证券母表与事件证据通过前保持阻断。"
            "2024/2025 只允许一次冻结验证，V5 永久保持 RESEARCH_ONLY。"
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
                "trade_signals_enabled": False,
                "candidate_generation_enabled": False,
                "frozen_validation_opened": False,
                "promotion_allowed": False,
            },
        )


__all__ = ["EarlyWinnerV5Strategy", "PROJECT_ID", "STRATEGY_ID"]
