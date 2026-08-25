from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests

from .bse_current_delisting_events import (
    EVIDENCE_COMPLETE as BSE_CURRENT_DELISTING_EVIDENCE_COMPLETE,
    PROTOCOL_VERSION as BSE_CURRENT_DELISTING_PROTOCOL_VERSION,
    SOURCE_CONTRACT_ADMITTED as BSE_CURRENT_DELISTING_SOURCE_ADMITTED,
    BSECurrentDelistingArtifact,
    BSECurrentDelistingBlockedError,
    BSECurrentDelistingCAS,
    BSECurrentDelistingManifestStore,
    ManifestReference as BSECurrentDelistingManifestReference,
    NOTICE_SPECS as BSE_CURRENT_DELISTING_NOTICE_SPECS,
    validate_current_delisting_freshness,
)
from .bse_termination_events import (
    PROTOCOL_VERSION as BSE_TERMINATION_EVENT_PROTOCOL_VERSION,
    SOURCE_COMPLETE as BSE_TERMINATION_EVENT_SOURCE_COMPLETE,
    BSEEventLedgerStore,
    BSETerminationEventBlockedError,
    ManifestEvidence as BSETerminationManifestEvidence,
)
from .sse_risk_warning_source import (
    PROTOCOL_VERSION as SSE_RISK_WARNING_PROTOCOL_VERSION,
    SOURCE_SPECS as SSE_RISK_WARNING_SOURCE_SPECS,
    SOURCE_CONTRACT_ADMITTED as SSE_RISK_WARNING_SOURCE_ADMITTED,
    SSERiskWarningListArtifact,
    SSERiskWarningManifestReference,
    SSERiskWarningManifestStore,
    SSERiskWarningRawCAS,
    SSERiskWarningSourceClient,
    SSERiskWarningSourceBlockedError,
)
from .sse_risk_warning_active_intervals import (
    PROTOCOL_VERSION as SSE_RISK_WARNING_ACTIVE_INTERVALS_PROTOCOL_VERSION,
    SOURCE_NAME as SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME,
    SOURCE_STATUS as SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_ADMITTED,
    TRANSITION_BINDING_CONVERGED as SSE_TRANSITION_BINDING_CONVERGED,
    TRANSITION_BINDING_LAG as SSE_TRANSITION_BINDING_LAG,
    SSERiskWarningActiveIntervalsArtifact,
    SSERiskWarningActiveIntervalsBlockedError,
    SSERiskWarningActiveIntervalsCAS,
    SSERiskWarningActiveIntervalsClient,
    SSERiskWarningActiveIntervalsManifestReference,
    SSERiskWarningActiveIntervalsManifestStore,
)
from .sse_risk_warning_transition_source import (
    FROZEN_TRANSITION as SSE_RISK_WARNING_FROZEN_TRANSITION,
    SSERiskWarningTransitionCAS,
    SSERiskWarningTransitionBlockedError,
    SSERiskWarningTransitionManifestStore,
)
from .pending_listing_source import (
    PENDING_LISTING_EVIDENCE_COMPLETE,
    PROTOCOL_VERSION as PENDING_LISTING_PROTOCOL_VERSION,
    SOURCE_CONTRACT_ADMITTED as PENDING_LISTING_SOURCE_ADMITTED,
    SOURCE_ORDER as PENDING_LISTING_SOURCE_ORDER,
    PendingListingArtifact,
    PendingListingManifestReference,
    PendingListingManifestStore,
    PendingListingRawCAS,
    PendingListingSourceBlockedError,
    validate_pending_listing_freshness,
)
from .security_master_observation import (
    OBSERVATION_READY as CURRENT_OBSERVATION_READY,
    PROTOCOL_VERSION as CURRENT_OBSERVATION_PROTOCOL_VERSION,
    ObservationManifestReference,
    SecurityMasterObservationBatch,
    SecurityMasterObservationBlockedError,
    SecurityMasterObservationPolicy,
    SecurityMasterObservationStore,
)
from .szse_code_change_events import (
    CANONICAL_ENTITY_ID as SZSE_CODE_CHANGE_ENTITY_ID,
    EFFECTIVE_DATE as SZSE_CODE_CHANGE_EFFECTIVE_DATE,
    NEW_CODE as SZSE_CODE_CHANGE_NEW_CODE,
    OLD_CODE as SZSE_CODE_CHANGE_OLD_CODE,
    PRIMARY_DISCLOSURE_URL as SZSE_CODE_CHANGE_SOURCE_URL,
    PROTOCOL_VERSION as SZSE_CODE_CHANGE_PROTOCOL_VERSION,
    SOURCE_CONTRACT_ADMITTED as SZSE_CODE_CHANGE_ADMITTED,
    SZSECodeChangeArtifact,
    SZSECodeChangeBlockedError,
    SZSECodeChangeClient,
    SZSEDisclosureCAS,
    parse_szse_code_change_pdf,
    validate_alias_intervals as validate_szse_code_change_intervals,
)


PROTOCOL_VERSION = "cn-historical-security-master-v1"
QUALITY_POLICY_VERSION = "cn-historical-security-master-quality-v15"
HISTORICAL_START = "2018-01-01"
HISTORICAL_END = "2023-12-31"
EXPECTED_SSE_SZSE_OVERLAP = 239
BSE_OPEN_DATE = "2021-11-15"
BSE_PILOT_SWITCH_DATE = "2025-05-06"
BSE_GENERAL_SWITCH_DATE = "2025-10-09"
BSE_TERMINATION_EVENT_SOURCE_NAME = "bse_termination_and_transfer_events"
BSE_TERMINATION_EVENT_STORE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "security_master"
    / "bse_termination_events_v2"
)
BSE_TERMINATION_EVENT_MANIFEST_SHA256 = (
    "dc8f33e386158b88dde6d47f937d2804b3eb544e1b54cc2c0f26cd79da8dab8f"
)
BSE_TERMINATION_EVENT_LOGICAL_SHA256 = (
    "e2411d61c5dc91ff089aaf331b2be388d5d8bae7a21340a418e4e6d1065b1745"
)
BSE_PILOT_OLD_CODES = frozenset(
    {"833819", "830799", "831445", "430489", "839167", "834682"}
)
PENDING_LISTING_STORE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "security_master"
    / "pending_listing"
    / "cas"
)
PENDING_LISTING_MANIFEST_SHA256 = (
    "8878c2be2e26ca534364311a3c86717d15c176bfcf8a3deeabf9771e3b2e9765"
)
PENDING_LISTING_LOGICAL_SHA256 = (
    "81c2f4252c0d49591309b0a6b03cb8036a92de72b2a075ef284222b18212ed90"
)
PENDING_LISTING_RECONCILIATION_CODES = frozenset(
    {
        "688826.SH",
        "688835.SH",
        "688836.SH",
        "301655.SZ",
        "301688.SZ",
        "301697.SZ",
    }
)
BSE_CURRENT_DELISTING_SOURCE_NAME = "bse_current_delisting_events"
BSE_CURRENT_DELISTING_STORE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "security_master"
    / "bse_current_delisting"
    / "cas"
)
BSE_CURRENT_DELISTING_MANIFEST_SHA256 = (
    "9a405d4e2499615abaca659fe08ede6f101cef2c1bbdb3a73623488664cbd8dd"
)
BSE_CURRENT_DELISTING_LOGICAL_SHA256 = (
    "2f713be4941af59b2038728b4c0d6df9dd199840514f665fa10ca1bfe6edf728"
)
BSE_CURRENT_DELISTING_CODES = frozenset(
    {"920305.BJ", "920680.BJ"}
)
CURRENT_RECONCILIATION_CLOCK_SKEW = timedelta(seconds=5)
CURRENT_OBSERVATION_STORE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "security_master"
    / "current_observations"
)
CURRENT_OBSERVATION_MINIMUM_TDX_CODE_COUNT = 5_000
CURRENT_OBSERVATION_HISTORICAL_PUBLISH_MAX_AGE = timedelta(minutes=5)
HISTORICAL_SECURITY_MASTER_STORE_ROOT = (
    Path(__file__).resolve().parent.parent / "data" / "security_master"
)
SSE_RISK_WARNING_STORE_ROOT = (
    HISTORICAL_SECURITY_MASTER_STORE_ROOT / "sse_risk_warning" / "cas"
)
SSE_RISK_WARNING_ACTIVE_INTERVALS_STORE_ROOT = (
    HISTORICAL_SECURITY_MASTER_STORE_ROOT
    / "sse_risk_warning_active_intervals"
    / "cas"
)
SSE_RISK_WARNING_TRANSITION_STORE_ROOT = (
    HISTORICAL_SECURITY_MASTER_STORE_ROOT
    / "sse_risk_warning_transition"
    / "cas"
)
SSE_RISK_WARNING_TRANSITION_MANIFEST_SHA256 = (
    "92f11262ec874473b3da630d75fcc841ff1598ce1f1a076292b1a49430ea4fa2"
)
SZSE_CODE_CHANGE_STORE_ROOT = (
    HISTORICAL_SECURITY_MASTER_STORE_ROOT / "szse_code_change_events" / "raw"
)
SZSE_CODE_CHANGE_RAW_PDF_SHA256 = (
    "9234be47b07dcfa6e4ef070f099d6b19ce927864897bd6e827eca31830dd6779"
)

SSE_DELIST_PAGE_URL = "https://www.sse.com.cn/assortment/stock/list/delisting/"
SSE_DELIST_API_URL = (
    "https://query.sse.com.cn/commonQuery.do?"
    "sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L&isPagination=true&"
    "STOCK_CODE=&CSRC_CODE=&REG_PROVINCE=&STOCK_TYPE=1%2C8&"
    "COMPANY_STATUS=3&type=inParams&pageHelp.cacheSize=1&"
    "pageHelp.beginPage=1&pageHelp.pageSize=500&pageHelp.pageNo=1"
)
SSE_ACTIVE_PAGE_URL = "https://www.sse.com.cn/assortment/stock/list/share/"
SSE_ACTIVE_API_URL = (
    "https://query.sse.com.cn/commonQuery.do?"
    "sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L&isPagination=true&"
    "STOCK_CODE=&CSRC_CODE=&REG_PROVINCE=&STOCK_TYPE=1%2C8&"
    "COMPANY_STATUS=2&type=inParams&pageHelp.cacheSize=1&"
    "pageHelp.beginPage=1&pageHelp.pageSize=5000&pageHelp.pageNo=1"
)
SZSE_DELIST_XLSX_URL = (
    "https://www.szse.cn/api/report/ShowReport?"
    "SHOWTYPE=xlsx&CATALOGID=1793_ssgs&TABKEY=tab2"
)
SZSE_ACTIVE_PAGE_URL = "https://www.szse.cn/market/product/stock/list/index.html"
SZSE_ACTIVE_XLSX_URL = (
    "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110"
)
SZSE_ACTIVE_HEADER = (
    "板块",
    "公司全称",
    "英文名称",
    "注册地址",
    "A股代码",
    "A股简称",
    "A股上市日期",
    "A股总股本",
    "A股流通股本",
    "B股代码",
    "B股 简 称",
    "B股上市日期",
    "B股总股本",
    "B股流通股本",
    "地      区",
    "省    份",
    "城     市",
    "所属行业",
    "公司网址",
    "目前尚未盈利",
    "具有表决权差异安排",
    "具有协议控制架构",
)
SSE_ACTIVE_PAGE_BUNDLE_PROTOCOL = "sse-active-page-bundle-v1"
SZSE_UNRESOLVED_ALIAS_PREDECESSORS = {"302132": "300114"}
SZSE_CODE_CHANGE_SOURCE_NAME = "szse_code_change_300114_302132"
BSE_CODE_MAPPING_URL = "https://www.bse.cn/service/code_mapping.html"


class HistoricalSecurityMasterBlockedError(RuntimeError):
    """The official-source security master cannot be trusted or is incomplete."""


@dataclass(frozen=True)
class SecurityMasterRecord:
    canonical_entity_id: str
    exchange: str
    code_alias: str
    board: str
    listed_at: str
    delisted_at: str | None
    valid_from: str
    valid_to: str | None
    event_type: str
    source_url: str
    source_hash: str
    retrieved_at: str
    name: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["attributes"] = dict(sorted(dict(self.attributes).items()))
        return value


@dataclass(frozen=True)
class ParsedOfficialSource:
    name: str
    source_url: str
    source_hash: str
    retrieved_at: str
    raw_bytes: bytes
    records: tuple[SecurityMasterRecord, ...]
    statistics: Mapping[str, Any]


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


def _normalized_retrieved_at(value: str | None) -> str:
    text = value or datetime.now().astimezone().isoformat()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"retrieved_at is not ISO-8601: {text}"
        ) from exc
    if parsed.tzinfo is None:
        raise HistoricalSecurityMasterBlockedError("retrieved_at must include a timezone")
    return parsed.isoformat()


def _iso_date(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise HistoricalSecurityMasterBlockedError(f"invalid {field_name}: {text!r}")


def _canonical_entity_id(exchange: str, original_code: str) -> str:
    return f"CN:{exchange}:{original_code}"


def _board(exchange: str, code: str) -> str:
    if exchange == "SSE":
        return "STAR" if code.startswith("688") else "SSE_MAIN"
    if exchange == "SZSE":
        return "CHINEXT" if code.startswith(("300", "301", "302")) else "SZSE_MAIN"
    if exchange == "BSE":
        return "BSE"
    raise HistoricalSecurityMasterBlockedError(f"unsupported exchange: {exchange}")


def _validate_code(exchange: str, code: str) -> str:
    value = str(code or "").strip().upper()
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange)
    if suffix is None:
        raise HistoricalSecurityMasterBlockedError(f"unsupported exchange: {exchange}")
    if value.endswith(f".{suffix}"):
        value = value[:-3]
    if not re.fullmatch(r"\d{6}", value):
        raise HistoricalSecurityMasterBlockedError(f"invalid {exchange} code: {code!r}")
    return f"{value}.{suffix}"


def parse_sse_delist_json(
    raw_bytes: bytes,
    *,
    source_url: str = SSE_DELIST_API_URL,
    retrieved_at: str | None = None,
    expected_hash: str | None = None,
) -> ParsedOfficialSource:
    retrieved = _normalized_retrieved_at(retrieved_at)
    _verify_expected_hash(raw_bytes, expected_hash, "SSE")
    if not raw_bytes or len(raw_bytes) > 20 * 1024 * 1024:
        raise HistoricalSecurityMasterBlockedError("SSE response is empty or oversized")
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalSecurityMasterBlockedError("SSE response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HistoricalSecurityMasterBlockedError("SSE response root must be an object")
    if payload.get("actionErrors") or payload.get("fieldErrors"):
        raise HistoricalSecurityMasterBlockedError("SSE response contains API errors")
    page = payload.get("pageHelp")
    rows = payload.get("result")
    if not isinstance(page, dict) or not isinstance(rows, list):
        raise HistoricalSecurityMasterBlockedError("SSE pagination/result fields are missing")
    required = {"A_STOCK_CODE", "LIST_DATE", "DELIST_DATE", "COMPANY_ABBR"}
    total = _strict_int(page.get("total"), "SSE page total")
    page_no = _strict_int(page.get("pageNo"), "SSE page number")
    page_count = _strict_int(page.get("pageCount"), "SSE page count")
    if page_no != 1 or page_count != 1 or total != len(rows):
        raise HistoricalSecurityMasterBlockedError(
            f"SSE pagination incomplete: page={page_no}/{page_count}, rows={len(rows)}/{total}"
        )
    source_hash = _sha256(raw_bytes)
    records: list[SecurityMasterRecord] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not required.issubset(row):
            raise HistoricalSecurityMasterBlockedError("SSE schema drift detected")
        code = str(row["A_STOCK_CODE"] or "").strip()
        alias = _validate_code("SSE", code)
        if alias in seen:
            raise HistoricalSecurityMasterBlockedError(f"duplicate SSE code: {alias}")
        seen.add(alias)
        listed_at = _iso_date(row["LIST_DATE"], field_name="SSE LIST_DATE")
        delisted_at = _iso_date(row["DELIST_DATE"], field_name="SSE DELIST_DATE")
        records.append(
            SecurityMasterRecord(
                canonical_entity_id=_canonical_entity_id("SSE", code),
                exchange="SSE",
                code_alias=alias,
                board=_board("SSE", code),
                listed_at=listed_at,
                delisted_at=delisted_at,
                valid_from=listed_at,
                valid_to=delisted_at,
                event_type="TERMINATED_LISTING",
                source_url=source_url,
                source_hash=source_hash,
                retrieved_at=retrieved,
                name=str(row.get("COMPANY_ABBR") or "").strip(),
                attributes={"company_code": str(row.get("COMPANY_CODE") or code)},
            )
        )
    validate_security_master_records(records)
    return ParsedOfficialSource(
        name="sse_terminated_a_shares",
        source_url=source_url,
        source_hash=source_hash,
        retrieved_at=retrieved,
        raw_bytes=raw_bytes,
        records=tuple(records),
        statistics={"rows": len(records), "api_total": total},
    )


def parse_sse_active_json(
    raw_bytes: bytes,
    *,
    source_url: str = SSE_ACTIVE_API_URL,
    retrieved_at: str | None = None,
    expected_hash: str | None = None,
) -> ParsedOfficialSource:
    """Parse a complete SSE active A-share response or replayable page bundle."""

    retrieved = _normalized_retrieved_at(retrieved_at)
    _verify_expected_hash(raw_bytes, expected_hash, "SSE active")
    if not raw_bytes or len(raw_bytes) > 100 * 1024 * 1024:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active response is empty or oversized"
        )
    page_evidence = _decode_sse_active_page_evidence(
        raw_bytes,
        source_url=source_url,
    )
    decoded_pages = [
        _decode_sse_active_page(content, label=f"SSE active page {page_no}")
        for page_no, _request_url, content, _page_hash in page_evidence
    ]
    first_page_no, first_page_count, total, first_rows = decoded_pages[0]
    if first_page_no != 1:
        raise HistoricalSecurityMasterBlockedError(
            f"SSE active pagination does not start at page 1: {first_page_no}"
        )
    if first_page_count != len(decoded_pages):
        raise HistoricalSecurityMasterBlockedError(
            "SSE active pagination incomplete: "
            f"pages={len(decoded_pages)}/{first_page_count}"
        )
    first_page_size = len(first_rows)
    if first_page_size <= 0:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active first page contains no rows"
        )
    expected_page_count = (total + first_page_size - 1) // first_page_size
    if first_page_count != expected_page_count:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active page count is inconsistent with total and page size"
        )
    all_rows: list[Mapping[str, Any]] = []
    for index, decoded in enumerate(decoded_pages, start=1):
        page_no, page_count, page_total, rows = decoded
        if page_no != index:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active page sequence is discontinuous at {page_no}, expected {index}"
            )
        if page_count != first_page_count or page_total != total:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active pagination metadata drifted on page {page_no}"
            )
        expected_rows = (
            first_page_size
            if page_no < first_page_count
            else total - first_page_size * (first_page_count - 1)
        )
        if expected_rows <= 0 or len(rows) != expected_rows:
            raise HistoricalSecurityMasterBlockedError(
                "SSE active page row count is invalid: "
                f"page={page_no}, rows={len(rows)}, expected={expected_rows}"
            )
        all_rows.extend(rows)
    if len(all_rows) != total:
        raise HistoricalSecurityMasterBlockedError(
            f"SSE active row total mismatch: rows={len(all_rows)}, total={total}"
        )
    required = {
        "A_STOCK_CODE",
        "LIST_DATE",
        "DELIST_DATE",
        "COMPANY_ABBR",
        "COMPANY_CODE",
    }
    source_hash = _sha256(raw_bytes)
    records: list[SecurityMasterRecord] = []
    seen: set[str] = set()
    for row in all_rows:
        if not isinstance(row, dict) or not required.issubset(row):
            raise HistoricalSecurityMasterBlockedError(
                "SSE active schema drift detected"
        )
        code = str(row["A_STOCK_CODE"] or "").strip()
        if not code.startswith(("600", "601", "603", "605", "688", "689")):
            raise HistoricalSecurityMasterBlockedError(
                f"unknown SSE security class in active response: {code!r}"
            )
        alias = _validate_code("SSE", code)
        if alias in seen:
            raise HistoricalSecurityMasterBlockedError(
                f"duplicate SSE active code: {alias}"
            )
        seen.add(alias)
        if str(row["DELIST_DATE"] or "").strip() not in {"", "-"}:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active row unexpectedly has DELIST_DATE: {alias}"
            )
        listed_at = _iso_date(row["LIST_DATE"], field_name="SSE active LIST_DATE")
        company_code = str(row["COMPANY_CODE"] or "").strip()
        company_name = str(row["COMPANY_ABBR"] or "").strip()
        if not re.fullmatch(r"\d{6}", company_code) or not company_name:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active identity fields are incomplete: {alias}"
            )
        records.append(
            SecurityMasterRecord(
                canonical_entity_id=_canonical_entity_id("SSE", code),
                exchange="SSE",
                code_alias=alias,
                board=_board("SSE", code),
                listed_at=listed_at,
                delisted_at=None,
                valid_from=listed_at,
                valid_to=None,
                event_type="ACTIVE_LISTING",
                source_url=source_url,
                source_hash=source_hash,
                retrieved_at=retrieved,
                name=company_name,
                attributes={"company_code": company_code},
            )
        )
    validate_security_master_records(records)
    return ParsedOfficialSource(
        name="sse_active_a_shares",
        source_url=source_url,
        source_hash=source_hash,
        retrieved_at=retrieved,
        raw_bytes=raw_bytes,
        records=tuple(records),
        statistics={
            "rows": len(records),
            "api_total": total,
            "page_count": first_page_count,
            "page_hashes": [item[3] for item in page_evidence],
            "page_row_counts": [len(item[3]) for item in decoded_pages],
            "raw_evidence": (
                SSE_ACTIVE_PAGE_BUNDLE_PROTOCOL
                if len(page_evidence) > 1
                else "single_page_exact_bytes"
            ),
        },
    )


def _sse_active_page_url(source_url: str, page_no: int) -> str:
    page = _strict_int(page_no, "SSE active requested page number")
    if page < 1:
        raise HistoricalSecurityMasterBlockedError(
            f"invalid SSE active requested page number: {page_no!r}"
        )
    parsed = urlsplit(source_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "pageHelp.pageNo" not in query or "pageHelp.beginPage" not in query:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active URL pagination parameters are missing"
        )
    query["pageHelp.pageNo"] = str(page)
    query["pageHelp.beginPage"] = str(page)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def build_sse_active_page_bundle(
    pages: Sequence[tuple[str, bytes]],
    *,
    source_url: str = SSE_ACTIVE_API_URL,
) -> bytes:
    """Wrap exact SSE page bytes in a canonical, content-addressable envelope."""

    if len(pages) < 2:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active page bundle requires at least two pages"
        )
    entries: list[dict[str, Any]] = []
    for page_no, (request_url, content) in enumerate(pages, start=1):
        if request_url != _sse_active_page_url(source_url, page_no):
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active bundle request URL mismatch on page {page_no}"
            )
        if not content or len(content) > 50 * 1024 * 1024:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active page {page_no} is empty or oversized"
            )
        entries.append(
            {
                "content_base64": base64.b64encode(content).decode("ascii"),
                "content_sha256": _sha256(content),
                "page_no": page_no,
                "request_url": request_url,
            }
        )
    return _canonical_json_bytes(
        {
            "pages": entries,
            "protocol": SSE_ACTIVE_PAGE_BUNDLE_PROTOCOL,
            "source_url": source_url,
        }
    )


def _decode_sse_active_page_evidence(
    raw_bytes: bytes,
    *,
    source_url: str,
) -> list[tuple[int, str, bytes, str]]:
    try:
        candidate = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active response is not valid JSON"
        ) from exc
    if not isinstance(candidate, dict) or candidate.get("protocol") != (
        SSE_ACTIVE_PAGE_BUNDLE_PROTOCOL
    ):
        return [(1, _sse_active_page_url(source_url, 1), raw_bytes, _sha256(raw_bytes))]
    if raw_bytes != _canonical_json_bytes(candidate):
        raise HistoricalSecurityMasterBlockedError(
            "SSE active page bundle is not canonical JSON"
        )
    if set(candidate) != {"pages", "protocol", "source_url"}:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active page bundle schema drift detected"
        )
    if candidate.get("source_url") != source_url:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active page bundle source URL mismatch"
        )
    entries = candidate.get("pages")
    if not isinstance(entries, list) or len(entries) < 2:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active page bundle is incomplete"
        )
    evidence: list[tuple[int, str, bytes, str]] = []
    for expected_page, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or set(entry) != {
            "content_base64",
            "content_sha256",
            "page_no",
            "request_url",
        }:
            raise HistoricalSecurityMasterBlockedError(
                "SSE active page bundle entry schema drift detected"
            )
        page_no = _strict_int(entry.get("page_no"), "SSE active bundle page number")
        if page_no != expected_page:
            raise HistoricalSecurityMasterBlockedError(
                "SSE active page bundle sequence is discontinuous"
            )
        request_url = str(entry.get("request_url") or "")
        if request_url != _sse_active_page_url(source_url, page_no):
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active bundle request URL mismatch on page {page_no}"
            )
        try:
            content = base64.b64decode(
                str(entry.get("content_base64") or ""), validate=True
            )
        except (ValueError, binascii.Error) as exc:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active page {page_no} base64 is invalid"
            ) from exc
        digest = str(entry.get("content_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or _sha256(content) != digest:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active page {page_no} content hash mismatch"
            )
        evidence.append((page_no, request_url, content, digest))
    return evidence


def _decode_sse_active_page(
    raw_bytes: bytes,
    *,
    label: str,
) -> tuple[int, int, int, list[Mapping[str, Any]]]:
    if not raw_bytes or len(raw_bytes) > 50 * 1024 * 1024:
        raise HistoricalSecurityMasterBlockedError(f"{label} is empty or oversized")
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalSecurityMasterBlockedError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("actionErrors") or payload.get(
        "fieldErrors"
    ):
        raise HistoricalSecurityMasterBlockedError(f"{label} contains API errors")
    page = payload.get("pageHelp")
    result = payload.get("result")
    page_data = page.get("data") if isinstance(page, dict) else None
    if not isinstance(page, dict):
        raise HistoricalSecurityMasterBlockedError(f"{label} pagination is missing")
    rows = result if isinstance(result, list) else page_data
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HistoricalSecurityMasterBlockedError(f"{label} result rows are invalid")
    total = _strict_int(page.get("total"), f"{label} total")
    page_no = _strict_int(page.get("pageNo"), f"{label} page number")
    page_count = _strict_int(page.get("pageCount"), f"{label} page count")
    if page_no < 1 or page_count < 1 or page_no > page_count or total < 1:
        raise HistoricalSecurityMasterBlockedError(f"{label} pagination is invalid")
    return page_no, page_count, total, rows


def _strict_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise HistoricalSecurityMasterBlockedError(f"invalid {label}: {value!r}") from exc
    if result < 0:
        raise HistoricalSecurityMasterBlockedError(f"invalid {label}: {value!r}")
    return result


def _verify_expected_hash(content: bytes, expected_hash: str | None, label: str) -> None:
    if expected_hash is None:
        return
    expected = str(expected_hash).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise HistoricalSecurityMasterBlockedError(f"invalid expected {label} SHA-256")
    actual = _sha256(content)
    if actual != expected:
        raise HistoricalSecurityMasterBlockedError(
            f"{label} source hash mismatch: expected {expected}, got {actual}"
        )


def _xlsx_rows(raw_bytes: bytes) -> list[list[str]]:
    if not raw_bytes or len(raw_bytes) > 20 * 1024 * 1024:
        raise HistoricalSecurityMasterBlockedError("SZSE XLSX is empty or oversized")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise HistoricalSecurityMasterBlockedError("SZSE response is not XLSX") from exc
    with archive:
        members = archive.infolist()
        if len(members) > 100 or sum(item.file_size for item in members) > 100 * 1024 * 1024:
            raise HistoricalSecurityMasterBlockedError("SZSE XLSX archive is unsafe")
        sheet_names = sorted(
            item.filename
            for item in members
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item.filename)
        )
        if len(sheet_names) != 1:
            raise HistoricalSecurityMasterBlockedError("SZSE XLSX must contain one worksheet")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            except ElementTree.ParseError as exc:
                raise HistoricalSecurityMasterBlockedError(
                    "SZSE shared strings XML is invalid"
                ) from exc
            shared = ["".join(item.itertext()) for item in list(shared_root)]
        try:
            root = ElementTree.fromstring(archive.read(sheet_names[0]))
        except ElementTree.ParseError as exc:
            raise HistoricalSecurityMasterBlockedError("SZSE worksheet XML is invalid") from exc

    rows: list[list[str]] = []
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for row in root.iter(f"{namespace}row"):
        cells: dict[int, str] = {}
        for cell in row.findall(f"{namespace}c"):
            reference = str(cell.attrib.get("r") or "")
            match = re.match(r"([A-Z]+)", reference)
            if not match:
                raise HistoricalSecurityMasterBlockedError("SZSE cell reference is invalid")
            column = _excel_column_index(match.group(1))
            cell_type = cell.attrib.get("t", "")
            if cell_type == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.iter(f"{namespace}t")
                )
            else:
                value_node = cell.find(f"{namespace}v")
                value = value_node.text if value_node is not None and value_node.text else ""
                if cell_type == "s":
                    index = _strict_int(value, "SZSE shared-string index")
                    if index >= len(shared):
                        raise HistoricalSecurityMasterBlockedError(
                            "SZSE shared-string index is out of range"
                        )
                    value = shared[index]
            cells[column] = value.strip()
        if cells:
            rows.append([cells.get(index, "") for index in range(max(cells) + 1)])
    return rows


def _excel_column_index(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - ord("A") + 1
    return result - 1


def _excel_date(value: str, *, field_name: str) -> str:
    text = value.strip()
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        serial = int(float(text))
        if serial <= 0:
            raise HistoricalSecurityMasterBlockedError(f"invalid {field_name}: {text!r}")
        return (date(1899, 12, 30) + timedelta(days=serial)).isoformat()
    return _iso_date(text, field_name=field_name)


def parse_szse_delist_xlsx(
    raw_bytes: bytes,
    *,
    source_url: str = SZSE_DELIST_XLSX_URL,
    retrieved_at: str | None = None,
    expected_hash: str | None = None,
) -> ParsedOfficialSource:
    retrieved = _normalized_retrieved_at(retrieved_at)
    _verify_expected_hash(raw_bytes, expected_hash, "SZSE")
    rows = _xlsx_rows(raw_bytes)
    expected_header = ["证券代码", "证券简称", "上市日期", "终止上市日期"]
    if not rows or rows[0][:4] != expected_header or len(rows[0]) != 4:
        raise HistoricalSecurityMasterBlockedError("SZSE XLSX schema drift detected")
    source_hash = _sha256(raw_bytes)
    records: list[SecurityMasterRecord] = []
    b_share_rows = 0
    seen: set[str] = set()
    for row in rows[1:]:
        if len(row) != 4 or not any(row):
            raise HistoricalSecurityMasterBlockedError("SZSE XLSX row width is invalid")
        raw_code = row[0].strip()
        if re.fullmatch(r"\d+(?:\.0+)?", raw_code):
            raw_code = str(int(float(raw_code))).zfill(6)
        if not re.fullmatch(r"\d{6}", raw_code):
            raise HistoricalSecurityMasterBlockedError(f"invalid SZSE code: {raw_code!r}")
        if raw_code.startswith("200"):
            b_share_rows += 1
            continue
        if not raw_code.startswith(("000", "001", "002", "003", "300", "301")):
            raise HistoricalSecurityMasterBlockedError(
                f"unknown SZSE security class in termination file: {raw_code}"
            )
        alias = _validate_code("SZSE", raw_code)
        if alias in seen:
            raise HistoricalSecurityMasterBlockedError(f"duplicate SZSE code: {alias}")
        seen.add(alias)
        listed_at = _excel_date(row[2], field_name="SZSE LIST_DATE")
        delisted_at = _excel_date(row[3], field_name="SZSE DELIST_DATE")
        records.append(
            SecurityMasterRecord(
                canonical_entity_id=_canonical_entity_id("SZSE", raw_code),
                exchange="SZSE",
                code_alias=alias,
                board=_board("SZSE", raw_code),
                listed_at=listed_at,
                delisted_at=delisted_at,
                valid_from=listed_at,
                valid_to=delisted_at,
                event_type="TERMINATED_LISTING",
                source_url=source_url,
                source_hash=source_hash,
                retrieved_at=retrieved,
                name=row[1].strip(),
            )
        )
    if not records:
        raise HistoricalSecurityMasterBlockedError("SZSE XLSX contains no A shares")
    validate_security_master_records(records)
    return ParsedOfficialSource(
        name="szse_terminated_a_shares",
        source_url=source_url,
        source_hash=source_hash,
        retrieved_at=retrieved,
        raw_bytes=raw_bytes,
        records=tuple(records),
        statistics={
            "rows": len(records),
            "b_share_rows_excluded": b_share_rows,
            "workbook_rows": len(rows) - 1,
        },
    )


def parse_szse_active_xlsx(
    raw_bytes: bytes,
    *,
    source_url: str = SZSE_ACTIVE_XLSX_URL,
    retrieved_at: str | None = None,
    expected_hash: str | None = None,
) -> ParsedOfficialSource:
    """Parse the complete official SZSE current A-share catalogue.

    CATALOGID=1110 is treated as a versioned, fail-closed source.  The full
    workbook header is frozen because silently accepting a renamed or inserted
    column could move the code/listing-date fields and corrupt historical
    membership intervals.
    """

    retrieved = _normalized_retrieved_at(retrieved_at)
    _verify_expected_hash(raw_bytes, expected_hash, "SZSE active")
    rows = _xlsx_rows(raw_bytes)
    if not rows or tuple(rows[0]) != SZSE_ACTIVE_HEADER:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE active XLSX schema drift detected"
        )
    source_hash = _sha256(raw_bytes)
    records: list[SecurityMasterRecord] = []
    seen: set[str] = set()
    unresolved_aliases: list[dict[str, str]] = []
    observation_date = datetime.fromisoformat(retrieved).date().isoformat()
    for row in rows[1:]:
        if len(row) != len(SZSE_ACTIVE_HEADER) or not any(row):
            raise HistoricalSecurityMasterBlockedError(
                "SZSE active XLSX row width is invalid"
            )
        board_label = row[0].strip()
        company_name = row[1].strip()
        raw_code = row[4].strip()
        name = row[5].strip()
        if re.fullmatch(r"\d+(?:\.0+)?", raw_code):
            raw_code = str(int(float(raw_code))).zfill(6)
        if not re.fullmatch(r"\d{6}", raw_code):
            raise HistoricalSecurityMasterBlockedError(
                f"invalid SZSE active code: {raw_code!r}"
            )
        if not raw_code.startswith(
            ("000", "001", "002", "003", "300", "301", "302")
        ):
            raise HistoricalSecurityMasterBlockedError(
                f"unknown SZSE security class in active file: {raw_code}"
            )
        if not board_label or not company_name or not name:
            raise HistoricalSecurityMasterBlockedError(
                f"SZSE active identity fields are incomplete: {raw_code}"
            )
        alias = _validate_code("SZSE", raw_code)
        if alias in seen:
            raise HistoricalSecurityMasterBlockedError(
                f"duplicate SZSE active code: {alias}"
            )
        seen.add(alias)
        listed_at = _excel_date(row[6], field_name="SZSE active LIST_DATE")
        unresolved_alias = raw_code.startswith("302")
        previous_code = SZSE_UNRESOLVED_ALIAS_PREDECESSORS.get(raw_code, "")
        if unresolved_alias:
            unresolved_aliases.append(
                {
                    "current_alias": alias,
                    "previous_alias_candidate": (
                        f"{previous_code}.SZ" if previous_code else ""
                    ),
                }
            )
        records.append(
            SecurityMasterRecord(
                canonical_entity_id=_canonical_entity_id("SZSE", raw_code),
                exchange="SZSE",
                code_alias=alias,
                board=_board("SZSE", raw_code),
                listed_at=listed_at,
                delisted_at=None,
                # A 302 code may be a renamed 300/301 security.  The current
                # catalogue proves only that the alias exists at retrieval;
                # it cannot safely backfill that alias to the original IPO.
                valid_from=observation_date if unresolved_alias else listed_at,
                valid_to=None,
                event_type=(
                    "ACTIVE_ALIAS_OBSERVATION"
                    if unresolved_alias
                    else "ACTIVE_LISTING"
                ),
                source_url=source_url,
                source_hash=source_hash,
                retrieved_at=retrieved,
                name=name,
                attributes={
                    "official_board": board_label,
                    "company_full_name": company_name,
                    "code_alias_history_status": (
                        "UNRESOLVED" if unresolved_alias else "NOT_APPLICABLE"
                    ),
                    "previous_code_candidate": previous_code,
                    "entity_chain_evidence_required": unresolved_alias,
                },
            )
        )
    if not records:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE active XLSX contains no A shares"
        )
    validate_security_master_records(records)
    return ParsedOfficialSource(
        name="szse_active_a_shares",
        source_url=source_url,
        source_hash=source_hash,
        retrieved_at=retrieved,
        raw_bytes=raw_bytes,
        records=tuple(records),
        statistics={
            "rows": len(records),
            "workbook_rows": len(rows) - 1,
            "header_columns": len(SZSE_ACTIVE_HEADER),
            "code_alias_history_complete": not unresolved_aliases,
            "unresolved_alias_count": len(unresolved_aliases),
            "unresolved_aliases": unresolved_aliases,
        },
    )


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_bse_code_mapping_html(
    raw_bytes: bytes,
    *,
    source_url: str = BSE_CODE_MAPPING_URL,
    retrieved_at: str | None = None,
    expected_hash: str | None = None,
) -> ParsedOfficialSource:
    retrieved = _normalized_retrieved_at(retrieved_at)
    _verify_expected_hash(raw_bytes, expected_hash, "BSE")
    if not raw_bytes or len(raw_bytes) > 20 * 1024 * 1024:
        raise HistoricalSecurityMasterBlockedError("BSE mapping HTML is empty or oversized")
    decoded: str | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            decoded = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise HistoricalSecurityMasterBlockedError("BSE mapping encoding is unsupported")
    parser = _TableParser()
    parser.feed(decoded)
    expected_header = ["序号", "证券简称", "上市日期", "旧代码", "新代码"]
    header_positions = [index for index, row in enumerate(parser.rows) if row == expected_header]
    if len(header_positions) != 1:
        raise HistoricalSecurityMasterBlockedError("BSE mapping schema drift detected")
    data_rows: list[list[str]] = []
    for row in parser.rows[header_positions[0] + 1 :]:
        if len(row) == 5 and re.fullmatch(r"\d+", row[0].strip()):
            data_rows.append(row)
    if not data_rows:
        raise HistoricalSecurityMasterBlockedError("BSE mapping contains no rows")
    ordinals = [_strict_int(row[0], "BSE mapping ordinal") for row in data_rows]
    if sorted(ordinals) != list(range(1, len(data_rows) + 1)):
        raise HistoricalSecurityMasterBlockedError("BSE mapping rows are incomplete or duplicated")
    source_hash = _sha256(raw_bytes)
    records: list[SecurityMasterRecord] = []
    old_codes: set[str] = set()
    new_codes: set[str] = set()
    for row in data_rows:
        name, official_listed, old_code, new_code = (
            row[1].strip(),
            row[2].strip(),
            row[3].strip(),
            row[4].strip(),
        )
        old_alias = _validate_code("BSE", old_code)
        new_alias = _validate_code("BSE", new_code)
        if not new_code.startswith("920"):
            raise HistoricalSecurityMasterBlockedError(f"invalid BSE new code: {new_code}")
        if old_alias in old_codes or new_alias in new_codes:
            raise HistoricalSecurityMasterBlockedError("duplicate BSE old/new code mapping")
        old_codes.add(old_alias)
        new_codes.add(new_alias)
        source_listed_at = _iso_date(official_listed, field_name="BSE LIST_DATE")
        listed_at = max(source_listed_at, BSE_OPEN_DATE)
        switch_date = (
            BSE_PILOT_SWITCH_DATE
            if old_code in BSE_PILOT_OLD_CODES
            else BSE_GENERAL_SWITCH_DATE
        )
        entity_id = _canonical_entity_id("BSE", old_code)
        common = {
            "canonical_entity_id": entity_id,
            "exchange": "BSE",
            "board": "BSE",
            "listed_at": listed_at,
            "delisted_at": None,
            "event_type": "CODE_ALIAS",
            "source_url": source_url,
            "source_hash": source_hash,
            "retrieved_at": retrieved,
            "name": name,
            "attributes": {
                "official_page_listed_at": source_listed_at,
                "old_code": old_code,
                "new_code": new_code,
                "switch_date": switch_date,
            },
        }
        records.append(
            SecurityMasterRecord(
                **common,
                code_alias=old_alias,
                valid_from=listed_at,
                valid_to=switch_date,
            )
        )
        records.append(
            SecurityMasterRecord(
                **common,
                code_alias=new_alias,
                valid_from=switch_date,
                valid_to=None,
            )
        )
    validate_security_master_records(records)
    return ParsedOfficialSource(
        name="bse_code_mapping",
        source_url=source_url,
        source_hash=source_hash,
        retrieved_at=retrieved,
        raw_bytes=raw_bytes,
        records=tuple(records),
        statistics={
            "mapping_rows": len(data_rows),
            "alias_records": len(records),
            "historical_rows_through_2023": sum(
                1
                for row in data_rows
                if _iso_date(row[2], field_name="BSE LIST_DATE") <= HISTORICAL_END
            ),
            "event_history_complete": False,
        },
    )


def make_transfer_records(
    *,
    canonical_entity_id: str,
    from_exchange: str,
    from_code: str,
    from_listed_at: str,
    from_delisted_at: str,
    to_exchange: str,
    to_code: str,
    to_listed_at: str,
    source_url: str,
    source_hash: str,
    retrieved_at: str,
    name: str = "",
) -> tuple[SecurityMasterRecord, SecurityMasterRecord]:
    """Create non-overlapping listing intervals for an officially evidenced transfer."""

    retrieved = _normalized_retrieved_at(retrieved_at)
    source_exchange = from_exchange.upper()
    target_exchange = to_exchange.upper()
    source_listed = _iso_date(from_listed_at, field_name="transfer source listed_at")
    source_delisted = _iso_date(from_delisted_at, field_name="transfer source delisted_at")
    target_listed = _iso_date(to_listed_at, field_name="transfer target listed_at")
    records = (
        SecurityMasterRecord(
            canonical_entity_id=canonical_entity_id,
            exchange=source_exchange,
            code_alias=_validate_code(source_exchange, from_code),
            board=_board(source_exchange, from_code),
            listed_at=source_listed,
            delisted_at=source_delisted,
            valid_from=source_listed,
            valid_to=source_delisted,
            event_type="TRANSFER_OUT",
            source_url=source_url,
            source_hash=source_hash,
            retrieved_at=retrieved,
            name=name,
        ),
        SecurityMasterRecord(
            canonical_entity_id=canonical_entity_id,
            exchange=target_exchange,
            code_alias=_validate_code(target_exchange, to_code),
            board=_board(target_exchange, to_code),
            listed_at=target_listed,
            delisted_at=None,
            valid_from=target_listed,
            valid_to=None,
            event_type="TRANSFER_IN",
            source_url=source_url,
            source_hash=source_hash,
            retrieved_at=retrieved,
            name=name,
        ),
    )
    validate_security_master_records(records)
    return records


def validate_security_master_records(records: Iterable[SecurityMasterRecord]) -> None:
    values = list(records)
    if not values:
        raise HistoricalSecurityMasterBlockedError("security master contains no records")
    exact: set[bytes] = set()
    aliases: dict[tuple[str, str], list[SecurityMasterRecord]] = {}
    entities: dict[str, list[SecurityMasterRecord]] = {}
    for record in values:
        if not record.canonical_entity_id or not record.event_type:
            raise HistoricalSecurityMasterBlockedError("security master identity is incomplete")
        _validate_code(record.exchange, record.code_alias)
        listed = _iso_date(record.listed_at, field_name="listed_at")
        valid_from = _iso_date(record.valid_from, field_name="valid_from")
        delisted = (
            _iso_date(record.delisted_at, field_name="delisted_at")
            if record.delisted_at
            else None
        )
        valid_to = (
            _iso_date(record.valid_to, field_name="valid_to") if record.valid_to else None
        )
        _normalized_retrieved_at(record.retrieved_at)
        if not re.fullmatch(r"[0-9a-f]{64}", record.source_hash):
            raise HistoricalSecurityMasterBlockedError("record source_hash is not SHA-256")
        if valid_from < listed:
            raise HistoricalSecurityMasterBlockedError("alias starts before listing")
        if delisted and delisted <= listed:
            raise HistoricalSecurityMasterBlockedError("delisting must follow listing")
        if valid_to and valid_to <= valid_from:
            raise HistoricalSecurityMasterBlockedError("valid_to must follow valid_from")
        if delisted and valid_to and valid_to > delisted:
            raise HistoricalSecurityMasterBlockedError("alias exceeds listing interval")
        encoded = _canonical_json_bytes(record.to_dict())
        if encoded in exact:
            raise HistoricalSecurityMasterBlockedError("duplicate security master record")
        exact.add(encoded)
        aliases.setdefault((record.exchange, record.code_alias), []).append(record)
        entities.setdefault(record.canonical_entity_id, []).append(record)
    for label, grouped in (("alias", aliases), ("entity", entities)):
        for key, intervals in grouped.items():
            ordered = sorted(intervals, key=lambda item: (item.valid_from, item.valid_to or "9999"))
            for previous, current in zip(ordered, ordered[1:]):
                previous_end = previous.valid_to or "9999-12-31"
                if current.valid_from < previous_end:
                    raise HistoricalSecurityMasterBlockedError(
                        f"overlapping {label} intervals for {key}"
                    )


def records_active_on(
    records: Iterable[SecurityMasterRecord], asof: str
) -> tuple[SecurityMasterRecord, ...]:
    target = _iso_date(asof, field_name="asof")
    return tuple(
        record
        for record in records
        if record.listed_at <= target
        and (record.delisted_at is None or target < record.delisted_at)
        and record.valid_from <= target
        and (record.valid_to is None or target < record.valid_to)
    )


def _normalize_active_codes(codes: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for item in codes:
        value = str(item or "").strip().upper()
        if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", value):
            raise HistoricalSecurityMasterBlockedError(
                f"TDX active snapshot contains invalid code: {item!r}"
            )
        if value in normalized:
            raise HistoricalSecurityMasterBlockedError(
                f"TDX active snapshot contains duplicate code: {value}"
            )
        normalized.add(value)
    if not normalized:
        raise HistoricalSecurityMasterBlockedError("TDX active snapshot is empty")
    return normalized


def _record_fingerprints(
    records: Iterable[SecurityMasterRecord],
) -> tuple[bytes, ...]:
    return tuple(sorted(_canonical_json_bytes(record.to_dict()) for record in records))


def _revalidate_szse_code_change_artifact(
    artifact: SZSECodeChangeArtifact,
) -> tuple[SZSECodeChangeArtifact, bytes]:
    """Rebuild an admitted event from its exact CAS object, never a caller flag."""

    if type(artifact) is not SZSECodeChangeArtifact:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change artifact type is not admitted"
        )
    if not artifact.ready or artifact.status != SZSE_CODE_CHANGE_ADMITTED:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change artifact is not admitted"
        )
    if (
        artifact.text_evidence is None
        or not artifact.text_evidence.recomputed_from_raw
        or artifact.text_evidence.raw_pdf_sha256
        != artifact.raw_evidence.content_sha256
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change artifact lacks raw-recomputed text evidence"
        )
    try:
        raw_pdf = Path(artifact.raw_evidence.object_path).read_bytes()
        replayed = parse_szse_code_change_pdf(
            raw_pdf,
            raw_evidence=artifact.raw_evidence,
        )
    except (OSError, SZSECodeChangeBlockedError) as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"SZSE code-change CAS/text replay failed closed: {exc}"
        ) from exc
    if (
        not replayed.ready
        or replayed.status != SZSE_CODE_CHANGE_ADMITTED
        or replayed.to_dict() != artifact.to_dict()
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change artifact changed after replay"
        )
    try:
        validate_szse_code_change_intervals(replayed.intervals)
    except SZSECodeChangeBlockedError as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"SZSE code-change interval contract failed: {exc}"
        ) from exc
    if (
        replayed.raw_evidence.source_url != SZSE_CODE_CHANGE_SOURCE_URL
        or replayed.raw_evidence.content_sha256 != _sha256(raw_pdf)
        or len(replayed.intervals) != 2
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change source identity is inconsistent"
        )
    return replayed, raw_pdf


def _verified_szse_active_source_for_resolution(
    sources: Sequence[ParsedOfficialSource],
) -> ParsedOfficialSource:
    matches = [source for source in sources if source.name == "szse_active_a_shares"]
    if len(matches) != 1:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change resolution requires exactly one active catalogue"
        )
    source = matches[0]
    reparsed = parse_szse_active_xlsx(
        source.raw_bytes,
        source_url=source.source_url,
        retrieved_at=source.retrieved_at,
        expected_hash=source.source_hash,
    )
    if (
        reparsed.source_url != SZSE_ACTIVE_XLSX_URL
        or reparsed.name != source.name
        or reparsed.source_hash != source.source_hash
        or _record_fingerprints(reparsed.records)
        != _record_fingerprints(source.records)
        or dict(reparsed.statistics) != dict(source.statistics)
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SZSE active catalogue changed during code-change resolution"
        )
    return source


def _materialize_szse_code_change_source(
    *,
    artifact: SZSECodeChangeArtifact,
    raw_pdf: bytes,
    observation: SecurityMasterRecord,
) -> ParsedOfficialSource:
    if observation.code_alias != SZSE_CODE_CHANGE_NEW_CODE or str(
        observation.attributes.get("previous_code_candidate") or ""
    ) != SZSE_CODE_CHANGE_OLD_CODE.removesuffix(".SZ"):
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change event does not match the active alias observation"
        )
    if observation.event_type != "ACTIVE_ALIAS_OBSERVATION":
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change target is not an unresolved active observation"
        )
    if observation.listed_at >= SZSE_CODE_CHANGE_EFFECTIVE_DATE:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE active listing date does not precede the code-change boundary"
        )
    if artifact.text_evidence is None:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change text evidence is missing"
        )
    common_attributes = {
        "active_catalog_source_url": observation.source_url,
        "active_catalog_source_hash": observation.source_hash,
        "active_catalog_retrieved_at": observation.retrieved_at,
        "active_catalog_listed_at": observation.listed_at,
        "event_protocol_version": SZSE_CODE_CHANGE_PROTOCOL_VERSION,
        "event_raw_pdf_sha256": artifact.raw_evidence.content_sha256,
        "event_text_sha256": artifact.text_evidence.text_sha256,
        "event_text_engine": artifact.text_evidence.engine,
        "event_text_engine_version": artifact.text_evidence.engine_version,
        "event_text_recomputed_from_raw": True,
        "event_effective_at": SZSE_CODE_CHANGE_EFFECTIVE_DATE,
    }
    materialized: list[SecurityMasterRecord] = []
    for interval in artifact.intervals:
        if interval.canonical_entity_id != SZSE_CODE_CHANGE_ENTITY_ID:
            raise HistoricalSecurityMasterBlockedError(
                "SZSE code-change canonical entity is inconsistent"
            )
        if interval.code_alias == SZSE_CODE_CHANGE_OLD_CODE:
            valid_from = observation.listed_at
            valid_to = interval.valid_to
        elif interval.code_alias == SZSE_CODE_CHANGE_NEW_CODE:
            valid_from = interval.valid_from
            valid_to = interval.valid_to
        else:
            raise HistoricalSecurityMasterBlockedError(
                "SZSE code-change artifact contains an unexpected alias"
            )
        materialized.append(
            SecurityMasterRecord(
                canonical_entity_id=SZSE_CODE_CHANGE_ENTITY_ID,
                exchange="SZSE",
                code_alias=interval.code_alias,
                board=observation.board,
                listed_at=observation.listed_at,
                delisted_at=None,
                valid_from=str(valid_from or ""),
                valid_to=valid_to,
                event_type=interval.event_type,
                source_url=artifact.raw_evidence.source_url,
                source_hash=artifact.raw_evidence.content_sha256,
                retrieved_at=artifact.raw_evidence.retrieved_at,
                name=interval.name,
                attributes={
                    **common_attributes,
                    "alias_interval_role": (
                        "PRE_CHANGE" if interval.code_alias == SZSE_CODE_CHANGE_OLD_CODE else "POST_CHANGE"
                    ),
                },
            )
        )
    records = tuple(materialized)
    validate_security_master_records(records)
    old, new = sorted(records, key=lambda item: item.valid_from)
    if (
        old.code_alias != SZSE_CODE_CHANGE_OLD_CODE
        or old.valid_from != observation.listed_at
        or old.valid_to != SZSE_CODE_CHANGE_EFFECTIVE_DATE
        or new.code_alias != SZSE_CODE_CHANGE_NEW_CODE
        or new.valid_from != SZSE_CODE_CHANGE_EFFECTIVE_DATE
        or new.valid_to is not None
        or len({item.canonical_entity_id for item in records}) != 1
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SZSE materialized code-change intervals are not atomic"
        )
    interval_manifest = [
        {
            "canonical_entity_id": item.canonical_entity_id,
            "code_alias": item.code_alias,
            "valid_from": item.valid_from,
            "valid_to": item.valid_to,
        }
        for item in sorted(records, key=lambda item: item.valid_from)
    ]
    return ParsedOfficialSource(
        name=SZSE_CODE_CHANGE_SOURCE_NAME,
        source_url=artifact.raw_evidence.source_url,
        source_hash=artifact.raw_evidence.content_sha256,
        retrieved_at=artifact.raw_evidence.retrieved_at,
        raw_bytes=raw_pdf,
        records=records,
        statistics={
            "protocol_version": SZSE_CODE_CHANGE_PROTOCOL_VERSION,
            "admission_status": artifact.status,
            "raw_pdf_sha256": artifact.raw_evidence.content_sha256,
            "raw_pdf_cas_uri": artifact.raw_evidence.cas_uri,
            "text_sha256": artifact.text_evidence.text_sha256,
            "text_raw_pdf_sha256": artifact.text_evidence.raw_pdf_sha256,
            "text_engine": artifact.text_evidence.engine,
            "text_engine_version": artifact.text_evidence.engine_version,
            "text_page_count": artifact.text_evidence.page_count,
            "text_recomputed_from_raw": artifact.text_evidence.recomputed_from_raw,
            "canonical_entity_id": SZSE_CODE_CHANGE_ENTITY_ID,
            "effective_at": SZSE_CODE_CHANGE_EFFECTIVE_DATE,
            "interval_count": len(records),
            "intervals": interval_manifest,
            "active_catalog_source_hash": observation.source_hash,
            "active_catalog_listed_at": observation.listed_at,
        },
    )


def _integrate_szse_code_change_artifacts(
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    artifacts: Sequence[SZSECodeChangeArtifact],
) -> tuple[
    tuple[SecurityMasterRecord, ...],
    tuple[ParsedOfficialSource, ...],
    tuple[str, ...],
]:
    values = tuple(artifacts)
    if not values:
        return tuple(records), tuple(sources), ()
    if len(values) != 1:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change artifacts must resolve each alias exactly once"
        )
    validate_security_master_records(records)
    active_source = _verified_szse_active_source_for_resolution(sources)
    artifact, raw_pdf = _revalidate_szse_code_change_artifact(values[0])
    matches = [
        record
        for record in active_source.records
        if record.code_alias == SZSE_CODE_CHANGE_NEW_CODE
        and record.event_type == "ACTIVE_ALIAS_OBSERVATION"
        and str(record.attributes.get("previous_code_candidate") or "")
        == SZSE_CODE_CHANGE_OLD_CODE.removesuffix(".SZ")
    ]
    if len(matches) != 1:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change event must match exactly one unresolved observation"
        )
    observation = matches[0]
    event_source = _materialize_szse_code_change_source(
        artifact=artifact,
        raw_pdf=raw_pdf,
        observation=observation,
    )
    existing_event_sources = [
        source for source in sources if source.name == SZSE_CODE_CHANGE_SOURCE_NAME
    ]
    observation_fingerprint = _canonical_json_bytes(observation.to_dict())
    working_records = list(records)
    observation_positions = [
        index
        for index, record in enumerate(working_records)
        if _canonical_json_bytes(record.to_dict()) == observation_fingerprint
    ]
    if existing_event_sources:
        if len(existing_event_sources) != 1 or existing_event_sources[0] != event_source:
            raise HistoricalSecurityMasterBlockedError(
                "SZSE code-change source conflicts with the admitted replay"
            )
        if observation_positions:
            raise HistoricalSecurityMasterBlockedError(
                "resolved SZSE observation remains beside materialized aliases"
            )
        event_fingerprints = set(_record_fingerprints(event_source.records))
        master_fingerprints = set(_record_fingerprints(working_records))
        if not event_fingerprints.issubset(master_fingerprints):
            raise HistoricalSecurityMasterBlockedError(
                "materialized SZSE code-change records are missing from master"
            )
        normalized_sources = tuple(sources)
    else:
        if len(observation_positions) != 1:
            raise HistoricalSecurityMasterBlockedError(
                "SZSE active observation is missing or duplicated in master"
            )
        del working_records[observation_positions[0]]
        working_records.extend(event_source.records)
        normalized_sources = (*tuple(sources), event_source)
    normalized_records = tuple(working_records)
    validate_security_master_records(normalized_records)
    return (
        normalized_records,
        tuple(normalized_sources),
        (f"{SZSE_CODE_CHANGE_OLD_CODE}->{SZSE_CODE_CHANGE_NEW_CODE}",),
    )


def integrate_szse_code_change_artifacts(
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    artifacts: Sequence[SZSECodeChangeArtifact],
) -> tuple[tuple[SecurityMasterRecord, ...], tuple[ParsedOfficialSource, ...]]:
    """Replace unresolved active observations with admitted, point-in-time aliases."""

    normalized_records, normalized_sources, _ = _integrate_szse_code_change_artifacts(
        records,
        sources,
        artifacts,
    )
    return normalized_records, normalized_sources


def _replay_fixed_bse_termination_manifest(
    manifest_sha256: str,
) -> tuple[Mapping[str, Any], bytes]:
    """Replay the admitted V2 manifest from the one policy-bound CAS root."""

    digest = str(manifest_sha256 or "").strip().lower()
    if digest != BSE_TERMINATION_EVENT_MANIFEST_SHA256:
        raise HistoricalSecurityMasterBlockedError(
            "BSE termination-event manifest is not the policy-bound V2 release"
        )
    root = Path(BSE_TERMINATION_EVENT_STORE_ROOT).resolve()
    path = (root / "manifests" / f"{digest}.json").resolve()
    try:
        byte_count = path.stat().st_size
    except OSError as exc:
        raise HistoricalSecurityMasterBlockedError(
            "BSE termination-event manifest is unavailable from the fixed CAS store"
        ) from exc
    evidence = BSETerminationManifestEvidence(
        manifest_sha256=digest,
        cas_uri=f"sha256:{digest}",
        object_path=str(path),
        byte_count=byte_count,
    )
    try:
        value = BSEEventLedgerStore(root).verify_manifest(evidence)
    except (OSError, BSETerminationEventBlockedError) as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"BSE termination-event fixed-store replay failed closed: {exc}"
        ) from exc
    try:
        manifest_bytes = path.read_bytes()
    except OSError as exc:
        raise HistoricalSecurityMasterBlockedError(
            "BSE termination-event manifest disappeared after replay"
        ) from exc
    completeness = value.get("completeness")
    scope = completeness.get("scope") if isinstance(completeness, dict) else None
    events = value.get("events")
    if (
        _sha256(manifest_bytes) != digest
        or value.get("protocol_version")
        != BSE_TERMINATION_EVENT_PROTOCOL_VERSION
        or value.get("logical_content_sha256")
        != BSE_TERMINATION_EVENT_LOGICAL_SHA256
        or not isinstance(completeness, dict)
        or completeness.get("ready") is not True
        or completeness.get("status")
        != BSE_TERMINATION_EVENT_SOURCE_COMPLETE
        or completeness.get("promotion_blocked") is not False
        or completeness.get("source_pagination_complete") is not True
        or completeness.get("termination_count") != 3
        or completeness.get("transfer_count") != 3
        or completeness.get("delist_count") != 0
        or completeness.get("unclassified_count") != 0
        or completeness.get("missing_effective_date_count") != 0
        or not isinstance(scope, dict)
        or scope.get("start_date") != BSE_OPEN_DATE
        or scope.get("end_date") != HISTORICAL_END
        or not isinstance(events, list)
        or len(events) != 3
    ):
        raise HistoricalSecurityMasterBlockedError(
            "BSE termination-event V2 completeness contract is inconsistent"
        )
    return value, manifest_bytes


def _bse_manifest_retrieved_at(value: Mapping[str, Any]) -> str:
    observed: list[datetime] = []
    for collection_name in (
        "raw_pages",
        "termination_notice_evidence",
        "target_evidence",
    ):
        collection = value.get(collection_name)
        if not isinstance(collection, list) or not collection:
            raise HistoricalSecurityMasterBlockedError(
                f"BSE termination-event manifest lacks {collection_name}"
            )
        for item in collection:
            if not isinstance(item, dict):
                raise HistoricalSecurityMasterBlockedError(
                    "BSE termination-event evidence schema is invalid"
                )
            retrieved_at = item.get("retrieved_at")
            if not isinstance(retrieved_at, str) or not retrieved_at:
                raise HistoricalSecurityMasterBlockedError(
                    "BSE termination-event evidence lacks retrieved_at"
                )
            normalized = _normalized_retrieved_at(retrieved_at)
            observed.append(datetime.fromisoformat(normalized))
    if not observed:
        raise HistoricalSecurityMasterBlockedError(
            "BSE termination-event manifest has no retrieval timestamps"
        )
    return max(observed).isoformat()


def _materialize_bse_termination_event_source(
    *,
    value: Mapping[str, Any],
    manifest_bytes: bytes,
    sources: Sequence[ParsedOfficialSource],
) -> ParsedOfficialSource:
    digest = _sha256(manifest_bytes)
    if digest != BSE_TERMINATION_EVENT_MANIFEST_SHA256:
        raise HistoricalSecurityMasterBlockedError(
            "BSE termination-event manifest bytes changed during materialization"
        )
    target_observations: dict[str, SecurityMasterRecord] = {}
    for source in sources:
        if source.name not in {
            "sse_active_a_shares",
            "szse_active_a_shares",
            "sse_terminated_a_shares",
            "szse_terminated_a_shares",
        }:
            continue
        for record in source.records:
            if record.code_alias in target_observations:
                raise HistoricalSecurityMasterBlockedError(
                    "transfer target alias is duplicated across official catalogues"
                )
            target_observations[record.code_alias] = record
    events = value.get("events")
    if not isinstance(events, list):
        raise HistoricalSecurityMasterBlockedError(
            "BSE termination-event manifest lacks classified events"
        )
    retrieved_at = _bse_manifest_retrieved_at(value)
    source_url = f"urn:sha256:{digest}"
    logical_sha256 = str(value.get("logical_content_sha256") or "")
    records: list[SecurityMasterRecord] = []
    target_catalog_hashes: dict[str, str] = {}
    target_catalog_event_types: dict[str, str] = {}
    event_codes: list[str] = []
    for event in sorted(events, key=lambda item: str(item.get("source_code_alias"))):
        if not isinstance(event, dict):
            raise HistoricalSecurityMasterBlockedError(
                "BSE termination-event item is invalid"
            )
        canonical_entity_id = str(event.get("canonical_entity_id") or "")
        legal_name = str(event.get("legal_name") or "").strip()
        source_code = str(event.get("source_code_alias") or "")
        target_code = str(event.get("target_code_alias") or "")
        target_exchange = str(event.get("target_exchange") or "")
        effective_at = _iso_date(
            event.get("termination_effective_date"),
            field_name="BSE termination effective date",
        )
        target_listed_at = _iso_date(
            event.get("target_listing_date"),
            field_name="BSE transfer target listing date",
        )
        if (
            event.get("classification") != "TRANSFER"
            or event.get("source_exchange") != "BSE"
            or not canonical_entity_id.startswith("CN:LEGAL_ENTITY:SHA256:")
            or not legal_name
            or _validate_code("BSE", source_code) != source_code
            or target_exchange not in {"SSE", "SZSE"}
            or _validate_code(target_exchange, target_code) != target_code
            or effective_at <= BSE_OPEN_DATE
            or target_listed_at < effective_at
        ):
            raise HistoricalSecurityMasterBlockedError(
                "BSE termination-event transfer identity or dates are invalid"
            )
        target_observation = target_observations.get(target_code)
        target_is_active = (
            target_observation is not None
            and target_observation.event_type == "ACTIVE_LISTING"
            and target_observation.valid_to is None
            and target_observation.delisted_at is None
        )
        target_is_terminated = (
            target_observation is not None
            and target_observation.event_type == "TERMINATED_LISTING"
            and target_observation.valid_to is not None
            and target_observation.delisted_at == target_observation.valid_to
            and target_observation.valid_to > target_listed_at
        )
        if (
            target_observation is None
            or target_observation.exchange != target_exchange
            or target_observation.listed_at != target_listed_at
            or target_observation.valid_from != target_listed_at
            or not (target_is_active or target_is_terminated)
        ):
            raise HistoricalSecurityMasterBlockedError(
                "BSE transfer target is not confirmed by exactly one official "
                f"active-or-terminated catalogue interval: {target_code}"
            )
        common_attributes = {
            "bse_event_protocol_version": BSE_TERMINATION_EVENT_PROTOCOL_VERSION,
            "bse_event_manifest_sha256": digest,
            "bse_event_logical_content_sha256": logical_sha256,
            "legal_name": legal_name,
            "termination_notice_date": str(event.get("termination_notice_date") or ""),
            "termination_notice_url": str(event.get("termination_notice_url") or ""),
            "termination_effective_date": effective_at,
            "termination_evidence_url": str(event.get("termination_evidence_url") or ""),
            "termination_evidence_sha256": str(
                event.get("termination_evidence_sha256") or ""
            ),
            "target_exchange": target_exchange,
            "target_code_alias": target_code,
            "target_listing_date": target_listed_at,
            "target_evidence_url": str(event.get("target_evidence_url") or ""),
            "target_evidence_sha256": str(event.get("target_evidence_sha256") or ""),
            "target_catalog_event_type": target_observation.event_type,
            "target_catalog_source_url": target_observation.source_url,
            "target_catalog_source_hash": target_observation.source_hash,
            "target_catalog_retrieved_at": target_observation.retrieved_at,
            "target_catalog_delisted_at": target_observation.delisted_at or "",
        }
        if any(
            not common_attributes[key]
            for key in (
                "termination_notice_date",
                "termination_notice_url",
                "termination_evidence_url",
                "termination_evidence_sha256",
                "target_evidence_url",
                "target_evidence_sha256",
            )
        ):
            raise HistoricalSecurityMasterBlockedError(
                "BSE transfer event is missing official evidence metadata"
            )
        records.extend(
            (
                SecurityMasterRecord(
                    canonical_entity_id=canonical_entity_id,
                    exchange="BSE",
                    code_alias=source_code,
                    board="BSE",
                    listed_at=BSE_OPEN_DATE,
                    delisted_at=effective_at,
                    valid_from=BSE_OPEN_DATE,
                    valid_to=effective_at,
                    event_type="TRANSFER_OUT",
                    source_url=source_url,
                    source_hash=digest,
                    retrieved_at=retrieved_at,
                    name=legal_name,
                    attributes={**common_attributes, "transfer_interval_role": "SOURCE"},
                ),
                SecurityMasterRecord(
                    canonical_entity_id=canonical_entity_id,
                    exchange=target_exchange,
                    code_alias=target_code,
                    board=_board(target_exchange, target_code),
                    listed_at=target_listed_at,
                    delisted_at=target_observation.delisted_at,
                    valid_from=target_listed_at,
                    valid_to=target_observation.valid_to,
                    event_type="TRANSFER_IN",
                    source_url=source_url,
                    source_hash=digest,
                    retrieved_at=retrieved_at,
                    name=legal_name,
                    attributes={**common_attributes, "transfer_interval_role": "TARGET"},
                ),
            )
        )
        event_codes.append(f"{source_code}->{target_code}")
        target_catalog_hashes[target_code] = target_observation.source_hash
        target_catalog_event_types[target_code] = target_observation.event_type
    normalized_records = tuple(records)
    validate_security_master_records(normalized_records)
    completeness = dict(value["completeness"])
    return ParsedOfficialSource(
        name=BSE_TERMINATION_EVENT_SOURCE_NAME,
        source_url=source_url,
        source_hash=digest,
        retrieved_at=retrieved_at,
        raw_bytes=manifest_bytes,
        records=normalized_records,
        statistics={
            "protocol_version": BSE_TERMINATION_EVENT_PROTOCOL_VERSION,
            "status": completeness["status"],
            "ready": completeness["ready"],
            "manifest_sha256": digest,
            "logical_content_sha256": logical_sha256,
            "termination_count": completeness["termination_count"],
            "transfer_count": completeness["transfer_count"],
            "unclassified_count": completeness["unclassified_count"],
            "missing_effective_date_count": completeness[
                "missing_effective_date_count"
            ],
            "source_pagination_complete": completeness[
                "source_pagination_complete"
            ],
            "event_codes": event_codes,
            "interval_count": len(normalized_records),
            "target_catalog_hashes": dict(sorted(target_catalog_hashes.items())),
            "target_catalog_event_types": dict(
                sorted(target_catalog_event_types.items())
            ),
        },
    )


def _integrate_bse_termination_event_manifest(
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    manifest_sha256: str,
) -> tuple[tuple[SecurityMasterRecord, ...], tuple[ParsedOfficialSource, ...]]:
    validate_security_master_records(records)
    value, manifest_bytes = _replay_fixed_bse_termination_manifest(manifest_sha256)
    event_source = _materialize_bse_termination_event_source(
        value=value,
        manifest_bytes=manifest_bytes,
        sources=sources,
    )
    existing = [
        source
        for source in sources
        if source.name == BSE_TERMINATION_EVENT_SOURCE_NAME
    ]
    working = list(records)
    event_fingerprints = set(_record_fingerprints(event_source.records))
    master_fingerprints = set(_record_fingerprints(working))
    target_codes = {
        record.code_alias
        for record in event_source.records
        if record.event_type == "TRANSFER_IN"
    }
    if existing:
        if len(existing) != 1 or existing[0] != event_source:
            raise HistoricalSecurityMasterBlockedError(
                "BSE termination-event source conflicts with fixed-store replay"
            )
        if not event_fingerprints.issubset(master_fingerprints):
            raise HistoricalSecurityMasterBlockedError(
                "BSE transfer intervals are missing from the master"
            )
        if any(
            record.event_type == "ACTIVE_LISTING"
            and record.code_alias in target_codes
            for record in working
        ):
            raise HistoricalSecurityMasterBlockedError(
                "superseded transfer-target active observations remain in the master"
            )
        return tuple(working), tuple(sources)
    if event_fingerprints & master_fingerprints:
        raise HistoricalSecurityMasterBlockedError(
            "BSE transfer intervals exist without their fixed-store source"
        )
    target_observations = [
        record
        for source in sources
        if source.name
        in {
            "sse_active_a_shares",
            "szse_active_a_shares",
            "sse_terminated_a_shares",
            "szse_terminated_a_shares",
        }
        for record in source.records
        if record.code_alias in target_codes
    ]
    for observation in target_observations:
        fingerprint = _canonical_json_bytes(observation.to_dict())
        positions = [
            index
            for index, record in enumerate(working)
            if _canonical_json_bytes(record.to_dict()) == fingerprint
        ]
        if len(positions) != 1:
            raise HistoricalSecurityMasterBlockedError(
                f"BSE transfer target is missing or duplicated in master: {observation.code_alias}"
            )
        del working[positions[0]]
    if len(target_observations) != len(target_codes):
        raise HistoricalSecurityMasterBlockedError(
            "not every BSE transfer target has one official active-or-terminated observation"
        )
    source_codes = {
        record.code_alias
        for record in event_source.records
        if record.event_type == "TRANSFER_OUT"
    }
    if any(record.code_alias in source_codes for record in working):
        raise HistoricalSecurityMasterBlockedError(
            "BSE transfer source conflicts with an existing master interval"
        )
    working.extend(event_source.records)
    normalized_records = tuple(working)
    normalized_sources = (*tuple(sources), event_source)
    validate_security_master_records(normalized_records)
    return normalized_records, normalized_sources


def integrate_bse_termination_event_manifest(
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    manifest_sha256: str = BSE_TERMINATION_EVENT_MANIFEST_SHA256,
) -> tuple[tuple[SecurityMasterRecord, ...], tuple[ParsedOfficialSource, ...]]:
    """Materialize BSE transfer intervals only after fixed-store V2 replay."""

    return _integrate_bse_termination_event_manifest(
        records,
        sources,
        manifest_sha256,
    )


def _verified_bse_termination_event_metadata(
    *,
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    source_by_name: Mapping[str, ParsedOfficialSource],
) -> dict[str, Any]:
    source = source_by_name.get(BSE_TERMINATION_EVENT_SOURCE_NAME)
    if source is None:
        return {
            "verified": False,
            "protocol_version": "",
            "manifest_sha256": "",
            "logical_content_sha256": "",
            "termination_count": 0,
            "transfer_count": 0,
            "interval_count": 0,
        }
    value, manifest_bytes = _replay_fixed_bse_termination_manifest(
        BSE_TERMINATION_EVENT_MANIFEST_SHA256
    )
    expected = _materialize_bse_termination_event_source(
        value=value,
        manifest_bytes=manifest_bytes,
        sources=sources,
    )
    if source != expected:
        raise HistoricalSecurityMasterBlockedError(
            "BSE termination-event source changed after fixed-store replay"
        )
    master_fingerprints = set(_record_fingerprints(records))
    if not set(_record_fingerprints(expected.records)).issubset(master_fingerprints):
        raise HistoricalSecurityMasterBlockedError(
            "verified BSE transfer intervals are missing from the master"
        )
    target_codes = {
        record.code_alias
        for record in expected.records
        if record.event_type == "TRANSFER_IN"
    }
    if any(
        record.code_alias in target_codes
        and record.event_type in {"ACTIVE_LISTING", "TERMINATED_LISTING"}
        for record in records
    ):
        raise HistoricalSecurityMasterBlockedError(
            "verified BSE transfer target remains duplicated as an active listing"
        )
    statistics = dict(expected.statistics)
    return {
        "verified": True,
        "protocol_version": statistics["protocol_version"],
        "manifest_sha256": statistics["manifest_sha256"],
        "logical_content_sha256": statistics["logical_content_sha256"],
        "termination_count": statistics["termination_count"],
        "transfer_count": statistics["transfer_count"],
        "interval_count": statistics["interval_count"],
    }


def _validate_source_bundle(
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
) -> dict[str, ParsedOfficialSource]:
    """Verify raw/source/record identity before any completeness decision."""

    by_name: dict[str, ParsedOfficialSource] = {}
    master_fingerprints = set(_record_fingerprints(records))
    resolved_active_aliases = {
        record.code_alias
        for source in sources
        if source.name == SZSE_CODE_CHANGE_SOURCE_NAME
        for record in source.records
        if record.event_type == "SECURITY_CODE_CHANGE_IN"
    }
    bse_transfer_targets = {
        record.code_alias
        for source in sources
        if source.name == BSE_TERMINATION_EVENT_SOURCE_NAME
        for record in source.records
        if record.event_type == "TRANSFER_IN"
    }
    bse_current_delisted_aliases = {
        record.code_alias
        for source in sources
        if source.name == BSE_CURRENT_DELISTING_SOURCE_NAME
        for record in source.records
        if record.event_type == "TERMINATED_LISTING"
    }
    for source in sources:
        if not source.name or source.name in by_name:
            raise HistoricalSecurityMasterBlockedError(
                f"duplicate or empty official source name: {source.name!r}"
            )
        if not source.records:
            raise HistoricalSecurityMasterBlockedError(
                f"official source contains no records: {source.name}"
            )
        if _sha256(source.raw_bytes) != source.source_hash:
            raise HistoricalSecurityMasterBlockedError(
                f"official source hash mismatch: {source.name}"
            )
        _normalized_retrieved_at(source.retrieved_at)
        for record in source.records:
            if (
                record.source_url != source.source_url
                or record.source_hash != source.source_hash
                or record.retrieved_at != source.retrieved_at
            ):
                raise HistoricalSecurityMasterBlockedError(
                    f"official source metadata mismatch: {source.name}"
                )
            if _canonical_json_bytes(record.to_dict()) not in master_fingerprints:
                superseded_observation = (
                    source.name == "szse_active_a_shares"
                    and record.event_type == "ACTIVE_ALIAS_OBSERVATION"
                    and record.code_alias in resolved_active_aliases
                )
                if superseded_observation:
                    continue
                superseded_transfer_target = (
                    source.name
                    in {
                        "sse_active_a_shares",
                        "szse_active_a_shares",
                        "sse_terminated_a_shares",
                        "szse_terminated_a_shares",
                    }
                    and record.event_type
                    in {"ACTIVE_LISTING", "TERMINATED_LISTING"}
                    and record.code_alias in bse_transfer_targets
                )
                if superseded_transfer_target:
                    continue
                superseded_bse_current_observation = (
                    source.name == "bse_code_mapping"
                    and record.code_alias in bse_current_delisted_aliases
                    and record.valid_to is None
                )
                if superseded_bse_current_observation:
                    continue
                raise HistoricalSecurityMasterBlockedError(
                    f"official source record omitted from master: {source.name}"
                )
        by_name[source.name] = source
    return by_name


def _verified_active_aliases(
    *,
    source_by_name: Mapping[str, ParsedOfficialSource],
    name: str,
    source_url: str,
    exchange: str,
    parser: Any,
    allow_unresolved_alias_observations: bool = False,
) -> tuple[bool, set[str], tuple[str, ...]]:
    source = source_by_name.get(name)
    if source is None:
        return False, set(), ()
    if source.source_url != source_url:
        raise HistoricalSecurityMasterBlockedError(
            f"{name} official URL changed"
        )
    reparsed = parser(
        source.raw_bytes,
        source_url=source.source_url,
        retrieved_at=source.retrieved_at,
        expected_hash=source.source_hash,
    )
    if (
        reparsed.name != source.name
        or reparsed.source_hash != source.source_hash
        or _record_fingerprints(reparsed.records)
        != _record_fingerprints(source.records)
        or dict(reparsed.statistics) != dict(source.statistics)
    ):
        raise HistoricalSecurityMasterBlockedError(
            f"{name} parsed evidence changed after ingestion"
        )
    unresolved_aliases: list[str] = []
    observation_date = datetime.fromisoformat(source.retrieved_at).date().isoformat()
    for record in source.records:
        common_invalid = (
            record.exchange != exchange
            or record.delisted_at is not None
            or record.valid_to is not None
        )
        if common_invalid:
            raise HistoricalSecurityMasterBlockedError(
                f"{name} contains an invalid active interval"
            )
        if record.event_type == "ACTIVE_LISTING":
            if record.valid_from != record.listed_at:
                raise HistoricalSecurityMasterBlockedError(
                    f"{name} contains an invalid active listing interval"
                )
            continue
        if not (
            allow_unresolved_alias_observations
            and record.event_type == "ACTIVE_ALIAS_OBSERVATION"
            and record.code_alias.startswith("302")
            and record.valid_from == observation_date
            and record.attributes.get("code_alias_history_status") == "UNRESOLVED"
            and record.attributes.get("entity_chain_evidence_required") is True
        ):
            raise HistoricalSecurityMasterBlockedError(
                f"{name} contains an invalid active alias observation"
            )
        previous = str(record.attributes.get("previous_code_candidate") or "")
        unresolved_aliases.append(
            f"{previous + '.SZ' if previous else '?'}->{record.code_alias}"
        )
    return (
        True,
        {record.code_alias for record in source.records},
        tuple(sorted(unresolved_aliases)),
    )


def _verified_szse_code_change_metadata(
    source_by_name: Mapping[str, ParsedOfficialSource],
    *,
    admitted: bool,
) -> dict[str, Any]:
    if not admitted:
        return {
            "protocol_version": "",
            "raw_pdf_sha256": "",
            "text_sha256": "",
            "interval_count": 0,
        }
    source = source_by_name.get(SZSE_CODE_CHANGE_SOURCE_NAME)
    if source is None:
        raise HistoricalSecurityMasterBlockedError(
            "admitted SZSE code-change source is absent"
        )
    statistics = dict(source.statistics)
    expected_intervals = [
        {
            "canonical_entity_id": record.canonical_entity_id,
            "code_alias": record.code_alias,
            "valid_from": record.valid_from,
            "valid_to": record.valid_to,
        }
        for record in sorted(source.records, key=lambda item: item.valid_from)
    ]
    text_hash = str(statistics.get("text_sha256") or "")
    if (
        source.source_url != SZSE_CODE_CHANGE_SOURCE_URL
        or statistics.get("protocol_version")
        != SZSE_CODE_CHANGE_PROTOCOL_VERSION
        or statistics.get("admission_status") != SZSE_CODE_CHANGE_ADMITTED
        or statistics.get("raw_pdf_sha256") != source.source_hash
        or statistics.get("text_raw_pdf_sha256") != source.source_hash
        or statistics.get("text_recomputed_from_raw") is not True
        or not re.fullmatch(r"[0-9a-f]{64}", text_hash)
        or statistics.get("canonical_entity_id")
        != SZSE_CODE_CHANGE_ENTITY_ID
        or statistics.get("effective_at")
        != SZSE_CODE_CHANGE_EFFECTIVE_DATE
        or statistics.get("interval_count") != 2
        or len(source.records) != 2
        or statistics.get("intervals") != expected_intervals
        or any(
            record.attributes.get("event_protocol_version")
            != SZSE_CODE_CHANGE_PROTOCOL_VERSION
            or record.attributes.get("event_raw_pdf_sha256")
            != source.source_hash
            or record.attributes.get("event_text_sha256") != text_hash
            or record.attributes.get("event_text_recomputed_from_raw") is not True
            for record in source.records
        )
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change source metadata is inconsistent"
        )
    return {
        "protocol_version": SZSE_CODE_CHANGE_PROTOCOL_VERSION,
        "raw_pdf_sha256": source.source_hash,
        "text_sha256": text_hash,
        "interval_count": 2,
    }


def _bse_current_delisting_failure_metadata(
    *,
    error: str,
    manifest_sha256: str,
    tdx_snapshot_observed_at: datetime,
    validation_now: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    return {
        "verified": False,
        "status": "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE",
        "error": error,
        "protocol_version": "",
        "expected_protocol_version": BSE_CURRENT_DELISTING_PROTOCOL_VERSION,
        "manifest_sha256": manifest_sha256,
        "logical_content_sha256": "",
        "retrieved_at": "",
        "target_codes": sorted(BSE_CURRENT_DELISTING_CODES),
        "event_count": 0,
        "catalogue_page_count": 0,
        "catalogue_total_elements": 0,
        "catalogue_code_set_sha256": "",
        "tdx_snapshot_observed_at": tdx_snapshot_observed_at.isoformat(),
        "validation_now": validation_now.isoformat(),
        "as_of": as_of.isoformat(),
        "current_catalogue_is_reconciliation_only": True,
        "historical_effective_dates_from_notice_pdfs_only": True,
        "historical_listing_intervals_contributed": False,
        "trading_eligibility_contributed": False,
    }


def _replay_bse_current_delisting_artifact(
    *,
    manifest: str | BSECurrentDelistingManifestReference | None,
    store: BSECurrentDelistingManifestStore | None,
    tdx_snapshot_observed_at: datetime,
    validation_now: datetime | str | None,
    as_of: datetime | str | None,
    expected_manifest_sha256: str | None = None,
    expected_logical_content_sha256: str | None = None,
    prevalidated_observation: bool = False,
) -> tuple[BSECurrentDelistingArtifact | None, dict[str, Any]]:
    """Cold-replay one policy-bound current BSE observation window."""

    actual_wall_clock = _current_wall_clock()
    try:
        checked_now = (
            actual_wall_clock
            if validation_now is None
            else _aware_current_datetime(
                validation_now,
                field_name="bse_current_delisting_validation_now",
            )
        )
        checked_as_of = (
            tdx_snapshot_observed_at
            if as_of is None
            else _aware_current_datetime(
                as_of,
                field_name="bse_current_delisting_as_of",
            )
        )
    except HistoricalSecurityMasterBlockedError as exc:
        return None, _bse_current_delisting_failure_metadata(
            error=str(exc),
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=actual_wall_clock,
            as_of=tdx_snapshot_observed_at,
        )

    if (
        not prevalidated_observation
        and abs(checked_now - actual_wall_clock)
        > CURRENT_RECONCILIATION_CLOCK_SKEW
    ):
        return None, _bse_current_delisting_failure_metadata(
            error=(
                "BSE current-delisting validation clock is not the process wall "
                "clock; caller re-dating is forbidden"
            ),
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )
    if abs(checked_as_of - tdx_snapshot_observed_at) > CURRENT_RECONCILIATION_CLOCK_SKEW:
        return None, _bse_current_delisting_failure_metadata(
            error=(
                "BSE current-delisting as_of is not bound to the in-process TDX "
                "active snapshot observation"
            ),
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )

    if manifest is None and store is None:
        metadata = _bse_current_delisting_failure_metadata(
            error="BSE current-delisting manifest and CAS store were not supplied",
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )
        metadata.update(
            {
                "tdx_snapshot_observed_at": "",
                "validation_now": "",
                "as_of": "",
            }
        )
        return None, metadata
    if manifest is None:
        return None, _bse_current_delisting_failure_metadata(
            error="BSE current-delisting evidence requires the policy-bound manifest",
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )

    digest = (
        BSE_CURRENT_DELISTING_MANIFEST_SHA256
        if expected_manifest_sha256 is None
        else str(expected_manifest_sha256).strip().lower()
    )
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HistoricalSecurityMasterBlockedError(
                "BSE current-delisting expected manifest SHA-256 is invalid"
            )
        if isinstance(manifest, BSECurrentDelistingManifestReference):
            if manifest.manifest_sha256 != digest:
                raise HistoricalSecurityMasterBlockedError(
                    "BSE current-delisting manifest is not the policy-bound release"
                )
        elif isinstance(manifest, str):
            if manifest.strip().lower() != digest:
                raise HistoricalSecurityMasterBlockedError(
                    "BSE current-delisting manifest is not the policy-bound release"
                )
        else:
            raise HistoricalSecurityMasterBlockedError(
                "BSE current-delisting manifest reference has an unadmitted type"
            )

        if store is None:
            candidate_store = BSECurrentDelistingManifestStore(
                BSECurrentDelistingCAS(BSE_CURRENT_DELISTING_STORE_ROOT)
            )
        elif isinstance(store, BSECurrentDelistingManifestStore):
            candidate_store = store
        else:
            raise HistoricalSecurityMasterBlockedError(
                "BSE current-delisting store has an unadmitted type"
            )
        configured_root = Path(os.path.abspath(os.fspath(candidate_store.cas.root)))
        fixed_root = Path(os.path.abspath(os.fspath(BSE_CURRENT_DELISTING_STORE_ROOT)))
        if configured_root != fixed_root:
            raise HistoricalSecurityMasterBlockedError(
                "BSE current-delisting CAS store root is not policy-bound"
            )

        cold_store = BSECurrentDelistingManifestStore(
            BSECurrentDelistingCAS(fixed_root)
        )
        if isinstance(manifest, BSECurrentDelistingManifestReference):
            content = cold_store.cas.read_manifest(digest)
            expected_path = fixed_root / "manifests" / f"{digest}.json"
            if (
                manifest.byte_count != len(content)
                or manifest.cas_uri != f"sha256:{digest}"
                or not expected_path.is_file()
            ):
                raise HistoricalSecurityMasterBlockedError(
                    "BSE current-delisting manifest reference metadata is inconsistent"
                )
        artifact = cold_store.replay(digest)
        if not prevalidated_observation:
            validate_current_delisting_freshness(
                artifact,
                now=checked_now,
                as_of=checked_as_of,
            )
        contract = artifact.source_contract
        completeness = artifact.completeness
        event_codes = sorted(item.code_alias for item in artifact.events)
        expected_effective_dates = {
            spec.code_alias: spec.effective_date
            for spec in BSE_CURRENT_DELISTING_NOTICE_SPECS
        }
        catalogue_total = int(completeness.get("catalogue_total_elements") or 0)
        if (
            artifact.logical_content_sha256
            != (
                BSE_CURRENT_DELISTING_LOGICAL_SHA256
                if expected_logical_content_sha256 is None
                else str(expected_logical_content_sha256).strip().lower()
            )
            or contract.get("ready") is not True
            or contract.get("status") != BSE_CURRENT_DELISTING_SOURCE_ADMITTED
            or contract.get("scope")
            != "BSE_FIXED_CURRENT_DELISTING_RECONCILIATION"
            or contract.get("current_catalogue_is_reconciliation_only") is not True
            or contract.get("current_catalogue_contributes_historical_dates") is not False
            or contract.get("historical_effective_dates_come_only_from_notice_pdfs") is not True
            or contract.get("trading_eligibility") is not False
            or contract.get("audit_only") is not True
            or completeness.get("status")
            != BSE_CURRENT_DELISTING_EVIDENCE_COMPLETE
            or completeness.get("complete") is not True
            or completeness.get("notice_count") != 2
            or completeness.get("event_count") != 2
            or completeness.get("full_pagination_closed") is not True
            or completeness.get("page_zero_closure_probe_matches") is not True
            or event_codes != sorted(BSE_CURRENT_DELISTING_CODES)
            or any(
                item.event_type != "TERMINATED_LISTING"
                or item.exchange != "BSE"
                or item.effective_date != expected_effective_dates.get(item.code_alias)
                for item in artifact.events
            )
            or catalogue_total <= 0
        ):
            raise HistoricalSecurityMasterBlockedError(
                "BSE current-delisting replayed artifact is not admitted"
            )
    except (
        HistoricalSecurityMasterBlockedError,
        BSECurrentDelistingBlockedError,
        TypeError,
        ValueError,
        OSError,
    ) as exc:
        return None, _bse_current_delisting_failure_metadata(
            error=str(exc),
            manifest_sha256=digest,
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )

    return artifact, {
        "verified": True,
        "status": BSE_CURRENT_DELISTING_EVIDENCE_COMPLETE,
        "error": "",
        "protocol_version": BSE_CURRENT_DELISTING_PROTOCOL_VERSION,
        "expected_protocol_version": BSE_CURRENT_DELISTING_PROTOCOL_VERSION,
        "manifest_sha256": digest,
        "logical_content_sha256": artifact.logical_content_sha256,
        "retrieved_at": artifact.retrieved_at,
        "target_codes": event_codes,
        "event_count": len(artifact.events),
        "catalogue_page_count": len(artifact.catalogue_pages),
        "catalogue_total_elements": catalogue_total,
        "catalogue_code_set_sha256": artifact.current_catalogue_code_set_sha256,
        "tdx_snapshot_observed_at": tdx_snapshot_observed_at.isoformat(),
        "validation_now": checked_now.isoformat(),
        "as_of": checked_as_of.isoformat(),
        "current_catalogue_is_reconciliation_only": True,
        "historical_effective_dates_from_notice_pdfs_only": True,
        "historical_listing_intervals_contributed": True,
        "trading_eligibility_contributed": False,
    }


def _materialize_bse_current_delisting_source(
    *,
    artifact: BSECurrentDelistingArtifact,
    manifest_sha256: str,
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
) -> tuple[tuple[SecurityMasterRecord, ...], tuple[ParsedOfficialSource, ...]]:
    """Close current BSE aliases with notice-PDF dates on their existing entities."""

    existing_sources = [
        source for source in sources if source.name == BSE_CURRENT_DELISTING_SOURCE_NAME
    ]
    if len(existing_sources) > 1:
        raise HistoricalSecurityMasterBlockedError(
            "BSE current-delisting source is duplicated"
        )
    if not existing_sources:
        _validate_source_bundle(records, sources)
    mapping_sources = [source for source in sources if source.name == "bse_code_mapping"]
    if len(mapping_sources) != 1:
        raise HistoricalSecurityMasterBlockedError(
            "BSE current-delisting events require one verified code-mapping source"
        )
    mapping = mapping_sources[0]
    manifest_bytes = BSECurrentDelistingCAS(
        BSE_CURRENT_DELISTING_STORE_ROOT
    ).read_manifest(manifest_sha256)
    retrieved_at = artifact.retrieved_at
    replacements: dict[bytes, SecurityMasterRecord] = {}
    materialized: list[SecurityMasterRecord] = []
    for event in artifact.events:
        observations = [
            record
            for record in mapping.records
            if record.code_alias == event.code_alias
            and record.exchange == "BSE"
            and record.valid_to is None
        ]
        if len(observations) != 1:
            raise HistoricalSecurityMasterBlockedError(
                f"BSE mapping does not contain one open alias for {event.code_alias}"
            )
        observation = observations[0]
        if event.effective_date <= observation.valid_from:
            raise HistoricalSecurityMasterBlockedError(
                "BSE delisting effective date does not follow the alias interval"
            )
        notice = next(
            (item for item in artifact.notices if item.code_alias == event.code_alias),
            None,
        )
        if notice is None or notice.effective_date != event.effective_date:
            raise HistoricalSecurityMasterBlockedError(
                "BSE delisting event is not backed by its notice PDF"
            )
        attributes = {
            **dict(observation.attributes),
            "bse_current_delisting_protocol_version": (
                BSE_CURRENT_DELISTING_PROTOCOL_VERSION
            ),
            "bse_current_delisting_manifest_sha256": manifest_sha256,
            "bse_current_delisting_logical_content_sha256": (
                artifact.logical_content_sha256
            ),
            "delisting_notice_publication_date": notice.publication_date,
            "delisting_notice_announcement_number": notice.announcement_number,
            "delisting_notice_url": notice.source_url,
            "delisting_notice_pdf_sha256": notice.final_pdf.content_sha256,
            "delisting_effective_date": event.effective_date,
            "effective_date_source": "NOTICE_PDF_ONLY",
            "current_catalogue_is_reconciliation_only": True,
        }
        replacement = SecurityMasterRecord(
            canonical_entity_id=observation.canonical_entity_id,
            exchange="BSE",
            code_alias=event.code_alias,
            board="BSE",
            listed_at=observation.listed_at,
            delisted_at=event.effective_date,
            valid_from=observation.valid_from,
            valid_to=event.effective_date,
            event_type="TERMINATED_LISTING",
            source_url=f"urn:sha256:{manifest_sha256}",
            source_hash=manifest_sha256,
            retrieved_at=retrieved_at,
            name=observation.name or event.legal_name,
            attributes=attributes,
        )
        replacements[_canonical_json_bytes(observation.to_dict())] = replacement
        materialized.append(replacement)

    event_source = ParsedOfficialSource(
        name=BSE_CURRENT_DELISTING_SOURCE_NAME,
        source_url=f"urn:sha256:{manifest_sha256}",
        source_hash=manifest_sha256,
        retrieved_at=retrieved_at,
        raw_bytes=manifest_bytes,
        records=tuple(materialized),
        statistics={
            "protocol_version": BSE_CURRENT_DELISTING_PROTOCOL_VERSION,
            "status": BSE_CURRENT_DELISTING_EVIDENCE_COMPLETE,
            "manifest_sha256": manifest_sha256,
            "logical_content_sha256": artifact.logical_content_sha256,
            "event_count": len(materialized),
            "event_codes": sorted(record.code_alias for record in materialized),
            "interval_count": len(materialized),
            "current_catalogue_is_reconciliation_only": True,
            "historical_effective_dates_from_notice_pdfs_only": True,
        },
    )
    if existing_sources:
        if existing_sources[0] != event_source:
            raise HistoricalSecurityMasterBlockedError(
                "BSE current-delisting source conflicts with fixed-store replay"
            )
        master_fingerprints = set(_record_fingerprints(records))
        if not set(_record_fingerprints(event_source.records)).issubset(
            master_fingerprints
        ):
            raise HistoricalSecurityMasterBlockedError(
                "BSE current-delisting intervals are missing from the master"
            )
        if set(replacements).intersection(master_fingerprints):
            raise HistoricalSecurityMasterBlockedError(
                "closed BSE aliases remain duplicated as open mapping intervals"
            )
        validate_security_master_records(records)
        return tuple(records), tuple(sources)

    working_records: list[SecurityMasterRecord] = []
    replacement_hits: set[bytes] = set()
    for record in records:
        fingerprint = _canonical_json_bytes(record.to_dict())
        replacement = replacements.get(fingerprint)
        if replacement is None:
            working_records.append(record)
            continue
        if fingerprint in replacement_hits:
            raise HistoricalSecurityMasterBlockedError(
                "BSE open mapping alias is duplicated in the master"
            )
        replacement_hits.add(fingerprint)
        working_records.append(replacement)
    if replacement_hits != set(replacements):
        raise HistoricalSecurityMasterBlockedError(
            "BSE open mapping alias is absent from the master"
        )
    normalized_records = tuple(working_records)
    normalized_sources = (*tuple(sources), event_source)
    validate_security_master_records(normalized_records)
    return normalized_records, normalized_sources


def _replay_observation_bse_current_delisting_source(
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    *,
    batch: SecurityMasterObservationBatch,
) -> tuple[tuple[SecurityMasterRecord, ...], tuple[ParsedOfficialSource, ...]]:
    observed_at = _aware_current_datetime(
        batch.tdx_a_share.observed_at,
        field_name="current observation TDX observed_at",
    )
    artifact, metadata = _replay_bse_current_delisting_artifact(
        manifest=batch.bse_current_delisting.manifest_sha256,
        store=BSECurrentDelistingManifestStore(
            BSECurrentDelistingCAS(BSE_CURRENT_DELISTING_STORE_ROOT)
        ),
        tdx_snapshot_observed_at=observed_at,
        validation_now=batch.validated_at,
        as_of=batch.as_of,
        expected_manifest_sha256=(
            batch.bse_current_delisting.manifest_sha256
        ),
        expected_logical_content_sha256=(
            batch.bse_current_delisting.logical_content_sha256
        ),
        prevalidated_observation=True,
    )
    if artifact is None:
        raise HistoricalSecurityMasterBlockedError(
            "current observation BSE evidence failed cold replay: "
            f"{metadata['error']}"
        )
    return _materialize_bse_current_delisting_source(
        artifact=artifact,
        manifest_sha256=metadata["manifest_sha256"],
        records=records,
        sources=sources,
    )


def integrate_bse_current_delisting_manifest(
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    manifest: str | BSECurrentDelistingManifestReference,
    *,
    store: BSECurrentDelistingManifestStore | None = None,
    validation_now: datetime | str | None = None,
    as_of: datetime | str | None = None,
) -> tuple[tuple[SecurityMasterRecord, ...], tuple[ParsedOfficialSource, ...]]:
    """Replay the fixed BSE release and close its two open mapping intervals."""

    observed_at = _current_wall_clock()
    artifact, metadata = _replay_bse_current_delisting_artifact(
        manifest=manifest,
        store=store,
        tdx_snapshot_observed_at=observed_at,
        validation_now=validation_now,
        as_of=as_of,
    )
    if artifact is None:
        raise HistoricalSecurityMasterBlockedError(
            "BSE current-delisting fixed-store replay failed closed: "
            f"{metadata['error']}"
        )
    return _materialize_bse_current_delisting_source(
        artifact=artifact,
        manifest_sha256=metadata["manifest_sha256"],
        records=records,
        sources=sources,
    )


def _replay_sse_risk_warning_artifact(
    *,
    manifest: str | SSERiskWarningManifestReference | None,
    store: SSERiskWarningManifestStore | None,
) -> tuple[SSERiskWarningListArtifact | None, str]:
    """Replay both raw official lists; never admit caller-supplied derived rows."""

    if manifest is None and store is None:
        return None, ""
    if manifest is None or store is None:
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning evidence requires both a manifest and CAS store"
        )
    if not isinstance(store, SSERiskWarningManifestStore):
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning store has an unadmitted type"
        )
    if isinstance(manifest, SSERiskWarningManifestReference):
        digest = manifest.manifest_sha256
        try:
            content, path = store.cas.read_blob(digest)
        except SSERiskWarningSourceBlockedError as exc:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE risk-warning manifest failed CAS verification: {exc}"
            ) from exc
        if (
            manifest.byte_count != len(content)
            or manifest.cas_uri != f"sha256:{digest}"
            or Path(manifest.object_path).resolve() != path.resolve()
        ):
            raise HistoricalSecurityMasterBlockedError(
                "SSE risk-warning manifest reference metadata is inconsistent"
            )
    elif isinstance(manifest, str):
        digest = manifest.strip().lower()
    else:
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning manifest reference has an unadmitted type"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning manifest SHA-256 is invalid"
        )
    try:
        artifact = store.replay(digest)
    except SSERiskWarningSourceBlockedError as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"SSE risk-warning canonical manifest replay failed: {exc}"
        ) from exc
    contract = artifact.source_contract
    a_share_codes = sorted(
        item.code for item in artifact.securities if item.share_class == "A"
    )
    b_share_codes = sorted(
        item.code for item in artifact.securities if item.share_class == "B"
    )
    if (
        contract.get("ready") is not True
        or contract.get("status") != SSE_RISK_WARNING_SOURCE_ADMITTED
        or contract.get("scope") != "CURRENT_RISK_WARNING_SECURITIES"
        or contract.get("audit_only") is not True
        or contract.get("caller_attestation_allowed") is not False
        or contract.get("pagination_transition_policy")
        != "SOURCE_CONTRACT_UNADMITTED"
        or artifact.statistics.get("a_share_rows") != len(a_share_codes)
        or artifact.statistics.get("b_share_rows_excluded_from_a_share_set")
        != len(b_share_codes)
        or artifact.statistics.get("a_share_code_set_encoding")
        != "canonical-json-sorted-suffixed-codes-utf8"
        or artifact.statistics.get("a_share_code_set_sha256")
        != _sha256(_canonical_json_bytes(a_share_codes))
        or not a_share_codes
        or any(not code.endswith(".SH") for code in (*a_share_codes, *b_share_codes))
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning replayed artifact is not admitted for reconciliation"
        )
    return artifact, digest


def _replay_sse_risk_warning_active_intervals_artifact(
    *,
    manifest: (
        str | SSERiskWarningActiveIntervalsManifestReference | None
    ),
    store: SSERiskWarningActiveIntervalsManifestStore | None,
) -> tuple[SSERiskWarningActiveIntervalsArtifact | None, str]:
    """Cold-replay status-7 pages and both bound dependency manifests."""

    if manifest is None and store is None:
        return None, ""
    if manifest is None or store is None:
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning active intervals require both a manifest and CAS store"
        )
    if not isinstance(store, SSERiskWarningActiveIntervalsManifestStore):
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning active-interval store has an unadmitted type"
        )
    if isinstance(manifest, SSERiskWarningActiveIntervalsManifestReference):
        digest = manifest.manifest_sha256
        try:
            content, path = store.cas.read_blob(digest)
        except SSERiskWarningActiveIntervalsBlockedError as exc:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE risk-warning active-interval manifest failed CAS verification: {exc}"
            ) from exc
        if (
            manifest.byte_count != len(content)
            or manifest.cas_uri != f"sha256:{digest}"
            or Path(manifest.object_path).resolve() != path.resolve()
        ):
            raise HistoricalSecurityMasterBlockedError(
                "SSE risk-warning active-interval manifest reference is inconsistent"
            )
    elif isinstance(manifest, str):
        digest = manifest.strip().lower()
    else:
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning active-interval manifest has an unadmitted type"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning active-interval manifest SHA-256 is invalid"
        )
    try:
        artifact = store.replay(digest)
    except SSERiskWarningActiveIntervalsBlockedError as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"SSE risk-warning active-interval manifest replay failed: {exc}"
        ) from exc
    contract = artifact.source_contract
    codes = sorted(item.code_alias for item in artifact.intervals)
    transition_code = artifact.transition_code_alias
    transition_state = artifact.transition_binding_state
    expected_transition_lag_codes = (
        [transition_code]
        if transition_state == SSE_TRANSITION_BINDING_LAG
        else []
    )
    if (
        contract.get("status")
        != SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_ADMITTED
        or contract.get("source_name")
        != SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME
        or contract.get("historical_master_integration_allowed") is not True
        or contract.get("training_allowed") is not False
        or contract.get("trading_allowed") is not False
        or contract.get("risk_warning_state_marker") != "7|8"
        or contract.get("transition_lag_state_marker") != "7|4"
        or contract.get("state_marker_4_allowed_only_for_fixed_transition")
        is not True
        or artifact.statistics.get("interval_count") != len(codes)
        or artifact.statistics.get("code_set_sha256")
        != _sha256(_canonical_json_bytes(codes))
        or transition_state
        not in {SSE_TRANSITION_BINDING_LAG, SSE_TRANSITION_BINDING_CONVERGED}
        or contract.get("transition_binding_state") != transition_state
        or contract.get("transition_code_alias") != transition_code
        or contract.get("transition_new_name") != artifact.transition_new_name
        or contract.get("transition_effective_date")
        != artifact.transition_effective_date
        or artifact.statistics.get("transition_binding_state") != transition_state
        or artifact.statistics.get("transition_code_alias") != transition_code
        or artifact.statistics.get("transition_new_name")
        != artifact.transition_new_name
        or artifact.statistics.get("transition_effective_date")
        != artifact.transition_effective_date
        or list(artifact.transition_lag_codes) != expected_transition_lag_codes
        or (
            transition_state == SSE_TRANSITION_BINDING_LAG
            and transition_code not in codes
        )
        or (
            transition_state == SSE_TRANSITION_BINDING_CONVERGED
            and transition_code in codes
        )
        or not codes
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning active-interval artifact is not admitted"
        )
    return artifact, digest


def _materialize_sse_risk_warning_active_interval_source(
    *,
    artifact: SSERiskWarningActiveIntervalsArtifact,
    manifest_sha256: str,
    manifest_bytes: bytes,
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
) -> tuple[tuple[SecurityMasterRecord, ...], tuple[ParsedOfficialSource, ...]]:
    existing_sources = [
        source
        for source in sources
        if source.name == SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME
    ]
    if len(existing_sources) > 1:
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning active-interval source is duplicated"
        )
    existing_source_fingerprints = {
        _canonical_json_bytes(record.to_dict())
        for source in existing_sources
        for record in source.records
    }
    base_sources = tuple(
        source
        for source in sources
        if source.name != SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME
    )
    base_records = tuple(
        record
        for record in records
        if _canonical_json_bytes(record.to_dict())
        not in existing_source_fingerprints
    )
    base_source_by_name = _validate_source_bundle(base_records, base_sources)
    status_2_source = base_source_by_name.get("sse_active_a_shares")
    if status_2_source is not None:
        status_2_verified, _status_2_codes, status_2_unresolved = (
            _verified_active_aliases(
                source_by_name=base_source_by_name,
                name="sse_active_a_shares",
                source_url=SSE_ACTIVE_API_URL,
                exchange="SSE",
                parser=parse_sse_active_json,
            )
        )
        if not status_2_verified or status_2_unresolved:
            raise HistoricalSecurityMasterBlockedError(
                "SSE status-2 source is not admitted for status-7 deduplication"
            )
    base_fingerprints = set(_record_fingerprints(base_records))
    existing_by_code = {
        record.code_alias: record
        for record in (status_2_source.records if status_2_source is not None else ())
        if _canonical_json_bytes(record.to_dict()) in base_fingerprints
        and record.exchange == "SSE"
        and record.valid_to is None
        and record.delisted_at is None
        and record.event_type == "ACTIVE_LISTING"
    }
    materialized: list[SecurityMasterRecord] = []
    for interval in artifact.intervals:
        expected_entity = _canonical_entity_id("SSE", interval.code_alias[:6])
        if (
            interval.exchange != "SSE"
            or interval.event_type != "ACTIVE_LISTING"
            or interval.canonical_entity_id != expected_entity
            or interval.attributes.get("company_code") != interval.code_alias[:6]
            or interval.valid_from != interval.listed_at
            or interval.valid_to is not None
            or interval.delisted_at is not None
        ):
            raise HistoricalSecurityMasterBlockedError(
                "SSE risk-warning active interval identity changed"
            )
        existing = existing_by_code.get(interval.code_alias)
        if existing is not None:
            if (
                existing.canonical_entity_id != interval.canonical_entity_id
                or existing.exchange != interval.exchange
                or existing.code_alias != interval.code_alias
                or existing.listed_at != interval.listed_at
                or existing.valid_from != interval.valid_from
                or existing.valid_to is not None
                or existing.delisted_at is not None
                or existing.event_type != "ACTIVE_LISTING"
                or existing.board != interval.board
                or existing.name != interval.name
                or existing.attributes.get("company_code")
                != interval.attributes.get("company_code")
            ):
                raise HistoricalSecurityMasterBlockedError(
                    "SSE status-2/status-7 active interval identity conflicts"
                )
            continue
        attributes = {
            **dict(interval.attributes),
            "sse_risk_warning_active_intervals_protocol_version": (
                SSE_RISK_WARNING_ACTIVE_INTERVALS_PROTOCOL_VERSION
            ),
            "sse_risk_warning_active_intervals_manifest_sha256": manifest_sha256,
            "sse_risk_warning_active_intervals_logical_content_sha256": (
                artifact.logical_content_sha256
            ),
            "sse_risk_warning_active_intervals_source_snapshot_sha256": (
                artifact.source_snapshot_sha256
            ),
            "risk_warning_manifest_sha256": artifact.risk_warning_manifest_sha256,
            "transition_manifest_sha256": artifact.transition_manifest_sha256 or "",
            "transition_binding_state": artifact.transition_binding_state,
            "transition_code_alias": artifact.transition_code_alias,
            "transition_new_name": artifact.transition_new_name,
            "transition_effective_date": artifact.transition_effective_date,
        }
        materialized.append(
            SecurityMasterRecord(
                canonical_entity_id=interval.canonical_entity_id,
                exchange=interval.exchange,
                code_alias=interval.code_alias,
                board=interval.board,
                listed_at=interval.listed_at,
                delisted_at=None,
                valid_from=interval.valid_from,
                valid_to=None,
                event_type="ACTIVE_LISTING",
                source_url=f"urn:sha256:{manifest_sha256}",
                source_hash=manifest_sha256,
                retrieved_at=artifact.retrieved_at,
                name=interval.name,
                attributes=attributes,
            )
        )
    if _sha256(manifest_bytes) != manifest_sha256:
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning active-interval manifest bytes changed"
        )
    source = ParsedOfficialSource(
        name=SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME,
        source_url=f"urn:sha256:{manifest_sha256}",
        source_hash=manifest_sha256,
        retrieved_at=artifact.retrieved_at,
        raw_bytes=manifest_bytes,
        records=tuple(materialized),
        statistics={
            "protocol_version": SSE_RISK_WARNING_ACTIVE_INTERVALS_PROTOCOL_VERSION,
            "status": SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_ADMITTED,
            "manifest_sha256": manifest_sha256,
            "logical_content_sha256": artifact.logical_content_sha256,
            "source_snapshot_sha256": artifact.source_snapshot_sha256,
            "interval_count": len(materialized),
            "artifact_interval_count": len(artifact.intervals),
            "deduplicated_status_2_count": (
                len(artifact.intervals) - len(materialized)
            ),
            "risk_warning_interval_count": artifact.risk_warning_code_count,
            "state_marker_counts": dict(
                artifact.statistics.get("state_marker_counts") or {}
            ),
            "risk_warning_state_marker": artifact.source_contract[
                "risk_warning_state_marker"
            ],
            "transition_lag_state_marker": artifact.source_contract[
                "transition_lag_state_marker"
            ],
            "state_marker_4_allowed_only_for_fixed_transition": (
                artifact.source_contract[
                    "state_marker_4_allowed_only_for_fixed_transition"
                ]
            ),
            "risk_warning_manifest_sha256": artifact.risk_warning_manifest_sha256,
            "transition_manifest_sha256": artifact.transition_manifest_sha256 or "",
            "transition_binding_state": artifact.transition_binding_state,
            "transition_code_alias": artifact.transition_code_alias,
            "transition_new_name": artifact.transition_new_name,
            "transition_effective_date": artifact.transition_effective_date,
        },
    )
    if existing_sources:
        if existing_sources[0] != source:
            raise HistoricalSecurityMasterBlockedError(
                "SSE risk-warning active-interval source changed after replay"
            )
        if set(_record_fingerprints(records)) != set(
            _record_fingerprints(base_records + tuple(materialized))
        ):
            raise HistoricalSecurityMasterBlockedError(
                "SSE risk-warning active intervals changed in the master"
            )
        _validate_source_bundle(records, sources)
        return tuple(records), tuple(sources)
    combined_records = base_records + tuple(materialized)
    combined_sources = tuple(sources) + (source,)
    validate_security_master_records(combined_records)
    _validate_source_bundle(combined_records, combined_sources)
    return combined_records, combined_sources


def _replay_and_materialize_sse_risk_warning_active_intervals(
    *,
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    manifest: str | SSERiskWarningActiveIntervalsManifestReference | None,
    store: SSERiskWarningActiveIntervalsManifestStore | None,
) -> tuple[
    tuple[SecurityMasterRecord, ...],
    tuple[ParsedOfficialSource, ...],
    SSERiskWarningActiveIntervalsArtifact | None,
    str,
]:
    artifact, digest = _replay_sse_risk_warning_active_intervals_artifact(
        manifest=manifest,
        store=store,
    )
    if artifact is None:
        if any(
            source.name == SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME
            for source in sources
        ):
            raise HistoricalSecurityMasterBlockedError(
                "SSE risk-warning active intervals cannot be trusted without manifest replay"
            )
        return tuple(records), tuple(sources), None, ""
    assert store is not None
    try:
        manifest_bytes, _manifest_path = store.cas.read_blob(digest)
    except SSERiskWarningActiveIntervalsBlockedError as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"SSE risk-warning active-interval manifest failed stable reread: {exc}"
        ) from exc
    materialized_records, materialized_sources = (
        _materialize_sse_risk_warning_active_interval_source(
            artifact=artifact,
            manifest_sha256=digest,
            manifest_bytes=manifest_bytes,
            records=records,
            sources=sources,
        )
    )
    return materialized_records, materialized_sources, artifact, digest


def _current_wall_clock() -> datetime:
    return datetime.now().astimezone()


def _current_observation_policy() -> SecurityMasterObservationPolicy:
    return SecurityMasterObservationPolicy(
        pending_cas_root=PENDING_LISTING_STORE_ROOT,
        bse_current_delisting_cas_root=BSE_CURRENT_DELISTING_STORE_ROOT,
        minimum_tdx_code_count=CURRENT_OBSERVATION_MINIMUM_TDX_CODE_COUNT,
    )


def _current_observation_store(
    root: Path | None = None,
) -> SecurityMasterObservationStore:
    configured = CURRENT_OBSERVATION_STORE_ROOT if root is None else Path(root)
    return SecurityMasterObservationStore(
        configured,
        policy=_current_observation_policy(),
    )


def _normalize_current_observation_reference(
    manifest: str | ObservationManifestReference,
    *,
    store: SecurityMasterObservationStore | None,
    require_current: bool,
) -> tuple[SecurityMasterObservationBatch, dict[str, Any]]:
    """Cold-replay one observation manifest; caller summaries are never trusted."""

    if isinstance(manifest, ObservationManifestReference):
        digest = manifest.manifest_sha256.strip().lower()
    elif isinstance(manifest, str):
        digest = manifest.strip().lower()
    else:
        raise HistoricalSecurityMasterBlockedError(
            "current observation manifest reference has an unadmitted type"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HistoricalSecurityMasterBlockedError(
            "current observation manifest SHA-256 is invalid"
        )
    if store is None:
        candidate_store = _current_observation_store()
    elif isinstance(store, SecurityMasterObservationStore):
        candidate_store = store
    else:
        raise HistoricalSecurityMasterBlockedError(
            "current observation store has an unadmitted type"
        )
    configured_root = Path(os.path.abspath(os.fspath(candidate_store.root)))
    fixed_root = Path(
        os.path.abspath(os.fspath(CURRENT_OBSERVATION_STORE_ROOT))
    )
    if configured_root != fixed_root:
        raise HistoricalSecurityMasterBlockedError(
            "current observation CAS store root is not policy-bound"
        )
    cold_store = _current_observation_store(fixed_root)
    manifest_path = fixed_root / "manifests" / f"{digest}.json"
    if isinstance(manifest, ObservationManifestReference):
        expected_path = manifest_path.resolve()
        if (
            manifest.cas_uri != f"sha256:{digest}"
            or Path(manifest.object_path).resolve() != expected_path
            or not manifest_path.is_file()
            or manifest.byte_count != manifest_path.stat().st_size
        ):
            raise HistoricalSecurityMasterBlockedError(
                "current observation manifest reference metadata is inconsistent"
            )
    try:
        # Historical publication has a separate release-time gate.  Always
        # rebuild the immutable observation first, so audit replay never turns
        # stale merely because the live-trading 30-second TDX window elapsed.
        batch = cold_store.replay(digest)
        if require_current:
            _validate_current_observation_for_historical_publish(
                batch,
                now=_current_wall_clock(),
            )
    except (SecurityMasterObservationBlockedError, OSError) as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"current observation manifest failed cold replay: {exc}"
        ) from exc
    if batch.status != CURRENT_OBSERVATION_READY:
        raise HistoricalSecurityMasterBlockedError(
            "current observation manifest is not admitted"
        )
    tdx_codes = sorted(batch.tdx_a_share.codes)
    tdx_hash = _sha256(_canonical_json_bytes(tdx_codes))
    tdx_names = {
        code: str(batch.tdx_a_share.names[code])
        for code in tdx_codes
    }
    tdx_identity_hash = _sha256(_canonical_json_bytes(tdx_names))
    if (
        batch.tdx_a_share.code_count != len(tdx_codes)
        or batch.tdx_a_share.code_set_sha256 != tdx_hash
        or set(tdx_names) != set(tdx_codes)
        or any(not name.strip() for name in tdx_names.values())
        or batch.tdx_a_share.identity_sha256 != tdx_identity_hash
    ):
        raise HistoricalSecurityMasterBlockedError(
            "current observation TDX code/name identity set is inconsistent"
        )
    return batch, {
        "protocol_version": CURRENT_OBSERVATION_PROTOCOL_VERSION,
        "manifest_sha256": digest,
        "logical_content_sha256": batch.logical_content_sha256,
        "validated_at": batch.validated_at,
        "as_of": batch.as_of,
        "tdx_observed_at": batch.tdx_a_share.observed_at,
        "tdx_code_count": batch.tdx_a_share.code_count,
        "tdx_code_set_sha256": tdx_hash,
        "tdx_names": tdx_names,
        "tdx_identity_sha256": tdx_identity_hash,
        "pending_listing_manifest_sha256": (
            batch.pending_listing.manifest_sha256
        ),
        "pending_listing_logical_content_sha256": (
            batch.pending_listing.logical_content_sha256
        ),
        "bse_current_delisting_manifest_sha256": (
            batch.bse_current_delisting.manifest_sha256
        ),
        "bse_current_delisting_logical_content_sha256": (
            batch.bse_current_delisting.logical_content_sha256
        ),
        "freshness_required_at_publish": require_current,
        "immutable_replay_after_publish": True,
    }


def _aware_current_datetime(value: datetime | str, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HistoricalSecurityMasterBlockedError(
                f"{field_name} is not ISO-8601"
            ) from exc
    else:
        raise HistoricalSecurityMasterBlockedError(
            f"{field_name} must be an aware datetime or ISO-8601 string"
        )
    if parsed.tzinfo is None:
        raise HistoricalSecurityMasterBlockedError(
            f"{field_name} must include a timezone"
        )
    return parsed


def _validate_current_observation_for_historical_publish(
    batch: SecurityMasterObservationBatch,
    *,
    now: datetime | str,
) -> None:
    """Require a recently validated immutable observation for publication.

    The observation's own cold replay proves that the TDX sample was at most
    30 seconds old and that all official evidence fit the five-minute capture
    window *at validation time*.  Historical publication gets a distinct
    five-minute window after ``validated_at``; later ``load_gate`` calls use
    immutable replay only and intentionally do not call this function.
    """

    checked_now = _aware_current_datetime(
        now,
        field_name="historical publication wall clock",
    )
    validated_at = _aware_current_datetime(
        batch.validated_at,
        field_name="current observation validated_at",
    )
    if validated_at > checked_now + CURRENT_RECONCILIATION_CLOCK_SKEW:
        raise HistoricalSecurityMasterBlockedError(
            "current observation validation is future-dated relative to publication"
        )
    if checked_now - validated_at > CURRENT_OBSERVATION_HISTORICAL_PUBLISH_MAX_AGE:
        raise HistoricalSecurityMasterBlockedError(
            "current observation is outside the five-minute historical publication window"
        )


def _pending_listing_failure_metadata(
    *,
    error: str,
    manifest_sha256: str,
    tdx_snapshot_observed_at: datetime,
    validation_now: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    return {
        "verified": False,
        "status": "PENDING_LISTING_STATUS_INCOMPLETE",
        "error": error,
        "protocol_version": "",
        "expected_protocol_version": PENDING_LISTING_PROTOCOL_VERSION,
        "manifest_sha256": manifest_sha256,
        "logical_content_sha256": "",
        "raw_hashes": {},
        "source_count": 0,
        "official_code_count": 0,
        "code_set_sha256": "",
        "retrieved_at": "",
        "earliest_source_retrieved_at": "",
        "latest_source_retrieved_at": "",
        "tdx_snapshot_observed_at": tdx_snapshot_observed_at.isoformat(),
        "validation_now": validation_now.isoformat(),
        "as_of": as_of.isoformat(),
        "current_reconciliation_only": True,
        "historical_listing_intervals_contributed": False,
        "trading_eligibility_contributed": False,
    }


def _replay_pending_listing_artifact(
    *,
    manifest: str | PendingListingManifestReference | None,
    store: PendingListingManifestStore | None,
    tdx_snapshot_observed_at: datetime,
    validation_now: datetime | str | None,
    as_of: datetime | str | None,
    expected_manifest_sha256: str | None = None,
    expected_logical_content_sha256: str | None = None,
    prevalidated_observation: bool = False,
) -> tuple[PendingListingArtifact | None, dict[str, Any]]:
    """Cold-replay the fixed twelve-source release; never trust derived input."""

    actual_wall_clock = _current_wall_clock()
    try:
        checked_now = (
            actual_wall_clock
            if validation_now is None
            else _aware_current_datetime(
                validation_now, field_name="pending_listing_validation_now"
            )
        )
        checked_as_of = (
            tdx_snapshot_observed_at
            if as_of is None
            else _aware_current_datetime(as_of, field_name="pending_listing_as_of")
        )
    except HistoricalSecurityMasterBlockedError as exc:
        fallback_as_of = tdx_snapshot_observed_at
        return None, _pending_listing_failure_metadata(
            error=str(exc),
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=actual_wall_clock,
            as_of=fallback_as_of,
        )

    if (
        not prevalidated_observation
        and abs(checked_now - actual_wall_clock)
        > CURRENT_RECONCILIATION_CLOCK_SKEW
    ):
        return None, _pending_listing_failure_metadata(
            error=(
                "pending-listing validation clock is not the process wall clock; "
                "caller re-dating is forbidden"
            ),
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )
    if abs(checked_as_of - tdx_snapshot_observed_at) > CURRENT_RECONCILIATION_CLOCK_SKEW:
        return None, _pending_listing_failure_metadata(
            error=(
                "pending-listing as_of is not bound to the in-process TDX active "
                "snapshot observation"
            ),
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )

    if manifest is None and store is None:
        metadata = _pending_listing_failure_metadata(
            error="pending-listing manifest and CAS store were not supplied",
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )
        metadata.update(
            {
                "tdx_snapshot_observed_at": "",
                "validation_now": "",
                "as_of": "",
            }
        )
        return None, metadata
    if manifest is None:
        return None, _pending_listing_failure_metadata(
            error="pending-listing evidence requires the policy-bound manifest",
            manifest_sha256="",
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )

    digest = (
        PENDING_LISTING_MANIFEST_SHA256
        if expected_manifest_sha256 is None
        else str(expected_manifest_sha256).strip().lower()
    )
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HistoricalSecurityMasterBlockedError(
                "pending-listing expected manifest SHA-256 is invalid"
            )
        if manifest is None:
            pass
        elif isinstance(manifest, PendingListingManifestReference):
            if manifest.manifest_sha256 != digest:
                raise HistoricalSecurityMasterBlockedError(
                    "pending-listing manifest is not the policy-bound release"
                )
        elif isinstance(manifest, str):
            if manifest.strip().lower() != digest:
                raise HistoricalSecurityMasterBlockedError(
                    "pending-listing manifest is not the policy-bound release"
                )
        else:
            raise HistoricalSecurityMasterBlockedError(
                "pending-listing manifest reference has an unadmitted type"
            )

        if store is None:
            candidate_store = PendingListingManifestStore(
                PendingListingRawCAS(PENDING_LISTING_STORE_ROOT)
            )
        elif isinstance(store, PendingListingManifestStore):
            candidate_store = store
        else:
            raise HistoricalSecurityMasterBlockedError(
                "pending-listing store has an unadmitted type"
            )
        configured_root = Path(os.path.abspath(os.fspath(candidate_store.cas.root)))
        fixed_root = Path(os.path.abspath(os.fspath(PENDING_LISTING_STORE_ROOT)))
        if configured_root != fixed_root:
            raise HistoricalSecurityMasterBlockedError(
                "pending-listing CAS store root is not policy-bound"
            )

        # Deliberately discard the supplied store object and cold-replay through a
        # fresh CAS/store graph rooted at the policy path.
        cold_store = PendingListingManifestStore(PendingListingRawCAS(fixed_root))
        if isinstance(manifest, PendingListingManifestReference):
            content, object_path = cold_store.cas.read_blob(digest)
            expected_path = fixed_root / "sha256" / digest[:2] / digest
            reference_path = Path(os.path.abspath(manifest.object_path))
            if (
                manifest.byte_count != len(content)
                or manifest.cas_uri != f"sha256:{digest}"
                or reference_path != expected_path
                or object_path != expected_path
            ):
                raise HistoricalSecurityMasterBlockedError(
                    "pending-listing manifest reference metadata is inconsistent"
                )
        artifact = cold_store.replay(digest)
        if not prevalidated_observation:
            validate_pending_listing_freshness(
                artifact,
                now=checked_now,
                as_of=checked_as_of,
            )
        contract = artifact.source_contract
        codes = sorted(item.code for item in artifact.securities)
        raw_hashes = {
            item.source_id: item.content_sha256 for item in artifact.raw_sources
        }
        source_times = sorted(
            datetime.fromisoformat(item.retrieved_at)
            for item in artifact.raw_sources
        )
        if (
            artifact.to_dict().get("protocol_version")
            != PENDING_LISTING_PROTOCOL_VERSION
            or artifact.logical_content_sha256
            != (
                PENDING_LISTING_LOGICAL_SHA256
                if expected_logical_content_sha256 is None
                else str(expected_logical_content_sha256).strip().lower()
            )
            or contract.get("ready") is not True
            or contract.get("status") != PENDING_LISTING_SOURCE_ADMITTED
            or contract.get("scope")
            != "CURRENT_ASSIGNED_IPO_CODES_NOT_YET_LISTED"
            or contract.get("method") != "GET"
            or contract.get("redirects_allowed") is not False
            or contract.get("historical_interval_evidence") is not False
            or contract.get("trading_eligibility") is not False
            or contract.get("audit_only") is not True
            or set(codes) != set(PENDING_LISTING_RECONCILIATION_CODES)
            or len(codes) != 6
            or tuple(item.source_id for item in artifact.raw_sources)
            != tuple(PENDING_LISTING_SOURCE_ORDER)
            or len(raw_hashes) != 12
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in raw_hashes.values())
            or artifact.statistics.get("status")
            != PENDING_LISTING_EVIDENCE_COMPLETE
            or artifact.statistics.get("target_count") != 6
            or artifact.statistics.get("raw_source_count") != 12
            or artifact.statistics.get("codes") != codes
            or artifact.statistics.get("code_set_sha256")
            != _sha256(_canonical_json_bytes(codes))
        ):
            raise HistoricalSecurityMasterBlockedError(
                "pending-listing replayed artifact is not admitted for reconciliation"
            )
    except (
        HistoricalSecurityMasterBlockedError,
        PendingListingSourceBlockedError,
        TypeError,
        ValueError,
        OSError,
    ) as exc:
        return None, _pending_listing_failure_metadata(
            error=str(exc),
            manifest_sha256=digest,
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=checked_now,
            as_of=checked_as_of,
        )

    return artifact, {
        "verified": True,
        "status": PENDING_LISTING_EVIDENCE_COMPLETE,
        "error": "",
        "protocol_version": PENDING_LISTING_PROTOCOL_VERSION,
        "expected_protocol_version": PENDING_LISTING_PROTOCOL_VERSION,
        "manifest_sha256": digest,
        "logical_content_sha256": artifact.logical_content_sha256,
        "raw_hashes": dict(sorted(raw_hashes.items())),
        "source_count": len(raw_hashes),
        "official_code_count": len(codes),
        "code_set_sha256": _sha256(_canonical_json_bytes(codes)),
        "retrieved_at": artifact.retrieved_at,
        "earliest_source_retrieved_at": source_times[0].isoformat(),
        "latest_source_retrieved_at": source_times[-1].isoformat(),
        "tdx_snapshot_observed_at": tdx_snapshot_observed_at.isoformat(),
        "validation_now": checked_now.isoformat(),
        "as_of": checked_as_of.isoformat(),
        "current_reconciliation_only": True,
        "historical_listing_intervals_contributed": False,
        "trading_eligibility_contributed": False,
    }


def build_quality_report(
    records: Sequence[SecurityMasterRecord],
    sources: Sequence[ParsedOfficialSource],
    tdx_active_codes: Iterable[str],
    *,
    expected_sse_szse_overlap: int | None = EXPECTED_SSE_SZSE_OVERLAP,
    sse_active_interval_history_complete: bool = False,
    szse_active_interval_history_complete: bool = False,
    bse_event_history_complete: bool = False,
    szse_code_change_artifacts: Sequence[SZSECodeChangeArtifact] = (),
    sse_risk_warning_manifest: str | SSERiskWarningManifestReference | None = None,
    sse_risk_warning_store: SSERiskWarningManifestStore | None = None,
    pending_listing_manifest: (
        str | PendingListingManifestReference | None
    ) = None,
    pending_listing_store: PendingListingManifestStore | None = None,
    pending_listing_validation_now: datetime | str | None = None,
    pending_listing_as_of: datetime | str | None = None,
    bse_current_delisting_manifest: (
        str | BSECurrentDelistingManifestReference | None
    ) = None,
    bse_current_delisting_store: BSECurrentDelistingManifestStore | None = None,
    bse_current_delisting_validation_now: datetime | str | None = None,
    bse_current_delisting_as_of: datetime | str | None = None,
    current_observation_manifest: (
        str | ObservationManifestReference | None
    ) = None,
    current_observation_store: SecurityMasterObservationStore | None = None,
    require_current_observation: bool = False,
    sse_risk_warning_active_intervals_manifest: (
        str | SSERiskWarningActiveIntervalsManifestReference | None
    ) = None,
    sse_risk_warning_active_intervals_store: (
        SSERiskWarningActiveIntervalsManifestStore | None
    ) = None,
) -> dict[str, Any]:
    records, sources, szse_resolved_aliases = _integrate_szse_code_change_artifacts(
        records,
        sources,
        szse_code_change_artifacts,
    )
    active_codes = _normalize_active_codes(tdx_active_codes)
    current_observation_batch: SecurityMasterObservationBatch | None = None
    current_observation_metadata: dict[str, Any] | None = None
    if current_observation_manifest is not None:
        current_observation_batch, current_observation_metadata = (
            _normalize_current_observation_reference(
                current_observation_manifest,
                store=current_observation_store,
                require_current=True,
            )
        )
        if current_observation_metadata["tdx_code_set_sha256"] != _sha256(
            _canonical_json_bytes(sorted(active_codes))
        ):
            raise HistoricalSecurityMasterBlockedError(
                "current observation TDX code set does not match the master build"
            )
        pending_listing_manifest = (
            current_observation_batch.pending_listing.manifest_sha256
        )
        pending_listing_store = PendingListingManifestStore(
            PendingListingRawCAS(PENDING_LISTING_STORE_ROOT)
        )
        pending_listing_validation_now = current_observation_batch.validated_at
        pending_listing_as_of = current_observation_batch.as_of
        bse_current_delisting_manifest = (
            current_observation_batch.bse_current_delisting.manifest_sha256
        )
        bse_current_delisting_store = BSECurrentDelistingManifestStore(
            BSECurrentDelistingCAS(BSE_CURRENT_DELISTING_STORE_ROOT)
        )
        bse_current_delisting_validation_now = (
            current_observation_batch.validated_at
        )
        bse_current_delisting_as_of = current_observation_batch.as_of
        tdx_snapshot_observed_at = _aware_current_datetime(
            current_observation_batch.tdx_a_share.observed_at,
            field_name="current observation TDX observed_at",
        )
    else:
        tdx_snapshot_observed_at = _current_wall_clock()
    records, sources, active_interval_artifact, active_interval_manifest_sha256 = (
        _replay_and_materialize_sse_risk_warning_active_intervals(
            records=records,
            sources=sources,
            manifest=sse_risk_warning_active_intervals_manifest,
            store=sse_risk_warning_active_intervals_store,
        )
    )
    active_interval_source_verified = active_interval_artifact is not None
    if active_interval_artifact is not None:
        assert sse_risk_warning_active_intervals_store is not None
        if sse_risk_warning_manifest is None:
            sse_risk_warning_manifest = (
                active_interval_artifact.risk_warning_manifest_sha256
            )
            sse_risk_warning_store = (
                sse_risk_warning_active_intervals_store.risk_warning_store
            )
        else:
            requested_risk_digest = (
                sse_risk_warning_manifest.manifest_sha256
                if isinstance(
                    sse_risk_warning_manifest,
                    SSERiskWarningManifestReference,
                )
                else str(sse_risk_warning_manifest).strip().lower()
            )
            if (
                requested_risk_digest
                != active_interval_artifact.risk_warning_manifest_sha256
            ):
                raise HistoricalSecurityMasterBlockedError(
                    "risk-warning list and status-7 interval manifests are not bound"
                )
    bse_current_artifact, bse_current_metadata = (
        _replay_bse_current_delisting_artifact(
            manifest=bse_current_delisting_manifest,
            store=bse_current_delisting_store,
            tdx_snapshot_observed_at=tdx_snapshot_observed_at,
            validation_now=bse_current_delisting_validation_now,
            as_of=bse_current_delisting_as_of,
            expected_manifest_sha256=(
                current_observation_metadata[
                    "bse_current_delisting_manifest_sha256"
                ]
                if current_observation_metadata is not None
                else None
            ),
            expected_logical_content_sha256=(
                current_observation_metadata[
                    "bse_current_delisting_logical_content_sha256"
                ]
                if current_observation_metadata is not None
                else None
            ),
            prevalidated_observation=(current_observation_metadata is not None),
        )
    )
    if bse_current_artifact is not None:
        records, sources = _materialize_bse_current_delisting_source(
            artifact=bse_current_artifact,
            manifest_sha256=bse_current_metadata["manifest_sha256"],
            records=records,
            sources=sources,
        )
    elif any(source.name == BSE_CURRENT_DELISTING_SOURCE_NAME for source in sources):
        raise HistoricalSecurityMasterBlockedError(
            "BSE current-delisting intervals cannot be trusted without fixed-store replay"
        )
    validate_security_master_records(records)
    source_by_name = _validate_source_bundle(records, sources)
    sse_active_source_verified, sse_official_active, sse_unresolved_aliases = (
        _verified_active_aliases(
            source_by_name=source_by_name,
            name="sse_active_a_shares",
            source_url=SSE_ACTIVE_API_URL,
            exchange="SSE",
            parser=parse_sse_active_json,
        )
    )
    szse_active_source_verified, szse_official_active, szse_unresolved_aliases = (
        _verified_active_aliases(
            source_by_name=source_by_name,
            name="szse_active_a_shares",
            source_url=SZSE_ACTIVE_XLSX_URL,
            exchange="SZSE",
            parser=parse_szse_active_xlsx,
            allow_unresolved_alias_observations=True,
        )
    )
    if sse_unresolved_aliases:
        raise HistoricalSecurityMasterBlockedError(
            "SSE active source unexpectedly contains unresolved aliases"
        )
    sse_active_complete = sse_active_source_verified
    discovered_szse_unresolved = set(szse_unresolved_aliases)
    resolved_szse_aliases = set(szse_resolved_aliases)
    unexpected_resolutions = resolved_szse_aliases - discovered_szse_unresolved
    if unexpected_resolutions:
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change event resolves an alias absent from the active catalogue"
        )
    remaining_szse_unresolved = tuple(
        sorted(discovered_szse_unresolved - resolved_szse_aliases)
    )
    szse_code_change_source_verified = bool(resolved_szse_aliases)
    szse_code_change_metadata = _verified_szse_code_change_metadata(
        source_by_name,
        admitted=szse_code_change_source_verified,
    )
    szse_alias_history_complete = (
        szse_active_source_verified and not remaining_szse_unresolved
    )
    szse_active_complete = (
        szse_active_source_verified and szse_alias_history_complete
    )
    # Boolean caller assertions are intentionally non-authoritative.  Active
    # completeness is established only by reparsing exact official bytes, and
    # BSE event completeness only by replaying the policy-bound V2 CAS release.
    _ = (sse_active_interval_history_complete, szse_active_interval_history_complete)
    _ = bse_event_history_complete
    bse_event_metadata = _verified_bse_termination_event_metadata(
        records=records,
        sources=sources,
        source_by_name=source_by_name,
    )
    bse_events_complete = bool(bse_event_metadata["verified"])
    risk_artifact, risk_manifest_sha256 = _replay_sse_risk_warning_artifact(
        manifest=sse_risk_warning_manifest,
        store=sse_risk_warning_store,
    )
    risk_source_verified = risk_artifact is not None
    risk_a_codes = (
        {
            item.code
            for item in risk_artifact.securities
            if item.share_class == "A"
        }
        if risk_artifact is not None
        else set()
    )
    risk_b_codes = (
        {
            item.code
            for item in risk_artifact.securities
            if item.share_class == "B"
        }
        if risk_artifact is not None
        else set()
    )
    risk_main_a_codes = (
        {
            item.code
            for item in risk_artifact.securities
            if item.market_segment == "MAIN_BOARD" and item.share_class == "A"
        }
        if risk_artifact is not None
        else set()
    )
    risk_main_b_codes = (
        {
            item.code
            for item in risk_artifact.securities
            if item.market_segment == "MAIN_BOARD" and item.share_class == "B"
        }
        if risk_artifact is not None
        else set()
    )
    risk_star_a_codes = (
        {
            item.code
            for item in risk_artifact.securities
            if item.market_segment == "STAR_MARKET" and item.share_class == "A"
        }
        if risk_artifact is not None
        else set()
    )
    risk_star_b_codes = (
        {
            item.code
            for item in risk_artifact.securities
            if item.market_segment == "STAR_MARKET" and item.share_class == "B"
        }
        if risk_artifact is not None
        else set()
    )
    risk_raw_hashes = (
        {
            item.source_id: item.content_sha256
            for item in risk_artifact.raw_responses
        }
        if risk_artifact is not None
        else {}
    )
    risk_logical_sha256 = (
        risk_artifact.logical_content_sha256 if risk_artifact is not None else ""
    )
    risk_code_set_sha256 = (
        str(risk_artifact.statistics["a_share_code_set_sha256"])
        if risk_artifact is not None
        else ""
    )
    status_7_source = source_by_name.get(
        SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME
    )
    status_7_interval_codes = {
        record.code_alias
        for record in (status_7_source.records if status_7_source is not None else ())
        if record.exchange == "SSE"
        and record.delisted_at is None
        and record.valid_to is None
        and record.event_type == "ACTIVE_LISTING"
    }
    transition_binding_state = (
        active_interval_artifact.transition_binding_state
        if active_interval_artifact is not None
        else ""
    )
    transition_code_alias = (
        active_interval_artifact.transition_code_alias
        if active_interval_artifact is not None
        else ""
    )
    transition_expected_codes = (
        {transition_code_alias} if transition_code_alias else set()
    )
    transition_active_codes = (
        set(active_interval_artifact.transition_lag_codes)
        if active_interval_artifact is not None
        else set()
    )
    transfer_source = source_by_name.get(BSE_TERMINATION_EVENT_SOURCE_NAME)
    admitted_open_transfer_codes = {
        record.code_alias
        for record in (transfer_source.records if transfer_source is not None else ())
        if record.exchange == "SSE"
        and record.event_type == "TRANSFER_IN"
        and record.delisted_at is None
        and record.valid_to is None
    }
    admitted_open_sse_codes = (
        set(sse_official_active)
        | status_7_interval_codes
        | admitted_open_transfer_codes
    )
    risk_listing_interval_covered = sorted(
        risk_a_codes & admitted_open_sse_codes
    )
    risk_listing_interval_missing = sorted(
        risk_a_codes - admitted_open_sse_codes
    )
    # The normal SSE active catalogue omits current risk-warning securities.
    # Current-code reconciliation alone does not establish their historical
    # listing intervals, so fail closed until an official interval source is
    # materialized into the security master.
    pending_artifact, pending_metadata = _replay_pending_listing_artifact(
        manifest=pending_listing_manifest,
        store=pending_listing_store,
        tdx_snapshot_observed_at=tdx_snapshot_observed_at,
        validation_now=pending_listing_validation_now,
        as_of=pending_listing_as_of,
        expected_manifest_sha256=(
            current_observation_metadata[
                "pending_listing_manifest_sha256"
            ]
            if current_observation_metadata is not None
            else None
        ),
        expected_logical_content_sha256=(
            current_observation_metadata[
                "pending_listing_logical_content_sha256"
            ]
            if current_observation_metadata is not None
            else None
        ),
        prevalidated_observation=(current_observation_metadata is not None),
    )
    pending_source_verified = bool(pending_metadata["verified"])
    admitted_pending_listing_codes = (
        {item.code for item in pending_artifact.securities}
        if pending_artifact is not None
        else set()
    )
    pending_sse_codes = {
        code for code in admitted_pending_listing_codes if code.endswith(".SH")
    }
    pending_szse_codes = {
        code for code in admitted_pending_listing_codes if code.endswith(".SZ")
    }
    terminated_overlap = sorted(
        {
            record.code_alias
            for record in records
            if record.exchange in {"SSE", "SZSE"}
            and record.event_type in {"TERMINATED_LISTING", "TRANSFER_IN"}
            and record.listed_at <= HISTORICAL_END
            and record.delisted_at is not None
            and record.delisted_at >= HISTORICAL_START
        }
    )
    expected = len(terminated_overlap) if expected_sse_szse_overlap is None else int(
        expected_sse_szse_overlap
    )
    sse_sz_complete = len(terminated_overlap) == expected
    active_overlap = sorted(set(terminated_overlap) & active_codes)
    tdx_sse_active = {code for code in active_codes if code.endswith(".SH")}
    tdx_szse_active = {code for code in active_codes if code.endswith(".SZ")}
    sse_missing_from_tdx = sorted(sse_official_active - tdx_sse_active)
    sse_extra_in_tdx = sorted(tdx_sse_active - sse_official_active)
    risk_duplicate_normal_active = sorted(risk_a_codes & sse_official_active)
    risk_missing_from_tdx = sorted(risk_a_codes - tdx_sse_active)
    risk_explained_sse_extra = sorted(
        risk_a_codes & set(sse_extra_in_tdx)
    )
    sse_unexplained_after_risk = sorted(
        set(sse_extra_in_tdx) - risk_a_codes
    )
    szse_missing_from_tdx = sorted(szse_official_active - tdx_szse_active)
    szse_extra_in_tdx = sorted(tdx_szse_active - szse_official_active)
    pending_duplicate_sse_normal_active = sorted(
        pending_sse_codes & sse_official_active
    )
    pending_duplicate_sse_risk_warning = sorted(pending_sse_codes & risk_a_codes)
    pending_duplicate_szse_normal_active = sorted(
        pending_szse_codes & szse_official_active
    )
    pending_missing_from_tdx = sorted(
        admitted_pending_listing_codes - active_codes
    )
    transition_duplicate_risk_warning = sorted(
        transition_expected_codes & risk_a_codes
    )
    transition_duplicate_pending = sorted(
        transition_expected_codes & pending_sse_codes
    )
    transition_missing_from_tdx = sorted(
        transition_expected_codes - tdx_sse_active
    )
    transition_as_of_date = (
        current_observation_batch.as_of[:10]
        if current_observation_batch is not None
        else (
            active_interval_artifact.retrieved_at[:10]
            if active_interval_artifact is not None
            else HISTORICAL_END
        )
    )
    transition_terminated_conflicts = sorted(
        transition_expected_codes
        & {
            record.code_alias
            for record in records
            if record.exchange == "SSE"
            and record.delisted_at is not None
            and record.delisted_at <= transition_as_of_date
        }
    )
    transition_name_mismatches: list[dict[str, str]] = []
    transition_official_name_mismatches: list[dict[str, str]] = []
    transition_state_conflicts: list[str] = []
    if active_interval_artifact is not None:
        if transition_binding_state == SSE_TRANSITION_BINDING_LAG:
            if (
                transition_expected_codes != transition_active_codes
                or not transition_expected_codes <= status_7_interval_codes
                or transition_expected_codes & sse_official_active
            ):
                transition_state_conflicts.append(
                    "LAG_IN_STATUS7 does not match the official status-7/normal catalogues"
                )
        elif transition_binding_state == SSE_TRANSITION_BINDING_CONVERGED:
            if (
                transition_active_codes
                or transition_expected_codes & status_7_interval_codes
                or not transition_expected_codes <= sse_official_active
            ):
                transition_state_conflicts.append(
                    "CONVERGED_OUT_OF_STATUS7 is not proven by the official normal catalogue"
                )
            status_2_source = source_by_name.get("sse_active_a_shares")
            status_2_names = {
                record.code_alias: record.name
                for record in (
                    status_2_source.records if status_2_source is not None else ()
                )
                if record.exchange == "SSE"
                and record.event_type == "ACTIVE_LISTING"
                and record.valid_to is None
                and record.delisted_at is None
            }
            for code in sorted(transition_expected_codes):
                expected_name = active_interval_artifact.transition_new_name
                observed_name = str(status_2_names.get(code) or "")
                if not expected_name or observed_name != expected_name:
                    transition_official_name_mismatches.append(
                        {
                            "code": code,
                            "expected_name": expected_name,
                            "observed_name": observed_name,
                        }
                    )
        else:
            transition_state_conflicts.append(
                "transition binding state is not admitted"
            )
    if current_observation_metadata is not None and active_interval_artifact is not None:
        tdx_names = dict(current_observation_metadata["tdx_names"])
        interval_names = {
            interval.code_alias: interval.name
            for interval in active_interval_artifact.intervals
        }
        for code in sorted(transition_expected_codes):
            expected_name = (
                active_interval_artifact.transition_new_name
                if code == transition_code_alias
                else interval_names.get(code, "")
            )
            observed_name = str(tdx_names.get(code) or "")
            if not expected_name or observed_name != expected_name:
                transition_name_mismatches.append(
                    {
                        "code": code,
                        "expected_name": expected_name,
                        "observed_name": observed_name,
                    }
                )
    sse_unexplained_before_pending = sorted(
        set(sse_unexplained_after_risk)
        - transition_expected_codes
        - admitted_open_transfer_codes
    )
    pending_explained_sse_extra = sorted(
        pending_sse_codes & set(sse_unexplained_before_pending)
    )
    pending_explained_szse_extra = sorted(
        pending_szse_codes & set(szse_extra_in_tdx)
    )
    sse_unexplained_after_pending = sorted(
        set(sse_unexplained_before_pending) - pending_sse_codes
    )
    szse_unexplained_after_pending = sorted(
        set(szse_extra_in_tdx) - pending_szse_codes
    )
    sse_expected_after_risk = (
        sse_official_active
        | risk_a_codes
        | transition_expected_codes
        | admitted_open_transfer_codes
    )
    sse_expected_current = sse_expected_after_risk | pending_sse_codes
    sse_expected_non_pending_missing_from_tdx = sorted(
        sse_expected_after_risk - tdx_sse_active
    )
    required_non_pending_sse_codes = set(sse_expected_after_risk)
    required_non_pending_sse_interval_missing = sorted(
        required_non_pending_sse_codes - admitted_open_sse_codes
    )
    admitted_open_sse_unexpected = sorted(
        admitted_open_sse_codes - required_non_pending_sse_codes
    )
    sse_active_listing_intervals_complete = (
        sse_active_source_verified
        and active_interval_source_verified
        and not required_non_pending_sse_interval_missing
        and not admitted_open_sse_unexpected
    )
    szse_expected_current = szse_official_active | pending_szse_codes
    base_current_reconciliation_failed = bool(
        sse_missing_from_tdx
        or risk_duplicate_normal_active
        or risk_missing_from_tdx
        or transition_duplicate_risk_warning
        or transition_duplicate_pending
        or transition_missing_from_tdx
        or transition_terminated_conflicts
        or transition_name_mismatches
        or transition_official_name_mismatches
        or transition_state_conflicts
        or sse_expected_non_pending_missing_from_tdx
        or szse_missing_from_tdx
    )
    active_reconciliation_failed = (
        sse_active_complete
        and szse_active_complete
        and bool(
            base_current_reconciliation_failed
            or (
                pending_source_verified
                and (
                    pending_duplicate_sse_normal_active
                    or pending_duplicate_sse_risk_warning
                    or pending_duplicate_szse_normal_active
                    or pending_missing_from_tdx
                    or sse_unexplained_after_pending
                    or szse_unexplained_after_pending
                    or sse_expected_current != tdx_sse_active
                    or szse_expected_current != tdx_szse_active
                )
            )
        )
    )
    pending_listing_status_incomplete = (
        sse_active_complete
        and szse_active_complete
        and risk_source_verified
        and not base_current_reconciliation_failed
        and not pending_source_verified
    )
    pending_listing_difference_requires_source = bool(
        sse_unexplained_before_pending or szse_extra_in_tdx
    )
    bse_current_aliases = sorted(
        {
            record.code_alias
            for record in records
            if record.exchange == "BSE" and record.valid_to is None
        }
    )
    bse_active_missing = sorted(set(bse_current_aliases) - active_codes)
    bse_current_delisted_codes = (
        set(bse_current_metadata["target_codes"])
        if bse_current_metadata["verified"]
        else set()
    )
    bse_delisted_still_active = sorted(bse_current_delisted_codes & active_codes)
    source_counts = {
        source.name: dict(source.statistics)
        for source in sorted(sources, key=lambda item: item.name)
    }
    if risk_artifact is not None:
        source_counts["sse_current_risk_warning"] = {
            "protocol_version": SSE_RISK_WARNING_PROTOCOL_VERSION,
            "manifest_sha256": risk_manifest_sha256,
            "logical_content_sha256": risk_logical_sha256,
            "a_share_code_set_sha256": risk_code_set_sha256,
            "raw_hashes": dict(sorted(risk_raw_hashes.items())),
            "main_board_rows": len(risk_main_a_codes) + len(risk_main_b_codes),
            "main_board_a_share_rows": len(risk_main_a_codes),
            "main_board_b_share_rows": len(risk_main_b_codes),
            "star_market_rows": len(risk_star_a_codes) + len(risk_star_b_codes),
            "star_market_a_share_rows": len(risk_star_a_codes),
            "star_market_b_share_rows": len(risk_star_b_codes),
            "a_share_rows": len(risk_a_codes),
            "b_share_rows_excluded": len(risk_b_codes),
            "current_reconciliation_only": True,
            "historical_listing_intervals_contributed": False,
        }
    if pending_artifact is not None:
        source_counts["pending_listing_current_official"] = {
            "protocol_version": pending_metadata["protocol_version"],
            "manifest_sha256": pending_metadata["manifest_sha256"],
            "logical_content_sha256": pending_metadata[
                "logical_content_sha256"
            ],
            "code_set_sha256": pending_metadata["code_set_sha256"],
            "raw_hashes": dict(pending_metadata["raw_hashes"]),
            "raw_source_count": pending_metadata["source_count"],
            "official_code_count": pending_metadata["official_code_count"],
            "retrieved_at": pending_metadata["retrieved_at"],
            "earliest_source_retrieved_at": pending_metadata[
                "earliest_source_retrieved_at"
            ],
            "latest_source_retrieved_at": pending_metadata[
                "latest_source_retrieved_at"
            ],
            "current_reconciliation_only": True,
            "historical_listing_intervals_contributed": False,
            "trading_eligibility_contributed": False,
        }
    if bse_current_artifact is not None:
        source_counts[BSE_CURRENT_DELISTING_SOURCE_NAME] = {
            "protocol_version": bse_current_metadata["protocol_version"],
            "manifest_sha256": bse_current_metadata["manifest_sha256"],
            "logical_content_sha256": bse_current_metadata[
                "logical_content_sha256"
            ],
            "event_count": bse_current_metadata["event_count"],
            "event_codes": list(bse_current_metadata["target_codes"]),
            "catalogue_page_count": bse_current_metadata[
                "catalogue_page_count"
            ],
            "catalogue_total_elements": bse_current_metadata[
                "catalogue_total_elements"
            ],
            "catalogue_code_set_sha256": bse_current_metadata[
                "catalogue_code_set_sha256"
            ],
            "current_catalogue_is_reconciliation_only": True,
            "historical_effective_dates_from_notice_pdfs_only": True,
        }
    source_completeness = {
        "sse_szse_terminated_listing_events": sse_sz_complete,
        "sse_active_listing_intervals": sse_active_listing_intervals_complete,
        "szse_active_listing_intervals": szse_active_complete,
        "sse_active_listing_source_verified": sse_active_source_verified,
        "szse_active_listing_source_verified": szse_active_source_verified,
        "szse_code_alias_history_complete": szse_alias_history_complete,
        "szse_code_change_event_source_verified": (
            szse_code_change_source_verified
        ),
        "szse_code_change_event_protocol_version": (
            szse_code_change_metadata["protocol_version"]
        ),
        "szse_code_change_event_raw_pdf_sha256": (
            szse_code_change_metadata["raw_pdf_sha256"]
        ),
        "szse_code_change_event_text_sha256": (
            szse_code_change_metadata["text_sha256"]
        ),
        "szse_code_change_event_interval_count": (
            szse_code_change_metadata["interval_count"]
        ),
        "bse_termination_and_transfer_events": bse_events_complete,
        "bse_termination_event_protocol_version": bse_event_metadata[
            "protocol_version"
        ],
        "bse_termination_event_manifest_sha256": bse_event_metadata[
            "manifest_sha256"
        ],
        "bse_termination_event_logical_content_sha256": bse_event_metadata[
            "logical_content_sha256"
        ],
        "bse_termination_event_transfer_count": bse_event_metadata[
            "transfer_count"
        ],
        "bse_termination_event_interval_count": bse_event_metadata[
            "interval_count"
        ],
        "bse_current_delisting_source_verified": bse_current_metadata["verified"],
        "bse_current_delisting_status": bse_current_metadata["status"],
        "bse_current_delisting_error": bse_current_metadata["error"],
        "bse_current_delisting_protocol_version": bse_current_metadata[
            "protocol_version"
        ],
        "bse_current_delisting_expected_protocol_version": (
            bse_current_metadata["expected_protocol_version"]
        ),
        "bse_current_delisting_manifest_sha256": bse_current_metadata[
            "manifest_sha256"
        ],
        "bse_current_delisting_logical_content_sha256": bse_current_metadata[
            "logical_content_sha256"
        ],
        "bse_current_delisting_event_count": bse_current_metadata[
            "event_count"
        ],
        "bse_current_delisting_target_codes": list(
            bse_current_metadata["target_codes"]
        ),
        "bse_current_delisting_catalogue_page_count": bse_current_metadata[
            "catalogue_page_count"
        ],
        "bse_current_delisting_catalogue_total_elements": bse_current_metadata[
            "catalogue_total_elements"
        ],
        "bse_current_delisting_catalogue_code_set_sha256": bse_current_metadata[
            "catalogue_code_set_sha256"
        ],
        "bse_current_delisting_retrieved_at": bse_current_metadata[
            "retrieved_at"
        ],
        "bse_current_delisting_validation_now": bse_current_metadata[
            "validation_now"
        ],
        "bse_current_delisting_as_of": bse_current_metadata["as_of"],
        "bse_current_delisting_current_catalogue_is_reconciliation_only": True,
        "bse_current_delisting_historical_effective_dates_from_notice_pdfs_only": True,
        "bse_current_delisting_contributes_historical_intervals": bool(
            bse_current_metadata["historical_listing_intervals_contributed"]
        ),
        "bse_current_delisting_contributes_trading_eligibility": False,
        "sse_current_risk_warning_source_verified": risk_source_verified,
        "sse_current_risk_warning_protocol_version": (
            SSE_RISK_WARNING_PROTOCOL_VERSION if risk_source_verified else ""
        ),
        "sse_current_risk_warning_manifest_sha256": risk_manifest_sha256,
        "sse_current_risk_warning_logical_content_sha256": risk_logical_sha256,
        "sse_current_risk_warning_a_share_code_set_sha256": (
            risk_code_set_sha256
        ),
        "sse_current_risk_warning_raw_hashes": dict(
            sorted(risk_raw_hashes.items())
        ),
        "sse_current_risk_warning_main_board_rows": (
            len(risk_main_a_codes) + len(risk_main_b_codes)
        ),
        "sse_current_risk_warning_main_board_a_share_rows": len(
            risk_main_a_codes
        ),
        "sse_current_risk_warning_main_board_b_share_rows": len(
            risk_main_b_codes
        ),
        "sse_current_risk_warning_star_market_rows": (
            len(risk_star_a_codes) + len(risk_star_b_codes)
        ),
        "sse_current_risk_warning_star_market_a_share_rows": len(
            risk_star_a_codes
        ),
        "sse_current_risk_warning_star_market_b_share_rows": len(
            risk_star_b_codes
        ),
        "sse_current_risk_warning_a_share_count": len(risk_a_codes),
        "sse_current_risk_warning_b_share_excluded_count": len(risk_b_codes),
        "sse_current_risk_warning_listing_interval_covered_count": len(
            risk_listing_interval_covered
        ),
        "sse_current_risk_warning_listing_interval_missing_count": len(
            risk_listing_interval_missing
        ),
        "sse_current_risk_warning_listing_interval_missing_sample": (
            risk_listing_interval_missing[:20]
        ),
        "sse_current_risk_warning_listing_interval_missing_code_set_sha256": (
            _sha256(_canonical_json_bytes(risk_listing_interval_missing))
        ),
        "sse_current_risk_warning_is_current_reconciliation_only": True,
        "sse_current_risk_warning_contributes_historical_intervals": False,
        "sse_risk_warning_active_intervals_source_verified": (
            active_interval_source_verified
        ),
        "sse_risk_warning_active_intervals_protocol_version": (
            SSE_RISK_WARNING_ACTIVE_INTERVALS_PROTOCOL_VERSION
            if active_interval_source_verified
            else ""
        ),
        "sse_risk_warning_active_intervals_manifest_sha256": (
            active_interval_manifest_sha256
        ),
        "sse_risk_warning_active_intervals_logical_content_sha256": (
            active_interval_artifact.logical_content_sha256
            if active_interval_artifact is not None
            else ""
        ),
        "sse_risk_warning_active_intervals_source_snapshot_sha256": (
            active_interval_artifact.source_snapshot_sha256
            if active_interval_artifact is not None
            else ""
        ),
        "sse_risk_warning_active_intervals_interval_count": (
            len(active_interval_artifact.intervals)
            if active_interval_artifact is not None
            else 0
        ),
        "sse_risk_warning_active_intervals_materialized_count": len(
            status_7_interval_codes
        ),
        "sse_risk_warning_active_intervals_transition_binding_state": (
            transition_binding_state
        ),
        "sse_risk_warning_active_intervals_transition_code_alias": (
            transition_code_alias
        ),
        "sse_risk_warning_active_intervals_transition_new_name": (
            active_interval_artifact.transition_new_name
            if active_interval_artifact is not None
            else ""
        ),
        "sse_risk_warning_active_intervals_transition_effective_date": (
            active_interval_artifact.transition_effective_date
            if active_interval_artifact is not None
            else ""
        ),
        "sse_risk_warning_active_intervals_transition_lag_codes": sorted(
            transition_active_codes
        ),
        "sse_transition_state_conflict_count": len(transition_state_conflicts),
        "sse_transition_state_conflict_sample": transition_state_conflicts[:20],
        "sse_required_non_pending_interval_missing_count": len(
            required_non_pending_sse_interval_missing
        ),
        "sse_required_non_pending_interval_missing_sample": (
            required_non_pending_sse_interval_missing[:20]
        ),
        "sse_required_non_pending_interval_missing_code_set_sha256": (
            _sha256(
                _canonical_json_bytes(
                    required_non_pending_sse_interval_missing
                )
            )
        ),
        "sse_admitted_open_interval_unexpected_count": len(
            admitted_open_sse_unexpected
        ),
        "sse_admitted_open_interval_unexpected_sample": (
            admitted_open_sse_unexpected[:20]
        ),
        "sse_transition_name_mismatch_count": len(transition_name_mismatches),
        "sse_transition_name_mismatch_sample": transition_name_mismatches[:20],
        "sse_transition_official_name_mismatch_count": len(
            transition_official_name_mismatches
        ),
        "sse_transition_official_name_mismatch_sample": (
            transition_official_name_mismatches[:20]
        ),
        "pending_listing_status_source_verified": pending_source_verified,
        "pending_listing_status": pending_metadata["status"],
        "pending_listing_error": pending_metadata["error"],
        "pending_listing_protocol_version": pending_metadata[
            "protocol_version"
        ],
        "pending_listing_expected_protocol_version": pending_metadata[
            "expected_protocol_version"
        ],
        "pending_listing_manifest_sha256": pending_metadata[
            "manifest_sha256"
        ],
        "pending_listing_logical_content_sha256": pending_metadata[
            "logical_content_sha256"
        ],
        "pending_listing_raw_hashes": dict(pending_metadata["raw_hashes"]),
        "pending_listing_raw_source_count": pending_metadata["source_count"],
        "pending_listing_official_code_count": pending_metadata[
            "official_code_count"
        ],
        "pending_listing_admitted_code_set_sha256": _sha256(
            _canonical_json_bytes(sorted(admitted_pending_listing_codes))
        ),
        "pending_listing_retrieved_at": pending_metadata["retrieved_at"],
        "pending_listing_earliest_source_retrieved_at": pending_metadata[
            "earliest_source_retrieved_at"
        ],
        "pending_listing_latest_source_retrieved_at": pending_metadata[
            "latest_source_retrieved_at"
        ],
        "pending_listing_validation_now": pending_metadata["validation_now"],
        "pending_listing_as_of": pending_metadata["as_of"],
        "pending_listing_is_current_reconciliation_only": True,
        "pending_listing_contributes_historical_intervals": False,
        "pending_listing_contributes_trading_eligibility": False,
        # The current TDX code list is useful reconciliation evidence, but cannot
        # establish historical listed_at/delisted_at intervals by itself.
        "tdx_active_snapshot_is_reconciliation_only": True,
        "tdx_active_snapshot_observed_at": pending_metadata[
            "tdx_snapshot_observed_at"
        ],
        "tdx_active_snapshot_code_set_sha256": _sha256(
            _canonical_json_bytes(sorted(active_codes))
        ),
        "tdx_active_snapshot_caller_retrieved_at_accepted": False,
        "current_observation_source_verified": (
            current_observation_metadata is not None
        ),
        "current_observation_protocol_version": (
            current_observation_metadata["protocol_version"]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_manifest_sha256": (
            current_observation_metadata["manifest_sha256"]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_logical_content_sha256": (
            current_observation_metadata["logical_content_sha256"]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_validated_at": (
            current_observation_metadata["validated_at"]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_as_of": (
            current_observation_metadata["as_of"]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_tdx_observed_at": (
            current_observation_metadata["tdx_observed_at"]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_tdx_code_count": (
            current_observation_metadata["tdx_code_count"]
            if current_observation_metadata is not None
            else 0
        ),
        "current_observation_tdx_code_set_sha256": (
            current_observation_metadata["tdx_code_set_sha256"]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_tdx_identity_sha256": (
            current_observation_metadata["tdx_identity_sha256"]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_pending_listing_manifest_sha256": (
            current_observation_metadata[
                "pending_listing_manifest_sha256"
            ]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_pending_listing_logical_content_sha256": (
            current_observation_metadata[
                "pending_listing_logical_content_sha256"
            ]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_bse_current_delisting_manifest_sha256": (
            current_observation_metadata[
                "bse_current_delisting_manifest_sha256"
            ]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_bse_current_delisting_logical_content_sha256": (
            current_observation_metadata[
                "bse_current_delisting_logical_content_sha256"
            ]
            if current_observation_metadata is not None
            else ""
        ),
        "current_observation_freshness_required_at_publish": (
            current_observation_metadata is not None
        ),
        "current_observation_immutable_replay_after_publish": True,
    }
    if not sse_sz_complete:
        status = "SOURCE_COVERAGE_FAILED"
        detail = (
            f"SSE/SZSE official overlap coverage is {len(terminated_overlap)}/{expected}"
        )
    elif not (sse_active_source_verified and szse_active_source_verified):
        status = "ACTIVE_INTERVAL_SOURCE_INCOMPLETE"
        detail = (
            "SSE/SZSE terminated listings are covered, but official historical "
            "interval sources for securities that remain active are incomplete"
        )
    elif remaining_szse_unresolved:
        status = "SZSE_CODE_ALIAS_HISTORY_INCOMPLETE"
        detail = (
            "The SZSE active catalogue contains 302-series aliases whose prior "
            "code, common entity, and effective switch date require a separate "
            "official code-change event source"
        )
    elif (
        pending_listing_status_incomplete
        and pending_listing_difference_requires_source
    ):
        status = "PENDING_LISTING_STATUS_INCOMPLETE"
        detail = (
            "Current TDX SH/SZ extras require the fixed twelve-source "
            "pending-listing release, but verification failed closed: "
            f"{pending_metadata['error']}"
        )
    elif active_reconciliation_failed:
        status = "ACTIVE_RECONCILIATION_FAILED"
        detail = (
            "Official SSE/SZSE current A-share evidence does not exactly "
            "reconcile to the TDX current SH/SZ snapshot"
        )
    elif not bse_events_complete:
        status = "SOURCE_INCOMPLETE"
        detail = (
            "SSE/SZSE terminated listings are covered, but the fixed-store BSE "
            "termination/transfer event ledger is absent or incomplete"
        )
    elif not bse_current_metadata["verified"]:
        status = "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
        detail = (
            "The fixed BSE current-delisting observation did not pass cold "
            "replay, freshness, and TDX observation-time binding: "
            f"{bse_current_metadata['error']}"
        )
    elif bse_delisted_still_active:
        status = "ACTIVE_RECONCILIATION_FAILED"
        detail = (
            "BSE aliases closed by official delisting notices remain present in "
            "the TDX active snapshot"
        )
    elif bse_active_missing:
        status = "ACTIVE_RECONCILIATION_FAILED"
        detail = "BSE active aliases do not reconcile to the TDX active snapshot"
    elif not risk_source_verified:
        status = "SSE_RISK_WARNING_SOURCE_INCOMPLETE"
        detail = (
            "No canonical replay of both official SSE current risk-warning lists "
            "is available"
        )
    elif pending_listing_status_incomplete:
        status = "PENDING_LISTING_STATUS_INCOMPLETE"
        detail = (
            "The fixed twelve-source pending-listing release did not pass cold "
            f"replay, freshness, and TDX observation-time binding: "
            f"{pending_metadata['error']}"
        )
    elif require_current_observation and current_observation_metadata is None:
        status = "CURRENT_OBSERVATION_REQUIRED"
        detail = (
            "A fresh, immutable current-observation manifest is required "
            "for publication"
        )
    elif not (sse_active_listing_intervals_complete and szse_active_complete):
        status = "ACTIVE_INTERVAL_SOURCE_INCOMPLETE"
        detail = "Official active listing interval evidence is incomplete"
    else:
        status = "READY"
        detail = "Official listing intervals and the TDX active snapshot reconcile"
    ready = status == "READY"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "quality_policy_version": QUALITY_POLICY_VERSION,
        "gate": {
            "ready": ready,
            "status": status,
            "detail": detail,
            "snapshot_id": "",
            "manifest_hash": "",
            "protocol_version": PROTOCOL_VERSION,
            "coverage_start": HISTORICAL_START,
            "coverage_end": HISTORICAL_END,
            "source_counts": source_counts,
            "source_completeness": source_completeness,
            "reconciliation": {
                "official_sse_szse_overlap": len(terminated_overlap),
                "required_sse_szse_overlap": expected,
                "historical_master_covered": len(terminated_overlap),
                "historical_master_missing": max(0, expected - len(terminated_overlap)),
                "tdx_active_snapshot_count": len(active_codes),
                "tdx_active_overlap_count": len(active_overlap),
                "tdx_active_overlap_sample": active_overlap[:20],
                "bse_current_alias_count": len(bse_current_aliases),
                "bse_current_alias_covered": len(bse_current_aliases) - len(bse_active_missing),
                "bse_current_alias_missing": len(bse_active_missing),
                "bse_current_alias_missing_sample": bse_active_missing[:20],
                "bse_delisted_still_active_count": len(
                    bse_delisted_still_active
                ),
                "bse_delisted_still_active_sample": bse_delisted_still_active[:20],
                "sse_official_active_count": len(sse_official_active),
                "sse_tdx_active_count": len(tdx_sse_active),
                "sse_missing_from_tdx_count": len(sse_missing_from_tdx),
                "sse_missing_from_tdx_sample": sse_missing_from_tdx[:20],
                "sse_extra_in_tdx_count": len(sse_extra_in_tdx),
                "sse_extra_in_tdx_sample": sse_extra_in_tdx[:20],
                "sse_current_risk_warning_source_verified": risk_source_verified,
                "sse_current_risk_warning_protocol_version": (
                    SSE_RISK_WARNING_PROTOCOL_VERSION
                    if risk_source_verified
                    else ""
                ),
                "sse_current_risk_warning_manifest_sha256": (
                    risk_manifest_sha256
                ),
                "sse_current_risk_warning_logical_content_sha256": (
                    risk_logical_sha256
                ),
                "sse_current_risk_warning_a_share_code_set_sha256": (
                    risk_code_set_sha256
                ),
                "sse_current_risk_warning_raw_hashes": dict(
                    sorted(risk_raw_hashes.items())
                ),
                "sse_current_risk_warning_main_board_rows": (
                    len(risk_main_a_codes) + len(risk_main_b_codes)
                ),
                "sse_current_risk_warning_main_board_a_share_rows": len(
                    risk_main_a_codes
                ),
                "sse_current_risk_warning_main_board_b_share_rows": len(
                    risk_main_b_codes
                ),
                "sse_current_risk_warning_star_market_rows": (
                    len(risk_star_a_codes) + len(risk_star_b_codes)
                ),
                "sse_current_risk_warning_star_market_a_share_rows": len(
                    risk_star_a_codes
                ),
                "sse_current_risk_warning_star_market_b_share_rows": len(
                    risk_star_b_codes
                ),
                "sse_current_risk_warning_a_share_count": len(risk_a_codes),
                "sse_current_risk_warning_b_share_excluded_count": len(
                    risk_b_codes
                ),
                "sse_current_risk_warning_listing_interval_covered_count": len(
                    risk_listing_interval_covered
                ),
                "sse_current_risk_warning_listing_interval_missing_count": len(
                    risk_listing_interval_missing
                ),
                "sse_current_risk_warning_listing_interval_missing_sample": (
                    risk_listing_interval_missing[:20]
                ),
                "sse_current_risk_warning_listing_interval_missing_code_set_sha256": (
                    _sha256(
                        _canonical_json_bytes(risk_listing_interval_missing)
                    )
                ),
                "sse_current_risk_warning_duplicate_normal_active_count": len(
                    risk_duplicate_normal_active
                ),
                "sse_current_risk_warning_duplicate_normal_active_sample": (
                    risk_duplicate_normal_active[:20]
                ),
                "sse_current_risk_warning_missing_from_tdx_count": len(
                    risk_missing_from_tdx
                ),
                "sse_current_risk_warning_missing_from_tdx_sample": (
                    risk_missing_from_tdx[:20]
                ),
                "sse_current_risk_warning_explained_extra_count": len(
                    risk_explained_sse_extra
                ),
                "sse_current_risk_warning_explained_extra_code_set_sha256": (
                    _sha256(_canonical_json_bytes(risk_explained_sse_extra))
                ),
                "sse_unexplained_after_risk_warning_count": len(
                    sse_unexplained_after_risk
                ),
                "sse_unexplained_after_risk_warning_sample": (
                    sse_unexplained_after_risk[:20]
                ),
                "sse_unexplained_after_risk_warning_code_set_sha256": (
                    _sha256(_canonical_json_bytes(sse_unexplained_after_risk))
                ),
                "sse_unexplained_after_pending_listing_count": len(
                    sse_unexplained_after_pending
                ),
                "sse_unexplained_after_pending_listing_sample": (
                    sse_unexplained_after_pending[:20]
                ),
                "sse_unexplained_after_pending_listing_code_set_sha256": (
                    _sha256(
                        _canonical_json_bytes(sse_unexplained_after_pending)
                    )
                ),
                "sse_expected_current_after_risk_warning_count": len(
                    sse_expected_after_risk
                ),
                "sse_expected_current_after_pending_listing_count": len(
                    sse_expected_current
                ),
                "sse_expected_current_code_set_sha256": _sha256(
                    _canonical_json_bytes(sorted(sse_expected_current))
                ),
                "sse_tdx_current_code_set_sha256": _sha256(
                    _canonical_json_bytes(sorted(tdx_sse_active))
                ),
                "sse_current_set_equality_holds": (
                    sse_expected_current == tdx_sse_active
                ),
                "sse_risk_warning_is_current_reconciliation_only": True,
                "sse_risk_warning_contributes_historical_intervals": False,
                "szse_official_active_count": len(szse_official_active),
                "szse_tdx_active_count": len(tdx_szse_active),
                "szse_missing_from_tdx_count": len(szse_missing_from_tdx),
                "szse_missing_from_tdx_sample": szse_missing_from_tdx[:20],
                "szse_extra_in_tdx_count": len(szse_extra_in_tdx),
                "szse_extra_in_tdx_sample": szse_extra_in_tdx[:20],
                "szse_unexplained_after_pending_listing_count": len(
                    szse_unexplained_after_pending
                ),
                "szse_unexplained_after_pending_listing_sample": (
                    szse_unexplained_after_pending[:20]
                ),
                "szse_unexplained_after_pending_listing_code_set_sha256": (
                    _sha256(
                        _canonical_json_bytes(szse_unexplained_after_pending)
                    )
                ),
                "szse_expected_current_after_pending_listing_count": len(
                    szse_expected_current
                ),
                "szse_expected_current_code_set_sha256": _sha256(
                    _canonical_json_bytes(sorted(szse_expected_current))
                ),
                "szse_tdx_current_code_set_sha256": _sha256(
                    _canonical_json_bytes(sorted(tdx_szse_active))
                ),
                "szse_current_set_equality_holds": (
                    szse_expected_current == tdx_szse_active
                ),
                "pending_listing_status_source_verified": (
                    pending_source_verified
                ),
                "pending_listing_status": pending_metadata["status"],
                "pending_listing_error": pending_metadata["error"],
                "pending_listing_protocol_version": pending_metadata[
                    "protocol_version"
                ],
                "pending_listing_expected_protocol_version": pending_metadata[
                    "expected_protocol_version"
                ],
                "pending_listing_manifest_sha256": pending_metadata[
                    "manifest_sha256"
                ],
                "pending_listing_logical_content_sha256": pending_metadata[
                    "logical_content_sha256"
                ],
                "pending_listing_raw_hashes": dict(
                    pending_metadata["raw_hashes"]
                ),
                "pending_listing_raw_source_count": pending_metadata[
                    "source_count"
                ],
                "pending_listing_official_code_count": pending_metadata[
                    "official_code_count"
                ],
                "pending_listing_admitted_code_set_sha256": _sha256(
                    _canonical_json_bytes(sorted(admitted_pending_listing_codes))
                ),
                "pending_listing_retrieved_at": pending_metadata[
                    "retrieved_at"
                ],
                "pending_listing_earliest_source_retrieved_at": (
                    pending_metadata["earliest_source_retrieved_at"]
                ),
                "pending_listing_latest_source_retrieved_at": (
                    pending_metadata["latest_source_retrieved_at"]
                ),
                "pending_listing_validation_now": pending_metadata[
                    "validation_now"
                ],
                "pending_listing_as_of": pending_metadata["as_of"],
                "pending_listing_explained_sse_count": len(
                    pending_explained_sse_extra
                ),
                "pending_listing_explained_sse_code_set_sha256": _sha256(
                    _canonical_json_bytes(pending_explained_sse_extra)
                ),
                "pending_listing_explained_szse_count": len(
                    pending_explained_szse_extra
                ),
                "pending_listing_explained_szse_code_set_sha256": _sha256(
                    _canonical_json_bytes(pending_explained_szse_extra)
                ),
                "pending_listing_missing_from_tdx_count": len(
                    pending_missing_from_tdx
                ),
                "pending_listing_missing_from_tdx_sample": (
                    pending_missing_from_tdx[:20]
                ),
                "pending_listing_duplicate_sse_normal_active_count": len(
                    pending_duplicate_sse_normal_active
                ),
                "pending_listing_duplicate_sse_risk_warning_count": len(
                    pending_duplicate_sse_risk_warning
                ),
                "pending_listing_duplicate_szse_normal_active_count": len(
                    pending_duplicate_szse_normal_active
                ),
                "pending_listing_is_current_reconciliation_only": True,
                "pending_listing_contributes_historical_intervals": False,
                "pending_listing_contributes_trading_eligibility": False,
                "tdx_active_snapshot_observed_at": pending_metadata[
                    "tdx_snapshot_observed_at"
                ],
                "tdx_active_snapshot_code_set_sha256": _sha256(
                    _canonical_json_bytes(sorted(active_codes))
                ),
                "tdx_active_snapshot_caller_retrieved_at_accepted": False,
                "active_reconciliation_status": (
                    "SOURCE_INCOMPLETE"
                    if not (sse_active_complete and szse_active_complete)
                    else (
                        "ACTIVE_RECONCILIATION_FAILED"
                        if active_reconciliation_failed
                        or bse_delisted_still_active
                        or (bse_events_complete and bse_active_missing)
                        else (
                            "SOURCE_INCOMPLETE"
                            if not risk_source_verified
                            else (
                                "PENDING_LISTING_STATUS_INCOMPLETE"
                                if pending_listing_status_incomplete
                                else "RECONCILED"
                            )
                        )
                    )
                ),
                "bse_event_history_complete": bse_events_complete,
                "bse_termination_event_protocol_version": bse_event_metadata[
                    "protocol_version"
                ],
                "bse_termination_event_manifest_sha256": bse_event_metadata[
                    "manifest_sha256"
                ],
                "bse_termination_event_logical_content_sha256": (
                    bse_event_metadata["logical_content_sha256"]
                ),
                "bse_termination_event_termination_count": bse_event_metadata[
                    "termination_count"
                ],
                "bse_termination_event_transfer_count": bse_event_metadata[
                    "transfer_count"
                ],
                "bse_termination_event_interval_count": bse_event_metadata[
                    "interval_count"
                ],
                "bse_current_delisting_source_verified": bse_current_metadata[
                    "verified"
                ],
                "bse_current_delisting_status": bse_current_metadata["status"],
                "bse_current_delisting_error": bse_current_metadata["error"],
                "bse_current_delisting_protocol_version": bse_current_metadata[
                    "protocol_version"
                ],
                "bse_current_delisting_manifest_sha256": bse_current_metadata[
                    "manifest_sha256"
                ],
                "bse_current_delisting_logical_content_sha256": (
                    bse_current_metadata["logical_content_sha256"]
                ),
                "bse_current_delisting_event_count": bse_current_metadata[
                    "event_count"
                ],
                "bse_current_delisting_target_codes": list(
                    bse_current_metadata["target_codes"]
                ),
                "bse_current_delisting_catalogue_page_count": (
                    bse_current_metadata["catalogue_page_count"]
                ),
                "bse_current_delisting_catalogue_total_elements": (
                    bse_current_metadata["catalogue_total_elements"]
                ),
                "bse_current_delisting_catalogue_code_set_sha256": (
                    bse_current_metadata["catalogue_code_set_sha256"]
                ),
                "bse_current_delisting_retrieved_at": bse_current_metadata[
                    "retrieved_at"
                ],
                "bse_current_delisting_validation_now": bse_current_metadata[
                    "validation_now"
                ],
                "bse_current_delisting_as_of": bse_current_metadata["as_of"],
                "bse_current_delisting_current_catalogue_is_reconciliation_only": True,
                "bse_current_delisting_historical_effective_dates_from_notice_pdfs_only": True,
                "bse_current_delisting_contributes_historical_intervals": bool(
                    bse_current_metadata[
                        "historical_listing_intervals_contributed"
                    ]
                ),
                "bse_current_delisting_contributes_trading_eligibility": False,
                "sse_active_listing_source_verified": sse_active_source_verified,
                "szse_active_listing_source_verified": szse_active_source_verified,
                "szse_code_alias_history_complete": szse_alias_history_complete,
                "szse_code_change_event_source_verified": (
                    szse_code_change_source_verified
                ),
                "szse_code_change_event_protocol_version": (
                    szse_code_change_metadata["protocol_version"]
                ),
                "szse_code_change_event_raw_pdf_sha256": (
                    szse_code_change_metadata["raw_pdf_sha256"]
                ),
                "szse_code_change_event_text_sha256": (
                    szse_code_change_metadata["text_sha256"]
                ),
                "szse_code_change_event_interval_count": (
                    szse_code_change_metadata["interval_count"]
                ),
                "szse_code_change_event_count": len(resolved_szse_aliases),
                "szse_unresolved_alias_discovered_count": len(
                    discovered_szse_unresolved
                ),
                "szse_unresolved_alias_resolved_count": len(
                    resolved_szse_aliases
                ),
                "szse_unresolved_alias_count": len(remaining_szse_unresolved),
                "szse_unresolved_alias_sample": list(
                    remaining_szse_unresolved[:20]
                ),
                "sse_active_interval_history_complete": (
                    sse_active_listing_intervals_complete
                ),
                "szse_active_interval_history_complete": szse_active_complete,
            },
            "promotion_blocked": not ready,
        },
    }


class HistoricalSecurityMasterStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.manifests = self.root / "manifests"
        self.latest_attempt = self.root / "latest_attempt.json"
        self.current = self.root / "current.json"

    def _validate_v15_ready_admission(
        self,
        *,
        quality_report: Mapping[str, Any],
        source_entries: Sequence[Mapping[str, Any]],
    ) -> None:
        """Reject any READY claim that is not backed by the complete V15 bundle.

        ``build_quality_report`` remains the semantic auditor.  This storage
        boundary deliberately repeats the immutable, promotion-critical
        invariants so a caller cannot bypass that auditor by supplying an
        arbitrary ``{"status": "READY"}`` mapping directly to ``publish``.
        """

        def fail(detail: str) -> None:
            raise HistoricalSecurityMasterBlockedError(
                f"V15 READY admission failed: {detail}"
            )

        def is_sha256(value: Any) -> bool:
            return isinstance(value, str) and re.fullmatch(
                r"[0-9a-f]{64}", value
            ) is not None

        def require_sha256(value: Any, field_name: str) -> str:
            if not is_sha256(value):
                fail(f"{field_name} is not a canonical SHA-256 digest")
            return str(value)

        if not isinstance(quality_report, Mapping):
            fail("quality report is not an object")
        if quality_report.get("protocol_version") != PROTOCOL_VERSION:
            fail("quality report protocol mismatch")
        if quality_report.get("quality_policy_version") != QUALITY_POLICY_VERSION:
            fail("quality policy mismatch")

        gate = quality_report.get("gate")
        if not isinstance(gate, Mapping):
            fail("gate is not an object")
        if (
            gate.get("ready") is not True
            or gate.get("status") != "READY"
            or gate.get("promotion_blocked") is not False
        ):
            fail("gate does not make one internally consistent READY claim")
        if gate.get("protocol_version") != PROTOCOL_VERSION:
            fail("gate protocol mismatch")
        if (
            gate.get("coverage_start") != HISTORICAL_START
            or gate.get("coverage_end") != HISTORICAL_END
        ):
            fail("historical coverage boundary changed")

        completeness = gate.get("source_completeness")
        source_counts = gate.get("source_counts")
        reconciliation = gate.get("reconciliation")
        if not isinstance(completeness, Mapping):
            fail("source completeness is not an object")
        if not isinstance(source_counts, Mapping):
            fail("source counts are not an object")
        if not isinstance(reconciliation, Mapping):
            fail("reconciliation is not an object")

        required_sources = frozenset(
            {
                "sse_terminated_a_shares",
                "szse_terminated_a_shares",
                "bse_code_mapping",
                "sse_active_a_shares",
                "szse_active_a_shares",
                SZSE_CODE_CHANGE_SOURCE_NAME,
                BSE_TERMINATION_EVENT_SOURCE_NAME,
                BSE_CURRENT_DELISTING_SOURCE_NAME,
                SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME,
            }
        )
        derived_sources = frozenset(
            {"sse_current_risk_warning", "pending_listing_current_official"}
        )
        source_by_name: dict[str, Mapping[str, Any]] = {}
        for entry in source_entries:
            if not isinstance(entry, Mapping):
                fail("manifest source entry is not an object")
            name = str(entry.get("name") or "")
            if not name or name in source_by_name:
                fail(f"duplicate or empty source name: {name!r}")
            if not str(entry.get("url") or "").strip():
                fail(f"source URL is empty: {name}")
            require_sha256(entry.get("content_hash"), f"{name} content hash")
            retrieved_at = entry.get("retrieved_at")
            if not isinstance(retrieved_at, str) or not retrieved_at.strip():
                fail(f"{name} retrieved_at is empty")
            try:
                _normalized_retrieved_at(retrieved_at)
            except HistoricalSecurityMasterBlockedError as exc:
                fail(f"{name} retrieved_at is invalid: {exc}")
            if not isinstance(entry.get("statistics"), Mapping):
                fail(f"source statistics are not an object: {name}")
            source_by_name[name] = entry
        if frozenset(source_by_name) != required_sources:
            missing = sorted(required_sources - frozenset(source_by_name))
            unexpected = sorted(frozenset(source_by_name) - required_sources)
            fail(f"official source set changed; missing={missing}, unexpected={unexpected}")
        if frozenset(source_counts) != required_sources | derived_sources:
            missing = sorted((required_sources | derived_sources) - frozenset(source_counts))
            unexpected = sorted(frozenset(source_counts) - (required_sources | derived_sources))
            fail(f"source-count set changed; missing={missing}, unexpected={unexpected}")
        if any(not isinstance(source_counts.get(name), Mapping) for name in source_counts):
            fail("one or more source-count entries are not objects")

        # Every source-count entry is produced directly from the immutable
        # source statistics except BSE-current, whose current-catalogue audit
        # fields intentionally replace the smaller materialized-source view.
        for name in sorted(required_sources - {BSE_CURRENT_DELISTING_SOURCE_NAME}):
            if dict(source_counts[name]) != dict(source_by_name[name]["statistics"]):
                fail(f"source statistics changed between manifest and gate: {name}")

        required_true = (
            "sse_szse_terminated_listing_events",
            "sse_active_listing_intervals",
            "szse_active_listing_intervals",
            "sse_active_listing_source_verified",
            "szse_active_listing_source_verified",
            "szse_code_alias_history_complete",
            "szse_code_change_event_source_verified",
            "bse_termination_and_transfer_events",
            "bse_current_delisting_source_verified",
            "sse_current_risk_warning_source_verified",
            "sse_risk_warning_active_intervals_source_verified",
            "pending_listing_status_source_verified",
            "current_observation_source_verified",
            "current_observation_freshness_required_at_publish",
            "current_observation_immutable_replay_after_publish",
        )
        false_flags = (
            "bse_current_delisting_contributes_trading_eligibility",
            "sse_current_risk_warning_contributes_historical_intervals",
            "pending_listing_contributes_historical_intervals",
            "pending_listing_contributes_trading_eligibility",
            "tdx_active_snapshot_caller_retrieved_at_accepted",
        )
        for field_name in required_true:
            if completeness.get(field_name) is not True:
                fail(f"required completeness flag is not true: {field_name}")
        for field_name in false_flags:
            if completeness.get(field_name) is not False:
                fail(f"fail-closed provenance flag changed: {field_name}")
        for field_name in (
            "bse_current_delisting_current_catalogue_is_reconciliation_only",
            "bse_current_delisting_historical_effective_dates_from_notice_pdfs_only",
            "bse_current_delisting_contributes_historical_intervals",
            "sse_current_risk_warning_is_current_reconciliation_only",
            "pending_listing_is_current_reconciliation_only",
            "tdx_active_snapshot_is_reconciliation_only",
        ):
            if completeness.get(field_name) is not True:
                fail(f"provenance role flag changed: {field_name}")

        zero_completeness_counts = (
            "sse_current_risk_warning_listing_interval_missing_count",
            "sse_transition_state_conflict_count",
            "sse_required_non_pending_interval_missing_count",
            "sse_admitted_open_interval_unexpected_count",
            "sse_transition_name_mismatch_count",
            "sse_transition_official_name_mismatch_count",
        )
        for field_name in zero_completeness_counts:
            value = completeness.get(field_name)
            if type(value) is not int or value != 0:
                fail(f"non-zero completeness conflict count: {field_name}")

        if reconciliation.get("active_reconciliation_status") != "RECONCILED":
            fail("active-code reconciliation is not RECONCILED")
        if (
            reconciliation.get("required_sse_szse_overlap")
            != EXPECTED_SSE_SZSE_OVERLAP
            or reconciliation.get("official_sse_szse_overlap")
            != EXPECTED_SSE_SZSE_OVERLAP
            or reconciliation.get("historical_master_covered")
            != EXPECTED_SSE_SZSE_OVERLAP
        ):
            fail("historical terminated-listing coverage target changed")
        for field_name in (
            "sse_current_set_equality_holds",
            "szse_current_set_equality_holds",
            "bse_event_history_complete",
            "bse_current_delisting_source_verified",
            "sse_active_listing_source_verified",
            "szse_active_listing_source_verified",
            "szse_code_alias_history_complete",
            "szse_code_change_event_source_verified",
            "sse_current_risk_warning_source_verified",
            "pending_listing_status_source_verified",
            "sse_active_interval_history_complete",
            "szse_active_interval_history_complete",
        ):
            if reconciliation.get(field_name) is not True:
                fail(f"reconciliation flag is not true: {field_name}")
        zero_reconciliation_counts = (
            "historical_master_missing",
            "bse_current_alias_missing",
            "bse_delisted_still_active_count",
            "sse_missing_from_tdx_count",
            "sse_current_risk_warning_listing_interval_missing_count",
            "sse_current_risk_warning_duplicate_normal_active_count",
            "sse_current_risk_warning_missing_from_tdx_count",
            "sse_unexplained_after_pending_listing_count",
            "szse_missing_from_tdx_count",
            "szse_unexplained_after_pending_listing_count",
            "pending_listing_missing_from_tdx_count",
            "pending_listing_duplicate_sse_normal_active_count",
            "pending_listing_duplicate_sse_risk_warning_count",
            "pending_listing_duplicate_szse_normal_active_count",
            "szse_unresolved_alias_count",
        )
        for field_name in zero_reconciliation_counts:
            value = reconciliation.get(field_name)
            if type(value) is not int or value != 0:
                fail(f"non-zero reconciliation conflict count: {field_name}")

        szse_change = dict(source_by_name[SZSE_CODE_CHANGE_SOURCE_NAME]["statistics"])
        if (
            szse_change.get("protocol_version") != SZSE_CODE_CHANGE_PROTOCOL_VERSION
            or szse_change.get("admission_status") != SZSE_CODE_CHANGE_ADMITTED
            or szse_change.get("raw_pdf_sha256")
            != source_by_name[SZSE_CODE_CHANGE_SOURCE_NAME]["content_hash"]
            or szse_change.get("text_raw_pdf_sha256")
            != szse_change.get("raw_pdf_sha256")
            or szse_change.get("text_recomputed_from_raw") is not True
            or szse_change.get("interval_count") != 2
            or not is_sha256(szse_change.get("text_sha256"))
            or completeness.get("szse_code_change_event_protocol_version")
            != szse_change.get("protocol_version")
            or completeness.get("szse_code_change_event_raw_pdf_sha256")
            != szse_change.get("raw_pdf_sha256")
            or completeness.get("szse_code_change_event_text_sha256")
            != szse_change.get("text_sha256")
            or completeness.get("szse_code_change_event_interval_count") != 2
        ):
            fail("SZSE code-change source binding is incomplete")

        bse_events = dict(
            source_by_name[BSE_TERMINATION_EVENT_SOURCE_NAME]["statistics"]
        )
        if (
            bse_events.get("protocol_version") != BSE_TERMINATION_EVENT_PROTOCOL_VERSION
            or bse_events.get("status") != BSE_TERMINATION_EVENT_SOURCE_COMPLETE
            or bse_events.get("ready") is not True
            or bse_events.get("manifest_sha256")
            != source_by_name[BSE_TERMINATION_EVENT_SOURCE_NAME]["content_hash"]
            or bse_events.get("manifest_sha256")
            != BSE_TERMINATION_EVENT_MANIFEST_SHA256
            or bse_events.get("logical_content_sha256")
            != BSE_TERMINATION_EVENT_LOGICAL_SHA256
            or bse_events.get("termination_count") != 3
            or bse_events.get("transfer_count") != 3
            or bse_events.get("interval_count") != 6
            or bse_events.get("unclassified_count") != 0
            or bse_events.get("missing_effective_date_count") != 0
            or bse_events.get("source_pagination_complete") is not True
            or completeness.get("bse_termination_event_manifest_sha256")
            != bse_events.get("manifest_sha256")
            or completeness.get("bse_termination_event_logical_content_sha256")
            != bse_events.get("logical_content_sha256")
            or completeness.get("bse_termination_event_transfer_count") != 3
            or completeness.get("bse_termination_event_interval_count") != 6
        ):
            fail("BSE termination/transfer source binding is incomplete")

        bse_current = dict(
            source_by_name[BSE_CURRENT_DELISTING_SOURCE_NAME]["statistics"]
        )
        bse_current_counts = dict(source_counts[BSE_CURRENT_DELISTING_SOURCE_NAME])
        expected_bse_current_codes = sorted(BSE_CURRENT_DELISTING_CODES)
        if (
            bse_current.get("protocol_version") != BSE_CURRENT_DELISTING_PROTOCOL_VERSION
            or bse_current.get("status") != BSE_CURRENT_DELISTING_EVIDENCE_COMPLETE
            or bse_current.get("manifest_sha256")
            != source_by_name[BSE_CURRENT_DELISTING_SOURCE_NAME]["content_hash"]
            or bse_current.get("event_count") != 2
            or bse_current.get("interval_count") != 2
            or sorted(bse_current.get("event_codes") or []) != expected_bse_current_codes
            or not is_sha256(bse_current.get("logical_content_sha256"))
            or bse_current_counts.get("protocol_version")
            != BSE_CURRENT_DELISTING_PROTOCOL_VERSION
            or bse_current_counts.get("manifest_sha256")
            != bse_current.get("manifest_sha256")
            or bse_current_counts.get("logical_content_sha256")
            != bse_current.get("logical_content_sha256")
            or bse_current_counts.get("event_count") != 2
            or sorted(bse_current_counts.get("event_codes") or [])
            != expected_bse_current_codes
            or completeness.get("bse_current_delisting_status")
            != BSE_CURRENT_DELISTING_EVIDENCE_COMPLETE
            or completeness.get("bse_current_delisting_protocol_version")
            != BSE_CURRENT_DELISTING_PROTOCOL_VERSION
            or completeness.get("bse_current_delisting_expected_protocol_version")
            != BSE_CURRENT_DELISTING_PROTOCOL_VERSION
            or completeness.get("bse_current_delisting_manifest_sha256")
            != bse_current.get("manifest_sha256")
            or completeness.get("bse_current_delisting_logical_content_sha256")
            != bse_current.get("logical_content_sha256")
            or completeness.get("bse_current_delisting_event_count") != 2
            or sorted(completeness.get("bse_current_delisting_target_codes") or [])
            != expected_bse_current_codes
        ):
            fail("BSE current-delisting source binding is incomplete")

        risk_counts = dict(source_counts["sse_current_risk_warning"])
        risk_raw_hashes = completeness.get("sse_current_risk_warning_raw_hashes")
        expected_risk_source_ids = {
            spec.source_id for spec in SSE_RISK_WARNING_SOURCE_SPECS
        }
        if not isinstance(risk_raw_hashes, Mapping) or not risk_raw_hashes:
            fail("SSE risk-warning raw hash map is missing")
        if set(risk_raw_hashes) != expected_risk_source_ids:
            fail("SSE risk-warning raw source set changed")
        if any(not is_sha256(value) for value in risk_raw_hashes.values()):
            fail("SSE risk-warning raw hash map contains an invalid digest")
        if (
            completeness.get("sse_current_risk_warning_protocol_version")
            != SSE_RISK_WARNING_PROTOCOL_VERSION
            or not is_sha256(
                completeness.get("sse_current_risk_warning_manifest_sha256")
            )
            or not is_sha256(
                completeness.get("sse_current_risk_warning_logical_content_sha256")
            )
            or not is_sha256(
                completeness.get("sse_current_risk_warning_a_share_code_set_sha256")
            )
            or risk_counts.get("protocol_version") != SSE_RISK_WARNING_PROTOCOL_VERSION
            or risk_counts.get("manifest_sha256")
            != completeness.get("sse_current_risk_warning_manifest_sha256")
            or risk_counts.get("logical_content_sha256")
            != completeness.get("sse_current_risk_warning_logical_content_sha256")
            or risk_counts.get("a_share_code_set_sha256")
            != completeness.get("sse_current_risk_warning_a_share_code_set_sha256")
            or dict(risk_counts.get("raw_hashes") or {}) != dict(risk_raw_hashes)
        ):
            fail("SSE current risk-warning source binding is incomplete")

        status_7 = dict(
            source_by_name[SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME][
                "statistics"
            ]
        )
        transition_state = status_7.get("transition_binding_state")
        transition_code = str(status_7.get("transition_code_alias") or "")
        transition_name = str(status_7.get("transition_new_name") or "")
        transition_effective_date = str(
            status_7.get("transition_effective_date") or ""
        )
        state_marker_counts = status_7.get("state_marker_counts")
        lag_codes = completeness.get(
            "sse_risk_warning_active_intervals_transition_lag_codes"
        )
        if (
            status_7.get("protocol_version")
            != SSE_RISK_WARNING_ACTIVE_INTERVALS_PROTOCOL_VERSION
            or status_7.get("status")
            != SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_ADMITTED
            or status_7.get("manifest_sha256")
            != source_by_name[SSE_RISK_WARNING_ACTIVE_INTERVALS_SOURCE_NAME][
                "content_hash"
            ]
            or not is_sha256(status_7.get("logical_content_sha256"))
            or not is_sha256(status_7.get("source_snapshot_sha256"))
            or status_7.get("risk_warning_manifest_sha256")
            != completeness.get("sse_current_risk_warning_manifest_sha256")
            or status_7.get("transition_manifest_sha256")
            != SSE_RISK_WARNING_TRANSITION_MANIFEST_SHA256
            or transition_state
            not in {SSE_TRANSITION_BINDING_LAG, SSE_TRANSITION_BINDING_CONVERGED}
            or transition_code
            != f"{SSE_RISK_WARNING_FROZEN_TRANSITION.code}.SH"
            or transition_name != SSE_RISK_WARNING_FROZEN_TRANSITION.new_name
            or transition_effective_date
            != SSE_RISK_WARNING_FROZEN_TRANSITION.effective_date
            or type(status_7.get("interval_count")) is not int
            or type(status_7.get("artifact_interval_count")) is not int
            or type(status_7.get("deduplicated_status_2_count")) is not int
            or status_7.get("interval_count") < 0
            or status_7.get("deduplicated_status_2_count") < 0
            or type(status_7.get("risk_warning_interval_count")) is not int
            or status_7.get("risk_warning_interval_count") <= 0
            or status_7.get("risk_warning_interval_count")
            != risk_counts.get("a_share_rows")
            or status_7.get("risk_warning_interval_count")
            != completeness.get("sse_current_risk_warning_a_share_count")
            or status_7.get("risk_warning_state_marker") != "7|8"
            or status_7.get("transition_lag_state_marker") != "7|4"
            or status_7.get("state_marker_4_allowed_only_for_fixed_transition")
            is not True
            or status_7.get("artifact_interval_count")
            != status_7.get("interval_count")
            + status_7.get("deduplicated_status_2_count")
            or completeness.get("sse_risk_warning_active_intervals_protocol_version")
            != status_7.get("protocol_version")
            or completeness.get("sse_risk_warning_active_intervals_manifest_sha256")
            != status_7.get("manifest_sha256")
            or completeness.get(
                "sse_risk_warning_active_intervals_logical_content_sha256"
            )
            != status_7.get("logical_content_sha256")
            or completeness.get(
                "sse_risk_warning_active_intervals_source_snapshot_sha256"
            )
            != status_7.get("source_snapshot_sha256")
            or completeness.get("sse_risk_warning_active_intervals_interval_count")
            != status_7.get("artifact_interval_count")
            or completeness.get("sse_risk_warning_active_intervals_materialized_count")
            != status_7.get("interval_count")
            or completeness.get(
                "sse_risk_warning_active_intervals_transition_binding_state"
            )
            != transition_state
            or completeness.get(
                "sse_risk_warning_active_intervals_transition_code_alias"
            )
            != transition_code
            or completeness.get(
                "sse_risk_warning_active_intervals_transition_new_name"
            )
            != transition_name
            or completeness.get(
                "sse_risk_warning_active_intervals_transition_effective_date"
            )
            != transition_effective_date
        ):
            fail("SSE status-7 active-interval source binding is incomplete")
        if transition_state == SSE_TRANSITION_BINDING_LAG:
            if lag_codes != [transition_code]:
                fail("SSE transition lag state does not contain exactly its code")
            if status_7.get("artifact_interval_count") != (
                status_7.get("risk_warning_interval_count") + 1
            ):
                fail("SSE transition lag interval cardinality changed")
            expected_state_marker_counts = {
                "7|4": 1,
                "7|8": status_7.get("risk_warning_interval_count"),
            }
        elif lag_codes != []:
            fail("converged SSE transition still declares a status-7 lag code")
        else:
            if status_7.get("artifact_interval_count") != status_7.get(
                "risk_warning_interval_count"
            ):
                fail("converged SSE transition interval cardinality changed")
            expected_state_marker_counts = {
                "7|8": status_7.get("risk_warning_interval_count"),
            }
        if state_marker_counts != expected_state_marker_counts:
            fail("SSE status-7 state-marker evidence changed")

        pending_counts = dict(source_counts["pending_listing_current_official"])
        pending_raw_hashes = completeness.get("pending_listing_raw_hashes")
        expected_pending_code_set_sha256 = _sha256(
            _canonical_json_bytes(sorted(PENDING_LISTING_RECONCILIATION_CODES))
        )
        if not isinstance(pending_raw_hashes, Mapping):
            fail("pending-listing raw hash map is missing")
        if set(pending_raw_hashes) != set(PENDING_LISTING_SOURCE_ORDER):
            fail("pending-listing raw source set changed")
        if (
            completeness.get("pending_listing_status")
            != PENDING_LISTING_EVIDENCE_COMPLETE
            or completeness.get("pending_listing_protocol_version")
            != PENDING_LISTING_PROTOCOL_VERSION
            or completeness.get("pending_listing_expected_protocol_version")
            != PENDING_LISTING_PROTOCOL_VERSION
            or not is_sha256(completeness.get("pending_listing_manifest_sha256"))
            or not is_sha256(
                completeness.get("pending_listing_logical_content_sha256")
            )
            or len(pending_raw_hashes) != len(PENDING_LISTING_SOURCE_ORDER)
            or any(not is_sha256(value) for value in pending_raw_hashes.values())
            or completeness.get("pending_listing_raw_source_count")
            != len(PENDING_LISTING_SOURCE_ORDER)
            or completeness.get("pending_listing_official_code_count")
            != len(PENDING_LISTING_RECONCILIATION_CODES)
            or pending_counts.get("protocol_version") != PENDING_LISTING_PROTOCOL_VERSION
            or pending_counts.get("manifest_sha256")
            != completeness.get("pending_listing_manifest_sha256")
            or pending_counts.get("logical_content_sha256")
            != completeness.get("pending_listing_logical_content_sha256")
            or dict(pending_counts.get("raw_hashes") or {})
            != dict(pending_raw_hashes)
            or pending_counts.get("raw_source_count")
            != len(PENDING_LISTING_SOURCE_ORDER)
            or pending_counts.get("official_code_count")
            != len(PENDING_LISTING_RECONCILIATION_CODES)
            or pending_counts.get("code_set_sha256")
            != completeness.get("pending_listing_admitted_code_set_sha256")
            or pending_counts.get("code_set_sha256")
            != expected_pending_code_set_sha256
        ):
            fail("pending-listing source binding is incomplete")

        if (
            completeness.get("current_observation_protocol_version")
            != CURRENT_OBSERVATION_PROTOCOL_VERSION
            or not is_sha256(
                completeness.get("current_observation_manifest_sha256")
            )
            or not is_sha256(
                completeness.get("current_observation_logical_content_sha256")
            )
            or not is_sha256(
                completeness.get("current_observation_tdx_code_set_sha256")
            )
            or not is_sha256(
                completeness.get("current_observation_tdx_identity_sha256")
            )
            or type(completeness.get("current_observation_tdx_code_count")) is not int
            or completeness.get("current_observation_tdx_code_count")
            < CURRENT_OBSERVATION_MINIMUM_TDX_CODE_COUNT
            or completeness.get(
                "current_observation_pending_listing_manifest_sha256"
            )
            != completeness.get("pending_listing_manifest_sha256")
            or completeness.get(
                "current_observation_pending_listing_logical_content_sha256"
            )
            != completeness.get("pending_listing_logical_content_sha256")
            or completeness.get(
                "current_observation_bse_current_delisting_manifest_sha256"
            )
            != completeness.get("bse_current_delisting_manifest_sha256")
            or completeness.get(
                "current_observation_bse_current_delisting_logical_content_sha256"
            )
            != completeness.get("bse_current_delisting_logical_content_sha256")
        ):
            fail("current-observation structural binding is incomplete")

    def publish(
        self,
        *,
        sources: Sequence[ParsedOfficialSource],
        records: Sequence[SecurityMasterRecord],
        quality_report: Mapping[str, Any],
        tdx_active_codes: Iterable[str],
        current_observation_manifest: (
            str | ObservationManifestReference | None
        ) = None,
        current_observation_store: SecurityMasterObservationStore | None = None,
    ) -> dict[str, Any]:
        validate_security_master_records(records)
        _validate_source_bundle(records, sources)
        if current_observation_manifest is None:
            raise HistoricalSecurityMasterBlockedError(
                "publication requires a fresh current-observation manifest"
            )
        _observation, observation_metadata = (
            _normalize_current_observation_reference(
                current_observation_manifest,
                store=current_observation_store,
                require_current=True,
            )
        )
        source_entries: list[dict[str, Any]] = []
        for source in sorted(sources, key=lambda item: item.name):
            if _sha256(source.raw_bytes) != source.source_hash:
                raise HistoricalSecurityMasterBlockedError(
                    f"source hash mismatch before publish: {source.name}"
                )
            source_entries.append(
                {
                    "name": source.name,
                    "url": source.source_url,
                    "content_hash": source.source_hash,
                    "retrieved_at": source.retrieved_at,
                    "statistics": dict(source.statistics),
                }
            )
        active_codes = sorted(_normalize_active_codes(tdx_active_codes))
        active_bytes = _canonical_json_bytes(active_codes)
        identity_names = {
            code: str(observation_metadata["tdx_names"][code])
            for code in active_codes
        }
        identity_bytes = _canonical_json_bytes(identity_names)
        if (
            observation_metadata is not None
            and observation_metadata["tdx_code_set_sha256"] != _sha256(active_bytes)
        ):
            raise HistoricalSecurityMasterBlockedError(
                "publication TDX snapshot does not match current observation"
            )
        quality_value = dict(quality_report)
        gate_value = dict(quality_value.get("gate") or {})
        completeness = dict(gate_value.get("source_completeness") or {})
        if observation_metadata is not None:
            required_observation_fields = {
                "current_observation_source_verified": True,
                "current_observation_protocol_version": observation_metadata[
                    "protocol_version"
                ],
                "current_observation_manifest_sha256": observation_metadata[
                    "manifest_sha256"
                ],
                "current_observation_logical_content_sha256": observation_metadata[
                    "logical_content_sha256"
                ],
                "current_observation_validated_at": observation_metadata[
                    "validated_at"
                ],
                "current_observation_as_of": observation_metadata["as_of"],
                "current_observation_tdx_observed_at": observation_metadata[
                    "tdx_observed_at"
                ],
                "current_observation_tdx_code_count": observation_metadata[
                    "tdx_code_count"
                ],
                "current_observation_tdx_code_set_sha256": observation_metadata[
                    "tdx_code_set_sha256"
                ],
                "current_observation_tdx_identity_sha256": observation_metadata[
                    "tdx_identity_sha256"
                ],
                "current_observation_pending_listing_manifest_sha256": (
                    observation_metadata["pending_listing_manifest_sha256"]
                ),
                "current_observation_pending_listing_logical_content_sha256": (
                    observation_metadata[
                        "pending_listing_logical_content_sha256"
                    ]
                ),
                "current_observation_bse_current_delisting_manifest_sha256": (
                    observation_metadata[
                        "bse_current_delisting_manifest_sha256"
                    ]
                ),
                "current_observation_bse_current_delisting_logical_content_sha256": (
                    observation_metadata[
                        "bse_current_delisting_logical_content_sha256"
                    ]
                ),
                "pending_listing_manifest_sha256": observation_metadata[
                    "pending_listing_manifest_sha256"
                ],
                "pending_listing_logical_content_sha256": observation_metadata[
                    "pending_listing_logical_content_sha256"
                ],
                "bse_current_delisting_manifest_sha256": observation_metadata[
                    "bse_current_delisting_manifest_sha256"
                ],
                "bse_current_delisting_logical_content_sha256": observation_metadata[
                    "bse_current_delisting_logical_content_sha256"
                ],
            }
            if any(
                completeness.get(key) != expected
                for key, expected in required_observation_fields.items()
            ):
                raise HistoricalSecurityMasterBlockedError(
                    "quality report is not bound to the cold-replayed current observation"
                )
        if _sha256(identity_bytes) != observation_metadata["tdx_identity_sha256"]:
            raise HistoricalSecurityMasterBlockedError(
                "publication TDX identity snapshot does not match current observation"
            )
        promotion_claimed = (
            gate_value.get("ready") is True
            or gate_value.get("status") == "READY"
            or gate_value.get("promotion_blocked") is False
        )
        if promotion_claimed:
            self._validate_v15_ready_admission(
                quality_report=quality_value,
                source_entries=source_entries,
            )
        for entry, source in zip(
            source_entries,
            sorted(sources, key=lambda item: item.name),
            strict=True,
        ):
            entry["object_path"] = str(self._write_object(source.raw_bytes))
        active_path = self._write_object(active_bytes)
        identity_path = self._write_object(identity_bytes)
        master_bytes = b"\n".join(
            _canonical_json_bytes(record.to_dict())
            for record in sorted(
                records,
                key=lambda item: (
                    item.canonical_entity_id,
                    item.valid_from,
                    item.exchange,
                    item.code_alias,
                ),
            )
        ) + b"\n"
        master_path = self._write_object(master_bytes)
        quality_bytes = _canonical_json_bytes(quality_value)
        quality_path = self._write_object(quality_bytes)
        manifest_payload = {
            "protocol_version": PROTOCOL_VERSION,
            "quality_policy_version": QUALITY_POLICY_VERSION,
            "current_observation": {
                key: value
                for key, value in observation_metadata.items()
                if key != "tdx_names"
            },
            "sources": source_entries,
            "artifacts": {
                "security_master_jsonl": {
                    "content_hash": _sha256(master_bytes),
                    "object_path": str(master_path),
                    "row_count": len(records),
                },
                "quality_report": {
                    "content_hash": _sha256(quality_bytes),
                    "object_path": str(quality_path),
                },
                "tdx_active_snapshot": {
                    "content_hash": _sha256(active_bytes),
                    "object_path": str(active_path),
                    "row_count": len(active_codes),
                },
                "tdx_identity_snapshot": {
                    "content_hash": _sha256(identity_bytes),
                    "object_path": str(identity_path),
                    "row_count": len(identity_names),
                },
            },
        }
        manifest_bytes = _canonical_json_bytes(manifest_payload)
        snapshot_id = _sha256(manifest_bytes)
        manifest_path = self.manifests / f"{snapshot_id}.json"
        self._atomic_write_exact(manifest_path, manifest_bytes)
        pointer = {
            "snapshot_id": snapshot_id,
            "manifest_hash": snapshot_id,
            "manifest_path": str(manifest_path),
            "protocol_version": PROTOCOL_VERSION,
        }
        gate = dict(quality_value["gate"])
        gate.update({"snapshot_id": snapshot_id, "manifest_hash": snapshot_id})
        published = gate.get("ready") is True and gate.get("status") == "READY"
        pointer_bytes = _canonical_json_bytes(pointer)
        with self._exclusive_publish_lock():
            self._atomic_replace(self.latest_attempt, pointer_bytes)
            if published:
                # ``current.json`` is the promotion commit point.  If this
                # replace fails, the immutable attempt remains auditable via
                # ``latest_attempt.json`` but operational readers stay blocked.
                self._atomic_replace(self.current, pointer_bytes)
        return {**pointer, "gate": gate, "published": published}

    def _validate_current_observation_binding(
        self,
        release: Mapping[str, Any],
        gate: Mapping[str, Any],
    ) -> None:
        completeness = dict(gate.get("source_completeness") or {})
        observation_binding = dict(
            release["manifest"].get("current_observation") or {}
        )
        if not observation_binding:
            raise HistoricalSecurityMasterBlockedError(
                "current security master release lacks a current-observation binding"
            )
        digest = str(observation_binding.get("manifest_sha256") or "")
        batch, replayed = _normalize_current_observation_reference(
            digest,
            store=None,
            require_current=False,
        )
        _ = batch
        expected_binding = {
                "protocol_version": replayed["protocol_version"],
                "manifest_sha256": replayed["manifest_sha256"],
                "logical_content_sha256": replayed[
                    "logical_content_sha256"
                ],
                "validated_at": replayed["validated_at"],
                "as_of": replayed["as_of"],
                "tdx_observed_at": replayed["tdx_observed_at"],
                "tdx_code_count": replayed["tdx_code_count"],
                "tdx_code_set_sha256": replayed["tdx_code_set_sha256"],
                "tdx_identity_sha256": replayed["tdx_identity_sha256"],
                "pending_listing_manifest_sha256": replayed[
                    "pending_listing_manifest_sha256"
                ],
                "pending_listing_logical_content_sha256": replayed[
                    "pending_listing_logical_content_sha256"
                ],
                "bse_current_delisting_manifest_sha256": replayed[
                    "bse_current_delisting_manifest_sha256"
                ],
                "bse_current_delisting_logical_content_sha256": replayed[
                    "bse_current_delisting_logical_content_sha256"
                ],
                "freshness_required_at_publish": True,
                "immutable_replay_after_publish": True,
        }
        if observation_binding != expected_binding:
            raise HistoricalSecurityMasterBlockedError(
                "published current observation binding changed"
            )
        active_metadata = release["manifest"]["artifacts"][
            "tdx_active_snapshot"
        ]
        identity_metadata = release["manifest"]["artifacts"].get(
            "tdx_identity_snapshot"
        )
        if (
            active_metadata.get("content_hash")
            != replayed["tdx_code_set_sha256"]
            or not isinstance(identity_metadata, dict)
            or identity_metadata.get("content_hash")
            != replayed["tdx_identity_sha256"]
            or identity_metadata.get("row_count")
            != replayed["tdx_code_count"]
            or completeness.get("current_observation_source_verified") is not True
            or completeness.get("current_observation_protocol_version")
            != replayed["protocol_version"]
            or completeness.get("current_observation_manifest_sha256") != digest
            or completeness.get("current_observation_logical_content_sha256")
            != replayed["logical_content_sha256"]
            or completeness.get("current_observation_validated_at")
            != replayed["validated_at"]
            or completeness.get("current_observation_as_of") != replayed["as_of"]
            or completeness.get("current_observation_tdx_observed_at")
            != replayed["tdx_observed_at"]
            or completeness.get("current_observation_tdx_code_count")
            != replayed["tdx_code_count"]
            or completeness.get("current_observation_tdx_code_set_sha256")
            != replayed["tdx_code_set_sha256"]
            or completeness.get("current_observation_tdx_identity_sha256")
            != replayed["tdx_identity_sha256"]
            or completeness.get(
                "current_observation_pending_listing_manifest_sha256"
            )
            != replayed["pending_listing_manifest_sha256"]
            or completeness.get(
                "current_observation_pending_listing_logical_content_sha256"
            )
            != replayed["pending_listing_logical_content_sha256"]
            or completeness.get(
                "current_observation_bse_current_delisting_manifest_sha256"
            )
            != replayed["bse_current_delisting_manifest_sha256"]
            or completeness.get(
                "current_observation_bse_current_delisting_logical_content_sha256"
            )
            != replayed["bse_current_delisting_logical_content_sha256"]
            or completeness.get("pending_listing_manifest_sha256")
            != replayed["pending_listing_manifest_sha256"]
            or completeness.get("pending_listing_logical_content_sha256")
            != replayed["pending_listing_logical_content_sha256"]
            or completeness.get("bse_current_delisting_manifest_sha256")
            != replayed["bse_current_delisting_manifest_sha256"]
            or completeness.get("bse_current_delisting_logical_content_sha256")
            != replayed["bse_current_delisting_logical_content_sha256"]
        ):
            raise HistoricalSecurityMasterBlockedError(
                "published quality report does not match current observation"
            )

    def load_gate(self) -> dict[str, Any]:
        # Operational consumers only trust an explicitly promoted current
        # pointer.  ``latest_attempt`` is audit evidence and can contain a
        # failed gate or a READY attempt whose current-pointer commit crashed.
        pointer_path = self.current
        if not pointer_path.exists():
            return missing_historical_universe_gate()
        try:
            release = self._load_pointer_release(pointer_path)
            gate = dict(release["quality_report"]["gate"])
            if gate.get("ready") is not True or gate.get("status") != "READY":
                raise HistoricalSecurityMasterBlockedError(
                    "current security master pointer references a non-ready release"
                )
            gate.update(
                {
                    "snapshot_id": release["snapshot_id"],
                    "manifest_hash": release["manifest_hash"],
                }
            )
            self._validate_v15_ready_admission(
                quality_report=release["quality_report"],
                source_entries=release["manifest"]["sources"],
            )
            self._validate_current_observation_binding(release, gate)
            return gate
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return {
                **missing_historical_universe_gate(),
                "status": "ARTIFACT_INVALID",
                "detail": f"Historical security master artifact validation failed: {exc}",
            }
        except HistoricalSecurityMasterBlockedError as exc:
            return {
                **missing_historical_universe_gate(),
                "status": "ARTIFACT_INVALID",
                "detail": f"Historical security master artifact validation failed: {exc}",
            }

    def load_current_release(self) -> dict[str, Any]:
        release = self._load_pointer_release(self.current)
        gate = dict(release["quality_report"]["gate"])
        if gate.get("ready") is not True or gate.get("status") != "READY":
            raise HistoricalSecurityMasterBlockedError(
                "current security master pointer references a non-ready release"
            )
        self._validate_v15_ready_admission(
            quality_report=release["quality_report"],
            source_entries=release["manifest"]["sources"],
        )
        self._validate_current_observation_binding(release, gate)
        return release

    def load_latest_attempt(self) -> dict[str, Any]:
        return self._load_pointer_release(self.latest_attempt)

    def _load_pointer_release(self, pointer_path: Path) -> dict[str, Any]:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        snapshot_id = str(pointer["snapshot_id"])
        return self.load_release(snapshot_id, manifest_path=pointer.get("manifest_path"))

    def load_release(
        self,
        snapshot_id: str,
        *,
        manifest_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Cold-replay one immutable attempt without treating it as current.

        Failed quality attempts remain useful audit evidence, but only a READY
        attempt may be promoted through ``current.json``.  This method keeps
        those two concepts separate.
        """

        if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
            raise HistoricalSecurityMasterBlockedError("invalid security master snapshot id")
        manifest_path = Path(
            str(manifest_path)
            if manifest_path is not None
            else str(self.manifests / f"{snapshot_id}.json")
        )
        expected_manifest = self.manifests / f"{snapshot_id}.json"
        if manifest_path.resolve() != expected_manifest.resolve():
            raise HistoricalSecurityMasterBlockedError("manifest path escapes the master store")
        manifest_bytes = manifest_path.read_bytes()
        if _sha256(manifest_bytes) != snapshot_id:
            raise HistoricalSecurityMasterBlockedError("manifest content hash mismatch")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if manifest.get("protocol_version") != PROTOCOL_VERSION:
            raise HistoricalSecurityMasterBlockedError("security master protocol mismatch")
        if manifest.get("quality_policy_version") != QUALITY_POLICY_VERSION:
            raise HistoricalSecurityMasterBlockedError(
                "security master quality policy mismatch"
            )
        observation_binding = manifest.get("current_observation")
        if observation_binding is not None and not isinstance(
            observation_binding, dict
        ):
            raise HistoricalSecurityMasterBlockedError(
                "security master current observation binding is invalid"
            )
        artifacts = manifest["artifacts"]
        decoded: dict[str, bytes] = {}
        for name, metadata in artifacts.items():
            path = Path(str(metadata["object_path"]))
            expected_hash = str(metadata["content_hash"])
            expected_path = self._object_path(expected_hash)
            if path.resolve() != expected_path.resolve():
                raise HistoricalSecurityMasterBlockedError(f"{name} object path mismatch")
            content = path.read_bytes()
            if _sha256(content) != expected_hash:
                raise HistoricalSecurityMasterBlockedError(f"{name} content hash mismatch")
            decoded[name] = content
        for source in manifest["sources"]:
            name = str(source["name"])
            expected_hash = str(source["content_hash"])
            path = Path(str(source["object_path"]))
            expected_path = self._object_path(expected_hash)
            if path.resolve() != expected_path.resolve():
                raise HistoricalSecurityMasterBlockedError(
                    f"{name} source object path mismatch"
                )
            content = path.read_bytes()
            if _sha256(content) != expected_hash:
                raise HistoricalSecurityMasterBlockedError(
                    f"{name} source content hash mismatch"
                )
        quality = json.loads(decoded["quality_report"].decode("utf-8"))
        return {
            "snapshot_id": snapshot_id,
            "manifest_hash": snapshot_id,
            "manifest": manifest,
            "quality_report": quality,
        }

    def _object_path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HistoricalSecurityMasterBlockedError("invalid object hash")
        return self.objects / digest[:2] / digest

    def _write_object(self, content: bytes) -> Path:
        path = self._object_path(_sha256(content))
        self._atomic_write_exact(path, content)
        return path

    @contextmanager
    def _exclusive_publish_lock(self):
        """Serialize the latest/current pointer commit across processes."""

        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".publish.lock"
        handle = path.open("a+b")
        acquired = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise HistoricalSecurityMasterBlockedError(
                    "another security master publication is already in progress"
                ) from exc
            yield
        finally:
            try:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    @staticmethod
    def _atomic_write_exact(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise HistoricalSecurityMasterBlockedError(
                    f"content-address collision or corruption: {path}"
                )
            return
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                if path.read_bytes() != content:
                    raise HistoricalSecurityMasterBlockedError(
                        f"content-address collision or corruption: {path}"
                    )
                return
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _atomic_replace(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def missing_historical_universe_gate() -> dict[str, Any]:
    return {
        "ready": False,
        "status": "NOT_BUILT",
        "detail": "No validated historical security master release is installed",
        "snapshot_id": "",
        "manifest_hash": "",
        "protocol_version": PROTOCOL_VERSION,
        "coverage_start": HISTORICAL_START,
        "coverage_end": HISTORICAL_END,
        "source_counts": {},
        "source_completeness": {
            "sse_szse_terminated_listing_events": False,
            "sse_active_listing_intervals": False,
            "szse_active_listing_intervals": False,
            "sse_active_listing_source_verified": False,
            "szse_active_listing_source_verified": False,
            "szse_code_alias_history_complete": False,
            "szse_code_change_event_source_verified": False,
            "szse_code_change_event_protocol_version": "",
            "szse_code_change_event_raw_pdf_sha256": "",
            "szse_code_change_event_text_sha256": "",
            "szse_code_change_event_interval_count": 0,
            "sse_current_risk_warning_source_verified": False,
            "sse_current_risk_warning_protocol_version": "",
            "sse_current_risk_warning_manifest_sha256": "",
            "sse_current_risk_warning_logical_content_sha256": "",
            "sse_current_risk_warning_a_share_code_set_sha256": "",
            "sse_current_risk_warning_raw_hashes": {},
            "sse_current_risk_warning_main_board_rows": 0,
            "sse_current_risk_warning_main_board_a_share_rows": 0,
            "sse_current_risk_warning_main_board_b_share_rows": 0,
            "sse_current_risk_warning_star_market_rows": 0,
            "sse_current_risk_warning_star_market_a_share_rows": 0,
            "sse_current_risk_warning_star_market_b_share_rows": 0,
            "sse_current_risk_warning_a_share_count": 0,
            "sse_current_risk_warning_b_share_excluded_count": 0,
            "sse_current_risk_warning_listing_interval_covered_count": 0,
            "sse_current_risk_warning_listing_interval_missing_count": 0,
            "sse_current_risk_warning_listing_interval_missing_sample": [],
            "sse_current_risk_warning_listing_interval_missing_code_set_sha256": "",
            "sse_current_risk_warning_is_current_reconciliation_only": True,
            "sse_current_risk_warning_contributes_historical_intervals": False,
            "sse_risk_warning_active_intervals_source_verified": False,
            "sse_risk_warning_active_intervals_protocol_version": "",
            "sse_risk_warning_active_intervals_manifest_sha256": "",
            "sse_risk_warning_active_intervals_logical_content_sha256": "",
            "sse_risk_warning_active_intervals_source_snapshot_sha256": "",
            "sse_risk_warning_active_intervals_interval_count": 0,
            "sse_risk_warning_active_intervals_materialized_count": 0,
            "sse_risk_warning_active_intervals_transition_binding_state": "",
            "sse_risk_warning_active_intervals_transition_code_alias": "",
            "sse_risk_warning_active_intervals_transition_new_name": "",
            "sse_risk_warning_active_intervals_transition_effective_date": "",
            "sse_risk_warning_active_intervals_transition_lag_codes": [],
            "sse_transition_state_conflict_count": 0,
            "sse_transition_state_conflict_sample": [],
            "sse_required_non_pending_interval_missing_count": 0,
            "sse_required_non_pending_interval_missing_sample": [],
            "sse_required_non_pending_interval_missing_code_set_sha256": "",
            "sse_admitted_open_interval_unexpected_count": 0,
            "sse_admitted_open_interval_unexpected_sample": [],
            "sse_transition_name_mismatch_count": 0,
            "sse_transition_name_mismatch_sample": [],
            "sse_transition_official_name_mismatch_count": 0,
            "sse_transition_official_name_mismatch_sample": [],
            "pending_listing_status_source_verified": False,
            "pending_listing_status": "PENDING_LISTING_STATUS_INCOMPLETE",
            "pending_listing_error": "No verified pending-listing release is installed",
            "pending_listing_protocol_version": "",
            "pending_listing_expected_protocol_version": PENDING_LISTING_PROTOCOL_VERSION,
            "pending_listing_manifest_sha256": "",
            "pending_listing_logical_content_sha256": "",
            "pending_listing_raw_hashes": {},
            "pending_listing_raw_source_count": 0,
            "pending_listing_official_code_count": 0,
            "pending_listing_admitted_code_set_sha256": "",
            "pending_listing_retrieved_at": "",
            "pending_listing_earliest_source_retrieved_at": "",
            "pending_listing_latest_source_retrieved_at": "",
            "pending_listing_validation_now": "",
            "pending_listing_as_of": "",
            "pending_listing_is_current_reconciliation_only": True,
            "pending_listing_contributes_historical_intervals": False,
            "pending_listing_contributes_trading_eligibility": False,
            "bse_termination_and_transfer_events": False,
            "bse_termination_event_protocol_version": "",
            "bse_termination_event_manifest_sha256": "",
            "bse_termination_event_logical_content_sha256": "",
            "bse_termination_event_transfer_count": 0,
            "bse_termination_event_interval_count": 0,
            "bse_current_delisting_source_verified": False,
            "bse_current_delisting_status": (
                "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
            ),
            "bse_current_delisting_error": (
                "No verified BSE current-delisting release is installed"
            ),
            "bse_current_delisting_protocol_version": "",
            "bse_current_delisting_expected_protocol_version": (
                BSE_CURRENT_DELISTING_PROTOCOL_VERSION
            ),
            "bse_current_delisting_manifest_sha256": "",
            "bse_current_delisting_logical_content_sha256": "",
            "bse_current_delisting_event_count": 0,
            "bse_current_delisting_target_codes": sorted(
                BSE_CURRENT_DELISTING_CODES
            ),
            "bse_current_delisting_catalogue_page_count": 0,
            "bse_current_delisting_catalogue_total_elements": 0,
            "bse_current_delisting_catalogue_code_set_sha256": "",
            "bse_current_delisting_retrieved_at": "",
            "bse_current_delisting_validation_now": "",
            "bse_current_delisting_as_of": "",
            "bse_current_delisting_current_catalogue_is_reconciliation_only": True,
            "bse_current_delisting_historical_effective_dates_from_notice_pdfs_only": True,
            "bse_current_delisting_contributes_historical_intervals": False,
            "bse_current_delisting_contributes_trading_eligibility": False,
            "tdx_active_snapshot_is_reconciliation_only": True,
            "tdx_active_snapshot_observed_at": "",
            "tdx_active_snapshot_code_set_sha256": "",
            "tdx_active_snapshot_caller_retrieved_at_accepted": False,
            "current_observation_tdx_identity_sha256": "",
        },
        "reconciliation": {
            "official_sse_szse_overlap": 0,
            "required_sse_szse_overlap": EXPECTED_SSE_SZSE_OVERLAP,
            "historical_master_covered": 0,
            "historical_master_missing": EXPECTED_SSE_SZSE_OVERLAP,
            "bse_event_history_complete": False,
            "bse_termination_event_protocol_version": "",
            "bse_termination_event_manifest_sha256": "",
            "bse_termination_event_logical_content_sha256": "",
            "bse_termination_event_termination_count": 0,
            "bse_termination_event_transfer_count": 0,
            "bse_termination_event_interval_count": 0,
            "bse_current_delisting_source_verified": False,
            "bse_current_delisting_status": (
                "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
            ),
            "bse_current_delisting_error": (
                "No verified BSE current-delisting release is installed"
            ),
            "bse_current_delisting_protocol_version": "",
            "bse_current_delisting_manifest_sha256": "",
            "bse_current_delisting_logical_content_sha256": "",
            "bse_current_delisting_event_count": 0,
            "bse_current_delisting_target_codes": sorted(
                BSE_CURRENT_DELISTING_CODES
            ),
            "bse_current_delisting_catalogue_page_count": 0,
            "bse_current_delisting_catalogue_total_elements": 0,
            "bse_current_delisting_catalogue_code_set_sha256": "",
            "bse_current_delisting_retrieved_at": "",
            "bse_current_delisting_validation_now": "",
            "bse_current_delisting_as_of": "",
            "bse_current_delisting_current_catalogue_is_reconciliation_only": True,
            "bse_current_delisting_historical_effective_dates_from_notice_pdfs_only": True,
            "bse_current_delisting_contributes_historical_intervals": False,
            "bse_current_delisting_contributes_trading_eligibility": False,
            "bse_delisted_still_active_count": 0,
            "bse_delisted_still_active_sample": [],
            "sse_active_listing_source_verified": False,
            "szse_active_listing_source_verified": False,
            "szse_code_alias_history_complete": False,
            "szse_code_change_event_source_verified": False,
            "szse_code_change_event_protocol_version": "",
            "szse_code_change_event_raw_pdf_sha256": "",
            "szse_code_change_event_text_sha256": "",
            "szse_code_change_event_interval_count": 0,
            "szse_code_change_event_count": 0,
            "szse_unresolved_alias_discovered_count": 0,
            "szse_unresolved_alias_resolved_count": 0,
            "szse_unresolved_alias_count": 0,
            "szse_unresolved_alias_sample": [],
            "sse_active_interval_history_complete": False,
            "szse_active_interval_history_complete": False,
            "sse_current_risk_warning_source_verified": False,
            "sse_current_risk_warning_protocol_version": "",
            "sse_current_risk_warning_manifest_sha256": "",
            "sse_current_risk_warning_logical_content_sha256": "",
            "sse_current_risk_warning_a_share_code_set_sha256": "",
            "sse_current_risk_warning_raw_hashes": {},
            "sse_current_risk_warning_main_board_rows": 0,
            "sse_current_risk_warning_main_board_a_share_rows": 0,
            "sse_current_risk_warning_main_board_b_share_rows": 0,
            "sse_current_risk_warning_star_market_rows": 0,
            "sse_current_risk_warning_star_market_a_share_rows": 0,
            "sse_current_risk_warning_star_market_b_share_rows": 0,
            "sse_current_risk_warning_a_share_count": 0,
            "sse_current_risk_warning_b_share_excluded_count": 0,
            "sse_current_risk_warning_listing_interval_covered_count": 0,
            "sse_current_risk_warning_listing_interval_missing_count": 0,
            "sse_current_risk_warning_listing_interval_missing_sample": [],
            "sse_current_risk_warning_listing_interval_missing_code_set_sha256": "",
            "sse_current_risk_warning_duplicate_normal_active_count": 0,
            "sse_current_risk_warning_duplicate_normal_active_sample": [],
            "sse_current_risk_warning_missing_from_tdx_count": 0,
            "sse_current_risk_warning_missing_from_tdx_sample": [],
            "sse_current_risk_warning_explained_extra_count": 0,
            "sse_current_risk_warning_explained_extra_code_set_sha256": "",
            "sse_unexplained_after_risk_warning_count": 0,
            "sse_unexplained_after_risk_warning_sample": [],
            "sse_unexplained_after_risk_warning_code_set_sha256": "",
            "sse_expected_current_after_risk_warning_count": 0,
            "sse_unexplained_after_pending_listing_count": 0,
            "sse_unexplained_after_pending_listing_sample": [],
            "sse_unexplained_after_pending_listing_code_set_sha256": "",
            "sse_expected_current_after_pending_listing_count": 0,
            "sse_expected_current_code_set_sha256": "",
            "sse_tdx_current_code_set_sha256": "",
            "sse_current_set_equality_holds": False,
            "sse_risk_warning_is_current_reconciliation_only": True,
            "sse_risk_warning_contributes_historical_intervals": False,
            "sse_risk_warning_active_intervals_source_verified": False,
            "sse_risk_warning_active_intervals_protocol_version": "",
            "sse_risk_warning_active_intervals_manifest_sha256": "",
            "sse_risk_warning_active_intervals_logical_content_sha256": "",
            "sse_risk_warning_active_intervals_source_snapshot_sha256": "",
            "sse_risk_warning_active_intervals_interval_count": 0,
            "sse_risk_warning_active_intervals_materialized_count": 0,
            "sse_risk_warning_active_intervals_transition_binding_state": "",
            "sse_risk_warning_active_intervals_transition_code_alias": "",
            "sse_risk_warning_active_intervals_transition_new_name": "",
            "sse_risk_warning_active_intervals_transition_effective_date": "",
            "sse_risk_warning_active_intervals_transition_lag_codes": [],
            "sse_transition_state_conflict_count": 0,
            "sse_transition_state_conflict_sample": [],
            "sse_required_non_pending_interval_missing_count": 0,
            "sse_required_non_pending_interval_missing_sample": [],
            "sse_required_non_pending_interval_missing_code_set_sha256": "",
            "sse_admitted_open_interval_unexpected_count": 0,
            "sse_admitted_open_interval_unexpected_sample": [],
            "sse_transition_name_mismatch_count": 0,
            "sse_transition_name_mismatch_sample": [],
            "sse_transition_official_name_mismatch_count": 0,
            "sse_transition_official_name_mismatch_sample": [],
            "szse_unexplained_after_pending_listing_count": 0,
            "szse_unexplained_after_pending_listing_sample": [],
            "szse_unexplained_after_pending_listing_code_set_sha256": "",
            "szse_expected_current_after_pending_listing_count": 0,
            "szse_expected_current_code_set_sha256": "",
            "szse_tdx_current_code_set_sha256": "",
            "szse_current_set_equality_holds": False,
            "pending_listing_status_source_verified": False,
            "pending_listing_status": "PENDING_LISTING_STATUS_INCOMPLETE",
            "pending_listing_error": "No verified pending-listing release is installed",
            "pending_listing_protocol_version": "",
            "pending_listing_expected_protocol_version": PENDING_LISTING_PROTOCOL_VERSION,
            "pending_listing_manifest_sha256": "",
            "pending_listing_logical_content_sha256": "",
            "pending_listing_raw_hashes": {},
            "pending_listing_raw_source_count": 0,
            "pending_listing_official_code_count": 0,
            "pending_listing_admitted_code_set_sha256": "",
            "pending_listing_retrieved_at": "",
            "pending_listing_earliest_source_retrieved_at": "",
            "pending_listing_latest_source_retrieved_at": "",
            "pending_listing_validation_now": "",
            "pending_listing_as_of": "",
            "pending_listing_explained_sse_count": 0,
            "pending_listing_explained_sse_code_set_sha256": "",
            "pending_listing_explained_szse_count": 0,
            "pending_listing_explained_szse_code_set_sha256": "",
            "pending_listing_missing_from_tdx_count": 0,
            "pending_listing_missing_from_tdx_sample": [],
            "pending_listing_duplicate_sse_normal_active_count": 0,
            "pending_listing_duplicate_sse_risk_warning_count": 0,
            "pending_listing_duplicate_szse_normal_active_count": 0,
            "pending_listing_is_current_reconciliation_only": True,
            "pending_listing_contributes_historical_intervals": False,
            "pending_listing_contributes_trading_eligibility": False,
            "tdx_active_snapshot_observed_at": "",
            "tdx_active_snapshot_code_set_sha256": "",
            "tdx_active_snapshot_caller_retrieved_at_accepted": False,
            "active_reconciliation_status": "SOURCE_INCOMPLETE",
        },
        "promotion_blocked": True,
    }


def load_historical_universe_master_gate(runtime_dir: Path) -> dict[str, Any]:
    """Load and cryptographically verify the current research-data gate."""

    return HistoricalSecurityMasterStore(Path(runtime_dir) / "security_master").load_gate()


def publish_historical_security_master(
    current_observation_manifest: str,
) -> dict[str, Any]:
    """Publish the production master from one immutable observation digest.

    The public surface intentionally accepts no paths, child manifests, caller
    summaries, or security code sets.  It first cold-replays the observation
    from the policy-bound store, derives the exact TDX set and observation time
    from that replay, then obtains every remaining admitted source through its
    fixed production root or read-only official adapter.
    """

    digest = str(current_observation_manifest or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HistoricalSecurityMasterBlockedError(
            "security-master publication accepts only one observation manifest SHA-256"
        )
    observation, _metadata = _normalize_current_observation_reference(
        digest,
        store=None,
        require_current=True,
    )
    retrieved_at = _current_wall_clock().isoformat()

    risk_cas = SSERiskWarningRawCAS(SSE_RISK_WARNING_STORE_ROOT)
    risk_store = SSERiskWarningManifestStore(risk_cas)
    risk_source = SSERiskWarningSourceClient(cas=risk_cas)
    try:
        risk_artifact = risk_source.fetch_current(retrieved_at=retrieved_at)
        risk_reference = risk_store.seal(risk_artifact)
    except (OSError, SSERiskWarningSourceBlockedError) as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"SSE risk-warning production evidence failed closed: {exc}"
        ) from exc

    transition_store = SSERiskWarningTransitionManifestStore(
        SSERiskWarningTransitionCAS(SSE_RISK_WARNING_TRANSITION_STORE_ROOT)
    )
    active_interval_cas = SSERiskWarningActiveIntervalsCAS(
        SSE_RISK_WARNING_ACTIVE_INTERVALS_STORE_ROOT
    )
    active_interval_store = SSERiskWarningActiveIntervalsManifestStore(
        active_interval_cas,
        risk_warning_store=risk_store,
        transition_store=transition_store,
    )
    active_interval_source = SSERiskWarningActiveIntervalsClient(
        cas=active_interval_cas
    )
    try:
        transition_store.replay(SSE_RISK_WARNING_TRANSITION_MANIFEST_SHA256)
        active_interval_artifact = active_interval_source.fetch_current(
            risk_warning_manifest_sha256=risk_reference.manifest_sha256,
            risk_warning_store=risk_store,
            transition_manifest_sha256=(
                SSE_RISK_WARNING_TRANSITION_MANIFEST_SHA256
            ),
            transition_store=transition_store,
            retrieved_at=retrieved_at,
        )
        active_interval_reference = active_interval_store.seal(
            active_interval_artifact
        )
    except (
        OSError,
        SSERiskWarningTransitionBlockedError,
        SSERiskWarningActiveIntervalsBlockedError,
    ) as exc:
        raise HistoricalSecurityMasterBlockedError(
            "SSE risk-warning listing-interval production evidence failed closed: "
            f"{exc}"
        ) from exc

    code_change_cas = SZSEDisclosureCAS(SZSE_CODE_CHANGE_STORE_ROOT)
    code_change_source = SZSECodeChangeClient(cas=code_change_cas)
    try:
        code_change_artifact = code_change_source.fetch_primary(
            retrieved_at=retrieved_at,
            expected_sha256=SZSE_CODE_CHANGE_RAW_PDF_SHA256,
        )
    except (OSError, SZSECodeChangeBlockedError) as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"SZSE code-change production evidence failed closed: {exc}"
        ) from exc
    if (
        not code_change_artifact.ready
        or code_change_artifact.status != SZSE_CODE_CHANGE_ADMITTED
    ):
        raise HistoricalSecurityMasterBlockedError(
            "SZSE code-change production evidence is not independently admitted"
        )

    builder = HistoricalSecurityMasterBuilder(
        HistoricalSecurityMasterStore(HISTORICAL_SECURITY_MASTER_STORE_ROOT)
    )
    try:
        result = builder.fetch_and_build(
            tdx_active_codes=observation.tdx_a_share.codes,
            retrieved_at=retrieved_at,
            szse_code_change_artifacts=(code_change_artifact,),
            bse_termination_event_manifest_sha256=(
                BSE_TERMINATION_EVENT_MANIFEST_SHA256
            ),
            sse_risk_warning_manifest=risk_reference.manifest_sha256,
            sse_risk_warning_store=risk_store,
            sse_risk_warning_active_intervals_manifest=(
                active_interval_reference.manifest_sha256
            ),
            sse_risk_warning_active_intervals_store=active_interval_store,
            current_observation_manifest=digest,
            current_observation_store=None,
        )
    except HistoricalSecurityMasterBlockedError:
        raise
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HistoricalSecurityMasterBlockedError(
            f"security master build or pointer commit failed closed: {exc}"
        ) from exc
    if result.get("published") is not True:
        gate = dict(result.get("gate") or {})
        raise HistoricalSecurityMasterBlockedError(
            "security master quality gate rejected publication: "
            f"{gate.get('status') or 'UNKNOWN'}: "
            f"{gate.get('detail') or 'no detail'}; "
            f"audit_manifest={result.get('manifest_hash') or ''}"
        )
    return result


class OfficialSecurityMasterClient:
    """GET-only adapters for official exchange artifacts."""

    def __init__(self, *, timeout_seconds: float = 30.0, session: requests.Session | None = None):
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def fetch(
        self,
        *,
        retrieved_at: str | None = None,
        include_active: bool = True,
    ) -> tuple[ParsedOfficialSource, ...]:
        retrieved = _normalized_retrieved_at(retrieved_at)
        sse = self._get(
            SSE_DELIST_API_URL,
            expected_host="query.sse.com.cn",
            expected_content=("application/json", "text/json", "text/plain"),
            headers={"Referer": SSE_DELIST_PAGE_URL},
        )
        szse = self._get(
            SZSE_DELIST_XLSX_URL,
            expected_host="www.szse.cn",
            expected_content=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/octet-stream",
            ),
        )
        bse = self._get(
            BSE_CODE_MAPPING_URL,
            expected_host="www.bse.cn",
            expected_content=("text/html",),
        )
        sources: list[ParsedOfficialSource] = [
            parse_sse_delist_json(sse, retrieved_at=retrieved),
            parse_szse_delist_xlsx(szse, retrieved_at=retrieved),
            parse_bse_code_mapping_html(bse, retrieved_at=retrieved),
        ]
        if include_active:
            szse_active = self._get(
                SZSE_ACTIVE_XLSX_URL,
                expected_host="www.szse.cn",
                expected_content=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "application/octet-stream",
                ),
                headers={"Referer": SZSE_ACTIVE_PAGE_URL},
            )
            sources.extend(
                (
                    self._fetch_sse_active(retrieved_at=retrieved),
                    parse_szse_active_xlsx(szse_active, retrieved_at=retrieved),
                )
            )
        return tuple(sources)

    def _fetch_sse_active(self, *, retrieved_at: str) -> ParsedOfficialSource:
        first_url = _sse_active_page_url(SSE_ACTIVE_API_URL, 1)
        first_content = self._get(
            first_url,
            expected_host="query.sse.com.cn",
            expected_content=("application/json", "text/json", "text/plain"),
            headers={"Referer": SSE_ACTIVE_PAGE_URL},
        )
        first_no, page_count, total, _first_rows = _decode_sse_active_page(
            first_content,
            label="SSE active page 1",
        )
        if first_no != 1:
            raise HistoricalSecurityMasterBlockedError(
                f"SSE active first response has page number {first_no}"
            )
        if page_count > 1000 or total > 1_000_000:
            raise HistoricalSecurityMasterBlockedError(
                "SSE active pagination exceeds the admitted safety bounds"
            )
        if page_count == 1:
            return parse_sse_active_json(
                first_content,
                source_url=SSE_ACTIVE_API_URL,
                retrieved_at=retrieved_at,
            )
        pages: list[tuple[str, bytes]] = [(first_url, first_content)]
        for expected_page in range(2, page_count + 1):
            request_url = _sse_active_page_url(SSE_ACTIVE_API_URL, expected_page)
            content = self._get(
                request_url,
                expected_host="query.sse.com.cn",
                expected_content=("application/json", "text/json", "text/plain"),
                headers={"Referer": SSE_ACTIVE_PAGE_URL},
            )
            page_no, response_page_count, response_total, _rows = (
                _decode_sse_active_page(
                    content,
                    label=f"SSE active page {expected_page}",
                )
            )
            if page_no != expected_page:
                raise HistoricalSecurityMasterBlockedError(
                    "SSE active page sequence is discontinuous: "
                    f"received {page_no}, expected {expected_page}"
                )
            if response_page_count != page_count or response_total != total:
                raise HistoricalSecurityMasterBlockedError(
                    f"SSE active pagination metadata drifted on page {expected_page}"
                )
            pages.append((request_url, content))
        bundle = build_sse_active_page_bundle(
            pages,
            source_url=SSE_ACTIVE_API_URL,
        )
        return parse_sse_active_json(
            bundle,
            source_url=SSE_ACTIVE_API_URL,
            retrieved_at=retrieved_at,
        )

    def _get(
        self,
        url: str,
        *,
        expected_host: str,
        expected_content: Sequence[str],
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        request_headers = {"User-Agent": "tdx-research-platform/security-master-v1"}
        request_headers.update(dict(headers or {}))
        response = self.session.get(
            url,
            headers=request_headers,
            timeout=self.timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise HistoricalSecurityMasterBlockedError(
                f"official GET failed closed: {url} -> HTTP {response.status_code}"
            )
        if (urlparse(response.url).hostname or "").lower() != expected_host:
            raise HistoricalSecurityMasterBlockedError("official response host changed")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type not in expected_content:
            raise HistoricalSecurityMasterBlockedError(
                f"official response content type changed: {content_type!r}"
            )
        return bytes(response.content)


class HistoricalSecurityMasterBuilder:
    def __init__(self, store: HistoricalSecurityMasterStore) -> None:
        self.store = store

    def build_from_bytes(
        self,
        *,
        sse_json: bytes,
        szse_xlsx: bytes,
        bse_mapping_html: bytes,
        sse_active_json: bytes | None = None,
        szse_active_xlsx: bytes | None = None,
        tdx_active_codes: Iterable[str],
        retrieved_at: str | None = None,
        expected_sse_szse_overlap: int | None = EXPECTED_SSE_SZSE_OVERLAP,
        additional_records: Sequence[SecurityMasterRecord] = (),
        szse_code_change_artifacts: Sequence[SZSECodeChangeArtifact] = (),
        bse_termination_event_manifest_sha256: str | None = None,
        sse_risk_warning_manifest: (
            str | SSERiskWarningManifestReference | None
        ) = None,
        sse_risk_warning_store: SSERiskWarningManifestStore | None = None,
        pending_listing_manifest: (
            str | PendingListingManifestReference | None
        ) = None,
        pending_listing_store: PendingListingManifestStore | None = None,
        pending_listing_validation_now: datetime | str | None = None,
        pending_listing_as_of: datetime | str | None = None,
        bse_current_delisting_manifest: (
            str | BSECurrentDelistingManifestReference | None
        ) = None,
        bse_current_delisting_store: BSECurrentDelistingManifestStore | None = None,
        bse_current_delisting_validation_now: datetime | str | None = None,
        bse_current_delisting_as_of: datetime | str | None = None,
        current_observation_manifest: (
            str | ObservationManifestReference | None
        ) = None,
        current_observation_store: SecurityMasterObservationStore | None = None,
        sse_risk_warning_active_intervals_manifest: (
            str | SSERiskWarningActiveIntervalsManifestReference | None
        ) = None,
        sse_risk_warning_active_intervals_store: (
            SSERiskWarningActiveIntervalsManifestStore | None
        ) = None,
    ) -> dict[str, Any]:
        retrieved = _normalized_retrieved_at(retrieved_at)
        parsed_sources: list[ParsedOfficialSource] = [
            parse_sse_delist_json(sse_json, retrieved_at=retrieved),
            parse_szse_delist_xlsx(szse_xlsx, retrieved_at=retrieved),
            parse_bse_code_mapping_html(bse_mapping_html, retrieved_at=retrieved),
        ]
        if sse_active_json is not None:
            parsed_sources.append(
                parse_sse_active_json(sse_active_json, retrieved_at=retrieved)
            )
        if szse_active_xlsx is not None:
            parsed_sources.append(
                parse_szse_active_xlsx(szse_active_xlsx, retrieved_at=retrieved)
            )
        sources: tuple[ParsedOfficialSource, ...] = tuple(parsed_sources)
        records = tuple(record for source in sources for record in source.records) + tuple(
            additional_records
        )
        records, sources = integrate_szse_code_change_artifacts(
            records,
            sources,
            szse_code_change_artifacts,
        )
        if bse_termination_event_manifest_sha256 is not None:
            records, sources = integrate_bse_termination_event_manifest(
                records,
                sources,
                bse_termination_event_manifest_sha256,
            )
        if (
            bse_current_delisting_manifest is not None
            and current_observation_manifest is None
        ):
            records, sources = integrate_bse_current_delisting_manifest(
                records,
                sources,
                bse_current_delisting_manifest,
                store=bse_current_delisting_store,
                validation_now=bse_current_delisting_validation_now,
                as_of=bse_current_delisting_as_of,
            )
        elif current_observation_manifest is not None:
            observation_batch, _observation_metadata = (
                _normalize_current_observation_reference(
                    current_observation_manifest,
                    store=current_observation_store,
                    require_current=True,
                )
            )
            records, sources = _replay_observation_bse_current_delisting_source(
                records,
                sources,
                batch=observation_batch,
            )
        records, sources, _active_interval_artifact, _active_interval_digest = (
            _replay_and_materialize_sse_risk_warning_active_intervals(
                records=records,
                sources=sources,
                manifest=sse_risk_warning_active_intervals_manifest,
                store=sse_risk_warning_active_intervals_store,
            )
        )
        active_codes = tuple(tdx_active_codes)
        quality = build_quality_report(
            records,
            sources,
            active_codes,
            expected_sse_szse_overlap=expected_sse_szse_overlap,
            szse_code_change_artifacts=szse_code_change_artifacts,
            sse_risk_warning_manifest=sse_risk_warning_manifest,
            sse_risk_warning_store=sse_risk_warning_store,
            pending_listing_manifest=pending_listing_manifest,
            pending_listing_store=pending_listing_store,
            pending_listing_validation_now=pending_listing_validation_now,
            pending_listing_as_of=pending_listing_as_of,
            bse_current_delisting_manifest=bse_current_delisting_manifest,
            bse_current_delisting_store=bse_current_delisting_store,
            bse_current_delisting_validation_now=(
                bse_current_delisting_validation_now
            ),
            bse_current_delisting_as_of=bse_current_delisting_as_of,
            current_observation_manifest=current_observation_manifest,
            current_observation_store=current_observation_store,
            sse_risk_warning_active_intervals_manifest=(
                sse_risk_warning_active_intervals_manifest
            ),
            sse_risk_warning_active_intervals_store=(
                sse_risk_warning_active_intervals_store
            ),
        )
        return self.store.publish(
            sources=sources,
            records=records,
            quality_report=quality,
            tdx_active_codes=active_codes,
            current_observation_manifest=current_observation_manifest,
            current_observation_store=current_observation_store,
        )

    def fetch_and_build(
        self,
        *,
        tdx_active_codes: Iterable[str],
        client: OfficialSecurityMasterClient | None = None,
        retrieved_at: str | None = None,
        szse_code_change_artifacts: Sequence[SZSECodeChangeArtifact] = (),
        bse_termination_event_manifest_sha256: str | None = None,
        sse_risk_warning_manifest: (
            str | SSERiskWarningManifestReference | None
        ) = None,
        sse_risk_warning_store: SSERiskWarningManifestStore | None = None,
        pending_listing_manifest: (
            str | PendingListingManifestReference | None
        ) = None,
        pending_listing_store: PendingListingManifestStore | None = None,
        pending_listing_validation_now: datetime | str | None = None,
        pending_listing_as_of: datetime | str | None = None,
        bse_current_delisting_manifest: (
            str | BSECurrentDelistingManifestReference | None
        ) = None,
        bse_current_delisting_store: BSECurrentDelistingManifestStore | None = None,
        bse_current_delisting_validation_now: datetime | str | None = None,
        bse_current_delisting_as_of: datetime | str | None = None,
        current_observation_manifest: (
            str | ObservationManifestReference | None
        ) = None,
        current_observation_store: SecurityMasterObservationStore | None = None,
        sse_risk_warning_active_intervals_manifest: (
            str | SSERiskWarningActiveIntervalsManifestReference | None
        ) = None,
        sse_risk_warning_active_intervals_store: (
            SSERiskWarningActiveIntervalsManifestStore | None
        ) = None,
    ) -> dict[str, Any]:
        retrieved = _normalized_retrieved_at(retrieved_at)
        sources = (client or OfficialSecurityMasterClient()).fetch(
            retrieved_at=retrieved,
            include_active=True,
        )
        records = tuple(record for source in sources for record in source.records)
        records, sources = integrate_szse_code_change_artifacts(
            records,
            sources,
            szse_code_change_artifacts,
        )
        if bse_termination_event_manifest_sha256 is not None:
            records, sources = integrate_bse_termination_event_manifest(
                records,
                sources,
                bse_termination_event_manifest_sha256,
            )
        if (
            bse_current_delisting_manifest is not None
            and current_observation_manifest is None
        ):
            records, sources = integrate_bse_current_delisting_manifest(
                records,
                sources,
                bse_current_delisting_manifest,
                store=bse_current_delisting_store,
                validation_now=bse_current_delisting_validation_now,
                as_of=bse_current_delisting_as_of,
            )
        elif current_observation_manifest is not None:
            observation_batch, _observation_metadata = (
                _normalize_current_observation_reference(
                    current_observation_manifest,
                    store=current_observation_store,
                    require_current=True,
                )
            )
            records, sources = _replay_observation_bse_current_delisting_source(
                records,
                sources,
                batch=observation_batch,
            )
        records, sources, _active_interval_artifact, _active_interval_digest = (
            _replay_and_materialize_sse_risk_warning_active_intervals(
                records=records,
                sources=sources,
                manifest=sse_risk_warning_active_intervals_manifest,
                store=sse_risk_warning_active_intervals_store,
            )
        )
        active_codes = tuple(tdx_active_codes)
        quality = build_quality_report(
            records,
            sources,
            active_codes,
            expected_sse_szse_overlap=EXPECTED_SSE_SZSE_OVERLAP,
            szse_code_change_artifacts=szse_code_change_artifacts,
            sse_risk_warning_manifest=sse_risk_warning_manifest,
            sse_risk_warning_store=sse_risk_warning_store,
            pending_listing_manifest=pending_listing_manifest,
            pending_listing_store=pending_listing_store,
            pending_listing_validation_now=pending_listing_validation_now,
            pending_listing_as_of=pending_listing_as_of,
            bse_current_delisting_manifest=bse_current_delisting_manifest,
            bse_current_delisting_store=bse_current_delisting_store,
            bse_current_delisting_validation_now=(
                bse_current_delisting_validation_now
            ),
            bse_current_delisting_as_of=bse_current_delisting_as_of,
            current_observation_manifest=current_observation_manifest,
            current_observation_store=current_observation_store,
            sse_risk_warning_active_intervals_manifest=(
                sse_risk_warning_active_intervals_manifest
            ),
            sse_risk_warning_active_intervals_store=(
                sse_risk_warning_active_intervals_store
            ),
        )
        return self.store.publish(
            sources=sources,
            records=records,
            quality_report=quality,
            tdx_active_codes=active_codes,
            current_observation_manifest=current_observation_manifest,
            current_observation_store=current_observation_store,
        )
