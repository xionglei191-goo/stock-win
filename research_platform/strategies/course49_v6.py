from __future__ import annotations

from dataclasses import replace
from typing import Any

from .course49 import Course49Market
from .course49_v2 import MarketStyle
from .course49_v5 import Course49V5Strategy


class Course49V6Strategy(Course49V5Strategy):
    metadata = replace(
        Course49V5Strategy.metadata,
        strategy_id="course49_v6",
        version="6.3.0",
        name="49课小盘加速首板",
        description=(
            "只在小盘投机风格的加速期参与质量0.55至0.70的首板，"
            "以五个持有交易日退出，并用3%结构止损约束首板失败的亏损尾部。"
        ),
        strategy_family="course49_v3",
        lifecycle="EXPERIMENTAL",
        scan_enabled=False,
        backtest_enabled=True,
    )

    def candidate_minimum_streak(self) -> int:
        return 1

    def candidate_limit(self) -> int:
        return 10

    def stop_loss_ratio(self) -> float:
        return 0.03

    def entry_allowed(self, market: Course49Market, style: MarketStyle) -> bool:
        return bool(
            market.entry_allowed
            and market.phase == "ACCELERATION"
            and style.code == "SMALL_CAP_SPECULATION"
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
        if streak == 1 and 0.55 <= quality < 0.70:
            return "SMALL_CAP_ACCELERATION_FIRST_BOARD", 0.30
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
            and 0.55 <= board_quality < 0.70
            and not capital_risk
        )

    def capital_allowed(self, capital: Any) -> bool:
        return not bool(capital and capital.risk)

    def candidate_score(
        self,
        *,
        board_quality: float,
        streak: int,
        continuation_rate: float,
        capital_score: float,
        first_limit_score: float,
        historical_premium: float,
    ) -> float:
        del streak
        del board_quality
        return float(
            first_limit_score * 0.30
            + capital_score * 0.20
            + continuation_rate * 0.20
            + historical_premium * 0.30
        )

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
        if current["holding_days"] >= 5:
            return current, "FIRST_BOARD_TIME_EXIT"
        return current, ""

    def entry_sector_reason(self) -> str:
        return "SMALL_CAP_ACCELERATION_FIRST_BOARD"
