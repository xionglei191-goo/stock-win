from __future__ import annotations

from typing import Any

import pandas as pd


MARKET_ACTIVITY_FIELDS = (
    "SC03",
    "SC04",
    "SC15",
    "SC23",
    "SC24",
    "SC30",
    "SC31",
    "SC35",
    "SC36",
    "SC39",
)


def normalize_market_activity(raw: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    events: dict[str, dict[str, list[Any]]] = {}
    for field in MARKET_ACTIVITY_FIELDS:
        for row in raw.get(field, []):
            event_date = _date_key(row.get("Date"))
            if event_date:
                events.setdefault(event_date, {})[field] = list(row.get("Value") or [])
    rows = []
    for event_date, fields in sorted(events.items()):
        sealed_funds = _first(fields.get("SC15"))
        failed_funds = _second(fields.get("SC15"))
        rows.append(
            {
                "timestamp": pd.Timestamp(event_date),
                "limit_up_total": _first(fields.get("SC03")),
                "touched_limit_up_total": _second(fields.get("SC03")),
                "limit_down_total": _first(fields.get("SC04")),
                "touched_limit_down_total": _second(fields.get("SC04")),
                "sealed_funds": sealed_funds,
                "failed_funds": failed_funds,
                "seal_fund_success_ratio": sealed_funds / (sealed_funds + failed_funds)
                if sealed_funds + failed_funds > 0
                else None,
                "continuous_count": _second(fields.get("SC23")),
                "limit_up": _first(fields.get("SC24")),
                "limit_down": _second(fields.get("SC24")),
                "max_streak": _first(fields.get("SC30")),
                "multi_board_count": _second(fields.get("SC30")),
                "advance_count": _first(fields.get("SC31")),
                "decline_count": _second(fields.get("SC31")),
                "turnover_limit_count": _first(fields.get("SC35")),
                "reseal_rate": _second(fields.get("SC35")) / 100.0,
                "touched_limit_up": _first(fields.get("SC36")),
                "touched_limit_down": _second(fields.get("SC36")),
                "up_5_count": _first(fields.get("SC39")),
                "down_5_count": _second(fields.get("SC39")),
            }
        )
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).set_index("timestamp").sort_index()
    frame.attrs["source"] = "tdx"
    frame.attrs["available_after"] = "close"
    return frame


def flatten_market_activity(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    item = frame.copy()
    item.index.name = "timestamp"
    item = item.reset_index()
    item["timestamp"] = pd.to_datetime(item["timestamp"]).dt.date.astype(str)
    return item.to_dict("records")


def _date_key(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())[:8]
    if len(text) != 8:
        return ""
    try:
        return pd.Timestamp(text).date().isoformat()
    except ValueError:
        return ""


def _first(values: list[Any] | None) -> float:
    return _number(values[0]) if values else 0.0


def _second(values: list[Any] | None) -> float:
    return _number(values[1]) if values and len(values) > 1 else 0.0


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
