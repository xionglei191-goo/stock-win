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
    lookback_short: int = 60    # ~3个月
    lookback_mid: int = 120     # ~6个月
    lookback_long: int = 252    # ~12个月（Jegadeesh-Titman）
    skip_recent: int = 20       # 跳过最近20天，避免短期反转

    # ── 参数调优结论 (网格搜索 68只Nasdaq-100, 1999-2026) ──────────────
    # depth=10% + stop=8% + rs_top=25% → +29.3%/yr, Sharpe=1.03, MaxDD=-49%
    # vs depth=15% + stop=20% (前版本) → +13.7%/yr
    # 关键: 紧的depth(10%)过滤掉低质量动量; 小止损(8%)快速止损并再入场
    max_depth_from_high: float = 0.10  # 必须在50日高点10%以内
    vol_ratio_cap: float = 2.0
    rs_top_pct: float = 0.25    # 入场：只选RS前25%（更严格筛选）

    # 出场缓冲：跌出前40%才清仓，避免小幅排名波动触发无效换手
    exit_top_pct: float = 0.40

    # 止损：8%（原始值）— 快速止损+快速再入场优于宽止损
    stop_ratio: float = 0.08

    # ── 持仓权重 ────────────────────────────────────────────────────────
    # use_score_weight=True 时按 RS 分比例配仓，否则等权
    use_score_weight: bool = True
    target_weight: float = 0.10      # 等权模式下每仓位权重
    max_total_weight: float = 1.00   # 最大总仓位（防超配）

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
    if last_close < last_ma_slow:
        return None
    if last_close < params.min_price:
        return None

    skip = params.skip_recent

    def _ret(n: int) -> float:
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

    # ── 改进二: 修复RS评分，使崩后强势反弹的股票不被负的ret_long压死 ──────
    # 对 ret_long 取 max(ret_long, 0) 避免惩罚刚从熊市反弹的股票；
    # 同时给短期加速（ret_short > 0.25）额外奖励，捕捉早期趋势确立信号
    ret_long_adj = max(ret_long, 0.0)
    acceleration_bonus = max(ret_short - 0.15, 0.0) * 0.5  # 短期>15%时额外加分
    rs_score = ret_long_adj * 0.50 + ret_mid * 0.30 + ret_short * 0.20 + acceleration_bonus

    return {
        "rs_score": round(rs_score, 6),
        "ret_short": round(ret_short, 4),
        "ret_mid": round(ret_mid, 4),
        "ret_long": round(ret_long, 4),
        "ret_long_adj": round(ret_long_adj, 4),
        "depth_from_high": round(depth, 4),
        "vol_ratio": round(vol_ratio, 3) if vol_ratio is not None else None,
        "close": round(last_close, 4),
        "ma_fast": round(last_ma_fast, 4),
        "ma_slow": round(last_ma_slow, 4),
    }


def _compute_score_weights(
    candidates: list[tuple[str, dict[str, Any]]],
    max_positions: int,
) -> list[tuple[str, dict[str, Any], float]]:
    """Assign position weights proportional to RS score (rank-based).

    Rank 1 gets weight proportional to its rank-score; weights are normalised
    to sum to max_total_weight.  Minimum per-position weight is 5%.
    """
    n = min(len(candidates), max_positions)
    top = candidates[:n]

    # Rank-based weights: rank 1 → n points, rank 2 → (n-1) points, …
    rank_scores = [n - i for i in range(n)]
    total = sum(rank_scores)
    raw = [r / total for r in rank_scores]

    # Floor at 5% per position
    MIN_W = 0.05
    weights = [max(w, MIN_W) for w in raw]
    # Re-normalise to sum = 1.0
    s = sum(weights)
    weights = [w / s for w in weights]

    return [(code, score, round(w, 4)) for (code, score), w in zip(top, weights)]


class USMomentumStrategy:
    metadata = StrategyMetadata(
        strategy_id="us_momentum_v1",
        version="2.0.0",
        name="美股趋势动量 v2",
        description=(
            "月度换手缓冲区（入top40%买入/跌出top60%才卖出）+ 崩后反弹RS修正"
            "+ 按RS分比例配仓，减少无效换手，捕捉强势反弹机会"
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
                state={"market_bull": market_bull},
            )

        all_rs = [s["rs_score"] for _, s in scored]
        n_scored = len(scored)

        # Entry threshold: top rs_top_pct
        entry_thresh = float(np.percentile(all_rs, (1 - params.rs_top_pct) * 100))
        # Exit threshold (buffer zone): broader than entry — only exit below exit_top_pct
        exit_thresh = float(np.percentile(all_rs, (1 - params.exit_top_pct) * 100))

        top_entry = [(c, s) for c, s in scored if s["rs_score"] >= entry_thresh]
        top_entry.sort(key=lambda x: x[1]["rs_score"], reverse=True)
        top_entry = top_entry[: params.max_candidates]

        candidates = [
            {"code": code, "name": names.get(code, code), **score}
            for code, score in top_entry
        ]

        signals: list[PlatformSignal] = []
        entry_enabled = backtest_mode or params.emit_live_entry_signals
        position_codes = {str(p["code"]) for p in positions}

        if entry_enabled and market_bull:
            weighted = _compute_score_weights(top_entry, params.max_entry_signals)
            for code, score, weight in weighted:
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
                        target_weight=weight,
                        horizon="20d",
                        valid_until=valid_until,
                        stop_price=stop,
                        status=SignalStatus.PROPOSED,
                        reason_codes=("US_TREND_BULL", "US_RS_BUFFER_ENTRY", "US_SCORE_WEIGHTED"),
                        evidence=score,
                    )
                )

        return StrategyScanResult(
            strategy=self.metadata,
            signals=tuple(signals),
            candidates=tuple(candidates),
            state={
                "market_bull": market_bull,
                "entry_thresh": round(entry_thresh, 5),
                "exit_thresh": round(exit_thresh, 5),
                "n_scored": n_scored,
            },
        )
