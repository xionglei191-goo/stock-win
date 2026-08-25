from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from .cash_sweep_research import EvaluationWindow, WINDOWS
from .cross_market_repo_research import GC001_ELIGIBLE_FROM, _slice_day_payload
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
HYPOTHESIS_ID = "v9_cross_market_calendar_matched_repo_tenor_ladder"
FROZEN_PROTOCOL_SHA256 = "5005a32ce8d84377e6e4177f454c314512c39bd68eda1971eb5a7e3caeb84065"
TARGET_WEIGHTED_ANNUALIZED_RETURN = 0.40
KNOWN_ONE_DAY_BASE_WEIGHTED_RETURN = 0.39914777775215443
KNOWN_ONE_DAY_STRESS_WEIGHTED_RETURN = 0.3936532859739442


@dataclass(frozen=True)
class CrossMarketTenor:
    code: str
    name: str
    market: str
    local_code: str
    tenor_days: int
    eligible_from: str


INSTRUMENTS = (
    CrossMarketTenor("131810.SZ", "R-001", "sz", "sz131810", 1, SNAPSHOT_START),
    CrossMarketTenor("131811.SZ", "R-002", "sz", "sz131811", 2, SNAPSHOT_START),
    CrossMarketTenor("131800.SZ", "R-003", "sz", "sz131800", 3, SNAPSHOT_START),
    CrossMarketTenor("131809.SZ", "R-004", "sz", "sz131809", 4, SNAPSHOT_START),
    CrossMarketTenor("131801.SZ", "R-007", "sz", "sz131801", 7, SNAPSHOT_START),
    CrossMarketTenor("204001.SH", "GC001", "sh", "sh204001", 1, GC001_ELIGIBLE_FROM),
    CrossMarketTenor("204002.SH", "GC002", "sh", "sh204002", 2, GC001_ELIGIBLE_FROM),
    CrossMarketTenor("204003.SH", "GC003", "sh", "sh204003", 3, GC001_ELIGIBLE_FROM),
    CrossMarketTenor("204004.SH", "GC004", "sh", "sh204004", 4, GC001_ELIGIBLE_FROM),
    CrossMarketTenor("204007.SH", "GC007", "sh", "sh204007", 7, GC001_ELIGIBLE_FROM),
)
SIMULATION_TENORS = tuple(
    RepoTenor(item.code, item.name, item.local_code, item.tenor_days)
    for item in INSTRUMENTS
)
ONE_DAY_TENORS = tuple(item for item in SIMULATION_TENORS if item.tenor_days == 1)


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "post_result_retrospective_extension",
        "post_hoc_disclosure": {
            "known_cross_market_one_day_base_weighted_return": (
                KNOWN_ONE_DAY_BASE_WEIGHTED_RETURN
            ),
            "known_cross_market_one_day_stress_weighted_return": (
                KNOWN_ONE_DAY_STRESS_WEIGHTED_RETURN
            ),
            "known_target_shortfall_percentage_points": 0.085222224784557,
            "longer_shanghai_tenor_history_seen_before_freeze": False,
            "interpretation": (
                "The one-day shortfall was known. Longer Shanghai tenor relative rates "
                "were not opened before this protocol; any pass remains retrospective."
            ),
        },
        "instruments": [asdict(item) for item in INSTRUMENTS],
        "historical_eligibility": {
            "before_2022_05_16": "all Shanghai tenors disabled",
            "from_2022_05_16": "Shanghai and Shenzhen matching orders use frozen rules",
            "no_backfill": True,
        },
        "selection": {
            "decision_time": "near close after all current-session V9 equity fills",
            "eligible_tenor": (
                "nominal calendar-day tenor no greater than the known gap to the next "
                "equity trading session"
            ),
            "liquidity": "positive DAY volume",
            "base_ranking": "highest eligible daily Close rate",
            "stress_ranking": "highest eligible daily Low rate",
            "tie_break": "shorter tenor then code ascending",
            "negative_net_quote": "skip without carry",
        },
        "settlement": {
            "principal_available_every_next_open": True,
            "actual_occupied_days": "next session inclusive to following session exclusive",
            "interest_credit": "following equity session",
            "terminal_treatment": "last two sessions recognize no unsettled interest",
        },
        "snapshot": {
            "source": "ten local TDX unadjusted DAY binary prefixes",
            "start_date": SNAPSHOT_START,
            "end_date": SNAPSHOT_END,
            "records_sliced_to_research_range_before_ohlc_validation": True,
            "full_cutoff_prefix_hashes_retained": True,
            "all_ten_rows_required_per eligible ledger session": True,
        },
        "scenarios": [asdict(item) for item in SCENARIOS],
        "evaluation": {
            "target_weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
            "cash_sensitivity_required_net_annual_yield": REQUIRED_NET_ANNUAL_YIELD,
            "weights": [asdict(item) for item in WINDOWS],
            "one_day_cross_market_control_must_reproduce": True,
            "maximum_window_drawdown": -0.10,
            "maximum_drawdown_degradation": 0.002,
        },
        "decision_rule": {
            "base_required": [
                "weighted annualized return at least 40%",
                "positive incremental return in all windows",
                "all drawdowns no worse than -10%",
                "drawdown degradation no more than 0.2 percentage points",
                "complete rate coverage and exact controls",
            ],
            "stress_required": [
                "positive net interest in all windows",
                "weighted return above unchanged V9",
            ],
            "passing_decision": "REQUIRE_BROKER_AUDIT_AND_FORWARD_VALIDATION",
            "passing_is_not_production_authorization": True,
        },
        "official_sources": [
            {
                "authority": "Shanghai Stock Exchange",
                "url": "https://bond.sse.com.cn/lawrule/sserules/trading/c/5702698.shtml",
                "frozen_fact": "new bond trading rules took effect on 2022-05-16",
            },
            {
                "authority": "Shanghai Stock Exchange",
                "url": (
                    "https://www.sse.com.cn/lawandrules/sselawsrules2025/bond/"
                    "trading/currency/c/c_20250606_10781048.shtml"
                ),
                "frozen_fact": "GC repo codes and 1,000 CNY matching order unit",
            },
        ],
        "invariants": {
            "v9_equity_trades_unchanged": True,
            "no_equity_cash_block": True,
            "no_scan_or_push_registration": True,
            "no_paper_or_real_orders": True,
            "no_production_promotion": True,
            "no_parameter_scan_after_result": True,
        },
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen cross-market tenor protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def create_snapshot(
    *,
    tdx_root: Path,
    output_root: Path,
    instruments: Sequence[CrossMarketTenor] = INSTRUMENTS,
    start_date: str = SNAPSHOT_START,
    end_date: str = SNAPSHOT_END,
) -> dict[str, Any]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Snapshot cannot extend beyond the frozen end")
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
        payload = _slice_day_payload(prefix, start, end)
        tenor = RepoTenor(
            instrument.code,
            instrument.name,
            instrument.local_code,
            instrument.tenor_days,
        )
        frame = decode_tenor_day_bytes(payload, tenor)
        if frame.empty:
            raise ValueError(f"Snapshot is empty for {instrument.code}")
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
        raise ValueError("Snapshot contains duplicate session-code keys")
    expected = {item.code for item in instruments}
    if set(rates["code"].unique()) != expected:
        raise ValueError("Snapshot instrument set mismatch")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cross_tenor_", dir=str(output_root)))
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
                raise ValueError(f"Immutable snapshot collision: {snapshot_id}")
            return existing
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_snapshot(snapshot_dir: Path) -> pd.DataFrame:
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    path = snapshot_dir / "rates.parquet"
    actual_hash = _file_sha256(path)
    if actual_hash != manifest.get("rates_sha256"):
        raise ValueError(
            "Snapshot hash mismatch: "
            f"expected={manifest.get('rates_sha256')}, actual={actual_hash}"
        )
    rates = pd.read_parquet(path)
    rates["timestamp"] = _normalize_timestamp(rates["timestamp"])
    if rates["timestamp"].max() > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Snapshot contains post-protocol data")
    return rates


def simulate_cross_market_tenor_ladder(
    equity: pd.DataFrame,
    rates: pd.DataFrame,
    *,
    initial_cash: float,
    rate_field: str,
    commission_rate: float,
    tenors: Sequence[RepoTenor] = SIMULATION_TENORS,
) -> dict[str, Any]:
    eligible = rates.copy()
    eligible["timestamp"] = _normalize_timestamp(eligible["timestamp"])
    eligibility = {item.code: pd.Timestamp(item.eligible_from) for item in INSTRUMENTS}
    for code in {item.code for item in tenors}:
        before = eligible["code"].eq(code) & eligible["timestamp"].lt(eligibility[code])
        eligible.loc[before, "Volume"] = 0.0
    result = simulate_tenor_ladder(
        equity,
        eligible,
        initial_cash=initial_cash,
        rate_field=rate_field,
        commission_rate=commission_rate,
        tenors=tenors,
    )
    frame = result["equity"]
    invalid = frame["repo_code"].str.endswith(".SH") & frame["timestamp"].lt(
        pd.Timestamp(GC001_ELIGIBLE_FROM)
    )
    if invalid.any():
        raise AssertionError("Shanghai repo selected before eligibility")
    return result


def evaluate(
    database_path: Path,
    rates: pd.DataFrame,
    *,
    windows: Sequence[EvaluationWindow] = WINDOWS,
) -> dict[str, Any]:
    ledgers = _load_ledgers(database_path, windows)
    baseline = _baseline_report(ledgers, windows)
    scenarios = [
        _evaluate_scenario(ledgers, rates, windows, item, SIMULATION_TENORS)
        for item in SCENARIOS
    ]
    controls = [
        _evaluate_scenario(ledgers, rates, windows, item, ONE_DAY_TENORS)
        for item in SCENARIOS
    ]
    control_exact = (
        abs(
            float(controls[0]["weighted_annualized_return"])
            - KNOWN_ONE_DAY_BASE_WEIGHTED_RETURN
        )
        < 1e-12
        and abs(
            float(controls[1]["weighted_annualized_return"])
            - KNOWN_ONE_DAY_STRESS_WEIGHTED_RETURN
        )
        < 1e-12
    )
    generic = assess_reverse_repo_sweep(baseline, scenarios[0], scenarios[1])
    qualified = bool(generic["retrospective_qualified"]) and control_exact
    decision = {
        **generic,
        "decision": (
            "REQUIRE_BROKER_AUDIT_AND_FORWARD_VALIDATION" if qualified else "REJECT"
        ),
        "retrospective_qualified": qualified,
        "one_day_control_exact": control_exact,
        "production_authorized": False,
    }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "baseline": baseline,
        "one_day_control": controls,
        "scenarios": scenarios,
        "increment_over_one_day": {
            "base_weighted_annualized_return": float(
                scenarios[0]["weighted_annualized_return"]
                - KNOWN_ONE_DAY_BASE_WEIGHTED_RETURN
            ),
            "stress_weighted_annualized_return": float(
                scenarios[1]["weighted_annualized_return"]
                - KNOWN_ONE_DAY_STRESS_WEIGHTED_RETURN
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
    protocol_hash = _file_sha256(output_dir / "protocol.json")
    if protocol_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen cross-market tenor protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={protocol_hash}"
        )
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    rates = load_snapshot(snapshot_dir)
    result = evaluate(database_path, rates)
    payload = {
        "protocol_sha256": protocol_hash,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_rates_sha256": manifest["rates_sha256"],
        **result,
    }
    (output_dir / "validation_result.json").write_text(
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
        report = simulate_cross_market_tenor_ladder(
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
                "baseline_annualized_return": float(metrics["annualized_return"]),
                "baseline_total_return": float(metrics["total_return"]),
                "baseline_max_drawdown": float(metrics["max_drawdown"]),
                **report,
                "incremental_total_return": float(
                    report["total_return"] - float(metrics["total_return"])
                ),
                "drawdown_degradation": float(
                    report["max_drawdown"] - float(metrics["max_drawdown"])
                ),
            }
        )
    return {
        "scenario_id": scenario.scenario_id,
        "rate_field": scenario.rate_field,
        "commission_rate": scenario.commission_rate,
        "weighted_annualized_return": float(
            sum(item["weight"] * item["annualized_return"] for item in reports)
        ),
        "windows": reports,
    }
