from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .config import PortfolioConfig
from .intraday_pullback_research import load_v9_trade_pairs


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "course49_v9_three_day_anchor_limit"
RECLAIM_PROTOCOL_VERSION = "1.0.0"
RECLAIM_HYPOTHESIS_ID = "course49_v9_anchor_reclaim_confirmation"


@dataclass(frozen=True)
class V9LimitWindow:
    label: str
    role: str
    start_date: str
    end_date: str
    backtest_id: str
    snapshot_id: str


WINDOWS = (
    V9LimitWindow(
        "dev_2021_2022",
        "DEVELOPMENT",
        "2021-04-01",
        "2022-04-29",
        "0a1df3159c534e33a897addefe5fae79",
        "bt_89d697919ea74826abe4a7702bd0a3e9",
    ),
    V9LimitWindow(
        "dev_2022_2023",
        "DEVELOPMENT",
        "2022-05-01",
        "2023-05-31",
        "24da8add0194458495df7bb45ddbfae7",
        "bt_4bec5474e50b44bdb53aff39bb4075ca",
    ),
    V9LimitWindow(
        "dev_2023_2024",
        "DEVELOPMENT",
        "2023-06-01",
        "2024-06-28",
        "e40fe0fd8a2546729bbfe591b768c27a",
        "bt_e40fe0fd8a2546729bbfe591b768c27a",
    ),
    V9LimitWindow(
        "replication_2024_2025",
        "REPLICATION",
        "2024-07-01",
        "2025-07-24",
        "ed7e53baab44467d8a6c6ff12212ee0d",
        "bt_1f2378fe2c984617911770ccb742a05e",
    ),
    V9LimitWindow(
        "holdout_2025_2026",
        "HOLDOUT",
        "2025-07-25",
        "2026-08-07",
        "acce084944934e619167c972fdefbe8e",
        "bt_6b96520a77fb4ef68726988f55ef57c1",
    ),
)


def load_window_pairs(database_path: Path, window: V9LimitWindow) -> pd.DataFrame:
    pairs = load_v9_trade_pairs(
        database_path,
        backtest_id=window.backtest_id,
        start_date=window.start_date,
    )
    if pairs.empty:
        return pairs
    dates = pd.to_datetime(pairs["entry_date"])
    return pairs.loc[dates.between(window.start_date, window.end_date)].reset_index(
        drop=True
    )


def build_anchor_limit_events(
    trade_pairs: pd.DataFrame,
    raw_bars: pd.DataFrame,
    *,
    maximum_waiting_sessions: int = 3,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Place a fixed limit at the V9 signal close for at most three sessions."""

    if maximum_waiting_sessions <= 0:
        raise ValueError("maximum_waiting_sessions must be positive")
    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if trade_pairs.empty:
        return pd.DataFrame()
    required_pairs = {
        "pair_id",
        "code",
        "entry_date",
        "raw_entry_open",
        "signal_close",
        "quantity",
        "exit_date",
        "raw_exit_open",
    }
    missing_pairs = required_pairs - set(trade_pairs.columns)
    if missing_pairs:
        raise ValueError(f"Missing pair columns: {sorted(missing_pairs)}")
    required_bars = {"code", "timestamp", "Open", "Low"}
    missing_bars = required_bars - set(raw_bars.columns)
    if missing_bars:
        raise ValueError(f"Missing bar columns: {sorted(missing_bars)}")

    pairs = trade_pairs.copy()
    pairs["entry_date"] = pd.to_datetime(pairs["entry_date"]).dt.normalize()
    pairs["exit_date"] = pd.to_datetime(pairs["exit_date"]).dt.normalize()
    bars = raw_bars.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce").dt.normalize()
    bars.dropna(subset=["timestamp"], inplace=True)
    bars.sort_values(["code", "timestamp"], inplace=True)
    config = execution_config or PortfolioConfig()
    rows: list[dict[str, Any]] = []
    grouped = {str(code): frame for code, frame in bars.groupby("code", sort=False)}
    for pair in pairs.to_dict("records"):
        code = str(pair["code"])
        signal_close = float(pair["signal_close"])
        entry_date = pd.Timestamp(pair["entry_date"]).normalize()
        exit_date = pd.Timestamp(pair["exit_date"]).normalize()
        code_bars = grouped.get(code, bars.iloc[0:0])
        eligible = code_bars.loc[
            code_bars["timestamp"].ge(entry_date)
            & code_bars["timestamp"].lt(exit_date)
        ].head(maximum_waiting_sessions)
        fill_date = pd.NaT
        fill_price = np.nan
        fill_type = "EXPIRED"
        observed_sessions = 0
        for bar in eligible.itertuples(index=False):
            observed_sessions += 1
            open_price = float(bar.Open)
            low_price = float(bar.Low)
            if open_price <= signal_close:
                fill_date = pd.Timestamp(bar.timestamp).normalize()
                fill_price = open_price
                fill_type = "OPEN_BELOW_LIMIT"
                break
            if low_price <= signal_close:
                fill_date = pd.Timestamp(bar.timestamp).normalize()
                fill_price = signal_close
                fill_type = "INTRADAY_LIMIT_TOUCH"
                break
        quantity = int(pair["quantity"])
        baseline_return = _round_trip_return(
            float(pair["raw_entry_open"]),
            float(pair["raw_exit_open"]),
            quantity,
            config,
            execution_cost_multiplier,
        )
        filled = pd.notna(fill_date) and np.isfinite(fill_price)
        alternative_return = (
            _round_trip_return(
                float(fill_price),
                float(pair["raw_exit_open"]),
                quantity,
                config,
                execution_cost_multiplier,
            )
            if filled
            else np.nan
        )
        rows.append(
            {
                **pair,
                "hypothesis_id": HYPOTHESIS_ID,
                "limit_price": signal_close,
                "maximum_waiting_sessions": maximum_waiting_sessions,
                "observed_sessions": observed_sessions,
                "filled": bool(filled),
                "alternative_entry_date": fill_date,
                "alternative_raw_entry": fill_price,
                "fill_type": fill_type,
                "baseline_recomputed_return": baseline_return,
                "alternative_net_return": alternative_return,
                "paired_return_delta": (
                    alternative_return - baseline_return if filled else np.nan
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(["entry_date", "code"]).reset_index(drop=True)


def evaluate_limit_window(
    events: pd.DataFrame,
    window: V9LimitWindow,
    *,
    trading_days: int,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    if trading_days <= 0:
        raise ValueError("trading_days must be positive")
    scoped = events.loc[
        pd.to_datetime(events["entry_date"]).between(
            window.start_date, window.end_date
        )
    ].copy()
    filled = scoped.loc[scoped["filled"].fillna(False)].copy()
    baseline = pd.to_numeric(
        scoped.get("baseline_recomputed_return"), errors="coerce"
    ).dropna()
    alternative = pd.to_numeric(
        filled.get("alternative_net_return"), errors="coerce"
    ).dropna()
    deltas = pd.to_numeric(filled.get("paired_return_delta"), errors="coerce").dropna()
    config = execution_config or PortfolioConfig()
    baseline_cashflow = simulate_fixed_quantity_cashflows(
        scoped,
        mode="baseline",
        trading_days=trading_days,
        config=config,
        cost_multiplier=execution_cost_multiplier,
    )
    alternative_cashflow = simulate_fixed_quantity_cashflows(
        scoped,
        mode="alternative",
        trading_days=trading_days,
        config=config,
        cost_multiplier=execution_cost_multiplier,
    )
    ex_top3 = alternative.sort_values(ascending=False).iloc[3:]
    return {
        "window": asdict(window),
        "intended_trades": int(len(scoped)),
        "filled_trades": int(len(filled)),
        "fill_rate": float(len(filled) / len(scoped)) if len(scoped) else 0.0,
        "open_below_limit": int((filled["fill_type"] == "OPEN_BELOW_LIMIT").sum()),
        "intraday_limit_touch": int(
            (filled["fill_type"] == "INTRADAY_LIMIT_TOUCH").sum()
        ),
        "baseline_median_return": float(baseline.median()) if not baseline.empty else None,
        "alternative_median_return": (
            float(alternative.median()) if not alternative.empty else None
        ),
        "median_paired_return_delta": float(deltas.median()) if not deltas.empty else None,
        "alternative_ex_top3_return_sum": float(ex_top3.sum()),
        "baseline_signal_contribution": float(baseline.sum()),
        "alternative_signal_contribution": float(alternative.sum()),
        "opportunity_cost_adjusted_delta": float(alternative.sum() - baseline.sum()),
        "baseline_cashflow": baseline_cashflow,
        "alternative_cashflow": alternative_cashflow,
    }


def assess_development(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for report in reports:
        if str(report["window"]["role"]).upper() != "DEVELOPMENT":
            continue
        window_checks = {
            "minimum_intended_trades": int(report["intended_trades"]) >= 20,
            "minimum_fill_rate": float(report["fill_rate"]) >= 0.60,
            "positive_total_return": (
                float(report["alternative_cashflow"]["total_return"]) > 0.0
            ),
            "positive_median_trade": (
                report["alternative_median_return"] is not None
                and float(report["alternative_median_return"]) > 0.0
            ),
            "positive_ex_top3": float(report["alternative_ex_top3_return_sum"]) > 0.0,
            "not_worse_than_v9": (
                float(report["alternative_cashflow"]["total_return"])
                >= float(report["baseline_cashflow"]["total_return"])
            ),
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


def simulate_fixed_quantity_cashflows(
    events: pd.DataFrame,
    *,
    mode: str,
    trading_days: int,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> dict[str, Any]:
    if mode not in {"baseline", "alternative"}:
        raise ValueError("mode must be baseline or alternative")
    initial_cash = config.initial_cash * config.strategy_budget_weight
    cash = initial_cash
    actions: list[tuple[pd.Timestamp, int, str, float, int, str]] = []
    for row in events.to_dict("records"):
        if mode == "alternative" and not bool(row.get("filled")):
            continue
        entry_date = pd.Timestamp(
            row["entry_date"] if mode == "baseline" else row["alternative_entry_date"]
        ).normalize()
        entry_price = float(
            row["raw_entry_open"]
            if mode == "baseline"
            else row["alternative_raw_entry"]
        )
        exit_date = pd.Timestamp(row["exit_date"]).normalize()
        quantity = int(row["quantity"])
        pair_id = str(row["pair_id"])
        actions.append((entry_date, 1, "BUY", entry_price, quantity, pair_id))
        actions.append(
            (exit_date, 0, "SELL", float(row["raw_exit_open"]), quantity, pair_id)
        )
    positions: dict[str, tuple[int, float]] = {}
    equity_points = [initial_cash]
    insufficient_cash = 0
    for _, _, side, raw_price, quantity, pair_id in sorted(actions):
        if side == "SELL":
            position = positions.pop(pair_id, None)
            if position is None:
                continue
            execution = raw_price * (1.0 - config.slippage_rate * cost_multiplier)
            value = execution * quantity
            fee = max(
                config.min_commission * cost_multiplier,
                value * config.commission_rate * cost_multiplier,
            ) + value * config.stamp_duty_rate * cost_multiplier
            cash += value - fee
        else:
            execution = raw_price * (1.0 + config.slippage_rate * cost_multiplier)
            value = execution * quantity
            fee = max(
                config.min_commission * cost_multiplier,
                value * config.commission_rate * cost_multiplier,
            )
            if value + fee > cash + 1e-8:
                insufficient_cash += 1
                continue
            cash -= value + fee
            positions[pair_id] = (quantity, value)
        equity_points.append(cash + sum(value for _, value in positions.values()))
    if positions:
        raise ValueError("Cashflow simulation ended with open positions")
    total_return = cash / initial_cash - 1.0
    years = max(trading_days / 252.0, 1.0 / 252.0)
    annualized = (1.0 + total_return) ** (1.0 / years) - 1.0
    equity = pd.Series(equity_points, dtype=float)
    drawdown = equity / equity.cummax() - 1.0
    return {
        "initial_cash": float(initial_cash),
        "final_cash": float(cash),
        "total_return": float(total_return),
        "annualized_return": float(annualized),
        "realized_max_drawdown": float(drawdown.min()),
        "insufficient_cash": int(insufficient_cash),
    }


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "selection": "unchanged completed course49_v9 trades and quantities",
        "entry": {
            "limit_anchor": "V9 signal-day raw close from entry evidence",
            "waiting_sessions": 3,
            "open_below_limit": "fill at raw open",
            "intraday_touch": "fill at fixed limit when raw low reaches it",
            "expiry": "cancel after the third session; never carry forward",
            "latest_entry": "strictly before the unchanged V9 exit date",
        },
        "exit": "unchanged V9 exit date and raw open",
        "costs": "standard slippage, commission, minimum commission, and stamp duty",
        "portfolio": "same V9 quantities; missed entries remain cash",
        "development_gate": {
            "minimum_intended_trades_per_window": 20,
            "minimum_fill_rate": 0.60,
            "positive_total_return": True,
            "positive_median_trade": True,
            "positive_ex_top3": True,
            "not_worse_than_v9_total_return": True,
            "all_development_windows_must_pass": True,
        },
        "windows": [asdict(window) for window in WINDOWS],
        "opening_rule": "development, then replication, then holdout",
    }


def build_anchor_reclaim_events(
    trade_pairs: pd.DataFrame,
    raw_bars: pd.DataFrame,
    *,
    maximum_observation_sessions: int = 5,
    holding_sessions: int = 3,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Wait for a V9 anchor touch and a daily reclaim before next-open entry."""

    if maximum_observation_sessions <= 0 or holding_sessions <= 0:
        raise ValueError("observation and holding sessions must be positive")
    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if trade_pairs.empty:
        return pd.DataFrame()
    required_pairs = {"pair_id", "code", "entry_date", "signal_close"}
    missing_pairs = required_pairs - set(trade_pairs.columns)
    if missing_pairs:
        raise ValueError(f"Missing pair columns: {sorted(missing_pairs)}")
    required_bars = {"code", "timestamp", "Open", "High", "Low", "Close"}
    missing_bars = required_bars - set(raw_bars.columns)
    if missing_bars:
        raise ValueError(f"Missing bar columns: {sorted(missing_bars)}")
    pairs = trade_pairs.copy()
    pairs["entry_date"] = pd.to_datetime(pairs["entry_date"]).dt.normalize()
    bars = raw_bars.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce").dt.normalize()
    bars.dropna(subset=["timestamp"], inplace=True)
    bars.sort_values(["code", "timestamp"], inplace=True)
    grouped = {
        str(code): frame.reset_index(drop=True)
        for code, frame in bars.groupby("code", sort=False)
    }
    config = execution_config or PortfolioConfig()
    rows: list[dict[str, Any]] = []
    for pair in pairs.to_dict("records"):
        code = str(pair["code"])
        anchor = float(pair["signal_close"])
        original_entry_date = pd.Timestamp(pair["entry_date"]).normalize()
        code_bars = grouped.get(code, bars.iloc[0:0].reset_index(drop=True))
        start_positions = code_bars.index[
            code_bars["timestamp"].ge(original_entry_date)
        ].tolist()
        confirmation_position: int | None = None
        touched = False
        touch_date = pd.NaT
        previous_close = anchor
        observed_sessions = 0
        if start_positions:
            start = int(start_positions[0])
            stop = min(len(code_bars), start + maximum_observation_sessions)
            for position in range(start, stop):
                bar = code_bars.iloc[position]
                observed_sessions += 1
                if float(bar["Low"]) <= anchor:
                    touched = True
                    if pd.isna(touch_date):
                        touch_date = pd.Timestamp(bar["timestamp"]).normalize()
                if (
                    touched
                    and float(bar["Close"]) >= anchor
                    and float(bar["Close"]) > float(bar["Open"])
                    and float(bar["Close"]) > previous_close
                ):
                    confirmation_position = position
                    break
                previous_close = float(bar["Close"])
        base = {
            **pair,
            "hypothesis_id": RECLAIM_HYPOTHESIS_ID,
            "original_entry_date": original_entry_date,
            "anchor_price": anchor,
            "touch_date": touch_date,
            "observed_sessions": observed_sessions,
            "confirmed": confirmation_position is not None,
            "confirmation_date": pd.NaT,
            "confirmation_close": np.nan,
            "entry_date": pd.NaT,
            "entry_price": np.nan,
            "entry_gap": np.nan,
            "exit_date_3d": pd.NaT,
            "exit_open_3d": np.nan,
            "net_return_3d": np.nan,
            "executable": False,
            "cancellation_reason": "NO_RECLAIM",
        }
        if confirmation_position is None:
            rows.append(base)
            continue
        confirmation = code_bars.iloc[confirmation_position]
        entry_position = confirmation_position + 1
        exit_position = entry_position + holding_sessions
        base["confirmation_date"] = pd.Timestamp(confirmation["timestamp"]).normalize()
        base["confirmation_close"] = float(confirmation["Close"])
        if entry_position >= len(code_bars) or exit_position >= len(code_bars):
            base["cancellation_reason"] = "MISSING_FUTURE_BARS"
            rows.append(base)
            continue
        entry = code_bars.iloc[entry_position]
        exit_bar = code_bars.iloc[exit_position]
        entry_open = float(entry["Open"])
        entry_gap = entry_open / float(confirmation["Close"]) - 1.0
        base.update(
            {
                "entry_date": pd.Timestamp(entry["timestamp"]).normalize(),
                "entry_price": entry_open,
                "entry_gap": entry_gap,
                "exit_date_3d": pd.Timestamp(exit_bar["timestamp"]).normalize(),
                "exit_open_3d": float(exit_bar["Open"]),
            }
        )
        if not -0.03 <= entry_gap <= 0.08:
            base["cancellation_reason"] = "OPEN_GAP"
            rows.append(base)
            continue
        buy_price = entry_open * (1.0 + config.slippage_rate * execution_cost_multiplier)
        target_cash = config.initial_cash * 0.10
        quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
        if quantity <= 0:
            base["cancellation_reason"] = "BOARD_LOT"
            rows.append(base)
            continue
        base["net_return_3d"] = _round_trip_return(
            entry_open,
            float(exit_bar["Open"]),
            quantity,
            config,
            execution_cost_multiplier,
        )
        base["executable"] = True
        base["cancellation_reason"] = ""
        rows.append(base)
    return pd.DataFrame(rows).sort_values(
        ["original_entry_date", "code"]
    ).reset_index(drop=True)


def select_reclaim_entries(
    events: pd.DataFrame,
    *,
    maximum_positions: int = 3,
) -> pd.DataFrame:
    if maximum_positions <= 0:
        raise ValueError("maximum_positions must be positive")
    if events.empty:
        return events.copy()
    ordered = events.loc[events["executable"].fillna(False)].sort_values(
        ["entry_date", "code"]
    )
    active: list[tuple[str, pd.Timestamp]] = []
    accepted: list[pd.Series] = []
    for _, event in ordered.iterrows():
        entry_date = pd.Timestamp(event["entry_date"]).normalize()
        active = [position for position in active if position[1] > entry_date]
        code = str(event["code"])
        if code in {position[0] for position in active} or len(active) >= maximum_positions:
            continue
        active.append((code, pd.Timestamp(event["exit_date_3d"]).normalize()))
        accepted.append(event)
    return (
        pd.DataFrame(accepted).reset_index(drop=True)
        if accepted
        else events.iloc[0:0].copy()
    )


def evaluate_reclaim_window(
    events: pd.DataFrame,
    window: V9LimitWindow,
    *,
    trading_days: int,
    execution_config: PortfolioConfig | None = None,
) -> dict[str, Any]:
    scoped = events.loc[
        pd.to_datetime(events["original_entry_date"]).between(
            window.start_date, window.end_date
        )
    ].copy()
    accepted = select_reclaim_entries(scoped, maximum_positions=3)
    returns = pd.to_numeric(accepted.get("net_return_3d"), errors="coerce").dropna()
    config = execution_config or PortfolioConfig()
    portfolio = _simulate_reclaim_cashflows(
        accepted,
        trading_days=trading_days,
        config=config,
    )
    ex_top3 = returns.sort_values(ascending=False).iloc[3:]
    return {
        "window": asdict(window),
        "source_v9_trades": int(len(scoped)),
        "anchor_touches": int(scoped["touch_date"].notna().sum()),
        "confirmations": int(scoped["confirmed"].fillna(False).sum()),
        "executable_signals": int(scoped["executable"].fillna(False).sum()),
        "portfolio_trades": int(len(accepted)),
        "median_net_return_3d": float(returns.median()) if not returns.empty else None,
        "mean_net_return_3d": float(returns.mean()) if not returns.empty else None,
        "win_rate_3d": float((returns > 0).mean()) if not returns.empty else None,
        "ex_top3_return_sum": float(ex_top3.sum()),
        **portfolio,
    }


def assess_reclaim_development(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for report in reports:
        if str(report["window"]["role"]).upper() != "DEVELOPMENT":
            continue
        window_checks = {
            "minimum_trades": int(report["portfolio_trades"]) >= 15,
            "positive_total_return": float(report["portfolio_total_return"]) > 0.0,
            "positive_median_trade": (
                report["median_net_return_3d"] is not None
                and float(report["median_net_return_3d"]) > 0.0
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
        "protocol_version": RECLAIM_PROTOCOL_VERSION,
        "decision": "OPEN_REPLICATION" if passed else "REJECT",
        "checks": checks,
        "replication_opened": passed,
        "holdout_opened": False,
    }


def reclaim_protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": RECLAIM_PROTOCOL_VERSION,
        "hypothesis_id": RECLAIM_HYPOTHESIS_ID,
        "selection": "completed course49_v9 trades; V9 selection remains unchanged",
        "confirmation": {
            "observation_sessions": 5,
            "anchor": "V9 signal-day raw close",
            "touch": "raw daily low at or below anchor",
            "reclaim": [
                "raw close at or above anchor",
                "raw close above raw open",
                "raw close above previous observed close",
            ],
            "entry": "next trading-day raw open",
            "entry_gap": [-0.03, 0.08],
        },
        "portfolio": {
            "target_weight": 0.10,
            "maximum_positions": 3,
            "holding_sessions": 3,
            "costs": "standard slippage, commission, minimum commission, and stamp duty",
        },
        "development_gate": {
            "minimum_portfolio_trades_per_window": 15,
            "positive_total_return": True,
            "positive_median_trade": True,
            "positive_ex_top3": True,
            "maximum_drawdown": -0.10,
            "all_development_windows_must_pass": True,
        },
        "windows": [asdict(window) for window in WINDOWS],
        "opening_rule": "development, then replication, then holdout",
    }


def save_reclaim_protocol(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "reclaim_protocol.json"
    path.write_text(
        json.dumps(reclaim_protocol_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _simulate_reclaim_cashflows(
    events: pd.DataFrame,
    *,
    trading_days: int,
    config: PortfolioConfig,
) -> dict[str, Any]:
    initial_cash = config.initial_cash
    cash = initial_cash
    actions: list[tuple[pd.Timestamp, int, str, float, int, str]] = []
    for row in events.to_dict("records"):
        buy_price = float(row["entry_price"]) * (1.0 + config.slippage_rate)
        target_cash = initial_cash * 0.10
        quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
        if quantity <= 0:
            continue
        actions.append(
            (
                pd.Timestamp(row["entry_date"]).normalize(),
                1,
                "BUY",
                float(row["entry_price"]),
                quantity,
                str(row["pair_id"]),
            )
        )
        actions.append(
            (
                pd.Timestamp(row["exit_date_3d"]).normalize(),
                0,
                "SELL",
                float(row["exit_open_3d"]),
                quantity,
                str(row["pair_id"]),
            )
        )
    positions: dict[str, tuple[int, float]] = {}
    equity_points = [initial_cash]
    for _, _, side, raw_price, quantity, pair_id in sorted(actions):
        if side == "SELL":
            position = positions.pop(pair_id, None)
            if position is None:
                continue
            execution = raw_price * (1.0 - config.slippage_rate)
            value = execution * quantity
            fee = max(config.min_commission, value * config.commission_rate)
            fee += value * config.stamp_duty_rate
            cash += value - fee
        else:
            execution = raw_price * (1.0 + config.slippage_rate)
            value = execution * quantity
            fee = max(config.min_commission, value * config.commission_rate)
            if value + fee > cash:
                continue
            cash -= value + fee
            positions[pair_id] = (quantity, value)
        equity_points.append(cash + sum(value for _, value in positions.values()))
    if positions:
        raise ValueError("Reclaim simulation ended with open positions")
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


def save_protocol(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "protocol.json"
    path.write_text(
        json.dumps(protocol_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _round_trip_return(
    entry_open: float,
    exit_open: float,
    quantity: int,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> float:
    if (
        not np.isfinite(entry_open)
        or not np.isfinite(exit_open)
        or entry_open <= 0
        or exit_open <= 0
        or quantity <= 0
    ):
        return np.nan
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
