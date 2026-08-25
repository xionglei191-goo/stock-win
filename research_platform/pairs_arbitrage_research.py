from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .storage import Database


STRATEGY_ID = "pairs_arbitrage_v1"
STRATEGY_VERSION = "1.0.0"
BASELINE_BACKTEST_IDS = (
    "883dcc0333a84ad187ab3ef2dba164c9",
    "e91bddb751c147c386a028a1fc5e206a",
    "8047ac75144549a09f1cf73568ac92cc",
    "11a4cd0a58a842c6b34fa05e39275d7a",
    "3c356e775dd3405e9e27bd488a02da68",
)
STRESS_BACKTEST_IDS = (
    "17b2dc36a8a4442da02aaf7864c8fd83",
    "97ff577b320c413db3b704c21309f790",
    "3f4b135a321e4c9298ff9d61a5a9b7fd",
    "de890dfdd878493485748d11ffb804c1",
    "00b059ce61694acd808b8564fc63aaa7",
)
VALIDATION_WINDOWS = (
    ("2021-04-01", "2022-04-29"),
    ("2022-05-01", "2023-05-31"),
    ("2023-06-01", "2024-06-28"),
    ("2024-07-01", "2025-07-24"),
    ("2025-07-25", "2026-08-07"),
)


def analyze_pairs_arbitrage_validation(
    database: Database,
    *,
    baseline_backtest_ids: Iterable[str] = BASELINE_BACKTEST_IDS,
    stress_backtest_ids: Iterable[str] = STRESS_BACKTEST_IDS,
) -> dict[str, Any]:
    baseline_ids = tuple(str(item) for item in baseline_backtest_ids)
    stress_ids = tuple(str(item) for item in stress_backtest_ids)
    if len(baseline_ids) != len(VALIDATION_WINDOWS) or len(stress_ids) != len(
        VALIDATION_WINDOWS
    ):
        raise ValueError("Pairs validation requires five baseline and five stress runs")

    baseline = [
        _summarize_backtest(database, backtest_id, expected_multiplier=1.0)
        for backtest_id in baseline_ids
    ]
    stress = [
        _summarize_backtest(database, backtest_id, expected_multiplier=2.0)
        for backtest_id in stress_ids
    ]
    for index, expected_window in enumerate(VALIDATION_WINDOWS):
        actual_window = (baseline[index]["start_date"], baseline[index]["end_date"])
        stress_window = (stress[index]["start_date"], stress[index]["end_date"])
        if actual_window != expected_window or stress_window != expected_window:
            raise ValueError(
                f"Unexpected pairs validation window at position {index}: "
                f"{actual_window} / {stress_window}"
            )

    same_snapshot_replay = all(
        base["snapshot_id"]
        == base["source_snapshot_id"]
        == stressed["snapshot_id"]
        == stressed["source_snapshot_id"]
        and base["stock_pool_hash"] == stressed["stock_pool_hash"]
        for base, stressed in zip(baseline, stress)
    )
    universe_hashes = {
        str(item["stock_pool_hash"]) for item in (*baseline, *stress)
    }
    baseline_group_pnls = [
        float(value)
        for item in baseline
        for value in item.pop("_group_pnls")
    ]
    stress_group_pnls = [
        float(value)
        for item in stress
        for value in item.pop("_group_pnls")
    ]
    baseline_chained_return = _chained_return(baseline)
    stress_chained_return = _chained_return(stress)
    baseline_positive_windows = sum(
        float(item["total_return"]) > 0 for item in baseline
    )
    stress_positive_windows = sum(float(item["total_return"]) > 0 for item in stress)
    aggregate = {
        "baseline_positive_windows": baseline_positive_windows,
        "stress_positive_windows": stress_positive_windows,
        "baseline_chained_return": baseline_chained_return,
        "stress_chained_return": stress_chained_return,
        "baseline_completed_pair_groups": len(baseline_group_pnls),
        "stress_completed_pair_groups": len(stress_group_pnls),
        "baseline_median_pair_pnl": _median(baseline_group_pnls),
        "stress_median_pair_pnl": _median(stress_group_pnls),
        "baseline_ex_top3_pair_pnl": _ex_top_n(baseline_group_pnls, 3),
        "stress_ex_top3_pair_pnl": _ex_top_n(stress_group_pnls, 3),
        "same_snapshot_replay": same_snapshot_replay,
        "stable_universe_hash": len(universe_hashes) == 1 and "" not in universe_hashes,
    }
    gates = {
        "five_non_overlapping_windows": len(baseline) == 5 and len(stress) == 5,
        "same_snapshot_cost_replay": same_snapshot_replay,
        "stable_fixed_universe": len(universe_hashes) == 1 and "" not in universe_hashes,
        "minimum_completed_pair_groups": len(baseline_group_pnls) >= 50,
        "baseline_window_stability": baseline_positive_windows >= 4,
        "stress_window_stability": stress_positive_windows >= 3,
        "baseline_chained_return_positive": baseline_chained_return > 0,
        "stress_chained_return_positive": stress_chained_return > 0,
        "median_pair_pnl_positive": bool(
            baseline_group_pnls and statistics.median(baseline_group_pnls) > 0
        ),
        "ex_top3_pair_pnl_positive": _ex_top_n(baseline_group_pnls, 3) > 0,
    }
    historical_gates_passed = all(gates.values())
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "decision": (
            "HISTORICAL_RESEARCH_CANDIDATE"
            if historical_gates_passed
            else "HISTORICAL_REJECTED"
        ),
        "study_type": "RETROSPECTIVE_FROZEN_AUDIT",
        "historical_gates_passed": historical_gates_passed,
        "promotion_qualified": False,
        "promotion_use": "NOT_ELIGIBLE_WITHOUT_NEW_INDEPENDENT_WINDOWS",
        "protocol": {
            "windows": [
                {"start_date": start, "end_date": end}
                for start, end in VALIDATION_WINDOWS
            ],
            "baseline_cost_multiplier": 1.0,
            "stress_cost_multiplier": 2.0,
            "minimum_completed_pair_groups": 50,
            "minimum_positive_baseline_windows": 4,
            "minimum_positive_stress_windows": 3,
            "requires_positive_chained_returns": True,
            "requires_positive_median_pair_pnl": True,
            "requires_positive_ex_top3_pair_pnl": True,
        },
        "baseline_windows": baseline,
        "stress_windows": stress,
        "aggregate": aggregate,
        "gates": gates,
        "known_limitations": [
            "The two pairs are fixed current constituents and do not represent a point-in-time pair discovery universe.",
            "The entry gate checks return correlation but does not test residual stationarity or cointegration.",
            "Version 1.0 executes equal gross leg weights even though the signal records a rolling hedge ratio.",
            "Short selling is paper-only and excludes borrow availability, margin, recalls, and borrow fees.",
        ],
        "next_action": (
            "Keep version 1.0 backtest-only as a rejected baseline. A future version must "
            "pre-register point-in-time pair discovery, stationarity, hedge sizing, and short costs "
            "before opening new validation windows."
        ),
    }


def persist_pairs_arbitrage_validation(
    result: dict[str, Any],
    directory: str | Path,
) -> Path:
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "historical_validation.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def run_persisted_pairs_arbitrage_validation(
    database: Database,
    directory: str | Path,
    *,
    baseline_backtest_ids: Iterable[str] = BASELINE_BACKTEST_IDS,
    stress_backtest_ids: Iterable[str] = STRESS_BACKTEST_IDS,
) -> dict[str, Any]:
    result = analyze_pairs_arbitrage_validation(
        database,
        baseline_backtest_ids=baseline_backtest_ids,
        stress_backtest_ids=stress_backtest_ids,
    )
    path = persist_pairs_arbitrage_validation(result, directory)
    return {**result, "artifact_path": str(path)}


def _summarize_backtest(
    database: Database,
    backtest_id: str,
    *,
    expected_multiplier: float,
) -> dict[str, Any]:
    rows = database.query("SELECT * FROM backtests WHERE backtest_id=?", (backtest_id,))
    if not rows:
        raise ValueError(f"Unknown pairs backtest: {backtest_id}")
    row = rows[0]
    if str(row["strategy_id"]) != STRATEGY_ID or str(row["status"]) != "SUCCEEDED":
        raise ValueError(f"Backtest {backtest_id} is not a successful {STRATEGY_ID} run")
    metrics = _json_object(row.get("metrics_json"))
    parameters = _json_object(row.get("parameters_json"))
    versions = parameters.get("strategy_versions")
    components = parameters.get("components")
    if not isinstance(versions, dict) or versions.get(STRATEGY_ID) != STRATEGY_VERSION:
        raise ValueError(
            f"Backtest {backtest_id} is not frozen {STRATEGY_ID} {STRATEGY_VERSION}"
        )
    if components != [STRATEGY_ID]:
        raise ValueError(f"Backtest {backtest_id} is not a standalone pairs run")
    multiplier = float(parameters.get("execution_cost_multiplier", 1.0))
    if abs(multiplier - expected_multiplier) > 1e-12:
        raise ValueError(
            f"Backtest {backtest_id} has cost multiplier {multiplier}, "
            f"expected {expected_multiplier}"
        )
    grouped = _completed_pair_groups(database, backtest_id)
    group_pnls = [float(item["pnl"]) for item in grouped]
    pair_pnl: dict[str, float] = {}
    reason_pnl: dict[str, float] = {}
    for item in grouped:
        key = str(item["group_key"])
        pair_pnl[key] = pair_pnl.get(key, 0.0) + float(item["pnl"])
        reason = str(item["reason"])
        reason_pnl[reason] = reason_pnl.get(reason, 0.0) + float(item["pnl"])
    return {
        "backtest_id": backtest_id,
        "start_date": str(row["start_date"]),
        "end_date": str(row["end_date"]),
        "snapshot_id": str(row.get("snapshot_id") or ""),
        "source_snapshot_id": str(parameters.get("source_snapshot_id") or ""),
        "stock_pool_hash": str(parameters.get("stock_pool_hash") or ""),
        "data_asof": str(parameters.get("data_asof") or row["end_date"]),
        "cost_multiplier": multiplier,
        "trading_days": int(metrics.get("trading_days") or 0),
        "closed_legs": int(metrics.get("closed_trades") or 0),
        "completed_pair_groups": len(grouped),
        "total_return": float(metrics.get("total_return") or 0.0),
        "annualized_return": float(metrics.get("annualized_return") or 0.0),
        "max_drawdown": float(metrics.get("max_drawdown") or 0.0),
        "pair_win_rate": (
            float(sum(value > 0 for value in group_pnls) / len(group_pnls))
            if group_pnls
            else 0.0
        ),
        "median_pair_pnl": _median(group_pnls),
        "ex_top3_pair_pnl": _ex_top_n(group_pnls, 3),
        "pair_pnl": dict(sorted(pair_pnl.items())),
        "exit_reason_pnl": dict(sorted(reason_pnl.items())),
        "average_gross_exposure": _component_metric(
            metrics, "average_gross_exposure"
        ),
        "average_net_exposure": _component_metric(metrics, "average_net_exposure"),
        "_group_pnls": group_pnls,
    }


def _completed_pair_groups(database: Database, backtest_id: str) -> list[dict[str, Any]]:
    rows = database.query(
        """SELECT timestamp, group_key, pnl, reason FROM backtest_trades
        WHERE backtest_id=? AND side IN ('SELL', 'COVER')
        ORDER BY timestamp, group_key, code""",
        (backtest_id,),
    )
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["timestamp"])[:10], str(row["group_key"]))
        item = grouped.setdefault(
            key,
            {
                "exit_date": key[0],
                "group_key": key[1],
                "pnl": 0.0,
                "reasons": set(),
            },
        )
        item["pnl"] += float(row.get("pnl") or 0.0)
        item["reasons"].add(str(row.get("reason") or ""))
    output = []
    for item in grouped.values():
        reasons = sorted(value for value in item.pop("reasons") if value)
        output.append({**item, "reason": ",".join(reasons)})
    return output


def _json_object(value: Any) -> dict[str, Any]:
    try:
        result = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("Stored pairs validation JSON is invalid") from exc
    return result if isinstance(result, dict) else {}


def _component_metric(metrics: dict[str, Any], key: str) -> float | None:
    components = metrics.get("components")
    if not isinstance(components, dict):
        return None
    strategy = components.get(STRATEGY_ID)
    if not isinstance(strategy, dict) or strategy.get(key) is None:
        return None
    return float(strategy[key])


def _chained_return(windows: Iterable[dict[str, Any]]) -> float:
    value = 1.0
    for item in windows:
        value *= 1.0 + float(item["total_return"])
    return float(value - 1.0)


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _ex_top_n(values: list[float], count: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values, reverse=True)
    return float(sum(ordered[count:]))
