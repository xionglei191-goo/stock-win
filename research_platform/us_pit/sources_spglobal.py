from __future__ import annotations

import io
import html
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from .hashing import sha256_json
from .models import LicenseClass, SourceDependency, SourceRole, UNIVERSE_ID
from .sources import SourceAdapter, SourceArtifact, SyncRequest
from .sources_official import (
    HTTPTransport,
    RequestsHTTPTransport,
    SourceConfigurationError,
    SourceFetchError,
    SourcePolicyError,
    _header,
    _require_host,
    _require_payload,
    _sha256,
    _validate_observation_time,
)


SPGLOBAL_PRESS_HOST = "press.spglobal.com"
SPGLOBAL_PRESS_ARCHIVE_URL = "https://press.spglobal.com/index.php"
SPGLOBAL_EVENT_SOURCE_ID = "spglobal_sp500_membership_events"
SPGLOBAL_EVENT_SOURCE_VERSION = "spglobal-press-sp500-raw-v3"


@dataclass(frozen=True)
class SP500MembershipEvent:
    announced_at: datetime
    effective_date: date
    event_type: str
    company_name: str
    ticker: str


class _ArchiveLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() != "a":
            return
        self._href = next(
            (value for name, value in attrs if name.casefold() == "href" and value),
            None,
        )
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


def _clean_heading(value: Any) -> str:
    text = str(value).replace("\xa0", " ").strip().casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _announcement_time(payload: bytes) -> datetime:
    text = payload.decode("utf-8", errors="strict")
    matches = re.findall(
        r"<!--\s*ITEMDATE:\s*(\d{4}-\d{2}-\d{2})\s+"
        r"(\d{2}:\d{2}:\d{2})\s+(EST|EDT)\s*-->",
        text,
        flags=re.IGNORECASE,
    )
    if len(matches) != 1:
        raise SourceFetchError("S&P announcement must contain exactly one ITEMDATE")
    day_value, time_value, abbreviation = matches[0]
    naive = datetime.combine(
        date.fromisoformat(day_value), time.fromisoformat(time_value)
    )
    result = naive.replace(tzinfo=ZoneInfo("America/New_York"))
    expected = "EDT" if result.dst() and result.dst() != timedelta(0) else "EST"
    if abbreviation.upper() != expected:
        raise SourceFetchError("S&P announcement ITEMDATE timezone is inconsistent")
    return result


def _parse_effective_date(value: Any) -> date | None:
    text = str(value).replace("\xa0", " ").strip()
    if not text or text.casefold() in {"nan", "nat"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed).date()


def _plain_announcement_text(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="strict")
    text = re.sub(
        r"<script\b.*?</script>|<style\b.*?</style>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def _narrative_effective_dates(
    text: str, announced_at: datetime
) -> tuple[tuple[int, date], ...]:
    values: list[tuple[int, date]] = []
    pattern = re.compile(
        r"(?:effective\s+)?prior\s+to\s+the\s+"
        r"(?:open|opening)(?:\s+of\s+trading)?(?:\s+on)?\s+"
        r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday),?\s+)?"
        r"([A-Z][a-z]+\.?\s+\d{1,2}(?:,?\s+\d{4})?)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        parsed = pd.to_datetime(match.group(1), errors="coerce")
        if pd.isna(parsed):
            continue
        day = pd.Timestamp(parsed).date()
        if not re.search(r"\d{4}", match.group(1)):
            year = announced_at.year
            if day.month + 6 < announced_at.month:
                year += 1
            day = day.replace(year=year)
        values.append((match.start(), day))
    return tuple(values)


def _company_tail(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip(" ,:;-")
    for marker in (" / -- ", " -- "):
        if marker in text:
            text = text.rsplit(marker, 1)[-1]
    text = re.sub(
        r"^.*?(?:S&P\s+(?:500(?:\s+and\s+100)?|MidCap\s+400|SmallCap\s+600)\s+"
        r"constituents?\s+)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    # Multi-replacement narratives reach back into the preceding sentence;
    # keep only the trailing sentence fragment.
    if ". " in text:
        text = text.rsplit(". ", 1)[-1]
    if " will replace " in text.lower():
        text = re.split(r" will replace ", text, flags=re.IGNORECASE)[-1]
    text = re.sub(r"^(?:and|or)\s+", "", text, flags=re.IGNORECASE)
    return text.strip(" ,:;-")


def _narrative_securities(fragment: str) -> tuple[tuple[str, str], ...]:
    pattern = re.compile(
        r"(?P<company>[A-Z][^();]{0,120}?)\s*\("
        r"(?:NYSE|NASD|NASDAQ|NYSE\s+ARCA)\s*:\s*"
        r"(?P<ticker>[A-Z0-9.-]{1,16})\)",
        flags=re.IGNORECASE,
    )
    rows: list[tuple[str, str]] = []
    for match in pattern.finditer(fragment):
        company = _company_tail(match.group("company"))
        ticker = match.group("ticker").upper()
        if company:
            rows.append((company, ticker))
    return tuple(rows)


def _parse_narrative_sp500_events(
    payload: bytes, announced_at: datetime
) -> tuple[SP500MembershipEvent, ...]:
    text = _plain_announcement_text(payload)
    dates = _narrative_effective_dates(text, announced_at)
    if not dates:
        return ()

    def nearest_date(position: int) -> date:
        ranked = sorted(
            ((abs(date_position - position), date_position, day) for date_position, day in dates)
        )
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            raise SourceFetchError("S&P narrative event has ambiguous effective date")
        return ranked[0][2]

    events: list[SP500MembershipEvent] = []

    replacement = re.compile(r"\bwill\s+replace\b", flags=re.IGNORECASE)
    sp500_object = re.compile(
        r"^\s*(?P<object>.{1,220}?\((?:NYSE|NASD|NASDAQ|NYSE\s+ARCA)\s*:\s*"
        r"[A-Z0-9.-]{1,16}\))\s+in\s+the\s+S&P\s+500\b",
        flags=re.IGNORECASE,
    )
    for match in replacement.finditer(text):
        object_match = sp500_object.search(text[match.end() : match.end() + 260])
        if object_match is None:
            continue
        additions = _narrative_securities(text[max(0, match.start() - 240) : match.start()])
        removals = _narrative_securities(object_match.group("object"))
        if not additions or not removals:
            continue
        if len(removals) == 1:
            effective = nearest_date(match.start())
            for event_type, (company, ticker) in (
                ("ADD", additions[-1]), ("REMOVE", removals[0])
            ):
                events.append(
                    SP500MembershipEvent(announced_at, effective, event_type, company, ticker)
                )
            continue
        if len(additions) != len(removals):
            raise SourceFetchError("S&P narrative replacement is not one-to-one")
        # Multi-replacement narratives state the addition date before the
        # sentence and the removal date after it (e.g. Otis/Carrier joined
        # April 3 while Raytheon/Macy's were removed April 6).  Pick each
        # side's date positionally instead of a single nearest date.
        before = [value for value in dates if value[0] < match.start()]
        after = [value for value in dates if value[0] > match.end()]
        if not before or not after:
            effective = nearest_date(match.start())
            add_effective = remove_effective = effective
        else:
            add_effective, remove_effective = before[-1][1], after[0][1]
        for addition, removal in zip(additions, removals):
            events.append(
                SP500MembershipEvent(announced_at, add_effective, "ADD", addition[0], addition[1])
            )
            events.append(
                SP500MembershipEvent(announced_at, remove_effective, "REMOVE", removal[0], removal[1])
            )

    dated_group = re.compile(
        r"(?P<group>[A-Z][^.]{0,200}(?:\.[^.]{0,200}){0,3})\s+will\s+be\s+"
        r"(?P<action>added\s+to|removed\s+from)\s+the\s+S&P\s+500\s+"
        r"(?P<date_clause>(?:effective\s+)?prior\s+to\s+the\s+"
        r"(?:open|opening)(?:\s+of\s+trading)?(?:\s+on)?\s+"
        r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday),?\s+)?"
        r"[A-Z][a-z]+\.?\s+\d{1,2}(?:,?\s+\d{4})?)",
        flags=re.IGNORECASE,
    )
    for match in dated_group.finditer(text):
        securities = _narrative_securities(match.group("group"))
        if not securities:
            continue
        event_type = "ADD" if match.group("action").lower().startswith("added") else "REMOVE"
        effective = nearest_date(match.start("date_clause"))
        events.extend(
            SP500MembershipEvent(announced_at, effective, event_type, company, ticker)
            for company, ticker in securities
        )

    move = re.compile(
        r"(?P<additions>S&P\s+MidCap\s+400\s+constituents?\s+.{1,600}?)\s+"
        r"will\s+move\s+to\s+the\s+S&P\s+500,?\s+replacing\s+"
        r"(?P<removals>.{1,600}?)"
        r"(?:\s*,\s*respectively"
        r"|\s+all\s+of\s+which\s+will\s+move\s+to\s+the\s+S&P\s+(?:MidCap\s+400|SmallCap\s+600|500))",
        flags=re.IGNORECASE,
    )
    for match in move.finditer(text):
        additions = _narrative_securities(match.group("additions"))
        removals = _narrative_securities(match.group("removals"))
        if not additions or len(additions) != len(removals):
            raise SourceFetchError("S&P narrative rebalance is not explicitly paired")
        effective = nearest_date(match.start())
        events.extend(
            SP500MembershipEvent(announced_at, effective, "ADD", company, ticker)
            for company, ticker in additions
        )
        events.extend(
            SP500MembershipEvent(announced_at, effective, "REMOVE", company, ticker)
            for company, ticker in removals
        )
    deduped: dict[tuple[date, str, str], SP500MembershipEvent] = {}
    for event in events:
        key = (event.effective_date, event.event_type, event.ticker)
        deduped.setdefault(key, event)
    return tuple(deduped.values())


def _explicit_market_closure_override(payload: bytes) -> tuple[date, date] | None:
    """Return (closed_day, effective_day) only when the issuer states both.

    Some S&P tables use the scheduled rebalance date even when that date is a
    market holiday.  We never infer a roll-forward from a calendar.  The only
    permitted correction is an explicit statement in the same frozen payload
    naming both the closure and the actual pre-open effective date.
    """

    text = html.unescape(payload.decode("utf-8", errors="strict"))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    effective_match = re.search(
        r"effective\s+prior\s+to\s+the\s+open\s+of\s+trading\s+on\s+"
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday),?\s+"
        r"([A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?)",
        text,
        flags=re.IGNORECASE,
    )
    closure_match = re.search(
        r"markets?\s+will\s+be\s+closed\s+on\s+"
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday),?\s+"
        r"([A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?)",
        text,
        flags=re.IGNORECASE,
    )
    if effective_match is None or closure_match is None:
        return None
    announced_at = _announcement_time(payload)

    def parse_named_day(value: str) -> date:
        parsed = pd.to_datetime(value, errors="raise")
        result = pd.Timestamp(parsed).date()
        if not re.search(r"\d{4}", value):
            result = result.replace(year=announced_at.year)
        return result

    effective_day = parse_named_day(effective_match.group(1))
    closed_day = parse_named_day(closure_match.group(1))
    if effective_day <= closed_day:
        raise SourceFetchError("S&P explicit market-closure override is not forward causal")
    return closed_day, effective_day


def parse_sp500_membership_announcement(payload: bytes) -> tuple[SP500MembershipEvent, ...]:
    """Parse explicit S&P 500 table rows or fully specified narrative changes."""

    announced_at = _announcement_time(payload)
    closure_override = _explicit_market_closure_override(payload)
    try:
        tables = pd.read_html(io.StringIO(payload.decode("utf-8", errors="strict")))
    except (UnicodeDecodeError, ValueError):
        tables = []
    events: list[SP500MembershipEvent] = []
    for table in tables:
        if all(isinstance(column, int) for column in table.columns) and not table.empty:
            first = {_clean_heading(value) for value in table.iloc[0].tolist()}
            if {
                "effectivedate",
                "indexname",
                "action",
                "companyname",
                "ticker",
            }.issubset(first):
                table = table.copy()
                table.columns = table.iloc[0].tolist()
                table = table.iloc[1:].reset_index(drop=True)
        columns = {_clean_heading(column): column for column in table.columns}
        required = {
            "effectivedate",
            "indexname",
            "action",
            "companyname",
            "ticker",
        }
        if not required.issubset(columns):
            continue
        effective_values = table[columns["effectivedate"]].ffill()
        for ordinal, row in table.iterrows():
            index_name = str(row[columns["indexname"]]).replace("\xa0", " ").strip()
            normalized_index = re.sub(r"[^A-Z0-9]+", "", index_name.upper())
            if normalized_index not in {"SP500", "STANDARDPOORS500"}:
                continue
            action = str(row[columns["action"]]).strip().upper()
            if action not in {"ADDITION", "DELETION", "ADD", "DELETE"}:
                raise SourceFetchError("S&P 500 event table contains an unsupported action")
            effective_date = _parse_effective_date(effective_values.loc[ordinal])
            if closure_override is not None and effective_date == closure_override[0]:
                effective_date = closure_override[1]
            ticker_text = str(row[columns["ticker"]]).strip().upper()
            company = str(row[columns["companyname"]]).strip()
            tickers = tuple(item.strip() for item in ticker_text.split("/"))
            if effective_date is None and str(effective_values.loc[ordinal]).strip().upper() in {
                "TBA",
                "TO BE ANNOUNCED",
            }:
                # The page remains frozen as a probe. An undated change is not
                # executable evidence and cannot enter membership replay.
                continue
            if (
                effective_date is None
                or not company
                or company.casefold() == "nan"
                or not tickers
                or any(re.fullmatch(r"[A-Z0-9.-]{1,16}", ticker) is None for ticker in tickers)
            ):
                raise SourceFetchError(
                    "S&P 500 event table contains an invalid row: "
                    f"effective={effective_values.loc[ordinal]!r}, "
                    f"company={company!r}, ticker={ticker_text!r}"
                )
            effective_open = datetime.combine(
                effective_date, time(9, 30), ZoneInfo("America/New_York")
            )
            if announced_at >= effective_open:
                raise SourceFetchError("S&P membership event was not announced before effective open")
            for ticker in tickers:
                events.append(
                    SP500MembershipEvent(
                        announced_at=announced_at,
                        effective_date=effective_date,
                        event_type="ADD" if action in {"ADDITION", "ADD"} else "REMOVE",
                        company_name=company,
                        ticker=ticker,
                    )
                )
    if not events:
        events.extend(_parse_narrative_sp500_events(payload, announced_at))
    unique: dict[tuple[date, str, str], SP500MembershipEvent] = {}
    for item in events:
        key = (item.effective_date, item.event_type, item.ticker)
        prior = unique.get(key)
        if prior is not None and prior != item:
            raise SourceFetchError("S&P announcement contains conflicting duplicate events")
        unique[key] = item
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (item.effective_date, item.event_type, item.ticker),
        )
    )


def _archive_links(payload: bytes, *, year: int) -> tuple[str, ...]:
    text = payload.decode("utf-8", errors="strict")
    parser = _ArchiveLinkParser()
    parser.feed(text)
    result: set[str] = set()
    for href, label in parser.links:
        parsed = urlparse(href)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() != SPGLOBAL_PRESS_HOST
            or not parsed.path.startswith(f"/{year}-")
        ):
            continue
        searchable = f"{href} {label}".upper().replace("&AMP;", "&")
        if "S-P-500" in searchable or "S&P 500" in searchable:
            result.add(href)
    return tuple(sorted(result))


class SPGlobalSP500MembershipEventAdapter(SourceAdapter):
    """Capture official S&P press archive pages and explicit S&P 500 events."""

    source_id = SPGLOBAL_EVENT_SOURCE_ID
    source_version = SPGLOBAL_EVENT_SOURCE_VERSION

    def __init__(
        self,
        *,
        transport: HTTPTransport | None = None,
        timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        observation_tolerance: timedelta = timedelta(minutes=5),
        archive_endpoint: str = SPGLOBAL_PRESS_ARCHIVE_URL,
        page_size: int = 100,
        maximum_pages_per_year: int = 10,
        minimum_request_interval_seconds: float = 0.35,
        maximum_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.transport = transport or RequestsHTTPTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observation_tolerance = observation_tolerance
        self.archive_endpoint = archive_endpoint
        self.page_size = int(page_size)
        self.maximum_pages_per_year = int(maximum_pages_per_year)
        self.minimum_request_interval_seconds = float(minimum_request_interval_seconds)
        self.maximum_attempts = int(maximum_attempts)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.monotonic_clock = monotonic_clock
        self.sleeper = sleeper
        self._last_request_started: float | None = None
        if self.timeout_seconds <= 0 or self.page_size != 100:
            raise SourceConfigurationError("S&P capture requires a positive timeout and page_size=100")
        if self.maximum_pages_per_year <= 0:
            raise SourceConfigurationError("maximum_pages_per_year must be positive")
        if self.minimum_request_interval_seconds < 0:
            raise SourceConfigurationError("minimum_request_interval_seconds cannot be negative")
        if self.maximum_attempts <= 0 or self.retry_backoff_seconds < 0:
            raise SourceConfigurationError("S&P retry policy is invalid")
        _require_host(self.archive_endpoint, SPGLOBAL_PRESS_HOST, source="S&P press archive")

    def _get(self, url: str) -> tuple[bytes, str, Mapping[str, str]]:
        if self._last_request_started is not None:
            remaining = self.minimum_request_interval_seconds - (
                self.monotonic_clock() - self._last_request_started
            )
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request_started = self.monotonic_clock()
        response = None
        last_error: SourceFetchError | None = None
        for attempt in range(self.maximum_attempts):
            try:
                response = self.transport.get(
                    url,
                    headers={
                        "User-Agent": "tdx-research-platform/0.1 (local research capture)",
                        "Accept": "text/html,application/xhtml+xml",
                    },
                    timeout=self.timeout_seconds,
                )
                break
            except SourceFetchError as exc:
                last_error = exc
                if attempt + 1 >= self.maximum_attempts:
                    raise SourceFetchError(
                        f"S&P source capture failed after {self.maximum_attempts} attempts: {url}"
                    ) from exc
                self.sleeper(self.retry_backoff_seconds * (attempt + 1))
        if response is None:
            raise SourceFetchError(f"S&P source capture failed: {url}") from last_error
        final_url = response.url or url
        _require_host(final_url, SPGLOBAL_PRESS_HOST, source="S&P press archive")
        payload = _require_payload(response, source="S&P press archive")
        if b"<html" not in payload[:4096].lower() and b"<!doctype html" not in payload[:4096].lower():
            raise SourceFetchError("S&P press endpoint did not return HTML")
        return payload, final_url, response.headers

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        _validate_observation_time(
            request.observed_at,
            clock=self.clock,
            tolerance=self.observation_tolerance,
        )
        artifacts: list[SourceArtifact] = []
        candidate_urls: set[str] = set()
        for year in range(request.start_date.year, request.end_date.year + 1):
            for page in range(self.maximum_pages_per_year):
                offset = page * self.page_size
                url = f"{self.archive_endpoint}?{urlencode({'s': 2429, 'year': year, 'l': self.page_size, 'o': offset})}"
                payload, final_url, headers = self._get(url)
                all_year_links = re.findall(
                    rf'href="https://press\.spglobal\.com/{year}-',
                    payload.decode("utf-8", errors="strict"),
                )
                candidate_urls.update(_archive_links(payload, year=year))
                artifacts.append(
                    SourceArtifact(
                        dataset="membership_event_index",
                        payload=payload,
                        media_type=_header(headers, "Content-Type") or "text/html",
                        url=final_url,
                        observed_at=request.observed_at,
                        as_of_date=request.end_date,
                        published_at=request.observed_at,
                        role=SourceRole.VALIDATION_ANCHOR,
                        license_class=LicenseClass.OFFICIAL_PUBLIC,
                        metadata={
                            "artifact_kind": "raw_spglobal_press_archive_page",
                            "archive_year": year,
                            "archive_offset": offset,
                            "archive_page_size": self.page_size,
                            "archive_link_count": len(all_year_links),
                            "candidate_link_count": len(_archive_links(payload, year=year)),
                            "terminal_archive_page": len(all_year_links) < self.page_size,
                            "raw_frozen": True,
                            "response_sha256": _sha256(payload),
                        },
                    )
                )
                if len(all_year_links) < self.page_size:
                    break
            else:
                raise SourceFetchError("S&P press archive exceeded the configured page bound")

        event_count = 0
        for url in sorted(candidate_urls):
            payload, final_url, headers = self._get(url)
            try:
                events = parse_sp500_membership_announcement(payload)
                parse_status = "EXPLICIT_SP500_EVENT_TABLE" if events else "NO_SP500_EVENT_ROWS"
            except SourceFetchError as exc:
                if "no parseable HTML tables" not in str(exc):
                    raise SourceFetchError(f"{exc}; url={final_url}") from exc
                events = ()
                parse_status = "NO_PARSEABLE_EVENT_TABLE"
            announced_at: datetime | None
            try:
                announced_at = _announcement_time(payload)
            except SourceFetchError:
                announced_at = None
            artifacts.append(
                SourceArtifact(
                    dataset="membership_event_probe",
                    payload=payload,
                    media_type=_header(headers, "Content-Type") or "text/html",
                    url=final_url,
                    observed_at=request.observed_at,
                    as_of_date=request.end_date,
                    published_at=announced_at or request.observed_at,
                    role=SourceRole.VALIDATION_ANCHOR,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "artifact_kind": "raw_spglobal_sp500_candidate_announcement",
                        "parse_status": parse_status,
                        "raw_frozen": True,
                        "response_sha256": _sha256(payload),
                    },
                )
            )
            if not events:
                continue
            in_window = tuple(
                item
                for item in events
                if request.start_date <= item.effective_date <= request.end_date
            )
            if not in_window:
                continue
            announced_at = events[0].announced_at
            if any(item.announced_at != announced_at for item in events):
                raise SourceFetchError("one S&P announcement has inconsistent publication times")
            event_count += len(in_window)
            artifacts.append(
                SourceArtifact(
                    dataset="membership_events",
                    payload=payload,
                    media_type=_header(headers, "Content-Type") or "text/html",
                    url=final_url,
                    observed_at=request.observed_at,
                    as_of_date=max(item.effective_date for item in in_window),
                    published_at=announced_at,
                    role=SourceRole.SIGNAL_INPUT,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "artifact_kind": "raw_spglobal_sp500_membership_announcement",
                        "universe_id": UNIVERSE_ID,
                        "event_count": len(in_window),
                        "effective_start_date": min(
                            item.effective_date for item in in_window
                        ).isoformat(),
                        "effective_end_date": max(
                            item.effective_date for item in in_window
                        ).isoformat(),
                        "event_rows_sha256": sha256_json(
                            [
                                {
                                    "effective_date": item.effective_date.isoformat(),
                                    "event_type": item.event_type,
                                    "ticker": item.ticker,
                                    "company_name": item.company_name,
                                }
                                for item in in_window
                            ]
                        ),
                        "raw_frozen": True,
                        "publication_time_from_payload": True,
                        "eligible_for_historical_signal": True,
                        "response_sha256": _sha256(payload),
                    },
                )
            )
        if event_count == 0:
            raise SourceFetchError("S&P press archive yielded no explicit S&P 500 membership events")
        return tuple(artifacts)


__all__ = [
    "SP500MembershipEvent",
    "SPGLOBAL_EVENT_SOURCE_ID",
    "SPGLOBAL_EVENT_SOURCE_VERSION",
    "SPGLOBAL_PRESS_ARCHIVE_URL",
    "SPGlobalSP500MembershipEventAdapter",
    "parse_sp500_membership_announcement",
]
