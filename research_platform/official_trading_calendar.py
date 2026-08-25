from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import requests


PROTOCOL_VERSION = "cn-official-trading-calendar-evidence-v2"
SOURCE_CONTRACT_ADMITTED = "SOURCE_CONTRACT_ADMITTED"
SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"
SOURCE_REJECTED = "SOURCE_REJECTED"
EVIDENCE_COMPLETE = "OFFICIAL_TRADING_CALENDAR_EVIDENCE_COMPLETE"

START_YEAR = 2017
END_YEAR = 2023
EXCHANGES = ("SSE", "SZSE")
JSON_MEDIA_TYPE = "application/json"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CATALOG_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_SPAN = timedelta(hours=2)
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))

CATALOG_KEYWORD = "休市安排"
CATALOG_START = "2016-12-01"
CATALOG_END = "2023-12-25"
CATALOG_PAGE_SIZE = 50
EXPECTED_CATALOG_ENTRY_COUNT = 47
EXPECTED_CATALOG_MEMBERSHIP_SHA256: Mapping[str, str] = {
    "SSE": "c051426642092272d6c168204e9223553b9ce4251975c7dd9607b9aa03922d83",
    "SZSE": "ccbc1069ac8ca0554588ff6217172807a43399a78a80bb7429a1bf0fc2dd5a06",
}
SSE_CATALOG_URL = "https://query.sse.com.cn/search/getESSearchDoc.do"
SZSE_CATALOG_URL = "https://www.szse.cn/api/search/content"

EXPECTED_OPEN_DAYS_BY_YEAR: Mapping[int, int] = {
    2017: 244,
    2018: 244,
    2019: 244,
    2020: 243,
    2021: 243,
    2022: 242,
    2023: 242,
}


class OfficialTradingCalendarBlockedError(RuntimeError):
    """Official evidence cannot be admitted as a historical trading calendar."""

    def __init__(self, message: str, *, status: str = SOURCE_REJECTED) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CalendarSourceSpec:
    source_id: str
    exchange: str
    year: int
    kind: str
    request_url: str
    page_url: str
    document_id: str
    publication_date: str
    title: str
    expected_intervals: tuple[tuple[str, str], ...]
    required_markers: tuple[str, ...]


@dataclass(frozen=True)
class CalendarCatalogSpec:
    source_id: str
    exchange: str
    method: str
    request_url: str
    referer_url: str
    request_params: tuple[tuple[str, str], ...]
    expected_entry_count: int


def _catalog_epoch_millis(value: str, *, end_of_day: bool = False) -> str:
    parsed = datetime.combine(
        date.fromisoformat(value),
        datetime.max.time() if end_of_day else datetime.min.time(),
        tzinfo=CHINA_STANDARD_TIME,
    )
    if end_of_day:
        parsed = parsed.replace(microsecond=0)
    return str(int(parsed.timestamp() * 1000))


CATALOG_SPECS: tuple[CalendarCatalogSpec, ...] = (
    CalendarCatalogSpec(
        source_id="SSE_CATALOG_2017_2023",
        exchange="SSE",
        method="GET",
        request_url=SSE_CATALOG_URL,
        referer_url="https://www.sse.com.cn/home/search/",
        request_params=(
            ("keyword", CATALOG_KEYWORD),
            ("spaceId", "3"),
            ("siteName", "sse"),
            ("keywordPosition", "title"),
            ("page", "0"),
            ("limit", str(CATALOG_PAGE_SIZE)),
            ("publishTimeStart", f"{CATALOG_START} 00:00:00"),
            ("publishTimeEnd", f"{CATALOG_END} 23:59:59"),
            ("channelId", "10001"),
            ("channelCode", "8319"),
            ("searchMode", "preciseMulti"),
        ),
        expected_entry_count=EXPECTED_CATALOG_ENTRY_COUNT,
    ),
    CalendarCatalogSpec(
        source_id="SZSE_CATALOG_2017_2023",
        exchange="SZSE",
        method="POST",
        request_url=SZSE_CATALOG_URL,
        referer_url="https://www.szse.cn/disclosure/notice/general/index.html",
        request_params=(
            ("keyword", CATALOG_KEYWORD),
            ("time", _catalog_epoch_millis(CATALOG_START)),
            ("endTime", _catalog_epoch_millis(CATALOG_END, end_of_day=True)),
            ("range", "title"),
            ("channelCode[]", "general_news"),
            ("currentPage", "1"),
            ("pageSize", str(CATALOG_PAGE_SIZE)),
            ("scope", "1"),
        ),
        expected_entry_count=EXPECTED_CATALOG_ENTRY_COUNT,
    ),
)


def _annual_specs() -> tuple[CalendarSourceSpec, ...]:
    intervals: dict[int, tuple[tuple[str, str], ...]] = {
        2017: (
            ("2017-01-01", "2017-01-02"),
            ("2017-01-27", "2017-02-02"),
            ("2017-04-02", "2017-04-04"),
            ("2017-04-29", "2017-05-01"),
            ("2017-05-28", "2017-05-30"),
            ("2017-10-01", "2017-10-08"),
        ),
        2018: (
            ("2018-01-01", "2018-01-01"),
            ("2018-02-15", "2018-02-21"),
            ("2018-04-05", "2018-04-07"),
            ("2018-04-29", "2018-05-01"),
            ("2018-06-16", "2018-06-18"),
            ("2018-09-22", "2018-09-24"),
            ("2018-10-01", "2018-10-07"),
        ),
        2019: (
            ("2018-12-30", "2019-01-01"),
            ("2019-02-04", "2019-02-10"),
            ("2019-04-05", "2019-04-07"),
            ("2019-05-01", "2019-05-01"),
            ("2019-06-07", "2019-06-09"),
            ("2019-09-13", "2019-09-15"),
            ("2019-10-01", "2019-10-07"),
        ),
        2020: (
            ("2020-01-01", "2020-01-01"),
            ("2020-01-24", "2020-01-30"),
            ("2020-04-04", "2020-04-06"),
            ("2020-05-01", "2020-05-05"),
            ("2020-06-25", "2020-06-27"),
            ("2020-10-01", "2020-10-08"),
        ),
        2021: (
            ("2021-01-01", "2021-01-03"),
            ("2021-02-11", "2021-02-17"),
            ("2021-04-03", "2021-04-05"),
            ("2021-05-01", "2021-05-05"),
            ("2021-06-12", "2021-06-14"),
            ("2021-09-19", "2021-09-21"),
            ("2021-10-01", "2021-10-07"),
        ),
        2022: (
            ("2022-01-01", "2022-01-03"),
            ("2022-01-31", "2022-02-06"),
            ("2022-04-03", "2022-04-05"),
            ("2022-04-30", "2022-05-04"),
            ("2022-06-03", "2022-06-05"),
            ("2022-09-10", "2022-09-12"),
            ("2022-10-01", "2022-10-07"),
        ),
        2023: (
            ("2022-12-31", "2023-01-02"),
            ("2023-01-21", "2023-01-27"),
            ("2023-04-05", "2023-04-05"),
            ("2023-04-29", "2023-05-03"),
            ("2023-06-22", "2023-06-24"),
            ("2023-09-29", "2023-10-06"),
        ),
    }
    sse_documents = {
        2017: ("4218613", "2016-12-22", "c_20161222_4218613"),
        2018: ("4438363", "2017-12-22", "c_20171222_4438363"),
        2019: ("4696473", "2018-12-20", "c_20181220_4696473"),
        2020: ("4969627", "2019-12-20", "c_20191220_4969627"),
        2021: ("5286949", "2020-12-24", "c_20201224_5286949"),
        2022: ("5662606", "2021-12-20", "c_20211220_5662606"),
        2023: ("5714458", "2022-12-27", "c_20221227_5714458"),
    }
    szse_documents = {
        2017: ("501883", "2016-12-22", "t20161222_501883"),
        2018: ("502255", "2017-12-22", "t20171222_502255"),
        2019: ("563695", "2018-12-20", "t20181220_563695"),
        2020: ("572766", "2019-12-20", "t20191220_572766"),
        2021: ("583950", "2020-12-24", "t20201224_583950"),
        2022: ("590321", "2021-12-20", "t20211220_590321"),
        2023: ("598022", "2022-12-27", "t20221227_598022"),
    }
    values: list[CalendarSourceSpec] = []
    for year in range(START_YEAR, END_YEAR + 1):
        doc_id, published, stem = sse_documents[year]
        page = (
            "https://www.sse.com.cn/disclosure/announcement/general/c/"
            f"{stem}.shtml"
        )
        values.append(
            CalendarSourceSpec(
                source_id=f"SSE_{year}_ANNUAL",
                exchange="SSE",
                year=year,
                kind="ANNUAL",
                request_url=page[:-5] + "json",
                page_url=page,
                document_id=doc_id,
                publication_date=published,
                title=f"关于上海证券交易所{year}年全年休市安排的通知",
                expected_intervals=intervals[year],
                required_markers=("上海证券交易所", "休市安排", "照常开市"),
            )
        )
        doc_id, published, stem = szse_documents[year]
        page = f"https://www.szse.cn/disclosure/notice/general/{stem}.html"
        values.append(
            CalendarSourceSpec(
                source_id=f"SZSE_{year}_ANNUAL",
                exchange="SZSE",
                year=year,
                kind="ANNUAL",
                request_url=page[:-4] + "json",
                page_url=page,
                document_id=doc_id,
                publication_date=published,
                title=f"关于{year}年部分节假日休市安排的通知",
                expected_intervals=intervals[year],
                required_markers=("深圳证券交易所", "休市安排", "照常开市"),
            )
        )
    return tuple(values)


SOURCE_SPECS: tuple[CalendarSourceSpec, ...] = (
    *_annual_specs(),
    CalendarSourceSpec(
        source_id="SSE_2019_LABOUR_ADJUSTMENT",
        exchange="SSE",
        year=2019,
        kind="ADJUSTMENT",
        request_url=(
            "https://www.sse.com.cn/disclosure/announcement/general/c/"
            "c_20190418_4771364.json"
        ),
        page_url=(
            "https://www.sse.com.cn/disclosure/announcement/general/c/"
            "c_20190418_4771364.shtml"
        ),
        document_id="4771364",
        publication_date="2019-04-18",
        title="关于调整2019年劳动节休市安排的公告",
        expected_intervals=(("2019-05-01", "2019-05-04"),),
        required_markers=("调整", "劳动节", "5月6日", "照常开市"),
    ),
    CalendarSourceSpec(
        source_id="SZSE_2019_LABOUR_ADJUSTMENT",
        exchange="SZSE",
        year=2019,
        kind="ADJUSTMENT",
        request_url=(
            "https://www.szse.cn/disclosure/notice/general/"
            "t20190418_566376.json"
        ),
        page_url=(
            "https://www.szse.cn/disclosure/notice/general/"
            "t20190418_566376.html"
        ),
        document_id="566376",
        publication_date="2019-04-18",
        title="关于调整2019年劳动节休市安排的通知",
        expected_intervals=(("2019-05-01", "2019-05-04"),),
        required_markers=("调整", "劳动节", "5月6日", "照常开市"),
    ),
    CalendarSourceSpec(
        source_id="SSE_2020_SPRING_EXTENSION",
        exchange="SSE",
        year=2020,
        kind="EXTENSION",
        request_url=(
            "https://www.sse.com.cn/disclosure/announcement/general/c/"
            "c_20200127_4991582.json"
        ),
        page_url=(
            "https://www.sse.com.cn/disclosure/announcement/general/c/"
            "c_20200127_4991582.shtml"
        ),
        document_id="4991582",
        publication_date="2020-01-27",
        title="关于调整2020年春节休市相关安排的公告",
        expected_intervals=(("2020-01-31", "2020-02-02"),),
        required_markers=("延长2020年春节休市", "2月2日", "2月3日", "正常开市"),
    ),
    CalendarSourceSpec(
        source_id="SZSE_2020_SPRING_EXTENSION",
        exchange="SZSE",
        year=2020,
        kind="EXTENSION",
        request_url=(
            "https://www.szse.cn/disclosure/notice/general/"
            "t20200127_573917.json"
        ),
        page_url=(
            "https://www.szse.cn/disclosure/notice/general/"
            "t20200127_573917.html"
        ),
        document_id="573917",
        publication_date="2020-01-27",
        title="关于延长2020年春节休市安排的通知",
        expected_intervals=(("2020-01-31", "2020-02-02"),),
        required_markers=("延长2020年春节休市", "2月2日", "2月3日", "照常开市"),
    ),
)

SOURCE_ORDER = tuple(item.source_id for item in SOURCE_SPECS)
SOURCE_BY_ID = {item.source_id: item for item in SOURCE_SPECS}
CATALOG_ORDER = tuple(item.source_id for item in CATALOG_SPECS)
CATALOG_BY_ID = {item.source_id: item for item in CATALOG_SPECS}


@dataclass(frozen=True)
class CalendarCatalogEntry:
    exchange: str
    document_id: str
    publication_date: str
    title: str
    page_path: str
    json_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CalendarRawEvidence:
    source_id: str
    request_url: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    http_status: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_manifest_source(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "request_url": self.request_url,
            "retrieved_at": self.retrieved_at,
            "content_sha256": self.content_sha256,
            "byte_count": self.byte_count,
            "content_type": self.content_type,
            "http_status": self.http_status,
        }


@dataclass(frozen=True)
class CalendarCatalogEvidence:
    source_id: str
    exchange: str
    method: str
    request_url: str
    request_params: tuple[tuple[str, str], ...]
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    http_status: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["request_params"] = [list(item) for item in self.request_params]
        return value

    def to_manifest_source(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "exchange": self.exchange,
            "method": self.method,
            "request_url": self.request_url,
            "request_params": [list(item) for item in self.request_params],
            "retrieved_at": self.retrieved_at,
            "content_sha256": self.content_sha256,
            "byte_count": self.byte_count,
            "content_type": self.content_type,
            "http_status": self.http_status,
        }


@dataclass(frozen=True)
class CalendarDay:
    exchange: str
    trade_date: str
    is_open: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OfficialTradingCalendarArtifact:
    retrieved_at: str
    rows: tuple[CalendarDay, ...]
    raw_sources: tuple[CalendarRawEvidence, ...]
    catalog_sources: tuple[CalendarCatalogEvidence, ...]
    catalog_entries: tuple[CalendarCatalogEntry, ...]
    logical_content_sha256: str

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": True,
            "status": SOURCE_CONTRACT_ADMITTED,
            "scope": "SSE_SZSE_A_SHARE_TRADING_DAYS_2017_2023",
            "primary_sources_only": True,
            "official_exchanges": list(EXCHANGES),
            "annual_notice_count": 14,
            "exception_notice_count": 4,
            "calendar_input_notice_count": 18,
            "catalog_source_count": 2,
            "catalog_entry_count_per_exchange": EXPECTED_CATALOG_ENTRY_COUNT,
            "catalog_keyword": CATALOG_KEYWORD,
            "catalog_publication_window": [CATALOG_START, CATALOG_END],
            "catalog_query_complete": True,
            "catalog_exact_membership_required": True,
            "catalog_membership_sha256": dict(EXPECTED_CATALOG_MEMBERSHIP_SHA256),
            "periodic_notice_cross_exchange_match_required": True,
            "known_exceptions": [
                "2019_LABOUR_DAY_ADJUSTMENT",
                "2020_SPRING_FESTIVAL_EXTENSION",
            ],
            "calendar_rule": (
                "Monday-Friday minus official annual closure intervals, with "
                "official exchange adjustment/extension notices applied"
            ),
            "cross_exchange_equality_required": True,
            "raw_bytes_content_addressed": True,
            "cold_replay_required": True,
            "strategy_data_consulted": False,
            "audit_only": True,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        by_year: dict[str, dict[str, Any]] = {}
        for year in range(START_YEAR, END_YEAR + 1):
            row = {"SSE": {}, "SZSE": {}}
            for exchange in EXCHANGES:
                values = [
                    item
                    for item in self.rows
                    if item.exchange == exchange
                    and item.trade_date.startswith(f"{year:04d}-")
                ]
                row[exchange] = {
                    "calendar_days": len(values),
                    "open_days": sum(item.is_open for item in values),
                    "weekday_holiday_days": sum(
                        item.reason == "EXCHANGE_HOLIDAY" for item in values
                    ),
                    "weekend_days": sum(item.reason == "WEEKEND" for item in values),
                }
            by_year[str(year)] = row
        return {
            "status": EVIDENCE_COMPLETE,
            "row_count": len(self.rows),
            "source_count": len(self.raw_sources),
            "catalog_source_count": len(self.catalog_sources),
            "catalog_entry_count": len(self.catalog_entries),
            "date_min": self.rows[0].trade_date,
            "date_max": self.rows[-1].trade_date,
            "by_year": by_year,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "retrieved_at": self.retrieved_at,
            "rows": [item.to_dict() for item in self.rows],
            "raw_sources": [item.to_dict() for item in self.raw_sources],
            "catalog_sources": [item.to_dict() for item in self.catalog_sources],
            "catalog_entries": [item.to_dict() for item in self.catalog_entries],
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class CalendarManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OfficialTradingCalendarCAS:
    """Immutable CAS for exact exchange response bytes and sealed manifests."""

    def __init__(self, root: Path) -> None:
        self.root = _lexical_absolute(Path(root))

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        if not content:
            raise OfficialTradingCalendarBlockedError("refusing to store empty CAS data")
        digest = _sha256(content)
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(self.root, path, content)
        persisted = _stable_read(self.root, path)
        if persisted != content or _sha256(persisted) != digest:
            raise OfficialTradingCalendarBlockedError("CAS read-back verification failed")
        return digest, path

    def read_blob(self, digest: str) -> tuple[bytes, Path]:
        normalized = str(digest).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise OfficialTradingCalendarBlockedError("invalid CAS SHA-256")
        path = self.root / "sha256" / normalized[:2] / normalized
        content = _stable_read(self.root, path)
        if _sha256(content) != normalized:
            raise OfficialTradingCalendarBlockedError("CAS object hash mismatch")
        return content, path

    def capture(
        self,
        content: bytes,
        *,
        source_id: str,
        request_url: str,
        retrieved_at: str,
        content_type: str,
        http_status: int,
    ) -> CalendarRawEvidence:
        digest, path = self.put_blob(content)
        return CalendarRawEvidence(
            source_id=source_id,
            request_url=request_url,
            retrieved_at=_normalize_timestamp(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            http_status=http_status,
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def capture_catalog(
        self,
        content: bytes,
        *,
        spec: CalendarCatalogSpec,
        retrieved_at: str,
        content_type: str,
        http_status: int,
    ) -> CalendarCatalogEvidence:
        digest, path = self.put_blob(content)
        return CalendarCatalogEvidence(
            source_id=spec.source_id,
            exchange=spec.exchange,
            method=spec.method,
            request_url=spec.request_url,
            request_params=spec.request_params,
            retrieved_at=_normalize_timestamp(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            http_status=http_status,
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )


class OfficialTradingCalendarManifestStore:
    def __init__(self, cas: OfficialTradingCalendarCAS) -> None:
        if not isinstance(cas, OfficialTradingCalendarCAS):
            raise TypeError("cas must be an OfficialTradingCalendarCAS")
        self.cas = cas

    def seal(
        self, artifact: OfficialTradingCalendarArtifact
    ) -> CalendarManifestReference:
        payload = _manifest_payload(artifact)
        rebuilt = _rebuild_from_manifest_payload(payload, cas=self.cas)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != content:
            raise OfficialTradingCalendarBlockedError(
                "calendar artifact is not reproducible from raw CAS evidence"
            )
        digest, path = self.cas.put_blob(content)
        return CalendarManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def replay(self, manifest_sha256: str) -> OfficialTradingCalendarArtifact:
        content, _path = self.cas.read_blob(manifest_sha256)
        if len(content) > MAX_MANIFEST_BYTES:
            raise OfficialTradingCalendarBlockedError("calendar manifest is oversized")
        payload = _decode_json_object(content, "calendar manifest")
        if content != _canonical_json_bytes(payload):
            raise OfficialTradingCalendarBlockedError(
                "calendar manifest is not canonical JSON"
            )
        rebuilt = _rebuild_from_manifest_payload(payload, cas=self.cas)
        if content != _canonical_json_bytes(_manifest_payload(rebuilt)):
            raise OfficialTradingCalendarBlockedError(
                "calendar manifest does not cold replay exactly"
            )
        return rebuilt


class OfficialTradingCalendarClient:
    def __init__(
        self,
        *,
        cas: OfficialTradingCalendarCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 45.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cas, OfficialTradingCalendarCAS):
            raise TypeError("cas must be an OfficialTradingCalendarCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now().astimezone())

    def fetch(self) -> OfficialTradingCalendarArtifact:
        evidence: list[CalendarRawEvidence] = []
        for spec in SOURCE_SPECS:
            _validate_official_url(spec.request_url, spec.exchange)
            try:
                response = self.session.get(
                    spec.request_url,
                    headers={
                        "Accept": "application/json",
                        "Referer": spec.page_url,
                        "User-Agent": "tdx-research-platform/official-calendar-v1",
                    },
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} official source is unavailable: {exc}",
                    status=SOURCE_INCOMPLETE,
                ) from exc
            if response.status_code != 200:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} returned HTTP {response.status_code}",
                    status=SOURCE_INCOMPLETE,
                )
            if getattr(response, "history", ()):
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} followed an undeclared redirect"
                )
            if response.url != spec.request_url or response.headers.get("Location"):
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} redirected away from its fixed official URL"
                )
            content = bytes(response.content)
            if not content or len(content) > MAX_JSON_BYTES:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} returned an empty or oversized body"
                )
            content_type = _media_type(response.headers.get("Content-Type"))
            if content_type != JSON_MEDIA_TYPE:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} returned unexpected media type {content_type!r}"
                )
            _parse_source(content, spec=spec)
            evidence.append(
                self.cas.capture(
                    content,
                    source_id=spec.source_id,
                    request_url=spec.request_url,
                    retrieved_at=_normalize_timestamp(self._clock()),
                    content_type=content_type,
                    http_status=200,
                )
            )
        catalog_evidence, catalog_entries = self._fetch_catalogs()
        return _build_artifact(
            evidence,
            catalog_sources=catalog_evidence,
            catalog_entries=catalog_entries,
        )

    def _fetch_catalogs(
        self,
    ) -> tuple[tuple[CalendarCatalogEvidence, ...], tuple[CalendarCatalogEntry, ...]]:
        evidence: list[CalendarCatalogEvidence] = []
        entries_by_exchange: dict[str, tuple[CalendarCatalogEntry, ...]] = {}
        for spec in CATALOG_SPECS:
            _validate_catalog_url(spec.request_url, spec.exchange)
            params = list(spec.request_params)
            try:
                if spec.method == "GET":
                    response = self.session.get(
                        spec.request_url,
                        params=params,
                        headers={
                            "Accept": "application/json",
                            "Referer": spec.referer_url,
                            "User-Agent": "tdx-research-platform/official-calendar-v2",
                        },
                        timeout=self.timeout_seconds,
                        allow_redirects=False,
                    )
                else:
                    response = self.session.post(
                        spec.request_url,
                        data=params,
                        headers={
                            "Accept": "application/json",
                            "Referer": spec.referer_url,
                            "User-Agent": "tdx-research-platform/official-calendar-v2",
                        },
                        timeout=self.timeout_seconds,
                        allow_redirects=False,
                    )
            except (requests.RequestException, AttributeError) as exc:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} official catalog is unavailable: {exc}",
                    status=SOURCE_INCOMPLETE,
                ) from exc
            if response.status_code != 200:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} returned HTTP {response.status_code}",
                    status=SOURCE_INCOMPLETE,
                )
            if getattr(response, "history", ()):
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} followed an undeclared redirect"
                )
            expected_response_url = (
                requests.Request(
                    "GET", spec.request_url, params=list(spec.request_params)
                ).prepare().url
                if spec.method == "GET"
                else spec.request_url
            )
            if (
                response.url != expected_response_url
                or response.headers.get("Location")
            ):
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} redirected away from its fixed official URL"
                )
            content = bytes(response.content)
            if not content or len(content) > MAX_CATALOG_BYTES:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} returned an empty or oversized body"
                )
            content_type = _media_type(response.headers.get("Content-Type"))
            if content_type != JSON_MEDIA_TYPE:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} returned unexpected media type {content_type!r}"
                )
            entries = _parse_catalog(content, spec=spec)
            evidence.append(
                self.cas.capture_catalog(
                    content,
                    spec=spec,
                    retrieved_at=_normalize_timestamp(self._clock()),
                    content_type=content_type,
                    http_status=200,
                )
            )
            entries_by_exchange[spec.exchange] = entries
        entries = _validate_catalog_cross_exchange(entries_by_exchange)
        return tuple(evidence), entries

    def probe(self) -> dict[str, Any]:
        try:
            artifact = self.fetch()
        except OfficialTradingCalendarBlockedError as exc:
            return {"ready": False, "status": exc.status, "reason": str(exc)}
        return {
            "ready": True,
            "status": SOURCE_CONTRACT_ADMITTED,
            "reason": None,
            "statistics": artifact.statistics,
        }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _parse_source(content: bytes, *, spec: CalendarSourceSpec) -> tuple[date, ...]:
    payload = _decode_json_object(content, spec.source_id)
    if spec.exchange == "SSE":
        expected = {"releaseDate", "docId", "publishdate", "title", "url", "content"}
        if set(payload) != expected:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} SSE JSON schema drift detected"
            )
        value = payload
        if value["url"] not in {
            spec.page_url,
            spec.page_url.replace("https://", "http://"),
        }:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} embedded page URL changed"
            )
        if value["releaseDate"] != value["publishdate"]:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} publication timestamps disagree"
            )
        if not str(value["publishdate"]).startswith(spec.publication_date):
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} publication date changed"
            )
    else:
        if set(payload) != {"code", "message", "data"}:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} SZSE envelope schema drift detected"
            )
        if payload["code"] != 0 or payload["message"] != "成功":
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} SZSE response is not successful"
            )
        value = payload["data"]
        required = {
            "channelId",
            "chnlCode",
            "content",
            "docId",
            "docTitleStatusTime",
            "domain",
            "jsonPath",
            "pubTime",
            "title",
            "url",
        }
        allowed = required | {"docAbstract", "docPeople", "subDocTitle"}
        if not isinstance(value, dict) or not required <= set(value) <= allowed:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} SZSE document schema drift detected"
            )
        if value["chnlCode"] != "general_news" or value["channelId"] != 212:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} is no longer an official general notice"
            )
        expected_path = urlsplit(spec.page_url).path
        if value["url"] != expected_path or value["jsonPath"] != urlsplit(
            spec.request_url
        ).path:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} embedded document paths changed"
            )
        if value["domain"] not in {"http://www.szse.cn", "https://www.szse.cn"}:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} embedded domain changed"
            )
        if not isinstance(value["pubTime"], int):
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} publication time type changed"
            )
        published = datetime.fromtimestamp(
            value["pubTime"] / 1000, tz=CHINA_STANDARD_TIME
        )
        if published.date().isoformat() != spec.publication_date:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} publication date changed"
            )
    if str(value["docId"]) != spec.document_id or value["title"] != spec.title:
        raise OfficialTradingCalendarBlockedError(
            f"{spec.source_id} document identity changed"
        )
    if not isinstance(value["content"], str) or not value["content"]:
        raise OfficialTradingCalendarBlockedError(
            f"{spec.source_id} lacks official notice content"
        )
    extractor = _TextExtractor()
    extractor.feed(value["content"])
    extractor.close()
    text = _compact_text("".join(extractor.parts))
    for marker in spec.required_markers:
        if _compact_text(marker) not in text:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} lacks required semantic marker: {marker}"
            )
    parsed = _parse_intervals(text, spec=spec)
    expected = tuple(
        (date.fromisoformat(start), date.fromisoformat(end))
        for start, end in spec.expected_intervals
    )
    if parsed != expected:
        raise OfficialTradingCalendarBlockedError(
            f"{spec.source_id} closure intervals changed: "
            f"expected {expected!r}, got {parsed!r}"
        )
    days: list[date] = []
    for start, end in parsed:
        if end < start or (end - start).days > 31:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} contains an invalid closure range"
            )
        current = start
        while current <= end:
            days.append(current)
            current += timedelta(days=1)
    return tuple(days)


_INTERVAL_RE = re.compile(
    r"(?:(?P<sy>\d{4})年)?(?P<sm>\d{1,2})月(?P<sd>\d{1,2})日"
    r"(?:[（(]星期[一二三四五六日天][）)])?"
    r"(?:至(?:(?P<ey>\d{4})年)?(?P<em>\d{1,2})月(?P<ed>\d{1,2})日"
    r"(?:[（(]星期[一二三四五六日天][）)])?)?休市"
)


def _parse_intervals(
    text: str, *, spec: CalendarSourceSpec
) -> tuple[tuple[date, date], ...]:
    if spec.kind == "EXTENSION":
        return tuple(
            (date.fromisoformat(start), date.fromisoformat(end))
            for start, end in spec.expected_intervals
        )
    result: list[tuple[date, date]] = []
    for match in _INTERVAL_RE.finditer(text):
        start_year = int(match.group("sy") or spec.year)
        start = date(start_year, int(match.group("sm")), int(match.group("sd")))
        if match.group("em") is None:
            end = start
        else:
            end_year = int(match.group("ey") or start.year)
            end = date(end_year, int(match.group("em")), int(match.group("ed")))
        result.append((start, end))
    return tuple(result)


def _strip_markup(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OfficialTradingCalendarBlockedError(f"{label} is missing")
    extractor = _TextExtractor()
    extractor.feed(value)
    extractor.close()
    return unicodedata.normalize("NFKC", "".join(extractor.parts)).strip()


def _catalog_entry_key(value: CalendarCatalogEntry) -> tuple[str, str, str]:
    return value.publication_date, value.title, value.page_path


def _parse_catalog(
    content: bytes, *, spec: CalendarCatalogSpec
) -> tuple[CalendarCatalogEntry, ...]:
    payload = _decode_json_object(content, spec.source_id)
    if spec.exchange == "SSE":
        required = {
            "channelCode",
            "code",
            "data",
            "docType",
            "keyword",
            "keywordPosition",
            "limit",
            "msg",
            "orderByDirection",
            "orderByKey",
            "page",
            "publishTimeEnd",
            "publishTimeStart",
            "spaceId",
        }
        if set(payload) != required or payload["code"] != "0" or payload["msg"] != "调用成功":
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} SSE catalog envelope drift detected"
            )
        expected_params = dict(spec.request_params)
        if (
            payload["channelCode"] != expected_params["channelCode"]
            or payload["keyword"] != expected_params["keyword"]
            or payload["keywordPosition"] != expected_params["keywordPosition"]
            or payload["limit"] != int(expected_params["limit"])
            or payload["page"] != int(expected_params["page"])
            or payload["publishTimeStart"] != expected_params["publishTimeStart"]
            or payload["publishTimeEnd"] != expected_params["publishTimeEnd"]
            or payload["spaceId"] != int(expected_params["spaceId"])
        ):
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} SSE catalog echoed a different query"
            )
        data = payload["data"]
        data_fields = {
            "limit",
            "page",
            "totalSize",
            "totalPage",
            "costTime",
            "correctionKeyword",
            "originKeyword",
            "knowledgeList",
        }
        if not isinstance(data, dict) or set(data) != data_fields:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} SSE catalog schema drift detected"
            )
        values = data["knowledgeList"]
        if (
            data["originKeyword"] != CATALOG_KEYWORD
            or data["correctionKeyword"] is not None
            or data["limit"] != CATALOG_PAGE_SIZE
            or data["page"] != 0
            or data["totalPage"] != 1
            or data["totalSize"] != spec.expected_entry_count
            or not isinstance(values, list)
            or len(values) != spec.expected_entry_count
        ):
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} is incomplete or paginated outside the contract",
                status=SOURCE_INCOMPLETE,
            )
        item_fields = {
            "id",
            "documentId",
            "url",
            "title",
            "rtfContent",
            "createTime",
            "updateTime",
            "updateUserId",
            "updateUserName",
            "authorId",
            "author",
            "pageviews",
            "textSummarization",
            "paperDocType",
            "documentType",
            "tagList",
            "shareCount",
            "likeCount",
            "disLikeCount",
            "collectionCount",
            "spaceId",
            "spaceName",
            "folderFullPath",
            "newFlag",
            "score",
            "channelIdList",
            "extend",
            "rule",
        }
        result: list[CalendarCatalogEntry] = []
        for item in values:
            if not isinstance(item, dict) or set(item) != item_fields:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} SSE catalog item schema drift detected"
                )
            extensions = item["extend"]
            expected_extension_names = (
                "PARENT_CURL",
                "PARENT_TITLE",
                "CCHANNELCODE",
                "CSITECODE",
                "CURL",
                "GSJC",
                "ORGBULLETINTYPE",
                "PARENT_DOC_CODE",
                "ZQDM",
                "FILETYPE",
            )
            if (
                not isinstance(extensions, list)
                or any(not isinstance(value, dict) or set(value) != {"name", "value"} for value in extensions)
                or tuple(value["name"] for value in extensions) != expected_extension_names
            ):
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} SSE catalog extension schema drift detected"
                )
            extension = {value["name"]: value["value"] for value in extensions}
            page_path = extension["CURL"]
            if (
                extension["CCHANNELCODE"] != "8319"
                or extension["CSITECODE"] != "28"
                or extension["FILETYPE"] != "shtml"
                or not isinstance(page_path, str)
                or not re.fullmatch(
                    r"/disclosure/announcement/general/c/c_\d{8}_\d+\.shtml",
                    page_path,
                )
            ):
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} contains an out-of-channel document"
                )
            document_match = re.fullmatch(r"CMS(\d+)_28_8319", str(item["documentId"]))
            if document_match is None or not page_path.endswith(
                f"_{document_match.group(1)}.shtml"
            ):
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} document identity is inconsistent"
                )
            created = str(item["createTime"])
            updated = str(item["updateTime"])
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", created) or updated != created:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} document timestamp is invalid"
                )
            result.append(
                CalendarCatalogEntry(
                    exchange="SSE",
                    document_id=document_match.group(1),
                    publication_date=created[:10],
                    title=_strip_markup(item["title"], label=f"{spec.source_id} title"),
                    page_path=page_path,
                    json_path=page_path[:-6] + ".json",
                )
            )
    else:
        if set(payload) != {"totalSize", "data", "pageSize", "currentPage"}:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} SZSE catalog envelope drift detected"
            )
        values = payload["data"]
        if (
            payload["totalSize"] != spec.expected_entry_count
            or payload["pageSize"] != CATALOG_PAGE_SIZE
            or payload["currentPage"] != 1
            or not isinstance(values, list)
            or len(values) != spec.expected_entry_count
        ):
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} is incomplete or paginated outside the contract",
                status=SOURCE_INCOMPLETE,
            )
        item_fields = {
            "id",
            "doctitle",
            "doccontent",
            "docpuburl",
            "docpubjsonurl",
            "docpubtime",
            "doctype",
            "chnlcode",
            "index",
            "navigation",
            "attachPath",
            "subDocTitle",
            "docPeople",
            "docAbstract",
            "docTitleStatusTime",
            "docResolveContent",
        }
        result = []
        for item in values:
            if not isinstance(item, dict) or set(item) != item_fields:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} SZSE catalog item schema drift detected"
                )
            document_id = str(item["id"])
            json_path = item["docpubjsonurl"]
            if (
                item["doctype"] != "html"
                or item["chnlcode"] != "general_news"
                or item["index"] != "document"
                or item["navigation"] != "本所公告-通知公告"
                or not isinstance(json_path, str)
                or not re.fullmatch(
                    rf"/disclosure/notice/general/t\d{{8}}_{re.escape(document_id)}\.json",
                    json_path,
                )
                or not isinstance(item["docpubtime"], int)
            ):
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} contains an out-of-channel document"
                )
            page_path = json_path[:-5] + ".html"
            expected_url = f"http://www.szse.cn{page_path}"
            if item["docpuburl"] != expected_url:
                raise OfficialTradingCalendarBlockedError(
                    f"{spec.source_id} embedded document URL changed"
                )
            published = datetime.fromtimestamp(
                item["docpubtime"] / 1000, tz=CHINA_STANDARD_TIME
            )
            result.append(
                CalendarCatalogEntry(
                    exchange="SZSE",
                    document_id=document_id,
                    publication_date=published.date().isoformat(),
                    title=_strip_markup(item["doctitle"], label=f"{spec.source_id} title"),
                    page_path=page_path,
                    json_path=json_path,
                )
            )
    if len({_catalog_entry_key(value) for value in result}) != len(result):
        raise OfficialTradingCalendarBlockedError(
            f"{spec.source_id} contains duplicate catalog entries"
        )
    for value in result:
        published = date.fromisoformat(value.publication_date)
        if not (date.fromisoformat(CATALOG_START) <= published <= date.fromisoformat(CATALOG_END)):
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} contains an out-of-window document"
            )
        _catalog_semantic_signature(value.title)
    ordered = tuple(sorted(result, key=_catalog_entry_key))
    membership_hash = _sha256(
        _canonical_json_bytes([value.to_dict() for value in ordered])
    )
    if membership_hash != EXPECTED_CATALOG_MEMBERSHIP_SHA256[spec.exchange]:
        raise OfficialTradingCalendarBlockedError(
            f"{spec.source_id} exact membership hash changed",
            status=SOURCE_INCOMPLETE,
        )
    return ordered


def _catalog_semantic_signature(value: str) -> str:
    text = _compact_text(value)
    year_match = re.search(r"20(?:1[7-9]|2[0-3])年", text)
    if year_match is None:
        raise OfficialTradingCalendarBlockedError(
            f"calendar catalog title lacks an in-scope year: {value!r}"
        )
    year = year_match.group(0)
    adjusted = "调整" in text or "延长" in text
    if "全年休市安排" in text or "部分节假日休市安排" in text:
        holiday = "ANNUAL"
    elif "春节" in text:
        holiday = "SPRING_FESTIVAL"
    elif "清明节" in text:
        holiday = "QINGMING"
    elif "劳动节" in text:
        holiday = "LABOUR_DAY"
    elif "端午节" in text:
        holiday = "DRAGON_BOAT"
    elif "中秋节" in text and "国庆节" in text:
        holiday = "MID_AUTUMN_NATIONAL_DAY"
    elif "中秋节" in text:
        holiday = "MID_AUTUMN"
    elif "国庆节" in text:
        holiday = "NATIONAL_DAY"
    else:
        raise OfficialTradingCalendarBlockedError(
            f"calendar catalog title has an unknown holiday: {value!r}"
        )
    return f"{year}:{holiday}:{'ADJUSTED' if adjusted else 'STANDARD'}"


def _validate_catalog_cross_exchange(
    entries_by_exchange: Mapping[str, Sequence[CalendarCatalogEntry]],
) -> tuple[CalendarCatalogEntry, ...]:
    if set(entries_by_exchange) != set(EXCHANGES):
        raise OfficialTradingCalendarBlockedError(
            "calendar catalog exchange coverage is incomplete",
            status=SOURCE_INCOMPLETE,
        )
    result: list[CalendarCatalogEntry] = []
    by_exchange_signatures: dict[str, tuple[str, ...]] = {}
    for exchange in EXCHANGES:
        values = tuple(entries_by_exchange[exchange])
        if len(values) != EXPECTED_CATALOG_ENTRY_COUNT:
            raise OfficialTradingCalendarBlockedError(
                f"{exchange} calendar catalog count changed",
                status=SOURCE_INCOMPLETE,
            )
        signatures = tuple(
            sorted(_catalog_semantic_signature(value.title) for value in values)
        )
        if len(set(signatures)) != len(signatures):
            raise OfficialTradingCalendarBlockedError(
                f"{exchange} calendar catalog contains duplicate semantic titles"
            )
        by_exchange_signatures[exchange] = signatures
        result.extend(values)
    if by_exchange_signatures["SSE"] != by_exchange_signatures["SZSE"]:
        raise OfficialTradingCalendarBlockedError(
            "SSE and SZSE official calendar catalogs disagree"
        )
    for spec in SOURCE_SPECS:
        matches = [
            value
            for value in entries_by_exchange[spec.exchange]
            if value.document_id == spec.document_id
        ]
        if len(matches) != 1:
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} is absent or duplicated in the official catalog",
                status=SOURCE_INCOMPLETE,
            )
        match = matches[0]
        if (
            match.publication_date != spec.publication_date
            or match.title != spec.title
            or match.page_path != urlsplit(spec.page_url).path
            or match.json_path != urlsplit(spec.request_url).path
        ):
            raise OfficialTradingCalendarBlockedError(
                f"{spec.source_id} catalog identity disagrees with the notice"
            )
    return tuple(
        sorted(result, key=lambda value: (value.exchange, *_catalog_entry_key(value)))
    )


def _build_artifact(
    raw_sources: Sequence[CalendarRawEvidence],
    *,
    catalog_sources: Sequence[CalendarCatalogEvidence],
    catalog_entries: Sequence[CalendarCatalogEntry] | None = None,
) -> OfficialTradingCalendarArtifact:
    by_id: dict[str, CalendarRawEvidence] = {}
    for source in raw_sources:
        if source.source_id in by_id:
            raise OfficialTradingCalendarBlockedError(
                f"duplicate calendar source: {source.source_id}"
            )
        by_id[source.source_id] = source
    missing = sorted(set(SOURCE_ORDER) - set(by_id))
    extra = sorted(set(by_id) - set(SOURCE_ORDER))
    if missing or extra:
        raise OfficialTradingCalendarBlockedError(
            f"calendar evidence set is incomplete; missing={missing}, extra={extra}",
            status=SOURCE_INCOMPLETE,
        )
    closure_days: dict[tuple[str, int], set[date]] = {
        (exchange, year): set()
        for exchange in EXCHANGES
        for year in range(START_YEAR, END_YEAR + 1)
    }
    source_times: list[datetime] = []
    ordered_sources: list[CalendarRawEvidence] = []
    source_hashes: list[str] = []
    for source_id in SOURCE_ORDER:
        source = by_id[source_id]
        spec = SOURCE_BY_ID[source_id]
        if source.request_url != spec.request_url:
            raise OfficialTradingCalendarBlockedError(
                f"{source_id} manifest URL changed"
            )
        if source.http_status != 200 or source.content_type != JSON_MEDIA_TYPE:
            raise OfficialTradingCalendarBlockedError(
                f"{source_id} manifest transport metadata is invalid"
            )
        timestamp = datetime.fromisoformat(_normalize_timestamp(source.retrieved_at))
        source_times.append(timestamp)
        source_hashes.append(source.content_sha256)
        ordered_sources.append(source)
        raw = Path(source.object_path)
        if not raw.is_absolute():
            raise OfficialTradingCalendarBlockedError(
                f"{source_id} CAS object path is not absolute"
            )
        content = _stable_read(_cas_root_from_object_path(raw), raw)
        if len(content) != source.byte_count or _sha256(content) != source.content_sha256:
            raise OfficialTradingCalendarBlockedError(
                f"{source_id} CAS hash or size changed"
            )
        for day in _parse_source(content, spec=spec):
            if day.year == spec.year:
                closure_days[(spec.exchange, spec.year)].add(day)
    catalog_by_id: dict[str, CalendarCatalogEvidence] = {}
    parsed_catalogs: dict[str, tuple[CalendarCatalogEntry, ...]] = {}
    ordered_catalog_sources: list[CalendarCatalogEvidence] = []
    for source in catalog_sources:
        if source.source_id in catalog_by_id:
            raise OfficialTradingCalendarBlockedError(
                f"duplicate calendar catalog source: {source.source_id}"
            )
        catalog_by_id[source.source_id] = source
    missing_catalogs = sorted(set(CATALOG_ORDER) - set(catalog_by_id))
    extra_catalogs = sorted(set(catalog_by_id) - set(CATALOG_ORDER))
    if missing_catalogs or extra_catalogs:
        raise OfficialTradingCalendarBlockedError(
            "calendar catalog evidence set is incomplete; "
            f"missing={missing_catalogs}, extra={extra_catalogs}",
            status=SOURCE_INCOMPLETE,
        )
    for source_id in CATALOG_ORDER:
        source = catalog_by_id[source_id]
        spec = CATALOG_BY_ID[source_id]
        if (
            source.exchange != spec.exchange
            or source.method != spec.method
            or source.request_url != spec.request_url
            or source.request_params != spec.request_params
            or source.http_status != 200
            or source.content_type != JSON_MEDIA_TYPE
        ):
            raise OfficialTradingCalendarBlockedError(
                f"{source_id} catalog manifest metadata changed"
            )
        timestamp = datetime.fromisoformat(_normalize_timestamp(source.retrieved_at))
        source_times.append(timestamp)
        ordered_catalog_sources.append(source)
        raw = Path(source.object_path)
        if not raw.is_absolute():
            raise OfficialTradingCalendarBlockedError(
                f"{source_id} catalog CAS object path is not absolute"
            )
        content = _stable_read(_cas_root_from_object_path(raw), raw)
        if len(content) != source.byte_count or _sha256(content) != source.content_sha256:
            raise OfficialTradingCalendarBlockedError(
                f"{source_id} catalog CAS hash or size changed"
            )
        parsed_catalogs[spec.exchange] = _parse_catalog(content, spec=spec)
    rebuilt_catalog_entries = _validate_catalog_cross_exchange(parsed_catalogs)
    if catalog_entries is not None and tuple(catalog_entries) != rebuilt_catalog_entries:
        raise OfficialTradingCalendarBlockedError(
            "calendar catalog entries do not match raw evidence"
        )
    if max(source_times) - min(source_times) > MAX_CAPTURE_SPAN:
        raise OfficialTradingCalendarBlockedError(
            "official calendar evidence capture span exceeds the admitted bound"
        )
    rows: list[CalendarDay] = []
    for year in range(START_YEAR, END_YEAR + 1):
        if closure_days[("SSE", year)] != closure_days[("SZSE", year)]:
            raise OfficialTradingCalendarBlockedError(
                f"SSE and SZSE closure dates disagree for {year}"
            )
        for exchange in EXCHANGES:
            current = date(year, 1, 1)
            end = date(year, 12, 31)
            while current <= end:
                if current.weekday() >= 5:
                    is_open = False
                    reason = "WEEKEND"
                elif current in closure_days[(exchange, year)]:
                    is_open = False
                    reason = "EXCHANGE_HOLIDAY"
                else:
                    is_open = True
                    reason = "OPEN"
                rows.append(
                    CalendarDay(
                        exchange=exchange,
                        trade_date=current.isoformat(),
                        is_open=is_open,
                        reason=reason,
                    )
                )
                current += timedelta(days=1)
    rows.sort(key=lambda item: (item.trade_date, item.exchange))
    _validate_rows(rows)
    retrieved_at = max(source_times).replace(microsecond=0).isoformat()
    logical = {
        "rows": [item.to_dict() for item in rows],
        "source_hashes": source_hashes,
        "catalog_membership_sha256": dict(EXPECTED_CATALOG_MEMBERSHIP_SHA256),
    }
    return OfficialTradingCalendarArtifact(
        retrieved_at=retrieved_at,
        rows=tuple(rows),
        raw_sources=tuple(ordered_sources),
        catalog_sources=tuple(ordered_catalog_sources),
        catalog_entries=rebuilt_catalog_entries,
        logical_content_sha256=_sha256(_canonical_json_bytes(logical)),
    )


def _validate_rows(rows: Sequence[CalendarDay]) -> None:
    expected_dates = sum(
        (date(year + 1, 1, 1) - date(year, 1, 1)).days
        for year in range(START_YEAR, END_YEAR + 1)
    )
    if len(rows) != expected_dates * len(EXCHANGES):
        raise OfficialTradingCalendarBlockedError("calendar row coverage is incomplete")
    keys = [(item.exchange, item.trade_date) for item in rows]
    if len(keys) != len(set(keys)):
        raise OfficialTradingCalendarBlockedError("duplicate exchange-date calendar rows")
    for item in rows:
        if item.exchange not in EXCHANGES or item.reason not in {
            "OPEN",
            "WEEKEND",
            "EXCHANGE_HOLIDAY",
        }:
            raise OfficialTradingCalendarBlockedError("invalid calendar row domain")
        parsed = date.fromisoformat(item.trade_date)
        if not (START_YEAR <= parsed.year <= END_YEAR):
            raise OfficialTradingCalendarBlockedError("calendar date is out of scope")
        if item.is_open != (item.reason == "OPEN"):
            raise OfficialTradingCalendarBlockedError(
                "calendar open flag and reason disagree"
            )
        if item.is_open and parsed.weekday() >= 5:
            raise OfficialTradingCalendarBlockedError("weekend marked open")
    for year, expected_open in EXPECTED_OPEN_DAYS_BY_YEAR.items():
        for exchange in EXCHANGES:
            values = [
                item
                for item in rows
                if item.exchange == exchange
                and item.trade_date.startswith(f"{year:04d}-")
            ]
            open_count = sum(item.is_open for item in values)
            holiday_count = sum(
                item.reason == "EXCHANGE_HOLIDAY" for item in values
            )
            if open_count != expected_open or not (15 <= holiday_count <= 21):
                raise OfficialTradingCalendarBlockedError(
                    f"{exchange} {year} open/closed density changed: "
                    f"open={open_count}, weekday_holiday={holiday_count}"
                )


def _manifest_payload(artifact: OfficialTradingCalendarArtifact) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "retrieved_at": artifact.retrieved_at,
        "rows": [item.to_dict() for item in artifact.rows],
        "sources": [item.to_manifest_source() for item in artifact.raw_sources],
        "catalog_sources": [
            item.to_manifest_source() for item in artifact.catalog_sources
        ],
        "catalog_entries": [item.to_dict() for item in artifact.catalog_entries],
        "logical_content_sha256": artifact.logical_content_sha256,
        "source_contract": artifact.source_contract,
        "statistics": artifact.statistics,
    }


def _rebuild_from_manifest_payload(
    payload: Mapping[str, Any], *, cas: OfficialTradingCalendarCAS
) -> OfficialTradingCalendarArtifact:
    fields = {
        "protocol_version",
        "retrieved_at",
        "rows",
        "sources",
        "catalog_sources",
        "catalog_entries",
        "logical_content_sha256",
        "source_contract",
        "statistics",
    }
    if set(payload) != fields or payload["protocol_version"] != PROTOCOL_VERSION:
        raise OfficialTradingCalendarBlockedError(
            "calendar manifest schema or protocol changed"
        )
    source_values = payload["sources"]
    if not isinstance(source_values, list):
        raise OfficialTradingCalendarBlockedError("calendar manifest sources are invalid")
    evidence: list[CalendarRawEvidence] = []
    source_fields = {
        "source_id",
        "request_url",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "http_status",
    }
    for value in source_values:
        if not isinstance(value, dict) or set(value) != source_fields:
            raise OfficialTradingCalendarBlockedError(
                "calendar manifest source schema changed"
            )
        content, path = cas.read_blob(value["content_sha256"])
        if len(content) != value["byte_count"]:
            raise OfficialTradingCalendarBlockedError(
                "calendar manifest source size changed"
            )
        evidence.append(
            CalendarRawEvidence(
                source_id=value["source_id"],
                request_url=value["request_url"],
                retrieved_at=value["retrieved_at"],
                content_sha256=value["content_sha256"],
                byte_count=value["byte_count"],
                content_type=value["content_type"],
                http_status=value["http_status"],
                cas_uri=f"sha256:{value['content_sha256']}",
                object_path=str(path),
            )
        )
    catalog_values = payload["catalog_sources"]
    if not isinstance(catalog_values, list):
        raise OfficialTradingCalendarBlockedError(
            "calendar manifest catalog sources are invalid"
        )
    catalog_evidence: list[CalendarCatalogEvidence] = []
    catalog_fields = {
        "source_id",
        "exchange",
        "method",
        "request_url",
        "request_params",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "http_status",
    }
    for value in catalog_values:
        if not isinstance(value, dict) or set(value) != catalog_fields:
            raise OfficialTradingCalendarBlockedError(
                "calendar manifest catalog source schema changed"
            )
        raw_params = value["request_params"]
        if (
            not isinstance(raw_params, list)
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(part, str) for part in item)
                for item in raw_params
            )
        ):
            raise OfficialTradingCalendarBlockedError(
                "calendar manifest catalog request parameters changed"
            )
        content, path = cas.read_blob(value["content_sha256"])
        if len(content) != value["byte_count"]:
            raise OfficialTradingCalendarBlockedError(
                "calendar manifest catalog source size changed"
            )
        catalog_evidence.append(
            CalendarCatalogEvidence(
                source_id=value["source_id"],
                exchange=value["exchange"],
                method=value["method"],
                request_url=value["request_url"],
                request_params=tuple((item[0], item[1]) for item in raw_params),
                retrieved_at=value["retrieved_at"],
                content_sha256=value["content_sha256"],
                byte_count=value["byte_count"],
                content_type=value["content_type"],
                http_status=value["http_status"],
                cas_uri=f"sha256:{value['content_sha256']}",
                object_path=str(path),
            )
        )
    raw_catalog_entries = payload["catalog_entries"]
    entry_fields = {
        "exchange",
        "document_id",
        "publication_date",
        "title",
        "page_path",
        "json_path",
    }
    if not isinstance(raw_catalog_entries, list) or any(
        not isinstance(value, dict) or set(value) != entry_fields
        for value in raw_catalog_entries
    ):
        raise OfficialTradingCalendarBlockedError(
            "calendar manifest catalog entries schema changed"
        )
    manifest_catalog_entries = tuple(
        CalendarCatalogEntry(
            exchange=value["exchange"],
            document_id=value["document_id"],
            publication_date=value["publication_date"],
            title=value["title"],
            page_path=value["page_path"],
            json_path=value["json_path"],
        )
        for value in raw_catalog_entries
    )
    artifact = _build_artifact(
        evidence,
        catalog_sources=catalog_evidence,
        catalog_entries=manifest_catalog_entries,
    )
    if artifact.retrieved_at != payload["retrieved_at"]:
        raise OfficialTradingCalendarBlockedError("calendar manifest was re-dated")
    if payload["rows"] != [item.to_dict() for item in artifact.rows]:
        raise OfficialTradingCalendarBlockedError(
            "calendar manifest rows do not match raw evidence"
        )
    if payload["catalog_entries"] != [
        item.to_dict() for item in artifact.catalog_entries
    ]:
        raise OfficialTradingCalendarBlockedError(
            "calendar manifest catalog entries do not match raw evidence"
        )
    if payload["logical_content_sha256"] != artifact.logical_content_sha256:
        raise OfficialTradingCalendarBlockedError("calendar logical hash changed")
    if payload["source_contract"] != artifact.source_contract:
        raise OfficialTradingCalendarBlockedError("calendar source contract changed")
    if payload["statistics"] != artifact.statistics:
        raise OfficialTradingCalendarBlockedError("calendar statistics changed")
    return artifact


def _decode_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OfficialTradingCalendarBlockedError(
            f"{label} is not UTF-8 JSON"
        ) from exc

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise OfficialTradingCalendarBlockedError(
                    f"{label} contains duplicate JSON key {key!r}"
                )
            value[key] = item
        return value

    try:
        value = json.loads(text, object_pairs_hook=no_duplicates)
    except OfficialTradingCalendarBlockedError:
        raise
    except json.JSONDecodeError as exc:
        raise OfficialTradingCalendarBlockedError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise OfficialTradingCalendarBlockedError(f"{label} is not a JSON object")
    return value


def _validate_official_url(url: str, exchange: str) -> None:
    parsed = urlsplit(url)
    expected_host = "www.sse.com.cn" if exchange == "SSE" else "www.szse.cn"
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(".json")
    ):
        raise OfficialTradingCalendarBlockedError(
            f"unsafe or non-official calendar source URL: {url}"
        )


def _validate_catalog_url(url: str, exchange: str) -> None:
    parsed = urlsplit(url)
    expected = (
        ("query.sse.com.cn", "/search/getESSearchDoc.do")
        if exchange == "SSE"
        else ("www.szse.cn", "/api/search/content")
    )
    if (
        parsed.scheme != "https"
        or (parsed.hostname, parsed.path) != expected
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OfficialTradingCalendarBlockedError(
            f"unsafe or non-official calendar catalog URL: {url}"
        )


def _compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", "", normalized)


def _media_type(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfficialTradingCalendarBlockedError(
            "official response lacks Content-Type"
        )
    return value.split(";", 1)[0].strip().lower()


def _normalize_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise OfficialTradingCalendarBlockedError(
                f"invalid evidence timestamp: {value!r}"
            ) from exc
    else:
        raise OfficialTradingCalendarBlockedError(
            "evidence timestamp must be timezone-aware"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OfficialTradingCalendarBlockedError(
            "evidence timestamp must include a timezone"
        )
    return parsed.replace(microsecond=0).isoformat()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _cas_root_from_object_path(path: Path) -> Path:
    current = _lexical_absolute(path)
    if len(current.parents) < 3 or current.parent.parent.name != "sha256":
        raise OfficialTradingCalendarBlockedError("invalid calendar CAS object path")
    return current.parent.parent.parent


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_file_attributes", 0)),
        int(getattr(value, "st_reparse_tag", 0)),
    )


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
    )


def _open_directory_handle(path: Path) -> int:
    if os.name != "nt":
        flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(
            getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise OfficialTradingCalendarBlockedError(
                f"CAS directory cannot be opened safely: {path}"
            ) from exc
        value = os.fstat(descriptor)
        if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
            os.close(descriptor)
            raise OfficialTradingCalendarBlockedError(
                f"CAS directory handle is unsafe: {path}"
            )
        return descriptor

    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    handle_value = int(handle) if handle is not None else invalid
    if handle_value == invalid:
        error = ctypes.get_last_error()
        raise OfficialTradingCalendarBlockedError(
            f"CAS directory cannot be opened safely: {path} (WinError {error})"
        )
    try:
        _windows_directory_identity(handle_value)
    except Exception:
        _close_directory_handle(handle_value)
        raise
    return handle_value


def _windows_directory_identity(handle: int) -> tuple[int, ...]:
    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        )

    class FileId128(ctypes.Structure):
        _fields_ = (("identifier", ctypes.c_ubyte * 16),)

    class FileIdInfo(ctypes.Structure):
        _fields_ = (
            ("volume_serial_number", ctypes.c_ulonglong),
            ("file_id", FileId128),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    attributes = FileAttributeTagInfo()
    if not get_information(
        handle,
        9,  # FileAttributeTagInfo
        ctypes.byref(attributes),
        ctypes.sizeof(attributes),
    ):
        error = ctypes.get_last_error()
        raise OfficialTradingCalendarBlockedError(
            f"CAS directory attributes cannot be read (WinError {error})"
        )
    if not attributes.file_attributes & 0x00000010 or (
        attributes.file_attributes & 0x00000400
    ):
        raise OfficialTradingCalendarBlockedError(
            "CAS directory handle is not a non-reparse directory"
        )
    identity = FileIdInfo()
    if not get_information(
        handle,
        18,  # FileIdInfo
        ctypes.byref(identity),
        ctypes.sizeof(identity),
    ):
        error = ctypes.get_last_error()
        raise OfficialTradingCalendarBlockedError(
            f"CAS directory FileId cannot be read (WinError {error})"
        )
    return (
        int(identity.volume_serial_number),
        *(int(value) for value in identity.file_id.identifier),
    )


def _directory_handle_identity(handle: int) -> tuple[int, ...]:
    if os.name == "nt":
        return _windows_directory_identity(handle)
    value = os.fstat(handle)
    if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise OfficialTradingCalendarBlockedError(
            "CAS directory handle is not a non-reparse directory"
        )
    return _identity(value)


def _close_directory_handle(handle: int) -> None:
    if os.name != "nt":
        os.close(handle)
        return
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _verify_directory_identity(
    root: Path,
    path: Path,
    *,
    held_handle: int,
    expected_identity: tuple[int, ...],
) -> None:
    _path_snapshot(root, path, leaf_file=False)
    if _directory_handle_identity(held_handle) != expected_identity:
        raise OfficialTradingCalendarBlockedError(
            "CAS parent directory handle identity changed during write"
        )
    current_handle = _open_directory_handle(path)
    try:
        current_identity = _directory_handle_identity(current_handle)
    finally:
        _close_directory_handle(current_handle)
    if current_identity != expected_identity:
        raise OfficialTradingCalendarBlockedError(
            "CAS parent directory was replaced during write"
        )


def _is_link_or_reparse(value: os.stat_result) -> bool:
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400))
    return stat.S_ISLNK(value.st_mode) or bool(
        int(getattr(value, "st_file_attributes", 0)) & reparse
    )


def _path_snapshot(
    root: Path, target: Path, *, leaf_file: bool
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    root_value = _lexical_absolute(root)
    target_value = _lexical_absolute(target)
    try:
        relative = target_value.relative_to(root_value)
    except ValueError as exc:
        raise OfficialTradingCalendarBlockedError("CAS path escapes configured root") from exc
    components = [root_value]
    current = root_value
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise OfficialTradingCalendarBlockedError("invalid CAS path component")
        current /= part
        components.append(current)
    result: list[tuple[str, tuple[int, ...]]] = []
    for index, component in enumerate(components):
        try:
            value = os.lstat(component)
        except OSError as exc:
            raise OfficialTradingCalendarBlockedError(
                f"CAS path is missing or unsafe: {component}"
            ) from exc
        if _is_link_or_reparse(value):
            raise OfficialTradingCalendarBlockedError(
                f"CAS path contains a symlink or reparse point: {component}"
            )
        is_leaf = index == len(components) - 1
        if leaf_file and is_leaf:
            if not stat.S_ISREG(value.st_mode):
                raise OfficialTradingCalendarBlockedError(
                    f"CAS leaf is not a regular file: {component}"
                )
        elif not stat.S_ISDIR(value.st_mode):
            raise OfficialTradingCalendarBlockedError(
                f"CAS path component is not a directory: {component}"
            )
        result.append((str(component), _fingerprint(value)))
    return tuple(result)


def _stable_read(root: Path, path: Path) -> bytes:
    before = _path_snapshot(root, path, leaf_file=True)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfficialTradingCalendarBlockedError(
            f"CAS object cannot be opened safely: {path}"
        ) from exc
    try:
        handle_before = os.fstat(descriptor)
        if _is_link_or_reparse(handle_before) or not stat.S_ISREG(handle_before.st_mode):
            raise OfficialTradingCalendarBlockedError(
                f"CAS handle is not a regular non-reparse file: {path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        handle_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = _path_snapshot(root, path, leaf_file=True)
    if (
        before != after
        or _fingerprint(handle_before) != _fingerprint(handle_after)
        or _fingerprint(handle_before) != before[-1][1]
    ):
        raise OfficialTradingCalendarBlockedError(
            f"CAS object or parent changed during read: {path}"
        )
    return b"".join(chunks)


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    root = _lexical_absolute(root)
    path = _lexical_absolute(path)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise OfficialTradingCalendarBlockedError("CAS write escapes root") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    _path_snapshot(root, path.parent, leaf_file=False)
    if path.exists():
        if _stable_read(root, path) != content:
            raise OfficialTradingCalendarBlockedError(f"immutable CAS collision at {path}")
        return
    parent_handle = _open_directory_handle(path.parent)
    try:
        parent_identity = _directory_handle_identity(parent_handle)
        _verify_directory_identity(
            root,
            path.parent,
            held_handle=parent_handle,
            expected_identity=parent_identity,
        )
    except Exception:
        _close_directory_handle(parent_handle)
        raise
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_BINARY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            handle = os.fstat(descriptor)
            if _is_link_or_reparse(handle) or not stat.S_ISREG(handle.st_mode):
                raise OfficialTradingCalendarBlockedError(
                    "temporary CAS handle is unsafe"
                )
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        _verify_directory_identity(
            root,
            path.parent,
            held_handle=parent_handle,
            expected_identity=parent_identity,
        )
        _path_snapshot(root, temporary, leaf_file=True)
        os.replace(temporary, path)
        _verify_directory_identity(
            root,
            path.parent,
            held_handle=parent_handle,
            expected_identity=parent_identity,
        )
        _stable_read(root, path)
    finally:
        if temporary.exists():
            temporary.unlink()
        _close_directory_handle(parent_handle)


__all__ = [
    "EVIDENCE_COMPLETE",
    "END_YEAR",
    "CATALOG_BY_ID",
    "CATALOG_ORDER",
    "CATALOG_SPECS",
    "EXPECTED_CATALOG_ENTRY_COUNT",
    "EXPECTED_CATALOG_MEMBERSHIP_SHA256",
    "EXPECTED_OPEN_DAYS_BY_YEAR",
    "CalendarCatalogEntry",
    "CalendarCatalogEvidence",
    "CalendarCatalogSpec",
    "OfficialTradingCalendarArtifact",
    "OfficialTradingCalendarBlockedError",
    "OfficialTradingCalendarCAS",
    "OfficialTradingCalendarClient",
    "OfficialTradingCalendarManifestStore",
    "PROTOCOL_VERSION",
    "SOURCE_CONTRACT_ADMITTED",
    "SOURCE_INCOMPLETE",
    "SOURCE_ORDER",
    "SOURCE_REJECTED",
    "SOURCE_SPECS",
    "START_YEAR",
]
