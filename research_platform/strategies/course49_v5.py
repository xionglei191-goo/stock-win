from __future__ import annotations

from dataclasses import replace
from typing import Any

from .course49 import Course49Market
from .course49_v2 import MarketStyle
from .course49_v4 import Course49V4Strategy


class Course49V5Strategy(Course49V4Strategy):
    metadata = replace(
        Course49V4Strategy.metadata,
        strategy_id="course49_v5",
        version="5.0.0",
        name="49课资金确认风险预算",
        description=(
            "完全复用V4资金确认信号，仅把高确认交易的基础风险预算从22%提高到25%；"
            "用于验证20%目标的风险收益边界，不参与默认扫描。"
        ),
        strategy_family="course49_v3",
        lifecycle="EXPERIMENTAL",
        scan_enabled=False,
        backtest_enabled=True,
    )

    def select_mode(
        self,
        market: Course49Market,
        style: MarketStyle,
        leader: dict[str, Any],
    ) -> tuple[str, float] | None:
        mode = super().select_mode(market, style, leader)
        return (mode[0], 0.25) if mode else None
