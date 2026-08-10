from __future__ import annotations

import json
import hashlib
import math
import random
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

import numpy as np
import pandas as pd
import psutil

from strategy_v1.backtest import build_daily_schedule, run_backtest as run_legacy_chan_backtest
from strategy_v1.config import RiskConfig, StrategyConfig
from strategy_v1.market import filter_universe
from strategy_v1.portfolio import price_limit_ratio

from .config import PlatformConfig, PortfolioConfig
from .composition import CompositionMode, StrategyCatalog, built_in_groups
from .course49_market import flatten_market_activity, normalize_market_activity
from .data import TdxProvider
from .data_cache import DataCacheManager
from .data_plan import DataPlan, build_data_plan
from .lhb import (
    LhbFeatures,
    flatten_lhb_history,
    inflate_lhb_history,
    normalize_lhb_history,
)
from .models import (
    ExecutionModel,
    OrderGroupAction,
    OrderGroupIntent,
    PlatformSignal,
    RuntimeAdapter,
    StrategyScanResult,
)
from .plugin_loader import load_strategy_registry
from .registry import SourceRegistry
from .storage import Database, ParquetSnapshotStore
from .strategies.course49_system import (
    CONTEXT_VERSION,
    POLICY_VERSION,
    framework_metadata,
)
from .strategies import (
    build_course49_eligibility_matrix,
    build_course49_feature_matrix,
    build_course49_market_matrix,
    build_course49_v3_candidate_matrix,
)


ADAPTIVE_COURSE49_IDS = frozenset({"course49_v2", "course49_v3"})
COURSE49_IDS = frozenset({"course49_v1", *ADAPTIVE_COURSE49_IDS})


@dataclass
class HistoricalPosition:
    code: str
    quantity: int
    average_price: float
    entry_date: str
    stop_price: float
    last_price: float
    evidence: str
    entry_fees: float


@dataclass
class HistoricalPairLeg:
    group_key: str
    code: str
    side: str
    quantity: int
    average_price: float
    entry_date: str
    last_price: float
    ratio: float
    target_weight: float
    entry_fees: float
    evidence: str


@dataclass
class BacktestDataset:
    names: dict[str, str]
    daily_front: dict[str, pd.DataFrame]
    daily_raw: dict[str, pd.DataFrame]
    index_bars: pd.DataFrame
    sector_members: dict[str, dict[str, Any]]
    benchmark_bars: dict[str, pd.DataFrame]
    lhb_history: dict[str, dict[str, LhbFeatures]]
    market_activity: pd.DataFrame


class BacktestService:
    def __init__(self, config: PlatformConfig, database: Database):
        self.config = config
        self.database = database
        self.snapshots = ParquetSnapshotStore(config, database)
        self.cache = DataCacheManager(config, database)
        self.sources = SourceRegistry()
        self.strategies, self.plugin_issues = load_strategy_registry(self.config)
        for strategy in self.strategies.values():
            self.database.register_strategy(
                strategy.metadata,
                getattr(strategy, "__plugin_origin__", "builtin"),
            )
        system = self.strategies.get("course49_system")
        if system is not None:
            self.database.register_framework(
                framework_metadata().as_record(),
                [item.metadata.as_record() for item in system.playbooks],
                policy_version=POLICY_VERSION,
            )
        for group in built_in_groups():
            self.database.upsert_strategy_group(group)
        self.catalog = StrategyCatalog(self.strategies, self.database.load_strategy_groups())

    def refresh_catalog(self) -> None:
        self.catalog = StrategyCatalog(self.strategies, self.database.load_strategy_groups())

    def reload_strategies(self) -> None:
        strategies, issues = load_strategy_registry(self.config)
        catalog = StrategyCatalog(strategies, self.database.load_strategy_groups())
        self.strategies = strategies
        self.plugin_issues = issues
        self.catalog = catalog
        for strategy in strategies.values():
            self.database.register_strategy(
                strategy.metadata,
                getattr(strategy, "__plugin_origin__", "builtin"),
            )
        system = strategies.get("course49_system")
        if system is not None:
            self.database.register_framework(
                framework_metadata().as_record(),
                [item.metadata.as_record() for item in system.playbooks],
                policy_version=POLICY_VERSION,
            )

    def _strategy_family(self, strategy_id: str) -> str:
        strategy = self.strategies[strategy_id]
        return strategy.metadata.strategy_family or strategy_id

    def _runtime_adapter(self, strategy_id: str) -> RuntimeAdapter:
        return RuntimeAdapter(self.strategies[strategy_id].metadata.runtime_adapter)

    def _required_codes(self, strategy_id: str) -> tuple[str, ...]:
        values = getattr(self.strategies[strategy_id], "required_codes", ())
        return tuple(str(item) for item in values)

    def _candidate_minimum_streak(self, strategy_id: str) -> int:
        resolver = getattr(
            self.strategies[strategy_id], "candidate_minimum_streak", None
        )
        return max(1, int(resolver())) if callable(resolver) else 1

    def run(
        self,
        strategy_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        daily_bars: int = 180,
        max_stocks: int | None = None,
        universe: str = "all_a",
        stock_codes: list[str] | None = None,
        refresh_sectors: bool = False,
        sampling_mode: str = "full",
        sample_seed: int = 49,
        execution_cost_multiplier: float = 1.0,
        refresh_data: bool = False,
        playbook_ids: list[str] | None = None,
        progress_callback: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        run_started = perf_counter()
        stage_durations: dict[str, float] = {}
        try:
            strategy_group, component_ids = self.catalog.resolve(strategy_id)
        except KeyError:
            raise ValueError(f"Unknown backtest strategy: {strategy_id}")
        if strategy_group and strategy_group.composition_mode not in {
            CompositionMode.CAPITAL_SLEEVES,
            CompositionMode.COMPARISON,
        }:
            raise ValueError(
                f"Strategy group '{strategy_id}' uses scan-only composition mode "
                f"'{strategy_group.composition_mode.value}'"
            )
        disabled = sorted(
            item
            for item in component_ids
            if not self.strategies[item].metadata.enabled
            or not self.strategies[item].metadata.backtest_enabled
        )
        if disabled:
            raise ValueError(
                f"Strategies are not enabled for backtesting: {', '.join(disabled)}"
            )
        self.sources.validate_requirements(
            requirement
            for item in component_ids
            for requirement in self.strategies[item].metadata.data_requirements
        )
        capital_weights = self.catalog.capital_weights(strategy_id)
        if not 0.0 <= execution_cost_multiplier <= 5.0:
            raise ValueError("Execution cost multiplier must be between 0 and 5")
        if execution_cost_multiplier != 1.0 and any(
            self._runtime_adapter(item) == RuntimeAdapter.CHAN_DAILY for item in component_ids
        ):
            raise ValueError("Execution cost stress is unavailable for the legacy Chan adapter")
        execution_config = _execution_cost_config(
            self.config.portfolio, execution_cost_multiplier
        )
        sampling_mode = _resolve_sampling_mode(sampling_mode, max_stocks)
        start_date, end_date = _validate_date_range(start_date, end_date)
        count = _required_daily_bars(daily_bars, start_date, end_date)
        stock_codes = stock_codes or []
        playbook_ids = list(dict.fromkeys(playbook_ids or []))
        if playbook_ids and "course49_system" not in component_ids:
            raise ValueError("playbook_ids is only supported by course49_system backtests")
        if universe == "custom" and not stock_codes:
            raise ValueError("Custom universe requires at least one stock code")
        course49_ids = tuple(
            item
            for item in component_ids
            if self._runtime_adapter(item) == RuntimeAdapter.COURSE49_DAILY
        )
        event_minimum_streak = min(
            (self._candidate_minimum_streak(item) for item in course49_ids),
            default=1,
        )
        data_plan = build_data_plan(
            (self.strategies[item].metadata for item in component_ids),
            event_minimum_streak=event_minimum_streak,
        )
        data_asof = end_date or date.today().isoformat()
        required_data_codes = tuple(
            dict.fromkeys(
                code
                for item in component_ids
                for code in self._required_codes(item)
            )
        )
        cache_identity = {
            "universe": universe,
            "stock_codes": sorted(stock_codes),
            "sampling_mode": sampling_mode,
            "max_stocks": max_stocks,
            "sample_seed": sample_seed,
            "refresh_sectors": bool(refresh_sectors),
            "required_codes": list(required_data_codes),
        }
        if end_date is None:
            cache_identity["latest_data_asof"] = data_asof
        cache_coverage = {
            "start_date": start_date,
            "end_date": end_date,
            "resolved_daily_bars": count,
            "datasets": list(data_plan.datasets),
            "event_minimum_streak": event_minimum_streak,
        }
        cache_query = {
            "identity": cache_identity,
            "coverage": cache_coverage,
            "data_asof": data_asof,
            "data_plan": data_plan.as_dict(),
        }
        data_cache_key = self.cache.key(cache_query)
        parameters: dict[str, Any] = {
            "strategy_id": strategy_id,
            "start_date": start_date,
            "end_date": end_date,
            "daily_bars": daily_bars,
            "max_stocks": max_stocks,
            "universe": universe,
            "stock_codes": stock_codes,
            "sampling_mode": sampling_mode,
            "sample_seed": sample_seed,
            "execution_cost_multiplier": execution_cost_multiplier,
            "refresh_data": bool(refresh_data),
            "playbook_ids": playbook_ids,
            "resolved_daily_bars": count,
            "data_plan": data_plan.as_dict(),
            "data_cache_key": data_cache_key,
            "data_asof": data_asof,
            "worker_threads": self.config.performance.worker_threads,
            "memory_cache_limit_bytes": self.config.performance.memory_cache_bytes,
            "components": list(component_ids),
            "framework_versions": {
                item: self.strategies[item].metadata.framework_id
                for item in component_ids
                if self.strategies[item].metadata.framework_id
            },
            "policy_versions": {
                item: self.strategies[item].metadata.policy_version
                for item in component_ids
                if self.strategies[item].metadata.policy_version
            },
            "capital_weights": capital_weights,
            "composition_mode": (
                strategy_group.composition_mode.value if strategy_group else "standalone"
            ),
            "conflict_policy": (
                strategy_group.conflict_policy.value if strategy_group else "risk_first"
            ),
        }
        backtest_id = uuid4().hex
        started_at = datetime.now().astimezone().isoformat()
        self.database.execute(
            """INSERT INTO backtests
            (backtest_id, strategy_id, status, started_at, start_date, end_date, parameters_json)
            VALUES (?, ?, 'RUNNING', ?, ?, ?, ?)""",
            (
                backtest_id,
                strategy_id,
                started_at,
                start_date,
                end_date,
                json.dumps(parameters, ensure_ascii=False),
            ),
        )
        cache_lock = self.cache.flight_lock(data_cache_key)
        cache_build_started = False
        build_cache_key = f"{data_cache_key}:building:{backtest_id}"
        cache_lock.acquire()
        try:
            self._progress(progress_callback, "CACHE_LOOKUP", 0.03, "正在检查可复用数据")
            cache_match = None if refresh_data or refresh_sectors else self.cache.find(
                data_cache_key,
                identity=cache_identity,
                coverage=cache_coverage,
            )
            if cache_match is not None and all(
                self.snapshots.has_dataset(cache_match.snapshot_id, dataset)
                for dataset in data_plan.datasets
            ):
                return self._run_cached_backtest(
                    backtest_id,
                    strategy_id,
                    component_ids,
                    capital_weights,
                    count,
                    start_date,
                    end_date,
                    execution_config,
                    parameters,
                    data_plan,
                    cache_match.snapshot_id,
                    cache_match.cache_key,
                    cache_match.hit_type,
                    progress_callback,
                    run_started,
                )
            snapshot_id = f"bt_{backtest_id}"
            self.cache.begin_snapshot(
                build_cache_key,
                snapshot_id,
                data_asof,
                cache_query,
                cache_coverage,
            )
            cache_build_started = True
            self._progress(
                progress_callback,
                "WAITING_TDX",
                0.05,
                "等待通达信数据通道",
                waiting_reason="single_tdx_channel",
            )
            def batch_progress(
                phase: str,
                lower: float,
                upper: float,
                label: str,
            ) -> Callable[[int, int, int], None]:
                def report(completed: int, total: int, batch_size: int) -> None:
                    ratio = completed / total if total else 1.0
                    self._progress(
                        progress_callback,
                        phase,
                        lower + (upper - lower) * ratio,
                        f"{label} {completed}/{total}（本批 {batch_size}）",
                    )

                return report

            with TdxProvider(
                self.config, __file__, cache_reads=not refresh_data
            ) as provider:
                self._progress(progress_callback, "MARKET_DATA", 0.08, "正在读取通达信行情")
                stage_started = perf_counter()
                codes, names = provider.list_a_shares()
                codes = _select_universe(codes, universe, stock_codes)
                constrained_codes = list(required_data_codes)
                if all(
                    self._runtime_adapter(item) == RuntimeAdapter.GENERIC_DAILY
                    and self._required_codes(item)
                    for item in component_ids
                ):
                    codes = constrained_codes
                if not codes:
                    raise ValueError("Selected stock universe is empty")
                if sampling_mode == "stratified":
                    sample_size = max_stocks or min(500, len(codes))
                    eligibility_bars = provider.fetch_bars(
                        codes,
                        "1d",
                        90,
                        fields=data_plan.front_fields,
                        dividend_type="front",
                        end_time=end_date,
                        batch_callback=batch_progress(
                            "UNIVERSE_SAMPLE", 0.08, 0.17, "样本资格行情"
                        ),
                    )
                    eligible_for_sample = filter_universe(
                        _slice_to_date(eligibility_bars, end_date),
                        names,
                        StrategyConfig(tdx_root=self.config.tdx_root, daily_lookback=90),
                    )
                    codes = _stratified_sample(
                        list(eligible_for_sample),
                        eligible_for_sample,
                        sample_size,
                        sample_seed,
                    )
                codes = list(dict.fromkeys([*codes, *constrained_codes]))
                bar_window = {
                    "start_time": start_date,
                    "end_time": end_date,
                    "warmup_bars": 90,
                }
                daily_front = provider.fetch_bars(
                    codes,
                    "1d",
                    count,
                    fields=data_plan.front_fields,
                    dividend_type="front",
                    batch_callback=batch_progress(
                        "MARKET_DATA_FRONT", 0.10, 0.27, "前复权行情"
                    ),
                    **bar_window,
                )
                daily_raw = provider.fetch_bars(
                    codes,
                    "1d",
                    count,
                    fields=data_plan.raw_fields,
                    dividend_type="none",
                    batch_callback=batch_progress(
                        "MARKET_DATA_RAW", 0.27, 0.44, "不复权行情"
                    ),
                    **bar_window,
                )
                end_eligible = filter_universe(
                    _slice_to_date(daily_front, end_date),
                    names,
                    StrategyConfig(tdx_root=self.config.tdx_root, daily_lookback=count),
                )
                if all(
                    self._runtime_adapter(item) == RuntimeAdapter.GENERIC_DAILY
                    and self._required_codes(item)
                    for item in component_ids
                ):
                    end_eligible = {
                        code: frame for code, frame in daily_front.items() if not frame.empty
                    }
                parameters["loaded_symbols"] = len(codes)
                parameters["resolved_symbols"] = len(end_eligible)
                parameters["stock_pool_hash"] = _stock_pool_hash(list(end_eligible))
                parameters["universe_distribution"] = _universe_distribution(list(end_eligible))
                index_map = provider.fetch_bars(
                    ["999999.SH"], "1d", count, dividend_type="front", **bar_window
                )
                index_bars = index_map.get("999999.SH")
                if index_bars is None:
                    raise RuntimeError("Shanghai index data is unavailable")
                _ensure_date_range_available(index_bars, start_date, end_date)
                sector_asof = end_date or _trading_days(index_bars.index).max().date().isoformat()
                sector_members: dict[str, dict[str, Any]] = {}
                sector_metadata = {
                    "quality": "NOT_REQUIRED",
                    "source": "data_plan",
                    "effective_asof": sector_asof,
                }
                if data_plan.require_sectors:
                    historical_sectors = self.snapshots.load_sector_membership(sector_asof)
                    if historical_sectors is not None and not refresh_sectors:
                        sector_members, sector_metadata = historical_sectors
                    else:
                        sector_members = provider.load_sectors(refresh=refresh_sectors)
                        sector_metadata = {
                            "quality": "LIMITED",
                            "source": "current_fallback",
                            "effective_asof": sector_asof,
                        }
                benchmark_codes: list[str] = []
                benchmark_bars: dict[str, pd.DataFrame] = {}
                if data_plan.require_style_benchmarks:
                    benchmark_codes = [
                        "000300.CSI",
                        "000300.SH",
                        "000852.CSI",
                        "000852.SH",
                        "399006.SZ",
                    ]
                    benchmark_bars = provider.fetch_bars(
                        benchmark_codes, "1d", count, dividend_type="front", **bar_window
                    )
                    parameters["benchmark_actual_codes"] = {
                        "large": _resolved_benchmark(
                            benchmark_bars, ("000300.CSI", "000300.SH")
                        ),
                        "small": _resolved_benchmark(
                            benchmark_bars, ("000852.CSI", "000852.SH")
                        ),
                        "growth": _resolved_benchmark(benchmark_bars, ("399006.SZ",)),
                    }
                parameters["strategy_versions"] = {
                    item: self.strategies[item].metadata.version for item in component_ids
                }
                parameters["sector_membership_quality"] = sector_metadata["quality"]
                parameters["sector_membership_source"] = sector_metadata["source"]
                parameters["sector_membership_effective_asof"] = sector_metadata["effective_asof"]
                stage_durations["market_data"] = perf_counter() - stage_started

                stage_started = perf_counter()
                self._progress(
                    progress_callback,
                    "SNAPSHOT_WRITE",
                    0.47,
                    "正在写入原始数据快照",
                )
                snapshot_query = {**parameters, "count": count}
                self.snapshots.write_bars(
                    snapshot_id, "daily_front", daily_front, {**snapshot_query, "adjustment": "front"}
                )
                self.snapshots.write_bars(
                    snapshot_id, "daily_raw", daily_raw, {**snapshot_query, "adjustment": "none"}
                )
                if data_plan.require_style_benchmarks:
                    self.snapshots.write_bars(
                        snapshot_id,
                        "style_benchmarks",
                        benchmark_bars,
                        {**snapshot_query, "codes": benchmark_codes, "adjustment": "front"},
                    )
                self.snapshots.write_bars(
                    snapshot_id,
                    "market_index",
                    index_map,
                    {**snapshot_query, "codes": ["999999.SH"], "adjustment": "front"},
                )
                self.snapshots.write_records(
                    snapshot_id,
                    "security_master",
                    [{"code": code, "name": names.get(code, "")} for code in codes],
                    {**snapshot_query, "asof": sector_asof},
                )
                if data_plan.require_sectors:
                    sector_rows = _sector_membership_rows(sector_members)
                    sector_hash = _records_hash(sector_rows)
                    parameters["sector_membership_hash"] = sector_hash
                    self.snapshots.write_records(
                        snapshot_id,
                        "sector_membership",
                        sector_rows,
                        {
                            "quality": sector_metadata["quality"],
                            "source": sector_metadata["source"],
                            "asof": sector_asof,
                            "content_hash": sector_hash,
                        },
                    )
                stage_durations["core_snapshot"] = perf_counter() - stage_started

                lhb_history: dict[str, dict[str, LhbFeatures]] = {}
                market_activity = pd.DataFrame()
                if any(
                    self._runtime_adapter(item) == RuntimeAdapter.COURSE49_DAILY
                    for item in component_ids
                ):
                    stage_started = perf_counter()
                    self._progress(
                        progress_callback,
                        "COURSE49_DATA",
                        0.58,
                        "正在准备49课事件数据",
                    )
                    available_days = _trading_days(index_bars.index)
                    requested_start = _trading_day(start_date) if start_date else available_days.min()
                    requested_end = _trading_day(end_date) if end_date else available_days.max()
                    lhb_start_day = max(available_days.min(), requested_start - pd.Timedelta(days=30))
                    all_lhb_codes = _historical_limit_codes(
                        daily_raw,
                        names,
                        lhb_start_day,
                        requested_end,
                    )
                    retained_lhb_codes: set[str] | None = None
                    if all(
                        self._strategy_family(item) == "course49_v3"
                        for item in component_ids
                    ):
                        event_minimum_streak = min(
                            self._candidate_minimum_streak(item)
                            for item in component_ids
                        )
                        replay_eligibility = build_course49_eligibility_matrix(
                            daily_front, names
                        )
                        replay_candidates = build_course49_v3_candidate_matrix(
                            daily_raw,
                            names,
                            replay_eligibility,
                            minimum_streak=event_minimum_streak,
                        )
                        retained_lhb_codes = set(
                            replay_candidates.columns[
                                replay_candidates.fillna(False).astype(bool).any(axis=0)
                            ]
                        )
                        lhb_codes = sorted(set(all_lhb_codes) & retained_lhb_codes)
                        parameters["course49_event_scope"] = "strategy_candidates"
                    else:
                        event_minimum_streak = 1
                        lhb_codes = all_lhb_codes
                        parameters["course49_event_scope"] = "all_limit_symbols"
                    parameters["course49_event_minimum_streak"] = event_minimum_streak
                    parameters["course49_all_limit_symbols"] = len(all_lhb_codes)
                    lhb_symbols: set[str] = set()
                    event_query = {
                        "start": lhb_start_day.date().isoformat(),
                        "end": requested_end.date().isoformat(),
                        "candidate_symbols": len(lhb_codes),
                    }
                    lhb_writer = self.snapshots.open_record_writer(
                        snapshot_id,
                        "dragon_tiger",
                        event_query,
                        schema=_lhb_snapshot_schema(),
                    )
                    limit_writer = self.snapshots.open_record_writer(
                        snapshot_id,
                        "limit_behavior",
                        event_query,
                        schema=_lhb_snapshot_schema(),
                    )
                    if lhb_codes:
                        with lhb_writer, limit_writer:
                            for lhb_batch in provider.iter_course49_history(
                                lhb_codes,
                                lhb_start_day.strftime("%Y%m%d"),
                                requested_end.strftime("%Y%m%d"),
                                batch_callback=batch_progress(
                                    "COURSE49_EVENTS",
                                    0.60,
                                    0.78,
                                    "49课事件",
                                ),
                            ):
                                normalized_batch = normalize_lhb_history(
                                    lhb_batch, daily_raw
                                )
                                lhb_writer.append(
                                    flatten_lhb_history(
                                        normalized_batch, listed_only=True
                                    )
                                )
                                limit_writer.append(
                                    flatten_lhb_history(
                                        normalized_batch, limit_only=True
                                    )
                                )
                                lhb_symbols.update(
                                    code
                                    for code, events in normalized_batch.items()
                                    if any(feature.listed for feature in events.values())
                                )
                                if retained_lhb_codes is None:
                                    lhb_history.update(normalized_batch)
                                else:
                                    lhb_history.update(
                                        {
                                            code: events
                                            for code, events in normalized_batch.items()
                                            if code in retained_lhb_codes
                                        }
                                    )
                    else:
                        with lhb_writer, limit_writer:
                            pass
                    activity_start_day = max(
                        available_days.min(), requested_start - pd.Timedelta(days=180)
                    )
                    if data_plan.require_market_activity:
                        market_activity = normalize_market_activity(
                            provider.fetch_market_activity(
                                activity_start_day.strftime("%Y%m%d"),
                                requested_end.strftime("%Y%m%d"),
                            )
                        )
                    parameters["course49_candidate_symbols"] = len(lhb_codes)
                    parameters["course49_strategy_event_symbols"] = len(lhb_history)
                    parameters["lhb_symbols"] = len(lhb_symbols)
                    parameters["lhb_events"] = lhb_writer.row_count
                    parameters["limit_behavior_events"] = limit_writer.row_count
                    parameters["market_activity_days"] = len(market_activity)
                    if data_plan.require_market_activity:
                        self.snapshots.write_records(
                            snapshot_id,
                            "market_activity",
                            flatten_market_activity(market_activity),
                            {
                                "start": activity_start_day.date().isoformat(),
                                "end": requested_end.date().isoformat(),
                            },
                        )
                    for event_dataset in (
                        "dragon_tiger",
                        "limit_behavior",
                        "market_activity",
                    ):
                        if self.snapshots.has_dataset(snapshot_id, event_dataset):
                            self.database.add_snapshot_dependency(
                                snapshot_id,
                                "EVENT_FRAGMENT",
                                f"{snapshot_id}:{event_dataset}",
                                {
                                    "start": lhb_start_day.date().isoformat(),
                                    "end": requested_end.date().isoformat(),
                                },
                            )
                    stage_durations["course49_data"] = perf_counter() - stage_started

                parameters["effective_batch_sizes"] = provider.effective_batch_sizes()
                parameters["cache_status"] = "refresh" if refresh_data else "miss"
                parameters["source_snapshot_id"] = snapshot_id

                # Release the process-wide TQ channel before CPU-only strategy work.
                provider.__exit__(None, None, None)

                stage_started = perf_counter()
                if "course49_system" in component_ids:
                    self._progress(
                        progress_callback,
                        "COURSE49_CONTEXT",
                        0.80,
                        "正在构建共享市场、题材、龙头和资金上下文",
                    )
                    self._progress(
                        progress_callback,
                        "PLAYBOOK_EVALUATION",
                        0.82,
                        "正在评估生产剧本",
                    )
                else:
                    self._progress(progress_callback, "STRATEGY_EXECUTION", 0.82, "正在执行策略")
                results = self._execute_components(
                    backtest_id,
                    strategy_id,
                    component_ids,
                    capital_weights,
                    BacktestDataset(
                        names,
                        daily_front,
                        daily_raw,
                        index_bars,
                        sector_members,
                        benchmark_bars,
                        lhb_history,
                        market_activity,
                    ),
                    count,
                    start_date,
                    end_date,
                    execution_config,
                    snapshot_id=snapshot_id,
                    parameters=parameters,
                )
                if "course49_system" in component_ids:
                    self._progress(
                        progress_callback,
                        "ROUTING",
                        0.94,
                        "剧本去重、排序和资金路由已完成",
                    )
                stage_durations["strategy_execution"] = perf_counter() - stage_started

                metrics = self._combine_results(results)
                metrics["backtest_id"] = backtest_id
                metrics["strategy_id"] = strategy_id
                metrics["snapshot_id"] = snapshot_id
                metrics["parameters"] = parameters
                metrics["components"] = {key: value["metrics"] for key, value in results.items()}
                stage_durations["total"] = perf_counter() - run_started
                parameters["peak_memory_bytes"] = psutil.Process().memory_info().rss
                parameters["stage_durations_seconds"] = {
                    key: round(value, 3) for key, value in stage_durations.items()
                }
                self._progress(
                    progress_callback,
                    "PERSISTENCE",
                    0.97,
                    "正在保存回测、状态和证据",
                )
                self.database.execute(
                    """UPDATE backtests SET status='SUCCEEDED', finished_at=?, snapshot_id=?,
                    parameters_json=?, metrics_json=?
                    WHERE backtest_id=?""",
                    (
                        datetime.now().astimezone().isoformat(),
                        snapshot_id,
                        json.dumps(parameters, ensure_ascii=False),
                        json.dumps(metrics, ensure_ascii=False),
                        backtest_id,
                    ),
                )
                self.cache.commit_snapshot(build_cache_key, data_cache_key, snapshot_id)
                cache_build_started = False
                self.cache.prune()
                self.cache.memory.put(
                    f"snapshot:{snapshot_id}",
                    BacktestDataset(
                        names,
                        daily_front,
                        daily_raw,
                        index_bars,
                        sector_members,
                        benchmark_bars,
                        lhb_history,
                        market_activity,
                    ),
                )
                self._progress(progress_callback, "COMPLETED", 1.0, "回测完成")
                return metrics
        except Exception as exc:
            if cache_build_started:
                self.cache.fail(build_cache_key, str(exc))
            self.database.execute(
                "UPDATE backtests SET status='FAILED', finished_at=?, error=? WHERE backtest_id=?",
                (datetime.now().astimezone().isoformat(), str(exc), backtest_id),
            )
            raise
        finally:
            cache_lock.release()

    @staticmethod
    def _progress(
        callback: Callable[..., None] | None,
        phase: str,
        progress: float,
        detail: str,
        *,
        cache_status: str = "",
        waiting_reason: str = "",
    ) -> None:
        if callback is not None:
            callback(
                phase=phase,
                progress=max(0.0, min(1.0, float(progress))),
                detail=detail,
                cache_status=cache_status,
                waiting_reason=waiting_reason,
            )

    def _load_snapshot_dataset(
        self,
        snapshot_id: str,
        data_plan: DataPlan,
    ) -> tuple[BacktestDataset, bool]:
        memory_key = f"snapshot:{snapshot_id}"
        cached = self.cache.memory.get(memory_key)
        if isinstance(cached, BacktestDataset):
            return cached, True

        security_master = self.snapshots.load_records(snapshot_id, "security_master")
        names = {
            str(row["code"]): str(row.get("name", ""))
            for row in security_master.to_dict("records")
        }
        daily_front = self.snapshots.load_bars(snapshot_id, "daily_front")
        daily_raw = self.snapshots.load_bars(snapshot_id, "daily_raw")
        index_map = self.snapshots.load_bars(snapshot_id, "market_index")
        if not index_map:
            raise ValueError(f"Snapshot market index is empty: {snapshot_id}")
        index_bars = next(iter(index_map.values()))
        sector_members: dict[str, dict[str, Any]] = {}
        if data_plan.require_sectors:
            sector_members = _sector_membership_from_frame(
                self.snapshots.load_records(snapshot_id, "sector_membership")
            )
        benchmark_bars = (
            self.snapshots.load_bars(snapshot_id, "style_benchmarks")
            if data_plan.require_style_benchmarks
            else {}
        )
        lhb_frames: list[pd.DataFrame] = []
        if data_plan.require_course49_events:
            for dataset_name in ("dragon_tiger", "limit_behavior"):
                frame = self.snapshots.load_records(snapshot_id, dataset_name)
                if not frame.empty:
                    lhb_frames.append(frame)
        if lhb_frames:
            lhb_frame = pd.concat(lhb_frames, ignore_index=True)
            lhb_frame = lhb_frame.drop_duplicates(["code", "event_date"], keep="last")
            lhb_history = inflate_lhb_history(lhb_frame.to_dict("records"))
        else:
            lhb_history = {}
        market_activity = (
            _market_activity_from_frame(
                self.snapshots.load_records(snapshot_id, "market_activity")
            )
            if data_plan.require_market_activity
            else pd.DataFrame()
        )
        result = BacktestDataset(
            names,
            daily_front,
            daily_raw,
            index_bars,
            sector_members,
            benchmark_bars,
            lhb_history,
            market_activity,
        )
        self.cache.memory.put(memory_key, result)
        return result, False

    def _run_cached_backtest(
        self,
        backtest_id: str,
        strategy_id: str,
        component_ids: tuple[str, ...],
        capital_weights: dict[str, float],
        daily_count: int,
        start_date: str | None,
        end_date: str | None,
        execution_config: PortfolioConfig,
        parameters: dict[str, Any],
        data_plan: DataPlan,
        snapshot_id: str,
        cache_entry_key: str,
        hit_type: str,
        progress_callback: Callable[..., None] | None,
        run_started: float,
    ) -> dict[str, Any]:
        self._progress(
            progress_callback,
            "SNAPSHOT_LOAD",
            0.12,
            "正在加载可复用快照",
            cache_status=hit_type,
        )
        stage_started = perf_counter()
        dataset, memory_hit = self._load_snapshot_dataset(snapshot_id, data_plan)
        load_duration = perf_counter() - stage_started
        _ensure_date_range_available(dataset.index_bars, start_date, end_date)
        end_eligible = filter_universe(
            _slice_to_date(dataset.daily_front, end_date),
            dataset.names,
            StrategyConfig(tdx_root=self.config.tdx_root, daily_lookback=daily_count),
        )
        if all(
            self._runtime_adapter(item) == RuntimeAdapter.GENERIC_DAILY
            and self._required_codes(item)
            for item in component_ids
        ):
            end_eligible = {
                code: frame
                for code, frame in dataset.daily_front.items()
                if not frame.empty
            }
        parameters.update(
            {
                "loaded_symbols": len(dataset.daily_front),
                "resolved_symbols": len(end_eligible),
                "stock_pool_hash": _stock_pool_hash(list(end_eligible)),
                "universe_distribution": _universe_distribution(list(end_eligible)),
                "strategy_versions": {
                    item: self.strategies[item].metadata.version for item in component_ids
                },
                "cache_status": "memory_hit" if memory_hit else hit_type,
                "cache_hit_type": hit_type,
                "source_snapshot_id": snapshot_id,
                "effective_batch_sizes": {"source": "snapshot", "bars": [], "events": []},
            }
        )
        if data_plan.require_style_benchmarks:
            parameters["benchmark_actual_codes"] = {
                "large": _resolved_benchmark(
                    dataset.benchmark_bars, ("000300.CSI", "000300.SH")
                ),
                "small": _resolved_benchmark(
                    dataset.benchmark_bars, ("000852.CSI", "000852.SH")
                ),
                "growth": _resolved_benchmark(dataset.benchmark_bars, ("399006.SZ",)),
            }
        if data_plan.require_sectors:
            sector_query = self.snapshots.dataset_query(snapshot_id, "sector_membership")
            parameters["sector_membership_quality"] = sector_query.get("quality", "LIMITED")
            parameters["sector_membership_source"] = sector_query.get("source", "snapshot")
            parameters["sector_membership_effective_asof"] = sector_query.get("asof", "")
            parameters["sector_membership_hash"] = sector_query.get("content_hash", "")

        if "course49_system" in component_ids:
            self._progress(
                progress_callback,
                "COURSE49_CONTEXT",
                0.30,
                "快照已就绪，正在构建共享上下文",
                cache_status=parameters["cache_status"],
            )
            self._progress(
                progress_callback,
                "PLAYBOOK_EVALUATION",
                0.35,
                "正在评估生产剧本",
                cache_status=parameters["cache_status"],
            )
        else:
            self._progress(
                progress_callback,
                "STRATEGY_EXECUTION",
                0.35,
                "快照已就绪，正在执行策略",
                cache_status=parameters["cache_status"],
            )
        strategy_started = perf_counter()
        results = self._execute_components(
            backtest_id,
            strategy_id,
            component_ids,
            capital_weights,
            dataset,
            daily_count,
            start_date,
            end_date,
            execution_config,
            snapshot_id=snapshot_id,
            parameters=parameters,
        )
        metrics = self._combine_results(results)
        parameters["peak_memory_bytes"] = psutil.Process().memory_info().rss
        parameters["stage_durations_seconds"] = {
            "snapshot_load": round(load_duration, 3),
            "strategy_execution": round(perf_counter() - strategy_started, 3),
            "total": round(perf_counter() - run_started, 3),
        }
        metrics.update(
            {
                "backtest_id": backtest_id,
                "strategy_id": strategy_id,
                "snapshot_id": snapshot_id,
                "parameters": parameters,
                "components": {key: value["metrics"] for key, value in results.items()},
            }
        )
        self.database.execute(
            """UPDATE backtests SET status='SUCCEEDED', finished_at=?, snapshot_id=?,
            parameters_json=?, metrics_json=? WHERE backtest_id=?""",
            (
                datetime.now().astimezone().isoformat(),
                snapshot_id,
                json.dumps(parameters, ensure_ascii=False),
                json.dumps(metrics, ensure_ascii=False),
                backtest_id,
            ),
        )
        self.cache.touch(cache_entry_key)
        self._progress(
            progress_callback,
            "COMPLETED",
            1.0,
            "回测完成",
            cache_status=parameters["cache_status"],
        )
        return metrics

    def _execute_components(
        self,
        backtest_id: str,
        requested_strategy_id: str,
        component_ids: tuple[str, ...],
        capital_weights: dict[str, float],
        dataset: BacktestDataset,
        daily_count: int,
        start_date: str | None,
        end_date: str | None,
        execution_config: PortfolioConfig,
        *,
        snapshot_id: str,
        parameters: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        prepared = self._prepare_course49_features(
            snapshot_id,
            component_ids,
            dataset,
        )
        if "course49_system" in component_ids:
            self._progress(
                progress_callback,
                "ROUTING",
                0.92,
                "剧本去重、排序和资金路由已完成",
                cache_status=parameters["cache_status"],
            )
        prepared["playbook_ids"] = list(parameters.get("playbook_ids") or [])
        prepared["stock_pool_hash"] = str(parameters.get("stock_pool_hash") or "")
        prepared["sector_membership_hash"] = str(
            parameters.get("sector_membership_hash") or ""
        )
        parameters["feature_cache"] = prepared.get("cache_status", {})
        results: dict[str, dict[str, Any]] = {}
        chan_reserved: dict[str, set[str]] = {}
        ordered_components = sorted(
            component_ids,
            key=lambda item: self._runtime_adapter(item) != RuntimeAdapter.CHAN_DAILY,
        )
        for component_id in ordered_components:
            adapter = self._runtime_adapter(component_id)
            metadata = self.strategies[component_id].metadata
            if adapter == RuntimeAdapter.CHAN_DAILY:
                results[component_id] = self._run_chan(
                    backtest_id,
                    dataset.names,
                    dataset.daily_front,
                    dataset.daily_raw,
                    dataset.index_bars,
                    dataset.sector_members,
                    daily_count,
                    start_date,
                    end_date,
                    capital_weight=capital_weights[component_id],
                )
                chan_reserved = _reserved_codes_by_date(results[component_id]["trades"])
            elif adapter == RuntimeAdapter.COURSE49_DAILY:
                results[component_id] = self._run_course49(
                    backtest_id,
                    dataset.names,
                    dataset.daily_front,
                    dataset.daily_raw,
                    dataset.index_bars,
                    dataset.sector_members,
                    start_date,
                    end_date,
                    chan_reserved if requested_strategy_id == "combined" else {},
                    dataset.lhb_history,
                    dataset.market_activity,
                    strategy_id=component_id,
                    benchmark_bars=dataset.benchmark_bars,
                    capital_weight=capital_weights[component_id],
                    execution_config=execution_config,
                    prepared_features=prepared,
                )
            elif metadata.execution_model == ExecutionModel.MULTI_LEG:
                results[component_id] = self._run_group_intent_strategy(
                    backtest_id,
                    component_id,
                    dataset.names,
                    dataset.daily_front,
                    dataset.daily_raw,
                    dataset.index_bars,
                    dataset.sector_members,
                    dataset.benchmark_bars,
                    start_date,
                    end_date,
                    capital_weight=capital_weights[component_id],
                    execution_config=execution_config,
                )
            else:
                results[component_id] = self._run_generic_signal_strategy(
                    backtest_id,
                    component_id,
                    dataset.names,
                    dataset.daily_front,
                    dataset.daily_raw,
                    dataset.index_bars,
                    dataset.sector_members,
                    dataset.benchmark_bars,
                    start_date,
                    end_date,
                    capital_weight=capital_weights[component_id],
                    execution_config=execution_config,
                )
        return results

    def _prepare_course49_features(
        self,
        snapshot_id: str,
        component_ids: tuple[str, ...],
        dataset: BacktestDataset,
    ) -> dict[str, Any]:
        course49_ids = tuple(
            item
            for item in component_ids
            if self._runtime_adapter(item) == RuntimeAdapter.COURSE49_DAILY
        )
        if not course49_ids:
            return {"cache_status": {}}
        cache_status: dict[str, str] = {}
        market_key = self.cache.feature_key(
            snapshot_id, "course49_market", CONTEXT_VERSION + "-market-2"
        )
        market_matrix, cache_status["market_matrix"] = self.cache.get_or_build_feature_frames(
            market_key,
            lambda: build_course49_market_matrix(
                dataset.daily_raw,
                dataset.names,
                dataset.market_activity,
            ),
        )
        prepared: dict[str, Any] = {
            "market_matrix": market_matrix,
            "cache_status": cache_status,
            "candidate_matrices": {},
        }
        families = {self._strategy_family(item) for item in course49_ids}
        if "course49_v2" in families:
            feature_key = self.cache.feature_key(
                snapshot_id, "course49_v2_features", CONTEXT_VERSION
            )
            feature_matrix, cache_status["feature_matrix"] = self.cache.get_or_build_feature_frames(
                feature_key,
                lambda: build_course49_feature_matrix(
                    dataset.daily_front,
                    dataset.daily_raw,
                    dataset.names,
                ),
            )
            prepared["feature_matrix"] = feature_matrix
        if families & {"course49_v2", "course49_v3"}:
            eligibility_key = self.cache.feature_key(
                snapshot_id,
                "course49_eligibility",
                CONTEXT_VERSION,
                {"listing_bars": 60, "turnover": 20_000_000},
            )
            eligibility, cache_status["eligibility_matrix"] = self.cache.get_or_build_feature_frames(
                eligibility_key,
                lambda: build_course49_eligibility_matrix(
                    dataset.daily_front,
                    dataset.names,
                ),
            )
            prepared["eligibility_matrix"] = eligibility
        if "course49_v2" in families:
            candidates_key = self.cache.feature_key(
                snapshot_id,
                "course49_v2_candidates",
                CONTEXT_VERSION,
            )
            v2_candidates, cache_status["v2_candidate_matrix"] = (
                self.cache.get_or_build_feature_frames(
                    candidates_key,
                    lambda: _course49_v2_candidate_matrix(
                        feature_matrix,
                        eligibility,
                    ),
                )
            )
            prepared["v2_candidate_matrix"] = v2_candidates
        if "course49_v3" in families:
            for minimum_streak in sorted(
                {
                    self._candidate_minimum_streak(item)
                    for item in course49_ids
                    if self._strategy_family(item) == "course49_v3"
                }
            ):
                candidate_key = self.cache.feature_key(
                    snapshot_id,
                    "course49_candidates",
                    "1",
                    {"minimum_streak": minimum_streak},
                )
                candidates, status = self.cache.get_or_build_feature_frames(
                    candidate_key,
                    lambda minimum_streak=minimum_streak: build_course49_v3_candidate_matrix(
                        dataset.daily_raw,
                        dataset.names,
                        eligibility,
                        minimum_streak=minimum_streak,
                    ),
                )
                prepared["candidate_matrices"][minimum_streak] = candidates
                cache_status[f"candidate_matrix_{minimum_streak}"] = status
        return prepared

    def replay_course49(
        self,
        source_backtest_id: str,
        *,
        strategy_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        execution_cost_multiplier: float = 1.0,
    ) -> dict[str, Any]:
        source_rows = self.database.query(
            "SELECT * FROM backtests WHERE backtest_id=?",
            (source_backtest_id,),
        )
        if not source_rows:
            raise ValueError(f"Unknown source backtest: {source_backtest_id}")
        source = source_rows[0]
        snapshot_id = str(source.get("snapshot_id") or "")
        if not snapshot_id:
            raise ValueError(f"Backtest {source_backtest_id} has no immutable snapshot")
        source_strategy = str(source.get("strategy_id") or "")
        strategy_id = strategy_id or source_strategy
        if strategy_id not in self.strategies or not self._strategy_family(strategy_id).startswith("course49_"):
            raise ValueError("Snapshot replay currently supports standalone Course49 strategies only")
        if not 0.0 <= execution_cost_multiplier <= 5.0:
            raise ValueError("Execution cost multiplier must be between 0 and 5")
        start_date, end_date = _validate_date_range(
            start_date or str(source.get("start_date") or "") or None,
            end_date or str(source.get("end_date") or "") or None,
        )
        try:
            source_parameters = json.loads(str(source.get("parameters_json") or "{}"))
        except json.JSONDecodeError:
            source_parameters = {}
        target_minimum_streak = self._candidate_minimum_streak(strategy_id)
        source_minimum_streak = _snapshot_event_minimum_streak(source_parameters)
        if target_minimum_streak < source_minimum_streak:
            raise ValueError(
                "Snapshot event coverage starts at "
                f"{source_minimum_streak} consecutive limit-ups, but {strategy_id} "
                f"requires coverage from {target_minimum_streak}; run a fresh backtest"
            )
        parameters = {
            **source_parameters,
            "strategy_id": strategy_id,
            "components": [strategy_id],
            "composition_mode": "standalone",
            "capital_weights": {strategy_id: 0.5},
            "start_date": start_date,
            "end_date": end_date,
            "execution_cost_multiplier": execution_cost_multiplier,
            "snapshot_replay": True,
            "source_backtest_id": source_backtest_id,
            "source_snapshot_id": snapshot_id,
            "course49_event_minimum_streak": source_minimum_streak,
            "strategy_versions": {
                strategy_id: self.strategies[strategy_id].metadata.version
            },
        }
        backtest_id = uuid4().hex
        started_at = datetime.now().astimezone().isoformat()
        self._progress(
            progress_callback,
            "PERSISTENCE",
            0.97,
            "正在保存回测、状态和证据",
            cache_status=parameters["cache_status"],
        )
        self.database.execute(
            """INSERT INTO backtests
            (backtest_id, strategy_id, status, started_at, start_date, end_date, parameters_json)
            VALUES (?, ?, 'RUNNING', ?, ?, ?, ?)""",
            (
                backtest_id,
                strategy_id,
                started_at,
                start_date,
                end_date,
                json.dumps(parameters, ensure_ascii=False),
            ),
        )
        replay_started = perf_counter()
        try:
            daily_front = self.snapshots.load_bars(snapshot_id, "daily_front")
            daily_raw = self.snapshots.load_bars(snapshot_id, "daily_raw")
            benchmark_bars = self.snapshots.load_bars(snapshot_id, "style_benchmarks")
            if self.snapshots.has_dataset(snapshot_id, "market_index"):
                index_map = self.snapshots.load_bars(snapshot_id, "market_index")
                index_bars = next(iter(index_map.values()))
            else:
                index_bars = _snapshot_index_fallback(benchmark_bars, source_parameters)
                parameters["market_index_source"] = "style_benchmark_fallback"
            _ensure_date_range_available(index_bars, start_date, end_date)

            if self.snapshots.has_dataset(snapshot_id, "security_master"):
                security_master = self.snapshots.load_records(snapshot_id, "security_master")
                names = {
                    str(row["code"]): str(row.get("name", ""))
                    for row in security_master.to_dict("records")
                }
            else:
                with TdxProvider(self.config, __file__) as provider:
                    _, names = provider.list_a_shares()
                parameters["security_master_source"] = "current_fallback"

            sector_frame = self.snapshots.load_records(snapshot_id, "sector_membership")
            sector_members = _sector_membership_from_frame(sector_frame)
            lhb_frames = []
            for dataset in ("dragon_tiger", "limit_behavior"):
                if self.snapshots.has_dataset(snapshot_id, dataset):
                    lhb_frames.append(self.snapshots.load_records(snapshot_id, dataset))
            if lhb_frames:
                lhb_frame = pd.concat(lhb_frames, ignore_index=True)
                lhb_frame = lhb_frame.drop_duplicates(["code", "event_date"], keep="last")
                lhb_history = inflate_lhb_history(lhb_frame.to_dict("records"))
            else:
                lhb_history = {}
            activity_frame = self.snapshots.load_records(snapshot_id, "market_activity")
            market_activity = _market_activity_from_frame(activity_frame)
            execution_config = _execution_cost_config(
                self.config.portfolio, execution_cost_multiplier
            )
            result = self._run_course49(
                backtest_id,
                names,
                daily_front,
                daily_raw,
                index_bars,
                sector_members,
                start_date,
                end_date,
                {},
                lhb_history,
                market_activity,
                strategy_id=strategy_id,
                benchmark_bars=benchmark_bars,
                capital_weight=0.5,
                execution_config=execution_config,
            )
            metrics = self._combine_results({strategy_id: result})
            parameters["stage_durations_seconds"] = {
                "snapshot_replay_total": round(perf_counter() - replay_started, 3)
            }
            metrics.update(
                {
                    "backtest_id": backtest_id,
                    "strategy_id": strategy_id,
                    "snapshot_id": snapshot_id,
                    "parameters": parameters,
                    "components": {strategy_id: result["metrics"]},
                }
            )
            self.database.execute(
                """UPDATE backtests SET status='SUCCEEDED', finished_at=?, snapshot_id=?,
                parameters_json=?, metrics_json=? WHERE backtest_id=?""",
                (
                    datetime.now().astimezone().isoformat(),
                    snapshot_id,
                    json.dumps(parameters, ensure_ascii=False),
                    json.dumps(metrics, ensure_ascii=False),
                    backtest_id,
                ),
            )
            return metrics
        except Exception as exc:
            self.database.execute(
                "UPDATE backtests SET status='FAILED', finished_at=?, error=? WHERE backtest_id=?",
                (datetime.now().astimezone().isoformat(), str(exc), backtest_id),
            )
            raise

    def _run_chan(
        self,
        backtest_id: str,
        names: dict[str, str],
        daily_front: dict[str, pd.DataFrame],
        daily_raw: dict[str, pd.DataFrame],
        index_bars: pd.DataFrame,
        sector_members: dict[str, dict[str, Any]],
        daily_count: int,
        start_date: str | None,
        end_date: str | None,
        *,
        capital_weight: float,
    ) -> dict[str, Any]:
        output_dir = self.config.runtime_dir / "backtests" / backtest_id / "chan_v1"
        risk = replace(
            RiskConfig(),
            initial_cash=self.config.portfolio.initial_cash * capital_weight,
            max_positions=self.config.portfolio.max_strategy_positions,
            max_position_weight=self.config.portfolio.max_strategy_symbol_weight,
        )
        legacy_config = replace(
            StrategyConfig(),
            tdx_root=self.config.tdx_root,
            output_dir=output_dir,
            cache_dir=self.config.cache_dir / "legacy",
            daily_lookback=daily_count,
            risk=risk,
        )
        eligible = filter_universe(daily_front, names, legacy_config)
        eligible_raw = {code: frame for code, frame in daily_raw.items() if code in eligible}
        schedule, _ = build_daily_schedule(index_bars, eligible, names, sector_members, legacy_config)
        schedule = _slice_schedule(schedule, start_date, end_date)
        candidate_codes = sorted(set().union(*(set(items) for items in schedule.values())) if schedule else set())
        signal_front = {code: eligible[code] for code in candidate_codes if code in eligible}
        signal_raw = {code: eligible_raw[code] for code in candidate_codes if code in eligible_raw}
        metrics = run_legacy_chan_backtest(
            legacy_config,
            names,
            eligible,
            eligible_raw,
            index_bars,
            sector_members,
            signal_front,
            signal_raw,
            start_date=start_date,
            end_date=end_date,
        )
        equity = _read_csv(output_dir / "backtest_equity.csv")
        trades = _read_csv(output_dir / "backtest_trades.csv")
        if equity.empty:
            fallback_days = list(_trading_days(index_bars.index).unique())
            if start_date:
                fallback_days = [day for day in fallback_days if day >= _trading_day(start_date)]
            if end_date:
                fallback_days = [day for day in fallback_days if day <= _trading_day(end_date)]
            fallback = fallback_days[-1]
            equity = pd.DataFrame(
                [{"timestamp": fallback.isoformat(), "equity": risk.initial_cash, "cash": risk.initial_cash, "positions": 0}]
            )
        metrics = {
            **metrics,
            **_performance_metrics(equity, trades, risk.initial_cash),
        }
        self._persist_rows(backtest_id, "chan_v1", equity, trades)
        return {"metrics": metrics, "equity": equity, "trades": trades}

    def _run_course49(
        self,
        backtest_id: str,
        names: dict[str, str],
        daily_front: dict[str, pd.DataFrame],
        daily_raw: dict[str, pd.DataFrame],
        index_bars: pd.DataFrame,
        sector_members: dict[str, dict[str, Any]],
        start_date: str | None,
        end_date: str | None,
        reserved_codes_by_date: dict[str, set[str]],
        lhb_history: dict[str, dict[str, LhbFeatures]],
        market_activity: pd.DataFrame,
        *,
        strategy_id: str,
        benchmark_bars: dict[str, pd.DataFrame],
        capital_weight: float,
        execution_config: PortfolioConfig,
        prepared_features: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        strategy = self.strategies[strategy_id]
        family = self._strategy_family(strategy_id)
        is_v3 = family == "course49_v3"
        is_adaptive = family != "course49_v1"
        runtime_state: dict[str, dict[str, Any]] = {}
        prepared_features = prepared_features or {}
        feature_matrix: dict[str, pd.DataFrame] | pd.DataFrame = {}
        if family == "course49_v2":
            feature_matrix = prepared_features.get("feature_matrix")
            if feature_matrix is None:
                feature_matrix = build_course49_feature_matrix(
                    daily_front, daily_raw, names
                )
        eligibility_matrix = (
            prepared_features.get("eligibility_matrix")
            if prepared_features.get("eligibility_matrix") is not None
            else build_course49_eligibility_matrix(daily_front, names)
            if is_adaptive
            else pd.DataFrame()
        )
        v2_candidate_matrix = pd.DataFrame()
        if family == "course49_v2":
            v2_candidate_matrix = prepared_features.get("v2_candidate_matrix")
            if v2_candidate_matrix is None:
                v2_candidate_matrix = _course49_v2_candidate_matrix(
                    feature_matrix,
                    eligibility_matrix,
                )
        candidate_matrix = pd.DataFrame()
        if is_v3:
            minimum_streak = self._candidate_minimum_streak(strategy_id)
            candidate_matrix = prepared_features.get("candidate_matrices", {}).get(
                minimum_streak
            )
            if candidate_matrix is None:
                candidate_matrix = build_course49_v3_candidate_matrix(
                    daily_raw,
                    names,
                    eligibility_matrix,
                    minimum_streak=minimum_streak,
                )
        market_matrix = prepared_features.get("market_matrix")
        if market_matrix is None:
            market_matrix = build_course49_market_matrix(daily_raw, names, market_activity)
        cash = self.config.portfolio.initial_cash * capital_weight
        initial_cash = cash
        positions: dict[str, HistoricalPosition] = {}
        pending: list[PlatformSignal] = []
        trades: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        dates = list(_trading_days(index_bars.index).unique())
        if start_date:
            dates = [item for item in dates if item >= _trading_day(start_date)]
        if end_date:
            dates = [item for item in dates if item <= _trading_day(end_date)]
        all_dates = list(_trading_days(index_bars.index).unique())
        if not dates:
            raise ValueError("Backtest date range contains no trading days")

        for current_date in dates:
            history_dates = [item for item in all_dates if item <= current_date]
            if len(history_dates) < 60:
                continue
            eligible_codes: set[str] = set()
            if is_v3:
                eligible_codes = _matrix_codes_at(eligibility_matrix, current_date)
                candidate_codes = _matrix_codes_at(candidate_matrix, current_date)
                required_codes = candidate_codes | set(positions) | {
                    signal.code for signal in pending
                }
                visible_codes = required_codes & eligible_codes
                visible_front = _slice_daily_codes(daily_front, current_date, visible_codes)
                visible_raw = _slice_daily_codes(daily_raw, current_date, visible_codes)
            elif family == "course49_v2":
                eligible_codes = _matrix_codes_at(eligibility_matrix, current_date)
                candidate_codes = _matrix_codes_at(v2_candidate_matrix, current_date)
                required_codes = candidate_codes | set(positions) | {
                    signal.code for signal in pending
                }
                visible_codes = required_codes & eligible_codes
                visible_front = _slice_daily_codes(
                    daily_front, current_date, visible_codes
                )
                visible_raw = _slice_daily_codes(
                    daily_raw, current_date, visible_codes
                )
            else:
                visible_front = _slice_daily(daily_front, current_date)
                visible_raw = _slice_daily(daily_raw, current_date)
            visible_benchmarks = _slice_daily(benchmark_bars, current_date)
            if is_adaptive and eligibility_matrix.empty:
                visible_front = filter_universe(
                    visible_front,
                    names,
                    StrategyConfig(tdx_root=self.config.tdx_root, daily_lookback=120),
                )
                visible_raw = {
                    code: frame for code, frame in visible_raw.items() if code in visible_front
                }
            if not visible_raw and not is_adaptive:
                continue
            reserved = reserved_codes_by_date.get(current_date.date().isoformat(), set())
            cash, day_trades = self._fill_course49_pending(
                pending,
                current_date,
                visible_raw,
                names,
                positions,
                cash,
                reserved,
                execution_config,
            )
            trades.extend(day_trades)
            filled_signal_ids = {item["signal_id"] for item in day_trades}
            pending = _roll_course49_pending(
                pending,
                current_date,
                positions,
                filled_signal_ids,
            )

            for code, position in positions.items():
                frame = visible_raw.get(code)
                if frame is not None and not frame.empty:
                    position.last_price = float(pd.to_numeric(frame["Close"], errors="coerce").dropna().iloc[-1])
            position_rows = [
                {
                    "code": position.code,
                    "stop_price": position.stop_price,
                    "entry_time": position.entry_date,
                    "average_price": position.average_price,
                    "evidence": position.evidence,
                }
                for position in positions.values()
            ]
            try:
                scan_arguments: dict[str, Any] = dict(
                    run_id=backtest_id,
                    front_bars=visible_front,
                    raw_bars=visible_raw,
                    names=names,
                    sector_members=sector_members,
                    positions=position_rows,
                    lhb_history=lhb_history,
                    market_activity=market_activity.loc[:current_date]
                    if not market_activity.empty
                    else market_activity,
                    market_matrix=market_matrix,
                )
                if is_adaptive:
                    scan_arguments.update(
                        benchmark_bars=visible_benchmarks,
                        runtime_state=runtime_state,
                        feature_matrix=feature_matrix,
                        asof=current_date,
                    )
                if family == "course49_v2":
                    scan_arguments["eligible_codes"] = eligible_codes
                if strategy_id == "course49_system":
                    scan_arguments["playbook_ids"] = tuple(
                        prepared_features.get("playbook_ids")
                        or []
                    ) or None
                    scan_arguments["context_metadata"] = {
                        "stock_pool_hash": prepared_features.get("stock_pool_hash", ""),
                        "sector_membership_hash": prepared_features.get(
                            "sector_membership_hash", ""
                        ),
                        "market_count": len(daily_front),
                        "eligible_count": len(eligible_codes),
                    }
                result = strategy.scan(**scan_arguments)
                if is_adaptive:
                    runtime_state = dict(result.state.get("runtime_state") or {})
                    self.database.save_backtest_state(
                        backtest_id,
                        strategy_id,
                        current_date.date().isoformat(),
                        result.state,
                    )
                for signal in sorted(result.signals, key=lambda item: (-item.strength, item.code)):
                    pending = [
                        item
                        for item in pending
                        if not (item.code == signal.code and item.side == signal.side)
                    ]
                    pending.append(signal)
            except ValueError:
                pass
            equity = cash + sum(position.quantity * position.last_price for position in positions.values())
            equity_rows.append(
                {
                    "timestamp": current_date.replace(hour=15).isoformat(),
                    "equity": equity,
                    "cash": cash,
                    "positions": len(positions),
                }
            )

        equity = pd.DataFrame(equity_rows)
        trade_frame = pd.DataFrame(trades)
        if equity.empty:
            equity = pd.DataFrame(
                [{"timestamp": dates[-1].isoformat(), "equity": initial_cash, "cash": initial_cash, "positions": 0}]
            )
        metrics = {
            "data_status": "ok",
            **_performance_metrics(equity, trade_frame, initial_cash),
            "course49_attribution": _course49_attribution(trade_frame),
        }
        if is_adaptive:
            metrics["style_attribution"] = _evidence_attribution(trade_frame, "market_style")
            metrics["trade_mode_attribution"] = _evidence_attribution(trade_frame, "trade_mode")
            metrics["exit_reason_attribution"] = _exit_reason_attribution(trade_frame)
            metrics["average_capital_invested"] = _average_capital_invested(equity, initial_cash)
        if strategy_id == "course49_system":
            metrics["playbook_attribution"] = _evidence_attribution(
                trade_frame, "playbook_id"
            )
        self._persist_rows(backtest_id, strategy_id, equity, trade_frame)
        return {"metrics": metrics, "equity": equity, "trades": trade_frame}

    def _fill_course49_pending(
        self,
        pending: list[PlatformSignal],
        current_date: pd.Timestamp,
        bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        positions: dict[str, HistoricalPosition],
        cash: float,
        reserved_codes: set[str],
        config: PortfolioConfig | None = None,
    ) -> tuple[float, list[dict[str, Any]]]:
        trades: list[dict[str, Any]] = []
        config = config or self.config.portfolio
        for signal in sorted(pending, key=lambda item: (item.side != "SELL", -item.strength, item.code)):
            current_day = _trading_day(current_date)
            if _trading_day(signal.generated_at) >= current_day:
                continue
            frame = bars.get(signal.code)
            if frame is None or frame.empty:
                continue
            frame_days = _trading_days(frame.index)
            row = frame[frame_days == current_day]
            prior = frame[frame_days < current_day]
            if row.empty or prior.empty:
                continue
            open_price = float(row["Open"].iloc[-1])
            previous_close = float(prior["Close"].iloc[-1])
            ratio = price_limit_ratio(signal.code, names.get(signal.code, ""))
            if signal.side == "BUY":
                if open_price >= previous_close * (1 + ratio - 0.001):
                    continue
                distinct = set(positions) | reserved_codes
                if signal.code in positions or signal.code in reserved_codes:
                    continue
                if len(positions) >= config.max_strategy_positions or len(distinct) >= config.max_total_positions:
                    continue
                execution = open_price * (1 + config.slippage_rate)
                strategy_equity = cash + sum(item.quantity * item.last_price for item in positions.values())
                budget = min(cash, strategy_equity * min(signal.target_weight, config.max_strategy_symbol_weight))
                quantity = int(budget / execution / config.board_lot) * config.board_lot
                while quantity > 0:
                    value = quantity * execution
                    fees = max(config.min_commission, value * config.commission_rate)
                    if value + fees <= cash:
                        break
                    quantity -= config.board_lot
                if quantity <= 0:
                    continue
                value = quantity * execution
                fees = max(config.min_commission, value * config.commission_rate)
                cash -= value + fees
                positions[signal.code] = HistoricalPosition(
                    signal.code,
                    quantity,
                    execution,
                    current_date.date().isoformat(),
                    signal.stop_price or execution * 0.95,
                    execution,
                    json.dumps(signal.evidence, ensure_ascii=False),
                    fees,
                )
                trades.append(_trade(signal, current_date, quantity, execution, fees, None))
            else:
                position = positions.get(signal.code)
                if position is None or current_date.date() <= date.fromisoformat(position.entry_date):
                    continue
                if open_price <= previous_close * (1 - ratio + 0.001):
                    continue
                execution = open_price * (1 - config.slippage_rate)
                value = position.quantity * execution
                fees = max(config.min_commission, value * config.commission_rate) + value * config.stamp_duty_rate
                pnl = (
                    (execution - position.average_price) * position.quantity
                    - position.entry_fees
                    - fees
                )
                cash += value - fees
                trades.append(_trade(signal, current_date, position.quantity, execution, fees, pnl))
                del positions[signal.code]
        return cash, trades

    def _run_generic_signal_strategy(
        self,
        backtest_id: str,
        strategy_id: str,
        names: dict[str, str],
        daily_front: dict[str, pd.DataFrame],
        daily_raw: dict[str, pd.DataFrame],
        index_bars: pd.DataFrame,
        sector_members: dict[str, dict[str, Any]],
        benchmark_bars: dict[str, pd.DataFrame],
        start_date: str | None,
        end_date: str | None,
        *,
        capital_weight: float,
        execution_config: PortfolioConfig,
    ) -> dict[str, Any]:
        strategy = self.strategies[strategy_id]
        initial_cash = self.config.portfolio.initial_cash * capital_weight
        cash = initial_cash
        positions: dict[str, HistoricalPosition] = {}
        pending: list[PlatformSignal] = []
        trades: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        runtime_state: dict[str, dict[str, Any]] = {}
        dates = list(_trading_days(index_bars.index).unique())
        if start_date:
            dates = [item for item in dates if item >= _trading_day(start_date)]
        if end_date:
            dates = [item for item in dates if item <= _trading_day(end_date)]
        if not dates:
            raise ValueError("Backtest date range contains no trading days")
        required_codes = set(self._required_codes(strategy_id))

        for current_date in dates:
            visible_front = _slice_daily(daily_front, current_date)
            visible_raw = _slice_daily(daily_raw, current_date)
            if required_codes:
                visible_front = {
                    code: frame for code, frame in visible_front.items() if code in required_codes
                }
                visible_raw = {
                    code: frame for code, frame in visible_raw.items() if code in required_codes
                }
            visible_benchmarks = _slice_daily(benchmark_bars, current_date)
            if not visible_raw:
                equity_rows.append(
                    {
                        "timestamp": current_date.replace(hour=15).isoformat(),
                        "equity": cash,
                        "cash": cash,
                        "positions": 0,
                    }
                )
                continue
            cash, day_trades = self._fill_course49_pending(
                pending,
                current_date,
                visible_raw,
                names,
                positions,
                cash,
                set(),
                execution_config,
            )
            trades.extend(day_trades)
            filled_signal_ids = {item["signal_id"] for item in day_trades}
            pending = _roll_course49_pending(
                pending,
                current_date,
                positions,
                filled_signal_ids,
            )
            for code, position in positions.items():
                frame = visible_raw.get(code)
                if frame is not None and not frame.empty:
                    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
                    if not close.empty:
                        position.last_price = float(close.iloc[-1])
            position_rows = [
                {
                    "code": item.code,
                    "stop_price": item.stop_price,
                    "entry_time": item.entry_date,
                    "average_price": item.average_price,
                    "evidence": item.evidence,
                }
                for item in positions.values()
            ]
            result = strategy.scan(
                run_id=backtest_id,
                asof=current_date,
                front_bars=visible_front,
                raw_bars=visible_raw,
                names=names,
                sector_members=sector_members,
                benchmark_bars=visible_benchmarks,
                positions=position_rows,
                runtime_state=runtime_state,
            )
            _validate_scan_result(
                strategy_id,
                result,
                current_date,
                ExecutionModel.SINGLE_LEG,
            )
            runtime_state = dict(result.state.get("runtime_state") or runtime_state)
            self.database.save_backtest_state(
                backtest_id,
                strategy_id,
                current_date.date().isoformat(),
                result.state,
            )
            for signal in sorted(result.signals, key=lambda item: (-item.strength, item.code)):
                pending = [
                    item
                    for item in pending
                    if not (item.code == signal.code and item.side == signal.side)
                ]
                pending.append(signal)
            equity = cash + sum(
                item.quantity * item.last_price for item in positions.values()
            )
            equity_rows.append(
                {
                    "timestamp": current_date.replace(hour=15).isoformat(),
                    "equity": equity,
                    "cash": cash,
                    "positions": len(positions),
                }
            )

        equity = pd.DataFrame(equity_rows)
        trade_frame = pd.DataFrame(trades)
        metrics = {
            "data_status": "ok",
            **_performance_metrics(equity, trade_frame, initial_cash),
            "runtime_adapter": RuntimeAdapter.GENERIC_DAILY.value,
            "execution_model": ExecutionModel.SINGLE_LEG.value,
        }
        self._persist_rows(backtest_id, strategy_id, equity, trade_frame)
        return {"metrics": metrics, "equity": equity, "trades": trade_frame}

    def _run_group_intent_strategy(
        self,
        backtest_id: str,
        strategy_id: str,
        names: dict[str, str],
        daily_front: dict[str, pd.DataFrame],
        daily_raw: dict[str, pd.DataFrame],
        index_bars: pd.DataFrame,
        sector_members: dict[str, dict[str, Any]],
        benchmark_bars: dict[str, pd.DataFrame],
        start_date: str | None,
        end_date: str | None,
        *,
        capital_weight: float,
        execution_config: PortfolioConfig,
    ) -> dict[str, Any]:
        strategy = self.strategies[strategy_id]
        initial_cash = self.config.portfolio.initial_cash * capital_weight
        cash = initial_cash
        positions: dict[str, dict[str, HistoricalPairLeg]] = {}
        pending: list[OrderGroupIntent] = []
        trades: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        exposure_rows: list[dict[str, float]] = []
        dates = list(_trading_days(index_bars.index).unique())
        if start_date:
            dates = [item for item in dates if item >= _trading_day(start_date)]
        if end_date:
            dates = [item for item in dates if item <= _trading_day(end_date)]
        if not dates:
            raise ValueError("Backtest date range contains no trading days")
        required_codes = set(self._required_codes(strategy_id))

        for current_date in dates:
            visible_front = _slice_daily(daily_front, current_date)
            visible_raw = _slice_daily(daily_raw, current_date)
            if required_codes:
                visible_front = {
                    code: frame for code, frame in visible_front.items() if code in required_codes
                }
                visible_raw = {
                    code: frame for code, frame in visible_raw.items() if code in required_codes
                }
            visible_benchmarks = _slice_daily(benchmark_bars, current_date)
            cash, day_trades, filled_ids = self._fill_group_pending(
                pending,
                current_date,
                visible_raw,
                names,
                positions,
                cash,
                execution_config,
            )
            trades.extend(day_trades)
            pending = [
                intent
                for intent in pending
                if intent.intent_id not in filled_ids
                and not (
                    intent.action == OrderGroupAction.OPEN
                    and _trading_day(intent.generated_at) < current_date
                )
            ]
            for group in positions.values():
                for leg in group.values():
                    frame = visible_raw.get(leg.code)
                    if frame is not None and not frame.empty:
                        leg.last_price = float(
                            pd.to_numeric(frame["Close"], errors="coerce").dropna().iloc[-1]
                        )
            position_rows = [
                {
                    "group_key": group_key,
                    "legs": [
                        {
                            "code": leg.code,
                            "side": leg.side,
                            "ratio": leg.ratio,
                            "target_weight": leg.target_weight,
                        }
                        for leg in group.values()
                    ],
                }
                for group_key, group in positions.items()
            ]
            result = strategy.scan(
                run_id=backtest_id,
                asof=current_date,
                front_bars=visible_front,
                raw_bars=visible_raw,
                names=names,
                sector_members=sector_members,
                benchmark_bars=visible_benchmarks,
                positions=position_rows,
            )
            _validate_scan_result(
                strategy_id,
                result,
                current_date,
                ExecutionModel.MULTI_LEG,
            )
            for intent in result.order_groups:
                pending = [item for item in pending if item.group_key != intent.group_key]
                pending.append(intent)
            self.database.save_backtest_state(
                backtest_id,
                strategy_id,
                current_date.date().isoformat(),
                {
                    "market_phase": result.state.get("market_phase", "GROUP_RESEARCH"),
                    "market_style": result.state.get("market_style", "MARKET_NEUTRAL"),
                    "style_suitability": 1.0,
                    "trade_modes": result.state.get("trade_modes", ["GROUP_INTENT"]),
                    "entry_allowed": any(
                        item.action == OrderGroupAction.OPEN for item in result.order_groups
                    ),
                    **result.state,
                },
            )
            long_value = sum(
                leg.quantity * leg.last_price
                for group in positions.values()
                for leg in group.values()
                if leg.side == "LONG"
            )
            short_value = sum(
                leg.quantity * leg.last_price
                for group in positions.values()
                for leg in group.values()
                if leg.side == "SHORT"
            )
            equity = cash + long_value - short_value
            equity_rows.append(
                {
                    "timestamp": current_date.replace(hour=15).isoformat(),
                    "equity": equity,
                    "cash": cash,
                    "positions": len(positions),
                }
            )
            exposure_rows.append(
                {
                    "gross": (long_value + short_value) / equity if equity > 0 else 0.0,
                    "net": (long_value - short_value) / equity if equity > 0 else 0.0,
                }
            )

        equity = pd.DataFrame(equity_rows)
        trade_frame = pd.DataFrame(trades)
        metrics = {
            "data_status": "ok",
            **_performance_metrics(equity, trade_frame, initial_cash),
            "average_gross_exposure": float(
                np.mean([item["gross"] for item in exposure_rows]) if exposure_rows else 0.0
            ),
            "average_net_exposure": float(
                np.mean([item["net"] for item in exposure_rows]) if exposure_rows else 0.0
            ),
            "pair_groups": len(
                {str(item.get("group_key")) for item in trades if item.get("group_key")}
            ),
            **_pair_attribution(trade_frame),
            "atomic_execution": True,
            "short_execution": "paper_only",
            "runtime_adapter": RuntimeAdapter.GENERIC_DAILY.value,
            "execution_model": ExecutionModel.MULTI_LEG.value,
        }
        self._persist_rows(backtest_id, strategy_id, equity, trade_frame)
        return {"metrics": metrics, "equity": equity, "trades": trade_frame}

    def _fill_group_pending(
        self,
        pending: list[OrderGroupIntent],
        current_date: pd.Timestamp,
        bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        positions: dict[str, dict[str, HistoricalPairLeg]],
        cash: float,
        config: PortfolioConfig,
    ) -> tuple[float, list[dict[str, Any]], set[str]]:
        trades: list[dict[str, Any]] = []
        filled_ids: set[str] = set()
        for intent in sorted(pending, key=lambda item: item.action != OrderGroupAction.CLOSE):
            if _trading_day(intent.generated_at) >= current_date:
                continue
            if intent.action == OrderGroupAction.OPEN and intent.group_key in positions:
                continue
            if (
                intent.action == OrderGroupAction.OPEN
                and len(positions) >= config.max_strategy_positions
            ):
                continue
            if intent.action == OrderGroupAction.CLOSE and intent.group_key not in positions:
                continue
            execution: list[tuple[Any, float, float]] = []
            blocked = False
            for leg in intent.legs:
                frame = bars.get(leg.code)
                if frame is None or frame.empty:
                    blocked = True
                    break
                frame_days = _trading_days(frame.index)
                row = frame[frame_days == current_date]
                prior = frame[frame_days < current_date]
                if row.empty or prior.empty:
                    blocked = True
                    break
                open_price = float(row["Open"].iloc[-1])
                previous_close = float(prior["Close"].iloc[-1])
                ratio = price_limit_ratio(leg.code, names.get(leg.code, ""))
                if leg.side in {"BUY", "COVER"} and open_price >= previous_close * (1 + ratio - 0.001):
                    blocked = True
                    break
                if leg.side in {"SELL", "SHORT"} and open_price <= previous_close * (1 - ratio + 0.001):
                    blocked = True
                    break
                execution_price = open_price * (
                    1 + config.slippage_rate if leg.side in {"BUY", "COVER"} else 1 - config.slippage_rate
                )
                execution.append((leg, execution_price, previous_close))
            if blocked:
                continue

            evidence = json.dumps(intent.evidence, ensure_ascii=False)
            prepared: list[tuple[Any, int, float, float, float | None]] = []
            if intent.action == OrderGroupAction.OPEN:
                equity = cash + sum(
                    leg.quantity * leg.last_price * (1 if leg.side == "LONG" else -1)
                    for group in positions.values()
                    for leg in group.values()
                )
                gross_budget = equity * intent.gross_target_weight
                long_cost = 0.0
                for leg, price, _ in execution:
                    quantity = int(
                        gross_budget * leg.target_weight / price / config.board_lot
                    ) * config.board_lot
                    if quantity <= 0:
                        blocked = True
                        break
                    value = quantity * price
                    fees = max(config.min_commission, value * config.commission_rate)
                    if leg.side == "BUY":
                        long_cost += value + fees
                    prepared.append((leg, quantity, price, fees, None))
                if blocked or long_cost > cash:
                    continue
            else:
                current = positions[intent.group_key]
                if current_date.date() <= date.fromisoformat(next(iter(current.values())).entry_date):
                    continue
                for leg, price, _ in execution:
                    held = current.get(leg.code)
                    if held is None:
                        blocked = True
                        break
                    value = held.quantity * price
                    fees = max(config.min_commission, value * config.commission_rate)
                    if leg.side == "SELL":
                        fees += value * config.stamp_duty_rate
                        pnl = (price - held.average_price) * held.quantity - held.entry_fees - fees
                    else:
                        pnl = (held.average_price - price) * held.quantity - held.entry_fees - fees
                    prepared.append((leg, held.quantity, price, fees, pnl))
                if blocked:
                    continue

            cash_change = 0.0
            for leg, quantity, price, fees, _ in prepared:
                value = quantity * price
                cash_change += value - fees if leg.side in {"SELL", "SHORT"} else -value - fees
            if cash + cash_change < -1e-6:
                continue
            cash += cash_change
            if intent.action == OrderGroupAction.OPEN:
                positions[intent.group_key] = {
                    leg.code: HistoricalPairLeg(
                        intent.group_key,
                        leg.code,
                        "LONG" if leg.side == "BUY" else "SHORT",
                        quantity,
                        price,
                        current_date.date().isoformat(),
                        price,
                        leg.ratio,
                        leg.target_weight,
                        fees,
                        evidence,
                    )
                    for leg, quantity, price, fees, _ in prepared
                }
            else:
                del positions[intent.group_key]
            for leg, quantity, price, fees, pnl in prepared:
                trades.append(
                    {
                        "signal_id": intent.intent_id,
                        "timestamp": current_date.replace(hour=9, minute=30).isoformat(),
                        "code": leg.code,
                        "side": leg.side,
                        "quantity": quantity,
                        "price": price,
                        "fees": fees,
                        "pnl": pnl,
                        "reason": ",".join(intent.reason_codes),
                        "evidence": evidence,
                        "group_key": intent.group_key,
                        "leg_id": leg.leg_id,
                    }
                )
            filled_ids.add(intent.intent_id)
        return cash, trades, filled_ids

    def _persist_rows(
        self,
        backtest_id: str,
        strategy_id: str,
        equity: pd.DataFrame,
        trades: pd.DataFrame,
    ) -> None:
        with self.database.connect() as connection:
            for row in equity.to_dict("records"):
                connection.execute(
                    """INSERT INTO backtest_equity
                    (backtest_id, strategy_id, timestamp, equity, cash, positions) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        backtest_id, strategy_id, str(row["timestamp"]), float(row["equity"]),
                        float(row["cash"]), int(row["positions"]),
                    ),
                )
            for row in trades.to_dict("records"):
                connection.execute(
                    """INSERT INTO backtest_trades
                    (backtest_id, strategy_id, timestamp, code, side, quantity, price, fees, pnl,
                     reason, evidence, group_key, leg_id, framework_id, playbook_id, policy_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        backtest_id, strategy_id, str(row["timestamp"]), str(row["code"]), str(row["side"]),
                        int(row["quantity"]), float(row["price"]), float(row["fees"]),
                        None if pd.isna(row.get("pnl")) or row.get("pnl") == "" else float(row["pnl"]),
                        str(row.get("reason", "")),
                        str(row.get("evidence", "{}")),
                        str(row.get("group_key", "")),
                        str(row.get("leg_id", "")),
                        str(row.get("framework_id", "")),
                        str(row.get("playbook_id", "")),
                        str(row.get("policy_version", "")),
                    ),
                )

    def _combine_results(self, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        curves = []
        for strategy_id, result in results.items():
            frame = result["equity"].copy()
            frame["timestamp"] = pd.to_datetime(frame["timestamp"])
            frame = frame.set_index("timestamp").sort_index()
            curves.append(frame[["equity"]].rename(columns={"equity": strategy_id}))
        combined = pd.concat(curves, axis=1).sort_index().ffill() if curves else pd.DataFrame()
        initial = sum(
            float(result.get("metrics", {}).get("initial_cash", 0.0))
            for result in results.values()
        ) or self.config.portfolio.initial_cash * self.config.portfolio.strategy_budget_weight
        if not combined.empty:
            for column in combined:
                combined[column] = combined[column].fillna(
                    float(results[column].get("metrics", {}).get("initial_cash", 0.0))
                )
        equity = pd.DataFrame(
            {
                "timestamp": combined.index,
                "equity": combined.sum(axis=1),
            }
        ) if not combined.empty else pd.DataFrame()
        trade_frames = [result["trades"] for result in results.values() if not result["trades"].empty]
        trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
        return _performance_metrics(equity, trades, initial)


def _validate_scan_result(
    strategy_id: str,
    result: StrategyScanResult,
    asof: pd.Timestamp,
    execution_model: ExecutionModel,
) -> None:
    if result.strategy.strategy_id != strategy_id:
        raise ValueError(
            f"Strategy '{strategy_id}' returned metadata for '{result.strategy.strategy_id}'"
        )
    if execution_model == ExecutionModel.SINGLE_LEG and result.order_groups:
        raise ValueError("Single-leg strategy returned multi-leg order intents")
    if execution_model == ExecutionModel.MULTI_LEG and result.signals:
        raise ValueError("Multi-leg strategy returned single-leg signals")
    boundary = pd.Timestamp(asof).tz_localize(None).normalize()
    for event in (*result.signals, *result.order_groups):
        if event.strategy_id != strategy_id:
            raise ValueError("Strategy output contains an inconsistent strategy_id")
        generated = pd.Timestamp(event.generated_at).tz_localize(None)
        available = pd.Timestamp(event.available_at).tz_localize(None)
        if generated.normalize() > boundary:
            raise ValueError("Strategy output uses a future generated_at timestamp")
        if available < generated:
            raise ValueError("Strategy output available_at precedes generated_at")
        if not 0.0 <= float(event.strength) <= 1.0:
            raise ValueError("Strategy output strength must be between 0 and 1")


def _select_universe(codes: list[str], universe: str, stock_codes: list[str]) -> list[str]:
    supported = {"all_a", "main_board", "growth", "star", "beijing", "custom"}
    if universe not in supported:
        raise ValueError(f"Unknown stock universe: {universe}")
    available = set(codes)
    if universe == "custom":
        requested = [_normalize_stock_code(code) for code in stock_codes if code.strip()]
        missing = [code for code in requested if code not in available]
        if missing:
            raise ValueError(f"Stocks are unavailable in TDX: {', '.join(missing[:10])}")
        return list(dict.fromkeys(requested))
    if universe == "all_a":
        return codes
    if universe == "growth":
        return [code for code in codes if code.endswith(".SZ") and code[:3] in ("300", "301")]
    if universe == "star":
        return [code for code in codes if code.endswith(".SH") and code[:3] in ("688", "689")]
    if universe == "beijing":
        return [code for code in codes if code.endswith(".BJ")]
    return [
        code
        for code in codes
        if (
            code.endswith(".SH") and code[:3] in ("600", "601", "603", "605")
        ) or (
            code.endswith(".SZ") and code[:3] in ("000", "001", "002", "003")
        )
    ]


def _resolve_sampling_mode(sampling_mode: str, max_stocks: int | None) -> str:
    if sampling_mode not in {"full", "stratified"}:
        raise ValueError(f"Unknown sampling mode: {sampling_mode}")
    return "stratified" if max_stocks is not None else sampling_mode


def _slice_to_date(
    bars: dict[str, pd.DataFrame], end_date: str | None
) -> dict[str, pd.DataFrame]:
    if not end_date:
        return bars
    cutoff = _trading_day(end_date)
    return {
        code: frame[_trading_days(frame.index) <= cutoff]
        for code, frame in bars.items()
    }


def _market_segment(code: str) -> str:
    if code.endswith(".BJ"):
        return "BEIJING"
    if code.endswith(".SH") and code.startswith(("688", "689")):
        return "STAR"
    if code.endswith(".SZ") and code.startswith(("300", "301")):
        return "GROWTH"
    if code.endswith(".SH"):
        return "SH_MAIN"
    return "SZ_MAIN"


def _average_turnover(frame: pd.DataFrame) -> float:
    if "Amount" in frame and pd.to_numeric(frame["Amount"], errors="coerce").notna().any():
        amount = pd.to_numeric(frame["Amount"], errors="coerce")
        scale = 1.0 if frame.attrs.get("amount_unit") == "CNY" else 10_000.0
        return float(amount.tail(20).mean()) * scale
    close = pd.to_numeric(frame.get("Close"), errors="coerce")
    volume = pd.to_numeric(frame.get("Volume"), errors="coerce")
    return float((close * volume).tail(20).mean())


def _stratified_sample(
    codes: list[str],
    bars: dict[str, pd.DataFrame],
    size: int,
    seed: int,
) -> list[str]:
    available = sorted(set(codes))
    if size >= len(available):
        return available
    rows = pd.DataFrame(
        [
            {
                "code": code,
                "segment": _market_segment(code),
                "turnover": _average_turnover(bars[code]),
            }
            for code in available
            if code in bars
        ]
    )
    if rows.empty:
        return []
    rows["tercile"] = rows.groupby("segment")["turnover"].transform(
        lambda values: pd.qcut(
            values.rank(method="first"),
            q=min(3, len(values)),
            labels=False,
            duplicates="drop",
        )
    )
    groups: dict[tuple[str, int], list[str]] = {}
    for (segment, tercile), frame in rows.groupby(["segment", "tercile"], dropna=False):
        values = sorted(frame["code"].astype(str))
        random.Random(f"{seed}:{segment}:{tercile}").shuffle(values)
        groups[(str(segment), int(tercile) if pd.notna(tercile) else 0)] = values
    selected: list[str] = []
    keys = sorted(groups)
    while len(selected) < size and keys:
        next_keys = []
        for key in keys:
            values = groups[key]
            if values and len(selected) < size:
                selected.append(values.pop())
            if values:
                next_keys.append(key)
        keys = next_keys
    return sorted(selected)


def _stock_pool_hash(codes: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(codes))).encode("utf-8")).hexdigest()


def _universe_distribution(codes: list[str]) -> dict[str, Any]:
    segments: dict[str, int] = {}
    exchanges: dict[str, int] = {}
    for code in codes:
        segment = _market_segment(code)
        exchange = code.rsplit(".", 1)[-1] if "." in code else "UNKNOWN"
        segments[segment] = segments.get(segment, 0) + 1
        exchanges[exchange] = exchanges.get(exchange, 0) + 1
    return {
        "total": len(codes),
        "segments": dict(sorted(segments.items())),
        "exchanges": dict(sorted(exchanges.items())),
    }


def _resolved_benchmark(
    bars: dict[str, pd.DataFrame], candidates: tuple[str, ...]
) -> str | None:
    return next(
        (
            code
            for code in candidates
            if code in bars
            and len(pd.to_numeric(bars[code].get("Close"), errors="coerce").dropna()) >= 21
        ),
        None,
    )


def _sector_membership_rows(
    sectors: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "sector_code": str(sector_code),
            "sector_name": str(metadata.get("name", sector_code)),
            "member_code": str(member),
        }
        for sector_code, metadata in sorted(sectors.items())
        for member in sorted(set(metadata.get("members", [])))
    ]


def _sector_membership_from_frame(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    required = {"sector_code", "sector_name", "member_code"}
    if frame.empty or not required.issubset(frame.columns):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for sector_code, rows in frame.groupby("sector_code"):
        result[str(sector_code)] = {
            "name": str(rows["sector_name"].iloc[0]),
            "members": sorted(set(rows["member_code"].astype(str))),
        }
    return result


def _market_activity_from_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "timestamp" not in frame.columns:
        return pd.DataFrame()
    item = frame.copy()
    item["timestamp"] = pd.to_datetime(item["timestamp"], errors="coerce")
    item = item.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    item.attrs["source"] = "snapshot"
    item.attrs["available_after"] = "close"
    return item


def _snapshot_index_fallback(
    benchmark_bars: dict[str, pd.DataFrame],
    parameters: dict[str, Any],
) -> pd.DataFrame:
    actual_codes = parameters.get("benchmark_actual_codes") or {}
    preferred = [
        actual_codes.get("large"),
        actual_codes.get("small"),
        actual_codes.get("growth"),
    ]
    for code in [*preferred, *benchmark_bars]:
        frame = benchmark_bars.get(str(code)) if code else None
        if frame is not None and not frame.empty:
            return frame
    raise ValueError("Snapshot has no usable market index or style benchmark")


def _records_hash(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lhb_snapshot_schema() -> Any:
    import pyarrow as pa

    string_fields = {
        "code",
        "event_date",
        "risk",
        "first_limit_time",
        "last_limit_time",
        "board_risk",
    }
    boolean_fields = {"listed", "limit_event"}
    integer_fields = {
        "institution_buy_count",
        "institution_sell_count",
        "consecutive_list_days",
        "open_board_count",
        "year_limit_count",
        "year_premium5_count",
    }
    list_fields = {"confirmations", "board_confirmations"}
    names = ["code", *LhbFeatures.__dataclass_fields__]
    fields = []
    for name in names:
        if name in string_fields:
            field_type = pa.string()
        elif name in boolean_fields:
            field_type = pa.bool_()
        elif name in integer_fields:
            field_type = pa.int64()
        elif name in list_fields:
            field_type = pa.list_(pa.string())
        else:
            field_type = pa.float64()
        fields.append(pa.field(name, field_type, nullable=True))
    return pa.schema(fields)


def _normalize_stock_code(value: str) -> str:
    code = value.strip().upper()
    if "." in code:
        return code
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"Invalid stock code: {value}")
    if code.startswith(("4", "8")):
        suffix = "BJ"
    elif code.startswith(("5", "6", "9")):
        suffix = "SH"
    else:
        suffix = "SZ"
    return f"{code}.{suffix}"


def _snapshot_event_minimum_streak(parameters: dict[str, Any]) -> int:
    value = parameters.get("course49_event_minimum_streak")
    if value is not None:
        return max(1, int(value))
    if parameters.get("course49_event_scope") == "strategy_candidates":
        return 2
    return 1


def _validate_date_range(start_date: str | None, end_date: str | None) -> tuple[str | None, str | None]:
    start = _trading_day(start_date).date().isoformat() if start_date else None
    end = _trading_day(end_date).date().isoformat() if end_date else None
    if start and end and start > end:
        raise ValueError("Backtest start date must not be after end date")
    return start, end


def _execution_cost_config(
    config: PortfolioConfig,
    multiplier: float,
) -> PortfolioConfig:
    return replace(
        config,
        commission_rate=config.commission_rate * multiplier,
        min_commission=config.min_commission * multiplier,
        stamp_duty_rate=config.stamp_duty_rate * multiplier,
        slippage_rate=config.slippage_rate * multiplier,
    )


def _required_daily_bars(
    requested: int,
    start_date: str | None,
    end_date: str | None = None,
) -> int:
    count = max(90, requested)
    if start_date:
        range_end = date.fromisoformat(end_date) if end_date else date.today()
        calendar_days = max(0, (range_end - date.fromisoformat(start_date)).days)
        count = max(count, math.ceil(calendar_days * 252 / 365) + 90)
    return min(2000, count)


def _ensure_date_range_available(
    index_bars: pd.DataFrame,
    start_date: str | None,
    end_date: str | None,
) -> None:
    days = _trading_days(index_bars.index)
    if start_date:
        days = days[days >= _trading_day(start_date)]
    if end_date:
        days = days[days <= _trading_day(end_date)]
    if not len(days):
        raise ValueError("Backtest date range is outside the downloaded TDX history")


def _slice_schedule(
    schedule: dict[str, dict[str, Any]],
    start_date: str | None,
    end_date: str | None,
) -> dict[str, dict[str, Any]]:
    return {
        day: values
        for day, values in schedule.items()
        if (not start_date or day >= start_date) and (not end_date or day <= end_date)
    }


def _performance_metrics(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
) -> dict[str, Any]:
    if equity.empty or "equity" not in equity:
        daily_curve = pd.Series([initial_cash], index=[pd.Timestamp.today().normalize()])
    else:
        frame = equity.copy().reset_index(drop=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame["equity"] = pd.to_numeric(frame["equity"], errors="coerce")
        frame = frame.dropna(subset=["timestamp", "equity"]).sort_values("timestamp")
        daily_curve = frame.groupby(frame["timestamp"].dt.normalize())["equity"].last()
        if daily_curve.empty:
            daily_curve = pd.Series([initial_cash], index=[pd.Timestamp.today().normalize()])
    final_equity = float(daily_curve.iloc[-1])
    total_return = float(final_equity / initial_cash - 1.0)
    trading_days = int(len(daily_curve))
    annualized_return = (
        float((final_equity / initial_cash) ** (252 / max(1, trading_days - 1)) - 1.0)
        if final_equity > 0 and trading_days > 1
        else 0.0
    )
    daily_returns = daily_curve.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
    volatility = float(daily_returns.std(ddof=0)) if not daily_returns.empty else 0.0
    sharpe_ratio = (
        float(daily_returns.mean() / volatility * math.sqrt(252))
        if volatility > 0
        else 0.0
    )
    drawdown = daily_curve / daily_curve.cummax() - 1.0
    if trades.empty or "side" not in trades:
        sells = pd.DataFrame()
    else:
        sells = trades[trades["side"].astype(str).str.upper().isin({"SELL", "COVER"})].copy()
        sells["pnl"] = pd.to_numeric(sells.get("pnl"), errors="coerce")
        sells = sells.dropna(subset=["pnl"])
    gross_profit = float(sells.loc[sells["pnl"] > 0, "pnl"].sum()) if not sells.empty else 0.0
    gross_loss = abs(float(sells.loc[sells["pnl"] < 0, "pnl"].sum())) if not sells.empty else 0.0
    metrics = {
        "initial_cash": float(initial_cash),
        "final_equity": final_equity,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": float(drawdown.min()),
        "sharpe_ratio": sharpe_ratio,
        "trades": int(len(trades)),
        "win_rate": float((sells["pnl"] > 0).mean()) if not sells.empty else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "trading_days": trading_days,
        "closed_trades": int(len(sells)),
    }
    metrics["validation"] = _validation_summary(metrics)
    return metrics


def _validation_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    trading_days = int(metrics.get("trading_days", 0) or 0)
    closed_trades = int(metrics.get("closed_trades", 0) or 0)
    annualized_return = float(metrics.get("annualized_return", 0.0) or 0.0)
    minimum_trading_days = 250
    minimum_closed_trades = 30
    target_annualized_return = 0.20
    period_met = trading_days >= minimum_trading_days
    trades_met = closed_trades >= minimum_closed_trades
    evidence_sufficient = period_met and trades_met
    target_met = annualized_return >= target_annualized_return
    reasons = []
    if not period_met:
        reasons.append("INSUFFICIENT_TRADING_DAYS")
    if not trades_met:
        reasons.append("INSUFFICIENT_CLOSED_TRADES")
    if not target_met:
        reasons.append("ANNUALIZED_TARGET_NOT_MET")
    historical_threshold_met = evidence_sufficient and target_met
    return {
        "status": "HISTORICAL_RETURN_TARGET_MET" if historical_threshold_met else "UNVERIFIED",
        "target_verified": False,
        "historical_threshold_met": historical_threshold_met,
        "evidence_sufficient": evidence_sufficient,
        "target_met": target_met,
        "minimum_trading_days": minimum_trading_days,
        "minimum_closed_trades": minimum_closed_trades,
        "target_annualized_return": target_annualized_return,
        "reasons": reasons,
    }


def _pair_attribution(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty or "side" not in trades or "group_key" not in trades:
        return {"completed_pair_groups": 0, "pair_win_rate": 0.0, "pair_total_pnl": 0.0}
    closes = trades[trades["side"].astype(str).str.upper().isin({"SELL", "COVER"})].copy()
    if closes.empty:
        return {"completed_pair_groups": 0, "pair_win_rate": 0.0, "pair_total_pnl": 0.0}
    closes["pnl"] = pd.to_numeric(closes.get("pnl"), errors="coerce")
    closes["timestamp"] = pd.to_datetime(closes.get("timestamp"), errors="coerce")
    closes = closes.dropna(subset=["pnl", "timestamp"])
    grouped = closes.groupby([closes["timestamp"].dt.normalize(), "group_key"])["pnl"].sum()
    return {
        "completed_pair_groups": int(len(grouped)),
        "pair_win_rate": float((grouped > 0).mean()) if len(grouped) else 0.0,
        "pair_total_pnl": float(grouped.sum()) if len(grouped) else 0.0,
    }


def _course49_attribution(trades: pd.DataFrame) -> list[dict[str, Any]]:
    cohort_names = ("CAPITAL_AND_BOARD", "CAPITAL_ONLY", "BOARD_ONLY", "BASIC")
    rows = {
        cohort: {"cohort": cohort, "entries": 0, "closed": 0, "wins": 0, "total_pnl": 0.0}
        for cohort in cohort_names
    }
    if trades.empty or "side" not in trades:
        return [{**row, "win_rate": 0.0, "avg_pnl": 0.0} for row in rows.values()]

    capital_reasons = {
        "SECOND_BOARD_CAPITAL_CONFIRMED",
        "LHB_NET_BUY",
        "INSTITUTION_BUY",
        "NORTHBOUND_BUY",
    }
    board_reasons = {
        "SECOND_BOARD_QUALITY_CONFIRMED",
        "EARLY_SEAL",
        "STRONG_SEAL",
        "AUCTION_STRENGTH",
        "RELIABLE_FIRST_BOARD",
        "PREMIUM_MEMORY",
    }
    active: dict[str, str] = {}
    frame = trades.copy()
    frame["timestamp"] = pd.to_datetime(frame.get("timestamp"), errors="coerce")
    frame = frame.sort_values("timestamp")
    for trade in frame.to_dict("records"):
        code = str(trade.get("code", ""))
        side = str(trade.get("side", "")).upper()
        if side == "BUY":
            reasons = {
                item.strip()
                for item in str(trade.get("reason", "")).split(",")
                if item.strip()
            }
            has_capital = bool(reasons & capital_reasons)
            has_board = bool(reasons & board_reasons)
            if has_capital and has_board:
                cohort = "CAPITAL_AND_BOARD"
            elif has_capital:
                cohort = "CAPITAL_ONLY"
            elif has_board:
                cohort = "BOARD_ONLY"
            else:
                cohort = "BASIC"
            active[code] = cohort
            rows[cohort]["entries"] += 1
        elif side == "SELL" and code in active:
            cohort = active.pop(code)
            pnl = pd.to_numeric(pd.Series([trade.get("pnl")]), errors="coerce").iloc[0]
            if pd.isna(pnl):
                continue
            rows[cohort]["closed"] += 1
            rows[cohort]["wins"] += int(float(pnl) > 0)
            rows[cohort]["total_pnl"] += float(pnl)

    result = []
    for row in rows.values():
        closed = int(row["closed"])
        total_pnl = float(row["total_pnl"])
        result.append(
            {
                **row,
                "win_rate": float(row["wins"]) / closed if closed else 0.0,
                "avg_pnl": total_pnl / closed if closed else 0.0,
            }
        )
    return result


def _slice_daily(bars: dict[str, pd.DataFrame], asof: pd.Timestamp) -> dict[str, pd.DataFrame]:
    result = {}
    cutoff = _trading_day(asof)
    for code, frame in bars.items():
        item = frame[_trading_days(frame.index) <= cutoff]
        if len(item) >= 20:
            result[code] = item
    return result


def _slice_daily_codes(
    bars: dict[str, pd.DataFrame],
    asof: pd.Timestamp,
    codes: set[str],
) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    cutoff = _trading_day(asof)
    for code in codes:
        frame = bars.get(code)
        if frame is None:
            continue
        item = frame[_trading_days(frame.index) <= cutoff]
        if len(item) >= 20:
            result[code] = item
    return result


def _matrix_codes_at(matrix: pd.DataFrame, asof: pd.Timestamp) -> set[str]:
    if matrix.empty:
        return set()
    timestamp = _trading_day(asof)
    if timestamp not in matrix.index:
        return set()
    row = matrix.loc[timestamp]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    return set(row.index[row.fillna(False).astype(bool)])


def _course49_v2_candidate_matrix(
    feature_matrix: dict[str, pd.DataFrame] | pd.DataFrame,
    eligibility: pd.DataFrame,
) -> pd.DataFrame:
    if not isinstance(feature_matrix, dict) or eligibility.empty:
        return pd.DataFrame()
    limit_up = feature_matrix.get("limit_up")
    if limit_up is None or limit_up.empty:
        return pd.DataFrame()
    eligible = eligibility.reindex(
        index=limit_up.index,
        columns=limit_up.columns,
        fill_value=False,
    ).fillna(False)
    return limit_up.fillna(False).astype(bool) & eligible.astype(bool)


def _historical_limit_codes(
    bars: dict[str, pd.DataFrame],
    names: dict[str, str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> list[str]:
    start = _trading_day(start_date)
    end = _trading_day(end_date)
    candidates: list[str] = []
    for code, frame in bars.items():
        close = pd.to_numeric(frame.get("Close"), errors="coerce")
        if close.empty:
            continue
        returns = close.pct_change(fill_method=None)
        days = _trading_days(frame.index)
        in_range = (days >= start) & (days <= end)
        ratio = price_limit_ratio(code, names.get(code, ""))
        if bool((returns[in_range] >= ratio - 0.001).any()):
            candidates.append(code)
    return sorted(candidates)


def _roll_course49_pending(
    pending: list[PlatformSignal],
    current_date: pd.Timestamp,
    positions: dict[str, HistoricalPosition],
    filled_signal_ids: set[str],
) -> list[PlatformSignal]:
    current_day = _trading_day(current_date)
    result: list[PlatformSignal] = []
    for signal in pending:
        if signal.signal_id in filled_signal_ids:
            continue
        if signal.side == "BUY" and _trading_day(signal.generated_at) < current_day:
            continue
        if signal.side == "SELL" and signal.code not in positions:
            continue
        result.append(signal)
    return result


def _trading_day(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp.normalize()


def _trading_days(values: Any) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([_trading_day(value) for value in values])


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size <= 5:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _trade(
    signal: PlatformSignal,
    timestamp: pd.Timestamp,
    quantity: int,
    price: float,
    fees: float,
    pnl: float | None,
) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "timestamp": timestamp.replace(hour=9, minute=30).isoformat(),
        "code": signal.code,
        "side": signal.side,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "pnl": pnl,
        "reason": ",".join(signal.reason_codes),
        "evidence": json.dumps(signal.evidence, ensure_ascii=False),
        "framework_id": signal.framework_id,
        "playbook_id": signal.playbook_id,
        "policy_version": signal.policy_version,
    }


def _evidence_attribution(trades: pd.DataFrame, field: str) -> list[dict[str, Any]]:
    if trades.empty:
        return []
    rows: dict[str, dict[str, Any]] = {}
    active: dict[str, str] = {}
    for trade in trades.to_dict("records"):
        evidence = trade.get("evidence") or "{}"
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except json.JSONDecodeError:
                evidence = {}
        code = str(trade.get("code", ""))
        side = str(trade.get("side", "")).upper()
        if side == "BUY":
            key = str(evidence.get(field, "UNKNOWN"))
            active[code] = key
            rows.setdefault(key, {field: key, "entries": 0, "closed": 0, "wins": 0, "total_pnl": 0.0})
            rows[key]["entries"] += 1
        elif side == "SELL" and code in active:
            key = active.pop(code)
            row = rows[key]
            pnl = float(trade.get("pnl", 0.0) or 0.0)
            row["closed"] += 1
            row["wins"] += int(pnl > 0)
            row["total_pnl"] += pnl
    for row in rows.values():
        row["win_rate"] = row["wins"] / row["closed"] if row["closed"] else 0.0
    return sorted(rows.values(), key=lambda item: str(item[field]))


def _exit_reason_attribution(trades: pd.DataFrame) -> list[dict[str, Any]]:
    if trades.empty or "side" not in trades:
        return []
    sells = trades[trades["side"].astype(str).str.upper() == "SELL"]
    rows: dict[str, dict[str, Any]] = {}
    for trade in sells.to_dict("records"):
        reason = str(trade.get("reason", "UNKNOWN")).split(",", 1)[0] or "UNKNOWN"
        row = rows.setdefault(reason, {"reason": reason, "count": 0, "wins": 0, "total_pnl": 0.0})
        pnl = float(trade.get("pnl", 0.0) or 0.0)
        row["count"] += 1
        row["wins"] += int(pnl > 0)
        row["total_pnl"] += pnl
    return sorted(rows.values(), key=lambda item: (-int(item["count"]), str(item["reason"])))


def _average_capital_invested(equity: pd.DataFrame, initial_cash: float) -> float:
    if equity.empty:
        return 0.0
    frame = equity.copy()
    equity_values = pd.to_numeric(frame.get("equity"), errors="coerce")
    cash_values = pd.to_numeric(frame.get("cash"), errors="coerce")
    ratios = ((equity_values - cash_values) / initial_cash).replace([np.inf, -np.inf], np.nan)
    return float(ratios.dropna().mean()) if ratios.notna().any() else 0.0


def _reserved_codes_by_date(trades: pd.DataFrame) -> dict[str, set[str]]:
    if trades.empty or "timestamp" not in trades:
        return {}
    frame = trades.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame = frame.sort_values("timestamp")
    dates = pd.date_range(frame["timestamp"].min().normalize(), frame["timestamp"].max().normalize(), freq="B")
    result: dict[str, set[str]] = {}
    active: set[str] = set()
    offset = 0
    rows = frame.to_dict("records")
    for current in dates:
        while offset < len(rows) and pd.Timestamp(rows[offset]["timestamp"]).normalize() <= current:
            row = rows[offset]
            if row["side"] == "BUY":
                active.add(str(row["code"]))
            else:
                active.discard(str(row["code"]))
            offset += 1
        result[current.date().isoformat()] = set(active)
    return result
