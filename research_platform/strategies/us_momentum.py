from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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


NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class USMomentumParameters:
    ma_fast: int = 50
    ma_slow: int = 200
    #改进2: 经典 12-1月动量 —— 252日总动量跳过最近20天，120日动量跳过最近20天
    lookback_short: int = 60    # ~3个月（跳过最近20天在_score_bars内处理）
    lookback_mid: int = 120     # ~6个月
    lookback_long: int = 252    # ~12个月（经典Jegadeesh-Titman动量）
    skip_recent: int = 20       # 跳过最近20天，避免短期反转噪音
    max_depth_from_high: float = 0.15  # 放宽：允许距高点15%以内
    vol_ratio_cap: float = 2.0
    rs_top_pct: float = 0.40
    # 改进3: 止损从8%放宽到20%，减少被震出优质动量股
    stop_ratio: float = 0.20
    target_weight: float = 0.10
    max_candidates: int = 30
    max_entry_signals: int = 10
    emit_live_entry_signals: bool = False
    min_price: float = 5.0
    min_avg_volume: float = 500_000.0
    min_dollar_volume: float = 5_000_000.0
    max_ret_long: float = 10.0  # 放宽到1000%，只过滤极端数据异常
    min_vol_ratio: float = 0.05
    # 改进4: 市场择时 —— 用指数级别MA50>MA200作为开关
    use_market_regime: bool = True
    market_code: str = "SPY.US"


def _score_bars(bars: pd.DataFrame, params: USMomentumParameters) -> dict[str, Any] | None:
    min_bars = max(params.ma_slow, params.lookback_long + params.skip_recent) + 10
    if len(bars) < min_bars:
        return None

    close = bars["Close"]
    volume = bars["Volume"] if "Volume" in bars.columns else None

    ma_fast = close.rolling(params.ma_fast).mean()
    ma_slow = close.rolling(params.ma_slow).mean()

    last_close = float(close.iloc[-1])
    last_ma_fast = float(ma_fast.iloc[-1])
    last_ma_slow = float(ma_slow.iloc[-1])

    if np.isnan(last_ma_fast) or np.isnan(last_ma_slow):
        return None
    # 改进4: 个股不再要求自身MA过滤，改由外部市场择时控制
    # 仍保留 close > MA200 作为最低趋势条件（不要求MA50>MA200）
    if last_close < last_ma_slow:
        return None
    if last_close < params.min_price:
        return None

    skip = params.skip_recent  # 跳过最近N天，避免短期反转噪音

    def _ret(n: int) -> float:
        # 回报从 -(n+skip) 到 -skip，跳过最近skip天
        idx_end = -(skip + 1) if skip > 0 else -1
        idx_start = -(n + skip + 1)
        if len(close) <= n + skip:
            return 0.0
        p_end = float(close.iloc[idx_end])
        p_start = float(close.iloc[idx_start])
        return (p_end / p_start - 1) if p_start > 0 else 0.0

    ret_short = _ret(params.lookback_short)
    ret_mid = _ret(params.lookback_mid)
    ret_long = _ret(params.lookback_long)

    # 只过滤极端异常数据（>1000%）
    if abs(ret_long) > params.max_ret_long:
        return None

    high_window = min(params.ma_fast, len(close))
    recent_high = float(close.iloc[-high_window:].max())
    depth = (recent_high - last_close) / recent_high if recent_high > 0 else 1.0
    if depth > params.max_depth_from_high:
        return None

    vol_ratio: float | None = None
    if volume is not None:
        vol5 = float(volume.iloc[-5:].mean())
        vol_base_slice = volume.iloc[-65:-5]
        if len(vol_base_slice) >= 20:
            vol60 = float(vol_base_slice.mean())
            if vol60 > 0:
                if vol60 < params.min_avg_volume:
                    return None
                dollar_vol = vol60 * last_close
                if dollar_vol < params.min_dollar_volume:
                    return None
                vol_ratio = vol5 / vol60
                if vol_ratio > params.vol_ratio_cap:
                    return None
                if vol_ratio < params.min_vol_ratio:
                    return None

    # 改进2: 经典动量权重 —— 长期动量最重要
    rs_score = ret_long * 0.50 + ret_mid * 0.30 + ret_short * 0.20

    return {
        "rs_score": round(rs_score, 6),
        "ret_short": round(ret_short, 4),
        "ret_mid": round(ret_mid, 4),
        "ret_long": round(ret_long, 4),
        "depth_from_high": round(depth, 4),
        "vol_ratio": round(vol_ratio, 3) if vol_ratio is not None else None,
        "close": round(last_close, 4),
        "ma_fast": round(last_ma_fast, 4),
        "ma_slow": round(last_ma_slow, 4),
    }


class USMomentumStrategy:
    metadata = StrategyMetadata(
        strategy_id="us_momentum_v1",
        version="1.0.0",
        name="美股趋势动量",
        description=(
            "双均线趋势过滤（MA50>MA200）+ 相对强度排名，筛选处于上升趋势且近期动量"
            "领先的美股；研究阶段仅输出候选，入场信号需人工审批"
        ),
        frequency="1d",
        requires_approval=True,
        lifecycle="RESEARCH_ONLY",
        category=StrategyCategory.RESEARCH_PROJECT,
        asset_classes=("US_STOCK",),
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
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
        names: dict[str, str] | None = None,
        positions: list[dict[str, Any]] | None = None,
        backtest_mode: bool = False,
        asof: pd.Timestamp | None = None,
        **_: Any,
    ) -> StrategyScanResult:
        names = names or {}
        positions = positions or []
        params = self.parameters

        now = datetime.now(NY_TZ)
        generated_at = now if asof is None else asof.to_pydatetime().replace(tzinfo=NY_TZ)
        available_at = generated_at
        valid_until = generated_at + timedelta(days=1)

        scored: list[tuple[str, dict[str, Any]]] = []

        # 改进4: 市场择时 —— SPY MA50>MA200为熊市时不开新仓
        market_bull = True
        if params.use_market_regime:
            spy_bars = front_bars.get(params.market_code)
            if spy_bars is not None and len(spy_bars) >= params.ma_slow + 10:
                spy_close = spy_bars["Close"]
                spy_ma50 = float(spy_close.rolling(params.ma_fast).mean().iloc[-1])
                spy_ma200 = float(spy_close.rolling(params.ma_slow).mean().iloc[-1])
                market_bull = spy_ma50 > spy_ma200

        for code, bars in front_bars.items():
            if not code.endswith(".US"):
                continue
            if code == params.market_code:
                continue
            result = _score_bars(bars, params)
            if result is not None:
                scored.append((code, result))

        if not scored:
            return StrategyScanResult(
                strategy=self.metadata,
                signals=(),
                candidates=(),
                state={},
            )

        # Relative strength percentile filter
        all_rs = [s["rs_score"] for _, s in scored]
        threshold = float(np.percentile(all_rs, (1 - params.rs_top_pct) * 100))
        top = [(c, s) for c, s in scored if s["rs_score"] >= threshold]
        top.sort(key=lambda x: x[1]["rs_score"], reverse=True)
        top = top[: params.max_candidates]

        candidates = [
            {
                "code": code,
                "name": names.get(code, code),
                **score,
            }
            for code, score in top
        ]

        signals: list[PlatformSignal] = []
        entry_enabled = backtest_mode or params.emit_live_entry_signals
        position_codes = {str(p["code"]) for p in positions}

        if entry_enabled and market_bull:
            for code, score in top[: params.max_entry_signals]:
                if code in position_codes:
                    continue
                stop = round(score["close"] * (1 - params.stop_ratio), 4)
                signals.append(
                    PlatformSignal(
                        run_id=run_id,
                        strategy_id=self.metadata.strategy_id,
                        strategy_version=self.metadata.version,
                        generated_at=generated_at,
                        available_at=available_at,
                        code=code,
                        side="BUY",
                        strength=float(score["rs_score"]),
                        target_weight=params.target_weight,
                        horizon="20d",
                        valid_until=valid_until,
                        stop_price=stop,
                        status=SignalStatus.PROPOSED,
                        reason_codes=("US_TREND_BULL", "US_RS_TOP30PCT", "US_TIGHT_BASE"),
                        evidence=score,
                    )
                )

        return StrategyScanResult(
            strategy=self.metadata,
            signals=tuple(signals),
            candidates=tuple(candidates),
            state={"market_bull": market_bull},
        )
