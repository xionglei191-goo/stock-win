from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Callable

import pandas as pd


LHB_FIELDS = ("GP02", "GP08", "GP09", "GP17", "GP18", "GP37", "GP42")
LHB_EVENT_FIELDS = ("GP02", "GP08", "GP09", "GP17", "GP18", "GP42")
LIMIT_BEHAVIOR_FIELDS = ("GP14", "GP22", "GP24", "GP25", "GP36", "GP38", "GP39", "GP40")
COURSE49_FIELDS = (*LHB_FIELDS, *LIMIT_BEHAVIOR_FIELDS)


@dataclass(frozen=True, slots=True)
class LhbFeatures:
    event_date: str
    listed: bool
    total_buy: float
    total_sell: float
    total_net: float
    net_buy_ratio: float | None
    institution_buy_count: int
    institution_sell_count: int
    institution_buy: float
    institution_sell: float
    institution_net: float
    institution_net_ratio: float | None
    branch_buy: float
    branch_sell: float
    branch_net: float
    northbound_buy: float
    northbound_sell: float
    northbound_net: float
    consecutive_list_days: int
    professional_buy_net: float
    professional_sell_net: float
    score: float
    risk: str
    confirmations: tuple[str, ...]
    limit_event: bool
    limit_amount: float
    open_board_count: int
    seal_turnover_ratio: float
    seal_float_ratio: float
    first_limit_time: str
    first_limit_score: float
    max_seal_amount: float
    max_seal_turnover_ratio: float | None
    auction_volume: float
    auction_volume_ratio: float | None
    auction_limit_buy: float
    auction_limit_buy_ratio: float | None
    year_limit_count: int
    year_premium5_count: int
    first_board_seal_rate: float
    next_day_red_rate: float
    continuation_rate: float
    last_limit_time: str
    board_quality_score: float
    board_risk: str
    board_confirmations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["confirmations"] = list(self.confirmations)
        payload["board_confirmations"] = list(self.board_confirmations)
        return payload

    def behavior_dict(self) -> dict[str, Any]:
        keys = (
            "event_date",
            "limit_event",
            "limit_amount",
            "open_board_count",
            "seal_turnover_ratio",
            "seal_float_ratio",
            "first_limit_time",
            "first_limit_score",
            "max_seal_amount",
            "max_seal_turnover_ratio",
            "auction_volume",
            "auction_volume_ratio",
            "auction_limit_buy",
            "auction_limit_buy_ratio",
            "year_limit_count",
            "year_premium5_count",
            "first_board_seal_rate",
            "next_day_red_rate",
            "continuation_rate",
            "last_limit_time",
            "board_quality_score",
            "board_risk",
        )
        payload = {key: getattr(self, key) for key in keys}
        payload["confirmations"] = list(self.board_confirmations)
        return payload


def normalize_lhb_history(
    raw: dict[str, dict[str, list[dict[str, Any]]]],
    daily_bars: dict[str, pd.DataFrame],
) -> dict[str, dict[str, LhbFeatures]]:
    result: dict[str, dict[str, LhbFeatures]] = {}
    for code, field_data in raw.items():
        events: dict[str, dict[str, list[Any]]] = {}
        for field in COURSE49_FIELDS:
            for row in field_data.get(field, []):
                event_date = _date_key(row.get("Date"))
                if event_date:
                    events.setdefault(event_date, {})[field] = list(row.get("Value") or [])
        if not events:
            continue
        frame = daily_bars.get(code)
        relevant: dict[str, LhbFeatures] = {}
        for event_date, event_fields in sorted(events.items()):
            feature = _build_features(event_date, event_fields, frame)
            if feature.listed or feature.limit_event:
                relevant[event_date] = feature
        if relevant:
            result[code] = relevant
    return result


def latest_lhb_features(
    history: dict[str, dict[str, LhbFeatures]],
    code: str,
    asof: Any,
    *,
    max_age_days: int = 10,
) -> LhbFeatures | None:
    return _latest_features(history, code, asof, max_age_days, lambda feature: feature.listed)


def latest_limit_features(
    history: dict[str, dict[str, LhbFeatures]],
    code: str,
    asof: Any,
    *,
    max_age_days: int = 0,
) -> LhbFeatures | None:
    return _latest_features(history, code, asof, max_age_days, lambda feature: feature.limit_event)


def flatten_lhb_history(
    history: dict[str, dict[str, LhbFeatures]],
    *,
    listed_only: bool = False,
    limit_only: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code, events in history.items():
        for feature in events.values():
            if listed_only and not feature.listed:
                continue
            if limit_only and not feature.limit_event:
                continue
            rows.append({"code": code, **feature.as_dict()})
    return sorted(rows, key=lambda row: (str(row["event_date"]), str(row["code"])))


def inflate_lhb_history(records: list[dict[str, Any]]) -> dict[str, dict[str, LhbFeatures]]:
    result: dict[str, dict[str, LhbFeatures]] = {}
    tuple_fields = {"confirmations", "board_confirmations"}
    boolean_fields = {"listed", "limit_event"}
    integer_fields = {
        "institution_buy_count",
        "institution_sell_count",
        "consecutive_list_days",
        "open_board_count",
        "year_limit_count",
        "year_premium5_count",
    }
    feature_fields = {item.name for item in fields(LhbFeatures)}
    for row in records:
        code = str(row.get("code", ""))
        event_date = str(row.get("event_date", ""))[:10]
        if not code or not event_date:
            continue
        payload: dict[str, Any] = {}
        for name in feature_fields:
            value = row.get(name)
            if _is_missing(value):
                value = None
            if name in tuple_fields:
                if value is None:
                    value = ()
                elif hasattr(value, "tolist"):
                    value = value.tolist()
                payload[name] = tuple(str(item) for item in value)
            elif name in boolean_fields:
                payload[name] = bool(value)
            elif name in integer_fields:
                payload[name] = int(value or 0)
            else:
                payload[name] = value
        payload["event_date"] = event_date
        result.setdefault(code, {})[event_date] = LhbFeatures(**payload)
    return result


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(missing) if not hasattr(missing, "__len__") else False


def _latest_features(
    history: dict[str, dict[str, LhbFeatures]],
    code: str,
    asof: Any,
    max_age_days: int,
    predicate: Callable[[LhbFeatures], bool],
) -> LhbFeatures | None:
    cutoff = pd.Timestamp(asof)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    cutoff = cutoff.normalize()
    candidates = []
    for event_date, feature in history.get(code, {}).items():
        event = pd.Timestamp(event_date)
        age = int((cutoff - event).days)
        if 0 <= age <= max_age_days and predicate(feature):
            candidates.append((event, feature))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _build_features(
    event_date: str,
    fields: dict[str, list[Any]],
    frame: pd.DataFrame | None,
) -> LhbFeatures:
    listed = any(field in fields for field in LHB_EVENT_FIELDS)
    total_buy, total_sell = _amount_pair(fields.get("GP02"))
    institution_sell_count, institution_sell = _count_amount_pair(fields.get("GP08"))
    institution_buy_count, institution_buy = _count_amount_pair(fields.get("GP09"))
    branch_buy, branch_sell = _amount_pair(fields.get("GP17"))
    northbound_buy, northbound_sell = _amount_pair(fields.get("GP18"))
    consecutive_list_days = int(_first(fields.get("GP37")))
    professional_buy_net, professional_sell_net = _amount_pair(fields.get("GP42"))
    turnover = _turnover_for_date(frame, event_date)
    daily_volume = _volume_for_date(frame, event_date)
    total_net = total_buy - total_sell
    institution_net = institution_buy - institution_sell
    branch_net = branch_buy - branch_sell
    northbound_net = northbound_buy - northbound_sell
    net_buy_ratio = total_net / turnover if turnover and turnover > 0 else None
    institution_net_ratio = institution_net / turnover if turnover and turnover > 0 else None

    capital_score = _capital_score(
        listed,
        net_buy_ratio,
        institution_net_ratio,
        northbound_net / turnover if turnover and turnover > 0 else None,
        consecutive_list_days,
    )
    confirmations = _capital_confirmations(
        listed,
        net_buy_ratio,
        institution_net_ratio,
        northbound_net / turnover if turnover and turnover > 0 else None,
        consecutive_list_days,
    )
    risk = ""
    if listed and institution_net_ratio is not None and institution_net_ratio <= -0.05:
        risk = "INSTITUTION_DISTRIBUTION"
    elif listed and net_buy_ratio is not None and net_buy_ratio <= -0.08:
        risk = "LHB_DISTRIBUTION"

    limit_amount = _first(fields.get("GP14")) * 10_000.0
    open_board_count = int(_second(fields.get("GP14")))
    seal_turnover_ratio = _first(fields.get("GP22"))
    seal_float_ratio = _second(fields.get("GP22"))
    first_limit_time = _time_value(_first(fields.get("GP24")))
    limit_event = limit_amount > 0 or bool(first_limit_time)
    first_limit_score = _first_limit_score(first_limit_time)
    max_seal_amount = _second(fields.get("GP24")) * 10_000.0
    max_seal_turnover_ratio = max_seal_amount / turnover if turnover and turnover > 0 else None
    auction_volume = _first(fields.get("GP25")) * 100.0
    auction_volume_ratio = auction_volume / daily_volume if daily_volume and daily_volume > 0 else None
    auction_limit_buy = _first(fields.get("GP36")) * 10_000.0
    auction_limit_buy_ratio = auction_limit_buy / turnover if turnover and turnover > 0 else None
    year_limit_count = int(_first(fields.get("GP38")))
    year_premium5_count = int(_second(fields.get("GP38")))
    first_board_seal_rate = _rate(_first(fields.get("GP39")))
    next_day_red_rate = _rate(_second(fields.get("GP39")))
    continuation_rate = _rate(_first(fields.get("GP40")))
    last_limit_time = _time_value(_second(fields.get("GP40")))
    board_quality_score = _board_quality_score(
        limit_event,
        first_limit_score,
        open_board_count,
        seal_turnover_ratio,
        seal_float_ratio,
        max_seal_turnover_ratio,
        auction_limit_buy_ratio,
        first_board_seal_rate,
        next_day_red_rate,
        continuation_rate,
    )
    board_confirmations = _board_confirmations(
        limit_event,
        first_limit_time,
        open_board_count,
        seal_float_ratio,
        max_seal_turnover_ratio,
        auction_limit_buy_ratio,
        first_board_seal_rate,
        next_day_red_rate,
    )
    board_risk = _board_risk(
        limit_event,
        first_limit_time,
        open_board_count,
        max_seal_turnover_ratio,
        first_board_seal_rate,
    )

    return LhbFeatures(
        event_date=event_date,
        listed=listed,
        total_buy=total_buy,
        total_sell=total_sell,
        total_net=total_net,
        net_buy_ratio=net_buy_ratio,
        institution_buy_count=institution_buy_count,
        institution_sell_count=institution_sell_count,
        institution_buy=institution_buy,
        institution_sell=institution_sell,
        institution_net=institution_net,
        institution_net_ratio=institution_net_ratio,
        branch_buy=branch_buy,
        branch_sell=branch_sell,
        branch_net=branch_net,
        northbound_buy=northbound_buy,
        northbound_sell=northbound_sell,
        northbound_net=northbound_net,
        consecutive_list_days=consecutive_list_days,
        professional_buy_net=professional_buy_net,
        professional_sell_net=professional_sell_net,
        score=capital_score,
        risk=risk,
        confirmations=tuple(confirmations),
        limit_event=limit_event,
        limit_amount=limit_amount,
        open_board_count=open_board_count,
        seal_turnover_ratio=seal_turnover_ratio,
        seal_float_ratio=seal_float_ratio,
        first_limit_time=first_limit_time,
        first_limit_score=first_limit_score,
        max_seal_amount=max_seal_amount,
        max_seal_turnover_ratio=max_seal_turnover_ratio,
        auction_volume=auction_volume,
        auction_volume_ratio=auction_volume_ratio,
        auction_limit_buy=auction_limit_buy,
        auction_limit_buy_ratio=auction_limit_buy_ratio,
        year_limit_count=year_limit_count,
        year_premium5_count=year_premium5_count,
        first_board_seal_rate=first_board_seal_rate,
        next_day_red_rate=next_day_red_rate,
        continuation_rate=continuation_rate,
        last_limit_time=last_limit_time,
        board_quality_score=board_quality_score,
        board_risk=board_risk,
        board_confirmations=tuple(board_confirmations),
    )


def _capital_score(
    listed: bool,
    net_buy_ratio: float | None,
    institution_net_ratio: float | None,
    northbound_net_ratio: float | None,
    consecutive_list_days: int,
) -> float:
    if not listed:
        return 0.5
    score_parts = (
        (_signed_score(net_buy_ratio, 0.15), 0.55),
        (_signed_score(institution_net_ratio, 0.08), 0.30),
        (_signed_score(northbound_net_ratio, 0.05), 0.10),
        (min(1.0, 0.5 + consecutive_list_days / 10.0), 0.05),
    )
    return float(max(0.0, min(1.0, sum(value * weight for value, weight in score_parts))))


def _capital_confirmations(
    listed: bool,
    net_buy_ratio: float | None,
    institution_net_ratio: float | None,
    northbound_net_ratio: float | None,
    consecutive_list_days: int,
) -> list[str]:
    if not listed:
        return []
    confirmations = []
    if net_buy_ratio is not None and net_buy_ratio >= 0.05:
        confirmations.append("LHB_NET_BUY")
    if institution_net_ratio is not None and institution_net_ratio >= 0.02:
        confirmations.append("INSTITUTION_BUY")
    if northbound_net_ratio is not None and northbound_net_ratio >= 0.01:
        confirmations.append("NORTHBOUND_BUY")
    if consecutive_list_days >= 2:
        confirmations.append("REPEATED_LIST")
    return confirmations


def _board_quality_score(
    limit_event: bool,
    time_score: float,
    open_board_count: int,
    seal_turnover_ratio: float,
    seal_float_ratio: float,
    max_seal_turnover_ratio: float | None,
    auction_limit_buy_ratio: float | None,
    first_board_seal_rate: float,
    next_day_red_rate: float,
    continuation_rate: float,
) -> float:
    if not limit_event:
        return 0.5
    open_score = max(0.0, 1.0 - open_board_count / 5.0)
    seal_score = max(
        min(1.0, seal_turnover_ratio / 20.0),
        min(1.0, seal_float_ratio / 2.0),
        min(1.0, (max_seal_turnover_ratio or 0.0) / 0.10),
    )
    auction_score = min(1.0, (auction_limit_buy_ratio or 0.0) / 0.03)
    score = (
        time_score * 0.25
        + open_score * 0.15
        + seal_score * 0.20
        + auction_score * 0.10
        + first_board_seal_rate * 0.10
        + next_day_red_rate * 0.10
        + continuation_rate * 0.10
    )
    return float(max(0.0, min(1.0, score)))


def _board_confirmations(
    limit_event: bool,
    first_limit_time: str,
    open_board_count: int,
    seal_float_ratio: float,
    max_seal_turnover_ratio: float | None,
    auction_limit_buy_ratio: float | None,
    first_board_seal_rate: float,
    next_day_red_rate: float,
) -> list[str]:
    if not limit_event:
        return []
    confirmations = []
    seconds = _time_seconds(first_limit_time)
    if seconds is not None and seconds <= 10 * 3600:
        confirmations.append("EARLY_SEAL")
    if open_board_count == 0:
        confirmations.append("SEALED_ONCE")
    if seal_float_ratio >= 1.0 or (max_seal_turnover_ratio or 0.0) >= 0.05:
        confirmations.append("STRONG_SEAL")
    if (auction_limit_buy_ratio or 0.0) >= 0.01:
        confirmations.append("AUCTION_STRENGTH")
    if first_board_seal_rate >= 0.60:
        confirmations.append("RELIABLE_FIRST_BOARD")
    if next_day_red_rate >= 0.65:
        confirmations.append("PREMIUM_MEMORY")
    return confirmations


def _board_risk(
    limit_event: bool,
    first_limit_time: str,
    open_board_count: int,
    max_seal_turnover_ratio: float | None,
    first_board_seal_rate: float,
) -> str:
    if not limit_event:
        return ""
    seconds = _time_seconds(first_limit_time)
    if seconds is not None and seconds >= 14 * 3600 + 30 * 60 and (max_seal_turnover_ratio or 0.0) < 0.03:
        return "LATE_WEAK_SEAL"
    if open_board_count >= 3:
        return "REPEATED_OPEN"
    if first_board_seal_rate and first_board_seal_rate < 0.30:
        return "LOW_SEAL_RELIABILITY"
    return ""


def _date_key(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())[:8]
    if len(text) != 8:
        return ""
    try:
        return pd.Timestamp(text).date().isoformat()
    except ValueError:
        return ""


def _value_for_date(frame: pd.DataFrame | None, event_date: str, column: str) -> float | None:
    if frame is None or frame.empty or column not in frame:
        return None
    days = pd.DatetimeIndex(frame.index)
    if days.tz is not None:
        days = days.tz_localize(None)
    rows = frame[days.normalize() == pd.Timestamp(event_date)]
    if rows.empty:
        return None
    values = pd.to_numeric(rows[column], errors="coerce").dropna()
    return float(values.iloc[-1]) if not values.empty else None


def _turnover_for_date(frame: pd.DataFrame | None, event_date: str) -> float | None:
    return _value_for_date(frame, event_date, "Amount")


def _volume_for_date(frame: pd.DataFrame | None, event_date: str) -> float | None:
    return _value_for_date(frame, event_date, "Volume")


def _amount_pair(values: list[Any] | None) -> tuple[float, float]:
    return _first(values) * 10_000.0, _second(values) * 10_000.0


def _count_amount_pair(values: list[Any] | None) -> tuple[int, float]:
    return int(_first(values)), _second(values) * 10_000.0


def _first(values: list[Any] | None) -> float:
    return _number(values[0]) if values else 0.0


def _second(values: list[Any] | None) -> float:
    return _number(values[1]) if values and len(values) > 1 else 0.0


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rate(value: float) -> float:
    return max(0.0, min(1.0, value / 100.0))


def _time_value(value: float) -> str:
    if value <= 0:
        return ""
    digits = str(int(value)).zfill(6)[-6:]
    return digits if _time_seconds(digits) is not None else ""


def _time_seconds(value: str) -> int | None:
    digits = "".join(character for character in str(value or "") if character.isdigit()).zfill(6)[-6:]
    if len(digits) != 6:
        return None
    hour, minute, second = int(digits[:2]), int(digits[2:4]), int(digits[4:])
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour * 3600 + minute * 60 + second


def _first_limit_score(value: str) -> float:
    seconds = _time_seconds(value)
    if seconds is None:
        return 0.5
    return max(0.0, min(1.0, (15 * 3600 - seconds) / (5.5 * 3600)))


def _signed_score(value: float | None, scale: float) -> float:
    if value is None:
        return 0.5
    return max(0.0, min(1.0, 0.5 + value / (2 * scale)))
