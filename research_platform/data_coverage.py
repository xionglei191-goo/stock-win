"""Coverage audit for point-in-time sector membership snapshots.

Every scan that requires sectors persists a ``sector_membership`` snapshot in
the ``data_snapshots`` table with an effective ``asof`` date.  This module
turns those rows into an auditable forward-accumulation report so gaps in the
point-in-time constituent library are visible instead of silent.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DATASET = "sector_membership"
MAX_GAP_DAYS = 7


def _effective_asof(query: dict[str, Any]) -> str:
    value = query.get("asof") or query.get("effective_asof")
    return str(value) if value else ""


def sector_membership_coverage(database_path: Path | str) -> dict[str, Any]:
    """Summarize stored sector-membership snapshots and their date gaps."""

    connection = sqlite3.connect(str(database_path))
    try:
        rows = connection.execute(
            "SELECT snapshot_id, created_at, content_hash, query_json "
            "FROM data_snapshots WHERE dataset=?",
            (DATASET,),
        ).fetchall()
    finally:
        connection.close()

    snapshots: list[dict[str, Any]] = []
    quality_counts: dict[str, int] = {}
    seen_effective: set[str] = set()
    for snapshot_id, created_at, content_hash, query_json in rows:
        try:
            query = json.loads(str(query_json or "{}"))
        except json.JSONDecodeError:
            query = {}
        if not isinstance(query, dict):
            query = {}
        effective = _effective_asof(query)
        quality = str(query.get("quality", "UNKNOWN"))
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
        if effective:
            seen_effective.add(effective)
        snapshots.append(
            {
                "snapshot_id": str(snapshot_id),
                "created_at": str(created_at),
                "effective_asof": effective,
                "quality": quality,
                "content_hash": str(content_hash),
            }
        )

    effective_dates = sorted(seen_effective)
    gaps: list[dict[str, Any]] = []
    for previous, current in zip(effective_dates, effective_dates[1:]):
        start = date.fromisoformat(previous)
        end = date.fromisoformat(current)
        delta_days = (end - start).days
        if delta_days > MAX_GAP_DAYS:
            gaps.append({"from": previous, "to": current, "calendar_days": delta_days})

    return {
        "dataset": DATASET,
        "snapshot_count": len(snapshots),
        "distinct_effective_dates": len(effective_dates),
        "earliest_effective": effective_dates[0] if effective_dates else None,
        "latest_effective": effective_dates[-1] if effective_dates else None,
        "quality_counts": quality_counts,
        "gaps_over_threshold_days": MAX_GAP_DAYS,
        "gaps": gaps,
        "snapshots": sorted(
            snapshots,
            key=lambda item: (item["effective_asof"], item["created_at"]),
        ),
    }


__all__ = ["DATASET", "MAX_GAP_DAYS", "sector_membership_coverage"]
