from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
)

from .weekly_triangle import WeeklyTriangleStrategy


@dataclass(frozen=True)
class WeeklyBullPlatformParameters:
    base_weeks: int = 24
    minimum_base_weeks: int = 12
    rise_weeks: int = 40
    minimum_rise_weeks: int = 8
    pre_base_lookback_weeks: int = 16
    moving_average_periods: tuple[int, ...] = (5, 10, 20, 30)
    minimum_advance_from_base: float = 0.35
    maximum_base_close_band: float = 0.18
    maximum_base_total_range: float = 0.30
    maximum_base_slope: float = 0.007
    maximum_pre_base_position: float = 0.50
    maximum_structure_base_position: float = 0.30
    maximum_drawdown_from_high: float = 0.20
    maximum_single_week_advance_share: float = 0.50
    minimum_rising_week_ratio: float = 0.40
    maximum_ma_dispersion: float = 0.18
    ma_slope_lookback: int = 5
    minimum_ma10_slope: float = -0.005
    minimum_ma20_slope: float = 0.005
    minimum_ma30_slope: float = 0.005
    minimum_close_to_ma10: float = 0.98
    breakout_weeks: int = 8
    breakout_buffer: float = 0.02
    breakout_volume_ratio: float = 1.20
    market_ma_period: int = 30
    require_market_filter: bool = True
    fixed_stop_ratio: float = 0.08
    trailing_stop_ratio: float = 0.12
    maximum_holding_days: int = 30
    target_weight: float = 0.15
    max_candidates: int = 100
    max_entry_signals: int = 20
    emit_live_entry_signals: bool = False
    daily_lookback: int = 360


class WeeklyBullPlatformStrategy(WeeklyTriangleStrategy):
    metadata = StrategyMetadata(
        strategy_id="weekly_bull_platform_v1",
        version="2.0.0",
        name="周线底部平台多头",
        description=(
            "识别历史底部平台、平台后周线持续上涨以及 MA5/10/20/30 多头排列；"
            "研究阶段只输出观察候选"
        ),
        frequency="1w",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        asset_classes=("A_STOCK",),
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement(
                "bars",
                "1d",
                "front",
                360,
                True,
                ("Open", "High", "Low", "Close", "Volume", "Amount"),
            ),
            DataRequirement(
                "bars",
                "1d",
                "none",
                360,
                True,
                ("Open", "High", "Low", "Close", "Volume", "Amount"),
            ),
        ),
    )

    def __init__(self, parameters: WeeklyBullPlatformParameters | None = None):
        self.parameters = parameters or WeeklyBullPlatformParameters()

    def _analyze_weekly(self, weekly: pd.DataFrame) -> dict[str, Any] | None:
        return analyze_weekly_bull_platform(weekly, self.parameters)

    def _eligible_code(self, code: str, name: str) -> bool:
        return is_a_share_stock_code(code)

    def _parameter_state(self) -> dict[str, Any]:
        return {
            "base_weeks": self.parameters.base_weeks,
            "minimum_base_weeks": self.parameters.minimum_base_weeks,
            "rise_weeks": self.parameters.rise_weeks,
            "minimum_rise_weeks": self.parameters.minimum_rise_weeks,
            "pre_base_lookback_weeks": self.parameters.pre_base_lookback_weeks,
            "moving_average_periods": list(self.parameters.moving_average_periods),
            "minimum_advance_from_base": self.parameters.minimum_advance_from_base,
            "maximum_base_close_band": self.parameters.maximum_base_close_band,
            "maximum_base_total_range": self.parameters.maximum_base_total_range,
            "maximum_pre_base_position": self.parameters.maximum_pre_base_position,
            "maximum_structure_base_position": (
                self.parameters.maximum_structure_base_position
            ),
            "maximum_drawdown_from_high": self.parameters.maximum_drawdown_from_high,
            "maximum_ma_dispersion": self.parameters.maximum_ma_dispersion,
            "minimum_ma20_slope": self.parameters.minimum_ma20_slope,
            "minimum_ma30_slope": self.parameters.minimum_ma30_slope,
            "breakout_weeks": self.parameters.breakout_weeks,
            "breakout_buffer": self.parameters.breakout_buffer,
            "breakout_volume_ratio": self.parameters.breakout_volume_ratio,
            "market_ma_period": self.parameters.market_ma_period,
            "require_market_filter": self.parameters.require_market_filter,
            "maximum_holding_days": self.parameters.maximum_holding_days,
            "emit_live_entry_signals": self.parameters.emit_live_entry_signals,
        }

    def _entry_reason_codes(self) -> tuple[str, ...]:
        return (
            "WEEKLY_BOTTOM_BASE_BREAKOUT",
            "WEEKLY_RISING_TREND",
            "WEEKLY_MA_BULL_ALIGNMENT",
        )


def is_a_share_stock_code(code: str) -> bool:
    value = str(code).upper()
    if value.endswith(".SH"):
        return value[:3] in {"600", "601", "603", "605", "688", "689"}
    if value.endswith(".SZ"):
        return value[:3] in {"000", "001", "002", "003", "300", "301"}
    if value.endswith(".BJ"):
        return value[:2] in {"43", "83", "87", "88", "92"}
    return False


def analyze_weekly_bull_platform(
    weekly: pd.DataFrame,
    parameters: WeeklyBullPlatformParameters | None = None,
) -> dict[str, Any] | None:
    params = parameters or WeeklyBullPlatformParameters()
    required_periods = (5, 10, 20, 30)
    if not set(required_periods).issubset(params.moving_average_periods):
        return None
    minimum = max(
        max(params.moving_average_periods),
        params.minimum_base_weeks + params.minimum_rise_weeks,
        params.ma_slope_lookback,
    )
    if len(weekly) < minimum:
        return None

    close = pd.to_numeric(weekly.get("Close"), errors="coerce")
    high = pd.to_numeric(weekly.get("High"), errors="coerce")
    low = pd.to_numeric(weekly.get("Low"), errors="coerce")
    if close.isna().any() or high.isna().any() or low.isna().any():
        return None
    if float(close.iloc[-1]) <= 0 or bool((high <= low).any()):
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
    ordered_mas = [latest_mas[period] for period in required_periods]
    if any(left <= right for left, right in zip(ordered_mas, ordered_mas[1:])):
        return None
    ma_values = np.asarray(ordered_mas, dtype=float)
    ma_dispersion = float((ma_values.max() - ma_values.min()) / ma_values.mean())
    if ma_dispersion > params.maximum_ma_dispersion:
        return None

    ma_slopes = {
        period: _period_change(values, params.ma_slope_lookback)
        for period, values in moving_averages.items()
    }
    trend_ok = (
        ma_slopes[10] >= params.minimum_ma10_slope
        and ma_slopes[20] >= params.minimum_ma20_slope
        and ma_slopes[30] >= params.minimum_ma30_slope
        and float(close.iloc[-1])
        >= latest_mas[10] * params.minimum_close_to_ma10
    )
    if not trend_ok:
        return None

    selected: dict[str, Any] | None = None
    selected_score = -1.0
    for rise_weeks in range(
        params.minimum_rise_weeks,
        params.rise_weeks + 1,
    ):
        base_end = len(weekly) - rise_weeks
        for base_weeks in range(
            params.minimum_base_weeks,
            params.base_weeks + 1,
        ):
            base_start = base_end - base_weeks
            if base_start < 0:
                continue
            geometry = _bottom_base_geometry(
                weekly,
                base_start,
                base_end,
                params,
            )
            if geometry is None:
                continue
            score = _shape_score(geometry, ma_slopes, ma_dispersion, params)
            if score <= selected_score:
                continue
            selected = geometry
            selected_score = score
    if selected is None:
        return None

    upper_boundary, lower_boundary = _recent_boundaries(weekly, params.breakout_weeks)
    current_close = float(close.iloc[-1])
    previous_close = float(close.iloc[-2])
    volume_ratio = _latest_volume_ratio(weekly)
    breakout_threshold = upper_boundary * (1.0 + params.breakout_buffer)
    price_above_breakout = current_close > breakout_threshold
    breakout = (
        price_above_breakout
        and previous_close <= breakout_threshold
        and volume_ratio >= params.breakout_volume_ratio
    )
    if price_above_breakout and not breakout:
        return None
    recent_width = max(upper_boundary - lower_boundary, current_close * 0.01)
    price_location = (current_close - lower_boundary) / recent_width

    return {
        "asof": pd.Timestamp(weekly.index[-1]).date().isoformat(),
        "stage": "BREAKOUT" if breakout else "SETUP",
        "breakout": breakout,
        "base_start": str(selected["base_start"]),
        "base_end": str(selected["base_end"]),
        "base_weeks": int(selected["base_weeks"]),
        "rise_weeks": int(selected["rise_weeks"]),
        "platform_weeks": int(selected["base_weeks"]),
        "score": float(selected_score),
        "close": current_close,
        "upper_boundary": upper_boundary,
        "lower_boundary": lower_boundary,
        "price_location": float(price_location),
        "advance_from_base": float(selected["advance_from_base"]),
        "prior_advance": float(selected["advance_from_base"]),
        "base_close_band": float(selected["base_close_band"]),
        "close_band": float(selected["base_close_band"]),
        "base_total_range": float(selected["base_total_range"]),
        "total_range": float(selected["base_total_range"]),
        "base_slope_pct": float(selected["base_slope_pct"]),
        "platform_slope_pct": float(selected["base_slope_pct"]),
        "pre_base_position": float(selected["pre_base_position"]),
        "structure_base_position": float(selected["structure_base_position"]),
        "drawdown_from_structure_high": float(
            selected["drawdown_from_structure_high"]
        ),
        "largest_weekly_advance_share": float(
            selected["largest_weekly_advance_share"]
        ),
        "rising_week_ratio": float(selected["rising_week_ratio"]),
        "advance_volume_ratio": float(selected["advance_volume_ratio"]),
        "ma_dispersion": ma_dispersion,
        "ma5_slope": ma_slopes[5],
        "ma10_slope": ma_slopes[10],
        "ma20_slope": ma_slopes[20],
        "ma30_slope": ma_slopes[30],
        "ma5": latest_mas[5],
        "ma10": latest_mas[10],
        "ma20": latest_mas[20],
        "ma30": latest_mas[30],
        "volume_ratio": volume_ratio,
    }


def _bottom_base_geometry(
    weekly: pd.DataFrame,
    base_start: int,
    base_end: int,
    params: WeeklyBullPlatformParameters,
) -> dict[str, Any] | None:
    base = weekly.iloc[base_start:base_end]
    base_close = pd.to_numeric(base["Close"], errors="coerce").to_numpy(dtype=float)
    base_high = pd.to_numeric(base["High"], errors="coerce").to_numpy(dtype=float)
    base_low = pd.to_numeric(base["Low"], errors="coerce").to_numpy(dtype=float)
    if (
        len(base) < params.minimum_base_weeks
        or not np.isfinite(base_close).all()
        or not np.isfinite(base_high).all()
        or not np.isfinite(base_low).all()
    ):
        return None
    mean_price = float(np.mean(base_close))
    if mean_price <= 0:
        return None
    base_close_band = float(
        (np.quantile(base_close, 0.90) - np.quantile(base_close, 0.10))
        / mean_price
    )
    base_total_range = float((np.max(base_high) - np.min(base_low)) / mean_price)
    base_slope_pct = float(
        np.polyfit(np.arange(len(base_close), dtype=float), base_close, 1)[0]
        / mean_price
    )
    if (
        base_close_band > params.maximum_base_close_band
        or base_total_range > params.maximum_base_total_range
        or abs(base_slope_pct) > params.maximum_base_slope
    ):
        return None

    close = pd.to_numeric(weekly["Close"], errors="coerce")
    structure = weekly.iloc[base_start:]
    structure_low = float(pd.to_numeric(structure["Low"], errors="coerce").min())
    structure_high = float(pd.to_numeric(structure["High"], errors="coerce").max())
    structure_width = max(structure_high - structure_low, mean_price * 0.01)
    structure_base_position = (mean_price - structure_low) / structure_width

    context_start = max(0, base_start - params.pre_base_lookback_weeks)
    pre_base = weekly.iloc[context_start:base_end]
    pre_base_low = float(pd.to_numeric(pre_base["Low"], errors="coerce").min())
    pre_base_high = float(pd.to_numeric(pre_base["High"], errors="coerce").max())
    pre_base_width = max(pre_base_high - pre_base_low, mean_price * 0.01)
    pre_base_position = (mean_price - pre_base_low) / pre_base_width

    current_close = float(close.iloc[-1])
    advance_from_base = current_close / mean_price - 1.0
    drawdown_from_structure_high = current_close / structure_high - 1.0
    trajectory = close.iloc[base_end - 1 :]
    log_returns = np.log(trajectory).diff().dropna()
    total_log_advance = np.log(current_close / mean_price)
    largest_weekly_advance_share = (
        float(max(0.0, float(log_returns.max())) / total_log_advance)
        if total_log_advance > 0 and not log_returns.empty
        else float("inf")
    )
    rising_week_ratio = (
        float((log_returns > 0).mean()) if not log_returns.empty else 0.0
    )

    if (
        structure_base_position > params.maximum_structure_base_position
        or pre_base_position > params.maximum_pre_base_position
        or advance_from_base < params.minimum_advance_from_base
        or drawdown_from_structure_high < -params.maximum_drawdown_from_high
        or largest_weekly_advance_share > params.maximum_single_week_advance_share
        or rising_week_ratio < params.minimum_rising_week_ratio
    ):
        return None

    volume = pd.to_numeric(weekly.get("Volume"), errors="coerce")
    base_volume = float(volume.iloc[base_start:base_end].median())
    advance_volume = float(volume.iloc[base_end:].median())
    advance_volume_ratio = advance_volume / base_volume if base_volume > 0 else 1.0
    return {
        "base_start": pd.Timestamp(base.index[0]).date().isoformat(),
        "base_end": pd.Timestamp(base.index[-1]).date().isoformat(),
        "base_weeks": len(base),
        "rise_weeks": len(weekly) - base_end,
        "advance_from_base": advance_from_base,
        "base_close_band": base_close_band,
        "base_total_range": base_total_range,
        "base_slope_pct": base_slope_pct,
        "pre_base_position": float(pre_base_position),
        "structure_base_position": float(structure_base_position),
        "drawdown_from_structure_high": float(drawdown_from_structure_high),
        "largest_weekly_advance_share": largest_weekly_advance_share,
        "rising_week_ratio": rising_week_ratio,
        "advance_volume_ratio": float(advance_volume_ratio),
    }


def _shape_score(
    geometry: dict[str, Any],
    ma_slopes: dict[int, float],
    ma_dispersion: float,
    params: WeeklyBullPlatformParameters,
) -> float:
    advance_score = min(1.0, float(geometry["advance_from_base"]) / 1.20)
    base_score = (
        0.60
        * _inverse_score(
            float(geometry["base_close_band"]),
            params.maximum_base_close_band,
        )
        + 0.40
        * _inverse_score(
            float(geometry["base_total_range"]),
            params.maximum_base_total_range,
        )
    )
    bottom_score = (
        0.60
        * _inverse_score(
            float(geometry["structure_base_position"]),
            params.maximum_structure_base_position,
        )
        + 0.40
        * _inverse_score(
            float(geometry["pre_base_position"]),
            params.maximum_pre_base_position,
        )
    )
    trend_score = (
        0.20 * min(1.0, max(0.0, ma_slopes[10]) / 0.04)
        + 0.45 * min(1.0, max(0.0, ma_slopes[20]) / 0.06)
        + 0.35 * min(1.0, max(0.0, ma_slopes[30]) / 0.06)
    )
    alignment_score = _inverse_score(ma_dispersion, params.maximum_ma_dispersion)
    smooth_score = _inverse_score(
        float(geometry["largest_weekly_advance_share"]),
        params.maximum_single_week_advance_share,
    )
    near_high_score = _inverse_score(
        abs(float(geometry["drawdown_from_structure_high"])),
        params.maximum_drawdown_from_high,
    )
    volume_score = min(
        1.0,
        max(0.0, (float(geometry["advance_volume_ratio"]) - 0.80) / 1.20),
    )
    return min(
        1.0,
        0.10
        + 0.24 * advance_score
        + 0.18 * base_score
        + 0.14 * bottom_score
        + 0.16 * trend_score
        + 0.07 * alignment_score
        + 0.04 * smooth_score
        + 0.03 * near_high_score
        + 0.04 * volume_score,
    )


def _period_change(values: pd.Series, lookback: int) -> float:
    if lookback < 2 or len(values.dropna()) < lookback:
        return float("-inf")
    latest = float(values.iloc[-1])
    previous = float(values.iloc[-lookback])
    return latest / previous - 1.0 if previous > 0 else float("-inf")


def _recent_boundaries(weekly: pd.DataFrame, weeks: int) -> tuple[float, float]:
    end = -1 if len(weekly) > weeks else None
    start = max(0, len(weekly) - weeks - 1)
    recent = pd.to_numeric(weekly["Close"].iloc[start:end], errors="coerce")
    if recent.empty:
        recent = pd.to_numeric(weekly["Close"].tail(weeks), errors="coerce")
    return float(recent.quantile(0.90)), float(recent.quantile(0.10))


def _inverse_score(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0.0
    return min(1.0, max(0.0, 1.0 - value / maximum))


def _latest_volume_ratio(weekly: pd.DataFrame) -> float:
    if "Volume" not in weekly or len(weekly) < 6:
        return 0.0
    volume = pd.to_numeric(weekly["Volume"], errors="coerce")
    reference = float(volume.iloc[-6:-1].median())
    latest = float(volume.iloc[-1])
    return latest / reference if reference > 0 else 0.0
