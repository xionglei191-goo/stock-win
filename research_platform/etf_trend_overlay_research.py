from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .config import PlatformConfig, PortfolioConfig
from .equity_etf_reversal_research import (
    EquityEtfAsset,
    build_cross_sectional_reversal_events,
    discover_current_domestic_equity_etfs,
)
from .etf_pullback_research import DAY_DTYPE, decode_day_bytes
from .storage import Database, ParquetSnapshotStore


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "domestic_equity_etf_trend_pullback_cash_overlay"
FROZEN_PROTOCOL_SHA256 = "5373ca6eb8203220f8617cf64f2315da53f0b4d33a20c8f4f62a9ca201199285"
REPLICATION_START = "2024-07-01"
REPLICATION_END = "2025-07-24"
SNAPSHOT_START = "2019-01-01"
MARKET_SNAPSHOT_ID = "bt_1f2378fe2c984617911770ccb742a05e"
V9_BACKTEST_ID = "ed7e53baab44467d8a6c6ff12212ee0d"


@dataclass(frozen=True)
class ReplicationWindow:
    label: str
    role: str
    start_date: str
    end_date: str


REPLICATION_WINDOW = ReplicationWindow(
    "replication_2024_2025",
    "REPLICATION",
    REPLICATION_START,
    REPLICATION_END,
)


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "replication_only",
        "derivation": (
            "The frozen development entry showed positive total and median returns in all "
            "three windows but insufficient annualized return; this protocol changes only "
            "the exit horizon and tests it on an unopened time window."
        ),
        "population": {
            "definition": "current TDX-listed domestic equity ETFs with local DAY history",
            "classification": "unchanged from domestic_equity_etf_cross_sectional_reversal",
            "known_bias": "current-survivor roster omits ETFs liquidated before 2026-08-10",
            "promotion_block": "historical listed-and-liquidated ETF roster audit is required",
        },
        "data": {
            "source": "local TDX unadjusted DAY files, truncated while reading",
            "snapshot_start": SNAPSHOT_START,
            "replication_end": REPLICATION_END,
            "holdout_window": ["2025-07-25", "2026-08-07"],
            "holdout_rows_excluded_from_snapshot": True,
            "market_snapshot_id": MARKET_SNAPSHOT_ID,
            "v9_backtest_id": V9_BACKTEST_ID,
        },
        "entry": {
            "unchanged_protocol": "domestic_equity_etf_cross_sectional_reversal/1.0.0",
            "minimum_history_sessions": 200,
            "minimum_amount_20d": 50_000_000,
            "close_above_ma200": True,
            "positive_return_120d": True,
            "top_return_120d_cross_section": 0.30,
            "return_3d_maximum": -0.03,
            "return_3d_at_or_below_prior_q10": True,
            "bottom_return_3d_cross_section": 0.20,
            "close_below_ma10": True,
            "maximum_volume_ratio": 2.0,
            "benchmark_close_above_ma120": True,
            "next_open_gap_bounds": [-0.03, 0.03],
            "maximum_daily_candidates": 2,
            "correlation_deduplication": "unchanged 60-session 0.95 threshold",
        },
        "exit": {
            "signal_time": "daily close",
            "execution": "next trading-day raw open",
            "fixed_stop_close": -0.08,
            "trend_failure": "two consecutive closes below MA50",
            "trailing_activation": 0.12,
            "trailing_drawdown": 0.06,
            "maximum_holding_sessions": 60,
            "t_plus_one": True,
        },
        "portfolio": {
            "target_weight": 0.10,
            "maximum_etf_positions": 2,
            "maximum_etf_overlay_weight": 0.20,
            "v9_priority": "all exits, then unchanged V9 entries, then ETF entries",
            "v9_trade_contract": "dates, codes, quantities, prices, and fees must be unchanged",
            "costs": "commission, minimum commission, stamp duty, fixed slippage",
        },
        "replication_gate": {
            "minimum_portfolio_trades": 15,
            "minimum_standalone_annualized_return": 0.05,
            "positive_standalone_total_return": True,
            "positive_median_trade": True,
            "positive_ex_top3_contribution": True,
            "maximum_standalone_drawdown": -0.10,
            "minimum_fill_rate": 0.60,
            "minimum_overlay_incremental_total_return": 0.02,
            "maximum_combined_drawdown": -0.10,
            "maximum_daily_return_correlation_with_v9": 0.60,
            "requires_exact_v9_cashflow_reproduction": True,
            "passing_action": "audit survivor roster before opening final holdout",
        },
        "window": asdict(REPLICATION_WINDOW),
        "opening_rule": "replication, survivor audit, then final holdout",
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen protocol already exists with different content: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def create_replication_snapshot(
    *,
    tdx_root: Path,
    output_root: Path,
    assets: Sequence[EquityEtfAsset] | None = None,
    start_date: str = SNAPSHOT_START,
    end_date: str = REPLICATION_END,
) -> dict[str, Any]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end > pd.Timestamp(REPLICATION_END):
        raise ValueError("Replication snapshot cannot include final holdout dates")
    selected_assets = tuple(assets or discover_current_domestic_equity_etfs(tdx_root))
    if not selected_assets:
        raise ValueError("No domestic equity ETF assets were discovered")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".etf_trend_", dir=str(output_root)))
    try:
        frames: list[pd.DataFrame] = []
        sources: list[dict[str, Any]] = []
        for asset in selected_assets:
            path = Path(tdx_root) / "vipdoc" / asset.market / "lday" / f"{asset.local_code}.day"
            prefix, source = _read_day_prefix(path, end)
            decoded = decode_day_bytes(prefix, asset)
            decoded = decoded.loc[pd.to_datetime(decoded["timestamp"]).between(start, end)]
            if not decoded.empty:
                frames.append(decoded)
            sources.append({"code": asset.code, "name": asset.name, **source, "rows": len(decoded)})
        bars = pd.concat(frames, ignore_index=True).sort_values(
            ["code", "timestamp"]
        ).reset_index(drop=True)
        _validate_snapshot_bars(bars, end)
        bars_path = staging / "bars.parquet"
        bars.to_parquet(bars_path, index=False)
        bars_hash = _file_sha256(bars_path)
        identity = {
            "protocol_version": PROTOCOL_VERSION,
            "start_date": str(start.date()),
            "end_date": str(end.date()),
            "survivor_roster_asof": "2026-08-10",
            "assets": [asdict(asset) for asset in selected_assets],
            "source_prefixes": sources,
            "bars_sha256": bars_hash,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        manifest = {
            **identity,
            "snapshot_id": snapshot_id,
            "bars_rows": int(len(bars)),
            "bars_codes": int(bars["code"].nunique()),
            "duplicate_keys": 0,
            "invalid_ohlc": 0,
            "holdout_rows_included": 0,
            "known_bias": "current-survivor roster",
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        target = output_root / snapshot_id
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("bars_sha256") != bars_hash:
                raise ValueError(f"Immutable ETF trend snapshot collision: {snapshot_id}")
            return existing
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_replication_snapshot(snapshot_dir: Path) -> pd.DataFrame:
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    path = snapshot_dir / "bars.parquet"
    actual_hash = _file_sha256(path)
    if actual_hash != manifest.get("bars_sha256"):
        raise ValueError(
            "ETF trend snapshot hash mismatch: "
            f"expected={manifest.get('bars_sha256')}, actual={actual_hash}"
        )
    bars = pd.read_parquet(path)
    _validate_snapshot_bars(bars, pd.Timestamp(REPLICATION_END))
    return bars


def build_trend_overlay_events(
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    base = build_cross_sectional_reversal_events(
        bars,
        market_index,
        execution_config=execution_config,
        execution_cost_multiplier=execution_cost_multiplier,
    )
    if base.empty:
        return _empty_events()
    start = pd.Timestamp(REPLICATION_START)
    end = pd.Timestamp(REPLICATION_END)
    events = base.loc[pd.to_datetime(base["signal_date"]).between(start, end)].copy()
    events["overlay_selected"] = events["selected"] & pd.to_numeric(
        events["daily_rank"], errors="coerce"
    ).le(2)
    return _annotate_trend_execution(
        events,
        bars,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
    )


def evaluate_replication(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    config = execution_config or PortfolioConfig()
    selected = events.loc[events["overlay_selected"]]
    end = pd.Timestamp(REPLICATION_END)
    complete = selected.loc[
        selected["executable"] & pd.to_datetime(selected["exit_date"]).le(end)
    ].copy()
    calendar = _calendar(market_index)
    portfolio = simulate_etf_sleeve(
        complete,
        bars,
        calendar,
        initial_cash=float(config.initial_cash),
        config=config,
        cost_multiplier=execution_cost_multiplier,
    )
    returns = pd.Series(portfolio.pop("accepted_trade_returns"), dtype=float)
    portfolio.pop("equity")
    ex_top3 = returns.sort_values(ascending=False).iloc[3:]
    blocked_daily_capacity = int(events["blocked_daily_capacity"].sum()) + int(
        (events["selected"] & ~events["overlay_selected"]).sum()
    )
    return {
        "window": asdict(REPLICATION_WINDOW),
        "raw_signals": int(len(events)),
        "selected_signals": int(len(selected)),
        "executable_signals": int(len(complete)),
        "blocked_correlation": int(events["blocked_correlation"].sum()),
        "blocked_daily_capacity": blocked_daily_capacity,
        "blocked_entry_gap": int(selected["blocked_entry_gap"].sum()),
        "blocked_missing_bars": int(
            selected[["blocked_missing_entry", "blocked_missing_exit"]].any(axis=1).sum()
        ),
        "fill_rate": float(len(complete) / len(selected)) if len(selected) else 0.0,
        "median_trade_return": float(returns.median()) if not returns.empty else None,
        "mean_trade_return": float(returns.mean()) if not returns.empty else None,
        "win_rate": float((returns > 0).mean()) if not returns.empty else None,
        "ex_top3_contribution": float(ex_top3.sum() * 0.10),
        **portfolio,
    }


def simulate_etf_sleeve(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: Sequence[Any],
    *,
    initial_cash: float,
    config: PortfolioConfig,
    cost_multiplier: float,
    maximum_positions: int = 2,
) -> dict[str, Any]:
    if maximum_positions <= 0:
        raise ValueError("maximum_positions must be positive")
    cash = float(initial_cash)
    positions: dict[str, dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    closes = _close_pivot(bars)
    entries = {
        pd.Timestamp(day).normalize(): group.sort_values(
            ["score", "code"], ascending=[False, True]
        )
        for day, group in events.groupby("entry_date", sort=True)
    }
    for day in sorted({pd.Timestamp(value).normalize() for value in calendar}):
        for code in sorted(
            [code for code, position in positions.items() if position["exit_date"] <= day]
        ):
            position = positions.pop(code)
            proceeds = _sell_proceeds(
                float(position["exit_open"]), int(position["quantity"]), config, cost_multiplier
            )
            cash += proceeds
            accepted[position["accepted_index"]]["proceeds"] = proceeds
        for _, event in entries.get(day, pd.DataFrame()).iterrows():
            code = str(event["code"])
            if code in positions or len(positions) >= maximum_positions:
                continue
            quantity, cost = _buy_quantity_and_cost(
                float(event["entry_open"]), initial_cash * 0.10, config, cost_multiplier
            )
            if quantity <= 0 or cost > cash:
                continue
            cash -= cost
            accepted_index = len(accepted)
            accepted.append({"code": code, "cost": cost, "proceeds": np.nan})
            positions[code] = {
                "quantity": quantity,
                "exit_date": pd.Timestamp(event["exit_date"]).normalize(),
                "exit_open": float(event["exit_open"]),
                "accepted_index": accepted_index,
                "last_close": float(event["entry_open"]),
            }
        market_value = _mark_positions(positions, closes, day)
        equity_rows.append({"timestamp": day, "equity": cash + market_value})
    returns = [
        float((item["proceeds"] - item["cost"]) / item["cost"])
        for item in accepted
        if np.isfinite(item["proceeds"])
    ]
    metrics = _equity_metrics(pd.DataFrame(equity_rows), initial_cash)
    return {
        "portfolio_trades": int(len(returns)),
        **metrics,
        "accepted_trade_returns": returns,
        "equity": equity_rows,
    }


def audit_v9_capital_efficiency(
    database_path: Path,
    windows: Sequence[tuple[str, str, float]],
) -> dict[str, Any]:
    import sqlite3

    rows: list[dict[str, Any]] = []
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        for label, backtest_id, weight in windows:
            backtest = connection.execute(
                "SELECT metrics_json FROM backtests WHERE backtest_id=?", (backtest_id,)
            ).fetchone()
            if backtest is None:
                raise ValueError(f"Missing V9 backtest: {backtest_id}")
            metrics = json.loads(str(backtest["metrics_json"] or "{}"))
            equity = pd.read_sql_query(
                "SELECT timestamp,equity,cash,positions FROM backtest_equity "
                "WHERE backtest_id=? AND strategy_id='course49_v9' ORDER BY timestamp",
                connection,
                params=(backtest_id,),
            )
            trades = pd.read_sql_query(
                "SELECT pnl FROM backtest_trades WHERE backtest_id=? "
                "AND strategy_id='course49_v9' AND side='SELL' ORDER BY timestamp",
                connection,
                params=(backtest_id,),
            )
            equity["exposure"] = (equity["equity"] - equity["cash"]) / equity["equity"]
            pnl = pd.to_numeric(trades["pnl"], errors="coerce").dropna().sort_values(
                ascending=False
            )
            rows.append(
                {
                    "label": label,
                    "backtest_id": backtest_id,
                    "evaluation_weight": float(weight),
                    "annualized_return": float(metrics["annualized_return"]),
                    "total_return": float(metrics["total_return"]),
                    "max_drawdown": float(metrics["max_drawdown"]),
                    "closed_trades": int(metrics["closed_trades"]),
                    "mean_exposure": float(equity["exposure"].mean()),
                    "zero_position_days": float(equity["positions"].eq(0).mean()),
                    "two_plus_position_days": float(equity["positions"].ge(2).mean()),
                    "median_trade_pnl": float(pnl.median()),
                    "ex_top3_pnl": float(pnl.iloc[3:].sum()),
                }
            )
    return {
        "windows": rows,
        "weighted_annualized_return": float(
            sum(row["evaluation_weight"] * row["annualized_return"] for row in rows)
        ),
        "decision": (
            "ADD_LOW_CORRELATION_CASH_OVERLAY"
            if all(row["mean_exposure"] < 0.12 for row in rows)
            else "DO_NOT_ADD_OVERLAY"
        ),
    }


def simulate_v9_overlay(
    v9_trades: pd.DataFrame,
    v9_bars: pd.DataFrame,
    etf_events: pd.DataFrame,
    etf_bars: pd.DataFrame,
    calendar: Sequence[Any],
    *,
    initial_cash: float,
    config: PortfolioConfig,
    cost_multiplier: float = 1.0,
    maximum_etf_positions: int = 2,
) -> dict[str, Any]:
    if maximum_etf_positions <= 0:
        raise ValueError("maximum_etf_positions must be positive")
    cash = float(initial_cash)
    v9_positions: dict[str, dict[str, Any]] = {}
    etf_positions: dict[str, dict[str, Any]] = {}
    accepted_etf: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    v9_cash_blocked = 0
    v9_closes = _close_pivot(v9_bars)
    etf_closes = _close_pivot(etf_bars)
    trades = v9_trades.copy()
    trades["timestamp"] = pd.to_datetime(trades["timestamp"]).dt.normalize()
    if not trades.empty and "side" not in trades:
        raise ValueError("V9 trades must contain a side column")
    trades_by_day = {day: group for day, group in trades.groupby("timestamp", sort=True)}
    etf_entries = {
        pd.Timestamp(day).normalize(): group.sort_values(
            ["score", "code"], ascending=[False, True]
        )
        for day, group in etf_events.groupby("entry_date", sort=True)
    }
    for day in sorted({pd.Timestamp(value).normalize() for value in calendar}):
        day_trades = trades_by_day.get(day, pd.DataFrame())
        day_sells = day_trades.loc[day_trades["side"].eq("SELL")] if not day_trades.empty else day_trades
        day_buys = day_trades.loc[day_trades["side"].eq("BUY")] if not day_trades.empty else day_trades
        for _, trade in day_sells.iterrows():
            code = str(trade["code"])
            if code not in v9_positions:
                raise ValueError(f"V9 sell has no open position: {code}/{day.date()}")
            position = v9_positions.pop(code)
            if int(position["quantity"]) != int(trade["quantity"]):
                raise ValueError(f"V9 sell quantity changed: {code}/{day.date()}")
            cash += float(trade["price"]) * int(trade["quantity"]) - float(trade["fees"])
        for code in sorted(
            [code for code, position in etf_positions.items() if position["exit_date"] <= day]
        ):
            position = etf_positions.pop(code)
            proceeds = _sell_proceeds(
                float(position["exit_open"]), int(position["quantity"]), config, cost_multiplier
            )
            cash += proceeds
            accepted_etf[position["accepted_index"]]["proceeds"] = proceeds
        for _, trade in day_buys.iterrows():
            code = str(trade["code"])
            cost = float(trade["price"]) * int(trade["quantity"]) + float(trade["fees"])
            if cost > cash + 1e-8:
                v9_cash_blocked += 1
                continue
            cash -= cost
            v9_positions[code] = {
                "quantity": int(trade["quantity"]),
                "last_close": float(trade["price"]),
            }
        for _, event in etf_entries.get(day, pd.DataFrame()).iterrows():
            code = str(event["code"])
            if code in etf_positions or len(etf_positions) >= maximum_etf_positions:
                continue
            quantity, cost = _buy_quantity_and_cost(
                float(event["entry_open"]), initial_cash * 0.10, config, cost_multiplier
            )
            if quantity <= 0 or cost > cash:
                continue
            cash -= cost
            accepted_index = len(accepted_etf)
            accepted_etf.append({"code": code, "cost": cost, "proceeds": np.nan})
            etf_positions[code] = {
                "quantity": quantity,
                "exit_date": pd.Timestamp(event["exit_date"]).normalize(),
                "exit_open": float(event["exit_open"]),
                "accepted_index": accepted_index,
                "last_close": float(event["entry_open"]),
            }
        market_value = _mark_positions(v9_positions, v9_closes, day)
        market_value += _mark_positions(etf_positions, etf_closes, day)
        equity_rows.append(
            {
                "timestamp": day,
                "equity": cash + market_value,
                "cash": cash,
                "v9_positions": len(v9_positions),
                "etf_positions": len(etf_positions),
            }
        )
    metrics = _equity_metrics(pd.DataFrame(equity_rows), initial_cash)
    etf_returns = [
        float((item["proceeds"] - item["cost"]) / item["cost"])
        for item in accepted_etf
        if np.isfinite(item["proceeds"])
    ]
    return {
        **metrics,
        "v9_cash_blocked": int(v9_cash_blocked),
        "v9_trade_rows_processed": int(len(v9_trades)),
        "etf_trades": int(len(etf_returns)),
        "etf_trade_returns": etf_returns,
        "equity": equity_rows,
    }


def assess_replication(
    report: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    checks = {
        "minimum_trades": int(report["portfolio_trades"]) >= 15,
        "minimum_standalone_annualized_return": float(
            report["portfolio_annualized_return"]
        )
        >= 0.05,
        "positive_standalone_total_return": float(report["portfolio_total_return"]) > 0.0,
        "positive_median_trade": (
            report["median_trade_return"] is not None
            and float(report["median_trade_return"]) > 0.0
        ),
        "positive_ex_top3_contribution": float(report["ex_top3_contribution"]) > 0.0,
        "maximum_standalone_drawdown": float(report["portfolio_max_drawdown"]) >= -0.10,
        "minimum_fill_rate": float(report["fill_rate"]) >= 0.60,
        "minimum_overlay_incremental_return": float(overlay["incremental_total_return"]) >= 0.02,
        "maximum_combined_drawdown": float(overlay["combined_max_drawdown"]) >= -0.10,
        "maximum_v9_correlation": (
            overlay["daily_return_correlation"] is not None
            and float(overlay["daily_return_correlation"]) <= 0.60
        ),
        "exact_v9_reproduction": bool(overlay["v9_reproduction_match"]),
        "no_v9_cash_block": int(overlay["v9_cash_blocked"]) == 0,
    }
    passed = all(checks.values())
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "decision": "REQUIRE_SURVIVOR_AUDIT" if passed else "REJECT",
        "replication_qualified": passed,
        "checks": checks,
        "survivor_audit_required": passed,
        "holdout_opened": False,
    }


def run_frozen_replication(
    config: PlatformConfig,
    database: Database,
    *,
    snapshot_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    protocol_hash = _file_sha256(output_dir / "protocol.json")
    if protocol_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen ETF trend overlay protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={protocol_hash}"
        )
    bars = load_replication_snapshot(snapshot_dir)
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    store = ParquetSnapshotStore(config, database)
    market = store.load_records(MARKET_SNAPSHOT_ID, "market_index")
    events = build_trend_overlay_events(bars, market, execution_config=config.portfolio)
    report = evaluate_replication(events, bars, market, execution_config=config.portfolio)
    v9_trades = pd.DataFrame(
        database.query(
            "SELECT rowid,* FROM backtest_trades WHERE backtest_id=? "
            "AND strategy_id='course49_v9' ORDER BY timestamp,rowid",
            (V9_BACKTEST_ID,),
        )
    )
    v9_metrics_rows = database.query(
        "SELECT metrics_json FROM backtests WHERE backtest_id=?", (V9_BACKTEST_ID,)
    )
    if not v9_metrics_rows:
        raise ValueError(f"Missing V9 baseline backtest: {V9_BACKTEST_ID}")
    v9_metrics = json.loads(str(v9_metrics_rows[0]["metrics_json"] or "{}"))
    v9_raw = store.load_records(MARKET_SNAPSHOT_ID, "daily_raw")
    traded_codes = set(v9_trades["code"].astype(str))
    v9_raw = v9_raw.loc[v9_raw["code"].astype(str).isin(traded_codes)].copy()
    complete = events.loc[
        events["overlay_selected"]
        & events["executable"]
        & pd.to_datetime(events["exit_date"]).le(pd.Timestamp(REPLICATION_END))
    ].copy()
    calendar = _calendar(market)
    baseline = simulate_v9_overlay(
        v9_trades,
        v9_raw,
        complete.iloc[0:0],
        bars,
        calendar,
        initial_cash=float(v9_metrics["initial_cash"]),
        config=config.portfolio,
    )
    combined = simulate_v9_overlay(
        v9_trades,
        v9_raw,
        complete,
        bars,
        calendar,
        initial_cash=float(v9_metrics["initial_cash"]),
        config=config.portfolio,
    )
    sleeve = simulate_etf_sleeve(
        complete,
        bars,
        calendar,
        initial_cash=float(v9_metrics["initial_cash"]),
        config=config.portfolio,
        cost_multiplier=1.0,
    )
    baseline_equity = pd.DataFrame(baseline.pop("equity"))
    combined_equity = pd.DataFrame(combined.pop("equity"))
    sleeve_equity = pd.DataFrame(sleeve.pop("equity"))
    baseline_returns = baseline_equity.set_index("timestamp")["equity"].pct_change()
    sleeve_returns = sleeve_equity.set_index("timestamp")["equity"].pct_change()
    correlation = baseline_returns.corr(sleeve_returns)
    overlay = {
        "v9_baseline_total_return": float(v9_metrics["total_return"]),
        "v9_recomputed_total_return": float(baseline["portfolio_total_return"]),
        "v9_reproduction_match": abs(
            float(baseline["portfolio_total_return"]) - float(v9_metrics["total_return"])
        )
        < 1e-10,
        "combined_total_return": float(combined["portfolio_total_return"]),
        "combined_annualized_return": float(combined["portfolio_annualized_return"]),
        "combined_max_drawdown": float(combined["portfolio_max_drawdown"]),
        "incremental_total_return": float(
            combined["portfolio_total_return"] - baseline["portfolio_total_return"]
        ),
        "daily_return_correlation": float(correlation) if np.isfinite(correlation) else None,
        "v9_cash_blocked": int(combined["v9_cash_blocked"]),
        "v9_trade_rows_processed": int(combined["v9_trade_rows_processed"]),
        "etf_trades": int(combined["etf_trades"]),
    }
    decision = assess_replication(report, overlay)
    events_path = output_dir / "replication_events.parquet"
    events.to_parquet(events_path, index=False)
    equity_path = output_dir / "combined_equity.parquet"
    combined_equity.to_parquet(equity_path, index=False)
    payload = {
        "protocol_sha256": protocol_hash,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_rows": int(len(bars)),
        "snapshot_codes": int(bars["code"].nunique()),
        "snapshot_bars_sha256": manifest["bars_sha256"],
        "report": report,
        "overlay": overlay,
        "decision": decision,
        "events_sha256": _file_sha256(events_path),
        "combined_equity_sha256": _file_sha256(equity_path),
    }
    (output_dir / "replication_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return payload


def _annotate_trend_execution(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> pd.DataFrame:
    result = events.copy()
    for column in ("blocked_missing_entry", "blocked_entry_gap", "blocked_missing_exit", "executable"):
        result[column] = False
    for column in ("entry_open", "entry_gap", "exit_open", "net_return"):
        result[column] = np.nan
    result["entry_date"] = pd.NaT
    result["exit_date"] = pd.NaT
    result["exit_reason"] = ""
    result["holding_sessions"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    result["quantity"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    prepared = bars.copy()
    prepared["timestamp"] = pd.to_datetime(prepared["timestamp"]).dt.normalize()
    prepared.sort_values(["code", "timestamp"], inplace=True)
    prepared["ma50"] = prepared.groupby("code", sort=False)["Close"].transform(
        lambda values: values.rolling(50, min_periods=50).mean()
    )
    selected = result.loc[result["overlay_selected"]]
    for code, indexes in selected.groupby("code", sort=False).groups.items():
        history = prepared.loc[prepared["code"].eq(code)].reset_index(drop=True)
        positions = {pd.Timestamp(value): index for index, value in history["timestamp"].items()}
        for event_index in indexes:
            signal_date = pd.Timestamp(result.at[event_index, "signal_date"]).normalize()
            signal_position = positions.get(signal_date)
            if signal_position is None or signal_position + 1 >= len(history):
                result.at[event_index, "blocked_missing_entry"] = True
                continue
            entry_position = signal_position + 1
            signal = history.iloc[signal_position]
            entry = history.iloc[entry_position]
            entry_open = float(entry["Open"])
            entry_gap = entry_open / float(signal["Close"]) - 1.0
            result.at[event_index, "entry_date"] = pd.Timestamp(entry["timestamp"])
            result.at[event_index, "entry_open"] = entry_open
            result.at[event_index, "entry_gap"] = entry_gap
            if not -0.03 <= entry_gap <= 0.03:
                result.at[event_index, "blocked_entry_gap"] = True
                continue
            highest_close = entry_open
            below_ma50 = 0
            exit_position: int | None = None
            exit_reason = ""
            for position in range(entry_position, min(entry_position + 60, len(history))):
                observed = history.iloc[position]
                close = float(observed["Close"])
                highest_close = max(highest_close, close)
                below_ma50 = below_ma50 + 1 if close < float(observed["ma50"]) else 0
                if close <= entry_open * 0.92:
                    exit_reason = "FIXED_STOP_CLOSE"
                elif below_ma50 >= 2:
                    exit_reason = "MA50_TWO_DAY_BREAK"
                elif highest_close >= entry_open * 1.12 and close <= highest_close * 0.94:
                    exit_reason = "TRAILING_PROFIT"
                elif position - entry_position + 1 >= 60:
                    exit_reason = "MAX_HOLDING"
                if exit_reason:
                    exit_position = position + 1
                    break
            if exit_position is None:
                exit_position = entry_position + 60
                exit_reason = "MAX_HOLDING"
            if exit_position >= len(history):
                result.at[event_index, "blocked_missing_exit"] = True
                continue
            exit_bar = history.iloc[exit_position]
            quantity, cost = _buy_quantity_and_cost(
                entry_open, config.initial_cash * 0.10, config, cost_multiplier
            )
            proceeds = _sell_proceeds(float(exit_bar["Open"]), quantity, config, cost_multiplier)
            if quantity <= 0 or cost <= 0:
                result.at[event_index, "blocked_missing_entry"] = True
                continue
            result.at[event_index, "exit_date"] = pd.Timestamp(exit_bar["timestamp"])
            result.at[event_index, "exit_open"] = float(exit_bar["Open"])
            result.at[event_index, "exit_reason"] = exit_reason
            result.at[event_index, "holding_sessions"] = int(exit_position - entry_position)
            result.at[event_index, "quantity"] = int(quantity)
            result.at[event_index, "net_return"] = float((proceeds - cost) / cost)
            result.at[event_index, "executable"] = True
    return result


def _read_day_prefix(path: Path, end: pd.Timestamp) -> tuple[bytes, dict[str, Any]]:
    chunks: list[bytes] = []
    previous_date = 0
    maximum_decoded_date: int | None = None
    cutoff = int(end.strftime("%Y%m%d"))
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(DAY_DTYPE.itemsize)
            if not chunk:
                break
            if len(chunk) != DAY_DTYPE.itemsize:
                raise ValueError(f"Truncated DAY record: {path}")
            date_value = int.from_bytes(chunk[:4], "little", signed=False)
            if date_value < previous_date:
                raise ValueError(f"Unsorted DAY records: {path}")
            previous_date = date_value
            if date_value > cutoff:
                break
            chunks.append(chunk)
            maximum_decoded_date = date_value
    payload = b"".join(chunks)
    return payload, {
        "path": str(Path(path).resolve()),
        "prefix_bytes": len(payload),
        "prefix_sha256": hashlib.sha256(payload).hexdigest(),
        "maximum_decoded_date": (
            pd.to_datetime(str(maximum_decoded_date), format="%Y%m%d").date().isoformat()
            if maximum_decoded_date is not None
            else None
        ),
    }


def _validate_snapshot_bars(bars: pd.DataFrame, end: pd.Timestamp) -> None:
    if bars.empty:
        raise ValueError("ETF trend snapshot is empty")
    if bars.duplicated(["code", "timestamp"]).any():
        raise ValueError("ETF trend snapshot contains duplicate code-session keys")
    if pd.to_datetime(bars["timestamp"]).max() > end:
        raise ValueError("ETF trend snapshot contains a final-holdout row")
    invalid = (
        bars["Low"].gt(bars[["Open", "Close"]].min(axis=1))
        | bars["High"].lt(bars[["Open", "Close"]].max(axis=1))
        | bars["Low"].gt(bars["High"])
        | bars[["Open", "High", "Low", "Close"]].le(0.0).any(axis=1)
    )
    if invalid.any():
        raise ValueError(f"ETF trend snapshot contains {int(invalid.sum())} invalid OHLC rows")


def _calendar(market_index: pd.DataFrame) -> list[pd.Timestamp]:
    values = pd.to_datetime(market_index["timestamp"], errors="coerce").dropna().dt.normalize()
    return sorted(values.loc[values.between(REPLICATION_START, REPLICATION_END)].unique())


def _close_pivot(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.loc[:, ["code", "timestamp", "Close"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.normalize()
    return frame.pivot(index="timestamp", columns="code", values="Close")


def _mark_positions(
    positions: dict[str, dict[str, Any]],
    closes: pd.DataFrame,
    day: pd.Timestamp,
) -> float:
    value = 0.0
    for code, position in positions.items():
        if day in closes.index and code in closes and pd.notna(closes.at[day, code]):
            position["last_close"] = float(closes.at[day, code])
        value += int(position["quantity"]) * float(position["last_close"])
    return value


def _buy_quantity_and_cost(
    raw_open: float,
    target_cash: float,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> tuple[int, float]:
    buy_price = raw_open * (1.0 + config.slippage_rate * cost_multiplier)
    quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
    if quantity <= 0:
        return 0, 0.0
    value = buy_price * quantity
    fee = max(
        config.min_commission * cost_multiplier,
        value * config.commission_rate * cost_multiplier,
    )
    return quantity, float(value + fee)


def _sell_proceeds(
    raw_open: float,
    quantity: int,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> float:
    sell_price = raw_open * (1.0 - config.slippage_rate * cost_multiplier)
    value = sell_price * quantity
    fee = max(
        config.min_commission * cost_multiplier,
        value * config.commission_rate * cost_multiplier,
    ) + value * config.stamp_duty_rate * cost_multiplier
    return float(value - fee)


def _equity_metrics(equity: pd.DataFrame, initial_cash: float) -> dict[str, Any]:
    if equity.empty:
        return {
            "portfolio_total_return": 0.0,
            "portfolio_annualized_return": 0.0,
            "portfolio_max_drawdown": 0.0,
            "portfolio_final_equity": float(initial_cash),
        }
    curve = pd.to_numeric(equity["equity"], errors="coerce").ffill()
    total_return = float(curve.iloc[-1] / initial_cash - 1.0)
    annualized = float((1.0 + total_return) ** (252.0 / len(curve)) - 1.0)
    drawdown = float((curve / curve.cummax() - 1.0).min())
    return {
        "portfolio_total_return": total_return,
        "portfolio_annualized_return": annualized,
        "portfolio_max_drawdown": drawdown,
        "portfolio_final_equity": float(curve.iloc[-1]),
    }


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "code",
            "signal_date",
            "score",
            "daily_rank",
            "selected",
            "overlay_selected",
            "blocked_correlation",
            "blocked_daily_capacity",
            "blocked_missing_entry",
            "blocked_entry_gap",
            "blocked_missing_exit",
            "executable",
            "entry_date",
            "exit_date",
            "net_return",
        ]
    )


def _file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
