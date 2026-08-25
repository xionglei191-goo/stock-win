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
from strategy_v1.indicators import percentile_rank
from strategy_v1.market import evaluate_market_regime, filter_universe, rank_leaders, rank_sectors
from strategy_v1.models import LeaderCandidate, MarketState, PortfolioState, SectorScore, Signal
from strategy_v1.portfolio import PaperBroker
from strategy_v1.tdx_adapter import TdxAdapter


def _slice_bars(
    bars: dict[str, pd.DataFrame],
    asof: pd.Timestamp,
    lookback: int | None = None,
) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    cutoff = asof + pd.Timedelta(hours=23, minutes=59, seconds=59)
    for code, frame in bars.items():
        sliced = frame[frame.index <= cutoff]
        if lookback is not None:
            sliced = sliced.tail(lookback)
        if not sliced.empty and pd.Timestamp(sliced.index[-1]).normalize() == asof.normalize():
            result[code] = sliced
    return result


def _build_daily_schedule_reference(
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
        daily = filter_universe(
            _slice_bars(raw_daily, asof, config.daily_lookback),
            names,
            config,
        )
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


def build_daily_schedule(
    index_bars: pd.DataFrame,
    raw_daily: dict[str, pd.DataFrame],
    names: dict[str, str],
    sector_members: dict,
    config: StrategyConfig,
) -> tuple[dict[str, dict[str, LeaderCandidate]], dict[str, MarketState]]:
    dates = pd.DatetimeIndex(index_bars.index).normalize().unique().sort_values()
    if len(dates) <= config.minimum_listing_bars:
        return {}, {}
    features = _daily_point_in_time_features(raw_daily, names, config)
    if features.empty:
        return {}, {}
    sector_membership = _sector_membership_table(sector_members)
    by_day = {
        pd.Timestamp(day): rows.set_index("code")
        for day, rows in features.groupby("day", sort=False)
    }
    index_close = pd.to_numeric(index_bars.get("Close"), errors="coerce")
    index_frame = pd.DataFrame(
        {
            "close": index_close,
            "ma20": index_close.rolling(20, min_periods=20).mean(),
        },
        index=pd.DatetimeIndex(index_bars.index).normalize(),
    )
    index_frame = index_frame[~index_frame.index.duplicated(keep="last")]
    leader_schedule: dict[str, dict[str, LeaderCandidate]] = {}
    market_schedule: dict[str, MarketState] = {}
    for offset in range(config.minimum_listing_bars - 1, len(dates) - 1):
        asof = pd.Timestamp(dates[offset])
        next_date = pd.Timestamp(dates[offset + 1]).date().isoformat()
        daily = by_day.get(asof)
        index_row = index_frame.loc[asof] if asof in index_frame.index else None
        if daily is None or index_row is None or pd.isna(index_row["ma20"]):
            continue
        eligible = daily[daily["eligible"]]
        if eligible.empty:
            continue
        market_rows = eligible[eligible["market_valid"]]
        breadth = (
            float(market_rows["above_ma20"].mean())
            if not market_rows.empty
            else 0.0
        )
        returns = pd.to_numeric(
            eligible.loc[eligible["return_5d_valid"], "return_5d"],
            errors="coerce",
        ).dropna()
        average_return = float(returns.mean()) if not returns.empty else -1.0
        index_condition = bool(float(index_row["close"]) > float(index_row["ma20"]))
        passed = sum(
            (
                index_condition,
                breadth >= config.market_breadth_floor,
                average_return >= config.market_return_floor,
            )
        )
        market = MarketState(
            asof=asof.to_pydatetime(),
            regime="NORMAL" if passed >= 2 else "WEAK",
            index_above_ma20=index_condition,
            breadth=breadth,
            average_return_5d=average_return,
            passed_conditions=passed,
        )
        market_schedule[next_date] = market
        if market.regime == "WEAK":
            leader_schedule[next_date] = {}
            continue
        sectors = _rank_sectors_from_features(
            sector_membership,
            eligible,
            average_return,
            config,
        )
        leaders = _rank_leaders_from_features(
            sectors,
            sector_members,
            eligible,
            names,
            config,
        )
        leader_schedule[next_date] = {leader.code: leader for leader in leaders}
    return leader_schedule, market_schedule


def _daily_point_in_time_features(
    raw_daily: dict[str, pd.DataFrame],
    names: dict[str, str],
    config: StrategyConfig,
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for code, source in raw_daily.items():
        name = names.get(code, "")
        if "ST" in name.upper() or "退" in name:
            continue
        if "Close" not in source.columns or "Volume" not in source.columns:
            continue
        frame = source[~source.index.duplicated(keep="last")].sort_index()
        close = pd.to_numeric(frame["Close"], errors="coerce")
        volume = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0.0)
        valid_count = close.notna().cumsum()
        price_turnover = (close * volume).rolling(20, min_periods=1).mean()
        if "Amount" in frame.columns:
            amount = pd.to_numeric(frame["Amount"], errors="coerce")
            amount_scale = 1.0 if frame.attrs.get("amount_unit") == "CNY" else 10_000.0
            amount_turnover = amount.rolling(20, min_periods=1).mean() * amount_scale
            average_turnover = amount_turnover.where(
                amount.notna().cumsum() > 0,
                price_turnover,
            )
        else:
            average_turnover = price_turnover
        previous_volume = volume.shift(5).rolling(15, min_periods=1).mean()
        recent_volume = volume.rolling(5, min_periods=1).mean()
        previous_close_5 = close.shift(5)
        previous_close_20 = close.shift(20)
        values = pd.DataFrame(
            {
                "day": pd.DatetimeIndex(frame.index).normalize(),
                "code": code,
                "eligible": (
                    (valid_count >= config.minimum_listing_bars)
                    & (volume > 0)
                    & np.isfinite(average_turnover)
                    & (average_turnover >= config.minimum_average_turnover)
                    & close.notna()
                ),
                "market_valid": valid_count >= 21,
                "return_5d_valid": (valid_count >= 21) & (previous_close_5 > 0),
                "leader_valid": (
                    (valid_count >= 21)
                    & (previous_close_5 > 0)
                    & (previous_close_20 > 0)
                ),
                "above_ma20": close > close.rolling(20, min_periods=20).mean(),
                "return_5d": close / previous_close_5 - 1.0,
                "return_20d": close / previous_close_20 - 1.0,
                "volume_ratio": np.where(
                    previous_volume > 0,
                    recent_volume / previous_volume,
                    0.0,
                ),
                "turnover": price_turnover,
            },
            index=frame.index,
        )
        rows.append(values)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def _rank_sectors_from_features(
    sector_membership: pd.DataFrame,
    eligible: pd.DataFrame,
    market_return: float,
    config: StrategyConfig,
) -> list[SectorScore]:
    if sector_membership.empty:
        return []
    members = sector_membership.join(
        eligible[
            ["return_5d_valid", "return_5d", "above_ma20", "volume_ratio"]
        ],
        on="code",
        how="inner",
    )
    members = members[members["return_5d_valid"].fillna(False)].dropna(
        subset=["return_5d", "above_ma20", "volume_ratio"]
    )
    if members.empty:
        return []
    table = members.groupby("sector_code", sort=False).agg(
        name=("sector_name", "first"),
        return_5d=("return_5d", "mean"),
        breadth=("above_ma20", "mean"),
        volume_ratio=("volume_ratio", "mean"),
        valid_members=("code", "size"),
    )
    table = table[table["valid_members"] >= config.min_sector_members]
    if table.empty:
        return []
    table["relative_return_5d"] = table["return_5d"] - market_return
    table.index.name = "code"
    table["score"] = (
        percentile_rank(table["relative_return_5d"]) * 0.50
        + percentile_rank(table["breadth"]) * 0.30
        + percentile_rank(table["volume_ratio"]) * 0.20
    )
    table = table.sort_values(
        ["score", "relative_return_5d", "code"],
        ascending=[False, False, True],
    )
    return [
        SectorScore(
            code=str(code),
            name=str(row["name"]),
            score=float(row["score"]),
            relative_return_5d=float(row["relative_return_5d"]),
            breadth=float(row["breadth"]),
            volume_ratio=float(row["volume_ratio"]),
            valid_members=int(row["valid_members"]),
        )
        for code, row in table.head(config.top_sector_count).iterrows()
    ]


def _sector_membership_table(
    sector_members: dict[str, dict[str, object]],
) -> pd.DataFrame:
    rows = [
        {
            "sector_code": sector_code,
            "sector_name": str(metadata.get("name", sector_code)),
            "code": str(code),
        }
        for sector_code, metadata in sector_members.items()
        for code in metadata.get("members", [])
    ]
    return pd.DataFrame(rows, columns=["sector_code", "sector_name", "code"])


def _rank_leaders_from_features(
    sectors: list[SectorScore],
    sector_members: dict[str, dict[str, object]],
    eligible: pd.DataFrame,
    names: dict[str, str],
    config: StrategyConfig,
) -> list[LeaderCandidate]:
    best_by_code: dict[str, LeaderCandidate] = {}
    for sector in sectors:
        table = eligible.reindex(
            list(sector_members.get(sector.code, {}).get("members", []))
        )
        table = table[table["leader_valid"].fillna(False)].dropna(
            subset=["return_5d", "return_20d", "turnover"]
        )
        if table.empty:
            continue
        table = table.copy()
        table["leader_score"] = (
            percentile_rank(table["return_5d"]) * 0.40
            + percentile_rank(table["return_20d"]) * 0.30
            + percentile_rank(table["turnover"]) * 0.30
        )
        table = table.sort_values(
            ["leader_score", "return_5d", "code"],
            ascending=[False, False, True],
        )
        for rank, (code, row) in enumerate(
            table.head(config.leaders_per_sector).iterrows(),
            start=1,
        ):
            candidate = LeaderCandidate(
                code=str(code),
                name=names.get(str(code), str(code)),
                sector_code=sector.code,
                sector_name=sector.name,
                sector_score=sector.score,
                leader_score=float(row["leader_score"]),
                leader_rank=rank,
            )
            existing = best_by_code.get(candidate.code)
            if existing is None or (candidate.sector_score, candidate.leader_score) > (
                existing.sector_score,
                existing.leader_score,
            ):
                best_by_code[candidate.code] = candidate
    return sorted(
        best_by_code.values(),
        key=lambda candidate: (
            -candidate.sector_score,
            candidate.leader_rank,
            -candidate.leader_score,
            candidate.code,
        ),
    )


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
    leader_schedule: dict[str, dict[str, LeaderCandidate]] | None = None,
    market_schedule: dict[str, MarketState] | None = None,
) -> dict[str, float | int | str]:
    if leader_schedule is None or market_schedule is None:
        leader_schedule, market_schedule = build_daily_schedule(
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
        eligible = leader_schedule.get(date_key, {})
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
            visible_front = front_frame[front_frame.index <= timestamp].tail(
                config.daily_lookback
            )
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
                visible_front = front_frame[front_frame.index <= timestamp].tail(
                    config.daily_lookback
                )
                if len(visible_front) < 20:
                    continue
                chan = analyze_chan(visible_front, config.chan)
                if not daily_entry_allowed(visible_front, config.chan):
                    continue
                if chan.breakout_confirmed:
                    reason = "日线中枢上破(MACD确认)"
                else:
                    continue
                raw_price = float(raw_frame.loc[timestamp, "Close"])
                signal = _latest_signal(code, visible_front, chan, "BUY", reason, market, leader)
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
