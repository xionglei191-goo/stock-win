from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .indicators import macd


FractalKind = Literal["TOP", "BOTTOM"]
TrendKind = Literal["UP", "DOWN", "RANGE"]


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
class Segment:
    """线段：至少三笔，且末笔极值突破首笔同向极值。"""

    start: Fractal
    end: Fractal
    low: float
    high: float
    stroke_count: int
    direction: Literal["UP", "DOWN"]


@dataclass(frozen=True)
class Center:
    start_position: int
    end_position: int
    lower: float
    upper: float
    confirmed_at: pd.Timestamp
    unit_count: int = 3
    level: Literal["STROKE", "SEGMENT"] = "SEGMENT"


@dataclass(frozen=True)
class ChanState:
    center: Center | None
    breakout: bool
    breakdown: bool
    bearish_divergence: bool
    bullish_divergence: bool
    breakout_confirmed: bool  # breakout + MACD diff > 0 + segment-level center
    trend: TrendKind
    merged_bars: pd.DataFrame
    fractals: tuple[Fractal, ...]
    strokes: tuple[Stroke, ...]
    segments: tuple[Segment, ...]
    centers: tuple[Center, ...]


@dataclass(frozen=True)
class ChanParameters:
    min_bar_distance: int = 5
    atr_window: int = 20
    # Entry filter: previously all at permissive defaults that passed everything.
    # Real values that eliminate noise while allowing genuine setups:
    max_atr_ratio: float = 0.05       # daily ATR/close ≤ 5% — avoids hyper-volatile stocks
    max_signal_return: float = 0.07   # don't chase a stock already up >7% on signal day
    min_volume_ratio: float = 1.2     # require volume pickup vs 20-day baseline on entry
    # Trailing exit: disabled in old implementation (thresholds = 1.0).
    # Enable with conservative values so winners can run but short-lived pops are cut.
    trailing_activation: float = 0.10 # activate after gaining 10%
    trailing_drawdown: float = 0.05   # exit if price retreats 5% from peak
    divergence_area_ratio: float = 0.80
    center_level: Literal["STROKE", "SEGMENT"] = "SEGMENT"
    require_segment_center: bool = True  # never buy on a stroke-level center


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
    """包含处理。方向由包含发生前已确立的独立K线关系决定，避免默认向上的偏置。"""
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

        if direction == 0:
            # 序列开头即遇包含：用已有合并K线的相对位置推断，而不是无条件按向上处理。
            if len(merged) >= 2:
                reference = merged[-2]
                direction = 1 if float(previous["High"]) >= float(reference["High"]) else -1
            else:
                direction = 1 if float(current["Close"]) >= float(previous["Open"]) else -1

        if direction > 0:
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
    """分型识别。

    按缠中说禅原始定义：顶分型只要求中间K线的高点高于左右两侧高点，
    底分型只要求中间K线的低点低于左右两侧低点。包含处理已消除包含关系，
    无需再对另一端做冗余校验（旧实现的冗余条件会漏掉有效分型）。
    """
    fractals: list[Fractal] = []
    for position in range(1, len(merged) - 1):
        left = merged.iloc[position - 1]
        middle = merged.iloc[position]
        right = merged.iloc[position + 1]
        if middle["High"] > left["High"] and middle["High"] > right["High"]:
            fractals.append(Fractal(position, merged.index[position], "TOP", float(middle["High"])))
        elif middle["Low"] < left["Low"] and middle["Low"] < right["Low"]:
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


def build_segments(strokes: list[Stroke]) -> list[Segment]:
    """线段构建（原实现完全缺失该层级）。

    线段由奇数笔（至少三笔）组成，同向末笔必须突破首笔的同向极值：
    上升线段以底分型起，末笔顶点须高于首笔顶点；下降线段以顶分型起，
    末笔底点须低于首笔底点。突破失败则该起点不成段，起点前移一笔重试。
    """
    if len(strokes) < 3:
        return []

    segments: list[Segment] = []
    cursor = 0
    while cursor <= len(strokes) - 3:
        first = strokes[cursor]
        going_up = first.start.kind == "BOTTOM"
        closed = False
        for end_index in range(cursor + 2, len(strokes), 2):
            last = strokes[end_index]
            broken = (
                last.end.price > first.end.price
                if going_up
                else last.end.price < first.end.price
            )
            if not broken:
                continue
            span = strokes[cursor : end_index + 1]
            segments.append(
                Segment(
                    start=first.start,
                    end=last.end,
                    low=min(stroke.low for stroke in span),
                    high=max(stroke.high for stroke in span),
                    stroke_count=end_index - cursor + 1,
                    direction="UP" if going_up else "DOWN",
                )
            )
            cursor = end_index
            closed = True
            break
        if not closed:
            cursor += 1
    return segments


def _overlap(units: list[Stroke] | list[Segment]) -> tuple[float, float]:
    return max(unit.low for unit in units), min(unit.high for unit in units)


def find_centers(units: list[Stroke] | list[Segment]) -> list[Center]:
    """中枢识别，支持中枢延伸。

    原实现对每个三笔窗口独立建一个中枢，导致同一个真实中枢被拆成多个重叠的伪中枢，
    且 ``centers[-1]`` 常常不是当前有效中枢。这里改为：三个单位确认基础中枢后，
    只要后续单位仍与中枢区间重叠就并入延伸，直到走势离开区间才结束当前中枢。
    """
    centers: list[Center] = []
    if len(units) < 3:
        return centers

    level: Literal["STROKE", "SEGMENT"] = (
        "SEGMENT" if units and isinstance(units[0], Segment) else "STROKE"
    )
    cursor = 0
    while cursor <= len(units) - 3:
        lower, upper = _overlap(list(units[cursor : cursor + 3]))
        if lower >= upper:
            cursor += 1
            continue

        end_index = cursor + 2
        while end_index + 1 < len(units):
            candidate = units[end_index + 1]
            if candidate.low >= upper or candidate.high <= lower:
                break
            extended_lower = max(lower, candidate.low)
            extended_upper = min(upper, candidate.high)
            if extended_lower >= extended_upper:
                break
            lower, upper = extended_lower, extended_upper
            end_index += 1

        centers.append(
            Center(
                start_position=units[cursor].start.position,
                end_position=units[end_index].end.position,
                lower=float(lower),
                upper=float(upper),
                confirmed_at=units[end_index].end.timestamp,
                unit_count=end_index - cursor + 1,
                level=level,
            )
        )
        cursor = end_index + 1
    return centers


def classify_trend(centers: list[Center]) -> TrendKind:
    """走势类型：单中枢为盘整，多个同向推进的中枢为趋势。"""
    if len(centers) < 2:
        return "RANGE"
    previous, latest = centers[-2], centers[-1]
    if latest.lower > previous.upper:
        return "UP"
    if latest.upper < previous.lower:
        return "DOWN"
    return "RANGE"


def _histogram_area(histogram: pd.Series, start_position: int, end_position: int) -> float:
    """同向走势段对应的 MACD 柱面积（缠中说禅的背驰比较依据）。"""
    if start_position > end_position:
        return 0.0
    window = histogram.iloc[start_position : end_position + 1].dropna()
    if window.empty:
        return 0.0
    return float(window.abs().sum())


def _divergence(
    merged: pd.DataFrame,
    fractals: list[Fractal],
    kind: FractalKind,
    area_ratio: float,
) -> bool:
    same = [item for item in fractals if item.kind == kind]
    opposite_kind: FractalKind = "BOTTOM" if kind == "TOP" else "TOP"
    opposite = [item for item in fractals if item.kind == opposite_kind]
    if len(same) < 2 or not opposite or len(merged) < 2:
        return False

    latest = same[-1]
    if latest.position >= len(merged) - 1:
        return False

    pivots = [item for item in opposite if item.position < latest.position]
    if not pivots:
        return False
    pivot = pivots[-1]

    earlier = [item for item in same if item.position < pivot.position]
    if not earlier:
        return False
    previous = earlier[-1]

    # 价格必须创新高（顶背驰）或创新低（底背驰）
    extended = latest.price > previous.price if kind == "TOP" else latest.price < previous.price
    if not extended:
        return False

    histogram = macd(merged["Close"])["histogram"]
    if histogram.dropna().empty:
        return False

    earlier_pivots = [item for item in opposite if item.position < previous.position]
    previous_start = earlier_pivots[-1].position if earlier_pivots else 0
    previous_area = _histogram_area(histogram, previous_start, previous.position)
    latest_area = _histogram_area(histogram, pivot.position, latest.position)
    if previous_area <= 0.0:
        return False
    return bool(latest_area < previous_area * area_ratio)


def detect_bearish_divergence(
    merged: pd.DataFrame,
    fractals: list[Fractal],
    area_ratio: float = 0.80,
) -> bool:
    """顶背驰：后一段上涨价格更高，但 MACD 柱面积明显萎缩。

    旧实现比较的是两个顶分型位置上的单点柱值，会漏掉真实背驰并产生假信号；
    缠中说禅的判据是两段同向走势各自对应的柱面积。
    """
    return _divergence(merged, fractals, "TOP", area_ratio)


def detect_bullish_divergence(
    merged: pd.DataFrame,
    fractals: list[Fractal],
    area_ratio: float = 0.80,
) -> bool:
    """底背驰：后一段下跌价格更低，但 MACD 柱面积明显萎缩，对应第一类买点。"""
    return _divergence(merged, fractals, "BOTTOM", area_ratio)


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
    segments = build_segments(strokes)

    units: list[Stroke] | list[Segment]
    if parameters.center_level == "SEGMENT" and len(segments) >= 3:
        units = segments
    else:
        units = strokes
    centers = find_centers(units)
    center = centers[-1] if centers else None

    breakout, breakdown = detect_center_cross(merged, center)
    bearish = detect_bearish_divergence(merged, fractals, parameters.divergence_area_ratio)
    bullish = detect_bullish_divergence(merged, fractals, parameters.divergence_area_ratio)

    # breakout_confirmed: all three gates must pass before a buy is warranted.
    # 1. Raw center cross happened.
    # 2. Center is segment-level (when required) — avoids stroke-level noise.
    # 3. MACD DIF line is above the zero axis — macro momentum is positive.
    segment_center = center is not None and center.level == "SEGMENT"
    center_ok = not parameters.require_segment_center or segment_center
    macd_data = macd(merged["Close"])
    diff_series = macd_data["diff"].dropna()
    macd_positive = not diff_series.empty and float(diff_series.iloc[-1]) > 0
    breakout_confirmed = bool(breakout and center_ok and macd_positive)

    return ChanState(
        center=center,
        breakout=breakout,
        breakdown=breakdown,
        bearish_divergence=bearish,
        bullish_divergence=bullish,
        breakout_confirmed=breakout_confirmed,
        trend=classify_trend(centers),
        merged_bars=merged,
        fractals=tuple(fractals),
        strokes=tuple(strokes),
        segments=tuple(segments),
        centers=tuple(centers),
    )
