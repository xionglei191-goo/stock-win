from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ENTRY_MARKET_PHASES = frozenset({"RECOVERY", "FERMENT", "ACCELERATION"})
ENTRY_THEME_PHASES = frozenset({"START", "FERMENT", "ACCELERATION"})


@dataclass(frozen=True)
class Course49Market:
    asof: pd.Timestamp
    score: float
    regime: str
    phase: str
    score_change_3d: float
    entry_allowed: bool
    advance_percentile: float
    limit_strength_percentile: float
    premium_percentile: float
    streak_percentile: float
    seal_quality_percentile: float = 0.0
    data_source: str = "price_cross_section"


def infer_market_phase(score: float, score_change_3d: float) -> str:
    if score < 0.25:
        return "ICE"
    if score_change_3d <= -0.12 and score >= 0.50:
        return "DIVERGENCE"
    if score < 0.45 and score_change_3d < 0:
        return "RETREAT"
    if score_change_3d >= 0.10 and score < 0.65:
        return "RECOVERY"
    if score >= 0.82:
        return "CLIMAX"
    if score >= 0.65 and score_change_3d >= 0.03:
        return "ACCELERATION"
    if score >= 0.50:
        return "FERMENT"
    return "NORMAL"


def infer_theme_phase(
    current_limit_count: int,
    previous_limit_count: int,
    recent_limit_peak: int,
    breadth: float,
    volume_ratio: float,
) -> str:
    if current_limit_count < 4:
        return "RETREAT"
    if previous_limit_count < 2:
        return "START"
    if (
        current_limit_count >= max(8, recent_limit_peak)
        and breadth >= 0.75
        and volume_ratio >= 1.40
    ):
        return "CLIMAX"
    if current_limit_count > previous_limit_count and breadth >= 0.60:
        return "ACCELERATION"
    if current_limit_count < previous_limit_count:
        return "DIVERGENCE"
    return "FERMENT"


def _percentile_of_latest(values: pd.Series, window: int = 60) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna().tail(window)
    if clean.empty:
        return 0.0
    return float(clean.rank(pct=True).iloc[-1])


def _cross_section_percentile(values: pd.Series) -> pd.Series:
    if values.empty:
        return values
    return values.rank(method="average", pct=True).fillna(0.0)


def _daily_return(frame: pd.DataFrame, days: int) -> float:
    close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
    if len(close) <= days or close.iloc[-days - 1] <= 0:
        return float("nan")
    return float(close.iloc[-1] / close.iloc[-days - 1] - 1.0)


def _is_limit_return(value: float, ratio: float, side: str = "UP") -> bool:
    if not np.isfinite(value):
        return False
    return value >= ratio - 0.001 if side == "UP" else value <= -ratio + 0.001


def _consecutive_limit_ups(frame: pd.DataFrame, code: str, name: str) -> int:
    close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
    returns = close.pct_change(fill_method=None)
    ratio = price_limit_ratio(code, name)
    streak = 0
    for value in reversed(returns.tolist()):
        if _is_limit_return(float(value), ratio):
            streak += 1
        else:
            break
    return streak


def _score_market_at(
    series: tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series],
    trim: int = 0,
) -> tuple[float, tuple[float, float, float, float, float]]:
    factors = tuple(
        _percentile_of_latest(values.iloc[:-trim] if trim and len(values) > trim else values)
        for values in series
    )
    score = sum(value * 0.20 for value in factors)
    return float(score), factors


def build_course49_market_matrix(
    raw_bars: dict[str, pd.DataFrame],
    names: dict[str, str],
    market_activity: pd.DataFrame | None = None,
) -> pd.DataFrame:
    codes = sorted(raw_bars)
    if not codes:
        return pd.DataFrame()
    close = pd.concat(
        {code: pd.to_numeric(raw_bars[code].get("Close"), errors="coerce") for code in codes},
        axis=1,
    ).sort_index()
    returns = close.pct_change(fill_method=None)
    thresholds = pd.Series(
        {code: price_limit_ratio(code, names.get(code, "")) - 0.001 for code in codes}
    )
    up_table = returns.ge(thresholds, axis="columns")
    down_table = returns.le(-thresholds, axis="columns")
    valid_count = returns.notna().sum(axis=1).clip(lower=1)
    advance = (returns > 0).sum(axis=1) / valid_count
    limit_strength = (up_table.sum(axis=1) - down_table.sum(axis=1)) / valid_count
    premium = returns.where(up_table.shift(1).fillna(False)).median(axis=1)
    running = np.zeros(len(codes), dtype=float)
    streak_values: list[float] = []
    for values in up_table.to_numpy(dtype=bool):
        running = np.where(values, running + 1.0, 0.0)
        streak_values.append(float(running.max(initial=0.0)))
    streak = pd.Series(streak_values, index=returns.index)
    seal_quality = ((limit_strength + 1.0) / 2.0).clip(0.0, 1.0)
    data_source = "price_cross_section"

    if market_activity is not None and not market_activity.empty:
        activity = market_activity.copy()
        activity.index = pd.DatetimeIndex(activity.index)
        if activity.index.tz is not None:
            activity.index = activity.index.tz_localize(None)
        activity = activity.sort_index().loc[: close.index.max()]
        if len(activity) >= 20:
            breadth_total = (
                pd.to_numeric(activity.get("advance_count"), errors="coerce")
                + pd.to_numeric(activity.get("decline_count"), errors="coerce")
            ).replace(0, np.nan)
            advance = pd.to_numeric(activity.get("advance_count"), errors="coerce") / breadth_total
            limit_total = (
                pd.to_numeric(activity.get("limit_up"), errors="coerce")
                + pd.to_numeric(activity.get("limit_down"), errors="coerce")
            ).replace(0, np.nan)
            limit_strength = (
                pd.to_numeric(activity.get("limit_up"), errors="coerce")
                - pd.to_numeric(activity.get("limit_down"), errors="coerce")
            ) / limit_total
            streak = pd.to_numeric(activity.get("max_streak"), errors="coerce")
            seal_quality = pd.concat(
                [
                    pd.to_numeric(activity.get("reseal_rate"), errors="coerce"),
                    pd.to_numeric(activity.get("seal_fund_success_ratio"), errors="coerce"),
                ],
                axis=1,
            ).mean(axis=1)
            premium = premium.reindex(activity.index)
            data_source = "tdx_market_activity"

    factor_series = (advance, limit_strength, premium, streak, seal_quality)
    rows: list[dict[str, Any]] = []
    for timestamp in advance.index:
        visible = tuple(series.loc[:timestamp] for series in factor_series)
        score, factors = _score_market_at(visible)
        prior_score, _ = _score_market_at(visible, trim=3)
        change = score - prior_score
        phase = infer_market_phase(score, change)
        regime = "STRONG" if score >= 0.65 else "NORMAL" if score >= 0.40 else "WEAK"
        rows.append(
            {
                "timestamp": pd.Timestamp(timestamp),
                "score": score,
                "regime": regime,
                "phase": phase,
                "score_change_3d": change,
                "entry_allowed": score >= 0.55 and phase in ENTRY_MARKET_PHASES,
                "advance_percentile": factors[0],
                "limit_strength_percentile": factors[1],
                "premium_percentile": factors[2],
                "streak_percentile": factors[3],
                "seal_quality_percentile": factors[4],
                "data_source": data_source,
            }
        )
    return pd.DataFrame(rows).set_index("timestamp") if rows else pd.DataFrame()


def course49_market_from_matrix(
    matrix: pd.DataFrame,
    asof: Any,
) -> Course49Market | None:
    if matrix.empty:
        return None
    visible = matrix.loc[:pd.Timestamp(asof)]
    if visible.empty:
        return None
    timestamp = pd.Timestamp(visible.index[-1])
    row = visible.iloc[-1]
    return Course49Market(
        timestamp,
        float(row["score"]),
        str(row["regime"]),
        str(row["phase"]),
        float(row["score_change_3d"]),
        bool(row["entry_allowed"]),
        float(row["advance_percentile"]),
        float(row["limit_strength_percentile"]),
        float(row["premium_percentile"]),
        float(row["streak_percentile"]),
        float(row["seal_quality_percentile"]),
        str(row["data_source"]),
    )


class Course49Strategy:
    metadata = StrategyMetadata(
        strategy_id="course49_v1",
        version="1.2.0",
        name="49课题材龙头",
        description="市场生态、题材周期、涨停行为、龙头角色与龙虎榜资金结构的日线策略",
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

    def analyze_market(
        self,
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        market_activity: pd.DataFrame | None = None,
    ) -> Course49Market:
        returns: dict[str, pd.Series] = {}
        up_flags: dict[str, pd.Series] = {}
        down_flags: dict[str, pd.Series] = {}
        for code, frame in raw_bars.items():
            close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
            if len(close) < 20:
                continue
            item_return = close.pct_change(fill_method=None)
            ratio = price_limit_ratio(code, names.get(code, ""))
            returns[code] = item_return
            up_flags[code] = item_return >= ratio - 0.001
            down_flags[code] = item_return <= -ratio + 0.001
        if not returns:
            raise ValueError("At least 20 daily bars are required for 49-course market analysis")

        return_table = pd.DataFrame(returns).sort_index()
        up_table = pd.DataFrame(up_flags).reindex(return_table.index).fillna(False)
        down_table = pd.DataFrame(down_flags).reindex(return_table.index).fillna(False)
        valid_count = return_table.notna().sum(axis=1).clip(lower=1)
        advance = (return_table > 0).sum(axis=1) / valid_count
        limit_strength = (up_table.sum(axis=1) - down_table.sum(axis=1)) / valid_count
        premium_values: list[float] = []
        streak_values: list[float] = []
        running = pd.Series(0, index=return_table.columns, dtype=float)
        for offset, timestamp in enumerate(return_table.index):
            if offset == 0:
                premium_values.append(np.nan)
            else:
                prior_up = up_table.iloc[offset - 1]
                day_returns = return_table.iloc[offset][prior_up]
                premium_values.append(float(day_returns.median()) if not day_returns.empty else np.nan)
            current_up = up_table.loc[timestamp]
            running = (running + 1).where(current_up, 0)
            streak_values.append(float(running.max()))
        premium = pd.Series(premium_values, index=return_table.index)
        streak = pd.Series(streak_values, index=return_table.index)
        seal_quality = ((limit_strength + 1.0) / 2.0).clip(0.0, 1.0)
        data_source = "price_cross_section"
        asof = pd.Timestamp(return_table.index[-1])
        if market_activity is not None and not market_activity.empty:
            activity = market_activity.copy()
            activity.index = pd.DatetimeIndex(activity.index)
            if activity.index.tz is not None:
                activity.index = activity.index.tz_localize(None)
            activity = activity.sort_index().loc[:asof]
            if len(activity) >= 20:
                breadth_total = (
                    pd.to_numeric(activity.get("advance_count"), errors="coerce")
                    + pd.to_numeric(activity.get("decline_count"), errors="coerce")
                ).replace(0, np.nan)
                advance = pd.to_numeric(activity.get("advance_count"), errors="coerce") / breadth_total
                limit_total = (
                    pd.to_numeric(activity.get("limit_up"), errors="coerce")
                    + pd.to_numeric(activity.get("limit_down"), errors="coerce")
                ).replace(0, np.nan)
                limit_strength = (
                    pd.to_numeric(activity.get("limit_up"), errors="coerce")
                    - pd.to_numeric(activity.get("limit_down"), errors="coerce")
                ) / limit_total
                streak = pd.to_numeric(activity.get("max_streak"), errors="coerce")
                reseal = pd.to_numeric(activity.get("reseal_rate"), errors="coerce")
                fund_success = pd.to_numeric(
                    activity.get("seal_fund_success_ratio"), errors="coerce"
                )
                seal_quality = pd.concat([reseal, fund_success], axis=1).mean(axis=1)
                premium = premium.reindex(activity.index)
                data_source = "tdx_market_activity"
        factor_series = (advance, limit_strength, premium, streak, seal_quality)
        score, factors = _score_market_at(factor_series)
        prior_score, _ = _score_market_at(factor_series, trim=3)
        change = score - prior_score
        phase = infer_market_phase(score, change)
        regime = "STRONG" if score >= 0.65 else "NORMAL" if score >= 0.40 else "WEAK"
        entry_allowed = score >= 0.55 and phase in ENTRY_MARKET_PHASES
        return Course49Market(
            asof,
            score,
            regime,
            phase,
            change,
            entry_allowed,
            *factors,
            data_source,
        )

    def rank_sectors(
        self,
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        sector_members: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        market_returns = [_daily_return(frame, 5) for frame in front_bars.values()]
        market_return = float(np.nanmedian(market_returns)) if market_returns else 0.0
        rows: list[dict[str, Any]] = []
        for sector_code, metadata in sector_members.items():
            member_rows = []
            limit_history: dict[str, pd.Series] = {}
            for code in metadata.get("members", []):
                front = front_bars.get(code)
                raw = raw_bars.get(code)
                if front is None or raw is None or len(front) < 21 or len(raw) < 2:
                    continue
                close = pd.to_numeric(front["Close"], errors="coerce").dropna()
                raw_close = pd.to_numeric(raw["Close"], errors="coerce").dropna()
                volume = pd.to_numeric(front.get("Volume"), errors="coerce").dropna()
                if len(close) < 21 or len(raw_close) < 2 or len(volume) < 20:
                    continue
                ratio = price_limit_ratio(code, names.get(code, ""))
                returns = raw_close.pct_change(fill_method=None)
                limit_history[code] = returns >= ratio - 0.001
                daily_return = float(raw_close.iloc[-1] / raw_close.iloc[-2] - 1.0)
                member_rows.append(
                    {
                        "code": code,
                        "limit_up": _is_limit_return(daily_return, ratio),
                        "return_5d": _daily_return(front, 5),
                        "above_ma20": bool(close.iloc[-1] > close.tail(20).mean()),
                        "volume_ratio": float(volume.iloc[-1] / volume.tail(20).mean())
                        if volume.tail(20).mean() > 0
                        else 0.0,
                    }
                )
            if len(member_rows) < 3:
                continue
            table = pd.DataFrame(member_rows)
            limit_count = int(table["limit_up"].sum())
            counts = pd.DataFrame(limit_history).fillna(False).sum(axis=1) if limit_history else pd.Series(dtype=float)
            previous_count = int(counts.iloc[-2]) if len(counts) >= 2 else 0
            recent_peak = int(counts.tail(5).max()) if not counts.empty else limit_count
            breadth = float(table["above_ma20"].mean())
            volume_ratio = float(table["volume_ratio"].median())
            theme_phase = infer_theme_phase(
                limit_count,
                previous_count,
                recent_peak,
                breadth,
                volume_ratio,
            )
            rows.append(
                {
                    "sector_code": sector_code,
                    "sector_name": str(metadata.get("name", sector_code)),
                    "limit_count": limit_count,
                    "previous_limit_count": previous_count,
                    "recent_limit_peak": recent_peak,
                    "limit_ratio": float(table["limit_up"].mean()),
                    "relative_return_5d": float(table["return_5d"].median() - market_return),
                    "breadth": breadth,
                    "volume_ratio": volume_ratio,
                    "theme_phase": theme_phase,
                    "valid_members": len(table),
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
        table = table.sort_values(["score", "limit_count", "sector_code"], ascending=[False, False, True])
        return table.head(3).to_dict("records")

    def rank_leaders(
        self,
        sectors: list[dict[str, Any]],
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        sector_members: dict[str, dict[str, Any]],
        limit_snapshot: dict[str, dict[str, Any]] | None = None,
        lhb_history: dict[str, dict[str, LhbFeatures]] | None = None,
    ) -> list[dict[str, Any]]:
        limit_snapshot = limit_snapshot or {}
        lhb_history = lhb_history or {}
        leaders: list[dict[str, Any]] = []
        for sector in sectors:
            rows: list[dict[str, Any]] = []
            for code in sector_members.get(str(sector["sector_code"]), {}).get("members", []):
                front = front_bars.get(code)
                raw = raw_bars.get(code)
                if front is None or raw is None or len(raw) < 21:
                    continue
                streak = _consecutive_limit_ups(raw, code, names.get(code, ""))
                if streak == 0:
                    continue
                close = pd.to_numeric(front["Close"], errors="coerce").dropna()
                volume = pd.to_numeric(front.get("Volume"), errors="coerce").reindex(close.index).fillna(0.0)
                history_returns = pd.to_numeric(raw["Close"], errors="coerce").pct_change(fill_method=None)
                ratio = price_limit_ratio(code, names.get(code, ""))
                historical_up = history_returns >= ratio - 0.001
                next_returns = history_returns.shift(-1)[historical_up]
                positive_premium = float((next_returns > 0).mean()) if len(next_returns.dropna()) else 0.5
                first_time = str(limit_snapshot.get(code, {}).get("FirstTimeZT", ""))
                digits = "".join(character for character in first_time if character.isdigit())[-6:]
                first_limit_available = len(digits) == 6
                if first_limit_available:
                    seconds = int(digits[:2]) * 3600 + int(digits[2:4]) * 60 + int(digits[4:])
                    time_score = max(0.0, min(1.0, (15 * 3600 - seconds) / (5.5 * 3600)))
                else:
                    time_score = 0.5
                asof = pd.Timestamp(raw.index[-1])
                capital = latest_lhb_features(lhb_history, code, asof)
                behavior = latest_limit_features(lhb_history, code, asof)
                if behavior:
                    time_score = behavior.first_limit_score
                    first_limit_available = bool(behavior.first_limit_time)
                rows.append(
                    {
                        "code": code,
                        "name": names.get(code, code),
                        "streak": streak,
                        "first_limit_score": time_score,
                        "first_limit_available": first_limit_available,
                        "return_5d": _daily_return(front, 5),
                        "turnover": float((close * volume).tail(20).mean()),
                        "historical_premium": positive_premium,
                        "capital_score": capital.score if capital else 0.5,
                        "capital_risk": capital.risk if capital else "",
                        "lhb": capital.as_dict() if capital else {"listed": False},
                        "board_quality_score": behavior.board_quality_score if behavior else 0.5,
                        "board_risk": behavior.board_risk if behavior else "",
                        "limit_behavior": behavior.behavior_dict() if behavior else {"limit_event": False},
                        "price": float(pd.to_numeric(raw["Close"], errors="coerce").dropna().iloc[-1]),
                    }
                )
            if not rows:
                continue
            table = pd.DataFrame(rows)
            table["turnover_rank"] = _cross_section_percentile(table["turnover"])
            table["leader_score"] = (
                _cross_section_percentile(table["streak"]) * 0.22
                + table["board_quality_score"] * 0.20
                + _cross_section_percentile(table["return_5d"]) * 0.13
                + table["turnover_rank"] * 0.15
                + table["historical_premium"] * 0.10
                + table["capital_score"] * 0.15
                + table["first_limit_score"] * 0.05
            )
            table = table.sort_values(["leader_score", "streak", "code"], ascending=[False, False, True])
            max_streak = int(table["streak"].max())
            for rank, (_, row) in enumerate(table.head(2).iterrows(), start=1):
                if rank == 1 and int(row["streak"]) == max_streak and max_streak >= 2:
                    role = "SPACE_LEADER"
                elif rank == 1:
                    role = "THEME_LEADER"
                elif float(row["turnover_rank"]) >= 0.75:
                    role = "CAPACITY_CORE"
                else:
                    role = "CHALLENGER"
                leader = row.to_dict()
                leader.update(
                    {
                        "leader_rank": rank,
                        "role": role,
                        "sector_code": sector["sector_code"],
                        "sector_name": sector["sector_name"],
                        "sector_score": sector["score"],
                        "sector_limit_count": sector["limit_count"],
                        "theme_phase": sector["theme_phase"],
                    }
                )
                leaders.append(leader)
        best_by_code: dict[str, dict[str, Any]] = {}
        for leader in leaders:
            existing = best_by_code.get(str(leader["code"]))
            score = (float(leader["sector_score"]), float(leader["leader_score"]), -int(leader["leader_rank"]))
            if existing is None:
                best_by_code[str(leader["code"])] = leader
                continue
            existing_score = (
                float(existing["sector_score"]),
                float(existing["leader_score"]),
                -int(existing["leader_rank"]),
            )
            if score > existing_score:
                best_by_code[str(leader["code"])] = leader
        return sorted(
            best_by_code.values(),
            key=lambda item: (
                -float(item["sector_score"]),
                int(item["leader_rank"]),
                -float(item["leader_score"]),
                str(item["code"]),
            ),
        )

    def scan(
        self,
        *,
        run_id: str,
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        sector_members: dict[str, dict[str, Any]],
        positions: list[dict[str, Any]],
        limit_snapshot: dict[str, dict[str, Any]] | None = None,
        lhb_history: dict[str, dict[str, LhbFeatures]] | None = None,
        market_activity: pd.DataFrame | None = None,
        market_matrix: pd.DataFrame | None = None,
    ) -> StrategyScanResult:
        lhb_history = lhb_history or {}
        asof = max(pd.Timestamp(frame.index[-1]) for frame in raw_bars.values() if not frame.empty)
        market = (
            course49_market_from_matrix(market_matrix, asof)
            if market_matrix is not None
            else None
        ) or self.analyze_market(raw_bars, names, market_activity)
        sectors = self.rank_sectors(front_bars, raw_bars, names, sector_members)
        leaders = self.rank_leaders(
            sectors,
            front_bars,
            raw_bars,
            names,
            sector_members,
            limit_snapshot=limit_snapshot,
            lhb_history=lhb_history,
        )
        signals: list[PlatformSignal] = []
        position_by_code = {item["code"]: item for item in positions}
        sector_by_code = {str(item["sector_code"]): item for item in sectors}
        leader_by_code = {str(item["code"]): item for item in leaders if int(item["leader_rank"]) == 1}
        generated_at = _shanghai_time(market.asof.replace(hour=18))
        next_day = _shanghai_time((market.asof + pd.offsets.BDay(1)).replace(hour=9, minute=25))

        for code, position in position_by_code.items():
            frame = raw_bars.get(code)
            if frame is None or len(frame) < 20:
                continue
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
            price = float(close.iloc[-1])
            evidence = json_evidence(position)
            sector_code = str(evidence.get("sector_code", ""))
            capital = latest_lhb_features(lhb_history, code, market.asof)
            behavior = latest_limit_features(lhb_history, code, market.asof)
            reason = ""
            if price <= float(position["stop_price"]):
                reason = "FIXED_STOP"
            elif capital and capital.risk:
                reason = "CAPITAL_DISTRIBUTION"
            elif market.phase in {"ICE", "RETREAT"} or market.regime == "WEAK":
                reason = "MARKET_RETREAT"
            elif sector_code and sector_code not in sector_by_code:
                reason = "SECTOR_FADED"
            elif code not in leader_by_code and price < float(close.tail(5).mean()):
                reason = "LEADER_LOST"
            elif price < float(close.tail(5).mean()):
                reason = "BELOW_MA5"
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
                            "market_phase": market.phase,
                            "market_data_source": market.data_source,
                            "sector_code": sector_code,
                            "lhb": capital.as_dict() if capital else {"listed": False},
                            "limit_behavior": behavior.behavior_dict() if behavior else {"limit_event": False},
                        },
                    )
                )

        if market.entry_allowed:
            for leader in leaders:
                code = str(leader["code"])
                if (
                    code in position_by_code
                    or int(leader["streak"]) < 2
                    or int(leader["leader_rank"]) != 1
                    or str(leader["theme_phase"]) not in ENTRY_THEME_PHASES
                    or str(leader["capital_risk"])
                    or str(leader["board_risk"]) == "LATE_WEAK_SEAL"
                ):
                    continue
                price = float(leader["price"])
                lhb = leader["lhb"] if isinstance(leader["lhb"], dict) else {"listed": False}
                confirmations = tuple(str(item) for item in lhb.get("confirmations", []))
                limit_behavior = (
                    leader["limit_behavior"]
                    if isinstance(leader["limit_behavior"], dict)
                    else {"limit_event": False}
                )
                board_confirmations = tuple(
                    str(item) for item in limit_behavior.get("confirmations", [])
                )
                if int(leader["streak"]) == 2 and confirmations:
                    setup = "SECOND_BOARD_CAPITAL_CONFIRMED"
                elif int(leader["streak"]) == 2 and float(leader["board_quality_score"]) >= 0.70:
                    setup = "SECOND_BOARD_QUALITY_CONFIRMED"
                elif int(leader["streak"]) == 2:
                    setup = "SECOND_BOARD_LEADER"
                else:
                    setup = "HIGH_BOARD_RELAY"
                strength = max(
                    0.0,
                    min(
                        1.0,
                        float(leader["leader_score"]) * 0.70
                        + float(leader["sector_score"]) * 0.20
                        + market.score * 0.05
                        + float(leader["board_quality_score"]) * 0.05,
                    ),
                )
                target_weight = _target_weight(
                    market.phase,
                    str(leader["theme_phase"]),
                    bool(confirmations),
                    float(leader["board_quality_score"]),
                )
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
                        stop_price=price * 0.95,
                        status=SignalStatus.PROPOSED,
                        reason_codes=(
                            setup,
                            "TOP_THEME",
                            str(leader["role"]),
                            *confirmations,
                            *board_confirmations,
                        ),
                        evidence={
                            "price": price,
                            "market_score": market.score,
                            "market_regime": market.regime,
                            "market_phase": market.phase,
                            "market_score_change_3d": market.score_change_3d,
                            "market_data_source": market.data_source,
                            "market_seal_quality_percentile": market.seal_quality_percentile,
                            "sector_code": leader["sector_code"],
                            "sector_name": leader["sector_name"],
                            "sector_score": leader["sector_score"],
                            "sector_limit_count": leader["sector_limit_count"],
                            "theme_phase": leader["theme_phase"],
                            "limit_streak": int(leader["streak"]),
                            "leader_rank": int(leader["leader_rank"]),
                            "role": leader["role"],
                            "leader_score": leader["leader_score"],
                            "setup": setup,
                            "lhb": lhb,
                            "limit_behavior": limit_behavior,
                        },
                    )
                )

        return StrategyScanResult(
            strategy=self.metadata,
            signals=tuple(signals),
            candidates=tuple(leaders),
            state={
                "market_regime": market.regime,
                "market_phase": market.phase,
                "market_score": market.score,
                "market_score_change_3d": market.score_change_3d,
                "entry_allowed": market.entry_allowed,
                "advance_percentile": market.advance_percentile,
                "limit_strength_percentile": market.limit_strength_percentile,
                "premium_percentile": market.premium_percentile,
                "streak_percentile": market.streak_percentile,
                "seal_quality_percentile": market.seal_quality_percentile,
                "market_data_source": market.data_source,
                "strong_sectors": sectors,
                "lhb_event_count": sum(
                    int(feature.listed)
                    for events in lhb_history.values()
                    for feature in events.values()
                ),
                "limit_behavior_event_count": sum(
                    int(feature.limit_event)
                    for events in lhb_history.values()
                    for feature in events.values()
                ),
            },
        )


def _target_weight(
    market_phase: str,
    theme_phase: str,
    capital_confirmed: bool,
    board_quality_score: float,
) -> float:
    market_weights = {"RECOVERY": 0.25, "FERMENT": 0.35, "ACCELERATION": 0.30}
    theme_weights = {"START": 0.25, "FERMENT": 0.35, "ACCELERATION": 0.30}
    weight = min(market_weights.get(market_phase, 0.20), theme_weights.get(theme_phase, 0.20))
    if capital_confirmed:
        weight += 0.05
    if board_quality_score >= 0.70:
        weight += 0.05
    elif board_quality_score < 0.40:
        weight -= 0.05
    return max(0.10, min(0.40, weight))


def _shanghai_time(value: Any) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(SHANGHAI_TZ)
    else:
        timestamp = timestamp.tz_convert(SHANGHAI_TZ)
    return timestamp.to_pydatetime()


def json_evidence(position: dict[str, Any]) -> dict[str, Any]:
    import json

    evidence = position.get("evidence", {})
    if isinstance(evidence, str):
        try:
            return json.loads(evidence)
        except json.JSONDecodeError:
            return {}
    return evidence if isinstance(evidence, dict) else {}
