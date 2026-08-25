from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .config import PortfolioConfig
from .leader_pullback_research import (
    FROZEN_HYPOTHESES,
    _add_point_in_time_features,
    _merge_bar_inputs,
    _trade_net_return,
    annotate_research_context,
    simulate_event_portfolio,
)


PROTOCOL_VERSION = "1.1.0"
HYPOTHESIS_ID = "intraday_pullback_reversal"
V9_ENTRY_PROTOCOL_VERSION = "1.0.0"
V9_ENTRY_HYPOTHESIS_ID = "course49_v9_intraday_pullback_entry"
V9_STAGED_PROTOCOL_VERSION = "1.0.0"
V9_STAGED_HYPOTHESIS_ID = "course49_v9_staged_intraday_entry"
V9_SOURCE_BACKTEST_ID = "e42795364d324c08999575dbe133b4f0"
DAILY_SNAPSHOT_ID = "bt_07fae55f362a46d495484ee6bc983db9"
STATE_BACKTEST_ID = "07fae55f362a46d495484ee6bc983db9"
LC5_DTYPE = np.dtype(
    [
        ("encoded_date", "<u2"),
        ("minute", "<u2"),
        ("Open", "<f4"),
        ("High", "<f4"),
        ("Low", "<f4"),
        ("Close", "<f4"),
        ("Amount", "<f4"),
        ("Volume", "<u4"),
        ("reserved", "<u4"),
    ]
)


@dataclass(frozen=True)
class IntradayWindow:
    label: str
    role: str
    start_date: str
    end_date: str


RESEARCH_WINDOWS = (
    IntradayWindow("dev_2026_a", "DEVELOPMENT", "2026-01-05", "2026-03-20"),
    IntradayWindow("dev_2026_b", "DEVELOPMENT", "2026-03-23", "2026-05-15"),
    IntradayWindow("replication_2026", "REPLICATION", "2026-05-18", "2026-06-26"),
    IntradayWindow("validation_2026", "VALIDATION", "2026-06-29", "2026-07-17"),
    IntradayWindow("holdout_2026", "HOLDOUT", "2026-07-20", "2026-08-07"),
)


def decode_lc5_bytes(data: bytes, code: str) -> pd.DataFrame:
    """Decode TongdaXin LC5 records without mutating the source file."""

    if len(data) % LC5_DTYPE.itemsize:
        raise ValueError("LC5 payload size must be a multiple of 32 bytes")
    records = np.frombuffer(data, dtype=LC5_DTYPE)
    if not len(records):
        return _empty_intraday_bars()
    encoded = records["encoded_date"].astype(np.int64)
    month_day = encoded % 2048
    dates = pd.to_datetime(
        {
            "year": encoded // 2048 + 2004,
            "month": month_day // 100,
            "day": month_day % 100,
        },
        errors="coerce",
    )
    timestamps = dates + pd.to_timedelta(
        records["minute"].astype(np.int64), unit="m"
    )
    frame = pd.DataFrame(
        {
            "code": str(code),
            "timestamp": timestamps,
            "Open": records["Open"].astype(float),
            "High": records["High"].astype(float),
            "Low": records["Low"].astype(float),
            "Close": records["Close"].astype(float),
            "Volume": records["Volume"].astype(float),
            "Amount": records["Amount"].astype(float),
        }
    )
    return frame.dropna(subset=["timestamp"]).reset_index(drop=True)


def load_lc5_file(path: Path, code: str) -> tuple[pd.DataFrame, str]:
    data = path.read_bytes()
    return decode_lc5_bytes(data, code), hashlib.sha256(data).hexdigest()


def build_daily_watchlist(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build the prior-close watchlist used by the intraday protocol."""

    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_point_in_time_features(frame, FROZEN_HYPOTHESES)
    grouped = frame.groupby("code", sort=False)
    frame["ma60"] = grouped["adj_close"].transform(
        lambda values: values.rolling(60, min_periods=60).mean()
    )
    frame["return_60d"] = frame["adj_close"] / grouped["adj_close"].shift(60) - 1.0
    frame["return_5d"] = frame["adj_close"] / grouped["adj_close"].shift(5) - 1.0
    not_at_price_limit = (
        frame["raw_signal_return"].lt(frame["limit_ratio"] - 0.002)
        & frame["raw_signal_return"].gt(-frame["limit_ratio"] + 0.002)
    )
    eligible = (
        frame["return_60d"].between(0.05, 0.50)
        & frame["ma20"].gt(frame["ma60"])
        & frame["adj_close"].gt(frame["ma60"])
        & frame["return_5d"].between(-0.12, 0.0)
        & frame["turnover_20d"].ge(50_000_000.0)
        & frame["raw_close"].ge(3.0)
        & frame["entry_date"].notna()
        & not_at_price_limit
        & ~frame["name"].str.upper().str.contains("ST", regex=False, na=False)
    ).fillna(False)
    if not eligible.any():
        return pd.DataFrame()
    columns = [
        "code",
        "name",
        "timestamp",
        "raw_close",
        "limit_ratio",
        "turnover_20d",
        "entry_date",
        "exit_open_1d",
        "exit_date_1d",
        "exit_open_3d",
        "exit_date_3d",
        "exit_open_5d",
        "exit_date_5d",
    ]
    watchlist = frame.loc[eligible, columns].copy()
    for field in ("return_60d", "return_5d", "ma20", "ma60"):
        watchlist[field] = frame.loc[eligible, field].to_numpy()
    watchlist.rename(columns={"timestamp": "signal_date"}, inplace=True)
    watchlist = annotate_research_context(
        watchlist,
        market_states=market_states,
        sector_membership=None,
    )
    return watchlist.sort_values(["entry_date", "code"]).reset_index(drop=True)


def create_intraday_snapshot(
    watchlist: pd.DataFrame,
    *,
    lc5_root: Path,
    output_dir: Path,
    daily_snapshot_id: str = DAILY_SNAPSHOT_ID,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Materialize only watchlist-date LC5 rows and hash every source file."""

    output_dir.mkdir(parents=True, exist_ok=True)
    if watchlist.empty:
        raise ValueError("watchlist must not be empty")
    targets = watchlist.copy()
    targets["entry_date"] = pd.to_datetime(targets["entry_date"]).dt.normalize()
    date_map = {
        str(code): set(group["entry_date"].dt.date)
        for code, group in targets.groupby("code", sort=True)
    }
    frames: list[pd.DataFrame] = []
    sources: list[dict[str, Any]] = []
    missing: list[str] = []
    for code in sorted(date_map):
        path = lc5_path_for_code(lc5_root, code)
        if not path.exists():
            missing.append(code)
            continue
        before = path.stat()
        bars, digest = load_lc5_file(path, code)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"LC5 source changed while reading: {path}")
        session_dates = bars["timestamp"].dt.date
        selected = bars.loc[session_dates.isin(date_map[code])]
        if not selected.empty:
            frames.append(selected)
        sources.append(
            {
                "code": code,
                "path": str(path.resolve()),
                "bytes": int(before.st_size),
                "mtime_ns": int(before.st_mtime_ns),
                "sha256": digest,
                "selected_rows": int(len(selected)),
            }
        )
    intraday = (
        pd.concat(frames, ignore_index=True)
        if frames
        else _empty_intraday_bars()
    )
    intraday.sort_values(["code", "timestamp"], inplace=True)
    intraday.reset_index(drop=True, inplace=True)
    source_payload = {
        "protocol_version": PROTOCOL_VERSION,
        "daily_snapshot_id": daily_snapshot_id,
        "source_files": sources,
        "missing_codes": missing,
    }
    snapshot_id = hashlib.sha256(
        json.dumps(source_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    bars_path = output_dir / "intraday_pullback_2026_bars.parquet"
    watchlist_path = output_dir / "intraday_pullback_2026_watchlist.parquet"
    intraday.to_parquet(bars_path, index=False)
    targets.to_parquet(watchlist_path, index=False)
    manifest = {
        **source_payload,
        "snapshot_id": snapshot_id,
        "bars_path": str(bars_path.resolve()),
        "bars_sha256": _file_sha256(bars_path),
        "bars_rows": int(len(intraday)),
        "watchlist_path": str(watchlist_path.resolve()),
        "watchlist_sha256": _file_sha256(watchlist_path),
        "watchlist_rows": int(len(targets)),
        "codes": int(targets["code"].nunique()),
        "start_date": str(targets["entry_date"].min().date()),
        "end_date": str(targets["entry_date"].max().date()),
    }
    (output_dir / "intraday_pullback_2026_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return intraday, manifest


def build_intraday_reversal_events(
    watchlist: pd.DataFrame,
    intraday_bars: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Apply the frozen intraday reversal confirmation and next-bar entry."""

    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if watchlist.empty or intraday_bars.empty:
        return pd.DataFrame()
    watch = watchlist.copy()
    watch["entry_date"] = pd.to_datetime(watch["entry_date"]).dt.normalize()
    bars = intraday_bars.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce")
    bars.dropna(subset=["timestamp"], inplace=True)
    bars["entry_date"] = bars["timestamp"].dt.normalize()
    merged = bars.merge(
        watch,
        on=["code", "entry_date"],
        how="inner",
        validate="many_to_one",
    ).sort_values(["code", "entry_date", "timestamp"])
    if merged.empty:
        return pd.DataFrame()
    group_keys = [merged["code"], merged["entry_date"]]
    grouped = merged.groupby(["code", "entry_date"], sort=False)
    merged["session_bars"] = grouped["timestamp"].transform("size")
    merged["first_open"] = grouped["Open"].transform("first")
    merged["session_low"] = grouped["Low"].cummin()
    merged["previous_bar_close"] = grouped["Close"].shift(1)
    merged["second_previous_bar_close"] = grouped["Close"].shift(2)
    merged["next_bar_open"] = grouped["Open"].shift(-1)
    merged["next_bar_timestamp"] = grouped["timestamp"].shift(-1)
    merged["cumulative_amount"] = grouped["Amount"].cumsum()
    merged["cumulative_volume"] = grouped["Volume"].cumsum()
    merged["cumulative_vwap"] = (
        merged["cumulative_amount"] / merged["cumulative_volume"].replace(0.0, np.nan)
    )
    merged["opening_gap"] = merged["first_open"] / merged["raw_close"] - 1.0
    merged["session_low_return"] = merged["session_low"] / merged["raw_close"] - 1.0
    merged["rebound_from_low"] = merged["Close"] / merged["session_low"] - 1.0
    merged["vwap_spread"] = merged["Close"] / merged["cumulative_vwap"] - 1.0
    merged["entry_jump"] = merged["next_bar_open"] / merged["Close"] - 1.0
    minute_of_day = merged["timestamp"].dt.hour * 60 + merged["timestamp"].dt.minute
    same_session_entry = (
        merged["next_bar_timestamp"].dt.normalize().eq(merged["entry_date"])
    )
    signal = (
        merged["session_bars"].ge(46)
        & merged["opening_gap"].between(-0.03, 0.01)
        & merged["session_low_return"].between(-0.05, -0.015)
        & merged["rebound_from_low"].ge(0.01)
        & merged["Close"].gt(merged["previous_bar_close"])
        & merged["previous_bar_close"].gt(merged["second_previous_bar_close"])
        & merged["Close"].ge(merged["cumulative_vwap"])
        & merged["Close"].le(merged["raw_close"] * 1.01)
        & minute_of_day.between(10 * 60, 14 * 60 + 30)
        & same_session_entry
        & merged["next_bar_open"].gt(0.0)
        & merged["entry_jump"].le(0.01)
        & merged["next_bar_open"].lt(
            merged["raw_close"] * (1.0 + merged["limit_ratio"] - 0.001)
        )
    ).fillna(False)
    if not signal.any():
        return pd.DataFrame()
    triggers = merged.loc[signal].groupby(
        ["code", "entry_date"], sort=False, as_index=False
    ).head(1).copy()
    triggers.rename(
        columns={
            "timestamp": "confirmation_timestamp",
            "next_bar_timestamp": "entry_timestamp",
            "next_bar_open": "entry_price",
        },
        inplace=True,
    )
    triggers["hypothesis_id"] = HYPOTHESIS_ID
    triggers["hypothesis_name"] = "Intraday pullback reversal"
    triggers["trend_quality"] = (
        (triggers["return_60d"] - 0.05) / 0.45
    ).clip(0.0, 1.0)
    triggers["pullback_quality"] = (-triggers["return_5d"] / 0.12).clip(0.0, 1.0)
    triggers["rebound_quality"] = (
        (triggers["rebound_from_low"] - 0.01) / 0.03
    ).clip(0.0, 1.0)
    triggers["vwap_quality"] = (triggers["vwap_spread"] / 0.02).clip(0.0, 1.0)
    triggers["score"] = (
        0.25 * triggers["trend_quality"]
        + 0.25 * triggers["pullback_quality"]
        + 0.35 * triggers["rebound_quality"]
        + 0.15 * triggers["vwap_quality"]
    )
    config = execution_config or PortfolioConfig()
    for horizon in (1, 3, 5):
        triggers[f"net_return_{horizon}d"] = [
            _trade_net_return(entry, exit_price, config, execution_cost_multiplier)
            for entry, exit_price in zip(
                triggers["entry_price"], triggers[f"exit_open_{horizon}d"]
            )
        ]
    triggers["executable"] = triggers["net_return_1d"].notna()
    keep = [
        "code",
        "name",
        "hypothesis_id",
        "hypothesis_name",
        "signal_date",
        "entry_date",
        "confirmation_timestamp",
        "entry_timestamp",
        "entry_price",
        "raw_close",
        "limit_ratio",
        "opening_gap",
        "session_low_return",
        "rebound_from_low",
        "vwap_spread",
        "entry_jump",
        "return_60d",
        "return_5d",
        "turnover_20d",
        "market_gate",
        "market_phase",
        "market_score",
        "market_regime",
        "score",
        "executable",
        "exit_open_1d",
        "exit_date_1d",
        "net_return_1d",
        "exit_open_3d",
        "exit_date_3d",
        "net_return_3d",
        "exit_open_5d",
        "exit_date_5d",
        "net_return_5d",
    ]
    return triggers.loc[:, keep].sort_values(
        ["entry_timestamp", "score", "code"],
        ascending=[True, False, True],
    ).reset_index(drop=True)


def select_chronological_entries(
    events: pd.DataFrame,
    *,
    maximum_positions: int = 3,
) -> pd.DataFrame:
    """Allocate capacity when signals become observable, never at day end."""

    if maximum_positions <= 0:
        raise ValueError("maximum_positions must be positive")
    if events.empty:
        return events.copy()
    ordered = events.loc[events["executable"]].sort_values(
        ["entry_timestamp", "score", "code"],
        ascending=[True, False, True],
    )
    active: list[tuple[str, pd.Timestamp]] = []
    accepted: list[pd.Series] = []
    for _, event in ordered.iterrows():
        entry_date = pd.Timestamp(event["entry_date"]).normalize()
        active = [position for position in active if position[1] > entry_date]
        active_codes = {position[0] for position in active}
        code = str(event["code"])
        if code in active_codes or len(active) >= maximum_positions:
            continue
        active.append((code, pd.Timestamp(event["exit_date_1d"]).normalize()))
        accepted.append(event)
    return (
        pd.DataFrame(accepted).reset_index(drop=True)
        if accepted
        else events.iloc[0:0].copy()
    )


def evaluate_intraday_window(
    events: pd.DataFrame,
    window: IntradayWindow,
    *,
    trading_days: int,
) -> dict[str, Any]:
    start = pd.Timestamp(window.start_date)
    end = pd.Timestamp(window.end_date)
    scoped = events.loc[pd.to_datetime(events["entry_date"]).between(start, end)]
    accepted = select_chronological_entries(scoped, maximum_positions=3)
    returns = pd.to_numeric(accepted.get("net_return_1d"), errors="coerce").dropna()
    portfolio = simulate_event_portfolio(
        accepted,
        trading_days=trading_days,
        target_weight=0.10,
        maximum_positions=3,
        holding_days=1,
    )
    return {
        "window": asdict(window),
        "raw_signals": int(len(scoped)),
        "trades": int(len(accepted)),
        "signal_days": int(scoped["entry_date"].nunique()) if not scoped.empty else 0,
        "trade_days": int(accepted["entry_date"].nunique()) if not accepted.empty else 0,
        "median_net_return_1d": float(returns.median()) if not returns.empty else None,
        "mean_net_return_1d": float(returns.mean()) if not returns.empty else None,
        "win_rate_1d": float((returns > 0).mean()) if not returns.empty else None,
        "market_gate_trade_rate": (
            float(accepted["market_gate"].fillna(False).mean())
            if not accepted.empty
            else 0.0
        ),
        **portfolio,
    }


def assess_development_windows(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for report in reports:
        if str(report["window"]["role"]).upper() != "DEVELOPMENT":
            continue
        passed = {
            "minimum_trades": int(report["portfolio_trades"]) >= 20,
            "positive_total_return": float(report["portfolio_total_return"]) > 0.0,
            "positive_median_trade": (
                report["portfolio_median_trade_return"] is not None
                and float(report["portfolio_median_trade_return"]) > 0.0
            ),
            "positive_ex_top3": float(report["portfolio_ex_top3_total_return"]) > 0.0,
            "maximum_drawdown": float(report["portfolio_realized_max_drawdown"]) >= -0.10,
        }
        checks.append(
            {
                "window": report["window"]["label"],
                "checks": passed,
                "passed": all(passed.values()),
            }
        )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "decision": "OPEN_REPLICATION" if checks and all(x["passed"] for x in checks) else "REJECT",
        "checks": checks,
        "replication_opened": bool(checks and all(x["passed"] for x in checks)),
        "validation_opened": False,
        "holdout_opened": False,
    }


def load_v9_trade_pairs(
    database_path: Path,
    *,
    backtest_id: str = V9_SOURCE_BACKTEST_ID,
    start_date: str = "2026-01-01",
    slippage_rate: float = 0.001,
) -> pd.DataFrame:
    """Load and pair V9 trades while recovering the raw open prices."""

    with sqlite3.connect(database_path) as connection:
        trades = pd.read_sql_query(
            """SELECT rowid, timestamp, code, side, quantity, price, fees, pnl,
                      reason, evidence
               FROM backtest_trades
               WHERE backtest_id=? AND timestamp>=?
               ORDER BY timestamp, rowid""",
            connection,
            params=(backtest_id, start_date),
        )
    return build_v9_trade_pairs(trades, slippage_rate=slippage_rate)


def build_v9_trade_pairs(
    trades: pd.DataFrame,
    *,
    slippage_rate: float = 0.001,
) -> pd.DataFrame:
    """Pair completed long trades without using any post-exit information at entry."""

    if not 0.0 <= slippage_rate < 1.0:
        raise ValueError("slippage_rate must be between zero and one")
    if trades.empty:
        return pd.DataFrame()
    required = {"timestamp", "code", "side", "price"}
    missing = required - set(trades.columns)
    if missing:
        raise ValueError(f"Missing trade columns: {sorted(missing)}")
    frame = trades.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame.dropna(subset=["timestamp"], inplace=True)
    order = ["timestamp"] + (["rowid"] if "rowid" in frame else [])
    frame.sort_values(order, inplace=True)
    open_trades: dict[str, dict[str, Any]] = {}
    pairs: list[dict[str, Any]] = []
    for trade in frame.to_dict("records"):
        code = str(trade["code"])
        side = str(trade["side"]).upper()
        if side == "BUY":
            if code in open_trades:
                raise ValueError(f"Overlapping V9 position for {code}")
            open_trades[code] = trade
            continue
        if side != "SELL" or code not in open_trades:
            continue
        entry = open_trades.pop(code)
        try:
            evidence = json.loads(str(entry.get("evidence") or "{}"))
        except json.JSONDecodeError:
            evidence = {}
        entry_price = float(entry["price"])
        exit_price = float(trade["price"])
        quantity = int(entry.get("quantity", 0) or 0)
        buy_fees = float(entry.get("fees", 0.0) or 0.0)
        pnl = pd.to_numeric(pd.Series([trade.get("pnl")]), errors="coerce").iloc[0]
        invested = entry_price * quantity + buy_fees
        pairs.append(
            {
                "pair_id": f"{pd.Timestamp(entry['timestamp']).date()}:{code}",
                "code": code,
                "entry_date": pd.Timestamp(entry["timestamp"]).normalize(),
                "entry_timestamp": pd.Timestamp(entry["timestamp"]),
                "raw_entry_open": entry_price / (1.0 + slippage_rate),
                "executed_entry_price": entry_price,
                "signal_close": pd.to_numeric(
                    pd.Series([evidence.get("entry_price")]), errors="coerce"
                ).iloc[0],
                "quantity": quantity,
                "buy_fees": buy_fees,
                "exit_date": pd.Timestamp(trade["timestamp"]).normalize(),
                "raw_exit_open": exit_price / (1.0 - slippage_rate),
                "executed_exit_price": exit_price,
                "sell_fees": float(trade.get("fees", 0.0) or 0.0),
                "realized_pnl": float(pnl) if pd.notna(pnl) else np.nan,
                "realized_net_return": (
                    float(pnl) / invested if pd.notna(pnl) and invested > 0 else np.nan
                ),
                "exit_reason": str(trade.get("reason", "")),
            }
        )
    if open_trades:
        raise ValueError(f"Unclosed V9 positions: {sorted(open_trades)}")
    return pd.DataFrame(pairs).sort_values(
        ["entry_date", "code"]
    ).reset_index(drop=True)


def create_v9_entry_snapshot(
    trade_pairs: pd.DataFrame,
    *,
    lc5_root: Path,
    output_dir: Path,
    source_backtest_id: str = V9_SOURCE_BACKTEST_ID,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Freeze the exact LC5 sessions used by the paired V9 entry study."""

    if trade_pairs.empty:
        raise ValueError("trade_pairs must not be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "v9_intraday_entry_protocol.json").write_text(
        json.dumps(v9_entry_protocol_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pairs = trade_pairs.copy()
    pairs["entry_date"] = pd.to_datetime(pairs["entry_date"]).dt.normalize()
    date_map = {
        str(code): set(group["entry_date"].dt.date)
        for code, group in pairs.groupby("code", sort=True)
    }
    frames: list[pd.DataFrame] = []
    sources: list[dict[str, Any]] = []
    missing: list[str] = []
    for code in sorted(date_map):
        path = lc5_path_for_code(lc5_root, code)
        if not path.exists():
            missing.append(code)
            continue
        before = path.stat()
        bars, digest = load_lc5_file(path, code)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"LC5 source changed while reading: {path}")
        selected = bars.loc[bars["timestamp"].dt.date.isin(date_map[code])].copy()
        if not selected.empty:
            frames.append(selected)
        sources.append(
            {
                "code": code,
                "path": str(path.resolve()),
                "bytes": int(before.st_size),
                "mtime_ns": int(before.st_mtime_ns),
                "sha256": digest,
                "selected_rows": int(len(selected)),
            }
        )
    intraday = pd.concat(frames, ignore_index=True) if frames else _empty_intraday_bars()
    intraday.sort_values(["code", "timestamp"], inplace=True)
    intraday.reset_index(drop=True, inplace=True)
    pairs_path = output_dir / "v9_intraday_entry_pairs.parquet"
    bars_path = output_dir / "v9_intraday_entry_bars.parquet"
    pairs.to_parquet(pairs_path, index=False)
    intraday.to_parquet(bars_path, index=False)
    source_payload = {
        "protocol_version": V9_ENTRY_PROTOCOL_VERSION,
        "source_backtest_id": source_backtest_id,
        "source_files": sources,
        "missing_codes": missing,
        "pairs_sha256": _file_sha256(pairs_path),
    }
    snapshot_id = hashlib.sha256(
        json.dumps(source_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest = {
        **source_payload,
        "snapshot_id": snapshot_id,
        "pairs_path": str(pairs_path.resolve()),
        "pair_rows": int(len(pairs)),
        "bars_path": str(bars_path.resolve()),
        "bars_sha256": _file_sha256(bars_path),
        "bars_rows": int(len(intraday)),
        "start_date": str(pairs["entry_date"].min().date()),
        "end_date": str(pairs["entry_date"].max().date()),
    }
    (output_dir / "v9_intraday_entry_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return intraday, manifest


def build_v9_intraday_entry_events(
    trade_pairs: pd.DataFrame,
    intraday_bars: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Test a frozen pullback/reclaim entry on the same V9 names and exit dates."""

    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if trade_pairs.empty or intraday_bars.empty:
        return pd.DataFrame()
    pairs = trade_pairs.copy()
    pairs["entry_date"] = pd.to_datetime(pairs["entry_date"]).dt.normalize()
    bars = intraday_bars.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce")
    bars.dropna(subset=["timestamp"], inplace=True)
    bars["entry_date"] = bars["timestamp"].dt.normalize()
    merged = bars.merge(
        pairs,
        on=["code", "entry_date"],
        how="inner",
        validate="many_to_one",
    ).sort_values(["code", "entry_date", "timestamp"])
    if merged.empty:
        return pd.DataFrame()
    grouped = merged.groupby(["code", "entry_date"], sort=False)
    merged["session_bars"] = grouped["timestamp"].transform("size")
    merged["first_open"] = grouped["Open"].transform("first")
    merged["session_low"] = grouped["Low"].cummin()
    merged["previous_bar_close"] = grouped["Close"].shift(1)
    merged["second_previous_bar_close"] = grouped["Close"].shift(2)
    merged["next_bar_open"] = grouped["Open"].shift(-1)
    merged["next_bar_timestamp"] = grouped["timestamp"].shift(-1)
    merged["cumulative_amount"] = grouped["Amount"].cumsum()
    merged["cumulative_volume"] = grouped["Volume"].cumsum()
    merged["cumulative_vwap"] = (
        merged["cumulative_amount"] / merged["cumulative_volume"].replace(0.0, np.nan)
    )
    merged["pullback_from_open"] = merged["session_low"] / merged["first_open"] - 1.0
    merged["rebound_from_low"] = merged["Close"] / merged["session_low"] - 1.0
    merged["vwap_spread"] = merged["Close"] / merged["cumulative_vwap"] - 1.0
    merged["entry_jump"] = merged["next_bar_open"] / merged["Close"] - 1.0
    minute_of_day = merged["timestamp"].dt.hour * 60 + merged["timestamp"].dt.minute
    same_session_entry = merged["next_bar_timestamp"].dt.normalize().eq(
        merged["entry_date"]
    )
    signal = (
        merged["session_bars"].ge(46)
        & merged["pullback_from_open"].between(-0.05, -0.015)
        & merged["rebound_from_low"].ge(0.01)
        & merged["Close"].gt(merged["previous_bar_close"])
        & merged["previous_bar_close"].gt(merged["second_previous_bar_close"])
        & merged["Close"].ge(merged["cumulative_vwap"])
        & minute_of_day.between(10 * 60, 14 * 60 + 30)
        & same_session_entry
        & merged["next_bar_open"].gt(0.0)
        & merged["entry_jump"].le(0.01)
    ).fillna(False)
    if not signal.any():
        return pd.DataFrame()
    events = merged.loc[signal].groupby(
        ["code", "entry_date"], sort=False, as_index=False
    ).head(1).copy()
    events.rename(
        columns={
            "timestamp": "confirmation_timestamp",
            "next_bar_timestamp": "alternative_entry_timestamp",
            "next_bar_open": "alternative_raw_entry",
        },
        inplace=True,
    )
    config = execution_config or PortfolioConfig()
    events["baseline_recomputed_return"] = [
        _trade_net_return(entry, exit_price, config, execution_cost_multiplier)
        for entry, exit_price in zip(events["raw_entry_open"], events["raw_exit_open"])
    ]
    events["alternative_net_return"] = [
        _trade_net_return(entry, exit_price, config, execution_cost_multiplier)
        for entry, exit_price in zip(events["alternative_raw_entry"], events["raw_exit_open"])
    ]
    events["entry_improvement"] = (
        events["raw_entry_open"] / events["alternative_raw_entry"] - 1.0
    )
    events["paired_return_delta"] = (
        events["alternative_net_return"] - events["baseline_recomputed_return"]
    )
    keep = [
        "pair_id",
        "code",
        "entry_date",
        "confirmation_timestamp",
        "alternative_entry_timestamp",
        "raw_entry_open",
        "alternative_raw_entry",
        "raw_exit_open",
        "exit_date",
        "pullback_from_open",
        "rebound_from_low",
        "vwap_spread",
        "entry_jump",
        "realized_net_return",
        "baseline_recomputed_return",
        "alternative_net_return",
        "entry_improvement",
        "paired_return_delta",
        "exit_reason",
    ]
    return events.loc[:, keep].sort_values(
        ["alternative_entry_timestamp", "code"]
    ).reset_index(drop=True)


def evaluate_v9_intraday_entry(
    trade_pairs: pd.DataFrame,
    events: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    label: str,
) -> dict[str, Any]:
    """Evaluate both paired price improvement and missed-trade opportunity cost."""

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    pairs = trade_pairs.loc[
        pd.to_datetime(trade_pairs["entry_date"]).between(start, end)
    ].copy()
    scoped_events = events.loc[
        pd.to_datetime(events["entry_date"]).between(start, end)
    ].copy()
    baseline = pd.to_numeric(pairs.get("realized_net_return"), errors="coerce").dropna()
    alternative = pd.to_numeric(
        scoped_events.get("alternative_net_return"), errors="coerce"
    ).dropna()
    improvements = pd.to_numeric(
        scoped_events.get("entry_improvement"), errors="coerce"
    ).dropna()
    deltas = pd.to_numeric(
        scoped_events.get("paired_return_delta"), errors="coerce"
    ).dropna()
    baseline_contribution = float(baseline.sum())
    alternative_contribution = float(alternative.sum())
    return {
        "label": label,
        "start_date": start_date,
        "end_date": end_date,
        "intended_trades": int(len(pairs)),
        "filled_trades": int(len(scoped_events)),
        "fill_rate": float(len(scoped_events) / len(pairs)) if len(pairs) else 0.0,
        "baseline_median_return": float(baseline.median()) if not baseline.empty else None,
        "paired_alternative_median_return": (
            float(alternative.median()) if not alternative.empty else None
        ),
        "median_entry_improvement": (
            float(improvements.median()) if not improvements.empty else None
        ),
        "median_paired_return_delta": float(deltas.median()) if not deltas.empty else None,
        "baseline_signal_contribution": baseline_contribution,
        "alternative_signal_contribution": alternative_contribution,
        "opportunity_cost_adjusted_delta": (
            alternative_contribution - baseline_contribution
        ),
    }


def assess_v9_entry_development(report: Mapping[str, Any]) -> dict[str, Any]:
    checks = {
        "minimum_intended_trades": int(report["intended_trades"]) >= 10,
        "minimum_fill_rate": float(report["fill_rate"]) >= 0.60,
        "positive_median_entry_improvement": (
            report["median_entry_improvement"] is not None
            and float(report["median_entry_improvement"]) > 0.0
        ),
        "positive_median_paired_return_delta": (
            report["median_paired_return_delta"] is not None
            and float(report["median_paired_return_delta"]) > 0.0
        ),
        "nonnegative_opportunity_cost_adjusted_delta": (
            float(report["opportunity_cost_adjusted_delta"]) >= 0.0
        ),
    }
    passed = all(checks.values())
    return {
        "protocol_version": V9_ENTRY_PROTOCOL_VERSION,
        "decision": "OPEN_REPLICATION" if passed else "REJECT",
        "checks": checks,
        "replication_opened": passed,
    }


def v9_entry_protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": V9_ENTRY_PROTOCOL_VERSION,
        "hypothesis_id": V9_ENTRY_HYPOTHESIS_ID,
        "source_backtest_id": V9_SOURCE_BACKTEST_ID,
        "scope": "paired execution study on completed 2026 course49_v9 trades",
        "intraday_confirmation": {
            "pullback_from_first_open": [-0.05, -0.015],
            "minimum_rebound_from_low": 0.01,
            "three_rising_five_minute_closes": True,
            "close_above_cumulative_vwap": True,
            "confirmation_time": ["10:00", "14:30"],
            "entry": "next 5-minute raw open",
            "maximum_entry_jump": 0.01,
        },
        "comparison": {
            "same_codes": True,
            "same_v9_exit_date_and_raw_open": True,
            "missed_entry_return": 0.0,
            "costs": "standard portfolio costs and slippage",
        },
        "development": ["2026-01-01", "2026-05-15"],
        "replication": ["2026-05-18", "2026-06-26"],
        "development_gate": {
            "minimum_intended_trades": 10,
            "minimum_fill_rate": 0.60,
            "positive_median_entry_improvement": True,
            "positive_median_paired_return_delta": True,
            "nonnegative_opportunity_cost_adjusted_delta": True,
        },
    }


def build_v9_staged_entry_events(
    trade_pairs: pd.DataFrame,
    intraday_bars: pd.DataFrame,
    pullback_events: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Split V9 entry 50/50, with a frozen 14:35 fallback for tranche two."""

    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if trade_pairs.empty or intraday_bars.empty:
        return pd.DataFrame()
    pairs = trade_pairs.copy()
    pairs["entry_date"] = pd.to_datetime(pairs["entry_date"]).dt.normalize()
    bars = intraday_bars.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce")
    bars.dropna(subset=["timestamp"], inplace=True)
    bars["entry_date"] = bars["timestamp"].dt.normalize()
    fallback = bars.loc[
        (bars["timestamp"].dt.hour == 14)
        & (bars["timestamp"].dt.minute == 35),
        ["code", "entry_date", "timestamp", "Open"],
    ].rename(
        columns={
            "timestamp": "fallback_entry_timestamp",
            "Open": "fallback_raw_entry",
        }
    )
    fallback.drop_duplicates(["code", "entry_date"], keep="first", inplace=True)
    staged = pairs.merge(
        fallback, on=["code", "entry_date"], how="left", validate="one_to_one"
    )
    trigger_columns = [
        "pair_id",
        "confirmation_timestamp",
        "alternative_entry_timestamp",
        "alternative_raw_entry",
    ]
    if pullback_events.empty:
        trigger = pd.DataFrame(columns=trigger_columns)
    else:
        trigger = pullback_events.loc[:, trigger_columns].drop_duplicates("pair_id")
    staged = staged.merge(trigger, on="pair_id", how="left", validate="one_to_one")
    staged["pullback_triggered"] = staged["alternative_raw_entry"].notna()
    staged["second_entry_timestamp"] = staged["alternative_entry_timestamp"].where(
        staged["pullback_triggered"], staged["fallback_entry_timestamp"]
    )
    staged["second_raw_entry"] = staged["alternative_raw_entry"].where(
        staged["pullback_triggered"], staged["fallback_raw_entry"]
    )
    config = execution_config or PortfolioConfig()
    baseline_returns: list[float] = []
    staged_returns: list[float] = []
    staged_effective_entries: list[float] = []
    completed: list[bool] = []
    first_quantities: list[int] = []
    second_quantities: list[int] = []
    for row in staged.itertuples(index=False):
        quantity = int(row.quantity)
        first_quantity = max(
            config.board_lot,
            (quantity // (2 * config.board_lot)) * config.board_lot,
        )
        first_quantity = min(first_quantity, quantity)
        second_quantity = quantity - first_quantity
        first_quantities.append(first_quantity)
        second_quantities.append(second_quantity)
        baseline_return = _position_net_return(
            [(row.raw_entry_open, quantity)],
            row.raw_exit_open,
            config,
            execution_cost_multiplier,
        )
        baseline_returns.append(baseline_return)
        can_complete = second_quantity == 0 or (
            np.isfinite(row.second_raw_entry) and row.second_raw_entry > 0.0
        )
        completed.append(bool(can_complete))
        if not can_complete:
            staged_returns.append(np.nan)
            staged_effective_entries.append(np.nan)
            continue
        entries = [(row.raw_entry_open, first_quantity)]
        if second_quantity:
            entries.append((row.second_raw_entry, second_quantity))
        staged_returns.append(
            _position_net_return(
                entries,
                row.raw_exit_open,
                config,
                execution_cost_multiplier,
            )
        )
        staged_effective_entries.append(
            sum(price * leg_quantity for price, leg_quantity in entries) / quantity
        )
    staged["first_quantity"] = first_quantities
    staged["second_quantity"] = second_quantities
    staged["completed"] = completed
    staged["baseline_recomputed_return"] = baseline_returns
    staged["staged_net_return"] = staged_returns
    staged["staged_effective_raw_entry"] = staged_effective_entries
    staged["effective_entry_improvement"] = (
        staged["raw_entry_open"] / staged["staged_effective_raw_entry"] - 1.0
    )
    staged["paired_return_delta"] = (
        staged["staged_net_return"] - staged["baseline_recomputed_return"]
    )
    keep = [
        "pair_id",
        "code",
        "entry_date",
        "raw_entry_open",
        "pullback_triggered",
        "confirmation_timestamp",
        "second_entry_timestamp",
        "second_raw_entry",
        "fallback_raw_entry",
        "first_quantity",
        "second_quantity",
        "staged_effective_raw_entry",
        "effective_entry_improvement",
        "raw_exit_open",
        "exit_date",
        "baseline_recomputed_return",
        "staged_net_return",
        "paired_return_delta",
        "completed",
        "exit_reason",
    ]
    return staged.loc[:, keep].sort_values(["entry_date", "code"]).reset_index(drop=True)


def _position_net_return(
    entries: list[tuple[float, int]],
    exit_open: float,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> float:
    if not np.isfinite(exit_open) or exit_open <= 0:
        return np.nan
    buy_value = 0.0
    buy_fees = 0.0
    quantity = 0
    for raw_price, leg_quantity in entries:
        if not np.isfinite(raw_price) or raw_price <= 0 or leg_quantity <= 0:
            return np.nan
        execution = float(raw_price) * (1.0 + config.slippage_rate * cost_multiplier)
        value = execution * int(leg_quantity)
        buy_value += value
        buy_fees += max(
            config.min_commission * cost_multiplier,
            value * config.commission_rate * cost_multiplier,
        )
        quantity += int(leg_quantity)
    if quantity <= 0:
        return np.nan
    sell_execution = float(exit_open) * (1.0 - config.slippage_rate * cost_multiplier)
    sell_value = sell_execution * quantity
    sell_fee = max(
        config.min_commission * cost_multiplier,
        sell_value * config.commission_rate * cost_multiplier,
    ) + sell_value * config.stamp_duty_rate * cost_multiplier
    invested = buy_value + buy_fees
    return float((sell_value - sell_fee - invested) / invested)


def evaluate_v9_staged_entry(
    events: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    label: str,
) -> dict[str, Any]:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    scoped = events.loc[pd.to_datetime(events["entry_date"]).between(start, end)].copy()
    completed = scoped.loc[scoped["completed"].fillna(False)]
    baseline = pd.to_numeric(
        completed.get("baseline_recomputed_return"), errors="coerce"
    ).dropna()
    staged = pd.to_numeric(completed.get("staged_net_return"), errors="coerce").dropna()
    deltas = pd.to_numeric(completed.get("paired_return_delta"), errors="coerce").dropna()
    improvements = pd.to_numeric(
        completed.get("effective_entry_improvement"), errors="coerce"
    ).dropna()
    return {
        "label": label,
        "start_date": start_date,
        "end_date": end_date,
        "intended_trades": int(len(scoped)),
        "completed_trades": int(len(completed)),
        "completion_rate": float(len(completed) / len(scoped)) if len(scoped) else 0.0,
        "pullback_trigger_rate": (
            float(scoped["pullback_triggered"].fillna(False).mean()) if len(scoped) else 0.0
        ),
        "baseline_median_return": float(baseline.median()) if not baseline.empty else None,
        "staged_median_return": float(staged.median()) if not staged.empty else None,
        "median_effective_entry_improvement": (
            float(improvements.median()) if not improvements.empty else None
        ),
        "median_paired_return_delta": float(deltas.median()) if not deltas.empty else None,
        "baseline_signal_contribution": float(baseline.sum()),
        "staged_signal_contribution": float(staged.sum()),
        "signal_contribution_delta": float(staged.sum() - baseline.sum()),
    }


def assess_v9_staged_development(report: Mapping[str, Any]) -> dict[str, Any]:
    checks = {
        "minimum_intended_trades": int(report["intended_trades"]) >= 10,
        "complete_execution": float(report["completion_rate"]) == 1.0,
        "positive_median_paired_return_delta": (
            report["median_paired_return_delta"] is not None
            and float(report["median_paired_return_delta"]) > 0.0
        ),
        "nonnegative_signal_contribution_delta": (
            float(report["signal_contribution_delta"]) >= 0.0
        ),
    }
    passed = all(checks.values())
    return {
        "protocol_version": V9_STAGED_PROTOCOL_VERSION,
        "decision": "OPEN_REPLICATION" if passed else "REJECT",
        "checks": checks,
        "replication_opened": passed,
    }


def v9_staged_protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": V9_STAGED_PROTOCOL_VERSION,
        "hypothesis_id": V9_STAGED_HYPOTHESIS_ID,
        "source_backtest_id": V9_SOURCE_BACKTEST_ID,
        "research_status": (
            "adaptive development after the full-delay entry protocol failed; "
            "not independent validation"
        ),
        "execution": {
            "first_tranche": "50% at original V9 raw open",
            "second_tranche": (
                "50% at frozen pullback/reclaim next-bar open; otherwise 14:35 raw open"
            ),
            "pullback_protocol_version": V9_ENTRY_PROTOCOL_VERSION,
            "exit": "unchanged V9 exit date and raw open",
            "costs": "each tranche pays independent slippage and commission",
            "board_lot_rule": (
                "split original V9 quantity into board lots; one-lot positions remain at open"
            ),
        },
        "development": ["2026-01-01", "2026-05-15"],
        "replication": ["2026-05-18", "2026-06-26"],
        "development_gate": {
            "minimum_intended_trades": 10,
            "completion_rate": 1.0,
            "positive_median_paired_return_delta": True,
            "nonnegative_signal_contribution_delta": True,
        },
    }


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "daily_snapshot_id": DAILY_SNAPSHOT_ID,
        "state_backtest_id": STATE_BACKTEST_ID,
        "scope": "price_only; market state is annotation only",
        "daily_watchlist": {
            "return_60d": [0.05, 0.50],
            "ma20_above_ma60": True,
            "close_above_ma60": True,
            "return_5d": [-0.12, 0.0],
            "minimum_turnover_20d": 50_000_000,
            "minimum_price": 3.0,
            "exclude_st_and_price_limits": True,
        },
        "intraday_confirmation": {
            "opening_gap": [-0.03, 0.01],
            "confirmation_time": ["10:00", "14:30"],
            "session_low_return": [-0.05, -0.015],
            "minimum_rebound_from_low": 0.01,
            "three_rising_closes": True,
            "close_above_cumulative_vwap": True,
            "maximum_close_vs_previous_close": 0.01,
            "entry": "next 5-minute raw open",
            "maximum_entry_jump": 0.01,
        },
        "portfolio": {
            "target_weight": 0.10,
            "maximum_positions": 3,
            "capacity_order": "observable entry timestamp, then score, then code",
            "exit": "next trading-day raw open",
            "t_plus_one": True,
        },
        "windows": [asdict(window) for window in RESEARCH_WINDOWS],
        "opening_rule": "two development windows, then replication, validation, holdout",
    }


def lc5_path_for_code(root: Path, code: str) -> Path:
    numeric, exchange = str(code).split(".", maxsplit=1)
    market = exchange.lower()
    if market not in {"sh", "sz", "bj"}:
        raise ValueError(f"Unsupported A-share code: {code}")
    return root / market / "fzline" / f"{market}{numeric}.lc5"


def _empty_intraday_bars() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["code", "timestamp", "Open", "High", "Low", "Close", "Volume", "Amount"]
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
