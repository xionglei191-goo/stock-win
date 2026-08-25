from __future__ import annotations

from typing import Any

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


PROJECT_ID = "early_winner_v2"
ML_STRATEGY_ID = "early_winner_ml_v2"


class EarlyWinnerV2Strategy:
    """Development-only V2 wrapper; it can never emit platform trade signals."""

    metadata = StrategyMetadata(
        strategy_id=ML_STRATEGY_ID,
        version="2.0.0-dev1",
        name="早期强势股识别 V2 · 开发候选",
        description="修复标签范围、无信息特征和异常值处理；开发期否决前不生成候选。",
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
            DataRequirement("industry_history", "event", "none", 0, True),
            DataRequirement("financials", "quarterly", "none", 0, True),
            DataRequirement("announcements", "event", "none", 0, True),
        ),
    )

    def scan(self, **context: Any) -> StrategyScanResult:
        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=(),
            state={
                "asof": context.get("asof"),
                "status": "DEVELOPMENT_REJECTED",
                "candidate_count": 0,
                "trade_signals_enabled": False,
                "forward_validation_opened": False,
            },
        )


__all__ = ["EarlyWinnerV2Strategy", "ML_STRATEGY_ID", "PROJECT_ID"]
