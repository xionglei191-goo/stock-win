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
        strategy_plugin_dir=root / "strategy_plugins",
    )
