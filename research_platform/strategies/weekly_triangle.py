from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research_platform.models import (
    DataRequirement,
    PlatformSignal,
    RuntimeAdapter,
    SignalStatus,
    StrategyMetadata,
    StrategyScanResult,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class WeeklyTriangleParameters:
    triangle_weeks: int = 10
    minimum_triangle_weeks: int = 5
    moving_average_periods: tuple[int, ...] = (5, 10, 20, 30)
    max_ma_dispersion: float = 0.12
    max_width_ratio: float = 0.72
    minimum_start_width: float = 0.08
    minimum_convergence_per_week: float = 0.003
    maximum_upper_slope: float = 0.006
    minimum_lower_slope: float = -0.003
    touch_tolerance: float = 0.03
    minimum_touches: int = 2
    maximum_apex_weeks: float = 12.0
    breakout_buffer: float = 0.015
    breakout_volume_ratio: float = 1.20
    market_ma_period: int = 30
    require_market_filter: bool = True
    fixed_stop_ratio: float = 0.08
    trailing_stop_ratio: float = 0.12
    maximum_holding_days: int = 20
    target_weight: float = 0.20
    max_candidates: int = 100
    max_entry_signals: int = 20
    emit_live_entry_signals: bool = False


class WeeklyTriangleStrategy:
    metadata = StrategyMetadata(
        strategy_id="weekly_triangle_v1",
        version="1.2.0",
        name="周线均线聚合收敛三角形",
        description="识别周线均线聚合与收敛三角形；历史入场规则已否决，实时仅输出观察候选",
        frequency="1w",
        requires_approval=True,
        lifecycle="HISTORICAL_REJECTED",
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement(
                "bars",
                "1d",
                "front",
                180,
                True,
                ("Open", "High", "Low", "Close", "Volume", "Amount"),
            ),
            DataRequirement(
                "bars",
                "1d",
                "none",
                180,
                True,
                ("Open", "High", "Low", "Close", "Volume", "Amount"),
            ),
        ),
    )

    def __init__(self, parameters: WeeklyTriangleParameters | None = None):
        self.parameters = parameters or WeeklyTriangleParameters()

    def _analyze_weekly(self, weekly: pd.DataFrame) -> dict[str, Any] | None:
        return analyze_weekly_triangle(weekly, self.parameters)

    def _eligible_code(self, code: str, name: str) -> bool:
        return True

    def _parameter_state(self) -> dict[str, Any]:
        return {
            "triangle_weeks": self.parameters.triangle_weeks,
            "minimum_triangle_weeks": self.parameters.minimum_triangle_weeks,
            "moving_average_periods": list(self.parameters.moving_average_periods),
            "max_ma_dispersion": self.parameters.max_ma_dispersion,
            "max_width_ratio": self.parameters.max_width_ratio,
            "breakout_buffer": self.parameters.breakout_buffer,
            "breakout_volume_ratio": self.parameters.breakout_volume_ratio,
            "market_ma_period": self.parameters.market_ma_period,
            "require_market_filter": self.parameters.require_market_filter,
            "maximum_holding_days": self.parameters.maximum_holding_days,
            "emit_live_entry_signals": self.parameters.emit_live_entry_signals,
        }

    def _entry_reason_codes(self) -> tuple[str, ...]:
        return (
            "WEEKLY_TRIANGLE_BREAKOUT",
            "WEEKLY_MA_CONVERGENCE",
            "WEEKLY_VOLUME_CONFIRMATION",
        )

    def _cache_signature(self) -> str:
        return f"{self.metadata.strategy_id}:{self.metadata.version}:{self.parameters!r}"

    def prepare_backtest_data(
        self,
        *,
        front_bars: dict[str, pd.DataFrame],
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "weekly_front": {
                code: weekly
                for code, frame in front_bars.items()
                if not (weekly := resample_weekly_bars(frame)).empty
            }
        }

    def scan(
        self,
        *,
        run_id: str,
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str] | None = None,
        positions: list[dict[str, Any]] | None = None,
        runtime_state: dict[str, dict[str, Any]] | None = None,
        prepared_backtest_data: dict[str, Any] | None = None,
        backtest_mode: bool = False,
        index_bars: pd.DataFrame | None = None,
        asof: pd.Timestamp | None = None,
        **_: Any,
    ) -> StrategyScanResult:
        names = names or {}
        positions = positions or []
        state_by_code = {
            str(code): dict(value)
            for code, value in (runtime_state or {}).items()
            if isinstance(value, dict)
        }
        position_by_code = {str(item["code"]): item for item in positions}
        prepared_weekly = (prepared_backtest_data or {}).get("weekly_front")
        if not isinstance(prepared_weekly, dict):
            prepared_weekly = {}
        latest_day = _latest_day(raw_bars, asof)
        market = evaluate_weekly_market(
            index_bars,
            latest_day,
            self.parameters.market_ma_period,
        )
        entry_signals_enabled = bool(
            backtest_mode or self.parameters.emit_live_entry_signals
        )
        candidates: list[dict[str, Any]] = []
        exits: list[PlatformSignal] = []

        for code, position in position_by_code.items():
            signal = self._exit_signal(
                run_id,
                code,
                position,
                front_bars.get(code),
                raw_bars.get(code),
                latest_day,
            )
            if signal is not None:
                exits.append(signal)

        completed_period = _completed_period(latest_day)
        cache_signature = self._cache_signature()
        cached_scan = state_by_code.get("__scan__", {})
        cached_period = str(cached_scan.get("completed_period") or "")
        cached_signature = str(cached_scan.get("cache_signature") or "")
        if (
            completed_period
            and cached_period == completed_period
            and cached_signature == cache_signature
        ):
            for code, item in state_by_code.items():
                candidate = item.get("candidate")
                frame = front_bars.get(code)
                if (
                    code == "__scan__"
                    or code in position_by_code
                    or not self._eligible_code(code, names.get(code, ""))
                    or not isinstance(candidate, dict)
                    or frame is None
                    or latest_day is None
                    or not _is_current(frame, latest_day)
                ):
                    continue
                cached_candidate = dict(candidate)
                cached_candidate["observation_only"] = not entry_signals_enabled
                candidates.append(cached_candidate)
        else:
            preserved_entries = {
                code: {"last_entry_week": str(item["last_entry_week"])}
                for code, item in state_by_code.items()
                if code != "__scan__" and item.get("last_entry_week")
            }
            state_by_code = {
                "__scan__": {
                    "completed_period": completed_period,
                    "cache_signature": cache_signature,
                },
                **preserved_entries,
            }
            for code, frame in front_bars.items():
                if code in position_by_code:
                    continue
                if not self._eligible_code(code, names.get(code, "")):
                    continue
                if latest_day is not None and not _is_current(frame, latest_day):
                    continue
                cached_weekly = prepared_weekly.get(code)
                weekly = (
                    completed_precomputed_weekly_bars(cached_weekly, latest_day)
                    if isinstance(cached_weekly, pd.DataFrame)
                    else completed_weekly_bars(
                        frame.tail(int(getattr(self.parameters, "daily_lookback", 180))),
                        latest_day,
                    )
                )
                analysis = self._analyze_weekly(weekly)
                if analysis is None:
                    continue
                candidate = {
                    "code": code,
                    "name": names.get(code, ""),
                    **analysis,
                    "market_above_ma": market["above_ma"],
                    "entry_allowed": (
                        not self.parameters.require_market_filter
                        or bool(market["above_ma"])
                    ),
                    "observation_only": not entry_signals_enabled,
                }
                candidates.append(candidate)
                state_by_code[code] = {
                    **state_by_code.get(code, {}),
                    "candidate": candidate,
                }

        candidates.sort(key=lambda item: (-float(item["score"]), str(item["code"])))
        entries: list[PlatformSignal] = []
        if entry_signals_enabled:
            for candidate in candidates:
                if not bool(candidate["breakout"]) or not bool(candidate["entry_allowed"]):
                    continue
                code = str(candidate["code"])
                breakout_week = str(candidate["asof"])
                if state_by_code.get(code, {}).get("last_entry_week") == breakout_week:
                    continue
                raw = raw_bars.get(code)
                if raw is None or raw.empty:
                    continue
                raw_close = pd.to_numeric(raw.get("Close"), errors="coerce").dropna()
                if raw_close.empty:
                    continue
                raw_price = float(raw_close.iloc[-1])
                front_price = float(candidate["close"])
                adjustment_ratio = raw_price / front_price if front_price > 0 else 1.0
                technical_stop = max(
                    float(candidate["lower_boundary"]) * 0.98,
                    float(candidate["ma20"]) * 0.97,
                ) * adjustment_ratio
                stop_price = min(
                    raw_price * 0.98,
                    max(
                        raw_price * (1.0 - self.parameters.fixed_stop_ratio),
                        technical_stop,
                    ),
                )
                entries.append(
                    self._entry_signal(run_id, code, candidate, raw_price, stop_price)
                )
                state_by_code[code] = {
                    **state_by_code.get(code, {}),
                    "last_entry_week": breakout_week,
                }
                if len(entries) >= self.parameters.max_entry_signals:
                    break

        visible_candidates = candidates[: self.parameters.max_candidates]
        completed_weeks = [str(item["asof"]) for item in candidates]
        return StrategyScanResult(
            strategy=self.metadata,
            signals=tuple([*exits, *entries]),
            candidates=tuple(visible_candidates),
            state={
                "asof": latest_day.date().isoformat() if latest_day is not None else None,
                "completed_week": max(completed_weeks) if completed_weeks else None,
                "candidate_count": len(candidates),
                "setup_count": sum(not bool(item["breakout"]) for item in candidates),
                "breakout_count": sum(bool(item["breakout"]) for item in candidates),
                "qualified_breakout_count": sum(
                    bool(item["breakout"]) and bool(item["entry_allowed"])
                    for item in candidates
                ),
                "entry_ready_count": sum(
                    entry_signals_enabled
                    and bool(item["breakout"])
                    and bool(item["entry_allowed"])
                    for item in candidates
                ),
                "entry_signals_enabled": entry_signals_enabled,
                "market_above_ma": market["above_ma"],
                "market_close": market["close"],
                "market_ma": market["ma"],
                "runtime_state": state_by_code,
                "parameters": self._parameter_state(),
            },
        )

    def _entry_signal(
        self,
        run_id: str,
        code: str,
        candidate: dict[str, Any],
        price: float,
        stop_price: float,
    ) -> PlatformSignal:
        generated, available, valid_until = _signal_times(candidate["asof"], 5)
        evidence = {
            key: value
            for key, value in candidate.items()
            if key not in {"code", "name"}
        }
        evidence["raw_price"] = price
        return PlatformSignal(
            run_id=run_id,
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            generated_at=generated,
            available_at=available,
            code=code,
            side="BUY",
            strength=float(candidate["score"]),
            target_weight=self.parameters.target_weight,
            horizon="weekly-swing",
            valid_until=valid_until,
            stop_price=stop_price,
            status=SignalStatus.PROPOSED,
            reason_codes=self._entry_reason_codes(),
            evidence=evidence,
        )

    def _exit_signal(
        self,
        run_id: str,
        code: str,
        position: dict[str, Any],
        front: pd.DataFrame | None,
        raw: pd.DataFrame | None,
        latest_day: pd.Timestamp | None,
    ) -> PlatformSignal | None:
        if raw is None or raw.empty:
            return None
        close = pd.to_numeric(raw.get("Close"), errors="coerce").dropna()
        if close.empty:
            return None
        price = float(close.iloc[-1])
        signal_day = pd.Timestamp(close.index[-1])
        stop_price = float(position.get("stop_price") or 0.0)
        reason = ""
        holding_days = 0

        if stop_price > 0 and price <= stop_price:
            reason = "FIXED_STOP"
        else:
            entry_time = pd.Timestamp(position.get("entry_time"))
            entry_day = (
                entry_time.tz_localize(None).normalize()
                if entry_time.tzinfo
                else entry_time.normalize()
            )
            raw_days = pd.DatetimeIndex(raw.index)
            if raw_days.tz is not None:
                raw_days = raw_days.tz_localize(None)
            since_entry = pd.to_numeric(
                raw.loc[raw_days.normalize() >= entry_day, "Close"], errors="coerce"
            ).dropna()
            holding_days = int((raw_days.normalize() >= entry_day).sum())
            entry_price = float(position.get("average_price") or price)
            peak = float(since_entry.max()) if not since_entry.empty else price
            if (
                peak >= entry_price * 1.10
                and price <= peak * (1.0 - self.parameters.trailing_stop_ratio)
            ):
                reason = "TRAILING_STOP"

        weekly = (
            completed_weekly_bars(front.tail(180), latest_day)
            if front is not None
            else pd.DataFrame()
        )
        if not reason and len(weekly) >= 20:
            weekly_close = pd.to_numeric(weekly["Close"], errors="coerce")
            ma20 = weekly_close.rolling(20).mean().iloc[-1]
            if np.isfinite(ma20) and float(weekly_close.iloc[-1]) < float(ma20) * 0.98:
                reason = "WEEKLY_MA20_BREAKDOWN"
                signal_day = pd.Timestamp(weekly.index[-1])
        if not reason and holding_days >= self.parameters.maximum_holding_days:
            reason = "WEEKLY_TIME_EXIT"
        if not reason:
            return None

        generated, available, valid_until = _signal_times(signal_day, 5)
        return PlatformSignal(
            run_id=run_id,
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            generated_at=generated,
            available_at=available,
            code=code,
            side="SELL",
            strength=1.0,
            target_weight=0.0,
            horizon="weekly-swing",
            valid_until=valid_until,
            stop_price=None,
            status=SignalStatus.APPROVED,
            reason_codes=(reason,),
            evidence={
                "price": price,
                "position_stop": stop_price,
                "holding_days": holding_days,
            },
        )


def resample_weekly_bars(frame: pd.DataFrame) -> pd.DataFrame:
    required = ("Open", "High", "Low", "Close")
    if frame.empty or any(column not in frame for column in required):
        return pd.DataFrame()
    source = frame.copy().sort_index()
    source.index = pd.DatetimeIndex(source.index)
    if source.index.tz is not None:
        source.index = source.index.tz_localize(None)
    for column in (*required, "Volume", "Amount"):
        if column in source:
            source[column] = pd.to_numeric(source[column], errors="coerce")
    periods = source.index.to_period("W-FRI")
    aggregation: dict[str, str] = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
    }
    if "Volume" in source:
        aggregation["Volume"] = "sum"
    if "Amount" in source:
        aggregation["Amount"] = "sum"
    weekly = source.groupby(periods).agg(aggregation)
    source_end = pd.Series(source.index, index=source.index).groupby(periods).max()
    weekly["WeekEnd"] = pd.DatetimeIndex(weekly.index.end_time).normalize()
    weekly.index = pd.DatetimeIndex(source_end.to_numpy())
    weekly.index.name = frame.index.name
    return weekly.dropna(subset=list(required))


def completed_weekly_bars(
    frame: pd.DataFrame | None,
    asof: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    weekly = resample_weekly_bars(frame)
    if weekly.empty:
        return weekly
    cutoff = pd.Timestamp(asof) if asof is not None else pd.Timestamp(frame.index[-1])
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    return weekly.loc[pd.to_datetime(weekly["WeekEnd"]) <= cutoff.normalize()].copy()


def completed_precomputed_weekly_bars(
    weekly: pd.DataFrame | None,
    asof: pd.Timestamp | None,
) -> pd.DataFrame:
    if weekly is None or weekly.empty or asof is None or "WeekEnd" not in weekly:
        return pd.DataFrame()
    cutoff = pd.Timestamp(asof)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    visible = weekly.loc[
        pd.to_datetime(weekly["WeekEnd"]) <= cutoff.normalize()
    ]
    return visible.tail(180).copy()


def evaluate_weekly_market(
    index_bars: pd.DataFrame | None,
    asof: pd.Timestamp | None,
    ma_period: int = 30,
) -> dict[str, float | bool | None]:
    if index_bars is None or index_bars.empty or asof is None:
        return {"above_ma": False, "close": None, "ma": None}
    weekly = completed_weekly_bars(index_bars.tail(ma_period * 7), asof)
    if len(weekly) < ma_period:
        return {"above_ma": False, "close": None, "ma": None}
    close = pd.to_numeric(weekly["Close"], errors="coerce")
    market_close = float(close.iloc[-1])
    market_ma = float(close.rolling(ma_period).mean().iloc[-1])
    if not np.isfinite(market_close) or not np.isfinite(market_ma):
        return {"above_ma": False, "close": None, "ma": None}
    return {
        "above_ma": market_close >= market_ma,
        "close": market_close,
        "ma": market_ma,
    }


def analyze_weekly_triangle(
    weekly: pd.DataFrame,
    parameters: WeeklyTriangleParameters | None = None,
) -> dict[str, Any] | None:
    params = parameters or WeeklyTriangleParameters()
    minimum = max(max(params.moving_average_periods), params.minimum_triangle_weeks + 1)
    if len(weekly) < minimum:
        return None
    close = pd.to_numeric(weekly.get("Close"), errors="coerce")
    if close.isna().any() or float(close.iloc[-1]) <= 0:
        return None

    moving_averages = {
        period: close.rolling(period).mean()
        for period in params.moving_average_periods
    }
    latest_mas = {
        period: float(values.iloc[-1]) for period, values in moving_averages.items()
    }
    if not all(np.isfinite(value) and value > 0 for value in latest_mas.values()):
        return None
    ma_values = np.asarray(list(latest_mas.values()), dtype=float)
    ma_dispersion = float((ma_values.max() - ma_values.min()) / ma_values.mean())
    ma20 = moving_averages[20]
    trend_ok = (
        len(ma20.dropna()) >= 5
        and float(ma20.iloc[-1]) >= float(ma20.iloc[-5]) * 0.98
        and float(close.iloc[-1])
        >= latest_mas[max(params.moving_average_periods)] * 0.98
    )
    if ma_dispersion > params.max_ma_dispersion or not trend_ok:
        return None

    current_close = float(close.iloc[-1])
    breakout = False
    volume_ratio = 0.0
    geometry: dict[str, float | int] | None = None
    selected_weeks = 0
    for window_weeks in range(
        params.triangle_weeks,
        params.minimum_triangle_weeks - 1,
        -1,
    ):
        window_params = replace(params, triangle_weeks=window_weeks)
        base = weekly.iloc[-window_weeks - 1 : -1]
        breakout_geometry = _triangle_geometry(base, window_params)
        if breakout_geometry is None:
            continue
        upper_now = _line_value(breakout_geometry, "upper", window_weeks)
        upper_previous = _line_value(
            breakout_geometry, "upper", window_weeks - 1
        )
        previous_close = float(close.iloc[-2])
        volume = (
            pd.to_numeric(weekly["Volume"], errors="coerce")
            if "Volume" in weekly
            else pd.Series(0.0, index=weekly.index)
        )
        reference_volume = float(volume.iloc[-6:-1].median()) if len(volume) >= 6 else 0.0
        current_volume = float(volume.iloc[-1]) if len(volume) else 0.0
        volume_ratio = current_volume / reference_volume if reference_volume > 0 else 0.0
        confirmed = (
            current_close > upper_now * (1.0 + params.breakout_buffer)
            and previous_close <= upper_previous * (1.0 + params.breakout_buffer)
            and volume_ratio >= params.breakout_volume_ratio
        )
        if confirmed:
            breakout = True
            geometry = breakout_geometry
            selected_weeks = window_weeks
            break

    if geometry is None:
        for window_weeks in range(
            params.triangle_weeks,
            params.minimum_triangle_weeks - 1,
            -1,
        ):
            window_params = replace(params, triangle_weeks=window_weeks)
            setup = weekly.iloc[-window_weeks:]
            setup_geometry = _triangle_geometry(setup, window_params)
            if setup_geometry is None:
                continue
            lower_now = _line_value(setup_geometry, "lower", window_weeks - 1)
            upper_now = _line_value(setup_geometry, "upper", window_weeks - 1)
            width = max(upper_now - lower_now, 1e-12)
            price_location = (current_close - lower_now) / width
            if not 0.45 <= price_location <= 1.03:
                continue
            geometry = setup_geometry
            selected_weeks = window_weeks
            break
        if geometry is None:
            return None

    projected_offset = selected_weeks if breakout else selected_weeks - 1
    upper_boundary = _line_value(geometry, "upper", projected_offset)
    lower_boundary = _line_value(geometry, "lower", projected_offset)
    ma_score = max(0.0, 1.0 - ma_dispersion / params.max_ma_dispersion)
    convergence_score = max(
        0.0,
        1.0 - float(geometry["width_ratio"]) / params.max_width_ratio,
    )
    touch_score = min(
        1.0,
        (int(geometry["upper_touches"]) + int(geometry["lower_touches"])) / 8.0,
    )
    volume_score = (
        min(1.0, volume_ratio / 2.0)
        if breakout
        else max(0.0, 1.0 - float(geometry["range_contraction"]))
    )
    score = min(
        1.0,
        0.45
        + 0.20 * ma_score
        + 0.15 * convergence_score
        + 0.10 * touch_score
        + 0.10 * volume_score
        + (0.10 if breakout else 0.0),
    )
    return {
        "asof": pd.Timestamp(weekly.index[-1]).date().isoformat(),
        "stage": "BREAKOUT" if breakout else "SETUP",
        "breakout": breakout,
        "triangle_weeks": selected_weeks,
        "score": float(score),
        "close": current_close,
        "upper_boundary": float(upper_boundary),
        "lower_boundary": float(lower_boundary),
        "price_location": float(
            (current_close - lower_boundary)
            / max(upper_boundary - lower_boundary, 1e-12)
        ),
        "width_ratio": float(geometry["width_ratio"]),
        "range_contraction": float(geometry["range_contraction"]),
        "upper_slope_pct": float(geometry["upper_slope_pct"]),
        "lower_slope_pct": float(geometry["lower_slope_pct"]),
        "upper_touches": int(geometry["upper_touches"]),
        "lower_touches": int(geometry["lower_touches"]),
        "apex_weeks": float(geometry["apex_weeks"]),
        "ma_dispersion": ma_dispersion,
        "ma5": latest_mas[5],
        "ma10": latest_mas[10],
        "ma20": latest_mas[20],
        "ma30": latest_mas[30],
        "volume_ratio": float(volume_ratio),
    }


def _triangle_geometry(
    window: pd.DataFrame,
    params: WeeklyTriangleParameters,
) -> dict[str, float | int] | None:
    if len(window) != params.triangle_weeks:
        return None
    high = pd.to_numeric(window.get("High"), errors="coerce").to_numpy(dtype=float)
    low = pd.to_numeric(window.get("Low"), errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(window.get("Close"), errors="coerce").to_numpy(dtype=float)
    if (
        not np.isfinite(high).all()
        or not np.isfinite(low).all()
        or not np.isfinite(close).all()
    ):
        return None
    if np.any(low <= 0) or np.any(high <= low):
        return None
    x = np.arange(len(window), dtype=float)
    upper_slope, upper_intercept = np.polyfit(x, high, 1)
    lower_slope, lower_intercept = np.polyfit(x, low, 1)
    upper_intercept += float(np.max(high - (upper_slope * x + upper_intercept)))
    lower_intercept += float(np.min(low - (lower_slope * x + lower_intercept)))
    upper_line = upper_slope * x + upper_intercept
    lower_line = lower_slope * x + lower_intercept
    widths = upper_line - lower_line
    mean_price = float(np.mean(close))
    if mean_price <= 0 or np.any(widths <= 0):
        return None
    start_width = float(widths[0])
    end_width = float(widths[-1])
    width_ratio = end_width / start_width if start_width > 0 else float("inf")
    upper_slope_pct = float(upper_slope / mean_price)
    lower_slope_pct = float(lower_slope / mean_price)
    convergence_per_week = lower_slope_pct - upper_slope_pct
    upper_touches = int(
        np.sum((upper_line - high) / mean_price <= params.touch_tolerance)
    )
    lower_touches = int(
        np.sum((low - lower_line) / mean_price <= params.touch_tolerance)
    )
    half = max(2, len(window) // 2)
    candle_ranges = (high - low) / close
    early_range = float(np.median(candle_ranges[:half]))
    late_range = float(np.median(candle_ranges[-half:]))
    range_contraction = late_range / early_range if early_range > 0 else float("inf")
    closing_speed = float(lower_slope - upper_slope)
    apex_weeks = end_width / closing_speed if closing_speed > 0 else float("inf")
    valid = (
        upper_slope_pct <= params.maximum_upper_slope
        and lower_slope_pct >= params.minimum_lower_slope
        and convergence_per_week >= params.minimum_convergence_per_week
        and start_width / mean_price >= params.minimum_start_width
        and width_ratio <= params.max_width_ratio
        and upper_touches >= params.minimum_touches
        and lower_touches >= params.minimum_touches
        and 0.0 <= apex_weeks <= params.maximum_apex_weeks
        and range_contraction <= 1.10
    )
    if not valid:
        return None
    return {
        "upper_slope": float(upper_slope),
        "upper_intercept": float(upper_intercept),
        "lower_slope": float(lower_slope),
        "lower_intercept": float(lower_intercept),
        "upper_slope_pct": upper_slope_pct,
        "lower_slope_pct": lower_slope_pct,
        "width_ratio": float(width_ratio),
        "range_contraction": float(range_contraction),
        "upper_touches": upper_touches,
        "lower_touches": lower_touches,
        "apex_weeks": float(apex_weeks),
    }


def _line_value(geometry: dict[str, Any], prefix: str, offset: int) -> float:
    return float(geometry[f"{prefix}_slope"]) * offset + float(
        geometry[f"{prefix}_intercept"]
    )


def _latest_day(
    bars: dict[str, pd.DataFrame],
    asof: pd.Timestamp | None,
) -> pd.Timestamp | None:
    if asof is not None:
        value = pd.Timestamp(asof)
        return value.tz_localize(None) if value.tzinfo is not None else value
    latest = [pd.Timestamp(frame.index[-1]) for frame in bars.values() if not frame.empty]
    if not latest:
        return None
    value = max(latest)
    return value.tz_localize(None) if value.tzinfo is not None else value


def _is_current(frame: pd.DataFrame, latest_day: pd.Timestamp) -> bool:
    if frame.empty:
        return False
    value = pd.Timestamp(frame.index[-1])
    if value.tzinfo is not None:
        value = value.tz_localize(None)
    return value.normalize() == latest_day.normalize()


def _completed_period(latest_day: pd.Timestamp | None) -> str:
    if latest_day is None:
        return ""
    day = latest_day.normalize()
    period = day.to_period("W-FRI")
    if pd.Timestamp(period.end_time).normalize() > day:
        period -= 1
    return pd.Timestamp(period.end_time).date().isoformat()


def _signal_times(
    value: Any,
    valid_business_days: int,
) -> tuple[datetime, datetime, datetime]:
    timestamp = pd.Timestamp(value).normalize() + pd.Timedelta(hours=18)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(SHANGHAI_TZ)
    else:
        timestamp = timestamp.tz_convert(SHANGHAI_TZ)
    available = (timestamp + pd.offsets.BDay(1)).replace(hour=9, minute=30)
    valid_until = (timestamp + pd.offsets.BDay(valid_business_days)).replace(hour=15)
    return (
        timestamp.to_pydatetime(),
        available.to_pydatetime(),
        valid_until.to_pydatetime(),
    )
