from __future__ import annotations

from dataclasses import replace
from typing import Any

from research_platform.lhb import LhbFeatures

from .course49 import Course49Market
from .course49_v2 import MarketStyle
from .course49_v6 import Course49V6Strategy


class Course49V7Strategy(Course49V6Strategy):
    """Broad-risk-on first-board reseal reward mode.

    This is deliberately separate from V6. It tests a distinct, execution-aware
    cohort and stays disabled for scans until out-of-sample validation exists.
    """

    metadata = replace(
        Course49V6Strategy.metadata,
        strategy_id="course49_v7",
        version="7.0.0",
        name="49课全面风险偏好首板回封",
        description=(
            "仅在全面风险偏好风格参与封板质量低于0.55、盘中至少两次开板后回封的首板，"
            "持有三个交易日并使用3%固定止损。"
        ),
        lifecycle="EXPERIMENTAL",
        scan_enabled=False,
        backtest_enabled=True,
    )

    def candidate_limit(self) -> int:
        return 5

    def entry_allowed(self, market: Course49Market, style: MarketStyle) -> bool:
        del market
        return bool(style.code == "BROAD_RISK_ON" and style.entry_allowed)

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
        if streak == 1 and quality < 0.55 and open_count >= 2:
            return "BROAD_RISK_ON_FIRST_BOARD_RESEAL", 0.20
        return None

    def candidate_allowed(
        self,
        streak: int,
        board_quality: float,
        confirmations: set[str],
        capital_risk: str,
    ) -> bool:
        del confirmations
        return bool(streak == 1 and board_quality < 0.55 and not capital_risk)

    def candidate_behavior_allowed(self, behavior: LhbFeatures) -> bool:
        return behavior.open_board_count >= 2

    def evaluate_exit_state(
        self,
        state: dict[str, Any],
        *,
        price: float,
        entry_price: float,
        below_ma5: bool,
        market_weak: bool,
        sector_weak: bool,
        leader_weak: bool,
        immediate_reason: str,
    ) -> tuple[dict[str, Any], str]:
        del below_ma5, market_weak, sector_weak, leader_weak
        current = {
            **state,
            "holding_days": int(state.get("holding_days", 0) or 0) + 1,
            "entry_price": float(state.get("entry_price", entry_price) or entry_price),
            "max_close": max(float(state.get("max_close", entry_price) or entry_price), price),
        }
        if immediate_reason in {"FIXED_STOP", "CAPITAL_DISTRIBUTION"}:
            return current, immediate_reason
        if current["holding_days"] >= 3:
            return current, "BROAD_FIRST_BOARD_TIME_EXIT"
        return current, ""

    def entry_sector_reason(self) -> str:
        return "BROAD_RISK_ON_FIRST_BOARD_RESEAL"
