from __future__ import annotations

from dataclasses import replace
from typing import Any

from .course49 import Course49Market
from .course49_v2 import MarketStyle
from .course49_v8 import Course49V8Strategy


class Course49V9Strategy(Course49V8Strategy):
    """Concentration-controlled V8 cohort with a bounded risk budget."""

    metadata = replace(
        Course49V8Strategy.metadata,
        strategy_id="course49_v9",
        version="9.0.0",
        name="49课全面风险偏好低拥挤回封（留出否决）",
        description=(
            "只参与封板质量0.50至0.55、至少两次开板回封且入场前无近期龙虎榜拥挤的首板；"
            "基础权重30%，经风格适用度后实际单票目标24%，最多三只。"
        ),
        lifecycle="HISTORICAL_REJECTED",
    )

    def select_mode(
        self,
        market: Course49Market,
        style: MarketStyle,
        leader: dict[str, Any],
    ) -> tuple[str, float] | None:
        if not self.entry_allowed(market, style):
            return None
        streak = int(leader.get("streak", 0) or 0)
        quality = float(leader.get("board_quality_score", 0.0) or 0.0)
        behavior = leader.get("limit_behavior")
        open_count = int(behavior.get("open_board_count", 0) or 0) if isinstance(behavior, dict) else 0
        if streak == 1 and 0.50 <= quality < 0.55 and open_count >= 2:
            return "BROAD_RISK_ON_LOW_CROWDING_RESEAL", 0.30
        return None

    def candidate_allowed(
        self,
        streak: int,
        board_quality: float,
        confirmations: set[str],
        capital_risk: str,
    ) -> bool:
        del confirmations
        return bool(
            streak == 1
            and 0.50 <= board_quality < 0.55
            and not capital_risk
        )

    def entry_sector_reason(self) -> str:
        return "BROAD_RISK_ON_LOW_CROWDING_RESEAL"
