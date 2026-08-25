from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from research_platform.models import (
    DataRequirement,
    RuntimeAdapter,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


PROJECT_ID = "us_etf_allocation"
STRATEGY_ID = "qqq_vol_dca_v1"


@dataclass(frozen=True)
class QQQVolDCAParameters:
    qqq_code: str = "QQQ.US"
    primary_volatility_code: str = "VXN.US"
    fallback_volatility_code: str = "VIX.US"
    volatility_lookback_days: int = 1260
    minimum_volatility_observations: int = 252
    drawdown_lookback_days: int = 252
    trend_days: int = 200
    base_multiple: float = 0.80
    reserve_contribution_multiple: float = 0.20
    reserve_cap_multiple: float = 4.00
    panic_tranche_multiple: float = 0.50
    tier_percentiles: tuple[float, ...] = (0.80, 0.90, 0.97)
    tier_drawdowns: tuple[float, ...] = (0.08, 0.15, 0.25)
    tier_extra_multiples: tuple[float, ...] = (0.50, 1.00, 2.00)
    reset_percentile: float = 0.60
    reset_weeks: int = 4
    weekly_decision_weekday: int = 4

    def __post_init__(self) -> None:
        tier_lengths = {
            len(self.tier_percentiles),
            len(self.tier_drawdowns),
            len(self.tier_extra_multiples),
        }
        if tier_lengths != {3}:
            raise ValueError("QQQ volatility DCA must define exactly three aligned tiers")
        if abs(self.base_multiple + self.reserve_contribution_multiple - 1.0) > 1e-9:
            raise ValueError("Base and reserve contribution multiples must sum to 1")
        if self.panic_tranche_multiple <= 0 or self.reserve_cap_multiple < 0:
            raise ValueError("Reserve and panic tranche parameters must be positive")
        if self.minimum_volatility_observations < 2:
            raise ValueError("At least two volatility observations are required")


class QQQVolDCAStrategy:
    """Funding-plan strategy; it deliberately emits no portfolio order signals."""

    metadata = StrategyMetadata(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        name="QQQ 波动率增强定投 V1",
        description=(
            "每月80%基础定投、20%进入机会资金池；使用VXN（缺失时退回VIX）"
            "五年滚动分位与QQQ回撤分层安排恐慌加仓。"
        ),
        frequency="1w",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        strategy_family=PROJECT_ID,
        scan_enabled=False,
        backtest_enabled=False,
        asset_classes=("US_ETF",),
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement(
                "bars",
                "1d",
                "front",
                1300,
                True,
                ("Close",),
            ),
        ),
    )

    def __init__(self, parameters: QQQVolDCAParameters | None = None):
        self.parameters = parameters or QQQVolDCAParameters()

    @property
    def required_codes(self) -> tuple[str, ...]:
        params = self.parameters
        return (
            params.qqq_code,
            params.primary_volatility_code,
            params.fallback_volatility_code,
        )

    def scan(
        self,
        *,
        run_id: str,
        front_bars: dict[str, pd.DataFrame],
        runtime_state: dict[str, Any] | None = None,
        asof: pd.Timestamp | None = None,
        **_: Any,
    ) -> StrategyScanResult:
        del run_id
        params = self.parameters
        qqq = _visible_close(front_bars.get(params.qqq_code), asof)
        state = _normalise_state(runtime_state)
        if qqq.empty:
            return _empty_result(self.metadata, state, "QQQ_DATA_UNAVAILABLE")

        latest_date = pd.Timestamp(qqq.index[-1]).tz_localize(None).normalize()
        month_key = latest_date.strftime("%Y-%m")
        week_key = f"{latest_date.isocalendar().year}-W{latest_date.isocalendar().week:02d}"

        base_multiple = 0.0
        overflow_multiple = 0.0
        reserve = float(state["reserve_multiple"])
        if state["last_base_month"] != month_key:
            base_multiple = params.base_multiple
            deposited = reserve + params.reserve_contribution_multiple
            reserve = min(params.reserve_cap_multiple, deposited)
            overflow_multiple = max(0.0, deposited - params.reserve_cap_multiple)
            state["last_base_month"] = month_key

        metrics = _market_metrics(front_bars, qqq, asof, params)
        weekly_due = (
            latest_date.weekday() == params.weekly_decision_weekday
            and state["last_week"] != week_key
        )
        newly_triggered: list[int] = []
        panic_multiple = 0.0
        reset_completed = False

        if weekly_due:
            state["last_week"] = week_key
            if metrics["ready"]:
                if (
                    float(metrics["volatility_percentile"]) < params.reset_percentile
                    and bool(metrics["above_trend"])
                ):
                    state["reset_streak"] = int(state["reset_streak"]) + 1
                else:
                    state["reset_streak"] = 0

                if int(state["reset_streak"]) >= params.reset_weeks:
                    state["triggered_tiers"] = []
                    state["pending_panic_tranches"] = 0
                    state["reset_streak"] = 0
                    state["cycle"] = int(state["cycle"]) + 1
                    reset_completed = True

                triggered = {int(item) for item in state["triggered_tiers"]}
                for index, (percentile, drawdown, extra) in enumerate(
                    zip(
                        params.tier_percentiles,
                        params.tier_drawdowns,
                        params.tier_extra_multiples,
                    ),
                    start=1,
                ):
                    if (
                        index not in triggered
                        and float(metrics["volatility_percentile"]) >= percentile
                        and float(metrics["drawdown"]) >= drawdown
                    ):
                        triggered.add(index)
                        newly_triggered.append(index)
                        tranches = max(1, round(extra / params.panic_tranche_multiple))
                        state["pending_panic_tranches"] = (
                            int(state["pending_panic_tranches"]) + tranches
                        )
                state["triggered_tiers"] = sorted(triggered)

            if int(state["pending_panic_tranches"]) > 0 and reserve > 0:
                panic_multiple = min(params.panic_tranche_multiple, reserve)
                reserve -= panic_multiple
                state["pending_panic_tranches"] = max(
                    0, int(state["pending_panic_tranches"]) - 1
                )

        state["reserve_multiple"] = round(reserve, 6)
        contribution_multiple = round(
            base_multiple + overflow_multiple + panic_multiple,
            6,
        )
        reasons: list[str] = []
        if base_multiple:
            reasons.append("MONTHLY_BASE_DCA")
        if overflow_multiple:
            reasons.append("RESERVE_CAP_OVERFLOW")
        if panic_multiple:
            reasons.append("PANIC_TRANCHE")
        if newly_triggered:
            reasons.extend(f"PANIC_TIER_{tier}" for tier in newly_triggered)
        if reset_completed:
            reasons.append("PANIC_CYCLE_RESET")

        candidates: tuple[dict[str, Any], ...] = ()
        if contribution_multiple > 0:
            candidates = (
                {
                    "code": params.qqq_code,
                    "action": "CONTRIBUTE",
                    "contribution_multiple": contribution_multiple,
                    "base_multiple": round(base_multiple, 6),
                    "reserve_overflow_multiple": round(overflow_multiple, 6),
                    "panic_multiple": round(panic_multiple, 6),
                    "reserve_multiple_after": state["reserve_multiple"],
                    "reason_codes": reasons,
                    **metrics,
                },
            )

        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=candidates,
            state={
                "status": "PLAN_READY" if metrics["ready"] else "VOLATILITY_WARMUP",
                "asof": latest_date.date().isoformat(),
                "weekly_due": weekly_due,
                "contribution_multiple": contribution_multiple,
                "target_asset": params.qqq_code,
                "trade_signals_enabled": False,
                "execution_note": (
                    "Contribution multiples are funding instructions, not total portfolio weights."
                ),
                "market": metrics,
                "runtime_state": state,
            },
        )


def _normalise_state(runtime_state: dict[str, Any] | None) -> dict[str, Any]:
    source = runtime_state or {}
    return {
        "last_base_month": str(source.get("last_base_month", "")),
        "last_week": str(source.get("last_week", "")),
        "reserve_multiple": max(0.0, float(source.get("reserve_multiple", 0.0))),
        "triggered_tiers": sorted(
            {
                int(item)
                for item in source.get("triggered_tiers", [])
                if str(item).isdigit() and 1 <= int(item) <= 3
            }
        ),
        "pending_panic_tranches": max(
            0, int(source.get("pending_panic_tranches", 0))
        ),
        "reset_streak": max(0, int(source.get("reset_streak", 0))),
        "cycle": max(1, int(source.get("cycle", 1))),
    }


def _market_metrics(
    front_bars: dict[str, pd.DataFrame],
    qqq: pd.Series,
    asof: pd.Timestamp | None,
    params: QQQVolDCAParameters,
) -> dict[str, Any]:
    volatility_code = ""
    volatility = pd.Series(dtype=float)
    for code in (params.primary_volatility_code, params.fallback_volatility_code):
        candidate = _visible_close(front_bars.get(code), asof)
        if len(candidate) >= params.minimum_volatility_observations:
            volatility_code = code
            volatility = candidate
            break

    qqq_window = qqq.iloc[-params.drawdown_lookback_days :]
    latest = float(qqq.iloc[-1])
    recent_high = float(qqq_window.max())
    drawdown = 1.0 - latest / recent_high if recent_high > 0 else 0.0
    trend = qqq.iloc[-params.trend_days :]
    sma = float(trend.mean()) if len(trend) >= params.trend_days else None
    above_trend = sma is not None and latest > sma

    ready = (
        bool(volatility_code)
        and len(qqq) >= max(params.drawdown_lookback_days, params.trend_days)
    )
    percentile: float | None = None
    volatility_value: float | None = None
    if volatility_code:
        window = volatility.iloc[-params.volatility_lookback_days :]
        volatility_value = float(window.iloc[-1])
        percentile = float((window <= volatility_value).mean())

    return {
        "ready": ready,
        "volatility_code": volatility_code or None,
        "volatility_value": (
            round(volatility_value, 6) if volatility_value is not None else None
        ),
        "volatility_percentile": (
            round(percentile, 6) if percentile is not None else None
        ),
        "qqq_close": round(latest, 6),
        "qqq_recent_high": round(recent_high, 6),
        "drawdown": round(max(0.0, drawdown), 6),
        "sma_200": round(sma, 6) if sma is not None else None,
        "above_trend": above_trend,
    }


def _visible_close(
    frame: pd.DataFrame | None,
    asof: pd.Timestamp | None,
) -> pd.Series:
    if frame is None or frame.empty or "Close" not in frame.columns:
        return pd.Series(dtype=float)
    index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    if index.tz is not None:
        index = index.tz_localize(None)
    values = pd.to_numeric(frame["Close"], errors="coerce")
    output = pd.Series(values.to_numpy(), index=index).dropna().sort_index()
    output = output[~output.index.duplicated(keep="last")]
    if asof is not None:
        boundary = pd.Timestamp(asof)
        if boundary.tzinfo is not None:
            boundary = boundary.tz_localize(None)
        output = output[output.index.normalize() <= boundary.normalize()]
    return output


def _empty_result(
    metadata: StrategyMetadata,
    state: dict[str, Any],
    status: str,
) -> StrategyScanResult:
    return StrategyScanResult(
        strategy=metadata,
        signals=(),
        candidates=(),
        state={
            "status": status,
            "trade_signals_enabled": False,
            "runtime_state": state,
        },
    )


__all__ = [
    "PROJECT_ID",
    "QQQVolDCAParameters",
    "QQQVolDCAStrategy",
    "STRATEGY_ID",
]
