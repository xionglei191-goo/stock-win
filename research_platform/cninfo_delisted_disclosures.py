from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests


PROTOCOL_VERSION = "cninfo-delisted-disclosures-v1"
MANIFEST_SCHEMA_VERSION = "cninfo-delisted-disclosures-manifest-v1"
NORMALIZED_SCHEMA_VERSION = "cninfo-delisted-announcements-v1"
PDF_PARSE_EVIDENCE_SCHEMA_VERSION = "cninfo-pdf-parse-evidence-v1"

CNINFO_STOCK_MASTER_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_HOME_URL = "https://www.cninfo.com.cn/"
CNINFO_PDF_HOSTS = frozenset({"static.cninfo.com.cn", "www.cninfo.com.cn"})

PAGE_SIZE = 30
AUDIT_START = "2018-01-01"
AUDIT_END = "2023-12-31"
MIN_STOCK_MASTER_ROWS = 1_000
MAX_STOCK_MASTER_BYTES = 4 * 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_TOTAL_PDF_BYTES = 8 * 1024 * 1024 * 1024
MAX_ANNOUNCEMENT_PAGES_PER_CODE = 1_000
MAX_DOCUMENTS = 50_000
MAX_PDF_PAGES = 5_000
MAX_EXTRACTED_TEXT_CHARACTERS = 10_000_000

SOURCE_AUTHORITY = "CNINFO_OFFICIAL_DISCLOSURE"
MASTER_BINDING_UNVERIFIED = "UNVERIFIED_EXTERNAL_MASTER_BINDING"
EFFECTIVE_AT_UNRESOLVED = "REQUIRES_EXTERNAL_TRADING_CALENDAR"
STRUCTURED_VALUES_UNRESOLVED = "STRUCTURED_FINANCIAL_VALUES_UNRESOLVED"
RAW_EVIDENCE_ONLY = "RAW_SOURCE_EVIDENCE_ONLY"

STOCK_MASTER_TOP_FIELDS = frozenset({"stockList"})
STOCK_MASTER_ROW_FIELDS = frozenset(
    {"category", "code", "orgId", "pinyin", "zwjc"}
)
ANNOUNCEMENT_TOP_FIELDS = frozenset(
    {
        "announcements",
        "categoryList",
        "classifiedAnnouncements",
        "hasMore",
        "totalAnnouncement",
        "totalRecordNum",
        "totalSecurities",
        "totalpages",
    }
)
ANNOUNCEMENT_ROW_FIELDS = frozenset(
    {
        "adjunctSize",
        "adjunctType",
        "adjunctUrl",
        "announcementContent",
        "announcementId",
        "announcementTime",
        "announcementTitle",
        "announcementType",
        "announcementTypeName",
        "associateAnnouncement",
        "batchNum",
        "columnId",
        "id",
        "important",
        "orgId",
        "orgName",
        "pageColumn",
        "secCode",
        "secName",
        "secNameList",
        "shortTitle",
        "storageTime",
        "tileSecName",
    }
)
ANNOUNCEMENT_POST_FIELDS = frozenset(
    {
        "pageNum",
        "pageSize",
        "column",
        "tabName",
        "plate",
        "stock",
        "searchkey",
        "secid",
        "category",
        "trade",
        "seDate",
        "sortName",
        "sortType",
        "isHLtitle",
    }
)

_CHINA = ZoneInfo("Asia/Shanghai")
_BUILDER_SEAL = object()


class CninfoDelistedDisclosureBlockedError(RuntimeError):
    """Official disclosure evidence failed the frozen, fail-closed contract."""

    def __init__(self, message: str, *, status: str = "SOURCE_REJECTED") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class FrozenDisclosureTarget:
    canonical_entity_id: str
    exchange: str
    code: str
    query_start: str
    query_end: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RawBlobEvidence:
    source_id: str
    role: str
    source_url: str
    method: str
    retrieved_at: str
    content_hash: str
    byte_count: int
    content_type: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CninfoDelistedDisclosureArtifact:
    master_snapshot_id: str
    targets: tuple[FrozenDisclosureTarget, ...]
    stock_master: RawBlobEvidence
    query_pages: tuple[Mapping[str, Any], ...]
    documents: tuple[Mapping[str, Any], ...]
    normalized_announcements: tuple[Mapping[str, Any], ...]
    classification_candidates: tuple[Mapping[str, Any], ...]
    logical_content_sha256: str
    parser_dependencies: Mapping[str, str]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _BUILDER_SEAL:
            raise TypeError("disclosure artifacts must be rebuilt from immutable raw bytes")

    @property
    def dataset_gates(self) -> dict[str, Any]:
        unresolved_effective = sum(
            item["effective_at_status"] == EFFECTIVE_AT_UNRESOLVED
            for item in self.normalized_announcements
        )
        return {
            "announcement_documents": {
                "ready": False,
                "status": RAW_EVIDENCE_ONLY,
                "raw_document_count": len(self.normalized_announcements),
                "unresolved_effective_at_count": unresolved_effective,
                "blocked_by": [
                    MASTER_BINDING_UNVERIFIED,
                    *([EFFECTIVE_AT_UNRESOLVED] if unresolved_effective else []),
                    "DELISTED_QUALITY_PARTITIONS_NOT_ASSEMBLED",
                ],
            },
            "financial_reports": {
                "ready": False,
                "status": STRUCTURED_VALUES_UNRESOLVED,
                "candidate_count": sum(
                    item["dataset"] == "financial_reports"
                    for item in self.classification_candidates
                ),
                "structured_values_emitted": 0,
            },
            "earnings_guidance_express": {
                "ready": False,
                "status": STRUCTURED_VALUES_UNRESOLVED,
                "candidate_count": sum(
                    item["dataset"] == "earnings_guidance_express"
                    for item in self.classification_candidates
                ),
                "structured_values_emitted": 0,
            },
        }

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": False,
            "status": MASTER_BINDING_UNVERIFIED,
            "authority": SOURCE_AUTHORITY,
            "scope": "FROZEN_DELISTED_INTERVAL_CODES",
            "read_only_methods": ["GET", "POST_QUERY"],
            "redirects_allowed": False,
            "cold_replay_required": True,
            "caller_ready_ignored": True,
            "trading_eligibility": False,
            "quality_dataset_eligibility": False,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        return {
            "target_count": len(self.targets),
            "query_page_count": len(self.query_pages),
            "announcement_count": len(self.normalized_announcements),
            "document_count": len(self.documents),
            "classification_candidate_count": len(self.classification_candidates),
            "financial_report_rows_emitted": 0,
            "earnings_guidance_express_rows_emitted": 0,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "master_snapshot_id": self.master_snapshot_id,
            "targets": [item.to_dict() for item in self.targets],
            "stock_master": self.stock_master.to_dict(),
            "query_pages": [dict(item) for item in self.query_pages],
            "documents": [dict(item) for item in self.documents],
            "normalized_announcements": [
                dict(item) for item in self.normalized_announcements
            ],
            "classification_candidates": [
                dict(item) for item in self.classification_candidates
            ],
            "logical_content_sha256": self.logical_content_sha256,
            "parser_dependencies": dict(self.parser_dependencies),
            "dataset_gates": self.dataset_gates,
            "source_contract": self.source_contract,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class CninfoDelistedDisclosureManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CninfoPdfParseEvidence:
    schema_version: str
    announcement_id: str
    announcement_row_sha256: str
    raw_content_sha256: str
    normalized_text_sha256: str
    pdf_text_status: str
    pdf_page_count: int
    pypdf_version: str
    classification_candidate: Mapping[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.classification_candidate is not None:
            value["classification_candidate"] = dict(
                self.classification_candidate
            )
        return value


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_datetime(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now().astimezone()
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise CninfoDelistedDisclosureBlockedError(
                "retrieved_at is not ISO-8601"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CninfoDelistedDisclosureBlockedError(
            "retrieved_at must include a timezone"
        )
    return parsed.replace(microsecond=0).isoformat()


def _strict_int(value: Any, field_name: str, *, nonnegative: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CninfoDelistedDisclosureBlockedError(
            f"{field_name} is not a strict integer"
        )
    if nonnegative and value < 0:
        raise CninfoDelistedDisclosureBlockedError(
            f"{field_name} must be non-negative"
        )
    return value


def _iso_date(value: Any, field_name: str) -> date:
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise CninfoDelistedDisclosureBlockedError(
            f"{field_name} is not a canonical ISO date"
        ) from exc
    if parsed.isoformat() != text:
        raise CninfoDelistedDisclosureBlockedError(
            f"{field_name} is not a canonical ISO date"
        )
    return parsed


def _normalize_target(value: FrozenDisclosureTarget) -> FrozenDisclosureTarget:
    if not isinstance(value, FrozenDisclosureTarget):
        raise TypeError("targets must contain FrozenDisclosureTarget values")
    exchange = str(value.exchange).upper()
    if exchange not in {"SSE", "SZSE"}:
        raise CninfoDelistedDisclosureBlockedError("target exchange is not SSE/SZSE")
    suffix = ".SH" if exchange == "SSE" else ".SZ"
    code = str(value.code).upper()
    if not re.fullmatch(r"\d{6}" + re.escape(suffix), code):
        raise CninfoDelistedDisclosureBlockedError("target code is invalid")
    start = _iso_date(value.query_start, "query_start")
    end = _iso_date(value.query_end, "query_end")
    if end < start:
        raise CninfoDelistedDisclosureBlockedError("target query interval is reversed")
    if start < date.fromisoformat(AUDIT_START) or end > date.fromisoformat(AUDIT_END):
        raise CninfoDelistedDisclosureBlockedError(
            "target query interval escapes the frozen 2018-2023 audit window"
        )
    entity = str(value.canonical_entity_id).strip()
    if not entity or len(entity) > 200:
        raise CninfoDelistedDisclosureBlockedError(
            "target canonical_entity_id is invalid"
        )
    return FrozenDisclosureTarget(entity, exchange, code, start.isoformat(), end.isoformat())


def _normalize_targets(
    values: Sequence[FrozenDisclosureTarget],
) -> tuple[FrozenDisclosureTarget, ...]:
    if not values:
        raise CninfoDelistedDisclosureBlockedError("no frozen disclosure targets")
    output = tuple(
        sorted(
            (_normalize_target(value) for value in values),
            key=lambda item: (item.exchange, item.code, item.query_start),
        )
    )
    seen_codes: set[str] = set()
    for target in output:
        if target.code in seen_codes:
            raise CninfoDelistedDisclosureBlockedError(
                f"duplicate frozen target code: {target.code}"
            )
        seen_codes.add(target.code)
    return output


def _decode_json_object(raw: bytes, label: str, maximum: int) -> dict[str, Any]:
    if not raw or len(raw) > maximum:
        raise CninfoDelistedDisclosureBlockedError(f"{label} is empty or oversized")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CninfoDelistedDisclosureBlockedError(
            f"{label} is not UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CninfoDelistedDisclosureBlockedError(f"{label} is not a JSON object")
    return value


def parse_cninfo_stock_master(raw: bytes) -> dict[str, Mapping[str, Any]]:
    value = _decode_json_object(raw, "CNINFO stock master", MAX_STOCK_MASTER_BYTES)
    if set(value) != STOCK_MASTER_TOP_FIELDS:
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO stock-master top-level schema drift"
        )
    rows = value["stockList"]
    if not isinstance(rows, list) or len(rows) < MIN_STOCK_MASTER_ROWS:
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO stock-master coverage is below the admitted floor"
        )
    output: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != STOCK_MASTER_ROW_FIELDS:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO stock-master row schema drift"
            )
        code = str(row["code"] or "").strip()
        org_id = str(row["orgId"] or "").strip()
        if not re.fullmatch(r"\d{6}", code) or not org_id:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO stock-master code/orgId is invalid"
            )
        if code in output:
            raise CninfoDelistedDisclosureBlockedError(
                f"CNINFO stock master has duplicate code: {code}"
            )
        output[code] = dict(row)
    return output


def _announcement_request(target: FrozenDisclosureTarget, org_id: str, page: int) -> dict[str, str]:
    return {
        "pageNum": str(page),
        "pageSize": str(PAGE_SIZE),
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{target.code[:6]},{org_id}",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{target.query_start}~{target.query_end}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }


def _parse_announcement_page(
    raw: bytes,
    *,
    target: FrozenDisclosureTarget,
    org_id: str,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    value = _decode_json_object(raw, "CNINFO announcement page", MAX_PAGE_BYTES)
    if set(value) != ANNOUNCEMENT_TOP_FIELDS:
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO announcement top-level schema drift"
        )
    total = _strict_int(value["totalAnnouncement"], "totalAnnouncement")
    if _strict_int(value["totalRecordNum"], "totalRecordNum") != total:
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO totalRecordNum disagrees with totalAnnouncement"
        )
    reported_pages = _strict_int(value["totalpages"], "totalpages")
    _strict_int(value["totalSecurities"], "totalSecurities")
    if not isinstance(value["hasMore"], bool):
        raise CninfoDelistedDisclosureBlockedError("CNINFO hasMore is not boolean")
    for field_name in ("categoryList", "classifiedAnnouncements"):
        if value[field_name] is not None:
            raise CninfoDelistedDisclosureBlockedError(
                f"CNINFO {field_name} changed under the frozen empty-filter query"
            )
    rows = value["announcements"]
    if not isinstance(rows, list):
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO announcements is not an array"
        )
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != ANNOUNCEMENT_ROW_FIELDS:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcement row schema drift"
            )
        code = str(row["secCode"] or "").strip()
        observed_org_id = str(row["orgId"] or "").strip()
        announcement_id = str(row["announcementId"] or "").strip()
        title = str(row["announcementTitle"] or "").strip()
        adjunct_type = str(row["adjunctType"] or "").strip().upper()
        if code != target.code[:6] or observed_org_id != org_id:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcement code/orgId does not match stock master"
            )
        if not re.fullmatch(r"\d+", announcement_id) or not title:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcement identity/title is invalid"
            )
        if "<" in title or ">" in title:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcement title contains unexpected markup"
            )
        if adjunct_type != "PDF":
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcement document is not PDF"
            )
        if isinstance(row["announcementTime"], bool) or not isinstance(
            row["announcementTime"], int
        ):
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcementTime is not epoch milliseconds"
            )
        normalized.append(dict(row))
    return (
        {
            "total": total,
            "reported_totalpages": reported_pages,
            "has_more": value["hasMore"],
            "row_count": len(rows),
        },
        tuple(normalized),
    )


def _normalize_pdf_url(value: Any, announcement_id: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CninfoDelistedDisclosureBlockedError("CNINFO PDF URL is empty")
    if text.startswith("/"):
        text = text.lstrip("/")
    if not text.lower().startswith("https://"):
        text = f"https://static.cninfo.com.cn/{text}"
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in CNINFO_PDF_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or ".." in parsed.path.split("/")
        or not parsed.path.startswith("/finalpage/")
    ):
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO PDF URL escaped the admitted origin/path"
        )
    if not re.search(rf"/{re.escape(announcement_id)}\.PDF$", parsed.path, re.I):
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO PDF filename does not bind to announcementId"
        )
    return text


def _announcement_time(value: int) -> tuple[str, str, str | None, str]:
    if value <= 0 or value > 9_999_999_999_999:
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO announcementTime is outside the admitted epoch range"
        )
    try:
        observed_utc = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO announcementTime cannot be parsed"
        ) from exc
    if observed_utc.microsecond:
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO announcementTime has sub-second precision drift"
        )
    observed_local = observed_utc.astimezone(_CHINA).replace(microsecond=0)
    if observed_local.time().replace(tzinfo=None) == datetime.min.time():
        published = observed_local
        return (
            published.isoformat(),
            "DATE_ONLY",
            None,
            EFFECTIVE_AT_UNRESOLVED,
        )
    published = observed_local
    return published.isoformat(), "TIMESTAMP", published.isoformat(), "SOURCE_TIMESTAMP"


def _optional_nonnegative_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise CninfoDelistedDisclosureBlockedError(f"{field_name} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CninfoDelistedDisclosureBlockedError(
            f"{field_name} is invalid"
        ) from exc
    if str(value).strip() != str(parsed) or parsed < 0:
        raise CninfoDelistedDisclosureBlockedError(f"{field_name} is invalid")
    return parsed


def _normalized_announcement_base(
    *,
    target: FrozenDisclosureTarget,
    row: Mapping[str, Any],
    evidence: RawBlobEvidence,
) -> dict[str, Any]:
    announcement_id = str(row["announcementId"])
    expected_url = _normalize_pdf_url(row["adjunctUrl"], announcement_id)
    if evidence.source_url != expected_url:
        raise CninfoDelistedDisclosureBlockedError(
            "document URL does not match announcement page"
        )
    published_at, precision, effective_at, effective_status = _announcement_time(
        int(row["announcementTime"])
    )
    return {
        "schema_version": NORMALIZED_SCHEMA_VERSION,
        "exchange": target.exchange,
        "code": target.code,
        "org_id": str(row["orgId"]),
        "announcement_id": announcement_id,
        "announcement_type": str(
            row.get("announcementTypeName") or row.get("announcementType") or ""
        ),
        "title": str(row["announcementTitle"]),
        "published_at": published_at,
        "publication_precision": precision,
        "effective_at": effective_at,
        "effective_at_status": effective_status,
        "url": expected_url,
        "content_hash": evidence.content_hash,
        "document_size_kb": _optional_nonnegative_int(
            row.get("adjunctSize"), "adjunctSize"
        ),
    }


def _extract_pdf_text(raw: bytes) -> tuple[str, str, int, str]:
    try:
        import pypdf
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        return "", "DEPENDENCY_MISSING", 0, ""
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        pages = len(reader.pages)
        if pages <= 0 or pages > MAX_PDF_PAGES:
            return "", "PDF_PAGE_COUNT_INVALID", pages, ""
        parts: list[str] = []
        character_count = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            character_count += len(page_text)
            if character_count > MAX_EXTRACTED_TEXT_CHARACTERS:
                return "", "PDF_TEXT_OVERSIZED", pages, ""
            parts.append(page_text)
        text = "\n".join(parts)
    except Exception:
        return "", "PDF_TEXT_UNREADABLE", 0, ""
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized, "EXTRACTED_BY_PYPDF", pages, str(pypdf.__version__)


def _period_end(year: int, period: str) -> str:
    suffix = {
        "ANNUAL": "12-31",
        "Q1": "03-31",
        "HALF_YEAR": "06-30",
        "Q3": "09-30",
    }[period]
    return f"{year:04d}-{suffix}"


def _find_year(value: str) -> int | None:
    match = re.search(r"(?<!\d)(20\d{2})(?:年|\s)", value)
    if match is None:
        return None
    year = int(match.group(1))
    return year if 2000 <= year <= 2099 else None


def _classify_document(
    announcement: Mapping[str, Any], pdf_text: str
) -> dict[str, Any] | None:
    if not pdf_text:
        return None
    title = str(announcement["title"])
    lower_title = title.casefold()
    lower_text = pdf_text.casefold()
    if "摘要" in title or "summary" in lower_title:
        return None
    year = _find_year(title)
    if year is None or str(year) not in pdf_text:
        return None

    report_markers = (
        ("Q1", ("第一季度报告", "first quarter report")),
        ("HALF_YEAR", ("半年度报告", "semi-annual report", "half-year report")),
        ("Q3", ("第三季度报告", "third quarter report")),
        ("ANNUAL", ("年度报告", "annual report")),
    )
    for report_type, markers in report_markers:
        marker = next(
            (
                candidate
                for candidate in markers
                if candidate.casefold() in lower_title
                and candidate.casefold() in lower_text
            ),
            None,
        )
        if marker is not None:
            return {
                "dataset": "financial_reports",
                "candidate_type": report_type,
                "period_end_candidate": _period_end(year, report_type),
                "classification_rule": "TITLE_AND_PDF_TEXT_EXACT_MARKER",
                "matched_marker": marker,
            }

    event_rules = (
        ("GUIDANCE", ("业绩预告", "earnings forecast")),
        ("EXPRESS", ("业绩快报", "earnings express")),
    )
    period_rules = (
        ("Q1", ("第一季度", "first quarter")),
        ("HALF_YEAR", ("半年度", "semi-annual", "half-year")),
        ("Q3", ("前三季度", "第三季度", "third quarter")),
        ("ANNUAL", ("年度", "annual")),
    )
    for event_type, markers in event_rules:
        event_marker = next(
            (
                marker
                for marker in markers
                if marker.casefold() in lower_title and marker.casefold() in lower_text
            ),
            None,
        )
        if event_marker is None:
            continue
        period = next(
            (
                period_type
                for period_type, markers_for_period in period_rules
                if any(
                    marker.casefold() in lower_title
                    for marker in markers_for_period
                )
            ),
            None,
        )
        if period is None:
            return None
        return {
            "dataset": "earnings_guidance_express",
            "candidate_type": event_type,
            "period_end_candidate": _period_end(year, period),
            "classification_rule": "TITLE_AND_PDF_TEXT_EXACT_MARKER",
            "matched_marker": event_marker,
        }
    return None


def _build_pdf_parse_evidence(
    *,
    announcement_row: Mapping[str, Any],
    normalized_announcement: Mapping[str, Any],
    raw_content_sha256: str,
    pdf_raw: bytes,
) -> CninfoPdfParseEvidence:
    announcement_id = str(normalized_announcement.get("announcement_id") or "")
    if not re.fullmatch(r"\d+", announcement_id):
        raise CninfoDelistedDisclosureBlockedError(
            "parse-evidence announcement_id is invalid"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", raw_content_sha256):
        raise CninfoDelistedDisclosureBlockedError(
            "parse-evidence raw content hash is invalid"
        )
    text, text_status, page_count, observed_pypdf_version = _extract_pdf_text(
        pdf_raw
    )
    if observed_pypdf_version and observed_pypdf_version != _pypdf_version():
        raise CninfoDelistedDisclosureBlockedError(
            "pypdf dependency changed during reconstruction"
        )
    text_hash = _sha256(text.encode("utf-8")) if text else ""
    classification = _classify_document(normalized_announcement, text)
    return CninfoPdfParseEvidence(
        schema_version=PDF_PARSE_EVIDENCE_SCHEMA_VERSION,
        announcement_id=announcement_id,
        announcement_row_sha256=_sha256(_canonical_json_bytes(announcement_row)),
        raw_content_sha256=raw_content_sha256,
        normalized_text_sha256=text_hash,
        pdf_text_status=text_status,
        pdf_page_count=page_count,
        pypdf_version=_pypdf_version(),
        classification_candidate=classification,
    )


def _validate_pdf_parse_evidence(
    value: Mapping[str, Any],
    *,
    announcement_row: Mapping[str, Any],
    normalized_announcement: Mapping[str, Any],
    raw_content_sha256: str,
) -> CninfoPdfParseEvidence:
    expected = {
        "schema_version",
        "announcement_id",
        "announcement_row_sha256",
        "raw_content_sha256",
        "normalized_text_sha256",
        "pdf_text_status",
        "pdf_page_count",
        "pypdf_version",
        "classification_candidate",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CninfoDelistedDisclosureBlockedError(
            "PDF parse-evidence schema drift"
        )
    announcement_id = str(normalized_announcement.get("announcement_id") or "")
    candidate_value = value.get("classification_candidate")
    if candidate_value is not None and not isinstance(candidate_value, Mapping):
        raise CninfoDelistedDisclosureBlockedError(
            "PDF parse-evidence classification is invalid"
        )
    evidence = CninfoPdfParseEvidence(
        schema_version=str(value.get("schema_version") or ""),
        announcement_id=str(value.get("announcement_id") or ""),
        announcement_row_sha256=str(
            value.get("announcement_row_sha256") or ""
        ),
        raw_content_sha256=str(value.get("raw_content_sha256") or ""),
        normalized_text_sha256=str(
            value.get("normalized_text_sha256") or ""
        ),
        pdf_text_status=str(value.get("pdf_text_status") or ""),
        pdf_page_count=value.get("pdf_page_count"),  # type: ignore[arg-type]
        pypdf_version=str(value.get("pypdf_version") or ""),
        classification_candidate=(
            None if candidate_value is None else dict(candidate_value)
        ),
    )
    if (
        evidence.schema_version != PDF_PARSE_EVIDENCE_SCHEMA_VERSION
        or evidence.announcement_id != announcement_id
        or evidence.announcement_row_sha256
        != _sha256(_canonical_json_bytes(announcement_row))
        or evidence.raw_content_sha256 != raw_content_sha256
        or not (
            evidence.normalized_text_sha256 == ""
            or re.fullmatch(
                r"[0-9a-f]{64}", evidence.normalized_text_sha256
            )
        )
        or type(evidence.pdf_page_count) is not int
        or evidence.pdf_page_count < 0
        or evidence.pypdf_version != _pypdf_version()
    ):
        raise CninfoDelistedDisclosureBlockedError(
            "PDF parse-evidence identity mismatch"
        )
    return evidence


def _media_type(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _pypdf_version() -> str:
    try:
        import pypdf
    except ModuleNotFoundError:
        return "MISSING"
    return str(pypdf.__version__)


def _logical_hash(
    master_snapshot_id: str,
    targets: Sequence[FrozenDisclosureTarget],
    stock_master: RawBlobEvidence,
    query_pages: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    normalized: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> str:
    return _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": PROTOCOL_VERSION,
                "master_snapshot_id": master_snapshot_id,
                "targets": [item.to_dict() for item in targets],
                "source_hashes": [
                    stock_master.content_hash,
                    *[item["raw"]["content_hash"] for item in query_pages],
                    *[item["raw"]["content_hash"] for item in documents],
                ],
                "normalized_announcements": list(normalized),
                "classification_candidates": list(candidates),
            }
        )
    )


def _raw_from_mapping(value: Mapping[str, Any]) -> RawBlobEvidence:
    expected = {
        "source_id",
        "role",
        "source_url",
        "method",
        "retrieved_at",
        "content_hash",
        "byte_count",
        "content_type",
        "object_path",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CninfoDelistedDisclosureBlockedError("raw evidence schema drift")
    evidence = RawBlobEvidence(**dict(value))
    if _canonical_datetime(evidence.retrieved_at) != evidence.retrieved_at:
        raise CninfoDelistedDisclosureBlockedError(
            "raw evidence retrieved_at is not canonical"
        )
    if evidence.method not in {"GET", "POST"}:
        raise CninfoDelistedDisclosureBlockedError("raw evidence method is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence.content_hash):
        raise CninfoDelistedDisclosureBlockedError("raw evidence hash is invalid")
    if type(evidence.byte_count) is not int or evidence.byte_count <= 0:
        raise CninfoDelistedDisclosureBlockedError(
            "raw evidence byte_count is invalid"
        )
    return evidence


class CninfoDisclosureCAS:
    """Immutable, stable-read CAS for exact CNINFO response and PDF bytes."""

    def __init__(self, root: Path) -> None:
        self.root = _lexical_absolute(Path(root))
        _prepare_root(self.root)

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        if not isinstance(content, bytes) or not content:
            raise CninfoDelistedDisclosureBlockedError(
                "refusing to store an empty/non-bytes CAS object"
            )
        digest = _sha256(content)
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(self.root, path, content)
        persisted = _stable_read(self.root, path)
        if persisted != content or _sha256(persisted) != digest:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO disclosure CAS verification failed"
            )
        return digest, path

    def read_blob(self, digest: str, *, expected_path: Any | None = None) -> tuple[bytes, Path]:
        normalized = str(digest or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise CninfoDelistedDisclosureBlockedError("invalid CAS SHA-256")
        path = self.root / "sha256" / normalized[:2] / normalized
        if expected_path is not None and _lexical_absolute(Path(str(expected_path))) != path:
            raise CninfoDelistedDisclosureBlockedError("CAS object_path mismatch")
        content = _stable_read(self.root, path)
        if _sha256(content) != normalized:
            raise CninfoDelistedDisclosureBlockedError("CAS object hash mismatch")
        return content, path

    def capture(
        self,
        content: bytes,
        *,
        source_id: str,
        role: str,
        source_url: str,
        method: str,
        retrieved_at: datetime | str,
        content_type: str,
    ) -> RawBlobEvidence:
        digest, path = self.put_blob(content)
        return RawBlobEvidence(
            source_id=str(source_id),
            role=str(role),
            source_url=str(source_url),
            method=str(method),
            retrieved_at=_canonical_datetime(retrieved_at),
            content_hash=digest,
            byte_count=len(content),
            content_type=str(content_type),
            object_path=str(path),
        )


class CninfoDelistedDisclosureClient:
    """Read-only CNINFO collector for a verified caller-supplied frozen scope."""

    def __init__(
        self,
        *,
        cas: CninfoDisclosureCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cas, CninfoDisclosureCAS):
            raise TypeError("cas must be CninfoDisclosureCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now().astimezone())

    def capture_stock_master(
        self,
    ) -> tuple[RawBlobEvidence, dict[str, Mapping[str, Any]]]:
        """Capture and validate one immutable CNINFO stock-master response."""

        master_raw, master_type = self._get_json(
            CNINFO_STOCK_MASTER_URL, maximum=MAX_STOCK_MASTER_BYTES
        )
        evidence = self.cas.capture(
            master_raw,
            source_id="CNINFO_STOCK_MASTER",
            role="STOCK_MASTER",
            source_url=CNINFO_STOCK_MASTER_URL,
            method="GET",
            retrieved_at=self._observed_at(),
            content_type=master_type,
        )
        return evidence, parse_cninfo_stock_master(master_raw)

    def capture_announcement_page(
        self,
        *,
        target: FrozenDisclosureTarget,
        org_id: str,
        page: int,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], dict[str, Any]]:
        """Capture one page with its exact frozen POST scope."""

        normalized_target = _normalize_target(target)
        normalized_org_id = str(org_id or "").strip()
        if not normalized_org_id:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcement orgId is empty"
            )
        if type(page) is not int or page <= 0:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcement page number is invalid"
            )
        request = _announcement_request(normalized_target, normalized_org_id, page)
        raw, content_type = self._post_query(request)
        summary, rows = _parse_announcement_page(
            raw,
            target=normalized_target,
            org_id=normalized_org_id,
        )
        evidence = self.cas.capture(
            raw,
            source_id=f"CNINFO_ANNOUNCEMENTS_{normalized_target.code}_{page}",
            role="ANNOUNCEMENT_PAGE",
            source_url=CNINFO_ANNOUNCEMENT_URL,
            method="POST",
            retrieved_at=self._observed_at(),
            content_type=content_type,
        )
        return (
            summary,
            rows,
            {
                "exchange": normalized_target.exchange,
                "code": normalized_target.code,
                "org_id": normalized_org_id,
                "query_start": normalized_target.query_start,
                "query_end": normalized_target.query_end,
                "page_num": page,
                "page_size": PAGE_SIZE,
                "request": request,
                "raw": evidence.to_dict(),
            },
        )

    def capture_document(
        self,
        *,
        target: FrozenDisclosureTarget,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Capture one PDF after its announcement row has been admitted."""

        normalized_target = _normalize_target(target)
        if not isinstance(row, Mapping):
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcement row is invalid"
            )
        announcement_id = str(row.get("announcementId") or "").strip()
        if not re.fullmatch(r"\d+", announcement_id):
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO announcementId is invalid"
            )
        if str(row.get("secCode") or "").strip() != normalized_target.code[:6]:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO document row escaped the frozen target"
            )
        pdf_url = _normalize_pdf_url(row.get("adjunctUrl"), announcement_id)
        pdf_raw, content_type = self._get_pdf(pdf_url)
        evidence = self.cas.capture(
            pdf_raw,
            source_id=f"CNINFO_PDF_{announcement_id}",
            role="SOURCE_DOCUMENT",
            source_url=pdf_url,
            method="GET",
            retrieved_at=self._observed_at(),
            content_type=content_type,
        )
        return {
            "exchange": normalized_target.exchange,
            "code": normalized_target.code,
            "announcement_id": announcement_id,
            "raw": evidence.to_dict(),
        }

    def fetch(
        self,
        *,
        master_snapshot_id: str,
        targets: Sequence[FrozenDisclosureTarget],
    ) -> CninfoDelistedDisclosureArtifact:
        snapshot_id = str(master_snapshot_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
            raise CninfoDelistedDisclosureBlockedError(
                "external master_snapshot_id is not SHA-256"
            )
        frozen_targets = _normalize_targets(targets)
        stock_master_evidence, stock_master = self.capture_stock_master()

        page_values: list[dict[str, Any]] = []
        all_rows: list[tuple[FrozenDisclosureTarget, dict[str, Any]]] = []
        global_ids: set[str] = set()
        for target in frozen_targets:
            code = target.code[:6]
            master_row = stock_master.get(code)
            if master_row is None:
                raise CninfoDelistedDisclosureBlockedError(
                    f"CNINFO stock master has no frozen target: {target.code}"
                )
            org_id = str(master_row["orgId"])
            page = 1
            expected_total: int | None = None
            expected_page_count: int | None = None
            target_rows: list[dict[str, Any]] = []
            while True:
                if page > MAX_ANNOUNCEMENT_PAGES_PER_CODE:
                    raise CninfoDelistedDisclosureBlockedError(
                        "CNINFO announcement pagination exceeds safety limit"
                    )
                summary, rows, page_value = self.capture_announcement_page(
                    target=target,
                    org_id=org_id,
                    page=page,
                )
                if expected_total is None:
                    expected_total = int(summary["total"])
                    expected_page_count = max(1, math.ceil(expected_total / PAGE_SIZE))
                    if int(summary["reported_totalpages"]) != expected_page_count - 1:
                        raise CninfoDelistedDisclosureBlockedError(
                            "CNINFO totalpages semantics changed"
                        )
                    if expected_total > MAX_DOCUMENTS:
                        raise CninfoDelistedDisclosureBlockedError(
                            "CNINFO document count exceeds safety limit"
                        )
                elif (
                    summary["total"] != expected_total
                    or summary["reported_totalpages"] != expected_page_count - 1
                ):
                    raise CninfoDelistedDisclosureBlockedError(
                        "CNINFO pagination totals changed across pages"
                    )
                assert expected_page_count is not None
                expected_has_more = page < expected_page_count
                if summary["has_more"] is not expected_has_more:
                    raise CninfoDelistedDisclosureBlockedError(
                        "CNINFO hasMore disagrees with frozen pagination"
                    )
                expected_rows = (
                    PAGE_SIZE
                    if page < expected_page_count
                    else expected_total - PAGE_SIZE * (expected_page_count - 1)
                )
                if int(summary["row_count"]) != expected_rows:
                    raise CninfoDelistedDisclosureBlockedError(
                        "CNINFO page row count disagrees with total"
                    )
                page_values.append(page_value)
                for row in rows:
                    announcement_id = str(row["announcementId"])
                    if announcement_id in global_ids:
                        raise CninfoDelistedDisclosureBlockedError(
                            f"duplicate announcementId: {announcement_id}"
                        )
                    global_ids.add(announcement_id)
                    target_rows.append(row)
                if page == expected_page_count:
                    break
                page += 1
            if len(target_rows) != expected_total:
                raise CninfoDelistedDisclosureBlockedError(
                    "CNINFO pagination did not reproduce totalAnnouncement"
                )
            all_rows.extend((target, row) for row in target_rows)

        if len(all_rows) > MAX_DOCUMENTS:
            raise CninfoDelistedDisclosureBlockedError(
                "aggregate CNINFO document count exceeds safety limit"
            )
        document_values: list[dict[str, Any]] = []
        total_pdf_bytes = 0
        for target, row in all_rows:
            document = self.capture_document(target=target, row=row)
            evidence = _raw_from_mapping(document["raw"])
            total_pdf_bytes += evidence.byte_count
            if total_pdf_bytes > MAX_TOTAL_PDF_BYTES:
                raise CninfoDelistedDisclosureBlockedError(
                    "aggregate CNINFO PDF bytes exceed safety limit"
                )
            document_values.append(document)

        return _rebuild_artifact(
            cas=self.cas,
            master_snapshot_id=snapshot_id,
            targets=frozen_targets,
            stock_master=stock_master_evidence.to_dict(),
            query_pages=page_values,
            documents=document_values,
        )

    def _observed_at(self) -> str:
        observed = self.clock()
        if not isinstance(observed, datetime):
            raise CninfoDelistedDisclosureBlockedError("clock must return datetime")
        return _canonical_datetime(observed)

    def _get_json(self, url: str, *, maximum: int) -> tuple[bytes, str]:
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://www.cninfo.com.cn",
            "Referer": CNINFO_HOME_URL,
            "User-Agent": "tdx-research-platform/cninfo-delisted-disclosures-v1",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            response = self.session.get(
                url,
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CninfoDelistedDisclosureBlockedError(
                f"CNINFO GET failed: {exc}", status="SOURCE_UNAVAILABLE"
            ) from exc
        return self._admit_response(
            response,
            expected_url=url,
            expected_type="application/json",
            maximum=maximum,
            magic=None,
        )

    def _post_query(self, request: Mapping[str, str]) -> tuple[bytes, str]:
        if set(request) != ANNOUNCEMENT_POST_FIELDS:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO POST parameter schema changed"
            )
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.cninfo.com.cn",
            "Referer": CNINFO_HOME_URL,
            "User-Agent": "tdx-research-platform/cninfo-delisted-disclosures-v1",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            response = self.session.post(
                CNINFO_ANNOUNCEMENT_URL,
                data=dict(request),
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CninfoDelistedDisclosureBlockedError(
                f"CNINFO POST query failed: {exc}", status="SOURCE_UNAVAILABLE"
            ) from exc
        return self._admit_response(
            response,
            expected_url=CNINFO_ANNOUNCEMENT_URL,
            expected_type="application/json",
            maximum=MAX_PAGE_BYTES,
            magic=None,
        )

    def _get_pdf(self, url: str) -> tuple[bytes, str]:
        _normalize_pdf_url(url, Path(urlsplit(url).path).stem)
        try:
            response = self.session.get(
                url,
                headers={
                    "Accept": "application/pdf",
                    "Referer": CNINFO_HOME_URL,
                    "User-Agent": "tdx-research-platform/cninfo-delisted-disclosures-v1",
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CninfoDelistedDisclosureBlockedError(
                f"CNINFO PDF GET failed: {exc}", status="SOURCE_UNAVAILABLE"
            ) from exc
        return self._admit_response(
            response,
            expected_url=url,
            expected_type="application/pdf",
            maximum=MAX_PDF_BYTES,
            magic=b"%PDF-",
        )

    @staticmethod
    def _admit_response(
        response: Any,
        *,
        expected_url: str,
        expected_type: str,
        maximum: int,
        magic: bytes | None,
    ) -> tuple[bytes, str]:
        if int(response.status_code) != 200:
            raise CninfoDelistedDisclosureBlockedError(
                f"CNINFO HTTP status is {response.status_code}"
            )
        if str(response.url) != expected_url:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO response URL changed or redirected"
            )
        content_type = _media_type(response.headers.get("Content-Type"))
        if content_type != expected_type:
            raise CninfoDelistedDisclosureBlockedError(
                f"CNINFO content type changed: {content_type!r}"
            )
        raw = response.content
        if not isinstance(raw, bytes):
            raw = bytes(raw)
        if not raw or len(raw) > maximum:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO response is empty or oversized"
            )
        if magic is not None and not raw.startswith(magic):
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO PDF magic bytes are invalid"
            )
        return raw, content_type


class CninfoDelistedDisclosureManifestStore:
    """Seal/replay manifests only after full reconstruction from raw CAS bytes."""

    def __init__(self, cas: CninfoDisclosureCAS) -> None:
        if not isinstance(cas, CninfoDisclosureCAS):
            raise TypeError("cas must be CninfoDisclosureCAS")
        self.cas = cas

    def seal(
        self, artifact: CninfoDelistedDisclosureArtifact
    ) -> CninfoDelistedDisclosureManifestReference:
        if not isinstance(artifact, CninfoDelistedDisclosureArtifact):
            raise TypeError("artifact must be CninfoDelistedDisclosureArtifact")
        payload = _manifest_payload(artifact)
        rebuilt = _rebuild_from_manifest(payload, self.cas)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != content:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO disclosure artifact does not replay from raw bytes"
            )
        digest, path = self.cas.put_blob(content)
        return CninfoDelistedDisclosureManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def seal_candidate_for_full_replay(
        self, artifact: CninfoDelistedDisclosureArtifact
    ) -> CninfoDelistedDisclosureManifestReference:
        """Persist an untrusted candidate that must be replayed before admission."""

        if not isinstance(artifact, CninfoDelistedDisclosureArtifact):
            raise TypeError("artifact must be CninfoDelistedDisclosureArtifact")
        content = _canonical_json_bytes(_manifest_payload(artifact))
        digest, path = self.cas.put_blob(content)
        return CninfoDelistedDisclosureManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def replay(self, manifest_sha256: str) -> CninfoDelistedDisclosureArtifact:
        raw, _path = self.cas.read_blob(manifest_sha256)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO disclosure manifest is invalid"
            ) from exc
        if not isinstance(payload, dict) or _canonical_json_bytes(payload) != raw:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO disclosure manifest is not canonical JSON"
            )
        rebuilt = _rebuild_from_manifest(payload, self.cas)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != raw:
            raise CninfoDelistedDisclosureBlockedError(
                "CNINFO disclosure manifest does not replay exactly"
            )
        return rebuilt


def _manifest_payload(artifact: CninfoDelistedDisclosureArtifact) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "master_snapshot_id": artifact.master_snapshot_id,
        "master_binding_status": MASTER_BINDING_UNVERIFIED,
        "targets": [item.to_dict() for item in artifact.targets],
        "stock_master": artifact.stock_master.to_dict(),
        "query_pages": [dict(item) for item in artifact.query_pages],
        "documents": [dict(item) for item in artifact.documents],
        "normalized_announcements": [
            dict(item) for item in artifact.normalized_announcements
        ],
        "classification_candidates": [
            dict(item) for item in artifact.classification_candidates
        ],
        "logical_content_sha256": artifact.logical_content_sha256,
        "parser_dependencies": dict(artifact.parser_dependencies),
        "dataset_gates": artifact.dataset_gates,
        "source_contract": artifact.source_contract,
        "statistics": artifact.statistics,
    }


def _rebuild_from_manifest(
    payload: Mapping[str, Any], cas: CninfoDisclosureCAS
) -> CninfoDelistedDisclosureArtifact:
    expected = {
        "protocol_version",
        "manifest_schema_version",
        "master_snapshot_id",
        "master_binding_status",
        "targets",
        "stock_master",
        "query_pages",
        "documents",
        "normalized_announcements",
        "classification_candidates",
        "logical_content_sha256",
        "parser_dependencies",
        "dataset_gates",
        "source_contract",
        "statistics",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO disclosure manifest schema drift"
        )
    if (
        payload["protocol_version"] != PROTOCOL_VERSION
        or payload["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION
        or payload["master_binding_status"] != MASTER_BINDING_UNVERIFIED
    ):
        raise CninfoDelistedDisclosureBlockedError(
            "CNINFO disclosure manifest protocol changed"
        )
    targets_value = payload["targets"]
    if not isinstance(targets_value, list):
        raise CninfoDelistedDisclosureBlockedError("manifest targets are invalid")
    targets = _normalize_targets(
        tuple(FrozenDisclosureTarget(**dict(item)) for item in targets_value)
    )
    artifact = _rebuild_artifact(
        cas=cas,
        master_snapshot_id=str(payload["master_snapshot_id"]),
        targets=targets,
        stock_master=payload["stock_master"],
        query_pages=payload["query_pages"],
        documents=payload["documents"],
    )
    current = _manifest_payload(artifact)
    for field_name in (
        "normalized_announcements",
        "classification_candidates",
        "logical_content_sha256",
        "parser_dependencies",
        "dataset_gates",
        "source_contract",
        "statistics",
    ):
        if payload[field_name] != current[field_name]:
            raise CninfoDelistedDisclosureBlockedError(
                f"manifest {field_name} does not replay from raw bytes"
            )
    return artifact


def _rebuild_artifact(
    *,
    cas: CninfoDisclosureCAS,
    master_snapshot_id: str,
    targets: Sequence[FrozenDisclosureTarget],
    stock_master: Mapping[str, Any],
    query_pages: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    parse_evidence_by_announcement_id: Mapping[
        str, Mapping[str, Any]
    ] | None = None,
) -> CninfoDelistedDisclosureArtifact:
    snapshot_id = str(master_snapshot_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise CninfoDelistedDisclosureBlockedError("master_snapshot_id is invalid")
    frozen_targets = _normalize_targets(targets)
    target_by_code = {item.code: item for item in frozen_targets}

    master_evidence = _raw_from_mapping(stock_master)
    if (
        master_evidence.source_id != "CNINFO_STOCK_MASTER"
        or master_evidence.role != "STOCK_MASTER"
        or master_evidence.source_url != CNINFO_STOCK_MASTER_URL
        or master_evidence.method != "GET"
        or master_evidence.content_type != "application/json"
    ):
        raise CninfoDelistedDisclosureBlockedError(
            "stock-master raw identity changed"
        )
    master_raw, _ = cas.read_blob(
        master_evidence.content_hash, expected_path=master_evidence.object_path
    )
    if len(master_raw) != master_evidence.byte_count:
        raise CninfoDelistedDisclosureBlockedError(
            "stock-master byte_count mismatch"
        )
    master_rows = parse_cninfo_stock_master(master_raw)

    if not isinstance(query_pages, Sequence) or isinstance(query_pages, (str, bytes)):
        raise CninfoDelistedDisclosureBlockedError("query_pages is invalid")
    page_fields = {
        "exchange",
        "code",
        "org_id",
        "query_start",
        "query_end",
        "page_num",
        "page_size",
        "request",
        "raw",
    }
    pages_by_code: dict[str, list[tuple[int, tuple[dict[str, Any], ...]]]] = {
        item.code: [] for item in frozen_targets
    }
    page_values: list[dict[str, Any]] = []
    for item in query_pages:
        if not isinstance(item, Mapping) or set(item) != page_fields:
            raise CninfoDelistedDisclosureBlockedError("query-page schema drift")
        code = str(item["code"])
        target = target_by_code.get(code)
        if target is None or item["exchange"] != target.exchange:
            raise CninfoDelistedDisclosureBlockedError(
                "query page is outside frozen target scope"
            )
        page_num = _strict_int(item["page_num"], "page_num", nonnegative=False)
        if page_num <= 0 or item["page_size"] != PAGE_SIZE:
            raise CninfoDelistedDisclosureBlockedError("query page number/size invalid")
        org_id = str(item["org_id"])
        master_row = master_rows.get(code[:6])
        if master_row is None or master_row["orgId"] != org_id:
            raise CninfoDelistedDisclosureBlockedError(
                "query page orgId is not backed by stock master"
            )
        expected_request = _announcement_request(target, org_id, page_num)
        if item["request"] != expected_request:
            raise CninfoDelistedDisclosureBlockedError(
                "query-page POST parameters changed"
            )
        if (
            item["query_start"] != target.query_start
            or item["query_end"] != target.query_end
        ):
            raise CninfoDelistedDisclosureBlockedError(
                "query-page interval changed"
            )
        raw_evidence = _raw_from_mapping(item["raw"])
        if (
            raw_evidence.source_id != f"CNINFO_ANNOUNCEMENTS_{code}_{page_num}"
            or raw_evidence.role != "ANNOUNCEMENT_PAGE"
            or raw_evidence.source_url != CNINFO_ANNOUNCEMENT_URL
            or raw_evidence.method != "POST"
            or raw_evidence.content_type != "application/json"
        ):
            raise CninfoDelistedDisclosureBlockedError(
                "query-page raw identity changed"
            )
        raw, _ = cas.read_blob(
            raw_evidence.content_hash, expected_path=raw_evidence.object_path
        )
        if len(raw) != raw_evidence.byte_count:
            raise CninfoDelistedDisclosureBlockedError(
                "query-page byte_count mismatch"
            )
        _summary, rows = _parse_announcement_page(raw, target=target, org_id=org_id)
        pages_by_code[code].append((page_num, rows))
        page_values.append(dict(item))

    raw_rows: list[tuple[FrozenDisclosureTarget, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    for target in frozen_targets:
        pages = sorted(pages_by_code[target.code], key=lambda value: value[0])
        if not pages or [item[0] for item in pages] != list(range(1, len(pages) + 1)):
            raise CninfoDelistedDisclosureBlockedError(
                f"query pages are incomplete for {target.code}"
            )
        total: int | None = None
        for index, (page_num, rows) in enumerate(pages):
            page_item = next(
                item
                for item in page_values
                if item["code"] == target.code and item["page_num"] == page_num
            )
            raw_evidence = _raw_from_mapping(page_item["raw"])
            raw, _ = cas.read_blob(raw_evidence.content_hash)
            summary, rows = _parse_announcement_page(
                raw,
                target=target,
                org_id=str(page_item["org_id"]),
            )
            if total is None:
                total = int(summary["total"])
                page_count = max(1, math.ceil(total / PAGE_SIZE))
                if page_count != len(pages):
                    raise CninfoDelistedDisclosureBlockedError(
                        "query-page count disagrees with total"
                    )
            assert total is not None
            if (
                summary["total"] != total
                or summary["reported_totalpages"] != len(pages) - 1
                or summary["has_more"] is not (index < len(pages) - 1)
            ):
                raise CninfoDelistedDisclosureBlockedError(
                    "query-page pagination semantics do not replay"
                )
            expected_rows = (
                PAGE_SIZE if index < len(pages) - 1 else total - PAGE_SIZE * index
            )
            if len(rows) != expected_rows:
                raise CninfoDelistedDisclosureBlockedError(
                    "query-page row coverage does not replay"
                )
            for row in rows:
                announcement_id = str(row["announcementId"])
                if announcement_id in seen_ids:
                    raise CninfoDelistedDisclosureBlockedError(
                        f"duplicate announcementId: {announcement_id}"
                    )
                seen_ids.add(announcement_id)
                raw_rows.append((target, row))
    if len(raw_rows) > MAX_DOCUMENTS:
        raise CninfoDelistedDisclosureBlockedError(
            "replayed announcement count exceeds safety limit"
        )

    if not isinstance(documents, Sequence) or isinstance(documents, (str, bytes)):
        raise CninfoDelistedDisclosureBlockedError("documents is invalid")
    document_fields = {"exchange", "code", "announcement_id", "raw"}
    documents_by_id: dict[str, tuple[Mapping[str, Any], RawBlobEvidence, bytes]] = {}
    document_values: list[dict[str, Any]] = []
    total_pdf_bytes = 0
    for item in documents:
        if not isinstance(item, Mapping) or set(item) != document_fields:
            raise CninfoDelistedDisclosureBlockedError("document schema drift")
        announcement_id = str(item["announcement_id"])
        if announcement_id in documents_by_id:
            raise CninfoDelistedDisclosureBlockedError(
                f"duplicate document announcementId: {announcement_id}"
            )
        raw_evidence = _raw_from_mapping(item["raw"])
        expected_url = _normalize_pdf_url(raw_evidence.source_url, announcement_id)
        if (
            raw_evidence.source_id != f"CNINFO_PDF_{announcement_id}"
            or raw_evidence.role != "SOURCE_DOCUMENT"
            or raw_evidence.source_url != expected_url
            or raw_evidence.method != "GET"
            or raw_evidence.content_type != "application/pdf"
        ):
            raise CninfoDelistedDisclosureBlockedError(
                "document raw identity changed"
            )
        raw, _ = cas.read_blob(
            raw_evidence.content_hash, expected_path=raw_evidence.object_path
        )
        if (
            len(raw) != raw_evidence.byte_count
            or len(raw) > MAX_PDF_BYTES
            or not raw.startswith(b"%PDF-")
        ):
            raise CninfoDelistedDisclosureBlockedError(
                "document bytes do not satisfy PDF contract"
            )
        total_pdf_bytes += len(raw)
        if total_pdf_bytes > MAX_TOTAL_PDF_BYTES:
            raise CninfoDelistedDisclosureBlockedError(
                "replayed aggregate PDF bytes exceed safety limit"
            )
        documents_by_id[announcement_id] = (item, raw_evidence, raw)
        document_values.append(dict(item))

    if set(documents_by_id) != seen_ids:
        raise CninfoDelistedDisclosureBlockedError(
            "every announcement must have exactly one immutable PDF"
        )

    normalized: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for target, row in raw_rows:
        announcement_id = str(row["announcementId"])
        document_item, evidence, pdf_raw = documents_by_id[announcement_id]
        if (
            document_item["exchange"] != target.exchange
            or document_item["code"] != target.code
        ):
            raise CninfoDelistedDisclosureBlockedError(
                "document scope does not match announcement"
            )
        normalized_row = _normalized_announcement_base(
            target=target,
            row=row,
            evidence=evidence,
        )
        supplied_parse = (
            None
            if parse_evidence_by_announcement_id is None
            else parse_evidence_by_announcement_id.get(announcement_id)
        )
        if supplied_parse is None:
            parse_evidence = _build_pdf_parse_evidence(
                announcement_row=row,
                normalized_announcement=normalized_row,
                raw_content_sha256=evidence.content_hash,
                pdf_raw=pdf_raw,
            )
        else:
            parse_evidence = _validate_pdf_parse_evidence(
                supplied_parse,
                announcement_row=row,
                normalized_announcement=normalized_row,
                raw_content_sha256=evidence.content_hash,
            )
        normalized_row.update(
            {
                "pdf_text_status": parse_evidence.pdf_text_status,
                "pdf_text_sha256": parse_evidence.normalized_text_sha256,
                "pdf_page_count": parse_evidence.pdf_page_count,
            }
        )
        normalized.append(normalized_row)
        classification = parse_evidence.classification_candidate
        if classification is not None:
            candidates.append(
                {
                    **classification,
                    "exchange": target.exchange,
                    "code": target.code,
                    "announcement_id": announcement_id,
                    "published_at": normalized_row["published_at"],
                    "source_document_hash": evidence.content_hash,
                    "pdf_text_sha256": parse_evidence.normalized_text_sha256,
                    "structured_values_status": STRUCTURED_VALUES_UNRESOLVED,
                    "quality_row_emitted": False,
                }
            )
    if (
        parse_evidence_by_announcement_id is not None
        and set(parse_evidence_by_announcement_id) != seen_ids
    ):
        raise CninfoDelistedDisclosureBlockedError(
            "PDF parse-evidence coverage does not match announcements"
        )
    normalized.sort(
        key=lambda item: (
            str(item["published_at"]),
            str(item["code"]),
            str(item["announcement_id"]),
        )
    )
    candidates.sort(
        key=lambda item: (
            str(item["dataset"]),
            str(item["published_at"]),
            str(item["code"]),
            str(item["announcement_id"]),
        )
    )
    page_values.sort(key=lambda item: (str(item["code"]), int(item["page_num"])))
    document_values.sort(key=lambda item: str(item["announcement_id"]))
    dependencies = {"pypdf": _pypdf_version()}
    artifact = CninfoDelistedDisclosureArtifact(
        master_snapshot_id=snapshot_id,
        targets=frozen_targets,
        stock_master=master_evidence,
        query_pages=tuple(page_values),
        documents=tuple(document_values),
        normalized_announcements=tuple(normalized),
        classification_candidates=tuple(candidates),
        logical_content_sha256="",
        parser_dependencies=dependencies,
        _seal=_BUILDER_SEAL,
    )
    logical = _logical_hash(
        snapshot_id,
        frozen_targets,
        master_evidence,
        page_values,
        document_values,
        normalized,
        candidates,
    )
    return CninfoDelistedDisclosureArtifact(
        master_snapshot_id=artifact.master_snapshot_id,
        targets=artifact.targets,
        stock_master=artifact.stock_master,
        query_pages=artifact.query_pages,
        documents=artifact.documents,
        normalized_announcements=artifact.normalized_announcements,
        classification_candidates=artifact.classification_candidates,
        logical_content_sha256=logical,
        parser_dependencies=artifact.parser_dependencies,
        _seal=_BUILDER_SEAL,
    )


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse(value: os.stat_result) -> bool:
    attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400))
    return stat.S_ISLNK(value.st_mode) or bool(
        int(getattr(value, "st_file_attributes", 0)) & attribute
    )


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _prepare_root(root: Path) -> None:
    current = root
    missing: list[Path] = []
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    while True:
        value = os.lstat(current)
        if _is_reparse(value):
            raise CninfoDelistedDisclosureBlockedError(
                "CAS root contains a symlink, junction, or reparse point"
            )
        parent = current.parent
        if parent == current:
            break
        current = parent
    for path in reversed(missing):
        path.mkdir(exist_ok=True)
        value = os.lstat(path)
        if _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise CninfoDelistedDisclosureBlockedError(
                "CAS root was replaced by a reparse/non-directory"
            )
    _path_snapshot(root, root, leaf_is_file=False)


def _path_snapshot(
    root: Path, target: Path, *, leaf_is_file: bool
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    root = _lexical_absolute(root)
    target = _lexical_absolute(target)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise CninfoDelistedDisclosureBlockedError("CAS path escapes root") from exc
    components = [root]
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise CninfoDelistedDisclosureBlockedError("CAS path is invalid")
        current = current / part
        components.append(current)
    snapshot: list[tuple[str, tuple[int, ...]]] = []
    for index, component in enumerate(components):
        try:
            value = os.lstat(component)
        except OSError as exc:
            raise CninfoDelistedDisclosureBlockedError(
                f"CAS component is missing: {component}"
            ) from exc
        if _is_reparse(value):
            raise CninfoDelistedDisclosureBlockedError(
                f"CAS path contains a symlink, junction, or reparse point: {component}"
            )
        expect_file = leaf_is_file and index == len(components) - 1
        if expect_file:
            if not stat.S_ISREG(value.st_mode):
                raise CninfoDelistedDisclosureBlockedError(
                    "CAS object is not a regular file"
                )
        elif not stat.S_ISDIR(value.st_mode):
            raise CninfoDelistedDisclosureBlockedError(
                "CAS path component is not a directory"
            )
        snapshot.append((os.fspath(component), _fingerprint(value)))
    return tuple(snapshot)


def _stable_read(root: Path, path: Path) -> bytes:
    before = _path_snapshot(root, path, leaf_is_file=True)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CninfoDelistedDisclosureBlockedError(
            "CAS object cannot be opened safely"
        ) from exc
    try:
        handle_before = os.fstat(descriptor)
        if _is_reparse(handle_before) or not stat.S_ISREG(handle_before.st_mode):
            raise CninfoDelistedDisclosureBlockedError(
                "CAS handle is not a regular non-reparse file"
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
    after = _path_snapshot(root, path, leaf_is_file=True)
    if (
        before != after
        or _fingerprint(handle_before) != _fingerprint(handle_after)
        or _fingerprint(handle_before) != before[-1][1]
    ):
        raise CninfoDelistedDisclosureBlockedError(
            "CAS object or parent changed during stable read"
        )
    return b"".join(chunks)


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _path_snapshot(root, path.parent, leaf_is_file=False)
    if path.exists():
        if _stable_read(root, path) != content:
            raise CninfoDelistedDisclosureBlockedError("immutable CAS collision")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0)),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _path_snapshot(root, temporary, leaf_is_file=True)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _stable_read(root, path) != content:
                raise CninfoDelistedDisclosureBlockedError("immutable CAS collision")
        except OSError as exc:
            raise CninfoDelistedDisclosureBlockedError(
                "CAS cannot atomically publish an immutable object"
            ) from exc
        if _stable_read(root, path) != content:
            raise CninfoDelistedDisclosureBlockedError(
                "published CAS object failed verification"
            )
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "CNINFO_ANNOUNCEMENT_URL",
    "CNINFO_STOCK_MASTER_URL",
    "EFFECTIVE_AT_UNRESOLVED",
    "MASTER_BINDING_UNVERIFIED",
    "PROTOCOL_VERSION",
    "RAW_EVIDENCE_ONLY",
    "STRUCTURED_VALUES_UNRESOLVED",
    "CninfoDelistedDisclosureArtifact",
    "CninfoDelistedDisclosureBlockedError",
    "CninfoDelistedDisclosureClient",
    "CninfoDelistedDisclosureManifestReference",
    "CninfoDelistedDisclosureManifestStore",
    "CninfoDisclosureCAS",
    "FrozenDisclosureTarget",
    "parse_cninfo_stock_master",
]
