from __future__ import annotations

from typing import Any

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)

from .early_winner import PROJECT_ID


TRADE_STRATEGY_ID = "early_winner_trade_v1"


class EarlyWinnerTradeStrategy:
    """Sealed research shell for a formerly proposed deployment.

    Candidate creation stays in the research service. This paper-only build
    never emits platform signals and exposes no executable broker path.
    """

    metadata = StrategyMetadata(
        strategy_id=TRADE_STRATEGY_ID,
        version="1.0.0",
        name="早期强势股·部署封存",
        description="冻结研究包装；当前 paper-only 构建没有真实券商执行入口。",
        frequency="1w",
        requires_approval=True,
        lifecycle="VALIDATION_REQUIRED",
        category=StrategyCategory.INDEPENDENT,
        strategy_family=PROJECT_ID,
        scan_enabled=False,
        backtest_enabled=False,
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement("bars", "1d", "front", 180, True),
            DataRequirement("bars", "1d", "none", 180, True),
            DataRequirement("trading_calendar", "1d", "none", 0, True),
        ),
    )

    def scan(self, **context: Any) -> StrategyScanResult:
        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=(),
            state={
                "status": context.get("deployment_state", "VALIDATION_REQUIRED"),
                "trade_signals_enabled": False,
                "execution_path": "disabled_paper_only_build",
            },
        )


__all__ = ["EarlyWinnerTradeStrategy", "TRADE_STRATEGY_ID"]
