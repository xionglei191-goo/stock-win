from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import warnings

import pandas as pd

from .chan import ChanState, analyze_chan, daily_entry_allowed, daily_trailing_exit
from .config import StrategyConfig
from .market import evaluate_market_regime, filter_universe, rank_leaders, rank_sectors
from .models import LeaderCandidate, MarketState, SectorScore, Signal
from .portfolio import PaperBroker
from .storage import append_equity, append_signals, append_trades, load_portfolio, save_portfolio
from .tdx_adapter import TdxAdapter


@dataclass(frozen=True)
class ScanResult:
    market: MarketState
    sectors: tuple[SectorScore, ...]
    leaders: tuple[LeaderCandidate, ...]
    signals: tuple[Signal, ...]
    equity: float
    position_count: int


def _previous_closes(daily_bars: dict[str, pd.DataFrame]) -> dict[str, float]:
    result: dict[str, float] = {}
    for code, frame in daily_bars.items():
        if "Close" not in frame.columns:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
        if not close.empty:
            result[code] = float(close.iloc[-1])
    return result


def _latest_signal(
    code: str,
    frame: pd.DataFrame,
    chan: ChanState,
    side: str,
    reason: str,
    market: MarketState,
    leader: LeaderCandidate | None = None,
    execution_price: float | None = None,
) -> Signal:
    latest = frame.iloc[-1]
    center = chan.center
    return Signal(
        timestamp=pd.Timestamp(frame.index[-1]).to_pydatetime(),
        code=code,
        side=side,  # type: ignore[arg-type]
        price=float(execution_price if execution_price is not None else latest["Close"]),
        reason=reason,
        market_regime=market.regime,
        sector_code=leader.sector_code if leader else "",
        sector_name=leader.sector_name if leader else "",
        leader_rank=leader.leader_rank if leader else 0,
        center_lower=center.lower if center else None,
        center_upper=center.upper if center else None,
    )


def run_scan(
    adapter: TdxAdapter,
    config: StrategyConfig,
    refresh_sectors: bool = False,
    max_stocks: int | None = None,
) -> ScanResult:
    config.ensure_runtime_dirs()
    codes, names = adapter.list_a_shares()
    if max_stocks is not None:
        codes = codes[:max_stocks]

    raw_daily = adapter.fetch_bars(codes, "1d", config.daily_lookback)
    daily_bars = filter_universe(raw_daily, names, config)
    if not daily_bars:
        raise RuntimeError("No eligible A-share daily data was returned")

    index_map = adapter.fetch_bars(["999999.SH"], "1d", config.daily_lookback)
    index_bars = index_map.get("999999.SH")
    if index_bars is None:
        fallback = adapter.fetch_bars(["000001.SH"], "1d", config.daily_lookback)
        index_bars = fallback.get("000001.SH")
    if index_bars is None:
        raise RuntimeError("Shanghai index data is unavailable")

    market = evaluate_market_regime(index_bars, daily_bars, config)
    sector_members = adapter.load_sector_members(refresh=refresh_sectors)
    sectors = rank_sectors(sector_members, daily_bars, config)
    leaders = rank_leaders(sectors, sector_members, daily_bars, names, config)
    leader_by_code = {leader.code: leader for leader in leaders}

    state = load_portfolio(config)
    broker = PaperBroker(config, state)
    active_codes = set(leader_by_code)
    active_codes.update(state.positions)
    active_codes.update(order.code for order in state.pending_orders)
    signal_bars = (
        adapter.fetch_bars(sorted(active_codes), "1d", config.daily_lookback, dividend_type="front")
        if active_codes
        else {}
    )
    execution_bars = (
        adapter.fetch_bars(sorted(active_codes), "1d", config.daily_lookback, dividend_type="none")
        if active_codes
        else {}
    )
    execution_daily = (
        adapter.fetch_bars(sorted(active_codes), "1d", 5, dividend_type="none")
        if active_codes
        else {}
    )
    if active_codes and not signal_bars:
        warnings.warn(
            "No daily signal data is available for the active candidates.",
            RuntimeWarning,
            stacklevel=2,
        )

    broker.process_pending(execution_bars, _previous_closes(execution_daily), names)
    signals: list[Signal] = []
    top_sector_codes = {sector.code for sector in sectors}

    for code, position in list(state.positions.items()):
        frame = signal_bars.get(code)
        if frame is None or len(frame) < 10:
            continue
        raw_frame = execution_bars.get(code)
        latest_price = float(raw_frame["Close"].iloc[-1]) if raw_frame is not None else float(frame["Close"].iloc[-1])
        broker.mark(code, latest_price)
        chan = analyze_chan(frame, config.chan)
        if latest_price <= position.stop_price:
            signals.append(_latest_signal(code, frame, chan, "SELL", "固定止损", market, leader_by_code.get(code), latest_price))
        elif daily_trailing_exit(frame, position.entry_time, position.average_price, config.chan):
            signals.append(_latest_signal(code, frame, chan, "SELL", "日线追踪止盈", market, leader_by_code.get(code), latest_price))
        elif chan.breakdown:
            signals.append(_latest_signal(code, frame, chan, "SELL", "跌破日线中枢下沿", market, leader_by_code.get(code), latest_price))
        elif chan.bearish_divergence:
            signals.append(_latest_signal(code, frame, chan, "SELL", "日线顶背驰", market, leader_by_code.get(code), latest_price))
        elif position.sector_code not in top_sector_codes:
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
            if len(close) >= 20 and close.iloc[-1] < close.tail(20).mean():
                signals.append(_latest_signal(code, frame, chan, "SELL", "板块转弱且个股失守均线", market, leader_by_code.get(code), latest_price))

    pending_buys = {order.code for order in state.pending_orders if order.side == "BUY"}
    if market.regime == "NORMAL" and len(state.positions) < config.risk.max_positions:
        for leader in leaders:
            if leader.code in state.positions or leader.code in pending_buys:
                continue
            frame = signal_bars.get(leader.code)
            if frame is None or len(frame) < 20:
                continue
            chan = analyze_chan(frame, config.chan)
            if chan.breakout and daily_entry_allowed(frame, config.chan):
                raw_frame = execution_bars.get(leader.code)
                raw_price = float(raw_frame["Close"].iloc[-1]) if raw_frame is not None else float(frame["Close"].iloc[-1])
                signals.append(_latest_signal(leader.code, frame, chan, "BUY", "日线中枢上破", market, leader, raw_price))

    broker.queue(signals)
    latest_times = [pd.Timestamp(frame.index[-1]) for frame in signal_bars.values() if not frame.empty]
    asof = max(latest_times).isoformat() if latest_times else market.asof.isoformat()
    state.last_asof = asof
    append_signals(config, signals)
    append_trades(config, broker.trades)
    append_equity(config, asof, broker.equity, state.cash, len(state.positions))
    save_portfolio(config, state)
    return ScanResult(market, tuple(sectors), tuple(leaders), tuple(signals), broker.equity, len(state.positions))
