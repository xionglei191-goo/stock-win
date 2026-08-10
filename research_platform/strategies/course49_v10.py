from __future__ import annotations

from dataclasses import replace

from .course49 import Course49Market
from .course49_v2 import MarketStyle
from .course49_v9 import Course49V9Strategy


MIN_MARKET_SCORE_CHANGE_3D = 0.06


class Course49V10Strategy(Course49V9Strategy):
    """V9 cohort gated by a rising, point-in-time market reward regime."""

    metadata = replace(
        Course49V9Strategy.metadata,
        strategy_id="course49_v10",
        version="10.0.0",
        name="49课市场奖励过滤首板回封",
        description=(
            "保留V9低拥挤首板回封形态，仅在BROAD_RISK_ON且市场生态分数"
            "相对三个交易日前至少上升0.06时入场。"
        ),
        lifecycle="HOLDOUT_TARGET_REJECTED",
        scan_enabled=False,
        backtest_enabled=True,
    )

    def entry_allowed(self, market: Course49Market, style: MarketStyle) -> bool:
        return bool(
            super().entry_allowed(market, style)
            and market.score_change_3d >= MIN_MARKET_SCORE_CHANGE_3D
        )

    def entry_block_reason(self, market: Course49Market, style: MarketStyle) -> str:
        parent_reason = super().entry_block_reason(market, style)
        if parent_reason:
            return parent_reason
        if market.score_change_3d < MIN_MARKET_SCORE_CHANGE_3D:
            return "market_reward_momentum_below_threshold"
        return ""
