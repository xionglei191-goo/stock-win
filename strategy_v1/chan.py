from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .indicators import macd


FractalKind = Literal["TOP", "BOTTOM"]


@dataclass(frozen=True)
class Fractal:
    position: int
    timestamp: pd.Timestamp
    kind: FractalKind
    price: float


@dataclass(frozen=True)
class Stroke:
    start: Fractal
    end: Fractal
    low: float
    high: float


@dataclass(frozen=True)
class Center:
    start_position: int
    end_position: int
    lower: float
    upper: float
    confirmed_at: pd.Timestamp


@dataclass(frozen=True)
class ChanState:
    center: Center | None
    breakout: bool
    breakdown: bool
    bearish_divergence: bool
    merged_bars: pd.DataFrame
    fractals: tuple[Fractal, ...]
    strokes: tuple[Stroke, ...]


@dataclass(frozen=True)
class ChanParameters:
    min_bar_distance: int = 5
    atr_window: int = 20
    max_atr_ratio: float = 1.0
    max_signal_return: float = 1.0
    min_volume_ratio: float = 0.0
    trailing_activation: float = 1.0
    trailing_drawdown: float = 1.0


REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def _prepare_bars(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"K-line data missing columns: {missing}")
    bars = frame.loc[:, REQUIRED_COLUMNS].copy()
    bars.index = pd.to_datetime(bars.index)
    bars = bars[~bars.index.duplicated(keep="last")].sort_index()
    bars = bars.apply(pd.to_numeric, errors="coerce").dropna(subset=["High", "Low", "Close"])
    return bars


def merge_inclusions(frame: pd.DataFrame) -> pd.DataFrame:
    bars = _prepare_bars(frame)
    if bars.empty:
        return bars.assign(SourceCount=pd.Series(dtype=int))

    merged: list[dict[str, object]] = []
    direction = 0
    for timestamp, row in bars.iterrows():
        current = {
            "timestamp": timestamp,
            "Open": float(row["Open"]),
            "High": float(row["High"]),
            "Low": float(row["Low"]),
            "Close": float(row["Close"]),
            "Volume": float(row["Volume"]),
            "SourceCount": 1,
        }
        if not merged:
            merged.append(current)
            continue

        previous = merged[-1]
        contains = (
            (current["High"] >= previous["High"] and current["Low"] <= previous["Low"])
            or (previous["High"] >= current["High"] and previous["Low"] <= current["Low"])
        )
        if not contains:
            if current["High"] > previous["High"] and current["Low"] > previous["Low"]:
                direction = 1
            elif current["High"] < previous["High"] and current["Low"] < previous["Low"]:
                direction = -1
            merged.append(current)
            continue

        if direction >= 0:
            previous["High"] = max(float(previous["High"]), float(current["High"]))
            previous["Low"] = max(float(previous["Low"]), float(current["Low"]))
        else:
            previous["High"] = min(float(previous["High"]), float(current["High"]))
            previous["Low"] = min(float(previous["Low"]), float(current["Low"]))
        previous["Close"] = current["Close"]
        previous["Volume"] = float(previous["Volume"]) + float(current["Volume"])
        previous["timestamp"] = timestamp
        previous["SourceCount"] = int(previous["SourceCount"]) + 1

    result = pd.DataFrame(merged).set_index("timestamp")
    result.index.name = bars.index.name
    return result


def find_fractals(merged: pd.DataFrame) -> list[Fractal]:
    fractals: list[Fractal] = []
    for position in range(1, len(merged) - 1):
        left = merged.iloc[position - 1]
        middle = merged.iloc[position]
        right = merged.iloc[position + 1]
        is_top = (
            middle["High"] > left["High"]
            and middle["High"] > right["High"]
            and middle["Low"] > left["Low"]
            and middle["Low"] > right["Low"]
        )
        is_bottom = (
            middle["Low"] < left["Low"]
            and middle["Low"] < right["Low"]
            and middle["High"] < left["High"]
            and middle["High"] < right["High"]
        )
        if is_top:
            fractals.append(Fractal(position, merged.index[position], "TOP", float(middle["High"])))
        elif is_bottom:
            fractals.append(Fractal(position, merged.index[position], "BOTTOM", float(middle["Low"])))
    return fractals


def build_strokes(fractals: list[Fractal], min_bar_distance: int = 5) -> list[Stroke]:
    selected: list[Fractal] = []
    for fractal in fractals:
        if not selected:
            selected.append(fractal)
            continue
        last = selected[-1]
        if fractal.kind == last.kind:
            more_extreme = fractal.price > last.price if fractal.kind == "TOP" else fractal.price < last.price
            if more_extreme:
                selected[-1] = fractal
            continue
        if fractal.position - last.position < min_bar_distance:
            continue
        if last.kind == "BOTTOM" and fractal.price <= last.price:
            continue
        if last.kind == "TOP" and fractal.price >= last.price:
            continue
        selected.append(fractal)

    strokes: list[Stroke] = []
    for start, end in zip(selected, selected[1:]):
        strokes.append(Stroke(start, end, min(start.price, end.price), max(start.price, end.price)))
    return strokes


def find_centers(strokes: list[Stroke]) -> list[Center]:
    centers: list[Center] = []
    for index in range(len(strokes) - 2):
        group = strokes[index : index + 3]
        lower = max(stroke.low for stroke in group)
        upper = min(stroke.high for stroke in group)
        if lower < upper:
            centers.append(
                Center(
                    start_position=group[0].start.position,
                    end_position=group[-1].end.position,
                    lower=float(lower),
                    upper=float(upper),
                    confirmed_at=group[-1].end.timestamp,
                )
            )
    return centers


def detect_bearish_divergence(merged: pd.DataFrame, fractals: list[Fractal]) -> bool:
    tops = [fractal for fractal in fractals if fractal.kind == "TOP"]
    if len(tops) < 2 or tops[-1].position != len(merged) - 2:
        return False
    previous, latest = tops[-2], tops[-1]
    histogram = macd(merged["Close"])["histogram"]
    previous_hist = histogram.iloc[previous.position]
    latest_hist = histogram.iloc[latest.position]
    return bool(
        latest.price > previous.price
        and pd.notna(previous_hist)
        and pd.notna(latest_hist)
        and latest_hist < previous_hist
    )


def detect_center_cross(merged: pd.DataFrame, center: Center | None) -> tuple[bool, bool]:
    if center is None or len(merged) < 2 or merged.index[-1] <= center.confirmed_at:
        return False, False
    previous_close = float(merged["Close"].iloc[-2])
    latest_close = float(merged["Close"].iloc[-1])
    breakout = previous_close <= center.upper < latest_close
    breakdown = previous_close >= center.lower > latest_close
    return breakout, breakdown


def daily_entry_allowed(frame: pd.DataFrame, parameters: ChanParameters) -> bool:
    bars = _prepare_bars(frame)
    window = max(2, parameters.atr_window)
    if len(bars) < window + 1:
        return False
    previous_close = bars["Close"].shift(1)
    true_range = pd.concat(
        [
            bars["High"] - bars["Low"],
            (bars["High"] - previous_close).abs(),
            (bars["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    close = float(bars["Close"].iloc[-1])
    atr_ratio = float(true_range.tail(window).mean() / close) if close > 0 else float("inf")
    signal_return = float(bars["Close"].iloc[-1] / bars["Close"].iloc[-2] - 1.0)
    volume = pd.to_numeric(bars["Volume"], errors="coerce")
    baseline_volume = float(volume.iloc[-window - 1 : -1].mean())
    volume_ratio = float(volume.iloc[-1] / baseline_volume) if baseline_volume > 0 else 0.0
    return bool(
        atr_ratio <= parameters.max_atr_ratio
        and signal_return <= parameters.max_signal_return
        and volume_ratio >= parameters.min_volume_ratio
    )


def daily_trailing_exit(
    frame: pd.DataFrame,
    entry_time: str | pd.Timestamp,
    entry_price: float,
    parameters: ChanParameters,
) -> bool:
    if parameters.trailing_activation >= 1.0 or parameters.trailing_drawdown >= 1.0:
        return False
    bars = _prepare_bars(frame)
    held = bars[bars.index >= pd.Timestamp(entry_time)]
    if held.empty or entry_price <= 0:
        return False
    highest_close = float(held["Close"].max())
    latest_close = float(held["Close"].iloc[-1])
    return bool(
        highest_close >= entry_price * (1.0 + parameters.trailing_activation)
        and latest_close <= highest_close * (1.0 - parameters.trailing_drawdown)
    )


def analyze_chan(
    frame: pd.DataFrame,
    parameters: ChanParameters | None = None,
) -> ChanState:
    parameters = parameters or ChanParameters()
    merged = merge_inclusions(frame)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals, min_bar_distance=parameters.min_bar_distance)
    centers = find_centers(strokes)
    center = centers[-1] if centers else None
    breakout, breakdown = detect_center_cross(merged, center)
    divergence = detect_bearish_divergence(merged, fractals)
    return ChanState(center, breakout, breakdown, divergence, merged, tuple(fractals), tuple(strokes))
