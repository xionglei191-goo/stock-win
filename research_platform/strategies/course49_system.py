from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

import pandas as pd

from research_platform.lhb import LhbFeatures, latest_lhb_features, latest_limit_features
from research_platform.models import (
    DataRequirement,
    PlatformSignal,
    RuntimeAdapter,
    SignalStatus,
    StrategyMetadata,
    StrategyScanResult,
)

from .course49 import (
    Course49Market,
    _shanghai_time,
    course49_market_from_matrix,
    json_evidence,
)
from .course49_v2 import Course49V2Strategy, MarketStyle, POSITIVE_LHB_REASONS


FRAMEWORK_ID = "course49"
FRAMEWORK_VERSION = "1.0.0"
CONTEXT_VERSION = "1.0.0"
POLICY_VERSION = "1.0.0"


class PlaybookLifecycle(StrEnum):
    RESEARCH = "RESEARCH"
    VALIDATED = "VALIDATED"
    PRODUCTION = "PRODUCTION"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True)
class FrameworkMetadata:
    framework_id: str
    version: str
    name: str
    description: str
    strategy_id: str

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlaybookMetadata:
    playbook_id: str
    framework_id: str
    version: str
    name: str
    description: str
    lifecycle: PlaybookLifecycle
    data_requirements: tuple[DataRequirement, ...]
    base_weight: float
    market_phase: str

    def as_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["lifecycle"] = self.lifecycle.value
        record["data_requirements"] = [asdict(item) for item in self.data_requirements]
        return record


@dataclass(frozen=True)
class Course49Context:
    asof: pd.Timestamp
    context_version: str
    stock_pool_hash: str
    sector_membership_hash: str
    data_completeness: dict[str, Any]
    market: Course49Market
    style: MarketStyle
    suitability: float
    entry_allowed: bool
    entry_block_reason: str
    sectors: tuple[dict[str, Any], ...]
    entry_sector_codes: frozenset[str]
    holding_sectors: tuple[dict[str, Any], ...]
    leaders: tuple[dict[str, Any], ...]
    positions: dict[str, dict[str, Any]]
    runtime_state: dict[str, dict[str, Any]]
    front_bars: dict[str, pd.DataFrame] = field(repr=False)
    raw_bars: dict[str, pd.DataFrame] = field(repr=False)
    lhb_history: dict[str, dict[str, LhbFeatures]] = field(repr=False)


@dataclass(frozen=True)
class PlaybookCandidate:
    playbook_id: str
    code: str
    score: float
    target_weight: float
    reason_codes: tuple[str, ...]
    evidence: dict[str, Any]
    leader: dict[str, Any]


@dataclass(frozen=True)
class PlaybookResult:
    playbook_id: str
    admitted: bool
    candidates: tuple[PlaybookCandidate, ...]
    blocked_reasons: tuple[str, ...]


class Course49Playbook(Protocol):
    metadata: PlaybookMetadata

    def evaluate(self, context: Course49Context) -> PlaybookResult: ...


@dataclass(frozen=True)
class PlaybookValidationEvidence:
    trading_days: int
    closed_trades: int
    lookahead_passed: bool
    out_of_sample_passed: bool
    double_cost_passed: bool
    concentration_passed: bool

    @property
    def passed(self) -> bool:
        return (
            self.trading_days >= 250
            and self.closed_trades >= 30
            and self.lookahead_passed
            and self.out_of_sample_passed
            and self.double_cost_passed
            and self.concentration_passed
        )


class Course49PlaybookRegistry:
    def __init__(self) -> None:
        self._playbooks: dict[str, Course49Playbook] = {}

    def register(
        self,
        playbook: Course49Playbook,
        *,
        trusted_production: bool = False,
    ) -> None:
        metadata = playbook.metadata
        if metadata.framework_id != FRAMEWORK_ID:
            raise ValueError("Course49 playbook must belong to the course49 framework")
        if metadata.playbook_id in self._playbooks:
            raise ValueError(f"Duplicate Course49 playbook: {metadata.playbook_id}")
        if (
            metadata.lifecycle == PlaybookLifecycle.PRODUCTION
            and not trusted_production
        ):
            raise ValueError("New playbooks must enter as RESEARCH and pass promotion gates")
        self._playbooks[metadata.playbook_id] = playbook

    def all(self) -> tuple[Course49Playbook, ...]:
        return tuple(self._playbooks[key] for key in sorted(self._playbooks))

    def production(self) -> tuple[Course49Playbook, ...]:
        return tuple(
            item
            for item in self.all()
            if item.metadata.lifecycle == PlaybookLifecycle.PRODUCTION
        )

    def promote(
        self,
        playbook_id: str,
        evidence: PlaybookValidationEvidence,
    ) -> PlaybookMetadata:
        playbook = self._playbooks.get(playbook_id)
        if playbook is None:
            raise KeyError(playbook_id)
        if not evidence.passed:
            raise ValueError("Playbook promotion gates are not satisfied")
        current = playbook.metadata
        if current.lifecycle not in {
            PlaybookLifecycle.RESEARCH,
            PlaybookLifecycle.VALIDATED,
        }:
            raise ValueError(f"Cannot promote playbook from {current.lifecycle.value}")
        promoted = replace(current, lifecycle=PlaybookLifecycle.PRODUCTION)
        playbook.metadata = promoted
        return promoted


class _ProductionPlaybook:
    metadata: PlaybookMetadata
    trade_mode: str

    def __init__(self, metadata: PlaybookMetadata) -> None:
        self.metadata = metadata

    def evaluate(self, context: Course49Context) -> PlaybookResult:
        if self.metadata.lifecycle != PlaybookLifecycle.PRODUCTION:
            return PlaybookResult(
                self.metadata.playbook_id, False, (), ("PLAYBOOK_NOT_PRODUCTION",)
            )
        if not context.entry_allowed:
            return PlaybookResult(
                self.metadata.playbook_id,
                False,
                (),
                (context.entry_block_reason or "ENTRY_NOT_ALLOWED",),
            )
        if context.market.phase != self.metadata.market_phase:
            return PlaybookResult(
                self.metadata.playbook_id,
                False,
                (),
                (f"MARKET_PHASE_{context.market.phase}",),
            )

        candidates: list[PlaybookCandidate] = []
        for leader in context.leaders:
            code = str(leader["code"])
            if (
                code in context.positions
                or str(leader.get("sector_code", "")) not in context.entry_sector_codes
                or str(leader.get("capital_risk", ""))
            ):
                continue
            mode = Course49V2Strategy.select_mode(
                Course49V2Strategy(), context.market, context.style, leader
            )
            if mode is None or mode[0] != self.trade_mode:
                continue
            lhb = leader.get("lhb") if isinstance(leader.get("lhb"), dict) else {"listed": False}
            behavior = (
                leader.get("limit_behavior")
                if isinstance(leader.get("limit_behavior"), dict)
                else {"limit_event": False}
            )
            lhb_confirmations = tuple(str(item) for item in lhb.get("confirmations", []))
            board_confirmations = tuple(
                str(item) for item in behavior.get("confirmations", [])
            )
            board_quality = float(leader.get("board_quality_score", 0.0) or 0.0)
            score = min(
                1.0,
                float(leader["leader_score"]) * 0.55
                + float(leader["sector_score"]) * 0.20
                + context.market.score * 0.10
                + board_quality * 0.10
                + (0.05 if set(lhb_confirmations) & POSITIVE_LHB_REASONS else 0.0),
            )
            target_weight = Course49V2Strategy.target_weight(
                Course49V2Strategy(),
                self.metadata.base_weight,
                context.suitability,
                board_quality,
            )
            evidence = {
                "price": float(leader["price"]),
                "entry_price": float(leader["price"]),
                "market_score": context.market.score,
                "market_score_change_3d": context.market.score_change_3d,
                "market_regime": context.market.regime,
                "market_phase": context.market.phase,
                "market_style": context.style.code,
                "style_suitability": context.suitability,
                "classified_style_suitability": context.style.suitability,
                "trade_mode": self.trade_mode,
                "benchmark_codes": context.style.benchmark_codes,
                "sector_code": leader["sector_code"],
                "sector_name": leader["sector_name"],
                "sector_rank": int(
                    leader.get("sector_rank")
                    or next(
                        (
                            item["rank"]
                            for item in context.sectors
                            if str(item["sector_code"])
                            == str(leader["sector_code"])
                        ),
                        999,
                    )
                ),
                "sector_score": leader["sector_score"],
                "theme_phase": leader["theme_phase"],
                "limit_streak": int(leader["streak"]),
                "leader_rank": int(leader["leader_rank"]),
                "role": leader["role"],
                "leader_score": leader["leader_score"],
                "base_target_weight": self.metadata.base_weight,
                "lhb": lhb,
                "limit_behavior": behavior,
                "framework_id": FRAMEWORK_ID,
                "playbook_id": self.metadata.playbook_id,
                "policy_version": POLICY_VERSION,
            }
            candidates.append(
                PlaybookCandidate(
                    self.metadata.playbook_id,
                    code,
                    score,
                    target_weight,
                    (
                        self.trade_mode,
                        "TOP3_THEME",
                        str(leader["role"]),
                        *lhb_confirmations,
                        *board_confirmations,
                    ),
                    evidence,
                    leader,
                )
            )
        return PlaybookResult(self.metadata.playbook_id, True, tuple(candidates), ())


class RecoveryIgnitionPlaybook(_ProductionPlaybook):
    trade_mode = "RECOVERY_IGNITION"


class FermentSecondBoardPlaybook(_ProductionPlaybook):
    trade_mode = "FERMENT_SECOND_BOARD"


class AccelerationCoreRelayPlaybook(_ProductionPlaybook):
    trade_mode = "ACCELERATION_CORE_RELAY"


def framework_metadata() -> FrameworkMetadata:
    return FrameworkMetadata(
        FRAMEWORK_ID,
        FRAMEWORK_VERSION,
        "49课体系",
        "以市场生态、题材演化、龙头角色和资金证据为共享上下文的短线交易体系。",
        "course49_system",
    )


def production_playbooks(
    requirements: tuple[DataRequirement, ...],
) -> tuple[Course49Playbook, ...]:
    definitions = (
        (
            RecoveryIgnitionPlaybook,
            "recovery_ignition",
            "修复启动",
            "修复期首板或二板的题材启动确认。",
            0.15,
            "RECOVERY",
        ),
        (
            FermentSecondBoardPlaybook,
            "ferment_second_board",
            "发酵二板",
            "发酵期二至三板的核心龙头确认。",
            0.25,
            "FERMENT",
        ),
        (
            AccelerationCoreRelayPlaybook,
            "acceleration_core_relay",
            "加速核心接力",
            "加速期空间龙头的强封接力。",
            0.20,
            "ACCELERATION",
        ),
    )
    return tuple(
        playbook_class(
            PlaybookMetadata(
                playbook_id,
                FRAMEWORK_ID,
                FRAMEWORK_VERSION,
                name,
                description,
                PlaybookLifecycle.PRODUCTION,
                requirements,
                base_weight,
                phase,
            )
        )
        for playbook_class, playbook_id, name, description, base_weight, phase in definitions
    )


class Course49Router:
    def route(
        self,
        results: tuple[PlaybookResult, ...],
    ) -> tuple[tuple[PlaybookCandidate, ...], tuple[dict[str, Any], ...]]:
        all_candidates = [item for result in results for item in result.candidates]
        ranked = sorted(
            all_candidates,
            key=lambda item: (
                -item.score,
                -float(item.leader.get("leader_score", 0.0) or 0.0),
                item.code,
                item.playbook_id,
            ),
        )
        selected: list[PlaybookCandidate] = []
        seen: set[str] = set()
        audit: list[dict[str, Any]] = []
        for rank, item in enumerate(ranked, start=1):
            status = "ROUTED"
            if item.code in seen:
                status = "DEDUPED"
            else:
                seen.add(item.code)
                selected.append(item)
            audit.append(
                {
                    "code": item.code,
                    "playbook_id": item.playbook_id,
                    "route_score": item.score,
                    "route_rank": rank,
                    "target_weight": item.target_weight,
                    "status": status,
                    **item.leader,
                }
            )
        return tuple(selected), tuple(audit)


class Course49SystemStrategy(Course49V2Strategy):
    metadata = StrategyMetadata(
        strategy_id="course49_system",
        version=FRAMEWORK_VERSION,
        name="49课体系",
        description="共享市场、题材、龙头与资金上下文，由生产剧本统一路由的49课体系。",
        frequency="1d-after-close",
        requires_approval=True,
        runtime_adapter=RuntimeAdapter.COURSE49_DAILY,
        data_requirements=Course49V2Strategy.metadata.data_requirements,
        strategy_family="course49_v2",
        lifecycle="PRODUCTION",
        framework_id=FRAMEWORK_ID,
        policy_version=POLICY_VERSION,
    )

    def __init__(self) -> None:
        self.playbook_registry = Course49PlaybookRegistry()
        for playbook in production_playbooks(self.metadata.data_requirements):
            self.playbook_registry.register(playbook, trusted_production=True)
        self.playbooks = self.playbook_registry.production()
        self.router = Course49Router()

    def scan(
        self,
        *,
        run_id: str,
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        sector_members: dict[str, dict[str, Any]],
        positions: list[dict[str, Any]],
        benchmark_bars: dict[str, pd.DataFrame],
        runtime_state: dict[str, dict[str, Any]] | None = None,
        limit_snapshot: dict[str, dict[str, Any]] | None = None,
        lhb_history: dict[str, dict[str, LhbFeatures]] | None = None,
        market_activity: pd.DataFrame | None = None,
        feature_matrix: dict[str, pd.DataFrame] | None = None,
        market_matrix: pd.DataFrame | None = None,
        asof: pd.Timestamp | None = None,
        eligible_codes: set[str] | None = None,
        playbook_ids: tuple[str, ...] | list[str] | None = None,
        context_metadata: dict[str, Any] | None = None,
    ) -> StrategyScanResult:
        lhb_history = lhb_history or {}
        runtime_state = runtime_state or {}
        context_metadata = context_metadata or {}
        if asof is None:
            visible_ends = [
                pd.Timestamp(frame.index[-1]) for frame in raw_bars.values() if not frame.empty
            ]
            if not visible_ends:
                raise ValueError("No visible daily bars are available")
            asof = max(visible_ends)
        else:
            asof = pd.Timestamp(asof)
        market = (
            course49_market_from_matrix(market_matrix, asof)
            if market_matrix is not None
            else None
        ) or self.analyze_market(raw_bars, names, market_activity)
        style = self.infer_style(benchmark_bars, market)
        entry_allowed = self.entry_allowed(market, style)
        suitability = self.effective_suitability(market, style)
        sectors = self.rank_sectors(
            front_bars,
            raw_bars,
            names,
            sector_members,
            feature_matrix=feature_matrix,
            asof=asof,
            eligible_codes=eligible_codes,
        )
        holding_sectors = sectors[:5]
        leaders = self.rank_leaders(
            holding_sectors,
            front_bars,
            raw_bars,
            names,
            sector_members,
            limit_snapshot=limit_snapshot,
            lhb_history=lhb_history,
        )
        position_by_code = {str(item["code"]): item for item in positions}
        stock_codes = sorted(eligible_codes if eligible_codes is not None else front_bars)
        context = Course49Context(
            asof=pd.Timestamp(asof),
            context_version=CONTEXT_VERSION,
            stock_pool_hash=str(context_metadata.get("stock_pool_hash") or _stable_hash(stock_codes)),
            sector_membership_hash=str(
                context_metadata.get("sector_membership_hash")
                or _stable_hash(
                    {
                        code: sorted(str(item) for item in value.get("members", []))
                        for code, value in sorted(sector_members.items())
                    }
                )
            ),
            data_completeness={
                "critical_benchmarks": style.reason != "missing_critical_benchmark",
                "sector_membership": bool(sector_members),
                "market_activity": market_activity is not None and not market_activity.empty,
                "event_history": bool(lhb_history),
                **dict(context_metadata.get("data_completeness") or {}),
            },
            market=market,
            style=style,
            suitability=suitability,
            entry_allowed=entry_allowed,
            entry_block_reason="" if entry_allowed else self.entry_block_reason(market, style),
            sectors=tuple(sectors),
            entry_sector_codes=frozenset(
                str(item["sector_code"]) for item in sectors[: self.entry_sector_count()]
            ),
            holding_sectors=tuple(holding_sectors),
            leaders=tuple(leaders),
            positions=position_by_code,
            runtime_state=runtime_state,
            front_bars=front_bars,
            raw_bars=raw_bars,
            lhb_history=lhb_history,
        )
        generated_at = _shanghai_time(market.asof.replace(hour=18))
        next_day = _shanghai_time((market.asof + pd.offsets.BDay(1)).replace(hour=9, minute=25))
        signals, next_runtime = self._exit_signals(context, run_id, generated_at, next_day)

        selected_ids = set(playbook_ids or [item.metadata.playbook_id for item in self.playbooks])
        unknown = selected_ids - {item.metadata.playbook_id for item in self.playbooks}
        if unknown:
            raise ValueError(f"Unknown Course49 playbooks: {', '.join(sorted(unknown))}")
        playbook_results = tuple(
            item.evaluate(context)
            for item in self.playbooks
            if item.metadata.playbook_id in selected_ids
        )
        routed, route_audit = self.router.route(playbook_results)
        for item in routed:
            price = float(item.evidence["price"])
            signals.append(
                PlatformSignal(
                    run_id=run_id,
                    strategy_id=self.metadata.strategy_id,
                    strategy_version=self.metadata.version,
                    generated_at=generated_at,
                    available_at=generated_at,
                    code=item.code,
                    side="BUY",
                    strength=item.score,
                    target_weight=item.target_weight,
                    horizon="daily-short",
                    valid_until=next_day,
                    stop_price=price * (1.0 - self.stop_loss_ratio()),
                    status=SignalStatus.PROPOSED,
                    reason_codes=item.reason_codes,
                    evidence=item.evidence,
                    framework_id=FRAMEWORK_ID,
                    playbook_id=item.playbook_id,
                    policy_version=POLICY_VERSION,
                )
            )

        playbook_states = []
        for result in playbook_results:
            routed_count = sum(
                1
                for item in route_audit
                if item["playbook_id"] == result.playbook_id and item["status"] == "ROUTED"
            )
            metadata = next(
                item.metadata for item in self.playbooks if item.metadata.playbook_id == result.playbook_id
            )
            playbook_states.append(
                {
                    "playbook_id": result.playbook_id,
                    "lifecycle": metadata.lifecycle.value,
                    "admitted": result.admitted,
                    "candidate_count": len(result.candidates),
                    "routed_count": routed_count,
                    "budget": metadata.base_weight * suitability if result.admitted else 0.0,
                    "blocked_reasons": list(result.blocked_reasons),
                }
            )
        state = {
            "asof": market.asof.date().isoformat(),
            "framework_id": FRAMEWORK_ID,
            "framework_version": FRAMEWORK_VERSION,
            "policy_version": POLICY_VERSION,
            "context_version": CONTEXT_VERSION,
            "context_hash": _context_hash(context),
            "stock_pool_hash": context.stock_pool_hash,
            "sector_membership_hash": context.sector_membership_hash,
            "data_completeness": context.data_completeness,
            "market_regime": market.regime,
            "market_phase": market.phase,
            "market_score": market.score,
            "market_score_change_3d": market.score_change_3d,
            "market_style": style.code,
            "style_suitability": suitability,
            "classified_style_suitability": style.suitability,
            "entry_allowed": entry_allowed,
            "entry_block_reason": context.entry_block_reason,
            "benchmark_codes": style.benchmark_codes,
            "benchmark_state": asdict(style),
            "trade_modes": sorted(
                str(signal.evidence.get("trade_mode"))
                for signal in signals
                if signal.side == "BUY"
            ),
            "strong_sectors": sectors,
            "runtime_state": next_runtime,
            "playbook_states": playbook_states,
            "route_audit": list(route_audit),
            "funnel": {
                "market": int(context_metadata.get("market_count") or len(front_bars)),
                "eligible": int(
                    context_metadata.get("eligible_count")
                    or len(eligible_codes if eligible_codes is not None else front_bars)
                ),
                "strong_themes": len(sectors[:3]),
                "leaders": len(leaders),
                "playbook_hits": sum(len(item.candidates) for item in playbook_results),
                "routed": len(routed),
            },
        }
        return StrategyScanResult(
            strategy=self.metadata,
            signals=tuple(signals),
            candidates=route_audit,
            state=state,
        )

    def _exit_signals(
        self,
        context: Course49Context,
        run_id: str,
        generated_at: Any,
        next_day: Any,
    ) -> tuple[list[PlatformSignal], dict[str, dict[str, Any]]]:
        holding_sector_map = {
            str(item["sector_code"]): item for item in context.holding_sectors
        }
        leaders_by_code = {str(item["code"]): item for item in context.leaders}
        signals: list[PlatformSignal] = []
        next_runtime: dict[str, dict[str, Any]] = {}
        for code, position in context.positions.items():
            frame = context.raw_bars.get(code)
            if frame is None or len(frame) < 20:
                next_runtime[code] = context.runtime_state.get(code, {})
                continue
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
            price = float(close.iloc[-1])
            below_ma5 = price < float(close.tail(5).mean())
            evidence = json_evidence(position)
            sector_code = str(evidence.get("sector_code", ""))
            sector = holding_sector_map.get(sector_code)
            leader = leaders_by_code.get(code)
            leader_rank = int(leader.get("leader_rank", 99)) if leader else 99
            capital = latest_lhb_features(context.lhb_history, code, context.market.asof)
            behavior = latest_limit_features(context.lhb_history, code, context.market.asof)
            immediate_reason = ""
            if price <= float(position.get("stop_price", 0.0) or 0.0):
                immediate_reason = "FIXED_STOP"
            elif capital and capital.risk:
                immediate_reason = "CAPITAL_DISTRIBUTION"
            elif context.market.phase == "ICE":
                immediate_reason = "MARKET_ICE"
            entry_price = float(
                evidence.get("entry_price")
                or position.get("average_price")
                or context.runtime_state.get(code, {}).get("entry_price")
                or price
            )
            state, reason = self.evaluate_exit_state(
                context.runtime_state.get(code, {}),
                price=price,
                entry_price=entry_price,
                below_ma5=below_ma5,
                market_weak=context.market.phase in {"RETREAT", "DIVERGENCE"}
                or context.market.regime == "WEAK",
                sector_weak=self.holding_sector_weak(sector_code, sector),
                leader_weak=self.holding_leader_weak(code, leader, leader_rank),
                immediate_reason=immediate_reason,
            )
            state.update(
                {"sector_code": sector_code, "asof": context.market.asof.date().isoformat()}
            )
            next_runtime[code] = state
            if not reason:
                continue
            source_playbook = str(evidence.get("playbook_id", "holding_management"))
            signals.append(
                PlatformSignal(
                    run_id=run_id,
                    strategy_id=self.metadata.strategy_id,
                    strategy_version=self.metadata.version,
                    generated_at=generated_at,
                    available_at=generated_at,
                    code=code,
                    side="SELL",
                    strength=1.0,
                    target_weight=0.0,
                    horizon="daily-short",
                    valid_until=next_day,
                    stop_price=None,
                    status=SignalStatus.APPROVED,
                    reason_codes=(reason,),
                    evidence={
                        "price": price,
                        "market_score": context.market.score,
                        "market_score_change_3d": context.market.score_change_3d,
                        "market_phase": context.market.phase,
                        "market_style": context.style.code,
                        "style_suitability": context.style.suitability,
                        "trade_mode": "HOLDING_MANAGEMENT",
                        "sector_code": sector_code,
                        "leader_rank": leader_rank,
                        "runtime_state": state,
                        "lhb": capital.as_dict() if capital else {"listed": False},
                        "limit_behavior": behavior.behavior_dict()
                        if behavior
                        else {"limit_event": False},
                        "framework_id": FRAMEWORK_ID,
                        "playbook_id": source_playbook,
                        "policy_version": POLICY_VERSION,
                    },
                    framework_id=FRAMEWORK_ID,
                    playbook_id=source_playbook,
                    policy_version=POLICY_VERSION,
                )
            )
        return signals, next_runtime


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _context_hash(context: Course49Context) -> str:
    return _stable_hash(
        {
            "version": context.context_version,
            "asof": context.asof.date().isoformat(),
            "stock_pool_hash": context.stock_pool_hash,
            "sector_membership_hash": context.sector_membership_hash,
            "market_phase": context.market.phase,
            "market_style": context.style.code,
            "sector_codes": [str(item["sector_code"]) for item in context.sectors],
            "leader_codes": [str(item["code"]) for item in context.leaders],
        }
    )
