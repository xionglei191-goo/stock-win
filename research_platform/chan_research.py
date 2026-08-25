from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .backtest_engine import BacktestService, CHAN_REPLAY_CONTRACT_VERSION
from .config import PlatformConfig
from .storage import Database


STRATEGY_ID = "chan_v1"
STRATEGY_VERSION = "2.0.0"
PROTOCOL_FILENAME = "protocol.json"
MANIFEST_FILENAME = "run_manifest.json"
ARTIFACT_FILENAME = "historical_validation.json"


def run_persisted_chan_validation(
    config: PlatformConfig,
    database: Database,
    directory: str | Path,
) -> dict[str, Any]:
    destination = Path(directory)
    protocol, protocol_hash = load_chan_protocol(destination / PROTOCOL_FILENAME)
    manifest = _load_manifest(destination / MANIFEST_FILENAME, protocol_hash)
    backtests = BacktestService(config, database)
    run_ids: dict[str, dict[str, str]] = {}

    for window in protocol["windows"]:
        window_key = _window_key(window)
        run_ids[window_key] = {}
        for label, multiplier in (
            ("baseline", float(protocol["cost_multipliers"]["baseline"])),
            ("stress", float(protocol["cost_multipliers"]["stress"])),
        ):
            stored_id = str(
                manifest.get("runs", {}).get(window_key, {}).get(label, "")
            )
            backtest_id = _matching_run_id(
                database,
                window,
                multiplier,
                preferred_id=stored_id,
            )
            if not backtest_id:
                result = backtests.replay_chan(
                    str(window["source_backtest_id"]),
                    strategy_id=STRATEGY_ID,
                    start_date=str(window["start_date"]),
                    end_date=str(window["end_date"]),
                    execution_cost_multiplier=multiplier,
                )
                backtest_id = str(result["backtest_id"])
            run_ids[window_key][label] = backtest_id
            manifest.setdefault("runs", {}).setdefault(window_key, {})[
                label
            ] = backtest_id
            manifest["updated_at"] = datetime.now().astimezone().isoformat()
            _write_json(destination / MANIFEST_FILENAME, manifest)
        backtests.cache.memory.clear()

    baseline_ids = [
        run_ids[_window_key(window)]["baseline"] for window in protocol["windows"]
    ]
    stress_ids = [
        run_ids[_window_key(window)]["stress"] for window in protocol["windows"]
    ]
    result = analyze_chan_validation(
        database,
        protocol,
        protocol_hash=protocol_hash,
        baseline_backtest_ids=baseline_ids,
        stress_backtest_ids=stress_ids,
    )
    artifact_path = destination / ARTIFACT_FILENAME
    _write_json(artifact_path, result)
    return {**result, "artifact_path": str(artifact_path)}


def load_chan_protocol(path: str | Path) -> tuple[dict[str, Any], str]:
    source = Path(path)
    raw = source.read_bytes()
    try:
        protocol = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Chan protocol is unreadable: {source}") from exc
    if not isinstance(protocol, dict):
        raise ValueError("Chan protocol must be a JSON object")
    _validate_protocol(protocol)
    return protocol, hashlib.sha256(raw).hexdigest()


def analyze_chan_validation(
    database: Database,
    protocol: dict[str, Any],
    *,
    protocol_hash: str,
    baseline_backtest_ids: Iterable[str],
    stress_backtest_ids: Iterable[str],
) -> dict[str, Any]:
    _validate_protocol(protocol)
    windows = list(protocol["windows"])
    baseline_ids = tuple(str(item) for item in baseline_backtest_ids)
    stress_ids = tuple(str(item) for item in stress_backtest_ids)
    if len(baseline_ids) != len(windows) or len(stress_ids) != len(windows):
        raise ValueError("Chan validation requires baseline and stress runs for every window")

    baseline = [
        _summarize_backtest(
            database,
            backtest_id,
            window,
            float(protocol["cost_multipliers"]["baseline"]),
        )
        for backtest_id, window in zip(baseline_ids, windows)
    ]
    stress = [
        _summarize_backtest(
            database,
            backtest_id,
            window,
            float(protocol["cost_multipliers"]["stress"]),
        )
        for backtest_id, window in zip(stress_ids, windows)
    ]
    baseline_pnls = [
        float(value) for item in baseline for value in item.pop("_trade_pnls")
    ]
    stress_pnls = [
        float(value) for item in stress for value in item.pop("_trade_pnls")
    ]
    gates_config = protocol["historical_gates"]
    baseline_positive = sum(float(item["total_return"]) > 0 for item in baseline)
    stress_positive = sum(float(item["total_return"]) > 0 for item in stress)
    same_snapshot = all(
        base["snapshot_id"]
        == base["source_snapshot_id"]
        == stressed["snapshot_id"]
        == stressed["source_snapshot_id"]
        for base, stressed in zip(baseline, stress)
    )
    matching_pools = all(
        base["stock_pool_hash"] == stressed["stock_pool_hash"]
        and bool(base["stock_pool_hash"])
        for base, stressed in zip(baseline, stress)
    )
    worst_return = min(
        (float(item["total_return"]) for item in (*baseline, *stress)),
        default=0.0,
    )
    maximum_drawdown = max(
        (abs(float(item["max_drawdown"])) for item in (*baseline, *stress)),
        default=0.0,
    )
    baseline_chained = _chained_return(baseline)
    stress_chained = _chained_return(stress)
    baseline_median = _median(baseline_pnls)
    baseline_ex_top3 = _ex_top_n(baseline_pnls, 3)
    gates = {
        "five_non_overlapping_windows": _non_overlapping_windows(windows),
        "same_snapshot_cost_replay": same_snapshot,
        "matching_cost_replay_stock_pools": matching_pools,
        "baseline_window_stability": baseline_positive
        >= int(gates_config["minimum_positive_baseline_windows"]),
        "stress_window_stability": stress_positive
        >= int(gates_config["minimum_positive_stress_windows"]),
        "minimum_completed_trades": len(baseline_pnls)
        >= int(gates_config["minimum_completed_trades"]),
        "minimum_active_windows": sum(
            int(item["closed_trades"]) >= 5 for item in baseline
        )
        >= int(gates_config["minimum_windows_with_five_completed_trades"]),
        "baseline_chained_return_positive": baseline_chained > 0,
        "stress_chained_return_positive": stress_chained > 0,
        "median_trade_pnl_positive": baseline_median is not None
        and baseline_median > 0,
        "ex_top3_trade_pnl_positive": baseline_ex_top3 > 0,
        "maximum_window_drawdown": maximum_drawdown
        <= float(gates_config["maximum_absolute_window_drawdown"]),
        "minimum_worst_window_return": worst_return
        >= float(gates_config["minimum_worst_window_total_return"]),
    }
    historical_gates_passed = all(gates.values())
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "chan_replay_contract_version": CHAN_REPLAY_CONTRACT_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "protocol_sha256": protocol_hash,
        "decision": (
            "HISTORICAL_RESEARCH_CANDIDATE"
            if historical_gates_passed
            else "HISTORICAL_REJECTED"
        ),
        "study_type": str(protocol["study_type"]),
        "historical_gates_passed": historical_gates_passed,
        "promotion_qualified": False,
        "promotion_use": str(protocol["promotion_use"]),
        "baseline_windows": baseline,
        "stress_windows": stress,
        "aggregate": {
            "baseline_positive_windows": baseline_positive,
            "stress_positive_windows": stress_positive,
            "baseline_chained_return": baseline_chained,
            "stress_chained_return": stress_chained,
            "baseline_completed_trades": len(baseline_pnls),
            "stress_completed_trades": len(stress_pnls),
            "baseline_median_trade_pnl": baseline_median,
            "stress_median_trade_pnl": _median(stress_pnls),
            "baseline_ex_top3_trade_pnl": baseline_ex_top3,
            "stress_ex_top3_trade_pnl": _ex_top_n(stress_pnls, 3),
            "maximum_absolute_window_drawdown": maximum_drawdown,
            "worst_window_total_return": worst_return,
            "same_snapshot_replay": same_snapshot,
            "matching_cost_replay_stock_pools": matching_pools,
        },
        "gates": gates,
        "known_limitations": list(protocol.get("known_limitations") or []),
        "next_action": (
            "Keep Chan V1 backtest-only as a rejected historical baseline. Any successor "
            "must freeze point-in-time sector membership and new independent windows before "
            "results are inspected."
            if not historical_gates_passed
            else "Treat the result as retrospective research only; collect new independent "
            "windows with point-in-time sector membership before any promotion decision."
        ),
    }


def _summarize_backtest(
    database: Database,
    backtest_id: str,
    window: dict[str, Any],
    expected_multiplier: float,
) -> dict[str, Any]:
    rows = database.query("SELECT * FROM backtests WHERE backtest_id=?", (backtest_id,))
    if not rows:
        raise ValueError(f"Unknown Chan backtest: {backtest_id}")
    row = rows[0]
    if str(row["strategy_id"]) != STRATEGY_ID or str(row["status"]) != "SUCCEEDED":
        raise ValueError(f"Backtest {backtest_id} is not a successful {STRATEGY_ID} run")
    parameters = _json_object(row.get("parameters_json"))
    metrics = _json_object(row.get("metrics_json"))
    versions = parameters.get("strategy_versions")
    if not isinstance(versions, dict) or versions.get(STRATEGY_ID) != STRATEGY_VERSION:
        raise ValueError(f"Backtest {backtest_id} is not frozen {STRATEGY_ID} {STRATEGY_VERSION}")
    if parameters.get("components") != [STRATEGY_ID]:
        raise ValueError(f"Backtest {backtest_id} is not a standalone Chan run")
    if parameters.get("chan_replay_contract_version") != CHAN_REPLAY_CONTRACT_VERSION:
        raise ValueError(f"Backtest {backtest_id} uses an incompatible Chan replay contract")
    multiplier = float(parameters.get("execution_cost_multiplier", 1.0))
    if abs(multiplier - expected_multiplier) > 1e-12:
        raise ValueError(
            f"Backtest {backtest_id} has cost multiplier {multiplier}, expected {expected_multiplier}"
        )
    expected_window = (str(window["start_date"]), str(window["end_date"]))
    if (str(row["start_date"]), str(row["end_date"])) != expected_window:
        raise ValueError(f"Backtest {backtest_id} does not match its frozen window")
    if str(parameters.get("source_backtest_id") or "") != str(
        window["source_backtest_id"]
    ):
        raise ValueError(f"Backtest {backtest_id} does not match its frozen source run")
    expected_snapshot = str(window["source_snapshot_id"])
    if str(row.get("snapshot_id") or "") != expected_snapshot or str(
        parameters.get("source_snapshot_id") or ""
    ) != expected_snapshot:
        raise ValueError(f"Backtest {backtest_id} does not match its frozen snapshot")
    trade_rows = database.query(
        """SELECT pnl FROM backtest_trades
        WHERE backtest_id=? AND strategy_id=? AND side='SELL' ORDER BY timestamp, code""",
        (backtest_id, STRATEGY_ID),
    )
    trade_pnls = [float(item.get("pnl") or 0.0) for item in trade_rows]
    return {
        "backtest_id": backtest_id,
        "start_date": str(row["start_date"]),
        "end_date": str(row["end_date"]),
        "snapshot_id": str(row.get("snapshot_id") or ""),
        "source_snapshot_id": str(parameters.get("source_snapshot_id") or ""),
        "stock_pool_hash": str(parameters.get("stock_pool_hash") or ""),
        "sector_membership_quality": str(
            parameters.get("sector_membership_quality") or "LIMITED"
        ),
        "sector_membership_source": str(
            parameters.get("sector_membership_source") or "snapshot"
        ),
        "cost_multiplier": multiplier,
        "trading_days": int(metrics.get("trading_days") or 0),
        "closed_trades": int(metrics.get("closed_trades") or len(trade_pnls)),
        "total_return": float(metrics.get("total_return") or 0.0),
        "annualized_return": float(metrics.get("annualized_return") or 0.0),
        "max_drawdown": float(metrics.get("max_drawdown") or 0.0),
        "win_rate": float(metrics.get("win_rate") or 0.0),
        "profit_factor": float(metrics.get("profit_factor") or 0.0),
        "median_trade_pnl": _median(trade_pnls),
        "ex_top3_trade_pnl": _ex_top_n(trade_pnls, 3),
        "_trade_pnls": trade_pnls,
    }


def _matching_run_id(
    database: Database,
    window: dict[str, Any],
    multiplier: float,
    *,
    preferred_id: str = "",
) -> str:
    candidates = database.query(
        """SELECT * FROM backtests WHERE strategy_id=? AND status='SUCCEEDED'
        AND start_date=? AND end_date=? ORDER BY finished_at DESC""",
        (STRATEGY_ID, str(window["start_date"]), str(window["end_date"])),
    )
    if preferred_id:
        candidates.sort(key=lambda item: str(item["backtest_id"]) != preferred_id)
    for row in candidates:
        parameters = _json_object(row.get("parameters_json"))
        versions = parameters.get("strategy_versions")
        if (
            isinstance(versions, dict)
            and versions.get(STRATEGY_ID) == STRATEGY_VERSION
            and parameters.get("components") == [STRATEGY_ID]
            and parameters.get("chan_replay_contract_version")
            == CHAN_REPLAY_CONTRACT_VERSION
            and str(parameters.get("source_backtest_id") or "")
            == str(window["source_backtest_id"])
            and str(parameters.get("source_snapshot_id") or "")
            == str(window["source_snapshot_id"])
            and str(row.get("snapshot_id") or "")
            == str(window["source_snapshot_id"])
            and abs(
                float(parameters.get("execution_cost_multiplier", 1.0)) - multiplier
            )
            <= 1e-12
        ):
            return str(row["backtest_id"])
    return ""


def _validate_protocol(protocol: dict[str, Any]) -> None:
    if protocol.get("strategy_id") != STRATEGY_ID:
        raise ValueError("Chan protocol has an unexpected strategy id")
    if protocol.get("strategy_version") != STRATEGY_VERSION:
        raise ValueError("Chan protocol has an unexpected strategy version")
    if protocol.get("chan_replay_contract_version") != CHAN_REPLAY_CONTRACT_VERSION:
        raise ValueError("Chan protocol has an unexpected replay contract version")
    windows = protocol.get("windows")
    if not isinstance(windows, list) or len(windows) != 5:
        raise ValueError("Chan protocol requires exactly five windows")
    required_window_fields = {
        "start_date",
        "end_date",
        "source_backtest_id",
        "source_snapshot_id",
    }
    if any(not required_window_fields.issubset(window) for window in windows):
        raise ValueError("Chan protocol window is incomplete")
    if not _non_overlapping_windows(windows):
        raise ValueError("Chan protocol windows must be ordered and non-overlapping")
    if not isinstance(protocol.get("cost_multipliers"), dict):
        raise ValueError("Chan protocol cost multipliers are missing")
    if not isinstance(protocol.get("historical_gates"), dict):
        raise ValueError("Chan protocol historical gates are missing")


def _non_overlapping_windows(windows: list[dict[str, Any]]) -> bool:
    previous_end = ""
    for window in windows:
        start = str(window["start_date"])
        end = str(window["end_date"])
        if start > end or (previous_end and start <= previous_end):
            return False
        previous_end = end
    return True


def _window_key(window: dict[str, Any]) -> str:
    return f"{window['start_date']}__{window['end_date']}"


def _load_manifest(path: Path, protocol_hash: str) -> dict[str, Any]:
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("protocol_sha256") == protocol_hash:
            payload.setdefault("runs", {})
            return payload
    return {
        "strategy_id": STRATEGY_ID,
        "protocol_sha256": protocol_hash,
        "created_at": datetime.now().astimezone().isoformat(),
        "updated_at": datetime.now().astimezone().isoformat(),
        "runs": {},
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_object(value: Any) -> dict[str, Any]:
    try:
        result = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("Stored Chan validation JSON is invalid") from exc
    return result if isinstance(result, dict) else {}


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
    return float(sum(sorted(values, reverse=True)[count:]))
