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
from .data_plan import build_data_plan
from .lhb import flatten_lhb_history, normalize_lhb_history
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


class PlatformService:
    def __init__(self, config: PlatformConfig | None = None):
        self.config = config or PlatformConfig()
        self.database = Database(self.config)
        self.database.initialize()
        self.snapshots = ParquetSnapshotStore(self.config, self.database)
        self.data_cache = DataCacheManager(self.config, self.database)
        self.data_hub = ResearchDataHub(self.config)
        self.portfolio = PaperPortfolio(self.config, self.database)
        self.strategies, self.plugin_issues = load_strategy_registry(self.config)
        for strategy in self.strategies.values():
            self.database.register_strategy(
                strategy.metadata,
                getattr(strategy, "__plugin_origin__", "builtin"),
            )
        for group in built_in_groups():
            self.database.upsert_strategy_group(group)
        self.catalog = StrategyCatalog(self.strategies, self.database.load_strategy_groups())
        self.composition = CompositionEngine()

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

    def reload_strategies(self) -> dict[str, Any]:
        strategies, issues = load_strategy_registry(self.config)
        catalog = StrategyCatalog(strategies, self.database.load_strategy_groups())
        self.strategies = strategies
        self.plugin_issues = issues
        self.catalog = catalog
        for strategy in self.strategies.values():
            self.database.register_strategy(
                strategy.metadata,
                getattr(strategy, "__plugin_origin__", "builtin"),
            )
        return self.strategy_catalog()

    def strategy_catalog(self) -> dict[str, Any]:
        payload = self.catalog.as_records()
        payload["plugin_issues"] = [
            *[issue.as_record() for issue in self.plugin_issues],
            *self.catalog.group_issues,
        ]
        for strategy in payload["strategies"]:
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
        requested_ids = strategy_ids or ["combined"]
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
        run_id = uuid4().hex
        started = datetime.now().astimezone()
        self.database.create_run(run_id, "scan", mode, requested_ids)
        self.database.update_run(run_id, RunStatus.RUNNING)
        health: list[DataHealth] = []
        results: list[StrategyScanResult] = []
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
            with TdxProvider(self.config, __file__) as provider:
                if progress_callback is not None:
                    progress_callback(
                        phase="MARKET_DATA",
                        progress=0.10,
                        detail="正在读取扫描数据",
                        cache_status="refresh" if refresh_data else "miss",
                        waiting_reason="",
                    )
                codes, names = provider.list_a_shares()
                constrained_codes = list(
                    dict.fromkeys(
                        code
                        for item in component_ids
                        for code in self._required_codes(item)
                    )
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
                    sample_eligible = filter_universe(
                        sample_bars,
                        names,
                        StrategyConfig(tdx_root=self.config.tdx_root, daily_lookback=90),
                    )
                    codes = _stratified_sample(
                        list(sample_eligible),
                        sample_eligible,
                        max_stocks or min(500, len(sample_eligible)),
                        sample_seed,
                    )
                codes = list(dict.fromkeys([*codes, *constrained_codes]))
                front = provider.fetch_bars(
                    codes,
                    "1d",
                    120,
                    fields=data_plan.front_fields,
                    dividend_type="front",
                    batch_callback=batch_progress(
                        "MARKET_DATA_FRONT", 0.12, 0.30, "前复权行情"
                    ),
                )
                raw = provider.fetch_bars(
                    codes,
                    "1d",
                    120,
                    fields=data_plan.raw_fields,
                    dividend_type="none",
                    batch_callback=batch_progress(
                        "MARKET_DATA_RAW", 0.30, 0.48, "不复权行情"
                    ),
                )
                index_map = provider.fetch_bars(["999999.SH"], "1d", 120, dividend_type="front")
                index_bars = index_map.get("999999.SH")
                if index_bars is None:
                    fallback = provider.fetch_bars(["000001.SH"], "1d", 120, dividend_type="front")
                    index_bars = fallback.get("000001.SH")
                daily_health = self.data_hub.assess_daily(raw, index_bars)
                health.append(daily_health)
                if daily_health.status != DataStatus.READY:
                    raise DataBlockedError(daily_health.message or "Daily data is unavailable")
                legacy_config = StrategyConfig(tdx_root=self.config.tdx_root, daily_lookback=120)
                fixed_generic_only = all(
                    self._runtime_adapter(item) == RuntimeAdapter.GENERIC_DAILY
                    and self._required_codes(item)
                    for item in component_ids
                )
                eligible_front = (
                    {code: frame for code, frame in front.items() if not frame.empty}
                    if fixed_generic_only
                    else filter_universe(front, names, legacy_config)
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
                    provider.fetch_bars(benchmark_codes, "1d", 120, dividend_type="front")
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

                generic_ids = [
                    item
                    for item in component_ids
                    if self._runtime_adapter(item) == RuntimeAdapter.GENERIC_DAILY
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
                        front_bars=eligible_front,
                        raw_bars=eligible_raw,
                        names=names,
                        sector_members=sectors_map,
                        benchmark_bars=benchmark_bars,
                        positions=generic_positions,
                        runtime_state=self.database.load_runtime_states(generic_id),
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
                self.database.save_signals(signals)
                self.database.save_order_groups(order_groups)
                self.portfolio.queue_approved(signals)
                snapshot_id = f"scan_{run_id}"
                snapshot_metadata = {
                    "count": 120,
                    "adjustment": "front",
                    "sampling_mode": sampling_mode,
                    "sample_seed": sample_seed,
                    "stock_pool_hash": _stock_pool_hash(list(eligible_front)),
                    "universe_distribution": _universe_distribution(list(eligible_front)),
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
                if push_tdx:
                    self._push_scan(provider, results)

            metadata = {
                "strategies": {result.strategy.strategy_id: result.state for result in results},
                "components": [result.strategy.strategy_id for result in component_results],
                "requested": requested_ids,
                "composition_mode": (
                    requested_groups[0].composition_mode.value
                    if len(requested_ids) == 1 and requested_groups
                    else "independent"
                ),
                "order_group_count": sum(len(result.order_groups) for result in results),
                "sampling_mode": sampling_mode,
                "sample_seed": sample_seed,
                "data_plan": data_plan.as_dict(),
                "effective_batch_sizes": provider.effective_batch_sizes(),
                "stock_pool_hash": _stock_pool_hash(list(eligible_front)) if results else "",
                "universe_distribution": _universe_distribution(list(eligible_front)) if results else {},
                "sector_membership_quality": "LIMITED" if data_plan.require_sectors else "NOT_REQUIRED",
                "sector_membership_source": "current_fallback" if data_plan.require_sectors else "data_plan",
                "sector_membership_hash": sector_hash if results and data_plan.require_sectors else "",
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
                with TdxProvider(self.config, __file__) as provider:
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
