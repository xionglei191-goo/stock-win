from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

import pandas as pd

from strategy_v1.portfolio import price_limit_ratio

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
FRAMEWORK_VERSION = "1.1.0"
CONTEXT_VERSION = "1.0.0"
POLICY_VERSION = "1.1.0"
LEADER_PULLBACK_PLAYBOOK_ID = "leader_pullback_reclaim"


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
    sector_members: dict[str, dict[str, Any]] = field(repr=False)
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
    stop_price: float | None = None


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


class LeaderPullbackReclaimPlaybook:
    """Research-only strong-leader pullback and close-reclaim playbook."""

    metadata: PlaybookMetadata
    trade_mode = "LEADER_PULLBACK_RECLAIM"

    def __init__(self, metadata: PlaybookMetadata) -> None:
        self.metadata = metadata

    def evaluate(self, context: Course49Context) -> PlaybookResult:
        if self.metadata.lifecycle != PlaybookLifecycle.RESEARCH:
            return PlaybookResult(
                self.metadata.playbook_id, False, (), ("PLAYBOOK_NOT_RESEARCH",)
            )
        healthy_divergence = (
            context.market.phase == "DIVERGENCE"
            and context.market.score >= 0.55
            and context.market.regime != "WEAK"
            and context.style.entry_allowed
        )
        ordinary_entry = (
            context.market.phase in {"RECOVERY", "FERMENT"}
            and context.entry_allowed
        )
        if not (healthy_divergence or ordinary_entry):
            return PlaybookResult(
                self.metadata.playbook_id,
                False,
                (),
                (f"MARKET_PHASE_{context.market.phase}",),
            )

        sector_results: list[PlaybookCandidate] = []
        for sector in context.sectors[:3]:
            theme_phase = str(sector.get("theme_phase", ""))
            if theme_phase not in {"START", "FERMENT", "DIVERGENCE"}:
                continue
            sector_code = str(sector["sector_code"])
            members = context.sector_members.get(sector_code, {}).get("members", [])
            rows: list[dict[str, Any]] = []
            for code_value in members:
                code = str(code_value)
                if code in context.positions:
                    continue
                features = _pullback_candidate_features(
                    context.front_bars.get(code),
                    context.raw_bars.get(code),
                    code,
                    context.asof,
                )
                if features is None:
                    continue
                capital = latest_lhb_features(context.lhb_history, code, context.asof)
                if capital and capital.risk:
                    continue
                rows.append(
                    {
                        "code": code,
                        "features": features,
                        "capital": capital,
                    }
                )
            if not rows:
                continue

            strength = pd.Series(
                {item["code"]: item["features"]["return_20d"] for item in rows}
            ).rank(method="average", pct=True)
            liquidity = pd.Series(
                {item["code"]: item["features"]["turnover_20d"] for item in rows}
            ).rank(method="average", pct=True)
            ranked: list[tuple[float, str, dict[str, Any], LhbFeatures | None]] = []
            for item in rows:
                code = item["code"]
                features = item["features"]
                volume_score = max(
                    0.0,
                    min(1.0, (1.0 - features["pullback_volume_ratio"]) / 0.5),
                )
                depth_score = max(
                    0.0,
                    min(1.0, 1.0 - abs(features["pullback_depth"] - 0.06) / 0.04),
                )
                score = min(
                    1.0,
                    float(sector.get("score", 0.0) or 0.0) * 0.25
                    + float(strength[code]) * 0.25
                    + volume_score * 0.20
                    + depth_score * 0.15
                    + float(liquidity[code]) * 0.15,
                )
                ranked.append((score, code, features, item["capital"]))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            score, code, features, capital = ranked[0]
            price = float(features["close"])
            stop_price = max(price * 0.95, float(features["pullback_low"]) * 0.99)
            leader = {
                "code": code,
                "name": code,
                "price": price,
                "sector_code": sector_code,
                "sector_name": str(sector.get("sector_name", sector_code)),
                "sector_rank": int(sector.get("rank", len(sector_results) + 1)),
                "sector_score": float(sector.get("score", 0.0) or 0.0),
                "theme_phase": theme_phase,
                "streak": 0,
                "leader_rank": 1,
                "role": "PULLBACK_LEADER",
                "leader_score": score,
                "board_quality_score": 0.0,
                "capital_risk": "",
            }
            evidence = {
                "price": price,
                "entry_price": price,
                "market_score": context.market.score,
                "market_score_change_3d": context.market.score_change_3d,
                "market_regime": context.market.regime,
                "market_phase": context.market.phase,
                "market_style": context.style.code,
                "style_suitability": context.suitability,
                "classified_style_suitability": context.style.suitability,
                "trade_mode": self.trade_mode,
                "benchmark_codes": context.style.benchmark_codes,
                "sector_code": sector_code,
                "sector_name": leader["sector_name"],
                "sector_rank": leader["sector_rank"],
                "sector_score": leader["sector_score"],
                "theme_phase": theme_phase,
                "leader_rank": 1,
                "role": leader["role"],
                "leader_score": score,
                "base_target_weight": self.metadata.base_weight,
                "lhb": capital.as_dict() if capital else {"listed": False},
                "limit_behavior": {"limit_event": False},
                "pullback": features,
                "entry_gap_min": -0.03,
                "entry_gap_max": 0.08,
                "framework_id": FRAMEWORK_ID,
                "playbook_id": self.metadata.playbook_id,
                "policy_version": POLICY_VERSION,
            }
            sector_results.append(
                PlaybookCandidate(
                    self.metadata.playbook_id,
                    code,
                    score,
                    self.metadata.base_weight,
                    (
                        self.trade_mode,
                        "TOP3_THEME",
                        "SHRINKING_PULLBACK",
                        "MA5_RECLAIM",
                    ),
                    evidence,
                    leader,
                    stop_price=stop_price,
                )
            )
        return PlaybookResult(
            self.metadata.playbook_id, True, tuple(sector_results[:3]), ()
        )


def _pullback_candidate_features(
    front: pd.DataFrame | None,
    raw: pd.DataFrame | None,
    code: str,
    asof: pd.Timestamp,
) -> dict[str, float | int] | None:
    if front is None or raw is None:
        return None
    visible_front = front.loc[:asof].copy().sort_index()
    visible_raw = raw.loc[:asof].copy().sort_index()
    if len(visible_front) < 21 or len(visible_raw) < 21:
        return None
    close = pd.to_numeric(visible_front.get("Close"), errors="coerce")
    open_price = pd.to_numeric(visible_front.get("Open"), errors="coerce")
    low = pd.to_numeric(visible_front.get("Low"), errors="coerce")
    volume = pd.to_numeric(visible_front.get("Volume"), errors="coerce")
    if any(item.isna().iloc[-21:].any() for item in (close, open_price, low, volume)):
        return None
    raw_close = pd.to_numeric(visible_raw.get("Close"), errors="coerce")
    raw_low = pd.to_numeric(visible_raw.get("Low"), errors="coerce")
    raw_volume = pd.to_numeric(visible_raw.get("Volume"), errors="coerce")
    if any(item.isna().iloc[-21:].any() for item in (raw_close, raw_low, raw_volume)):
        return None

    return_20d = float(close.iloc[-1] / close.iloc[-21] - 1.0)
    if return_20d < 0.10:
        return None
    ratio = price_limit_ratio(code, "")
    raw_returns = raw_close.pct_change(fill_method=None)
    if not bool((raw_returns.iloc[-21:-2] >= ratio - 0.001).any()):
        return None

    recent = close.iloc[-10:]
    peak_position = int(recent.to_numpy().argmax())
    peak_age = len(recent) - 1 - peak_position
    if peak_age < 2 or peak_age > 4:
        return None
    peak_index = len(close) - len(recent) + peak_position
    peak_close = float(close.iloc[peak_index])
    pullback_depth = float(1.0 - close.iloc[-1] / peak_close)
    if pullback_depth < 0.03 or pullback_depth > 0.10:
        return None

    pullback_volume = volume.iloc[peak_index + 1 : -1]
    baseline_volume = float(volume.iloc[-21:-1].mean())
    if pullback_volume.empty or baseline_volume <= 0:
        return None
    pullback_volume_ratio = float(pullback_volume.mean() / baseline_volume)
    if pullback_volume_ratio > 0.80:
        return None

    peak_date = pd.Timestamp(close.index[peak_index])
    raw_pullback_returns = raw_returns.loc[peak_date:].iloc[1:]
    if bool((raw_pullback_returns <= -ratio + 0.001).any()):
        return None

    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())
    adjusted_pullback_low = float(low.iloc[peak_index + 1 :].min())
    touched_support = adjusted_pullback_low <= max(ma5, ma10) * 1.01
    current_close = float(close.iloc[-1])
    previous_close = float(close.iloc[-2])
    current_return = current_close / previous_close - 1.0
    if not (
        touched_support
        and current_close >= ma5
        and current_close >= ma10 * 0.99
        and current_close > ma20
        and current_close > float(open_price.iloc[-1])
        and current_close > previous_close
        and current_return <= 0.05
    ):
        return None

    raw_pullback_low = float(raw_low.loc[peak_date:].iloc[1:].min())
    raw_current_close = float(raw_close.iloc[-1])
    turnover_20d = float((raw_close.tail(20) * raw_volume.tail(20)).mean())
    return {
        "close": raw_current_close,
        "adjusted_close": current_close,
        "return_20d": return_20d,
        "peak_age": peak_age,
        "peak_close": peak_close,
        "pullback_depth": pullback_depth,
        "pullback_volume_ratio": pullback_volume_ratio,
        "pullback_low": raw_pullback_low,
        "adjusted_pullback_low": adjusted_pullback_low,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "turnover_20d": turnover_20d,
    }


def build_leader_pullback_candidate_matrix(
    front_bars: dict[str, pd.DataFrame],
    raw_bars: dict[str, pd.DataFrame],
    names: dict[str, str],
    eligibility: pd.DataFrame,
) -> pd.DataFrame:
    """Broad point-in-time mask; the playbook applies the exact pullback rules."""
    codes = sorted(set(front_bars) & set(raw_bars) & set(eligibility.columns))
    if not codes:
        return pd.DataFrame()
    front_close = pd.concat(
        {code: pd.to_numeric(front_bars[code].get("Close"), errors="coerce") for code in codes},
        axis=1,
    ).sort_index()
    raw_close = pd.concat(
        {code: pd.to_numeric(raw_bars[code].get("Close"), errors="coerce") for code in codes},
        axis=1,
    ).reindex(front_close.index)
    raw_returns = raw_close.pct_change(fill_method=None)
    thresholds = pd.Series(
        {code: price_limit_ratio(code, names.get(code, "")) - 0.001 for code in codes}
    )
    limit_up = raw_returns.ge(thresholds, axis="columns")
    recent_limit = limit_up.shift(2).rolling(19, min_periods=19).max().fillna(False)
    strong = front_close / front_close.shift(20) - 1.0 >= 0.10
    eligible = eligibility.reindex(index=front_close.index, columns=codes).fillna(False)
    return recent_limit.astype(bool) & strong.fillna(False) & eligible.astype(bool)


def update_pullback_exit_state(
    state: dict[str, Any],
    *,
    price: float,
    entry_price: float,
    below_ma5: bool,
    below_ma10: bool,
    market_weak: bool,
    sector_weak: bool,
    immediate_reason: str = "",
) -> tuple[dict[str, Any], str]:
    del below_ma5
    current = {
        "market_weak_days": int(state.get("market_weak_days", 0) or 0),
        "sector_weak_days": int(state.get("sector_weak_days", 0) or 0),
        "below_ma10_days": int(state.get("below_ma10_days", 0) or 0),
        "max_close": float(state.get("max_close", entry_price) or entry_price),
        "entry_price": float(state.get("entry_price", entry_price) or entry_price),
        "holding_days": int(state.get("holding_days", 0) or 0) + 1,
    }
    current["max_close"] = max(current["max_close"], price)
    current["market_weak_days"] = current["market_weak_days"] + 1 if market_weak else 0
    current["sector_weak_days"] = current["sector_weak_days"] + 1 if sector_weak else 0
    current["below_ma10_days"] = current["below_ma10_days"] + 1 if below_ma10 else 0
    if immediate_reason:
        return current, immediate_reason
    if current["max_close"] > entry_price * 1.08 and price <= current["max_close"] * 0.96:
        return current, "PULLBACK_TRAILING_PROFIT"
    if current["below_ma10_days"] >= 2:
        return current, "PULLBACK_STRUCTURE_BROKEN"
    if current["market_weak_days"] >= 2:
        return current, "PULLBACK_MARKET_WEAK_CONFIRMED"
    if current["sector_weak_days"] >= 2:
        return current, "PULLBACK_SECTOR_FADED_CONFIRMED"
    if current["holding_days"] >= 5:
        return current, "PULLBACK_TIME_EXIT"
    return current, ""


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


def research_playbooks(
    requirements: tuple[DataRequirement, ...],
) -> tuple[Course49Playbook, ...]:
    return (
        LeaderPullbackReclaimPlaybook(
            PlaybookMetadata(
                LEADER_PULLBACK_PLAYBOOK_ID,
                FRAMEWORK_ID,
                "0.1.0",
                "强势回调确认低吸",
                "近20日强势股缩量回调后收盘重新站回短期均线。",
                PlaybookLifecycle.RESEARCH,
                requirements,
                0.10,
                "MULTI_PHASE",
            )
        ),
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
        for playbook in research_playbooks(self.metadata.data_requirements):
            self.playbook_registry.register(playbook)
        self.playbooks = self.playbook_registry.all()
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
            sector_members=sector_members,
            lhb_history=lhb_history,
        )
        generated_at = _shanghai_time(market.asof.replace(hour=18))
        next_day = _shanghai_time((market.asof + pd.offsets.BDay(1)).replace(hour=9, minute=25))
        signals, next_runtime = self._exit_signals(context, run_id, generated_at, next_day)

        selected_ids = set(
            playbook_ids
            or [
                item.metadata.playbook_id
                for item in self.playbooks
                if item.metadata.lifecycle == PlaybookLifecycle.PRODUCTION
            ]
        )
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
                    stop_price=item.stop_price
                    if item.stop_price is not None
                    else price * (1.0 - self.stop_loss_ratio()),
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
            below_ma10 = price < float(close.tail(10).mean())
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
            source_playbook = str(evidence.get("playbook_id", "holding_management"))
            if source_playbook == LEADER_PULLBACK_PLAYBOOK_ID:
                state, reason = update_pullback_exit_state(
                    context.runtime_state.get(code, {}),
                    price=price,
                    entry_price=entry_price,
                    below_ma5=below_ma5,
                    below_ma10=below_ma10,
                    market_weak=context.market.phase == "RETREAT"
                    or context.market.regime == "WEAK",
                    sector_weak=self.holding_sector_weak(sector_code, sector),
                    immediate_reason=immediate_reason,
                )
            else:
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
