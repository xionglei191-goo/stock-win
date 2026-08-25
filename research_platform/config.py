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
class USPortfolioConfig:
    """Conservative US-equity execution assumptions used by research backtests.

    Commission is configurable because broker schedules differ. Regulatory sell
    fees use the current 2026 SEC/FINRA rates; frozen historical studies should
    override these with an effective-dated fee schedule when one is available.
    """

    initial_cash: float = 100_000.0
    strategy_budget_weight: float = 1.0
    max_strategy_positions: int = 10
    max_total_positions: int = 10
    max_strategy_symbol_weight: float = 0.10
    max_total_symbol_weight: float = 0.10
    fixed_stop_loss: float = 0.08
    commission_rate: float = 0.0005
    min_commission: float = 0.0
    stamp_duty_rate: float = 0.0
    slippage_rate: float = 0.0005
    board_lot: int = 1
    sec_sell_fee_rate: float = 20.60 / 1_000_000
    finra_taf_per_share: float = 0.000195
    finra_taf_cap: float = 9.79


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
    us_pit_dir: Path = field(default_factory=lambda: REPOSITORY_ROOT / "data" / "us_pit")
    us_paper_database_path: Path = field(
        default_factory=lambda: REPOSITORY_ROOT / "data" / "us_paper.db"
    )
    us_paper_runtime_database_path: Path = field(
        default_factory=lambda: REPOSITORY_ROOT / "data" / "us_paper_runtime.db"
    )
    us_paper_decision_archive_dir: Path = field(
        default_factory=lambda: REPOSITORY_ROOT / "data" / "us_paper_decisions"
    )
    us_program_database_path: Path = field(
        default_factory=lambda: REPOSITORY_ROOT / "data" / "us_momentum_program.db"
    )
    us_tdx_shadow_database_path: Path = field(
        default_factory=lambda: REPOSITORY_ROOT / "data" / "us_tdx_shadow.db"
    )
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
    us_portfolio: USPortfolioConfig = field(default_factory=USPortfolioConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)

    @property
    def tq_user_dir(self) -> Path:
        return self.tdx_root / "PYPlugins" / "user"

    @property
    def openai_api_key(self) -> str:
        return os.getenv("OPENAI_API_KEY", "")

    @property
    def trading_account(self) -> str:
        """Broker accounts are unavailable in this paper-only build."""
        return ""

    @property
    def trading_account_type(self) -> str:
        return "DISABLED"

    @property
    def trading_account_alias(self) -> str:
        return ""

    @property
    def trading_operator_token(self) -> str:
        return ""

    @property
    def live_trading_enabled(self) -> bool:
        # This repository profile is research + paper-only.  Real broker
        # writes are intentionally impossible even if a stale local
        # environment variable remains configured.
        return False

    @property
    def trading_scheduler_enabled(self) -> bool:
        return False

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.runtime_dir,
            self.cache_dir,
            self.snapshot_dir,
            self.strategy_lab_dir,
            self.us_pit_dir,
            self.us_paper_decision_archive_dir,
            self.strategy_plugin_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
