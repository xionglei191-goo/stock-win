"""Broad-universe backtest for USMomentumStrategy.

Universe: all TDX US stocks with 10yr+ history (2500+ bars), pre-filtered by
recent daily dollar volume >= $50M. This replaces the Nasdaq-100 universe to
enable genuine cross-sector momentum rotation.

Usage:
    python -m research_platform.strategies.us_momentum_broad_backtest
"""
from __future__ import annotations

import os
import struct
import sys
import time

import pandas as pd

sys.path.insert(0, r"D:\Project\stock")

from strategy_v1.config import StrategyConfig
from strategy_v1.tdx_adapter import TdxAdapter
from research_platform.strategies.us_momentum import USMomentumParameters
from research_platform.strategies.us_momentum_backtest import run_backtest

TDX_DIR = r"D:\Project\stock\tdx-mock\vipdoc\ds\lday"
TDX_BASE = r"D:\Project\stock\strategy_v1"


def prefilter_universe(
    min_bars: int = 2500,
    min_dollar_vol: float = 50_000_000,
) -> list[str]:
    """Scan TDX files directly (no DLL) to build the candidate universe.

    Returns codes like 'AAPL.US'. Reads only the last bar of each file so this
    runs in seconds even across 13,000+ files.
    """
    codes: list[str] = []
    for fname in os.listdir(TDX_DIR):
        if not (fname.startswith("74#") and fname.endswith(".day")):
            continue
        path = os.path.join(TDX_DIR, fname)
        sz = os.path.getsize(path)
        if sz < min_bars * 32:
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(-32, 2)
                row = struct.unpack("<IffffIII", fh.read(32))
            # row: date, open, high, low, close, volume, amount, count
            close, volume = row[4], row[5]
            if close * volume >= min_dollar_vol:
                ticker = fname[3:-4]  # strip '74#' prefix and '.day' suffix
                codes.append(f"{ticker}.US")
        except Exception:
            continue
    return sorted(codes)


def _equity_subperiod(equity: pd.Series, start: str, end: str) -> dict:
    sub = equity[(equity.index >= start) & (equity.index <= end)]
    if len(sub) < 3:
        return {}
    n_months = len(sub)
    total = float(sub.iloc[-1] / sub.iloc[0] - 1)
    annual = float((1 + total) ** (12 / max(1, n_months)) - 1)
    mr = sub.pct_change().dropna()
    sharpe = float(mr.mean() / mr.std() * 12 ** 0.5) if mr.std() > 0 else 0.0
    roll_max = sub.expanding().max()
    max_dd = float(((sub - roll_max) / roll_max).min())
    return {"total": total, "annual": annual, "sharpe": sharpe, "max_dd": max_dd}


def _benchmark_stats(df: pd.DataFrame, start: str, end: str) -> dict:
    sub = df[(df.index >= start) & (df.index <= end)]
    if len(sub) < 12:
        return {}
    monthly = sub["Close"].resample("ME").last().pct_change().dropna()
    if len(monthly) < 3:
        return {}
    total = float(sub["Close"].iloc[-1] / sub["Close"].iloc[0] - 1)
    annual = float((1 + monthly.mean()) ** 12 - 1)
    sharpe = float(monthly.mean() / monthly.std() * 12 ** 0.5) if monthly.std() > 0 else 0.0
    roll_max = sub["Close"].expanding().max()
    max_dd = float(((sub["Close"] - roll_max) / roll_max).min())
    return {"total": total, "annual": annual, "sharpe": sharpe, "max_dd": max_dd}


def _print_row(label: str, s: dict, bm: dict | None = None) -> None:
    if not s:
        return
    line = (
        f"  {label:<18}  ann={s['annual']*100:+6.1f}%"
        f"  sharpe={s['sharpe']:.2f}"
        f"  maxdd={s['max_dd']*100:5.1f}%"
        f"  total={s['total']*100:+7.1f}%"
    )
    if bm:
        alpha = s["annual"] - bm["annual"]
        line += f"  |  QQQ ann={bm['annual']*100:+6.1f}%  alpha={alpha*100:+5.1f}%"
    print(line)


if __name__ == "__main__":
    t0 = time.time()

    # ── 1. Build universe ──────────────────────────────────────────────────
    print("Scanning universe from TDX files …")
    universe = prefilter_universe(min_bars=2500, min_dollar_vol=50_000_000)
    for extra in ("SPY.US", "QQQ.US"):
        if extra not in universe:
            universe.append(extra)
    print(f"Universe: {len(universe)} stocks  ({time.time()-t0:.1f}s)")

    # ── 2. Fetch bars ──────────────────────────────────────────────────────
    print("Fetching bars (this may take a few minutes) …")
    config = StrategyConfig()
    with TdxAdapter(config, TDX_BASE) as adapter:
        _, names = adapter.list_us_stocks()
        bars = adapter.fetch_bars(universe, period="1d", count=9999)
    print(f"Fetched {len(bars)} stocks  ({time.time()-t0:.1f}s)")

    qqq_df = bars.get("QQQ.US")
    spy_df = bars.get("SPY.US")

    # ── 3. Full-history backtest ──────────────────────────────────────────
    print("Running full-history backtest …")
    params = USMomentumParameters()
    result = run_backtest(bars, names, params=params)
    if "error" in result:
        print("ERROR:", result["error"])
        sys.exit(1)
    print(f"Backtest done  ({time.time()-t0:.1f}s)\n")

    eq = pd.Series(
        {pd.Timestamp(k): v for k, v in result["equity_curve"].items()}
    ).sort_index()
    full_start = str(eq.index[0].date())
    full_end   = str(eq.index[-1].date())

    # ── 4. Print results ──────────────────────────────────────────────────
    print(f"{'='*72}")
    print(f"  BROAD UNIVERSE MOMENTUM BACKTEST  {full_start} → {full_end}")
    print(f"  Universe: {len(universe)} stocks (10yr+ history, dollar_vol≥$50M)")
    print(f"{'='*72}")

    periods = [
        ("Full period",   full_start,  full_end),
        ("1999–2013",     "1999-01-01", "2012-12-31"),
        ("2013–2026",     "2013-01-01", "2026-12-31"),
        ("2012–2026",     "2012-01-01", "2026-12-31"),
        ("2020–2026",     "2020-01-01", "2026-12-31"),
    ]
    for label, s, e in periods:
        strat = _equity_subperiod(eq, s, e)
        bm    = _benchmark_stats(qqq_df, s, e) if qqq_df is not None else {}
        _print_row(label, strat, bm or None)

    print(f"\n  Rebalances: {result['rebalances']}   "
          f"Trades: {result['n_trades']}   "
          f"Win-rate: {result['win_rate']*100:.1f}%   "
          f"Avg trade ret: {result['avg_trade_ret']*100:.2f}%")
    print(f"  Elapsed: {time.time()-t0:.0f}s")

    # ── 5. SPY comparison (bonus) ─────────────────────────────────────────
    if spy_df is not None:
        print(f"\n{'─'*72}")
        print("  SPY comparison")
        for label, s, e in periods:
            bm = _benchmark_stats(spy_df, s, e)
            if bm:
                strat = _equity_subperiod(eq, s, e)
                if strat:
                    alpha = strat["annual"] - bm["annual"]
                    print(f"  {label:<18}  strat={strat['annual']*100:+6.1f}%"
                          f"  SPY={bm['annual']*100:+6.1f}%"
                          f"  alpha vs SPY={alpha*100:+5.1f}%")
