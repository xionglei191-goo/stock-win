from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research_platform.models import (
    DataRequirement,
    PlatformSignal,
    RuntimeAdapter,
    SignalStatus,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)
from research_platform.us_market_calendar import next_nyse_session


NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class USMomentumParameters:
    ma_fast: int = 50
    ma_slow: int = 200
    lookback_short: int = 60
    lookback_mid: int = 120
    lookback_long: int = 252
    skip_recent: int = 20

    # Research defaults from the current walk-forward experiment. They remain
    # explicit parameters so production evaluation does not silently change them.
    max_depth_from_high: float = 0.10
    vol_ratio_cap: float = 2.0
    rs_top_pct: float = 0.25
    exit_top_pct: float = 0.40
    stop_ratio: float = 0.08

    use_score_weight: bool = True
    target_weight: float = 0.10
    max_total_weight: float = 1.00

    max_candidates: int = 30
    max_entry_signals: int = 10
    emit_live_entry_signals: bool = False
    min_price: float = 5.0
    min_avg_volume: float = 500_000.0
    min_dollar_volume: float = 5_000_000.0
    max_ret_long: float = 10.0
    min_vol_ratio: float = 0.05

    use_market_regime: bool = True
    market_code: str = "SPY.US"
    excluded_codes: tuple[str, ...] = ("SPY.US", "QQQ.US")

    def __post_init__(self) -> None:
        if min(self.ma_fast, self.ma_slow, self.lookback_short, self.lookback_mid, self.lookback_long) <= 0:
            raise ValueError("Momentum lookbacks must be positive")
        if self.skip_recent < 0:
            raise ValueError("skip_recent cannot be negative")
        if not 0 < self.rs_top_pct <= self.exit_top_pct <= 1:
            raise ValueError("RS entry/exit percentiles must satisfy 0 < entry <= exit <= 1")
        if not 0 <= self.max_depth_from_high < 1:
            raise ValueError("max_depth_from_high must be in [0, 1)")
        if not 0 < self.stop_ratio < 1:
            raise ValueError("stop_ratio must be in (0, 1)")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight must be in (0, 1]")
        if not 0 <= self.max_total_weight <= 1:
            raise ValueError("max_total_weight must be in [0, 1]")
        if min(self.max_candidates, self.max_entry_signals) <= 0:
            raise ValueError("Candidate and entry limits must be positive")
        if not _is_us_code(self.market_code):
            raise ValueError("market_code must be a .US instrument")
        if any(not _is_us_code(code) for code in self.excluded_codes):
            raise ValueError("excluded_codes must contain only .US instruments")


def _is_us_code(value: Any) -> bool:
    return str(value).strip().upper().endswith(".US")


def _normalise_code(value: Any) -> str:
    return str(value).strip().upper()


def _market_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(NY_TZ).tz_localize(None)
    return timestamp


def _visible_bars(
    bars: pd.DataFrame | None,
    asof: pd.Timestamp | None,
) -> pd.DataFrame:
    """Return a stable, point-in-time daily frame.

    The last duplicate is the value that was available after an upstream refresh.
    The caller still has to check freshness because an old final row is not evidence
    that the instrument traded at the requested boundary.
    """

    if bars is None or bars.empty:
        return pd.DataFrame()
    try:
        index = pd.DatetimeIndex(pd.to_datetime(bars.index))
    except (TypeError, ValueError):
        return pd.DataFrame()
    if index.tz is not None:
        index = index.tz_convert(NY_TZ).tz_localize(None)
    output = bars.copy()
    output.index = index
    output = output.sort_index(kind="stable")
    output = output[~output.index.duplicated(keep="last")]
    if asof is not None:
        boundary = _market_timestamp(asof).normalize()
        output = output[output.index.normalize() <= boundary]
    return output


def _is_fresh(bars: pd.DataFrame, asof: pd.Timestamp) -> bool:
    if bars.empty:
        return False
    return pd.Timestamp(bars.index[-1]).normalize() == _market_timestamp(asof).normalize()


def _score_bars(
    bars: pd.DataFrame,
    params: USMomentumParameters,
    asof: pd.Timestamp | None = None,
) -> dict[str, Any] | None:
    result, _ = _score_bars_with_reason(bars, params, asof)
    return result


def _score_bars_with_reason(
    bars: pd.DataFrame,
    params: USMomentumParameters,
    asof: pd.Timestamp | None = None,
) -> tuple[dict[str, Any] | None, str]:
    visible = _visible_bars(bars, asof)
    if asof is not None and not _is_fresh(visible, asof):
        return None, "STALE_DATA"
    required_columns = {"Close", "Volume"}
    if not required_columns.issubset(visible.columns):
        return None, "MISSING_COLUMNS"

    min_bars = max(params.ma_slow, params.lookback_long + params.skip_recent) + 10
    if len(visible) < min_bars:
        return None, "INSUFFICIENT_HISTORY"

    close = pd.to_numeric(visible["Close"], errors="coerce")
    volume = pd.to_numeric(visible["Volume"], errors="coerce")
    if close.isna().any() or volume.isna().any():
        return None, "NON_FINITE_DATA"
    close_values = close.to_numpy(dtype=float)
    volume_values = volume.to_numpy(dtype=float)
    if not np.isfinite(close_values).all() or not np.isfinite(volume_values).all():
        return None, "NON_FINITE_DATA"
    if (close_values <= 0).any() or (volume_values < 0).any():
        return None, "INVALID_MARKET_DATA"

    ma_fast = close.rolling(params.ma_fast).mean()
    ma_slow = close.rolling(params.ma_slow).mean()
    last_close = float(close.iloc[-1])
    last_ma_fast = float(ma_fast.iloc[-1])
    last_ma_slow = float(ma_slow.iloc[-1])

    if not np.isfinite(last_ma_fast) or not np.isfinite(last_ma_slow):
        return None, "INSUFFICIENT_HISTORY"
    if last_close < last_ma_slow:
        return None, "TREND_FAILED"
    if last_close < params.min_price:
        return None, "PRICE_FAILED"

    skip = params.skip_recent

    def _ret(n: int) -> float:
        idx_end = -(skip + 1) if skip > 0 else -1
        idx_start = -(n + skip + 1)
        p_end = float(close.iloc[idx_end])
        p_start = float(close.iloc[idx_start])
        return p_end / p_start - 1.0

    ret_short = _ret(params.lookback_short)
    ret_mid = _ret(params.lookback_mid)
    ret_long = _ret(params.lookback_long)
    if not np.isfinite([ret_short, ret_mid, ret_long]).all():
        return None, "NON_FINITE_RETURN"
    if abs(ret_long) > params.max_ret_long:
        return None, "RETURN_OUTLIER"

    recent_high = float(close.iloc[-params.ma_fast :].max())
    depth = (recent_high - last_close) / recent_high if recent_high > 0 else 1.0
    if depth > params.max_depth_from_high:
        return None, "DEPTH_FAILED"

    vol5 = float(volume.iloc[-5:].mean())
    vol_base_slice = volume.iloc[-65:-5]
    if len(vol_base_slice) < 20:
        return None, "INSUFFICIENT_VOLUME_HISTORY"
    vol60 = float(vol_base_slice.mean())
    if not np.isfinite(vol5) or not np.isfinite(vol60) or vol60 <= 0:
        return None, "INVALID_VOLUME"
    if vol60 < params.min_avg_volume:
        return None, "LIQUIDITY_FAILED"
    dollar_vol = vol60 * last_close
    if dollar_vol < params.min_dollar_volume:
        return None, "LIQUIDITY_FAILED"
    vol_ratio = vol5 / vol60
    if vol_ratio > params.vol_ratio_cap or vol_ratio < params.min_vol_ratio:
        return None, "VOLUME_RATIO_FAILED"

    ret_long_adj = max(ret_long, 0.0)
    acceleration_bonus = max(ret_short - 0.15, 0.0) * 0.5
    rs_score = (
        ret_long_adj * 0.50
        + ret_mid * 0.30
        + ret_short * 0.20
        + acceleration_bonus
    )

    return {
        "rs_score": round(float(rs_score), 6),
        "ret_short": round(ret_short, 4),
        "ret_mid": round(ret_mid, 4),
        "ret_long": round(ret_long, 4),
        "ret_long_adj": round(ret_long_adj, 4),
        "depth_from_high": round(depth, 4),
        "vol_ratio": round(vol_ratio, 3),
        "close": round(last_close, 4),
        "ma_fast": round(last_ma_fast, 4),
        "ma_slow": round(last_ma_slow, 4),
    }, "ELIGIBLE"


def _compute_score_weights(
    candidates: list[tuple[str, dict[str, Any]]],
    max_positions: int,
    *,
    use_score_weight: bool = True,
    target_weight: float = 0.10,
    max_total_weight: float = 1.00,
) -> list[tuple[str, dict[str, Any], float]]:
    """Assign bounded absolute portfolio targets and leave unused cash unallocated."""

    n = min(len(candidates), max(0, max_positions))
    if n == 0 or target_weight <= 0 or max_total_weight <= 0:
        return []
    top = candidates[:n]
    if use_score_weight:
        fallback = [(n - index) / n for index in range(n)]
        factors = [
            min(1.0, max(0.0, float(score.get("rs_percentile", fallback[index]))))
            for index, (_, score) in enumerate(top)
        ]
    else:
        factors = [1.0] * n

    remaining = max_total_weight
    output: list[tuple[str, dict[str, Any], float]] = []
    for (code, score), factor in zip(top, factors):
        weight = min(target_weight * factor, target_weight, remaining)
        if weight <= 0:
            break
        rounded = round(weight, 6)
        output.append((code, score, rounded))
        remaining -= weight
    return output


class USMomentumStrategy:
    metadata = StrategyMetadata(
        strategy_id="us_momentum_v1",
        version="2.1.0",
        name="美股趋势动量 v2.1",
        description=(
            "仅限美股的月末趋势动量策略：RS前25%入场、跌出前40%退出，"
            "使用SPY 50/200日市场状态、单股10%上限和总仓位上限。"
        ),
        frequency="1mo",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        # The legacy current-constituent TDX scan is intentionally sealed.
        # Formal decisions are made only from an immutable DATA_READY IVV PIT
        # release by the dedicated strict/paper coordinators.
        scan_enabled=False,
        backtest_enabled=True,
        asset_classes=("US_STOCK",),
        runtime_adapter=RuntimeAdapter.US_STRICT,
        data_requirements=(
            DataRequirement(
                "bars",
                "1d",
                "front",
                1300,
                True,
                ("Open", "High", "Low", "Close", "Volume"),
            ),
        ),
    )

    def __init__(self, parameters: USMomentumParameters | None = None):
        self.parameters = parameters or USMomentumParameters()

    def scan(
        self,
        *,
        run_id: str,
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame] | None = None,
        names: dict[str, str] | None = None,
        positions: list[dict[str, Any]] | None = None,
        runtime_state: dict[str, Any] | None = None,
        backtest_mode: bool = False,
        asof: pd.Timestamp | None = None,
        is_rebalance_day: bool | None = None,
        tradable_codes: set[str] | tuple[str, ...] | list[str] | None = None,
        **_: Any,
    ) -> StrategyScanResult:
        names = names or {}
        positions = positions or []
        raw_bars = raw_bars or {}
        params = self.parameters
        state = dict(runtime_state or {})
        portfolio_state = dict(state.get("portfolio") or {})

        normalised_front = {
            _normalise_code(code): frame
            for code, frame in front_bars.items()
            if _is_us_code(code)
        }
        market_code = _normalise_code(params.market_code)
        excluded_codes = {
            _normalise_code(code) for code in (*params.excluded_codes, market_code)
        }
        tradable_set = (
            {_normalise_code(code) for code in tradable_codes}
            if tradable_codes is not None
            else set(normalised_front) - excluded_codes
        )
        market_source = normalised_front.get(market_code)

        if asof is not None:
            boundary = _market_timestamp(asof).normalize()
        elif market_source is not None and not market_source.empty:
            visible_market = _visible_bars(market_source, None)
            boundary = (
                pd.Timestamp(visible_market.index[-1]).normalize()
                if not visible_market.empty
                else None
            )
        else:
            latest = [
                pd.Timestamp(frame.index[-1]).normalize()
                for frame in (_visible_bars(item, None) for item in normalised_front.values())
                if not frame.empty
            ]
            boundary = max(latest) if latest else None

        if boundary is None:
            return self._empty_result(state, "US_DATA_UNAVAILABLE", None)

        asof_day = boundary.date().isoformat()
        visible_market = _visible_bars(market_source, boundary)
        if params.use_market_regime:
            if market_source is None or visible_market.empty:
                return self._empty_result(state, "MARKET_DATA_UNAVAILABLE", boundary)
            if not _is_fresh(visible_market, boundary):
                return self._empty_result(state, "MARKET_DATA_STALE", boundary)
            market_close = pd.to_numeric(visible_market.get("Close"), errors="coerce")
            if len(market_close) < params.ma_slow + 10 or market_close.isna().any():
                return self._empty_result(state, "MARKET_WARMUP", boundary)

        rebalance_due = (
            bool(is_rebalance_day)
            if is_rebalance_day is not None
            else False
        )
        decision_period = str(boundary.to_period("M"))
        if not rebalance_due:
            return self._empty_result(
                state,
                "NOT_REBALANCE_DAY",
                boundary,
                extra={"decision_period": decision_period, "rebalance_due": False},
            )
        if str(portfolio_state.get("last_rebalance_period", "")) == decision_period:
            return self._empty_result(
                state,
                "ALREADY_REBALANCED",
                boundary,
                extra={"decision_period": decision_period, "rebalance_due": False},
            )

        market_bull = True
        if params.use_market_regime:
            spy_ma_fast = float(market_close.rolling(params.ma_fast).mean().iloc[-1])
            spy_ma_slow = float(market_close.rolling(params.ma_slow).mean().iloc[-1])
            if not np.isfinite(spy_ma_fast) or not np.isfinite(spy_ma_slow):
                return self._empty_result(state, "MARKET_WARMUP", boundary)
            market_bull = spy_ma_fast > spy_ma_slow

        scored: list[tuple[str, dict[str, Any]]] = []
        rejection_reasons: dict[str, str] = {}
        stale_rejected: list[str] = []
        for code, bars in normalised_front.items():
            if code in excluded_codes or code not in tradable_set:
                continue
            score, reason = _score_bars_with_reason(bars, params, boundary)
            if score is None:
                rejection_reasons[code] = reason
                if reason == "STALE_DATA":
                    stale_rejected.append(code)
                continue
            scored.append((code, score))

        scored.sort(key=lambda item: (-float(item[1]["rs_score"]), item[0]))
        all_rs = [float(score["rs_score"]) for _, score in scored]
        for _, score in scored:
            raw_score = float(score["rs_score"])
            score["rs_percentile"] = round(
                sum(value <= raw_score for value in all_rs) / len(all_rs), 6
            )

        entry_thresh = (
            float(np.percentile(all_rs, (1 - params.rs_top_pct) * 100))
            if all_rs
            else None
        )
        exit_thresh = (
            float(np.percentile(all_rs, (1 - params.exit_top_pct) * 100))
            if all_rs
            else None
        )
        score_by_code = {code: score for code, score in scored}
        top_entry_all = [
            (code, score)
            for code, score in scored
            if entry_thresh is not None and float(score["rs_score"]) >= entry_thresh
        ]
        candidates = tuple(
            {"code": code, "name": names.get(code, code), **score}
            for code, score in top_entry_all[: params.max_candidates]
        )

        position_by_code = {
            _normalise_code(position.get("code", "")): position
            for position in positions
            if _is_us_code(position.get("code", ""))
        }
        signals: list[PlatformSignal] = []
        generated_at, available_at, valid_until = _signal_times(boundary)
        exited_codes: set[str] = set()

        for code, position in sorted(position_by_code.items()):
            reason = ""
            evidence: dict[str, Any] = {"asof": asof_day}
            if not market_bull:
                reason = "US_MARKET_REGIME_EXIT"
            elif code not in score_by_code:
                rejection = rejection_reasons.get(code, "MISSING_UNIVERSE_DATA")
                # Missing/stale data are operational faults, not investment
                # evidence. Keep the position and let its raw-price stop remain
                # active instead of selling on an incomplete cross-section.
                deterministic_exits = {
                    "TREND_FAILED",
                    "PRICE_FAILED",
                    "DEPTH_FAILED",
                    "LIQUIDITY_FAILED",
                    "VOLUME_RATIO_FAILED",
                }
                if rejection in deterministic_exits:
                    reason = (
                        "US_TREND_EXIT"
                        if rejection == "TREND_FAILED"
                        else "US_ELIGIBILITY_EXIT"
                    )
                    evidence["eligibility_reason"] = rejection
            elif exit_thresh is not None and float(score_by_code[code]["rs_score"]) < exit_thresh:
                reason = "US_RS_BUFFER_EXIT"
                evidence.update(score_by_code[code])
                evidence["exit_thresh"] = round(exit_thresh, 6)
            if not reason:
                continue
            exited_codes.add(code)
            signals.append(
                PlatformSignal(
                    run_id=run_id,
                    strategy_id=self.metadata.strategy_id,
                    strategy_version=self.metadata.version,
                    generated_at=generated_at,
                    available_at=available_at,
                    code=code,
                    side="SELL",
                    strength=1.0,
                    target_weight=0.0,
                    horizon="monthly",
                    valid_until=valid_until,
                    stop_price=None,
                    status=SignalStatus.PROPOSED,
                    reason_codes=(reason,),
                    evidence=evidence,
                )
            )

        retained_positions = {
            code: position
            for code, position in position_by_code.items()
            if code not in exited_codes
        }
        retained_weight = min(
            params.max_total_weight,
            sum(_position_weight(position, params.target_weight) for position in retained_positions.values()),
        )
        available_weight = max(0.0, params.max_total_weight - retained_weight)
        open_slots = max(0, params.max_entry_signals - len(retained_positions))
        new_entries = [
            (code, score)
            for code, score in top_entry_all
            if code not in position_by_code
        ]
        entry_enabled = market_bull and (backtest_mode or params.emit_live_entry_signals)
        proposed_weight = 0.0
        if entry_enabled and open_slots > 0 and available_weight > 0:
            weighted = _compute_score_weights(
                new_entries,
                open_slots,
                use_score_weight=params.use_score_weight,
                target_weight=params.target_weight,
                max_total_weight=available_weight,
            )
            for code, score, weight in weighted:
                raw_price = _latest_raw_price(raw_bars.get(code), boundary)
                stop_reference = raw_price if raw_price is not None else float(score["close"])
                stop = round(stop_reference * (1 - params.stop_ratio), 4)
                strength = min(1.0, max(0.0, float(score["rs_percentile"])))
                evidence = {
                    **score,
                    "stop_ratio": params.stop_ratio,
                    "stop_reference_price": round(stop_reference, 4),
                    "stop_reference_source": "raw_close" if raw_price is not None else "front_close",
                    "entry_thresh": round(entry_thresh, 6) if entry_thresh is not None else None,
                }
                signals.append(
                    PlatformSignal(
                        run_id=run_id,
                        strategy_id=self.metadata.strategy_id,
                        strategy_version=self.metadata.version,
                        generated_at=generated_at,
                        available_at=available_at,
                        code=code,
                        side="BUY",
                        strength=strength,
                        target_weight=weight,
                        horizon="monthly",
                        valid_until=valid_until,
                        stop_price=stop,
                        status=SignalStatus.PROPOSED,
                        reason_codes=("US_TREND_BULL", "US_RS_TOP_25_ENTRY"),
                        evidence=evidence,
                    )
                )
                proposed_weight += weight

        portfolio_state["last_rebalance_period"] = decision_period
        state["portfolio"] = portfolio_state
        return StrategyScanResult(
            strategy=self.metadata,
            signals=tuple(signals),
            candidates=candidates,
            state={
                "status": "REBALANCE_READY",
                "asof": asof_day,
                "decision_period": decision_period,
                "rebalance_due": True,
                "market_bull": market_bull,
                "entry_thresh": round(entry_thresh, 6) if entry_thresh is not None else None,
                "exit_thresh": round(exit_thresh, 6) if exit_thresh is not None else None,
                "n_scored": len(scored),
                "stale_rejected": tuple(sorted(stale_rejected)),
                "rejection_reasons": rejection_reasons,
                "retained_weight_reserve": round(retained_weight, 6),
                "proposed_new_weight": round(proposed_weight, 6),
                "runtime_state": state,
            },
        )

    def _empty_result(
        self,
        runtime_state: dict[str, Any],
        status: str,
        boundary: pd.Timestamp | None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> StrategyScanResult:
        state: dict[str, Any] = {
            "status": status,
            "market_bull": False,
            "stale_rejected": (),
            "runtime_state": runtime_state,
        }
        if boundary is not None:
            state["asof"] = boundary.date().isoformat()
        state.update(extra or {})
        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=(),
            state=state,
        )


def _position_weight(position: dict[str, Any], fallback: float) -> float:
    for key in ("weight", "current_weight", "target_weight"):
        try:
            value = float(position[key])
        except (KeyError, TypeError, ValueError):
            continue
        if np.isfinite(value) and value >= 0:
            return value
    return fallback


def _latest_raw_price(
    bars: pd.DataFrame | None,
    asof: pd.Timestamp,
) -> float | None:
    visible = _visible_bars(bars, asof)
    if not _is_fresh(visible, asof) or "Close" not in visible.columns:
        return None
    close = pd.to_numeric(visible["Close"], errors="coerce").dropna()
    if close.empty:
        return None
    price = float(close.iloc[-1])
    return price if np.isfinite(price) and price > 0 else None


def _signal_times(value: Any) -> tuple[datetime, datetime, datetime]:
    timestamp = _market_timestamp(value).normalize() + pd.Timedelta(hours=16)
    timestamp = timestamp.tz_localize(NY_TZ)
    next_session = next_nyse_session(timestamp.tz_localize(None)).tz_localize(NY_TZ)
    available = next_session.replace(hour=9, minute=30)
    valid_until = next_session.replace(hour=16, minute=0)
    return (
        timestamp.to_pydatetime(),
        available.to_pydatetime(),
        valid_until.to_pydatetime(),
    )


__all__ = [
    "USMomentumParameters",
    "USMomentumStrategy",
]
