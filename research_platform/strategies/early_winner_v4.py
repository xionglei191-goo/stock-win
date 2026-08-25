from __future__ import annotations

from typing import Any

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


PROJECT_ID = "early_winner_v4"
ML_STRATEGY_ID = "early_winner_ml_v4"


class EarlyWinnerV4Strategy:
    """Development-only 40-session target wrapper; it cannot emit trade signals."""

    metadata = StrategyMetadata(
        strategy_id=ML_STRATEGY_ID,
        version="4.0.0-dev1",
        name="早期强势股识别 V4 · 40日状态模型",
        description=(
            "只在 2018—2023 开发区研究正向市场状态下的 40 日强势股；"
            "开发门禁通过前不读取冻结测试集、不生成候选、不接交易。"
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
            DataRequirement("bars", "1d", "front", 180, True),
            DataRequirement("bars", "1d", "none", 60, True),
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
                "status": "DATA_BUILDING",
                "candidate_count": 0,
                "trade_signals_enabled": False,
                "frozen_validation_opened": False,
            },
        )


__all__ = ["EarlyWinnerV4Strategy", "ML_STRATEGY_ID", "PROJECT_ID"]
