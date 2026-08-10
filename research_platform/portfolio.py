from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any
from uuid import uuid4

import pandas as pd

from strategy_v1.portfolio import price_limit_ratio

from .config import PlatformConfig
from .models import PlatformSignal, SignalStatus
from .storage import Database


def can_trade_at_open(
    code: str,
    side: str,
    open_price: float,
    previous_close: float,
    name: str = "",
) -> bool:
    if not math.isfinite(open_price) or open_price <= 0 or previous_close <= 0:
        return False
    ratio = price_limit_ratio(code, name)
    if side == "BUY" and open_price >= previous_close * (1 + ratio - 0.001):
        return False
    if side == "SELL" and open_price <= previous_close * (1 - ratio + 0.001):
        return False
    return True


class PaperPortfolio:
    def __init__(self, config: PlatformConfig, database: Database):
        self.config = config
        self.database = database

    def queue_approved(self, signals: list[PlatformSignal]) -> None:
        rows = [signal for signal in signals if signal.status == SignalStatus.APPROVED]
        if not rows:
            return
        with self.database.connect() as connection:
            for signal in rows:
                connection.execute(
                    """INSERT OR IGNORE INTO paper_orders
                    (order_id, signal_id, strategy_id, code, side, status, signal_time, target_weight, reason)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
                    (
                        f"ord_{signal.signal_id}", signal.signal_id, signal.strategy_id, signal.code,
                        signal.side, signal.generated_at.isoformat(), signal.target_weight,
                        json.dumps(signal.reason_codes, ensure_ascii=False),
                    ),
                )

    def positions(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        if strategy_id:
            return self.database.query(
                "SELECT * FROM paper_positions WHERE strategy_id=? ORDER BY code", (strategy_id,)
            )
        return self.database.query("SELECT * FROM paper_positions ORDER BY strategy_id, code")

    def process_pending(
        self,
        bars: dict[str, pd.DataFrame],
        names: dict[str, str],
    ) -> list[dict[str, Any]]:
        fills: list[dict[str, Any]] = []
        orders = self.database.query("SELECT * FROM paper_orders WHERE status='PENDING' ORDER BY signal_time, order_id")
        for order in orders:
            frame = bars.get(order["code"])
            if frame is None or frame.empty or "Open" not in frame.columns:
                continue
            frame = frame.copy().sort_index()
            frame.index = pd.to_datetime(frame.index)
            signal_time = _align_timestamp(frame.index, pd.Timestamp(order["signal_time"]))
            candidates = frame[frame.index > signal_time]
            if candidates.empty:
                continue
            one_session = str(order["strategy_id"]).startswith("course49_") and order["side"] == "BUY"
            if one_session:
                candidates = candidates.head(1)
            filled = False
            block_reason = ""
            for timestamp, row in candidates.iterrows():
                open_price = float(row["Open"])
                prior = frame[frame.index < pd.Timestamp(timestamp)]
                previous_close = float(prior["Close"].iloc[-1]) if not prior.empty and "Close" in prior else open_price
                if not self._can_fill(order, pd.Timestamp(timestamp), open_price, previous_close, names.get(order["code"], "")):
                    block_reason = "NEXT_OPEN_NOT_TRADABLE"
                    continue
                fill = self._fill(order, pd.Timestamp(timestamp), open_price)
                if fill:
                    fills.append(fill)
                    filled = True
                    break
                block_reason = "PORTFOLIO_CONSTRAINT"
            if (
                not filled
                and one_session
            ):
                self.database.execute(
                    "UPDATE paper_orders SET status='CANCELED', block_reason=? WHERE order_id=?",
                    (block_reason or "NEXT_OPEN_UNFILLED", order["order_id"]),
                )
        self.mark_to_market(bars)
        self.record_equity(datetime.now().astimezone().isoformat())
        return fills

    def process_pending_groups(
        self,
        bars: dict[str, pd.DataFrame],
        names: dict[str, str],
    ) -> list[dict[str, Any]]:
        fills: list[dict[str, Any]] = []
        intents = self.database.query(
            """SELECT * FROM order_group_intents WHERE status='APPROVED'
            ORDER BY generated_at, intent_id"""
        )
        for intent in intents:
            legs = self.database.query(
                "SELECT * FROM order_group_legs WHERE intent_id=? ORDER BY leg_id",
                (intent["intent_id"],),
            )
            execution = self._group_execution_rows(intent, legs, bars, names)
            if execution is None:
                latest = max(
                    (
                        pd.Timestamp(frame.index[-1])
                        for code, frame in bars.items()
                        if code in {str(leg["code"]) for leg in legs} and not frame.empty
                    ),
                    default=None,
                )
                if latest is not None and _naive_timestamp(latest) > _naive_timestamp(intent["valid_until"]):
                    self.database.execute(
                        "UPDATE order_group_intents SET status=? WHERE intent_id=?",
                        (SignalStatus.EXPIRED.value, intent["intent_id"]),
                    )
                continue
            fills.extend(self._fill_order_group(intent, execution))
        self.mark_to_market(bars)
        self.record_equity(datetime.now().astimezone().isoformat())
        return fills
    def _group_execution_rows(
        self,
        intent: dict[str, Any],
        legs: list[dict[str, Any]],
        bars: dict[str, pd.DataFrame],
        names: dict[str, str],
    ) -> list[dict[str, Any]] | None:
        if not legs:
            return None
        rows: list[dict[str, Any]] = []
        execution_day: date | None = None
        for leg in legs:
            frame = bars.get(str(leg["code"]))
            if frame is None or frame.empty or "Open" not in frame.columns:
                return None
            frame = frame.copy().sort_index()
            frame.index = pd.to_datetime(frame.index)
            signal_time = _align_timestamp(frame.index, pd.Timestamp(intent["generated_at"]))
            candidates = frame[frame.index > signal_time]
            if candidates.empty:
                return None
            timestamp = pd.Timestamp(candidates.index[0])
            if execution_day is not None and timestamp.date() != execution_day:
                return None
            execution_day = timestamp.date()
            row = candidates.iloc[0]
            open_price = float(row["Open"])
            prior = frame[frame.index < timestamp]
            previous_close = (
                float(prior["Close"].iloc[-1])
                if not prior.empty and "Close" in prior.columns
                else open_price
            )
            if not math.isfinite(open_price) or open_price <= 0:
                return None
            ratio = price_limit_ratio(str(leg["code"]), names.get(str(leg["code"]), ""))
            side = str(leg["side"])
            if side in {"BUY", "COVER"} and open_price >= previous_close * (1 + ratio - 0.001):
                return None
            if side in {"SELL", "SHORT"} and open_price <= previous_close * (1 - ratio + 0.001):
                return None
            rows.append({**leg, "timestamp": timestamp, "open_price": open_price})
        return rows

    def _fill_order_group(
        self,
        intent: dict[str, Any],
        execution: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        config = self.config.portfolio
        output: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            account = connection.execute(
                "SELECT * FROM paper_accounts WHERE strategy_id=?", (intent["strategy_id"],)
            ).fetchone()
            if account is None:
                return []
            existing = connection.execute(
                """SELECT * FROM paper_group_positions
                WHERE strategy_id=? AND group_key=? ORDER BY code""",
                (intent["strategy_id"], intent["group_key"]),
            ).fetchall()
            action = str(intent["action"])
            if action == "OPEN" and existing:
                return []
            if action == "CLOSE" and not existing:
                return []
            timestamp = pd.Timestamp(execution[0]["timestamp"])
            if existing and timestamp.date() <= date.fromisoformat(str(existing[0]["entry_time"])[:10]):
                return []

            regular_positions = connection.execute(
                "SELECT * FROM paper_positions WHERE strategy_id=?", (intent["strategy_id"],)
            ).fetchall()
            group_positions = connection.execute(
                "SELECT * FROM paper_group_positions WHERE strategy_id=?", (intent["strategy_id"],)
            ).fetchall()
            strategy_equity = float(account["cash"]) + sum(
                float(item["quantity"]) * float(item["last_price"]) for item in regular_positions
            ) + sum(
                float(item["quantity"]) * float(item["last_price"])
                * (1 if item["side"] == "LONG" else -1)
                for item in group_positions
            )
            prepared: list[dict[str, Any]] = []
            if action == "OPEN":
                gross_budget = max(0.0, strategy_equity * float(intent["gross_target_weight"]))
                long_cost = 0.0
                for leg in execution:
                    side = str(leg["side"])
                    price = float(leg["open_price"]) * (
                        1 + config.slippage_rate if side in {"BUY", "COVER"} else 1 - config.slippage_rate
                    )
                    budget = gross_budget * float(leg["target_weight"])
                    quantity = int(budget / price / config.board_lot) * config.board_lot
                    if quantity <= 0:
                        return []
                    value = price * quantity
                    fees = max(config.min_commission, value * config.commission_rate)
                    if side == "BUY":
                        long_cost += value + fees
                    prepared.append({**leg, "price": price, "quantity": quantity, "fees": fees, "pnl": None})
                if long_cost > float(account["cash"]):
                    return []
            else:
                positions_by_code = {str(item["code"]): item for item in existing}
                for leg in execution:
                    position = positions_by_code.get(str(leg["code"]))
                    if position is None:
                        return []
                    side = str(leg["side"])
                    price = float(leg["open_price"]) * (
                        1 + config.slippage_rate if side == "COVER" else 1 - config.slippage_rate
                    )
                    quantity = int(position["quantity"])
                    value = price * quantity
                    fees = max(config.min_commission, value * config.commission_rate)
                    if side == "SELL":
                        fees += value * config.stamp_duty_rate
                        pnl = (
                            (price - float(position["average_price"])) * quantity
                            - float(position["entry_fees"])
                            - fees
                        )
                    else:
                        pnl = (
                            (float(position["average_price"]) - price) * quantity
                            - float(position["entry_fees"])
                            - fees
                        )
                    prepared.append({**leg, "price": price, "quantity": quantity, "fees": fees, "pnl": pnl})

            cash_change = 0.0
            for leg in prepared:
                value = float(leg["price"]) * int(leg["quantity"])
                side = str(leg["side"])
                if side in {"BUY", "COVER"}:
                    cash_change -= value + float(leg["fees"])
                else:
                    cash_change += value - float(leg["fees"])
            if float(account["cash"]) + cash_change < -1e-6:
                return []

            connection.execute(
                "UPDATE paper_accounts SET cash=cash+?, updated_at=? WHERE strategy_id=?",
                (cash_change, timestamp.isoformat(), intent["strategy_id"]),
            )
            if action == "OPEN":
                for leg in prepared:
                    connection.execute(
                        """INSERT INTO paper_group_positions
                        (strategy_id, group_key, code, side, quantity, average_price, entry_time,
                         last_price, ratio, target_weight, entry_fees, evidence)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            intent["strategy_id"],
                            intent["group_key"],
                            leg["code"],
                            "LONG" if leg["side"] == "BUY" else "SHORT",
                            leg["quantity"],
                            leg["price"],
                            timestamp.isoformat(),
                            leg["price"],
                            leg["ratio"],
                            leg["target_weight"],
                            leg["fees"],
                            intent["evidence"],
                        ),
                    )
            else:
                connection.execute(
                    "DELETE FROM paper_group_positions WHERE strategy_id=? AND group_key=?",
                    (intent["strategy_id"], intent["group_key"]),
                )

            for leg in prepared:
                fill = {
                    "fill_id": uuid4().hex,
                    "intent_id": intent["intent_id"],
                    "strategy_id": intent["strategy_id"],
                    "group_key": intent["group_key"],
                    "leg_id": leg["leg_id"],
                    "code": leg["code"],
                    "side": leg["side"],
                    "action": action,
                    "timestamp": timestamp.isoformat(),
                    "quantity": leg["quantity"],
                    "price": leg["price"],
                    "fees": leg["fees"],
                    "pnl": leg["pnl"],
                }
                connection.execute(
                    """INSERT INTO paper_group_fills
                    (fill_id, intent_id, strategy_id, group_key, leg_id, code, side, action,
                     timestamp, quantity, price, fees, pnl)
                    VALUES (:fill_id, :intent_id, :strategy_id, :group_key, :leg_id, :code,
                            :side, :action, :timestamp, :quantity, :price, :fees, :pnl)""",
                    fill,
                )
                output.append(fill)
            connection.execute(
                """UPDATE order_group_intents SET status=?, filled_at=? WHERE intent_id=?""",
                (SignalStatus.EXECUTED.value, timestamp.isoformat(), intent["intent_id"]),
            )
        return output

    def _can_fill(
        self,
        order: dict[str, Any],
        timestamp: pd.Timestamp,
        open_price: float,
        previous_close: float,
        name: str,
    ) -> bool:
        if not can_trade_at_open(
            str(order["code"]), str(order["side"]), open_price, previous_close, name
        ):
            return False
        if order["side"] == "SELL":
            rows = self.database.query(
                "SELECT * FROM paper_positions WHERE strategy_id=? AND code=?",
                (order["strategy_id"], order["code"]),
            )
            if not rows or timestamp.date() <= date.fromisoformat(rows[0]["entry_time"][:10]):
                return False
        return True
    def _fill(self, order: dict[str, Any], timestamp: pd.Timestamp, open_price: float) -> dict[str, Any] | None:
        return self._fill_buy(order, timestamp, open_price) if order["side"] == "BUY" else self._fill_sell(
            order, timestamp, open_price
        )

    def _fill_buy(
        self, order: dict[str, Any], timestamp: pd.Timestamp, open_price: float
    ) -> dict[str, Any] | None:
        config = self.config.portfolio
        with self.database.connect() as connection:
            account = connection.execute(
                "SELECT * FROM paper_accounts WHERE strategy_id=?", (order["strategy_id"],)
            ).fetchone()
            own_positions = connection.execute(
                "SELECT * FROM paper_positions WHERE strategy_id=?", (order["strategy_id"],)
            ).fetchall()
            all_positions = connection.execute("SELECT * FROM paper_positions").fetchall()
            if account is None or any(position["code"] == order["code"] for position in own_positions):
                return None
            distinct_codes = {position["code"] for position in all_positions}
            if len(own_positions) >= config.max_strategy_positions:
                return None
            if order["code"] not in distinct_codes and len(distinct_codes) >= config.max_total_positions:
                return None

            strategy_equity = float(account["cash"]) + sum(
                float(position["quantity"]) * float(position["last_price"]) for position in own_positions
            )
            accounts = connection.execute("SELECT * FROM paper_accounts").fetchall()
            total_equity = sum(float(item["cash"]) for item in accounts) + sum(
                float(position["quantity"]) * float(position["last_price"]) for position in all_positions
            )
            existing_symbol_value = sum(
                float(position["quantity"]) * float(position["last_price"])
                for position in all_positions
                if position["code"] == order["code"]
            )
            symbol_capacity = max(0.0, total_equity * config.max_total_symbol_weight - existing_symbol_value)
            budget = min(
                strategy_equity * min(float(order["target_weight"]), config.max_strategy_symbol_weight),
                float(account["cash"]),
                symbol_capacity,
            )
            execution_price = open_price * (1 + config.slippage_rate)
            quantity = int(budget / execution_price / config.board_lot) * config.board_lot
            while quantity > 0:
                value = execution_price * quantity
                fees = max(config.min_commission, value * config.commission_rate)
                if value + fees <= float(account["cash"]):
                    break
                quantity -= config.board_lot
            if quantity <= 0:
                return None
            value = execution_price * quantity
            fees = max(config.min_commission, value * config.commission_rate)
            evidence_rows = connection.execute(
                "SELECT evidence, stop_price FROM signals WHERE signal_id=?", (order["signal_id"],)
            ).fetchone()
            evidence = evidence_rows["evidence"] if evidence_rows else "{}"
            stop_price = (
                float(evidence_rows["stop_price"])
                if evidence_rows and evidence_rows["stop_price"] is not None
                else execution_price * (1 - config.fixed_stop_loss)
            )
            connection.execute(
                "UPDATE paper_accounts SET cash=cash-?, updated_at=? WHERE strategy_id=?",
                (value + fees, timestamp.isoformat(), order["strategy_id"]),
            )
            connection.execute(
                """INSERT INTO paper_positions
                (strategy_id, code, quantity, average_price, entry_time, stop_price, last_price, evidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order["strategy_id"], order["code"], quantity, execution_price, timestamp.isoformat(),
                    stop_price, execution_price, evidence,
                ),
            )
            return self._record_fill(connection, order, timestamp, quantity, execution_price, fees, None)

    def _fill_sell(
        self, order: dict[str, Any], timestamp: pd.Timestamp, open_price: float
    ) -> dict[str, Any] | None:
        config = self.config.portfolio
        with self.database.connect() as connection:
            position = connection.execute(
                "SELECT * FROM paper_positions WHERE strategy_id=? AND code=?",
                (order["strategy_id"], order["code"]),
            ).fetchone()
            if position is None:
                return None
            execution_price = open_price * (1 - config.slippage_rate)
            value = execution_price * int(position["quantity"])
            fees = max(config.min_commission, value * config.commission_rate) + value * config.stamp_duty_rate
            entry_value = float(position["average_price"]) * int(position["quantity"])
            entry_fees = max(
                config.min_commission,
                entry_value * config.commission_rate,
            )
            pnl = (
                (execution_price - float(position["average_price"])) * int(position["quantity"])
                - entry_fees
                - fees
            )
            connection.execute(
                "UPDATE paper_accounts SET cash=cash+?, updated_at=? WHERE strategy_id=?",
                (value - fees, timestamp.isoformat(), order["strategy_id"]),
            )
            connection.execute(
                "DELETE FROM paper_positions WHERE strategy_id=? AND code=?",
                (order["strategy_id"], order["code"]),
            )
            return self._record_fill(
                connection, order, timestamp, int(position["quantity"]), execution_price, fees, pnl
            )

    def _record_fill(
        self,
        connection: Any,
        order: dict[str, Any],
        timestamp: pd.Timestamp,
        quantity: int,
        price: float,
        fees: float,
        pnl: float | None,
    ) -> dict[str, Any]:
        fill = {
            "fill_id": uuid4().hex,
            "order_id": order["order_id"],
            "strategy_id": order["strategy_id"],
            "code": order["code"],
            "side": order["side"],
            "timestamp": timestamp.isoformat(),
            "quantity": quantity,
            "price": price,
            "fees": fees,
            "pnl": pnl,
        }
        connection.execute(
            """INSERT INTO paper_fills
            (fill_id, order_id, strategy_id, code, side, timestamp, quantity, price, fees, pnl)
            VALUES (:fill_id, :order_id, :strategy_id, :code, :side, :timestamp, :quantity, :price, :fees, :pnl)""",
            fill,
        )
        connection.execute(
            "UPDATE paper_orders SET status='FILLED', filled_at=?, fill_price=?, quantity=? WHERE order_id=?",
            (timestamp.isoformat(), price, quantity, order["order_id"]),
        )
        connection.execute(
            "UPDATE signals SET status=? WHERE signal_id=?",
            (SignalStatus.EXECUTED.value, order["signal_id"]),
        )
        return fill
    def mark_to_market(self, bars: dict[str, pd.DataFrame]) -> None:
        with self.database.connect() as connection:
            for position in connection.execute("SELECT * FROM paper_positions").fetchall():
                frame = bars.get(position["code"])
                if frame is None or frame.empty or "Close" not in frame.columns:
                    continue
                price = float(pd.to_numeric(frame["Close"], errors="coerce").dropna().iloc[-1])
                connection.execute(
                    "UPDATE paper_positions SET last_price=? WHERE strategy_id=? AND code=?",
                    (price, position["strategy_id"], position["code"]),
                )
            for position in connection.execute("SELECT * FROM paper_group_positions").fetchall():
                frame = bars.get(position["code"])
                if frame is None or frame.empty or "Close" not in frame.columns:
                    continue
                price = float(pd.to_numeric(frame["Close"], errors="coerce").dropna().iloc[-1])
                connection.execute(
                    """UPDATE paper_group_positions SET last_price=?
                    WHERE strategy_id=? AND group_key=? AND code=?""",
                    (price, position["strategy_id"], position["group_key"], position["code"]),
                )

    def record_equity(self, timestamp: str) -> None:
        with self.database.connect() as connection:
            for account in connection.execute("SELECT * FROM paper_accounts").fetchall():
                positions = connection.execute(
                    "SELECT * FROM paper_positions WHERE strategy_id=?", (account["strategy_id"],)
                ).fetchall()
                group_positions = connection.execute(
                    "SELECT * FROM paper_group_positions WHERE strategy_id=?", (account["strategy_id"],)
                ).fetchall()
                equity = float(account["cash"]) + sum(
                    float(position["quantity"]) * float(position["last_price"]) for position in positions
                ) + sum(
                    float(position["quantity"]) * float(position["last_price"])
                    * (1 if position["side"] == "LONG" else -1)
                    for position in group_positions
                )
                connection.execute(
                    """INSERT OR REPLACE INTO paper_equity
                    (strategy_id, timestamp, equity, cash, positions) VALUES (?, ?, ?, ?, ?)""",
                    (
                        account["strategy_id"],
                        timestamp,
                        equity,
                        float(account["cash"]),
                        len(positions) + len({item["group_key"] for item in group_positions}),
                    ),
                )


def _align_timestamp(index: pd.DatetimeIndex, timestamp: pd.Timestamp) -> pd.Timestamp:
    if index.tz is None and timestamp.tzinfo is not None:
        return timestamp.tz_localize(None)
    if index.tz is not None and timestamp.tzinfo is None:
        return timestamp.tz_localize(index.tz)
    if index.tz is not None and timestamp.tzinfo is not None:
        return timestamp.tz_convert(index.tz)
    return timestamp


def _naive_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize(None) if timestamp.tzinfo is not None else timestamp
