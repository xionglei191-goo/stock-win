from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .config import PlatformConfig, PortfolioConfig
from .leader_pullback_research import (
    FROZEN_HYPOTHESES,
    _add_point_in_time_features,
    _annotate_execution,
    _merge_bar_inputs,
    annotate_research_context,
    simulate_event_portfolio,
)
from .storage import Database, ParquetSnapshotStore


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "liquid_mainboard_panic_absorption_reversal"
FROZEN_PROTOCOL_SHA256 = "2a8c15d2cb6ee2491ca2dd298a255c05fcd944730549f28553a12479537bfd1d"


@dataclass(frozen=True)
class PanicWindow:
    label: str
    role: str
    backtest_id: str
    snapshot_id: str
    start_date: str
    end_date: str


WINDOWS = (
    PanicWindow(
        "dev_2021_2022",
        "DEVELOPMENT",
        "0a1df3159c534e33a897addefe5fae79",
        "bt_89d697919ea74826abe4a7702bd0a3e9",
        "2021-04-01",
        "2022-04-29",
    ),
    PanicWindow(
        "dev_2022_2023",
        "DEVELOPMENT",
        "24da8add0194458495df7bb45ddbfae7",
        "bt_4bec5474e50b44bdb53aff39bb4075ca",
        "2022-05-01",
        "2023-05-31",
    ),
    PanicWindow(
        "replication_2023_2024",
        "REPLICATION",
        "e40fe0fd8a2546729bbfe591b768c27a",
        "bt_e40fe0fd8a2546729bbfe591b768c27a",
        "2023-06-01",
        "2024-06-28",
    ),
    PanicWindow(
        "validation_2024_2025",
        "VALIDATION",
        "ed7e53baab44467d8a6c6ff12212ee0d",
        "bt_1f2378fe2c984617911770ccb742a05e",
        "2024-07-01",
        "2025-07-24",
    ),
    PanicWindow(
        "holdout_2025_2026",
        "HOLDOUT",
        "acce084944934e619167c972fdefbe8e",
        "bt_6b96520a77fb4ef68726988f55ef57c1",
        "2025-07-25",
        "2026-08-07",
    ),
)


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "development_only",
        "derivation": (
            "Prior trend pullbacks and single-day washout recovery were negative. This "
            "separate mechanism requires a multi-day panic plus same-day absorption, excludes "
            "recent limit-up stocks, and is evaluated only on two unopened hypothesis windows."
        ),
        "population": {
            "price_limit_regime": "main-board 10% only",
            "minimum_raw_close": 3.0,
            "minimum_prior_20day_average_amount_cny": 100_000_000.0,
            "exclude_st": True,
            "exclude_recent_limit_up_days": 20,
        },
        "signal_at_close": {
            "pre_panic_60day_return": [0.10, 0.80],
            "three_day_return": [-0.25, -0.12],
            "current_day_return": [-0.095, -0.02],
            "intraday_low_return_maximum": -0.07,
            "minimum_rebound_from_low": 0.04,
            "minimum_close_location": 0.70,
            "close_above_open": True,
            "current_volume_ratio": [1.20, 4.00],
            "close_above_ma120": True,
            "first_signal_in_episode_only": True,
            "market_gate": (
                "existing point-in-time course49 entry permission plus recovery, ferment, "
                "or healthy divergence"
            ),
        },
        "ranking": {
            "maximum_daily_candidates": 3,
            "recovery_quality": 0.35,
            "panic_depth_quality": 0.25,
            "volume_quality": 0.20,
            "liquidity_percentile": 0.20,
            "tie_break": "code ascending",
        },
        "execution": {
            "signal_time": "daily close",
            "entry": "next trading-day raw open",
            "allowed_open_gap": [-0.03, 0.05],
            "limit_up_open": "cancel without delay",
            "position_weight": 0.10,
            "maximum_positions": 3,
            "exit": "raw open after three observed holding sessions",
            "costs": "platform fixed slippage, commission, minimum commission, and stamp duty",
            "stress_cost_multiplier": 2.0,
        },
        "sequence": {
            "development_windows_open": [asdict(window) for window in WINDOWS[:2]],
            "replication_window_sealed": asdict(WINDOWS[2]),
            "validation_window_sealed": asdict(WINDOWS[3]),
            "holdout_window_sealed": asdict(WINDOWS[4]),
            "open_next_window_only_after_all_prior_gates": True,
        },
        "development_gates": {
            "minimum_portfolio_trades_each_window": 20,
            "minimum_portfolio_annualized_return_each_window": 0.02,
            "positive_total_return_each_window": True,
            "positive_median_trade_return_each_window": True,
            "positive_ex_top3_return_each_window": True,
            "maximum_realized_drawdown": -0.10,
            "minimum_fill_rate": 0.60,
            "double_cost_positive_total_median_and_ex_top3_each_window": True,
            "all_gates_required": True,
        },
        "later_gates": {
            "same_rules_and_costs": True,
            "minimum_replication_trades": 15,
            "replication_total_median_ex_top3_positive": True,
            "validation_and_holdout_each_profitable": True,
            "maximum_drawdown": -0.10,
            "minimum_fill_rate": 0.60,
            "maximum_daily_return_correlation_with_v9": 0.60,
            "combined_v9_r001_weighted_annualized_return": 0.40,
            "v9_trades_must_be_exact_and_unblocked": True,
        },
        "invariants": {
            "point_in_time_signals": True,
            "raw_prices_for_execution": True,
            "adjusted_prices_for_shape": True,
            "no_production_registration": True,
            "no_default_scan": True,
            "passing_is_not_automatic_promotion": True,
        },
        "known_limitations": [
            "Historical security eligibility and sector membership retain current-fallback bias.",
            "Event-portfolio drawdown is realized on exit days and understates holding-period drawdown.",
            "The first two windows have been used by other hypotheses, but not by this fixed interaction.",
            "No result can authorize production without sequential replication and holdout gates.",
        ],
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen panic-reversal protocol already differs: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def build_panic_reversal_events(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    maximum_daily_candidates: int = 3,
) -> pd.DataFrame:
    if execution_cost_multiplier <= 0.0:
        raise ValueError("execution_cost_multiplier must be positive")
    if maximum_daily_candidates <= 0:
        raise ValueError("maximum_daily_candidates must be positive")
    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_point_in_time_features(frame, FROZEN_HYPOTHESES)
    grouped = frame.groupby("code", sort=False)
    frame["ma120"] = grouped["adj_close"].transform(
        lambda values: values.rolling(120, min_periods=120).mean()
    )
    frame["return_3d"] = frame["adj_close"] / grouped["adj_close"].shift(3) - 1.0
    frame["pre_panic_return_60d"] = (
        grouped["adj_close"].shift(3) / grouped["adj_close"].shift(63) - 1.0
    )
    frame["current_volume_ratio"] = frame["adj_volume"] / frame["previous_volume_20"]
    frame["intraday_low_return"] = frame["raw_low"] / frame["raw_previous_close"] - 1.0
    frame["rebound_from_low"] = frame["raw_close"] / frame["raw_low"] - 1.0
    raw_range = frame["raw_high"] - frame["raw_low"]
    frame["close_location"] = (
        (frame["raw_close"] - frame["raw_low"]) / raw_range.replace(0.0, np.nan)
    )
    recent_limit = frame["latest_limit_age"].between(1, 20).fillna(False)
    signal = (
        frame["limit_ratio"].le(0.100001)
        & frame["raw_close"].ge(3.0)
        & frame["turnover_20d"].ge(100_000_000.0)
        & ~frame["name"].str.upper().str.contains("ST", regex=False, na=False)
        & ~recent_limit
        & frame["pre_panic_return_60d"].between(0.10, 0.80)
        & frame["return_3d"].between(-0.25, -0.12)
        & frame["current_return"].between(-0.095, -0.02)
        & frame["intraday_low_return"].le(-0.07)
        & frame["rebound_from_low"].ge(0.04)
        & frame["close_location"].ge(0.70)
        & frame["raw_close"].gt(frame["raw_open"])
        & frame["current_volume_ratio"].between(1.20, 4.00)
        & frame["adj_close"].gt(frame["ma120"])
    ).fillna(False)
    prior = signal.groupby(frame["code"], sort=False).shift(1).fillna(False)
    trigger = signal & ~prior.astype(bool)
    if not trigger.any():
        return pd.DataFrame()

    columns = [
        "code",
        "name",
        "timestamp",
        "raw_close",
        "adj_close",
        "limit_ratio",
        "turnover_20d",
        "entry_open",
        "entry_low",
        "entry_date",
        "exit_open_1d",
        "exit_date_1d",
        "exit_open_3d",
        "exit_date_3d",
        "exit_open_5d",
        "exit_date_5d",
    ]
    events = frame.loc[trigger, columns].copy()
    for field in (
        "pre_panic_return_60d",
        "return_3d",
        "current_return",
        "current_volume_ratio",
        "intraday_low_return",
        "rebound_from_low",
        "close_location",
        "ma120",
    ):
        events[field] = frame.loc[trigger, field].to_numpy()
    events.rename(columns={"timestamp": "signal_date"}, inplace=True)
    events["hypothesis_id"] = HYPOTHESIS_ID
    events["recovery_quality"] = (
        0.5 * ((events["close_location"] - 0.70) / 0.30).clip(0.0, 1.0)
        + 0.5 * ((events["rebound_from_low"] - 0.04) / 0.08).clip(0.0, 1.0)
    )
    events["panic_quality"] = ((-events["return_3d"] - 0.12) / 0.13).clip(0.0, 1.0)
    events["volume_quality"] = (
        1.0 - (events["current_volume_ratio"] - 2.0).abs() / 2.0
    ).clip(0.0, 1.0)
    events["liquidity_quality"] = events.groupby("signal_date", sort=False)[
        "turnover_20d"
    ].rank(method="average", pct=True)
    events["score"] = (
        0.35 * events["recovery_quality"]
        + 0.25 * events["panic_quality"]
        + 0.20 * events["volume_quality"]
        + 0.20 * events["liquidity_quality"]
    )
    events.sort_values(
        ["signal_date", "score", "code"], ascending=[True, False, True], inplace=True
    )
    events["daily_rank"] = events.groupby("signal_date", sort=False).cumcount() + 1
    events["selected"] = events["daily_rank"].le(maximum_daily_candidates)
    events = _annotate_execution(
        events,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
        entry_gap_min=-0.03,
        entry_gap_max=0.05,
    )
    events = annotate_research_context(
        events,
        market_states=market_states,
        sector_membership=None,
    )
    return events.sort_values(["signal_date", "daily_rank", "code"]).reset_index(
        drop=True
    )


def summarize_panic_events(events: pd.DataFrame, *, trading_days: int) -> dict[str, Any]:
    if events.empty:
        return _empty_summary()
    market = events.loc[events["market_gate"].fillna(False).astype(bool)].copy()
    market.sort_values(
        ["signal_date", "score", "code"], ascending=[True, False, True], inplace=True
    )
    market["market_rank"] = market.groupby("signal_date", sort=False).cumcount() + 1
    selected = market.loc[market["market_rank"].le(3)].copy()
    executable = selected.loc[selected["executable"]].copy()
    portfolio = simulate_event_portfolio(
        executable,
        trading_days=trading_days,
        target_weight=0.10,
        maximum_positions=3,
        holding_days=3,
    )
    returns = pd.to_numeric(executable["net_return_3d"], errors="coerce").dropna()
    attempted = int(len(selected))
    return {
        "raw_signals": int(len(events)),
        "market_signals": int(len(market)),
        "selected_signals": attempted,
        "executable_signals": int(len(executable)),
        "blocked_limit_up_open": int(selected["blocked_limit_up_open"].sum()),
        "blocked_open_gap": int(selected["blocked_open_gap"].sum()),
        "blocked_missing_bars": int(selected["blocked_missing_bars"].sum()),
        "fill_rate": float(len(executable) / attempted) if attempted else 0.0,
        "median_executable_return_3d": (
            float(returns.median()) if not returns.empty else None
        ),
        "mean_executable_return_3d": float(returns.mean()) if not returns.empty else None,
        "win_rate_3d": float((returns > 0.0).mean()) if not returns.empty else None,
        **portfolio,
    }


def evaluate_window(
    config: PlatformConfig,
    database: Database,
    window: PanicWindow,
    *,
    execution_cost_multiplier: float,
) -> tuple[dict[str, Any], pd.DataFrame]:
    snapshots = ParquetSnapshotStore(config, database)
    front = snapshots.load_records(window.snapshot_id, "daily_front")
    raw = snapshots.load_records(window.snapshot_id, "daily_raw")
    master = snapshots.load_records(window.snapshot_id, "security_master")
    states = pd.DataFrame(
        database.query(
            """SELECT timestamp, market_phase, market_style, entry_allowed, state_json
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id IN ('course49_system', 'course49_v9')
               ORDER BY timestamp""",
            (window.backtest_id,),
        )
    )
    if states.empty:
        raise ValueError(f"Backtest {window.backtest_id} has no course49 states")
    names = {
        str(row.code): str(row.name)
        for row in master.loc[:, ["code", "name"]].itertuples(index=False)
    }
    events = build_panic_reversal_events(
        front,
        raw,
        names,
        market_states=states,
        execution_config=config.portfolio,
        execution_cost_multiplier=execution_cost_multiplier,
    )
    start = pd.Timestamp(window.start_date).normalize()
    end = pd.Timestamp(window.end_date).normalize()
    if not events.empty:
        events = events.loc[
            pd.to_datetime(events["signal_date"]).between(start, end)
        ].reset_index(drop=True)
    trading_days = int(
        pd.to_datetime(states["timestamp"]).dt.normalize().between(start, end).sum()
    )
    report = {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "window": asdict(window),
        "execution_cost_multiplier": float(execution_cost_multiplier),
        "data_quality": {
            "daily_front_rows": int(len(front)),
            "daily_raw_rows": int(len(raw)),
            "daily_codes": int(front["code"].nunique()),
            "state_days": trading_days,
            "front_duplicate_keys": int(front.duplicated(["code", "timestamp"]).sum()),
            "raw_duplicate_keys": int(raw.duplicated(["code", "timestamp"]).sum()),
        },
        "summary": summarize_panic_events(events, trading_days=trading_days),
    }
    return report, events


def assess_development(
    base_reports: Iterable[Mapping[str, Any]],
    stress_reports: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    base = list(base_reports)
    stress = list(stress_reports)
    if len(base) != 2 or len(stress) != 2:
        raise ValueError("Exactly two base and two stress development reports are required")
    windows = []
    all_checks: list[bool] = []
    for base_report, stress_report in zip(base, stress, strict=True):
        base_summary = dict(base_report["summary"])
        stress_summary = dict(stress_report["summary"])
        checks = {
            "minimum_sample": int(base_summary["portfolio_trades"]) >= 20,
            "minimum_annualized_return": (
                float(base_summary["portfolio_annualized_return"]) >= 0.02
            ),
            "positive_total_return": float(base_summary["portfolio_total_return"]) > 0.0,
            "positive_median": (
                base_summary["portfolio_median_trade_return"] is not None
                and float(base_summary["portfolio_median_trade_return"]) > 0.0
            ),
            "positive_ex_top3": (
                float(base_summary["portfolio_ex_top3_total_return"]) > 0.0
            ),
            "drawdown_within_ten_percent": (
                float(base_summary["portfolio_realized_max_drawdown"]) >= -0.10
            ),
            "fill_rate_at_least_sixty_percent": float(base_summary["fill_rate"]) >= 0.60,
            "double_cost_positive_total": (
                float(stress_summary["portfolio_total_return"]) > 0.0
            ),
            "double_cost_positive_median": (
                stress_summary["portfolio_median_trade_return"] is not None
                and float(stress_summary["portfolio_median_trade_return"]) > 0.0
            ),
            "double_cost_positive_ex_top3": (
                float(stress_summary["portfolio_ex_top3_total_return"]) > 0.0
            ),
        }
        all_checks.extend(checks.values())
        windows.append(
            {
                "label": str(base_report["window"]["label"]),
                "base": base_summary,
                "double_cost": stress_summary,
                "checks": checks,
                "qualified": all(checks.values()),
            }
        )
    qualified = all(all_checks)
    return {
        "decision": "OPEN_REPLICATION" if qualified else "REJECT",
        "development_qualified": qualified,
        "replication_opened": False,
        "validation_opened": False,
        "holdout_opened": False,
        "production_authorized": False,
        "windows": windows,
    }


def run_frozen_development(
    config: PlatformConfig,
    database: Database,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    actual_hash = _file_sha256(output_dir / "protocol.json")
    if actual_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen panic-reversal protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={actual_hash}"
        )
    base_reports: list[dict[str, Any]] = []
    stress_reports: list[dict[str, Any]] = []
    for window in WINDOWS[:2]:
        base_report, events = evaluate_window(
            config, database, window, execution_cost_multiplier=1.0
        )
        stress_report, _ = evaluate_window(
            config, database, window, execution_cost_multiplier=2.0
        )
        base_reports.append(base_report)
        stress_reports.append(stress_report)
        (output_dir / f"{window.label}_result.json").write_text(
            json.dumps(base_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output_dir / f"{window.label}_double_cost_result.json").write_text(
            json.dumps(stress_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        events.to_parquet(output_dir / f"{window.label}_events.parquet", index=False)
    decision = assess_development(base_reports, stress_reports)
    payload = {"protocol_sha256": actual_hash, **decision}
    (output_dir / "development_decision.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _empty_summary() -> dict[str, Any]:
    return {
        "raw_signals": 0,
        "market_signals": 0,
        "selected_signals": 0,
        "executable_signals": 0,
        "blocked_limit_up_open": 0,
        "blocked_open_gap": 0,
        "blocked_missing_bars": 0,
        "fill_rate": 0.0,
        "median_executable_return_3d": None,
        "mean_executable_return_3d": None,
        "win_rate_3d": None,
        "portfolio_trades": 0,
        "portfolio_total_return": 0.0,
        "portfolio_annualized_return": 0.0,
        "portfolio_realized_max_drawdown": 0.0,
        "portfolio_ex_top3_total_return": 0.0,
        "portfolio_median_trade_return": None,
    }


def _file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
