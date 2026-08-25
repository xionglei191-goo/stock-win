from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

import math
import pandas as pd

from .config import StrategyConfig
from .models import PendingOrder, PortfolioState, Position, Signal


def price_limit_ratio(code: str, name: str = "") -> float:
    if "ST" in name.upper():
        return 0.05
    number = code.split(".", 1)[0]
    if code.endswith(".BJ"):
        return 0.30
    if number.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def _commission(value: float, config: StrategyConfig) -> float:
    return max(config.costs.min_commission, value * config.costs.commission_rate)


class PaperBroker:
    def __init__(self, config: StrategyConfig, state: PortfolioState):
        self.config = config
        self.state = state
        self.trades: list[dict[str, Any]] = []

    @property
    def equity(self) -> float:
        market_value = sum(
            position.quantity * (position.last_price or position.average_price)
            for position in self.state.positions.values()
        )
        return float(self.state.cash + market_value)

    def mark(self, code: str, price: float) -> None:
        position = self.state.positions.get(code)
        if position is not None and price > 0:
            position.last_price = float(price)

    def queue(self, signals: list[Signal]) -> None:
        existing = {(order.code, order.side) for order in self.state.pending_orders}
        for signal in signals:
            key = (signal.code, signal.side)
            if key in existing:
                continue
            if signal.side == "BUY" and signal.code in self.state.positions:
                continue
            if signal.side == "SELL" and signal.code not in self.state.positions:
                continue
            self.state.pending_orders.append(
                PendingOrder(
                    code=signal.code,
                    side=signal.side,
                    signal_time=pd.Timestamp(signal.timestamp).isoformat(),
                    reason=signal.reason,
                    sector_code=signal.sector_code,
                    reference_price=signal.price,
                )
            )
            existing.add(key)

    def process_pending(
        self,
        bars: dict[str, pd.DataFrame],
        previous_closes: dict[str, float],
        names: dict[str, str],
    ) -> None:
        remaining: list[PendingOrder] = []
        for order in self.state.pending_orders:
            frame = bars.get(order.code)
            if frame is None or frame.empty or "Open" not in frame.columns:
                remaining.append(order)
                continue
            signal_time = pd.Timestamp(order.signal_time)
            candidates = frame[frame.index > signal_time]
            if candidates.empty:
                remaining.append(order)
                continue
            filled = False
            for timestamp_value, row in candidates.iterrows():
                timestamp = pd.Timestamp(timestamp_value)
                open_price = float(row["Open"])
                effective_previous_close = float(
                    previous_closes.get(order.code, order.reference_price or open_price)
                )
                if not self._can_fill(
                    order,
                    timestamp,
                    open_price,
                    effective_previous_close,
                    names.get(order.code, ""),
                ):
                    continue
                filled = self._fill(order, timestamp, open_price)
                if filled:
                    break
            if not filled:
                remaining.append(order)
        self.state.pending_orders = remaining

    def _can_fill(
        self,
        order: PendingOrder,
        timestamp: pd.Timestamp,
        open_price: float,
        previous_close: float,
        name: str,
    ) -> bool:
        if not math.isfinite(open_price) or open_price <= 0:
            return False
        ratio = price_limit_ratio(order.code, name)
        if previous_close > 0:
            if order.side == "BUY" and open_price >= previous_close * (1 + ratio - 0.001):
                return False
            if order.side == "SELL" and open_price <= previous_close * (1 - ratio + 0.001):
                return False
        if order.side == "SELL":
            position = self.state.positions.get(order.code)
            if position is None:
                return False
            if timestamp.date() <= date.fromisoformat(position.entry_date):
                return False
        return True

    def _fill(self, order: PendingOrder, timestamp: pd.Timestamp, open_price: float) -> bool:
        if order.side == "BUY":
            return self._fill_buy(order, timestamp, open_price)
        return self._fill_sell(order, timestamp, open_price)

    def _fill_buy(self, order: PendingOrder, timestamp: pd.Timestamp, open_price: float) -> bool:
        if order.code in self.state.positions or len(self.state.positions) >= self.config.risk.max_positions:
            return False
        execution_price = open_price * (1 + self.config.costs.slippage_rate)
        equity_limit = self.equity * self.config.risk.max_position_weight
        risk_limit = self.equity * self.config.risk.risk_per_trade / self.config.risk.fixed_stop_loss
        budget = min(equity_limit, risk_limit, self.state.cash)
        quantity = int(budget / execution_price / self.config.risk.board_lot) * self.config.risk.board_lot
        while quantity > 0:
            value = execution_price * quantity
            fee = _commission(value, self.config)
            if value + fee <= self.state.cash:
                break
            quantity -= self.config.risk.board_lot
        if quantity <= 0:
            return False
        value = execution_price * quantity
        fee = _commission(value, self.config)
        self.state.cash -= value + fee
        self.state.positions[order.code] = Position(
            code=order.code,
            quantity=quantity,
            average_price=execution_price,
            entry_time=timestamp.isoformat(),
            entry_date=timestamp.date().isoformat(),
            stop_price=execution_price * (1 - self.config.risk.fixed_stop_loss),
            sector_code=order.sector_code,
            last_price=execution_price,
            entry_fees=fee,
        )
        self.trades.append(
            {
                "timestamp": timestamp.isoformat(),
                "code": order.code,
                "side": "BUY",
                "quantity": quantity,
                "price": execution_price,
                "value": value,
                "fees": fee,
                "reason": order.reason,
                "pnl": "",
                "cash_after": self.state.cash,
            }
        )
        return True

    def _fill_sell(self, order: PendingOrder, timestamp: pd.Timestamp, open_price: float) -> bool:
        position = self.state.positions.get(order.code)
        if position is None:
            return False
        execution_price = open_price * (1 - self.config.costs.slippage_rate)
        value = execution_price * position.quantity
        fee = _commission(value, self.config) + value * self.config.costs.stamp_duty_rate
        self.state.cash += value - fee
        pnl = (
            (execution_price - position.average_price) * position.quantity
            - fee
            - position.entry_fees
        )
        self.trades.append(
            {
                "timestamp": timestamp.isoformat(),
                "code": order.code,
                "side": "SELL",
                "quantity": position.quantity,
                "price": execution_price,
                "value": value,
                "fees": fee,
                "reason": order.reason,
                "pnl": pnl,
                "cash_after": self.state.cash,
            }
        )
        del self.state.positions[order.code]
        return True

    def state_record(self) -> dict[str, Any]:
        return asdict(self.state)
