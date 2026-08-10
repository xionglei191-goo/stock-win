from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .config import StrategyConfig
from .models import PendingOrder, PortfolioState, Position, Signal


def load_portfolio(config: StrategyConfig) -> PortfolioState:
    config.ensure_runtime_dirs()
    path = config.output_dir / "positions.json"
    if not path.exists():
        return PortfolioState(cash=config.risk.initial_cash)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return PortfolioState(
        cash=float(raw.get("cash", config.risk.initial_cash)),
        positions={code: Position(**value) for code, value in raw.get("positions", {}).items()},
        pending_orders=[PendingOrder(**value) for value in raw.get("pending_orders", [])],
        last_asof=str(raw.get("last_asof", "")),
    )


def save_portfolio(config: StrategyConfig, state: PortfolioState) -> None:
    config.ensure_runtime_dirs()
    path = config.output_dir / "positions.json"
    path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def _append_csv(path: Path, records: Iterable[dict[str, Any]]) -> None:
    rows = list(records)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    if exists:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            existing_header = next(reader, [])
        for key in existing_header:
            if key not in fieldnames:
                fieldnames.append(key)
        fieldnames = existing_header + [key for key in fieldnames if key not in existing_header]
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def append_signals(config: StrategyConfig, signals: list[Signal]) -> None:
    path = config.output_dir / "signals.csv"
    existing: set[tuple[str, str, str]] = set()
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                existing.add((row.get("timestamp", ""), row.get("code", ""), row.get("side", "")))
    rows = []
    for signal in signals:
        record = signal.as_record()
        record["timestamp"] = signal.timestamp.isoformat()
        key = (record["timestamp"], signal.code, signal.side)
        if key not in existing:
            rows.append(record)
            existing.add(key)
    _append_csv(path, rows)


def append_trades(config: StrategyConfig, trades: list[dict[str, Any]]) -> None:
    _append_csv(config.output_dir / "trades.csv", trades)


def append_equity(config: StrategyConfig, timestamp: str, equity: float, cash: float, positions: int) -> None:
    _append_csv(
        config.output_dir / "equity.csv",
        [{"timestamp": timestamp, "equity": equity, "cash": cash, "positions": positions}],
    )
