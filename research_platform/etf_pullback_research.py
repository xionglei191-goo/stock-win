from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import PortfolioConfig


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "broad_etf_trend_rsi2_pullback"
RELATIVE_PROTOCOL_VERSION = "1.0.0"
RELATIVE_HYPOTHESIS_ID = "broad_etf_leader_relative_pullback"
MARKET_STATE_PROTOCOL_VERSION = "1.0.0"
MARKET_STATE_HYPOTHESIS_ID = "broad_etf_market_state_relative_pullback"
DAY_DTYPE = np.dtype(
    [
        ("date", "<u4"),
        ("open", "<u4"),
        ("high", "<u4"),
        ("low", "<u4"),
        ("close", "<u4"),
        ("amount", "<f4"),
        ("volume", "<u4"),
        ("reserved", "<u4"),
    ]
)


@dataclass(frozen=True)
class EtfAsset:
    code: str
    name: str
    market: str
    local_code: str


@dataclass(frozen=True)
class EtfWindow:
    label: str
    role: str
    start_date: str
    end_date: str
    evaluation_weight: float


ASSETS = (
    EtfAsset("510050.SH", "上证50ETF", "sh", "sh510050"),
    EtfAsset("510300.SH", "沪深300ETF", "sh", "sh510300"),
    EtfAsset("510500.SH", "中证500ETF", "sh", "sh510500"),
    EtfAsset("512100.SH", "中证1000ETF", "sh", "sh512100"),
    EtfAsset("588000.SH", "科创50ETF", "sh", "sh588000"),
    EtfAsset("159915.SZ", "创业板ETF", "sz", "sz159915"),
    EtfAsset("159949.SZ", "创业板50ETF", "sz", "sz159949"),
)

WINDOWS = (
    EtfWindow("dev_2021_2022", "DEVELOPMENT", "2021-04-01", "2022-04-29", 0.05),
    EtfWindow("dev_2022_2023", "DEVELOPMENT", "2022-05-01", "2023-05-31", 0.05),
    EtfWindow("dev_2023_2024", "DEVELOPMENT", "2023-06-01", "2024-06-28", 0.05),
    EtfWindow("replication_2024_2025", "REPLICATION", "2024-07-01", "2025-07-24", 0.25),
    EtfWindow("holdout_2025_2026", "HOLDOUT", "2025-07-25", "2026-08-07", 0.60),
)


def decode_day_bytes(data: bytes, asset: EtfAsset) -> pd.DataFrame:
    if len(data) % DAY_DTYPE.itemsize:
        raise ValueError("DAY payload size must be a multiple of 32 bytes")
    records = np.frombuffer(data, dtype=DAY_DTYPE)
    if not len(records):
        return _empty_bars()
    dates = pd.to_datetime(records["date"].astype(str), errors="coerce")
    frame = pd.DataFrame(
        {
            "code": asset.code,
            "name": asset.name,
            "timestamp": dates,
            "Open": records["open"].astype(float) / 1000.0,
            "High": records["high"].astype(float) / 1000.0,
            "Low": records["low"].astype(float) / 1000.0,
            "Close": records["close"].astype(float) / 1000.0,
            "Amount": records["amount"].astype(float),
            "Volume": records["volume"].astype(float),
        }
    )
    return frame.dropna(subset=["timestamp"]).reset_index(drop=True)


def create_etf_snapshot(
    *,
    tdx_root: Path,
    output_dir: Path,
    assets: tuple[EtfAsset, ...] = ASSETS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "protocol.json").write_text(
        json.dumps(protocol_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    frames: list[pd.DataFrame] = []
    sources: list[dict[str, Any]] = []
    for asset in assets:
        path = tdx_root / "vipdoc" / asset.market / "lday" / f"{asset.local_code}.day"
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"DAY source changed while reading: {path}")
        frame = decode_day_bytes(data, asset)
        if frame.empty:
            raise ValueError(f"No valid DAY records for {asset.code}")
        frames.append(frame)
        sources.append(
            {
                "code": asset.code,
                "name": asset.name,
                "path": str(path.resolve()),
                "bytes": int(before.st_size),
                "mtime_ns": int(before.st_mtime_ns),
                "sha256": hashlib.sha256(data).hexdigest(),
                "rows": int(len(frame)),
                "start_date": str(frame["timestamp"].min().date()),
                "end_date": str(frame["timestamp"].max().date()),
            }
        )
    bars = pd.concat(frames, ignore_index=True).sort_values(
        ["code", "timestamp"]
    ).reset_index(drop=True)
    bars_path = output_dir / "bars.parquet"
    bars.to_parquet(bars_path, index=False)
    source_payload = {
        "protocol_version": PROTOCOL_VERSION,
        "assets": [asdict(asset) for asset in assets],
        "source_files": sources,
    }
    snapshot_id = hashlib.sha256(
        json.dumps(source_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest = {
        **source_payload,
        "snapshot_id": snapshot_id,
        "bars_path": str(bars_path.resolve()),
        "bars_sha256": _file_sha256(bars_path),
        "bars_rows": int(len(bars)),
        "duplicate_keys": int(bars.duplicated(["code", "timestamp"]).sum()),
        "invalid_ohlc": int(
            (
                bars["Low"].gt(bars["High"])
                | bars["Open"].le(0.0)
                | bars["Close"].le(0.0)
            ).sum()
        ),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return bars, manifest


def build_market_state_context(
    market_index: pd.DataFrame,
    market_activity: pd.DataFrame,
) -> pd.DataFrame:
    """Build point-in-time market filters from immutable daily snapshots."""

    columns = [
        "timestamp",
        "index_close",
        "index_ma120",
        "index_return_3d",
        "index_return_3d_q20",
        "advance_ratio",
        "limit_down_total",
        "limit_down_q80",
        "market_state_allowed",
    ]
    if market_index.empty or market_activity.empty:
        return pd.DataFrame(columns=columns)

    index = market_index.copy()
    index["timestamp"] = pd.to_datetime(
        index["timestamp"], errors="coerce"
    ).dt.normalize()
    index.dropna(subset=["timestamp"], inplace=True)
    if "code" in index.columns and index["code"].nunique(dropna=True) > 1:
        raise ValueError("market_index must contain one benchmark code")
    if index["timestamp"].duplicated().any():
        raise ValueError("market_index must contain one row per trading day")
    index.sort_values("timestamp", inplace=True)
    index["index_close"] = pd.to_numeric(index["Close"], errors="coerce")
    index["index_ma120"] = index["index_close"].rolling(
        120, min_periods=120
    ).mean()
    index["index_return_3d"] = index["index_close"] / index["index_close"].shift(3) - 1.0
    index["index_return_3d_q20"] = index["index_return_3d"].shift(1).rolling(
        126, min_periods=60
    ).quantile(0.20)

    activity = market_activity.copy()
    activity["timestamp"] = pd.to_datetime(
        activity["timestamp"], errors="coerce"
    ).dt.normalize()
    activity.dropna(subset=["timestamp"], inplace=True)
    if activity["timestamp"].duplicated().any():
        raise ValueError("market_activity must contain one row per trading day")
    activity.sort_values("timestamp", inplace=True)
    for column in ("advance_count", "decline_count", "limit_down_total"):
        activity[column] = pd.to_numeric(activity[column], errors="coerce")
    breadth_total = activity["advance_count"] + activity["decline_count"]
    activity["advance_ratio"] = activity["advance_count"] / breadth_total.where(
        breadth_total.gt(0.0)
    )
    activity["limit_down_q80"] = activity["limit_down_total"].shift(1).rolling(
        126, min_periods=60
    ).quantile(0.80)

    context = index[
        [
            "timestamp",
            "index_close",
            "index_ma120",
            "index_return_3d",
            "index_return_3d_q20",
        ]
    ].merge(
        activity[
            ["timestamp", "advance_ratio", "limit_down_total", "limit_down_q80"]
        ],
        on="timestamp",
        how="inner",
        validate="one_to_one",
    )
    context["market_state_allowed"] = (
        context["index_close"].gt(context["index_ma120"])
        & context["index_return_3d"].le(context["index_return_3d_q20"])
        & context["advance_ratio"].ge(0.25)
        & context["limit_down_total"].le(context["limit_down_q80"])
    ).fillna(False)
    return context[columns].sort_values("timestamp").reset_index(drop=True)


def build_etf_pullback_events(
    bars: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if bars.empty:
        return pd.DataFrame()
    frame = bars.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.normalize()
    frame.dropna(subset=["timestamp"], inplace=True)
    frame.sort_values(["code", "timestamp"], inplace=True)
    grouped = frame.groupby("code", sort=False)
    frame["ma5"] = grouped["Close"].transform(
        lambda values: values.rolling(5, min_periods=5).mean()
    )
    frame["ma200"] = grouped["Close"].transform(
        lambda values: values.rolling(200, min_periods=200).mean()
    )
    frame["return_3d"] = frame["Close"] / grouped["Close"].shift(3) - 1.0
    frame["return_60d"] = frame["Close"] / grouped["Close"].shift(60) - 1.0
    frame["amount_20d"] = grouped["Amount"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    frame["rsi2"] = grouped["Close"].transform(_rsi2)
    frame["eligible"] = (
        frame["Close"].gt(frame["ma200"])
        & frame["return_60d"].gt(0.0)
        & frame["return_3d"].le(-0.03)
        & frame["rsi2"].le(5.0)
        & frame["Close"].lt(frame["ma5"])
        & frame["amount_20d"].ge(200_000_000.0)
    ).fillna(False)
    config = execution_config or PortfolioConfig()
    rows: list[dict[str, Any]] = []
    for code, code_frame in frame.groupby("code", sort=False):
        code_frame = code_frame.reset_index(drop=True)
        # Groupby retains original indexes; recompute the local mask explicitly.
        local_eligible = code_frame["eligible"].astype(bool)
        local_trigger = local_eligible & ~local_eligible.shift(
            1, fill_value=False
        ).astype(bool)
        trigger_positions = code_frame.index[local_trigger].tolist()
        for signal_position in trigger_positions:
            entry_position = signal_position + 1
            if entry_position >= len(code_frame):
                continue
            entry = code_frame.iloc[entry_position]
            entry_open = float(entry["Open"])
            exit_position: int | None = None
            exit_reason = "MAX_HOLDING"
            maximum_exit_position = entry_position + 5
            for position in range(entry_position, min(maximum_exit_position, len(code_frame))):
                observed = code_frame.iloc[position]
                if float(observed["Close"]) <= entry_open * 0.94:
                    exit_position = position + 1
                    exit_reason = "FIXED_STOP_CLOSE"
                    break
                if float(observed["Close"]) >= float(observed["ma5"]):
                    exit_position = position + 1
                    exit_reason = "MA5_RECLAIM"
                    break
            if exit_position is None:
                exit_position = maximum_exit_position
            if exit_position >= len(code_frame):
                continue
            exit_bar = code_frame.iloc[exit_position]
            buy_execution = entry_open * (
                1.0 + config.slippage_rate * execution_cost_multiplier
            )
            target_cash = config.initial_cash * 0.95
            quantity = int(target_cash // (buy_execution * config.board_lot)) * config.board_lot
            if quantity <= 0:
                continue
            net_return = _round_trip_return(
                entry_open,
                float(exit_bar["Open"]),
                quantity,
                config,
                execution_cost_multiplier,
            )
            signal = code_frame.iloc[signal_position]
            rows.append(
                {
                    "code": str(code),
                    "name": str(signal["name"]),
                    "hypothesis_id": HYPOTHESIS_ID,
                    "signal_date": pd.Timestamp(signal["timestamp"]).normalize(),
                    "entry_date": pd.Timestamp(entry["timestamp"]).normalize(),
                    "entry_open": entry_open,
                    "exit_date": pd.Timestamp(exit_bar["timestamp"]).normalize(),
                    "exit_open": float(exit_bar["Open"]),
                    "exit_reason": exit_reason,
                    "holding_sessions": int(exit_position - entry_position),
                    "quantity": quantity,
                    "rsi2": float(signal["rsi2"]),
                    "return_3d": float(signal["return_3d"]),
                    "return_60d": float(signal["return_60d"]),
                    "ma5_distance": float(signal["Close"] / signal["ma5"] - 1.0),
                    "ma200_distance": float(signal["Close"] / signal["ma200"] - 1.0),
                    "amount_20d": float(signal["amount_20d"]),
                    "net_return": net_return,
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["entry_date", "rsi2", "return_60d", "code"],
        ascending=[True, True, False, True],
    ).reset_index(drop=True)


def select_single_position(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events.copy()
    ordered = events.sort_values(
        ["entry_date", "rsi2", "return_60d", "code"],
        ascending=[True, True, False, True],
    )
    accepted: list[pd.Series] = []
    active_until = pd.NaT
    for _, event in ordered.iterrows():
        entry_date = pd.Timestamp(event["entry_date"]).normalize()
        if pd.notna(active_until) and entry_date < active_until:
            continue
        accepted.append(event)
        active_until = pd.Timestamp(event["exit_date"]).normalize()
    return pd.DataFrame(accepted).reset_index(drop=True)


def evaluate_etf_window(
    events: pd.DataFrame,
    window: EtfWindow,
    *,
    trading_days: int,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    scoped = events.loc[
        pd.to_datetime(events["entry_date"]).between(window.start_date, window.end_date)
        & pd.to_datetime(events["exit_date"]).le(window.end_date)
    ].copy()
    accepted = select_single_position(scoped)
    returns = pd.to_numeric(accepted.get("net_return"), errors="coerce").dropna()
    portfolio = _simulate_etf_cashflows(
        accepted,
        trading_days=trading_days,
        config=execution_config or PortfolioConfig(),
        cost_multiplier=execution_cost_multiplier,
    )
    ex_top3 = returns.sort_values(ascending=False).iloc[3:]
    return {
        "window": asdict(window),
        "raw_signals": int(len(scoped)),
        "portfolio_trades": int(len(accepted)),
        "median_trade_return": float(returns.median()) if not returns.empty else None,
        "mean_trade_return": float(returns.mean()) if not returns.empty else None,
        "win_rate": float((returns > 0).mean()) if not returns.empty else None,
        "ex_top3_return_sum": float(ex_top3.sum()),
        "by_asset": {
            str(code): int(count)
            for code, count in accepted["code"].value_counts().sort_index().items()
        },
        **portfolio,
    }


def assess_etf_development(reports: list[dict[str, Any]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for report in reports:
        if str(report["window"]["role"]).upper() != "DEVELOPMENT":
            continue
        window_checks = {
            "minimum_trades": int(report["portfolio_trades"]) >= 10,
            "positive_total_return": float(report["portfolio_total_return"]) > 0.0,
            "positive_median_trade": (
                report["median_trade_return"] is not None
                and float(report["median_trade_return"]) > 0.0
            ),
            "positive_ex_top3": float(report["ex_top3_return_sum"]) > 0.0,
            "maximum_drawdown": float(report["portfolio_max_drawdown"]) >= -0.10,
        }
        checks.append(
            {
                "window": report["window"]["label"],
                "checks": window_checks,
                "passed": all(window_checks.values()),
            }
        )
    passed = bool(checks and all(item["passed"] for item in checks))
    return {
        "protocol_version": PROTOCOL_VERSION,
        "decision": "OPEN_REPLICATION" if passed else "REJECT",
        "checks": checks,
        "replication_opened": passed,
        "holdout_opened": False,
    }


def build_etf_relative_pullback_events(
    bars: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Buy relative pullbacks only among the three strongest broad ETFs."""

    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if bars.empty:
        return pd.DataFrame()
    frame = bars.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.normalize()
    frame.dropna(subset=["timestamp"], inplace=True)
    frame.sort_values(["code", "timestamp"], inplace=True)
    grouped = frame.groupby("code", sort=False)
    frame["ma10"] = grouped["Close"].transform(
        lambda values: values.rolling(10, min_periods=10).mean()
    )
    frame["ma200"] = grouped["Close"].transform(
        lambda values: values.rolling(200, min_periods=200).mean()
    )
    frame["return_3d"] = frame["Close"] / grouped["Close"].shift(3) - 1.0
    frame["return_120d"] = frame["Close"] / grouped["Close"].shift(120) - 1.0
    frame["amount_20d"] = grouped["Amount"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    frame["return_3d_q10"] = grouped["return_3d"].transform(
        lambda values: values.shift(1).rolling(252, min_periods=126).quantile(0.10)
    )
    frame["return_3d_vol"] = grouped["return_3d"].transform(
        lambda values: values.shift(1).rolling(252, min_periods=126).std(ddof=0)
    )
    frame["momentum_rank"] = frame.groupby("timestamp", sort=False)[
        "return_120d"
    ].rank(method="first", ascending=False)
    frame["relative_eligible"] = (
        frame["Close"].gt(frame["ma200"])
        & frame["momentum_rank"].le(3.0)
        & frame["return_3d"].le(frame["return_3d_q10"])
        & frame["Close"].lt(frame["ma10"])
        & frame["amount_20d"].ge(200_000_000.0)
    ).fillna(False)
    config = execution_config or PortfolioConfig()
    rows: list[dict[str, Any]] = []
    for code, code_frame in frame.groupby("code", sort=False):
        code_frame = code_frame.reset_index(drop=True)
        local_eligible = code_frame["relative_eligible"].astype(bool)
        local_trigger = local_eligible & ~local_eligible.shift(
            1, fill_value=False
        ).astype(bool)
        for signal_position in code_frame.index[local_trigger].tolist():
            entry_position = signal_position + 1
            if entry_position >= len(code_frame):
                continue
            entry = code_frame.iloc[entry_position]
            entry_open = float(entry["Open"])
            maximum_exit_position = entry_position + 10
            exit_position: int | None = None
            exit_reason = "MAX_HOLDING"
            for position in range(entry_position, min(maximum_exit_position, len(code_frame))):
                observed = code_frame.iloc[position]
                if float(observed["Close"]) <= entry_open * 0.92:
                    exit_position = position + 1
                    exit_reason = "FIXED_STOP_CLOSE"
                    break
                if float(observed["Close"]) >= float(observed["ma10"]):
                    exit_position = position + 1
                    exit_reason = "MA10_RECLAIM"
                    break
            if exit_position is None:
                exit_position = maximum_exit_position
            if exit_position >= len(code_frame):
                continue
            exit_bar = code_frame.iloc[exit_position]
            buy_execution = entry_open * (
                1.0 + config.slippage_rate * execution_cost_multiplier
            )
            target_cash = config.initial_cash * 0.95
            quantity = int(target_cash // (buy_execution * config.board_lot)) * config.board_lot
            if quantity <= 0:
                continue
            signal = code_frame.iloc[signal_position]
            rows.append(
                {
                    "code": str(code),
                    "name": str(signal["name"]),
                    "hypothesis_id": RELATIVE_HYPOTHESIS_ID,
                    "signal_date": pd.Timestamp(signal["timestamp"]).normalize(),
                    "entry_date": pd.Timestamp(entry["timestamp"]).normalize(),
                    "entry_open": entry_open,
                    "exit_date": pd.Timestamp(exit_bar["timestamp"]).normalize(),
                    "exit_open": float(exit_bar["Open"]),
                    "exit_reason": exit_reason,
                    "holding_sessions": int(exit_position - entry_position),
                    "quantity": quantity,
                    "return_3d": float(signal["return_3d"]),
                    "return_3d_q10": float(signal["return_3d_q10"]),
                    "return_3d_z": float(
                        signal["return_3d"] / signal["return_3d_vol"]
                    ),
                    "return_120d": float(signal["return_120d"]),
                    "momentum_rank": int(signal["momentum_rank"]),
                    "ma10_distance": float(signal["Close"] / signal["ma10"] - 1.0),
                    "ma200_distance": float(signal["Close"] / signal["ma200"] - 1.0),
                    "amount_20d": float(signal["amount_20d"]),
                    "net_return": _round_trip_return(
                        entry_open,
                        float(exit_bar["Open"]),
                        quantity,
                        config,
                        execution_cost_multiplier,
                    ),
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["entry_date", "return_3d_z", "return_120d", "code"],
        ascending=[True, True, False, True],
    ).reset_index(drop=True)


def build_etf_market_state_pullback_events(
    bars: pd.DataFrame,
    market_state: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Buy broad-ETF pullbacks only during healthy market risk-off states."""

    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if bars.empty or market_state.empty:
        return _empty_market_state_events()
    required_state_columns = {
        "timestamp",
        "index_close",
        "index_ma120",
        "index_return_3d",
        "index_return_3d_q20",
        "advance_ratio",
        "limit_down_total",
        "limit_down_q80",
        "market_state_allowed",
    }
    missing = required_state_columns.difference(market_state.columns)
    if missing:
        raise ValueError(f"market_state is missing columns: {sorted(missing)}")

    frame = bars.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.normalize()
    frame.dropna(subset=["timestamp"], inplace=True)
    frame.sort_values(["code", "timestamp"], inplace=True)
    grouped = frame.groupby("code", sort=False)
    frame["ma10"] = grouped["Close"].transform(
        lambda values: values.rolling(10, min_periods=10).mean()
    )
    frame["ma200"] = grouped["Close"].transform(
        lambda values: values.rolling(200, min_periods=200).mean()
    )
    frame["return_3d"] = frame["Close"] / grouped["Close"].shift(3) - 1.0
    frame["return_120d"] = frame["Close"] / grouped["Close"].shift(120) - 1.0
    frame["amount_20d"] = grouped["Amount"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    frame["return_3d_q20"] = grouped["return_3d"].transform(
        lambda values: values.shift(1).rolling(252, min_periods=126).quantile(0.20)
    )
    frame["return_3d_vol"] = grouped["return_3d"].transform(
        lambda values: values.shift(1).rolling(252, min_periods=126).std(ddof=0)
    )
    frame["momentum_rank"] = frame.groupby("timestamp", sort=False)[
        "return_120d"
    ].rank(method="first", ascending=False)

    state = market_state[list(required_state_columns)].copy()
    state["timestamp"] = pd.to_datetime(state["timestamp"], errors="coerce").dt.normalize()
    state.dropna(subset=["timestamp"], inplace=True)
    if state["timestamp"].duplicated().any():
        raise ValueError("market_state must contain one row per trading day")
    frame = frame.merge(
        state,
        on="timestamp",
        how="left",
        validate="many_to_one",
    )
    frame["state_eligible"] = (
        frame["Close"].gt(frame["ma200"])
        & frame["momentum_rank"].le(3.0)
        & frame["return_3d"].le(frame["return_3d_q20"])
        & frame["Close"].lt(frame["ma10"])
        & frame["amount_20d"].ge(200_000_000.0)
        & frame["market_state_allowed"].fillna(False).astype(bool)
    ).fillna(False)

    config = execution_config or PortfolioConfig()
    rows: list[dict[str, Any]] = []
    for code, code_frame in frame.groupby("code", sort=False):
        code_frame = code_frame.reset_index(drop=True)
        local_eligible = code_frame["state_eligible"].astype(bool)
        local_trigger = local_eligible & ~local_eligible.shift(
            1, fill_value=False
        ).astype(bool)
        for signal_position in code_frame.index[local_trigger].tolist():
            entry_position = signal_position + 1
            if entry_position >= len(code_frame):
                continue
            entry = code_frame.iloc[entry_position]
            entry_open = float(entry["Open"])
            maximum_exit_position = entry_position + 10
            exit_position: int | None = None
            exit_reason = "MAX_HOLDING"
            for position in range(entry_position, min(maximum_exit_position, len(code_frame))):
                observed = code_frame.iloc[position]
                if float(observed["Close"]) <= entry_open * 0.92:
                    exit_position = position + 1
                    exit_reason = "FIXED_STOP_CLOSE"
                    break
                if float(observed["Close"]) >= float(observed["ma10"]):
                    exit_position = position + 1
                    exit_reason = "MA10_RECLAIM"
                    break
            if exit_position is None:
                exit_position = maximum_exit_position
            if exit_position >= len(code_frame):
                continue
            exit_bar = code_frame.iloc[exit_position]
            buy_execution = entry_open * (
                1.0 + config.slippage_rate * execution_cost_multiplier
            )
            target_cash = config.initial_cash * 0.95
            quantity = int(target_cash // (buy_execution * config.board_lot)) * config.board_lot
            if quantity <= 0:
                continue
            signal = code_frame.iloc[signal_position]
            rows.append(
                {
                    "code": str(code),
                    "name": str(signal["name"]),
                    "hypothesis_id": MARKET_STATE_HYPOTHESIS_ID,
                    "signal_date": pd.Timestamp(signal["timestamp"]).normalize(),
                    "entry_date": pd.Timestamp(entry["timestamp"]).normalize(),
                    "entry_open": entry_open,
                    "exit_date": pd.Timestamp(exit_bar["timestamp"]).normalize(),
                    "exit_open": float(exit_bar["Open"]),
                    "exit_reason": exit_reason,
                    "holding_sessions": int(exit_position - entry_position),
                    "quantity": quantity,
                    "return_3d": float(signal["return_3d"]),
                    "return_3d_q20": float(signal["return_3d_q20"]),
                    "return_3d_z": float(signal["return_3d"] / signal["return_3d_vol"]),
                    "return_120d": float(signal["return_120d"]),
                    "momentum_rank": int(signal["momentum_rank"]),
                    "ma10_distance": float(signal["Close"] / signal["ma10"] - 1.0),
                    "ma200_distance": float(signal["Close"] / signal["ma200"] - 1.0),
                    "amount_20d": float(signal["amount_20d"]),
                    "index_close": float(signal["index_close"]),
                    "index_ma120": float(signal["index_ma120"]),
                    "index_return_3d": float(signal["index_return_3d"]),
                    "index_return_3d_q20": float(signal["index_return_3d_q20"]),
                    "advance_ratio": float(signal["advance_ratio"]),
                    "limit_down_total": float(signal["limit_down_total"]),
                    "limit_down_q80": float(signal["limit_down_q80"]),
                    "net_return": _round_trip_return(
                        entry_open,
                        float(exit_bar["Open"]),
                        quantity,
                        config,
                        execution_cost_multiplier,
                    ),
                }
            )
    if not rows:
        return _empty_market_state_events()
    return pd.DataFrame(rows).sort_values(
        ["entry_date", "return_3d_z", "return_120d", "code"],
        ascending=[True, True, False, True],
    ).reset_index(drop=True)


def select_relative_single_position(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events.copy()
    ordered = events.sort_values(
        ["entry_date", "return_3d_z", "return_120d", "code"],
        ascending=[True, True, False, True],
    )
    accepted: list[pd.Series] = []
    active_until = pd.NaT
    for _, event in ordered.iterrows():
        entry_date = pd.Timestamp(event["entry_date"]).normalize()
        if pd.notna(active_until) and entry_date < active_until:
            continue
        accepted.append(event)
        active_until = pd.Timestamp(event["exit_date"]).normalize()
    return (
        pd.DataFrame(accepted).reset_index(drop=True)
        if accepted
        else events.iloc[0:0].copy()
    )


def evaluate_etf_relative_window(
    events: pd.DataFrame,
    window: EtfWindow,
    *,
    trading_days: int,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    if events.empty:
        scoped = events.copy()
    else:
        scoped = events.loc[
            pd.to_datetime(events["entry_date"]).between(window.start_date, window.end_date)
            & pd.to_datetime(events["exit_date"]).le(window.end_date)
        ].copy()
    accepted = select_relative_single_position(scoped)
    returns = pd.to_numeric(
        accepted["net_return"] if "net_return" in accepted else pd.Series(dtype=float),
        errors="coerce",
    ).dropna()
    portfolio = _simulate_etf_cashflows(
        accepted,
        trading_days=trading_days,
        config=execution_config or PortfolioConfig(),
        cost_multiplier=execution_cost_multiplier,
    )
    ex_top3 = returns.sort_values(ascending=False).iloc[3:]
    return {
        "window": asdict(window),
        "raw_signals": int(len(scoped)),
        "portfolio_trades": int(len(accepted)),
        "median_trade_return": float(returns.median()) if not returns.empty else None,
        "mean_trade_return": float(returns.mean()) if not returns.empty else None,
        "win_rate": float((returns > 0).mean()) if not returns.empty else None,
        "ex_top3_return_sum": float(ex_top3.sum()),
        "by_asset": {
            str(code): int(count)
            for code, count in (
                accepted["code"].value_counts().sort_index().items()
                if "code" in accepted
                else []
            )
        },
        **portfolio,
    }


def evaluate_etf_market_state_window(
    events: pd.DataFrame,
    window: EtfWindow,
    *,
    trading_days: int,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    return evaluate_etf_relative_window(
        events,
        window,
        trading_days=trading_days,
        execution_config=execution_config,
        execution_cost_multiplier=execution_cost_multiplier,
    )


def assess_etf_relative_development(
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for report in reports:
        if str(report["window"]["role"]).upper() != "DEVELOPMENT":
            continue
        window_checks = {
            "minimum_trades": int(report["portfolio_trades"]) >= 10,
            "positive_total_return": float(report["portfolio_total_return"]) > 0.0,
            "positive_median_trade": (
                report["median_trade_return"] is not None
                and float(report["median_trade_return"]) > 0.0
            ),
            "positive_ex_top3": float(report["ex_top3_return_sum"]) > 0.0,
            "maximum_drawdown": float(report["portfolio_max_drawdown"]) >= -0.10,
        }
        checks.append(
            {
                "window": report["window"]["label"],
                "checks": window_checks,
                "passed": all(window_checks.values()),
            }
        )
    passed = bool(checks and all(item["passed"] for item in checks))
    return {
        "protocol_version": RELATIVE_PROTOCOL_VERSION,
        "decision": "OPEN_REPLICATION" if passed else "REJECT",
        "checks": checks,
        "replication_opened": passed,
        "holdout_opened": False,
    }


def assess_etf_market_state_development(
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for report in reports:
        if str(report["window"]["role"]).upper() != "DEVELOPMENT":
            continue
        window_checks = {
            "minimum_trades": int(report["portfolio_trades"]) >= 10,
            "positive_total_return": float(report["portfolio_total_return"]) > 0.0,
            "positive_median_trade": (
                report["median_trade_return"] is not None
                and float(report["median_trade_return"]) > 0.0
            ),
            "positive_ex_top3": float(report["ex_top3_return_sum"]) > 0.0,
            "maximum_drawdown": float(report["portfolio_max_drawdown"]) >= -0.10,
        }
        checks.append(
            {
                "window": report["window"]["label"],
                "checks": window_checks,
                "passed": all(window_checks.values()),
            }
        )
    passed = bool(checks and all(item["passed"] for item in checks))
    return {
        "protocol_version": MARKET_STATE_PROTOCOL_VERSION,
        "decision": "OPEN_REPLICATION" if passed else "REJECT",
        "checks": checks,
        "replication_opened": passed,
        "holdout_opened": False,
    }


def relative_protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": RELATIVE_PROTOCOL_VERSION,
        "hypothesis_id": RELATIVE_HYPOTHESIS_ID,
        "assets": [asdict(asset) for asset in ASSETS],
        "signal": {
            "momentum": "top three by point-in-time 120-day return",
            "close_above_ma200": True,
            "pullback": (
                "3-day return at or below the prior 252-session 10th percentile; "
                "current return is excluded from the threshold"
            ),
            "close_below_ma10": True,
            "minimum_amount_20d": 200_000_000,
            "repeat_signal": "only on false-to-true transition",
        },
        "execution": {
            "entry": "next trading-day raw open",
            "exit": "next open after close reclaims MA10 or closes 8% below entry",
            "maximum_holding_sessions": 10,
            "target_weight": 0.95,
            "maximum_positions": 1,
            "same_day_rank": "lower 3-day z-score, higher 120-day return, code",
            "costs": "conservative stock slippage, commission, and stamp duty",
        },
        "development_gate": {
            "minimum_trades_per_window": 10,
            "positive_total_return": True,
            "positive_median_trade": True,
            "positive_ex_top3": True,
            "maximum_drawdown": -0.10,
            "all_development_windows_must_pass": True,
        },
        "windows": [asdict(window) for window in WINDOWS],
        "opening_rule": "development, then replication, then holdout",
    }


def save_relative_protocol(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "relative_protocol.json"
    path.write_text(
        json.dumps(relative_protocol_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def market_state_protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": MARKET_STATE_PROTOCOL_VERSION,
        "hypothesis_id": MARKET_STATE_HYPOTHESIS_ID,
        "assets": [asdict(asset) for asset in ASSETS],
        "market_state": {
            "benchmark": "999999.SH",
            "close_above_ma120": True,
            "pullback": (
                "3-day benchmark return at or below the prior 126-session 20th "
                "percentile, with at least 60 prior observations"
            ),
            "minimum_advance_ratio": 0.25,
            "maximum_limit_down": (
                "at or below the prior 126-session 80th percentile, with at least "
                "60 prior observations"
            ),
            "threshold_timing": "all rolling thresholds exclude the current day",
            "sources": ["market_index", "market_activity"],
        },
        "signal": {
            "momentum": "top three by point-in-time 120-day return",
            "close_above_ma200": True,
            "pullback": (
                "3-day ETF return at or below the prior 252-session 20th percentile; "
                "current return is excluded from the threshold"
            ),
            "close_below_ma10": True,
            "minimum_amount_20d": 200_000_000,
            "repeat_signal": "only on combined false-to-true transition",
        },
        "execution": {
            "entry": "next trading-day raw open",
            "exit": "next open after close reclaims MA10 or closes 8% below entry",
            "maximum_holding_sessions": 10,
            "target_weight": 0.95,
            "maximum_positions": 1,
            "same_day_rank": "lower 3-day z-score, higher 120-day return, code",
            "costs": "conservative stock slippage, commission, and stamp duty",
        },
        "development_gate": {
            "minimum_trades_per_window": 10,
            "positive_total_return": True,
            "positive_median_trade": True,
            "positive_ex_top3": True,
            "maximum_drawdown": -0.10,
            "all_development_windows_must_pass": True,
        },
        "windows": [asdict(window) for window in WINDOWS],
        "opening_rule": "development, then replication, then holdout",
    }


def save_market_state_protocol(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "market_state_protocol.json"
    path.write_text(
        json.dumps(market_state_protocol_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "assets": [asdict(asset) for asset in ASSETS],
        "signal": {
            "close_above_ma200": True,
            "positive_return_60d": True,
            "maximum_return_3d": -0.03,
            "maximum_rsi2": 5.0,
            "close_below_ma5": True,
            "minimum_amount_20d": 200_000_000,
            "repeat_signal": "only on false-to-true transition",
        },
        "execution": {
            "entry": "next trading-day raw open",
            "exit": "next open after close reclaims MA5 or closes 6% below entry",
            "maximum_holding_sessions": 5,
            "target_weight": 0.95,
            "maximum_positions": 1,
            "same_day_rank": "lower RSI2, higher 60-day return, code",
            "costs": "conservative stock slippage, commission, and stamp duty",
        },
        "development_gate": {
            "minimum_trades_per_window": 10,
            "positive_total_return": True,
            "positive_median_trade": True,
            "positive_ex_top3": True,
            "maximum_drawdown": -0.10,
            "all_development_windows_must_pass": True,
        },
        "windows": [asdict(window) for window in WINDOWS],
        "opening_rule": "development, then replication, then holdout",
    }


def _rsi2(values: pd.Series) -> pd.Series:
    delta = values.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    average_gain = gains.ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    average_loss = losses.ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    relative = average_gain / average_loss.replace(0.0, np.nan)
    result = 100.0 - 100.0 / (1.0 + relative)
    result = result.where(average_loss.ne(0.0), 100.0)
    result = result.where(average_gain.ne(0.0), 0.0)
    return result


def _round_trip_return(
    entry_open: float,
    exit_open: float,
    quantity: int,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> float:
    buy_price = entry_open * (1.0 + config.slippage_rate * cost_multiplier)
    sell_price = exit_open * (1.0 - config.slippage_rate * cost_multiplier)
    buy_value = buy_price * quantity
    buy_fee = max(
        config.min_commission * cost_multiplier,
        buy_value * config.commission_rate * cost_multiplier,
    )
    sell_value = sell_price * quantity
    sell_fee = max(
        config.min_commission * cost_multiplier,
        sell_value * config.commission_rate * cost_multiplier,
    ) + sell_value * config.stamp_duty_rate * cost_multiplier
    return float((sell_value - sell_fee - buy_value - buy_fee) / (buy_value + buy_fee))


def _simulate_etf_cashflows(
    events: pd.DataFrame,
    *,
    trading_days: int,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> dict[str, Any]:
    cash = config.initial_cash
    initial_cash = cash
    equity_points = [cash]
    for event in events.sort_values(["entry_date", "code"]).to_dict("records"):
        buy_price = float(event["entry_open"]) * (
            1.0 + config.slippage_rate * cost_multiplier
        )
        target_cash = cash * 0.95
        quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
        if quantity <= 0:
            continue
        buy_value = buy_price * quantity
        buy_fee = max(
            config.min_commission * cost_multiplier,
            buy_value * config.commission_rate * cost_multiplier,
        )
        cash -= buy_value + buy_fee
        equity_points.append(cash + buy_value)
        sell_price = float(event["exit_open"]) * (
            1.0 - config.slippage_rate * cost_multiplier
        )
        sell_value = sell_price * quantity
        sell_fee = max(
            config.min_commission * cost_multiplier,
            sell_value * config.commission_rate * cost_multiplier,
        ) + sell_value * config.stamp_duty_rate * cost_multiplier
        cash += sell_value - sell_fee
        equity_points.append(cash)
    total_return = cash / initial_cash - 1.0
    years = max(trading_days / 252.0, 1.0 / 252.0)
    annualized = (1.0 + total_return) ** (1.0 / years) - 1.0
    equity = pd.Series(equity_points, dtype=float)
    drawdown = equity / equity.cummax() - 1.0
    return {
        "portfolio_total_return": float(total_return),
        "portfolio_annualized_return": float(annualized),
        "portfolio_max_drawdown": float(drawdown.min()),
        "portfolio_final_cash": float(cash),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "code",
            "name",
            "timestamp",
            "Open",
            "High",
            "Low",
            "Close",
            "Amount",
            "Volume",
        ]
    )


def _empty_market_state_events() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "code",
            "name",
            "hypothesis_id",
            "signal_date",
            "entry_date",
            "entry_open",
            "exit_date",
            "exit_open",
            "exit_reason",
            "holding_sessions",
            "quantity",
            "return_3d",
            "return_3d_q20",
            "return_3d_z",
            "return_120d",
            "momentum_rank",
            "ma10_distance",
            "ma200_distance",
            "amount_20d",
            "index_close",
            "index_ma120",
            "index_return_3d",
            "index_return_3d_q20",
            "advance_ratio",
            "limit_down_total",
            "limit_down_q80",
            "net_return",
        ]
    )
