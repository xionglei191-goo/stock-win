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
from .pairs_arbitrage import DEFAULT_PAIRS, PairSpec, PairsArbitrageStrategy


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
        PairsArbitrageStrategy(),
    )
    return {plugin.metadata.strategy_id: plugin for plugin in plugins}

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
    "select_trade_mode_v3",
    "build_course49_eligibility_matrix",
    "build_course49_feature_matrix",
    "build_course49_v3_candidate_matrix",
    "DEFAULT_PAIRS",
    "PairSpec",
    "PairsArbitrageStrategy",
    "create_strategy_registry",
]
