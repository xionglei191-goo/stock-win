from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


PROJECT_ID = "early_winner_v1"
RULE_STRATEGY_ID = "early_winner_rule_v1"
ML_STRATEGY_ID = "early_winner_ml_v1"


def _as_string_list(value: Any) -> list[str]:
    if value is None or value is pd.NA:
        return []
    if isinstance(value, np.ndarray):
        values = value.tolist()
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        try:
            if bool(pd.isna(value)):
                return []
        except (TypeError, ValueError):
            pass
        values = [value]
    return [str(item) for item in values if item is not None and str(item)]

FEATURE_COLUMNS = (
    "industry_momentum",
    "industry_breadth",
    "industry_amount_trend",
    "revenue_yoy",
    "profit_yoy",
    "gross_margin_change",
    "roe",
    "ocf_profit_ratio",
    "forecast_revision",
    "return_20",
    "return_60",
    "return_120",
    "relative_return_20",
    "relative_return_60",
    "relative_return_120",
    "volume_ratio",
    "amount_ratio",
    "breakout_distance",
    "ma20_slope",
    "event_score",
    "northbound_change_ratio",
    "institution_lhb_ratio",
    "institution_holding_change_ratio",
    "shareholder_count_change",
    "turnover_20",
    "price_to_ma60",
    "valuation_percentile",
)

HARD_NEGATIVE_EVENT_TYPES = {
    "CLARIFICATION",
    "REDUCTION",
    "RISK_WARNING",
}


@dataclass(frozen=True)
class EarlyWinnerParameters:
    minimum_listing_days: int = 120
    minimum_valid_days_20: int = 18
    minimum_adv20: float = 100_000_000.0
    maximum_candidates: int = 20
    maximum_industry_candidates: int = 5
    industry_rank_threshold: float = 0.70
    industry_breadth_threshold: float = 0.55
    rs60_rank_threshold: float = 0.80
    minimum_volume_ratio: float = 1.50
    minimum_amount_ratio: float = 1.30
    minimum_revenue_growth: float = 20.0
    minimum_profit_growth: float = 30.0
    minimum_forecast_revision: float = 5.0


def classify_announcement(title: str, text: str = "") -> dict[str, Any]:
    content = f"{title} {text}".strip()
    negative_rules = (
        ("CLARIFICATION", -3.0, ("澄清", "更正公告")),
        ("REDUCTION", -2.0, ("减持", "拟减持")),
        ("RISK_WARNING", -1.0, ("风险提示", "退市风险", "立案")),
    )
    positive_rules = (
        ("EARNINGS_FORECAST", 3.0, ("业绩预增", "扭亏为盈", "业绩快报")),
        ("ACQUISITION", 3.0, ("重大资产重组", "收购")),
        ("PRICE_INCREASE", 2.0, ("涨价", "价格调整")),
        ("MAJOR_ORDER", 2.0, ("重大合同", "中标", "大额订单")),
        ("CONTROL_CHANGE", 2.0, ("控制权变更", "实际控制人变更")),
        ("EXPANSION", 1.0, ("扩产", "投产", "产能建设")),
        ("BUYBACK", 1.0, ("回购股份", "股份回购")),
    )
    for event_type, score, keywords in negative_rules:
        if any(keyword in content for keyword in keywords):
            return {
                "event_type": event_type,
                "score": score,
                "confidence": 1.0,
                "hard_negative": True,
                "requires_ai_review": False,
            }
    for event_type, score, keywords in positive_rules:
        if any(keyword in content for keyword in keywords):
            return {
                "event_type": event_type,
                "score": score,
                "confidence": 0.90,
                "hard_negative": False,
                "requires_ai_review": False,
            }
    return {
        "event_type": "UNCLASSIFIED",
        "score": 0.0,
        "confidence": 0.25,
        "hard_negative": False,
        "requires_ai_review": True,
    }


def effective_publication_time(
    published_at: str | pd.Timestamp,
    trading_days: Iterable[str | pd.Timestamp],
) -> pd.Timestamp:
    published = pd.Timestamp(published_at)
    if published.tzinfo is not None:
        published = published.tz_convert("Asia/Shanghai").tz_localize(None)
    days = sorted({pd.Timestamp(day).normalize() for day in trading_days})
    if not days:
        raise ValueError("Trading calendar is required for point-in-time publication joins")
    publication_day = published.normalize()
    after_close = published > publication_day + pd.Timedelta(hours=15)
    for day in days:
        if day > publication_day or (day == publication_day and not after_close):
            return day + pd.Timedelta(hours=15)
    raise ValueError("Publication falls outside the available trading calendar")


def point_in_time_latest(
    records: Iterable[Mapping[str, Any]],
    asof: str | pd.Timestamp,
    *,
    published_field: str = "published_at",
    period_field: str = "period_end",
) -> dict[str, Any] | None:
    boundary = pd.Timestamp(asof)
    eligible: list[tuple[pd.Timestamp, pd.Timestamp, dict[str, Any]]] = []
    for raw in records:
        if not raw.get(published_field):
            continue
        published = pd.Timestamp(raw[published_field])
        if published > boundary:
            continue
        period = pd.Timestamp(raw.get(period_field) or published)
        eligible.append((period, published, dict(raw)))
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[0], item[1]))[2]


def technical_feature_row(
    code: str,
    bars: pd.DataFrame,
    benchmark: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    if bars.empty or len(bars) < 121:
        return None
    frame = bars.sort_index().copy()
    numeric: dict[str, pd.Series] = {}
    for column in ("Open", "High", "Low", "Close", "Volume", "Amount"):
        if column not in frame:
            return None
        numeric[column] = pd.to_numeric(frame[column], errors="coerce")
    close = numeric["Close"]
    latest = len(frame) - 1
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    prior_high = numeric["High"].rolling(60).max().shift(1)
    previous_volume = numeric["Volume"].shift(1).rolling(20).mean()
    previous_amount = numeric["Amount"].shift(1).rolling(20).mean()
    if any(
        pd.isna(value)
        for value in (
            ma20.iloc[latest],
            ma60.iloc[latest],
            prior_high.iloc[latest],
            previous_volume.iloc[latest],
            previous_amount.iloc[latest],
        )
    ):
        return None

    def period_return(period: int) -> float:
        base = float(close.iloc[-period - 1])
        return float(close.iloc[-1] / base - 1.0) if base > 0 else float("nan")

    benchmark_returns: dict[int, float] = {20: 0.0, 60: 0.0, 120: 0.0}
    if benchmark is not None and not benchmark.empty and "Close" in benchmark:
        benchmark_close = pd.to_numeric(benchmark["Close"], errors="coerce").dropna()
        for period in benchmark_returns:
            if len(benchmark_close) > period and float(benchmark_close.iloc[-period - 1]) > 0:
                benchmark_returns[period] = float(
                    benchmark_close.iloc[-1] / benchmark_close.iloc[-period - 1] - 1.0
                )
    amount20 = float(numeric["Amount"].tail(20).mean())
    latest_close = float(close.iloc[-1])
    return {
        "code": code,
        "asof": pd.Timestamp(frame.index[-1]).date().isoformat(),
        "listed_days": int(len(frame)),
        "valid_days_20": int(close.tail(20).notna().sum()),
        "adv20": amount20,
        "avg_volume_20": float(numeric["Volume"].tail(20).mean()),
        "suspended": bool(float(numeric["Volume"].iloc[-1] or 0.0) <= 0),
        "return_20": period_return(20),
        "return_60": period_return(60),
        "return_120": period_return(120),
        "relative_return_20": period_return(20) - benchmark_returns[20],
        "relative_return_60": period_return(60) - benchmark_returns[60],
        "relative_return_120": period_return(120) - benchmark_returns[120],
        "prior_high_60": float(prior_high.iloc[-1]),
        "breakout_distance": latest_close / float(prior_high.iloc[-1]) - 1.0,
        "volume_ratio": float(numeric["Volume"].iloc[-1] / previous_volume.iloc[-1]),
        "amount_ratio": float(numeric["Amount"].iloc[-1] / previous_amount.iloc[-1]),
        "close": latest_close,
        "ma20": float(ma20.iloc[-1]),
        "ma60": float(ma60.iloc[-1]),
        "ma20_slope": float(ma20.iloc[-1] / ma20.iloc[-6] - 1.0),
        "price_to_ma60": latest_close / float(ma60.iloc[-1]),
    }


def build_technical_feature_rows(
    bars: Mapping[str, pd.DataFrame],
    *,
    benchmark: pd.DataFrame | None = None,
    names: Mapping[str, str] | None = None,
    supplemental: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code, frame in bars.items():
        technical = technical_feature_row(code, frame, benchmark)
        if technical is None:
            continue
        technical["name"] = str((names or {}).get(code, ""))
        technical.update(dict((supplemental or {}).get(code, {})))
        rows.append(technical)
    return rows


def mark_research_universe_eligibility(
    rows: Iterable[Mapping[str, Any]],
    parameters: EarlyWinnerParameters | None = None,
) -> list[dict[str, Any]]:
    """Apply the shared stock-pool and new-position heat gates used by ML and RS60."""
    params = parameters or EarlyWinnerParameters()
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty:
        return []
    defaults: dict[str, Any] = {
        "listed_days": 0,
        "valid_days_20": 0,
        "adv20": 0.0,
        "suspended": False,
        "is_st": False,
        "is_quit": False,
        "return_60": np.nan,
        "turnover_20": np.nan,
        "price_to_ma60": np.nan,
    }
    for column, default in defaults.items():
        if column not in frame:
            frame[column] = default
    return_rank = pd.to_numeric(frame["return_60"], errors="coerce").rank(
        pct=True, method="average"
    )
    turnover_rank = pd.to_numeric(frame["turnover_20"], errors="coerce").rank(
        pct=True, method="average"
    )
    extreme_heat = (
        (return_rank >= 0.99)
        & (turnover_rank >= 0.95)
        & (pd.to_numeric(frame["price_to_ma60"], errors="coerce") > 1.80)
    )
    base_gate = (
        (pd.to_numeric(frame["listed_days"], errors="coerce") >= params.minimum_listing_days)
        & (pd.to_numeric(frame["valid_days_20"], errors="coerce") >= params.minimum_valid_days_20)
        & (pd.to_numeric(frame["adv20"], errors="coerce") >= params.minimum_adv20)
        & ~frame["suspended"].fillna(False).astype(bool)
        & ~frame["is_st"].fillna(False).astype(bool)
        & ~frame["is_quit"].fillna(False).astype(bool)
        & pd.to_numeric(frame.get("relative_return_60"), errors="coerce").notna()
    )
    frame["eligible"] = base_gate & ~extreme_heat
    frame["universe_gate"] = base_gate
    frame["extreme_heat"] = extreme_heat
    return frame.to_dict("records")


def is_one_price_limit(
    bar: Mapping[str, Any],
    previous_close: float,
    *,
    ratio: float,
    side: str,
) -> bool:
    """Return whether a raw daily bar is locked at the relevant price limit."""
    if not np.isfinite(previous_close) or previous_close <= 0 or side not in {"buy", "sell"}:
        return False
    prices = [float(bar.get(column) or 0.0) for column in ("Open", "High", "Low", "Close")]
    if any(not np.isfinite(price) or price <= 0 for price in prices):
        return False
    multiplier = Decimal("1") + (
        Decimal(str(ratio)) if side == "buy" else -Decimal(str(ratio))
    )
    limit_price = float(
        (Decimal(str(previous_close)) * multiplier).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )
    # All four raw prices must equal the legal one-cent limit price.  Half a
    # tick only absorbs binary floating-point noise; it must not accept the
    # adjacent quote at rounding boundaries such as 10.05 * 1.10 = 11.055.
    tolerance = 0.005 + 1e-12
    return all(abs(price - limit_price) <= tolerance for price in prices)


def _positive_forward_factor(value: Any) -> float | None:
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return None
    return factor if np.isfinite(factor) and factor > 0 else None


def _corporate_action_reference_price(
    previous_close: float,
    previous_factor: float | None,
    current_factor: float | None,
) -> float:
    if previous_factor is None or current_factor is None:
        return previous_close
    return previous_close * previous_factor / current_factor


def _sparse_status_series(
    histories: Mapping[str, Any] | None,
    code: str,
) -> pd.Series:
    if histories is None or code not in histories:
        return pd.Series(dtype=float)
    raw = histories[code]
    if isinstance(raw, pd.DataFrame):
        if raw.shape[1] != 1:
            raise ValueError(f"status history for {code} must contain exactly one column")
        series = raw.iloc[:, 0].copy()
    elif isinstance(raw, pd.Series):
        series = raw.copy()
    elif isinstance(raw, Mapping):
        series = pd.Series(dict(raw))
    else:
        raise TypeError(f"status history for {code} must be a Series or mapping")
    dates = pd.to_datetime(series.index, errors="coerce")
    valid = ~dates.isna()
    if not bool(valid.any()):
        return pd.Series(dtype=float)
    normalized_dates = pd.DatetimeIndex(dates[valid]).tz_localize(None).normalize()
    normalized = pd.Series(series.to_numpy()[valid], index=normalized_dates)
    return normalized.groupby(level=0, sort=True).last()


def _status_code(value: Any) -> int | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)


def _security_is_st_on_session(
    initial_is_st: bool,
    decision: pd.Timestamp,
    session: pd.Timestamp,
    changes: pd.Series,
) -> bool:
    is_st = initial_is_st
    if changes.empty:
        return is_st
    applicable = changes.loc[(changes.index > decision) & (changes.index <= session)]
    for value in applicable:
        status = _status_code(value)
        if status in {2, 3}:
            is_st = True
        elif status == 4:
            is_st = False
    return is_st


def _limit_status_on_session(statuses: pd.Series, session: pd.Timestamp) -> int | None:
    if statuses.empty or session not in statuses.index:
        return None
    return _status_code(statuses.loc[session])


def _is_one_price_bar(bar: Mapping[str, Any]) -> bool:
    prices = [float(bar.get(column) or 0.0) for column in ("Open", "High", "Low", "Close")]
    if any(not np.isfinite(price) or price <= 0 for price in prices):
        return False
    return max(prices) - min(prices) <= 0.005 + 1e-12


def _is_gp15_one_price_lock(
    bar: Mapping[str, Any],
    status: int,
    *,
    side: str,
) -> bool:
    expected = 2 if side == "buy" else -2
    return status == expected and _is_one_price_bar(bar)


def historical_price_limit_ratio(
    code: str,
    name: str,
    session: Any,
    *,
    listed_days: int | None = None,
) -> float | None:
    """Return the effective A-share daily limit for an historical session.

    ``None`` represents an IPO no-limit window.  The caller may conservatively
    treat those sessions as non-executable when the exact listing sequence is
    unavailable.  The V4 research universe already requires 120 listed
    sessions, so the branch is primarily an audit guard for synthetic inputs.
    """
    timestamp = pd.Timestamp(session).normalize()
    number = str(code).split(".", 1)[0]
    if str(code).endswith(".BJ"):
        if listed_days is not None and int(listed_days) <= 1:
            return None
        return 0.30
    if number.startswith(("688", "689")):
        if listed_days is not None and int(listed_days) <= 5:
            return None
        return 0.20
    if number.startswith(("300", "301")):
        if timestamp >= pd.Timestamp("2020-08-24"):
            if listed_days is not None and int(listed_days) <= 5:
                return None
            return 0.20
        return 0.05 if "ST" in str(name).upper() else 0.10
    if "ST" in str(name).upper():
        return 0.05
    return 0.10


def early_winner_exit_reason(
    *,
    current_rank: float | None,
    close: float,
    ma60: float | None,
    event_type: str = "",
    drawdown_from_high: float = 0.0,
) -> str | None:
    if event_type in HARD_NEGATIVE_EVENT_TYPES:
        return "MAJOR_NEGATIVE_EVENT"
    if ma60 is not None and np.isfinite(ma60) and close < ma60:
        return "BELOW_MA60"
    if drawdown_from_high <= -0.25:
        return "DRAWDOWN_25"
    if current_rank is not None and np.isfinite(current_rank) and current_rank > 40:
        return "RANK_OUTSIDE_40"
    return None


def attach_execution_outcomes(
    feature_frame: pd.DataFrame,
    raw_bars: Mapping[str, pd.DataFrame],
    *,
    weekly_states: Mapping[str, pd.DataFrame] | None = None,
    holding_days: int = 60,
    portfolio_value: float = 1_000_000.0,
    maximum_weight: float = 0.05,
    maximum_adv_ratio: float = 0.02,
    trading_calendar: Iterable[Any] | None = None,
    require_forward_factor: bool = False,
    security_status_history: Mapping[str, Any] | None = None,
    limit_status_history: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    """Attach next-open, T+1, limit-lock and liquidity-capped outcomes to PIT rows."""
    output = feature_frame.copy()
    return_column = f"forward_return_{int(holding_days)}"
    normalized_bars = {
        str(code): frame.sort_index()
        for code, frame in raw_bars.items()
        if frame is not None and not frame.empty
    }
    market_sessions = None
    if trading_calendar is not None:
        market_sessions = pd.DatetimeIndex(
            pd.to_datetime(list(trading_calendar), errors="coerce")
        ).dropna().normalize().sort_values().unique()
        if not len(market_sessions):
            raise ValueError("trading_calendar must contain at least one valid session")
    records: list[dict[str, Any]] = []
    for raw in output.to_dict("records"):
        item = dict(raw)
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        historical_is_st = bool(item.get("is_st", "ST" in name.upper()))
        security_changes = _sparse_status_series(security_status_history, code)
        limit_statuses = _sparse_status_series(limit_status_history, code)
        bars = normalized_bars.get(code)
        result: dict[str, Any] = {
            "entry_executable": False,
            "planned_entry_time": None,
            "entry_time": None,
            "entry_price": np.nan,
            "entry_forward_factor": np.nan,
            "exit_time": None,
            "planned_exit_time": None,
            "exit_price": np.nan,
            "exit_forward_factor": np.nan,
            "exit_delay_days": 0,
            "exit_reason": None,
            "order_value": 0.0,
            return_column: np.nan,
        }
        if bars is None or bars.empty:
            records.append({**item, **result})
            continue
        frame = bars
        decision = pd.Timestamp(item["asof"]).normalize()
        normalized_bar_dates = pd.to_datetime(frame.index).normalize()
        sessions = market_sessions if market_sessions is not None else normalized_bar_dates.unique()
        entry_session_position = int(sessions.searchsorted(decision, side="right"))
        if entry_session_position >= len(sessions):
            records.append({**item, **result})
            continue
        entry_date = pd.Timestamp(sessions[entry_session_position]).normalize()
        result["planned_entry_time"] = entry_date.isoformat()
        entry_position = int(normalized_bar_dates.searchsorted(entry_date, side="left"))
        if (
            entry_position <= 0
            or entry_position >= len(frame)
            or normalized_bar_dates[entry_position] != entry_date
        ):
            # A missing bar on the market's next session is a suspension/non-trading
            # stock, not permission to move the order to its eventual resumption day.
            records.append({**item, **result})
            continue
        entry = frame.iloc[entry_position]
        previous_entry = frame.iloc[entry_position - 1]
        previous_close = float(previous_entry.get("Close") or 0.0)
        entry_volume = float(entry.get("Volume") or 0.0)
        entry_price = float(entry.get("Open") or 0.0)
        observed_entry_factor = _positive_forward_factor(entry.get("ForwardFactor"))
        observed_previous_factor = _positive_forward_factor(
            previous_entry.get("ForwardFactor")
        )
        if require_forward_factor and (
            observed_entry_factor is None or observed_previous_factor is None
        ):
            records.append({**item, **result})
            continue
        entry_factor = observed_entry_factor or 1.0
        entry_reference_price = _corporate_action_reference_price(
            previous_close,
            observed_previous_factor,
            observed_entry_factor,
        )
        listed_days = item.get("listed_days")
        try:
            listed_days_value = int(listed_days) + 1 if pd.notna(listed_days) else None
        except (TypeError, ValueError):
            listed_days_value = None
        entry_is_st = _security_is_st_on_session(
            historical_is_st,
            decision,
            entry_date,
            security_changes,
        )
        ratio = historical_price_limit_ratio(
            code,
            "ST" if entry_is_st else "",
            entry_date,
            listed_days=listed_days_value,
        )
        entry_limit_status = _limit_status_on_session(limit_statuses, entry_date)
        if entry_limit_status is None and ratio is None:
            records.append({**item, **result})
            continue
        order_value = min(
            portfolio_value * maximum_weight,
            max(0.0, float(item.get("adv20") or 0.0)) * maximum_adv_ratio,
        )
        entry_locked = (
            _is_gp15_one_price_lock(
                entry,
                entry_limit_status,
                side="buy",
            )
            if entry_limit_status is not None
            else is_one_price_limit(
                entry, entry_reference_price, ratio=ratio, side="buy"
            )
        )
        if (
            entry_volume <= 0
            or entry_price <= 0
            or order_value <= 0
            or entry_locked
        ):
            records.append({**item, **result})
            continue
        # The planned exit is N sessions after entry and therefore always satisfies T+1.
        exit_session_position = entry_session_position + holding_days
        if exit_session_position >= len(sessions):
            records.append({**item, **result})
            continue
        planned_exit_date = pd.Timestamp(sessions[exit_session_position]).normalize()
        result["planned_exit_time"] = planned_exit_date.isoformat()
        exit_session = exit_session_position
        exit_reason = f"TIME_{int(holding_days)}D"
        states = (weekly_states or {}).get(code)
        if states is not None and not states.empty:
            peak = entry_price
            for state_time, state in states.sort_index().iterrows():
                state_date = pd.Timestamp(state_time).normalize()
                if state_date <= normalized_bar_dates[entry_position]:
                    continue
                if state_date >= planned_exit_date:
                    break
                state_close = float(state.get("close") or 0.0)
                if state_close <= 0:
                    continue
                peak = max(peak, state_close)
                reason = early_winner_exit_reason(
                    current_rank=(
                        float(state["rank"])
                        if pd.notna(state.get("rank"))
                        else None
                    ),
                    close=state_close,
                    ma60=(
                        float(state["ma60"])
                        if pd.notna(state.get("ma60"))
                        else None
                    ),
                    event_type=str(state.get("event_type") or ""),
                    drawdown_from_high=state_close / peak - 1.0,
                )
                if reason is None:
                    continue
                next_session = int(sessions.searchsorted(state_date, side="right"))
                if next_session < exit_session and next_session > entry_session_position:
                    exit_session = next_session
                    exit_reason = reason
                    break
        delayed = 0
        exit_position = -1
        while exit_session < len(sessions):
            exit_date = pd.Timestamp(sessions[exit_session]).normalize()
            candidate_position = int(normalized_bar_dates.searchsorted(exit_date, side="left"))
            if (
                candidate_position >= len(frame)
                or normalized_bar_dates[candidate_position] != exit_date
            ):
                exit_session += 1
                delayed += 1
                continue
            exit_position = candidate_position
            exit_bar = frame.iloc[exit_position]
            previous_exit_bar = frame.iloc[exit_position - 1]
            exit_previous_close = float(previous_exit_bar.get("Close") or 0.0)
            observed_exit_factor = _positive_forward_factor(
                exit_bar.get("ForwardFactor")
            )
            observed_exit_previous_factor = _positive_forward_factor(
                previous_exit_bar.get("ForwardFactor")
            )
            if require_forward_factor and (
                observed_exit_factor is None
                or observed_exit_previous_factor is None
            ):
                exit_position = -1
                break
            exit_reference_price = _corporate_action_reference_price(
                exit_previous_close,
                observed_exit_previous_factor,
                observed_exit_factor,
            )
            exit_is_st = _security_is_st_on_session(
                historical_is_st,
                decision,
                exit_date,
                security_changes,
            )
            exit_ratio = historical_price_limit_ratio(
                code, "ST" if exit_is_st else "", exit_date
            )
            exit_limit_status = _limit_status_on_session(limit_statuses, exit_date)
            if exit_limit_status is None and exit_ratio is None:
                exit_session += 1
                delayed += 1
                continue
            exit_locked = (
                _is_gp15_one_price_lock(
                    exit_bar,
                    exit_limit_status,
                    side="sell",
                )
                if exit_limit_status is not None
                else is_one_price_limit(
                    exit_bar,
                    exit_reference_price,
                    ratio=exit_ratio,
                    side="sell",
                )
            )
            if float(exit_bar.get("Volume") or 0.0) > 0 and not exit_locked:
                break
            exit_session += 1
            delayed += 1
        if exit_position < 0 or exit_session >= len(sessions):
            records.append({**item, **result})
            continue
        exit_bar = frame.iloc[exit_position]
        exit_price = float(exit_bar.get("Open") or 0.0)
        observed_exit_factor = _positive_forward_factor(exit_bar.get("ForwardFactor"))
        if require_forward_factor and observed_exit_factor is None:
            records.append({**item, **result})
            continue
        exit_factor = observed_exit_factor or 1.0
        if exit_price <= 0:
            records.append({**item, **result})
            continue
        result.update(
            {
                "entry_executable": True,
                "entry_time": pd.Timestamp(frame.index[entry_position]).isoformat(),
                "entry_price": entry_price,
                "entry_forward_factor": entry_factor,
                "exit_time": pd.Timestamp(frame.index[exit_position]).isoformat(),
                "exit_price": exit_price,
                "exit_forward_factor": exit_factor,
                "exit_delay_days": delayed,
                "exit_reason": exit_reason,
                "order_value": order_value,
                # Raw opens are execution prices; the factor ratio carries
                # cash/stock corporate actions into the total-return label.
                return_column: (
                    exit_price * exit_factor / (entry_price * entry_factor) - 1.0
                ),
            }
        )
        records.append({**item, **result})
    return pd.DataFrame(records)


def score_rule_candidates(
    rows: Iterable[Mapping[str, Any]],
    parameters: EarlyWinnerParameters | None = None,
) -> list[dict[str, Any]]:
    params = parameters or EarlyWinnerParameters()
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty:
        return []
    numeric_defaults = {
        "listed_days": 0,
        "valid_days_20": 0,
        "adv20": 0.0,
        "close": np.nan,
        "ma20": np.nan,
        "ma60": np.nan,
        "relative_return_20": np.nan,
        "relative_return_60": np.nan,
        "relative_return_120": np.nan,
    }
    for column, default in numeric_defaults.items():
        if column not in frame:
            frame[column] = default
    for column in ("suspended", "is_st", "is_quit"):
        if column not in frame:
            frame[column] = False
    for column in FEATURE_COLUMNS:
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    rank_fields = {
        "industry_momentum": "industry_momentum_pct",
        "revenue_yoy": "revenue_yoy_pct",
        "profit_yoy": "profit_yoy_pct",
        "gross_margin_change": "gross_margin_change_pct",
        "roe": "roe_pct",
        "ocf_profit_ratio": "ocf_profit_ratio_pct",
        "relative_return_20": "rs20_pct",
        "relative_return_60": "rs60_pct",
        "relative_return_120": "rs120_pct",
        "breakout_distance": "breakout_distance_pct",
        "volume_ratio": "volume_ratio_pct",
        "amount_ratio": "amount_ratio_pct",
        "ma20_slope": "ma20_slope_pct",
        "northbound_change_ratio": "northbound_pct",
        "institution_lhb_ratio": "institution_lhb_pct",
        "institution_holding_change_ratio": "institution_holding_pct",
        "shareholder_count_change": "shareholder_count_pct",
        "turnover_20": "turnover_pct",
        "return_60": "heat_return_pct",
        "price_to_ma60": "price_to_ma60_pct",
    }
    for source, target in rank_fields.items():
        if source not in frame:
            frame[source] = np.nan
        ascending = source != "shareholder_count_change"
        frame[target] = frame[source].rank(pct=True, method="average", ascending=ascending)
    frame["valuation_percentile"] = frame["valuation_percentile"].fillna(0.50).clip(0, 1)
    frame["industry_breadth"] = frame["industry_breadth"].fillna(0.0)
    frame["industry_amount_trend"] = frame["industry_amount_trend"].fillna(0.0)
    frame["event_score"] = frame["event_score"].fillna(0.0).clip(-4, 3)

    frame["industry_score"] = 100 * (
        0.60 * frame["industry_momentum_pct"].fillna(0.0)
        + 0.25 * frame["industry_breadth"].clip(0, 1)
        + 0.15 * (frame["industry_amount_trend"] > 0).astype(float)
    )
    frame["fundamental_score"] = 100 * (
        0.30 * frame["revenue_yoy_pct"].fillna(0.0)
        + 0.30 * frame["profit_yoy_pct"].fillna(0.0)
        + 0.20 * frame["gross_margin_change_pct"].fillna(0.0)
        + 0.10 * frame["roe_pct"].fillna(0.0)
        + 0.10 * frame["ocf_profit_ratio_pct"].fillna(0.0)
    )
    frame["momentum_score"] = 100 * (
        0.20 * frame["rs20_pct"].fillna(0.0)
        + 0.50 * frame["rs60_pct"].fillna(0.0)
        + 0.30 * frame["rs120_pct"].fillna(0.0)
    )
    frame["breakout_score"] = 100 * (
        0.35 * frame["breakout_distance_pct"].fillna(0.0)
        + 0.25 * frame["volume_ratio_pct"].fillna(0.0)
        + 0.20 * frame["amount_ratio_pct"].fillna(0.0)
        + 0.20 * frame["ma20_slope_pct"].fillna(0.0)
    )
    frame["event_component"] = ((frame["event_score"] + 4.0) / 7.0 * 100).clip(0, 100)
    frame["flow_score"] = 100 * (
        0.30 * frame["northbound_pct"].fillna(0.50)
        + 0.25 * frame["institution_lhb_pct"].fillna(0.50)
        + 0.25 * frame["institution_holding_pct"].fillna(0.50)
        + 0.20 * frame["shareholder_count_pct"].fillna(0.50)
    )
    frame["heat_percentile"] = (
        0.25 * frame["rs20_pct"].fillna(0.50)
        + 0.20 * frame["heat_return_pct"].fillna(0.50)
        + 0.20 * frame["turnover_pct"].fillna(0.50)
        + 0.15 * frame["volume_ratio_pct"].fillna(0.50)
        + 0.20 * frame["valuation_percentile"]
    )
    frame["heat_penalty"] = ((frame["heat_percentile"] - 0.90).clip(lower=0) / 0.10 * 20).clip(0, 20)
    frame["rule_score"] = (
        0.25 * frame["industry_score"]
        + 0.20 * frame["fundamental_score"]
        + 0.20 * frame["momentum_score"]
        + 0.15 * frame["breakout_score"]
        + 0.10 * frame["event_component"]
        + 0.10 * frame["flow_score"]
        - frame["heat_penalty"]
    ).clip(0, 100)

    base_gate = (
        (pd.to_numeric(frame.get("listed_days"), errors="coerce") >= params.minimum_listing_days)
        & (pd.to_numeric(frame.get("valid_days_20"), errors="coerce") >= params.minimum_valid_days_20)
        & (pd.to_numeric(frame.get("adv20"), errors="coerce") >= params.minimum_adv20)
        & ~frame["suspended"].fillna(False).astype(bool)
        & ~frame["is_st"].fillna(False).astype(bool)
        & ~frame["is_quit"].fillna(False).astype(bool)
    )
    fundamental_gate = (
        (
            (frame["revenue_yoy"] > params.minimum_revenue_growth)
            & (frame["profit_yoy"] > params.minimum_profit_growth)
        )
        | (frame["forecast_revision"] > params.minimum_forecast_revision)
    ) & (
        (frame["gross_margin_change"] >= 0)
        | (frame["forecast_revision"] > params.minimum_forecast_revision)
    )
    industry_gate = (
        (frame["industry_momentum_pct"] >= params.industry_rank_threshold)
        & (frame["industry_breadth"] > params.industry_breadth_threshold)
        & (frame["industry_amount_trend"] > 0)
    )
    technical_gate = (
        (frame["rs60_pct"] >= params.rs60_rank_threshold)
        & (frame["breakout_distance"] > 0)
        & (frame["volume_ratio"] > params.minimum_volume_ratio)
        & (frame["amount_ratio"] > params.minimum_amount_ratio)
        & (pd.to_numeric(frame.get("close"), errors="coerce") > pd.to_numeric(frame.get("ma20"), errors="coerce"))
        & (pd.to_numeric(frame.get("ma20"), errors="coerce") > pd.to_numeric(frame.get("ma60"), errors="coerce"))
    )
    extreme_heat = (
        (frame["heat_return_pct"] >= 0.99)
        & (frame["turnover_pct"] >= 0.95)
        & (frame["price_to_ma60"] > 1.80)
        & ~(frame["forecast_revision"] > params.minimum_forecast_revision)
    )
    frame["eligible"] = base_gate & fundamental_gate & industry_gate & technical_gate & ~extreme_heat

    ranked = frame.loc[frame["eligible"]].sort_values(
        ["rule_score", "code"], ascending=[False, True]
    )
    selected: list[dict[str, Any]] = []
    industry_counts: dict[str, int] = {}
    for _, row in ranked.iterrows():
        industry = str(row.get("industry") or "未分类")
        if industry_counts.get(industry, 0) >= params.maximum_industry_candidates:
            continue
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        factors = {
            "industry": float(row["industry_score"]),
            "fundamental": float(row["fundamental_score"]),
            "momentum": float(row["momentum_score"]),
            "breakout": float(row["breakout_score"]),
            "event": float(row["event_component"]),
            "flow": float(row["flow_score"]),
            "heat_penalty": float(row["heat_penalty"]),
            "rs60_percentile": float(row["rs60_pct"]),
            "volume_ratio": float(row["volume_ratio"]),
            "amount_ratio": float(row["amount_ratio"]),
        }
        selected.append(
            {
                "code": str(row["code"]),
                "name": str(row.get("name") or ""),
                "industry": industry,
                "asof": str(row.get("asof") or ""),
                "score": float(row["rule_score"]),
                "factors": factors,
                "gates": {
                    "universe": True,
                    "fundamental": True,
                    "industry": True,
                    "technical": True,
                    "extreme_heat": False,
                },
                "evidence_refs": _as_string_list(row.get("evidence_refs")),
            }
        )
        if len(selected) >= params.maximum_candidates:
            break
    for rank, candidate in enumerate(selected, 1):
        candidate["rank"] = rank
    return selected


def select_ml_candidates(
    rows: Iterable[Mapping[str, Any]],
    parameters: EarlyWinnerParameters | None = None,
) -> list[dict[str, Any]]:
    params = parameters or EarlyWinnerParameters()
    prepared = [dict(row) for row in rows if row.get("probability") is not None]
    prepared.sort(key=lambda item: (-float(item["probability"]), str(item.get("code", ""))))
    selected: list[dict[str, Any]] = []
    industry_counts: dict[str, int] = {}
    for row in prepared:
        if row.get("eligible") is False:
            continue
        industry = str(row.get("industry") or "未分类")
        if industry_counts.get(industry, 0) >= params.maximum_industry_candidates:
            continue
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        selected.append(
            {
                "code": str(row["code"]),
                "name": str(row.get("name") or ""),
                "industry": industry,
                "asof": str(row.get("asof") or ""),
                "score": float(row["probability"]) * 100,
                "probability": float(row["probability"]),
                "factors": dict(row.get("factors") or {}),
                "gates": dict(row.get("gates") or {}),
                "evidence_refs": _as_string_list(row.get("evidence_refs")),
            }
        )
        if len(selected) >= params.maximum_candidates:
            break
    for rank, candidate in enumerate(selected, 1):
        candidate["rank"] = rank
    return selected


class EarlyWinnerRuleStrategy:
    metadata = StrategyMetadata(
        strategy_id=RULE_STRATEGY_ID,
        version="1.0.0",
        name="早期强势股识别 · 规则",
        description="规则多因子 Top20；仅研究候选，不产生交易信号。",
        frequency="1w",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        strategy_family=PROJECT_ID,
        scan_enabled=False,
        backtest_enabled=False,
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement("bars", "1d", "front", 180, True),
            DataRequirement("bars", "1d", "none", 180, True),
            DataRequirement("industry_history", "event", "none", 0, True),
            DataRequirement("financials", "quarterly", "none", 0, True),
            DataRequirement("announcements", "event", "none", 0, True),
            DataRequirement("institutional_flows", "1d", "none", 0, True),
        ),
    )

    def __init__(self, parameters: EarlyWinnerParameters | None = None):
        self.parameters = parameters or EarlyWinnerParameters()

    def scan(self, **context: Any) -> StrategyScanResult:
        rows = context.get("feature_rows") or ()
        candidates = score_rule_candidates(rows, self.parameters)
        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=tuple(candidates),
            state={
                "asof": candidates[0]["asof"] if candidates else context.get("asof"),
                "status": "READY" if rows else "BLOCKED_DATA",
                "candidate_count": len(candidates),
                "trade_signals_enabled": False,
            },
        )


class EarlyWinnerMLStrategy:
    metadata = StrategyMetadata(
        strategy_id=ML_STRATEGY_ID,
        version="1.0.0",
        name="早期强势股识别 · ML",
        description="固定 HistGradientBoosting 概率 Top20；与规则榜独立，不融合。",
        frequency="1w",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        strategy_family=PROJECT_ID,
        scan_enabled=False,
        backtest_enabled=False,
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=EarlyWinnerRuleStrategy.metadata.data_requirements,
    )

    def __init__(self, parameters: EarlyWinnerParameters | None = None):
        self.parameters = parameters or EarlyWinnerParameters()

    def scan(self, **context: Any) -> StrategyScanResult:
        rows = context.get("feature_rows") or ()
        candidates = select_ml_candidates(rows, self.parameters)
        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=tuple(candidates),
            state={
                "asof": candidates[0]["asof"] if candidates else context.get("asof"),
                "status": "READY" if rows else "BLOCKED_DATA",
                "candidate_count": len(candidates),
                "trade_signals_enabled": False,
            },
        )


__all__ = [
    "FEATURE_COLUMNS",
    "HARD_NEGATIVE_EVENT_TYPES",
    "ML_STRATEGY_ID",
    "PROJECT_ID",
    "RULE_STRATEGY_ID",
    "EarlyWinnerMLStrategy",
    "EarlyWinnerParameters",
    "EarlyWinnerRuleStrategy",
    "build_technical_feature_rows",
    "classify_announcement",
    "effective_publication_time",
    "point_in_time_latest",
    "score_rule_candidates",
    "select_ml_candidates",
    "technical_feature_row",
]
