from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from strategy_v1.chan import ChanParameters, analyze_chan, daily_entry_allowed, daily_trailing_exit
from strategy_v1.models import LeaderCandidate, MarketState

from research_platform.models import (
    DataRequirement,
    PlatformSignal,
    RuntimeAdapter,
    SignalStatus,
    StrategyMetadata,
    StrategyScanResult,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class ChanStrategy:
    parameters = ChanParameters()
    metadata = StrategyMetadata(
        strategy_id="chan_v1",
        version="3.0.0",
        name="缠论结构突破（线段中枢重构）",
        description="补齐线段层级与中枢延伸，背驰改为MACD面积比较并新增底背驰买点；等待新独立窗口验证。",
        frequency="1d",
        requires_approval=False,
        lifecycle="HISTORICAL_REJECTED",
        scan_enabled=False,
        backtest_enabled=True,
        runtime_adapter=RuntimeAdapter.CHAN_DAILY,
        data_requirements=(
            DataRequirement("bars", "1d", "front", 120, True, ("Open", "High", "Low", "Close", "Volume")),
            DataRequirement("bars", "1d", "none", 120, True, ("Open", "High", "Low", "Close", "Volume")),
            DataRequirement("sectors", "snapshot", "none", 0, True, ("members",)),
        ),
    )

    def scan(
        self,
        *,
        run_id: str,
        market: MarketState,
        leaders: list[LeaderCandidate],
        daily_front: dict[str, pd.DataFrame],
        daily_raw: dict[str, pd.DataFrame],
        positions: list[dict[str, Any]],
    ) -> StrategyScanResult:
        now = datetime.now(SHANGHAI_TZ)
        signals: list[PlatformSignal] = []
        candidates: list[dict[str, Any]] = []
        leader_by_code = {leader.code: leader for leader in leaders}
        position_by_code = {position["code"]: position for position in positions}

        for code, position in position_by_code.items():
            frame = daily_front.get(code)
            raw = daily_raw.get(code)
            if frame is None or raw is None or len(frame) < 20:
                continue
            state = analyze_chan(frame, self.parameters)
            price = float(pd.to_numeric(raw["Close"], errors="coerce").dropna().iloc[-1])
            reason = ""
            if price <= float(position["stop_price"]):
                reason = "FIXED_STOP"
            elif daily_trailing_exit(
                frame,
                position["entry_time"],
                float(position["average_price"]),
                self.parameters,
            ):
                reason = "TRAILING_PROFIT"
            elif state.breakdown:
                reason = "CENTER_BREAKDOWN"
            elif state.bearish_divergence:
                reason = "BEARISH_DIVERGENCE"
            if reason:
                signals.append(
                    self._signal(
                        run_id,
                        code,
                        "SELL",
                        price,
                        1.0,
                        reason,
                        state,
                        now,
                        leader_by_code.get(code),
                        market.regime,
                    )
                )

        if market.regime == "NORMAL":
            for leader in leaders:
                if leader.code in position_by_code:
                    continue
                frame = daily_front.get(leader.code)
                raw = daily_raw.get(leader.code)
                if frame is None or raw is None or len(frame) < 20:
                    continue
                state = analyze_chan(frame, self.parameters)
                price = float(pd.to_numeric(raw["Close"], errors="coerce").dropna().iloc[-1])
                candidates.append(
                    {
                        "code": leader.code,
                        "name": leader.name,
                        "sector": leader.sector_name,
                        "leader_rank": leader.leader_rank,
                        "leader_score": leader.leader_score,
                        "breakout": state.breakout,
                        "bullish_divergence": state.bullish_divergence,
                        "trend": state.trend,
                        "price": price,
                    }
                )
                if not daily_entry_allowed(frame, self.parameters):
                    continue
                if state.breakout_confirmed:
                    reason = "CENTER_BREAKOUT_MACD"
                else:
                    continue
                strength = min(1.0, 0.5 * leader.sector_score + 0.5 * leader.leader_score)
                signals.append(
                    self._signal(
                        run_id,
                        leader.code,
                        "BUY",
                        price,
                        strength,
                        reason,
                        state,
                        now,
                        leader,
                        market.regime,
                    )
                )

        return StrategyScanResult(
            strategy=self.metadata,
            signals=tuple(signals),
            candidates=tuple(candidates),
            state={
                "market_regime": market.regime,
                "breadth": market.breadth,
                "leader_count": len(leaders),
            },
        )

    def _signal(
        self,
        run_id: str,
        code: str,
        side: str,
        price: float,
        strength: float,
        reason: str,
        chan_state: Any,
        now: datetime,
        leader: LeaderCandidate | None,
        market_regime: str,
    ) -> PlatformSignal:
        timestamp = _shanghai_time(chan_state.merged_bars.index[-1])
        center = chan_state.center
        return PlatformSignal(
            run_id=run_id,
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            generated_at=timestamp,
            available_at=timestamp,
            code=code,
            side=side,  # type: ignore[arg-type]
            strength=float(max(0.0, min(1.0, strength))),
            target_weight=0.40 if side == "BUY" else 0.0,
            horizon="daily-swing",
            valid_until=now + timedelta(days=4),
            stop_price=price * 0.95 if side == "BUY" else None,
            status=SignalStatus.APPROVED,
            reason_codes=(reason,),
            evidence={
                "price": price,
                "market_regime": market_regime,
                "sector_code": leader.sector_code if leader else "",
                "sector_name": leader.sector_name if leader else "",
                "leader_rank": leader.leader_rank if leader else 0,
                "center_lower": center.lower if center else None,
                "center_upper": center.upper if center else None,
            },
        )


def _shanghai_time(value: Any) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(SHANGHAI_TZ)
    else:
        timestamp = timestamp.tz_convert(SHANGHAI_TZ)
    return timestamp.to_pydatetime()
