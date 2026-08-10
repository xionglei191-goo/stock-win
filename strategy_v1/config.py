from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .chan import ChanParameters


PROJECT_ROOT = Path(__file__).resolve().parent
TDX_ROOT = Path(r"D:\Project\stock\tdx-mock")


@dataclass(frozen=True)
class CostConfig:
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.001


@dataclass(frozen=True)
class RiskConfig:
    initial_cash: float = 100_000.0
    max_positions: int = 5
    max_position_weight: float = 0.20
    risk_per_trade: float = 0.01
    fixed_stop_loss: float = 0.05
    board_lot: int = 100


@dataclass(frozen=True)
class StrategyConfig:
    tdx_root: Path = TDX_ROOT
    output_dir: Path = PROJECT_ROOT / "outputs"
    cache_dir: Path = PROJECT_ROOT / "outputs" / "cache"
    candidate_block: str = "V1_CANDIDATES"
    daily_lookback: int = 90
    intraday_lookback: int = 320
    batch_size: int = 160
    minimum_listing_bars: int = 60
    minimum_average_turnover: float = 20_000_000.0
    market_breadth_floor: float = 0.45
    market_return_floor: float = -0.01
    top_sector_count: int = 5
    leaders_per_sector: int = 2
    min_sector_members: int = 3
    sector_markets: tuple[str, ...] = ("11", "12")
    chan: ChanParameters = field(default_factory=ChanParameters)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)

    @property
    def tq_user_dir(self) -> Path:
        return self.tdx_root / "PYPlugins" / "user"

    def ensure_runtime_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
