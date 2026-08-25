from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "v9_idle_cash_yield_sensitivity"
FROZEN_PROTOCOL_SHA256 = "8978e96355b2292f8e4b6100c3e97075407b2438ccf81d8e48041625220c4a3c"
TARGET_WEIGHTED_ANNUALIZED_RETURN = 0.40
MAXIMUM_FEASIBLE_CASH_YIELD = 0.02
SENSITIVITY_RATES = (0.0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03)


@dataclass(frozen=True)
class EvaluationWindow:
    label: str
    backtest_id: str
    start_date: str
    end_date: str
    weight: float


WINDOWS = (
    EvaluationWindow(
        "2021-04_2022-04",
        "0a1df3159c534e33a897addefe5fae79",
        "2021-04-01",
        "2022-04-29",
        0.05,
    ),
    EvaluationWindow(
        "2022-05_2023-05",
        "24da8add0194458495df7bb45ddbfae7",
        "2022-05-01",
        "2023-05-31",
        0.05,
    ),
    EvaluationWindow(
        "2023-06_2024-06",
        "e40fe0fd8a2546729bbfe591b768c27a",
        "2023-06-01",
        "2024-06-28",
        0.05,
    ),
    EvaluationWindow(
        "2024-07_2025-07",
        "ed7e53baab44467d8a6c6ff12212ee0d",
        "2024-07-01",
        "2025-07-24",
        0.25,
    ),
    EvaluationWindow(
        "2025-07_2026-08",
        "acce084944934e619167c972fdefbe8e",
        "2025-07-25",
        "2026-08-07",
        0.60,
    ),
)


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "sensitivity_only",
        "question": (
            "What annual net yield on otherwise idle V9 cash would be required to raise "
            "the frozen five-window weighted annualized return to 40%?"
        ),
        "invariants": {
            "v9_trade_dates_unchanged": True,
            "v9_codes_quantities_prices_fees_unchanged": True,
            "signal_logic_unchanged": True,
            "no_new_equity_positions": True,
            "no_production_registration": True,
        },
        "accrual": {
            "rate_definition": "constant net effective annual cash yield",
            "day_count": 365.25,
            "interval_balance": "previous session post-trade cash plus accrued interest",
            "weekends_and_holidays": "accrue by elapsed calendar days",
            "credit_time": "next observed session valuation",
            "negative_cash": "not permitted in source V9 ledger",
            "first_session": "no pre-window interest",
        },
        "evaluation": {
            "target_weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
            "sensitivity_rates": list(SENSITIVITY_RATES),
            "root_search_bounds": [0.0, 0.10],
            "root_tolerance": 1e-12,
            "maximum_feasible_cash_yield": MAXIMUM_FEASIBLE_CASH_YIELD,
            "maximum_window_drawdown": -0.10,
            "weights": [asdict(window) for window in WINDOWS],
        },
        "decision_rule": {
            "required_yield_at_or_below_two_percent": "REQUIRE_INSTRUMENT_VALIDATION",
            "required_yield_above_two_percent": "REJECT",
            "passing_is_not_promotion": True,
            "required_next_evidence": (
                "point-in-time total-return data, fees, settlement, liquidity, and same-open "
                "funding compatibility for a real cash instrument"
            ),
        },
        "known_limitations": [
            "A constant yield is a feasibility sensitivity, not a tradable return series.",
            "Historical money-market ETF distributions are not present in raw TDX DAY prices.",
            "No result authorizes using reverse repo or a fund without execution validation.",
        ],
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen cash-sweep protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def accrue_idle_cash_yield(
    equity: pd.DataFrame,
    *,
    initial_cash: float,
    annual_cash_yield: float,
) -> dict[str, Any]:
    if annual_cash_yield < 0.0:
        raise ValueError("annual_cash_yield must be non-negative")
    required = {"timestamp", "equity", "cash"}
    missing = required.difference(equity.columns)
    if missing:
        raise ValueError(f"Missing equity columns: {sorted(missing)}")
    frame = equity.loc[:, [column for column in equity.columns if column in required]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.normalize()
    frame["equity"] = pd.to_numeric(frame["equity"], errors="raise")
    frame["cash"] = pd.to_numeric(frame["cash"], errors="raise")
    frame.sort_values("timestamp", inplace=True)
    frame.reset_index(drop=True, inplace=True)
    if frame.empty:
        raise ValueError("Equity ledger is empty")
    if frame["timestamp"].duplicated().any():
        raise ValueError("Equity ledger contains duplicate sessions")
    if frame["cash"].lt(-1e-8).any():
        raise ValueError("Cash yield cannot be applied to a negative-cash ledger")

    accrued = np.zeros(len(frame), dtype=float)
    log_growth = np.log1p(float(annual_cash_yield))
    for index in range(1, len(frame)):
        elapsed_days = int(
            (frame.at[index, "timestamp"] - frame.at[index - 1, "timestamp"]).days
        )
        interval_growth = np.expm1(log_growth * elapsed_days / 365.25)
        prior_total_cash = float(frame.at[index - 1, "cash"]) + accrued[index - 1]
        accrued[index] = accrued[index - 1] + prior_total_cash * interval_growth

    frame["accrued_interest"] = accrued
    frame["adjusted_cash"] = frame["cash"] + frame["accrued_interest"]
    frame["adjusted_equity"] = frame["equity"] + frame["accrued_interest"]
    curve = frame["adjusted_equity"]
    total_return = float(curve.iloc[-1] / initial_cash - 1.0)
    annualized_return = (
        float((1.0 + total_return) ** (252.0 / max(1, len(frame) - 1)) - 1.0)
        if len(frame) > 1
        else 0.0
    )
    max_drawdown = float((curve / curve.cummax() - 1.0).min())
    return {
        "annual_cash_yield": float(annual_cash_yield),
        "sessions": int(len(frame)),
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "final_equity": float(curve.iloc[-1]),
        "accrued_interest": float(accrued[-1]),
        "interest_contribution": float(accrued[-1] / initial_cash),
        "equity": frame,
    }


def evaluate_cash_yield_sensitivity(
    database_path: Path,
    *,
    windows: Sequence[EvaluationWindow] = WINDOWS,
) -> dict[str, Any]:
    ledgers = _load_ledgers(database_path, windows)
    baseline_checks = _evaluate_rate(ledgers, windows, 0.0)
    required_yield = _solve_required_yield(ledgers, windows)
    rates = sorted(set((*SENSITIVITY_RATES, required_yield)))
    scenarios = [_evaluate_rate(ledgers, windows, rate) for rate in rates]
    required_scenario = min(
        scenarios, key=lambda item: abs(float(item["annual_cash_yield"]) - required_yield)
    )
    decision = assess_cash_yield_feasibility(baseline_checks, required_scenario)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "required_annual_cash_yield": required_yield,
        "baseline": baseline_checks,
        "required_yield_scenario": required_scenario,
        "scenarios": scenarios,
        "decision": decision,
    }


def assess_cash_yield_feasibility(
    baseline: Mapping[str, Any],
    required_scenario: Mapping[str, Any],
) -> dict[str, Any]:
    checks = {
        "zero_rate_reproduces_all_windows": bool(baseline["exact_baseline_reproduction"]),
        "target_reached": float(required_scenario["weighted_annualized_return"])
        >= TARGET_WEIGHTED_ANNUALIZED_RETURN - 1e-12,
        "required_yield_within_feasibility_cap": float(
            required_scenario["annual_cash_yield"]
        )
        <= MAXIMUM_FEASIBLE_CASH_YIELD,
        "all_window_drawdowns_within_ten_percent": all(
            float(window["max_drawdown"]) >= -0.10
            for window in required_scenario["windows"]
        ),
        "v9_trades_unchanged": True,
    }
    feasible = all(checks.values())
    return {
        "decision": "REQUIRE_INSTRUMENT_VALIDATION" if feasible else "REJECT",
        "feasibility_qualified": feasible,
        "production_authorized": False,
        "checks": checks,
    }


def run_frozen_sensitivity(database_path: Path, output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    protocol_path = output_dir / "protocol.json"
    actual_hash = _file_sha256(protocol_path)
    if actual_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen cash-sweep protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={actual_hash}"
        )
    result = evaluate_cash_yield_sensitivity(database_path)
    payload = {"protocol_sha256": actual_hash, **result}
    (output_dir / "sensitivity_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _load_ledgers(
    database_path: Path,
    windows: Sequence[EvaluationWindow],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        for window in windows:
            row = connection.execute(
                "SELECT metrics_json FROM backtests WHERE backtest_id=?",
                (window.backtest_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Missing V9 backtest: {window.backtest_id}")
            metrics = json.loads(str(row["metrics_json"] or "{}"))
            equity = pd.read_sql_query(
                "SELECT timestamp,equity,cash FROM backtest_equity "
                "WHERE backtest_id=? AND strategy_id='course49_v9' ORDER BY timestamp",
                connection,
                params=(window.backtest_id,),
            )
            result[window.label] = {"metrics": metrics, "equity": equity}
    return result


def _evaluate_rate(
    ledgers: Mapping[str, Mapping[str, Any]],
    windows: Sequence[EvaluationWindow],
    annual_cash_yield: float,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    exact_baseline = True
    for window in windows:
        source = ledgers[window.label]
        metrics = source["metrics"]
        report = accrue_idle_cash_yield(
            source["equity"],
            initial_cash=float(metrics["initial_cash"]),
            annual_cash_yield=annual_cash_yield,
        )
        report.pop("equity")
        if annual_cash_yield == 0.0:
            exact_baseline = exact_baseline and all(
                abs(float(report[key]) - float(metrics[metric_key])) < 1e-12
                for key, metric_key in (
                    ("total_return", "total_return"),
                    ("annualized_return", "annualized_return"),
                    ("max_drawdown", "max_drawdown"),
                    ("final_equity", "final_equity"),
                )
            )
        reports.append(
            {
                "label": window.label,
                "backtest_id": window.backtest_id,
                "weight": window.weight,
                **report,
            }
        )
    weighted = float(
        sum(report["weight"] * report["annualized_return"] for report in reports)
    )
    return {
        "annual_cash_yield": float(annual_cash_yield),
        "weighted_annualized_return": weighted,
        "exact_baseline_reproduction": exact_baseline if annual_cash_yield == 0.0 else None,
        "windows": reports,
    }


def _solve_required_yield(
    ledgers: Mapping[str, Mapping[str, Any]],
    windows: Sequence[EvaluationWindow],
) -> float:
    lower = 0.0
    upper = 0.10
    lower_value = _evaluate_rate(ledgers, windows, lower)["weighted_annualized_return"]
    if lower_value >= TARGET_WEIGHTED_ANNUALIZED_RETURN:
        return lower
    upper_value = _evaluate_rate(ledgers, windows, upper)["weighted_annualized_return"]
    if upper_value < TARGET_WEIGHTED_ANNUALIZED_RETURN:
        raise ValueError("Target is not reached within the frozen root-search bounds")
    for _ in range(80):
        midpoint = (lower + upper) / 2.0
        value = _evaluate_rate(ledgers, windows, midpoint)["weighted_annualized_return"]
        if value >= TARGET_WEIGHTED_ANNUALIZED_RETURN:
            upper = midpoint
        else:
            lower = midpoint
        if upper - lower <= 1e-12:
            break
    return upper


def _file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
