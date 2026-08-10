from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class DataFreshnessConfig:
    sector_cache_days: int = 7
    intraday_bar_minutes: int = 30
    minimum_daily_bars: int = 60
    minimum_intraday_bars: int = 20


@dataclass(frozen=True)
class PortfolioConfig:
    initial_cash: float = 100_000.0
    strategy_budget_weight: float = 0.50
    max_strategy_positions: int = 3
    max_total_positions: int = 5
    max_strategy_symbol_weight: float = 0.40
    max_total_symbol_weight: float = 0.20
    fixed_stop_loss: float = 0.05
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.001
    board_lot: int = 100


@dataclass(frozen=True)
class PerformanceConfig:
    worker_threads: int = field(
        default_factory=lambda: max(1, int(os.getenv("RESEARCH_WORKER_THREADS", "8")))
    )
    bar_batch_size: int = field(
        default_factory=lambda: max(100, int(os.getenv("RESEARCH_BAR_BATCH_SIZE", "800")))
    )
    event_batch_size: int = field(
        default_factory=lambda: max(100, int(os.getenv("RESEARCH_EVENT_BATCH_SIZE", "500")))
    )
    minimum_batch_size: int = 100
    memory_cache_bytes: int = field(
        default_factory=lambda: max(
            0, int(float(os.getenv("RESEARCH_MEMORY_CACHE_GB", "24")) * 1024**3)
        )
    )
    minimum_available_memory_bytes: int = field(
        default_factory=lambda: max(
            0, int(float(os.getenv("RESEARCH_MIN_AVAILABLE_MEMORY_GB", "8")) * 1024**3)
        )
    )
    disk_cache_bytes: int = field(
        default_factory=lambda: max(
            0, int(float(os.getenv("RESEARCH_DISK_CACHE_GB", "50")) * 1024**3)
        )
    )
    max_backtest_workers: int = field(
        default_factory=lambda: max(1, int(os.getenv("RESEARCH_BACKTEST_WORKERS", "3")))
    )


@dataclass(frozen=True)
class PlatformConfig:
    repository_root: Path = REPOSITORY_ROOT
    tdx_root: Path = field(
        default_factory=lambda: Path(os.getenv("TDX_ROOT", r"D:\Project\stock\tdx-mock"))
    )
    runtime_dir: Path = field(default_factory=lambda: REPOSITORY_ROOT / "data")
    database_path: Path = field(default_factory=lambda: REPOSITORY_ROOT / "data" / "research.db")
    cache_dir: Path = field(default_factory=lambda: REPOSITORY_ROOT / "data" / "cache")
    snapshot_dir: Path = field(default_factory=lambda: REPOSITORY_ROOT / "data" / "snapshots")
    frontend_dist: Path = field(default_factory=lambda: REPOSITORY_ROOT / "frontend" / "dist")
    strategy_lab_dir: Path = field(default_factory=lambda: REPOSITORY_ROOT / "data" / "strategy_lab")
    strategy_plugin_dir: Path = field(
        default_factory=lambda: REPOSITORY_ROOT / "strategy_plugins"
    )
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-5.6-terra"))
    openai_timeout_seconds: float = 60.0
    openai_max_retries: int = 2
    timezone: str = "Asia/Shanghai"
    host: str = "127.0.0.1"
    port: int = 8000
    freshness: DataFreshnessConfig = field(default_factory=DataFreshnessConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)

    @property
    def tq_user_dir(self) -> Path:
        return self.tdx_root / "PYPlugins" / "user"

    @property
    def openai_api_key(self) -> str:
        return os.getenv("OPENAI_API_KEY", "")

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.runtime_dir,
            self.cache_dir,
            self.snapshot_dir,
            self.strategy_lab_dir,
            self.strategy_plugin_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
