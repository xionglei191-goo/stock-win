from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit
from xml.etree import ElementTree

import requests


PROTOCOL_VERSION = "cn-pending-listing-official-evidence-v2"
SOURCE_CONTRACT_ADMITTED = "SOURCE_CONTRACT_ADMITTED"
SOURCE_REJECTED = "SOURCE_REJECTED"
PENDING_LISTING_EVIDENCE_COMPLETE = "PENDING_LISTING_EVIDENCE_COMPLETE"

SSE_IPO_PAGE_URL = "https://www.sse.com.cn/ipo/listing/"
SSE_IPO_ENDPOINT = "https://query.sse.com.cn/commonQuery.do"
SSE_IPO_SQL_ID = "COMMON_SSE_IPO_IPO_LIST_L"
SSE_JSONP_CALLBACK = "jsonpCallbackPendingListing"
CNINFO_STOCK_MASTER_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_IPO_PAGE_URL = "https://www.cninfo.com.cn/eipo/index.html"
CNINFO_CURRENT_IPO_URL = "https://www.cninfo.com.cn/neweipo/index/ipoListQuery"
CNINFO_IPO_ANNOUNCEMENT_ENDPOINT = (
    "https://www.cninfo.com.cn/neweipo/stock/getStockAnnouncementList"
)
SZSE_ACTIVE_PAGE_URL = "https://www.szse.cn/market/product/stock/list/index.html"
SZSE_ACTIVE_XLSX_URL = (
    "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110"
)

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_XLSX_BYTES = 20 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MIN_CNINFO_MASTER_ROWS = 1_000
MIN_SZSE_ACTIVE_ROWS = 2_000
MAX_CURRENT_EVIDENCE_AGE = timedelta(minutes=15)
MAX_CAPTURE_SPAN = timedelta(minutes=10)
MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=5)

JSON_MEDIA_TYPE = "application/json"
PDF_MEDIA_TYPE = "application/pdf"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

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

SSE_TOP_LEVEL_FIELDS = frozenset(
    {
        "actionErrors",
        "actionMessages",
        "fieldErrors",
        "isPagination",
        "jsonCallBack",
        "locale",
        "pageHelp",
        "pageNo",
        "pageSize",
        "queryDate",
        "result",
        "securityCode",
        "sqlId",
        "texts",
        "type",
        "validateCode",
    }
)
SSE_PAGE_FIELDS = frozenset(
    {
        "beginPage",
        "cacheSize",
        "data",
        "endDate",
        "endPage",
        "objectResult",
        "pageCount",
        "pageNo",
        "pageSize",
        "pageSizeWithOutLimit",
        "searchDate",
        "sort",
        "startDate",
        "total",
    }
)
SSE_ROW_FIELDS = frozenset(
    {
        "ACTUAL_FUNDS_RAISED",
        "ALLOTMENT_SHARES",
        "ANNOUNCEMENT_URL",
        "ANNOUNCE_SUCC_RATE_RS_DATE",
        "COMPANY_FULL_NAME",
        "IPO_OVERALL_STATUS",
        "ISSUANCE_PRICE_EARNINGS_RATIO",
        "ISSUE_PRICE",
        "LISTED_DATE",
        "LOT_WINNING_RATE",
        "NUM",
        "OFFLINE_CIRCULATION",
        "OFFLINE_ISSUANCE_END_DATE",
        "OFFLINE_ISSUANCE_START_DATE",
        "ONLINE_CIRCULATION",
        "ONLINE_ISSUANCE_DATE",
        "ONLINE_PURCHASE_LIMIT",
        "PAYMENT_END_DATE",
        "PAYMENT_START_DATE",
        "SECURITY_CODE",
        "SECURITY_EXPAND_NAME",
        "SECURITY_NAME",
        "STOCK_TYPE",
        "TOTAL_INITIAL_ISSUE",
        "TOTAL_ISSUED",
    }
)
CNINFO_CURRENT_IPO_ROW_FIELDS = frozenset(
    {
        "obSecCode0007",
        "obSecName0007",
        "f035d0089Date",
        "f008n0089",
        "f013n0089",
        "f003n0089",
        "f043n0089",
        "f042n0089",
        "f050n0089",
        "f108d0089",
        "f007d0007",
        "f001v0116",
        "f035d0089Time",
        "f004n0089",
        "f117n0089",
        "obSeqId",
    }
)
CNINFO_ANNOUNCEMENT_PAGE_FIELDS = frozenset(
    {
        "endRow",
        "hasNextPage",
        "hasPreviousPage",
        "isFirstPage",
        "isLastPage",
        "list",
        "navigateFirstPage",
        "navigateLastPage",
        "navigatePages",
        "navigatepageNums",
        "nextPage",
        "pageNum",
        "pageSize",
        "pages",
        "prePage",
        "size",
        "startRow",
        "total",
    }
)
CNINFO_ANNOUNCEMENT_ROW_FIELDS = frozenset(
    {
        "announcementId",
        "stockId",
        "title",
        "uri",
        "announcementDate",
        "announcementTime",
    }
)
CNINFO_HARD_NEGATIVE_ISSUANCE_MARKERS = (
    "中止发行",
    "暂停发行",
    "暂缓发行",
    "终止发行",
    "延期发行",
    "撤回发行",
    "取消发行",
)


class PendingListingSourceBlockedError(RuntimeError):
    """Official evidence does not satisfy the admitted pending-listing contract."""

    def __init__(self, message: str, *, status: str = SOURCE_REJECTED) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SSEPendingSpec:
    code: str
    name: str
    company_full_name: str

    @property
    def source_id(self) -> str:
        return f"SSE_IPO_{self.code}"

    @property
    def request_url(self) -> str:
        return build_sse_pending_request_url(self.code)


@dataclass(frozen=True)
class SZSEPendingDocumentSpec:
    code: str
    name: str
    company_full_name: str
    org_id: str
    announcement_id: str
    publication_date: str
    document_kind: str
    title_marker: str
    subscription_date: str
    request_url: str
    expected_sha256: str

    @property
    def source_id(self) -> str:
        return f"CNINFO_{self.code}_{self.document_kind}"


SSE_PENDING_SPECS: tuple[SSEPendingSpec, ...] = (
    SSEPendingSpec("688826", "频准激光", "上海频准激光科技股份有限公司"),
    SSEPendingSpec("688835", "高凯技术", "江苏高凯精密流体技术股份有限公司"),
    SSEPendingSpec("688836", "宇树科技", "宇树科技股份有限公司"),
)

SZSE_DOCUMENT_SPECS: tuple[SZSEPendingDocumentSpec, ...] = (
    SZSEPendingDocumentSpec(
        code="301655",
        name="绿控传动",
        company_full_name="苏州绿控传动科技股份有限公司",
        org_id="9900057453",
        announcement_id="1225462253",
        publication_date="2026-08-07",
        document_kind="ISSUE_ANNOUNCEMENT",
        title_marker="首次公开发行股票并在创业板上市发行公告",
        subscription_date="2026-08-10",
        request_url=(
            "https://static.cninfo.com.cn/finalpage/2026-08-07/1225462253.PDF"
        ),
        expected_sha256=(
            "c542031ac3fa72c79783a8e3feae12bb21ec003c24d94f852fbfbc1b79189cdd"
        ),
    ),
    SZSEPendingDocumentSpec(
        code="301688",
        name="格林生物",
        company_full_name="格林生物科技股份有限公司",
        org_id="9900041955",
        announcement_id="1225459014",
        publication_date="2026-08-06",
        document_kind="INITIAL_INQUIRY_ANNOUNCEMENT",
        title_marker="首次公开发行股票并在创业板上市初步询价及推介公告",
        subscription_date="2026-08-20",
        request_url=(
            "https://static.cninfo.com.cn/finalpage/2026-08-06/1225459014.PDF"
        ),
        expected_sha256=(
            "80f9128f1ee53e98c1a1d2940da906cf2b7c115149bc3912de1fe25bfaadf8c6"
        ),
    ),
    SZSEPendingDocumentSpec(
        code="301697",
        name="贝特利",
        company_full_name="苏州市贝特利高分子材料股份有限公司",
        org_id="gfbj0834488",
        announcement_id="1225465495",
        publication_date="2026-08-11",
        document_kind="INITIAL_INQUIRY_ANNOUNCEMENT",
        title_marker="首次公开发行股票并在创业板上市初步询价及推介公告",
        subscription_date="2026-08-19",
        request_url=(
            "https://static.cninfo.com.cn/finalpage/2026-08-11/1225465495.PDF"
        ),
        expected_sha256=(
            "765f3a83bf3c94348fe8e7225e5650914d1086b0ecba81db3179029ac47b614b"
        ),
    ),
)

CNINFO_MASTER_SOURCE_ID = "CNINFO_SZSE_STOCK_MASTER"
CNINFO_CURRENT_IPO_SOURCE_ID = "CNINFO_CURRENT_IPO_LIST"
SZSE_ACTIVE_SOURCE_ID = "SZSE_ACTIVE_A_SHARE_CATALOGUE"


def _cninfo_announcement_source_id(code: str) -> str:
    return f"CNINFO_{code}_CURRENT_IPO_ANNOUNCEMENTS"


def build_cninfo_announcement_request_url(code: str) -> str:
    if not re.fullmatch(r"301\d{3}", str(code)):
        raise PendingListingSourceBlockedError(
            f"invalid SZSE pending IPO announcement code: {code!r}"
        )
    return (
        f"{CNINFO_IPO_ANNOUNCEMENT_ENDPOINT}?"
        + urlencode((("stockCode", str(code)), ("pageNum", "1"), ("pageSize", "100")))
    )


SOURCE_ORDER = tuple(
    [spec.source_id for spec in SSE_PENDING_SPECS]
    + [CNINFO_CURRENT_IPO_SOURCE_ID, CNINFO_MASTER_SOURCE_ID]
    + [_cninfo_announcement_source_id(spec.code) for spec in SZSE_DOCUMENT_SPECS]
    + [spec.source_id for spec in SZSE_DOCUMENT_SPECS]
    + [SZSE_ACTIVE_SOURCE_ID]
)


@dataclass(frozen=True)
class PendingListingSecurity:
    exchange: str
    code: str
    name: str
    company_full_name: str
    status: str
    evidence_kind: str
    issue_milestone_date: str | None
    source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_ids"] = list(self.source_ids)
        return value


@dataclass(frozen=True)
class PendingListingRawEvidence:
    source_id: str
    request_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    http_status: int
    cas_uri: str
    object_path: str
    response_summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["response_summary"] = dict(self.response_summary)
        return value

    def to_manifest_source(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "request_url": self.request_url,
            "method": self.method,
            "retrieved_at": self.retrieved_at,
            "content_sha256": self.content_sha256,
            "byte_count": self.byte_count,
            "content_type": self.content_type,
            "http_status": self.http_status,
            "response_summary": dict(self.response_summary),
        }


@dataclass(frozen=True)
class PendingListingArtifact:
    retrieved_at: str
    securities: tuple[PendingListingSecurity, ...]
    raw_sources: tuple[PendingListingRawEvidence, ...]
    logical_content_sha256: str

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": True,
            "status": SOURCE_CONTRACT_ADMITTED,
            "scope": "CURRENT_ASSIGNED_IPO_CODES_NOT_YET_LISTED",
            "method": "GET",
            "redirects_allowed": False,
            "source_roles": {
                "SSE": (
                    "official IPO issue-in-progress rows with LISTED_DATE='-'"
                ),
                "SZSE": (
                    "presence in CNINFO's complete current IPO issuance list with a "
                    "null listing date, CNINFO legal issuance PDFs with explicit "
                    "code/name binding, a complete current IPO-announcement page "
                    "without a hard suspension/termination marker, CNINFO stock-master "
                    "identity binding, and "
                    "absence from the official SZSE active A-share catalogue"
                ),
            },
            "negative_evidence_rule": (
                "absence from TDX bars is never evidence; only the complete official "
                "SZSE active catalogue may close the SZSE not-listed check"
            ),
            "review_project_excluded": True,
            "historical_interval_evidence": False,
            "trading_eligibility": False,
            "freshness_contract": {
                "consumer_must_validate": True,
                "maximum_age_seconds": int(MAX_CURRENT_EVIDENCE_AGE.total_seconds()),
                "maximum_capture_span_seconds": int(MAX_CAPTURE_SPAN.total_seconds()),
                "maximum_future_clock_skew_seconds": int(
                    MAX_FUTURE_CLOCK_SKEW.total_seconds()
                ),
                "per_source_retrieved_at_manifest_bound": True,
            },
            "audit_only": True,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        codes = sorted(item.code for item in self.securities)
        return {
            "status": PENDING_LISTING_EVIDENCE_COMPLETE,
            "target_count": len(codes),
            "sse_count": sum(item.exchange == "SSE" for item in self.securities),
            "szse_count": sum(item.exchange == "SZSE" for item in self.securities),
            "codes": codes,
            "code_set_sha256": _sha256(_canonical_json_bytes(codes)),
            "raw_source_count": len(self.raw_sources),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "retrieved_at": self.retrieved_at,
            "securities": [item.to_dict() for item in self.securities],
            "raw_sources": [item.to_dict() for item in self.raw_sources],
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class PendingListingManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _SSEParsed:
    security: PendingListingSecurity
    summary: Mapping[str, Any]


@dataclass(frozen=True)
class _PDFParsed:
    security: PendingListingSecurity
    summary: Mapping[str, Any]


class PendingListingRawCAS:
    """Immutable content-addressed storage for the exact official response bytes."""

    def __init__(self, root: Path) -> None:
        self.root = _lexical_absolute_path(Path(root))

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        if not content:
            raise PendingListingSourceBlockedError("refusing to store an empty CAS blob")
        digest = _sha256(content)
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(self.root, path, content)
        persisted = _stable_read_cas_object(self.root, path)
        if persisted != content or _sha256(persisted) != digest:
            raise PendingListingSourceBlockedError("CAS read-back verification failed")
        return digest, path

    def read_blob(self, digest: str) -> tuple[bytes, Path]:
        normalized = str(digest).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise PendingListingSourceBlockedError("invalid CAS SHA-256")
        path = self.root / "sha256" / normalized[:2] / normalized
        content = _stable_read_cas_object(self.root, path)
        if _sha256(content) != normalized:
            raise PendingListingSourceBlockedError(
                f"CAS object hash mismatch: sha256:{normalized}"
            )
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
        response_summary: Mapping[str, Any],
        expected_sha256: str | None = None,
    ) -> PendingListingRawEvidence:
        digest = _verify_sha256(content, expected_sha256, source_id)
        stored, path = self.put_blob(content)
        if stored != digest:
            raise PendingListingSourceBlockedError(
                f"{source_id} CAS digest changed during capture"
            )
        return PendingListingRawEvidence(
            source_id=source_id,
            request_url=request_url,
            method="GET",
            retrieved_at=_normalize_retrieved_at(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            http_status=http_status,
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
            response_summary=dict(response_summary),
        )


class PendingListingManifestStore:
    """Seal and replay the artifact by recomputing every row from raw CAS bytes."""

    def __init__(self, cas: PendingListingRawCAS) -> None:
        if not isinstance(cas, PendingListingRawCAS):
            raise TypeError("cas must be a PendingListingRawCAS")
        self.cas = cas

    def seal(self, artifact: PendingListingArtifact) -> PendingListingManifestReference:
        payload = _manifest_payload(artifact)
        rebuilt = _rebuild_from_manifest_payload(payload, cas=self.cas)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != content:
            raise PendingListingSourceBlockedError(
                "pending-listing artifact is not reproducible from raw CAS bytes"
            )
        digest, path = self.cas.put_blob(content)
        return PendingListingManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def replay(self, manifest_sha256: str) -> PendingListingArtifact:
        content, _path = self.cas.read_blob(manifest_sha256)
        payload = _decode_canonical_json_object(content, "pending-listing manifest")
        if content != _canonical_json_bytes(payload):
            raise PendingListingSourceBlockedError(
                "pending-listing manifest is not canonical JSON"
            )
        rebuilt = _rebuild_from_manifest_payload(payload, cas=self.cas)
        if content != _canonical_json_bytes(_manifest_payload(rebuilt)):
            raise PendingListingSourceBlockedError(
                "pending-listing manifest does not replay exactly"
            )
        return rebuilt


class PendingListingSourceClient:
    """GET-only current pending-IPO evidence collector for the six admitted codes."""

    def __init__(
        self,
        *,
        cas: PendingListingRawCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 45.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cas, PendingListingRawCAS):
            raise TypeError("cas must be a PendingListingRawCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now().astimezone())

    def fetch_current(
        self,
        *,
        expected_hashes: Mapping[str, str] | None = None,
    ) -> PendingListingArtifact:
        pending_hashes = dict(expected_hashes or {})
        unknown = sorted(set(pending_hashes) - set(SOURCE_ORDER))
        if unknown:
            raise PendingListingSourceBlockedError(
                f"unknown expected source hashes: {unknown}"
            )

        evidence: list[PendingListingRawEvidence] = []
        for spec in SSE_PENDING_SPECS:
            raw, content_type, status = self._get(
                source_id=spec.source_id,
                request_url=spec.request_url,
                expected_media_type=JSON_MEDIA_TYPE,
                referer=SSE_IPO_PAGE_URL,
                maximum=MAX_JSON_BYTES,
            )
            expected = pending_hashes.pop(spec.source_id, None)
            parsed = parse_sse_pending_response(
                raw,
                spec=spec,
                expected_sha256=expected,
            )
            evidence.append(
                self.cas.capture(
                    raw,
                    source_id=spec.source_id,
                    request_url=spec.request_url,
                    retrieved_at=self._observed_at(),
                    content_type=content_type,
                    http_status=status,
                    response_summary=parsed.summary,
                    expected_sha256=expected,
                )
            )

        raw, content_type, status = self._get(
            source_id=CNINFO_CURRENT_IPO_SOURCE_ID,
            request_url=CNINFO_CURRENT_IPO_URL,
            expected_media_type=JSON_MEDIA_TYPE,
            referer=CNINFO_IPO_PAGE_URL,
            maximum=MAX_JSON_BYTES,
        )
        expected = pending_hashes.pop(CNINFO_CURRENT_IPO_SOURCE_ID, None)
        _current_rows, current_summary = parse_cninfo_current_ipo_list(
            raw,
            expected_sha256=expected,
        )
        evidence.append(
            self.cas.capture(
                raw,
                source_id=CNINFO_CURRENT_IPO_SOURCE_ID,
                request_url=CNINFO_CURRENT_IPO_URL,
                retrieved_at=self._observed_at(),
                content_type=content_type,
                http_status=status,
                response_summary=current_summary,
                expected_sha256=expected,
            )
        )

        raw, content_type, status = self._get(
            source_id=CNINFO_MASTER_SOURCE_ID,
            request_url=CNINFO_STOCK_MASTER_URL,
            expected_media_type=JSON_MEDIA_TYPE,
            referer="https://www.cninfo.com.cn/",
            maximum=MAX_JSON_BYTES,
        )
        expected = pending_hashes.pop(CNINFO_MASTER_SOURCE_ID, None)
        _bindings, master_summary = parse_cninfo_stock_master(
            raw,
            expected_sha256=expected,
        )
        evidence.append(
            self.cas.capture(
                raw,
                source_id=CNINFO_MASTER_SOURCE_ID,
                request_url=CNINFO_STOCK_MASTER_URL,
                retrieved_at=self._observed_at(),
                content_type=content_type,
                http_status=status,
                response_summary=master_summary,
                expected_sha256=expected,
            )
        )

        for spec in SZSE_DOCUMENT_SPECS:
            source_id = _cninfo_announcement_source_id(spec.code)
            request_url = build_cninfo_announcement_request_url(spec.code)
            raw, content_type, status = self._get(
                source_id=source_id,
                request_url=request_url,
                expected_media_type=JSON_MEDIA_TYPE,
                referer=CNINFO_IPO_PAGE_URL,
                maximum=MAX_JSON_BYTES,
            )
            expected = pending_hashes.pop(source_id, None)
            _rows, announcement_summary = parse_cninfo_ipo_announcements(
                raw,
                spec=spec,
                expected_sha256=expected,
            )
            evidence.append(
                self.cas.capture(
                    raw,
                    source_id=source_id,
                    request_url=request_url,
                    retrieved_at=self._observed_at(),
                    content_type=content_type,
                    http_status=status,
                    response_summary=announcement_summary,
                    expected_sha256=expected,
                )
            )

        for spec in SZSE_DOCUMENT_SPECS:
            supplied_hash = pending_hashes.pop(spec.source_id, None)
            if supplied_hash is not None and supplied_hash.lower() != spec.expected_sha256:
                raise PendingListingSourceBlockedError(
                    f"{spec.source_id} expected hash conflicts with the admitted PDF"
                )
            raw, content_type, status = self._get(
                source_id=spec.source_id,
                request_url=spec.request_url,
                expected_media_type=PDF_MEDIA_TYPE,
                referer="https://www.cninfo.com.cn/",
                maximum=MAX_PDF_BYTES,
            )
            parsed = parse_cninfo_issuance_pdf(raw, spec=spec)
            evidence.append(
                self.cas.capture(
                    raw,
                    source_id=spec.source_id,
                    request_url=spec.request_url,
                    retrieved_at=self._observed_at(),
                    content_type=content_type,
                    http_status=status,
                    response_summary=parsed.summary,
                    expected_sha256=spec.expected_sha256,
                )
            )

        raw, content_type, status = self._get(
            source_id=SZSE_ACTIVE_SOURCE_ID,
            request_url=SZSE_ACTIVE_XLSX_URL,
            expected_media_type=XLSX_MEDIA_TYPE,
            referer=SZSE_ACTIVE_PAGE_URL,
            maximum=MAX_XLSX_BYTES,
        )
        expected = pending_hashes.pop(SZSE_ACTIVE_SOURCE_ID, None)
        _active_codes, active_summary = parse_szse_active_catalogue(
            raw,
            expected_sha256=expected,
        )
        evidence.append(
            self.cas.capture(
                raw,
                source_id=SZSE_ACTIVE_SOURCE_ID,
                request_url=SZSE_ACTIVE_XLSX_URL,
                retrieved_at=self._observed_at(),
                content_type=content_type,
                http_status=status,
                response_summary=active_summary,
                expected_sha256=expected,
            )
        )
        if pending_hashes:
            raise PendingListingSourceBlockedError(
                f"unused expected source hashes: {sorted(pending_hashes)}"
            )

        descriptors = [item.to_manifest_source() for item in evidence]
        artifact = _rebuild_artifact_from_sources(
            retrieved_at=max(item.retrieved_at for item in evidence),
            source_values=descriptors,
            cas=self.cas,
        )
        validate_pending_listing_freshness(artifact, now=self._clock())
        return artifact

    def _observed_at(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise PendingListingSourceBlockedError("clock must return a datetime")
        return _normalize_retrieved_at(value)

    def _get(
        self,
        *,
        source_id: str,
        request_url: str,
        expected_media_type: str,
        referer: str,
        maximum: int,
    ) -> tuple[bytes, str, int]:
        _validate_source_url(source_id, request_url)
        try:
            response = self.session.get(
                request_url,
                headers={
                    "Accept": expected_media_type,
                    "Referer": referer,
                    "User-Agent": "tdx-research-platform/pending-listing-v2",
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PendingListingSourceBlockedError(
                f"{source_id} request failed: {exc}", status="SOURCE_UNAVAILABLE"
            ) from exc
        if response.status_code != 200:
            raise PendingListingSourceBlockedError(
                f"{source_id} HTTP status is {response.status_code}"
            )
        if str(response.url) != request_url:
            raise PendingListingSourceBlockedError(
                f"{source_id} response URL changed or redirected"
            )
        content_type = _media_type(response.headers.get("Content-Type"))
        if content_type != expected_media_type:
            raise PendingListingSourceBlockedError(
                f"{source_id} content type changed: {content_type!r}"
            )
        raw = response.content
        if not isinstance(raw, bytes):
            raw = bytes(raw)
        if not raw or len(raw) > maximum:
            raise PendingListingSourceBlockedError(
                f"{source_id} response is empty or oversized"
            )
        return raw, content_type, int(response.status_code)


def build_sse_pending_request_url(code: str) -> str:
    if not re.fullmatch(r"688\d{3}", str(code)):
        raise PendingListingSourceBlockedError(f"invalid SSE pending IPO code: {code!r}")
    query = urlencode(
        (
            ("jsonCallBack", SSE_JSONP_CALLBACK),
            ("isPagination", "true"),
            ("sqlId", SSE_IPO_SQL_ID),
            ("isIssue", "1"),
            ("isListing", ""),
            ("isNotStatus", "99"),
            ("stockCode", str(code)),
            ("pageHelp.pageSize", "25"),
            ("pageHelp.pageNo", "1"),
            ("pageHelp.beginPage", "1"),
            ("pageHelp.endPage", "1"),
        )
    )
    return f"{SSE_IPO_ENDPOINT}?{query}"


def parse_sse_pending_response(
    raw: bytes,
    *,
    spec: SSEPendingSpec,
    expected_sha256: str | None = None,
) -> _SSEParsed:
    _verify_sha256(raw, expected_sha256, spec.source_id)
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} response is empty or oversized"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} response is not UTF-8"
        ) from exc
    match = re.fullmatch(
        rf"{re.escape(SSE_JSONP_CALLBACK)}\((.*)\)", text, flags=re.DOTALL
    )
    if match is None:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} JSONP callback contract changed"
        )
    value = _loads_json_no_duplicates(
        match.group(1), f"{spec.source_id} JSONP payload"
    )
    if not isinstance(value, dict) or set(value) != SSE_TOP_LEVEL_FIELDS:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} top-level schema drift detected"
        )
    if (
        value["actionErrors"] != []
        or value["actionMessages"] != []
        or value["fieldErrors"] != {}
        or value["isPagination"] != "true"
        or value["jsonCallBack"] != SSE_JSONP_CALLBACK
        or value["sqlId"] != SSE_IPO_SQL_ID
        or value["locale"] != "en"
        or value["pageNo"] is not None
        or value["pageSize"] is not None
        or value["securityCode"] != ""
        or value["type"] != ""
        or value["validateCode"] != ""
    ):
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} response envelope changed"
        )
    page = value["pageHelp"]
    result = value["result"]
    if not isinstance(page, dict) or set(page) != SSE_PAGE_FIELDS:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} pagination schema drift detected"
        )
    if not isinstance(result, list) or len(result) != 1 or page["data"] != result:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} query did not return exactly one identical row"
        )
    if (
        _strict_int(page["beginPage"], "SSE beginPage") != 1
        or _strict_int(page["endPage"], "SSE endPage") != 1
        or _strict_int(page["pageCount"], "SSE pageCount") != 1
        or _strict_int(page["pageNo"], "SSE pageNo") != 1
        or _strict_int(page["pageSize"], "SSE pageSize") != 25
        or _strict_int(page["pageSizeWithOutLimit"], "SSE pageSizeWithOutLimit")
        != 25
        or _strict_int(page["total"], "SSE total") != 1
    ):
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} pagination is not a complete one-row result"
        )
    row = result[0]
    if not isinstance(row, dict) or set(row) != SSE_ROW_FIELDS:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} row schema drift detected"
        )
    if (
        row["SECURITY_CODE"] != spec.code
        or row["SECURITY_NAME"] != spec.name
        or row["SECURITY_EXPAND_NAME"] != spec.name
        or row["COMPANY_FULL_NAME"] != spec.company_full_name
        or row["STOCK_TYPE"] != "2"
        or row["IPO_OVERALL_STATUS"] != "0"
        or row["LISTED_DATE"] != "-"
        or row["NUM"] != "1"
    ):
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} is not the admitted assigned-but-unlisted IPO row"
        )
    issue_date = _optional_iso_date(row["ONLINE_ISSUANCE_DATE"])
    security = PendingListingSecurity(
        exchange="SSE",
        code=f"{spec.code}.SH",
        name=spec.name,
        company_full_name=spec.company_full_name,
        status="ISSUANCE_IN_PROGRESS_NOT_LISTED",
        evidence_kind="SSE_IPO_ISSUE_IN_PROGRESS_ROW",
        issue_milestone_date=issue_date,
        source_ids=(spec.source_id,),
    )
    summary = {
        "code": spec.code,
        "name": spec.name,
        "company_full_name": spec.company_full_name,
        "ipo_overall_status": "0",
        "listed_date": "-",
        "online_issuance_date": issue_date or "-",
        "result_count": 1,
    }
    return _SSEParsed(security=security, summary=summary)


def parse_cninfo_stock_master(
    raw: bytes,
    *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, dict[str, str]], Mapping[str, Any]]:
    _verify_sha256(raw, expected_sha256, CNINFO_MASTER_SOURCE_ID)
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise PendingListingSourceBlockedError(
            "CNINFO stock master is empty or oversized"
        )
    try:
        value = _loads_json_no_duplicates(
            raw.decode("utf-8"), "CNINFO stock master"
        )
    except UnicodeDecodeError as exc:
        raise PendingListingSourceBlockedError(
            "CNINFO stock master is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"stockList"}:
        raise PendingListingSourceBlockedError("CNINFO stock-master schema drift")
    rows = value["stockList"]
    if not isinstance(rows, list) or len(rows) < MIN_CNINFO_MASTER_ROWS:
        raise PendingListingSourceBlockedError(
            "CNINFO stock-master coverage is implausibly small"
        )
    expected_fields = {"category", "code", "orgId", "pinyin", "zwjc"}
    seen: set[str] = set()
    matches: dict[str, dict[str, str]] = {}
    targets = {spec.code: spec for spec in SZSE_DOCUMENT_SPECS}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise PendingListingSourceBlockedError("CNINFO stock-master row schema drift")
        if any(not isinstance(row[field], str) for field in expected_fields):
            raise PendingListingSourceBlockedError(
                "CNINFO stock-master row contains non-string fields"
            )
        code = row["code"].strip()
        if not re.fullmatch(r"\d{6}", code):
            raise PendingListingSourceBlockedError(
                f"CNINFO stock master contains invalid code: {code!r}"
            )
        if code in seen:
            raise PendingListingSourceBlockedError(
                f"CNINFO stock master contains duplicate code: {code}"
            )
        seen.add(code)
        if code in targets:
            spec = targets[code]
            normalized = {field: row[field].strip() for field in expected_fields}
            if (
                normalized["category"] != "A股"
                or normalized["orgId"] != spec.org_id
                or normalized["zwjc"] != spec.name
                or not normalized["pinyin"]
            ):
                raise PendingListingSourceBlockedError(
                    f"CNINFO stock-master identity mismatch for {code}"
                )
            matches[code] = normalized
    if set(matches) != set(targets):
        raise PendingListingSourceBlockedError(
            "CNINFO stock master does not bind every admitted SZSE code"
        )
    bindings = [
        {
            "code": code,
            "name": matches[code]["zwjc"],
            "org_id": matches[code]["orgId"],
        }
        for code in sorted(matches)
    ]
    summary = {
        "row_count": len(rows),
        "target_bindings": bindings,
        "target_binding_sha256": _sha256(_canonical_json_bytes(bindings)),
    }
    return matches, summary


def parse_cninfo_current_ipo_list(
    raw: bytes,
    *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, dict[str, Any]], Mapping[str, Any]]:
    """Parse CNINFO's complete current issuance list, excluding listed/cancelled rows.

    This endpoint is the data source rendered by the official IPO information-zone
    home page.  A target must still be present and its listing-date field must be
    null.  Consequently an old PDF cannot keep a withdrawn or subsequently listed
    security classified as currently pending.
    """

    _verify_sha256(raw, expected_sha256, CNINFO_CURRENT_IPO_SOURCE_ID)
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise PendingListingSourceBlockedError(
            "CNINFO current IPO list is empty or oversized"
        )
    try:
        value = _loads_json_no_duplicates(raw.decode("utf-8"), "CNINFO current IPO list")
    except UnicodeDecodeError as exc:
        raise PendingListingSourceBlockedError(
            "CNINFO current IPO list is not UTF-8"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"code", "message", "data"}:
        raise PendingListingSourceBlockedError(
            "CNINFO current IPO-list response schema drift"
        )
    if value["code"] != 200 or value["message"] != "执行成功":
        raise PendingListingSourceBlockedError(
            "CNINFO current IPO-list response reports failure"
        )
    rows = value["data"]
    if not isinstance(rows, list) or not rows:
        raise PendingListingSourceBlockedError(
            "CNINFO current IPO list contains no rows"
        )
    targets = {spec.code: spec for spec in SZSE_DOCUMENT_SPECS}
    seen: set[str] = set()
    matches: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != CNINFO_CURRENT_IPO_ROW_FIELDS:
            raise PendingListingSourceBlockedError(
                "CNINFO current IPO-list row schema drift"
            )
        code = row["obSecCode0007"]
        name = row["obSecName0007"]
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
            raise PendingListingSourceBlockedError(
                f"CNINFO current IPO list contains invalid code: {code!r}"
            )
        if not isinstance(name, str) or not name.strip():
            raise PendingListingSourceBlockedError(
                f"CNINFO current IPO list lacks a name for {code}"
            )
        if code in seen:
            raise PendingListingSourceBlockedError(
                f"CNINFO current IPO list contains duplicate code: {code}"
            )
        seen.add(code)
        if code in targets:
            spec = targets[code]
            if (
                name != spec.name
                or row["f035d0089Date"] != spec.subscription_date
                or row["f035d0089Time"] != f"{spec.subscription_date} 00:00:00"
                or row["f007d0007"] is not None
                # The IPO page exposes this value but does not document its
                # business meaning.  Freeze the observed contract exactly and
                # derive suspension/termination status from the complete
                # announcement page below, never from this opaque code.
                or row["f001v0116"] != "013006"
                or not isinstance(row["obSeqId"], str)
                or not row["obSeqId"]
            ):
                raise PendingListingSourceBlockedError(
                    f"CNINFO current IPO status is not pending/unlisted for {code}"
                )
            matches[code] = dict(row)
    if set(matches) != set(targets):
        missing = sorted(set(targets) - set(matches))
        raise PendingListingSourceBlockedError(
            "CNINFO current IPO list no longer contains every target; "
            f"withdrawn, suspended, or listed status must fail closed: {missing}"
        )
    normalized = [
        {
            "code": code,
            "name": matches[code]["obSecName0007"],
            "subscription_date": matches[code]["f035d0089Date"],
            "listing_date": matches[code]["f007d0007"],
            # Retained as an opaque source value.  Pending classification does
            # not infer its undocumented semantics.
            "source_process_code": matches[code]["f001v0116"],
            "sequence_id": matches[code]["obSeqId"],
        }
        for code in sorted(matches)
    ]
    summary = {
        "row_count": len(rows),
        "target_rows": normalized,
        "target_rows_sha256": _sha256(_canonical_json_bytes(normalized)),
        "target_codes_have_null_listing_date": True,
    }
    return matches, summary


def parse_cninfo_ipo_announcements(
    raw: bytes,
    *,
    spec: SZSEPendingDocumentSpec,
    expected_sha256: str | None = None,
) -> tuple[tuple[dict[str, Any], ...], Mapping[str, Any]]:
    """Validate a complete current IPO announcement page and hard negatives."""

    source_id = _cninfo_announcement_source_id(spec.code)
    _verify_sha256(raw, expected_sha256, source_id)
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise PendingListingSourceBlockedError(
            f"{source_id} response is empty or oversized"
        )
    try:
        value = _loads_json_no_duplicates(
            raw.decode("utf-8"), f"{source_id} response"
        )
    except UnicodeDecodeError as exc:
        raise PendingListingSourceBlockedError(
            f"{source_id} response is not UTF-8"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"code", "message", "data"}:
        raise PendingListingSourceBlockedError(f"{source_id} response schema drift")
    if value["code"] != 200 or value["message"] != "执行成功":
        raise PendingListingSourceBlockedError(f"{source_id} reports failure")
    page = value["data"]
    if not isinstance(page, dict) or set(page) != CNINFO_ANNOUNCEMENT_PAGE_FIELDS:
        raise PendingListingSourceBlockedError(f"{source_id} page schema drift")
    rows = page["list"]
    if not isinstance(rows, list) or not rows:
        raise PendingListingSourceBlockedError(f"{source_id} contains no announcements")
    total = _strict_int(page["total"], f"{source_id} total")
    if (
        page["pageNum"] != 0
        or isinstance(page["pageNum"], bool)
        or page["pageSize"] != 0
        or isinstance(page["pageSize"], bool)
        or page["pages"] != 0
        or isinstance(page["pages"], bool)
        or page["size"] != 0
        or isinstance(page["size"], bool)
        or page["startRow"] != "0"
        or page["endRow"] != "0"
        or page["isFirstPage"] is not False
        or page["isLastPage"] is not False
        or page["hasNextPage"] is not False
        or page["hasPreviousPage"] is not False
        or page["navigatepageNums"] is not None
        or page["navigateFirstPage"] != 0
        or page["navigateLastPage"] != 0
        or page["nextPage"] != 0
        or page["prePage"] != 0
        or total != len(rows)
        or total > 100
    ):
        raise PendingListingSourceBlockedError(
            f"{source_id} does not satisfy the admitted unpaginated full-response "
            "contract"
        )
    normalized: list[dict[str, Any]] = []
    seen_uris: set[str] = set()
    previous_date: str | None = None
    hard_negatives: list[str] = []
    admitted_document_found = False
    for row in rows:
        if not isinstance(row, dict) or set(row) != CNINFO_ANNOUNCEMENT_ROW_FIELDS:
            raise PendingListingSourceBlockedError(f"{source_id} row schema drift")
        if (
            row["announcementId"] is not None
            or row["stockId"] is not None
            or row["announcementTime"] is not None
            or not isinstance(row["title"], str)
            or not row["title"].strip()
            or not isinstance(row["uri"], str)
            or not re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", row["uri"])
            or not isinstance(row["announcementDate"], str)
        ):
            raise PendingListingSourceBlockedError(
                f"{source_id} row identity or precision changed"
            )
        date = _optional_iso_date(row["announcementDate"])
        if date is None:
            raise PendingListingSourceBlockedError(
                f"{source_id} announcement date is missing"
            )
        if previous_date is not None and date > previous_date:
            raise PendingListingSourceBlockedError(
                f"{source_id} rows are not date-descending"
            )
        previous_date = date
        if row["uri"] in seen_uris:
            raise PendingListingSourceBlockedError(
                f"{source_id} contains a duplicate document URI"
            )
        seen_uris.add(row["uri"])
        title = row["title"].strip()
        if any(marker in title for marker in CNINFO_HARD_NEGATIVE_ISSUANCE_MARKERS):
            hard_negatives.append(title)
        expected_uri = urlsplit(spec.request_url).path.lstrip("/")
        if row["uri"] == expected_uri and title == spec.title_marker:
            admitted_document_found = True
        normalized.append({"date": date, "title": title, "uri": row["uri"]})
    if not admitted_document_found:
        raise PendingListingSourceBlockedError(
            f"{source_id} no longer contains the admitted issuance document"
        )
    if hard_negatives:
        raise PendingListingSourceBlockedError(
            f"{source_id} contains hard negative issuance status: {hard_negatives}"
        )
    return tuple(normalized), {
        "code": spec.code,
        "row_count": len(normalized),
        "latest_announcement_date": normalized[0]["date"],
        "admitted_document_uri": urlsplit(spec.request_url).path.lstrip("/"),
        "hard_negative_count": 0,
        "hard_negative_markers": list(CNINFO_HARD_NEGATIVE_ISSUANCE_MARKERS),
        "rows_sha256": _sha256(_canonical_json_bytes(normalized)),
    }


def parse_cninfo_issuance_pdf(
    raw: bytes,
    *,
    spec: SZSEPendingDocumentSpec,
) -> _PDFParsed:
    _verify_sha256(raw, spec.expected_sha256, spec.source_id)
    if not raw or len(raw) > MAX_PDF_BYTES or not raw.startswith(b"%PDF-"):
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} is not an admitted PDF"
        )
    text, page_count, engine, engine_version, text_sha256 = _extract_pdf_text(raw)
    compact = re.sub(r"\s+", "", text)
    subscription_marker = (
        f"{spec.subscription_date[:4]}年{int(spec.subscription_date[5:7])}月"
        f"{int(spec.subscription_date[8:10])}日（T日）"
    )
    markers = (
        spec.company_full_name,
        spec.title_marker,
        f"发行人股票简称为“{spec.name}”",
        f"股票代码为“{spec.code}”",
        subscription_marker,
    )
    missing = [marker for marker in markers if marker not in compact]
    if missing:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} PDF lacks assigned-code issuance markers: {missing}"
        )
    if "上市日期" in compact:
        raise PendingListingSourceBlockedError(
            f"{spec.source_id} unexpectedly declares a listing date"
        )
    security = PendingListingSecurity(
        exchange="SZSE",
        code=f"{spec.code}.SZ",
        name=spec.name,
        company_full_name=spec.company_full_name,
        status="ISSUANCE_DISCLOSED_NOT_IN_ACTIVE_CATALOGUE",
        evidence_kind=spec.document_kind,
        issue_milestone_date=spec.subscription_date,
        source_ids=(
            CNINFO_CURRENT_IPO_SOURCE_ID,
            CNINFO_MASTER_SOURCE_ID,
            _cninfo_announcement_source_id(spec.code),
            spec.source_id,
            SZSE_ACTIVE_SOURCE_ID,
        ),
    )
    summary = {
        "announcement_id": spec.announcement_id,
        "publication_date": spec.publication_date,
        "code": spec.code,
        "name": spec.name,
        "company_full_name": spec.company_full_name,
        "document_kind": spec.document_kind,
        "subscription_date": spec.subscription_date,
        "page_count": page_count,
        "parser_engine": engine,
        "parser_version": engine_version,
        "normalized_text_sha256": text_sha256,
        "matched_markers": list(markers),
        "listing_date_declared": False,
    }
    return _PDFParsed(security=security, summary=summary)


def _extract_pdf_text(raw: bytes) -> tuple[str, int, str, str, str]:
    try:
        import pypdf
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise PendingListingSourceBlockedError(
            "pypdf is unavailable; official PDF text cannot be recomputed",
            status="DEPENDENCY_MISSING",
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise PendingListingSourceBlockedError("official issuance PDF is encrypted")
        page_texts: list[str] = []
        for page in reader.pages:
            value = page.extract_text()
            if not isinstance(value, str):
                raise PendingListingSourceBlockedError(
                    "official issuance PDF contains a page without extractable text"
                )
            page_texts.append(value.replace("\x00", ""))
    except PendingListingSourceBlockedError:
        raise
    except Exception as exc:
        raise PendingListingSourceBlockedError(
            f"official issuance PDF parsing failed: {exc}"
        ) from exc
    if not page_texts:
        raise PendingListingSourceBlockedError("official issuance PDF contains no pages")
    text = "\n\f\n".join(page_texts)
    compact = re.sub(r"\s+", "", text)
    if not compact:
        raise PendingListingSourceBlockedError(
            "official issuance PDF contains no extractable text"
        )
    return (
        text,
        len(page_texts),
        "pypdf",
        str(getattr(pypdf, "__version__", "UNKNOWN")),
        _sha256(compact.encode("utf-8")),
    )


def parse_szse_active_catalogue(
    raw: bytes,
    *,
    expected_sha256: str | None = None,
) -> tuple[set[str], Mapping[str, Any]]:
    _verify_sha256(raw, expected_sha256, SZSE_ACTIVE_SOURCE_ID)
    rows = _xlsx_rows(raw)
    if not rows or tuple(rows[0]) != SZSE_ACTIVE_HEADER:
        raise PendingListingSourceBlockedError(
            "SZSE active-catalogue XLSX schema drift detected"
        )
    codes: set[str] = set()
    for row in rows[1:]:
        if len(row) != len(SZSE_ACTIVE_HEADER) or not any(row):
            raise PendingListingSourceBlockedError(
                "SZSE active-catalogue row width is invalid"
            )
        code = row[4].strip()
        if re.fullmatch(r"\d+(?:\.0+)?", code):
            code = str(int(float(code))).zfill(6)
        if not re.fullmatch(r"\d{6}", code) or not code.startswith(
            ("000", "001", "002", "003", "300", "301", "302")
        ):
            raise PendingListingSourceBlockedError(
                f"SZSE active catalogue contains invalid A-share code: {code!r}"
            )
        if code in codes:
            raise PendingListingSourceBlockedError(
                f"SZSE active catalogue contains duplicate code: {code}"
            )
        if not row[0].strip() or not row[1].strip() or not row[5].strip():
            raise PendingListingSourceBlockedError(
                f"SZSE active catalogue has incomplete identity fields for {code}"
            )
        codes.add(code)
    if len(codes) < MIN_SZSE_ACTIVE_ROWS:
        raise PendingListingSourceBlockedError(
            "SZSE active-catalogue coverage is implausibly small"
        )
    targets = sorted(spec.code for spec in SZSE_DOCUMENT_SPECS)
    present = sorted(set(targets) & codes)
    if present:
        raise PendingListingSourceBlockedError(
            f"SZSE codes are already active and cannot be pending: {present}"
        )
    summary = {
        "workbook_rows": len(rows) - 1,
        "active_a_share_rows": len(codes),
        "header_columns": len(SZSE_ACTIVE_HEADER),
        "target_codes_absent": targets,
        "active_code_set_sha256": _sha256(_canonical_json_bytes(sorted(codes))),
    }
    return codes, summary


def _xlsx_rows(raw: bytes) -> list[list[str]]:
    if not raw or len(raw) > MAX_XLSX_BYTES:
        raise PendingListingSourceBlockedError("SZSE XLSX is empty or oversized")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise PendingListingSourceBlockedError("SZSE response is not XLSX") from exc
    with archive:
        members = archive.infolist()
        if (
            len(members) > 100
            or sum(item.file_size for item in members) > MAX_XLSX_UNCOMPRESSED_BYTES
        ):
            raise PendingListingSourceBlockedError("SZSE XLSX archive is unsafe")
        sheet_names = sorted(
            item.filename
            for item in members
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item.filename)
        )
        if len(sheet_names) != 1:
            raise PendingListingSourceBlockedError(
                "SZSE XLSX must contain exactly one worksheet"
            )
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            except ElementTree.ParseError as exc:
                raise PendingListingSourceBlockedError(
                    "SZSE shared-strings XML is invalid"
                ) from exc
            shared = ["".join(item.itertext()) for item in list(shared_root)]
        try:
            root = ElementTree.fromstring(archive.read(sheet_names[0]))
        except ElementTree.ParseError as exc:
            raise PendingListingSourceBlockedError(
                "SZSE worksheet XML is invalid"
            ) from exc

    rows: list[list[str]] = []
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for row in root.iter(f"{namespace}row"):
        cells: dict[int, str] = {}
        for cell in row.findall(f"{namespace}c"):
            reference = str(cell.attrib.get("r") or "")
            match = re.match(r"([A-Z]+)", reference)
            if match is None:
                raise PendingListingSourceBlockedError(
                    "SZSE cell reference is invalid"
                )
            column = _excel_column_index(match.group(1))
            if column in cells:
                raise PendingListingSourceBlockedError(
                    "SZSE worksheet contains a duplicate cell"
                )
            cell_type = cell.attrib.get("t", "")
            if cell_type == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(f"{namespace}t"))
            else:
                value_node = cell.find(f"{namespace}v")
                value = (
                    value_node.text
                    if value_node is not None and value_node.text is not None
                    else ""
                )
                if cell_type == "s":
                    index = _strict_int(value, "SZSE shared-string index")
                    if index >= len(shared):
                        raise PendingListingSourceBlockedError(
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


def _rebuild_artifact_from_sources(
    *,
    retrieved_at: str,
    source_values: Sequence[Mapping[str, Any]],
    cas: PendingListingRawCAS,
) -> PendingListingArtifact:
    retrieved = _normalize_retrieved_at(retrieved_at)
    if len(source_values) != len(SOURCE_ORDER):
        raise PendingListingSourceBlockedError(
            "pending-listing raw source set is incomplete"
        )
    expected_fields = {
        "source_id",
        "request_url",
        "method",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "http_status",
        "response_summary",
    }
    evidence: list[PendingListingRawEvidence] = []
    sse_securities: list[PendingListingSecurity] = []
    szse_securities: dict[str, PendingListingSecurity] = {}
    master_bindings: dict[str, dict[str, str]] | None = None
    current_ipo_rows: dict[str, dict[str, Any]] | None = None
    announcement_rows: dict[str, tuple[dict[str, Any], ...]] = {}
    active_codes: set[str] | None = None
    sse_by_id = {spec.source_id: spec for spec in SSE_PENDING_SPECS}
    document_by_id = {spec.source_id: spec for spec in SZSE_DOCUMENT_SPECS}
    announcement_by_id = {
        _cninfo_announcement_source_id(spec.code): spec
        for spec in SZSE_DOCUMENT_SPECS
    }

    for expected_source_id, source in zip(SOURCE_ORDER, source_values, strict=True):
        if not isinstance(source, Mapping) or set(source) != expected_fields:
            raise PendingListingSourceBlockedError(
                "pending-listing raw source manifest schema drift"
            )
        source_id = _required_string(source["source_id"], "source_id")
        if source_id != expected_source_id:
            raise PendingListingSourceBlockedError(
                "pending-listing raw source order or identity changed"
            )
        request_url = _required_string(source["request_url"], "request_url")
        _validate_source_url(source_id, request_url)
        content_type = _required_string(source["content_type"], "content_type")
        source_retrieved = _normalize_retrieved_at(source["retrieved_at"])
        if source_retrieved != source["retrieved_at"]:
            raise PendingListingSourceBlockedError(
                f"{source_id} retrieved_at is not canonical"
            )
        expected_type = _expected_media_type(source_id)
        if (
            source["method"] != "GET"
            or _strict_int(source["http_status"], "HTTP status") != 200
            or content_type != expected_type
        ):
            raise PendingListingSourceBlockedError(
                f"{source_id} transport contract changed"
            )
        digest = _required_string(source["content_sha256"], "content_sha256").lower()
        raw, path = cas.read_blob(digest)
        if _strict_int(source["byte_count"], "byte_count") != len(raw):
            raise PendingListingSourceBlockedError(
                f"{source_id} CAS byte count mismatch"
            )
        supplied_summary = source["response_summary"]
        if not isinstance(supplied_summary, dict):
            raise PendingListingSourceBlockedError(
                f"{source_id} response summary is invalid"
            )

        if source_id in sse_by_id:
            parsed = parse_sse_pending_response(
                raw,
                spec=sse_by_id[source_id],
                expected_sha256=digest,
            )
            summary = dict(parsed.summary)
            sse_securities.append(parsed.security)
        elif source_id == CNINFO_MASTER_SOURCE_ID:
            master_bindings, parsed_summary = parse_cninfo_stock_master(
                raw,
                expected_sha256=digest,
            )
            summary = dict(parsed_summary)
        elif source_id == CNINFO_CURRENT_IPO_SOURCE_ID:
            current_ipo_rows, parsed_summary = parse_cninfo_current_ipo_list(
                raw,
                expected_sha256=digest,
            )
            summary = dict(parsed_summary)
        elif source_id in announcement_by_id:
            spec = announcement_by_id[source_id]
            parsed_rows, parsed_summary = parse_cninfo_ipo_announcements(
                raw,
                spec=spec,
                expected_sha256=digest,
            )
            announcement_rows[spec.code] = parsed_rows
            summary = dict(parsed_summary)
        elif source_id in document_by_id:
            parsed = parse_cninfo_issuance_pdf(
                raw,
                spec=document_by_id[source_id],
            )
            summary = dict(parsed.summary)
            szse_securities[document_by_id[source_id].code] = parsed.security
        elif source_id == SZSE_ACTIVE_SOURCE_ID:
            active_codes, parsed_summary = parse_szse_active_catalogue(
                raw,
                expected_sha256=digest,
            )
            summary = dict(parsed_summary)
        else:
            raise PendingListingSourceBlockedError(f"unknown source: {source_id}")
        if supplied_summary != summary:
            raise PendingListingSourceBlockedError(
                f"{source_id} response summary does not match raw CAS bytes"
            )
        evidence.append(
            PendingListingRawEvidence(
                source_id=source_id,
                request_url=request_url,
                method="GET",
                retrieved_at=source_retrieved,
                content_sha256=digest,
                byte_count=len(raw),
                content_type=content_type,
                http_status=200,
                cas_uri=f"sha256:{digest}",
                object_path=str(path),
                response_summary=summary,
            )
        )

    if master_bindings is None or current_ipo_rows is None or active_codes is None:
        raise PendingListingSourceBlockedError(
            "SZSE identity, current-IPO, or current-active evidence is missing"
        )
    if set(szse_securities) != set(master_bindings) or set(szse_securities) != set(
        current_ipo_rows
    ) or set(szse_securities) != set(announcement_rows):
        raise PendingListingSourceBlockedError(
            "SZSE document, current-IPO, announcement, and stock-master bindings "
            "do not agree"
        )
    if set(szse_securities) & active_codes:
        raise PendingListingSourceBlockedError(
            "a purported SZSE pending code is already in the active catalogue"
        )
    securities = tuple(
        sorted(
            [*sse_securities, *szse_securities.values()],
            key=lambda item: (item.exchange, item.code),
        )
    )
    expected_codes = {
        *(f"{spec.code}.SH" for spec in SSE_PENDING_SPECS),
        *(f"{spec.code}.SZ" for spec in SZSE_DOCUMENT_SPECS),
    }
    if {item.code for item in securities} != expected_codes or len(securities) != 6:
        raise PendingListingSourceBlockedError(
            "pending-listing six-code evidence set is incomplete"
        )
    logical_value = {
        "securities": [item.to_dict() for item in securities],
        "source_hashes": [item.content_sha256 for item in evidence],
        "source_summaries": [dict(item.response_summary) for item in evidence],
    }
    source_times = [datetime.fromisoformat(item.retrieved_at) for item in evidence]
    maximum_source_time = max(source_times)
    minimum_source_time = min(source_times)
    if maximum_source_time - minimum_source_time > MAX_CAPTURE_SPAN:
        raise PendingListingSourceBlockedError(
            "pending-listing sources were not captured in one bounded observation window"
        )
    if retrieved != maximum_source_time.replace(microsecond=0).isoformat():
        raise PendingListingSourceBlockedError(
            "artifact retrieved_at must equal the latest immutable source capture time"
        )
    return PendingListingArtifact(
        retrieved_at=retrieved,
        securities=securities,
        raw_sources=tuple(evidence),
        logical_content_sha256=_sha256(_canonical_json_bytes(logical_value)),
    )


def _manifest_payload(artifact: PendingListingArtifact) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "retrieved_at": artifact.retrieved_at,
        "securities": [item.to_dict() for item in artifact.securities],
        "sources": [item.to_manifest_source() for item in artifact.raw_sources],
        "logical_content_sha256": artifact.logical_content_sha256,
        "source_contract": artifact.source_contract,
        "statistics": artifact.statistics,
    }


def validate_pending_listing_freshness(
    artifact: PendingListingArtifact,
    *,
    now: datetime | str | None = None,
    as_of: datetime | str | None = None,
    maximum_age: timedelta = MAX_CURRENT_EVIDENCE_AGE,
) -> None:
    """Fail closed when current-status evidence is stale, future-dated, or re-dated."""

    if not isinstance(artifact, PendingListingArtifact):
        raise TypeError("artifact must be a PendingListingArtifact")
    if maximum_age <= timedelta(0):
        raise ValueError("maximum_age must be positive")
    current_text = _normalize_retrieved_at(now)
    current = datetime.fromisoformat(current_text)
    decision = (
        datetime.fromisoformat(_normalize_retrieved_at(as_of))
        if as_of is not None
        else current
    )
    if decision > current + MAX_FUTURE_CLOCK_SKEW:
        raise PendingListingSourceBlockedError(
            "pending-listing as_of is future-dated relative to the validation clock"
        )
    if not artifact.raw_sources:
        raise PendingListingSourceBlockedError(
            "pending-listing artifact contains no raw source captures"
        )
    source_times = [
        datetime.fromisoformat(_normalize_retrieved_at(item.retrieved_at))
        for item in artifact.raw_sources
    ]
    earliest = min(source_times)
    latest = max(source_times)
    if latest - earliest > MAX_CAPTURE_SPAN:
        raise PendingListingSourceBlockedError(
            "pending-listing source capture span exceeds the admitted bound"
        )
    if latest > current + MAX_FUTURE_CLOCK_SKEW:
        raise PendingListingSourceBlockedError(
            "pending-listing evidence is future-dated"
        )
    if current - earliest > maximum_age:
        raise PendingListingSourceBlockedError(
            "pending-listing current-status evidence is stale",
            status="SOURCE_STALE",
        )
    if latest > decision + MAX_FUTURE_CLOCK_SKEW:
        raise PendingListingSourceBlockedError(
            "pending-listing evidence was captured after the requested as_of"
        )
    if decision - earliest > maximum_age:
        raise PendingListingSourceBlockedError(
            "pending-listing evidence is stale for the requested as_of",
            status="SOURCE_STALE",
        )
    expected_retrieved = latest.replace(microsecond=0).isoformat()
    if artifact.retrieved_at != expected_retrieved:
        raise PendingListingSourceBlockedError(
            "pending-listing artifact was re-dated independently of its raw captures"
        )


def _rebuild_from_manifest_payload(
    payload: Mapping[str, Any],
    *,
    cas: PendingListingRawCAS,
) -> PendingListingArtifact:
    expected_fields = {
        "protocol_version",
        "retrieved_at",
        "securities",
        "sources",
        "logical_content_sha256",
        "source_contract",
        "statistics",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise PendingListingSourceBlockedError(
            "pending-listing manifest schema drift detected"
        )
    if payload["protocol_version"] != PROTOCOL_VERSION:
        raise PendingListingSourceBlockedError(
            "pending-listing manifest protocol changed"
        )
    retrieved = _normalize_retrieved_at(payload["retrieved_at"])
    if retrieved != payload["retrieved_at"]:
        raise PendingListingSourceBlockedError(
            "pending-listing manifest retrieved_at is not canonical"
        )
    sources = payload["sources"]
    if not isinstance(sources, list):
        raise PendingListingSourceBlockedError(
            "pending-listing manifest source set is invalid"
        )
    artifact = _rebuild_artifact_from_sources(
        retrieved_at=retrieved,
        source_values=sources,
        cas=cas,
    )
    if payload["securities"] != [item.to_dict() for item in artifact.securities]:
        raise PendingListingSourceBlockedError(
            "manifest securities do not match raw official evidence"
        )
    if payload["logical_content_sha256"] != artifact.logical_content_sha256:
        raise PendingListingSourceBlockedError("manifest logical hash mismatch")
    if payload["source_contract"] != artifact.source_contract:
        raise PendingListingSourceBlockedError("manifest source contract changed")
    if payload["statistics"] != artifact.statistics:
        raise PendingListingSourceBlockedError(
            "manifest statistics do not match raw official evidence"
        )
    return artifact


def _validate_source_url(source_id: str, url: str) -> None:
    expected = _expected_request_url(source_id)
    if url != expected:
        raise PendingListingSourceBlockedError(
            f"{source_id} request URL does not match the admitted source"
        )
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PendingListingSourceBlockedError(f"{source_id} URL is unsafe")
    allowed_hosts = {
        "query.sse.com.cn",
        "www.cninfo.com.cn",
        "static.cninfo.com.cn",
        "www.szse.cn",
    }
    if parsed.hostname not in allowed_hosts or parsed.port not in (None, 443):
        raise PendingListingSourceBlockedError(
            f"{source_id} host is not admitted"
        )
    if source_id.startswith("SSE_IPO_"):
        expected_query = parse_qsl(urlsplit(expected).query, keep_blank_values=True)
        actual_query = parse_qsl(parsed.query, keep_blank_values=True)
        if actual_query != expected_query:
            raise PendingListingSourceBlockedError(
                f"{source_id} query contract changed"
            )


def _expected_request_url(source_id: str) -> str:
    for spec in SSE_PENDING_SPECS:
        if spec.source_id == source_id:
            return spec.request_url
    if source_id == CNINFO_MASTER_SOURCE_ID:
        return CNINFO_STOCK_MASTER_URL
    if source_id == CNINFO_CURRENT_IPO_SOURCE_ID:
        return CNINFO_CURRENT_IPO_URL
    for spec in SZSE_DOCUMENT_SPECS:
        if source_id == _cninfo_announcement_source_id(spec.code):
            return build_cninfo_announcement_request_url(spec.code)
    for spec in SZSE_DOCUMENT_SPECS:
        if spec.source_id == source_id:
            return spec.request_url
    if source_id == SZSE_ACTIVE_SOURCE_ID:
        return SZSE_ACTIVE_XLSX_URL
    raise PendingListingSourceBlockedError(f"unknown source id: {source_id}")


def _expected_media_type(source_id: str) -> str:
    if source_id.startswith("SSE_IPO_") or source_id in {
        CNINFO_MASTER_SOURCE_ID,
        CNINFO_CURRENT_IPO_SOURCE_ID,
    } or source_id.endswith("_CURRENT_IPO_ANNOUNCEMENTS"):
        return JSON_MEDIA_TYPE
    if source_id.startswith("CNINFO_301"):
        return PDF_MEDIA_TYPE
    if source_id == SZSE_ACTIVE_SOURCE_ID:
        return XLSX_MEDIA_TYPE
    raise PendingListingSourceBlockedError(f"unknown source id: {source_id}")


def _media_type(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PendingListingSourceBlockedError("official response lacks Content-Type")
    result = value.split(";", 1)[0].strip().lower()
    if not result:
        raise PendingListingSourceBlockedError("official response Content-Type is invalid")
    return result


def _optional_iso_date(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    text = str(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise PendingListingSourceBlockedError(
            f"invalid official issue date: {value!r}"
        ) from exc
    if parsed != text:
        raise PendingListingSourceBlockedError(
            f"official issue date is not canonical: {value!r}"
        )
    return parsed


def _normalize_retrieved_at(value: Any = None) -> str:
    if value is None:
        parsed = datetime.now().astimezone().replace(microsecond=0)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise PendingListingSourceBlockedError(
                f"invalid retrieved_at: {value!r}"
            ) from exc
    else:
        raise PendingListingSourceBlockedError("retrieved_at must be an ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PendingListingSourceBlockedError("retrieved_at must include a timezone")
    return parsed.replace(microsecond=0).isoformat()


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise PendingListingSourceBlockedError(f"invalid {label}: {value!r}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PendingListingSourceBlockedError(
            f"invalid {label}: {value!r}"
        ) from exc
    if result < 0:
        raise PendingListingSourceBlockedError(f"invalid {label}: {value!r}")
    return result


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PendingListingSourceBlockedError(f"invalid {label}")
    return value


def _verify_sha256(content: bytes, expected: str | None, label: str) -> str:
    actual = _sha256(content)
    if expected is None:
        return actual
    normalized = str(expected).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise PendingListingSourceBlockedError(
            f"invalid expected SHA-256 for {label}"
        )
    if actual != normalized:
        raise PendingListingSourceBlockedError(
            f"{label} source hash mismatch: expected {normalized}, got {actual}"
        )
    return actual


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decode_canonical_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = _loads_json_no_duplicates(content.decode("utf-8"), label)
    except UnicodeDecodeError as exc:
        raise PendingListingSourceBlockedError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PendingListingSourceBlockedError(f"{label} is not a JSON object")
    return value


def _loads_json_no_duplicates(text: str, label: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PendingListingSourceBlockedError(
                    f"{label} contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except PendingListingSourceBlockedError:
        raise
    except json.JSONDecodeError as exc:
        raise PendingListingSourceBlockedError(f"{label} is invalid JSON") from exc


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _lexical_absolute_path(path: Path) -> Path:
    """Return an absolute path without resolving links or reparse points."""

    return Path(os.path.abspath(os.fspath(path)))


def _stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_file_attributes", 0)),
        int(getattr(value, "st_reparse_tag", 0)),
    )


def _is_link_or_reparse(value: os.stat_result) -> bool:
    reparse_attribute = int(
        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)
    )
    return stat.S_ISLNK(value.st_mode) or bool(
        int(getattr(value, "st_file_attributes", 0)) & reparse_attribute
    )


def _cas_path_snapshot(
    root: Path,
    target: Path,
    *,
    leaf_is_file: bool,
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    root_value = _lexical_absolute_path(root)
    target_value = _lexical_absolute_path(target)
    try:
        relative = target_value.relative_to(root_value)
    except ValueError as exc:
        raise PendingListingSourceBlockedError(
            "CAS object escapes the configured root"
        ) from exc

    components = [root_value]
    current = root_value
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise PendingListingSourceBlockedError("invalid CAS object path")
        current = current / part
        components.append(current)

    snapshot: list[tuple[str, tuple[int, ...]]] = []
    for index, component in enumerate(components):
        try:
            value = os.lstat(component)
        except OSError as exc:
            raise PendingListingSourceBlockedError(
                f"CAS object is missing or unsafe: {component}"
            ) from exc
        if _is_link_or_reparse(value):
            raise PendingListingSourceBlockedError(
                f"CAS path contains a link or reparse point: {component}"
            )
        is_leaf = index == len(components) - 1
        expected_file = leaf_is_file and is_leaf
        if expected_file:
            if not stat.S_ISREG(value.st_mode):
                raise PendingListingSourceBlockedError(
                    f"CAS object is not a regular file: {component}"
                )
        elif not stat.S_ISDIR(value.st_mode):
            raise PendingListingSourceBlockedError(
                f"CAS path component is not a directory: {component}"
            )
        snapshot.append((os.fspath(component), _stat_fingerprint(value)))
    return tuple(snapshot)


def _stable_read_cas_object(root: Path, path: Path) -> bytes:
    before = _cas_path_snapshot(root, path, leaf_is_file=True)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PendingListingSourceBlockedError(
            f"CAS object cannot be opened safely: {path}"
        ) from exc
    try:
        handle_before = os.fstat(descriptor)
        if _is_link_or_reparse(handle_before) or not stat.S_ISREG(handle_before.st_mode):
            raise PendingListingSourceBlockedError(
                f"CAS object handle is not a regular non-reparse file: {path}"
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
    after = _cas_path_snapshot(root, path, leaf_is_file=True)
    if (
        before != after
        or _stat_fingerprint(handle_before) != _stat_fingerprint(handle_after)
        or _stat_fingerprint(handle_before) != before[-1][1]
    ):
        raise PendingListingSourceBlockedError(
            f"CAS object or parent path changed during read: {path}"
        )
    return b"".join(chunks)


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _cas_path_snapshot(root, path.parent, leaf_is_file=False)
    if path.exists():
        if _stable_read_cas_object(root, path) != content:
            raise PendingListingSourceBlockedError(
                f"immutable CAS collision at {path}"
            )
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _stable_read_cas_object(root, path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "CNINFO_CURRENT_IPO_SOURCE_ID",
    "CNINFO_CURRENT_IPO_URL",
    "CNINFO_MASTER_SOURCE_ID",
    "CNINFO_STOCK_MASTER_URL",
    "PENDING_LISTING_EVIDENCE_COMPLETE",
    "PROTOCOL_VERSION",
    "PendingListingArtifact",
    "PendingListingManifestReference",
    "PendingListingManifestStore",
    "PendingListingRawCAS",
    "PendingListingRawEvidence",
    "PendingListingSecurity",
    "PendingListingSourceBlockedError",
    "PendingListingSourceClient",
    "SOURCE_CONTRACT_ADMITTED",
    "SOURCE_ORDER",
    "SSE_IPO_PAGE_URL",
    "SSE_PENDING_SPECS",
    "SSEPendingSpec",
    "SZSE_ACTIVE_SOURCE_ID",
    "SZSE_ACTIVE_XLSX_URL",
    "SZSE_DOCUMENT_SPECS",
    "SZSEPendingDocumentSpec",
    "build_sse_pending_request_url",
    "parse_cninfo_issuance_pdf",
    "parse_cninfo_current_ipo_list",
    "parse_cninfo_stock_master",
    "parse_sse_pending_response",
    "parse_szse_active_catalogue",
    "validate_pending_listing_freshness",
]
