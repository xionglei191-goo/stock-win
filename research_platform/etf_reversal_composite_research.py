from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .config import PlatformConfig
from .cross_market_repo_research import (
    load_cross_market_snapshot,
    simulate_cross_market_sweep,
)
from .equity_etf_reversal_research import (
    FROZEN_PROTOCOL_SHA256 as ORIGINAL_ETF_PROTOCOL_SHA256,
    ResearchWindow,
    build_cross_sectional_reversal_events,
    evaluate_development_window,
)
from .etf_trend_overlay_research import (
    _file_sha256,
    load_replication_snapshot,
    simulate_etf_sleeve,
    simulate_v9_overlay,
)
from .reverse_repo_sweep_research import SCENARIOS
from .storage import Database, ParquetSnapshotStore


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "v9_original_etf_reversal_cross_market_repo_composite"
FROZEN_PROTOCOL_SHA256 = "db6f2d87a48f0a6bbbd226e2bcf60e582fcaafa01b1f043a4a176018ac2a9794"
REPLICATION_START = "2024-07-01"
REPLICATION_END = "2025-07-24"
HOLDOUT_START = "2025-07-25"
HOLDOUT_END = "2026-08-07"
ETF_REPLICATION_SNAPSHOT_ID = (
    "853141603716a0b8c680c188781c9218b6b53cd4c42b85b8fdbe2692a173d1b5"
)
MARKET_SNAPSHOT_ID = "bt_1f2378fe2c984617911770ccb742a05e"
V9_BACKTEST_ID = "ed7e53baab44467d8a6c6ff12212ee0d"
CROSS_MARKET_SNAPSHOT_ID = (
    "4ff910f10ac54ce3203dc7414e6031351e806513d9d9401a9d4c2b679dab1a04"
)
KNOWN_REPO_CONTROL_BASE_ANNUALIZED = 0.31001819070183734
KNOWN_REPO_CONTROL_STRESS_ANNUALIZED = 0.3033946424031826
REPLICATION_WINDOW = ResearchWindow(
    "replication_2024_2025",
    "REPLICATION",
    REPLICATION_START,
    REPLICATION_END,
    0.25,
)


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "replication_then_sealed_holdout",
        "post_hoc_disclosure": {
            "known_v9_plus_repo_weighted_annualized_return": 0.39914777775215443,
            "known_target_shortfall_percentage_points": 0.085222224784557,
            "original_etf_development_results_seen": True,
            "different_60_session_exit_replication_seen": True,
            "original_10_session_exit_replication_seen_before_freeze": False,
            "holdout_prices_or_returns_seen_for_this_hypothesis": False,
            "interpretation": (
                "Replication is retrospective because the period's prices were used by a "
                "different exit study. Only the final holdout can provide time-separated "
                "evidence for the unchanged original exit."
            ),
        },
        "components": {
            "v9": {
                "backtest_id": V9_BACKTEST_ID,
                "contract": "all dates, codes, quantities, prices, and fees unchanged",
                "priority": 1,
            },
            "etf_reversal": {
                "protocol_sha256": ORIGINAL_ETF_PROTOCOL_SHA256,
                "entry_and_exit": "unchanged original 1.0.0 protocol",
                "target_weight": 0.10,
                "maximum_positions": 3,
                "priority": 2,
            },
            "repo": {
                "snapshot_id": CROSS_MARKET_SNAPSHOT_ID,
                "rule": "R-001 before 2022-05-16, then best eligible R-001/GC001 quote",
                "base": "Close and 0.001% principal commission",
                "stress": "Low and 0.002% principal commission",
                "priority": 3,
            },
        },
        "cash_order": [
            "all scheduled exits",
            "unchanged V9 buys",
            "original ETF reversal buys",
            "eligible reverse repo sweep",
        ],
        "repo_accounting": {
            "next_open_availability": True,
            "actual_occupied_calendar_days": True,
            "settled_interest_only": True,
            "interest_does_not_enable_additional_historical_equity_fills": True,
        },
        "data": {
            "etf_replication_snapshot_id": ETF_REPLICATION_SNAPSHOT_ID,
            "market_snapshot_id": MARKET_SNAPSHOT_ID,
            "cross_market_snapshot_id": CROSS_MARKET_SNAPSHOT_ID,
            "replication_window": [REPLICATION_START, REPLICATION_END],
            "holdout_window": [HOLDOUT_START, HOLDOUT_END],
            "holdout_remains_sealed_until_replication_passes": True,
            "known_population_bias": "current-survivor ETF roster",
        },
        "replication_gate": {
            "minimum_portfolio_trades": 15,
            "positive_standalone_total_return": True,
            "positive_trade_median": True,
            "positive_ex_top3_contribution": True,
            "minimum_fill_rate": 0.60,
            "maximum_standalone_drawdown": -0.10,
            "minimum_base_composite_incremental_total_return": 0.002,
            "positive_stress_composite_increment": True,
            "maximum_daily_return_correlation_with_v9": 0.60,
            "maximum_composite_drawdown": -0.10,
            "exact_v9_reproduction": True,
            "exact_repo_control_reproduction": True,
            "zero_v9_cash_blocks": True,
            "all_checks_required": True,
        },
        "holdout_gate": {
            "five_window_weighted_annualized_return": 0.40,
            "positive_holdout_etf_total_and_median": True,
            "positive_holdout_ex_top3_contribution": True,
            "positive_base_and_stress_increment": True,
            "maximum_composite_drawdown": -0.10,
            "maximum_daily_return_correlation_with_v9": 0.60,
            "exact_v9_and_repo_controls": True,
            "passing_is_not_production_authorization": True,
        },
        "opening_rule": (
            "Run replication first. Open the final holdout exactly once only if every "
            "replication gate passes. Never tune on replication or holdout results."
        ),
        "invariants": {
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
        raise ValueError(f"Frozen composite protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def build_replication_events(
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    *,
    config: PlatformConfig,
    cost_multiplier: float,
) -> pd.DataFrame:
    events = build_cross_sectional_reversal_events(
        bars,
        market_index,
        execution_config=config.portfolio,
        execution_cost_multiplier=cost_multiplier,
    )
    if events.empty:
        return events
    signal_dates = pd.to_datetime(events["signal_date"]).dt.normalize()
    return events.loc[
        signal_dates.between(REPLICATION_START, REPLICATION_END)
    ].reset_index(drop=True)


def assess_replication(
    base_report: Mapping[str, Any],
    stress_report: Mapping[str, Any],
    base_bundle: Mapping[str, Any],
    stress_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    checks = {
        "minimum_trades": int(base_report["portfolio_trades"]) >= 15,
        "positive_standalone_total_return": float(
            base_report["portfolio_total_return"]
        )
        > 0.0,
        "positive_trade_median": (
            base_report["median_trade_return"] is not None
            and float(base_report["median_trade_return"]) > 0.0
        ),
        "positive_ex_top3_contribution": float(
            base_report["ex_top3_contribution"]
        )
        > 0.0,
        "minimum_fill_rate": float(base_report["fill_rate"]) >= 0.60,
        "maximum_standalone_drawdown": float(
            base_report["portfolio_max_drawdown"]
        )
        >= -0.10,
        "minimum_base_composite_increment": float(
            base_bundle["incremental_total_return"]
        )
        >= 0.002,
        "positive_stress_standalone_total_return": float(
            stress_report["portfolio_total_return"]
        )
        > 0.0,
        "positive_stress_composite_increment": float(
            stress_bundle["incremental_total_return"]
        )
        > 0.0,
        "maximum_daily_return_correlation": (
            base_bundle["daily_return_correlation"] is not None
            and float(base_bundle["daily_return_correlation"]) <= 0.60
        ),
        "maximum_base_composite_drawdown": float(base_bundle["max_drawdown"])
        >= -0.10,
        "maximum_stress_composite_drawdown": float(stress_bundle["max_drawdown"])
        >= -0.10,
        "exact_v9_reproduction": bool(base_bundle["v9_reproduction_match"]),
        "exact_base_repo_control": bool(base_bundle["repo_control_match"]),
        "exact_stress_repo_control": bool(stress_bundle["repo_control_match"]),
        "zero_v9_cash_blocks": int(base_bundle["v9_cash_blocked"]) == 0
        and int(stress_bundle["v9_cash_blocked"]) == 0,
    }
    passed = all(checks.values())
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "decision": "OPEN_FINAL_HOLDOUT" if passed else "REJECT",
        "replication_qualified": passed,
        "checks": checks,
        "holdout_opened": False,
        "production_authorized": False,
    }


def run_frozen_replication(
    config: PlatformConfig,
    database: Database,
    *,
    snapshot_dir: Path,
    repo_snapshot_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    protocol_hash = _file_sha256(output_dir / "protocol.json")
    if protocol_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen composite protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={protocol_hash}"
        )
    bars = load_replication_snapshot(snapshot_dir)
    snapshot_manifest = json.loads(
        (Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    repo_manifest = json.loads(
        (Path(repo_snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    if snapshot_manifest["snapshot_id"] != ETF_REPLICATION_SNAPSHOT_ID:
        raise ValueError("Unexpected ETF replication snapshot")
    if repo_manifest["snapshot_id"] != CROSS_MARKET_SNAPSHOT_ID:
        raise ValueError("Unexpected cross-market snapshot")
    store = ParquetSnapshotStore(config, database)
    market = store.load_records(MARKET_SNAPSHOT_ID, "market_index")
    base_events = build_replication_events(
        bars, market, config=config, cost_multiplier=1.0
    )
    stress_events = build_replication_events(
        bars, market, config=config, cost_multiplier=2.0
    )
    base_report = evaluate_development_window(
        base_events,
        bars,
        market,
        REPLICATION_WINDOW,
        execution_config=config.portfolio,
        execution_cost_multiplier=1.0,
    )
    stress_report = evaluate_development_window(
        stress_events,
        bars,
        market,
        REPLICATION_WINDOW,
        execution_config=config.portfolio,
        execution_cost_multiplier=2.0,
    )
    v9_trades, v9_bars, v9_metrics = _load_v9_inputs(
        database, store, V9_BACKTEST_ID, MARKET_SNAPSHOT_ID
    )
    rates = load_cross_market_snapshot(repo_snapshot_dir)
    calendar = _calendar(market, REPLICATION_START, REPLICATION_END)
    base_bundle = _simulate_bundle(
        v9_trades,
        v9_bars,
        v9_metrics,
        base_events,
        bars,
        rates,
        calendar,
        config=config,
        equity_cost_multiplier=1.0,
        repo_rate_field=SCENARIOS[0].rate_field,
        repo_commission_rate=SCENARIOS[0].commission_rate,
        known_repo_control=KNOWN_REPO_CONTROL_BASE_ANNUALIZED,
    )
    stress_bundle = _simulate_bundle(
        v9_trades,
        v9_bars,
        v9_metrics,
        stress_events,
        bars,
        rates,
        calendar,
        config=config,
        equity_cost_multiplier=2.0,
        repo_rate_field=SCENARIOS[1].rate_field,
        repo_commission_rate=SCENARIOS[1].commission_rate,
        known_repo_control=KNOWN_REPO_CONTROL_STRESS_ANNUALIZED,
    )
    decision = assess_replication(
        base_report, stress_report, base_bundle, stress_bundle
    )
    events_path = output_dir / "replication_events.parquet"
    base_events.to_parquet(events_path, index=False)
    payload = {
        "protocol_sha256": protocol_hash,
        "etf_snapshot_id": snapshot_manifest["snapshot_id"],
        "etf_snapshot_bars_sha256": snapshot_manifest["bars_sha256"],
        "repo_snapshot_id": repo_manifest["snapshot_id"],
        "repo_snapshot_rates_sha256": repo_manifest["rates_sha256"],
        "base_report": base_report,
        "stress_report": stress_report,
        "base_bundle": base_bundle,
        "stress_bundle": stress_bundle,
        "decision": decision,
        "events_sha256": _file_sha256(events_path),
    }
    (output_dir / "replication_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return payload


def _load_v9_inputs(
    database: Database,
    store: ParquetSnapshotStore,
    backtest_id: str,
    snapshot_id: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    trades = pd.DataFrame(
        database.query(
            "SELECT rowid,* FROM backtest_trades WHERE backtest_id=? "
            "AND strategy_id='course49_v9' ORDER BY timestamp,rowid",
            (backtest_id,),
        )
    )
    metrics_rows = database.query(
        "SELECT metrics_json FROM backtests WHERE backtest_id=?", (backtest_id,)
    )
    if not metrics_rows:
        raise ValueError(f"Missing V9 backtest: {backtest_id}")
    metrics = json.loads(str(metrics_rows[0]["metrics_json"] or "{}"))
    raw = store.load_records(snapshot_id, "daily_raw")
    traded_codes = set(trades["code"].astype(str))
    raw = raw.loc[raw["code"].astype(str).isin(traded_codes)].copy()
    return trades, raw, metrics


def _simulate_bundle(
    v9_trades: pd.DataFrame,
    v9_bars: pd.DataFrame,
    v9_metrics: Mapping[str, Any],
    events: pd.DataFrame,
    etf_bars: pd.DataFrame,
    rates: pd.DataFrame,
    calendar: list[pd.Timestamp],
    *,
    config: PlatformConfig,
    equity_cost_multiplier: float,
    repo_rate_field: str,
    repo_commission_rate: float,
    known_repo_control: float,
) -> dict[str, Any]:
    end = pd.Timestamp(REPLICATION_END)
    complete = events.loc[
        events["selected"]
        & events["executable"]
        & pd.to_datetime(events["exit_date"]).le(end)
    ].copy()
    baseline = simulate_v9_overlay(
        v9_trades,
        v9_bars,
        complete.iloc[0:0],
        etf_bars,
        calendar,
        initial_cash=float(v9_metrics["initial_cash"]),
        config=config.portfolio,
        maximum_etf_positions=3,
    )
    combined = simulate_v9_overlay(
        v9_trades,
        v9_bars,
        complete,
        etf_bars,
        calendar,
        initial_cash=float(v9_metrics["initial_cash"]),
        config=config.portfolio,
        cost_multiplier=equity_cost_multiplier,
        maximum_etf_positions=3,
    )
    sleeve = simulate_etf_sleeve(
        complete,
        etf_bars,
        calendar,
        initial_cash=float(v9_metrics["initial_cash"]),
        config=config.portfolio,
        cost_multiplier=equity_cost_multiplier,
        maximum_positions=3,
    )
    baseline_equity = pd.DataFrame(baseline.pop("equity"))
    combined_equity = pd.DataFrame(combined.pop("equity"))
    sleeve_equity = pd.DataFrame(sleeve.pop("equity"))
    control_repo = simulate_cross_market_sweep(
        baseline_equity,
        rates,
        initial_cash=float(v9_metrics["initial_cash"]),
        rate_field=repo_rate_field,
        commission_rate=repo_commission_rate,
    )
    combined_repo = simulate_cross_market_sweep(
        combined_equity,
        rates,
        initial_cash=float(v9_metrics["initial_cash"]),
        rate_field=repo_rate_field,
        commission_rate=repo_commission_rate,
    )
    baseline_returns = baseline_equity.set_index("timestamp")["equity"].pct_change()
    sleeve_returns = sleeve_equity.set_index("timestamp")["equity"].pct_change()
    correlation = baseline_returns.corr(sleeve_returns)
    initial_cash = float(v9_metrics["initial_cash"])
    equity_increment = float(
        combined["portfolio_total_return"] - baseline["portfolio_total_return"]
    )
    repo_interest_increment = float(
        (combined_repo["net_interest"] - control_repo["net_interest"]) / initial_cash
    )
    total_increment = float(
        combined_repo["total_return"] - control_repo["total_return"]
    )
    decomposition_residual = float(
        total_increment - equity_increment - repo_interest_increment
    )
    if abs(decomposition_residual) > 1e-10:
        raise AssertionError("Composite incremental return decomposition failed")
    return {
        "v9_reproduction_match": abs(
            float(baseline["portfolio_total_return"])
            - float(v9_metrics["total_return"])
        )
        < 1e-10,
        "repo_control_match": abs(
            float(control_repo["annualized_return"]) - known_repo_control
        )
        < 1e-12,
        "repo_control_annualized_return": float(control_repo["annualized_return"]),
        "repo_control_total_return": float(control_repo["total_return"]),
        "repo_control_net_interest": float(control_repo["net_interest"]),
        "annualized_return": float(combined_repo["annualized_return"]),
        "total_return": float(combined_repo["total_return"]),
        "incremental_total_return": total_increment,
        "equity_increment_before_repo": equity_increment,
        "repo_interest_increment": repo_interest_increment,
        "increment_decomposition_residual": decomposition_residual,
        "max_drawdown": float(combined_repo["max_drawdown"]),
        "net_repo_interest": float(combined_repo["net_interest"]),
        "daily_return_correlation": (
            float(correlation) if np.isfinite(correlation) else None
        ),
        "v9_cash_blocked": int(combined["v9_cash_blocked"]),
        "v9_trade_rows_processed": int(combined["v9_trade_rows_processed"]),
        "etf_trades": int(combined["etf_trades"]),
        "etf_trade_returns": [float(value) for value in combined["etf_trade_returns"]],
    }


def _calendar(
    market: pd.DataFrame, start_date: str, end_date: str
) -> list[pd.Timestamp]:
    values = pd.to_datetime(market["timestamp"], errors="coerce").dropna().dt.normalize()
    return sorted(values.loc[values.between(start_date, end_date)].unique())
