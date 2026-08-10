from __future__ import annotations

from dataclasses import replace
from typing import Any

from research_platform.lhb import LhbFeatures

from .course49 import Course49Market
from .course49_v2 import MarketStyle
from .course49_v3 import CORE_CONFIRMATIONS, Course49V3Strategy


class Course49V4Strategy(Course49V3Strategy):
    metadata = replace(
        Course49V3Strategy.metadata,
        strategy_id="course49_v4",
        version="4.0.0",
        name="49课资金确认加速",
        description=(
            "只参与龙虎榜净买确认的10%涨停制度二至四板空间核心；"
            "排除成长和大盘主导风格，并以更集中的风险预算验证资金确认溢价。"
        ),
        strategy_family="course49_v3",
    )

    def entry_allowed(self, market: Course49Market, style: MarketStyle) -> bool:
        return bool(
            market.entry_allowed
            and market.phase == "ACCELERATION"
            and style.code not in {"UNKNOWN", "GROWTH_TREND", "LARGE_CAP_TREND"}
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
        rank = int(leader.get("leader_rank", 99) or 99)
        role = str(leader.get("role", ""))
        board_score = float(leader.get("board_quality_score", 0.0) or 0.0)
        behavior = leader.get("limit_behavior")
        confirmations = {
            str(item) for item in behavior.get("confirmations", [])
        } if isinstance(behavior, dict) else set()
        if rank != 1 or role != "SPACE_LEADER" or not confirmations & CORE_CONFIRMATIONS:
            return None
        if streak == 4 and board_score >= 0.65:
            return "CAPITAL_CONFIRMED_FOURTH_BOARD", 0.22
        if streak in {2, 3} and board_score >= 0.75:
            return "CAPITAL_CONFIRMED_CORE", 0.22
        return None

    def candidate_allowed(
        self,
        streak: int,
        board_quality: float,
        confirmations: set[str],
        capital_risk: str,
    ) -> bool:
        quality_ok = (
            streak == 4 and board_quality >= 0.65
        ) or (
            streak in {2, 3} and board_quality >= 0.75
        )
        return bool(
            quality_ok
            and confirmations & CORE_CONFIRMATIONS
            and not capital_risk
        )

    def capital_allowed(self, capital: LhbFeatures | None) -> bool:
        return bool(
            capital is not None
            and capital.listed
            and not capital.risk
            and capital.net_buy_ratio is not None
            and capital.net_buy_ratio >= 0.05
        )

    def entry_sector_reason(self) -> str:
        return "CAPITAL_CONFIRMED_ACCELERATION"
