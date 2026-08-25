from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from .cash_sweep_research import EvaluationWindow, WINDOWS
from .reverse_repo_sweep_research import (
    INSTRUMENT_CODE,
    PRINCIPAL_LOT,
    REQUIRED_NET_ANNUAL_YIELD,
    SCENARIOS,
    SNAPSHOT_END,
    RepoScenario,
    _baseline_report,
    _file_sha256,
    _load_ledgers,
    _normalize_timestamp,
    assess_reverse_repo_sweep,
    load_repo_snapshot,
)


PROTOCOL_VERSION = "2.0.0"
HYPOTHESIS_ID = "v9_shenzhen_r001_actual_occupied_days"
FROZEN_PROTOCOL_SHA256 = "e86545e2d5db54a87f54027a4f14cbf89de33b3846afcc573d6242faeba918ad"
TARGET_WEIGHTED_ANNUALIZED_RETURN = 0.40
KNOWN_V1_WEIGHTED_ANNUALIZED_RETURN = 0.3935547179608531


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "retrospective_mechanics_correction",
        "post_hoc_disclosure": {
            "prior_protocol": "v9_shenzhen_r001_idle_cash_sweep",
            "prior_protocol_sha256": (
                "e7850362d64d91171da3c9b1982f3f3e2eb993abfea04c293e30ce743b6fd87b"
            ),
            "prior_result_seen": True,
            "prior_close_base_weighted_annualized_return": (
                KNOWN_V1_WEIGHTED_ANNUALIZED_RETURN
            ),
            "prior_decision": "REJECT",
            "interpretation": (
                "Crossing 40% after this correction is exploratory retrospective evidence, "
                "not an independent replication."
            ),
        },
        "mechanics_change_only": {
            "changed": (
                "replace the deliberately conservative one-day credit with exchange-defined "
                "actual occupied calendar days"
            ),
            "unchanged": [
                "R-001 instrument and immutable rate snapshot",
                "V9 signals and all equity trades",
                "Close base rate and Low stress rate",
                "0.001% base and 0.002% stress commission",
                "1,000 CNY principal lot",
                "all performance and risk gates",
            ],
        },
        "settlement_model": {
            "trade_day": "V9 ledger session i after equity fills, near the close",
            "first_settlement_day": "next exchange session i+1",
            "maturity_settlement_day": "following exchange session i+2",
            "actual_occupied_days": "calendar days from i+1 inclusive to i+2 exclusive",
            "interest_formula": (
                "principal * quoted annual rate / 100 * actual occupied days / 365"
            ),
            "principal_availability": (
                "principal is assumed securities-trading-available on i+1; this must be "
                "confirmed for the configured broker"
            ),
            "interest_credit": "net interest is added on i+2",
            "terminal_treatment": "last two sessions create no recognized unsettled interest",
            "calendar_proxy": (
                "the frozen V9 equity-session calendar is used as the exchange settlement calendar"
            ),
        },
        "invariants": {
            "v9_trade_dates_unchanged": True,
            "v9_codes_quantities_prices_fees_unchanged": True,
            "v9_signal_logic_unchanged": True,
            "equity_orders_have_priority": True,
            "no_new_equity_positions": True,
            "no_production_registration": True,
        },
        "instrument": {
            "code": INSTRUMENT_CODE,
            "rate_snapshot_end": SNAPSHOT_END,
            "principal_lot_cny": PRINCIPAL_LOT,
        },
        "scenarios": [asdict(scenario) for scenario in SCENARIOS],
        "evaluation": {
            "target_weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
            "cash_sensitivity_required_net_annual_yield": REQUIRED_NET_ANNUAL_YIELD,
            "maximum_window_drawdown": -0.10,
            "maximum_drawdown_degradation": 0.002,
            "weights": [asdict(window) for window in WINDOWS],
        },
        "decision_rule": {
            "base_required": [
                "weighted annualized return at least 40%",
                "positive incremental return in all five windows",
                "all window drawdowns no worse than -10%",
                "drawdown degradation no more than 0.2 percentage points",
                "100% repo-rate coverage for eligible sessions",
                "exact frozen V9 baseline reproduction",
            ],
            "stress_required": [
                "positive net repo interest in all five windows",
                "weighted annualized return above unchanged V9",
            ],
            "passing_decision": "REQUIRE_BROKER_AUDIT_AND_FORWARD_VALIDATION",
            "passing_is_not_production_authorization": True,
            "minimum_forward_paper_sessions": 60,
        },
        "official_sources": [
            {
                "authority": "Shenzhen Stock Exchange",
                "url": "https://www.szse.cn/marketServices/technicalservice/notice/t20170419_520995.html",
                "frozen_fact": (
                    "Actual occupied days run from the first settlement day inclusive to "
                    "the maturity settlement day exclusive, by calendar day; price is an "
                    "actual annualized rate."
                ),
            },
            {
                "authority": "Shenzhen Stock Exchange investor education",
                "url": "https://investor.szse.cn/knowledge/t20171023_538895.html",
                "frozen_fact": (
                    "Purchase price equals principal plus annual rate times actual occupied "
                    "days divided by 365."
                ),
            },
            {
                "authority": "Shanghai Stock Exchange",
                "url": "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20170519_4313753.shtml",
                "frozen_fact": (
                    "The published one-day example earns five calendar days between Friday "
                    "first settlement and Wednesday maturity settlement."
                ),
            },
            {
                "authority": "Shenzhen CSRC",
                "url": "https://www.csrc.gov.cn/shenzhen/c105614/c7521963/content.shtml",
                "frozen_fact": (
                    "One-day repo principal can be securities-trading-available the next morning."
                ),
            },
        ],
        "known_limitations": [
            "The correction was specified after observing the V1 target miss.",
            "TDX close and low rates do not prove a near-close executable quote.",
            "The ledger calendar does not encode exceptional settlement-system closures.",
            "Account-specific fees and principal availability remain unverified.",
            "No untouched historical holdout remains; forward evidence is mandatory.",
        ],
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "actual_days_protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen actual-days protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def simulate_actual_days_repo_sweep(
    equity: pd.DataFrame,
    repo_rates: pd.DataFrame,
    *,
    initial_cash: float,
    rate_field: str,
    commission_rate: float,
) -> dict[str, Any]:
    required = {"timestamp", "equity", "cash"}
    missing = required.difference(equity.columns)
    if missing:
        raise ValueError(f"Missing equity columns: {sorted(missing)}")
    if rate_field not in {"Close", "Low"}:
        raise ValueError("rate_field must be Close or Low")
    if commission_rate < 0.0:
        raise ValueError("commission_rate must be non-negative")

    frame = equity.loc[:, ["timestamp", "equity", "cash"]].copy()
    frame["timestamp"] = _normalize_timestamp(frame["timestamp"])
    frame["equity"] = pd.to_numeric(frame["equity"], errors="raise")
    frame["cash"] = pd.to_numeric(frame["cash"], errors="raise")
    frame.sort_values("timestamp", inplace=True)
    frame.reset_index(drop=True, inplace=True)
    if frame.empty:
        raise ValueError("Equity ledger is empty")
    if frame["timestamp"].duplicated().any():
        raise ValueError("Equity ledger contains duplicate sessions")
    if frame["cash"].lt(-1e-8).any():
        raise ValueError("Reverse repo cannot be applied to a negative-cash ledger")

    rates = repo_rates.loc[:, ["timestamp", rate_field]].copy()
    rates["timestamp"] = _normalize_timestamp(rates["timestamp"])
    rates[rate_field] = pd.to_numeric(rates[rate_field], errors="raise")
    if rates["timestamp"].duplicated().any():
        raise ValueError("Repo rates contain duplicate sessions")
    rate_by_day = rates.set_index("timestamp")[rate_field]
    eligible_days = frame["timestamp"].iloc[:-2]
    missing_days = eligible_days.loc[~eligible_days.isin(rate_by_day.index)]
    if not missing_days.empty:
        examples = ", ".join(day.date().isoformat() for day in missing_days.iloc[:3])
        raise ValueError(f"Missing R-001 rates for {len(missing_days)} sessions: {examples}")

    length = len(frame)
    settled_pnl = np.zeros(length, dtype=float)
    pending_credits = np.zeros(length, dtype=float)
    principal_by_day = np.zeros(length, dtype=float)
    gross_by_day = np.zeros(length, dtype=float)
    fee_by_day = np.zeros(length, dtype=float)
    occupied_days_by_day = np.zeros(length, dtype=int)
    rate_by_ledger_day = np.full(length, np.nan, dtype=float)
    executed = np.zeros(length, dtype=bool)
    skipped_unprofitable = np.zeros(length, dtype=bool)

    for index in range(length):
        if index > 0:
            settled_pnl[index] = settled_pnl[index - 1] + pending_credits[index]
        else:
            settled_pnl[index] = pending_credits[index]
        if index + 2 >= length:
            continue
        trade_day = frame.at[index, "timestamp"]
        quoted_rate = float(rate_by_day.loc[trade_day])
        first_settlement = frame.at[index + 1, "timestamp"]
        maturity_settlement = frame.at[index + 2, "timestamp"]
        occupied_days = int((maturity_settlement - first_settlement).days)
        if occupied_days <= 0:
            raise ValueError("Actual occupied days must be positive")
        available_cash = float(frame.at[index, "cash"]) + settled_pnl[index]
        principal = max(0.0, np.floor((available_cash + 1e-9) / PRINCIPAL_LOT)) * PRINCIPAL_LOT
        gross = principal * quoted_rate / 100.0 * occupied_days / 365.0
        fee = principal * commission_rate
        net = gross - fee
        rate_by_ledger_day[index] = quoted_rate
        occupied_days_by_day[index] = occupied_days
        principal_by_day[index] = principal
        if principal > 0.0 and net > 0.0:
            gross_by_day[index] = gross
            fee_by_day[index] = fee
            executed[index] = True
            pending_credits[index + 2] += net
        else:
            skipped_unprofitable[index] = principal > 0.0

    frame["repo_rate_percent"] = rate_by_ledger_day
    frame["actual_occupied_days"] = occupied_days_by_day
    frame["repo_principal"] = principal_by_day
    frame["repo_gross_interest"] = gross_by_day
    frame["repo_fee"] = fee_by_day
    frame["repo_executed"] = executed
    frame["repo_skipped_unprofitable"] = skipped_unprofitable
    frame["settled_repo_pnl"] = settled_pnl
    frame["adjusted_cash"] = frame["cash"] + settled_pnl
    frame["adjusted_equity"] = frame["equity"] + settled_pnl

    curve = frame["adjusted_equity"]
    total_return = float(curve.iloc[-1] / initial_cash - 1.0)
    annualized_return = (
        float((1.0 + total_return) ** (252.0 / max(1, length - 1)) - 1.0)
        if length > 1
        else 0.0
    )
    max_drawdown = float((curve / curve.cummax() - 1.0).min())
    total_principal_days = float(
        (principal_by_day[executed] * occupied_days_by_day[executed]).sum()
    )
    net_interest = float(settled_pnl[-1])
    net_annual_yield = (
        float(net_interest / total_principal_days * 365.0)
        if total_principal_days > 0.0
        else 0.0
    )
    eligible_count = max(0, length - 2)
    cash_base = frame["cash"].iloc[:eligible_count].to_numpy(dtype=float)
    cash_adjustment = settled_pnl[:eligible_count]
    return {
        "rate_field": rate_field,
        "commission_rate": float(commission_rate),
        "sessions": int(length),
        "eligible_sessions": int(eligible_count),
        "rate_coverage": 1.0 if eligible_count else 0.0,
        "repo_trades": int(executed.sum()),
        "skipped_unprofitable": int(skipped_unprofitable.sum()),
        "total_actual_occupied_days": int(occupied_days_by_day[executed].sum()),
        "average_actual_occupied_days": (
            float(occupied_days_by_day[executed].mean()) if executed.any() else 0.0
        ),
        "average_principal": (
            float(principal_by_day[:eligible_count].mean()) if eligible_count else 0.0
        ),
        "average_cash_utilization": (
            float(
                np.divide(
                    principal_by_day[:eligible_count],
                    cash_base + cash_adjustment,
                    out=np.zeros(eligible_count, dtype=float),
                    where=(cash_base + cash_adjustment) > 0.0,
                ).mean()
            )
            if eligible_count
            else 0.0
        ),
        "gross_interest": float(gross_by_day.sum()),
        "commission": float(fee_by_day.sum()),
        "net_interest": net_interest,
        "net_annual_yield_on_swept_principal": net_annual_yield,
        "interest_contribution": float(net_interest / initial_cash),
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "final_equity": float(curve.iloc[-1]),
        "equity": frame,
    }


def evaluate_actual_days_sweep(
    database_path: Path,
    repo_rates: pd.DataFrame,
    *,
    windows: Sequence[EvaluationWindow] = WINDOWS,
) -> dict[str, Any]:
    ledgers = _load_ledgers(database_path, windows)
    baseline = _baseline_report(ledgers, windows)
    scenarios = [
        _evaluate_scenario(ledgers, repo_rates, windows, scenario)
        for scenario in SCENARIOS
    ]
    by_id = {scenario["scenario_id"]: scenario for scenario in scenarios}
    generic_decision = assess_reverse_repo_sweep(
        baseline,
        by_id["close_base_commission"],
        by_id["low_double_commission"],
    )
    qualified = bool(generic_decision["retrospective_qualified"])
    decision = {
        **generic_decision,
        "decision": (
            "REQUIRE_BROKER_AUDIT_AND_FORWARD_VALIDATION" if qualified else "REJECT"
        ),
        "independent_replication": False,
        "production_authorized": False,
    }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "baseline": baseline,
        "scenarios": scenarios,
        "decision": decision,
    }


def run_frozen_validation(
    database_path: Path,
    snapshot_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    protocol_path = output_dir / "actual_days_protocol.json"
    actual_hash = _file_sha256(protocol_path)
    if actual_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen actual-days protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={actual_hash}"
        )
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    rates = load_repo_snapshot(snapshot_dir)
    result = evaluate_actual_days_sweep(database_path, rates)
    payload = {
        "protocol_sha256": actual_hash,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_rates_sha256": manifest["rates_sha256"],
        **result,
    }
    (output_dir / "actual_days_validation_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def diagnose_commission_threshold(
    database_path: Path,
    repo_rates: pd.DataFrame,
    *,
    windows: Sequence[EvaluationWindow] = WINDOWS,
    upper_commission_rate: float = 0.00002,
) -> dict[str, Any]:
    """Post-result diagnostic; it cannot change the frozen qualification decision."""

    ledgers = _load_ledgers(database_path, windows)

    def weighted_return(commission_rate: float) -> float:
        scenario = RepoScenario("commission_diagnostic", "Close", commission_rate)
        return float(
            _evaluate_scenario(ledgers, repo_rates, windows, scenario)[
                "weighted_annualized_return"
            ]
        )

    maximum_passing = _bisect_maximum_passing_commission(
        weighted_return,
        target=TARGET_WEIGHTED_ANNUALIZED_RETURN,
        lower=0.0,
        upper=upper_commission_rate,
    )
    diagnostic_rates = sorted(
        {
            0.0,
            0.0000025,
            0.000005,
            0.0000075,
            0.00001,
            float(maximum_passing) if maximum_passing is not None else 0.0,
        }
    )
    scenarios = []
    for commission_rate in diagnostic_rates:
        scenario = RepoScenario("commission_diagnostic", "Close", commission_rate)
        result = _evaluate_scenario(ledgers, repo_rates, windows, scenario)
        scenarios.append(
            {
                "commission_rate": commission_rate,
                "commission_percent_of_principal": commission_rate * 100.0,
                "weighted_annualized_return": result["weighted_annualized_return"],
                "window_annualized_returns": {
                    window["label"]: window["annualized_return"]
                    for window in result["windows"]
                },
            }
        )
    return {
        "diagnostic_status": "POST_RESULT_NON_QUALIFYING",
        "frozen_decision_unchanged": True,
        "target_weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
        "maximum_commission_rate_for_target": maximum_passing,
        "maximum_commission_percent_of_principal_for_target": (
            maximum_passing * 100.0 if maximum_passing is not None else None
        ),
        "frozen_base_commission_rate": 0.00001,
        "requires_discount_from_frozen_base": (
            maximum_passing is not None and maximum_passing < 0.00001
        ),
        "scenarios": scenarios,
        "interpretation": (
            "Only an account-specific documented commission at or below the threshold can "
            "justify a new preregistered forward validation; this diagnostic does not pass "
            "the frozen retrospective protocol."
        ),
    }


def save_commission_diagnostic(
    database_path: Path,
    snapshot_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    rates = load_repo_snapshot(snapshot_dir)
    result = diagnose_commission_threshold(database_path, rates)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "commission_diagnostic.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _evaluate_scenario(
    ledgers: Mapping[str, Mapping[str, Any]],
    repo_rates: pd.DataFrame,
    windows: Sequence[EvaluationWindow],
    scenario: RepoScenario,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for window in windows:
        source = ledgers[window.label]
        metrics = source["metrics"]
        report = simulate_actual_days_repo_sweep(
            source["equity"],
            repo_rates,
            initial_cash=float(metrics["initial_cash"]),
            rate_field=scenario.rate_field,
            commission_rate=scenario.commission_rate,
        )
        report.pop("equity")
        reports.append(
            {
                "label": window.label,
                "backtest_id": window.backtest_id,
                "weight": window.weight,
                "baseline_total_return": float(metrics["total_return"]),
                "baseline_annualized_return": float(metrics["annualized_return"]),
                "baseline_max_drawdown": float(metrics["max_drawdown"]),
                "incremental_total_return": (
                    float(report["total_return"]) - float(metrics["total_return"])
                ),
                "drawdown_degradation": (
                    float(report["max_drawdown"]) - float(metrics["max_drawdown"])
                ),
                **report,
            }
        )
    return {
        **asdict(scenario),
        "weighted_annualized_return": float(
            sum(report["weight"] * report["annualized_return"] for report in reports)
        ),
        "windows": reports,
    }


def _bisect_maximum_passing_commission(
    evaluator: Callable[[float], float],
    *,
    target: float,
    lower: float,
    upper: float,
    tolerance: float = 1e-12,
) -> float | None:
    if lower < 0.0 or upper <= lower:
        raise ValueError("Commission search bounds are invalid")
    if evaluator(lower) < target:
        return None
    if evaluator(upper) >= target:
        return upper
    passing = lower
    failing = upper
    for _ in range(80):
        midpoint = (passing + failing) / 2.0
        if evaluator(midpoint) >= target:
            passing = midpoint
        else:
            failing = midpoint
        if failing - passing <= tolerance:
            break
    return passing
