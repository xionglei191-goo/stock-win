from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from .config import PlatformConfig
from .models import RuntimeAdapter, StrategyMetadata
from .strategies import create_strategy_registry


PLUGIN_API_VERSION = "1"
_STRATEGY_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class PluginLoadIssue:
    plugin_id: str
    origin: str
    code: str
    message: str

    def as_record(self) -> dict[str, str]:
        return asdict(self)


def load_strategy_registry(
    config: PlatformConfig,
) -> tuple[dict[str, Any], tuple[PluginLoadIssue, ...]]:
    strategies = create_strategy_registry()
    for strategy in strategies.values():
        _set_origin(strategy, "builtin")

    from .strategy_lab import load_promoted_strategies

    issues: list[PluginLoadIssue] = []
    for strategy_id, strategy in load_promoted_strategies(config).items():
        if strategy_id in strategies:
            issues.append(
                PluginLoadIssue(
                    strategy_id,
                    "strategy_lab",
                    "DUPLICATE_STRATEGY_ID",
                    f"Strategy id '{strategy_id}' is already registered",
                )
            )
            continue
        try:
            _validate_strategy(strategy)
        except ValueError as exc:
            issues.append(
                PluginLoadIssue(strategy_id, "strategy_lab", "INVALID_CONTRACT", str(exc))
            )
            continue
        _set_origin(strategy, "strategy_lab")
        strategies[strategy_id] = strategy

    local, local_issues = load_local_strategy_plugins(config.strategy_plugin_dir, strategies)
    strategies.update(local)
    issues.extend(local_issues)
    return strategies, tuple(issues)


def load_local_strategy_plugins(
    plugin_root: Path,
    registered: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[PluginLoadIssue, ...]]:
    plugin_root.mkdir(parents=True, exist_ok=True)
    existing = set((registered or {}).keys())
    loaded: dict[str, Any] = {}
    issues: list[PluginLoadIssue] = []
    for manifest_path in sorted(plugin_root.glob("*/plugin.json")):
        origin = str(manifest_path.parent)
        plugin_id = manifest_path.parent.name
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("plugin.json must contain a JSON object")
            plugin_id = str(manifest.get("strategy_id") or plugin_id)
            if not bool(manifest.get("enabled", True)):
                continue
            api_version = str(manifest.get("api_version", ""))
            if api_version != PLUGIN_API_VERSION:
                raise ValueError(
                    f"Unsupported plugin API '{api_version}'; expected '{PLUGIN_API_VERSION}'"
                )
            strategy = _load_entrypoint(manifest_path.parent, str(manifest.get("entrypoint", "")))
            _validate_strategy(strategy)
            if strategy.metadata.runtime_adapter != RuntimeAdapter.GENERIC_DAILY:
                raise ValueError(
                    "Local plugins must use the generic_daily runtime adapter"
                )
            if strategy.metadata.strategy_id != plugin_id:
                raise ValueError(
                    "Manifest strategy_id does not match StrategyMetadata.strategy_id"
                )
            if plugin_id in existing or plugin_id in loaded:
                raise ValueError(f"Strategy id '{plugin_id}' is already registered")
            _set_origin(strategy, f"local:{manifest_path.parent.name}")
            loaded[plugin_id] = strategy
        except Exception as exc:
            code = (
                "DUPLICATE_STRATEGY_ID"
                if "already registered" in str(exc)
                else "PLUGIN_LOAD_FAILED"
            )
            issues.append(PluginLoadIssue(plugin_id, origin, code, str(exc)))
    return loaded, tuple(issues)


def _load_entrypoint(plugin_dir: Path, entrypoint: str) -> Any:
    if ":" not in entrypoint:
        raise ValueError("entrypoint must use 'relative_file.py:create_strategy' format")
    relative_file, factory_name = entrypoint.rsplit(":", 1)
    source_path = (plugin_dir / relative_file).resolve()
    plugin_dir_resolved = plugin_dir.resolve()
    if plugin_dir_resolved not in source_path.parents or source_path.suffix != ".py":
        raise ValueError("entrypoint source must be a Python file inside the plugin directory")
    if not source_path.is_file():
        raise ValueError(f"Entrypoint source does not exist: {relative_file}")
    module = _load_module(source_path)
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError(f"Entrypoint factory '{factory_name}' is not callable")
    strategy = factory()
    if isinstance(strategy, Iterable) and not isinstance(strategy, (str, bytes, dict)):
        strategies = list(strategy)
        if len(strategies) != 1:
            raise ValueError("Each plugin manifest must create exactly one strategy")
        strategy = strategies[0]
    return strategy


def _load_module(source_path: Path) -> ModuleType:
    digest = hashlib.sha256(
        f"{source_path}:{source_path.stat().st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    module_name = f"research_platform_local_plugin_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load plugin source: {source_path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _validate_strategy(strategy: Any) -> None:
    metadata = getattr(strategy, "metadata", None)
    if not isinstance(metadata, StrategyMetadata):
        raise ValueError("Strategy must expose a StrategyMetadata instance")
    if not _STRATEGY_ID.fullmatch(metadata.strategy_id):
        raise ValueError("strategy_id must match ^[a-z][a-z0-9_]{2,63}$")
    if not _SEMVER.fullmatch(metadata.version):
        raise ValueError("strategy version must use semantic versioning, for example 1.0.0")
    if metadata.plugin_api_version != PLUGIN_API_VERSION:
        raise ValueError(
            f"Strategy API '{metadata.plugin_api_version}' does not match '{PLUGIN_API_VERSION}'"
        )
    try:
        RuntimeAdapter(metadata.runtime_adapter)
    except ValueError as exc:
        raise ValueError(f"Unsupported runtime adapter '{metadata.runtime_adapter}'") from exc
    if not callable(getattr(strategy, "scan", None)):
        raise ValueError("Strategy must implement scan(**context)")
    if not metadata.data_requirements:
        raise ValueError("Strategy must declare at least one data requirement")


def _set_origin(strategy: Any, origin: str) -> None:
    setattr(strategy, "__plugin_origin__", origin)
