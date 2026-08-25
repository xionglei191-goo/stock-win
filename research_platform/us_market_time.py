from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


NEW_YORK = ZoneInfo("America/New_York")


def ny_session_date(value: Any) -> pd.Timestamp:
    """Return the New York calendar date that owns a market event.

    Date-only values are already exchange-calendar labels and must not be
    shifted. Timezone-aware instants are converted to America/New_York before
    their date is selected. Naive datetimes are rejected because their market
    date is ambiguous.
    """

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return pd.NaT
    if isinstance(value, pd.Timestamp):
        timestamp = value
    elif isinstance(value, datetime):
        timestamp = pd.Timestamp(value)
    elif isinstance(value, date):
        return pd.Timestamp(value).normalize()
    else:
        text = str(value).strip()
        if not text or text.casefold() in {"nat", "nan", "none", "null"}:
            return pd.NaT
        # Plain YYYY-MM-DD values are frozen XNYS labels, not UTC instants.
        if len(text) == 10:
            try:
                return pd.Timestamp(date.fromisoformat(text)).normalize()
            except ValueError:
                pass
        timestamp = pd.Timestamp(text)
    if pd.isna(timestamp):
        return pd.NaT
    if timestamp.tzinfo is None:
        if timestamp == timestamp.normalize():
            return timestamp.normalize()
        raise ValueError(f"timezone-aware market timestamp required: {value!r}")
    return timestamp.tz_convert(NEW_YORK).tz_localize(None).normalize()


def ny_session_dates(values: pd.Series) -> pd.Series:
    """Vectorized, fail-closed New York market-date conversion."""

    return values.map(ny_session_date)


def utc_instant(value: Any) -> pd.Timestamp:
    """Parse a causal publication/announcement timestamp as a UTC instant."""

    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError(f"timezone-aware causal timestamp required: {value!r}")
    return timestamp.tz_convert("UTC")


__all__ = ["NEW_YORK", "ny_session_date", "ny_session_dates", "utc_instant"]
