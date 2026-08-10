from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd

from .backtest_engine import _performance_metrics
from .storage import Database


COURSE49_V3_POLICY_FREEZE_DATE = "2026-08-09"


def validate_course49(
    database: Database,
    baseline_backtest_id: str,
    *,
    stress_backtest_id: str | None = None,
    historical_holdout_backtest_id: str | None = None,
    policy_freeze_date: str = COURSE49_V3_POLICY_FREEZE_DATE,
) -> dict[str, Any]:
    baseline, parameters, metrics = _load_backtest(database, baseline_backtest_id)
    if str(baseline.get("strategy_id")) not in {
        "course49_v3", "course49_v4", "course49_v5", "course49_v6", "course49_v7",
        "course49_v8", "course49_v9", "course49_v10", "course49_v11"
    }:
        raise ValueError("The validation protocol requires a versioned Course49 acceleration baseline")
    equity = pd.DataFrame(
        database.query(
            "SELECT * FROM backtest_equity WHERE backtest_id=? ORDER BY timestamp",
            (baseline_backtest_id,),
        )
    )
    trades = pd.DataFrame(
        database.query(
            "SELECT * FROM backtest_trades WHERE backtest_id=? ORDER BY timestamp",
            (baseline_backtest_id,),
        )
    )
    folds = _temporal_folds(equity, trades, 4)
    forward = _period_metrics(
        equity,
        trades,
        start=pd.Timestamp(policy_freeze_date) + pd.Timedelta(days=1),
    )
    overall_days = int(metrics.get("trading_days", 0) or 0)
    overall_closed = int(metrics.get("closed_trades", 0) or 0)
    overall_annualized = float(metrics.get("annualized_return", 0.0) or 0.0)
    stress = _stress_result(
        database,
        baseline,
        stress_backtest_id,
    )
    concentration = _trade_concentration(trades)
    holdout = _historical_holdout_result(
        database,
        baseline,
        historical_holdout_backtest_id,
    )
    positive_folds = sum(float(item.get("total_return", 0.0) or 0.0) > 0 for item in folds)
    checks = {
        "overall_250_days": overall_days >= 250,
        "overall_30_closed_trades": overall_closed >= 30,
        "overall_annualized_at_least_20pct": overall_annualized >= 0.20,
        "baseline_median_closed_trade_positive": float(
            concentration.get("median_closed_trade_pnl", 0.0) or 0.0
        ) > 0.0,
        "baseline_positive_without_top3_winners": float(
            concentration.get("pnl_without_top3_winners", 0.0) or 0.0
        ) > 0.0,
        "positive_in_at_least_half_of_temporal_folds": bool(folds)
        and positive_folds >= max(1, len(folds) // 2),
        "forward_60_days": int(forward.get("trading_days", 0) or 0) >= 60,
        "forward_10_closed_trades": int(forward.get("closed_trades", 0) or 0) >= 10,
        "forward_annualized_at_least_20pct": float(
            forward.get("annualized_return", 0.0) or 0.0
        )
        >= 0.20,
        "double_cost_stress_positive": bool(stress.get("passed", False)),
        "historical_holdout_provided": holdout.get("status") != "MISSING",
        "historical_holdout_same_strategy": bool(holdout.get("same_strategy", False)),
        "historical_holdout_non_overlapping": bool(holdout.get("non_overlapping", False)),
        "historical_holdout_250_days": int(holdout.get("trading_days", 0) or 0) >= 250,
        "historical_holdout_20_closed_trades": int(
            holdout.get("closed_trades", 0) or 0
        ) >= 20,
        "historical_holdout_positive": float(holdout.get("total_return", 0.0) or 0.0) > 0.0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "status": "VERIFIED" if not failed else "UNVERIFIED",
        "target_verified": not failed,
        "policy_freeze_date": policy_freeze_date,
        "baseline_backtest_id": baseline_backtest_id,
        "baseline_snapshot_id": str(baseline.get("snapshot_id") or ""),
        "stock_pool_hash": str(parameters.get("stock_pool_hash") or ""),
        "overall": {
            "trading_days": overall_days,
            "closed_trades": overall_closed,
            "annualized_return": overall_annualized,
            "total_return": float(metrics.get("total_return", 0.0) or 0.0),
            "max_drawdown": float(metrics.get("max_drawdown", 0.0) or 0.0),
        },
        "temporal_folds": folds,
        "trade_concentration": concentration,
        "historical_holdout": holdout,
        "forward_out_of_sample": forward,
        "cost_stress": stress,
        "checks": checks,
        "failed_checks": failed,
        "notes": [
            "Dates on or before the policy freeze are historical robustness evidence, not out-of-sample evidence.",
            "The cost stress must replay the same immutable snapshot with an execution cost multiplier of at least 2.",
            "A non-overlapping historical holdout must use the same strategy version and remain profitable.",
            "The baseline must remain profitable after removing its three largest winning trades.",
        ],
    }


def validate_course49_v3(
    database: Database,
    baseline_backtest_id: str,
    *,
    stress_backtest_id: str | None = None,
    historical_holdout_backtest_id: str | None = None,
    policy_freeze_date: str = COURSE49_V3_POLICY_FREEZE_DATE,
) -> dict[str, Any]:
    """Backward-compatible alias for the version-neutral Course49 gate."""
    return validate_course49(
        database,
        baseline_backtest_id,
        stress_backtest_id=stress_backtest_id,
        historical_holdout_backtest_id=historical_holdout_backtest_id,
        policy_freeze_date=policy_freeze_date,
    )


def _load_backtest(
    database: Database,
    backtest_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    rows = database.query("SELECT * FROM backtests WHERE backtest_id=?", (backtest_id,))
    if not rows:
        raise ValueError(f"Unknown backtest: {backtest_id}")
    row = rows[0]
    if str(row.get("status")) != "SUCCEEDED":
        raise ValueError(f"Backtest {backtest_id} has not succeeded")
    try:
        parameters = json.loads(str(row.get("parameters_json") or "{}"))
        metrics = json.loads(str(row.get("metrics_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Backtest {backtest_id} has invalid JSON metadata") from exc
    return row, parameters, metrics


def _period_metrics(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    if equity.empty:
        return {"trading_days": 0, "closed_trades": 0, "annualized_return": 0.0}
    curve = equity.copy()
    curve["timestamp"] = pd.to_datetime(curve["timestamp"], errors="coerce")
    curve = curve.dropna(subset=["timestamp"]).sort_values("timestamp")
    if start is not None:
        curve = curve[curve["timestamp"] >= start]
    if end is not None:
        curve = curve[curve["timestamp"] <= end]
    if curve.empty:
        return {"trading_days": 0, "closed_trades": 0, "annualized_return": 0.0}
    period_trades = trades.copy()
    if not period_trades.empty:
        period_trades["timestamp"] = pd.to_datetime(
            period_trades["timestamp"], errors="coerce"
        )
        period_trades = period_trades.dropna(subset=["timestamp"])
        period_trades = period_trades[
            (period_trades["timestamp"] >= curve["timestamp"].min())
            & (period_trades["timestamp"] <= curve["timestamp"].max())
        ]
    initial_equity = float(pd.to_numeric(curve["equity"], errors="coerce").iloc[0])
    result = _performance_metrics(curve, period_trades, initial_equity)
    result["start_date"] = curve["timestamp"].min().date().isoformat()
    result["end_date"] = curve["timestamp"].max().date().isoformat()
    return result


def _temporal_folds(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    count: int,
) -> list[dict[str, Any]]:
    if equity.empty:
        return []
    timestamps = pd.to_datetime(equity["timestamp"], errors="coerce").dropna().sort_values()
    unique_days = pd.DatetimeIndex(timestamps.dt.normalize().unique())
    if len(unique_days) < count * 2:
        return []
    result = []
    for index, days in enumerate(np.array_split(unique_days, count), start=1):
        if not len(days):
            continue
        item = _period_metrics(
            equity,
            trades,
            start=pd.Timestamp(days[0]),
            end=pd.Timestamp(days[-1]) + pd.Timedelta(hours=23, minutes=59),
        )
        item["fold"] = index
        result.append(item)
    return result


def _stress_result(
    database: Database,
    baseline: dict[str, Any],
    stress_backtest_id: str | None,
) -> dict[str, Any]:
    if not stress_backtest_id:
        return {"status": "MISSING", "passed": False}
    stress, parameters, metrics = _load_backtest(database, stress_backtest_id)
    same_snapshot = str(stress.get("snapshot_id") or "") == str(
        baseline.get("snapshot_id") or ""
    )
    multiplier = float(parameters.get("execution_cost_multiplier", 1.0) or 1.0)
    total_return = float(metrics.get("total_return", 0.0) or 0.0)
    passed = same_snapshot and multiplier >= 2.0 and total_return > 0.0
    return {
        "status": "PASSED" if passed else "FAILED",
        "passed": passed,
        "backtest_id": stress_backtest_id,
        "same_snapshot": same_snapshot,
        "execution_cost_multiplier": multiplier,
        "total_return": total_return,
        "annualized_return": float(metrics.get("annualized_return", 0.0) or 0.0),
    }


def _trade_concentration(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty or "side" not in trades:
        pnl = pd.Series(dtype=float)
    else:
        closed = trades[trades["side"].astype(str).str.upper().isin({"SELL", "COVER"})]
        pnl = pd.to_numeric(closed.get("pnl"), errors="coerce").dropna()
    winners = pnl[pnl > 0].sort_values(ascending=False)
    total = float(pnl.sum()) if not pnl.empty else 0.0
    top3 = float(winners.head(3).sum()) if not winners.empty else 0.0
    return {
        "closed_trades": int(len(pnl)),
        "median_closed_trade_pnl": float(pnl.median()) if not pnl.empty else 0.0,
        "total_realized_pnl": total,
        "top3_winner_pnl": top3,
        "top3_winner_concentration": top3 / total if total > 0 else None,
        "pnl_without_top3_winners": total - top3,
    }


def _historical_holdout_result(
    database: Database,
    baseline: dict[str, Any],
    holdout_backtest_id: str | None,
) -> dict[str, Any]:
    if not holdout_backtest_id:
        return {"status": "MISSING", "passed": False}
    holdout, _, metrics = _load_backtest(database, holdout_backtest_id)
    same_strategy = str(holdout.get("strategy_id") or "") == str(
        baseline.get("strategy_id") or ""
    )
    baseline_start = pd.Timestamp(baseline.get("start_date"))
    baseline_end = pd.Timestamp(baseline.get("end_date"))
    holdout_start = pd.Timestamp(holdout.get("start_date"))
    holdout_end = pd.Timestamp(holdout.get("end_date"))
    non_overlapping = bool(
        holdout_end < baseline_start or holdout_start > baseline_end
    )
    trading_days = int(metrics.get("trading_days", 0) or 0)
    closed_trades = int(metrics.get("closed_trades", 0) or 0)
    total_return = float(metrics.get("total_return", 0.0) or 0.0)
    passed = bool(
        same_strategy
        and non_overlapping
        and trading_days >= 250
        and closed_trades >= 20
        and total_return > 0.0
    )
    return {
        "status": "PASSED" if passed else "FAILED",
        "passed": passed,
        "backtest_id": holdout_backtest_id,
        "same_strategy": same_strategy,
        "non_overlapping": non_overlapping,
        "start_date": holdout_start.date().isoformat(),
        "end_date": holdout_end.date().isoformat(),
        "trading_days": trading_days,
        "closed_trades": closed_trades,
        "total_return": total_return,
        "annualized_return": float(metrics.get("annualized_return", 0.0) or 0.0),
    }
