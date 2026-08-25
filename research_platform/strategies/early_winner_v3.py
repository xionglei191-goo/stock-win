from __future__ import annotations

from typing import Any

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


PROJECT_ID = "early_winner_v3"
ML_STRATEGY_ID = "early_winner_ml_v3"


class EarlyWinnerV3Strategy:
    """Point-in-time data-repair wrapper; it never emits platform trade signals."""

    metadata = StrategyMetadata(
        strategy_id=ML_STRATEGY_ID,
        version="3.0.0-dev1",
        name="早期强势股识别 V3 · 点时补数",
        description="补齐历史换手率和点时 PE 分位；冻结验证通过前不生成候选。",
        frequency="1w",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        strategy_family=PROJECT_ID,
        scan_enabled=False,
        backtest_enabled=False,
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement("bars", "1d", "front", 180, True),
            DataRequirement("bars", "1d", "none", 180, True),
            DataRequirement("share_capital_history", "1d", "none", 0, True),
            DataRequirement("financials", "quarterly", "none", 0, True),
        ),
    )

    def scan(self, **context: Any) -> StrategyScanResult:
        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=(),
            state={
                "asof": context.get("asof"),
                "status": "DATA_BUILDING",
                "candidate_count": 0,
                "trade_signals_enabled": False,
                "frozen_validation_opened": False,
            },
        )


__all__ = ["EarlyWinnerV3Strategy", "ML_STRATEGY_ID", "PROJECT_ID"]
