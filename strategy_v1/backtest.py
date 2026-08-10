from __future__ import annotations

import argparse
import json
import sys
import warnings
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy_v1.chan import analyze_chan, daily_entry_allowed, daily_trailing_exit
from strategy_v1.config import StrategyConfig
from strategy_v1.engine import _latest_signal
from strategy_v1.market import evaluate_market_regime, filter_universe, rank_leaders, rank_sectors
from strategy_v1.models import LeaderCandidate, MarketState, PortfolioState, Signal
from strategy_v1.portfolio import PaperBroker
from strategy_v1.tdx_adapter import TdxAdapter


def _slice_bars(bars: dict[str, pd.DataFrame], asof: pd.Timestamp) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    cutoff = asof + pd.Timedelta(hours=23, minutes=59, seconds=59)
    for code, frame in bars.items():
        sliced = frame[frame.index <= cutoff]
        if not sliced.empty:
            result[code] = sliced
    return result


def build_daily_schedule(
    index_bars: pd.DataFrame,
    raw_daily: dict[str, pd.DataFrame],
    names: dict[str, str],
    sector_members: dict,
    config: StrategyConfig,
) -> tuple[dict[str, dict[str, LeaderCandidate]], dict[str, MarketState]]:
    dates = sorted(pd.DatetimeIndex(index_bars.index).normalize().unique())
    leader_schedule: dict[str, dict[str, LeaderCandidate]] = {}
    market_schedule: dict[str, MarketState] = {}
    for offset in range(config.minimum_listing_bars - 1, len(dates) - 1):
        asof = pd.Timestamp(dates[offset])
        next_date = pd.Timestamp(dates[offset + 1]).date().isoformat()
        daily = filter_universe(_slice_bars(raw_daily, asof), names, config)
        sliced_index = index_bars[index_bars.index <= asof + pd.Timedelta(hours=23, minutes=59)]
        if not daily or len(sliced_index) < 20:
            continue
        market = evaluate_market_regime(sliced_index, daily, config)
        market_schedule[next_date] = market
        if market.regime == "WEAK":
            leader_schedule[next_date] = {}
            continue
        sectors = rank_sectors(sector_members, daily, config)
        leaders = rank_leaders(sectors, sector_members, daily, names, config)
        leader_schedule[next_date] = {leader.code: leader for leader in leaders}
    return leader_schedule, market_schedule


def _previous_close(frame: pd.DataFrame | None, timestamp: pd.Timestamp) -> float:
    if frame is None or "Close" not in frame.columns:
        return 0.0
    prior = frame[frame.index.normalize() < timestamp.normalize()]
    close = pd.to_numeric(prior["Close"], errors="coerce").dropna()
    return float(close.iloc[-1]) if not close.empty else 0.0


def run_backtest(
    config: StrategyConfig,
    names: dict[str, str],
    daily_front: dict[str, pd.DataFrame],
    daily_raw: dict[str, pd.DataFrame],
    index_bars: pd.DataFrame,
    sector_members: dict,
    signal_front: dict[str, pd.DataFrame],
    signal_raw: dict[str, pd.DataFrame],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, float | int | str]:
    schedule, market_schedule = build_daily_schedule(
        index_bars, daily_front, names, sector_members, config
    )
    timestamps = sorted(
        set().union(*(set(frame.index) for frame in signal_front.values()))
        if signal_front
        else set()
    )
    if start_date:
        start = pd.Timestamp(start_date).normalize()
        timestamps = [timestamp for timestamp in timestamps if pd.Timestamp(timestamp).normalize() >= start]
    if end_date:
        end = pd.Timestamp(end_date).normalize()
        timestamps = [timestamp for timestamp in timestamps if pd.Timestamp(timestamp).normalize() <= end]
    broker = PaperBroker(config, PortfolioState(cash=config.risk.initial_cash))
    signals: list[Signal] = []
    equity_rows: list[dict[str, float | str | int]] = []

    for raw_timestamp in timestamps:
        timestamp = pd.Timestamp(raw_timestamp)
        date_key = timestamp.date().isoformat()
        eligible = schedule.get(date_key, {})
        market = market_schedule.get(date_key)
        if market is None:
            continue

        visible_raw = {
            code: frame[frame.index <= timestamp]
            for code, frame in signal_raw.items()
            if timestamp in frame.index
        }
        previous_closes = {
            code: _previous_close(daily_raw.get(code), timestamp)
            for code in set(visible_raw) | {order.code for order in broker.state.pending_orders}
        }
        broker.process_pending(visible_raw, previous_closes, names)

        step_signals: list[Signal] = []
        for code, position in list(broker.state.positions.items()):
            raw_frame = signal_raw.get(code)
            front_frame = signal_front.get(code)
            if raw_frame is None or front_frame is None or timestamp not in raw_frame.index:
                continue
            visible_front = front_frame[front_frame.index <= timestamp]
            if len(visible_front) < 20:
                continue
            raw_price = float(raw_frame.loc[timestamp, "Close"])
            broker.mark(code, raw_price)
            chan = analyze_chan(visible_front, config.chan)
            leader = eligible.get(code)
            if raw_price <= position.stop_price:
                signal = _latest_signal(code, visible_front, chan, "SELL", "固定止损", market, leader)
                step_signals.append(replace_signal_price(signal, raw_price))
            elif daily_trailing_exit(
                visible_front,
                position.entry_time,
                position.average_price,
                config.chan,
            ):
                signal = _latest_signal(code, visible_front, chan, "SELL", "日线追踪止盈", market, leader)
                step_signals.append(replace_signal_price(signal, raw_price))
            elif chan.breakdown:
                signal = _latest_signal(code, visible_front, chan, "SELL", "跌破日线中枢下沿", market, leader)
                step_signals.append(replace_signal_price(signal, raw_price))
            elif chan.bearish_divergence:
                signal = _latest_signal(code, visible_front, chan, "SELL", "日线顶背驰", market, leader)
                step_signals.append(replace_signal_price(signal, raw_price))
            elif code not in eligible:
                close = pd.to_numeric(visible_front["Close"], errors="coerce").dropna()
                if len(close) >= 20 and close.iloc[-1] < close.tail(20).mean():
                    signal = _latest_signal(code, visible_front, chan, "SELL", "板块转弱且个股失守均线", market, leader)
                    step_signals.append(replace_signal_price(signal, raw_price))

        pending_buys = {order.code for order in broker.state.pending_orders if order.side == "BUY"}
        if len(broker.state.positions) < config.risk.max_positions:
            for code, leader in eligible.items():
                if code in broker.state.positions or code in pending_buys:
                    continue
                front_frame = signal_front.get(code)
                raw_frame = signal_raw.get(code)
                if front_frame is None or raw_frame is None or timestamp not in front_frame.index:
                    continue
                visible_front = front_frame[front_frame.index <= timestamp]
                if len(visible_front) < 20:
                    continue
                chan = analyze_chan(visible_front, config.chan)
                if chan.breakout and daily_entry_allowed(visible_front, config.chan):
                    raw_price = float(raw_frame.loc[timestamp, "Close"])
                    signal = _latest_signal(code, visible_front, chan, "BUY", "日线中枢上破", market, leader)
                    step_signals.append(replace_signal_price(signal, raw_price))

        broker.queue(step_signals)
        signals.extend(step_signals)
        equity_rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "equity": broker.equity,
                "cash": broker.state.cash,
                "positions": len(broker.state.positions),
            }
        )

    config.ensure_runtime_dirs()
    pd.DataFrame([signal.as_record() for signal in signals]).to_csv(
        config.output_dir / "backtest_signals.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(broker.trades).to_csv(
        config.output_dir / "backtest_trades.csv", index=False, encoding="utf-8-sig"
    )
    equity = pd.DataFrame(equity_rows)
    equity.to_csv(config.output_dir / "backtest_equity.csv", index=False, encoding="utf-8-sig")

    if equity.empty:
        metrics: dict[str, float | int | str] = {
            "data_status": "no_daily_signal_data" if not signal_front else "ok",
            "initial_cash": config.risk.initial_cash,
            "final_equity": config.risk.initial_cash,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "trades": 0,
            "win_rate": 0.0,
        }
    else:
        curve = pd.to_numeric(equity["equity"], errors="coerce").ffill()
        drawdown = curve / curve.cummax() - 1.0
        sells = [trade for trade in broker.trades if trade["side"] == "SELL"]
        wins = [trade for trade in sells if float(trade.get("pnl", 0.0)) > 0]
        metrics = {
            "data_status": "ok",
            "initial_cash": config.risk.initial_cash,
            "final_equity": float(curve.iloc[-1]),
            "total_return": float(curve.iloc[-1] / config.risk.initial_cash - 1.0),
            "max_drawdown": float(drawdown.min()),
            "trades": len(broker.trades),
            "win_rate": len(wins) / len(sells) if sells else 0.0,
        }
    (config.output_dir / "backtest_summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


def replace_signal_price(signal: Signal, price: float) -> Signal:
    return Signal(
        timestamp=signal.timestamp,
        code=signal.code,
        side=signal.side,
        price=price,
        reason=signal.reason,
        market_regime=signal.market_regime,
        sector_code=signal.sector_code,
        sector_name=signal.sector_name,
        leader_rank=signal.leader_rank,
        center_lower=signal.center_lower,
        center_upper=signal.center_upper,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest V1 with rolling, no-lookahead selection")
    parser.add_argument("--daily-bars", type=int, default=120)
    parser.add_argument("--max-stocks", type=int, default=None, help="Diagnostic limit; omitted means all A shares")
    parser.add_argument("--refresh-sectors", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = replace(StrategyConfig(), daily_lookback=max(args.daily_bars, 70))
    with TdxAdapter(config, __file__) as adapter:
        codes, names = adapter.list_a_shares()
        if args.max_stocks is not None:
            codes = codes[: args.max_stocks]
        daily_front = adapter.fetch_bars(codes, "1d", config.daily_lookback, dividend_type="front")
        daily_raw = adapter.fetch_bars(codes, "1d", config.daily_lookback, dividend_type="none")
        index_map = adapter.fetch_bars(["999999.SH"], "1d", config.daily_lookback)
        index_bars = index_map.get("999999.SH")
        if index_bars is None:
            raise RuntimeError("Shanghai index data is unavailable")
        sector_members = adapter.load_sector_members(refresh=args.refresh_sectors)
        schedule, _ = build_daily_schedule(index_bars, daily_front, names, sector_members, config)
        candidate_codes = sorted(set().union(*(set(value) for value in schedule.values())) if schedule else set())
        signal_front = {code: daily_front[code] for code in candidate_codes if code in daily_front}
        signal_raw = {code: daily_raw[code] for code in candidate_codes if code in daily_raw}
        if candidate_codes and not signal_front:
            warnings.warn(
                "Backtest has no daily signal data for the selected candidates.",
                RuntimeWarning,
                stacklevel=2,
            )
        metrics = run_backtest(
            config,
            names,
            daily_front,
            daily_raw,
            index_bars,
            sector_members,
            signal_front,
            signal_raw,
        )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
