from dataclasses import replace
from typing import Any

from .base import Strategy
from .chan_strategy import ChanStrategy
from .course49 import Course49Strategy, build_course49_market_matrix
from .course49_v2 import (
    Course49V2Strategy,
    build_course49_eligibility_matrix,
    build_course49_feature_matrix,
)
from .course49_v3 import (
    Course49V3Strategy,
    build_course49_v3_candidate_matrix,
    select_trade_mode_v3,
)
from .course49_v4 import Course49V4Strategy
from .course49_v5 import Course49V5Strategy
from .course49_v6 import Course49V6Strategy
from .course49_v7 import Course49V7Strategy
from .course49_v8 import Course49V8Strategy
from .course49_v9 import Course49V9Strategy
from .course49_v10 import Course49V10Strategy
from .course49_v11 import Course49V11Strategy
from .course49_system import Course49SystemStrategy
from .pairs_arbitrage import DEFAULT_PAIRS, PairSpec, PairsArbitrageStrategy
from .weekly_triangle import (
    WeeklyTriangleParameters,
    WeeklyTriangleStrategy,
    analyze_weekly_triangle,
    completed_weekly_bars,
    resample_weekly_bars,
)
from .weekly_bull_platform import (
    WeeklyBullPlatformParameters,
    WeeklyBullPlatformStrategy,
    analyze_weekly_bull_platform,
    is_a_share_stock_code,
)
from .early_winner import (
    EarlyWinnerMLStrategy,
    EarlyWinnerRuleStrategy,
)
from .early_winner_trade import EarlyWinnerTradeStrategy
from .us_momentum import USMomentumStrategy, USMomentumParameters


def create_strategy_registry() -> dict[str, Any]:
    plugins = (
        ChanStrategy(),
        Course49Strategy(),
        Course49V2Strategy(),
        Course49V3Strategy(),
        Course49V4Strategy(),
        Course49V5Strategy(),
        Course49V6Strategy(),
        Course49V7Strategy(),
        Course49V8Strategy(),
        Course49V9Strategy(),
        Course49V10Strategy(),
        Course49V11Strategy(),
        Course49SystemStrategy(),
        PairsArbitrageStrategy(),
        WeeklyTriangleStrategy(),
        WeeklyBullPlatformStrategy(),
        EarlyWinnerRuleStrategy(),
        EarlyWinnerMLStrategy(),
        EarlyWinnerTradeStrategy(),
        USMomentumStrategy(),
    )
    registry = {plugin.metadata.strategy_id: plugin for plugin in plugins}
    for strategy_id, plugin in registry.items():
        if strategy_id.startswith("course49_v") and strategy_id[10:].isdigit():
            plugin.metadata = replace(
                plugin.metadata,
                archived=True,
                scan_enabled=False,
            )
    return registry

__all__ = [
    "Strategy",
    "ChanStrategy",
    "Course49Strategy",
    "build_course49_market_matrix",
    "Course49V2Strategy",
    "Course49V3Strategy",
    "Course49V4Strategy",
    "Course49V5Strategy",
    "Course49V6Strategy",
    "Course49V7Strategy",
    "Course49V8Strategy",
    "Course49V9Strategy",
    "Course49V10Strategy",
    "Course49V11Strategy",
    "Course49SystemStrategy",
    "select_trade_mode_v3",
    "build_course49_eligibility_matrix",
    "build_course49_feature_matrix",
    "build_course49_v3_candidate_matrix",
    "DEFAULT_PAIRS",
    "PairSpec",
    "PairsArbitrageStrategy",
    "WeeklyTriangleParameters",
    "WeeklyTriangleStrategy",
    "analyze_weekly_triangle",
    "completed_weekly_bars",
    "resample_weekly_bars",
    "create_strategy_registry",
    "WeeklyBullPlatformParameters",
    "WeeklyBullPlatformStrategy",
    "analyze_weekly_bull_platform",
    "is_a_share_stock_code",
    "EarlyWinnerRuleStrategy",
    "EarlyWinnerMLStrategy",
    "EarlyWinnerTradeStrategy",
    "USMomentumStrategy",
    "USMomentumParameters",
]
