"""Strict point-in-time backtest for :mod:`us_momentum`.

The legacy implementation in this module used today's surviving securities,
formed signals at a month-end close and filled them at that same close.  That
path has deliberately been replaced.  This implementation requires separate
adjusted signal bars, raw execution bars and an explicitly delisting-aware
point-in-time equity universe before it will run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from research_platform.config import USPortfolioConfig
from research_platform.models import PlatformSignal, SignalStatus
from research_platform.us_market_time import ny_session_date, ny_session_dates

from .us_momentum import (
    USMomentumParameters,
    USMomentumStrategy,
    _signal_times,
)


@dataclass(frozen=True)
class StrictUSPointInTimeUniverse:
    """Test-only certified universe used by focused engine fixtures.

    Production callers must pass a ``USBacktestDataset`` built from an immutable
    DATA_READY release.  This type exists so the execution rules can be unit
    tested without writing a release to disk; it is accepted only when
    ``allow_test_fixture=True``.  Delisting coverage is derived from the quality
    report and is no longer a caller-controlled boolean.
    """

    memberships: Mapping[Any, Iterable[str]]
    source: str
    listing_aliases: pd.DataFrame = field(default_factory=pd.DataFrame)
    trading_calendar: Iterable[Any] = field(default_factory=tuple)
    quality_report: Mapping[str, Any] = field(default_factory=dict)
    release_id: str = "test-fixture"
    universe_id: str = "sp500_ivv_proxy_v1"
    corporate_actions: pd.DataFrame = field(default_factory=pd.DataFrame)
    fee_schedule: pd.DataFrame = field(default_factory=pd.DataFrame)
    session_exceptions: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def includes_delisted(self) -> bool:
        return (
            str(self.quality_report.get("status", "")) == "DATA_READY"
            and bool(self.quality_report.get("includes_delisted", False))
        )

    def members_on(self, value: Any) -> frozenset[str]:
        day = _day(value)
        normalized = {
            _day(effective): frozenset(_normalize_code(code) for code in members)
            for effective, members in self.memberships.items()
        }
        eligible = [effective for effective in normalized if effective <= day]
        return normalized[max(eligible)] if eligible else frozenset()


@dataclass
class _Position:
    security_id: str
    code: str
    quantity: float
    average_price: float
    entry_date: str
    stop_price: float
    last_price: float
    entry_fees: float
    forced_exit_reason: str = ""


@dataclass(frozen=True)
class _Order:
    security_id: str
    signal: PlatformSignal


@dataclass(frozen=True)
class _FeeTerms:
    commission_rate: float
    min_commission: float
    slippage_rate: float
    sec_sell_fee_rate: float
    finra_taf_per_share: float
    finra_taf_cap: float


@dataclass
class _StrictDataView:
    release_id: str
    universe_id: str
    source: str
    memberships: Mapping[Any, Iterable[str]]
    calendar: pd.DatetimeIndex
    raw: dict[str, pd.DataFrame]
    benchmark_signal: dict[str, pd.DataFrame]
    benchmark_raw: dict[str, pd.DataFrame]
    listing_aliases: pd.DataFrame
    corporate_actions: pd.DataFrame
    fee_schedule: pd.DataFrame
    session_exceptions: pd.DataFrame
    static_signal: dict[str, pd.DataFrame] | None = None
    dataset: Any = None

    def members_on(self, value: Any) -> frozenset[str]:
        day = _day(value)
        eligible = [_day(item) for item in self.memberships if _day(item) <= day]
        if not eligible:
            return frozenset()
        selected = max(eligible)
        for effective, members in self.memberships.items():
            if _day(effective) == selected:
                return frozenset(_normalize_security_id(item) for item in members)
        return frozenset()

    def signal_bars(self, value: Any) -> dict[str, pd.DataFrame]:
        if self.dataset is not None:
            values = self.dataset.signal_bars(_day(value))
            return _normalize_security_bar_map(values, raw=False)
        return dict(self.static_signal or {})

    def vendor_code(self, security_id: str, value: Any) -> str:
        if self.dataset is not None:
            return _normalize_code(self.dataset.vendor_code(security_id, _day(value)))
        return _alias_on(self.listing_aliases, security_id, _day(value))


def run_backtest(
    bars: dict[str, pd.DataFrame] | None = None,
    names: dict[str, str] | None = None,
    initial_capital: float = 100_000.0,
    params: USMomentumParameters | None = None,
    *,
    dataset: Any | None = None,
    raw_bars: dict[str, pd.DataFrame] | None = None,
    point_in_time_universe: StrictUSPointInTimeUniverse | None = None,
    cost_config: USPortfolioConfig | None = None,
    allow_test_fixture: bool = False,
    start_date: Any | None = None,
    end_date: Any | None = None,
    commission_multiplier: float = 1.0,
    slippage_multiplier: float = 1.0,
) -> dict[str, Any]:
    """Run a strict daily US momentum simulation.

    Signals use ``bars`` (front-adjusted) through the month-end close.  Orders
    execute once, at the next frozen XNYS session's raw open.  ``start_date``
    and ``end_date`` crop execution and reporting only; month ends are always
    determined from the complete release calendar, so a mid-month end cannot
    become a synthetic rebalance.  Existing and
    newly purchased positions are checked against raw Open/Low on every session.
    Missing execution data for a held or pending security aborts the study
    instead of silently carrying a stale mark.
    """

    if not np.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if not np.isfinite(commission_multiplier) or commission_multiplier <= 0:
        raise ValueError("commission_multiplier must be positive")
    if not np.isfinite(slippage_multiplier) or slippage_multiplier <= 0:
        raise ValueError("slippage_multiplier must be positive")

    parameters = params or USMomentumParameters()
    costs = cost_config or USPortfolioConfig()
    data = _build_data_view(
        dataset=dataset,
        bars=bars,
        raw_bars=raw_bars,
        universe=point_in_time_universe,
        allow_test_fixture=allow_test_fixture,
    )
    names = names or {}
    benchmark = data.benchmark_signal.get(parameters.market_code)
    raw_benchmark = data.benchmark_raw.get(parameters.market_code)
    if benchmark is None or benchmark.empty:
        raise ValueError(
            f"Strict US momentum backtest requires {parameters.market_code} signal bars"
        )
    if raw_benchmark is None or raw_benchmark.empty:
        raise ValueError(
            f"Strict US momentum backtest requires {parameters.market_code} raw bars"
        )

    first_effective = min(_day(value) for value in data.memberships)
    final_bar = min(pd.Timestamp(benchmark.index.max()), pd.Timestamp(raw_benchmark.index.max()))
    full_sessions = [
        session
        for session in data.calendar
        if first_effective <= session <= final_bar
    ]
    if len(full_sessions) < 2:
        raise ValueError("Point-in-time universe and SPY bars have no usable overlap")
    requested_start = _day(start_date) if start_date is not None else full_sessions[0]
    requested_end = _day(end_date) if end_date is not None else full_sessions[-1]
    if requested_start > requested_end:
        raise ValueError("start_date cannot be after end_date")
    sessions = [
        session
        for session in full_sessions
        if requested_start <= session <= requested_end
    ]
    if not sessions:
        raise ValueError("Requested backtest window has no frozen XNYS sessions")
    _validate_benchmark_calendar(benchmark, raw_benchmark, sessions, parameters.market_code)
    calendar_next = {
        current: following
        for current, following in zip(data.calendar, data.calendar[1:])
    }
    month_ends = {
        current
        for current, following in calendar_next.items()
        if current.to_period("M") != following.to_period("M")
    }
    if not month_ends:
        raise ValueError("Backtest data contains no completed month with a next session")

    strategy = USMomentumStrategy(parameters)
    cash = float(initial_capital)
    positions: dict[str, _Position] = {}
    pending: dict[pd.Timestamp, list[_Order]] = {}
    receivables: list[tuple[pd.Timestamp, float, str]] = []
    runtime_state: dict[str, Any] = {}
    trades: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    rebalances = 0

    for session in sessions:
        cash = _settle_receivables(session, receivables, cash, trades)
        cash = _apply_corporate_actions(
            session,
            data,
            positions,
            receivables,
            cash,
            trades,
        )
        for security_id, position in positions.items():
            position.code = data.vendor_code(security_id, session)
        day_orders = pending.pop(session, [])
        cash = _execute_session(
            session,
            day_orders,
            data,
            positions,
            cash,
            costs,
            trades,
            strategy.metadata.version,
            commission_multiplier=commission_multiplier,
            slippage_multiplier=slippage_multiplier,
        )

        # A held security must have a raw daily mark on every benchmark session.
        # A missing row can hide a halt, delisting loss or stop breach, so a
        # strict study cannot substitute the previous close.
        for security_id, position in positions.items():
            position.code = data.vendor_code(security_id, session)
            row = _row_on(data.raw.get(security_id), session)
            if row is None:
                if _has_session_exception(data, security_id, session):
                    continue
                raise ValueError(
                    "Missing raw execution/mark row for held "
                    f"{security_id} on {session.date()}"
                )
            position.last_price = _positive(
                row.get("Close"), position.code, session, "Close"
            )

        close_equity = _equity(cash, positions)
        if session in month_ends:
            members = data.members_on(session)
            _validate_members(members, session)
            decision_signal = data.signal_bars(session)
            _validate_month_end_coverage(members, session, decision_signal, data.raw)

            member_codes = {
                security_id: data.vendor_code(security_id, session)
                for security_id in members
            }
            if len(set(member_codes.values())) != len(member_codes):
                raise ValueError(f"Ambiguous active ticker alias on {session.date()}")
            code_to_security = {code: security_id for security_id, code in member_codes.items()}
            needed = set(members) | set(positions)
            visible_front = {
                member_codes[security_id]: _slice_to(frame, session)
                for security_id, frame in decision_signal.items()
                if security_id in members
            }
            visible_front[parameters.market_code] = _slice_to(benchmark, session)
            visible_raw = {
                data.vendor_code(security_id, session): _slice_to(frame, session)
                for security_id, frame in data.raw.items()
                if security_id in needed
            }
            position_rows = [
                {
                    "code": position.code,
                    "weight": (
                        position.quantity * position.last_price / close_equity
                        if close_equity > 0
                        else 0.0
                    ),
                    "average_price": position.average_price,
                    "stop_price": position.stop_price,
                }
                for position in positions.values()
            ]
            result = strategy.scan(
                run_id=f"strict_us_{session.date()}",
                asof=session,
                front_bars=visible_front,
                raw_bars=visible_raw,
                names={
                    code: names.get(security_id, names.get(code, code))
                    for security_id, code in member_codes.items()
                },
                positions=position_rows,
                runtime_state=runtime_state,
                backtest_mode=True,
                is_rebalance_day=True,
                tradable_codes=set(member_codes.values()),
            )
            runtime_state = dict(result.state.get("runtime_state") or runtime_state)

            generated = list(result.signals)
            signaled_sells = {
                code_to_security.get(signal.code)
                or _security_for_active_code(data, signal.code, positions, session)
                for signal in generated
                if signal.side == "SELL"
            }
            for security_id in sorted(set(positions) - set(members) - signaled_sells):
                code = data.vendor_code(security_id, session)
                generated.append(
                    _membership_exit_signal(
                        code,
                        session,
                        strategy.metadata.version,
                    )
                )

            execution_day = calendar_next[session]
            orders: list[_Order] = []
            for signal in generated:
                security_id = code_to_security.get(signal.code)
                if security_id is None:
                    security_id = _security_for_active_code(
                        data,
                        signal.code,
                        positions,
                        session,
                    )
                if security_id is None:
                    raise ValueError(
                        f"Signal code has no stable security_id on {session.date()}: "
                        f"{signal.code}"
                    )
                stable_signal = replace(
                    signal,
                    signal_id=_strict_signal_id(
                        data.release_id,
                        session,
                        execution_day,
                        security_id,
                        signal,
                    ),
                )
                orders.append(_Order(security_id=security_id, signal=stable_signal))
            pending[execution_day] = sorted(
                orders,
                key=lambda order: (
                    order.signal.side != "SELL",
                    -float(order.signal.strength),
                    order.security_id,
                ),
            )
            rebalances += 1

        equity_rows.append(
            {
                "timestamp": session,
                "equity": _equity(cash, positions),
                "cash": cash,
                "positions": len(positions),
            }
        )

    equity = pd.DataFrame(equity_rows)
    trade_frame = pd.DataFrame(trades)
    execution_frame = (
        trade_frame.loc[trade_frame["side"].isin(["BUY", "SELL"])]
        if not trade_frame.empty and "side" in trade_frame
        else pd.DataFrame()
    )
    metrics = _metrics(equity, execution_frame, initial_capital)
    closed = execution_frame[
        execution_frame.get("side", pd.Series(dtype=str)).astype(str).eq("SELL")
    ] if not execution_frame.empty else pd.DataFrame()
    trade_returns = (
        pd.to_numeric(closed.get("return"), errors="coerce").dropna()
        if not closed.empty
        else pd.Series(dtype=float)
    )
    curve = {
        pd.Timestamp(row["timestamp"]).date().isoformat(): round(float(row["equity"]), 2)
        for row in equity_rows
    }
    return {
        "period": (
            f"{sessions[0].date()} -> {sessions[-1].date()}" if sessions else ""
        ),
        "rebalances": rebalances,
        "total_return": round(float(metrics["total_return"]), 6),
        "annual_return": round(float(metrics["annualized_return"]), 6),
        "sharpe_ratio": round(float(metrics["sharpe_ratio"]), 6),
        "sharpe_monthly": round(float(metrics["sharpe_ratio"]), 6),
        "max_drawdown": round(float(metrics["max_drawdown"]), 6),
        "n_trades": int(len(execution_frame)),
        "win_rate": round(float(metrics["win_rate"]), 6),
        "avg_trade_ret": (
            round(float(trade_returns.mean()), 6) if not trade_returns.empty else 0.0
        ),
        "equity_curve": curve,
        "equity_rows": equity_rows,
        "trades": trades,
        "open_positions": [
            {
                "security_id": position.security_id,
                "code": position.code,
                "quantity": position.quantity,
                "average_price": position.average_price,
                "stop_price": position.stop_price,
                "last_price": position.last_price,
            }
            for position in sorted(positions.values(), key=lambda item: item.code)
        ],
        "data_contract": {
            "release_id": data.release_id,
            "universe_id": data.universe_id,
            "point_in_time_source": data.source,
            "includes_delisted": True,
            "stable_position_key": "security_id",
            "calendar": "frozen_xnys_release",
            "signal_adjustment": "pit_front_by_decision",
            "execution_adjustment": "none",
            "execution_timing": "next_xnys_session_open",
            "stop_frequency": "daily_open_and_low",
            "corporate_actions": "effective_dated_before_execution",
            "fees": "effective_dated",
            "fee_multipliers": {
                "commission": float(commission_multiplier),
                "slippage": float(slippage_multiplier),
            },
        },
        "metrics": metrics,
    }


def _execute_session(
    session: pd.Timestamp,
    orders: list[_Order],
    data: _StrictDataView,
    positions: dict[str, _Position],
    cash: float,
    costs: USPortfolioConfig,
    trades: list[dict[str, Any]],
    strategy_version: str,
    *,
    commission_multiplier: float,
    slippage_multiplier: float,
) -> float:
    fees = _fees_on(
        data.fee_schedule,
        session,
        costs,
        commission_multiplier=commission_multiplier,
        slippage_multiplier=slippage_multiplier,
    )
    # Opening gaps below a stop are observable before discretionary open orders.
    for security_id, position in list(positions.items()):
        if _has_session_exception(data, security_id, session):
            continue
        row = _require_execution_row(data.raw, security_id, session)
        open_price = _positive(row.get("Open"), position.code, session, "Open")
        if open_price <= position.stop_price:
            cash += _close_position(
                session,
                position,
                open_price * (1 - fees.slippage_rate),
                "US_FIXED_STOP_GAP",
                fees,
                trades,
            )
            del positions[security_id]

    # A spinoff or out-of-universe stock-merger child is liquidated at its first
    # executable raw open.  A documented halt defers this risk exit; an
    # unexplained missing row remains fatal.
    for security_id, position in list(positions.items()):
        if not position.forced_exit_reason:
            continue
        if _has_session_exception(data, security_id, session):
            continue
        row = _require_execution_row(data.raw, security_id, session)
        open_price = _positive(row.get("Open"), position.code, session, "Open")
        cash += _close_position(
            session,
            position,
            open_price * (1 - fees.slippage_rate),
            position.forced_exit_reason,
            fees,
            trades,
        )
        del positions[security_id]

    for order in orders:
        signal = order.signal
        if signal.side == "SELL":
            position = positions.get(order.security_id)
            if position is None:
                continue
            row = _require_execution_row(data.raw, order.security_id, session)
            open_price = _positive(row.get("Open"), position.code, session, "Open")
            cash += _close_position(
                session,
                position,
                open_price * (1 - fees.slippage_rate),
                signal.reason_codes[0] if signal.reason_codes else "REBALANCE",
                fees,
                trades,
                signal=signal,
            )
            del positions[order.security_id]

    for order in orders:
        signal = order.signal
        if signal.side != "BUY" or order.security_id in positions:
            continue
        if len(positions) >= min(
            costs.max_strategy_positions,
            costs.max_total_positions,
        ):
            continue
        row = _require_execution_row(data.raw, order.security_id, session)
        open_price = _positive(row.get("Open"), signal.code, session, "Open")
        execution = open_price * (1 + fees.slippage_rate)
        portfolio_value = _equity(cash, positions)
        target_weight = min(
            max(0.0, float(signal.target_weight)),
            costs.max_strategy_symbol_weight,
            costs.max_total_symbol_weight,
        )
        budget = min(cash, portfolio_value * target_weight)
        quantity = int(budget / execution / costs.board_lot) * costs.board_lot
        while quantity > 0:
            value = quantity * execution
            commission = max(fees.min_commission, value * fees.commission_rate)
            if value + commission <= cash:
                break
            quantity -= costs.board_lot
        if quantity <= 0:
            continue
        value = quantity * execution
        commission = max(fees.min_commission, value * fees.commission_rate)
        cash -= value + commission
        stop_ratio = float(
            signal.evidence.get("stop_ratio", costs.fixed_stop_loss)
        )
        positions[order.security_id] = _Position(
            security_id=order.security_id,
            code=signal.code,
            quantity=quantity,
            average_price=execution,
            entry_date=session.date().isoformat(),
            stop_price=execution * (1 - stop_ratio),
            last_price=execution,
            entry_fees=commission,
        )
        trades.append(
            {
                "signal_id": signal.signal_id,
                "security_id": order.security_id,
                "code": signal.code,
                "side": "BUY",
                "timestamp": session.date().isoformat(),
                "quantity": quantity,
                "price": round(execution, 6),
                "fees": round(commission, 6),
                "pnl": None,
                "return": None,
                "reason": signal.reason_codes[0] if signal.reason_codes else "ENTRY",
                "evidence": dict(signal.evidence),
            }
        )

    # Stops are evaluated after all opening orders, including for new buys.
    for security_id, position in list(positions.items()):
        if _has_session_exception(data, security_id, session):
            continue
        row = _require_execution_row(data.raw, security_id, session)
        open_price = _positive(row.get("Open"), position.code, session, "Open")
        low_price = _positive(row.get("Low"), position.code, session, "Low")
        if open_price > position.stop_price and low_price <= position.stop_price:
            cash += _close_position(
                session,
                position,
                position.stop_price * (1 - fees.slippage_rate),
                "US_FIXED_STOP",
                fees,
                trades,
            )
            del positions[security_id]
    return cash


def _close_position(
    session: pd.Timestamp,
    position: _Position,
    execution: float,
    reason: str,
    costs: _FeeTerms,
    trades: list[dict[str, Any]],
    *,
    signal: PlatformSignal | None = None,
) -> float:
    value = position.quantity * execution
    fees = _sell_fees(value, position.quantity, costs)
    pnl = (
        (execution - position.average_price) * position.quantity
        - position.entry_fees
        - fees
    )
    basis = position.average_price * position.quantity + position.entry_fees
    trades.append(
        {
            "signal_id": signal.signal_id if signal is not None else "",
            "security_id": position.security_id,
            "code": position.code,
            "side": "SELL",
            "timestamp": session.date().isoformat(),
            "quantity": position.quantity,
            "price": round(execution, 6),
            "fees": round(fees, 6),
            "pnl": round(pnl, 6),
            "return": round(pnl / basis, 8) if basis > 0 else None,
            "reason": reason,
            "evidence": dict(signal.evidence) if signal is not None else {},
        }
    )
    return value - fees


def _membership_exit_signal(
    code: str,
    session: pd.Timestamp,
    version: str,
) -> PlatformSignal:
    generated_at, available_at, valid_until = _signal_times(session)
    return PlatformSignal(
        run_id=f"strict_us_{session.date()}",
        strategy_id=USMomentumStrategy.metadata.strategy_id,
        strategy_version=version,
        generated_at=generated_at,
        available_at=available_at,
        code=code,
        side="SELL",
        strength=1.0,
        target_weight=0.0,
        horizon="monthly",
        valid_until=valid_until,
        stop_price=None,
        status=SignalStatus.PROPOSED,
        reason_codes=("US_PIT_MEMBERSHIP_EXIT",),
        evidence={"asof": session.date().isoformat()},
    )


def _strict_signal_id(
    release_id: str,
    decision_day: pd.Timestamp,
    execution_day: pd.Timestamp,
    security_id: str,
    signal: PlatformSignal,
) -> str:
    payload = {
        "release_id": release_id,
        "strategy_id": signal.strategy_id,
        "strategy_version": signal.strategy_version,
        "decision_day": decision_day.date().isoformat(),
        "execution_day": execution_day.date().isoformat(),
        "security_id": security_id,
        "side": signal.side,
        "target_weight": float(signal.target_weight),
        "reason_codes": list(signal.reason_codes),
        "evidence": signal.evidence,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_security_bar_map(
    values: Mapping[str, pd.DataFrame],
    *,
    raw: bool,
) -> dict[str, pd.DataFrame]:
    output: dict[str, pd.DataFrame] = {}
    required = {"Open", "Low", "Close"} if raw else {"Close", "Volume"}
    for key_value, source in values.items():
        raw_key = str(key_value).strip()
        key = (
            _normalize_code(raw_key)
            if raw_key.upper().endswith(".US")
            else _normalize_security_id(raw_key)
        )
        if not isinstance(source, pd.DataFrame) or source.empty:
            continue
        missing = required - set(source.columns)
        # SPY is a non-tradable regime input and only requires Close.
        if not raw and key in {"SPY.US", "BIL.US"}:
            missing = {"Close"} - set(source.columns)
        if missing:
            raise ValueError(
                f"{key} is missing required {'raw' if raw else 'signal'} columns: "
                + ", ".join(sorted(missing))
            )
        frame = source.copy()
        index = pd.DatetimeIndex(pd.to_datetime(frame.index))
        if index.tz is not None:
            index = index.tz_convert("America/New_York").tz_localize(None)
        frame.index = index.normalize()
        frame = frame.sort_index(kind="stable")
        if frame.index.duplicated().any():
            raise ValueError(f"Duplicate bar date for {key}")
        output[key] = frame
    return output


def _build_data_view(
    *,
    dataset: Any | None,
    bars: dict[str, pd.DataFrame] | None,
    raw_bars: dict[str, pd.DataFrame] | None,
    universe: StrictUSPointInTimeUniverse | None,
    allow_test_fixture: bool,
) -> _StrictDataView:
    if dataset is not None and universe is not None:
        raise ValueError("Pass dataset or point_in_time_universe, not both")
    if dataset is not None:
        quality = getattr(dataset, "quality_report", None)
        quality_status = str(getattr(quality, "status", ""))
        hard_failures = tuple(getattr(quality, "hard_failures", ()))
        if quality_status != "DATA_READY" or hard_failures:
            raise ValueError("USBacktestDataset quality is not DATA_READY")
        if not bool(getattr(dataset, "includes_delisted", False)):
            raise ValueError("PIT release quality did not derive includes_delisted=True")
        release_id = str(getattr(dataset, "release_id", ""))
        universe_id = str(getattr(dataset, "universe_id", ""))
        if len(release_id) != 64 or any(
            character not in "0123456789abcdef" for character in release_id
        ):
            raise ValueError("USBacktestDataset requires a verified release_id")
        if universe_id != "sp500_ivv_proxy_v1":
            raise ValueError("USBacktestDataset must use sp500_ivv_proxy_v1")
        memberships = getattr(dataset, "membership_by_date", None)
        if not isinstance(memberships, Mapping) or not memberships:
            raise ValueError("USBacktestDataset has no PIT membership history")
        calendar = _calendar_index(getattr(dataset, "calendar", None))
        raw = _normalize_security_bar_map(getattr(dataset, "raw_bars", {}), raw=True)
        benchmark_values = _benchmark_bar_maps(dataset)
        signal_benchmark = _normalize_security_bar_map(benchmark_values[0], raw=False)
        raw_benchmark = _normalize_security_bar_map(benchmark_values[1], raw=True)
        fee_schedule = _frame_or_empty(getattr(dataset, "fee_schedule", None))
        if fee_schedule.empty:
            raise ValueError("DATA_READY release has no effective fee schedule")
        return _StrictDataView(
            release_id=release_id,
            universe_id=universe_id,
            source=f"us-pit-release:{release_id}",
            memberships=memberships,
            calendar=calendar,
            raw={key: value for key, value in raw.items() if not key.endswith(".US")},
            benchmark_signal=signal_benchmark,
            benchmark_raw=raw_benchmark,
            listing_aliases=_frame_or_empty(getattr(dataset, "listing_aliases", None)),
            corporate_actions=_frame_or_empty(getattr(dataset, "corporate_actions", None)),
            fee_schedule=fee_schedule,
            session_exceptions=_frame_or_empty(
                getattr(dataset, "session_exceptions", None)
            ),
            dataset=dataset,
        )

    if not allow_test_fixture:
        if raw_bars is None:
            raise ValueError(
                "Strict US momentum backtest requires a DATA_READY USBacktestDataset; "
                "raw_bars are not a production release"
            )
        raise ValueError(
            "Strict US momentum backtest requires a DATA_READY USBacktestDataset; "
            "point_in_time_universe is test-only"
        )
    if universe is None:
        raise ValueError("Test fixture requires point_in_time_universe")
    if bars is None or raw_bars is None:
        raise ValueError("Test fixture requires separate signal and raw_bars")
    if not universe.includes_delisted:
        raise ValueError(
            "Test fixture quality_report must derive includes_delisted=True"
        )
    if not str(universe.source).startswith("test://"):
        raise ValueError("Test-only PIT fixtures must use a test:// source")
    if universe.universe_id != "sp500_ivv_proxy_v1":
        raise ValueError("Test fixture must use sp500_ivv_proxy_v1")
    if not universe.memberships:
        raise ValueError("Test fixture has no effective-dated memberships")
    front = _normalize_security_bar_map(bars, raw=False)
    raw_all = _normalize_security_bar_map(raw_bars, raw=True)
    return _StrictDataView(
        release_id=universe.release_id,
        universe_id=universe.universe_id,
        source=universe.source,
        memberships=universe.memberships,
        calendar=_calendar_index(universe.trading_calendar),
        raw={key: value for key, value in raw_all.items() if not key.endswith(".US")},
        benchmark_signal={key: value for key, value in front.items() if key.endswith(".US")},
        benchmark_raw={key: value for key, value in raw_all.items() if key.endswith(".US")},
        listing_aliases=universe.listing_aliases.copy(),
        corporate_actions=universe.corporate_actions.copy(),
        fee_schedule=universe.fee_schedule.copy(),
        session_exceptions=universe.session_exceptions.copy(),
        static_signal={key: value for key, value in front.items() if not key.endswith(".US")},
    )


def _benchmark_bar_maps(dataset: Any) -> tuple[Mapping[str, pd.DataFrame], Mapping[str, pd.DataFrame]]:
    signal = getattr(dataset, "benchmark_signal_bars", None)
    raw = getattr(dataset, "benchmark_raw_bars", None)
    combined = getattr(dataset, "benchmark_bars", None)
    if signal is None:
        signal = combined
    if raw is None:
        raw = combined
    if not isinstance(signal, Mapping) or not isinstance(raw, Mapping):
        raise ValueError("USBacktestDataset has no benchmark bar maps")
    return signal, raw


def _calendar_index(value: Any) -> pd.DatetimeIndex:
    if isinstance(value, pd.DataFrame):
        if "session_date" not in value.columns:
            raise ValueError("Frozen XNYS calendar requires session_date")
        raw_days = value["session_date"]
    else:
        raw_days = value
    if raw_days is None:
        raise ValueError("Frozen XNYS calendar is required")
    days = pd.DatetimeIndex(pd.to_datetime(list(raw_days)))
    if days.tz is not None:
        days = days.tz_convert("America/New_York").tz_localize(None)
    days = days.normalize()
    if days.duplicated().any():
        raise ValueError("Frozen XNYS calendar contains duplicate sessions")
    days = days.sort_values()
    if len(days) < 2:
        raise ValueError("Frozen XNYS calendar has fewer than two sessions")
    return days


def _frame_or_empty(value: Any) -> pd.DataFrame:
    return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _validate_benchmark_calendar(
    signal: pd.DataFrame,
    raw: pd.DataFrame,
    sessions: list[pd.Timestamp],
    code: str,
) -> None:
    missing_signal = [item for item in sessions if _row_on(signal, item) is None]
    missing_raw = [item for item in sessions if _row_on(raw, item) is None]
    if missing_signal or missing_raw:
        raise ValueError(
            f"Frozen XNYS calendar coverage is incomplete for {code}: "
            f"signal={len(missing_signal)}, raw={len(missing_raw)}"
        )


def _alias_on(aliases: pd.DataFrame, security_id: str, session: pd.Timestamp) -> str:
    required = {"security_id", "valid_from"}
    if aliases.empty or not required.issubset(aliases.columns):
        raise ValueError(f"No listing alias evidence for {security_id} on {session.date()}")
    rows = aliases.loc[
        aliases["security_id"].astype(str).map(_normalize_security_id).eq(security_id)
    ].copy()
    starts = _normalized_date_series(rows["valid_from"])
    if "valid_to" in rows:
        ends = _normalized_date_series(rows["valid_to"])
        active = starts.le(session) & (ends.isna() | ends.ge(session))
    else:
        active = starts.le(session)
    rows = rows.loc[active]
    if len(rows) != 1:
        raise ValueError(
            f"Expected one active listing alias for {security_id} on {session.date()}, "
            f"found {len(rows)}"
        )
    row = rows.iloc[0]
    value = row.get("vendor_code")
    if pd.isna(value) or not str(value).strip():
        value = row.get("ticker")
    code = _normalize_code(value)
    if not code.endswith(".US"):
        code = f"{code}.US"
    return code


def _security_for_active_code(
    data: _StrictDataView,
    code: str,
    positions: Mapping[str, _Position],
    session: pd.Timestamp,
) -> str | None:
    target = _normalize_code(code)
    matches = [
        security_id
        for security_id in positions
        if data.vendor_code(security_id, session) == target
    ]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous held ticker {target} on {session.date()}")
    return matches[0] if matches else None


def _validate_members(members: frozenset[str], session: pd.Timestamp) -> None:
    if not members:
        raise ValueError(f"Point-in-time universe is empty on {session.date()}")
    invalid = sorted(
        security_id
        for security_id in members
        if not security_id.startswith("us_") or security_id.endswith(".us")
    )
    if invalid:
        raise ValueError(
            "Point-in-time universe must contain stable security_id values: "
            + ", ".join(invalid[:10])
        )


def _validate_month_end_coverage(
    members: frozenset[str],
    session: pd.Timestamp,
    front: dict[str, pd.DataFrame],
    raw: dict[str, pd.DataFrame],
) -> None:
    missing_front = sorted(
        code for code in members if _row_on(front.get(code), session) is None
    )
    missing_raw = sorted(
        code for code in members if _row_on(raw.get(code), session) is None
    )
    if missing_front or missing_raw:
        parts = []
        if missing_front:
            parts.append("front=" + ",".join(missing_front[:10]))
        if missing_raw:
            parts.append("raw=" + ",".join(missing_raw[:10]))
        raise ValueError(
            f"Incomplete point-in-time month-end coverage on {session.date()}: "
            + "; ".join(parts)
        )


def _fees_on(
    schedule: pd.DataFrame,
    session: pd.Timestamp,
    fallback: USPortfolioConfig,
    *,
    commission_multiplier: float = 1.0,
    slippage_multiplier: float = 1.0,
) -> _FeeTerms:
    if not np.isfinite(commission_multiplier) or commission_multiplier <= 0:
        raise ValueError("commission_multiplier must be positive")
    if not np.isfinite(slippage_multiplier) or slippage_multiplier <= 0:
        raise ValueError("slippage_multiplier must be positive")
    if schedule.empty:
        # Empty fee schedules are allowed only for explicit unit-test fixtures.
        return _FeeTerms(
            commission_rate=fallback.commission_rate * commission_multiplier,
            min_commission=fallback.min_commission * commission_multiplier,
            slippage_rate=fallback.slippage_rate * slippage_multiplier,
            sec_sell_fee_rate=fallback.sec_sell_fee_rate,
            finra_taf_per_share=fallback.finra_taf_per_share,
            finra_taf_cap=fallback.finra_taf_cap,
        )
    if "effective_from" not in schedule.columns:
        raise ValueError("Fee schedule requires effective_from")
    starts = _normalized_date_series(schedule["effective_from"])
    if starts.isna().any():
        raise ValueError("Fee schedule contains invalid effective_from")
    if "effective_to" in schedule.columns:
        ends = _normalized_date_series(schedule["effective_to"])
        active = starts.le(session) & (ends.isna() | ends.ge(session))
    else:
        active = starts.le(session)
    rows = schedule.loc[active]
    if len(rows) != 1:
        raise ValueError(
            f"Expected one effective fee row on {session.date()}, found {len(rows)}"
        )
    row = rows.iloc[0]
    return _FeeTerms(
        commission_rate=_nonnegative_rate(
            row.get("commission_rate", fallback.commission_rate), "commission_rate"
        )
        * commission_multiplier,
        min_commission=_nonnegative_rate(
            row.get("min_commission", fallback.min_commission), "min_commission"
        )
        * commission_multiplier,
        slippage_rate=_nonnegative_rate(
            row.get("slippage_rate", fallback.slippage_rate), "slippage_rate"
        )
        * slippage_multiplier,
        sec_sell_fee_rate=_nonnegative_rate(
            row.get("sec_sell_fee_rate", fallback.sec_sell_fee_rate),
            "sec_sell_fee_rate",
        ),
        finra_taf_per_share=_nonnegative_rate(
            row.get("finra_taf_per_share", fallback.finra_taf_per_share),
            "finra_taf_per_share",
        ),
        finra_taf_cap=_nonnegative_rate(
            row.get("finra_taf_cap", fallback.finra_taf_cap), "finra_taf_cap"
        ),
    )


def _nonnegative_rate(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float("nan")
    if not np.isfinite(result) or result < 0:
        raise ValueError(f"Invalid effective fee {name}")
    return result


def _has_session_exception(
    data: _StrictDataView,
    security_id: str,
    session: pd.Timestamp,
) -> bool:
    frame = data.session_exceptions
    if frame.empty:
        return False
    date_column = "session_date" if "session_date" in frame.columns else "date"
    if not {"security_id", date_column}.issubset(frame.columns):
        raise ValueError("session_exceptions schema is invalid")
    dates = _normalized_date_series(frame[date_column])
    rows = frame.loc[
        frame["security_id"].astype(str).map(_normalize_security_id).eq(security_id)
        & dates.eq(session)
    ]
    if rows.empty:
        return False
    if len(rows) != 1:
        raise ValueError(
            f"Duplicate session exception for {security_id} on {session.date()}"
        )
    kind = str(rows.iloc[0].get("exception_type", rows.iloc[0].get("status", ""))).upper()
    if kind not in {"HALTED", "NO_TRADE"}:
        raise ValueError(
            f"Unsupported session exception {kind} for {security_id} on {session.date()}"
        )
    return True


def _settle_receivables(
    session: pd.Timestamp,
    receivables: list[tuple[pd.Timestamp, float, str]],
    cash: float,
    trades: list[dict[str, Any]],
) -> float:
    pending: list[tuple[pd.Timestamp, float, str]] = []
    for pay_date, amount, security_id in receivables:
        if pay_date <= session:
            cash += amount
            trades.append(
                {
                    "signal_id": "",
                    "security_id": security_id,
                    "code": "",
                    "side": "CASH",
                    "timestamp": session.date().isoformat(),
                    "quantity": 0,
                    "price": 0.0,
                    "fees": 0.0,
                    "pnl": round(amount, 6),
                    "return": None,
                    "reason": "US_CASH_DIVIDEND_PAYMENT",
                    "evidence": {},
                }
            )
        else:
            pending.append((pay_date, amount, security_id))
    receivables[:] = pending
    return cash


def _apply_corporate_actions(
    session: pd.Timestamp,
    data: _StrictDataView,
    positions: dict[str, _Position],
    receivables: list[tuple[pd.Timestamp, float, str]],
    cash: float,
    trades: list[dict[str, Any]],
) -> float:
    frame = data.corporate_actions
    if frame.empty or "effective_at" not in frame.columns:
        return cash
    effective = ny_session_dates(frame["effective_at"])
    actions = frame.loc[effective.eq(session)]
    sort_columns = [
        column for column in ("security_id", "action_id") if column in frame.columns
    ]
    if sort_columns:
        actions = actions.sort_values(sort_columns, kind="stable")
    for _, action in actions.iterrows():
        security_id = _normalize_security_id(action.get("security_id"))
        position = positions.get(security_id)
        if position is None:
            continue
        announced = pd.to_datetime(action.get("announced_at"), errors="coerce", utc=True)
        announced_day = pd.NaT if pd.isna(announced) else ny_session_date(announced)
        if pd.isna(announced_day) or announced_day > session:
            raise ValueError(
                f"Corporate action was not available by effective date for {security_id}"
            )
        if not _truthy(action.get("terms_verified", False)):
            raise ValueError(f"Unverified corporate action terms for {security_id}")
        kind = str(action.get("action_type", "")).strip().upper()
        if kind in {"TICKER_CHANGE", "RENAME"}:
            continue
        if kind in {"SPLIT", "STOCK_DIVIDEND"}:
            ratio = _positive_term(action, ("split_ratio", "share_ratio", "ratio"), kind)
            new_quantity = position.quantity * ratio
            whole = round(new_quantity)
            if not np.isclose(new_quantity, whole, atol=1e-9):
                cash_in_lieu = _optional_nonnegative(
                    action, ("cash_in_lieu_price", "fractional_cash_price")
                )
                if cash_in_lieu is None:
                    raise ValueError(
                        f"Fractional {kind} has no cash-in-lieu terms for {security_id}"
                    )
                fractional = new_quantity - int(new_quantity)
                cash += fractional * cash_in_lieu
                new_quantity = float(int(new_quantity))
            position.quantity = float(new_quantity)
            position.average_price /= ratio
            position.stop_price /= ratio
            position.last_price /= ratio
            successor_value = str(
                action.get("successor_security_id") or ""
            ).strip()
            if successor_value and successor_value != security_id:
                successor = _normalize_security_id(successor_value)
                if successor in positions:
                    raise ValueError(
                        f"{kind} creates duplicate successor position {successor}"
                    )
                del positions[security_id]
                position.security_id = successor
                position.code = data.vendor_code(successor, session)
                positions[successor] = position
            continue
        if kind == "CASH_DIVIDEND":
            amount = _positive_term(
                action, ("cash_amount", "cash_per_share"), kind, allow_zero=True
            )
            entitlement = position.quantity * amount
            pay_value = action.get("pay_date")
            try:
                pay_date = ny_session_date(pay_value)
            except (TypeError, ValueError):
                pay_date = None
            if pay_date is None or pay_date < session:
                raise ValueError(f"Cash dividend has invalid pay_date for {security_id}")
            position.stop_price = max(0.000001, position.stop_price - amount)
            if pay_date == session:
                cash += entitlement
            else:
                receivables.append((pay_date, entitlement, security_id))
            continue
        if kind in {"CASH_MERGER", "DELISTING", "BANKRUPTCY"}:
            settlement = _optional_nonnegative(
                action,
                ("cash_amount", "cash_per_share", "settlement_cash_per_share"),
            )
            if settlement is None:
                raise ValueError(f"Unknown settlement terms for {kind} {security_id}")
            proceeds = position.quantity * settlement
            _record_action_exit(session, position, settlement, kind, trades)
            cash += proceeds
            del positions[security_id]
            continue
        if kind == "STOCK_MERGER":
            successor = _normalize_security_id(action.get("successor_security_id"))
            ratio = _positive_term(action, ("share_ratio", "exchange_ratio", "ratio"), kind)
            if successor in positions:
                raise ValueError(f"Stock merger creates duplicate position {successor}")
            new_quantity = position.quantity * ratio
            whole_quantity = round(new_quantity)
            if not np.isclose(new_quantity, whole_quantity, atol=1e-9):
                cash_in_lieu = _optional_nonnegative(
                    action, ("cash_in_lieu_price", "fractional_cash_price")
                )
                if cash_in_lieu is None:
                    raise ValueError(
                        f"Fractional stock merger has no cash-in-lieu terms for {security_id}"
                    )
                fractional = new_quantity - int(new_quantity)
                cash += fractional * cash_in_lieu
                new_quantity = float(int(new_quantity))
            del positions[security_id]
            position.security_id = successor
            position.quantity = float(new_quantity)
            position.average_price /= ratio
            position.stop_price /= ratio
            position.last_price /= ratio
            position.code = data.vendor_code(successor, session)
            if successor not in data.members_on(session):
                position.forced_exit_reason = "US_STOCK_MERGER_EXIT"
            positions[successor] = position
            continue
        if kind == "SPINOFF":
            child = _normalize_security_id(action.get("successor_security_id"))
            ratio = _positive_term(action, ("share_ratio", "ratio"), kind)
            allocation = _positive_term(
                action, ("cost_basis_fraction",), kind, allow_zero=True
            )
            if allocation > 1 or child in positions:
                raise ValueError(f"Invalid spinoff terms for {security_id}")
            child_quantity = position.quantity * ratio
            if not np.isclose(child_quantity, round(child_quantity), atol=1e-9):
                raise ValueError(f"Fractional spinoff terms unresolved for {security_id}")
            child_position = _Position(
                security_id=child,
                code=data.vendor_code(child, session),
                quantity=float(round(child_quantity)),
                average_price=position.average_price * allocation / ratio,
                entry_date=position.entry_date,
                stop_price=max(0.000001, position.stop_price * allocation / ratio),
                last_price=position.last_price * allocation / ratio,
                entry_fees=0.0,
                forced_exit_reason=(
                    "" if child in data.members_on(session) else "US_SPINOFF_EXIT"
                ),
            )
            position.average_price *= 1 - allocation
            position.stop_price = max(0.000001, position.stop_price * (1 - allocation))
            position.last_price = max(0.000001, position.last_price * (1 - allocation))
            positions[child] = child_position
            continue
        raise ValueError(f"Unsupported held corporate action {kind} for {security_id}")
    return cash


def _record_action_exit(
    session: pd.Timestamp,
    position: _Position,
    settlement: float,
    kind: str,
    trades: list[dict[str, Any]],
) -> None:
    value = position.quantity * settlement
    basis = position.average_price * position.quantity + position.entry_fees
    pnl = value - basis
    trades.append(
        {
            "signal_id": "",
            "security_id": position.security_id,
            "code": position.code,
            "side": "SELL",
            "timestamp": session.date().isoformat(),
            "quantity": position.quantity,
            "price": round(settlement, 6),
            "fees": 0.0,
            "pnl": round(pnl, 6),
            "return": round(pnl / basis, 8) if basis > 0 else None,
            "reason": f"US_{kind}",
            "evidence": {"settlement_source": "verified_corporate_action"},
        }
    )


def _positive_term(
    row: pd.Series,
    names: tuple[str, ...],
    kind: str,
    *,
    allow_zero: bool = False,
) -> float:
    value = _optional_nonnegative(row, names)
    if value is None or (value == 0 and not allow_zero):
        raise ValueError(f"{kind} is missing verified {'/'.join(names)}")
    return value


def _optional_nonnegative(row: pd.Series, names: tuple[str, ...]) -> float | None:
    for name in names:
        if name not in row or pd.isna(row.get(name)):
            continue
        try:
            value = float(row.get(name))
        except (TypeError, ValueError):
            continue
        if np.isfinite(value) and value >= 0:
            return value
    return None


def _truthy(value: Any) -> bool:
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _require_execution_row(
    raw: dict[str, pd.DataFrame],
    code: str,
    session: pd.Timestamp,
) -> pd.Series:
    row = _row_on(raw.get(code), session)
    if row is None:
        raise ValueError(
            f"Missing next-session raw execution row for {code} on {session.date()}"
        )
    return row


def _row_on(frame: pd.DataFrame | None, session: pd.Timestamp) -> pd.Series | None:
    if frame is None or frame.empty or session not in frame.index:
        return None
    row = frame.loc[session]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    return row


def _slice_to(frame: pd.DataFrame, session: pd.Timestamp) -> pd.DataFrame:
    return frame[frame.index <= session]


def _positive(value: Any, code: str, session: pd.Timestamp, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float("nan")
    if not np.isfinite(result) or result <= 0:
        raise ValueError(f"Invalid raw {field} for {code} on {session.date()}")
    return result


def _sell_fees(value: float, quantity: float, costs: _FeeTerms) -> float:
    commission = max(costs.min_commission, value * costs.commission_rate)
    sec_fee = value * costs.sec_sell_fee_rate
    finra = min(costs.finra_taf_cap, quantity * costs.finra_taf_per_share)
    return commission + sec_fee + finra


def _equity(cash: float, positions: dict[str, _Position]) -> float:
    return cash + sum(
        position.quantity * position.last_price for position in positions.values()
    )


def _metrics(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    initial_capital: float,
) -> dict[str, Any]:
    curve = equity.set_index("timestamp")["equity"].astype(float)
    total_return = float(curve.iloc[-1] / initial_capital - 1.0)
    annualized = (
        float((curve.iloc[-1] / initial_capital) ** (252 / max(1, len(curve) - 1)) - 1)
        if len(curve) > 1 and curve.iloc[-1] > 0
        else 0.0
    )
    returns = curve.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
    volatility = float(returns.std(ddof=0)) if not returns.empty else 0.0
    sharpe = float(returns.mean() / volatility * np.sqrt(252)) if volatility > 0 else 0.0
    drawdown = curve / curve.cummax() - 1.0
    sells = (
        trades[trades["side"].eq("SELL")]
        if not trades.empty and "side" in trades
        else pd.DataFrame()
    )
    pnl = pd.to_numeric(sells.get("pnl"), errors="coerce").dropna() if not sells.empty else pd.Series(dtype=float)
    return {
        "initial_cash": float(initial_capital),
        "final_equity": float(curve.iloc[-1]),
        "total_return": total_return,
        "annualized_return": annualized,
        "max_drawdown": float(drawdown.min()),
        "sharpe_ratio": sharpe,
        "trades": int(len(trades)),
        "closed_trades": int(len(sells)),
        "win_rate": float((pnl > 0).mean()) if not pnl.empty else 0.0,
        "trading_days": int(len(curve)),
    }


def _normalize_code(value: Any) -> str:
    return str(value).strip().upper()


def _normalize_security_id(value: Any) -> str:
    result = str(value).strip().lower()
    if not result or result in {"none", "nan"} or result.endswith(".us"):
        raise ValueError(f"Invalid stable security_id: {value}")
    return result


def _normalized_date_series(values: pd.Series) -> pd.Series:
    return ny_session_dates(values)


def _day(value: Any) -> pd.Timestamp:
    return ny_session_date(value)


__all__ = ["StrictUSPointInTimeUniverse", "run_backtest"]
