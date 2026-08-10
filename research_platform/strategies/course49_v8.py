from __future__ import annotations

from dataclasses import replace

from research_platform.lhb import LhbFeatures

from .course49_v7 import Course49V7Strategy


class Course49V8Strategy(Course49V7Strategy):
    """V7 reward mode with a point-in-time crowding veto."""

    metadata = replace(
        Course49V7Strategy.metadata,
        strategy_id="course49_v8",
        version="8.0.0",
        name="49课全面风险偏好低拥挤回封",
        description=(
            "在V7首板回封模式上排除入场时最近十日已有龙虎榜记录的股票，"
            "避免把资金一致性接力混入低位补涨收益源。"
        ),
    )

    def capital_allowed(self, capital: LhbFeatures | None) -> bool:
        return capital is None
