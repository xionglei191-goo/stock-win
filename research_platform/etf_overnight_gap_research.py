from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .config import PlatformConfig, PortfolioConfig
from .equity_etf_reversal_research import (
    WINDOWS,
    ResearchWindow,
    load_development_market_index,
    load_development_snapshot,
)
from .storage import Database


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "strong_domestic_equity_etf_gap_down_overnight_reversal"
FROZEN_PROTOCOL_SHA256 = "20c09dbe69b72a89d482a30ec872408d2cb1a448e3dc92dbec1006fd3ab994d2"
DEVELOPMENT_SNAPSHOT_ID = (
    "f24e155bf03374b1e3f1190eb48427c8934f7aed3d80045d1667ec37835cb51a"
)
DEVELOPMENT_SNAPSHOT_SHA256 = (
    "799f343419960140a499b2103331e6909e42a7ef8628cc1eff31543ab17cecbf"
)
TARGET_WEIGHT = 0.10
MAXIMUM_POSITIONS = 3
ENTRY_GAP_MIN = -0.03
ENTRY_GAP_MAX = -0.01


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "development_only",
        "derivation": (
            "A distinct short-horizon hypothesis after close-confirmed stock pullbacks, "
            "daily ETF reversal exits, and long ETF trend exits failed. It tests whether "
            "an opening discount in an otherwise strong domestic equity ETF mean-reverts "
            "over the minimum T+1 holding interval."
        ),
        "population": {
            "definition": "current TDX-listed domestic equity ETFs with local DAY history",
            "classification": "unchanged from domestic_equity_etf_cross_sectional_reversal",
            "known_bias": "current-survivor roster omits ETFs liquidated before 2026-08-10",
            "promotion_block": "historical listed-and-liquidated ETF roster audit is required",
        },
        "data": {
            "source": "immutable local TDX unadjusted DAY snapshot and market snapshots",
            "etf_snapshot_id": DEVELOPMENT_SNAPSHOT_ID,
            "etf_snapshot_bars_sha256": DEVELOPMENT_SNAPSHOT_SHA256,
            "development_end": "2024-06-28",
            "replication_window": ["2024-07-01", "2025-07-24"],
            "final_holdout_window": ["2025-07-25", "2026-08-07"],
            "replication_and_holdout_excluded_from_development_input": True,
        },
        "signal": {
            "decision_time": "entry-session open",
            "prior_close_only_features": True,
            "minimum_history_sessions": 120,
            "minimum_amount_20d": 50_000_000,
            "prior_close_above_ma20": True,
            "prior_close_above_ma120": True,
            "positive_return_60d": True,
            "top_return_60d_cross_section": 0.30,
            "benchmark_prior_close_above_ma120": True,
            "entry_open_gap_bounds": [ENTRY_GAP_MIN, ENTRY_GAP_MAX],
            "ranking": "opening gap ascending, 60-day momentum rank, liquidity, code",
            "correlation_lookback": 60,
            "minimum_correlation_observations": 40,
            "maximum_pair_correlation": 0.95,
            "maximum_daily_candidates": MAXIMUM_POSITIONS,
        },
        "execution": {
            "entry": "same-session raw open after the opening gap is known",
            "exit": "next trading-session raw open",
            "t_plus_one": True,
            "target_weight": TARGET_WEIGHT,
            "maximum_positions": MAXIMUM_POSITIONS,
            "daily_cash_order": "scheduled exits before new entries",
            "board_lot": 100,
            "base_costs": {
                "commission_rate": 0.0003,
                "minimum_commission": 5.0,
                "slippage_each_side": 0.001,
                "stamp_duty": 0.0,
            },
            "stress_costs": "double commission, minimum commission, and slippage",
        },
        "development_gate": {
            "minimum_portfolio_trades_per_window": 30,
            "minimum_base_annualized_return_per_window": 0.05,
            "positive_base_total_return": True,
            "positive_base_median_trade": True,
            "positive_base_ex_top3_contribution": True,
            "positive_stress_total_return": True,
            "positive_stress_median_trade": True,
            "positive_stress_ex_top3_contribution": True,
            "maximum_base_and_stress_drawdown": -0.10,
            "minimum_fill_rate": 0.60,
            "all_three_development_windows_must_pass": True,
            "passing_action": "audit survivor roster before opening replication",
        },
        "windows": [asdict(window) for window in WINDOWS],
        "opening_rule": "development, survivor audit, replication, then final holdout",
        "invariants": {
            "no_parameter_scan": True,
            "no_default_scan_registration": True,
            "no_tdx_push": True,
            "no_paper_or_real_orders": True,
            "no_production_promotion": True,
        },
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen overnight-gap protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def build_overnight_gap_events(
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    if execution_cost_multiplier <= 0.0:
        raise ValueError("execution_cost_multiplier must be positive")
    if bars.empty or market_index.empty:
        return _empty_events()
    config = _etf_config(execution_config or PortfolioConfig())
    frame = _prepare_features(bars, market_index)
    base_eligible = (
        frame["ma120"].notna()
        & frame["return_60d"].notna()
        & frame["amount_20d"].ge(50_000_000.0)
    )
    frame["momentum_percentile"] = np.nan
    frame.loc[base_eligible, "momentum_percentile"] = frame.loc[base_eligible].groupby(
        "timestamp", sort=False
    )["return_60d"].rank(method="average", pct=True, ascending=False)
    strong = (
        base_eligible
        & frame["Close"].gt(frame["ma20"])
        & frame["Close"].gt(frame["ma120"])
        & frame["return_60d"].gt(0.0)
        & frame["momentum_percentile"].le(0.30)
        & frame["market_allowed"].fillna(False)
    ).fillna(False)
    candidates = frame.loc[strong].copy()
    if candidates.empty:
        return _empty_events()

    calendar = _calendar(market_index)
    next_session = {
        calendar[index]: calendar[index + 1]
        for index in range(max(0, len(calendar) - 1))
    }
    candidates["signal_date"] = pd.to_datetime(candidates["timestamp"]).dt.normalize()
    candidates["entry_date"] = candidates["signal_date"].map(next_session)
    candidates["exit_date"] = candidates["entry_date"].map(next_session)

    opens = bars.loc[:, ["code", "timestamp", "Open"]].copy()
    opens["timestamp"] = pd.to_datetime(opens["timestamp"], errors="coerce").dt.normalize()
    entry_opens = opens.rename(columns={"timestamp": "entry_date", "Open": "entry_open"})
    exit_opens = opens.rename(columns={"timestamp": "exit_date", "Open": "exit_open"})
    candidates = candidates.merge(
        entry_opens, on=["code", "entry_date"], how="left", validate="many_to_one"
    ).merge(exit_opens, on=["code", "exit_date"], how="left", validate="many_to_one")
    candidates["entry_gap"] = candidates["entry_open"] / candidates["Close"] - 1.0
    candidates["blocked_missing_entry"] = candidates["entry_open"].isna()
    candidates["blocked_entry_gap"] = ~candidates["entry_gap"].between(
        ENTRY_GAP_MIN, ENTRY_GAP_MAX
    )
    candidates.loc[candidates["blocked_missing_entry"], "blocked_entry_gap"] = False
    candidates["gap_qualified"] = ~(
        candidates["blocked_missing_entry"] | candidates["blocked_entry_gap"]
    )
    candidates["blocked_missing_exit"] = candidates["exit_open"].isna()
    candidates["blocked_correlation"] = False
    candidates["blocked_daily_capacity"] = False
    candidates["selected"] = False
    candidates["daily_rank"] = pd.Series(pd.NA, index=candidates.index, dtype="Int64")

    return_pivot = frame.pivot(index="timestamp", columns="code", values="daily_return")
    qualified = candidates.loc[candidates["gap_qualified"]].sort_values(
        ["entry_date", "entry_gap", "momentum_percentile", "amount_20d", "code"],
        ascending=[True, True, True, False, True],
    )
    for _, day in qualified.groupby("entry_date", sort=True):
        chosen: list[str] = []
        for event_index, row in day.iterrows():
            code = str(row["code"])
            if len(chosen) >= MAXIMUM_POSITIONS:
                candidates.at[event_index, "blocked_daily_capacity"] = True
                continue
            if any(
                _trailing_correlation(
                    return_pivot,
                    pd.Timestamp(row["signal_date"]),
                    code,
                    other_code,
                )
                >= 0.95
                for other_code in chosen
            ):
                candidates.at[event_index, "blocked_correlation"] = True
                continue
            chosen.append(code)
            candidates.at[event_index, "selected"] = True
            candidates.at[event_index, "daily_rank"] = len(chosen)

    candidates["quantity"] = 0
    candidates["net_return"] = np.nan
    candidates["executable"] = False
    executable = candidates["selected"] & ~candidates["blocked_missing_exit"]
    for event_index, row in candidates.loc[executable].iterrows():
        quantity, net_return = _trade_quantity_and_return(
            float(row["entry_open"]),
            float(row["exit_open"]),
            config,
            execution_cost_multiplier,
        )
        candidates.at[event_index, "quantity"] = quantity
        candidates.at[event_index, "net_return"] = net_return
        candidates.at[event_index, "executable"] = quantity > 0 and np.isfinite(net_return)

    keep = [
        "code",
        "name",
        "signal_date",
        "entry_date",
        "exit_date",
        "Close",
        "ma20",
        "ma120",
        "return_60d",
        "momentum_percentile",
        "amount_20d",
        "market_close",
        "market_ma120",
        "entry_open",
        "entry_gap",
        "exit_open",
        "gap_qualified",
        "selected",
        "daily_rank",
        "blocked_missing_entry",
        "blocked_entry_gap",
        "blocked_correlation",
        "blocked_daily_capacity",
        "blocked_missing_exit",
        "executable",
        "quantity",
        "net_return",
    ]
    return candidates.loc[:, keep].sort_values(
        ["entry_date", "entry_gap", "code"], ascending=[True, True, True]
    ).reset_index(drop=True)


def evaluate_development_window(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    window: ResearchWindow,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    start = pd.Timestamp(window.start_date).normalize()
    end = pd.Timestamp(window.end_date).normalize()
    scoped = events.loc[pd.to_datetime(events["entry_date"]).between(start, end)].copy()
    selected = scoped.loc[scoped["selected"]]
    complete = selected.loc[
        selected["executable"] & pd.to_datetime(selected["exit_date"]).le(end)
    ].copy()
    calendar = [day for day in _calendar(market_index) if start <= day <= end]
    portfolio = simulate_overnight_portfolio(
        complete,
        bars,
        calendar,
        config=execution_config or PortfolioConfig(),
        cost_multiplier=execution_cost_multiplier,
    )
    accepted_returns = pd.Series(portfolio.pop("accepted_trade_returns"), dtype=float)
    accepted_pnls = pd.Series(portfolio.pop("accepted_trade_pnls"), dtype=float)
    ex_top3_pnls = accepted_pnls.sort_values(ascending=False).iloc[3:]
    return {
        "window": asdict(window),
        "trend_eligible_opens": int(len(scoped)),
        "gap_qualified_signals": int(scoped["gap_qualified"].sum()),
        "selected_signals": int(len(selected)),
        "executable_signals": int(len(complete)),
        "blocked_missing_entry": int(scoped["blocked_missing_entry"].sum()),
        "blocked_entry_gap": int(scoped["blocked_entry_gap"].sum()),
        "blocked_correlation": int(scoped["blocked_correlation"].sum()),
        "blocked_daily_capacity": int(scoped["blocked_daily_capacity"].sum()),
        "blocked_missing_exit": int(selected["blocked_missing_exit"].sum()),
        "fill_rate": float(len(complete) / len(selected)) if len(selected) else 0.0,
        "median_trade_return": (
            float(accepted_returns.median()) if not accepted_returns.empty else None
        ),
        "mean_trade_return": (
            float(accepted_returns.mean()) if not accepted_returns.empty else None
        ),
        "win_rate": (
            float((accepted_returns > 0.0).mean()) if not accepted_returns.empty else None
        ),
        "ex_top3_contribution": float(ex_top3_pnls.sum() / float(_etf_config(execution_config or PortfolioConfig()).initial_cash)),
        **portfolio,
    }


def simulate_overnight_portfolio(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: Sequence[Any],
    *,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> dict[str, Any]:
    config = _etf_config(config)
    initial_cash = float(config.initial_cash)
    cash = initial_cash
    positions: dict[str, dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    closes = bars.loc[:, ["code", "timestamp", "Close"]].copy()
    closes["timestamp"] = pd.to_datetime(closes["timestamp"], errors="coerce").dt.normalize()
    close_pivot = closes.pivot(index="timestamp", columns="code", values="Close")
    entries = {
        pd.Timestamp(day).normalize(): group.sort_values(
            ["daily_rank", "entry_gap", "code"], ascending=[True, True, True]
        )
        for day, group in events.groupby("entry_date", sort=True)
    }
    for value in sorted({pd.Timestamp(day).normalize() for day in calendar}):
        for code in sorted(
            [code for code, position in positions.items() if position["exit_date"] <= value]
        ):
            position = positions.pop(code)
            sell_price = float(position["exit_open"]) * (
                1.0 - config.slippage_rate * cost_multiplier
            )
            sell_value = sell_price * int(position["quantity"])
            sell_fee = max(
                config.min_commission * cost_multiplier,
                sell_value * config.commission_rate * cost_multiplier,
            )
            proceeds = sell_value - sell_fee
            cash += proceeds
            accepted[position["accepted_index"]]["pnl"] = proceeds - float(position["entry_cost"])
        for _, event in entries.get(value, pd.DataFrame()).iterrows():
            code = str(event["code"])
            if code in positions or len(positions) >= MAXIMUM_POSITIONS:
                continue
            buy_price = float(event["entry_open"]) * (
                1.0 + config.slippage_rate * cost_multiplier
            )
            target_cash = min(initial_cash * TARGET_WEIGHT, cash)
            quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
            if quantity <= 0:
                continue
            buy_value = buy_price * quantity
            buy_fee = max(
                config.min_commission * cost_multiplier,
                buy_value * config.commission_rate * cost_multiplier,
            )
            entry_cost = buy_value + buy_fee
            if entry_cost > cash:
                continue
            cash -= entry_cost
            accepted_index = len(accepted)
            accepted.append(
                {
                    "net_return": float(event["net_return"]),
                    "pnl": np.nan,
                }
            )
            positions[code] = {
                "quantity": quantity,
                "entry_cost": entry_cost,
                "exit_date": pd.Timestamp(event["exit_date"]).normalize(),
                "exit_open": float(event["exit_open"]),
                "accepted_index": accepted_index,
                "last_close": float(event["entry_open"]),
            }
        market_value = 0.0
        for code, position in positions.items():
            if (
                value in close_pivot.index
                and code in close_pivot.columns
                and pd.notna(close_pivot.at[value, code])
            ):
                position["last_close"] = float(close_pivot.at[value, code])
            market_value += int(position["quantity"]) * float(position["last_close"])
        equity_rows.append({"timestamp": value, "equity": cash + market_value})
    if positions:
        raise ValueError("Evaluation calendar ended with an unclosed overnight position")
    equity = pd.DataFrame(equity_rows)
    if equity.empty:
        total_return = 0.0
        max_drawdown = 0.0
    else:
        total_return = float(equity["equity"].iloc[-1] / initial_cash - 1.0)
        max_drawdown = float((equity["equity"] / equity["equity"].cummax() - 1.0).min())
    years = max(len(equity) / 252.0, 1.0 / 252.0)
    annualized = float((1.0 + total_return) ** (1.0 / years) - 1.0)
    return {
        "portfolio_trades": int(len(accepted)),
        "portfolio_total_return": total_return,
        "portfolio_annualized_return": annualized,
        "portfolio_max_drawdown": max_drawdown,
        "portfolio_final_equity": float(initial_cash * (1.0 + total_return)),
        "accepted_trade_returns": [float(item["net_return"]) for item in accepted],
        "accepted_trade_pnls": [float(item["pnl"]) for item in accepted],
    }


def assess_development(
    base_reports: Iterable[Mapping[str, Any]],
    stress_reports: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    base = list(base_reports)
    stress = list(stress_reports)
    if len(base) != 3 or len(stress) != 3:
        raise ValueError("All three base and stress development reports are required")
    stress_by_window = {str(item["window"]["label"]): item for item in stress}
    checks: list[dict[str, Any]] = []
    for report in base:
        label = str(report["window"]["label"])
        stressed = stress_by_window[label]
        window_checks = {
            "minimum_trades": int(report["portfolio_trades"]) >= 30,
            "minimum_base_annualized_return": float(
                report["portfolio_annualized_return"]
            )
            >= 0.05,
            "positive_base_total_return": float(report["portfolio_total_return"]) > 0.0,
            "positive_base_median_trade": (
                report["median_trade_return"] is not None
                and float(report["median_trade_return"]) > 0.0
            ),
            "positive_base_ex_top3_contribution": float(
                report["ex_top3_contribution"]
            )
            > 0.0,
            "positive_stress_total_return": float(
                stressed["portfolio_total_return"]
            )
            > 0.0,
            "positive_stress_median_trade": (
                stressed["median_trade_return"] is not None
                and float(stressed["median_trade_return"]) > 0.0
            ),
            "positive_stress_ex_top3_contribution": float(
                stressed["ex_top3_contribution"]
            )
            > 0.0,
            "maximum_base_drawdown": float(report["portfolio_max_drawdown"]) >= -0.10,
            "maximum_stress_drawdown": float(stressed["portfolio_max_drawdown"]) >= -0.10,
            "minimum_fill_rate": float(report["fill_rate"]) >= 0.60,
        }
        checks.append(
            {"window": label, "checks": window_checks, "passed": all(window_checks.values())}
        )
    passed = all(item["passed"] for item in checks)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "decision": "REQUIRE_SURVIVOR_AUDIT" if passed else "REJECT",
        "development_qualified": passed,
        "checks": checks,
        "survivor_audit_required": passed,
        "replication_opened": False,
        "holdout_opened": False,
        "production_authorized": False,
    }


def run_frozen_development(
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
            "Frozen overnight-gap protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={protocol_hash}"
        )
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    if str(manifest.get("snapshot_id")) != DEVELOPMENT_SNAPSHOT_ID:
        raise ValueError("Unexpected ETF development snapshot id")
    if str(manifest.get("bars_sha256")) != DEVELOPMENT_SNAPSHOT_SHA256:
        raise ValueError("Unexpected ETF development bars hash")
    bars = load_development_snapshot(snapshot_dir)
    market_index, market_sources = load_development_market_index(config, database)
    base_events = build_overnight_gap_events(
        bars,
        market_index,
        execution_config=config.portfolio,
        execution_cost_multiplier=1.0,
    )
    stress_events = build_overnight_gap_events(
        bars,
        market_index,
        execution_config=config.portfolio,
        execution_cost_multiplier=2.0,
    )
    selection_columns = ["code", "signal_date", "entry_date", "selected", "daily_rank"]
    pd.testing.assert_frame_equal(
        base_events.loc[:, selection_columns],
        stress_events.loc[:, selection_columns],
        check_dtype=False,
    )
    development_windows = [window for window in WINDOWS if window.role == "DEVELOPMENT"]
    base_reports = [
        evaluate_development_window(
            base_events,
            bars,
            market_index,
            window,
            execution_config=config.portfolio,
            execution_cost_multiplier=1.0,
        )
        for window in development_windows
    ]
    stress_reports = [
        evaluate_development_window(
            stress_events,
            bars,
            market_index,
            window,
            execution_config=config.portfolio,
            execution_cost_multiplier=2.0,
        )
        for window in development_windows
    ]
    decision = assess_development(base_reports, stress_reports)
    data_quality = _data_quality_summary(bars, market_index, manifest)
    paths = write_development_artifacts(
        output_dir,
        base_events,
        stress_events,
        base_reports,
        stress_reports,
        decision,
        data_quality,
        protocol_sha256=protocol_hash,
    )
    return {
        "protocol_sha256": protocol_hash,
        "snapshot_id": DEVELOPMENT_SNAPSHOT_ID,
        "market_sources": market_sources,
        "base_reports": base_reports,
        "stress_reports": stress_reports,
        "decision": decision,
        "data_quality": data_quality,
        "artifacts": paths,
    }


def write_development_artifacts(
    output_dir: Path,
    base_events: pd.DataFrame,
    stress_events: pd.DataFrame,
    base_reports: Sequence[Mapping[str, Any]],
    stress_reports: Sequence[Mapping[str, Any]],
    decision: Mapping[str, Any],
    data_quality: Mapping[str, Any],
    *,
    protocol_sha256: str,
) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_path = output_dir / "development_events.parquet"
    stress_path = output_dir / "development_events_stress.parquet"
    base_events.to_parquet(base_path, index=False)
    stress_events.to_parquet(stress_path, index=False)
    result = {
        "protocol_sha256": protocol_sha256,
        "snapshot_id": DEVELOPMENT_SNAPSHOT_ID,
        "base_reports": [dict(item) for item in base_reports],
        "stress_reports": [dict(item) for item in stress_reports],
        "decision": dict(decision),
        "data_quality": dict(data_quality),
        "base_events_sha256": _file_sha256(base_path),
        "stress_events_sha256": _file_sha256(stress_path),
    }
    result_path = output_dir / "development_result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return {
        "result": str(result_path.resolve()),
        "base_events": str(base_path.resolve()),
        "stress_events": str(stress_path.resolve()),
    }


def _prepare_features(bars: pd.DataFrame, market_index: pd.DataFrame) -> pd.DataFrame:
    required = {"code", "name", "timestamp", "Open", "High", "Low", "Close", "Amount", "Volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars is missing columns: {sorted(missing)}")
    frame = bars.loc[:, sorted(required)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.normalize()
    frame.dropna(subset=["timestamp"], inplace=True)
    frame.sort_values(["code", "timestamp"], inplace=True)
    if frame.duplicated(["code", "timestamp"]).any():
        raise ValueError("bars contains duplicate code-session keys")
    grouped = frame.groupby("code", sort=False)
    frame["daily_return"] = grouped["Close"].pct_change(fill_method=None)
    frame["return_60d"] = frame["Close"] / grouped["Close"].shift(60) - 1.0
    frame["ma20"] = grouped["Close"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    frame["ma120"] = grouped["Close"].transform(
        lambda values: values.rolling(120, min_periods=120).mean()
    )
    frame["amount_20d"] = grouped["Amount"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )

    market = market_index.loc[:, ["timestamp", "Close"]].copy()
    market["timestamp"] = pd.to_datetime(market["timestamp"], errors="coerce").dt.normalize()
    market.dropna(subset=["timestamp"], inplace=True)
    market.sort_values("timestamp", inplace=True)
    if market["timestamp"].duplicated().any():
        raise ValueError("market_index must contain one row per session")
    market["market_close"] = pd.to_numeric(market["Close"], errors="coerce")
    market["market_ma120"] = market["market_close"].rolling(120, min_periods=120).mean()
    market["market_allowed"] = market["market_close"].gt(market["market_ma120"])
    return frame.merge(
        market.loc[:, ["timestamp", "market_close", "market_ma120", "market_allowed"]],
        on="timestamp",
        how="left",
        validate="many_to_one",
    )


def _trailing_correlation(
    returns: pd.DataFrame,
    signal_date: pd.Timestamp,
    code: str,
    other_code: str,
    *,
    lookback: int = 60,
    minimum_observations: int = 40,
) -> float:
    if code not in returns.columns or other_code not in returns.columns:
        return -np.inf
    pair = returns.loc[returns.index <= signal_date, [code, other_code]].tail(lookback).dropna()
    if len(pair) < minimum_observations:
        return -np.inf
    value = pair[code].corr(pair[other_code])
    return float(value) if pd.notna(value) else -np.inf


def _trade_quantity_and_return(
    entry_open: float,
    exit_open: float,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> tuple[int, float]:
    if not np.isfinite(entry_open) or not np.isfinite(exit_open) or entry_open <= 0.0:
        return 0, np.nan
    buy_price = entry_open * (1.0 + config.slippage_rate * cost_multiplier)
    target_cash = config.initial_cash * TARGET_WEIGHT
    quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
    if quantity <= 0:
        return 0, np.nan
    buy_value = buy_price * quantity
    buy_fee = max(
        config.min_commission * cost_multiplier,
        buy_value * config.commission_rate * cost_multiplier,
    )
    sell_price = exit_open * (1.0 - config.slippage_rate * cost_multiplier)
    sell_value = sell_price * quantity
    sell_fee = max(
        config.min_commission * cost_multiplier,
        sell_value * config.commission_rate * cost_multiplier,
    )
    net_return = (sell_value - sell_fee - buy_value - buy_fee) / (buy_value + buy_fee)
    return quantity, float(net_return)


def _etf_config(config: PortfolioConfig) -> PortfolioConfig:
    return replace(config, stamp_duty_rate=0.0)


def _calendar(market_index: pd.DataFrame) -> list[pd.Timestamp]:
    values = pd.to_datetime(market_index["timestamp"], errors="coerce").dropna()
    return sorted({pd.Timestamp(value).normalize() for value in values})


def _data_quality_summary(
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    timestamps = pd.to_datetime(bars["timestamp"], errors="coerce")
    market_dates = pd.to_datetime(market_index["timestamp"], errors="coerce")
    invalid_ohlc = (
        bars[["Open", "High", "Low", "Close"]].isna().any(axis=1)
        | bars["High"].lt(bars[["Open", "Close"]].max(axis=1))
        | bars["Low"].gt(bars[["Open", "Close"]].min(axis=1))
    )
    return {
        "bars_rows": int(len(bars)),
        "bars_codes": int(bars["code"].nunique()),
        "bars_start": str(timestamps.min().date()),
        "bars_end": str(timestamps.max().date()),
        "duplicate_code_sessions": int(bars.duplicated(["code", "timestamp"]).sum()),
        "invalid_ohlc_rows": int(invalid_ohlc.sum()),
        "nonpositive_prices": int(bars[["Open", "High", "Low", "Close"]].le(0).any(axis=1).sum()),
        "negative_amount_or_volume": int(bars[["Amount", "Volume"]].lt(0).any(axis=1).sum()),
        "market_start": str(market_dates.min().date()),
        "market_end": str(market_dates.max().date()),
        "snapshot_holdout_rows": int(manifest.get("holdout_rows_included", 0)),
        "known_survivorship_bias": True,
        "ready_for_development": bool(
            not bars.duplicated(["code", "timestamp"]).any()
            and not invalid_ohlc.any()
            and timestamps.max() <= pd.Timestamp("2024-06-28")
        ),
        "ready_for_promotion": False,
    }


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "code",
            "name",
            "signal_date",
            "entry_date",
            "exit_date",
            "entry_gap",
            "gap_qualified",
            "selected",
            "executable",
            "net_return",
        ]
    )


def _file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
