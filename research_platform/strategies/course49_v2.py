from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np
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
    Course49Strategy,
    _consecutive_limit_ups,
    _cross_section_percentile,
    _daily_return,
    _is_limit_return,
    _shanghai_time,
    course49_market_from_matrix,
    infer_theme_phase,
    json_evidence,
)


CRITICAL_BENCHMARKS = {
    "large": ("000300.CSI", "000300.SH"),
    "small": ("000852.CSI", "000852.SH"),
}
GROWTH_BENCHMARKS = ("399006.SZ",)
POSITIVE_LHB_REASONS = frozenset(
    {"LHB_NET_BUY", "INSTITUTION_BUY", "NORTHBOUND_BUY", "REPEATED_LIST"}
)
RECOVERY_BOARD_REASONS = frozenset(
    {"EARLY_SEAL", "STRONG_SEAL", "RELIABLE_FIRST_BOARD"}
)
ACCELERATION_BOARD_REASONS = frozenset(
    {"EARLY_SEAL", "STRONG_SEAL", "PREMIUM_MEMORY"}
)
COURSE49_SECTOR_FEATURES = (
    "limit_up",
    "return_5d",
    "above_ma20",
    "volume_ratio",
)


@dataclass(frozen=True)
class MarketStyle:
    code: str
    suitability: float
    entry_allowed: bool
    benchmark_codes: dict[str, str | None]
    large_return_5d: float | None
    large_return_20d: float | None
    small_return_5d: float | None
    small_return_20d: float | None
    growth_return_20d: float | None
    large_above_ma20: bool | None
    small_above_ma20: bool | None
    growth_above_ma20: bool | None
    reason: str = ""


def infer_market_style(
    benchmark_bars: dict[str, pd.DataFrame],
    market: Course49Market,
) -> MarketStyle:
    selected = {
        "large": _first_available(benchmark_bars, CRITICAL_BENCHMARKS["large"]),
        "small": _first_available(benchmark_bars, CRITICAL_BENCHMARKS["small"]),
        "growth": _first_available(benchmark_bars, GROWTH_BENCHMARKS),
    }
    large = _benchmark_features(benchmark_bars.get(selected["large"])) if selected["large"] else None
    small = _benchmark_features(benchmark_bars.get(selected["small"])) if selected["small"] else None
    growth = _benchmark_features(benchmark_bars.get(selected["growth"])) if selected["growth"] else None
    values = {
        "benchmark_codes": selected,
        "large_return_5d": _value(large, "return_5d"),
        "large_return_20d": _value(large, "return_20d"),
        "small_return_5d": _value(small, "return_5d"),
        "small_return_20d": _value(small, "return_20d"),
        "growth_return_20d": _value(growth, "return_20d"),
        "large_above_ma20": _value(large, "above_ma20"),
        "small_above_ma20": _value(small, "above_ma20"),
        "growth_above_ma20": _value(growth, "above_ma20"),
    }
    if large is None or small is None:
        return MarketStyle("UNKNOWN", 0.0, False, reason="missing_critical_benchmark", **values)
    if market.phase in {"ICE", "RETREAT"} or (
        not bool(large["above_ma20"]) and not bool(small["above_ma20"])
    ):
        return MarketStyle("DEFENSIVE", 0.0, False, reason="risk_off", **values)

    small_lead_20 = float(small["return_20d"] - large["return_20d"])
    if (
        small_lead_20 >= 0.02
        and float(small["return_5d"] - large["return_5d"]) >= 0.0
        and market.limit_strength_percentile >= 0.55
    ):
        return MarketStyle("SMALL_CAP_SPECULATION", 1.0, True, **values)
    if (
        bool(large["above_ma20"])
        and bool(small["above_ma20"])
        and market.advance_percentile >= 0.55
    ):
        return MarketStyle("BROAD_RISK_ON", 0.80, True, **values)
    if (
        growth is not None
        and bool(growth["above_ma20"])
        and float(growth["return_20d"] - large["return_20d"]) >= 0.02
    ):
        return MarketStyle("GROWTH_TREND", 0.40, True, **values)
    if small_lead_20 <= -0.02 and bool(large["above_ma20"]):
        return MarketStyle("LARGE_CAP_TREND", 0.25, True, **values)
    return MarketStyle("MIXED", 0.55, True, **values)


def _rank_sectors_from_matrix(
    feature_matrix: dict[str, pd.DataFrame],
    sector_members: dict[str, dict[str, Any]],
    *,
    asof: pd.Timestamp,
    eligible_codes: set[str],
) -> list[dict[str, Any]]:
    visible = {
        key: feature_matrix[key].loc[:asof]
        for key in COURSE49_SECTOR_FEATURES
        if key in feature_matrix and not feature_matrix[key].empty
    }
    if any(key not in visible or visible[key].empty for key in COURSE49_SECTOR_FEATURES):
        return []

    latest = pd.DataFrame(
        {key: visible[key].iloc[-1] for key in COURSE49_SECTOR_FEATURES}
    )
    codes = sorted(set(latest.index) & eligible_codes)
    if not codes:
        return []
    latest = latest.reindex(codes).dropna()
    if latest.empty:
        return []
    valid_codes = set(latest.index)
    recent_limits = (
        visible["limit_up"]
        .tail(5)
        .reindex(columns=latest.index)
        .fillna(False)
        .astype(bool)
    )
    market_return = float(pd.to_numeric(latest["return_5d"], errors="coerce").median())
    rows: list[dict[str, Any]] = []
    for sector_code, metadata in sector_members.items():
        members = [code for code in metadata.get("members", []) if code in valid_codes]
        if len(members) < 3:
            continue
        table = latest.loc[members]
        counts = recent_limits.loc[:, members].sum(axis=1)
        limit_count = int(table["limit_up"].astype(bool).sum())
        previous_count = int(counts.iloc[-2]) if len(counts) >= 2 else 0
        recent_peak = int(counts.max()) if not counts.empty else limit_count
        breadth = float(table["above_ma20"].astype(bool).mean())
        volume_ratio = float(pd.to_numeric(table["volume_ratio"], errors="coerce").median())
        rows.append(
            {
                "sector_code": str(sector_code),
                "sector_name": str(metadata.get("name", sector_code)),
                "limit_count": limit_count,
                "previous_limit_count": previous_count,
                "recent_limit_peak": recent_peak,
                "limit_ratio": float(table["limit_up"].astype(bool).mean()),
                "relative_return_5d": float(
                    pd.to_numeric(table["return_5d"], errors="coerce").median()
                    - market_return
                ),
                "breadth": breadth,
                "volume_ratio": volume_ratio,
                "theme_phase": infer_theme_phase(
                    limit_count,
                    previous_count,
                    recent_peak,
                    breadth,
                    volume_ratio,
                ),
                "valid_members": len(members),
            }
        )
    if not rows:
        return []
    table = pd.DataFrame(rows)
    table["limit_factor"] = (
        _cross_section_percentile(table["limit_count"]) * 0.6
        + _cross_section_percentile(table["limit_ratio"]) * 0.4
    )
    table["base_score"] = (
        table["limit_factor"] * 0.40
        + _cross_section_percentile(table["relative_return_5d"]) * 0.25
        + _cross_section_percentile(table["breadth"]) * 0.20
        + _cross_section_percentile(table["volume_ratio"]) * 0.15
    )
    phase_scores = {
        "START": 0.80,
        "FERMENT": 0.90,
        "ACCELERATION": 0.75,
        "DIVERGENCE": 0.45,
        "CLIMAX": 0.25,
        "RETREAT": 0.0,
    }
    table["phase_score"] = table["theme_phase"].map(phase_scores).fillna(0.5)
    table["score"] = table["base_score"] * 0.85 + table["phase_score"] * 0.15
    table = table[table["limit_count"] >= 4]
    table = table.sort_values(
        ["score", "limit_count", "sector_code"], ascending=[False, False, True]
    )
    table["rank"] = range(1, len(table) + 1)
    return table.to_dict("records")


def select_trade_mode(
    market_phase: str,
    style: str,
    leader: dict[str, Any],
) -> tuple[str, float] | None:
    streak = int(leader.get("streak", 0) or 0)
    rank = int(leader.get("leader_rank", 99) or 99)
    role = str(leader.get("role", ""))
    theme = str(leader.get("theme_phase", ""))
    board_score = float(leader.get("board_quality_score", 0.0) or 0.0)
    board = leader.get("limit_behavior") if isinstance(leader.get("limit_behavior"), dict) else {}
    board_reasons = {str(item) for item in board.get("confirmations", [])}
    lhb = leader.get("lhb") if isinstance(leader.get("lhb"), dict) else {}
    lhb_reasons = {str(item) for item in lhb.get("confirmations", [])}
    capital_confirmed = bool(lhb_reasons & POSITIVE_LHB_REASONS)

    if (
        market_phase == "RECOVERY"
        and style in {"SMALL_CAP_SPECULATION", "BROAD_RISK_ON"}
        and theme in {"START", "FERMENT"}
        and streak in {1, 2}
        and rank == 1
        and board_score >= 0.65
        and bool(board_reasons & RECOVERY_BOARD_REASONS)
    ):
        return "RECOVERY_IGNITION", 0.15
    if (
        market_phase == "FERMENT"
        and style in {"SMALL_CAP_SPECULATION", "BROAD_RISK_ON", "MIXED"}
        and theme in {"START", "FERMENT"}
        and streak in {2, 3}
        and rank == 1
        and (board_score >= 0.60 or capital_confirmed)
    ):
        return "FERMENT_SECOND_BOARD", 0.25
    if (
        market_phase == "ACCELERATION"
        and style in {"SMALL_CAP_SPECULATION", "BROAD_RISK_ON"}
        and theme in {"FERMENT", "ACCELERATION"}
        and streak >= 2
        and role == "SPACE_LEADER"
        and board_score >= 0.70
        and bool(board_reasons & ACCELERATION_BOARD_REASONS)
    ):
        return "ACCELERATION_CORE_RELAY", 0.20
    return None


def adaptive_target_weight(base_weight: float, suitability: float, board_quality: float) -> float:
    weight = base_weight * suitability + (0.03 if board_quality >= 0.80 else 0.0)
    return float(max(0.10, min(0.30, weight)))


def update_exit_state(
    state: dict[str, Any],
    *,
    price: float,
    entry_price: float,
    below_ma5: bool,
    market_weak: bool,
    sector_weak: bool,
    leader_weak: bool,
    immediate_reason: str = "",
) -> tuple[dict[str, Any], str]:
    current = {
        "market_weak_days": int(state.get("market_weak_days", 0) or 0),
        "sector_weak_days": int(state.get("sector_weak_days", 0) or 0),
        "leader_weak_days": int(state.get("leader_weak_days", 0) or 0),
        "max_close": float(state.get("max_close", entry_price) or entry_price),
        "entry_price": float(state.get("entry_price", entry_price) or entry_price),
        "holding_days": int(state.get("holding_days", 0) or 0) + 1,
    }
    current["max_close"] = max(current["max_close"], price)
    current["market_weak_days"] = current["market_weak_days"] + 1 if market_weak else 0
    current["sector_weak_days"] = current["sector_weak_days"] + 1 if sector_weak else 0
    current["leader_weak_days"] = current["leader_weak_days"] + 1 if leader_weak else 0
    if immediate_reason:
        return current, immediate_reason
    if current["max_close"] >= entry_price * 1.08 and price <= current["max_close"] * 0.96:
        return current, "TRAILING_PROFIT"
    if current["market_weak_days"] >= 2 and below_ma5:
        return current, "MARKET_WEAK_CONFIRMED"
    if current["sector_weak_days"] >= 2 and (below_ma5 or leader_weak):
        return current, "SECTOR_FADED_CONFIRMED"
    if current["leader_weak_days"] >= 2 and below_ma5:
        return current, "LEADER_LOST_CONFIRMED"
    return current, ""


class Course49V2Strategy(Course49Strategy):
    metadata = StrategyMetadata(
        strategy_id="course49_v2",
        version="2.0.0",
        name="49课自适应龙头",
        description="按市场风格切换启动、二板与核心接力模式，并持久化弱势确认状态",
        frequency="1d-after-close",
        requires_approval=True,
        runtime_adapter=RuntimeAdapter.COURSE49_DAILY,
        data_requirements=(
            DataRequirement("bars", "1d", "front", 120, True, ("Open", "High", "Low", "Close", "Volume", "Amount")),
            DataRequirement("bars", "1d", "none", 120, True, ("Open", "High", "Low", "Close", "Volume", "Amount")),
            DataRequirement("sectors", "snapshot", "none", 0, True, ("members",)),
            DataRequirement("limit_behavior", "event", "none", 60, False),
            DataRequirement("dragon_tiger", "event", "none", 60, False),
            DataRequirement("market_activity", "1d", "none", 60, False),
        ),
    )

    def holding_sector_weak(
        self,
        sector_code: str,
        sector: dict[str, Any] | None,
    ) -> bool:
        del sector_code
        return sector is None or float(sector.get("score", 0.0)) < 0.45

    def holding_leader_weak(
        self,
        code: str,
        leader: dict[str, Any] | None,
        leader_rank: int,
    ) -> bool:
        del code
        return leader is None or leader_rank > 2

    def infer_style(
        self,
        benchmark_bars: dict[str, pd.DataFrame],
        market: Course49Market,
    ) -> MarketStyle:
        return infer_market_style(benchmark_bars, market)

    def entry_allowed(self, market: Course49Market, style: MarketStyle) -> bool:
        return bool(market.entry_allowed and style.entry_allowed)

    def entry_block_reason(self, market: Course49Market, style: MarketStyle) -> str:
        if style.reason:
            return style.reason
        if not market.entry_allowed:
            return "market_ecology_not_entry_ready"
        return ""

    def select_mode(
        self,
        market: Course49Market,
        style: MarketStyle,
        leader: dict[str, Any],
    ) -> tuple[str, float] | None:
        return select_trade_mode(market.phase, style.code, leader)

    def effective_suitability(self, market: Course49Market, style: MarketStyle) -> float:
        return style.suitability

    def target_weight(
        self,
        base_weight: float,
        suitability: float,
        board_quality: float,
    ) -> float:
        return adaptive_target_weight(base_weight, suitability, board_quality)

    def stop_loss_ratio(self) -> float:
        return 0.05

    def evaluate_exit_state(
        self,
        state: dict[str, Any],
        *,
        price: float,
        entry_price: float,
        below_ma5: bool,
        market_weak: bool,
        sector_weak: bool,
        leader_weak: bool,
        immediate_reason: str,
    ) -> tuple[dict[str, Any], str]:
        return update_exit_state(
            state,
            price=price,
            entry_price=entry_price,
            below_ma5=below_ma5,
            market_weak=market_weak,
            sector_weak=sector_weak,
            leader_weak=leader_weak,
            immediate_reason=immediate_reason,
        )

    def entry_sector_count(self) -> int:
        return 3

    def entry_sector_reason(self) -> str:
        return "TOP3_THEME"

    def leader_in_entry_scope(
        self,
        leader: dict[str, Any],
        entry_sector_codes: set[str],
    ) -> bool:
        return str(leader.get("sector_code", "")) in entry_sector_codes

    def rank_sectors(
        self,
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        sector_members: dict[str, dict[str, Any]],
        feature_matrix: dict[str, pd.DataFrame] | None = None,
        asof: pd.Timestamp | None = None,
        eligible_codes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        features: dict[str, dict[str, Any]] = {}
        limit_series: dict[str, pd.Series] = {}
        if asof is None:
            asof = max(
                (pd.Timestamp(frame.index[-1]) for frame in raw_bars.values() if not frame.empty),
                default=pd.Timestamp.min,
            )
        else:
            asof = pd.Timestamp(asof)
        if feature_matrix:
            return _rank_sectors_from_matrix(
                feature_matrix,
                sector_members,
                asof=asof,
                eligible_codes=eligible_codes
                if eligible_codes is not None
                else set(front_bars),
            )
        else:
            for code, front in front_bars.items():
                raw = raw_bars.get(code)
                if raw is None or len(front) < 21 or len(raw) < 2:
                    continue
                close = pd.to_numeric(front.get("Close"), errors="coerce").dropna()
                raw_close = pd.to_numeric(raw.get("Close"), errors="coerce").dropna()
                volume = pd.to_numeric(front.get("Volume"), errors="coerce").reindex(close.index).dropna()
                if len(close) < 21 or len(raw_close) < 2 or len(volume) < 20:
                    continue
                ratio = price_limit_ratio(code, names.get(code, ""))
                returns = raw_close.pct_change(fill_method=None)
                limit_series[code] = returns >= ratio - 0.001
                features[code] = {
                    "limit_up": _is_limit_return(float(returns.iloc[-1]), ratio),
                    "return_5d": _daily_return(front, 5),
                    "above_ma20": bool(close.iloc[-1] > close.tail(20).mean()),
                    "volume_ratio": float(volume.iloc[-1] / volume.tail(20).mean())
                    if volume.tail(20).mean() > 0
                    else 0.0,
                }
        market_returns = [float(item["return_5d"]) for item in features.values()]
        market_return = float(np.nanmedian(market_returns)) if market_returns else 0.0
        rows: list[dict[str, Any]] = []
        for sector_code, metadata in sector_members.items():
            members = [code for code in metadata.get("members", []) if code in features]
            if len(members) < 3:
                continue
            table = pd.DataFrame([features[code] for code in members], index=members)
            histories = [limit_series[code].rename(code) for code in members if code in limit_series]
            counts = pd.concat(histories, axis=1).fillna(False).sum(axis=1) if histories else pd.Series(dtype=float)
            limit_count = int(table["limit_up"].sum())
            previous_count = int(counts.iloc[-2]) if len(counts) >= 2 else 0
            recent_peak = int(counts.tail(5).max()) if not counts.empty else limit_count
            breadth = float(table["above_ma20"].mean())
            volume_ratio = float(table["volume_ratio"].median())
            rows.append(
                {
                    "sector_code": str(sector_code),
                    "sector_name": str(metadata.get("name", sector_code)),
                    "limit_count": limit_count,
                    "previous_limit_count": previous_count,
                    "recent_limit_peak": recent_peak,
                    "limit_ratio": float(table["limit_up"].mean()),
                    "relative_return_5d": float(table["return_5d"].median() - market_return),
                    "breadth": breadth,
                    "volume_ratio": volume_ratio,
                    "theme_phase": infer_theme_phase(
                        limit_count, previous_count, recent_peak, breadth, volume_ratio
                    ),
                    "valid_members": len(members),
                }
            )
        if not rows:
            return []
        table = pd.DataFrame(rows)
        table["limit_factor"] = (
            _cross_section_percentile(table["limit_count"]) * 0.6
            + _cross_section_percentile(table["limit_ratio"]) * 0.4
        )
        table["base_score"] = (
            table["limit_factor"] * 0.40
            + _cross_section_percentile(table["relative_return_5d"]) * 0.25
            + _cross_section_percentile(table["breadth"]) * 0.20
            + _cross_section_percentile(table["volume_ratio"]) * 0.15
        )
        phase_scores = {
            "START": 0.80,
            "FERMENT": 0.90,
            "ACCELERATION": 0.75,
            "DIVERGENCE": 0.45,
            "CLIMAX": 0.25,
            "RETREAT": 0.0,
        }
        table["phase_score"] = table["theme_phase"].map(phase_scores).fillna(0.5)
        table["score"] = table["base_score"] * 0.85 + table["phase_score"] * 0.15
        table = table[table["limit_count"] >= 4]
        table = table.sort_values(
            ["score", "limit_count", "sector_code"], ascending=[False, False, True]
        )
        table["rank"] = range(1, len(table) + 1)
        return table.to_dict("records")

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
    ) -> StrategyScanResult:
        lhb_history = lhb_history or {}
        runtime_state = runtime_state or {}
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
        entry_sectors = sectors[: self.entry_sector_count()]
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
        generated_at = _shanghai_time(market.asof.replace(hour=18))
        next_day = _shanghai_time((market.asof + pd.offsets.BDay(1)).replace(hour=9, minute=25))
        position_by_code = {str(item["code"]): item for item in positions}
        holding_sector_map = {str(item["sector_code"]): item for item in holding_sectors}
        leaders_by_code = {str(item["code"]): item for item in leaders}
        signals: list[PlatformSignal] = []
        next_runtime: dict[str, dict[str, Any]] = {}

        for code, position in position_by_code.items():
            frame = raw_bars.get(code)
            if frame is None or len(frame) < 20:
                next_runtime[code] = runtime_state.get(code, {})
                continue
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
            price = float(close.iloc[-1])
            below_ma5 = price < float(close.tail(5).mean())
            evidence = json_evidence(position)
            sector_code = str(evidence.get("sector_code", ""))
            sector = holding_sector_map.get(sector_code)
            leader = leaders_by_code.get(code)
            leader_rank = int(leader.get("leader_rank", 99)) if leader else 99
            capital = latest_lhb_features(lhb_history, code, market.asof)
            behavior = latest_limit_features(lhb_history, code, market.asof)
            immediate_reason = ""
            if price <= float(position.get("stop_price", 0.0) or 0.0):
                immediate_reason = "FIXED_STOP"
            elif capital and capital.risk:
                immediate_reason = "CAPITAL_DISTRIBUTION"
            elif market.phase == "ICE":
                immediate_reason = "MARKET_ICE"
            entry_price = float(
                evidence.get("entry_price")
                or position.get("average_price")
                or runtime_state.get(code, {}).get("entry_price")
                or price
            )
            state, reason = self.evaluate_exit_state(
                runtime_state.get(code, {}),
                price=price,
                entry_price=entry_price,
                below_ma5=below_ma5,
                market_weak=market.phase in {"RETREAT", "DIVERGENCE"} or market.regime == "WEAK",
                sector_weak=self.holding_sector_weak(sector_code, sector),
                leader_weak=self.holding_leader_weak(code, leader, leader_rank),
                immediate_reason=immediate_reason,
            )
            state.update({"sector_code": sector_code, "asof": market.asof.date().isoformat()})
            next_runtime[code] = state
            if reason:
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
                            "market_score": market.score,
                            "market_score_change_3d": market.score_change_3d,
                            "market_phase": market.phase,
                            "market_style": style.code,
                            "style_suitability": style.suitability,
                            "trade_mode": "HOLDING_MANAGEMENT",
                            "sector_code": sector_code,
                            "leader_rank": leader_rank,
                            "runtime_state": state,
                            "lhb": capital.as_dict() if capital else {"listed": False},
                            "limit_behavior": behavior.behavior_dict()
                            if behavior
                            else {"limit_event": False},
                        },
                    )
                )

        if entry_allowed:
            entry_sector_codes = {str(item["sector_code"]) for item in entry_sectors}
            for leader in leaders:
                code = str(leader["code"])
                if (
                    code in position_by_code
                    or not self.leader_in_entry_scope(leader, entry_sector_codes)
                    or str(leader.get("capital_risk", ""))
                ):
                    continue
                mode = self.select_mode(market, style, leader)
                if mode is None:
                    continue
                trade_mode, base_weight = mode
                lhb = leader.get("lhb") if isinstance(leader.get("lhb"), dict) else {"listed": False}
                behavior = (
                    leader.get("limit_behavior")
                    if isinstance(leader.get("limit_behavior"), dict)
                    else {"limit_event": False}
                )
                lhb_confirmations = tuple(str(item) for item in lhb.get("confirmations", []))
                board_confirmations = tuple(str(item) for item in behavior.get("confirmations", []))
                board_quality = float(leader.get("board_quality_score", 0.0) or 0.0)
                strength = min(
                    1.0,
                    float(leader["leader_score"]) * 0.55
                    + float(leader["sector_score"]) * 0.20
                    + market.score * 0.10
                    + board_quality * 0.10
                    + (0.05 if set(lhb_confirmations) & POSITIVE_LHB_REASONS else 0.0),
                )
                price = float(leader["price"])
                target_weight = self.target_weight(base_weight, suitability, board_quality)
                signals.append(
                    PlatformSignal(
                        run_id=run_id,
                        strategy_id=self.metadata.strategy_id,
                        strategy_version=self.metadata.version,
                        generated_at=generated_at,
                        available_at=generated_at,
                        code=code,
                        side="BUY",
                        strength=strength,
                        target_weight=target_weight,
                        horizon="daily-short",
                        valid_until=next_day,
                        stop_price=price * (1.0 - self.stop_loss_ratio()),
                        status=SignalStatus.PROPOSED,
                        reason_codes=(
                            trade_mode,
                            self.entry_sector_reason(),
                            str(leader["role"]),
                            *lhb_confirmations,
                            *board_confirmations,
                        ),
                        evidence={
                            "price": price,
                            "entry_price": price,
                            "market_score": market.score,
                            "market_score_change_3d": market.score_change_3d,
                            "market_regime": market.regime,
                            "market_phase": market.phase,
                            "market_style": style.code,
                            "style_suitability": suitability,
                            "classified_style_suitability": style.suitability,
                            "trade_mode": trade_mode,
                            "benchmark_codes": style.benchmark_codes,
                            "sector_code": leader["sector_code"],
                            "sector_name": leader["sector_name"],
                            "sector_rank": int(
                                leader.get("sector_rank")
                                or next(
                                    (
                                        item["rank"]
                                        for item in sectors
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
                            "base_target_weight": base_weight,
                            "lhb": lhb,
                            "limit_behavior": behavior,
                        },
                    )
                )

        state = {
            "asof": market.asof.date().isoformat(),
            "market_regime": market.regime,
            "market_phase": market.phase,
            "market_score": market.score,
            "market_score_change_3d": market.score_change_3d,
            "market_style": style.code,
            "style_suitability": suitability,
            "classified_style_suitability": style.suitability,
            "entry_allowed": entry_allowed,
            "entry_block_reason": "" if entry_allowed else self.entry_block_reason(market, style),
            "benchmark_codes": style.benchmark_codes,
            "benchmark_state": asdict(style),
            "trade_modes": sorted(
                {
                    str(signal.evidence.get("trade_mode"))
                    for signal in signals
                    if signal.side == "BUY"
                }
            ),
            "strong_sectors": sectors,
            "runtime_state": next_runtime,
        }
        return StrategyScanResult(
            strategy=self.metadata,
            signals=tuple(signals),
            candidates=tuple(leaders),
            state=state,
        )


def _first_available(
    bars: dict[str, pd.DataFrame], candidates: tuple[str, ...]
) -> str | None:
    for code in candidates:
        frame = bars.get(code)
        if _benchmark_features(frame) is not None:
            return code
    return None


def _benchmark_features(frame: pd.DataFrame | None) -> dict[str, float | bool] | None:
    if frame is None:
        return None
    close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
    if len(close) < 21 or close.iloc[-21] <= 0 or close.iloc[-6] <= 0:
        return None
    return {
        "return_5d": float(close.iloc[-1] / close.iloc[-6] - 1.0),
        "return_20d": float(close.iloc[-1] / close.iloc[-21] - 1.0),
        "above_ma20": bool(close.iloc[-1] > close.tail(20).mean()),
    }


def _value(features: dict[str, Any] | None, key: str) -> Any:
    return features.get(key) if features is not None else None


def build_course49_feature_matrix(
    front_bars: dict[str, pd.DataFrame],
    raw_bars: dict[str, pd.DataFrame],
    names: dict[str, str],
) -> dict[str, pd.DataFrame]:
    codes = sorted(set(front_bars) & set(raw_bars))
    if not codes:
        return {}
    front_close = pd.concat(
        {code: pd.to_numeric(front_bars[code].get("Close"), errors="coerce") for code in codes},
        axis=1,
    ).sort_index()
    volume = pd.concat(
        {code: pd.to_numeric(front_bars[code].get("Volume"), errors="coerce") for code in codes},
        axis=1,
    ).reindex(front_close.index)
    raw_close = pd.concat(
        {code: pd.to_numeric(raw_bars[code].get("Close"), errors="coerce") for code in codes},
        axis=1,
    ).sort_index()
    raw_returns = raw_close.pct_change(fill_method=None)
    thresholds = pd.Series(
        {code: price_limit_ratio(code, names.get(code, "")) - 0.001 for code in codes}
    )
    return {
        "return_5d": front_close / front_close.shift(5) - 1.0,
        "above_ma20": front_close > front_close.rolling(20, min_periods=20).mean(),
        "volume_ratio": volume / volume.rolling(20, min_periods=20).mean(),
        "limit_up": raw_returns.ge(thresholds, axis="columns"),
    }


def build_course49_eligibility_matrix(
    front_bars: dict[str, pd.DataFrame],
    names: dict[str, str],
    *,
    minimum_listing_bars: int = 60,
    minimum_average_turnover: float = 20_000_000.0,
) -> pd.DataFrame:
    """Precompute the point-in-time A-share eligibility mask."""

    codes = sorted(front_bars)
    if not codes:
        return pd.DataFrame()
    close = pd.concat(
        {code: pd.to_numeric(front_bars[code].get("Close"), errors="coerce") for code in codes},
        axis=1,
    ).sort_index()
    volume = pd.concat(
        {code: pd.to_numeric(front_bars[code].get("Volume"), errors="coerce") for code in codes},
        axis=1,
    ).reindex(close.index)
    amount_columns: dict[str, pd.Series] = {}
    for code in codes:
        frame = front_bars[code]
        if "Amount" in frame and pd.to_numeric(frame["Amount"], errors="coerce").notna().any():
            scale = 1.0 if frame.attrs.get("amount_unit") == "CNY" else 10_000.0
            amount_columns[code] = pd.to_numeric(frame["Amount"], errors="coerce") * scale
        else:
            amount_columns[code] = (
                pd.to_numeric(frame.get("Close"), errors="coerce")
                * pd.to_numeric(frame.get("Volume"), errors="coerce")
            )
    amount = pd.concat(amount_columns, axis=1).reindex(close.index)
    listing_count = close.notna().cumsum()
    eligible = (
        close.notna()
        & volume.fillna(0.0).gt(0.0)
        & listing_count.ge(minimum_listing_bars)
        & amount.rolling(20, min_periods=20).mean().ge(minimum_average_turnover)
    )
    allowed = pd.Series(
        {
            code: "ST" not in names.get(code, "").upper()
            and "退" not in names.get(code, "")
            for code in codes
        }
    )
    return eligible & allowed
