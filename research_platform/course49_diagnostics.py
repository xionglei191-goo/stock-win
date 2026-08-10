from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from strategy_v1.portfolio import price_limit_ratio

from .config import PlatformConfig
from .storage import Database
from .strategies.course49 import build_course49_market_matrix


HORIZONS = (1, 3, 5)
CORE_CONFIRMATIONS = frozenset({"EARLY_SEAL", "STRONG_SEAL", "AUCTION_STRENGTH"})


def build_execution_event_table(
    raw_bars: pd.DataFrame,
    limit_events: pd.DataFrame,
    market_states: pd.DataFrame,
    *,
    commission_rate: float = 0.0003,
    stamp_duty_rate: float = 0.0005,
    slippage_rate: float = 0.001,
) -> pd.DataFrame:
    """Build a close-to-next-open event study using only point-in-time labels.

    Signal features are taken from ``event_date``. Entry is the next trading-day
    open; events opening at the daily upper limit are marked untradable. Returns
    report both the event-study close and the platform-equivalent next-open exit
    after 1, 3, or 5 observed holding days. Both include slippage, commission,
    and sell-side stamp duty.
    """

    required_bars = {"code", "timestamp", "Open", "Close"}
    required_events = {"code", "event_date", "limit_event"}
    required_states = {"timestamp", "market_phase"}
    for required, columns, label in (
        (required_bars, set(raw_bars.columns), "raw_bars"),
        (required_events, set(limit_events.columns), "limit_events"),
        (required_states, set(market_states.columns), "market_states"),
    ):
        missing = required - columns
        if missing:
            raise ValueError(f"{label} is missing columns: {sorted(missing)}")

    bars = raw_bars.loc[:, ["code", "timestamp", "Open", "Close"]].copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce").dt.normalize()
    bars["Open"] = pd.to_numeric(bars["Open"], errors="coerce")
    bars["Close"] = pd.to_numeric(bars["Close"], errors="coerce")
    bars = bars.dropna(subset=["code", "timestamp", "Open", "Close"])
    bars = bars.sort_values(["code", "timestamp"]).drop_duplicates(
        ["code", "timestamp"], keep="last"
    )
    bars["previous_close"] = bars.groupby("code", sort=False)["Close"].shift(1)
    bars["signal_return"] = bars["Close"] / bars["previous_close"] - 1.0
    bars["limit_ratio"] = bars["code"].map(lambda code: price_limit_ratio(str(code)))

    streaks = pd.Series(0, index=bars.index, dtype="int64")
    for _, group in bars.groupby("code", sort=False):
        count = 0
        values: list[int] = []
        for signal_return, ratio in zip(group["signal_return"], group["limit_ratio"]):
            if pd.notna(signal_return) and float(signal_return) >= float(ratio) - 0.002:
                count += 1
            else:
                count = 0
            values.append(count)
        streaks.loc[group.index] = values
    bars["streak"] = streaks

    grouped = bars.groupby("code", sort=False)
    bars["entry_open"] = grouped["Open"].shift(-1)
    bars["next_open_return"] = bars["entry_open"] / bars["Close"] - 1.0
    for horizon in HORIZONS:
        bars[f"exit_close_{horizon}d"] = grouped["Close"].shift(-horizon)
        # A sell signal generated after the Nth holding-day close executes at
        # the following open, matching the portfolio simulator.
        bars[f"exit_open_{horizon}d"] = grouped["Open"].shift(-(horizon + 1))

    events = limit_events.loc[limit_events["limit_event"].fillna(False).astype(bool)].copy()
    events["event_date"] = pd.to_datetime(events["event_date"], errors="coerce").dt.normalize()
    events = events.sort_values(["code", "event_date"]).drop_duplicates(
        ["code", "event_date"], keep="last"
    )
    event_columns = [
        "code",
        "event_date",
        "listed",
        "risk",
        "score",
        "board_quality_score",
        "board_risk",
        "board_confirmations",
    ]
    events = events.loc[:, [column for column in event_columns if column in events.columns]]

    state_columns = [
        "timestamp",
        "market_phase",
        "market_style",
        "suitability",
        "trade_mode",
        "entry_allowed",
    ]
    states = market_states.loc[:, [column for column in state_columns if column in market_states.columns]].copy()
    states["timestamp"] = pd.to_datetime(states["timestamp"], errors="coerce").dt.normalize()
    states = states.drop_duplicates("timestamp", keep="last")

    result = events.merge(
        bars,
        left_on=["code", "event_date"],
        right_on=["code", "timestamp"],
        how="inner",
        validate="one_to_one",
    ).merge(states, on="timestamp", how="inner", validate="many_to_one")
    # ST events close near 5%; the snapshot does not currently persist names.
    # Requiring an 8% signal move removes those events without consulting future data.
    result = result.loc[result["signal_return"] >= 0.08].copy()
    result["next_open_tradable"] = (
        result["entry_open"].notna()
        & (result["next_open_return"] < result["limit_ratio"] - 0.002)
    )
    result["streak_group"] = result["streak"].map(_streak_group)
    result["limit_regime"] = result["limit_ratio"].map(
        lambda value: f"{int(round(float(value) * 100))}%"
    )
    confirmations = result.get(
        "board_confirmations", pd.Series([[]] * len(result), index=result.index)
    ).map(_as_string_set)
    quality = pd.to_numeric(result.get("board_quality_score", 0.0), errors="coerce").fillna(0.0)
    capital_risk = result.get("risk", pd.Series("", index=result.index)).fillna("").astype(str)
    result["v3_rule_eligible"] = (
        result["next_open_tradable"]
        & result["market_phase"].eq("ACCELERATION")
        & result["limit_ratio"].eq(0.10)
        & result["streak"].ge(2)
        & (
            (result["streak"].ge(4) & quality.ge(0.65))
            | (result["streak"].isin([2, 3]) & quality.ge(0.75))
        )
        & confirmations.map(lambda values: bool(values & CORE_CONFIRMATIONS))
        & capital_risk.eq("")
    )

    entry_multiplier = (1.0 + slippage_rate) * (1.0 + commission_rate)
    exit_multiplier = (1.0 - slippage_rate) * (
        1.0 - commission_rate - stamp_duty_rate
    )
    for horizon in HORIZONS:
        result[f"net_return_{horizon}d"] = (
            result[f"exit_close_{horizon}d"] * exit_multiplier
            / (result["entry_open"] * entry_multiplier)
            - 1.0
        )
        result[f"execution_net_return_{horizon}d"] = (
            result[f"exit_open_{horizon}d"] * exit_multiplier
            / (result["entry_open"] * entry_multiplier)
            - 1.0
        )
        result.loc[
            ~result["next_open_tradable"], f"net_return_{horizon}d"
        ] = pd.NA
        result.loc[
            ~result["next_open_tradable"], f"execution_net_return_{horizon}d"
        ] = pd.NA
    return result.sort_values(["event_date", "code"]).reset_index(drop=True)


def summarize_events(events: pd.DataFrame, dimensions: Iterable[str]) -> list[dict[str, Any]]:
    dimensions = list(dimensions)
    if events.empty:
        return []
    rows: list[dict[str, Any]] = []
    grouped: Any = events.groupby(dimensions, dropna=False, sort=True) if dimensions else [((), events)]
    for key, frame in grouped:
        keys = key if isinstance(key, tuple) else (key,)
        row = {dimension: str(value) for dimension, value in zip(dimensions, keys)}
        for horizon in HORIZONS:
            values = pd.to_numeric(
                frame[f"execution_net_return_{horizon}d"], errors="coerce"
            ).dropna()
            row[f"n_{horizon}d"] = int(len(values))
            row[f"net_{horizon}d_pct"] = round(float(values.mean() * 100.0), 4) if len(values) else None
            row[f"win_{horizon}d_pct"] = round(float((values > 0).mean() * 100.0), 2) if len(values) else None
        rows.append(row)
    return rows


def diagnose_backtest(
    config: PlatformConfig,
    database: Database,
    backtest_id: str,
    *,
    state_strategy_id: str | None = None,
    scope: str = "snapshot",
    output_dir: Path | None = None,
) -> dict[str, Any]:
    backtests = database.query("SELECT * FROM backtests WHERE backtest_id=?", (backtest_id,))
    if not backtests:
        raise ValueError(f"Unknown backtest: {backtest_id}")
    backtest = backtests[0]
    snapshot_id = str(backtest.get("snapshot_id") or "")
    if not snapshot_id:
        raise ValueError(f"Backtest {backtest_id} has no snapshot")
    snapshot_dir = config.snapshot_dir / snapshot_id
    raw_path = snapshot_dir / "daily_raw.parquet"
    event_path = snapshot_dir / "limit_behavior.parquet"
    for path in (raw_path, event_path):
        if not path.exists():
            raise ValueError(f"Snapshot dataset is missing: {path}")

    raw_bars = pd.read_parquet(raw_path)
    limit_events = pd.read_parquet(event_path)
    if scope == "snapshot":
        activity_path = snapshot_dir / "market_activity.parquet"
        if not activity_path.exists():
            raise ValueError(f"Snapshot dataset is missing: {activity_path}")
        states = _snapshot_market_states(raw_bars, pd.read_parquet(activity_path))
        selected_state_id = "course49_market_matrix"
    elif scope == "backtest":
        available_states = database.query(
            "SELECT DISTINCT strategy_id FROM backtest_states WHERE backtest_id=?",
            (backtest_id,),
        )
        available_ids = [str(row["strategy_id"]) for row in available_states]
        selected_state_id = state_strategy_id or _select_state_strategy(
            str(backtest.get("strategy_id") or ""), available_ids
        )
        state_rows = database.query(
            """SELECT timestamp, market_phase, market_style, suitability, trade_mode,
                      entry_allowed
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id=? ORDER BY timestamp""",
            (backtest_id, selected_state_id),
        )
        if not state_rows:
            raise ValueError(
                f"Backtest {backtest_id} has no market states for {selected_state_id}"
            )
        states = pd.DataFrame(state_rows)
    else:
        raise ValueError("scope must be 'snapshot' or 'backtest'")
    events = build_execution_event_table(raw_bars, limit_events, states)
    tradable = events.loc[events["next_open_tradable"]].copy()
    v3_events = events.loc[events["v3_rule_eligible"]].copy()
    parameters = json.loads(str(backtest.get("parameters_json") or "{}"))
    report = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "backtest_id": backtest_id,
        "snapshot_id": snapshot_id,
        "scope": scope,
        "state_strategy_id": selected_state_id,
        "window": {
            "start_date": str(pd.to_datetime(states["timestamp"]).min().date()),
            "end_date": str(pd.to_datetime(states["timestamp"]).max().date()),
            "backtest_start_date": str(backtest.get("start_date") or ""),
            "backtest_end_date": str(backtest.get("end_date") or ""),
        },
        "methodology": {
            "signal_time": "event-day close",
            "entry": "next trading-day open when not limit-up",
            "exits": [
                "next open after 1 observed holding day",
                "next open after 3 observed holding days",
                "next open after 5 observed holding days",
            ],
            "secondary_event_study_exits": [
                "1 trading-day close",
                "3 trading-day close",
                "5 trading-day close",
            ],
            "commission_rate": 0.0003,
            "stamp_duty_rate": 0.0005,
            "slippage_rate_per_side": 0.001,
        },
        "data_quality": {
            "daily_rows": int(len(raw_bars)),
            "daily_codes": int(raw_bars["code"].nunique()),
            "daily_duplicate_keys": int(raw_bars.duplicated(["code", "timestamp"]).sum()),
            "daily_ohlcv_null_cells": int(
                raw_bars[["Open", "High", "Low", "Close", "Volume", "Amount"]]
                .isna()
                .sum()
                .sum()
            ),
            "limit_event_rows": int(len(limit_events)),
            "limit_event_duplicate_keys": int(
                limit_events.duplicated(["code", "event_date"]).sum()
            ),
            "state_days": int(states["timestamp"].nunique()),
            "sector_membership_quality": parameters.get("sector_membership_quality", "UNKNOWN"),
            "sector_membership_source": parameters.get("sector_membership_source", "UNKNOWN"),
        },
        "event_counts": {
            "matched_events": int(len(events)),
            "tradable_next_open": int(events["next_open_tradable"].sum()),
            "five_day_observations": int(tradable["net_return_5d"].notna().sum()),
            "v3_rule_eligible": int(len(v3_events)),
        },
        "summary": {
            "overall": summarize_events(tradable, []),
            "by_market_phase": summarize_events(tradable, ["market_phase"]),
            "by_streak": summarize_events(tradable, ["streak_group"]),
            "by_phase_and_streak": summarize_events(
                tradable, ["market_phase", "streak_group"]
            ),
            "by_limit_regime_and_streak": summarize_events(
                tradable, ["limit_regime", "streak_group"]
            ),
            "v3_rule_eligible": summarize_events(v3_events, []),
            "v3_rule_eligible_by_streak": summarize_events(
                v3_events, ["streak_group"]
            ),
        },
        "limitations": [
            "This is an event study, not a portfolio backtest; overlapping events and capital constraints are not applied.",
            "Historical sector membership is current_fallback when the snapshot reports LIMITED quality.",
            "Five-day cohorts exclude events without a complete forward window.",
        ],
    }

    target_dir = output_dir or config.runtime_dir / "research"
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = f"course49_reward_{backtest_id}_{scope}"
    json_path = target_dir / f"{stem}.json"
    event_path_out = target_dir / f"{stem}_events.csv"
    report["outputs"] = {"report": str(json_path), "events": str(event_path_out)}
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    export_columns = [
        "code",
        "event_date",
        "market_phase",
        "market_style",
        "limit_regime",
        "streak",
        "streak_group",
        "board_quality_score",
        "risk",
        "next_open_tradable",
        "v3_rule_eligible",
        "net_return_1d",
        "net_return_3d",
        "net_return_5d",
        "execution_net_return_1d",
        "execution_net_return_3d",
        "execution_net_return_5d",
    ]
    events.loc[:, [column for column in export_columns if column in events.columns]].to_csv(
        event_path_out, index=False, encoding="utf-8-sig"
    )
    return report


def _snapshot_market_states(
    raw_bars: pd.DataFrame,
    market_activity: pd.DataFrame,
) -> pd.DataFrame:
    bars = raw_bars.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce")
    bars_by_code = {
        str(code): group.drop(columns="code").set_index("timestamp").sort_index()
        for code, group in bars.groupby("code", sort=False)
    }
    activity = market_activity.copy()
    if "timestamp" in activity.columns:
        activity["timestamp"] = pd.to_datetime(activity["timestamp"], errors="coerce")
        activity = activity.set_index("timestamp")
    matrix = build_course49_market_matrix(bars_by_code, {}, activity.sort_index())
    if matrix.empty:
        raise ValueError("Could not reconstruct market states from the snapshot")
    return matrix.reset_index().rename(columns={"phase": "market_phase"})


def _select_state_strategy(backtest_strategy_id: str, available: list[str]) -> str:
    if backtest_strategy_id in available:
        return backtest_strategy_id
    for strategy_id in ("course49_v3", "course49_v2", "course49_v1"):
        if strategy_id in available:
            return strategy_id
    raise ValueError("Backtest has no course49 market-state history")


def _streak_group(value: Any) -> str:
    streak = int(value or 0)
    if streak >= 4:
        return "4+"
    if streak >= 2:
        return "2-3"
    return "1"


def _as_string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    try:
        return {str(item) for item in value}
    except TypeError:
        return set()
