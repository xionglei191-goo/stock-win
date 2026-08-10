from __future__ import annotations

from dataclasses import replace

from research_platform.lhb import LhbFeatures

from .course49_v9 import Course49V9Strategy


MIN_OPEN_BOARD_COUNT = 3


class Course49V11Strategy(Course49V9Strategy):
    """V9 cohort requiring repeated intraday rejection and recovery."""

    metadata = replace(
        Course49V9Strategy.metadata,
        strategy_id="course49_v11",
        version="11.0.0",
        name="49课三次开板回封",
        description=(
            "保留V9的全面风险偏好、低拥挤和0.50至0.55封板质量规则，"
            "只参与盘中至少三次开板后仍能回封的首板。"
        ),
        lifecycle="HISTORICAL_ROBUSTNESS_REJECTED",
        scan_enabled=False,
        backtest_enabled=True,
    )

    def candidate_behavior_allowed(self, behavior: LhbFeatures) -> bool:
        return behavior.open_board_count >= MIN_OPEN_BOARD_COUNT
