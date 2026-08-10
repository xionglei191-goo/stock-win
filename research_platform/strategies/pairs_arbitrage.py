from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from research_platform.models import (
    DataRequirement,
    ExecutionModel,
    OrderGroupAction,
    OrderGroupIntent,
    OrderLegIntent,
    RuntimeAdapter,
    SignalStatus,
    StrategyMetadata,
    StrategyScanResult,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PairSpec:
    left: str
    right: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.left}|{self.right}"


DEFAULT_PAIRS = (
    PairSpec("600036.SH", "601166.SH", "招商银行 / 兴业银行"),
    PairSpec("601318.SH", "601628.SH", "中国平安 / 中国人寿"),
)


class PairsArbitrageStrategy:
    metadata = StrategyMetadata(
        strategy_id="pairs_arbitrage_v1",
        version="1.0.0",
        name="配对套利 V1",
        description="对固定、版本化的同行业股票对进行收盘价差均值回归研究。",
        frequency="1d",
        requires_approval=True,
        asset_classes=("A_STOCK",),
        execution_model=ExecutionModel.MULTI_LEG,
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        supports_short=True,
        data_requirements=(
            DataRequirement("bars", "1d", "front", 90, True, ("Close",)),
            DataRequirement("bars", "1d", "none", 90, True, ("Open", "Close")),
            DataRequirement("symbols", "static", "none", 0, True, ("Name", "BelongRZRQ")),
        ),
    )

    lookback = 60
    entry_zscore = 2.0
    exit_zscore = 0.5
    stop_zscore = 3.5
    minimum_correlation = 0.70
    gross_target_weight = 0.40

    def __init__(self, pairs: tuple[PairSpec, ...] = DEFAULT_PAIRS):
        self.pairs = pairs

    @property
    def required_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(code for pair in self.pairs for code in (pair.left, pair.right)))

    def scan(
        self,
        *,
        run_id: str,
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        positions: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> StrategyScanResult:
        positions = positions or []
        active = {str(item["group_key"]): item for item in positions}
        candidates: list[dict[str, Any]] = []
        intents: list[OrderGroupIntent] = []
        latest_asof: pd.Timestamp | None = None

        for pair in self.pairs:
            stats = self.pair_statistics(front_bars.get(pair.left), front_bars.get(pair.right))
            if stats is None:
                continue
            latest_asof = max(latest_asof, stats["asof"]) if latest_asof is not None else stats["asof"]
            candidate = {
                "group_key": pair.key,
                "name": pair.name,
                "left": pair.left,
                "right": pair.right,
                "zscore": stats["zscore"],
                "correlation": stats["correlation"],
                "hedge_ratio": stats["hedge_ratio"],
                "eligible": stats["correlation"] >= self.minimum_correlation,
            }
            candidates.append(candidate)
            current = active.get(pair.key)
            if current:
                reason = "PAIR_MEAN_REVERTED" if abs(stats["zscore"]) <= self.exit_zscore else ""
                if abs(stats["zscore"]) >= self.stop_zscore:
                    reason = "PAIR_DIVERGENCE_STOP"
                if reason:
                    intents.append(self._close_intent(run_id, pair, stats, current, reason))
                continue
            if stats["correlation"] < self.minimum_correlation:
                continue
            if self.entry_zscore <= abs(stats["zscore"]) < self.stop_zscore:
                intents.append(self._open_intent(run_id, pair, stats))

        return StrategyScanResult(
            strategy=self.metadata,
            signals=(),
            candidates=tuple(candidates),
            state={
                "asof": latest_asof.date().isoformat() if latest_asof is not None else None,
                "pair_count": len(candidates),
                "entry_zscore": self.entry_zscore,
                "exit_zscore": self.exit_zscore,
                "stop_zscore": self.stop_zscore,
                "minimum_correlation": self.minimum_correlation,
                "short_execution": "paper_only",
            },
            order_groups=tuple(intents),
        )

    def pair_statistics(
        self,
        left: pd.DataFrame | None,
        right: pd.DataFrame | None,
    ) -> dict[str, Any] | None:
        if left is None or right is None:
            return None
        left_close = pd.to_numeric(left.get("Close"), errors="coerce").rename("left")
        right_close = pd.to_numeric(right.get("Close"), errors="coerce").rename("right")
        prices = pd.concat([left_close, right_close], axis=1, join="inner").dropna()
        prices = prices[(prices > 0).all(axis=1)]
        if len(prices) < self.lookback + 1:
            return None
        window = prices.tail(self.lookback)
        x = np.log(window["right"].to_numpy(dtype=float))
        y = np.log(window["left"].to_numpy(dtype=float))
        variance = float(np.var(x))
        if variance <= 1e-12:
            return None
        hedge_ratio = float(np.cov(x, y, ddof=0)[0, 1] / variance)
        spread = pd.Series(y - hedge_ratio * x, index=window.index)
        spread_std = float(spread.std(ddof=0))
        if spread_std <= 1e-12:
            return None
        zscore = float((spread.iloc[-1] - spread.mean()) / spread_std)
        correlation = float(window["left"].pct_change().corr(window["right"].pct_change()))
        return {
            "asof": pd.Timestamp(window.index[-1]),
            "zscore": zscore,
            "correlation": correlation if np.isfinite(correlation) else 0.0,
            "hedge_ratio": hedge_ratio,
            "left_price": float(window["left"].iloc[-1]),
            "right_price": float(window["right"].iloc[-1]),
        }

    def _open_intent(
        self,
        run_id: str,
        pair: PairSpec,
        stats: dict[str, Any],
    ) -> OrderGroupIntent:
        if stats["zscore"] > 0:
            legs = (
                OrderLegIntent(pair.left, "SHORT", 1.0, 0.50),
                OrderLegIntent(pair.right, "BUY", abs(stats["hedge_ratio"]), 0.50),
            )
        else:
            legs = (
                OrderLegIntent(pair.left, "BUY", 1.0, 0.50),
                OrderLegIntent(pair.right, "SHORT", abs(stats["hedge_ratio"]), 0.50),
            )
        generated, available, valid_until = _intent_times(stats["asof"])
        return OrderGroupIntent(
            run_id=run_id,
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            generated_at=generated,
            available_at=available,
            valid_until=valid_until,
            group_key=pair.key,
            action=OrderGroupAction.OPEN,
            strength=min(1.0, abs(stats["zscore"]) / self.stop_zscore),
            gross_target_weight=self.gross_target_weight,
            status=SignalStatus.PROPOSED,
            reason_codes=("PAIR_ZSCORE_ENTRY", "CORRELATION_CONFIRMED"),
            legs=legs,
            evidence={"pair_name": pair.name, **_serializable_stats(stats)},
        )

    def _close_intent(
        self,
        run_id: str,
        pair: PairSpec,
        stats: dict[str, Any],
        position: dict[str, Any],
        reason: str,
    ) -> OrderGroupIntent:
        legs = tuple(
            OrderLegIntent(
                str(item["code"]),
                "SELL" if str(item["side"]) == "LONG" else "COVER",
                float(item.get("ratio", 1.0)),
                float(item.get("target_weight", 0.50)),
            )
            for item in position.get("legs", [])
        )
        generated, available, valid_until = _intent_times(stats["asof"])
        return OrderGroupIntent(
            run_id=run_id,
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            generated_at=generated,
            available_at=available,
            valid_until=valid_until,
            group_key=pair.key,
            action=OrderGroupAction.CLOSE,
            strength=1.0,
            gross_target_weight=0.0,
            status=SignalStatus.APPROVED,
            reason_codes=(reason,),
            legs=legs,
            evidence={"pair_name": pair.name, **_serializable_stats(stats)},
        )


def _intent_times(value: Any) -> tuple[datetime, datetime, datetime]:
    timestamp = pd.Timestamp(value).normalize() + pd.Timedelta(hours=18)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(SHANGHAI)
    else:
        timestamp = timestamp.tz_convert(SHANGHAI)
    next_day = timestamp + pd.offsets.BDay(1)
    return (
        timestamp.to_pydatetime(),
        timestamp.to_pydatetime(),
        next_day.replace(hour=15).to_pydatetime(),
    )


def _serializable_stats(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "asof": pd.Timestamp(stats["asof"]).date().isoformat(),
        "zscore": float(stats["zscore"]),
        "correlation": float(stats["correlation"]),
        "hedge_ratio": float(stats["hedge_ratio"]),
        "left_price": float(stats["left_price"]),
        "right_price": float(stats["right_price"]),
    }
