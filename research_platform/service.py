from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from strategy_v1.config import StrategyConfig
from strategy_v1.market import evaluate_market_regime, filter_universe, rank_leaders, rank_sectors
from strategy_v1.portfolio import price_limit_ratio

from .config import PlatformConfig
from .composition import (
    CompositionEngine,
    CompositionMode,
    StrategyCatalog,
    StrategyGroupDefinition,
    built_in_groups,
)
from .backtest_engine import (
    _records_hash,
    _resolve_sampling_mode,
    _sector_membership_rows,
    _stock_pool_hash,
    _stratified_sample,
    _universe_distribution,
    _validate_scan_result,
)
from .course49_market import flatten_market_activity, normalize_market_activity
from .data import ResearchDataHub, TdxProvider
from .data_cache import DataCacheManager
from .data_plan import build_data_plan, required_bar_lookback
from .early_winner_research import EarlyWinnerResearchService
from .early_winner_v2_research import EarlyWinnerV2ResearchService
from .early_winner_v3_research import EarlyWinnerV3ResearchService
from .early_winner_v4_research import EarlyWinnerV4ResearchService
from .early_winner_v5_research import EarlyWinnerV5ResearchService
from .early_winner_v6_research import EarlyWinnerV6ResearchService
from .early_winner_trading import EarlyWinnerTradingService
from .lhb import flatten_lhb_history, normalize_lhb_history
from .validation_gates import run_validation_gates
from .models import (
    DataHealth,
    DataStatus,
    ExecutionModel,
    RunStatus,
    RuntimeAdapter,
    ScanReport,
    SignalStatus,
    StrategyScanResult,
)
from .plugin_loader import load_strategy_registry
from .portfolio import PaperPortfolio
from .storage import Database, ParquetSnapshotStore
from .strategies.course49_system import (
    POLICY_VERSION,
    framework_metadata,
    production_playbooks,
)
from .weekly_triangle_observations import WeeklyTriangleObservationService
from .us_market_calendar import is_nyse_month_end


class PlatformService:
    def __init__(self, config: PlatformConfig | None = None):
        self.config = config or PlatformConfig()
        self.database = Database(self.config)
        self.database.initialize()
        self.snapshots = ParquetSnapshotStore(self.config, self.database)
        self.data_cache = DataCacheManager(self.config, self.database)
        self.data_hub = ResearchDataHub(self.config)
        self.portfolio = PaperPortfolio(self.config, self.database)
        self.weekly_triangle_observations = WeeklyTriangleObservationService(
            self.config,
            self.database,
        )
        self.early_winner = EarlyWinnerResearchService(self.config, self.database)
        self.early_winner_v2 = EarlyWinnerV2ResearchService(self.config, self.database)
        self.early_winner_v3 = EarlyWinnerV3ResearchService(self.config, self.database)
        self.early_winner_v4 = EarlyWinnerV4ResearchService(self.config, self.database)
        self.early_winner_v5 = EarlyWinnerV5ResearchService(self.config, self.database)
        self.early_winner_v6 = EarlyWinnerV6ResearchService(self.config, self.database)
        self.strategies, self.plugin_issues = load_strategy_registry(self.config)
        self.strategies.setdefault(
            self.early_winner_v6.strategy.metadata.strategy_id,
            self.early_winner_v6.strategy,
        )
        for strategy in self.strategies.values():
            self.database.register_strategy(
                strategy.metadata,
                getattr(strategy, "__plugin_origin__", "builtin"),
            )
        self.early_winner_trading = EarlyWinnerTradingService(self.config, self.database)
        self._register_frameworks()
        for group in built_in_groups():
            self.database.upsert_strategy_group(group)
        self.catalog = StrategyCatalog(self.strategies, self.database.load_strategy_groups())
        self.composition = CompositionEngine()

    def _register_frameworks(self) -> None:
        strategy = self.strategies.get("course49_system")
        if strategy is None:
            return
        self.database.register_framework(
            framework_metadata().as_record(),
            [item.metadata.as_record() for item in strategy.playbooks],
            policy_version=POLICY_VERSION,
        )

    def refresh_catalog(self) -> None:
        self.catalog = StrategyCatalog(self.strategies, self.database.load_strategy_groups())

    def _runtime_adapter(self, strategy_id: str) -> RuntimeAdapter:
        return RuntimeAdapter(self.strategies[strategy_id].metadata.runtime_adapter)

    def _strategy_family(self, strategy_id: str) -> str:
        metadata = self.strategies[strategy_id].metadata
        return metadata.strategy_family or strategy_id

    def _required_codes(self, strategy_id: str) -> tuple[str, ...]:
        values = getattr(self.strategies[strategy_id], "required_codes", ())
        return tuple(str(item) for item in values)

    def _strategy_market(self, strategy_id: str) -> str:
        asset_classes = {
            str(item).strip().upper()
            for item in self.strategies[strategy_id].metadata.asset_classes
            if str(item).strip()
        }
        markets: set[str] = set()
        if any(item.startswith("US_") for item in asset_classes):
            markets.add("US")
        if "A_STOCK" in asset_classes or any(item.startswith("CN_") for item in asset_classes):
            markets.add("CN")
        if len(markets) > 1:
            raise ValueError(
                f"Strategy '{strategy_id}' declares assets from multiple markets: "
                f"{', '.join(sorted(asset_classes))}"
            )
        return next(iter(markets), "CN")

    def _scan_market(self, strategy_ids: tuple[str, ...]) -> str:
        markets = {self._strategy_market(strategy_id) for strategy_id in strategy_ids}
        if len(markets) != 1:
            raise ValueError(
                "A single scan cannot combine CN and US strategies; use separate runs"
            )
        return next(iter(markets))

    def _us_benchmark_codes(self, strategy_ids: tuple[str, ...]) -> tuple[str, ...]:
        codes = ["SPY.US"]
        for strategy_id in strategy_ids:
            parameters = getattr(self.strategies[strategy_id], "parameters", None)
            market_code = str(getattr(parameters, "market_code", "") or "").strip()
            if not market_code:
                continue
            if not market_code.endswith(".US"):
                raise ValueError(
                    f"US strategy '{strategy_id}' declares a non-US market benchmark: "
                    f"{market_code}"
                )
            codes.append(market_code)
        return tuple(dict.fromkeys(codes))

    @staticmethod
    def _is_us_month_end(index: Any) -> bool:
        return is_nyse_month_end(index)

    def reload_strategies(self) -> dict[str, Any]:
        strategies, issues = load_strategy_registry(self.config)
        strategies.setdefault(
            self.early_winner_v6.strategy.metadata.strategy_id,
            self.early_winner_v6.strategy,
        )
        catalog = StrategyCatalog(strategies, self.database.load_strategy_groups())
        self.strategies = strategies
        self.plugin_issues = issues
        self.catalog = catalog
        for strategy in strategies.values():
            self.database.register_strategy(
                strategy.metadata,
                getattr(strategy, "__plugin_origin__", "builtin"),
            )
        self._register_frameworks()
        for strategy in self.strategies.values():
            self.database.register_strategy(
                strategy.metadata,
                getattr(strategy, "__plugin_origin__", "builtin"),
            )
        self._register_frameworks()
        self.early_winner_trading = EarlyWinnerTradingService(self.config, self.database)
        return self.strategy_catalog()

    def strategy_catalog(self) -> dict[str, Any]:
        payload = self.catalog.as_records()
        payload["frameworks"] = self.frameworks()
        payload["plugin_issues"] = [
            *[issue.as_record() for issue in self.plugin_issues],
            *self.catalog.group_issues,
        ]
        for strategy in [
            *payload["strategies"],
            *payload.get("archived_strategies", []),
        ]:
            requirements = strategy.get("data_requirements", [])
            if not isinstance(requirements, list):
                continue
            for requirement in requirements:
                if not isinstance(requirement, dict):
                    continue
                try:
                    source = self.data_hub.sources.resolve(str(requirement["dataset"]))
                    requirement["provider"] = source.provider
                    requirement["cacheable"] = source.cacheable
                    requirement["available"] = source.available
                except KeyError:
                    requirement["provider"] = "unregistered"
                    requirement["cacheable"] = False
                    requirement["available"] = False
        return payload

    def frameworks(self) -> list[dict[str, Any]]:
        frameworks = self.database.query(
            "SELECT * FROM strategy_frameworks WHERE enabled=1 ORDER BY framework_id"
        )
        playbooks = self.database.query(
            "SELECT * FROM strategy_playbooks WHERE enabled=1 ORDER BY framework_id, playbook_id"
        )
        for row in playbooks:
            try:
                row["data_requirements"] = json.loads(
                    str(row.pop("data_requirements_json", "[]"))
                )
            except json.JSONDecodeError:
                row["data_requirements"] = []
        for framework in frameworks:
            framework["playbooks"] = [
                item for item in playbooks if item["framework_id"] == framework["framework_id"]
            ]
        return frameworks

    def framework_detail(self, framework_id: str) -> dict[str, Any]:
        frameworks = [item for item in self.frameworks() if item["framework_id"] == framework_id]
        if not frameworks:
            raise KeyError(framework_id)
        framework = frameworks[0]
        recent_runs = self.database.query(
            """SELECT * FROM runs WHERE status='SUCCEEDED' AND run_type='scan'
            ORDER BY finished_at DESC LIMIT 100"""
        )
        run = None
        state: dict[str, Any] = {}
        candidates: list[dict[str, Any]] = []
        for candidate_run in recent_runs:
            try:
                metadata = json.loads(str(candidate_run.get("metadata_json") or "{}"))
            except json.JSONDecodeError:
                continue
            strategy_states = metadata.get("strategies") or {}
            candidate_state = strategy_states.get(framework["strategy_id"]) or {}
            if candidate_state:
                run = candidate_run
                state = candidate_state
                framework_candidates = metadata.get("framework_candidates") or {}
                candidates = list(framework_candidates.get(framework_id) or [])
                break
        signals = self.database.query(
            """SELECT * FROM signals WHERE framework_id=?
            ORDER BY generated_at DESC LIMIT 100""",
            (framework_id,),
        )
        positions = self.database.query(
            "SELECT * FROM paper_positions WHERE strategy_id=? ORDER BY code",
            (framework["strategy_id"],),
        )
        runtime_states = self.database.query(
            """SELECT scope, asof, state_json FROM strategy_runtime_states
            WHERE strategy_id=? ORDER BY scope""",
            (framework["strategy_id"],),
        )
        backtest_rows = self.database.query(
            """SELECT b.* FROM backtests b
            WHERE EXISTS (
                SELECT 1 FROM backtest_states s
                WHERE s.backtest_id=b.backtest_id AND s.strategy_id=?
            )
            ORDER BY b.started_at DESC LIMIT 1""",
            (framework["strategy_id"],),
        )
        backtest_id = str(backtest_rows[0]["backtest_id"]) if backtest_rows else ""
        history = (
            self.database.query(
                """SELECT * FROM backtest_states WHERE backtest_id=? AND strategy_id=?
                ORDER BY timestamp DESC LIMIT 120""",
                (backtest_id, framework["strategy_id"]),
            )
            if backtest_id
            else []
        )
        playbook_history = (
            self.database.query(
                """SELECT * FROM backtest_playbook_states
                WHERE backtest_id=? AND strategy_id=?
                ORDER BY timestamp DESC, playbook_id LIMIT 360""",
                (backtest_id, framework["strategy_id"]),
            )
            if backtest_id
            else []
        )
        if not state and history:
            try:
                fallback_state = json.loads(str(history[0].get("state_json") or "{}"))
            except (json.JSONDecodeError, TypeError):
                fallback_state = {}
            if isinstance(fallback_state, dict):
                state = fallback_state
                candidates = list(fallback_state.get("route_audit") or [])
                state["context_source"] = "latest_backtest"
        return {
            **framework,
            "latest_run": run,
            "state": state,
            "candidates": candidates,
            "signals": signals,
            "positions": positions,
            "runtime_states": runtime_states,
            "history": history,
            "playbook_history": playbook_history,
            "latest_backtest": backtest_rows[0] if backtest_rows else None,
        }

    def save_strategy_group(self, group: StrategyGroupDefinition) -> None:
        group.validate(self.strategies)
        if group.composition_mode not in {
            CompositionMode.CAPITAL_SLEEVES,
            CompositionMode.COMPARISON,
        } and any(
            self.strategies[member.strategy_id].metadata.execution_model.value == "MULTI_LEG"
            for member in group.members
        ):
            raise ValueError("Multi-leg strategies can only use capital_sleeves composition")
        self.database.upsert_strategy_group(group)
        self.refresh_catalog()

    def delete_strategy_group(self, group_id: str) -> None:
        self.database.delete_strategy_group(group_id)
        self.refresh_catalog()

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        checks.append({"name": "windows", "ok": sys.platform == "win32", "detail": sys.platform})
        checks.append(
            {
                "name": "tdx_executable",
                "ok": (self.config.tdx_root / "TdxW.exe").exists(),
                "detail": str(self.config.tdx_root / "TdxW.exe"),
            }
        )
        checks.append(
            {
                "name": "strategy_plugins",
                "ok": not self.plugin_issues and not self.catalog.group_issues,
                "detail": (
                    f"{len(self.strategies)} loaded"
                    if not self.plugin_issues and not self.catalog.group_issues
                    else (
                        f"{len(self.strategies)} loaded, "
                        f"{len(self.plugin_issues) + len(self.catalog.group_issues)} issue(s)"
                    )
                ),
            }
        )
        checks.append(
            {
                "name": "tqcenter",
                "ok": (self.config.tq_user_dir / "tqcenter.py").exists(),
                "detail": str(self.config.tq_user_dir / "tqcenter.py"),
            }
        )
        port_ok = False
        try:
            with socket.create_connection(("127.0.0.1", 17709), timeout=1):
                port_ok = True
        except OSError:
            pass
        checks.append({"name": "tdx_http_17709", "ok": port_ok, "detail": "127.0.0.1:17709"})
        checks.append(
            {
                "name": "database",
                "ok": self.config.database_path.exists(),
                "detail": str(self.config.database_path),
            }
        )
        for gate in run_validation_gates(self.config.repository_root):
            checks.append(
                {
                    "name": f"gate_{gate.name}",
                    "ok": gate.ok,
                    "detail": gate.detail,
                }
            )
        summary = "READY" if all(item["ok"] for item in checks) else "PARTIAL"
        return {"status": summary, "checked_at": datetime.now().astimezone().isoformat(), "checks": checks}

    def run_scan(
        self,
        strategy_ids: list[str] | None = None,
        *,
        mode: str = "research",
        push_tdx: bool = False,
        refresh_sectors: bool = False,
        max_stocks: int | None = None,
        sampling_mode: str = "full",
        sample_seed: int = 49,
        refresh_data: bool = False,
        progress_callback: Any | None = None,
    ) -> ScanReport:
        requested_ids = strategy_ids or ["course49_system"]
        sampling_mode = _resolve_sampling_mode(sampling_mode, max_stocks)
        unknown = sorted(
            item for item in set(requested_ids) if item not in self.strategies and item not in self.catalog.groups
        )
        if unknown:
            raise ValueError(f"Unknown strategies: {', '.join(unknown)}")
        requested_groups = [self.catalog.groups[item] for item in requested_ids if item in self.catalog.groups]
        component_ids = tuple(
            dict.fromkeys(
                component
                for item in requested_ids
                for component in self.catalog.resolve(item)[1]
            )
        )
        disabled = sorted(
            item
            for item in component_ids
            if not self.strategies[item].metadata.enabled
            or not self.strategies[item].metadata.scan_enabled
        )
        if disabled:
            raise ValueError(f"Strategies are not enabled for scanning: {', '.join(disabled)}")
        scan_market = self._scan_market(component_ids)
        if scan_market == "US" and mode != "research":
            raise ValueError(
                "US strategy scans are research-only. Automated US paper execution "
                "uses the isolated USMomentumPaperService after qualification."
            )
        us_benchmark_codes = (
            self._us_benchmark_codes(component_ids) if scan_market == "US" else ()
        )
        self.data_hub.sources.validate_requirements(
            requirement
            for item in component_ids
            for requirement in self.strategies[item].metadata.data_requirements
        )
        candidate_streaks: list[int] = []
        for item in component_ids:
            if self._runtime_adapter(item) != RuntimeAdapter.COURSE49_DAILY:
                continue
            resolver = getattr(
                self.strategies[item], "candidate_minimum_streak", None
            )
            candidate_streaks.append(
                max(1, int(resolver())) if callable(resolver) else 1
            )
        data_plan = build_data_plan(
            (self.strategies[item].metadata for item in component_ids),
            event_minimum_streak=min(candidate_streaks, default=1),
        )
        scan_bar_count = required_bar_lookback(
            (self.strategies[item].metadata for item in component_ids),
            minimum=120,
        )
        run_id = uuid4().hex
        started = datetime.now().astimezone()
        self.database.create_run(run_id, "scan", mode, requested_ids)
        self.database.update_run(run_id, RunStatus.RUNNING)
        health: list[DataHealth] = []
        results: list[StrategyScanResult] = []
        weekly_observation_update: dict[str, Any] = {}
        try:
            def batch_progress(
                phase: str,
                lower: float,
                upper: float,
                label: str,
            ) -> Any:
                def report(completed: int, total: int, batch_size: int) -> None:
                    if progress_callback is None:
                        return
                    ratio = completed / total if total else 1.0
                    progress_callback(
                        phase=phase,
                        progress=lower + (upper - lower) * ratio,
                        detail=f"{label} {completed}/{total}（本批 {batch_size}）",
                        cache_status="refresh" if refresh_data else "miss",
                        waiting_reason="",
                    )

                return report

            if progress_callback is not None:
                progress_callback(
                    phase="WAITING_TDX",
                    progress=0.05,
                    detail="等待通达信数据通道",
                    cache_status="refresh" if refresh_data else "",
                    waiting_reason="single_tdx_channel",
                )
            with TdxProvider(
                self.config, __file__, cache_reads=not refresh_data
            ) as provider:
                if progress_callback is not None:
                    progress_callback(
                        phase="MARKET_DATA",
                        progress=0.10,
                        detail="正在读取扫描数据",
                        cache_status="refresh" if refresh_data else "miss",
                        waiting_reason="",
                    )
                if scan_market == "US":
                    listed_codes, listed_names = provider.list_us_stocks()
                    codes = [code for code in listed_codes if code.endswith(".US")]
                    names = {code: listed_names.get(code, code) for code in codes}
                    if not codes:
                        raise DataBlockedError(
                            "US stock universe is unavailable; no .US symbols were returned"
                        )
                else:
                    codes, names = provider.list_a_shares()
                constrained_codes = list(
                    dict.fromkeys(
                        code
                        for item in component_ids
                        for code in self._required_codes(item)
                    )
                )
                if scan_market == "US":
                    invalid_codes = sorted(
                        code for code in constrained_codes if not code.endswith(".US")
                    )
                    if invalid_codes:
                        raise ValueError(
                            "US scans only accept .US symbols: "
                            + ", ".join(invalid_codes)
                        )
                    missing_codes = sorted(set(constrained_codes) - set(codes))
                    if missing_codes:
                        raise DataBlockedError(
                            "Required US symbols are absent from the stock universe: "
                            + ", ".join(missing_codes)
                        )
                if all(
                    self._runtime_adapter(item) == RuntimeAdapter.GENERIC_DAILY
                    and self._required_codes(item)
                    for item in component_ids
                ):
                    codes = constrained_codes
                if sampling_mode == "stratified":
                    sample_bars = provider.fetch_bars(
                        codes,
                        "1d",
                        90,
                        fields=data_plan.front_fields,
                        dividend_type="front",
                        batch_callback=batch_progress(
                            "UNIVERSE_SAMPLE", 0.10, 0.20, "样本资格行情"
                        ),
                    )
                    sample_eligible = (
                        {
                            code: frame
                            for code, frame in sample_bars.items()
                            if code.endswith(".US") and not frame.empty
                        }
                        if scan_market == "US"
                        else filter_universe(
                            sample_bars,
                            names,
                            StrategyConfig(
                                tdx_root=self.config.tdx_root,
                                daily_lookback=90,
                            ),
                        )
                    )
                    codes = _stratified_sample(
                        list(sample_eligible),
                        sample_eligible,
                        max_stocks or min(500, len(sample_eligible)),
                        sample_seed,
                    )
                tradable_codes = list(
                    dict.fromkeys(
                        [
                            *codes,
                            *constrained_codes,
                        ]
                    )
                )
                codes = list(
                    dict.fromkeys(
                        [
                            *tradable_codes,
                            *(us_benchmark_codes if scan_market == "US" else ()),
                        ]
                    )
                )
                if scan_market == "US" and any(
                    not code.endswith(".US") for code in codes
                ):
                    raise ValueError("US scans only accept .US symbols")
                front = provider.fetch_bars(
                    codes,
                    "1d",
                    scan_bar_count,
                    fields=data_plan.front_fields,
                    dividend_type="front",
                    batch_callback=batch_progress(
                        "MARKET_DATA_FRONT", 0.12, 0.30, "前复权行情"
                    ),
                )
                raw = provider.fetch_bars(
                    codes,
                    "1d",
                    scan_bar_count,
                    fields=data_plan.raw_fields,
                    dividend_type="none",
                    batch_callback=batch_progress(
                        "MARKET_DATA_RAW", 0.30, 0.48, "不复权行情"
                    ),
                )
                if scan_market == "US":
                    missing_benchmark_bars = [
                        code
                        for code in us_benchmark_codes
                        if code not in front or front[code].empty
                    ]
                    if missing_benchmark_bars:
                        raise DataBlockedError(
                            "Required US market benchmark data is unavailable: "
                            + ", ".join(missing_benchmark_bars)
                        )
                    index_bars = front["SPY.US"]
                else:
                    index_map = provider.fetch_bars(
                        ["999999.SH"], "1d", scan_bar_count, dividend_type="front"
                    )
                    index_bars = index_map.get("999999.SH")
                    if index_bars is None:
                        fallback = provider.fetch_bars(
                            ["000001.SH"], "1d", scan_bar_count, dividend_type="front"
                        )
                        index_bars = fallback.get("000001.SH")
                daily_health = self.data_hub.assess_daily(
                    raw,
                    index_bars,
                    expected_symbol_count=len(codes),
                )
                health.append(daily_health)
                if daily_health.status != DataStatus.READY:
                    raise DataBlockedError(daily_health.message or "Daily data is unavailable")
                legacy_config = StrategyConfig(
                    tdx_root=self.config.tdx_root,
                    daily_lookback=scan_bar_count,
                )
                fixed_generic_only = all(
                    self._runtime_adapter(item) == RuntimeAdapter.GENERIC_DAILY
                    and self._required_codes(item)
                    for item in component_ids
                )
                eligible_front = (
                    {
                        code: frame
                        for code, frame in front.items()
                        if not frame.empty
                        and (scan_market != "US" or code.endswith(".US"))
                    }
                    if fixed_generic_only or scan_market == "US"
                    else filter_universe(front, names, legacy_config)
                )
                if scan_market == "US" and not eligible_front:
                    raise DataBlockedError(
                        "US stock universe has no usable daily bar data"
                    )
                if scan_market == "US":
                    tradable_set = set(tradable_codes)
                    eligible_front = {
                        code: frame
                        for code, frame in eligible_front.items()
                        if code in tradable_set
                    }
                    if not eligible_front:
                        raise DataBlockedError(
                            "US stock universe has no usable tradable equity data"
                        )
                    # Benchmarks are analysis inputs, never tradable candidates.
                    # The strategy reads SPY from front_bars for regime control.
                    eligible_front.update(
                        {
                            code: front[code]
                            for code in us_benchmark_codes
                            if code in front and not front[code].empty
                        }
                    )
                eligible_raw = {code: raw[code] for code in eligible_front if code in raw}
                sectors_map = (
                    provider.load_sectors(refresh=refresh_sectors or refresh_data)
                    if data_plan.require_sectors
                    else {}
                )
                sector_rows = _sector_membership_rows(sectors_map)
                sector_hash = _records_hash(sector_rows)
                benchmark_codes = ["000300.CSI", "000300.SH", "000852.CSI", "000852.SH", "399006.SZ"]
                benchmark_bars = (
                    provider.fetch_bars(
                        benchmark_codes,
                        "1d",
                        scan_bar_count,
                        dividend_type="front",
                    )
                    if data_plan.require_style_benchmarks
                    else {}
                )

                chan_ids = [
                    item
                    for item in component_ids
                    if self._runtime_adapter(item) == RuntimeAdapter.CHAN_DAILY
                ]
                market = (
                    evaluate_market_regime(index_bars, eligible_front, legacy_config)
                    if chan_ids
                    else None
                )
                ranked_sectors = (
                    rank_sectors(sectors_map, eligible_front, legacy_config)
                    if chan_ids
                    else []
                )
                leaders = (
                    rank_leaders(
                        ranked_sectors,
                        sectors_map,
                        eligible_front,
                        names,
                        legacy_config,
                    )
                    if chan_ids
                    else []
                )
                chan_positions = self.portfolio.positions("chan_v1")
                pending_chan = self.database.query(
                    "SELECT code FROM paper_orders WHERE strategy_id='chan_v1' AND status='PENDING'"
                )
                chan_codes = sorted({item.code for item in leaders} | {item["code"] for item in chan_positions + pending_chan})
                chan_front = {code: eligible_front[code] for code in chan_codes if code in eligible_front}
                chan_raw = {code: eligible_raw[code] for code in chan_codes if code in eligible_raw}

                execution_bars = dict(raw)
                self.portfolio.process_pending(execution_bars, names)
                self.portfolio.process_pending_groups(raw, names)

                for chan_id in chan_ids:
                    results.append(
                        self.strategies[chan_id].scan(
                            run_id=run_id,
                            market=market,
                            leaders=leaders,
                            daily_front=chan_front,
                            daily_raw=chan_raw,
                            positions=self.portfolio.positions(chan_id),
                        )
                    )

                course49_ids = sorted(
                    item
                    for item in component_ids
                    if self._runtime_adapter(item) == RuntimeAdapter.COURSE49_DAILY
                )
                if course49_ids:
                    if progress_callback is not None:
                        progress_callback(
                            phase="EVENT_INCREMENT",
                            progress=0.52,
                            detail="正在补齐49课事件数据",
                            cache_status="refresh" if refresh_data else "miss",
                            waiting_reason="",
                        )
                    limit_codes = self._latest_limit_codes(eligible_raw, names)
                    limit_snapshot = provider.fetch_limit_snapshot(limit_codes) if limit_codes else {}
                    course49_positions = [
                        item
                        for item in self.portfolio.positions()
                        if str(item["strategy_id"]) in course49_ids
                    ]
                    lhb_codes = sorted(
                        set(limit_codes) | {str(item["code"]) for item in course49_positions}
                    )
                    latest_day = max(
                        pd.Timestamp(frame.index[-1]).normalize()
                        for frame in eligible_raw.values()
                        if not frame.empty
                    )
                    lhb_start = (latest_day - pd.Timedelta(days=30)).strftime("%Y%m%d")
                    lhb_end = latest_day.strftime("%Y%m%d")
                    course49_raw = (
                        provider.fetch_course49_history(
                            lhb_codes,
                            lhb_start,
                            lhb_end,
                            batch_callback=batch_progress(
                                "COURSE49_EVENTS", 0.55, 0.78, "49课事件"
                            ),
                        )
                        if lhb_codes
                        else {}
                    )
                    lhb_history = normalize_lhb_history(course49_raw, eligible_raw)
                    lhb_rows = flatten_lhb_history(lhb_history, listed_only=True)
                    course49_rows = flatten_lhb_history(lhb_history)
                    limit_rows = [row for row in course49_rows if row.get("limit_event")]
                    activity_start = max(
                        pd.Timestamp(index_bars.index[0]).normalize(),
                        latest_day - pd.Timedelta(days=180),
                    ).strftime("%Y%m%d")
                    activity_raw = provider.fetch_market_activity(activity_start, lhb_end)
                    market_activity = normalize_market_activity(activity_raw)
                    market_activity_rows = flatten_market_activity(market_activity)
                    health.append(
                        DataHealth(
                            "dragon_tiger",
                            DataStatus.READY,
                            lhb_rows[-1]["event_date"] if lhb_rows else None,
                            latest_day.date().isoformat(),
                            len(lhb_rows),
                            "No selected symbols appeared on the list" if not lhb_rows else "",
                        )
                    )
                    health.append(
                        DataHealth(
                            "limit_behavior",
                            DataStatus.READY,
                            limit_rows[-1]["event_date"] if limit_rows else None,
                            latest_day.date().isoformat(),
                            len(limit_rows),
                            "No limit-up behavior found" if not limit_rows else "",
                        )
                    )
                    health.append(
                        DataHealth(
                            "market_activity",
                            DataStatus.READY if len(market_activity) >= 20 else DataStatus.PARTIAL,
                            market_activity.index[-1].date().isoformat()
                            if not market_activity.empty
                            else None,
                            latest_day.date().isoformat(),
                            len(market_activity),
                            "Insufficient market ecology history" if len(market_activity) < 20 else "",
                        )
                    )
                    base_ids = [
                        item
                        for item in course49_ids
                        if self._strategy_family(item) == "course49_v1"
                    ]
                    for base_id in base_ids:
                        results.append(
                            self.strategies[base_id].scan(
                                run_id=run_id,
                                front_bars=eligible_front,
                                raw_bars=eligible_raw,
                                names=names,
                                sector_members=sectors_map,
                                positions=self.portfolio.positions(base_id),
                                limit_snapshot=limit_snapshot,
                                lhb_history=lhb_history,
                                market_activity=market_activity,
                            )
                        )
                    for adaptive_id in (
                        item
                        for item in course49_ids
                        if self._strategy_family(item) != "course49_v1"
                    ):
                        if progress_callback is not None and adaptive_id == "course49_system":
                            progress_callback(
                                phase="COURSE49_CONTEXT",
                                progress=0.80,
                                detail="正在构建共享市场、题材、龙头和资金上下文",
                                cache_status="",
                                waiting_reason="",
                            )
                        adaptive_result = self.strategies[adaptive_id].scan(
                            run_id=run_id,
                            front_bars=eligible_front,
                            raw_bars=eligible_raw,
                            names=names,
                            sector_members=sectors_map,
                            positions=self.portfolio.positions(adaptive_id),
                            benchmark_bars=benchmark_bars,
                            runtime_state=self.database.load_runtime_states(adaptive_id),
                            limit_snapshot=limit_snapshot,
                            lhb_history=lhb_history,
                            market_activity=market_activity,
                            **(
                                {
                                    "context_metadata": {
                                        "market_count": len(front_bars),
                                        "eligible_count": len(eligible_front),
                                        "stock_pool_hash": _stock_pool_hash(
                                            list(eligible_front)
                                        ),
                                        "sector_membership_hash": sector_hash,
                                    }
                                }
                                if adaptive_id == "course49_system"
                                else {}
                            ),
                        )
                        results.append(adaptive_result)
                        latest_asof = str(
                            adaptive_result.state.get("asof", latest_day.date().isoformat())
                        )
                        self.database.replace_runtime_states(
                            adaptive_id,
                            dict(adaptive_result.state.get("runtime_state") or {}),
                            latest_asof,
                        )
                        if progress_callback is not None and adaptive_id == "course49_system":
                            progress_callback(
                                phase="PLAYBOOK_ROUTING",
                                progress=0.88,
                                detail="剧本评估和统一路由已完成",
                                cache_status="",
                                waiting_reason="",
                            )

                generic_ids = [
                    item
                    for item in component_ids
                    if self._runtime_adapter(item)
                    in {RuntimeAdapter.GENERIC_DAILY, RuntimeAdapter.US_STRICT}
                ]
                for generic_id in generic_ids:
                    metadata = self.strategies[generic_id].metadata
                    generic_positions = (
                        self.database.grouped_positions(generic_id)
                        if metadata.execution_model == ExecutionModel.MULTI_LEG
                        else self.portfolio.positions(generic_id)
                    )
                    generic_result = self.strategies[generic_id].scan(
                        run_id=run_id,
                        asof=pd.Timestamp(index_bars.index[-1]),
                        front_bars=eligible_front,
                        raw_bars=eligible_raw,
                        names=names,
                        sector_members=sectors_map,
                        benchmark_bars=benchmark_bars,
                        index_bars=index_bars,
                        positions=generic_positions,
                        runtime_state=self.database.load_runtime_states(generic_id),
                        is_rebalance_day=(
                            self._is_us_month_end(index_bars.index)
                            if scan_market == "US"
                            else None
                        ),
                        tradable_codes=(
                            set(tradable_codes) if scan_market == "US" else None
                        ),
                    )
                    generic_latest = generic_result.state.get("asof")
                    _validate_scan_result(
                        generic_id,
                        generic_result,
                        pd.Timestamp(generic_latest or index_bars.index[-1]),
                        metadata.execution_model,
                    )
                    results.append(generic_result)
                    runtime_state = generic_result.state.get("runtime_state")
                    if isinstance(runtime_state, dict) and generic_latest:
                        self.database.replace_runtime_states(
                            generic_id,
                            runtime_state,
                            str(generic_latest),
                        )
                    health.append(
                        DataHealth(
                            f"strategy:{generic_id}",
                            DataStatus.READY,
                            str(generic_latest) if generic_latest else None,
                            pd.Timestamp(index_bars.index[-1]).date().isoformat(),
                            len(generic_result.candidates),
                            "No eligible candidate at the current data boundary"
                            if not generic_result.candidates
                            else "",
                        )
                    )

                component_results = list(results)
                if len(requested_ids) == 1 and requested_groups:
                    results = self.composition.compose(requested_groups[0], results, run_id)
                signals = [signal for result in results for signal in result.signals]
                order_groups = [intent for result in results for intent in result.order_groups]
                if progress_callback is not None:
                    progress_callback(
                        phase="PERSISTENCE",
                        progress=0.92,
                        detail="正在持久化信号、状态和证据",
                        cache_status="",
                        waiting_reason="",
                    )
                self.database.save_signals(signals)
                self.database.save_order_groups(order_groups)
                self.portfolio.queue_approved(signals)
                snapshot_id = f"scan_{run_id}"
                snapshot_metadata = {
                    "count": scan_bar_count,
                    "adjustment": "front",
                    "market": scan_market,
                    "sampling_mode": sampling_mode,
                    "sample_seed": sample_seed,
                    "stock_pool_hash": _stock_pool_hash(
                        list(tradable_codes if scan_market == "US" else eligible_front)
                    ),
                    "universe_distribution": _universe_distribution(
                        list(tradable_codes if scan_market == "US" else eligible_front)
                    ),
                    "analysis_benchmarks": list(us_benchmark_codes),
                }
                self.snapshots.write_bars(snapshot_id, "daily_front", eligible_front, snapshot_metadata)
                if data_plan.require_sectors:
                    self.snapshots.write_records(
                        snapshot_id,
                        "sector_membership",
                        sector_rows,
                        {
                            "quality": "LIMITED",
                            "source": "current_fallback",
                            "asof": latest_day.date().isoformat() if course49_ids else pd.Timestamp(index_bars.index[-1]).date().isoformat(),
                            "content_hash": sector_hash,
                        },
                    )
                if benchmark_bars:
                    self.snapshots.write_bars(
                        snapshot_id,
                        "style_benchmarks",
                        benchmark_bars,
                        {"codes": benchmark_codes, "adjustment": "front"},
                    )
                if course49_ids:
                    self.snapshots.write_records(
                        snapshot_id,
                        "dragon_tiger",
                        lhb_rows,
                        {"start": lhb_start, "end": lhb_end, "symbols": lhb_codes},
                    )
                    self.snapshots.write_records(
                        snapshot_id,
                        "limit_behavior",
                        limit_rows,
                        {"start": lhb_start, "end": lhb_end, "symbols": lhb_codes},
                    )
                    self.snapshots.write_records(
                        snapshot_id,
                        "market_activity",
                        market_activity_rows,
                        {"start": activity_start, "end": lhb_end},
                    )
                    for event_dataset in (
                        "dragon_tiger",
                        "limit_behavior",
                        "market_activity",
                    ):
                        self.database.add_snapshot_dependency(
                            snapshot_id,
                            "EVENT_FRAGMENT",
                            f"{snapshot_id}:{event_dataset}",
                            {"start": lhb_start, "end": lhb_end},
                        )
                if push_tdx:
                    self._push_scan(provider, results)

                weekly_result = next(
                    (
                        result
                        for result in component_results
                        if result.strategy.strategy_id == "weekly_triangle_v1"
                    ),
                    None,
                )
                if weekly_result is not None and sampling_mode == "full":
                    weekly_strategy = self.strategies["weekly_triangle_v1"]
                    runtime_candidates = [
                        dict(item["candidate"])
                        for item in dict(
                            weekly_result.state.get("runtime_state") or {}
                        ).values()
                        if isinstance(item, dict)
                        and isinstance(item.get("candidate"), dict)
                    ]
                    observation_candidates = sorted(
                        runtime_candidates or list(weekly_result.candidates),
                        key=lambda item: (
                            -float(item.get("score") or 0.0),
                            str(item.get("code") or ""),
                        ),
                    )
                    target_weight = float(
                        getattr(
                            getattr(weekly_strategy, "parameters", None),
                            "target_weight",
                            0.20,
                        )
                    )
                    try:
                        weekly_observation_update = (
                            self.weekly_triangle_observations.capture_and_refresh(
                                run_id=run_id,
                                strategy_version=weekly_result.strategy.version,
                                observed_at=str(weekly_result.state.get("asof") or ""),
                                candidates=observation_candidates,
                                bars=raw,
                                target_weight=target_weight,
                                maximum_entries=int(
                                    getattr(
                                        getattr(weekly_strategy, "parameters", None),
                                        "max_entry_signals",
                                        20,
                                    )
                                ),
                            )
                        )
                    except Exception as exc:
                        weekly_observation_update = {
                            "status": "FAILED",
                            "error": str(exc),
                        }
                elif weekly_result is not None:
                    weekly_observation_update = {
                        "status": "SKIPPED",
                        "reason": "NON_FULL_UNIVERSE",
                    }

            metadata = {
                "strategies": {result.strategy.strategy_id: result.state for result in results},
                "framework_candidates": {
                    result.strategy.framework_id: list(result.candidates)
                    for result in component_results
                    if result.strategy.framework_id
                },
                "strategy_candidates": {
                    result.strategy.strategy_id: list(result.candidates)
                    for result in component_results
                    if result.candidates
                },
                "components": [result.strategy.strategy_id for result in component_results],
                "requested": requested_ids,
                "composition_mode": (
                    requested_groups[0].composition_mode.value
                    if len(requested_ids) == 1 and requested_groups
                    else "independent"
                ),
                "market": scan_market,
                "order_group_count": sum(len(result.order_groups) for result in results),
                "sampling_mode": sampling_mode,
                "sample_seed": sample_seed,
                "data_plan": data_plan.as_dict(),
                "effective_batch_sizes": provider.effective_batch_sizes(),
                "stock_pool_hash": _stock_pool_hash(
                    list(tradable_codes if scan_market == "US" else eligible_front)
                ) if results else "",
                "universe_distribution": _universe_distribution(
                    list(tradable_codes if scan_market == "US" else eligible_front)
                ) if results else {},
                "analysis_benchmarks": list(us_benchmark_codes),
                "sector_membership_quality": "LIMITED" if data_plan.require_sectors else "NOT_REQUIRED",
                "sector_membership_source": "current_fallback" if data_plan.require_sectors else "data_plan",
                "sector_membership_hash": sector_hash if results and data_plan.require_sectors else "",
                "weekly_triangle_observations": weekly_observation_update,
            }
            self.database.update_run(run_id, RunStatus.SUCCEEDED, snapshot_id=snapshot_id, metadata=metadata)
            if progress_callback is not None:
                progress_callback(
                    phase="COMPLETED",
                    progress=1.0,
                    detail="扫描完成",
                    cache_status="refresh" if refresh_data else "miss",
                    waiting_reason="",
                )
            status = RunStatus.SUCCEEDED
            error = ""
        except DataBlockedError as exc:
            self.database.update_run(run_id, RunStatus.BLOCKED_DATA, error=str(exc))
            status = RunStatus.BLOCKED_DATA
            error = str(exc)
        except Exception as exc:
            self.database.update_run(run_id, RunStatus.FAILED, error=str(exc))
            status = RunStatus.FAILED
            error = str(exc)
        finished = datetime.now().astimezone()
        return ScanReport(
            run_id=run_id,
            status=status,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            data_health=tuple(health),
            strategy_results=tuple(results),
            error=error,
        )

    def run_daily_research(
        self,
        strategy_ids: list[str] | None = None,
        *,
        refresh_sectors: bool = False,
        max_stocks: int | None = None,
        sampling_mode: str = "full",
        sample_seed: int = 49,
        refresh_data: bool = False,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        from .ai_research import AIResearchService

        report = self.run_scan(
            strategy_ids,
            mode="research",
            push_tdx=False,
            refresh_sectors=refresh_sectors,
            max_stocks=max_stocks,
            sampling_mode=sampling_mode,
            sample_seed=sample_seed,
            refresh_data=refresh_data,
            progress_callback=progress_callback,
        )
        brief = None
        if report.status == RunStatus.SUCCEEDED:
            brief = AIResearchService(self.config, self.database).generate_brief(report.run_id)
        return {"run": asdict(report), "brief": brief}

    def decide_signal(
        self,
        signal_id: str,
        approve: bool,
        note: str = "",
        push_tdx: bool = False,
        *,
        reason_tags: list[str] | None = None,
        confidence: float | None = None,
        max_acceptable_loss: float | None = None,
        ai_review_id: str | None = None,
    ) -> dict[str, Any]:
        decision = SignalStatus.APPROVED if approve else SignalStatus.REJECTED
        signal = self.database.decide_signal(
            signal_id,
            decision,
            note,
            reason_tags=reason_tags or (),
            confidence=confidence,
            max_acceptable_loss=max_acceptable_loss,
            ai_review_id=ai_review_id,
        )
        if approve and push_tdx:
            try:
                with TdxProvider(self.config, __file__) as provider:
                    provider.push_candidates("RP_APPROVED", "投研已批准", [signal["code"]], show=False)
                    provider.push_warning(signal, approved=True)
                    provider.push_signal_values(signal)
            except Exception as exc:
                signal["tdx_push_error"] = str(exc)
        return signal

    def sync_data(
        self,
        *,
        daily_bars: int = 120,
        refresh_sectors: bool = False,
        refresh_data: bool = False,
    ) -> dict[str, Any]:
        data_asof = datetime.now().date().isoformat()
        identity = {"kind": "daily_sync", "universe": "all_a", "data_asof": data_asof}
        coverage = {
            "start_date": None,
            "end_date": None,
            "resolved_daily_bars": int(daily_bars),
            "datasets": ["daily_front", "market_index", "security_master", "sector_membership"],
            "event_minimum_streak": 1,
        }
        query = {"identity": identity, "coverage": coverage, "data_asof": data_asof}
        cache_key = self.data_cache.key(query)
        lock = self.data_cache.flight_lock(cache_key)
        with lock:
            match = None
            if not refresh_data and not refresh_sectors:
                match = self.data_cache.find(cache_key, identity=identity, coverage=coverage)
            if match is not None and all(
                self.snapshots.has_dataset(match.snapshot_id, item)
                for item in coverage["datasets"]
            ):
                bars = self.snapshots.load_bars(match.snapshot_id, "daily_front")
                index_map = self.snapshots.load_bars(match.snapshot_id, "market_index")
                index_bars = next(iter(index_map.values()), None)
                master = self.snapshots.load_records(match.snapshot_id, "security_master")
                sector_rows = self.snapshots.load_records(
                    match.snapshot_id, "sector_membership"
                ).to_dict("records")
                sector_query = self.snapshots.dataset_query(
                    match.snapshot_id, "sector_membership"
                )
                return {
                    "snapshot_id": match.snapshot_id,
                    "symbols": len(master),
                    "bars_symbols": len(bars),
                    "sectors": len({str(row.get('sector_code', '')) for row in sector_rows}),
                    "sector_membership_hash": sector_query.get("content_hash", ""),
                    "health": self.data_hub.assess_daily(bars, index_bars),
                    "path": str(self.config.snapshot_dir / match.snapshot_id),
                    "cache_status": match.hit_type,
                    "data_asof": match.data_asof,
                }

            snapshot_id = f"sync_{uuid4().hex}"
            build_key = f"{cache_key}:building:{snapshot_id}"
            self.data_cache.begin_snapshot(
                build_key, snapshot_id, data_asof, query, coverage
            )
            try:
                with TdxProvider(
                    self.config, __file__, cache_reads=not refresh_data
                ) as provider:
                    codes, names = provider.list_a_shares()
                    bars = provider.fetch_bars(
                        codes, "1d", daily_bars, dividend_type="front"
                    )
                    index_map = provider.fetch_bars(
                        ["999999.SH"], "1d", daily_bars, dividend_type="front"
                    )
                    index_bars = index_map.get("999999.SH")
                    sectors = provider.load_sectors(
                        refresh=refresh_sectors or refresh_data
                    )
                health = self.data_hub.assess_daily(bars, index_bars)
                path = self.snapshots.write_bars(
                    snapshot_id,
                    "daily_front",
                    bars,
                    {"count": daily_bars, "adjustment": "front"},
                )
                self.snapshots.write_bars(
                    snapshot_id,
                    "market_index",
                    index_map,
                    {"count": daily_bars, "codes": ["999999.SH"], "adjustment": "front"},
                )
                self.snapshots.write_records(
                    snapshot_id,
                    "security_master",
                    [{"code": code, "name": names.get(code, "")} for code in codes],
                    {"asof": data_asof},
                )
                sector_rows = _sector_membership_rows(sectors)
                sector_hash = _records_hash(sector_rows)
                self.snapshots.write_records(
                    snapshot_id,
                    "sector_membership",
                    sector_rows,
                    {
                        "quality": "CURRENT",
                        "source": "tdx_sync",
                        "asof": pd.Timestamp(index_bars.index[-1]).date().isoformat()
                        if index_bars is not None
                        else data_asof,
                        "content_hash": sector_hash,
                    },
                )
                self.data_cache.commit_snapshot(build_key, cache_key, snapshot_id)
                self.data_cache.prune()
                return {
                    "snapshot_id": snapshot_id,
                    "symbols": len(names),
                    "bars_symbols": len(bars),
                    "sectors": len(sectors),
                    "sector_membership_hash": sector_hash,
                    "health": health,
                    "path": str(path),
                    "cache_status": "refresh" if refresh_data else "miss",
                    "data_asof": data_asof,
                }
            except Exception as exc:
                self.data_cache.fail(build_key, str(exc))
                raise

    def _push_scan(self, provider: TdxProvider, results: list[StrategyScanResult]) -> None:
        by_strategy = {result.strategy.strategy_id: result for result in results}
        chan = by_strategy.get("chan_v1")
        course49 = by_strategy.get("course49_v1")
        course49_v2 = by_strategy.get("course49_v2")
        course49_v3 = by_strategy.get("course49_v3")
        course49_system = by_strategy.get("course49_system")
        weekly_triangle = by_strategy.get("weekly_triangle_v1")
        if chan:
            provider.push_candidates(
                "RP_CHAN", "缠论候选", [signal.code for signal in chan.signals if signal.side == "BUY"]
            )
        if course49:
            provider.push_candidates(
                "RP49_WAIT", "49课待确认", [signal.code for signal in course49.signals if signal.status == SignalStatus.PROPOSED]
            )
        if course49_v2:
            provider.push_candidates(
                "RP49_V2",
                "49课V2待确认",
                [
                    signal.code
                    for signal in course49_v2.signals
                    if signal.status == SignalStatus.PROPOSED
                ],
            )
        if course49_v3:
            provider.push_candidates(
                "RP49_V3",
                "49课V3待确认",
                [
                    signal.code
                    for signal in course49_v3.signals
                    if signal.status == SignalStatus.PROPOSED
                ],
            )
        if course49_system:
            provider.push_candidates(
                "RP49_SYS",
                "49课体系候选",
                [
                    signal.code
                    for signal in course49_system.signals
                    if signal.status == SignalStatus.PROPOSED
                ],
            )
        if weekly_triangle:
            provider.push_candidates(
                "RP_WTRI_WATCH",
                "周线三角观察",
                [
                    str(candidate["code"])
                    for candidate in weekly_triangle.candidates
                ],
            )
            provider.push_candidates(
                "RP_WTRI_BREAK",
                "周线三角突破观察",
                [
                    str(candidate["code"])
                    for candidate in weekly_triangle.candidates
                    if candidate.get("stage") == "BREAKOUT"
                ],
            )
            provider.push_candidates("RP_WTRI_BUY", "周线三角买入（停用）", [])
        sell_codes = [signal.code for result in results for signal in result.signals if signal.side == "SELL"]
        provider.push_candidates("RP_SELL", "投研卖出", sell_codes)
        for result in results:
            for signal in result.signals:
                provider.push_warning(signal.as_record(), approved=signal.status == SignalStatus.APPROVED)
                provider.push_signal_values(signal.as_record())

    @staticmethod
    def _latest_limit_codes(bars: dict[str, pd.DataFrame], names: dict[str, str]) -> list[str]:
        codes = []
        for code, frame in bars.items():
            close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
            if len(close) < 2 or close.iloc[-2] <= 0:
                continue
            item_return = float(close.iloc[-1] / close.iloc[-2] - 1.0)
            if item_return >= price_limit_ratio(code, names.get(code, "")) - 0.001:
                codes.append(code)
        return codes


class DataBlockedError(RuntimeError):
    pass
