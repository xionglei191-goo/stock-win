"""Append-only store for published dragon-tiger board seat details.

Seat-name standardization and rolling win-rate profiling require a historical
detail library saved by publication date.  This store accumulates raw seat
rows exactly as disclosed: rows are content-addressed, duplicates are ignored,
and no update or delete path exists.  Seat names are kept verbatim
(``seat_name_raw``); normalization is deliberately deferred until the library
has accumulated enough history.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "lhb-seat-detail-store-v1"

_REQUIRED_FIELDS = ("exchange", "code", "trade_date", "seat_side", "seat_name_raw")
_OPTIONAL_NUMERIC = ("buy_amount", "sell_amount")
_ALLOWED_SIDES = ("buy", "sell")


class LhbSeatDetailStoreError(ValueError):
    """Raised when a seat-detail row violates the append-only contract."""


def _canonical_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "exchange": row["exchange"],
        "code": row["code"],
        "trade_date": row["trade_date"],
        "publish_date": row.get("publish_date") or "",
        "seat_side": row["seat_side"],
        "seat_rank": row.get("seat_rank"),
        "seat_name_raw": row["seat_name_raw"],
        "buy_amount": row.get("buy_amount"),
        "sell_amount": row.get("sell_amount"),
        "source": row["source"],
    }


def _content_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise LhbSeatDetailStoreError(f"row {index} is not an object")
    missing = [field for field in _REQUIRED_FIELDS if not str(row.get(field) or "").strip()]
    if missing:
        raise LhbSeatDetailStoreError(f"row {index} missing fields: {', '.join(missing)}")
    side = str(row["seat_side"])
    if side not in _ALLOWED_SIDES:
        raise LhbSeatDetailStoreError(f"row {index} has invalid seat_side {side!r}")
    for field in _OPTIONAL_NUMERIC:
        value = row.get(field)
        if value is not None and not isinstance(value, (int, float)):
            raise LhbSeatDetailStoreError(f"row {index} field {field} must be numeric or null")
    prepared = dict(_canonical_payload(row))
    prepared["captured_at"] = row.get("captured_at") or ""
    return prepared


class LhbSeatDetailStore:
    """SQLite-backed, insert-only library of dragon-tiger seat disclosures."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path))
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lhb_seat_details (
                    content_sha256 TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    publish_date TEXT NOT NULL DEFAULT '',
                    seat_side TEXT NOT NULL CHECK (seat_side IN ('buy','sell')),
                    seat_rank INTEGER,
                    seat_name_raw TEXT NOT NULL,
                    buy_amount REAL,
                    sell_amount REAL,
                    source TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lhb_seat_details_publish_date "
                "ON lhb_seat_details(publish_date)"
            )
            connection.commit()
        finally:
            connection.close()

    def record_rows(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Insert rows; content-identical duplicates are silently ignored."""

        if not isinstance(rows, list):
            raise LhbSeatDetailStoreError("rows must be a list of objects")
        captured_at = datetime.now().astimezone().isoformat()
        prepared_rows: list[tuple[Any, ...]] = []
        seen_in_batch: set[str] = set()
        for index, row in enumerate(rows):
            prepared = _validate_row(row, index)
            payload = {key: value for key, value in prepared.items() if key != "captured_at"}
            digest = _content_sha256(payload)
            if digest in seen_in_batch:
                continue
            seen_in_batch.add(digest)
            prepared_rows.append(
                (
                    digest,
                    prepared["exchange"],
                    prepared["code"],
                    prepared["trade_date"],
                    prepared["publish_date"],
                    prepared["seat_side"],
                    prepared["seat_rank"],
                    prepared["seat_name_raw"],
                    prepared["buy_amount"],
                    prepared["sell_amount"],
                    prepared["source"],
                    prepared["captured_at"] or captured_at,
                )
            )
        connection = sqlite3.connect(str(self.database_path))
        inserted = 0
        try:
            cursor = connection.cursor()
            for values in prepared_rows:
                cursor.execute(
                    "INSERT OR IGNORE INTO lhb_seat_details VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                inserted += cursor.rowcount
            connection.commit()
        finally:
            connection.close()
        return {"submitted": len(rows), "unique": len(prepared_rows), "inserted": inserted}

    def coverage(self) -> dict[str, Any]:
        connection = sqlite3.connect(str(self.database_path))
        try:
            total = connection.execute("SELECT COUNT(*) FROM lhb_seat_details").fetchone()[0]
            codes = connection.execute(
                "SELECT COUNT(DISTINCT code) FROM lhb_seat_details"
            ).fetchone()[0]
            date_bounds = connection.execute(
                "SELECT MIN(trade_date), MAX(trade_date) FROM lhb_seat_details"
            ).fetchone()
            publish_dates = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT publish_date FROM lhb_seat_details "
                    "WHERE publish_date <> '' ORDER BY publish_date"
                )
            ]
            by_source = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT source, COUNT(*) FROM lhb_seat_details GROUP BY source"
                )
            }
        finally:
            connection.close()
        return {
            "schema_version": SCHEMA_VERSION,
            "row_count": int(total),
            "distinct_codes": int(codes),
            "earliest_trade_date": date_bounds[0],
            "latest_trade_date": date_bounds[1],
            "distinct_publish_dates": len(publish_dates),
            "rows_by_source": by_source,
        }


__all__ = [
    "SCHEMA_VERSION",
    "LhbSeatDetailStore",
    "LhbSeatDetailStoreError",
]
