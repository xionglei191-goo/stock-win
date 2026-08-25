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
from research_platform.strategies.qqq_vol_dca import PROJECT_ID, _visible_close


STRATEGY_ID = "qqq_treasury_rotation_v1"


@dataclass(frozen=True)
class QQQTreasuryRotationParameters:
    qqq_code: str = "QQQ.US"
    tlt_code: str = "TLT.US"
    sgov_code: str = "SGOV.US"
    moving_average_months: int = 10
    return_months: int = 12
    entry_buffer: float = 0.01
    exit_buffer: float = 0.01
    tolerance_band: float = 0.03

    def __post_init__(self) -> None:
        if self.moving_average_months < 2 or self.return_months < 2:
            raise ValueError("Rotation lookbacks must be at least two months")
        if min(self.entry_buffer, self.exit_buffer, self.tolerance_band) < 0:
            raise ValueError("Rotation buffers cannot be negative")


class QQQTreasuryRotationStrategy:
    """Monthly target-allocation research strategy for QQQ, TLT and SGOV."""

    metadata = StrategyMetadata(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        name="QQQ-SGOV-TLT 双动量轮动 V1",
        description=(
            "月度使用10月趋势和相对SGOV的12月总收益，在QQQ、TLT和SGOV之间"
            "切换；包含1%进出缓冲和3%目标权重容忍带。"
        ),
        frequency="1mo",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        strategy_family=PROJECT_ID,
        scan_enabled=False,
        backtest_enabled=False,
        asset_classes=("US_ETF", "US_TREASURY"),
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement(
                "bars",
                "1d",
                "front",
                400,
                True,
                ("Close",),
            ),
        ),
    )

    def __init__(self, parameters: QQQTreasuryRotationParameters | None = None):
        self.parameters = parameters or QQQTreasuryRotationParameters()

    @property
    def required_codes(self) -> tuple[str, ...]:
        params = self.parameters
        return params.qqq_code, params.tlt_code, params.sgov_code

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
        state = _normalise_state(runtime_state, params)
        closes = {
            code: _visible_close(front_bars.get(code), asof)
            for code in self.required_codes
        }
        if any(series.empty for series in closes.values()):
            return _empty_result(self.metadata, state, "ETF_DATA_UNAVAILABLE")

        latest_visible = max(pd.Timestamp(series.index[-1]) for series in closes.values())
        boundary = pd.Timestamp(asof) if asof is not None else latest_visible
        if boundary.tzinfo is not None:
            boundary = boundary.tz_localize(None)
        monthly = {
            code: _completed_monthly_close(series, boundary)
            for code, series in closes.items()
        }
        common_periods = monthly[params.qqq_code].index
        for code in (params.tlt_code, params.sgov_code):
            common_periods = common_periods.intersection(monthly[code].index)
        required = max(params.moving_average_months, params.return_months + 1)
        if len(common_periods) < required:
            return _empty_result(self.metadata, state, "MONTHLY_WARMUP")

        common_periods = common_periods.sort_values()
        decision_period = str(common_periods[-1])
        aligned = {
            code: series.reindex(common_periods).dropna()
            for code, series in monthly.items()
        }
        previous_targets = dict(state["target_weights"])
        sgov_return = _period_return(
            aligned[params.sgov_code], params.return_months
        )
        qqq_metrics = _asset_metrics(
            aligned[params.qqq_code],
            sgov_return,
            previous_targets[params.qqq_code] > 0,
            params,
        )
        tlt_metrics = _asset_metrics(
            aligned[params.tlt_code],
            sgov_return,
            previous_targets[params.tlt_code] > 0,
            params,
        )
        targets, regime = _target_allocation(
            bool(qqq_metrics["eligible"]),
            bool(tlt_metrics["eligible"]),
            params,
        )
        rebalance_due = state["last_rebalance_period"] != decision_period

        candidates: tuple[dict[str, Any], ...] = ()
        if rebalance_due:
            records: list[dict[str, Any]] = []
            metrics_by_code = {
                params.qqq_code: qqq_metrics,
                params.tlt_code: tlt_metrics,
                params.sgov_code: {
                    "return_12m": round(sgov_return, 6),
                    "eligible": True,
                },
            }
            for code in self.required_codes:
                previous = float(previous_targets[code])
                target = float(targets[code])
                delta = target - previous
                action = "HOLD"
                if abs(delta) > params.tolerance_band:
                    action = "INCREASE" if delta > 0 else "REDUCE"
                records.append(
                    {
                        "code": code,
                        "action": action,
                        "previous_target_weight": round(previous, 6),
                        "target_weight": round(target, 6),
                        "target_delta": round(delta, 6),
                        **metrics_by_code[code],
                    }
                )
            candidates = tuple(records)
            state["last_rebalance_period"] = decision_period
            state["target_weights"] = targets
            state["regime"] = regime

        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=candidates,
            state={
                "status": "TARGET_READY",
                "asof": boundary.normalize().date().isoformat(),
                "decision_period": decision_period,
                "rebalance_due": rebalance_due,
                "regime": regime,
                "target_weights": targets,
                "qqq": qqq_metrics,
                "tlt": tlt_metrics,
                "sgov_return_12m": round(sgov_return, 6),
                "trade_signals_enabled": False,
                "execution_note": (
                    "Targets require a US-ETF rebalance engine; generic A-share order execution is disabled."
                ),
                "runtime_state": state,
            },
        )


def _completed_monthly_close(
    close: pd.Series,
    asof: pd.Timestamp,
) -> pd.Series:
    current_period = asof.to_period("M")
    periods = close.index.to_period("M")
    completed = close[periods < current_period]
    completed_periods = completed.index.to_period("M")
    output = completed.groupby(completed_periods).last()
    output.index = pd.PeriodIndex(output.index, freq="M")
    return output.sort_index()


def _period_return(close: pd.Series, months: int) -> float:
    latest = float(close.iloc[-1])
    prior = float(close.iloc[-(months + 1)])
    return latest / prior - 1.0 if prior > 0 else 0.0


def _asset_metrics(
    close: pd.Series,
    cash_return: float,
    previously_held: bool,
    params: QQQTreasuryRotationParameters,
) -> dict[str, Any]:
    latest = float(close.iloc[-1])
    moving_average = float(close.iloc[-params.moving_average_months :].mean())
    total_return = _period_return(close, params.return_months)
    threshold = moving_average * (
        1.0 - params.exit_buffer if previously_held else 1.0 + params.entry_buffer
    )
    trend_pass = latest > threshold
    excess_pass = total_return > cash_return
    return {
        "close": round(latest, 6),
        "sma_10m": round(moving_average, 6),
        "return_12m": round(total_return, 6),
        "cash_return_12m": round(cash_return, 6),
        "previously_held": previously_held,
        "trend_threshold": round(threshold, 6),
        "trend_pass": trend_pass,
        "excess_return_pass": excess_pass,
        "eligible": trend_pass and excess_pass,
    }


def _target_allocation(
    qqq_eligible: bool,
    tlt_eligible: bool,
    params: QQQTreasuryRotationParameters,
) -> tuple[dict[str, float], str]:
    if qqq_eligible and tlt_eligible:
        weights = (0.60, 0.30, 0.10)
        regime = "QQQ_AND_TLT"
    elif qqq_eligible:
        weights = (0.70, 0.00, 0.30)
        regime = "QQQ_ONLY"
    elif tlt_eligible:
        weights = (0.00, 0.60, 0.40)
        regime = "TLT_ONLY"
    else:
        weights = (0.00, 0.00, 1.00)
        regime = "SGOV_ONLY"
    return dict(zip((params.qqq_code, params.tlt_code, params.sgov_code), weights)), regime


def _normalise_state(
    runtime_state: dict[str, Any] | None,
    params: QQQTreasuryRotationParameters,
) -> dict[str, Any]:
    source = runtime_state or {}
    raw_targets = source.get("target_weights") or {}
    targets = {
        code: max(0.0, float(raw_targets.get(code, 0.0)))
        for code in (params.qqq_code, params.tlt_code, params.sgov_code)
    }
    return {
        "last_rebalance_period": str(source.get("last_rebalance_period", "")),
        "target_weights": targets,
        "regime": str(source.get("regime", "UNALLOCATED")),
    }


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
    "QQQTreasuryRotationParameters",
    "QQQTreasuryRotationStrategy",
    "STRATEGY_ID",
]
