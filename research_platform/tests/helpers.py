from __future__ import annotations

from pathlib import Path

from research_platform.config import PlatformConfig


def temporary_config(root: Path) -> PlatformConfig:
    return PlatformConfig(
        repository_root=root,
        tdx_root=root / "tdx",
        runtime_dir=root / "data",
        database_path=root / "data" / "research.db",
        cache_dir=root / "data" / "cache",
        snapshot_dir=root / "data" / "snapshots",
        frontend_dist=root / "frontend" / "dist",
        strategy_lab_dir=root / "data" / "strategy_lab",
        us_pit_dir=root / "data" / "us_pit",
        us_paper_database_path=root / "data" / "us_paper.db",
        us_paper_runtime_database_path=root / "data" / "us_paper_runtime.db",
        us_program_database_path=root / "data" / "us_momentum_program.db",
        us_tdx_shadow_database_path=root / "data" / "us_tdx_shadow.db",
        strategy_plugin_dir=root / "strategy_plugins",
    )
