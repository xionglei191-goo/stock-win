from __future__ import annotations

from typing import Any

import exchange_calendars as xcals
import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
)
from pandas.tseries.offsets import CustomBusinessDay


class NYSEHolidayCalendar(AbstractHolidayCalendar):
    """Regular full-day NYSE holidays used for prospective scan scheduling.

    Historical simulations use the observed SPY session index instead.  That
    preserves one-off exchange closures which no static holiday rule can infer.
    """

    rules = (
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    )


NYSE_BUSINESS_DAY = CustomBusinessDay(calendar=NYSEHolidayCalendar())


def next_nyse_session(value: Any) -> pd.Timestamp:
    day = pd.Timestamp(value).normalize()
    try:
        calendar = xcals.get_calendar("XNYS")
        if calendar.is_session(day):
            next_session = calendar.next_session(day)
        else:
            # ``next_session`` accepts only an actual session label.  Signal
            # fixtures and source observations can legitimately end on a
            # weekend/holiday, so resolve the first real session after that
            # date without pretending the closed day traded.
            next_session = calendar.date_to_session(day, direction="next")
        return pd.Timestamp(next_session).tz_localize(None).normalize()
    except Exception as exc:
        raise ValueError(f"cannot resolve the next frozen XNYS session after {day.date()}") from exc


def is_nyse_month_end(index: Any) -> bool:
    days = pd.DatetimeIndex(pd.to_datetime(index))
    if days.tz is not None:
        days = days.tz_localize(None)
    days = days.normalize()
    if days.empty:
        return False
    latest = days.max()
    return next_nyse_session(latest).to_period("M") != latest.to_period("M")


__all__ = ["NYSE_BUSINESS_DAY", "NYSEHolidayCalendar", "is_nyse_month_end", "next_nyse_session"]
