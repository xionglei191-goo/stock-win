from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .cash_sweep_research import EvaluationWindow, WINDOWS
from .config import PlatformConfig
from .cross_market_repo_research import load_cross_market_snapshot
from .etf_trend_overlay_research import _file_sha256
from .reverse_repo_sweep_research import SCENARIOS
from .storage import Database, ParquetSnapshotStore
from .v9_shadow_regime_research import (
    CROSS_MARKET_SNAPSHOT_ID,
    KNOWN_REPO_STRESS_WEIGHTED_RETURN,
    _calendar,
    _evaluate_scenario,
    _load_metrics,
    _load_trades,
)


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "v9_prior_shanghai_composite_ma120_regime_with_repo"
FROZEN_PROTOCOL_SHA256 = "a7eb9cdd1893cf82f8cd248ef7e537c67dcfbccafa8232cecf718bc61ce27eeb"
MARKET_TREND_SESSIONS = 120
TARGET_WEIGHTED_ANNUALIZED_RETURN = 0.40


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "post_result_retrospective_external_regime",
        "post_hoc_disclosure": {
            "underlying_v9_and_repo_results_seen": True,
            "ma120_gate_result_seen_before_freeze": False,
            "interpretation": (
                "The gate has economic meaning but is evaluated retrospectively. A pass "
                "requires new forward evidence and does not authorize production."
            ),
        },
        "market_gate": {
            "series": "999999.SH Shanghai Composite from each frozen market snapshot",
            "adjustment": "existing snapshot front-adjusted index series",
            "lookback_sessions": MARKET_TREND_SESSIONS,
            "allowed": "last close strictly before entry day is above its MA120",
            "equal_to_ma": "blocked",
            "insufficient_history": "blocked",
            "same_day_market_data": "forbidden",
            "additional_trend_or_breadth_filters": False,
            "parameter_search": False,
        },
        "underlying": {
            "strategy": "course49_v9",
            "selection_and_exit": "unchanged",
            "accepted_quantity_price_and_fee": "unchanged",
            "action": "skip a complete entry/exit pair when the external gate is false",
            "never_increase_risk": True,
        },
        "cash": {
            "snapshot_id": CROSS_MARKET_SNAPSHOT_ID,
            "rule": "filtered idle cash uses frozen R-001/GC001 best eligible quote",
            "base": "Close plus 0.001% principal commission",
            "stress": "Low plus 0.002% principal commission",
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
            "required_checks": [
                "base weighted annualized return at least 40%",
                "stress weighted return improves frozen repo control",
                "2023-06 to 2024-06 is profitable",
                "latest two windows are profitable",
                "all drawdowns are no worse than -10%",
                "filtered closed-trade median and ex-top-three PnL are positive",
                "all controls reproduce exactly and no accepted buy is cash blocked",
            ],
        },
        "decision": {
            "passing_action": "require at least 60 new forward paper sessions",
            "passing_is_not_production_authorization": True,
            "failure_action": "reject without changing MA length or adding filters",
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
        raise ValueError(f"Frozen market-trend protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def filter_v9_trades_by_market_trend(
    trades: pd.DataFrame,
    market_index: pd.DataFrame,
    *,
    lookback_sessions: int = MARKET_TREND_SESSIONS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if lookback_sessions <= 1:
        raise ValueError("lookback_sessions must exceed one")
    required_trades = {"timestamp", "side", "code"}
    missing_trades = required_trades.difference(trades.columns)
    if missing_trades:
        raise ValueError(f"V9 trades are missing columns: {sorted(missing_trades)}")
    required_market = {"timestamp", "Close"}
    missing_market = required_market.difference(market_index.columns)
    if missing_market:
        raise ValueError(f"Market index is missing columns: {sorted(missing_market)}")
    market = market_index.loc[:, ["timestamp", "Close"]].copy()
    market["timestamp"] = pd.to_datetime(market["timestamp"], errors="raise").dt.normalize()
    market["Close"] = pd.to_numeric(market["Close"], errors="raise")
    market.sort_values("timestamp", inplace=True)
    if market["timestamp"].duplicated().any():
        raise ValueError("Market index contains duplicate sessions")
    market["ma"] = market["Close"].rolling(
        lookback_sessions, min_periods=lookback_sessions
    ).mean()
    frame = trades.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise").dt.normalize()
    frame["_order"] = np.arange(len(frame), dtype=int)
    frame.sort_values(["timestamp", "_order"], inplace=True)
    active_codes: set[str] = set()
    kept_indexes: list[int] = []
    decisions: list[dict[str, Any]] = []
    for day, rows in frame.groupby("timestamp", sort=True):
        prior = market.loc[market["timestamp"].lt(day)].tail(1)
        if prior.empty:
            market_date = pd.NaT
            close = np.nan
            ma = np.nan
            allowed = False
        else:
            market_date = pd.Timestamp(prior.iloc[0]["timestamp"])
            close = float(prior.iloc[0]["Close"])
            ma = float(prior.iloc[0]["ma"])
            allowed = bool(np.isfinite(ma) and close > ma)
        for index, row in rows.sort_values("_order").iterrows():
            side = str(row["side"]).upper()
            code = str(row["code"])
            if side == "SELL":
                if code in active_codes:
                    kept_indexes.append(index)
                    active_codes.remove(code)
                continue
            if side != "BUY":
                raise ValueError(f"Unexpected V9 trade side: {side}")
            accepted = allowed and code not in active_codes
            decisions.append(
                {
                    "timestamp": pd.Timestamp(day),
                    "code": code,
                    "accepted": bool(accepted),
                    "market_date": market_date,
                    "market_close": close,
                    "market_ma120": ma,
                }
            )
            if accepted:
                kept_indexes.append(index)
                active_codes.add(code)
    if active_codes:
        raise ValueError(f"Filtered V9 ledger ended with open positions: {sorted(active_codes)}")
    filtered = frame.loc[kept_indexes].sort_values(["timestamp", "_order"]).drop(
        columns="_order"
    )
    return filtered.reset_index(drop=True), pd.DataFrame(decisions)


def assess_market_trend_regime(
    base: Mapping[str, Any],
    stress: Mapping[str, Any],
    filtered_pnl: Sequence[float],
) -> dict[str, Any]:
    pnl = pd.Series(filtered_pnl, dtype=float)
    ex_top3 = pnl.sort_values(ascending=False).iloc[3:]
    by_label = {item["label"]: item for item in base["windows"]}
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
            by_label["2023-06_2024-06"]["annualized_return"]
        )
        > 0.0,
        "latest_two_windows_profitable": all(
            float(by_label[label]["annualized_return"]) > 0.0
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
            "Frozen market-trend protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={protocol_hash}"
        )
    rates = load_cross_market_snapshot(repo_snapshot_dir)
    repo_manifest = json.loads(
        (Path(repo_snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    if repo_manifest["snapshot_id"] != CROSS_MARKET_SNAPSHOT_ID:
        raise ValueError("Unexpected cross-market repo snapshot")
    store = ParquetSnapshotStore(config, database)
    prepared: list[dict[str, Any]] = []
    decision_frames: list[pd.DataFrame] = []
    filtered_pnl: list[float] = []
    for window in windows:
        trades = _load_trades(database, window.backtest_id)
        metrics = _load_metrics(database, window.backtest_id)
        market = store.load_records(str(metrics["snapshot_id"]), "market_index")
        filtered, decisions = filter_v9_trades_by_market_trend(trades, market)
        decisions["window"] = window.label
        decision_frames.append(decisions)
        kept_sell_pnl = pd.to_numeric(
            filtered.loc[filtered["side"].eq("SELL"), "pnl"], errors="coerce"
        ).dropna()
        filtered_pnl.extend(float(value) for value in kept_sell_pnl)
        raw = store.load_records(str(metrics["snapshot_id"]), "daily_raw")
        original_codes = set(trades["code"].astype(str))
        raw = raw.loc[raw["code"].astype(str).isin(original_codes)].copy()
        prepared.append(
            {
                "window": window,
                "trades": trades,
                "filtered": filtered,
                "metrics": metrics,
                "config": config.portfolio,
                "raw": raw,
                "calendar": _calendar(market, window.start_date, window.end_date),
                "kept_sell_pnl": kept_sell_pnl,
                "accepted_buys": int(decisions["accepted"].sum()),
                "attempted_buys": int(len(decisions)),
            }
        )
    base = _evaluate_scenario(prepared, rates, SCENARIOS[0])
    stress = _evaluate_scenario(prepared, rates, SCENARIOS[1])
    decision = assess_market_trend_regime(base, stress, filtered_pnl)
    decisions_path = output_dir / "entry_decisions.parquet"
    pd.concat(decision_frames, ignore_index=True).to_parquet(decisions_path, index=False)
    payload = {
        "protocol_sha256": protocol_hash,
        "repo_snapshot_id": repo_manifest["snapshot_id"],
        "repo_snapshot_rates_sha256": repo_manifest["rates_sha256"],
        "base": base,
        "stress": stress,
        "decision": decision,
        "entry_decisions_sha256": _file_sha256(decisions_path),
    }
    (output_dir / "validation_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return payload
