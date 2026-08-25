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

from .cash_sweep_research import EvaluationWindow, WINDOWS
from .etf_pullback_research import DAY_DTYPE
from .reverse_repo_sweep_research import (
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
from .reverse_repo_tenor_ladder_research import (
    RepoTenor,
    decode_tenor_day_bytes,
    simulate_tenor_ladder,
)


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "v9_cross_market_one_day_repo_best_quote"
FROZEN_PROTOCOL_SHA256 = "c082dfcf2945068de4645255537963a55996e69711c4bdfb5161b84643cb0230"
GC001_ELIGIBLE_FROM = "2022-05-16"
TARGET_WEIGHTED_ANNUALIZED_RETURN = 0.40
KNOWN_R001_BASE_WEIGHTED_RETURN = 0.39888869959508727
KNOWN_R001_STRESS_WEIGHTED_RETURN = 0.3926664456695309


@dataclass(frozen=True)
class CrossMarketRepo:
    code: str
    name: str
    market: str
    local_code: str
    eligible_from: str


INSTRUMENTS = (
    CrossMarketRepo("131810.SZ", "R-001", "sz", "sz131810", SNAPSHOT_START),
    CrossMarketRepo("204001.SH", "GC001", "sh", "sh204001", GC001_ELIGIBLE_FROM),
)
SIMULATION_TENORS = tuple(
    RepoTenor(item.code, item.name, item.local_code, 1) for item in INSTRUMENTS
)


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "post_result_retrospective_extension",
        "post_hoc_disclosure": {
            "known_r001_base_weighted_annualized_return": (
                KNOWN_R001_BASE_WEIGHTED_RETURN
            ),
            "known_r001_stress_weighted_annualized_return": (
                KNOWN_R001_STRESS_WEIGHTED_RETURN
            ),
            "known_target_shortfall_percentage_points": 0.111130040491273,
            "gc001_historical_rate_comparison_seen_before_freeze": False,
            "interpretation": (
                "The R-001 shortfall was known, but GC001 historical relative returns were "
                "not opened before this protocol. Any pass remains retrospective, not an "
                "independent holdout."
            ),
        },
        "instruments": [asdict(item) for item in INSTRUMENTS],
        "historical_eligibility": {
            "before_2022_05_16": (
                "R-001 only; the prior Shanghai matching minimum was 100,000 CNY and is "
                "incompatible with the frozen 50,000 CNY V9 ledger"
            ),
            "from_2022_05_16": (
                "R-001 and GC001; Shanghai matching orders are 1,000 CNY or multiples"
            ),
            "no_backfill": True,
        },
        "selection": {
            "decision_time": (
                "near the close after current-session V9 fills, comparing live executable "
                "one-day repo quotes in the unified brokerage cash account"
            ),
            "ranking": "highest annualized rate; ties use code ascending",
            "positive_day_volume_required": True,
            "daily_proxy_base": "highest eligible TDX daily Close rate",
            "daily_proxy_stress": (
                "highest eligible daily Low rate with doubled commission"
            ),
            "negative_net_quote": "skip when gross interest does not exceed commission",
        },
        "settlement": {
            "tenor": "one day only in both markets",
            "principal_lot_cny": 1_000,
            "first_settlement": "next equity trading session i+1",
            "principal_availability": "assumed available before every i+1 equity open",
            "maturity_settlement": "following equity trading session i+2",
            "actual_occupied_days": "calendar days from i+1 inclusive to i+2 exclusive",
            "interest_formula": (
                "principal * annualized rate / 100 * actual occupied days / 365 - commission"
            ),
            "terminal_treatment": "last two sessions recognize no unsettled interest",
        },
        "invariants": {
            "v9_trade_dates_unchanged": True,
            "v9_codes_quantities_prices_fees_unchanged": True,
            "v9_signal_logic_unchanged": True,
            "equity_orders_have_priority": True,
            "no_cross_market_selection_before_gc001_eligibility": True,
            "no_new_equity_positions": True,
            "no_production_registration": True,
        },
        "snapshot": {
            "source": "local TDX unadjusted DAY binary prefixes",
            "start_date": SNAPSHOT_START,
            "end_date": SNAPSHOT_END,
            "future_records_excluded_while_reading": True,
            "both_rate_rows_required_for_every_ledger_session": True,
            "immutable_content_addressed_parquet": True,
        },
        "scenarios": [asdict(scenario) for scenario in SCENARIOS],
        "evaluation": {
            "target_weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
            "cash_sensitivity_required_net_annual_yield": REQUIRED_NET_ANNUAL_YIELD,
            "maximum_window_drawdown": -0.10,
            "maximum_drawdown_degradation": 0.002,
            "weights": [asdict(window) for window in WINDOWS],
            "r001_control_must_reproduce_predecessor": True,
        },
        "decision_rule": {
            "base_required": [
                "weighted annualized return at least 40%",
                "positive incremental return in all five windows",
                "all window drawdowns no worse than -10%",
                "drawdown degradation no more than 0.2 percentage points",
                "100% rate coverage",
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
                "authority": "Shanghai Stock Exchange",
                "url": "https://bond.sse.com.cn/lawrule/sserules/trading/c/5702698.shtml",
                "frozen_fact": "The new bond rules actually took effect on 2022-05-16.",
            },
            {
                "authority": "Shanghai Stock Exchange",
                "url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/bond/trading/currency/c/c_20250606_10781048.shtml",
                "frozen_fact": (
                    "GC repo codes are 204*** and matching orders are 1,000 CNY face value "
                    "or integer multiples."
                ),
            },
        ],
        "known_limitations": [
            "Daily Close is a benchmark and daily Low is a bound, not synchronized quote replay.",
            "Cross-exchange cash fungibility and principal availability require broker confirmation.",
            "Broker-specific commissions remain assumed rather than account-verified.",
            "The extension follows an observed R-001 shortfall and has no untouched historical holdout.",
            "This is cash efficiency, not new low-buy alpha.",
        ],
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "cross_market_protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen cross-market protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def _slice_day_payload(data: bytes, start: pd.Timestamp, end: pd.Timestamp) -> bytes:
    if len(data) % DAY_DTYPE.itemsize:
        raise ValueError("DAY payload size must be a multiple of 32 bytes")
    records = np.frombuffer(data, dtype=DAY_DTYPE)
    start_value = int(start.strftime("%Y%m%d"))
    end_value = int(end.strftime("%Y%m%d"))
    return records[
        (records["date"] >= start_value) & (records["date"] <= end_value)
    ].tobytes()


def create_cross_market_snapshot(
    *,
    tdx_root: Path,
    output_root: Path,
    instruments: Sequence[CrossMarketRepo] = INSTRUMENTS,
    start_date: str = SNAPSHOT_START,
    end_date: str = SNAPSHOT_END,
) -> dict[str, Any]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Cross-market snapshot cannot extend beyond the frozen end")
    frames: list[pd.DataFrame] = []
    sources: list[dict[str, Any]] = []
    for instrument in instruments:
        path = (
            Path(tdx_root)
            / "vipdoc"
            / instrument.market
            / "lday"
            / f"{instrument.local_code}.day"
        )
        prefix, source = _read_day_prefix(path, end)
        tenor = RepoTenor(
            instrument.code, instrument.name, instrument.local_code, 1
        )
        research_payload = _slice_day_payload(prefix, start, end)
        frame = decode_tenor_day_bytes(research_payload, tenor)
        if frame.empty:
            raise ValueError(f"Cross-market snapshot is empty for {instrument.code}")
        frames.append(frame)
        sources.append(
            {
                "code": instrument.code,
                "market": instrument.market,
                "path": source["path"],
                "prefix_bytes": source["prefix_bytes"],
                "prefix_sha256": source["prefix_sha256"],
                "rows": int(len(frame)),
            }
        )
    rates = pd.concat(frames, ignore_index=True).sort_values(
        ["timestamp", "code"]
    ).reset_index(drop=True)
    if rates.duplicated(["timestamp", "code"]).any():
        raise ValueError("Cross-market snapshot contains duplicate keys")
    expected = {item.code for item in instruments}
    if set(rates["code"].unique()) != expected:
        raise ValueError("Cross-market snapshot instrument set mismatch")

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".repo_cross_market_", dir=str(output_root)))
    try:
        rates_path = staging / "rates.parquet"
        rates.to_parquet(rates_path, index=False)
        rates_hash = _file_sha256(rates_path)
        identity = {
            "protocol_version": PROTOCOL_VERSION,
            "start_date": str(start.date()),
            "end_date": str(end.date()),
            "instruments": [asdict(item) for item in instruments],
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
                raise ValueError(f"Immutable cross-market snapshot collision: {snapshot_id}")
            return existing
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_cross_market_snapshot(snapshot_dir: Path) -> pd.DataFrame:
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    path = snapshot_dir / "rates.parquet"
    actual_hash = _file_sha256(path)
    if actual_hash != manifest.get("rates_sha256"):
        raise ValueError(
            "Cross-market snapshot hash mismatch: "
            f"expected={manifest.get('rates_sha256')}, actual={actual_hash}"
        )
    rates = pd.read_parquet(path)
    rates["timestamp"] = _normalize_timestamp(rates["timestamp"])
    if rates["timestamp"].max() > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Cross-market snapshot contains post-protocol data")
    return rates


def simulate_cross_market_sweep(
    equity: pd.DataFrame,
    rates: pd.DataFrame,
    *,
    initial_cash: float,
    rate_field: str,
    commission_rate: float,
) -> dict[str, Any]:
    eligible_rates = rates.copy()
    eligible_rates["timestamp"] = _normalize_timestamp(eligible_rates["timestamp"])
    before_eligibility = (
        eligible_rates["code"].eq("204001.SH")
        & eligible_rates["timestamp"].lt(pd.Timestamp(GC001_ELIGIBLE_FROM))
    )
    eligible_rates.loc[before_eligibility, "Volume"] = 0.0
    result = simulate_tenor_ladder(
        equity,
        eligible_rates,
        initial_cash=initial_cash,
        rate_field=rate_field,
        commission_rate=commission_rate,
        tenors=SIMULATION_TENORS,
    )
    frame = result["equity"]
    invalid_early = frame["repo_code"].eq("204001.SH") & frame["timestamp"].lt(
        pd.Timestamp(GC001_ELIGIBLE_FROM)
    )
    if invalid_early.any():
        raise AssertionError("GC001 was selected before historical eligibility")
    result["gc001_eligible_from"] = GC001_ELIGIBLE_FROM
    result["gc001_selections"] = int(result["selection_counts"].get("204001.SH", 0))
    return result


def evaluate_cross_market_sweep(
    database_path: Path,
    rates: pd.DataFrame,
    *,
    windows: Sequence[EvaluationWindow] = WINDOWS,
) -> dict[str, Any]:
    ledgers = _load_ledgers(database_path, windows)
    baseline = _baseline_report(ledgers, windows)
    scenarios = [
        _evaluate_scenario(ledgers, rates, windows, scenario, cross_market=True)
        for scenario in SCENARIOS
    ]
    controls = [
        _evaluate_scenario(ledgers, rates, windows, scenario, cross_market=False)
        for scenario in SCENARIOS
    ]
    control_exact = (
        abs(
            float(controls[0]["weighted_annualized_return"])
            - KNOWN_R001_BASE_WEIGHTED_RETURN
        )
        < 1e-12
        and abs(
            float(controls[1]["weighted_annualized_return"])
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
        "r001_control_exact": control_exact,
        "independent_replication": False,
        "production_authorized": False,
    }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "baseline": baseline,
        "r001_control": controls,
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
    actual_hash = _file_sha256(output_dir / "cross_market_protocol.json")
    if actual_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen cross-market protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={actual_hash}"
        )
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    rates = load_cross_market_snapshot(snapshot_dir)
    result = evaluate_cross_market_sweep(database_path, rates)
    payload = {
        "protocol_sha256": actual_hash,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_rates_sha256": manifest["rates_sha256"],
        **result,
    }
    (output_dir / "cross_market_validation_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _evaluate_scenario(
    ledgers: Mapping[str, Mapping[str, Any]],
    rates: pd.DataFrame,
    windows: Sequence[EvaluationWindow],
    scenario: RepoScenario,
    *,
    cross_market: bool,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    r001_tenor = SIMULATION_TENORS[:1]
    for window in windows:
        source = ledgers[window.label]
        metrics = source["metrics"]
        if cross_market:
            report = simulate_cross_market_sweep(
                source["equity"],
                rates,
                initial_cash=float(metrics["initial_cash"]),
                rate_field=scenario.rate_field,
                commission_rate=scenario.commission_rate,
            )
        else:
            report = simulate_tenor_ladder(
                source["equity"],
                rates,
                initial_cash=float(metrics["initial_cash"]),
                rate_field=scenario.rate_field,
                commission_rate=scenario.commission_rate,
                tenors=r001_tenor,
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
