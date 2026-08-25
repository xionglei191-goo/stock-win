from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy_v1.chan import analyze_chan

from .config import PlatformConfig, PortfolioConfig
from .equity_etf_reversal_research import simulate_mark_to_market_portfolio
from .storage import Database, ParquetSnapshotStore


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "chan_center_breakout_pullback_reclaim"
FROZEN_PROTOCOL_SHA256 = "30ad0746d32cc73aa09c281cd78524a7fa1b8f85db8f42c1b2339819bba4b02e"


@dataclass(frozen=True)
class DevelopmentWindow:
    label: str
    start_date: str
    end_date: str
    snapshot_id: str


@dataclass(frozen=True)
class ChanAnchor:
    position: int
    timestamp: pd.Timestamp
    center_lower: float
    center_upper: float


DEVELOPMENT_WINDOWS = (
    DevelopmentWindow(
        "dev_2021_2022",
        "2021-04-01",
        "2022-04-29",
        "bt_89d697919ea74826abe4a7702bd0a3e9",
    ),
    DevelopmentWindow(
        "dev_2022_2023",
        "2022-05-01",
        "2023-05-31",
        "bt_4bec5474e50b44bdb53aff39bb4075ca",
    ),
    DevelopmentWindow(
        "dev_2023_2024",
        "2023-06-01",
        "2024-06-28",
        "bt_e40fe0fd8a2546729bbfe591b768c27a",
    ),
)


AnchorDetector = Callable[[pd.DataFrame, int], ChanAnchor | None]


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "development_only",
        "relationship_to_production": "independent research; never loaded by scan",
        "population": {
            "definition": "A-share common stocks present in each immutable snapshot",
            "excluded": ["ST names", "delisting names", "less than 120 sessions"],
            "known_bias": "snapshot universe and sector context are not survivor-complete point-in-time data",
            "promotion_block": "point-in-time listed and delisted universe audit is required",
        },
        "data": {
            "signal_prices": "front-adjusted daily bars",
            "execution_prices": "unadjusted daily bars",
            "development_snapshot_ids": [window.snapshot_id for window in DEVELOPMENT_WINDOWS],
            "replication_window": ["2024-07-01", "2025-07-24"],
            "holdout_window": ["2025-07-25", "2026-08-07"],
            "replication_and_holdout_snapshot_ids_intentionally_absent": True,
        },
        "signal": {
            "signal_time": "daily close",
            "minimum_history_sessions": 120,
            "market_filter": "Shanghai index close above MA120",
            "minimum_amount_20d": 50_000_000,
            "minimum_return_60d": 0.10,
            "close_above_ma20": True,
            "chan_min_bar_distance": 5,
            "breakout_age_sessions": [2, 8],
            "peak_age_sessions": [2, 8],
            "drawdown_from_post_breakout_peak": [0.02, 0.10],
            "center_touch_band": ["center_lower * 0.99", "center_upper * 1.03"],
            "requires_all_closes_above_center_lower": True,
            "requires_close_reclaim_center_upper": True,
            "maximum_volume_ratio_to_prior_20d": 0.80,
            "confirmation": "bullish candle, close above prior close, daily return in (0, 0.05]",
            "repeat_signal": "first qualifying reclaim for each breakout anchor",
        },
        "ranking": {
            "center_proximity": 0.35,
            "return_60d": 0.30,
            "volume_contraction": 0.20,
            "liquidity": 0.15,
            "maximum_daily_candidates": 3,
            "tie_break": "code ascending",
        },
        "execution": {
            "entry": "next trading-day unadjusted open",
            "entry_gap_bounds": [-0.03, 0.05],
            "limit_up_open": "cancel",
            "target_weight": 0.10,
            "maximum_positions": 3,
            "stop": "higher of signal close minus 5% and pullback low minus 1%",
            "exits": [
                "fixed stop",
                "close below center lower",
                "market below MA120",
                "after 8% profit, 4% close drawdown",
                "maximum 10 holding sessions",
            ],
            "exit_execution": "next trading-day unadjusted open",
            "t_plus_one": True,
            "costs": "commission, minimum commission, stamp duty, fixed slippage",
        },
        "development_gate": {
            "minimum_trades_per_window": 30,
            "minimum_annualized_return_per_window": 0.05,
            "positive_total_return": True,
            "positive_median_trade": True,
            "positive_ex_top3_contribution": True,
            "maximum_mark_to_market_drawdown": -0.10,
            "minimum_fill_rate": 0.60,
            "all_development_windows_must_pass": True,
            "passing_action": "freeze candidate, audit survivor bias, then open replication only",
        },
        "development_windows": [asdict(window) for window in DEVELOPMENT_WINDOWS],
        "opening_rule": "development, survivor audit, replication, then holdout",
    }


def save_protocol(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "protocol.json"
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"Frozen protocol already exists with different content: {path}")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}


def load_development_window(
    config: PlatformConfig,
    database: Database,
    window: DevelopmentWindow,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, str], dict[str, Any]]:
    if window not in DEVELOPMENT_WINDOWS:
        raise ValueError("Only preregistered development windows may be loaded")
    store = ParquetSnapshotStore(config, database)
    front = store.load_records(window.snapshot_id, "daily_front")
    raw = store.load_records(window.snapshot_id, "daily_raw")
    market = store.load_records(window.snapshot_id, "market_index")
    security = store.load_records(window.snapshot_id, "security_master")
    names = dict(security.loc[:, ["code", "name"]].itertuples(index=False, name=None))
    quality = _validate_window_inputs(front, raw, market, window)
    quality["snapshot_id"] = window.snapshot_id
    quality["daily_front_hash"] = _file_sha256(
        config.snapshot_dir / window.snapshot_id / "daily_front.parquet"
    )
    quality["daily_raw_hash"] = _file_sha256(
        config.snapshot_dir / window.snapshot_id / "daily_raw.parquet"
    )
    return front, raw, market, names, quality


def build_chan_pullback_events(
    daily_front: pd.DataFrame,
    daily_raw: pd.DataFrame,
    market_index: pd.DataFrame,
    names: Mapping[str, str],
    *,
    start_date: str,
    end_date: str,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    anchor_detector: AnchorDetector | None = None,
) -> pd.DataFrame:
    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    frame, market = _prepare_features(daily_front, daily_raw, market_index, names)
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    candidates: list[dict[str, Any]] = []
    detector = anchor_detector
    for code, history in frame.groupby("code", sort=True):
        history = history.reset_index(drop=True)
        coarse = (
            history["timestamp"].between(start, end)
            & history["market_allowed"].fillna(False)
            & history["amount_20d"].ge(50_000_000.0)
            & history["return_60d"].ge(0.10)
            & history["Close"].gt(history["ma20"])
            & history["drawdown_8d"].between(-0.10, -0.02)
            & history["volume_ratio"].le(0.80)
            & history["Close"].gt(history["Open"])
            & history["Close"].gt(history["previous_close"])
            & history["daily_return"].gt(0.0)
            & history["daily_return"].le(0.05)
        ).fillna(False)
        emitted_anchors: set[pd.Timestamp] = set()
        cache: dict[int, ChanAnchor | None] = {}
        front_history = history.loc[:, ["Open", "High", "Low", "Close", "Volume"]].copy()
        front_history.index = pd.DatetimeIndex(history["timestamp"])
        for signal_position in np.flatnonzero(coarse.to_numpy()):
            anchor = (
                detector(front_history, int(signal_position))
                if detector is not None
                else _find_recent_chan_anchor(front_history, int(signal_position), cache)
            )
            if anchor is None or anchor.timestamp in emitted_anchors:
                continue
            setup = evaluate_pullback_setup(history, int(signal_position), anchor)
            if setup is None:
                continue
            emitted_anchors.add(anchor.timestamp)
            candidates.append(setup)
    if not candidates:
        return _empty_events()
    events = pd.DataFrame(candidates)
    events["liquidity_quality"] = events.groupby("signal_date", sort=False)[
        "amount_20d"
    ].rank(method="average", pct=True)
    events["score"] = (
        0.35 * events["center_proximity_quality"]
        + 0.30 * events["strength_quality"]
        + 0.20 * events["volume_quality"]
        + 0.15 * events["liquidity_quality"]
    )
    events.sort_values(
        ["signal_date", "score", "code"], ascending=[True, False, True], inplace=True
    )
    events["daily_rank"] = events.groupby("signal_date", sort=False).cumcount() + 1
    events["selected"] = events["daily_rank"].le(3)
    events = _annotate_execution(
        events,
        frame,
        market,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
    )
    return events.sort_values(
        ["signal_date", "score", "code"], ascending=[True, False, True]
    ).reset_index(drop=True)


def evaluate_pullback_setup(
    history: pd.DataFrame,
    signal_position: int,
    anchor: ChanAnchor,
) -> dict[str, Any] | None:
    if signal_position - anchor.position not in range(2, 9):
        return None
    path = history.iloc[anchor.position : signal_position + 1]
    pre_signal = history.iloc[anchor.position:signal_position]
    if pre_signal.empty:
        return None
    peak_position = int(pre_signal["Close"].idxmax())
    peak_age = signal_position - peak_position
    signal = history.iloc[signal_position]
    peak_close = float(history.at[peak_position, "Close"])
    drawdown = float(signal["Close"] / peak_close - 1.0)
    pullback_low = float(path["Low"].min())
    if not (
        2 <= peak_age <= 8
        and -0.10 <= drawdown <= -0.02
        and pullback_low <= anchor.center_upper * 1.03
        and pullback_low >= anchor.center_lower * 0.99
        and path["Close"].ge(anchor.center_lower).all()
        and float(signal["Close"]) >= anchor.center_upper
    ):
        return None
    raw_pullback_low = float(path["raw_Low"].min())
    raw_signal_close = float(signal["raw_Close"])
    volume_ratio = float(signal["volume_ratio"])
    return_60d = float(signal["return_60d"])
    center_distance = max(0.0, float(signal["Close"] / anchor.center_upper - 1.0))
    return {
        "code": str(signal["code"]),
        "name": str(signal["name"]),
        "signal_date": pd.Timestamp(signal["timestamp"]).normalize(),
        "anchor_date": anchor.timestamp,
        "anchor_age": int(signal_position - anchor.position),
        "peak_age": int(peak_age),
        "center_lower": float(anchor.center_lower),
        "center_upper": float(anchor.center_upper),
        "front_signal_close": float(signal["Close"]),
        "raw_signal_close": raw_signal_close,
        "raw_pullback_low": raw_pullback_low,
        "stop_price": max(raw_signal_close * 0.95, raw_pullback_low * 0.99),
        "drawdown_from_peak": drawdown,
        "volume_ratio": volume_ratio,
        "return_60d": return_60d,
        "amount_20d": float(signal["amount_20d"]),
        "center_proximity_quality": float(np.clip(1.0 - center_distance / 0.03, 0.0, 1.0)),
        "strength_quality": float(np.clip(return_60d / 0.50, 0.0, 1.0)),
        "volume_quality": float(np.clip((0.80 - volume_ratio) / 0.60, 0.0, 1.0)),
    }


def evaluate_development_window(
    events: pd.DataFrame,
    daily_raw: pd.DataFrame,
    market_index: pd.DataFrame,
    window: DevelopmentWindow,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    start = pd.Timestamp(window.start_date).normalize()
    end = pd.Timestamp(window.end_date).normalize()
    scoped = events.loc[pd.to_datetime(events["signal_date"]).between(start, end)].copy()
    selected = scoped.loc[scoped["selected"]]
    complete = selected.loc[
        selected["executable"] & pd.to_datetime(selected["exit_date"]).le(end)
    ].copy()
    calendar = market_index.copy()
    calendar["timestamp"] = pd.to_datetime(calendar["timestamp"]).dt.normalize()
    calendar = calendar.loc[calendar["timestamp"].between(start, end)]
    portfolio = simulate_mark_to_market_portfolio(
        complete,
        daily_raw,
        calendar["timestamp"].tolist(),
        config=execution_config or PortfolioConfig(),
        cost_multiplier=execution_cost_multiplier,
    )
    accepted_returns = pd.Series(portfolio.pop("accepted_trade_returns"), dtype=float)
    ex_top3 = accepted_returns.sort_values(ascending=False).iloc[3:]
    attempted = int(len(selected))
    return {
        "window": {**asdict(window), "role": "DEVELOPMENT"},
        "raw_signals": int(len(scoped)),
        "selected_signals": attempted,
        "executable_signals": int(len(complete)),
        "blocked_daily_capacity": int((~scoped["selected"]).sum()),
        "blocked_limit_up_open": int(selected["blocked_limit_up_open"].sum()),
        "blocked_entry_gap": int(selected["blocked_entry_gap"].sum()),
        "blocked_missing_bars": int(
            selected[["blocked_missing_entry", "blocked_missing_exit"]].any(axis=1).sum()
        ),
        "blocked_portfolio": int(len(complete) - portfolio["portfolio_trades"]),
        "fill_rate": float(len(complete) / attempted) if attempted else 0.0,
        "median_trade_return": (
            float(accepted_returns.median()) if not accepted_returns.empty else None
        ),
        "mean_trade_return": (
            float(accepted_returns.mean()) if not accepted_returns.empty else None
        ),
        "win_rate": (
            float((accepted_returns > 0.0).mean()) if not accepted_returns.empty else None
        ),
        "ex_top3_contribution": float(ex_top3.sum() * 0.10),
        **portfolio,
    }


def assess_development(reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    reports = list(reports)
    if not reports or len(reports) > len(DEVELOPMENT_WINDOWS):
        raise ValueError("Between one and three ordered development reports are required")
    checks: list[dict[str, Any]] = []
    for report in reports:
        window_checks = _development_window_checks(report)
        checks.append(
            {
                "window": str(report["window"]["label"]),
                "checks": window_checks,
                "passed": all(window_checks.values()),
            }
        )
    failed = any(not item["passed"] for item in checks)
    complete = len(checks) == len(DEVELOPMENT_WINDOWS)
    passed = complete and not failed
    decision = (
        "REJECT"
        if failed
        else "REQUIRE_SURVIVOR_AUDIT"
        if passed
        else "CONTINUE_DEVELOPMENT"
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "decision": decision,
        "development_qualified": passed,
        "checks": checks,
        "survivor_audit_required": passed,
        "replication_opened": False,
        "holdout_opened": False,
        "evaluated_development_windows": [item["window"] for item in checks],
        "unopened_development_windows": [
            window.label for window in DEVELOPMENT_WINDOWS[len(checks) :]
        ],
        "early_stopped": failed and not complete,
    }


def run_frozen_development(
    config: PlatformConfig,
    database: Database,
    *,
    output_dir: Path,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    protocol_hash = _file_sha256(output_dir / "protocol.json")
    if protocol_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen Chan pullback protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={protocol_hash}"
        )
    reports: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []
    event_frames: list[pd.DataFrame] = []
    for window in DEVELOPMENT_WINDOWS:
        front, raw, market, names, window_quality = load_development_window(
            config, database, window
        )
        events = build_chan_pullback_events(
            front,
            raw,
            market,
            names,
            start_date=window.start_date,
            end_date=window.end_date,
            execution_config=config.portfolio,
            execution_cost_multiplier=execution_cost_multiplier,
        )
        events["window_label"] = window.label
        event_frames.append(events)
        reports.append(
            evaluate_development_window(
                events,
                raw,
                market,
                window,
                execution_config=config.portfolio,
                execution_cost_multiplier=execution_cost_multiplier,
            )
        )
        quality.append(window_quality)
        if not all(_development_window_checks(reports[-1]).values()):
            break
    all_events = pd.concat(event_frames, ignore_index=True) if event_frames else _empty_events()
    decision = assess_development(reports)
    events_path = output_dir / "development_events.parquet"
    all_events.to_parquet(events_path, index=False)
    payload = {
        "protocol_sha256": protocol_hash,
        "data_quality": quality,
        "reports": reports,
        "decision": decision,
        "events_sha256": _file_sha256(events_path),
    }
    result_path = output_dir / "development_result.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return payload


def _find_recent_chan_anchor(
    history: pd.DataFrame,
    signal_position: int,
    cache: dict[int, ChanAnchor | None],
) -> ChanAnchor | None:
    for position in range(signal_position - 2, max(-1, signal_position - 9), -1):
        if position < 119:
            continue
        if position not in cache:
            state = analyze_chan(history.iloc[: position + 1])
            cache[position] = (
                ChanAnchor(
                    position,
                    pd.Timestamp(history.index[position]).normalize(),
                    float(state.center.lower),
                    float(state.center.upper),
                )
                if state.breakout and state.center is not None
                else None
            )
        if cache[position] is not None:
            return cache[position]
    return None


def _development_window_checks(report: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "minimum_trades": int(report["portfolio_trades"]) >= 30,
        "minimum_annualized_return": float(report["portfolio_annualized_return"]) >= 0.05,
        "positive_total_return": float(report["portfolio_total_return"]) > 0.0,
        "positive_median_trade": (
            report["median_trade_return"] is not None
            and float(report["median_trade_return"]) > 0.0
        ),
        "positive_ex_top3_contribution": float(report["ex_top3_contribution"]) > 0.0,
        "maximum_drawdown": float(report["portfolio_max_drawdown"]) >= -0.10,
        "minimum_fill_rate": float(report["fill_rate"]) >= 0.60,
    }


def _prepare_features(
    daily_front: pd.DataFrame,
    daily_raw: pd.DataFrame,
    market_index: pd.DataFrame,
    names: Mapping[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = {"code", "timestamp", "Open", "High", "Low", "Close", "Volume", "Amount"}
    for label, value in (("daily_front", daily_front), ("daily_raw", daily_raw)):
        missing = required - set(value.columns)
        if missing:
            raise ValueError(f"{label} is missing columns: {sorted(missing)}")
    front = daily_front.loc[:, sorted(required)].copy()
    raw = daily_raw.loc[:, sorted(required)].copy()
    for value in (front, raw):
        value["timestamp"] = pd.to_datetime(value["timestamp"], errors="coerce").dt.normalize()
        value.dropna(subset=["timestamp"], inplace=True)
    raw.rename(
        columns={column: f"raw_{column}" for column in required - {"code", "timestamp"}},
        inplace=True,
    )
    frame = front.merge(raw, on=["code", "timestamp"], how="inner", validate="one_to_one")
    frame["name"] = frame["code"].map(names).fillna("")
    allowed_name = ~frame["name"].str.upper().str.contains("ST", regex=False)
    allowed_name &= ~frame["name"].str.contains("\u9000", regex=False)
    frame = frame.loc[allowed_name].sort_values(["code", "timestamp"]).reset_index(drop=True)
    grouped = frame.groupby("code", sort=False)
    frame["previous_close"] = grouped["Close"].shift(1)
    frame["daily_return"] = frame["Close"] / frame["previous_close"] - 1.0
    frame["return_60d"] = frame["Close"] / grouped["Close"].shift(60) - 1.0
    frame["ma20"] = grouped["Close"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    frame["amount_20d"] = grouped["raw_Amount"].transform(
        lambda values: values.shift(1).rolling(20, min_periods=20).mean()
    )
    prior_volume = grouped["Volume"].transform(
        lambda values: values.shift(1).rolling(20, min_periods=20).mean()
    )
    frame["volume_ratio"] = frame["Volume"] / prior_volume.replace(0.0, np.nan)
    prior_peak = grouped["Close"].transform(
        lambda values: values.shift(1).rolling(8, min_periods=2).max()
    )
    frame["drawdown_8d"] = frame["Close"] / prior_peak - 1.0

    market = market_index.copy()
    market["timestamp"] = pd.to_datetime(market["timestamp"], errors="coerce").dt.normalize()
    market.dropna(subset=["timestamp"], inplace=True)
    market.sort_values("timestamp", inplace=True)
    market = market.drop_duplicates("timestamp", keep="first")
    market["market_close"] = pd.to_numeric(market["Close"], errors="coerce")
    market["market_ma120"] = market["market_close"].rolling(120, min_periods=120).mean()
    market["market_allowed"] = market["market_close"].gt(market["market_ma120"])
    frame = frame.merge(
        market.loc[:, ["timestamp", "market_close", "market_ma120", "market_allowed"]],
        on="timestamp",
        how="left",
        validate="many_to_one",
    )
    return frame, market


def _annotate_execution(
    events: pd.DataFrame,
    feature_frame: pd.DataFrame,
    market: pd.DataFrame,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> pd.DataFrame:
    result = events.copy()
    for column in (
        "blocked_missing_entry",
        "blocked_limit_up_open",
        "blocked_entry_gap",
        "blocked_missing_exit",
        "executable",
    ):
        result[column] = False
    for column in ("entry_open", "entry_gap", "exit_open", "net_return"):
        result[column] = np.nan
    result["entry_date"] = pd.NaT
    result["exit_date"] = pd.NaT
    result["exit_reason"] = ""
    result["holding_sessions"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    result["quantity"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    market_allowed = dict(
        market.loc[:, ["timestamp", "market_allowed"]].itertuples(index=False, name=None)
    )
    selected = result.loc[result["selected"]]
    for code, indexes in selected.groupby("code", sort=False).groups.items():
        history = feature_frame.loc[feature_frame["code"].eq(code)].reset_index(drop=True)
        positions = {pd.Timestamp(value).normalize(): idx for idx, value in history["timestamp"].items()}
        for event_index in indexes:
            signal_date = pd.Timestamp(result.at[event_index, "signal_date"]).normalize()
            signal_position = positions.get(signal_date)
            if signal_position is None or signal_position + 1 >= len(history):
                result.at[event_index, "blocked_missing_entry"] = True
                continue
            signal = history.iloc[signal_position]
            entry_position = signal_position + 1
            entry = history.iloc[entry_position]
            entry_open = float(entry["raw_Open"])
            signal_close = float(result.at[event_index, "raw_signal_close"])
            entry_gap = entry_open / signal_close - 1.0
            result.at[event_index, "entry_date"] = pd.Timestamp(entry["timestamp"])
            result.at[event_index, "entry_open"] = entry_open
            result.at[event_index, "entry_gap"] = entry_gap
            limit_rate = _limit_rate(str(code))
            limit_price = float(signal["raw_Close"]) * (1.0 + limit_rate)
            if entry_open >= limit_price * 0.998 and float(entry["raw_Low"]) >= limit_price * 0.998:
                result.at[event_index, "blocked_limit_up_open"] = True
                continue
            if not -0.03 <= entry_gap <= 0.05:
                result.at[event_index, "blocked_entry_gap"] = True
                continue
            highest_close = entry_open
            exit_position: int | None = None
            exit_reason = ""
            maximum_observation = min(entry_position + 10, len(history))
            for position in range(entry_position, maximum_observation):
                observed = history.iloc[position]
                close = float(observed["raw_Close"])
                highest_close = max(highest_close, close)
                if close <= float(result.at[event_index, "stop_price"]):
                    exit_reason = "FIXED_STOP_CLOSE"
                elif float(observed["Close"]) < float(result.at[event_index, "center_lower"]):
                    exit_reason = "CENTER_BREAKDOWN"
                elif not bool(market_allowed.get(pd.Timestamp(observed["timestamp"]), False)):
                    exit_reason = "MARKET_WEAK"
                elif highest_close >= entry_open * 1.08 and close <= highest_close * 0.96:
                    exit_reason = "TRAILING_PROFIT"
                elif position - entry_position + 1 >= 10:
                    exit_reason = "MAX_HOLDING"
                if exit_reason:
                    exit_position = position + 1
                    break
            if exit_position is None:
                exit_position = entry_position + 10
                exit_reason = "MAX_HOLDING"
            if exit_position >= len(history):
                result.at[event_index, "blocked_missing_exit"] = True
                continue
            exit_bar = history.iloc[exit_position]
            quantity, net_return = _trade_quantity_and_return(
                entry_open, float(exit_bar["raw_Open"]), config, cost_multiplier
            )
            if quantity <= 0 or not np.isfinite(net_return):
                result.at[event_index, "blocked_missing_entry"] = True
                continue
            result.at[event_index, "exit_date"] = pd.Timestamp(exit_bar["timestamp"])
            result.at[event_index, "exit_open"] = float(exit_bar["raw_Open"])
            result.at[event_index, "exit_reason"] = exit_reason
            result.at[event_index, "holding_sessions"] = int(exit_position - entry_position)
            result.at[event_index, "quantity"] = int(quantity)
            result.at[event_index, "net_return"] = float(net_return)
            result.at[event_index, "executable"] = True
    return result


def _trade_quantity_and_return(
    entry_open: float,
    exit_open: float,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> tuple[int, float]:
    buy_price = entry_open * (1.0 + config.slippage_rate * cost_multiplier)
    quantity = int(
        (config.initial_cash * 0.10) // (buy_price * config.board_lot)
    ) * config.board_lot
    if quantity <= 0:
        return 0, np.nan
    buy_value = buy_price * quantity
    buy_fee = max(
        config.min_commission * cost_multiplier,
        buy_value * config.commission_rate * cost_multiplier,
    )
    sell_price = exit_open * (1.0 - config.slippage_rate * cost_multiplier)
    sell_value = sell_price * quantity
    sell_fee = max(
        config.min_commission * cost_multiplier,
        sell_value * config.commission_rate * cost_multiplier,
    ) + sell_value * config.stamp_duty_rate * cost_multiplier
    return quantity, float((sell_value - sell_fee - buy_value - buy_fee) / (buy_value + buy_fee))


def _validate_window_inputs(
    front: pd.DataFrame,
    raw: pd.DataFrame,
    market: pd.DataFrame,
    window: DevelopmentWindow,
) -> dict[str, Any]:
    for label, value in (("daily_front", front), ("daily_raw", raw)):
        if value.duplicated(["code", "timestamp"]).any():
            raise ValueError(f"{label} contains duplicate code-session keys")
    front_keys = pd.MultiIndex.from_frame(front.loc[:, ["code", "timestamp"]])
    raw_keys = pd.MultiIndex.from_frame(raw.loc[:, ["code", "timestamp"]])
    if not front_keys.equals(raw_keys):
        raise ValueError("daily front/raw keys do not match")
    market_dates = pd.to_datetime(market["timestamp"], errors="coerce")
    if not market_dates.between(window.start_date, window.end_date).any():
        raise ValueError(f"market index does not cover {window.label}")
    return {
        "window": window.label,
        "front_rows": int(len(front)),
        "raw_rows": int(len(raw)),
        "codes": int(front["code"].nunique()),
        "front_duplicate_keys": 0,
        "raw_duplicate_keys": 0,
        "market_rows": int(len(market)),
    }


def _limit_rate(code: str) -> float:
    local = str(code).split(".", 1)[0]
    return 0.20 if local.startswith(("300", "301", "688")) else 0.10


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "code",
            "name",
            "signal_date",
            "score",
            "daily_rank",
            "selected",
            "blocked_missing_entry",
            "blocked_limit_up_open",
            "blocked_entry_gap",
            "blocked_missing_exit",
            "executable",
            "entry_date",
            "entry_open",
            "entry_gap",
            "exit_date",
            "exit_open",
            "exit_reason",
            "holding_sessions",
            "quantity",
            "net_return",
        ]
    )


def _file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
