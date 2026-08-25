from __future__ import annotations

import hashlib
import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from time import monotonic, sleep
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

try:  # pragma: no cover - Windows deployment configuration
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

from .models import LicenseClass, SourceRole
from .sources import SourceAdapter, SourceArtifact, SyncRequest


SEC_NPORT_CIK = "0001100663"
SEC_NPORT_SERIES_ID = "S000004310"
SEC_NPORT_FORM = "NPORT-P"
SEC_NPORT_URL_TEMPLATE = (
    "https://www.sec.gov/files/dera/data/form-n-port-data-sets/{year}q{quarter}_nport.zip"
)
SEC_NPORT_SERIES_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
SEC_EDGAR_ARCHIVES_ROOT = "https://www.sec.gov/Archives/edgar/data/1100663"
ISHARES_IVV_PRODUCT_ID = "239726"
ISHARES_IVV_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/"
    "latest-holdings.csv"
)
ISHARES_IVV_PRODUCT_DATA_URL = (
    "https://www.ishares.com/varnish-api/blk-one01-product-data/"
    "product-data/api/v2/get-product-data"
)


class SourceConfigurationError(ValueError):
    """An evidence source is not configured safely enough to run."""


class SourceFetchError(RuntimeError):
    """An official response could not be captured without ambiguity."""


class SourcePolicyError(ValueError):
    """A source attempted to exceed its assigned evidence role."""


@dataclass(frozen=True)
class HTTPResponse:
    url: str
    status_code: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HTTPTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HTTPResponse:
        ...


class RequestsHTTPTransport:
    """Small injectable HTTP boundary; all callers still validate the response."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HTTPResponse:
        try:
            response = requests.get(
                url,
                headers=dict(headers),
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise SourceFetchError(f"failed to capture source response: {url}") from exc
        return HTTPResponse(
            url=response.url,
            status_code=response.status_code,
            content=response.content,
            headers={str(key): str(value) for key, value in response.headers.items()},
        )


@dataclass(frozen=True)
class MarketEvidencePayload:
    """Raw output from a read-only local market-data boundary."""

    dataset: str
    payload: bytes
    media_type: str
    url: str
    as_of_date: date | None = None
    published_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class MarketEvidenceProvider(Protocol):
    def fetch(self, request: SyncRequest) -> Iterable[MarketEvidencePayload]:
        ...


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_payload(response: HTTPResponse, *, source: str) -> bytes:
    if response.status_code != 200:
        raise SourceFetchError(
            f"{source} returned HTTP {response.status_code}; no evidence was accepted"
        )
    payload = bytes(response.content)
    if not payload:
        raise SourceFetchError(f"{source} returned an empty response")
    return payload


def _require_host(url: str, host: str, *, source: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != host.casefold():
        raise SourceFetchError(f"{source} redirected outside the official HTTPS host")


def _parse_last_modified(headers: Mapping[str, str], *, source: str) -> datetime | None:
    raw = _header(headers, "Last-Modified")
    if raw is None:
        return None
    try:
        result = parsedate_to_datetime(raw)
    except (TypeError, ValueError) as exc:
        raise SourceFetchError(f"{source} returned an invalid Last-Modified timestamp") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _validate_observation_time(
    requested: datetime,
    *,
    clock: Callable[[], datetime],
    tolerance: timedelta,
) -> None:
    now = clock()
    if now.tzinfo is None:
        raise SourceConfigurationError("source clock must return a timezone-aware timestamp")
    delta = abs(now.astimezone(timezone.utc) - requested.astimezone(timezone.utc))
    if delta > tolerance:
        raise SourcePolicyError(
            "observed_at must describe this capture run; historical backdating is forbidden"
        )


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.hrefs.append(value)


_ACCESSION_RE = re.compile(r"^(\d{10})-(\d{2})-(\d{6})$")
_ACCESSION_FOLDER_RE = re.compile(
    r"^/Archives/edgar/data/1100663/(\d{18})/[^/]+-index\.html?$",
    flags=re.IGNORECASE,
)


def _accession_from_digits(value: str) -> str:
    if not re.fullmatch(r"\d{18}", value):
        raise SourceFetchError("SEC returned an invalid accession directory")
    return f"{value[:10]}-{value[10:12]}-{value[12:]}"


def _parse_sec_series_listing(payload: bytes, *, page_url: str) -> tuple[str, ...]:
    text = payload.decode("utf-8", errors="replace")
    lowered = text.casefold()
    if "request rate threshold exceeded" in lowered or "your request originates" in lowered:
        raise SourceFetchError("SEC returned a rate-limit page instead of EDGAR results")
    if SEC_NPORT_SERIES_ID.casefold() not in lowered:
        raise SourceFetchError("SEC EDGAR results do not identify the requested IVV series")

    parser = _HrefParser()
    try:
        parser.feed(text)
    except Exception as exc:  # HTMLParser can surface malformed entity errors.
        raise SourceFetchError("SEC EDGAR results are not parseable HTML") from exc

    accessions: list[str] = []
    for raw_href in parser.hrefs:
        href = html.unescape(raw_href)
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "www.sec.gov":
            continue
        match = _ACCESSION_FOLDER_RE.fullmatch(parsed.path)
        if match is not None:
            accessions.append(_accession_from_digits(match.group(1)))
    return tuple(dict.fromkeys(accessions))


def _parse_yyyymmdd(value: bytes, *, field_name: str) -> date:
    try:
        return datetime.strptime(value.decode("ascii"), "%Y%m%d").date()
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceFetchError(f"SEC filing has an invalid {field_name}") from exc


def _parse_sec_acceptance(value: bytes) -> datetime:
    try:
        local = datetime.strptime(value.decode("ascii"), "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo("America/New_York")
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceFetchError("SEC filing has an invalid acceptance timestamp") from exc
    return local.astimezone(timezone.utc)


def _required_raw_match(payload: bytes, pattern: bytes, *, field_name: str) -> bytes:
    match = re.search(pattern, payload, flags=re.IGNORECASE)
    if match is None:
        raise SourceFetchError(f"SEC filing has no unambiguous {field_name}")
    return match.group(1).strip()


def _validate_ivv_nport_filing(
    payload: bytes,
    *,
    expected_accession: str,
    observed_at: datetime,
) -> dict[str, Any]:
    sample = payload[:16_384].lower()
    if b"<html" in sample or b"<!doctype html" in sample:
        raise SourceFetchError("SEC returned HTML instead of the complete filing text")

    accession = _required_raw_match(
        payload,
        rb"ACCESSION\s+NUMBER:\s*([^\r\n]+)",
        field_name="accession number",
    ).decode("ascii", errors="strict")
    if accession != expected_accession or _ACCESSION_RE.fullmatch(accession) is None:
        raise SourceFetchError("SEC filing accession does not match the selected filing")

    form = _required_raw_match(
        payload,
        rb"CONFORMED\s+SUBMISSION\s+TYPE:\s*([^\r\n]+)",
        field_name="submission type",
    ).decode("ascii", errors="strict")
    if form not in {"NPORT-P", "NPORT-P/A"}:
        raise SourceFetchError("SEC filing is not NPORT-P or NPORT-P/A")

    cik = _required_raw_match(
        payload,
        rb"CENTRAL\s+INDEX\s+KEY:\s*([^\r\n]+)",
        field_name="registrant CIK",
    ).decode("ascii", errors="strict")
    if cik.zfill(10) != SEC_NPORT_CIK:
        raise SourceFetchError("SEC filing registrant CIK is not iShares Trust")

    series_values: set[str] = set()
    for pattern in (
        rb"<SERIES-ID>\s*(S\d{9})",
        rb"<(?:[A-Za-z0-9_.-]+:)?seriesId(?:\s[^>]*)?>\s*(S\d{9})\s*</",
    ):
        for match in re.finditer(pattern, payload, flags=re.IGNORECASE):
            series_values.add(match.group(1).decode("ascii").upper())
    if series_values != {SEC_NPORT_SERIES_ID}:
        raise SourceFetchError(
            "SEC filing does not identify exactly the requested IVV series"
        )

    report_date = _parse_yyyymmdd(
        _required_raw_match(
            payload,
            rb"CONFORMED\s+PERIOD\s+OF\s+REPORT:\s*(\d{8})",
            field_name="report date",
        ),
        field_name="report date",
    )
    filing_date = _parse_yyyymmdd(
        _required_raw_match(
            payload,
            rb"FILED\s+AS\s+OF\s+DATE:\s*(\d{8})",
            field_name="filing date",
        ),
        field_name="filing date",
    )
    accepted_at = _parse_sec_acceptance(
        _required_raw_match(
            payload,
            rb"<ACCEPTANCE-DATETIME>\s*(\d{14})",
            field_name="acceptance timestamp",
        )
    )
    if accepted_at > observed_at.astimezone(timezone.utc):
        raise SourceFetchError("SEC filing acceptance time is after observed_at")
    accepted_date = accepted_at.astimezone(ZoneInfo("America/New_York")).date()
    # EDGAR can accept a filing after hours before the next business filing
    # date (for example Friday 2020-05-29 accepted, Monday 2020-06-01 filed).
    # Preserve both official timestamps and reject only an impossible ordering
    # or an implausibly long discrepancy.
    if not accepted_date <= filing_date <= accepted_date + timedelta(days=4):
        raise SourceFetchError("SEC filing date and acceptance timestamp disagree")
    return {
        "accession_number": accession,
        "form": form,
        "report_date": report_date,
        "filing_date": filing_date,
        "accepted_at": accepted_at,
    }


def _validate_ishares_csv(payload: bytes) -> None:
    sample = payload[:262_144].decode("utf-8-sig", errors="replace")
    lowered = sample.casefold()
    if "<html" in lowered or "<!doctype html" in lowered:
        raise SourceFetchError("iShares returned HTML instead of a holdings snapshot")
    # The current official CSV may omit CUSIP/ISIN.  That is a downstream
    # identity-quality gap, not evidence that the response is not a holdings
    # file.  Preserve the raw object and let normalization emit blocking
    # missing-identifier issues rather than discarding official evidence.
    tokens = set(re.findall(r"[a-z]+", lowered))
    has_identity_columns = bool({"cusip", "isin"} & tokens)
    has_asset_class = {"asset", "class"}.issubset(tokens)
    if "ticker" not in tokens or not (has_identity_columns or has_asset_class):
        raise SourceFetchError("iShares response does not identify a holdings CSV schema")


def _ishares_payload_as_of_date(payload: bytes) -> date:
    """Read the snapshot date from the frozen response, never from the query."""

    sample = payload[:262_144].decode("utf-8-sig", errors="replace")
    patterns = (
        r"Fund\s+Holdings\s+as\s+of\s*,?\s*\"?([^\"\r\n,]+(?:,\s*\d{4})?)",
        r"Holdings\s+as\s+of\s*,?\s*\"?([^\"\r\n,]+(?:,\s*\d{4})?)",
    )
    for pattern in patterns:
        match = re.search(pattern, sample, flags=re.IGNORECASE)
        if match is None:
            continue
        raw = match.group(1).strip().strip('"')
        for format_value in ("%b %d %Y", "%b %d, %Y", "%B %d %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw, format_value).date()
            except ValueError:
                continue
    raise SourceFetchError("iShares holdings response has no unambiguous as-of date")


def _require_sec_user_agent(explicit: str | None) -> str:
    configured = os.environ.get("SEC_USER_AGENT", "")
    if not configured and winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                configured = str(winreg.QueryValueEx(key, "SEC_USER_AGENT")[0] or "")
        except OSError:
            configured = ""
    value = (explicit if explicit is not None else configured).strip()
    if not value:
        raise SourceConfigurationError("SEC_USER_AGENT with contact information is required")
    has_contact = (
        "@" in value
        or "http://" in value
        or "https://" in value
        or re.search(r"\+?\d[\d ()-]{6,}", value) is not None
    )
    if len(value) < 8 or not has_contact:
        raise SourceConfigurationError(
            "SEC_USER_AGENT must identify the application and include contact information"
        )
    return value


class SECNPortIVVAdapter(SourceAdapter):
    """Freeze exact IVV N-PORT filings as official validation anchors.

    SEC's quarterly DERA archives contain every public N-PORT filing and are hundreds
    of megabytes each.  Merely attaching IVV identifiers to one of those archives is
    not a selection.  This adapter instead discovers filings through SEC's series
    results for ``S000004310``, freezes each complete EDGAR submission, and verifies
    the registrant CIK and series again inside the submission itself.

    N-PORT becomes public after the reported period.  These filings therefore remain
    validation anchors only: this adapter never reconstructs historical membership or
    claims that an accepted filing was known on its report date.
    """

    source_id = "sec_nport_ivv"
    source_version = "sec-edgar-ivv-nport-raw-v2"

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        transport: HTTPTransport | None = None,
        timeout_seconds: float = 120.0,
        clock: Callable[[], datetime] | None = None,
        observation_tolerance: timedelta = timedelta(minutes=5),
        browse_endpoint: str = SEC_NPORT_SERIES_BROWSE_URL,
        archives_root: str = SEC_EDGAR_ARCHIVES_ROOT,
        page_size: int = 100,
        max_listing_pages: int = 100,
        minimum_request_interval_seconds: float = 0.11,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.user_agent = _require_sec_user_agent(user_agent)
        self.transport = transport or RequestsHTTPTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observation_tolerance = observation_tolerance
        self.browse_endpoint = browse_endpoint
        self.archives_root = archives_root.rstrip("/")
        self.page_size = int(page_size)
        self.max_listing_pages = int(max_listing_pages)
        self.minimum_request_interval_seconds = float(minimum_request_interval_seconds)
        self.monotonic_clock = monotonic_clock
        self.sleeper = sleeper
        self._last_request_started: float | None = None
        if self.timeout_seconds <= 0:
            raise SourceConfigurationError("timeout_seconds must be positive")
        if not (1 <= self.page_size <= 100):
            raise SourceConfigurationError("SEC page_size must be between 1 and 100")
        if self.max_listing_pages <= 0:
            raise SourceConfigurationError("max_listing_pages must be positive")
        if self.minimum_request_interval_seconds < 0:
            raise SourceConfigurationError(
                "minimum_request_interval_seconds must not be negative"
            )
        _require_host(self.browse_endpoint, "www.sec.gov", source="SEC EDGAR")
        _require_host(self.archives_root, "www.sec.gov", source="SEC EDGAR")

    def _get(self, url: str, *, headers: Mapping[str, str]) -> HTTPResponse:
        if self._last_request_started is not None:
            elapsed = self.monotonic_clock() - self._last_request_started
            remaining = self.minimum_request_interval_seconds - elapsed
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request_started = self.monotonic_clock()
        return self.transport.get(
            url,
            headers=headers,
            timeout=self.timeout_seconds,
        )

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        _validate_observation_time(
            request.observed_at,
            clock=self.clock,
            tolerance=self.observation_tolerance,
        )
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html, text/plain, application/octet-stream",
            "Accept-Encoding": "gzip, deflate",
        }
        artifacts: list[SourceArtifact] = []
        accessions: list[str] = []
        for page_number in range(self.max_listing_pages):
            start = page_number * self.page_size
            query = urlencode(
                {
                    "action": "getcompany",
                    "CIK": SEC_NPORT_SERIES_ID,
                    "type": SEC_NPORT_FORM,
                    "dateb": request.observed_at.date().strftime("%Y%m%d"),
                    "owner": "exclude",
                    "count": str(self.page_size),
                    "start": str(start),
                }
            )
            url = f"{self.browse_endpoint}?{query}"
            response = self._get(url, headers=headers)
            _require_host(response.url or url, "www.sec.gov", source="SEC EDGAR")
            payload = _require_payload(response, source="SEC EDGAR series results")
            page_accessions = _parse_sec_series_listing(
                payload,
                page_url=response.url or url,
            )
            artifacts.append(
                SourceArtifact(
                    dataset="fund_holdings_observed",
                    payload=payload,
                    media_type=_header(response.headers, "Content-Type") or "text/html",
                    url=response.url or url,
                    observed_at=request.observed_at,
                    published_at=_parse_last_modified(
                        response.headers,
                        source="SEC EDGAR series results",
                    ),
                    role=SourceRole.VALIDATION_ANCHOR,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "artifact_kind": "edgar_series_listing",
                        "registrant_cik": SEC_NPORT_CIK,
                        "series_id": SEC_NPORT_SERIES_ID,
                        "form_filter": SEC_NPORT_FORM,
                        "page_start": start,
                        "page_size": self.page_size,
                        "listed_accession_count": len(page_accessions),
                        "selection_applied": True,
                        "membership_reconstruction_performed": False,
                        "evidence_policy": "discovery_only",
                        "response_sha256": _sha256(payload),
                        "http_etag": _header(response.headers, "ETag"),
                        "http_last_modified": _header(response.headers, "Last-Modified"),
                        "sec_user_agent_configured": True,
                    },
                )
            )
            new_accessions = [item for item in page_accessions if item not in accessions]
            if page_accessions and not new_accessions:
                raise SourceFetchError("SEC EDGAR pagination repeated a prior page")
            accessions.extend(new_accessions)
            if len(page_accessions) < self.page_size:
                break
        else:
            raise SourceFetchError("SEC EDGAR results exceeded the pagination safety limit")

        if not accessions:
            raise SourceFetchError("SEC EDGAR returned no N-PORT filings for the IVV series")

        selected_count = 0
        for accession in accessions:
            match = _ACCESSION_RE.fullmatch(accession)
            if match is None:
                raise SourceFetchError("SEC returned an invalid accession number")
            accession_digits = "".join(match.groups())
            url = f"{self.archives_root}/{accession_digits}/{accession}.txt"
            response = self._get(url, headers=headers)
            _require_host(response.url or url, "www.sec.gov", source="SEC EDGAR")
            payload = _require_payload(response, source="SEC EDGAR complete filing")
            filing = _validate_ivv_nport_filing(
                payload,
                expected_accession=accession,
                observed_at=request.observed_at,
            )
            report_date = filing["report_date"]
            if not (request.start_date <= report_date <= request.end_date):
                continue
            selected_count += 1
            artifacts.append(
                SourceArtifact(
                    dataset="fund_holdings_observed",
                    payload=payload,
                    media_type=_header(response.headers, "Content-Type") or "text/plain",
                    url=response.url or url,
                    observed_at=request.observed_at,
                    published_at=filing["accepted_at"],
                    as_of_date=report_date,
                    role=SourceRole.VALIDATION_ANCHOR,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "artifact_kind": "raw_complete_edgar_submission",
                        "form": filing["form"],
                        "registrant_cik": SEC_NPORT_CIK,
                        "series_id": SEC_NPORT_SERIES_ID,
                        "accession_number": filing["accession_number"],
                        "report_date": report_date.isoformat(),
                        "filing_date": filing["filing_date"].isoformat(),
                        "accepted_at": filing["accepted_at"].isoformat(),
                        "raw_frozen": True,
                        "selection_applied": True,
                        "series_id_verified_in_payload": True,
                        "membership_reconstruction_performed": False,
                        "eligible_for_historical_signal": False,
                        "evidence_policy": "validation_anchor_only",
                        "response_sha256": _sha256(payload),
                        "http_etag": _header(response.headers, "ETag"),
                        "http_last_modified": _header(response.headers, "Last-Modified"),
                        "sec_user_agent_configured": True,
                    },
                )
            )
        if selected_count == 0:
            raise SourceFetchError(
                "SEC EDGAR has no verified IVV N-PORT filing in the requested report window"
            )
        return tuple(artifacts)


class ISharesIVVObservedSnapshotAdapter(SourceAdapter):
    """Capture IVV holdings exactly as observed from the official iShares endpoint.

    With ``historical_as_of_dates`` the endpoint is a reconciliation probe only:
    its response does not prove when that historical representation was published.
    Without those dates the latest response is frozen and may only be used from the
    recorded ``observed_at`` onward.
    """

    source_id = "ishares_ivv_holdings"
    source_version = "ishares-ivv-observed-raw-v1"

    def __init__(
        self,
        *,
        historical_as_of_dates: Iterable[date] | None = None,
        transport: HTTPTransport | None = None,
        timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        observation_tolerance: timedelta = timedelta(minutes=5),
        endpoint: str = ISHARES_IVV_HOLDINGS_URL,
    ) -> None:
        self.historical_as_of_dates = (
            None
            if historical_as_of_dates is None
            else tuple(sorted(set(historical_as_of_dates)))
        )
        self.transport = transport or RequestsHTTPTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observation_tolerance = observation_tolerance
        self.endpoint = endpoint
        if self.timeout_seconds <= 0:
            raise SourceConfigurationError("timeout_seconds must be positive")
        _require_host(self.endpoint, "www.ishares.com", source="iShares IVV")

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        _validate_observation_time(
            request.observed_at,
            clock=self.clock,
            tolerance=self.observation_tolerance,
        )
        historical = self.historical_as_of_dates is not None
        dates: tuple[date | None, ...]
        if historical:
            dates = tuple(self.historical_as_of_dates or ())
            if not dates:
                raise SourcePolicyError("historical snapshot mode requires at least one date")
        else:
            dates = (None,)

        artifacts: list[SourceArtifact] = []
        for snapshot_date in dates:
            if snapshot_date is not None and not (
                request.start_date <= snapshot_date <= request.end_date
            ):
                raise SourcePolicyError("historical iShares snapshot is outside the sync window")
            query: dict[str, str] = {}
            if snapshot_date is not None:
                query["asOfDate"] = snapshot_date.strftime("%Y%m%d")
            url = (
                f"{self.endpoint}?{urlencode(query)}" if query else self.endpoint
            )
            response = self.transport.get(
                url,
                headers={
                    "User-Agent": "tdx-research-platform/0.1 (local research capture)",
                    "Accept": "text/csv, application/csv, application/octet-stream",
                },
                timeout=self.timeout_seconds,
            )
            _require_host(response.url or url, "www.ishares.com", source="iShares IVV")
            payload = _require_payload(response, source="iShares IVV")
            _validate_ishares_csv(payload)
            payload_as_of_date = _ishares_payload_as_of_date(payload)
            observed_new_york_date = request.observed_at.astimezone(
                ZoneInfo("America/New_York")
            ).date()
            if payload_as_of_date > observed_new_york_date:
                raise SourceFetchError(
                    "iShares holdings as-of date is after the observation date"
                )
            if not historical and (
                observed_new_york_date - payload_as_of_date
            ) > timedelta(days=10):
                raise SourceFetchError(
                    "iShares current holdings response is too stale for a causal snapshot"
                )
            if snapshot_date is not None and payload_as_of_date != snapshot_date:
                raise SourceFetchError(
                    "iShares response as-of date does not match the requested historical date"
                )
            http_published_at = _parse_last_modified(
                response.headers, source="iShares IVV"
            )
            # The current response is usable no earlier than the first actual
            # observation.  ``observed_at`` is therefore the conservative
            # publication boundary when HTTP metadata is absent or older; it
            # must never be used to backdate a historical response.
            published_at = (
                http_published_at
                if historical
                else request.observed_at
            )
            role = (
                SourceRole.VALIDATION_ANCHOR if historical else SourceRole.SIGNAL_INPUT
            )
            artifacts.append(
                SourceArtifact(
                    dataset="fund_holdings_observed",
                    payload=payload,
                    media_type=_header(response.headers, "Content-Type") or "text/csv",
                    url=response.url or url,
                    observed_at=request.observed_at,
                    as_of_date=payload_as_of_date,
                    published_at=published_at,
                    role=role,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "fund_ticker": "IVV",
                        "product_id": ISHARES_IVV_PRODUCT_ID,
                        "artifact_kind": "raw_observed_holdings_csv",
                        "raw_frozen": True,
                        "observation_mode": (
                            "historical_as_of_reconciliation" if historical else "current"
                        ),
                        "historical_publication_time_proven": False,
                        "eligible_for_historical_signal": not historical,
                        "eligible_from": (
                            None if historical else request.observed_at.isoformat()
                        ),
                        "availability_basis": (
                            "historical-publication-unproven"
                            if historical
                            else "first-local-observation"
                        ),
                        "membership_reconstruction_performed": False,
                        "response_sha256": _sha256(payload),
                        "http_etag": _header(response.headers, "ETag"),
                        "http_last_modified": _header(response.headers, "Last-Modified"),
                    },
                )
            )
        return tuple(artifacts)


def _ishares_product_data_points(payload: bytes, *, requested_date: date) -> tuple[Mapping[str, Any], int]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceFetchError("iShares product-data response is not valid JSON") from exc
    if not isinstance(document, dict) or str(document.get("productId")) != ISHARES_IVV_PRODUCT_ID:
        raise SourceFetchError("iShares product-data response has the wrong product id")
    try:
        points = document["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]["dataPointsByNameMap"]
    except (KeyError, TypeError) as exc:
        raise SourceFetchError("iShares product-data response has no holdings.all data") from exc
    if not isinstance(points, dict):
        raise SourceFetchError("iShares product-data holdings payload has the wrong shape")
    as_of = points.get("asOfDate")
    if not isinstance(as_of, dict) or str(as_of.get("value")) != requested_date.strftime("%Y%m%d"):
        raise SourceFetchError(
            "iShares product-data as-of date does not match the requested historical date"
        )
    required = (
        "ticker",
        "issueName",
        "assetClass",
        "cusip",
        "isin",
        "exchange",
        "currencyCode",
        "unitsHeld",
        "marketValue",
        "holdingPercent",
    )
    lengths: set[int] = set()
    for name in required:
        point = points.get(name)
        if not isinstance(point, dict):
            raise SourceFetchError(f"iShares product-data holdings is missing {name}")
        values = point.get("value")
        formatted = point.get("formattedValue")
        if not isinstance(values, list) or not isinstance(formatted, list):
            raise SourceFetchError(f"iShares product-data {name} is not a parallel array")
        if len(values) != len(formatted):
            raise SourceFetchError(f"iShares product-data {name} arrays disagree")
        lengths.add(len(values))
    if len(lengths) != 1:
        raise SourceFetchError("iShares product-data holdings arrays have different lengths")
    row_count = next(iter(lengths), 0)
    if row_count < 400:
        raise SourceFetchError("iShares product-data response has implausibly few holdings")
    return points, row_count


class ISharesIVVHistoricalReconciliationAdapter(SourceAdapter):
    """Freeze exact-date official IVV holdings for late-observed reconciliation only.

    The product-data API exposes identifiers that are absent from the current CSV,
    but observing it today cannot prove when a historical representation was first
    published.  These objects therefore remain validation anchors forever.
    """

    source_id = "ishares_ivv_holdings_api"
    source_version = "ishares-ivv-product-data-raw-v1"

    def __init__(
        self,
        historical_as_of_dates: Iterable[date],
        *,
        transport: HTTPTransport | None = None,
        timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        observation_tolerance: timedelta = timedelta(minutes=5),
        endpoint: str = ISHARES_IVV_PRODUCT_DATA_URL,
    ) -> None:
        self.historical_as_of_dates = tuple(sorted(set(historical_as_of_dates)))
        if not self.historical_as_of_dates:
            raise SourceConfigurationError("at least one historical iShares date is required")
        self.transport = transport or RequestsHTTPTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observation_tolerance = observation_tolerance
        self.endpoint = endpoint
        if self.timeout_seconds <= 0:
            raise SourceConfigurationError("timeout_seconds must be positive")
        _require_host(self.endpoint, "www.ishares.com", source="iShares IVV product data")

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        _validate_observation_time(
            request.observed_at,
            clock=self.clock,
            tolerance=self.observation_tolerance,
        )
        artifacts: list[SourceArtifact] = []
        last_request_started: float | None = None
        for snapshot_date in self.historical_as_of_dates:
            if not request.start_date <= snapshot_date <= request.end_date:
                raise SourcePolicyError("historical iShares snapshot is outside the sync window")
            query = {
                "appSubType": "ISHARES",
                "appType": "PRODUCT_PAGE",
                "component": "holdings.all",
                "locale": "en_US",
                "portfolioId": ISHARES_IVV_PRODUCT_ID,
                "targetSite": "us-ishares",
                "userType": "individual",
                "excludeContent": "true",
                "asOfDate": snapshot_date.strftime("%Y%m%d"),
                "includeConfig": "true",
            }
            url = f"{self.endpoint}?{urlencode(query)}"
            if last_request_started is not None:
                remaining = 0.10 - (monotonic() - last_request_started)
                if remaining > 0:
                    sleep(remaining)
            last_request_started = monotonic()
            response = self.transport.get(
                url,
                headers={
                    "User-Agent": "tdx-research-platform/0.1 (local research capture)",
                    "Accept": "application/json",
                },
                timeout=self.timeout_seconds,
            )
            _require_host(
                response.url or url,
                "www.ishares.com",
                source="iShares IVV product data",
            )
            payload = _require_payload(response, source="iShares IVV product data")
            _points, row_count = _ishares_product_data_points(
                payload, requested_date=snapshot_date
            )
            artifacts.append(
                SourceArtifact(
                    dataset="fund_holdings_observed",
                    payload=payload,
                    media_type=_header(response.headers, "Content-Type") or "application/json",
                    url=response.url or url,
                    observed_at=request.observed_at,
                    as_of_date=snapshot_date,
                    # This is the first defensible availability boundary, not a
                    # claim about historical publication.
                    published_at=request.observed_at,
                    role=SourceRole.VALIDATION_ANCHOR,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "fund_ticker": "IVV",
                        "product_id": ISHARES_IVV_PRODUCT_ID,
                        "artifact_kind": "raw_historical_holdings_product_data_json",
                        "raw_frozen": True,
                        "row_count": row_count,
                        "response_as_of_date": snapshot_date.isoformat(),
                        "observation_mode": "historical_as_of_reconciliation",
                        "historical_publication_time_proven": False,
                        "eligible_for_historical_signal": False,
                        "eligible_from": None,
                        "availability_basis": "first-local-observation-historical-reconciliation",
                        "membership_reconstruction_performed": False,
                        "response_sha256": _sha256(payload),
                        "http_etag": _header(response.headers, "ETag"),
                        "http_last_modified": _header(response.headers, "Last-Modified"),
                    },
                )
            )
        return tuple(artifacts)


class TDXUSMarketEvidenceAdapter(SourceAdapter):
    """Read-only TDX evidence boundary for raw/front bars; never membership data."""

    source_id = "tdx_us_market"

    def __init__(self, provider: MarketEvidenceProvider, *, source_version: str) -> None:
        if not source_version.strip():
            raise SourceConfigurationError("TDX source_version is required")
        self.provider = provider
        self.source_version = source_version.strip()

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        values = tuple(self.provider.fetch(request))
        if not values:
            raise SourceFetchError("TDX returned no market evidence")
        artifacts: list[SourceArtifact] = []
        for value in values:
            if value.dataset not in {"bars_raw", "bars_vendor_front"}:
                raise SourcePolicyError(
                    "TDX market adapter may emit only bars_raw or bars_vendor_front"
                )
            if not value.payload:
                raise SourceFetchError("TDX returned an empty market payload")
            parsed = urlparse(value.url)
            if parsed.scheme == "tdx":
                pass
            elif parsed.scheme in {"http", "https"} and parsed.hostname in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                pass
            else:
                raise SourcePolicyError("TDX evidence URL must identify the local read-only service")
            metadata = dict(value.metadata)
            metadata.update(
                {
                    "authority": "TDX_PRIMARY_MARKET_DATA",
                    "read_only": True,
                    "membership_authority": False,
                    "corporate_action_authority": False,
                    "overwrite_policy": "content_addressed_append_only",
                    "response_sha256": _sha256(value.payload),
                }
            )
            artifacts.append(
                SourceArtifact(
                    dataset=value.dataset,
                    payload=bytes(value.payload),
                    media_type=value.media_type,
                    url=value.url,
                    observed_at=request.observed_at,
                    as_of_date=value.as_of_date,
                    published_at=value.published_at,
                    role=SourceRole.SIGNAL_INPUT,
                    license_class=LicenseClass.LOCAL_VENDOR,
                    metadata=metadata,
                )
            )
        return tuple(artifacts)


class AKShareUSCrossCheckAdapter(SourceAdapter):
    """Pinned AKShare evidence that can diagnose differences but cannot replace TDX."""

    source_id = "akshare_us_cross_check"

    def __init__(self, provider: MarketEvidenceProvider, *, package_version: str) -> None:
        version = package_version.strip()
        if not version or version.casefold() in {"latest", "unknown", "unversioned"}:
            raise SourceConfigurationError("a pinned AKShare package_version is required")
        if re.search(r"\d", version) is None:
            raise SourceConfigurationError("AKShare package_version must contain a version number")
        self.provider = provider
        self.package_version = version
        self.source_version = f"akshare-{version}"

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        values = tuple(self.provider.fetch(request))
        if not values:
            raise SourceFetchError("AKShare returned no cross-check evidence")
        artifacts: list[SourceArtifact] = []
        for value in values:
            if value.dataset != "bars_cross_check":
                raise SourcePolicyError(
                    "AKShare may emit only bars_cross_check and cannot overwrite TDX artifacts"
                )
            if not value.payload:
                raise SourceFetchError("AKShare returned an empty cross-check payload")
            metadata = dict(value.metadata)
            metadata.update(
                {
                    "authority": "CROSS_CHECK_ONLY",
                    "read_only": True,
                    "package_version": self.package_version,
                    "may_override_tdx": False,
                    "eligible_for_signal": False,
                    "membership_authority": False,
                    "corporate_action_authority": False,
                    "response_sha256": _sha256(value.payload),
                }
            )
            artifacts.append(
                SourceArtifact(
                    dataset="bars_cross_check",
                    payload=bytes(value.payload),
                    media_type=value.media_type,
                    url=value.url,
                    observed_at=request.observed_at,
                    as_of_date=value.as_of_date,
                    published_at=value.published_at,
                    role=SourceRole.CROSS_CHECK,
                    license_class=LicenseClass.PERMISSIVE,
                    metadata=metadata,
                )
            )
        return tuple(artifacts)


# A conventional spelling for callers that do not preserve the iShares brand casing.
IsharesIVVObservedSnapshotAdapter = ISharesIVVObservedSnapshotAdapter


__all__ = [
    "AKShareUSCrossCheckAdapter",
    "HTTPResponse",
    "HTTPTransport",
    "ISHARES_IVV_HOLDINGS_URL",
    "ISHARES_IVV_PRODUCT_DATA_URL",
    "ISHARES_IVV_PRODUCT_ID",
    "ISharesIVVHistoricalReconciliationAdapter",
    "ISharesIVVObservedSnapshotAdapter",
    "IsharesIVVObservedSnapshotAdapter",
    "MarketEvidencePayload",
    "MarketEvidenceProvider",
    "RequestsHTTPTransport",
    "SEC_NPORT_CIK",
    "SEC_EDGAR_ARCHIVES_ROOT",
    "SEC_NPORT_FORM",
    "SEC_NPORT_SERIES_ID",
    "SEC_NPORT_SERIES_BROWSE_URL",
    "SEC_NPORT_URL_TEMPLATE",
    "SECNPortIVVAdapter",
    "SourceConfigurationError",
    "SourceFetchError",
    "SourcePolicyError",
    "TDXUSMarketEvidenceAdapter",
]
