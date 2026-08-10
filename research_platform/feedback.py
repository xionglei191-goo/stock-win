from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

import pandas as pd

from .config import PlatformConfig
from .data import TdxProvider
from .portfolio import can_trade_at_open
from .storage import Database, ParquetSnapshotStore


class FeedbackService:
    def __init__(self, config: PlatformConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.snapshots = ParquetSnapshotStore(config, database)

    def refresh(
        self,
        *,
        bars: dict[str, pd.DataFrame] | None = None,
        names: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        rows = self.database.query(
            """SELECT d.*, s.strategy_id, s.code, s.side, s.generated_at, s.target_weight,
               s.evidence, s.status AS signal_status
            FROM signal_decisions d JOIN signals s ON s.signal_id=d.signal_id
            ORDER BY d.decision_id"""
        )
        if not rows:
            return {"evaluated": 0, "snapshot_id": None}
        codes = sorted({str(row["code"]) for row in rows})
        if bars is None:
            with TdxProvider(self.config, __file__) as provider:
                available, resolved_names = provider.list_a_shares()
                bars = provider.fetch_bars(
                    [code for code in codes if code in set(available)], "1d", 400, dividend_type="none"
                )
                names = resolved_names
        names = names or {}
        snapshot_id = f"feedback_{uuid4().hex}"
        self.snapshots.write_bars(
            snapshot_id,
            "feedback_raw",
            bars,
            {"codes": codes, "adjustment": "none", "purpose": "decision_outcomes"},
        )
        evaluated = 0
        for row in rows:
            outcome = self._evaluate(row, bars.get(str(row["code"])), names.get(str(row["code"]), ""))
            outcome.update(
                {
                    "outcome_id": uuid4().hex,
                    "decision_id": int(row["decision_id"]),
                    "signal_id": str(row["signal_id"]),
                    "evaluated_at": datetime.now().astimezone().isoformat(),
                    "snapshot_id": snapshot_id,
                }
            )
            columns = list(outcome)
            with self.database.connect() as connection:
                connection.execute("DELETE FROM decision_outcomes WHERE decision_id=?", (row["decision_id"],))
                connection.execute(
                    f"INSERT INTO decision_outcomes({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [outcome[column] for column in columns],
                )
            evaluated += 1
        return {"evaluated": evaluated, "snapshot_id": snapshot_id}

    def summary(self) -> dict[str, Any]:
        rows = self.database.query(
            """SELECT o.*, d.decision, d.reason_tags, d.ai_alignment, d.confidence,
               s.strategy_id, s.evidence
            FROM decision_outcomes o
            JOIN signal_decisions d ON d.decision_id=o.decision_id
            JOIN signals s ON s.signal_id=o.signal_id
            ORDER BY o.evaluated_at DESC"""
        )
        decoded = []
        for row in rows:
            item = dict(row)
            for key in ("reason_tags", "evidence", "details_json"):
                try:
                    item[key.removesuffix("_json")] = json.loads(str(item.get(key) or "{}"))
                except json.JSONDecodeError:
                    item[key.removesuffix("_json")] = {} if key != "reason_tags" else []
            decoded.append(item)
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for item in decoded:
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            tags = item.get("reason_tags") if isinstance(item.get("reason_tags"), list) else []
            for tag in tags or ["UNSPECIFIED"]:
                key = (
                    str(item.get("strategy_id", "")),
                    str(evidence.get("market_phase", "UNKNOWN")),
                    str(tag),
                    str(item.get("ai_alignment", "NOT_AVAILABLE")),
                )
                groups.setdefault(key, []).append(item)
        aggregates = []
        for (strategy_id, market_phase, reason_tag, alignment), items in groups.items():
            values = [float(item["return_5d"]) for item in items if item.get("return_5d") is not None]
            aggregates.append(
                {
                    "strategy_id": strategy_id,
                    "market_phase": market_phase,
                    "reason_tag": reason_tag,
                    "ai_alignment": alignment,
                    "sample_size": len(items),
                    "sufficient_sample": len(items) >= 10,
                    "average_return_5d": sum(values) / len(values) if values else None,
                    "win_rate_5d": sum(value > 0 for value in values) / len(values) if values else None,
                }
            )
        return {"rows": decoded, "aggregates": aggregates}

    def _evaluate(self, row: dict[str, Any], frame: pd.DataFrame | None, name: str) -> dict[str, Any]:
        base = {
            "basis": "ACTUAL" if row["decision"] == "APPROVED" else "COUNTERFACTUAL",
            "status": "PENDING",
            "executable": 0,
            "block_reason": "",
            "entry_time": None,
            "entry_price": None,
            "return_1d": None,
            "return_3d": None,
            "return_5d": None,
            "mae": None,
            "mfe": None,
            "realized_pnl": None,
            "details_json": "{}",
        }
        if frame is None or frame.empty:
            return base | {"status": "BLOCKED", "block_reason": "MISSING_BARS"}
        frame = frame.copy().sort_index()
        frame.index = pd.to_datetime(frame.index)
        if row["decision"] == "APPROVED":
            fills = self.database.query(
                "SELECT * FROM paper_fills WHERE order_id=? ORDER BY timestamp", (f"ord_{row['signal_id']}",)
            )
            if not fills:
                orders = self.database.query(
                    "SELECT status, block_reason FROM paper_orders WHERE signal_id=?", (row["signal_id"],)
                )
                status = str(orders[0]["status"]) if orders else "NOT_QUEUED"
                reason = str(orders[0].get("block_reason") or status) if orders else status
                return base | {"status": "UNFILLED", "block_reason": reason}
            fill = fills[0]
            entry_time = pd.Timestamp(fill["timestamp"])
            entry_price = float(fill["price"])
            quantity = int(fill["quantity"])
            buy_fee = float(fill["fees"])
            candidates = frame[frame.index >= entry_time.tz_localize(None) if entry_time.tzinfo else frame.index >= entry_time]
            realized = self._realized_pnl(row, entry_time)
        else:
            if str(row["side"]) != "BUY":
                return base | {"status": "BLOCKED", "block_reason": "COUNTERFACTUAL_BUY_ONLY"}
            generated = pd.Timestamp(row["generated_at"])
            generated = generated.tz_localize(None) if generated.tzinfo else generated
            candidates = frame[frame.index > generated]
            if candidates.empty:
                return base | {"status": "PENDING", "block_reason": "NEXT_SESSION_NOT_AVAILABLE"}
            candidates = candidates.iloc[:5]
            entry_row = candidates.iloc[0]
            entry_time = pd.Timestamp(candidates.index[0])
            previous = frame[frame.index < entry_time]
            if previous.empty:
                return base | {"status": "BLOCKED", "block_reason": "MISSING_PREVIOUS_CLOSE"}
            open_price = float(entry_row["Open"])
            previous_close = float(previous.iloc[-1]["Close"])
            if not can_trade_at_open(str(row["code"]), str(row["side"]), open_price, previous_close, name):
                return base | {"status": "UNFILLED", "block_reason": "NEXT_OPEN_NOT_TRADABLE"}
            entry_price = open_price * (1 + self.config.portfolio.slippage_rate)
            quantity = self.config.portfolio.board_lot
            buy_fee = max(
                self.config.portfolio.min_commission,
                entry_price * quantity * self.config.portfolio.commission_rate,
            )
            realized = None
        candidates = candidates.iloc[:5]
        returns = {
            horizon: self._net_return(candidates, horizon, entry_price, quantity, buy_fee)
            for horizon in (1, 3, 5)
        }
        lows = pd.to_numeric(candidates.get("Low"), errors="coerce").dropna()
        highs = pd.to_numeric(candidates.get("High"), errors="coerce").dropna()
        return base | {
            "status": "COMPLETE" if len(candidates) >= 5 else "PARTIAL",
            "executable": 1,
            "entry_time": entry_time.isoformat(),
            "entry_price": entry_price,
            "return_1d": returns[1],
            "return_3d": returns[3],
            "return_5d": returns[5],
            "mae": float(lows.min() / entry_price - 1) if not lows.empty else None,
            "mfe": float(highs.max() / entry_price - 1) if not highs.empty else None,
            "realized_pnl": realized,
            "details_json": json.dumps({"available_sessions": len(candidates)}, ensure_ascii=False),
        }

    def _net_return(
        self,
        candidates: pd.DataFrame,
        horizon: int,
        entry_price: float,
        quantity: int,
        buy_fee: float,
    ) -> float | None:
        if len(candidates) < horizon:
            return None
        close = float(candidates.iloc[horizon - 1]["Close"])
        exit_price = close * (1 - self.config.portfolio.slippage_rate)
        proceeds = exit_price * quantity
        sell_fee = max(
            self.config.portfolio.min_commission,
            proceeds * self.config.portfolio.commission_rate,
        ) + proceeds * self.config.portfolio.stamp_duty_rate
        cost = entry_price * quantity + buy_fee
        return (proceeds - sell_fee - cost) / cost

    def _realized_pnl(self, row: dict[str, Any], entry_time: pd.Timestamp) -> float | None:
        fills = self.database.query(
            """SELECT pnl FROM paper_fills WHERE strategy_id=? AND code=? AND side='SELL'
            AND timestamp>? AND pnl IS NOT NULL ORDER BY timestamp LIMIT 1""",
            (row["strategy_id"], row["code"], entry_time.isoformat()),
        )
        return float(fills[0]["pnl"]) if fills else None
