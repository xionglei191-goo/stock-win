from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import PortfolioConfig
from .strategies.weekly_triangle import (
    WeeklyTriangleParameters,
    WeeklyTriangleStrategy,
    analyze_weekly_triangle,
    resample_weekly_bars,
)


WEEKLY_TRIANGLE_RANK_FEATURES = (
    "score",
    "triangle_weeks",
    "ma_dispersion",
    "volume_ratio",
    "width_ratio",
    "range_contraction",
    "upper_slope_pct",
    "lower_slope_pct",
    "upper_touches",
    "lower_touches",
    "apex_weeks",
    "breakout_extension",
    "price_location",
    "close_to_ma30",
    "ma5_to_ma30",
    "prior_return_4w",
    "prior_return_12w",
    "weekly_volatility_8w",
    "median_amount_4w",
    "touches",
    "log_median_amount_4w",
)

WEEKLY_TRIANGLE_SETUP_FEATURES = tuple(
    feature
    for feature in WEEKLY_TRIANGLE_RANK_FEATURES
    if feature != "volume_ratio"
)


@dataclass(frozen=True)
class WeeklyTriangleEvent:
    code: str
    asof: str
    stage: str
    score: float
    triangle_weeks: int
    ma_dispersion: float
    volume_ratio: float
    width_ratio: float | None = None
    range_contraction: float | None = None
    upper_slope_pct: float | None = None
    lower_slope_pct: float | None = None
    upper_touches: int | None = None
    lower_touches: int | None = None
    apex_weeks: float | None = None
    breakout_extension: float | None = None
    price_location: float | None = None
    close_to_ma30: float | None = None
    ma5_to_ma30: float | None = None
    prior_return_4w: float | None = None
    prior_return_12w: float | None = None
    weekly_volatility_8w: float | None = None
    median_amount_4w: float | None = None
    market_above_ma: bool | None = None
    entry_allowed: bool = True
    entry_selected: bool = True
    cross_section_rank: int | None = None
    cross_section_count: int | None = None
    entry_price: float | None = None
    entry_date: str | None = None
    entry_gap: float | None = None
    return_1w: float | None = None
    return_4w: float | None = None
    return_8w: float | None = None
    mae_8w: float | None = None
    mfe_8w: float | None = None
    exit_date: str | None = None
    exit_reason: str | None = None
    holding_days: int | None = None
    gross_return: float | None = None
    net_return: float | None = None
    net_return_2x: float | None = None

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_weekly_triangle_events(
    front_bars: dict[str, pd.DataFrame],
    *,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    parameters: WeeklyTriangleParameters | None = None,
    market_index: pd.DataFrame | None = None,
    market_ma_period: int = 30,
    raw_bars: dict[str, pd.DataFrame] | None = None,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    params = parameters or WeeklyTriangleParameters()
    strategy = WeeklyTriangleStrategy(params)
    costs = execution_config or PortfolioConfig()
    start_day = pd.Timestamp(start).normalize()
    end_day = pd.Timestamp(end).normalize()
    events: list[WeeklyTriangleEvent] = []
    market_state = _weekly_market_state(market_index, end_day, market_ma_period)

    for code, frame in front_bars.items():
        weekly = resample_weekly_bars(frame)
        if weekly.empty:
            continue
        weekly = weekly.loc[pd.to_datetime(weekly["WeekEnd"]) <= end_day]
        minimum = max(params.moving_average_periods) - 1
        for offset in range(minimum, len(weekly)):
            event_day = pd.Timestamp(weekly.index[offset]).normalize()
            if event_day < start_day or event_day > end_day:
                continue
            analysis = analyze_weekly_triangle(weekly.iloc[: offset + 1], params)
            if analysis is None:
                continue
            common = {
                "code": code,
                "asof": event_day.date().isoformat(),
                "stage": str(analysis["stage"]),
                "score": float(analysis["score"]),
                "triangle_weeks": int(analysis["triangle_weeks"]),
                "ma_dispersion": float(analysis["ma_dispersion"]),
                "volume_ratio": float(analysis["volume_ratio"]),
                **_point_in_time_features(weekly.iloc[: offset + 1], analysis),
            }
            market_above_ma = _market_allowed(market_state, event_day)
            common["market_above_ma"] = market_above_ma
            common["entry_allowed"] = market_above_ma is not False
            if analysis["stage"] != "BREAKOUT" or offset + 8 >= len(weekly):
                events.append(WeeklyTriangleEvent(**common))
                continue
            entry = float(weekly["Open"].iloc[offset + 1])
            forward = weekly.iloc[offset + 1 : offset + 9]
            if not np.isfinite(entry) or entry <= 0 or len(forward) < 8:
                events.append(WeeklyTriangleEvent(**common))
                continue
            simulation = _simulate_trade(
                strategy,
                code,
                event_day,
                analysis,
                frame,
                (raw_bars or {}).get(code),
                costs,
                execution_cost_multiplier,
            )
            stress_simulation = _simulate_trade(
                strategy,
                code,
                event_day,
                analysis,
                frame,
                (raw_bars or {}).get(code),
                costs,
                execution_cost_multiplier * 2.0,
            )
            if stress_simulation.get("net_return") is not None:
                simulation["net_return_2x"] = stress_simulation["net_return"]
            events.append(
                WeeklyTriangleEvent(
                    **common,
                    entry_price=entry,
                    return_1w=float(forward["Close"].iloc[0] / entry - 1.0),
                    return_4w=float(forward["Close"].iloc[3] / entry - 1.0),
                    return_8w=float(forward["Close"].iloc[7] / entry - 1.0),
                    mae_8w=float(forward["Low"].min() / entry - 1.0),
                    mfe_8w=float(forward["High"].max() / entry - 1.0),
                    **simulation,
                )
            )

    events = _rank_breakout_events(events, params.max_entry_signals)
    records = [event.as_record() for event in events]
    setup = [event for event in events if event.stage == "SETUP"]
    eligible_breakout = [
        event
        for event in events
        if event.stage == "BREAKOUT"
        and event.return_8w is not None
        and event.entry_allowed
    ]
    breakout = [event for event in eligible_breakout if event.entry_selected]
    raw_breakout = [
        event
        for event in events
        if event.stage == "BREAKOUT" and event.return_8w is not None
    ]
    breakouts_by_code: dict[str, list[pd.Timestamp]] = {}
    for event in breakout:
        breakouts_by_code.setdefault(event.code, []).append(pd.Timestamp(event.asof))
    converted = sum(
        any(
            setup_day < breakout_day <= setup_day + pd.Timedelta(days=35)
            for breakout_day in breakouts_by_code.get(event.code, [])
        )
        for event in setup
        for setup_day in [pd.Timestamp(event.asof)]
    )
    return {
        "start": start_day.date().isoformat(),
        "end": end_day.date().isoformat(),
        "symbols": len(front_bars),
        "setup_events": len(setup),
        "setup_symbols": len({event.code for event in setup}),
        "converted_4w": converted,
        "conversion_rate_4w": converted / len(setup) if setup else 0.0,
        "breakout_events": len(breakout),
        "eligible_breakout_events": len(eligible_breakout),
        "raw_breakout_events": len(raw_breakout),
        "market_blocked_events": len(raw_breakout) - len(eligible_breakout),
        "rank_blocked_events": len(eligible_breakout) - len(breakout),
        "breakout_symbols": len({event.code for event in breakout}),
        "return_1w": _distribution(breakout, "return_1w"),
        "return_4w": _distribution(breakout, "return_4w"),
        "return_8w": _distribution(breakout, "return_8w"),
        "mae_8w": _distribution(breakout, "mae_8w"),
        "mfe_8w": _distribution(breakout, "mfe_8w"),
        "net_return": _distribution(breakout, "net_return"),
        "net_return_2x": _distribution(breakout, "net_return_2x"),
        "entry_diagnostics": _entry_diagnostics(eligible_breakout, breakout),
        "exit_reasons": _exit_reason_counts(breakout),
        "execution_cost_multiplier": float(execution_cost_multiplier),
        "events": records,
    }


def persist_weekly_triangle_research(
    result: dict[str, Any],
    directory: str | Path,
    artifact_id: str,
) -> dict[str, str]:
    if not artifact_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in artifact_id
    ):
        raise ValueError("artifact_id must contain only letters, digits, '-' or '_'")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    event_path = target / f"{artifact_id}_events.parquet"
    summary_path = target / f"{artifact_id}_summary.json"
    event_temporary = event_path.with_suffix(".parquet.tmp")
    summary_temporary = summary_path.with_suffix(".json.tmp")
    pd.DataFrame(result.get("events") or []).to_parquet(event_temporary, index=False)
    summary = {key: value for key, value in result.items() if key != "events"}
    summary_temporary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    event_temporary.replace(event_path)
    summary_temporary.replace(summary_path)
    return {"events": str(event_path), "summary": str(summary_path)}


def analyze_weekly_triangle_feature_stability(
    event_frames: dict[str, pd.DataFrame],
    *,
    development_windows: tuple[str, ...],
    maximum_entries: int = 20,
    features: tuple[str, ...] = WEEKLY_TRIANGLE_RANK_FEATURES,
) -> dict[str, Any]:
    if not development_windows or any(
        window not in event_frames for window in development_windows
    ):
        raise ValueError("development_windows must identify available event frames")
    prepared = {
        window: _eligible_outcome_frame(frame)
        for window, frame in event_frames.items()
    }
    rows: list[dict[str, Any]] = []
    for feature in features:
        for direction, ascending in (("low", True), ("high", False)):
            windows = {
                window: _feature_selection_metrics(
                    frame,
                    feature,
                    ascending=ascending,
                    maximum_entries=maximum_entries,
                )
                for window, frame in prepared.items()
            }
            development = [windows[window] for window in development_windows]
            development_values = [
                float(item["mean_2x"])
                for item in development
                if item["mean_2x"] is not None
            ]
            qualifies = (
                len(development_values) == len(development)
                and all(value > 0 for value in development_values)
            )
            rows.append(
                {
                    "feature": feature,
                    "direction": direction,
                    "development_qualified": qualifies,
                    "development_worst_2x": (
                        min(development_values) if development_values else None
                    ),
                    "development_mean_2x": (
                        float(np.mean(development_values))
                        if development_values
                        else None
                    ),
                    "windows": windows,
                }
            )
    rows.sort(
        key=lambda item: (
            (
                float(item["development_worst_2x"])
                if item["development_worst_2x"] is not None
                else -np.inf
            ),
            (
                float(item["development_mean_2x"])
                if item["development_mean_2x"] is not None
                else -np.inf
            ),
        ),
        reverse=True,
    )
    return {
        "development_windows": list(development_windows),
        "maximum_entries": int(maximum_entries),
        "features": list(features),
        "qualified": [item for item in rows if item["development_qualified"]],
        "rankings": rows,
    }


def analyze_weekly_triangle_setup_stability(
    event_frames: dict[str, pd.DataFrame],
    *,
    development_windows: tuple[str, ...],
    validation_windows: tuple[str, ...],
    maximum_setups: int = 20,
    maximum_entries: int = 20,
    episode_gap_days: int = 14,
    conversion_days: int = 35,
    minimum_development_samples: int = 50,
    minimum_trade_samples: int = 20,
    features: tuple[str, ...] = WEEKLY_TRIANGLE_SETUP_FEATURES,
) -> dict[str, Any]:
    required_windows = (*development_windows, *validation_windows)
    if (
        not development_windows
        or not validation_windows
        or len(set(required_windows)) != len(required_windows)
        or any(window not in event_frames for window in required_windows)
    ):
        raise ValueError(
            "development_windows and validation_windows must be disjoint and available"
        )
    if maximum_setups <= 0 or maximum_entries <= 0:
        raise ValueError("selection limits must be positive")
    if episode_gap_days <= 0 or conversion_days <= 0:
        raise ValueError("episode and conversion windows must be positive")

    episodes = {
        window: _prepare_setup_episodes(
            frame,
            episode_gap_days=episode_gap_days,
            conversion_days=conversion_days,
        )
        for window, frame in event_frames.items()
    }
    baseline = {
        window: _setup_conversion_metrics(
            frame,
            "score",
            ascending=False,
            maximum_setups=maximum_setups,
        )
        for window, frame in episodes.items()
    }
    rows: list[dict[str, Any]] = []
    for feature in features:
        for direction, ascending in (("low", True), ("high", False)):
            conversion_windows = {
                window: _setup_conversion_metrics(
                    frame,
                    feature,
                    ascending=ascending,
                    maximum_setups=maximum_setups,
                    baseline=baseline[window],
                )
                for window, frame in episodes.items()
            }
            development_metrics = [
                conversion_windows[window] for window in development_windows
            ]
            development_qualified = all(
                int(metric["count"]) >= minimum_development_samples
                and metric["lift"] is not None
                and float(metric["lift"]) > 0
                for metric in development_metrics
            )
            trade_windows: dict[str, Any] = {}
            development_trade_qualified = False
            validation_conversion_confirmed = False
            validation_trade_confirmed = False
            if development_qualified:
                trade_windows = {
                    window: _setup_followthrough_metrics(
                        event_frames[window],
                        episodes[window],
                        feature,
                        ascending=ascending,
                        maximum_setups=maximum_setups,
                        maximum_entries=maximum_entries,
                        conversion_days=conversion_days,
                    )
                    for window in required_windows
                }
                development_trade_qualified = all(
                    _trade_gate(
                        trade_windows[window]["selected"],
                        minimum_trade_samples,
                    )
                    for window in development_windows
                )
                validation_conversion_confirmed = all(
                    conversion_windows[window]["lift"] is not None
                    and float(conversion_windows[window]["lift"]) > 0
                    for window in validation_windows
                )
                validation_trade_confirmed = all(
                    _trade_gate(
                        trade_windows[window]["selected"],
                        minimum_trade_samples,
                    )
                    for window in validation_windows
                )
            rows.append(
                {
                    "feature": feature,
                    "direction": direction,
                    "development_conversion_qualified": development_qualified,
                    "development_trade_qualified": development_trade_qualified,
                    "validation_conversion_confirmed": validation_conversion_confirmed,
                    "validation_trade_confirmed": validation_trade_confirmed,
                    "promotion_qualified": (
                        development_qualified
                        and development_trade_qualified
                        and validation_conversion_confirmed
                        and validation_trade_confirmed
                    ),
                    "development_worst_lift": min(
                        (
                            float(metric["lift"])
                            for metric in development_metrics
                            if metric["lift"] is not None
                        ),
                        default=None,
                    ),
                    "conversion_windows": conversion_windows,
                    "trade_windows": trade_windows,
                }
            )
    rows.sort(
        key=lambda item: (
            bool(item["development_conversion_qualified"]),
            float(item["development_worst_lift"] or -np.inf),
        ),
        reverse=True,
    )
    return {
        "development_windows": list(development_windows),
        "validation_windows": list(validation_windows),
        "maximum_setups": int(maximum_setups),
        "maximum_entries": int(maximum_entries),
        "episode_gap_days": int(episode_gap_days),
        "conversion_days": int(conversion_days),
        "minimum_development_samples": int(minimum_development_samples),
        "minimum_trade_samples": int(minimum_trade_samples),
        "features": list(features),
        "episode_windows": {
            window: {
                "episodes": int(len(frame)),
                "converted": int(frame["converted_4w"].sum()),
                "conversion_rate": (
                    float(frame["converted_4w"].mean()) if not frame.empty else None
                ),
                "cohorts": int(frame["asof_dt"].nunique()),
                "baseline_top20": baseline[window],
            }
            for window, frame in episodes.items()
        },
        "development_conversion_qualified": [
            item for item in rows if item["development_conversion_qualified"]
        ],
        "validation_conversion_confirmed": [
            item for item in rows if item["validation_conversion_confirmed"]
        ],
        "development_trade_qualified": [
            item for item in rows if item["development_trade_qualified"]
        ],
        "promotion_qualified": [
            item for item in rows if item["promotion_qualified"]
        ],
        "rankings": rows,
    }


def persist_weekly_triangle_setup_stability(
    result: dict[str, Any],
    directory: str | Path,
) -> str:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "setup_stability.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return str(path)


def run_persisted_weekly_triangle_setup_stability(
    directory: str | Path,
    *,
    development_windows: tuple[str, ...],
    validation_windows: tuple[str, ...],
    **kwargs: Any,
) -> dict[str, Any]:
    target = Path(directory)
    windows = (*development_windows, *validation_windows)
    event_frames: dict[str, pd.DataFrame] = {}
    for window in windows:
        path = target / f"{window}_events.parquet"
        if not path.exists():
            raise FileNotFoundError(path)
        event_frames[window] = pd.read_parquet(path)
    result = analyze_weekly_triangle_setup_stability(
        event_frames,
        development_windows=development_windows,
        validation_windows=validation_windows,
        **kwargs,
    )
    result["artifact_path"] = persist_weekly_triangle_setup_stability(result, target)
    return result


def _eligible_outcome_frame(frame: pd.DataFrame) -> pd.DataFrame:
    source = frame[
        (frame["stage"] == "BREAKOUT")
        & frame["entry_allowed"].fillna(False)
        & frame["net_return"].notna()
        & frame["net_return_2x"].notna()
    ].copy()
    if {"upper_touches", "lower_touches"}.issubset(source.columns):
        source["touches"] = source["upper_touches"] + source["lower_touches"]
    if "median_amount_4w" in source:
        source["log_median_amount_4w"] = np.log1p(source["median_amount_4w"])
    return source


def _prepare_setup_episodes(
    frame: pd.DataFrame,
    *,
    episode_gap_days: int,
    conversion_days: int,
) -> pd.DataFrame:
    required = {"code", "asof", "stage", "score"}
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(
            columns=[*frame.columns, "asof_dt", "converted_4w"]
        )
    source = frame.copy()
    source["asof_dt"] = pd.to_datetime(source["asof"], errors="coerce")
    source = source.dropna(subset=["asof_dt"])
    setups = source[source["stage"] == "SETUP"].copy()
    setups = setups.sort_values(["code", "asof_dt"], kind="mergesort")
    gaps = setups.groupby("code", sort=False)["asof_dt"].diff()
    episode_start = gaps.isna() | (gaps > pd.Timedelta(days=episode_gap_days))
    episodes = setups.loc[episode_start].copy()
    if source.empty:
        episodes["converted_4w"] = False
        return episodes
    cutoff = source["asof_dt"].max() - pd.Timedelta(days=conversion_days)
    episodes = episodes[episodes["asof_dt"] <= cutoff].copy()
    breakouts = source[source["stage"] == "BREAKOUT"][
        ["code", "asof_dt"]
    ].rename(columns={"asof_dt": "breakout_dt"})
    if episodes.empty or breakouts.empty:
        episodes["converted_4w"] = False
    else:
        pairs = episodes[["code", "asof_dt"]].merge(breakouts, on="code")
        pairs = pairs[
            (pairs["breakout_dt"] > pairs["asof_dt"])
            & (
                pairs["breakout_dt"]
                <= pairs["asof_dt"] + pd.Timedelta(days=conversion_days)
            )
        ]
        converted = set(zip(pairs["code"], pairs["asof_dt"]))
        episodes["converted_4w"] = [
            (code, asof) in converted
            for code, asof in zip(episodes["code"], episodes["asof_dt"])
        ]
    if {"upper_touches", "lower_touches"}.issubset(episodes.columns):
        episodes["touches"] = (
            episodes["upper_touches"] + episodes["lower_touches"]
        )
    if "median_amount_4w" in episodes:
        episodes["log_median_amount_4w"] = np.log1p(
            episodes["median_amount_4w"]
        )
    return episodes


def _setup_conversion_metrics(
    frame: pd.DataFrame,
    feature: str,
    *,
    ascending: bool,
    maximum_setups: int,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if frame.empty or feature not in frame:
        return {
            "count": 0,
            "cohorts": 0,
            "conversion_rate": None,
            "baseline_rate": None if baseline is None else baseline["conversion_rate"],
            "lift": None,
        }
    source = frame.dropna(subset=[feature])
    selected = source.sort_values(
        ["asof_dt", feature, "code"],
        ascending=[True, ascending, True],
        kind="mergesort",
    ).groupby("asof_dt", sort=False).head(maximum_setups)
    conversion_rate = (
        float(selected["converted_4w"].mean()) if not selected.empty else None
    )
    baseline_rate = (
        float(baseline["conversion_rate"])
        if baseline is not None and baseline["conversion_rate"] is not None
        else conversion_rate
    )
    return {
        "count": int(len(selected)),
        "cohorts": int(selected["asof_dt"].nunique()),
        "conversion_rate": conversion_rate,
        "baseline_rate": baseline_rate,
        "lift": (
            float(conversion_rate - baseline_rate)
            if conversion_rate is not None and baseline_rate is not None
            else None
        ),
    }


def _setup_followthrough_metrics(
    event_frame: pd.DataFrame,
    episodes: pd.DataFrame,
    feature: str,
    *,
    ascending: bool,
    maximum_setups: int,
    maximum_entries: int,
    conversion_days: int,
) -> dict[str, Any]:
    eligible = _eligible_outcome_frame(event_frame)
    if eligible.empty:
        empty = _trade_metrics(eligible)
        return {"selected": empty, "baseline": empty, "mean_2x_lift": None}
    eligible = eligible.copy()
    eligible["asof_dt"] = pd.to_datetime(eligible["asof"], errors="coerce")
    baseline = eligible.sort_values(
        ["asof_dt", "score", "code"],
        ascending=[True, False, True],
        kind="mergesort",
    ).groupby("asof_dt", sort=False).head(maximum_entries)
    if episodes.empty or feature not in episodes:
        return {
            "selected": _trade_metrics(eligible.iloc[:0]),
            "baseline": _trade_metrics(baseline),
            "mean_2x_lift": None,
        }
    setup_selected = episodes.dropna(subset=[feature]).sort_values(
        ["asof_dt", feature, "code"],
        ascending=[True, ascending, True],
        kind="mergesort",
    ).groupby("asof_dt", sort=False).head(maximum_setups)
    setup_keys = setup_selected[["code", "asof_dt"]].rename(
        columns={"asof_dt": "setup_dt"}
    )
    pairs = eligible[["code", "asof_dt"]].rename(
        columns={"asof_dt": "breakout_dt"}
    ).merge(setup_keys, on="code")
    pairs = pairs[
        (pairs["setup_dt"] < pairs["breakout_dt"])
        & (
            pairs["setup_dt"]
            >= pairs["breakout_dt"] - pd.Timedelta(days=conversion_days)
        )
    ]
    linked = set(zip(pairs["code"], pairs["breakout_dt"]))
    filtered = eligible[
        [
            (code, asof) in linked
            for code, asof in zip(eligible["code"], eligible["asof_dt"])
        ]
    ]
    selected = filtered.sort_values(
        ["asof_dt", "score", "code"],
        ascending=[True, False, True],
        kind="mergesort",
    ).groupby("asof_dt", sort=False).head(maximum_entries)
    selected_metrics = _trade_metrics(selected)
    baseline_metrics = _trade_metrics(baseline)
    return {
        "selected": selected_metrics,
        "baseline": baseline_metrics,
        "mean_2x_lift": (
            float(selected_metrics["mean_2x"] - baseline_metrics["mean_2x"])
            if selected_metrics["mean_2x"] is not None
            and baseline_metrics["mean_2x"] is not None
            else None
        ),
    }


def _trade_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "count": 0,
            "cohorts": 0,
            "mean": None,
            "mean_2x": None,
            "median": None,
            "win_rate": None,
        }
    return {
        "count": int(len(frame)),
        "cohorts": int(frame["asof_dt"].nunique()),
        "mean": float(frame["net_return"].mean()),
        "mean_2x": float(frame["net_return_2x"].mean()),
        "median": float(frame["net_return"].median()),
        "win_rate": float((frame["net_return"] > 0).mean()),
    }


def _trade_gate(metrics: dict[str, Any], minimum_samples: int) -> bool:
    return bool(
        int(metrics["count"]) >= minimum_samples
        and metrics["mean_2x"] is not None
        and float(metrics["mean_2x"]) > 0
        and metrics["median"] is not None
        and float(metrics["median"]) > 0
    )


def _feature_selection_metrics(
    frame: pd.DataFrame,
    feature: str,
    *,
    ascending: bool,
    maximum_entries: int,
) -> dict[str, float | int | None]:
    if feature not in frame:
        return {
            "count": 0,
            "mean": None,
            "mean_2x": None,
            "median": None,
            "win_rate": None,
        }
    source = frame.dropna(subset=[feature]).sort_values(
        ["asof", feature, "code"],
        ascending=[True, ascending, True],
        kind="mergesort",
    )
    selected = source.groupby("asof", sort=False).head(maximum_entries)
    if selected.empty:
        return {
            "count": 0,
            "mean": None,
            "mean_2x": None,
            "median": None,
            "win_rate": None,
        }
    return {
        "count": int(len(selected)),
        "mean": float(selected["net_return"].mean()),
        "mean_2x": float(selected["net_return_2x"].mean()),
        "median": float(selected["net_return"].median()),
        "win_rate": float((selected["net_return"] > 0).mean()),
    }


def _point_in_time_features(
    weekly: pd.DataFrame,
    analysis: dict[str, Any],
) -> dict[str, float | int | None]:
    close = pd.to_numeric(weekly["Close"], errors="coerce")
    returns = close.pct_change(fill_method=None).dropna()
    upper = float(analysis["upper_boundary"])
    lower = float(analysis["lower_boundary"])
    width = max(upper - lower, 1e-12)
    current = float(analysis["close"])
    amount = (
        pd.to_numeric(weekly["Amount"], errors="coerce").dropna()
        if "Amount" in weekly
        else pd.Series(dtype=float)
    )
    return {
        "width_ratio": float(analysis["width_ratio"]),
        "range_contraction": float(analysis["range_contraction"]),
        "upper_slope_pct": float(analysis["upper_slope_pct"]),
        "lower_slope_pct": float(analysis["lower_slope_pct"]),
        "upper_touches": int(analysis["upper_touches"]),
        "lower_touches": int(analysis["lower_touches"]),
        "apex_weeks": float(analysis["apex_weeks"]),
        "breakout_extension": float(current / upper - 1.0) if upper > 0 else None,
        "price_location": float((current - lower) / width),
        "close_to_ma30": float(current / float(analysis["ma30"]) - 1.0),
        "ma5_to_ma30": float(float(analysis["ma5"]) / float(analysis["ma30"]) - 1.0),
        "prior_return_4w": _period_return(close, 4),
        "prior_return_12w": _period_return(close, 12),
        "weekly_volatility_8w": (
            float(returns.tail(8).std(ddof=0)) if len(returns.tail(8)) >= 4 else None
        ),
        "median_amount_4w": float(amount.tail(4).median()) if not amount.empty else None,
    }


def _period_return(close: pd.Series, periods: int) -> float | None:
    if len(close) <= periods:
        return None
    current = float(close.iloc[-1])
    previous = float(close.iloc[-periods - 1])
    if not np.isfinite(current) or not np.isfinite(previous) or previous <= 0:
        return None
    return float(current / previous - 1.0)


def _simulate_trade(
    strategy: WeeklyTriangleStrategy,
    code: str,
    event_day: pd.Timestamp,
    analysis: dict[str, Any],
    front: pd.DataFrame,
    raw: pd.DataFrame | None,
    costs: PortfolioConfig,
    cost_multiplier: float,
) -> dict[str, Any]:
    if raw is None or raw.empty:
        return {}
    raw = raw.sort_index()
    raw_days = pd.DatetimeIndex(raw.index)
    if raw_days.tz is not None:
        raw_days = raw_days.tz_localize(None)
    signal_visible = raw.loc[raw_days.normalize() <= event_day]
    future = raw.loc[raw_days.normalize() > event_day]
    if signal_visible.empty or len(future) < 2:
        return {}
    raw_signal_close = float(pd.to_numeric(signal_visible["Close"], errors="coerce").iloc[-1])
    front_signal_close = float(analysis["close"])
    adjustment_ratio = (
        raw_signal_close / front_signal_close if front_signal_close > 0 else 1.0
    )
    technical_stop = max(
        float(analysis["lower_boundary"]) * 0.98,
        float(analysis["ma20"]) * 0.97,
    ) * adjustment_ratio
    stop_price = min(
        raw_signal_close * 0.98,
        max(
            raw_signal_close * (1.0 - strategy.parameters.fixed_stop_ratio),
            technical_stop,
        ),
    )
    entry_day = pd.Timestamp(future.index[0])
    entry_open = float(pd.to_numeric(future["Open"], errors="coerce").iloc[0])
    slippage = costs.slippage_rate * cost_multiplier
    commission = costs.commission_rate * cost_multiplier
    min_commission = costs.min_commission * cost_multiplier
    stamp_duty = costs.stamp_duty_rate * cost_multiplier
    entry_execution = entry_open * (1.0 + slippage)
    target_cash = 100_000.0 * strategy.parameters.target_weight
    quantity = int(target_cash // (entry_execution * costs.board_lot)) * costs.board_lot
    if quantity <= 0:
        return {}
    entry_value = entry_execution * quantity
    entry_fee = max(min_commission, entry_value * commission)
    position = {
        "code": code,
        "stop_price": stop_price,
        "entry_time": entry_day.date().isoformat(),
        "average_price": entry_execution,
    }
    future_index = list(future.index)
    for offset, current_index in enumerate(future_index[:-1]):
        current_day = pd.Timestamp(current_index)
        visible_raw = raw.loc[:current_index]
        visible_front = front.loc[:current_index]
        signal = strategy._exit_signal(
            "weekly_triangle_event_study",
            code,
            position,
            visible_front,
            visible_raw,
            current_day,
        )
        if signal is None:
            continue
        exit_index = future_index[offset + 1]
        exit_open = float(pd.to_numeric(raw.loc[exit_index, "Open"], errors="coerce"))
        exit_execution = exit_open * (1.0 - slippage)
        exit_value = exit_execution * quantity
        exit_fee = max(min_commission, exit_value * commission) + exit_value * stamp_duty
        net_return = (exit_value - exit_fee - entry_value - entry_fee) / (
            entry_value + entry_fee
        )
        return {
            "entry_date": entry_day.date().isoformat(),
            "entry_gap": float(entry_open / raw_signal_close - 1.0),
            "exit_date": pd.Timestamp(exit_index).date().isoformat(),
            "exit_reason": signal.reason_codes[0],
            "holding_days": int(signal.evidence.get("holding_days") or 0),
            "gross_return": float(exit_open / entry_open - 1.0),
            "net_return": float(net_return),
        }
    return {}


def _rank_breakout_events(
    events: list[WeeklyTriangleEvent],
    maximum_entries: int,
) -> list[WeeklyTriangleEvent]:
    by_day: dict[str, list[WeeklyTriangleEvent]] = {}
    for event in events:
        if event.stage == "BREAKOUT" and event.entry_allowed:
            by_day.setdefault(event.asof, []).append(event)
    ranked: dict[tuple[str, str], tuple[int, int]] = {}
    for asof, items in by_day.items():
        ordered = sorted(items, key=lambda item: (-item.score, item.code))
        count = len(ordered)
        for rank, event in enumerate(ordered, start=1):
            ranked[(event.code, asof)] = (rank, count)
    result: list[WeeklyTriangleEvent] = []
    for event in events:
        rank = ranked.get((event.code, event.asof))
        if rank is None:
            result.append(event)
            continue
        result.append(
            replace(
                event,
                entry_selected=rank[0] <= maximum_entries,
                cross_section_rank=rank[0],
                cross_section_count=rank[1],
            )
        )
    return result


def _entry_diagnostics(
    eligible: list[WeeklyTriangleEvent],
    selected: list[WeeklyTriangleEvent],
) -> dict[str, Any]:
    rank_limits = (3, 5, 10, 20)
    gap_ranges = ((-0.03, 0.03), (-0.03, 0.05), (-0.03, 0.08))
    return {
        "rank_limits": {
            str(limit): _dual_cost_distribution(
                [
                    event
                    for event in eligible
                    if event.cross_section_rank is not None
                    and event.cross_section_rank <= limit
                ]
            )
            for limit in rank_limits
        },
        "entry_gap_ranges": {
            f"{lower:.2f}:{upper:.2f}": _dual_cost_distribution(
                [
                    event
                    for event in selected
                    if event.entry_gap is not None
                    and lower <= event.entry_gap <= upper
                ]
            )
            for lower, upper in gap_ranges
        },
    }


def _dual_cost_distribution(
    events: list[WeeklyTriangleEvent],
) -> dict[str, Any]:
    return {
        "net_return": _distribution(events, "net_return"),
        "net_return_2x": _distribution(events, "net_return_2x"),
    }


def _exit_reason_counts(events: list[WeeklyTriangleEvent]) -> dict[str, int]:
    result: dict[str, int] = {}
    for event in events:
        if event.exit_reason:
            result[event.exit_reason] = result.get(event.exit_reason, 0) + 1
    return dict(sorted(result.items()))


def _distribution(
    events: list[WeeklyTriangleEvent],
    field: str,
) -> dict[str, float | int | None]:
    values = pd.Series(
        [getattr(event, field) for event in events],
        dtype=float,
    ).dropna()
    if values.empty:
        return {"count": 0, "mean": None, "median": None, "win_rate": None}
    return {
        "count": int(len(values)),
        "mean": float(values.mean()),
        "median": float(values.median()),
        "win_rate": float((values > 0).mean()),
    }


def _weekly_market_state(
    market_index: pd.DataFrame | None,
    end_day: pd.Timestamp,
    ma_period: int,
) -> pd.DataFrame:
    if market_index is None or market_index.empty:
        return pd.DataFrame()
    weekly = resample_weekly_bars(market_index)
    if weekly.empty:
        return weekly
    weekly = weekly.loc[pd.to_datetime(weekly["WeekEnd"]) <= end_day].copy()
    weekly["MarketMA"] = pd.to_numeric(weekly["Close"], errors="coerce").rolling(
        ma_period
    ).mean()
    weekly["Allowed"] = pd.to_numeric(weekly["Close"], errors="coerce") >= weekly[
        "MarketMA"
    ]
    return weekly


def _market_allowed(market_state: pd.DataFrame, event_day: pd.Timestamp) -> bool | None:
    if market_state.empty:
        return None
    visible = market_state.loc[:event_day]
    if visible.empty or not np.isfinite(float(visible["MarketMA"].iloc[-1])):
        return False
    return bool(visible["Allowed"].iloc[-1])
