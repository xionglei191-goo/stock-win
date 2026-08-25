from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .config import PlatformConfig, PortfolioConfig
from .equity_etf_reversal_research import load_development_market_index
from .storage import Database


TARGET_WEIGHT = 0.10
MAXIMUM_POSITIONS = 3


def validate_saved_development(
    config: PlatformConfig,
    database: Database,
    *,
    snapshot_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    result_path = output_dir / "development_result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    base_path = output_dir / "development_events.parquet"
    stress_path = output_dir / "development_events_stress.parquet"
    file_checks = {
        "protocol_sha256": _file_sha256(output_dir / "protocol.json")
        == result["protocol_sha256"],
        "base_events_sha256": _file_sha256(base_path)
        == result["base_events_sha256"],
        "stress_events_sha256": _file_sha256(stress_path)
        == result["stress_events_sha256"],
    }
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    bars_path = Path(snapshot_dir) / "bars.parquet"
    file_checks["bars_sha256"] = _file_sha256(bars_path) == manifest["bars_sha256"]
    if not all(file_checks.values()):
        raise ValueError(f"Independent input hash check failed: {file_checks}")

    bars = pd.read_parquet(bars_path)
    base_events = pd.read_parquet(base_path)
    stress_events = pd.read_parquet(stress_path)
    market_index, market_sources = load_development_market_index(config, database)
    base_checks = _validate_reports(
        base_events,
        bars,
        market_index,
        result["base_reports"],
        config.portfolio,
        cost_multiplier=1.0,
    )
    stress_checks = _validate_reports(
        stress_events,
        bars,
        market_index,
        result["stress_reports"],
        config.portfolio,
        cost_multiplier=2.0,
    )
    payload = {
        "validation_type": "independent cash ledger and mark-to-market recomputation",
        "does_not_call_research_simulator": True,
        "file_checks": file_checks,
        "base_checks": base_checks,
        "stress_checks": stress_checks,
        "market_sources": market_sources,
        "all_checks_passed": bool(
            all(item["passed"] for item in base_checks)
            and all(item["passed"] for item in stress_checks)
        ),
    }
    validation_path = output_dir / "independent_validation.json"
    validation_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return payload


def _validate_reports(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    reports: Sequence[Mapping[str, Any]],
    config: PortfolioConfig,
    *,
    cost_multiplier: float,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for report in reports:
        recomputed = _recompute_window(
            events,
            bars,
            market_index,
            report["window"],
            config,
            cost_multiplier=cost_multiplier,
        )
        comparisons = {
            key: _close(recomputed[key], report[key])
            for key in (
                "portfolio_trades",
                "portfolio_total_return",
                "portfolio_annualized_return",
                "portfolio_max_drawdown",
                "portfolio_final_equity",
                "median_trade_return",
                "mean_trade_return",
                "win_rate",
                "ex_top3_contribution",
            )
        }
        comparisons["saved_quantities_match"] = bool(recomputed["saved_quantities_match"])
        checks.append(
            {
                "window": report["window"]["label"],
                "comparisons": comparisons,
                "maximum_absolute_error": float(
                    max(
                        abs(float(recomputed[key]) - float(report[key]))
                        for key in (
                            "portfolio_total_return",
                            "portfolio_annualized_return",
                            "portfolio_max_drawdown",
                            "portfolio_final_equity",
                            "median_trade_return",
                            "mean_trade_return",
                            "win_rate",
                            "ex_top3_contribution",
                        )
                    )
                ),
                "recomputed": recomputed,
                "passed": all(comparisons.values()),
            }
        )
    return checks


def _recompute_window(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    window: Mapping[str, Any],
    config: PortfolioConfig,
    *,
    cost_multiplier: float,
) -> dict[str, Any]:
    config = replace(config, stamp_duty_rate=0.0)
    start = pd.Timestamp(window["start_date"]).normalize()
    end = pd.Timestamp(window["end_date"]).normalize()
    accepted_events = events.loc[
        events["selected"]
        & events["executable"]
        & pd.to_datetime(events["entry_date"]).between(start, end)
        & pd.to_datetime(events["exit_date"]).le(end)
    ].copy()
    accepted_events.sort_values(
        ["entry_date", "daily_rank", "entry_gap", "code"], inplace=True
    )
    entry_groups = {
        pd.Timestamp(day).normalize(): group
        for day, group in accepted_events.groupby("entry_date", sort=True)
    }
    close_frame = bars.loc[:, ["code", "timestamp", "Close"]].copy()
    close_frame["timestamp"] = pd.to_datetime(close_frame["timestamp"]).dt.normalize()
    close_lookup = close_frame.set_index(["timestamp", "code"])["Close"]
    calendar = sorted(
        {
            pd.Timestamp(day).normalize()
            for day in pd.to_datetime(market_index["timestamp"], errors="coerce").dropna()
            if start <= pd.Timestamp(day).normalize() <= end
        }
    )

    initial_cash = float(config.initial_cash)
    cash = initial_cash
    positions: dict[str, dict[str, Any]] = {}
    realized: list[dict[str, float]] = []
    equity_values: list[float] = []
    saved_quantities_match = True
    for day in calendar:
        for code in sorted(
            [code for code, position in positions.items() if position["exit_date"] <= day]
        ):
            position = positions.pop(code)
            sell_price = position["exit_open"] * (
                1.0 - config.slippage_rate * cost_multiplier
            )
            sell_value = sell_price * position["quantity"]
            sell_fee = max(
                config.min_commission * cost_multiplier,
                sell_value * config.commission_rate * cost_multiplier,
            )
            proceeds = sell_value - sell_fee
            cash += proceeds
            pnl = proceeds - position["entry_cost"]
            realized[position["accepted_index"]]["pnl"] = pnl
        for _, event in entry_groups.get(day, pd.DataFrame()).iterrows():
            if len(positions) >= MAXIMUM_POSITIONS:
                raise AssertionError("Frozen selected events exceeded the position limit")
            buy_price = float(event["entry_open"]) * (
                1.0 + config.slippage_rate * cost_multiplier
            )
            target_cash = min(initial_cash * TARGET_WEIGHT, cash)
            quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
            saved_quantities_match &= quantity == int(event["quantity"])
            buy_value = buy_price * quantity
            buy_fee = max(
                config.min_commission * cost_multiplier,
                buy_value * config.commission_rate * cost_multiplier,
            )
            entry_cost = buy_value + buy_fee
            if entry_cost > cash:
                raise AssertionError("Independent ledger could not fund a frozen trade")
            cash -= entry_cost
            accepted_index = len(realized)
            sell_price = float(event["exit_open"]) * (
                1.0 - config.slippage_rate * cost_multiplier
            )
            sell_value = sell_price * quantity
            sell_fee = max(
                config.min_commission * cost_multiplier,
                sell_value * config.commission_rate * cost_multiplier,
            )
            net_return = (sell_value - sell_fee - entry_cost) / entry_cost
            realized.append({"net_return": net_return, "pnl": np.nan})
            positions[str(event["code"])] = {
                "quantity": quantity,
                "entry_cost": entry_cost,
                "exit_date": pd.Timestamp(event["exit_date"]).normalize(),
                "exit_open": float(event["exit_open"]),
                "accepted_index": accepted_index,
                "last_close": float(event["entry_open"]),
            }
        market_value = 0.0
        for code, position in positions.items():
            key = (day, code)
            if key in close_lookup.index and pd.notna(close_lookup.loc[key]):
                position["last_close"] = float(close_lookup.loc[key])
            market_value += position["quantity"] * position["last_close"]
        equity_values.append(cash + market_value)
    if positions:
        raise AssertionError("Independent ledger ended with an open position")

    equity = pd.Series(equity_values, dtype=float)
    total_return = float(equity.iloc[-1] / initial_cash - 1.0) if len(equity) else 0.0
    max_drawdown = (
        float((equity / equity.cummax() - 1.0).min()) if len(equity) else 0.0
    )
    years = max(len(equity) / 252.0, 1.0 / 252.0)
    annualized = float((1.0 + total_return) ** (1.0 / years) - 1.0)
    returns = pd.Series([item["net_return"] for item in realized], dtype=float)
    pnls = pd.Series([item["pnl"] for item in realized], dtype=float)
    return {
        "portfolio_trades": int(len(realized)),
        "portfolio_total_return": total_return,
        "portfolio_annualized_return": annualized,
        "portfolio_max_drawdown": max_drawdown,
        "portfolio_final_equity": float(equity.iloc[-1]) if len(equity) else initial_cash,
        "median_trade_return": float(returns.median()) if len(returns) else None,
        "mean_trade_return": float(returns.mean()) if len(returns) else None,
        "win_rate": float((returns > 0.0).mean()) if len(returns) else None,
        "ex_top3_contribution": float(pnls.sort_values(ascending=False).iloc[3:].sum() / initial_cash),
        "saved_quantities_match": saved_quantities_match,
    }


def _close(left: Any, right: Any, *, tolerance: float = 1e-10) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, np.integer)) and isinstance(right, (int, np.integer)):
        return int(left) == int(right)
    return bool(abs(float(left) - float(right)) <= tolerance)


def _file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
