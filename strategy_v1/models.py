from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class MarketState:
    asof: datetime
    regime: Literal["NORMAL", "WEAK"]
    index_above_ma20: bool
    breadth: float
    average_return_5d: float
    passed_conditions: int


@dataclass(frozen=True)
class SectorScore:
    code: str
    name: str
    score: float
    relative_return_5d: float
    breadth: float
    volume_ratio: float
    valid_members: int


@dataclass(frozen=True)
class LeaderCandidate:
    code: str
    name: str
    sector_code: str
    sector_name: str
    sector_score: float
    leader_score: float
    leader_rank: int


@dataclass(frozen=True)
class Signal:
    timestamp: datetime
    code: str
    side: Side
    price: float
    reason: str
    market_regime: str
    sector_code: str = ""
    sector_name: str = ""
    leader_rank: int = 0
    center_lower: float | None = None
    center_upper: float | None = None

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    code: str
    quantity: int
    average_price: float
    entry_time: str
    entry_date: str
    stop_price: float
    sector_code: str = ""
    last_price: float = 0.0
    entry_fees: float = 0.0


@dataclass
class PendingOrder:
    code: str
    side: Side
    signal_time: str
    reason: str
    sector_code: str = ""
    reference_price: float = 0.0


@dataclass
class PortfolioState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    pending_orders: list[PendingOrder] = field(default_factory=list)
    last_asof: str = ""
