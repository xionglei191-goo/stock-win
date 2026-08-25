from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .cash_sweep_research import EvaluationWindow, WINDOWS
from .etf_pullback_research import DAY_DTYPE
from .reverse_repo_sweep_research import (
    PRINCIPAL_LOT,
    REQUIRED_NET_ANNUAL_YIELD,
    SCENARIOS,
    SNAPSHOT_END,
    SNAPSHOT_START,
    RepoScenario,
    _baseline_report,
    _file_sha256,
    _load_ledgers,
    _normalize_timestamp,
    _read_day_prefix,
    assess_reverse_repo_sweep,
)


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "v9_calendar_matched_reverse_repo_tenor_ladder"
FROZEN_PROTOCOL_SHA256 = "210bef406d154abcc8dac03688252d82a932103396eade9285da4a4e6467c9ab"
TARGET_WEIGHTED_ANNUALIZED_RETURN = 0.40
KNOWN_R001_BASE_WEIGHTED_RETURN = 0.39888869959508727
KNOWN_R001_STRESS_WEIGHTED_RETURN = 0.3926664456695309


@dataclass(frozen=True)
class RepoTenor:
    code: str
    name: str
    local_code: str
    tenor_days: int


TENORS = (
    RepoTenor("131810.SZ", "R-001", "sz131810", 1),
    RepoTenor("131811.SZ", "R-002", "sz131811", 2),
    RepoTenor("131800.SZ", "R-003", "sz131800", 3),
    RepoTenor("131809.SZ", "R-004", "sz131809", 4),
    RepoTenor("131801.SZ", "R-007", "sz131801", 7),
)


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "post_result_retrospective_extension",
        "post_hoc_disclosure": {
            "known_predecessor": "v9_shenzhen_r001_actual_occupied_days",
            "known_predecessor_protocol_sha256": (
                "e86545e2d5db54a87f54027a4f14cbf89de33b3846afcc573d6242faeba918ad"
            ),
            "known_predecessor_base_weighted_annualized_return": (
                KNOWN_R001_BASE_WEIGHTED_RETURN
            ),
            "known_predecessor_stress_weighted_annualized_return": (
                KNOWN_R001_STRESS_WEIGHTED_RETURN
            ),
            "known_target_shortfall_percentage_points": 0.111130040491273,
            "interpretation": (
                "The target shortfall was known before this protocol; a pass cannot be "
                "treated as independent historical validation."
            ),
        },
        "instruments": [asdict(tenor) for tenor in TENORS],
        "selection": {
            "decision_time": (
                "near the close after all current-session V9 equity fills, using live "
                "executable reverse-repo quotes"
            ),
            "eligible_tenor": (
                "nominal tenor in calendar days must be no greater than the known calendar "
                "gap to the next equity trading session"
            ),
            "liquidity": "candidate DAY volume must be positive",
            "ranking": (
                "highest observable annualized rate; ties use shorter tenor then code"
            ),
            "negative_net_quote": "skip when gross interest does not exceed commission",
            "daily_proxy_base": "highest eligible TDX daily Close rate",
            "daily_proxy_stress": (
                "highest eligible daily Low rate; because each live quote is no lower than "
                "that instrument's daily low, this is a conservative quote-selection bound"
            ),
        },
        "settlement": {
            "first_settlement": "next equity trading session i+1",
            "maturity_availability": (
                "eligible nominal maturity is no later than i+1, so principal is assumed "
                "available for every next-open V9 order"
            ),
            "maturity_settlement": "following equity trading session i+2",
            "actual_occupied_days": "calendar days from i+1 inclusive to i+2 exclusive",
            "interest_formula": (
                "principal * rate / 100 * actual occupied days / 365 - commission"
            ),
            "interest_credit": "net interest is credited on i+2",
            "terminal_treatment": "last two sessions recognize no unsettled interest",
        },
        "invariants": {
            "v9_trade_dates_unchanged": True,
            "v9_codes_quantities_prices_fees_unchanged": True,
            "v9_signal_logic_unchanged": True,
            "equity_orders_have_priority": True,
            "principal_available_every_next_open": True,
            "no_new_equity_positions": True,
            "no_production_registration": True,
        },
        "snapshot": {
            "source": "five local TDX unadjusted DAY binary prefixes",
            "start_date": SNAPSHOT_START,
            "end_date": SNAPSHOT_END,
            "future_records_excluded_while_reading": True,
            "all_five_rate_rows_required_for_every_eligible_ledger_session": True,
            "immutable_content_addressed_parquet": True,
        },
        "scenarios": [asdict(scenario) for scenario in SCENARIOS],
        "evaluation": {
            "target_weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
            "cash_sensitivity_required_net_annual_yield": REQUIRED_NET_ANNUAL_YIELD,
            "maximum_window_drawdown": -0.10,
            "maximum_drawdown_degradation": 0.002,
            "weights": [asdict(window) for window in WINDOWS],
            "single_tenor_r001_control_must_reproduce_predecessor": True,
        },
        "decision_rule": {
            "base_required": [
                "weighted annualized return at least 40%",
                "positive incremental return in all five windows",
                "all window drawdowns no worse than -10%",
                "drawdown degradation no more than 0.2 percentage points",
                "100% rate coverage for all five instruments",
                "exact V9 baseline and R-001 predecessor reproduction",
            ],
            "stress_required": [
                "positive net interest in all five windows",
                "weighted annualized return above unchanged V9",
            ],
            "passing_decision": "REQUIRE_BROKER_AUDIT_AND_FORWARD_VALIDATION",
            "passing_is_not_production_authorization": True,
            "minimum_forward_paper_sessions": 60,
        },
        "official_sources": [
            {
                "authority": "Shenzhen Stock Exchange",
                "url": "https://docs.static.szse.cn/www/bond/index/news/W020220127637593063957.pdf",
                "frozen_fact": (
                    "General repo tenors include 1, 2, 3, 4 and 7 days; maturity is based "
                    "on calendar days and moves to the next trading day when necessary."
                ),
            },
            {
                "authority": "Shenzhen Stock Exchange",
                "url": "https://www.szse.cn/marketServices/technicalservice/notice/t20170419_520995.html",
                "frozen_fact": (
                    "Quoted prices are actual annualized rates and interest uses actual "
                    "occupied calendar days on a 365-day basis."
                ),
            },
        ],
        "known_limitations": [
            "The extension was proposed after seeing the R-001 target shortfall.",
            "Daily Close is a benchmark and Low is a bound, not synchronized quote replay.",
            "Broker-specific fees and next-open principal availability remain unverified.",
            "The equity calendar may omit exceptional settlement-system closures.",
            "All historical windows are open; forward paper evidence is mandatory.",
            "This overlay improves cash efficiency and is not new low-buy alpha.",
        ],
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "tenor_ladder_protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen tenor-ladder protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def decode_tenor_day_bytes(data: bytes, tenor: RepoTenor) -> pd.DataFrame:
    if len(data) % DAY_DTYPE.itemsize:
        raise ValueError("DAY payload size must be a multiple of 32 bytes")
    records = np.frombuffer(data, dtype=DAY_DTYPE)
    if not len(records):
        return _empty_rates()
    frame = pd.DataFrame(
        {
            "code": tenor.code,
            "name": tenor.name,
            "tenor_days": tenor.tenor_days,
            "timestamp": pd.to_datetime(records["date"].astype(str), errors="coerce"),
            "Open": records["open"].astype(float) / 10_000.0,
            "High": records["high"].astype(float) / 10_000.0,
            "Low": records["low"].astype(float) / 10_000.0,
            "Close": records["close"].astype(float) / 10_000.0,
            "Amount": records["amount"].astype(float),
            "Volume": records["volume"].astype(float),
        }
    ).dropna(subset=["timestamp"])
    frame.sort_values("timestamp", inplace=True)
    frame.reset_index(drop=True, inplace=True)
    if frame["timestamp"].duplicated().any():
        raise ValueError(f"{tenor.code} contains duplicate sessions")
    invalid = (
        frame[["Open", "High", "Low", "Close"]].lt(0.0).any(axis=1)
        | frame["Low"].gt(frame[["Open", "Close"]].min(axis=1))
        | frame["High"].lt(frame[["Open", "Close"]].max(axis=1))
        | frame["Low"].gt(frame["High"])
        | frame[["Open", "High", "Low", "Close"]].gt(100.0).any(axis=1)
        | frame[["Amount", "Volume"]].lt(0.0).any(axis=1)
    )
    if invalid.any():
        raise ValueError(f"{tenor.code} contains {int(invalid.sum())} invalid rows")
    return frame


def create_tenor_snapshot(
    *,
    tdx_root: Path,
    output_root: Path,
    tenors: Sequence[RepoTenor] = TENORS,
    start_date: str = SNAPSHOT_START,
    end_date: str = SNAPSHOT_END,
) -> dict[str, Any]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Tenor snapshot cannot extend beyond the frozen end")
    frames: list[pd.DataFrame] = []
    sources: list[dict[str, Any]] = []
    for tenor in tenors:
        path = Path(tdx_root) / "vipdoc" / "sz" / "lday" / f"{tenor.local_code}.day"
        prefix, source = _read_day_prefix(path, end)
        frame = decode_tenor_day_bytes(prefix, tenor)
        frame = frame.loc[frame["timestamp"].between(start, end)].reset_index(drop=True)
        if frame.empty:
            raise ValueError(f"Tenor snapshot is empty for {tenor.code}")
        frames.append(frame)
        sources.append(
            {
                "code": tenor.code,
                "tenor_days": tenor.tenor_days,
                "path": source["path"],
                "prefix_bytes": source["prefix_bytes"],
                "prefix_sha256": source["prefix_sha256"],
                "rows": int(len(frame)),
            }
        )
    rates = pd.concat(frames, ignore_index=True).sort_values(
        ["timestamp", "tenor_days", "code"]
    ).reset_index(drop=True)
    expected_codes = {tenor.code for tenor in tenors}
    if set(rates["code"].unique()) != expected_codes:
        raise ValueError("Tenor snapshot does not contain the frozen instrument set")
    if rates.duplicated(["timestamp", "code"]).any():
        raise ValueError("Tenor snapshot contains duplicate session-code keys")

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".repo_tenors_", dir=str(output_root)))
    try:
        rates_path = staging / "rates.parquet"
        rates.to_parquet(rates_path, index=False)
        rates_hash = _file_sha256(rates_path)
        identity = {
            "protocol_version": PROTOCOL_VERSION,
            "start_date": str(start.date()),
            "end_date": str(end.date()),
            "instruments": [asdict(tenor) for tenor in tenors],
            "source_prefixes": sources,
            "rates_sha256": rates_hash,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        manifest = {
            **identity,
            "snapshot_id": snapshot_id,
            "rows": int(len(rates)),
            "codes": int(rates["code"].nunique()),
            "minimum_date": str(rates["timestamp"].min().date()),
            "maximum_date": str(rates["timestamp"].max().date()),
            "duplicate_keys": 0,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        target = output_root / snapshot_id
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("rates_sha256") != rates_hash:
                raise ValueError(f"Immutable tenor snapshot collision: {snapshot_id}")
            return existing
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_tenor_snapshot(snapshot_dir: Path) -> pd.DataFrame:
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    path = snapshot_dir / "rates.parquet"
    actual_hash = _file_sha256(path)
    if actual_hash != manifest.get("rates_sha256"):
        raise ValueError(
            "Tenor snapshot hash mismatch: "
            f"expected={manifest.get('rates_sha256')}, actual={actual_hash}"
        )
    rates = pd.read_parquet(path)
    rates["timestamp"] = _normalize_timestamp(rates["timestamp"])
    if rates["timestamp"].max() > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Tenor snapshot contains post-protocol data")
    return rates


def simulate_tenor_ladder(
    equity: pd.DataFrame,
    rates: pd.DataFrame,
    *,
    initial_cash: float,
    rate_field: str,
    commission_rate: float,
    tenors: Sequence[RepoTenor] = TENORS,
) -> dict[str, Any]:
    required_equity = {"timestamp", "equity", "cash"}
    missing = required_equity.difference(equity.columns)
    if missing:
        raise ValueError(f"Missing equity columns: {sorted(missing)}")
    required_rates = {"timestamp", "code", "tenor_days", rate_field, "Volume"}
    missing_rates = required_rates.difference(rates.columns)
    if missing_rates:
        raise ValueError(f"Missing rate columns: {sorted(missing_rates)}")
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
        raise ValueError("Tenor ladder cannot be applied to negative cash")

    quote_frame = rates.loc[:, list(required_rates)].copy()
    quote_frame["timestamp"] = _normalize_timestamp(quote_frame["timestamp"])
    quote_frame[rate_field] = pd.to_numeric(quote_frame[rate_field], errors="raise")
    quote_frame["Volume"] = pd.to_numeric(quote_frame["Volume"], errors="raise")
    quote_frame["tenor_days"] = pd.to_numeric(
        quote_frame["tenor_days"], errors="raise"
    ).astype(int)
    if quote_frame.duplicated(["timestamp", "code"]).any():
        raise ValueError("Tenor rates contain duplicate session-code keys")
    expected_codes = {tenor.code for tenor in tenors}
    quote_frame = quote_frame.loc[quote_frame["code"].isin(expected_codes)]
    eligible_days = frame["timestamp"].iloc[:-2]
    coverage = quote_frame.loc[quote_frame["timestamp"].isin(eligible_days)].groupby(
        "timestamp"
    )["code"].nunique()
    missing_coverage = eligible_days.loc[
        ~eligible_days.isin(coverage.loc[coverage.eq(len(expected_codes))].index)
    ]
    if not missing_coverage.empty:
        examples = ", ".join(day.date().isoformat() for day in missing_coverage.iloc[:3])
        raise ValueError(
            f"Incomplete tenor coverage for {len(missing_coverage)} sessions: {examples}"
        )
    quotes_by_day = {
        day: day_frame.copy()
        for day, day_frame in quote_frame.groupby("timestamp", sort=False)
    }

    length = len(frame)
    settled_pnl = np.zeros(length, dtype=float)
    pending_credits = np.zeros(length, dtype=float)
    principal_by_day = np.zeros(length, dtype=float)
    gross_by_day = np.zeros(length, dtype=float)
    fee_by_day = np.zeros(length, dtype=float)
    occupied_days_by_day = np.zeros(length, dtype=int)
    selected_rate = np.full(length, np.nan, dtype=float)
    selected_tenor = np.zeros(length, dtype=int)
    selected_code = np.full(length, "", dtype=object)
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
        next_session = frame.at[index + 1, "timestamp"]
        following_session = frame.at[index + 2, "timestamp"]
        calendar_gap = int((next_session - trade_day).days)
        occupied_days = int((following_session - next_session).days)
        candidates = quotes_by_day[trade_day]
        candidates = candidates.loc[
            candidates["tenor_days"].le(calendar_gap) & candidates["Volume"].gt(0.0)
        ].sort_values(
            [rate_field, "tenor_days", "code"],
            ascending=[False, True, True],
        )
        if candidates.empty:
            raise ValueError(f"No liquid eligible tenor on {trade_day.date().isoformat()}")
        selected = candidates.iloc[0]
        quoted_rate = float(selected[rate_field])
        available_cash = float(frame.at[index, "cash"]) + settled_pnl[index]
        principal = max(0.0, np.floor((available_cash + 1e-9) / PRINCIPAL_LOT)) * PRINCIPAL_LOT
        gross = principal * quoted_rate / 100.0 * occupied_days / 365.0
        fee = principal * commission_rate
        net = gross - fee
        selected_rate[index] = quoted_rate
        selected_tenor[index] = int(selected["tenor_days"])
        selected_code[index] = str(selected["code"])
        occupied_days_by_day[index] = occupied_days
        principal_by_day[index] = principal
        if principal > 0.0 and net > 0.0:
            gross_by_day[index] = gross
            fee_by_day[index] = fee
            executed[index] = True
            pending_credits[index + 2] += net
        else:
            skipped_unprofitable[index] = principal > 0.0

    frame["repo_code"] = selected_code
    frame["repo_tenor_days"] = selected_tenor
    frame["repo_rate_percent"] = selected_rate
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
    eligible_count = max(0, length - 2)
    selection_counts = Counter(selected_code[:eligible_count])
    return {
        "rate_field": rate_field,
        "commission_rate": float(commission_rate),
        "sessions": int(length),
        "eligible_sessions": int(eligible_count),
        "rate_coverage": 1.0 if eligible_count else 0.0,
        "repo_trades": int(executed.sum()),
        "skipped_unprofitable": int(skipped_unprofitable.sum()),
        "selection_counts": {
            code: int(selection_counts.get(code, 0)) for code in sorted(expected_codes)
        },
        "non_r001_selections": int(
            sum(count for code, count in selection_counts.items() if code != "131810.SZ")
        ),
        "average_actual_occupied_days": (
            float(occupied_days_by_day[executed].mean()) if executed.any() else 0.0
        ),
        "gross_interest": float(gross_by_day.sum()),
        "commission": float(fee_by_day.sum()),
        "net_interest": net_interest,
        "net_annual_yield_on_swept_principal": (
            float(net_interest / total_principal_days * 365.0)
            if total_principal_days > 0.0
            else 0.0
        ),
        "interest_contribution": float(net_interest / initial_cash),
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "final_equity": float(curve.iloc[-1]),
        "equity": frame,
    }


def evaluate_tenor_ladder(
    database_path: Path,
    rates: pd.DataFrame,
    *,
    windows: Sequence[EvaluationWindow] = WINDOWS,
) -> dict[str, Any]:
    ledgers = _load_ledgers(database_path, windows)
    baseline = _baseline_report(ledgers, windows)
    scenarios = [
        _evaluate_scenario(ledgers, rates, windows, scenario, TENORS)
        for scenario in SCENARIOS
    ]
    r001_control = [
        _evaluate_scenario(ledgers, rates, windows, scenario, TENORS[:1])
        for scenario in SCENARIOS
    ]
    control_exact = (
        abs(
            float(r001_control[0]["weighted_annualized_return"])
            - KNOWN_R001_BASE_WEIGHTED_RETURN
        )
        < 1e-12
        and abs(
            float(r001_control[1]["weighted_annualized_return"])
            - KNOWN_R001_STRESS_WEIGHTED_RETURN
        )
        < 1e-12
    )
    by_id = {scenario["scenario_id"]: scenario for scenario in scenarios}
    generic = assess_reverse_repo_sweep(
        baseline,
        by_id["close_base_commission"],
        by_id["low_double_commission"],
    )
    qualified = bool(generic["retrospective_qualified"]) and control_exact
    decision = {
        **generic,
        "decision": (
            "REQUIRE_BROKER_AUDIT_AND_FORWARD_VALIDATION" if qualified else "REJECT"
        ),
        "retrospective_qualified": qualified,
        "single_tenor_control_exact": control_exact,
        "independent_replication": False,
        "production_authorized": False,
    }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "baseline": baseline,
        "r001_control": r001_control,
        "scenarios": scenarios,
        "increment_over_r001": {
            "base_weighted_annualized_return": (
                float(scenarios[0]["weighted_annualized_return"])
                - KNOWN_R001_BASE_WEIGHTED_RETURN
            ),
            "stress_weighted_annualized_return": (
                float(scenarios[1]["weighted_annualized_return"])
                - KNOWN_R001_STRESS_WEIGHTED_RETURN
            ),
        },
        "decision": decision,
    }


def run_frozen_validation(
    database_path: Path,
    snapshot_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    actual_hash = _file_sha256(output_dir / "tenor_ladder_protocol.json")
    if actual_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen tenor-ladder protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={actual_hash}"
        )
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    rates = load_tenor_snapshot(snapshot_dir)
    result = evaluate_tenor_ladder(database_path, rates)
    payload = {
        "protocol_sha256": actual_hash,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_rates_sha256": manifest["rates_sha256"],
        **result,
    }
    (output_dir / "tenor_ladder_validation_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _evaluate_scenario(
    ledgers: Mapping[str, Mapping[str, Any]],
    rates: pd.DataFrame,
    windows: Sequence[EvaluationWindow],
    scenario: RepoScenario,
    tenors: Sequence[RepoTenor],
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for window in windows:
        source = ledgers[window.label]
        metrics = source["metrics"]
        report = simulate_tenor_ladder(
            source["equity"],
            rates,
            initial_cash=float(metrics["initial_cash"]),
            rate_field=scenario.rate_field,
            commission_rate=scenario.commission_rate,
            tenors=tenors,
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


def _empty_rates() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "code",
            "name",
            "tenor_days",
            "timestamp",
            "Open",
            "High",
            "Low",
            "Close",
            "Amount",
            "Volume",
        ]
    )
