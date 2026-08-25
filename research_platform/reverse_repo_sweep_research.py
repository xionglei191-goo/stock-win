from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .cash_sweep_research import EvaluationWindow, WINDOWS
from .etf_pullback_research import DAY_DTYPE


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "v9_shenzhen_r001_idle_cash_sweep"
FROZEN_PROTOCOL_SHA256 = "e7850362d64d91171da3c9b1982f3f3e2eb993abfea04c293e30ce743b6fd87b"
INSTRUMENT_CODE = "131810.SZ"
INSTRUMENT_NAME = "R-001"
RATE_SCALE = 10_000.0
PRINCIPAL_LOT = 1_000.0
BASE_COMMISSION_RATE = 0.00001
STRESS_COMMISSION_RATE = 0.00002
TARGET_WEIGHTED_ANNUALIZED_RETURN = 0.40
REQUIRED_NET_ANNUAL_YIELD = 0.013616367983195234
SNAPSHOT_START = "2021-04-01"
SNAPSHOT_END = "2026-08-07"


@dataclass(frozen=True)
class RepoScenario:
    scenario_id: str
    rate_field: str
    commission_rate: float


SCENARIOS = (
    RepoScenario("close_base_commission", "Close", BASE_COMMISSION_RATE),
    RepoScenario("low_double_commission", "Low", STRESS_COMMISSION_RATE),
)


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "retrospective_instrument_validation",
        "relationship_to_production": (
            "independent cash-efficiency research; never loaded by scans, signals, "
            "paper positions, or order execution"
        ),
        "instrument": {
            "code": INSTRUMENT_CODE,
            "name": INSTRUMENT_NAME,
            "market": "Shenzhen Stock Exchange",
            "local_day_file": "vipdoc/sz/lday/sz131810.day",
            "quoted_value": "annualized repo rate in percent",
            "tdx_integer_rate_scale": RATE_SCALE,
            "principal_lot_cny": PRINCIPAL_LOT,
            "selection_reason": (
                "The 1,000 CNY lot is compatible with the frozen V9 50,000 CNY ledger."
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
        "snapshot": {
            "source": "local TDX unadjusted DAY binary prefix",
            "start_date": SNAPSHOT_START,
            "end_date": SNAPSHOT_END,
            "future_records_excluded_while_reading": True,
            "immutable_content_addressed_parquet": True,
        },
        "execution": {
            "decision_time": "after the current session equity fills, near the close",
            "principal": "floor(post-trade cash plus credited repo interest / 1000) * 1000",
            "credit_time": "next observed equity session before its valuation",
            "interest_day_count": 365,
            "credited_days_per_roll": 1,
            "weekend_and_holiday_extra_days": (
                "deliberately ignored; this is conservative and avoids settlement overlap"
            ),
            "negative_net_quote": "skip the roll when gross interest does not exceed commission",
            "last_session": "no uncredited terminal interest",
            "same_day_equity_sale_cash": "eligible only after the equity fills, at the close",
            "same_open_funding": (
                "repo principal is assumed available for the next session equity open; "
                "broker confirmation remains mandatory"
            ),
        },
        "costs": {
            "base_commission_rate_of_principal_per_roll": BASE_COMMISSION_RATE,
            "stress_commission_rate_of_principal_per_roll": STRESS_COMMISSION_RATE,
            "minimum_commission": 0.0,
            "exchange_fee": (
                "assumed included in the commission stress; current waiver is not extrapolated"
            ),
            "broker_schedule_status": "assumption requiring account-specific confirmation",
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
                "100% repo-rate coverage for eligible V9 ledger sessions",
                "exact frozen V9 baseline reproduction",
            ],
            "stress_required": [
                "positive net repo interest in all five windows",
                "weighted annualized return above the unchanged V9 baseline",
            ],
            "passing_decision": "REQUIRE_BROKER_EXECUTION_AUDIT",
            "passing_is_not_production_authorization": True,
            "minimum_forward_paper_sessions_after_audit": 60,
        },
        "official_sources": [
            {
                "authority": "Shenzhen CSRC",
                "url": "https://www.csrc.gov.cn/shenzhen/c105614/c7521963/content.shtml",
                "frozen_fact": (
                    "One-day repo idle-cash products can credit principal and return before "
                    "the next morning; credited funds can be used for securities trading."
                ),
            },
            {
                "authority": "Shenzhen Stock Exchange",
                "url": "https://investor.szse.cn/institute/bookshelf/manualseriesbook/P020200528604329123743.pdf",
                "frozen_fact": "R-001 is code 131810 and Shenzhen repo units are stated in bond lots.",
            },
            {
                "authority": "Shanghai Stock Exchange",
                "url": "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20170519_4313753.shtml",
                "frozen_fact": "Repo interest uses the quoted annual rate, 365-day basis, and actual occupied days.",
            },
            {
                "authority": "Shanghai Stock Exchange",
                "url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/charge/c/c_20250610_10781461.shtml",
                "frozen_fact": "Published exchange repo fee schedules are separate from broker commission.",
            },
        ],
        "known_limitations": [
            "TDX daily close is a benchmark, not proof of an executable near-close quote.",
            "The daily low plus doubled commission is a stress scenario, not a fill guarantee.",
            "Historical broker commissions and account settlement behavior are not in TDX data.",
            "All five windows are retrospective; no untouched final holdout remains.",
            "This cash overlay is portfolio engineering, not new low-buy alpha.",
        ],
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen reverse-repo protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def decode_repo_day_bytes(data: bytes) -> pd.DataFrame:
    if len(data) % DAY_DTYPE.itemsize:
        raise ValueError("DAY payload size must be a multiple of 32 bytes")
    records = np.frombuffer(data, dtype=DAY_DTYPE)
    if not len(records):
        return pd.DataFrame(columns=["timestamp", "Open", "High", "Low", "Close"])
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(records["date"].astype(str), errors="coerce"),
            "Open": records["open"].astype(float) / RATE_SCALE,
            "High": records["high"].astype(float) / RATE_SCALE,
            "Low": records["low"].astype(float) / RATE_SCALE,
            "Close": records["close"].astype(float) / RATE_SCALE,
        }
    ).dropna(subset=["timestamp"])
    frame.sort_values("timestamp", inplace=True)
    frame.reset_index(drop=True, inplace=True)
    if frame["timestamp"].duplicated().any():
        raise ValueError("R-001 DAY data contains duplicate sessions")
    if not frame["timestamp"].is_monotonic_increasing:
        raise ValueError("R-001 DAY data is not sorted")
    invalid = (
        frame[["Open", "High", "Low", "Close"]].lt(0.0).any(axis=1)
        | frame["Low"].gt(frame[["Open", "Close"]].min(axis=1))
        | frame["High"].lt(frame[["Open", "Close"]].max(axis=1))
        | frame["Low"].gt(frame["High"])
        | frame[["Open", "High", "Low", "Close"]].gt(100.0).any(axis=1)
    )
    if invalid.any():
        raise ValueError(f"R-001 DAY data contains {int(invalid.sum())} invalid rate rows")
    return frame


def create_repo_snapshot(
    *,
    day_path: Path,
    output_root: Path,
    start_date: str = SNAPSHOT_START,
    end_date: str = SNAPSHOT_END,
) -> dict[str, Any]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Reverse-repo snapshot cannot extend beyond the frozen end")
    prefix, source = _read_day_prefix(Path(day_path), end)
    rates = decode_repo_day_bytes(prefix)
    rates = rates.loc[rates["timestamp"].between(start, end)].reset_index(drop=True)
    if rates.empty:
        raise ValueError("Reverse-repo snapshot is empty")

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".repo_r001_", dir=str(output_root)))
    try:
        rates_path = staging / "rates.parquet"
        rates.to_parquet(rates_path, index=False)
        rates_hash = _file_sha256(rates_path)
        identity = {
            "protocol_version": PROTOCOL_VERSION,
            "instrument_code": INSTRUMENT_CODE,
            "start_date": str(start.date()),
            "end_date": str(end.date()),
            "source_prefix_sha256": source["prefix_sha256"],
            "source_prefix_bytes": source["prefix_bytes"],
            "rates_sha256": rates_hash,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()
        manifest = {
            **identity,
            "snapshot_id": snapshot_id,
            "source_path": source["path"],
            "rows": int(len(rates)),
            "minimum_date": str(rates["timestamp"].min().date()),
            "maximum_date": str(rates["timestamp"].max().date()),
            "minimum_close_rate_percent": float(rates["Close"].min()),
            "maximum_close_rate_percent": float(rates["Close"].max()),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        target = output_root / snapshot_id
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("rates_sha256") != rates_hash:
                raise ValueError(f"Immutable reverse-repo snapshot collision: {snapshot_id}")
            return existing
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_repo_snapshot(snapshot_dir: Path) -> pd.DataFrame:
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    path = snapshot_dir / "rates.parquet"
    actual_hash = _file_sha256(path)
    if actual_hash != manifest.get("rates_sha256"):
        raise ValueError(
            "Reverse-repo snapshot hash mismatch: "
            f"expected={manifest.get('rates_sha256')}, actual={actual_hash}"
        )
    rates = pd.read_parquet(path)
    rates["timestamp"] = _normalize_timestamp(rates["timestamp"])
    if rates["timestamp"].max() > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Reverse-repo snapshot contains post-protocol data")
    return rates


def simulate_repo_sweep(
    equity: pd.DataFrame,
    repo_rates: pd.DataFrame,
    *,
    initial_cash: float,
    rate_field: str,
    commission_rate: float,
) -> dict[str, Any]:
    required_equity = {"timestamp", "equity", "cash"}
    missing = required_equity.difference(equity.columns)
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
    eligible_days = frame["timestamp"].iloc[:-1]
    missing_days = eligible_days.loc[~eligible_days.isin(rate_by_day.index)]
    if not missing_days.empty:
        examples = ", ".join(day.date().isoformat() for day in missing_days.iloc[:3])
        raise ValueError(f"Missing R-001 rates for {len(missing_days)} sessions: {examples}")

    accrued = np.zeros(len(frame), dtype=float)
    principal_by_day = np.zeros(len(frame), dtype=float)
    gross_by_day = np.zeros(len(frame), dtype=float)
    fee_by_day = np.zeros(len(frame), dtype=float)
    rate_by_ledger_day = np.full(len(frame), np.nan, dtype=float)
    executed = np.zeros(len(frame), dtype=bool)
    skipped_unprofitable = np.zeros(len(frame), dtype=bool)
    for index in range(1, len(frame)):
        trade_index = index - 1
        trade_day = frame.at[trade_index, "timestamp"]
        quoted_rate = float(rate_by_day.loc[trade_day])
        available_cash = float(frame.at[trade_index, "cash"]) + accrued[trade_index]
        principal = max(0.0, np.floor((available_cash + 1e-9) / PRINCIPAL_LOT)) * PRINCIPAL_LOT
        gross = principal * quoted_rate / 100.0 / 365.0
        fee = principal * commission_rate
        net = gross - fee
        rate_by_ledger_day[trade_index] = quoted_rate
        principal_by_day[trade_index] = principal
        if principal > 0.0 and net > 0.0:
            gross_by_day[trade_index] = gross
            fee_by_day[trade_index] = fee
            executed[trade_index] = True
            accrued[index] = accrued[trade_index] + net
        else:
            skipped_unprofitable[trade_index] = principal > 0.0
            accrued[index] = accrued[trade_index]

    frame["repo_rate_percent"] = rate_by_ledger_day
    frame["repo_principal"] = principal_by_day
    frame["repo_gross_interest"] = gross_by_day
    frame["repo_fee"] = fee_by_day
    frame["repo_executed"] = executed
    frame["repo_skipped_unprofitable"] = skipped_unprofitable
    frame["accrued_repo_interest"] = accrued
    frame["adjusted_cash"] = frame["cash"] + accrued
    frame["adjusted_equity"] = frame["equity"] + accrued

    curve = frame["adjusted_equity"]
    total_return = float(curve.iloc[-1] / initial_cash - 1.0)
    annualized_return = (
        float((1.0 + total_return) ** (252.0 / max(1, len(frame) - 1)) - 1.0)
        if len(frame) > 1
        else 0.0
    )
    max_drawdown = float((curve / curve.cummax() - 1.0).min())
    total_principal_days = float(principal_by_day[executed].sum())
    total_net_interest = float(accrued[-1])
    net_annual_yield = (
        float(total_net_interest / total_principal_days * 365.0)
        if total_principal_days > 0.0
        else 0.0
    )
    eligible_count = max(0, len(frame) - 1)
    return {
        "rate_field": rate_field,
        "commission_rate": float(commission_rate),
        "sessions": int(len(frame)),
        "eligible_sessions": int(eligible_count),
        "rate_coverage": 1.0 if eligible_count else 0.0,
        "repo_trades": int(executed.sum()),
        "skipped_unprofitable": int(skipped_unprofitable.sum()),
        "average_principal": (
            float(principal_by_day[:-1].mean()) if eligible_count else 0.0
        ),
        "average_cash_utilization": (
            float(
                np.divide(
                    principal_by_day[:-1],
                    frame["cash"].iloc[:-1].to_numpy(dtype=float) + accrued[:-1],
                    out=np.zeros(eligible_count, dtype=float),
                    where=(frame["cash"].iloc[:-1].to_numpy(dtype=float) + accrued[:-1]) > 0.0,
                ).mean()
            )
            if eligible_count
            else 0.0
        ),
        "gross_interest": float(gross_by_day.sum()),
        "commission": float(fee_by_day.sum()),
        "net_interest": total_net_interest,
        "net_annual_yield_on_swept_principal": net_annual_yield,
        "interest_contribution": float(total_net_interest / initial_cash),
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "final_equity": float(curve.iloc[-1]),
        "equity": frame,
    }


def evaluate_reverse_repo_sweep(
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
    scenario_by_id = {scenario["scenario_id"]: scenario for scenario in scenarios}
    decision = assess_reverse_repo_sweep(
        baseline,
        scenario_by_id["close_base_commission"],
        scenario_by_id["low_double_commission"],
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "baseline": baseline,
        "scenarios": scenarios,
        "decision": decision,
    }


def assess_reverse_repo_sweep(
    baseline: Mapping[str, Any],
    base: Mapping[str, Any],
    stress: Mapping[str, Any],
) -> dict[str, Any]:
    baseline_weighted = float(baseline["weighted_annualized_return"])
    base_checks = {
        "exact_v9_baseline_reproduction": bool(baseline["exact_baseline_reproduction"]),
        "weighted_annualized_return_at_least_40_percent": (
            float(base["weighted_annualized_return"])
            >= TARGET_WEIGHTED_ANNUALIZED_RETURN - 1e-12
        ),
        "positive_increment_all_windows": all(
            float(window["incremental_total_return"]) > 0.0 for window in base["windows"]
        ),
        "all_window_drawdowns_within_ten_percent": all(
            float(window["max_drawdown"]) >= -0.10 for window in base["windows"]
        ),
        "drawdown_degradation_within_point_two_percent": all(
            float(window["drawdown_degradation"]) >= -0.002 - 1e-12
            for window in base["windows"]
        ),
        "complete_rate_coverage": all(
            float(window["rate_coverage"]) == 1.0 for window in base["windows"]
        ),
        "v9_equity_trades_unchanged": True,
        "no_v9_cash_block": True,
    }
    stress_checks = {
        "positive_net_interest_all_windows": all(
            float(window["net_interest"]) > 0.0 for window in stress["windows"]
        ),
        "weighted_return_improves_v9": (
            float(stress["weighted_annualized_return"]) > baseline_weighted
        ),
        "v9_equity_trades_unchanged": True,
        "no_v9_cash_block": True,
    }
    qualified = all(base_checks.values()) and all(stress_checks.values())
    return {
        "decision": "REQUIRE_BROKER_EXECUTION_AUDIT" if qualified else "REJECT",
        "retrospective_qualified": qualified,
        "production_authorized": False,
        "base_checks": base_checks,
        "stress_checks": stress_checks,
        "required_next_evidence": (
            "account-specific commission and settlement confirmation, near-close quote audit, "
            "then at least 60 forward paper sessions"
        ),
    }


def run_frozen_validation(
    database_path: Path,
    snapshot_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    actual_hash = _file_sha256(output_dir / "protocol.json")
    if actual_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen reverse-repo protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={actual_hash}"
        )
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    rates = load_repo_snapshot(snapshot_dir)
    result = evaluate_reverse_repo_sweep(database_path, rates)
    payload = {
        "protocol_sha256": actual_hash,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_rates_sha256": manifest["rates_sha256"],
        **result,
    }
    (output_dir / "validation_result.json").write_text(
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


def _baseline_report(
    ledgers: Mapping[str, Mapping[str, Any]],
    windows: Sequence[EvaluationWindow],
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    exact = True
    for window in windows:
        source = ledgers[window.label]
        metrics = source["metrics"]
        equity = source["equity"]
        curve = pd.to_numeric(equity["equity"], errors="raise")
        initial_cash = float(metrics["initial_cash"])
        total_return = float(curve.iloc[-1] / initial_cash - 1.0)
        annualized_return = float(
            (1.0 + total_return) ** (252.0 / max(1, len(curve) - 1)) - 1.0
        )
        max_drawdown = float((curve / curve.cummax() - 1.0).min())
        values = {
            "total_return": total_return,
            "annualized_return": annualized_return,
            "max_drawdown": max_drawdown,
            "final_equity": float(curve.iloc[-1]),
        }
        exact = exact and all(
            abs(values[key] - float(metrics[metric_key])) < 1e-12
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
                **values,
            }
        )
    return {
        "weighted_annualized_return": float(
            sum(report["weight"] * report["annualized_return"] for report in reports)
        ),
        "exact_baseline_reproduction": exact,
        "windows": reports,
    }


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
        report = simulate_repo_sweep(
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


def _read_day_prefix(path: Path, end: pd.Timestamp) -> tuple[bytes, dict[str, Any]]:
    chunks: list[bytes] = []
    previous_date = 0
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
    payload = b"".join(chunks)
    return payload, {
        "path": str(Path(path).resolve()),
        "prefix_bytes": len(payload),
        "prefix_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _normalize_timestamp(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, utc=True, errors="raise").dt.tz_convert(None).dt.normalize()


def _file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
