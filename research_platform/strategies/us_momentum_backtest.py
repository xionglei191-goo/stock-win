"""Walk-forward monthly backtest for USMomentumStrategy.

Data requirement: each stock needs >= ma_slow + 10 bars (default 210) before it enters
any rebalance scan. With TDX US data covering ~220 bars (≈ Sep 2025–Aug 2026), the
effective out-of-sample window is roughly 3–5 monthly rebalances. For a longer backtest
download additional history from TDX before running.

Usage:
    python -m research_platform.strategies.us_momentum_backtest
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .us_momentum import USMomentumStrategy, USMomentumParameters


@dataclass
class _Trade:
    code: str
    entry_date: str
    entry_price: float
    stop_price: float
    invested: float
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""

    @property
    def ret(self) -> float:
        if not self.exit_price or not self.entry_price:
            return 0.0
        return self.exit_price / self.entry_price - 1

    @property
    def pnl(self) -> float:
        return self.invested * self.ret


def run_backtest(
    bars: dict[str, pd.DataFrame],
    names: dict[str, str],
    initial_capital: float = 100_000.0,
    params: USMomentumParameters | None = None,
) -> dict[str, Any]:
    """Simulate monthly momentum rebalancing.

    bars: dict code -> DataFrame with DatetimeIndex and columns Open/High/Low/Close/Volume.
    Returns a result dict with equity_curve, trades, and summary metrics.
    """
    if params is None:
        params = USMomentumParameters()
    strategy = USMomentumStrategy(params)

    # Aggregate all trading dates
    all_dates: list[pd.Timestamp] = []
    for df in bars.values():
        if isinstance(df.index, pd.DatetimeIndex) and len(df) > 0:
            all_dates.extend(df.index.tolist())
    if not all_dates:
        return {"error": "no bar data"}
    trading_dates = sorted(set(all_dates))

    # Month-end rebalance dates: last trading day of each month
    date_series = pd.Series(trading_dates)
    months = date_series.dt.to_period("M")
    rebalance_dates: list[pd.Timestamp] = []
    for period in sorted(months.unique()):
        month_dates = date_series[months == period]
        rebalance_dates.append(month_dates.iloc[-1])

    # Need >= ma_slow bars before first rebalance
    min_warmup = params.ma_slow + 10

    def _slice_bars(target: pd.Timestamp, lookback: int = 1300) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for code, df in bars.items():
            if not isinstance(df.index, pd.DatetimeIndex):
                continue
            sub = df[df.index <= target].tail(lookback)
            if len(sub) >= min_warmup:
                out[code] = sub
        return out

    def _price(code: str, target: pd.Timestamp, col: str = "Close") -> float | None:
        df = bars.get(code)
        if df is None or not isinstance(df.index, pd.DatetimeIndex):
            return None
        row = df[df.index == target]
        if row.empty:
            # nearest available price on or before target
            sub = df[df.index <= target]
            if sub.empty:
                return None
            row = sub.iloc[[-1]]
        val = row[col].iloc[0]
        return float(val) if pd.notna(val) else None

    cash = initial_capital
    positions: dict[str, _Trade] = {}
    closed_trades: list[_Trade] = []
    equity_curve: dict[pd.Timestamp, float] = {}

    valid_rebalances = [
        d for d in rebalance_dates
        if sum(1 for dt in trading_dates if dt <= d) >= min_warmup
    ]
    if not valid_rebalances:
        return {"error": f"need {min_warmup} bars before first rebalance, only {len(trading_dates)} total"}

    for rb_date in valid_rebalances:
        # Check stop losses at this rebalance date
        for code in list(positions):
            pos = positions[code]
            price = _price(code, rb_date)
            if price is not None and price <= pos.stop_price:
                pos.exit_date = str(rb_date.date())
                pos.exit_price = pos.stop_price
                pos.exit_reason = "stop"
                cash += pos.invested * (pos.stop_price / pos.entry_price)
                closed_trades.append(pos)
                del positions[code]

        # Scan candidates
        sliced = _slice_bars(rb_date)
        result = strategy.scan(
            run_id=f"bt_{rb_date.date()}",
            front_bars=sliced,
            names=names,
            backtest_mode=True,
        )

        # ── 改进一: 缓冲区出场 ──────────────────────────────────────────
        # 只有持仓跌出 exit_thresh（RS前60%）才清仓，避免无效换手
        exit_thresh = result.state.get("exit_thresh", None)
        entry_top_codes = {c["code"] for c in result.candidates[: params.max_entry_signals]}
        # Build a lookup of rs_score for all candidates
        cand_score: dict[str, float] = {c["code"]: c["rs_score"] for c in result.candidates}

        for code in list(positions):
            if code in entry_top_codes:
                continue  # still in top-entry set, keep holding
            pos_rs = cand_score.get(code)
            if pos_rs is not None and exit_thresh is not None and pos_rs >= exit_thresh:
                continue  # RS still above exit threshold, keep holding (buffer zone)
            # Either not scored at all, or RS fell below exit threshold → exit
            pos = positions[code]
            price = _price(code, rb_date) or pos.entry_price
            pos.exit_date = str(rb_date.date())
            pos.exit_price = price
            pos.exit_reason = "rebalance"
            cash += pos.invested * (price / pos.entry_price)
            closed_trades.append(pos)
            del positions[code]

        # ── 改进三: 按RS分比例配仓 ──────────────────────────────────────
        # Compute total equity for position sizing
        total_equity = cash + sum(
            pos.invested * ((_price(code, rb_date) or pos.entry_price) / pos.entry_price)
            for code, pos in positions.items()
        )
        from .us_momentum import _compute_score_weights
        weighted_slots = _compute_score_weights(
            [(c["code"], c) for c in result.candidates[: params.max_entry_signals]],
            params.max_entry_signals,
        )
        for code, score, weight in weighted_slots:
            if code in positions:
                continue
            price = score.get("close") or 0.0
            if price <= 0:
                continue
            slot_value = total_equity * weight
            if cash < slot_value * 0.5:
                continue
            stop = round(price * (1 - params.stop_ratio), 4)
            actual = min(slot_value, cash)
            cash -= actual
            positions[code] = _Trade(
                code=code,
                entry_date=str(rb_date.date()),
                entry_price=price,
                stop_price=stop,
                invested=actual,
            )

        # Mark-to-market equity
        mkt = cash
        for code, pos in positions.items():
            price = _price(code, rb_date) or pos.entry_price
            mkt += pos.invested * (price / pos.entry_price)
        equity_curve[rb_date] = mkt

    # Close remaining at last date
    last = valid_rebalances[-1] if valid_rebalances else None
    for code, pos in positions.items():
        price = _price(code, last) or pos.entry_price if last else pos.entry_price
        pos.exit_date = str(last.date()) if last else ""
        pos.exit_price = price
        pos.exit_reason = "end"
        closed_trades.append(pos)

    eq = pd.Series(equity_curve).sort_index()
    n_months = len(valid_rebalances)
    total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1) if len(eq) >= 2 else 0.0
    annual_ret = float((1 + total_ret) ** (12 / max(1, n_months)) - 1)

    monthly_rets = eq.pct_change().dropna()
    sharpe = float(monthly_rets.mean() / monthly_rets.std() * (12 ** 0.5)) if monthly_rets.std() > 0 else 0.0

    rolling_max = eq.expanding().max()
    max_dd = float(((eq - rolling_max) / rolling_max).min()) if len(eq) >= 2 else 0.0

    wins = [t for t in closed_trades if t.ret > 0]
    return {
        "period": f"{valid_rebalances[0].date()} → {valid_rebalances[-1].date()}" if valid_rebalances else "",
        "rebalances": n_months,
        "total_return": round(total_ret, 4),
        "annual_return": round(annual_ret, 4),
        "sharpe_monthly": round(sharpe, 3),
        "max_drawdown": round(max_dd, 4),
        "n_trades": len(closed_trades),
        "win_rate": round(len(wins) / len(closed_trades), 3) if closed_trades else 0.0,
        "avg_trade_ret": round(sum(t.ret for t in closed_trades) / len(closed_trades), 4) if closed_trades else 0.0,
        "equity_curve": {str(k.date()): round(v, 2) for k, v in equity_curve.items()},
        "trades": [
            {
                "code": t.code,
                "entry": t.entry_date,
                "exit": t.exit_date,
                "ret": round(t.ret, 4),
                "pnl": round(t.pnl, 2),
                "reason": t.exit_reason,
            }
            for t in closed_trades
        ],
    }
