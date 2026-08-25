from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy_v1.portfolio import price_limit_ratio

from .config import PlatformConfig, PortfolioConfig
from .storage import Database, ParquetSnapshotStore


RESEARCH_SCHEMA_VERSION = 1
ALLOWED_THEME_PHASES = frozenset({"START", "FERMENT", "DIVERGENCE"})
DIRECT_MARKET_PHASES = frozenset({"RECOVERY", "FERMENT"})
WEAK_MARKET_REGIMES = frozenset({"WEAK", "ICE", "EXTREME_WEAK"})
RETURN_HORIZONS = (1, 3, 5)
MATCHING_PROTOCOL_VERSION = "1.0.0"
LIMIT_ENTRY_PROTOCOL_VERSION = "1.0.0"
LIMIT_ENTRY_HYPOTHESIS_ID = "reclaim_limit_3pct"
LIMIT_ENTRY_DISCOUNT = 0.03
EXHAUSTION_PROTOCOL_VERSION = "1.0.0"
EXHAUSTION_HYPOTHESIS_ID = "two_day_pullback_exhaustion"
TREND_RSI2_PROTOCOL_VERSION = "1.0.0"
TREND_RSI2_HYPOTHESIS_ID = "trend_rsi2_oversold"
MA20_BOUNCE_PROTOCOL_VERSION = "1.0.0"
MA20_BOUNCE_HYPOTHESIS_ID = "ma20_low_vol_bounce"
WASHOUT_PROTOCOL_VERSION = "1.0.0"
WASHOUT_HYPOTHESIS_ID = "intraday_washout_reversal"
MATCHING_FEATURE_SCALES = {
    "return_20d": 0.10,
    "pullback_depth": 0.03,
    "pullback_volume_ratio": 0.25,
    "volatility_20d": 0.02,
    "log_turnover_20d": 1.00,
    "peak_age": 3.00,
    "distance_to_ma10": 0.03,
}


@dataclass(frozen=True)
class PullbackHypothesis:
    hypothesis_id: str
    name: str
    recent_limit_days: int
    minimum_return_20d: float
    minimum_peak_age: int
    maximum_peak_age: int
    minimum_pullback_depth: float
    maximum_pullback_depth: float
    maximum_volume_ratio: float
    support: str
    anchor: str = "rolling_peak"


@dataclass(frozen=True)
class ResearchWindow:
    label: str
    role: str
    backtest_id: str
    snapshot_id: str
    start_date: str
    end_date: str


# These three variants are intentionally small and structural. Treat this tuple
# as frozen before opening validation and holdout windows.
FROZEN_HYPOTHESES = (
    PullbackHypothesis(
        hypothesis_id="first_pullback_reclaim",
        name="First pullback reclaim",
        recent_limit_days=15,
        minimum_return_20d=0.08,
        minimum_peak_age=2,
        maximum_peak_age=7,
        minimum_pullback_depth=0.02,
        maximum_pullback_depth=0.12,
        maximum_volume_ratio=1.00,
        support="ma5_or_ma10",
    ),
    PullbackHypothesis(
        hypothesis_id="ma10_support_turn",
        name="MA10 support turn",
        recent_limit_days=20,
        minimum_return_20d=0.05,
        minimum_peak_age=2,
        maximum_peak_age=10,
        minimum_pullback_depth=0.03,
        maximum_pullback_depth=0.15,
        maximum_volume_ratio=0.90,
        support="ma10",
    ),
    PullbackHypothesis(
        hypothesis_id="limit_anchor_reclaim",
        name="Limit-day anchor reclaim",
        recent_limit_days=20,
        minimum_return_20d=0.05,
        minimum_peak_age=2,
        maximum_peak_age=10,
        minimum_pullback_depth=0.00,
        maximum_pullback_depth=0.08,
        maximum_volume_ratio=1.00,
        support="ma5_or_ma10",
        anchor="latest_limit_close",
    ),
)


def build_pullback_event_table(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame | None = None,
    sector_membership: pd.DataFrame | None = None,
    hypotheses: Sequence[PullbackHypothesis] = FROZEN_HYPOTHESES,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    maximum_daily_candidates: int = 3,
) -> pd.DataFrame:
    """Build point-in-time low-buy events and next-open execution labels.

    Adjusted bars define the setup; raw bars define price limits, gaps, and
    realized returns. Theme membership is only an annotation because historical
    snapshots currently use current-membership fallback data.
    """

    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if maximum_daily_candidates <= 0:
        raise ValueError("maximum_daily_candidates must be positive")
    if not hypotheses:
        return pd.DataFrame()

    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_point_in_time_features(frame, hypotheses)

    event_frames: list[pd.DataFrame] = []
    for hypothesis in hypotheses:
        mask = _hypothesis_mask(frame, hypothesis)
        prior_mask = mask.groupby(frame["code"], sort=False).shift(1).fillna(False)
        trigger = mask & ~prior_mask.astype(bool)
        if not trigger.any():
            continue
        events = frame.loc[trigger, _event_columns()].copy()
        events["hypothesis_id"] = hypothesis.hypothesis_id
        events["hypothesis_name"] = hypothesis.name
        events["hypothesis_anchor"] = hypothesis.anchor
        event_frames.append(events)

    if not event_frames:
        return _empty_event_table()
    events = pd.concat(event_frames, ignore_index=True)
    events = _rank_events(events, maximum_daily_candidates)
    events = _annotate_execution(
        events,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
    )
    events = annotate_research_context(
        events,
        market_states=market_states,
        sector_membership=sector_membership,
    )
    return events.sort_values(
        ["signal_date", "hypothesis_id", "daily_rank", "code"]
    ).reset_index(drop=True)


def annotate_research_context(
    events: pd.DataFrame,
    *,
    market_states: pd.DataFrame | None,
    sector_membership: pd.DataFrame | None,
) -> pd.DataFrame:
    result = events.copy()
    if result.empty:
        return result
    states = _normalize_market_states(market_states)
    if states.empty:
        result["market_phase"] = ""
        result["market_style"] = ""
        result["market_score"] = np.nan
        result["market_regime"] = ""
        result["market_entry_allowed"] = False
        result["market_gate"] = False
        result["theme_gate"] = False
        result["matched_theme_code"] = ""
        result["matched_theme_phase"] = ""
        return result

    result = result.merge(
        states.drop(columns=["top_themes"], errors="ignore"),
        left_on="signal_date",
        right_on="timestamp",
        how="left",
        validate="many_to_one",
    ).drop(columns=["timestamp"], errors="ignore")
    direct = result["market_phase"].isin(DIRECT_MARKET_PHASES)
    healthy_divergence = (
        result["market_phase"].eq("DIVERGENCE")
        & pd.to_numeric(result["market_score"], errors="coerce").ge(0.55)
        & ~result["market_regime"].isin(WEAK_MARKET_REGIMES)
    )
    result["market_gate"] = (
        result["market_entry_allowed"].fillna(False).astype(bool)
        & (direct | healthy_divergence)
    )

    top_themes_by_day = {
        pd.Timestamp(row.timestamp).normalize(): list(row.top_themes)
        for row in states.loc[:, ["timestamp", "top_themes"]].itertuples(index=False)
    }
    memberships = _member_sector_map(sector_membership)
    matched_codes: list[str] = []
    matched_phases: list[str] = []
    theme_allowed: list[bool] = []
    for row in result.loc[:, ["signal_date", "code"]].itertuples(index=False):
        member_sectors = memberships.get(str(row.code), set())
        matched = next(
            (
                theme
                for theme in top_themes_by_day.get(
                    pd.Timestamp(row.signal_date).normalize(), []
                )
                if str(theme.get("sector_code", "")) in member_sectors
            ),
            None,
        )
        matched_codes.append(str((matched or {}).get("sector_code", "")))
        matched_phases.append(str((matched or {}).get("theme_phase", "")))
        theme_allowed.append(
            bool(matched)
            and str((matched or {}).get("theme_phase", "")) in ALLOWED_THEME_PHASES
        )
    result["matched_theme_code"] = matched_codes
    result["matched_theme_phase"] = matched_phases
    result["theme_gate"] = theme_allowed
    return result


def summarize_pullback_events(
    events: pd.DataFrame,
    *,
    trading_days: int,
    target_weight: float = 0.10,
    maximum_positions: int = 3,
) -> list[dict[str, Any]]:
    if events.empty:
        return []
    scopes = {
        "price_only": pd.Series(True, index=events.index),
        "market": events["market_gate"].fillna(False).astype(bool),
        "market_and_theme": (
            events["market_gate"].fillna(False).astype(bool)
            & events["theme_gate"].fillna(False).astype(bool)
        ),
    }
    rows: list[dict[str, Any]] = []
    for hypothesis_id, hypothesis_events in events.groupby(
        "hypothesis_id", sort=True
    ):
        for scope, scope_mask in scopes.items():
            scoped = hypothesis_events.loc[scope_mask.reindex(hypothesis_events.index)]
            selected = _select_scoped_events(scoped, maximum_daily_candidates=3)
            executable = selected.loc[selected["executable"]].copy()
            portfolio = simulate_event_portfolio(
                executable,
                trading_days=trading_days,
                target_weight=target_weight,
                maximum_positions=maximum_positions,
            )
            returns = pd.to_numeric(
                executable["net_return_5d"], errors="coerce"
            ).dropna()
            attempted = int(len(selected))
            row = {
                "hypothesis_id": str(hypothesis_id),
                "scope": scope,
                "raw_signals": int(len(scoped)),
                "selected_signals": attempted,
                "executable_signals": int(len(executable)),
                "blocked_limit_up_open": int(
                    selected["blocked_limit_up_open"].sum()
                ),
                "blocked_open_gap": int(selected["blocked_open_gap"].sum()),
                "blocked_missing_bars": int(selected["blocked_missing_bars"].sum()),
                "fill_rate": float(len(executable) / attempted) if attempted else 0.0,
                "median_net_return_5d": (
                    float(returns.median()) if not returns.empty else None
                ),
                "mean_net_return_5d": (
                    float(returns.mean()) if not returns.empty else None
                ),
                "win_rate_5d": (
                    float((returns > 0).mean()) if not returns.empty else None
                ),
                **portfolio,
            }
            rows.append(row)
    return rows


def _select_scoped_events(
    events: pd.DataFrame,
    *,
    maximum_daily_candidates: int,
) -> pd.DataFrame:
    if events.empty:
        return events.copy()
    ranked = events.sort_values(
        ["signal_date", "score", "code"],
        ascending=[True, False, True],
    ).copy()
    ranked["scope_rank"] = ranked.groupby("signal_date", sort=False).cumcount() + 1
    return ranked.loc[ranked["scope_rank"].le(maximum_daily_candidates)].copy()


def simulate_event_portfolio(
    events: pd.DataFrame,
    *,
    trading_days: int,
    target_weight: float = 0.10,
    maximum_positions: int = 3,
    holding_days: int = 5,
) -> dict[str, Any]:
    """Estimate capacity-constrained realized returns for fixed-horizon exits.

    This is an event-study portfolio, not a replacement for the bar-by-bar
    simulator. Drawdown is measured on realized exit-day equity and therefore
    excludes intraday and unrealized drawdowns.
    """

    empty = {
        "portfolio_trades": 0,
        "portfolio_total_return": 0.0,
        "portfolio_annualized_return": 0.0,
        "portfolio_realized_max_drawdown": 0.0,
        "portfolio_ex_top3_total_return": 0.0,
        "portfolio_median_trade_return": None,
    }
    if events.empty:
        return empty
    if not 0 < target_weight <= 1:
        raise ValueError("target_weight must be in (0, 1]")
    if maximum_positions <= 0:
        raise ValueError("maximum_positions must be positive")
    if holding_days not in RETURN_HORIZONS:
        raise ValueError(f"holding_days must be one of {RETURN_HORIZONS}")

    candidates = events.dropna(
        subset=[
            "entry_date",
            f"exit_date_{holding_days}d",
            f"net_return_{holding_days}d",
        ]
    ).sort_values(
        ["entry_date", "score", "code"],
        ascending=[True, False, True],
    )
    active: list[tuple[str, pd.Timestamp]] = []
    accepted: list[pd.Series] = []
    for _, event in candidates.iterrows():
        entry_date = pd.Timestamp(event["entry_date"]).normalize()
        active = [item for item in active if item[1] > entry_date]
        active_codes = {item[0] for item in active}
        code = str(event["code"])
        if code in active_codes or len(active) >= maximum_positions:
            continue
        exit_date = pd.Timestamp(event[f"exit_date_{holding_days}d"]).normalize()
        active.append((code, exit_date))
        accepted.append(event)
    if not accepted:
        return empty

    accepted_frame = pd.DataFrame(accepted)
    realized = _realized_equity_curve(
        accepted_frame, target_weight, holding_days=holding_days
    )
    total_return = float(realized.iloc[-1] - 1.0)
    years = max(float(trading_days) / 252.0, 1.0 / 252.0)
    annualized = float((1.0 + total_return) ** (1.0 / years) - 1.0)
    drawdown = realized / realized.cummax() - 1.0
    trimmed = accepted_frame.drop(
        accepted_frame.nlargest(
            min(3, len(accepted_frame)), f"net_return_{holding_days}d"
        ).index
    )
    trimmed_curve = _realized_equity_curve(
        trimmed, target_weight, holding_days=holding_days
    )
    return {
        "portfolio_trades": int(len(accepted_frame)),
        "portfolio_total_return": total_return,
        "portfolio_annualized_return": annualized,
        "portfolio_realized_max_drawdown": float(drawdown.min()),
        "portfolio_ex_top3_total_return": (
            float(trimmed_curve.iloc[-1] - 1.0) if not trimmed_curve.empty else 0.0
        ),
        "portfolio_median_trade_return": float(
            pd.to_numeric(
                accepted_frame[f"net_return_{holding_days}d"], errors="coerce"
            ).median()
        ),
    }


def evaluate_research_window(
    config: PlatformConfig,
    database: Database,
    window: ResearchWindow,
    *,
    hypotheses: Sequence[PullbackHypothesis] = FROZEN_HYPOTHESES,
    execution_cost_multiplier: float = 1.0,
) -> tuple[dict[str, Any], pd.DataFrame]:
    snapshots = ParquetSnapshotStore(config, database)
    front = snapshots.load_records(window.snapshot_id, "daily_front")
    raw = snapshots.load_records(window.snapshot_id, "daily_raw")
    master = snapshots.load_records(window.snapshot_id, "security_master")
    membership = snapshots.load_records(window.snapshot_id, "sector_membership")
    states = pd.DataFrame(
        database.query(
            """SELECT timestamp, market_phase, market_style, entry_allowed, state_json
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id='course49_system'
               ORDER BY timestamp""",
            (window.backtest_id,),
        )
    )
    if states.empty:
        raise ValueError(f"Backtest {window.backtest_id} has no course49_system states")
    names = {
        str(row.code): str(row.name)
        for row in master.loc[:, ["code", "name"]].itertuples(index=False)
    }
    events = build_pullback_event_table(
        front,
        raw,
        names,
        market_states=states,
        sector_membership=membership,
        hypotheses=hypotheses,
        execution_config=config.portfolio,
        execution_cost_multiplier=execution_cost_multiplier,
    )
    start = pd.Timestamp(window.start_date).normalize()
    end = pd.Timestamp(window.end_date).normalize()
    events = events.loc[
        pd.to_datetime(events["signal_date"]).between(start, end)
    ].reset_index(drop=True)
    trading_days = int(
        states["timestamp"].pipe(pd.to_datetime).dt.normalize().between(start, end).sum()
    )
    report = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "window": asdict(window),
        "protocol": {
            "hypotheses_frozen": [asdict(item) for item in hypotheses],
            "signal_time": "daily close",
            "entry": "next trading-day raw open",
            "exit": "raw open after 1, 3, or 5 observed holding days",
            "daily_selection": "top three per hypothesis by fixed score",
            "target_weight": 0.10,
            "maximum_positions": 3,
            "execution_cost_multiplier": float(execution_cost_multiplier),
        },
        "data_quality": {
            "daily_front_rows": int(len(front)),
            "daily_raw_rows": int(len(raw)),
            "daily_codes": int(front["code"].nunique()),
            "front_duplicate_keys": int(front.duplicated(["code", "timestamp"]).sum()),
            "raw_duplicate_keys": int(raw.duplicated(["code", "timestamp"]).sum()),
            "state_days": trading_days,
            "sector_membership_quality": "LIMITED",
            "sector_membership_source": "current_fallback",
        },
        "summaries": summarize_pullback_events(events, trading_days=trading_days),
    }
    return report, events


def assess_development_hypotheses(
    reports: Iterable[Mapping[str, Any]],
    *,
    scope: str = "market",
    minimum_trades_per_window: int = 30,
) -> dict[str, Any]:
    development_reports = [
        report
        for report in reports
        if str(report.get("window", {}).get("role", "")).upper() == "DEVELOPMENT"
    ]
    if not development_reports:
        raise ValueError("At least one development report is required")
    hypothesis_ids = sorted(
        {
            str(row["hypothesis_id"])
            for report in development_reports
            for row in report.get("summaries", [])
            if str(row.get("scope")) == scope
        }
    )
    assessments: list[dict[str, Any]] = []
    for hypothesis_id in hypothesis_ids:
        windows = []
        for report in development_reports:
            summary = next(
                (
                    row
                    for row in report.get("summaries", [])
                    if str(row.get("hypothesis_id")) == hypothesis_id
                    and str(row.get("scope")) == scope
                ),
                None,
            )
            if summary is None:
                continue
            windows.append(
                {
                    "label": str(report["window"]["label"]),
                    "portfolio_trades": int(summary["portfolio_trades"]),
                    "portfolio_total_return": float(summary["portfolio_total_return"]),
                    "portfolio_annualized_return": float(
                        summary["portfolio_annualized_return"]
                    ),
                    "portfolio_realized_max_drawdown": float(
                        summary["portfolio_realized_max_drawdown"]
                    ),
                    "portfolio_ex_top3_total_return": float(
                        summary["portfolio_ex_top3_total_return"]
                    ),
                    "portfolio_median_trade_return": summary[
                        "portfolio_median_trade_return"
                    ],
                    "fill_rate": float(summary["fill_rate"]),
                }
            )
        complete = len(windows) == len(development_reports)
        checks = {
            "complete_windows": complete,
            "minimum_sample": complete
            and all(
                item["portfolio_trades"] >= minimum_trades_per_window
                for item in windows
            ),
            "all_windows_profitable": complete
            and all(item["portfolio_total_return"] > 0 for item in windows),
            "all_medians_positive": complete
            and all(
                item["portfolio_median_trade_return"] is not None
                and float(item["portfolio_median_trade_return"]) > 0
                for item in windows
            ),
            "all_ex_top3_positive": complete
            and all(item["portfolio_ex_top3_total_return"] > 0 for item in windows),
            "drawdown_within_10pct": complete
            and all(item["portfolio_realized_max_drawdown"] >= -0.10 for item in windows),
            "fill_rate_at_least_60pct": complete
            and all(item["fill_rate"] >= 0.60 for item in windows),
        }
        assessments.append(
            {
                "hypothesis_id": hypothesis_id,
                "scope": scope,
                "windows": windows,
                "checks": checks,
                "development_qualified": all(checks.values()),
            }
        )
    qualified = [item for item in assessments if item["development_qualified"]]
    selected = None
    if qualified:
        selected = max(
            qualified,
            key=lambda item: (
                min(
                    window["portfolio_annualized_return"]
                    for window in item["windows"]
                ),
                item["hypothesis_id"],
            ),
        )["hypothesis_id"]
    return {
        "scope": scope,
        "development_windows": [
            str(report["window"]["label"]) for report in development_reports
        ],
        "minimum_trades_per_window": minimum_trades_per_window,
        "assessments": assessments,
        "selected_hypothesis": selected,
        "decision": "FREEZE_CANDIDATE" if selected else "REJECT_ALL",
        "validation_opened": False,
        "holdout_opened": False,
    }


def build_reclaim_matched_pairs(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    maximum_match_distance: float = 4.0,
) -> pd.DataFrame:
    """Match close-strength confirmations to comparable unconfirmed setups.

    Matching is within signal day and price-limit regime. Covariates are all
    available at the signal close. Controls are used at most once per day.
    """

    if maximum_match_distance <= 0:
        raise ValueError("maximum_match_distance must be positive")
    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_point_in_time_features(frame, FROZEN_HYPOTHESES)
    frame = _add_matching_features(frame)
    base = _matched_control_base_mask(frame)
    reclaim = _reclaim_confirmation_mask(frame, base)
    previous_reclaim = reclaim.groupby(frame["code"], sort=False).shift(1).fillna(False)
    treatment = reclaim & ~previous_reclaim.astype(bool)
    controls = base & ~reclaim
    relevant_dates = set(frame.loc[treatment, "timestamp"])
    pool_mask = (treatment | controls) & frame["timestamp"].isin(relevant_dates)
    if not pool_mask.any():
        return pd.DataFrame()

    columns = [
        "code",
        "name",
        "timestamp",
        "raw_close",
        "adj_close",
        "limit_ratio",
        "return_20d",
        "current_return",
        "pullback_depth",
        "pullback_volume_ratio",
        "volatility_20d",
        "turnover_20d",
        "log_turnover_20d",
        "peak_age",
        "distance_to_ma10",
        "entry_open",
        "entry_date",
        *[f"exit_open_{horizon}d" for horizon in RETURN_HORIZONS],
        *[f"exit_date_{horizon}d" for horizon in RETURN_HORIZONS],
    ]
    pool = frame.loc[pool_mask, columns].copy()
    pool.rename(columns={"timestamp": "signal_date"}, inplace=True)
    pool["treated"] = treatment.loc[pool.index].astype(bool).to_numpy()
    pool = _annotate_execution(
        pool,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
    )
    pool = annotate_research_context(
        pool,
        market_states=market_states,
        sector_membership=None,
    )
    pool = pool.loc[
        pool["market_gate"]
        & pool["executable"]
        & pool["net_return_5d"].notna()
    ].copy()
    return _greedy_daily_matches(pool, maximum_match_distance)


def build_reclaim_limit_order_events(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    entry_discount: float = LIMIT_ENTRY_DISCOUNT,
    maximum_daily_candidates: int = 3,
) -> pd.DataFrame:
    """Evaluate a pre-placed next-day limit order below the signal close."""

    if not 0 < entry_discount < 0.10:
        raise ValueError("entry_discount must be in (0, 0.10)")
    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_matching_features(
        _add_point_in_time_features(frame, FROZEN_HYPOTHESES)
    )
    base = _matched_control_base_mask(frame)
    reclaim = _reclaim_confirmation_mask(frame, base)
    prior = reclaim.groupby(frame["code"], sort=False).shift(1).fillna(False)
    trigger = reclaim & ~prior.astype(bool)
    if not trigger.any():
        return pd.DataFrame()

    columns = _event_columns()
    events = frame.loc[trigger, columns].copy()
    events["hypothesis_id"] = LIMIT_ENTRY_HYPOTHESIS_ID
    events["hypothesis_name"] = "Reclaim with a 3% next-day limit order"
    events["hypothesis_anchor"] = "rolling_peak"
    events = _rank_events(events, maximum_daily_candidates)
    events = _annotate_limit_order_execution(
        events,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
        entry_discount,
    )
    events = annotate_research_context(
        events,
        market_states=market_states,
        sector_membership=None,
    )
    return events.sort_values(
        ["signal_date", "daily_rank", "code"]
    ).reset_index(drop=True)


def build_two_day_exhaustion_events(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    maximum_daily_candidates: int = 3,
) -> pd.DataFrame:
    """Build the frozen two-day pullback-exhaustion development signal."""

    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_point_in_time_features(frame, FROZEN_HYPOTHESES)
    frame["previous_return"] = frame.groupby("code", sort=False)[
        "current_return"
    ].shift(1)
    frame["pullback_depth"] = (
        1.0 - frame["adj_close"] / frame["peak_close_2_7"]
    )
    support = frame["low_3d"].le(frame["ma10"] * 1.02)
    not_at_price_limit = (
        frame["raw_signal_return"].lt(frame["limit_ratio"] - 0.002)
        & frame["raw_signal_return"].gt(-frame["limit_ratio"] + 0.002)
    )
    signal = (
        frame["recent_limit_15d"]
        & frame["return_20d"].between(0.08, 0.40)
        & frame["peak_age_2_7"].between(2, 7)
        & frame["pullback_depth"].between(0.04, 0.12)
        & frame["previous_return"].between(-0.06, -0.002)
        & frame["current_return"].between(-0.04, -0.005)
        & frame["pullback_volume_ratio"].between(0.20, 0.85)
        & frame["adj_close"].ge(frame["ma20"])
        & frame["adj_close"].le(frame["ma5"] * 1.01)
        & frame["turnover_20d"].ge(20_000_000.0)
        & frame["raw_close"].ge(2.0)
        & support
        & not_at_price_limit
        & ~frame["name"].str.upper().str.contains("ST", regex=False, na=False)
    ).fillna(False)
    prior = signal.groupby(frame["code"], sort=False).shift(1).fillna(False)
    trigger = signal & ~prior.astype(bool)
    if not trigger.any():
        return pd.DataFrame()

    events = frame.loc[trigger, _event_columns()].copy()
    events["pullback_depth"] = frame.loc[trigger, "pullback_depth"].to_numpy()
    events["hypothesis_id"] = EXHAUSTION_HYPOTHESIS_ID
    events["hypothesis_name"] = "Two-day pullback exhaustion"
    events["hypothesis_anchor"] = "rolling_peak"
    events = _rank_events(events, maximum_daily_candidates)
    events = _annotate_execution(
        events,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
        entry_gap_min=-0.02,
        entry_gap_max=0.03,
    )
    events = annotate_research_context(
        events,
        market_states=market_states,
        sector_membership=None,
    )
    return events.sort_values(
        ["signal_date", "daily_rank", "code"]
    ).reset_index(drop=True)


def summarize_exhaustion_events(
    events: pd.DataFrame,
    *,
    trading_days: int,
) -> list[dict[str, Any]]:
    return _summarize_three_day_events(
        events,
        trading_days=trading_days,
        hypothesis_id=EXHAUSTION_HYPOTHESIS_ID,
    )


def build_trend_rsi2_events(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    maximum_daily_candidates: int = 3,
) -> pd.DataFrame:
    """Build a liquid-uptrend RSI(2) short-term reversal event table."""

    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_point_in_time_features(frame, FROZEN_HYPOTHESES)
    grouped = frame.groupby("code", sort=False)
    frame["ma60"] = grouped["adj_close"].transform(
        lambda values: values.rolling(60, min_periods=60).mean()
    )
    frame["return_60d"] = frame["adj_close"] / grouped["adj_close"].shift(60) - 1.0
    frame["return_3d"] = frame["adj_close"] / grouped["adj_close"].shift(3) - 1.0
    delta = grouped["adj_close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    average_gain = gain.groupby(frame["code"], sort=False).transform(
        lambda values: values.rolling(2, min_periods=2).mean()
    )
    average_loss = loss.groupby(frame["code"], sort=False).transform(
        lambda values: values.rolling(2, min_periods=2).mean()
    )
    relative_strength = average_gain / average_loss.replace(0.0, np.nan)
    frame["rsi2"] = 100.0 - 100.0 / (1.0 + relative_strength)
    frame.loc[average_loss.eq(0.0) & average_gain.gt(0.0), "rsi2"] = 100.0
    frame.loc[average_loss.gt(0.0) & average_gain.eq(0.0), "rsi2"] = 0.0
    frame["current_volume_ratio"] = (
        frame["adj_volume"] / frame["previous_volume_20"]
    )
    not_at_price_limit = (
        frame["raw_signal_return"].lt(frame["limit_ratio"] - 0.002)
        & frame["raw_signal_return"].gt(-frame["limit_ratio"] + 0.002)
    )
    signal = (
        frame["return_60d"].between(0.05, 0.50)
        & frame["ma20"].gt(frame["ma60"])
        & frame["adj_close"].gt(frame["ma60"])
        & frame["return_3d"].between(-0.12, -0.05)
        & frame["current_return"].between(-0.07, -0.01)
        & frame["rsi2"].le(10.0)
        & frame["current_volume_ratio"].between(0.20, 1.20)
        & frame["adj_close"].le(frame["ma5"] * 0.97)
        & frame["turnover_20d"].ge(50_000_000.0)
        & frame["raw_close"].ge(3.0)
        & frame["limit_ratio"].le(0.20)
        & not_at_price_limit
        & ~frame["name"].str.upper().str.contains("ST", regex=False, na=False)
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
        "current_return",
        "entry_open",
        "entry_low",
        "entry_date",
        *[f"exit_open_{horizon}d" for horizon in RETURN_HORIZONS],
        *[f"exit_date_{horizon}d" for horizon in RETURN_HORIZONS],
    ]
    events = frame.loc[trigger, columns].copy()
    for field in (
        "return_60d",
        "return_3d",
        "rsi2",
        "current_volume_ratio",
        "ma20",
        "ma60",
    ):
        events[field] = frame.loc[trigger, field].to_numpy()
    events.rename(columns={"timestamp": "signal_date"}, inplace=True)
    events["hypothesis_id"] = TREND_RSI2_HYPOTHESIS_ID
    events["hypothesis_name"] = "Liquid uptrend RSI(2) oversold"
    events = _rank_trend_rsi2_events(events, maximum_daily_candidates)
    events = _annotate_execution(
        events,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
        entry_gap_min=-0.03,
        entry_gap_max=0.03,
    )
    events = annotate_research_context(
        events,
        market_states=market_states,
        sector_membership=None,
    )
    return events.sort_values(
        ["signal_date", "daily_rank", "code"]
    ).reset_index(drop=True)


def summarize_trend_rsi2_events(
    events: pd.DataFrame,
    *,
    trading_days: int,
) -> list[dict[str, Any]]:
    return _summarize_three_day_events(
        events,
        trading_days=trading_days,
        hypothesis_id=TREND_RSI2_HYPOTHESIS_ID,
    )


def build_ma20_bounce_events(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    maximum_daily_candidates: int = 3,
) -> pd.DataFrame:
    """Build the frozen low-volatility MA20 support-bounce signal."""

    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_point_in_time_features(frame, FROZEN_HYPOTHESES)
    grouped = frame.groupby("code", sort=False)
    frame["ma60"] = grouped["adj_close"].transform(
        lambda values: values.rolling(60, min_periods=60).mean()
    )
    frame["return_60d"] = frame["adj_close"] / grouped["adj_close"].shift(60) - 1.0
    frame["return_5d"] = frame["adj_close"] / grouped["adj_close"].shift(5) - 1.0
    frame["ma20_slope_5d"] = frame["ma20"] / grouped["ma20"].shift(5) - 1.0
    frame["distance_to_ma20"] = frame["adj_close"] / frame["ma20"] - 1.0
    frame["current_volume_ratio"] = (
        frame["adj_volume"] / frame["previous_volume_20"]
    )
    not_at_price_limit = (
        frame["raw_signal_return"].lt(frame["limit_ratio"] - 0.002)
        & frame["raw_signal_return"].gt(-frame["limit_ratio"] + 0.002)
    )
    signal = (
        frame["return_60d"].between(0.10, 0.40)
        & frame["ma20"].gt(frame["ma60"])
        & frame["ma20_slope_5d"].gt(0.0)
        & frame["volatility_20d"].le(0.035)
        & frame["return_5d"].between(-0.06, 0.0)
        & frame["distance_to_ma20"].between(-0.02, 0.02)
        & frame["current_return"].between(0.002, 0.03)
        & frame["adj_close"].gt(frame["adj_open"])
        & frame["current_volume_ratio"].between(0.20, 0.85)
        & frame["turnover_20d"].ge(50_000_000.0)
        & frame["raw_close"].ge(3.0)
        & frame["limit_ratio"].le(0.20)
        & not_at_price_limit
        & ~frame["name"].str.upper().str.contains("ST", regex=False, na=False)
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
        "current_return",
        "entry_open",
        "entry_low",
        "entry_date",
        *[f"exit_open_{horizon}d" for horizon in RETURN_HORIZONS],
        *[f"exit_date_{horizon}d" for horizon in RETURN_HORIZONS],
    ]
    events = frame.loc[trigger, columns].copy()
    for field in (
        "return_60d",
        "return_5d",
        "ma20_slope_5d",
        "distance_to_ma20",
        "volatility_20d",
        "current_volume_ratio",
        "ma20",
        "ma60",
    ):
        events[field] = frame.loc[trigger, field].to_numpy()
    events.rename(columns={"timestamp": "signal_date"}, inplace=True)
    events["hypothesis_id"] = MA20_BOUNCE_HYPOTHESIS_ID
    events["hypothesis_name"] = "Low-volatility MA20 support bounce"
    events = _rank_ma20_bounce_events(events, maximum_daily_candidates)
    events = _annotate_execution(
        events,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
        entry_gap_min=-0.02,
        entry_gap_max=0.03,
    )
    events = annotate_research_context(
        events,
        market_states=market_states,
        sector_membership=None,
    )
    return events.sort_values(
        ["signal_date", "daily_rank", "code"]
    ).reset_index(drop=True)


def summarize_ma20_bounce_events(
    events: pd.DataFrame,
    *,
    trading_days: int,
) -> list[dict[str, Any]]:
    return summarize_pullback_events(events, trading_days=trading_days)


def build_intraday_washout_events(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
    *,
    market_states: pd.DataFrame,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
    maximum_daily_candidates: int = 3,
) -> pd.DataFrame:
    """Build the frozen intraday washout-and-recovery event table."""

    frame = _merge_bar_inputs(front_bars, raw_bars, names)
    if frame.empty:
        return pd.DataFrame()
    frame = _add_point_in_time_features(frame, FROZEN_HYPOTHESES)
    grouped = frame.groupby("code", sort=False)
    frame["ma60"] = grouped["adj_close"].transform(
        lambda values: values.rolling(60, min_periods=60).mean()
    )
    frame["return_60d"] = frame["adj_close"] / grouped["adj_close"].shift(60) - 1.0
    frame["current_volume_ratio"] = (
        frame["adj_volume"] / frame["previous_volume_20"]
    )
    frame["intraday_low_return"] = (
        frame["raw_low"] / frame["raw_previous_close"] - 1.0
    )
    raw_range = frame["raw_high"] - frame["raw_low"]
    frame["close_location"] = (
        (frame["raw_close"] - frame["raw_low"]) / raw_range.replace(0.0, np.nan)
    )
    not_at_price_limit = (
        frame["raw_signal_return"].lt(frame["limit_ratio"] - 0.002)
        & frame["raw_signal_return"].gt(-frame["limit_ratio"] + 0.002)
    )
    signal = (
        frame["return_60d"].between(0.10, 0.50)
        & frame["ma20"].gt(frame["ma60"])
        & frame["adj_close"].gt(frame["ma60"])
        & frame["current_return"].between(-0.05, -0.005)
        & frame["intraday_low_return"].le(-0.05)
        & frame["raw_close"].gt(frame["raw_open"])
        & frame["close_location"].ge(0.70)
        & frame["current_volume_ratio"].between(1.20, 2.50)
        & frame["turnover_20d"].ge(50_000_000.0)
        & frame["raw_close"].ge(3.0)
        & frame["limit_ratio"].le(0.20)
        & not_at_price_limit
        & ~frame["name"].str.upper().str.contains("ST", regex=False, na=False)
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
        "current_return",
        "entry_open",
        "entry_low",
        "entry_date",
        *[f"exit_open_{horizon}d" for horizon in RETURN_HORIZONS],
        *[f"exit_date_{horizon}d" for horizon in RETURN_HORIZONS],
    ]
    events = frame.loc[trigger, columns].copy()
    for field in (
        "return_60d",
        "current_volume_ratio",
        "intraday_low_return",
        "close_location",
        "ma20",
        "ma60",
    ):
        events[field] = frame.loc[trigger, field].to_numpy()
    events.rename(columns={"timestamp": "signal_date"}, inplace=True)
    events["hypothesis_id"] = WASHOUT_HYPOTHESIS_ID
    events["hypothesis_name"] = "Intraday washout recovery"
    events = _rank_intraday_washout_events(events, maximum_daily_candidates)
    events = _annotate_execution(
        events,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
        entry_gap_min=-0.03,
        entry_gap_max=0.03,
    )
    events = annotate_research_context(
        events,
        market_states=market_states,
        sector_membership=None,
    )
    return events.sort_values(
        ["signal_date", "daily_rank", "code"]
    ).reset_index(drop=True)


def summarize_intraday_washout_events(
    events: pd.DataFrame,
    *,
    trading_days: int,
) -> list[dict[str, Any]]:
    return _summarize_three_day_events(
        events,
        trading_days=trading_days,
        hypothesis_id=WASHOUT_HYPOTHESIS_ID,
    )


def _summarize_three_day_events(
    events: pd.DataFrame,
    *,
    trading_days: int,
    hypothesis_id: str,
) -> list[dict[str, Any]]:
    if events.empty:
        return []
    scopes = {
        "price_only": pd.Series(True, index=events.index),
        "market": events["market_gate"].fillna(False).astype(bool),
    }
    rows: list[dict[str, Any]] = []
    for scope, scope_mask in scopes.items():
        scoped = events.loc[scope_mask]
        selected = _select_scoped_events(scoped, maximum_daily_candidates=3)
        executable = selected.loc[selected["executable"]]
        returns = pd.to_numeric(
            executable["net_return_3d"], errors="coerce"
        ).dropna()
        portfolio = simulate_event_portfolio(
            executable,
            trading_days=trading_days,
            target_weight=0.10,
            maximum_positions=3,
            holding_days=3,
        )
        attempted = int(len(selected))
        rows.append(
            {
                "hypothesis_id": hypothesis_id,
                "scope": scope,
                "raw_signals": int(len(scoped)),
                "selected_signals": attempted,
                "executable_signals": int(len(executable)),
                "blocked_limit_up_open": int(
                    selected["blocked_limit_up_open"].sum()
                ),
                "blocked_open_gap": int(selected["blocked_open_gap"].sum()),
                "blocked_missing_bars": int(selected["blocked_missing_bars"].sum()),
                "fill_rate": float(len(executable) / attempted) if attempted else 0.0,
                "holding_days": 3,
                "median_net_return_3d": (
                    float(returns.median()) if not returns.empty else None
                ),
                "mean_net_return_3d": (
                    float(returns.mean()) if not returns.empty else None
                ),
                "win_rate_3d": (
                    float((returns > 0).mean()) if not returns.empty else None
                ),
                **portfolio,
            }
        )
    return rows


def summarize_limit_order_events(
    events: pd.DataFrame,
    *,
    trading_days: int,
) -> list[dict[str, Any]]:
    summaries = summarize_pullback_events(events, trading_days=trading_days)
    for summary in summaries:
        scope = str(summary["scope"])
        if scope == "price_only":
            scoped = events
        elif scope == "market":
            scoped = events.loc[events["market_gate"]]
        else:
            scoped = events.loc[events["market_gate"] & events["theme_gate"]]
        selected = _select_scoped_events(scoped, maximum_daily_candidates=3)
        summary["unfilled_limit_order"] = int(
            selected.get(
                "unfilled_limit_order", pd.Series(False, index=selected.index)
            ).sum()
        )
        summary["entry_discount"] = LIMIT_ENTRY_DISCOUNT
    return summaries


def summarize_reclaim_matches(
    pairs: pd.DataFrame,
    *,
    bootstrap_samples: int = 2_000,
    random_seed: int = 49,
) -> dict[str, Any]:
    if bootstrap_samples <= 0:
        raise ValueError("bootstrap_samples must be positive")
    if pairs.empty:
        return {
            "protocol_version": MATCHING_PROTOCOL_VERSION,
            "pairs": 0,
            "signal_days": 0,
            "horizons": [],
        }
    rng = np.random.default_rng(random_seed)
    horizons: list[dict[str, Any]] = []
    for horizon in RETURN_HORIZONS:
        treated_field = f"treated_net_return_{horizon}d"
        control_field = f"control_net_return_{horizon}d"
        effect_field = f"effect_net_return_{horizon}d"
        complete = pairs.dropna(subset=[treated_field, control_field, effect_field])
        daily = complete.groupby("signal_date", sort=True).agg(
            treated_return=(treated_field, "mean"),
            control_return=(control_field, "mean"),
            effect=(effect_field, "mean"),
            pairs=("pair_id", "size"),
        )
        daily_effects = daily["effect"].to_numpy(dtype=float)
        if len(daily_effects):
            sampled = rng.choice(
                daily_effects,
                size=(bootstrap_samples, len(daily_effects)),
                replace=True,
            ).mean(axis=1)
            lower, upper = np.quantile(sampled, [0.025, 0.975])
        else:
            lower = upper = np.nan
        effects = pd.to_numeric(complete[effect_field], errors="coerce").dropna()
        horizons.append(
            {
                "holding_days": horizon,
                "pairs": int(len(complete)),
                "signal_days": int(len(daily)),
                "treated_mean_return": float(complete[treated_field].mean()),
                "control_mean_return": float(complete[control_field].mean()),
                "pair_mean_effect": float(effects.mean()),
                "pair_median_effect": float(effects.median()),
                "daily_mean_effect": float(daily["effect"].mean()),
                "daily_effect_ci_95_low": float(lower),
                "daily_effect_ci_95_high": float(upper),
                "positive_effect_day_rate": float((daily["effect"] > 0).mean()),
            }
        )
    return {
        "protocol_version": MATCHING_PROTOCOL_VERSION,
        "matching_scope": "same_day_same_price_limit_regime",
        "features": dict(MATCHING_FEATURE_SCALES),
        "pairs": int(len(pairs)),
        "signal_days": int(pairs["signal_date"].nunique()),
        "median_match_distance": float(pairs["match_distance"].median()),
        "maximum_match_distance": float(pairs["match_distance"].max()),
        "horizons": horizons,
    }


def evaluate_matched_control_window(
    config: PlatformConfig,
    database: Database,
    window: ResearchWindow,
    *,
    execution_cost_multiplier: float = 1.0,
    maximum_match_distance: float = 4.0,
) -> tuple[dict[str, Any], pd.DataFrame]:
    snapshots = ParquetSnapshotStore(config, database)
    front = snapshots.load_records(window.snapshot_id, "daily_front")
    raw = snapshots.load_records(window.snapshot_id, "daily_raw")
    master = snapshots.load_records(window.snapshot_id, "security_master")
    states = pd.DataFrame(
        database.query(
            """SELECT timestamp, market_phase, market_style, entry_allowed, state_json
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id='course49_system'
               ORDER BY timestamp""",
            (window.backtest_id,),
        )
    )
    if states.empty:
        raise ValueError(f"Backtest {window.backtest_id} has no course49_system states")
    names = {
        str(row.code): str(row.name)
        for row in master.loc[:, ["code", "name"]].itertuples(index=False)
    }
    pairs = build_reclaim_matched_pairs(
        front,
        raw,
        names,
        market_states=states,
        execution_config=config.portfolio,
        execution_cost_multiplier=execution_cost_multiplier,
        maximum_match_distance=maximum_match_distance,
    )
    start = pd.Timestamp(window.start_date).normalize()
    end = pd.Timestamp(window.end_date).normalize()
    if not pairs.empty:
        pairs = pairs.loc[
            pd.to_datetime(pairs["signal_date"]).between(start, end)
        ].reset_index(drop=True)
    report = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "window": asdict(window),
        "protocol": {
            "version": MATCHING_PROTOCOL_VERSION,
            "treatment": "close-strength reclaim after a recent limit-up pullback",
            "control": "same-day strong pullback without close-strength reclaim",
            "exact_match": ["signal_date", "price_limit_regime"],
            "distance_features": dict(MATCHING_FEATURE_SCALES),
            "maximum_match_distance": float(maximum_match_distance),
            "replacement": "without replacement within day",
            "execution_cost_multiplier": float(execution_cost_multiplier),
        },
        "summary": summarize_reclaim_matches(pairs),
        "data_quality": {
            "daily_front_rows": int(len(front)),
            "daily_raw_rows": int(len(raw)),
            "daily_codes": int(front["code"].nunique()),
            "sector_membership_quality": "not_used",
        },
    }
    return report, pairs


def evaluate_limit_entry_window(
    config: PlatformConfig,
    database: Database,
    window: ResearchWindow,
    *,
    execution_cost_multiplier: float = 1.0,
) -> tuple[dict[str, Any], pd.DataFrame]:
    snapshots = ParquetSnapshotStore(config, database)
    front = snapshots.load_records(window.snapshot_id, "daily_front")
    raw = snapshots.load_records(window.snapshot_id, "daily_raw")
    master = snapshots.load_records(window.snapshot_id, "security_master")
    states = pd.DataFrame(
        database.query(
            """SELECT timestamp, market_phase, market_style, entry_allowed, state_json
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id='course49_system'
               ORDER BY timestamp""",
            (window.backtest_id,),
        )
    )
    if states.empty:
        raise ValueError(f"Backtest {window.backtest_id} has no course49_system states")
    names = {
        str(row.code): str(row.name)
        for row in master.loc[:, ["code", "name"]].itertuples(index=False)
    }
    events = build_reclaim_limit_order_events(
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
        states["timestamp"].pipe(pd.to_datetime).dt.normalize().between(start, end).sum()
    )
    report = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "window": asdict(window),
        "protocol": {
            "version": LIMIT_ENTRY_PROTOCOL_VERSION,
            "hypothesis_id": LIMIT_ENTRY_HYPOTHESIS_ID,
            "entry_order": "signal raw close minus 3%, valid next trading day only",
            "entry_discount": LIMIT_ENTRY_DISCOUNT,
            "open_gap_bounds": [-0.03, 0.08],
            "daily_selection": "top three by frozen reclaim score",
            "target_weight": 0.10,
            "maximum_positions": 3,
            "execution_cost_multiplier": float(execution_cost_multiplier),
        },
        "summaries": summarize_limit_order_events(
            events, trading_days=trading_days
        ),
        "data_quality": {
            "daily_front_rows": int(len(front)),
            "daily_raw_rows": int(len(raw)),
            "daily_codes": int(front["code"].nunique()),
            "state_days": trading_days,
            "sector_membership_quality": "not_used",
        },
    }
    return report, events


def evaluate_exhaustion_window(
    config: PlatformConfig,
    database: Database,
    window: ResearchWindow,
    *,
    execution_cost_multiplier: float = 1.0,
) -> tuple[dict[str, Any], pd.DataFrame]:
    snapshots = ParquetSnapshotStore(config, database)
    front = snapshots.load_records(window.snapshot_id, "daily_front")
    raw = snapshots.load_records(window.snapshot_id, "daily_raw")
    master = snapshots.load_records(window.snapshot_id, "security_master")
    states = pd.DataFrame(
        database.query(
            """SELECT timestamp, market_phase, market_style, entry_allowed, state_json
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id='course49_system'
               ORDER BY timestamp""",
            (window.backtest_id,),
        )
    )
    if states.empty:
        raise ValueError(f"Backtest {window.backtest_id} has no course49_system states")
    names = {
        str(row.code): str(row.name)
        for row in master.loc[:, ["code", "name"]].itertuples(index=False)
    }
    events = build_two_day_exhaustion_events(
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
        states["timestamp"].pipe(pd.to_datetime).dt.normalize().between(start, end).sum()
    )
    report = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "window": asdict(window),
        "protocol": {
            "version": EXHAUSTION_PROTOCOL_VERSION,
            "hypothesis_id": EXHAUSTION_HYPOTHESIS_ID,
            "signal": "two consecutive down closes in a recent limit-up uptrend",
            "entry": "next raw open with gap between -2% and +3%",
            "holding_days": 3,
            "daily_selection": "top three by frozen pullback score",
            "target_weight": 0.10,
            "maximum_positions": 3,
            "execution_cost_multiplier": float(execution_cost_multiplier),
        },
        "summaries": summarize_exhaustion_events(
            events, trading_days=trading_days
        ),
        "data_quality": {
            "daily_front_rows": int(len(front)),
            "daily_raw_rows": int(len(raw)),
            "daily_codes": int(front["code"].nunique()),
            "state_days": trading_days,
            "sector_membership_quality": "not_used",
        },
    }
    return report, events


def evaluate_trend_rsi2_window(
    config: PlatformConfig,
    database: Database,
    window: ResearchWindow,
    *,
    execution_cost_multiplier: float = 1.0,
) -> tuple[dict[str, Any], pd.DataFrame]:
    snapshots = ParquetSnapshotStore(config, database)
    front = snapshots.load_records(window.snapshot_id, "daily_front")
    raw = snapshots.load_records(window.snapshot_id, "daily_raw")
    master = snapshots.load_records(window.snapshot_id, "security_master")
    states = pd.DataFrame(
        database.query(
            """SELECT timestamp, market_phase, market_style, entry_allowed, state_json
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id='course49_system'
               ORDER BY timestamp""",
            (window.backtest_id,),
        )
    )
    if states.empty:
        raise ValueError(f"Backtest {window.backtest_id} has no course49_system states")
    names = {
        str(row.code): str(row.name)
        for row in master.loc[:, ["code", "name"]].itertuples(index=False)
    }
    events = build_trend_rsi2_events(
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
        states["timestamp"].pipe(pd.to_datetime).dt.normalize().between(start, end).sum()
    )
    report = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "window": asdict(window),
        "protocol": {
            "version": TREND_RSI2_PROTOCOL_VERSION,
            "hypothesis_id": TREND_RSI2_HYPOTHESIS_ID,
            "signal": "liquid 60-day uptrend with RSI(2) <= 10 and 5%-12% three-day pullback",
            "entry": "next raw open with gap between -3% and +3%",
            "holding_days": 3,
            "daily_selection": "top three by frozen oversold score",
            "target_weight": 0.10,
            "maximum_positions": 3,
            "execution_cost_multiplier": float(execution_cost_multiplier),
        },
        "summaries": summarize_trend_rsi2_events(
            events, trading_days=trading_days
        ),
        "data_quality": {
            "daily_front_rows": int(len(front)),
            "daily_raw_rows": int(len(raw)),
            "daily_codes": int(front["code"].nunique()),
            "state_days": trading_days,
            "sector_membership_quality": "not_used",
        },
    }
    return report, events


def evaluate_ma20_bounce_window(
    config: PlatformConfig,
    database: Database,
    window: ResearchWindow,
    *,
    execution_cost_multiplier: float = 1.0,
) -> tuple[dict[str, Any], pd.DataFrame]:
    snapshots = ParquetSnapshotStore(config, database)
    front = snapshots.load_records(window.snapshot_id, "daily_front")
    raw = snapshots.load_records(window.snapshot_id, "daily_raw")
    master = snapshots.load_records(window.snapshot_id, "security_master")
    states = pd.DataFrame(
        database.query(
            """SELECT timestamp, market_phase, market_style, entry_allowed, state_json
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id='course49_system'
               ORDER BY timestamp""",
            (window.backtest_id,),
        )
    )
    if states.empty:
        raise ValueError(f"Backtest {window.backtest_id} has no course49_system states")
    names = {
        str(row.code): str(row.name)
        for row in master.loc[:, ["code", "name"]].itertuples(index=False)
    }
    events = build_ma20_bounce_events(
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
        states["timestamp"].pipe(pd.to_datetime).dt.normalize().between(start, end).sum()
    )
    report = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "window": asdict(window),
        "protocol": {
            "version": MA20_BOUNCE_PROTOCOL_VERSION,
            "hypothesis_id": MA20_BOUNCE_HYPOTHESIS_ID,
            "signal": "low-volatility rising-MA20 pullback followed by a restrained positive close",
            "entry": "next raw open with gap between -2% and +3%",
            "holding_days": 5,
            "daily_selection": "top three by frozen support-bounce score",
            "target_weight": 0.10,
            "maximum_positions": 3,
            "execution_cost_multiplier": float(execution_cost_multiplier),
        },
        "summaries": summarize_ma20_bounce_events(
            events, trading_days=trading_days
        ),
        "data_quality": {
            "daily_front_rows": int(len(front)),
            "daily_raw_rows": int(len(raw)),
            "daily_codes": int(front["code"].nunique()),
            "state_days": trading_days,
            "sector_membership_quality": "not_used",
        },
    }
    return report, events


def evaluate_intraday_washout_window(
    config: PlatformConfig,
    database: Database,
    window: ResearchWindow,
    *,
    execution_cost_multiplier: float = 1.0,
) -> tuple[dict[str, Any], pd.DataFrame]:
    snapshots = ParquetSnapshotStore(config, database)
    front = snapshots.load_records(window.snapshot_id, "daily_front")
    raw = snapshots.load_records(window.snapshot_id, "daily_raw")
    master = snapshots.load_records(window.snapshot_id, "security_master")
    states = pd.DataFrame(
        database.query(
            """SELECT timestamp, market_phase, market_style, entry_allowed, state_json
               FROM backtest_states
               WHERE backtest_id=? AND strategy_id='course49_system'
               ORDER BY timestamp""",
            (window.backtest_id,),
        )
    )
    if states.empty:
        raise ValueError(f"Backtest {window.backtest_id} has no course49_system states")
    names = {
        str(row.code): str(row.name)
        for row in master.loc[:, ["code", "name"]].itertuples(index=False)
    }
    events = build_intraday_washout_events(
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
        states["timestamp"].pipe(pd.to_datetime).dt.normalize().between(start, end).sum()
    )
    report = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "window": asdict(window),
        "protocol": {
            "version": WASHOUT_PROTOCOL_VERSION,
            "hypothesis_id": WASHOUT_HYPOTHESIS_ID,
            "signal": "uptrend stock recovers to the top 30% of its range after a 5% intraday washout",
            "entry": "next raw open with gap between -3% and +3%",
            "holding_days": 3,
            "daily_selection": "top three by frozen washout score",
            "target_weight": 0.10,
            "maximum_positions": 3,
            "execution_cost_multiplier": float(execution_cost_multiplier),
        },
        "summaries": summarize_intraday_washout_events(
            events, trading_days=trading_days
        ),
        "data_quality": {
            "daily_front_rows": int(len(front)),
            "daily_raw_rows": int(len(raw)),
            "daily_codes": int(front["code"].nunique()),
            "state_days": trading_days,
            "sector_membership_quality": "not_used",
        },
    }
    return report, events


def write_intraday_washout_artifacts(
    output_dir: Path,
    report: Mapping[str, Any],
    events: pd.DataFrame,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = str(report["window"]["label"])
    report_path = output_dir / f"{label}_intraday_washout.json"
    events_path = output_dir / f"{label}_intraday_washout_events.parquet"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    events.to_parquet(events_path, index=False)
    return report_path, events_path


def write_ma20_bounce_artifacts(
    output_dir: Path,
    report: Mapping[str, Any],
    events: pd.DataFrame,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = str(report["window"]["label"])
    report_path = output_dir / f"{label}_ma20_bounce.json"
    events_path = output_dir / f"{label}_ma20_bounce_events.parquet"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    events.to_parquet(events_path, index=False)
    return report_path, events_path


def write_trend_rsi2_artifacts(
    output_dir: Path,
    report: Mapping[str, Any],
    events: pd.DataFrame,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = str(report["window"]["label"])
    report_path = output_dir / f"{label}_trend_rsi2.json"
    events_path = output_dir / f"{label}_trend_rsi2_events.parquet"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    events.to_parquet(events_path, index=False)
    return report_path, events_path


def write_exhaustion_artifacts(
    output_dir: Path,
    report: Mapping[str, Any],
    events: pd.DataFrame,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = str(report["window"]["label"])
    report_path = output_dir / f"{label}_exhaustion.json"
    events_path = output_dir / f"{label}_exhaustion_events.parquet"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    events.to_parquet(events_path, index=False)
    return report_path, events_path


def write_limit_entry_artifacts(
    output_dir: Path,
    report: Mapping[str, Any],
    events: pd.DataFrame,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = str(report["window"]["label"])
    report_path = output_dir / f"{label}_limit3.json"
    events_path = output_dir / f"{label}_limit3_events.parquet"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    events.to_parquet(events_path, index=False)
    return report_path, events_path


def write_matched_control_artifacts(
    output_dir: Path,
    report: Mapping[str, Any],
    pairs: pd.DataFrame,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = str(report["window"]["label"])
    report_path = output_dir / f"{label}_matched.json"
    pairs_path = output_dir / f"{label}_matched_pairs.parquet"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pairs.to_parquet(pairs_path, index=False)
    return report_path, pairs_path


def write_research_artifacts(
    output_dir: Path,
    report: Mapping[str, Any],
    events: pd.DataFrame,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    label = str(report["window"]["label"])
    report_path = output_dir / f"{label}.json"
    event_path = output_dir / f"{label}_events.parquet"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    events.to_parquet(event_path, index=False)
    return report_path, event_path


def _merge_bar_inputs(
    front_bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    names: Mapping[str, str],
) -> pd.DataFrame:
    required = {"code", "timestamp", "Open", "High", "Low", "Close", "Volume", "Amount"}
    for label, bars in (("front_bars", front_bars), ("raw_bars", raw_bars)):
        missing = required - set(bars.columns)
        if missing:
            raise ValueError(f"{label} is missing columns: {sorted(missing)}")

    front = front_bars.loc[:, sorted(required)].copy()
    raw = raw_bars.loc[:, sorted(required)].copy()
    for bars in (front, raw):
        bars["code"] = bars["code"].astype(str)
        bars["timestamp"] = pd.to_datetime(
            bars["timestamp"], errors="coerce"
        ).dt.normalize()
        bars.dropna(subset=["timestamp"], inplace=True)
        bars.sort_values(["code", "timestamp"], inplace=True)
        bars.drop_duplicates(["code", "timestamp"], keep="last", inplace=True)
    front.rename(
        columns={column: f"adj_{column.lower()}" for column in required - {"code", "timestamp"}},
        inplace=True,
    )
    raw.rename(
        columns={column: f"raw_{column.lower()}" for column in required - {"code", "timestamp"}},
        inplace=True,
    )
    frame = front.merge(
        raw,
        on=["code", "timestamp"],
        how="inner",
        validate="one_to_one",
    ).sort_values(["code", "timestamp"])
    frame["name"] = frame["code"].map(lambda code: names.get(str(code), ""))
    frame["limit_ratio"] = [
        price_limit_ratio(str(code), str(name))
        for code, name in zip(frame["code"], frame["name"])
    ]
    return frame.reset_index(drop=True)


def _add_point_in_time_features(
    frame: pd.DataFrame,
    hypotheses: Sequence[PullbackHypothesis],
) -> pd.DataFrame:
    result = frame.copy()
    grouped = result.groupby("code", sort=False)
    result["raw_previous_close"] = grouped["raw_close"].shift(1)
    result["raw_signal_return"] = result["raw_close"] / result["raw_previous_close"] - 1.0
    result["limit_up"] = result["raw_signal_return"] >= result["limit_ratio"] - 0.002
    result["return_20d"] = result["adj_close"] / grouped["adj_close"].shift(20) - 1.0
    result["current_return"] = result["adj_close"] / grouped["adj_close"].shift(1) - 1.0
    result["volatility_20d"] = grouped["adj_close"].transform(
        lambda values: values.pct_change(fill_method=None).rolling(
            20, min_periods=15
        ).std()
    )
    result["ma5"] = grouped["adj_close"].transform(
        lambda values: values.rolling(5, min_periods=5).mean()
    )
    result["ma10"] = grouped["adj_close"].transform(
        lambda values: values.rolling(10, min_periods=10).mean()
    )
    result["ma20"] = grouped["adj_close"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    result["previous_volume_20"] = grouped["adj_volume"].transform(
        lambda values: values.shift(1).rolling(20, min_periods=10).mean()
    )
    result["pullback_volume_3"] = grouped["adj_volume"].transform(
        lambda values: values.rolling(3, min_periods=3).mean()
    )
    result["pullback_volume_ratio"] = (
        result["pullback_volume_3"] / result["previous_volume_20"]
    )
    result["low_3d"] = grouped["adj_low"].transform(
        lambda values: values.rolling(3, min_periods=3).min()
    )
    result["turnover_20d"] = grouped["raw_amount"].transform(
        lambda values: values.rolling(20, min_periods=10).mean()
    )

    maximum_peak_age = max(item.maximum_peak_age for item in hypotheses)
    for age in range(2, maximum_peak_age + 1):
        result[f"close_lag_{age}"] = grouped["adj_close"].shift(age)
    for maximum_age in sorted(
        {7, 10} | {item.maximum_peak_age for item in hypotheses}
    ):
        columns = [f"close_lag_{age}" for age in range(2, maximum_age + 1)]
        result[f"peak_close_2_{maximum_age}"] = result[columns].max(axis=1)
        lag_values = result[columns].to_numpy(dtype=float)
        valid = np.isfinite(lag_values).any(axis=1)
        maximum_offsets = np.argmax(
            np.where(np.isfinite(lag_values), lag_values, -np.inf), axis=1
        )
        peak_ages = np.asarray(range(2, maximum_age + 1), dtype=float)[
            maximum_offsets
        ]
        peak_ages[~valid] = np.nan
        result[f"peak_age_2_{maximum_age}"] = peak_ages

    ordinal = grouped.cumcount()
    last_limit_ordinal = ordinal.where(result["limit_up"]).groupby(
        result["code"], sort=False
    ).ffill()
    result["latest_limit_age"] = ordinal - last_limit_ordinal
    result["latest_limit_close"] = result["adj_close"].where(
        result["limit_up"]
    ).groupby(result["code"], sort=False).ffill()
    for days in sorted({item.recent_limit_days for item in hypotheses}):
        result[f"recent_limit_{days}d"] = (
            result["latest_limit_age"].between(1, days).fillna(False)
        )

    result["entry_open"] = grouped["raw_open"].shift(-1)
    result["entry_low"] = grouped["raw_low"].shift(-1)
    result["entry_date"] = grouped["timestamp"].shift(-1)
    for horizon in RETURN_HORIZONS:
        result[f"exit_open_{horizon}d"] = grouped["raw_open"].shift(-(horizon + 1))
        result[f"exit_date_{horizon}d"] = grouped["timestamp"].shift(-(horizon + 1))
    return result


def _matched_control_base_mask(frame: pd.DataFrame) -> pd.Series:
    pullback_depth = 1.0 - frame["adj_close"] / frame["peak_close_2_10"]
    support_nearby = (
        frame["low_3d"].le(frame[["ma5", "ma10"]].max(axis=1) * 1.03)
        & frame["adj_close"].ge(frame["ma10"] * 0.95)
    )
    not_at_price_limit = (
        frame["raw_signal_return"].lt(frame["limit_ratio"] - 0.002)
        & frame["raw_signal_return"].gt(-frame["limit_ratio"] + 0.002)
    )
    return (
        frame["recent_limit_20d"]
        & frame["return_20d"].between(0.05, 0.60)
        & frame["peak_age_2_10"].between(2, 10)
        & pullback_depth.between(0.02, 0.15)
        & frame["pullback_volume_ratio"].between(0.20, 1.20)
        & frame["current_return"].between(-0.08, 0.06)
        & frame["adj_close"].gt(frame["ma20"])
        & frame["turnover_20d"].ge(20_000_000.0)
        & frame["volatility_20d"].notna()
        & frame["raw_close"].ge(2.0)
        & support_nearby
        & not_at_price_limit
        & ~frame["name"].str.upper().str.contains("ST", regex=False, na=False)
    ).fillna(False)


def _add_matching_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["peak_age"] = result["peak_age_2_10"]
    result["peak_close"] = result["peak_close_2_10"]
    result["pullback_depth"] = 1.0 - result["adj_close"] / result["peak_close"]
    result["distance_to_ma10"] = result["adj_close"] / result["ma10"] - 1.0
    result["log_turnover_20d"] = np.log(
        pd.to_numeric(result["turnover_20d"], errors="coerce").clip(lower=1.0)
    )
    return result


def _reclaim_confirmation_mask(
    frame: pd.DataFrame,
    base: pd.Series,
) -> pd.Series:
    return (
        base
        & frame["adj_close"].gt(frame["adj_open"])
        & frame["adj_close"].gt(
            frame["adj_close"].groupby(frame["code"], sort=False).shift(1)
        )
        & frame["adj_close"].ge(frame["ma5"])
        & frame["adj_close"].ge(frame["ma10"] * 0.98)
        & frame["current_return"].gt(0.0)
    ).fillna(False)


def _greedy_daily_matches(
    pool: pd.DataFrame,
    maximum_match_distance: float,
) -> pd.DataFrame:
    if pool.empty:
        return pd.DataFrame()
    pair_rows: list[dict[str, Any]] = []
    feature_names = list(MATCHING_FEATURE_SCALES)
    for signal_date, day in pool.groupby("signal_date", sort=True):
        treated = day.loc[day["treated"]].sort_values("code")
        available = day.loc[~day["treated"]].copy()
        if treated.empty or available.empty:
            continue
        for treatment in treated.itertuples(index=False):
            candidates = available.loc[
                np.isclose(
                    pd.to_numeric(available["limit_ratio"], errors="coerce"),
                    float(treatment.limit_ratio),
                )
            ].copy()
            if candidates.empty:
                continue
            distance = pd.Series(0.0, index=candidates.index)
            valid = pd.Series(True, index=candidates.index)
            for feature in feature_names:
                treatment_value = float(getattr(treatment, feature))
                candidate_values = pd.to_numeric(candidates[feature], errors="coerce")
                valid &= candidate_values.notna() & np.isfinite(treatment_value)
                distance += (
                    (candidate_values - treatment_value)
                    / MATCHING_FEATURE_SCALES[feature]
                ) ** 2
            distance = np.sqrt(distance)
            distance = distance.loc[valid]
            if distance.empty:
                continue
            best_distance = float(distance.min())
            best_index = sorted(
                distance.loc[np.isclose(distance, best_distance)].index,
                key=lambda index: str(candidates.loc[index, "code"]),
            )[0]
            if best_distance > maximum_match_distance:
                continue
            control = candidates.loc[best_index]
            row: dict[str, Any] = {
                "pair_id": f"{pd.Timestamp(signal_date).date()}:{treatment.code}:{control['code']}",
                "signal_date": pd.Timestamp(signal_date).normalize(),
                "treated_code": str(treatment.code),
                "control_code": str(control["code"]),
                "limit_ratio": float(treatment.limit_ratio),
                "match_distance": best_distance,
                "treated_entry_date": pd.Timestamp(treatment.entry_date).normalize(),
                "control_entry_date": pd.Timestamp(control["entry_date"]).normalize(),
            }
            for feature in feature_names:
                row[f"treated_{feature}"] = float(getattr(treatment, feature))
                row[f"control_{feature}"] = float(control[feature])
            for horizon in RETURN_HORIZONS:
                treated_return = float(getattr(treatment, f"net_return_{horizon}d"))
                control_return = float(control[f"net_return_{horizon}d"])
                row[f"treated_net_return_{horizon}d"] = treated_return
                row[f"control_net_return_{horizon}d"] = control_return
                row[f"effect_net_return_{horizon}d"] = treated_return - control_return
            pair_rows.append(row)
            available = available.drop(index=best_index)
    if not pair_rows:
        return pd.DataFrame()
    return pd.DataFrame(pair_rows).sort_values(
        ["signal_date", "treated_code", "control_code"]
    ).reset_index(drop=True)


def _hypothesis_mask(
    frame: pd.DataFrame,
    hypothesis: PullbackHypothesis,
) -> pd.Series:
    peak_age = frame[f"peak_age_2_{hypothesis.maximum_peak_age}"]
    if hypothesis.anchor == "latest_limit_close":
        anchor_close = frame["latest_limit_close"]
    else:
        anchor_close = frame[f"peak_close_2_{hypothesis.maximum_peak_age}"]
    pullback_depth = 1.0 - frame["adj_close"] / anchor_close
    base = (
        frame[f"recent_limit_{hypothesis.recent_limit_days}d"]
        & frame["return_20d"].ge(hypothesis.minimum_return_20d)
        & peak_age.between(hypothesis.minimum_peak_age, hypothesis.maximum_peak_age)
        & pullback_depth.between(
            hypothesis.minimum_pullback_depth, hypothesis.maximum_pullback_depth
        )
        & frame["pullback_volume_ratio"].le(hypothesis.maximum_volume_ratio)
        & frame["adj_close"].gt(frame["adj_open"])
        & frame["adj_close"].gt(frame["adj_close"].groupby(frame["code"]).shift(1))
        & frame["current_return"].le(0.06)
        & frame["adj_close"].gt(frame["ma20"])
        & frame["turnover_20d"].ge(20_000_000.0)
        & frame["raw_close"].ge(2.0)
        & ~frame["name"].str.upper().str.contains("ST", regex=False, na=False)
    )
    if hypothesis.support == "ma10":
        support = (
            frame["low_3d"].le(frame["ma10"] * 1.02)
            & frame["adj_close"].ge(frame["ma10"])
        )
    else:
        support = (
            frame["low_3d"].le(frame[["ma5", "ma10"]].max(axis=1) * 1.02)
            & frame["adj_close"].ge(frame["ma5"])
            & frame["adj_close"].ge(frame["ma10"] * 0.98)
        )
    return (base & support).fillna(False)


def _event_columns() -> list[str]:
    return [
        "code",
        "name",
        "timestamp",
        "raw_close",
        "adj_close",
        "limit_ratio",
        "return_20d",
        "current_return",
        "pullback_volume_ratio",
        "turnover_20d",
        "ma5",
        "ma10",
        "ma20",
        "latest_limit_age",
        "latest_limit_close",
        "peak_close_2_7",
        "peak_age_2_7",
        "peak_close_2_10",
        "peak_age_2_10",
        "entry_open",
        "entry_low",
        "entry_date",
        *[f"exit_open_{horizon}d" for horizon in RETURN_HORIZONS],
        *[f"exit_date_{horizon}d" for horizon in RETURN_HORIZONS],
    ]


def _rank_events(events: pd.DataFrame, maximum_daily_candidates: int) -> pd.DataFrame:
    result = events.rename(columns={"timestamp": "signal_date"}).copy()
    anchor = np.where(
        result["hypothesis_anchor"].eq("latest_limit_close"),
        result["latest_limit_close"],
        np.where(
            result["hypothesis_id"].eq("first_pullback_reclaim"),
            result["peak_close_2_7"],
            result["peak_close_2_10"],
        ),
    )
    if "pullback_depth" not in result.columns:
        result["pullback_depth"] = 1.0 - result["adj_close"] / pd.to_numeric(
            pd.Series(anchor, index=result.index), errors="coerce"
        )
    depth = pd.to_numeric(result["pullback_depth"], errors="coerce")
    result["depth_quality"] = (1.0 - (depth - 0.06).abs() / 0.09).clip(0.0, 1.0)
    result["strength_quality"] = (result["return_20d"] / 0.40).clip(0.0, 1.0)
    result["volume_quality"] = (
        (1.20 - result["pullback_volume_ratio"]) / 0.70
    ).clip(0.0, 1.0)
    result["liquidity_quality"] = result.groupby(
        ["hypothesis_id", "signal_date"], sort=False
    )["turnover_20d"].rank(method="average", pct=True)
    result["score"] = (
        0.35 * result["strength_quality"]
        + 0.25 * result["volume_quality"]
        + 0.20 * result["depth_quality"]
        + 0.20 * result["liquidity_quality"]
    )
    result = result.sort_values(
        ["hypothesis_id", "signal_date", "score", "code"],
        ascending=[True, True, False, True],
    )
    result["daily_rank"] = result.groupby(
        ["hypothesis_id", "signal_date"], sort=False
    ).cumcount() + 1
    result["selected"] = result["daily_rank"].le(maximum_daily_candidates)
    return result


def _rank_trend_rsi2_events(
    events: pd.DataFrame,
    maximum_daily_candidates: int,
) -> pd.DataFrame:
    result = events.copy()
    result["oversold_quality"] = (-result["return_3d"] / 0.12).clip(0.0, 1.0)
    result["trend_quality"] = (result["return_60d"] / 0.40).clip(0.0, 1.0)
    result["volume_quality"] = (
        (1.20 - result["current_volume_ratio"]) / 1.00
    ).clip(0.0, 1.0)
    result["liquidity_quality"] = result.groupby("signal_date", sort=False)[
        "turnover_20d"
    ].rank(method="average", pct=True)
    result["score"] = (
        0.35 * result["oversold_quality"]
        + 0.25 * result["trend_quality"]
        + 0.20 * result["volume_quality"]
        + 0.20 * result["liquidity_quality"]
    )
    result.sort_values(
        ["signal_date", "score", "code"],
        ascending=[True, False, True],
        inplace=True,
    )
    result["daily_rank"] = result.groupby("signal_date", sort=False).cumcount() + 1
    result["selected"] = result["daily_rank"].le(maximum_daily_candidates)
    return result


def _rank_ma20_bounce_events(
    events: pd.DataFrame,
    maximum_daily_candidates: int,
) -> pd.DataFrame:
    result = events.copy()
    result["trend_quality"] = (result["return_60d"] / 0.40).clip(0.0, 1.0)
    result["volume_quality"] = (
        (0.85 - result["current_volume_ratio"]) / 0.65
    ).clip(0.0, 1.0)
    result["support_quality"] = (
        1.0 - result["distance_to_ma20"].abs() / 0.02
    ).clip(0.0, 1.0)
    result["liquidity_quality"] = result.groupby("signal_date", sort=False)[
        "turnover_20d"
    ].rank(method="average", pct=True)
    result["score"] = (
        0.30 * result["trend_quality"]
        + 0.25 * result["volume_quality"]
        + 0.25 * result["support_quality"]
        + 0.20 * result["liquidity_quality"]
    )
    result.sort_values(
        ["signal_date", "score", "code"],
        ascending=[True, False, True],
        inplace=True,
    )
    result["daily_rank"] = result.groupby("signal_date", sort=False).cumcount() + 1
    result["selected"] = result["daily_rank"].le(maximum_daily_candidates)
    return result


def _rank_intraday_washout_events(
    events: pd.DataFrame,
    maximum_daily_candidates: int,
) -> pd.DataFrame:
    result = events.copy()
    result["recovery_quality"] = (
        (result["close_location"] - 0.70) / 0.30
    ).clip(0.0, 1.0)
    result["washout_quality"] = (
        -result["intraday_low_return"] / 0.10
    ).clip(0.0, 1.0)
    result["volume_quality"] = (
        1.0 - (result["current_volume_ratio"] - 1.80).abs() / 0.70
    ).clip(0.0, 1.0)
    result["liquidity_quality"] = result.groupby("signal_date", sort=False)[
        "turnover_20d"
    ].rank(method="average", pct=True)
    result["score"] = (
        0.35 * result["recovery_quality"]
        + 0.25 * result["washout_quality"]
        + 0.20 * result["volume_quality"]
        + 0.20 * result["liquidity_quality"]
    )
    result.sort_values(
        ["signal_date", "score", "code"],
        ascending=[True, False, True],
        inplace=True,
    )
    result["daily_rank"] = result.groupby("signal_date", sort=False).cumcount() + 1
    result["selected"] = result["daily_rank"].le(maximum_daily_candidates)
    return result


def _annotate_execution(
    events: pd.DataFrame,
    config: PortfolioConfig,
    cost_multiplier: float,
    entry_gap_min: float = -0.03,
    entry_gap_max: float = 0.08,
) -> pd.DataFrame:
    result = events.copy()
    result["open_gap"] = result["entry_open"] / result["raw_close"] - 1.0
    result["blocked_missing_bars"] = result["entry_open"].isna()
    result["blocked_limit_up_open"] = (
        result["entry_open"].notna()
        & result["entry_open"].ge(
            result["raw_close"] * (1.0 + result["limit_ratio"] - 0.001)
        )
    )
    result["blocked_open_gap"] = (
        result["entry_open"].notna()
        & ~result["blocked_limit_up_open"]
        & (~result["open_gap"].between(entry_gap_min, entry_gap_max))
    )
    result["executable"] = ~(
        result["blocked_missing_bars"]
        | result["blocked_limit_up_open"]
        | result["blocked_open_gap"]
    )
    for horizon in RETURN_HORIZONS:
        result[f"net_return_{horizon}d"] = [
            _trade_net_return(entry, exit_price, config, cost_multiplier)
            if executable
            else np.nan
            for entry, exit_price, executable in zip(
                result["entry_open"],
                result[f"exit_open_{horizon}d"],
                result["executable"],
            )
        ]
    return result


def _annotate_limit_order_execution(
    events: pd.DataFrame,
    config: PortfolioConfig,
    cost_multiplier: float,
    entry_discount: float,
) -> pd.DataFrame:
    result = events.copy()
    result["limit_order_price"] = result["raw_close"] * (1.0 - entry_discount)
    result["open_gap"] = result["entry_open"] / result["raw_close"] - 1.0
    result["blocked_missing_bars"] = (
        result["entry_open"].isna() | result["entry_low"].isna()
    )
    result["blocked_limit_up_open"] = (
        result["entry_open"].notna()
        & result["entry_open"].ge(
            result["raw_close"] * (1.0 + result["limit_ratio"] - 0.001)
        )
    )
    result["blocked_open_gap"] = (
        result["entry_open"].notna()
        & ~result["blocked_limit_up_open"]
        & ~result["open_gap"].between(-0.03, 0.08)
    )
    order_eligible = ~(
        result["blocked_missing_bars"]
        | result["blocked_limit_up_open"]
        | result["blocked_open_gap"]
    )
    result["filled_at_open"] = (
        order_eligible & result["entry_open"].le(result["limit_order_price"])
    )
    result["filled_at_limit"] = (
        order_eligible
        & result["entry_open"].gt(result["limit_order_price"])
        & result["entry_low"].le(result["limit_order_price"])
    )
    result["executable"] = result["filled_at_open"] | result["filled_at_limit"]
    result["unfilled_limit_order"] = order_eligible & ~result["executable"]
    result["entry_price"] = np.where(
        result["filled_at_open"],
        result["entry_open"],
        np.where(result["filled_at_limit"], result["limit_order_price"], np.nan),
    )
    for horizon in RETURN_HORIZONS:
        result[f"net_return_{horizon}d"] = [
            _trade_net_return(entry, exit_price, config, cost_multiplier)
            if executable
            else np.nan
            for entry, exit_price, executable in zip(
                result["entry_price"],
                result[f"exit_open_{horizon}d"],
                result["executable"],
            )
        ]
    return result


def _trade_net_return(
    entry_open: float,
    exit_open: float,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> float:
    if not np.isfinite(entry_open) or not np.isfinite(exit_open) or entry_open <= 0:
        return np.nan
    buy_price = float(entry_open) * (1.0 + config.slippage_rate * cost_multiplier)
    sell_price = float(exit_open) * (1.0 - config.slippage_rate * cost_multiplier)
    target_cash = config.initial_cash * 0.10
    quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
    if quantity <= 0:
        return np.nan
    buy_value = buy_price * quantity
    buy_fee = max(
        config.min_commission * cost_multiplier,
        buy_value * config.commission_rate * cost_multiplier,
    )
    sell_value = sell_price * quantity
    sell_fee = max(
        config.min_commission * cost_multiplier,
        sell_value * config.commission_rate * cost_multiplier,
    ) + sell_value * config.stamp_duty_rate * cost_multiplier
    return float((sell_value - sell_fee - buy_value - buy_fee) / (buy_value + buy_fee))


def _normalize_market_states(states: pd.DataFrame | None) -> pd.DataFrame:
    columns = [
        "timestamp",
        "market_phase",
        "market_style",
        "market_score",
        "market_regime",
        "market_entry_allowed",
        "top_themes",
    ]
    if states is None or states.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, Any]] = []
    for item in states.to_dict("records"):
        try:
            state = json.loads(str(item.get("state_json") or "{}"))
        except json.JSONDecodeError:
            state = {}
        strong_sectors = state.get("strong_sectors") or item.get("strong_sectors") or []
        top_themes = [
            value
            for value in strong_sectors[:3]
            if str(value.get("theme_phase", "")) in ALLOWED_THEME_PHASES
        ]
        rows.append(
            {
                "timestamp": pd.Timestamp(item.get("timestamp")).normalize(),
                "market_phase": str(
                    item.get("market_phase") or state.get("market_phase") or ""
                ),
                "market_style": str(
                    item.get("market_style") or state.get("market_style") or ""
                ),
                "market_score": float(state.get("market_score") or 0.0),
                "market_regime": str(state.get("market_regime") or ""),
                "market_entry_allowed": bool(
                    item.get("entry_allowed")
                    if item.get("entry_allowed") is not None
                    else state.get("entry_allowed", False)
                ),
                "top_themes": top_themes,
            }
        )
    return pd.DataFrame(rows).drop_duplicates("timestamp", keep="last")


def _member_sector_map(
    sector_membership: pd.DataFrame | None,
) -> dict[str, set[str]]:
    if sector_membership is None or sector_membership.empty:
        return {}
    required = {"sector_code", "member_code"}
    if not required.issubset(sector_membership.columns):
        raise ValueError(
            f"sector_membership is missing columns: {sorted(required - set(sector_membership.columns))}"
        )
    result: dict[str, set[str]] = {}
    for row in sector_membership.loc[:, ["sector_code", "member_code"]].itertuples(
        index=False
    ):
        result.setdefault(str(row.member_code), set()).add(str(row.sector_code))
    return result


def _realized_equity_curve(
    events: pd.DataFrame,
    target_weight: float,
    *,
    holding_days: int,
) -> pd.Series:
    if events.empty:
        return pd.Series(dtype=float)
    returns = (
        events.assign(
            weighted_return=pd.to_numeric(
                events[f"net_return_{holding_days}d"], errors="coerce"
            )
            * target_weight
        )
        .groupby(f"exit_date_{holding_days}d", sort=True)["weighted_return"]
        .sum()
    )
    return (1.0 + returns).cumprod()


def _empty_event_table() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "code",
            "signal_date",
            "hypothesis_id",
            "selected",
            "executable",
            "market_gate",
            "theme_gate",
            "net_return_5d",
        ]
    )
