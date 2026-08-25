from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .cash_sweep_research import EvaluationWindow, WINDOWS
from .config import PlatformConfig
from .cross_market_repo_research import (
    load_cross_market_snapshot,
    simulate_cross_market_sweep,
)
from .etf_trend_overlay_research import _file_sha256, simulate_v9_overlay
from .reverse_repo_sweep_research import SCENARIOS
from .storage import Database, ParquetSnapshotStore


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "v9_trailing_shadow_pnl_regime_with_repo"
FROZEN_PROTOCOL_SHA256 = "39a88c9fcfd1398c40b3e9d8748e3863635c315a55557a0de78475a474bf2268"
SHADOW_LOOKBACK_TRADES = 10
TARGET_WEIGHTED_ANNUALIZED_RETURN = 0.40
CROSS_MARKET_SNAPSHOT_ID = (
    "4ff910f10ac54ce3203dc7414e6031351e806513d9d9401a9d4c2b679dab1a04"
)
KNOWN_REPO_BASE_WEIGHTED_RETURN = 0.39914777775215443
KNOWN_REPO_STRESS_WEIGHTED_RETURN = 0.3936532859739442
KNOWN_REPO_WINDOW_RETURNS = {
    "close_base_commission": {
        "2021-04_2022-04": 0.15131185863799357,
        "2022-05_2023-05": 0.07279944146064454,
        "2023-06_2024-06": -0.006720549811785936,
        "2024-07_2025-07": 0.31001819070183734,
        "2025-07_2026-08": 0.5179561542705875,
    },
    "low_double_commission": {
        "2021-04_2022-04": 0.14545661048058944,
        "2022-05_2023-05": 0.06617329349842516,
        "2023-06_2024-06": -0.012861320686284983,
        "2024-07_2025-07": 0.3033946424031826,
        "2025-07_2026-08": 0.5131103270141868,
    },
}


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "post_result_retrospective_meta_strategy",
        "post_hoc_disclosure": {
            "all_underlying_v9_window_results_seen": True,
            "cross_market_repo_results_seen": True,
            "this_shadow_gate_result_seen_before_freeze": False,
            "interpretation": (
                "A pass is retrospective evidence only and requires forward validation."
            ),
        },
        "underlying": {
            "strategy": "course49_v9",
            "signals_and_exits": "unchanged",
            "position_quantities": "unchanged when an entry is accepted",
            "allowed_action": "skip a complete original entry/exit pair only",
            "never_increase_risk": True,
        },
        "shadow_gate": {
            "source": "all original V9 closed-trade net PnL, including skipped pairs",
            "lookback_closed_trades": SHADOW_LOOKBACK_TRADES,
            "warmup": "accept entries until ten prior shadow closes exist",
            "active_condition": "sum of the latest ten prior shadow PnL values is positive",
            "decision_time": "before the trading-day open",
            "same_day_sells": "excluded from same-day entry gate; become available next session",
            "multiple_buys_same_day": "share one pre-open gate state",
            "window_state": "shadow outcomes carry across consecutive evaluation windows",
            "parameter_search": False,
        },
        "cash": {
            "rule": "filtered idle cash uses frozen R-001/GC001 best eligible quote",
            "snapshot_id": CROSS_MARKET_SNAPSHOT_ID,
            "base": "Close plus 0.001% principal commission",
            "stress": "Low plus 0.002% principal commission",
            "interest_does_not_enable_additional_historical_stock_fills": True,
        },
        "evaluation": {
            "windows": [
                {
                    "label": item.label,
                    "backtest_id": item.backtest_id,
                    "start_date": item.start_date,
                    "end_date": item.end_date,
                    "weight": item.weight,
                }
                for item in WINDOWS
            ],
            "target_weighted_annualized_return": TARGET_WEIGHTED_ANNUALIZED_RETURN,
            "required_checks": [
                "base weighted annualized return at least 40%",
                "stress weighted return improves frozen repo control",
                "2023-06 to 2024-06 window is profitable",
                "both latest windows are profitable",
                "all window drawdowns are no worse than -10%",
                "filtered closed-trade median PnL is positive",
                "filtered PnL excluding top three winners is positive",
                "all V9 and repo controls reproduce exactly",
                "no accepted V9 buy is cash blocked",
            ],
        },
        "decision": {
            "passing_action": "require at least 60 new forward paper sessions",
            "passing_is_not_production_authorization": True,
            "failure_action": "reject without changing lookback or threshold",
        },
        "invariants": {
            "no_scan_registration": True,
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
        raise ValueError(f"Frozen shadow-regime protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def filter_v9_trades_by_shadow_pnl(
    trades: pd.DataFrame,
    prior_shadow_pnl: Sequence[float] = (),
    *,
    lookback_trades: int = SHADOW_LOOKBACK_TRADES,
) -> tuple[pd.DataFrame, pd.DataFrame, list[float]]:
    if lookback_trades <= 0:
        raise ValueError("lookback_trades must be positive")
    required = {"timestamp", "side", "code", "pnl"}
    missing = required.difference(trades.columns)
    if missing:
        raise ValueError(f"V9 trades are missing columns: {sorted(missing)}")
    frame = trades.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise").dt.normalize()
    frame["_order"] = np.arange(len(frame), dtype=int)
    frame.sort_values(["timestamp", "_order"], inplace=True)
    shadow_pnl = [float(value) for value in prior_shadow_pnl]
    active_codes: set[str] = set()
    kept_indexes: list[int] = []
    decisions: list[dict[str, Any]] = []
    for day, rows in frame.groupby("timestamp", sort=True):
        trailing = shadow_pnl[-lookback_trades:]
        warmup = len(trailing) < lookback_trades
        trailing_sum = float(sum(trailing))
        gate_active = warmup or trailing_sum > 0.0
        day_shadow_closes: list[float] = []
        for index, row in rows.sort_values("_order").iterrows():
            side = str(row["side"]).upper()
            code = str(row["code"])
            if side == "SELL":
                if code in active_codes:
                    kept_indexes.append(index)
                    active_codes.remove(code)
                pnl = row["pnl"]
                if pd.notna(pnl):
                    day_shadow_closes.append(float(pnl))
                continue
            if side != "BUY":
                raise ValueError(f"Unexpected V9 trade side: {side}")
            accepted = gate_active and code not in active_codes
            decisions.append(
                {
                    "timestamp": pd.Timestamp(day),
                    "code": code,
                    "accepted": bool(accepted),
                    "warmup": bool(warmup),
                    "prior_shadow_trade_count": int(len(shadow_pnl)),
                    "trailing_shadow_pnl": trailing_sum,
                }
            )
            if accepted:
                kept_indexes.append(index)
                active_codes.add(code)
        shadow_pnl.extend(day_shadow_closes)
    if active_codes:
        raise ValueError(f"Filtered V9 ledger ended with open positions: {sorted(active_codes)}")
    filtered = frame.loc[kept_indexes].sort_values(["timestamp", "_order"]).drop(
        columns="_order"
    )
    decision_frame = pd.DataFrame(decisions)
    return filtered.reset_index(drop=True), decision_frame, shadow_pnl


def assess_shadow_regime(
    base: Mapping[str, Any],
    stress: Mapping[str, Any],
    filtered_pnl: Sequence[float],
) -> dict[str, Any]:
    pnl = pd.Series(filtered_pnl, dtype=float)
    ex_top3 = pnl.sort_values(ascending=False).iloc[3:]
    base_windows = {item["label"]: item for item in base["windows"]}
    checks = {
        "weighted_annualized_return_at_least_40_percent": float(
            base["weighted_annualized_return"]
        )
        >= TARGET_WEIGHTED_ANNUALIZED_RETURN,
        "stress_improves_frozen_repo_control": float(
            stress["weighted_annualized_return"]
        )
        > KNOWN_REPO_STRESS_WEIGHTED_RETURN,
        "weak_window_profitable": float(
            base_windows["2023-06_2024-06"]["annualized_return"]
        )
        > 0.0,
        "latest_two_windows_profitable": all(
            float(base_windows[label]["annualized_return"]) > 0.0
            for label in ("2024-07_2025-07", "2025-07_2026-08")
        ),
        "all_drawdowns_within_ten_percent": all(
            float(item["max_drawdown"]) >= -0.10 for item in base["windows"]
        ),
        "positive_filtered_trade_median": not pnl.empty and float(pnl.median()) > 0.0,
        "positive_ex_top3_pnl": not ex_top3.empty and float(ex_top3.sum()) > 0.0,
        "exact_v9_controls": bool(base["exact_v9_controls"]),
        "exact_base_repo_controls": bool(base["exact_repo_controls"]),
        "exact_stress_repo_controls": bool(stress["exact_repo_controls"]),
        "zero_cash_blocks": int(base["cash_blocks"]) == 0
        and int(stress["cash_blocks"]) == 0,
    }
    passed = all(checks.values())
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "decision": "REQUIRE_FORWARD_VALIDATION" if passed else "REJECT",
        "retrospective_qualified": passed,
        "checks": checks,
        "filtered_trade_median_pnl": float(pnl.median()) if not pnl.empty else None,
        "filtered_ex_top3_pnl": float(ex_top3.sum()) if not ex_top3.empty else None,
        "production_authorized": False,
    }


def run_frozen_validation(
    config: PlatformConfig,
    database: Database,
    *,
    repo_snapshot_dir: Path,
    output_dir: Path,
    windows: Sequence[EvaluationWindow] = WINDOWS,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    protocol_hash = _file_sha256(output_dir / "protocol.json")
    if protocol_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen shadow-regime protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={protocol_hash}"
        )
    rates = load_cross_market_snapshot(repo_snapshot_dir)
    repo_manifest = json.loads(
        (Path(repo_snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    if repo_manifest["snapshot_id"] != CROSS_MARKET_SNAPSHOT_ID:
        raise ValueError("Unexpected cross-market repo snapshot")
    store = ParquetSnapshotStore(config, database)
    shadow_pnl: list[float] = []
    filtered_pnl: list[float] = []
    decisions: list[pd.DataFrame] = []
    prepared: list[dict[str, Any]] = []
    for window in windows:
        trades = _load_trades(database, window.backtest_id)
        filtered, window_decisions, shadow_pnl = filter_v9_trades_by_shadow_pnl(
            trades, shadow_pnl
        )
        window_decisions["window"] = window.label
        decisions.append(window_decisions)
        kept_sell_pnl = pd.to_numeric(
            filtered.loc[filtered["side"].eq("SELL"), "pnl"], errors="coerce"
        ).dropna()
        filtered_pnl.extend(float(value) for value in kept_sell_pnl)
        metrics = _load_metrics(database, window.backtest_id)
        raw = store.load_records(str(metrics["snapshot_id"]), "daily_raw")
        original_codes = set(trades["code"].astype(str))
        raw = raw.loc[raw["code"].astype(str).isin(original_codes)].copy()
        market = store.load_records(str(metrics["snapshot_id"]), "market_index")
        calendar = _calendar(market, window.start_date, window.end_date)
        prepared.append(
            {
                "window": window,
                "trades": trades,
                "filtered": filtered,
                "metrics": metrics,
                "config": config.portfolio,
                "raw": raw,
                "calendar": calendar,
                "kept_sell_pnl": kept_sell_pnl,
                "accepted_buys": int(window_decisions["accepted"].sum()),
                "attempted_buys": int(len(window_decisions)),
            }
        )
    base = _evaluate_scenario(prepared, rates, SCENARIOS[0])
    stress = _evaluate_scenario(prepared, rates, SCENARIOS[1])
    decision = assess_shadow_regime(base, stress, filtered_pnl)
    decision_path = output_dir / "entry_decisions.parquet"
    pd.concat(decisions, ignore_index=True).to_parquet(decision_path, index=False)
    payload = {
        "protocol_sha256": protocol_hash,
        "repo_snapshot_id": repo_manifest["snapshot_id"],
        "repo_snapshot_rates_sha256": repo_manifest["rates_sha256"],
        "base": base,
        "stress": stress,
        "decision": decision,
        "entry_decisions_sha256": _file_sha256(decision_path),
    }
    (output_dir / "validation_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return payload


def _evaluate_scenario(
    prepared: Sequence[Mapping[str, Any]],
    rates: pd.DataFrame,
    scenario: Any,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    exact_v9 = True
    exact_repo = True
    cash_blocks = 0
    for source in prepared:
        window = source["window"]
        metrics = source["metrics"]
        empty_events = pd.DataFrame(columns=["entry_date", "score", "code"])
        empty_bars = source["raw"].iloc[0:0].copy()
        baseline = simulate_v9_overlay(
            source["trades"],
            source["raw"],
            empty_events,
            empty_bars,
            source["calendar"],
            initial_cash=float(metrics["initial_cash"]),
            config=source["config"],
        )
        filtered = simulate_v9_overlay(
            source["filtered"],
            source["raw"],
            empty_events,
            empty_bars,
            source["calendar"],
            initial_cash=float(metrics["initial_cash"]),
            config=source["config"],
        )
        baseline_equity = pd.DataFrame(baseline.pop("equity"))
        filtered_equity = pd.DataFrame(filtered.pop("equity"))
        baseline_repo = simulate_cross_market_sweep(
            baseline_equity,
            rates,
            initial_cash=float(metrics["initial_cash"]),
            rate_field=scenario.rate_field,
            commission_rate=scenario.commission_rate,
        )
        filtered_repo = simulate_cross_market_sweep(
            filtered_equity,
            rates,
            initial_cash=float(metrics["initial_cash"]),
            rate_field=scenario.rate_field,
            commission_rate=scenario.commission_rate,
        )
        exact_v9 = exact_v9 and abs(
            float(baseline["portfolio_total_return"]) - float(metrics["total_return"])
        ) < 1e-10
        known = KNOWN_REPO_WINDOW_RETURNS[scenario.scenario_id][window.label]
        exact_repo = exact_repo and abs(
            float(baseline_repo["annualized_return"]) - known
        ) < 1e-12
        cash_blocks += int(filtered["v9_cash_blocked"])
        reports.append(
            {
                "label": window.label,
                "backtest_id": window.backtest_id,
                "weight": window.weight,
                "attempted_buys": int(source["attempted_buys"]),
                "accepted_buys": int(source["accepted_buys"]),
                "retention_rate": (
                    float(source["accepted_buys"] / source["attempted_buys"])
                    if source["attempted_buys"]
                    else 0.0
                ),
                "closed_trades": int(len(source["kept_sell_pnl"])),
                "annualized_return": float(filtered_repo["annualized_return"]),
                "total_return": float(filtered_repo["total_return"]),
                "incremental_total_return": float(
                    filtered_repo["total_return"] - baseline_repo["total_return"]
                ),
                "max_drawdown": float(filtered_repo["max_drawdown"]),
                "net_repo_interest": float(filtered_repo["net_interest"]),
            }
        )
    return {
        "scenario_id": scenario.scenario_id,
        "weighted_annualized_return": float(
            sum(item["weight"] * item["annualized_return"] for item in reports)
        ),
        "exact_v9_controls": exact_v9,
        "exact_repo_controls": exact_repo,
        "cash_blocks": int(cash_blocks),
        "windows": reports,
    }


def _load_trades(database: Database, backtest_id: str) -> pd.DataFrame:
    return pd.DataFrame(
        database.query(
            "SELECT rowid,* FROM backtest_trades WHERE backtest_id=? "
            "AND strategy_id='course49_v9' ORDER BY timestamp,rowid",
            (backtest_id,),
        )
    )


def _load_metrics(database: Database, backtest_id: str) -> dict[str, Any]:
    rows = database.query(
        "SELECT snapshot_id,metrics_json FROM backtests WHERE backtest_id=?",
        (backtest_id,),
    )
    if not rows:
        raise ValueError(f"Missing V9 backtest: {backtest_id}")
    metrics = json.loads(str(rows[0]["metrics_json"] or "{}"))
    metrics["snapshot_id"] = str(rows[0]["snapshot_id"])
    return metrics


def _calendar(
    market: pd.DataFrame, start_date: str, end_date: str
) -> list[pd.Timestamp]:
    values = pd.to_datetime(market["timestamp"], errors="coerce").dropna().dt.normalize()
    return sorted(values.loc[values.between(start_date, end_date)].unique())
