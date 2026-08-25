from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import requests

from research_platform import cninfo_delisted_disclosures as cninfo


PROTOCOL_VERSION = "official-corporate-action-evidence-v1"
MANIFEST_SCHEMA_VERSION = "official-corporate-action-evidence-manifest-v1"
STATUS = "CORPORATE_ACTION_EVIDENCE_INCOMPLETE"
EXPECTED_TARGET_COUNT = 239
AUDIT_START = date(2018, 1, 1)
AUDIT_END = date(2023, 12, 31)

SSE_QUERY_URL = (
    "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
)
SSE_DOCUMENT_ORIGIN = "https://static.sse.com.cn"
SSE_SOURCE_AUTHORITY = "SSE_OFFICIAL_DISCLOSURE_CATALOG"
CNINFO_SOURCE_AUTHORITY = "CNINFO_OFFICIAL_DISCLOSURE_CATALOG"
SSE_PAGE_SIZE = 100
MAX_SSE_PAGES_PER_CODE = 1_000
MAX_CANDIDATE_DOCUMENTS = 10_000

_SSE_SECURITY_TYPES = "0101,120100,020100,020200,120200"
_SSE_CALLBACK = "jsonpCallback"
_SSE_TOP_FIELDS = frozenset(
    {
        "BULLETIN_TYPE",
        "END_DATE",
        "SECURITY_CODE",
        "START_DATE",
        "TITLE",
        "beginDate",
        "endDate",
        "isNew",
        "isPagination",
        "jsonCallBack",
        "keyWord",
        "pageHelp",
        "productId",
        "reportType",
        "reportType2",
        "result",
        "secCodes",
        "securityType",
        "stockType",
    }
)
_SSE_PAGE_FIELDS = frozenset(
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
_SSE_ROW_FIELDS = frozenset(
    {
        "ADDDATE",
        "BULLETIN_HEADING",
        "BULLETIN_TYPE",
        "BULLETIN_YEAR",
        "INDEXCLASS",
        "OPERATION_SEQ",
        "PLAN_Date",
        "PLAN_Year",
        "ROWNUM",
        "ROWNUM_",
        "SECURITY_CODE",
        "SECURITY_NAME",
        "SSEDATE",
        "SSEDate",
        "SSETime",
        "SSETimeStr",
        "TITLE",
        "URL",
        "author",
        "book_Name",
        "bulletinHeading",
        "bulletinType",
        "bulletin_No",
        "bulletin_Type",
        "bulletin_Year",
        "category_A",
        "category_B",
        "category_C",
        "category_D",
        "chapter_No",
        "companyAbbr",
        "dispatch_Organ",
        "file_Serial",
        "finish_Time",
        "initial_Date",
        "isChangeFlag",
        "journal_Issue",
        "journal_Name",
        "journal_Section",
        "journal_Year",
        "keyWord",
        "key_Word",
        "language",
        "lemma_CN",
        "lemma_EN",
        "publishing_Comp",
        "question",
        "question_Class",
        "read_Status",
        "save_Time",
        "section",
        "security_Code",
        "source",
        "spareVolEnd",
        "title",
        "title_ETC",
        "title_PY",
        "unit_Code",
        "unit_Type",
    }
)
_ACTION_TITLE_MARKERS = (
    "\u6743\u76ca\u5206\u6d3e",
    "\u5229\u6da6\u5206\u914d",
    "\u5206\u7ea2",
    "\u6d3e\u606f",
    "\u9664\u6743",
    "\u9664\u606f",
    "\u9001\u80a1",
    "\u8f6c\u589e",
    "\u914d\u80a1",
    "\u73b0\u91d1\u7ea2\u5229",
)
_SOURCE_CONTRACT = {
    "ready": False,
    "status": STATUS,
    "promotion_blocked": True,
    "expected_target_count": EXPECTED_TARGET_COUNT,
    "anonymous_read_only_sources": [
        CNINFO_SOURCE_AUTHORITY,
        SSE_SOURCE_AUTHORITY,
    ],
    "structured_corporate_action_rows_emitted": 0,
    "gp30_eligible": False,
    "gp43_eligible": False,
    "adjustment_factor_eligible": False,
    "zero_event_inference_allowed": False,
    "training_eligible": False,
    "trading_eligible": False,
    "caller_ready_ignored": True,
}


class CorporateActionEvidenceBlockedError(RuntimeError):
    """Official evidence did not satisfy the frozen, replayable contract."""


@dataclass(frozen=True)
class FrozenCorporateActionTarget:
    canonical_entity_id: str
    exchange: str
    code: str
    query_start: str
    query_end: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CorporateActionEvidenceReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str
    target_count: int
    sse_dual_source_reconciled_count: int
    zero_event_candidate_count: int
    event_candidate_count: int
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_official_corporate_action_evidence(
    *,
    cas_root: Path,
    targets: Sequence[FrozenCorporateActionTarget],
    session: requests.Session | None = None,
    timeout_seconds: float = 30.0,
    clock: Callable[[], datetime] | None = None,
) -> CorporateActionEvidenceReference:
    """Capture catalogs and candidate PDFs without producing quality rows."""

    client = _OfficialCorporateActionClient(
        cas=cninfo.CninfoDisclosureCAS(Path(cas_root)),
        session=session,
        timeout_seconds=timeout_seconds,
        clock=clock,
    )
    manifest = client.capture(targets)
    content = _canonical_json_bytes(manifest)
    digest, path = client.cas.put_blob(content)
    replayed = replay_official_corporate_action_evidence(
        cas_root=cas_root,
        manifest_sha256=digest,
    )
    if _canonical_json_bytes(replayed) != content:
        raise CorporateActionEvidenceBlockedError(
            "published corporate-action manifest failed cold replay"
        )
    return _reference(digest, path, content, manifest)


def replay_official_corporate_action_evidence(
    *,
    cas_root: Path,
    manifest_sha256: str,
) -> dict[str, Any]:
    """Cold-replay a manifest and every raw response from its CAS root."""

    cas = cninfo.CninfoDisclosureCAS(Path(cas_root))
    try:
        content, _path = cas.read_blob(manifest_sha256)
    except cninfo.CninfoDelistedDisclosureBlockedError as exc:
        raise CorporateActionEvidenceBlockedError(str(exc)) from exc
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorporateActionEvidenceBlockedError(
            "corporate-action manifest is not UTF-8 JSON"
        ) from exc
    if not isinstance(manifest, dict) or _canonical_json_bytes(manifest) != content:
        raise CorporateActionEvidenceBlockedError(
            "corporate-action manifest is not canonical JSON"
        )
    expected_fields = {
        "manifest_schema_version",
        "protocol_version",
        "targets",
        "stock_master",
        "sse_pages",
        "cninfo_pages",
        "candidate_documents",
        "candidate_document_gaps",
        "normalized",
        "logical_content_sha256",
        "source_contract",
        "statistics",
        "ready",
    }
    if set(manifest) != expected_fields:
        raise CorporateActionEvidenceBlockedError(
            "corporate-action manifest schema drift"
        )
    if (
        manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("protocol_version") != PROTOCOL_VERSION
        or manifest.get("ready") is not False
        or manifest.get("source_contract") != _SOURCE_CONTRACT
    ):
        raise CorporateActionEvidenceBlockedError(
            "corporate-action manifest attempted to change its blocked contract"
        )
    targets_value = manifest.get("targets")
    if not isinstance(targets_value, list):
        raise CorporateActionEvidenceBlockedError("manifest targets are missing")
    try:
        targets = tuple(
            FrozenCorporateActionTarget(**dict(item)) for item in targets_value
        )
    except (TypeError, ValueError) as exc:
        raise CorporateActionEvidenceBlockedError("manifest target schema drift") from exc
    rebuilt = _assemble_manifest(
        cas=cas,
        targets=targets,
        stock_master=manifest.get("stock_master"),
        sse_pages=manifest.get("sse_pages"),
        cninfo_pages=manifest.get("cninfo_pages"),
        candidate_documents=manifest.get("candidate_documents"),
        candidate_document_gaps=manifest.get("candidate_document_gaps"),
    )
    if _canonical_json_bytes(rebuilt) != content:
        raise CorporateActionEvidenceBlockedError(
            "corporate-action manifest does not replay from raw source bytes"
        )
    return rebuilt


class _OfficialCorporateActionClient:
    def __init__(
        self,
        *,
        cas: cninfo.CninfoDisclosureCAS,
        session: requests.Session | None,
        timeout_seconds: float,
        clock: Callable[[], datetime] | None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.cninfo_client = cninfo.CninfoDelistedDisclosureClient(
            cas=cas,
            session=self.session,
            timeout_seconds=timeout_seconds,
            clock=self.clock,
        )

    def capture(
        self, targets: Sequence[FrozenCorporateActionTarget]
    ) -> dict[str, Any]:
        frozen_targets = _normalize_targets(targets)
        stock_evidence, stock_rows = self.cninfo_client.capture_stock_master()
        sse_pages: list[dict[str, Any]] = []
        cninfo_pages: list[dict[str, Any]] = []
        candidate_documents: list[dict[str, Any]] = []
        candidate_document_gaps: list[dict[str, Any]] = []
        candidate_document_keys: set[tuple[str, str]] = set()

        for target in frozen_targets:
            master_row = stock_rows.get(target.code[:6])
            if master_row is None:
                raise CorporateActionEvidenceBlockedError(
                    f"CNINFO stock master has no target {target.code}"
                )
            org_id = str(master_row["orgId"])
            cninfo_rows, target_cninfo_pages = self._capture_cninfo_pages(
                target=target,
                org_id=org_id,
            )
            cninfo_pages.extend(target_cninfo_pages)
            for row in cninfo_rows:
                if not _is_action_candidate(str(row["announcementTitle"])):
                    continue
                key = (CNINFO_SOURCE_AUTHORITY, str(row["announcementId"]))
                if key in candidate_document_keys:
                    continue
                candidate_document_keys.add(key)
                document = self.cninfo_client.capture_document(
                    target=_to_cninfo_target(target),
                    row=row,
                )
                candidate_documents.append(
                    {
                        "authority": CNINFO_SOURCE_AUTHORITY,
                        "exchange": target.exchange,
                        "code": target.code,
                        "source_key": str(row["announcementId"]),
                        "raw": dict(document["raw"]),
                    }
                )

            if target.exchange == "SSE":
                sse_rows, target_sse_pages = self._capture_sse_pages(target)
                sse_pages.extend(target_sse_pages)
                for row in sse_rows:
                    if not _is_action_candidate(str(row["TITLE"])):
                        continue
                    source_key = str(row["URL"])
                    key = (SSE_SOURCE_AUTHORITY, source_key)
                    if key in candidate_document_keys:
                        continue
                    candidate_document_keys.add(key)
                    document, gap = self._capture_sse_document(target, row)
                    if document is not None:
                        candidate_documents.append(document)
                    else:
                        assert gap is not None
                        candidate_document_gaps.append(gap)
            elif target.exchange != "SZSE":
                raise CorporateActionEvidenceBlockedError(
                    f"unsupported exchange {target.exchange}"
                )
        if len(candidate_documents) > MAX_CANDIDATE_DOCUMENTS:
            raise CorporateActionEvidenceBlockedError(
                "corporate-action candidate document count exceeds safety limit"
            )
        return _assemble_manifest(
            cas=self.cas,
            targets=frozen_targets,
            stock_master=stock_evidence.to_dict(),
            sse_pages=sse_pages,
            cninfo_pages=cninfo_pages,
            candidate_documents=candidate_documents,
            candidate_document_gaps=candidate_document_gaps,
        )

    def _capture_cninfo_pages(
        self,
        *,
        target: FrozenCorporateActionTarget,
        org_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        expected_total: int | None = None
        expected_page_count: int | None = None
        page = 1
        while True:
            summary, part, page_value = self.cninfo_client.capture_announcement_page(
                target=_to_cninfo_target(target),
                org_id=org_id,
                page=page,
            )
            if expected_total is None:
                expected_total = int(summary["total"])
                expected_page_count = max(
                    1, math.ceil(expected_total / cninfo.PAGE_SIZE)
                )
            assert expected_page_count is not None
            if (
                int(summary["total"]) != expected_total
                or int(summary["reported_totalpages"]) != expected_page_count - 1
                or summary["has_more"] is not (page < expected_page_count)
            ):
                raise CorporateActionEvidenceBlockedError(
                    "CNINFO pagination totals changed during capture"
                )
            expected_rows = (
                cninfo.PAGE_SIZE
                if page < expected_page_count
                else expected_total - cninfo.PAGE_SIZE * (expected_page_count - 1)
            )
            if int(summary["row_count"]) != expected_rows:
                raise CorporateActionEvidenceBlockedError(
                    "CNINFO page row count disagrees with total"
                )
            rows.extend(dict(item) for item in part)
            pages.append(dict(page_value))
            if page >= expected_page_count:
                break
            page += 1
        if len(rows) != expected_total or len(
            {str(item["announcementId"]) for item in rows}
        ) != len(rows):
            raise CorporateActionEvidenceBlockedError(
                "CNINFO pagination is incomplete or duplicated"
            )
        return rows, pages

    def _capture_sse_pages(
        self, target: FrozenCorporateActionTarget
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        expected_total: int | None = None
        expected_page_count: int | None = None
        page = 1
        while True:
            if page > MAX_SSE_PAGES_PER_CODE:
                raise CorporateActionEvidenceBlockedError(
                    "SSE pagination exceeds safety limit"
                )
            request = _sse_request(target, page)
            raw, content_type = self._get_sse_jsonp(request)
            summary, part = _parse_sse_page(
                raw,
                target=target,
                page=page,
                page_size=SSE_PAGE_SIZE,
            )
            evidence = self.cas.capture(
                raw,
                source_id=f"SSE_ANNOUNCEMENTS_{target.code}_{page}",
                role="ANNOUNCEMENT_PAGE",
                source_url=SSE_QUERY_URL,
                method="GET",
                retrieved_at=self._observed_at(),
                content_type=content_type,
            )
            if expected_total is None:
                expected_total = int(summary["total"])
                expected_page_count = max(
                    1, math.ceil(expected_total / SSE_PAGE_SIZE)
                )
            assert expected_page_count is not None
            if (
                int(summary["total"]) != expected_total
                or int(summary["page_count"]) != expected_page_count
            ):
                raise CorporateActionEvidenceBlockedError(
                    "SSE pagination totals changed during capture"
                )
            rows.extend(dict(item) for item in part)
            pages.append(
                {
                    "exchange": target.exchange,
                    "code": target.code,
                    "query_start": target.query_start,
                    "query_end": target.query_end,
                    "page_num": page,
                    "page_size": SSE_PAGE_SIZE,
                    "request": request,
                    "raw": evidence.to_dict(),
                }
            )
            if page >= expected_page_count:
                break
            page += 1
        if len(rows) != expected_total or len({str(item["URL"]) for item in rows}) != len(
            rows
        ):
            raise CorporateActionEvidenceBlockedError(
                "SSE pagination is incomplete or duplicated"
            )
        return rows, pages

    def _capture_sse_document(
        self,
        target: FrozenCorporateActionTarget,
        row: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        source_key = str(row["URL"])
        url = _sse_document_url(source_key, target.code)
        try:
            response = self.session.get(
                url,
                headers={
                    "Accept": "application/pdf",
                    "Referer": "https://www.sse.com.cn/",
                    "User-Agent": PROTOCOL_VERSION,
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CorporateActionEvidenceBlockedError(
                f"SSE PDF GET failed: {exc}"
            ) from exc
        response_content = bytes(getattr(response, "content", b""))
        response_type = str(response.headers.get("Content-Type") or "")
        try:
            content = _admit_response(
                response,
                expected_host="static.sse.com.cn",
                expected_path=urlsplit(url).path,
                expected_media="application/pdf",
                magic=b"%PDF-",
                maximum=cninfo.MAX_PDF_BYTES,
            )
        except CorporateActionEvidenceBlockedError as exc:
            if not response_content or len(response_content) > cninfo.MAX_PDF_BYTES:
                raise
            rejected = self.cas.capture(
                response_content,
                source_id=f"SSE_PDF_REJECTED_{Path(urlsplit(url).path).stem}",
                role="SOURCE_DOCUMENT_REJECTED",
                source_url=url,
                method="GET",
                retrieved_at=self._observed_at(),
                content_type=response_type,
            )
            return None, {
                "authority": SSE_SOURCE_AUTHORITY,
                "exchange": target.exchange,
                "code": target.code,
                "source_key": source_key,
                "http_status": int(getattr(response, "status_code", 0)),
                "reason": str(exc),
                "raw": rejected.to_dict(),
            }
        evidence = self.cas.capture(
            content,
            source_id=f"SSE_PDF_{Path(urlsplit(url).path).stem}",
            role="SOURCE_DOCUMENT",
            source_url=url,
            method="GET",
            retrieved_at=self._observed_at(),
            content_type=response_type,
        )
        return {
            "authority": SSE_SOURCE_AUTHORITY,
            "exchange": target.exchange,
            "code": target.code,
            "source_key": source_key,
            "raw": evidence.to_dict(),
        }, None

    def _get_sse_jsonp(self, request: Mapping[str, str]) -> tuple[bytes, str]:
        try:
            response = self.session.get(
                SSE_QUERY_URL,
                params=dict(request),
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": "https://www.sse.com.cn/",
                    "User-Agent": PROTOCOL_VERSION,
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CorporateActionEvidenceBlockedError(
                f"SSE catalog GET failed: {exc}"
            ) from exc
        content = _admit_response(
            response,
            expected_host="query.sse.com.cn",
            expected_path=urlsplit(SSE_QUERY_URL).path,
            expected_media="application/json",
            magic=f"{_SSE_CALLBACK}(".encode("ascii"),
            maximum=cninfo.MAX_PAGE_BYTES,
        )
        return content, str(response.headers.get("Content-Type") or "")

    def _observed_at(self) -> str:
        observed = self.clock()
        if not isinstance(observed, datetime):
            raise CorporateActionEvidenceBlockedError("clock must return datetime")
        try:
            return cninfo._canonical_datetime(observed)
        except cninfo.CninfoDelistedDisclosureBlockedError as exc:
            raise CorporateActionEvidenceBlockedError(str(exc)) from exc


def _assemble_manifest(
    *,
    cas: cninfo.CninfoDisclosureCAS,
    targets: Sequence[FrozenCorporateActionTarget],
    stock_master: Any,
    sse_pages: Any,
    cninfo_pages: Any,
    candidate_documents: Any,
    candidate_document_gaps: Any,
) -> dict[str, Any]:
    frozen_targets = _normalize_targets(targets)
    if not isinstance(stock_master, Mapping):
        raise CorporateActionEvidenceBlockedError("stock-master evidence is missing")
    if not all(
        isinstance(value, list)
        for value in (
            sse_pages,
            cninfo_pages,
            candidate_documents,
            candidate_document_gaps,
        )
    ):
        raise CorporateActionEvidenceBlockedError("catalog evidence arrays are missing")
    stock_raw = _read_raw(cas, stock_master, role="STOCK_MASTER")
    try:
        stock_rows = cninfo.parse_cninfo_stock_master(stock_raw)
    except cninfo.CninfoDelistedDisclosureBlockedError as exc:
        raise CorporateActionEvidenceBlockedError(str(exc)) from exc

    sse_catalogs: dict[str, list[dict[str, Any]]] = {
        target.code: [] for target in frozen_targets
    }
    cninfo_catalogs: dict[str, list[dict[str, Any]]] = {
        target.code: [] for target in frozen_targets
    }
    target_by_code = {target.code: target for target in frozen_targets}
    short_name_by_code: dict[str, str] = {}
    for target in frozen_targets:
        row = stock_rows.get(target.code[:6])
        if row is None:
            raise CorporateActionEvidenceBlockedError(
                f"stock master has no target {target.code}"
            )
        short_name_by_code[target.code] = str(row["zwjc"] or "").strip()

    sse_page_values = [dict(value) for value in sse_pages]
    cninfo_page_values = [dict(value) for value in cninfo_pages]
    _replay_sse_pages(cas, sse_page_values, target_by_code, sse_catalogs)
    _replay_cninfo_pages(cas, cninfo_page_values, target_by_code, cninfo_catalogs)

    normalized_catalogs: list[dict[str, Any]] = []
    reconciliation: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    for target in frozen_targets:
        short_name = short_name_by_code[target.code]
        sse_records = [
            _normalized_sse_catalog_record(target, row, short_name)
            for row in sse_catalogs[target.code]
        ]
        cninfo_records = [
            _normalized_cninfo_catalog_record(target, row, short_name)
            for row in cninfo_catalogs[target.code]
        ]
        normalized_catalogs.extend(sse_records)
        normalized_catalogs.extend(cninfo_records)
        sse_keys = Counter(
            (row["published_date"], row["normalized_title"])
            for row in sse_records
        )
        cninfo_keys = Counter(
            (row["published_date"], row["normalized_title"])
            for row in cninfo_records
        )
        dual_source_available = target.exchange == "SSE"
        catalogs_reconcile = dual_source_available and sse_keys == cninfo_keys
        candidates = [
            row for row in (*sse_records, *cninfo_records) if row["action_candidate"]
        ]
        all_candidates.extend(candidates)
        reconciliation.append(
            {
                "exchange": target.exchange,
                "code": target.code,
                "sse_catalog_count": len(sse_records),
                "cninfo_catalog_count": len(cninfo_records),
                "dual_source_available": dual_source_available,
                "catalogs_reconcile": catalogs_reconcile,
                "sse_only_count": sum((sse_keys - cninfo_keys).values()),
                "cninfo_only_count": sum((cninfo_keys - sse_keys).values()),
                "event_candidate_count": len(candidates),
                "zero_event_candidate": catalogs_reconcile and not candidates,
                "zero_event_proven": False,
                "factor_change_crosscheck_complete": False,
                "gp30_rows_emitted": 0,
                "gp43_rows_emitted": 0,
            }
        )

    documents = [dict(value) for value in candidate_documents]
    document_gaps = [dict(value) for value in candidate_document_gaps]
    document_keys = _replay_candidate_documents(cas, documents, target_by_code)
    document_gap_keys = _replay_candidate_document_gaps(
        cas, document_gaps, target_by_code
    )
    required_document_keys = {
        (str(row["authority"]), str(row["source_key"])) for row in all_candidates
    }
    if document_keys & document_gap_keys:
        raise CorporateActionEvidenceBlockedError(
            "an action candidate is both admitted and rejected"
        )
    if required_document_keys != document_keys | document_gap_keys:
        raise CorporateActionEvidenceBlockedError(
            "action candidate document coverage does not match catalogs"
        )
    normalized = {
        "catalog_records": sorted(
            normalized_catalogs,
            key=lambda row: (
                str(row["code"]),
                str(row["authority"]),
                str(row["published_date"]),
                str(row["source_key"]),
            ),
        ),
        "reconciliation": sorted(
            reconciliation, key=lambda row: (str(row["exchange"]), str(row["code"]))
        ),
        "event_candidates": sorted(
            all_candidates,
            key=lambda row: (
                str(row["code"]),
                str(row["published_date"]),
                str(row["authority"]),
                str(row["source_key"]),
            ),
        ),
        "structured_events": [],
    }
    logical_content_sha256 = _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": PROTOCOL_VERSION,
                "targets": [item.to_dict() for item in frozen_targets],
                "raw_hashes": sorted(
                    {
                        str(stock_master["content_hash"]),
                        *[
                            str(item["raw"]["content_hash"])
                            for item in (
                                *sse_page_values,
                                *cninfo_page_values,
                                *documents,
                                *document_gaps,
                            )
                        ],
                    }
                ),
                "normalized": normalized,
            }
        )
    )
    statistics = {
        "expected_target_count": EXPECTED_TARGET_COUNT,
        "target_count": len(frozen_targets),
        "full_target_scope": len(frozen_targets) == EXPECTED_TARGET_COUNT,
        "sse_target_count": sum(item.exchange == "SSE" for item in frozen_targets),
        "szse_target_count": sum(item.exchange == "SZSE" for item in frozen_targets),
        "sse_page_count": len(sse_page_values),
        "cninfo_page_count": len(cninfo_page_values),
        "catalog_record_count": len(normalized_catalogs),
        "sse_dual_source_reconciled_count": sum(
            bool(item["catalogs_reconcile"]) for item in reconciliation
        ),
        "zero_event_candidate_count": sum(
            bool(item["zero_event_candidate"]) for item in reconciliation
        ),
        "event_candidate_count": len(all_candidates),
        "candidate_document_count": len(documents),
        "candidate_document_gap_count": len(document_gaps),
        "structured_event_count": 0,
        "gp30_row_count": 0,
        "gp43_row_count": 0,
    }
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "targets": [item.to_dict() for item in frozen_targets],
        "stock_master": dict(stock_master),
        "sse_pages": sse_page_values,
        "cninfo_pages": cninfo_page_values,
        "candidate_documents": documents,
        "candidate_document_gaps": document_gaps,
        "normalized": normalized,
        "logical_content_sha256": logical_content_sha256,
        "source_contract": dict(_SOURCE_CONTRACT),
        "statistics": statistics,
        "ready": False,
    }


def _replay_sse_pages(
    cas: cninfo.CninfoDisclosureCAS,
    pages: Sequence[Mapping[str, Any]],
    target_by_code: Mapping[str, FrozenCorporateActionTarget],
    output: dict[str, list[dict[str, Any]]],
) -> None:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    expected_fields = {
        "exchange",
        "code",
        "query_start",
        "query_end",
        "page_num",
        "page_size",
        "request",
        "raw",
    }
    for value in pages:
        if set(value) != expected_fields:
            raise CorporateActionEvidenceBlockedError("SSE page evidence schema drift")
        code = str(value["code"])
        target = target_by_code.get(code)
        if target is None or target.exchange != "SSE":
            raise CorporateActionEvidenceBlockedError("SSE page escaped target scope")
        if (
            value["exchange"] != "SSE"
            or value["query_start"] != target.query_start
            or value["query_end"] != target.query_end
            or value["page_size"] != SSE_PAGE_SIZE
        ):
            raise CorporateActionEvidenceBlockedError("SSE page target identity drift")
        grouped.setdefault(code, []).append(value)
    for code, target in target_by_code.items():
        parts = sorted(grouped.get(code, []), key=lambda item: int(item["page_num"]))
        if target.exchange != "SSE":
            if parts:
                raise CorporateActionEvidenceBlockedError("SZSE target has SSE pages")
            continue
        if not parts or [int(item["page_num"]) for item in parts] != list(
            range(1, len(parts) + 1)
        ):
            raise CorporateActionEvidenceBlockedError("SSE pages are not contiguous")
        total: int | None = None
        page_count: int | None = None
        rows: list[dict[str, Any]] = []
        for item in parts:
            page = int(item["page_num"])
            if item["request"] != _sse_request(target, page):
                raise CorporateActionEvidenceBlockedError("SSE request contract drift")
            raw = _read_raw(cas, item["raw"], role="ANNOUNCEMENT_PAGE")
            summary, parsed = _parse_sse_page(
                raw,
                target=target,
                page=page,
                page_size=SSE_PAGE_SIZE,
            )
            if total is None:
                total = int(summary["total"])
                page_count = int(summary["page_count"])
            if summary["total"] != total or summary["page_count"] != page_count:
                raise CorporateActionEvidenceBlockedError("SSE replay totals changed")
            rows.extend(dict(row) for row in parsed)
        if page_count != len(parts) or total != len(rows):
            raise CorporateActionEvidenceBlockedError("SSE replay coverage is incomplete")
        if len({str(row["URL"]) for row in rows}) != len(rows):
            raise CorporateActionEvidenceBlockedError("SSE replay contains duplicate URLs")
        output[code] = rows


def _replay_cninfo_pages(
    cas: cninfo.CninfoDisclosureCAS,
    pages: Sequence[Mapping[str, Any]],
    target_by_code: Mapping[str, FrozenCorporateActionTarget],
    output: dict[str, list[dict[str, Any]]],
) -> None:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    expected_fields = {
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
    for value in pages:
        if set(value) != expected_fields:
            raise CorporateActionEvidenceBlockedError("CNINFO page evidence schema drift")
        code = str(value["code"])
        target = target_by_code.get(code)
        if target is None:
            raise CorporateActionEvidenceBlockedError("CNINFO page escaped target scope")
        if (
            value["exchange"] != target.exchange
            or value["query_start"] != target.query_start
            or value["query_end"] != target.query_end
            or value["page_size"] != cninfo.PAGE_SIZE
        ):
            raise CorporateActionEvidenceBlockedError("CNINFO page identity drift")
        grouped.setdefault(code, []).append(value)
    for code, target in target_by_code.items():
        parts = sorted(grouped.get(code, []), key=lambda item: int(item["page_num"]))
        if not parts or [int(item["page_num"]) for item in parts] != list(
            range(1, len(parts) + 1)
        ):
            raise CorporateActionEvidenceBlockedError("CNINFO pages are not contiguous")
        total: int | None = None
        page_count: int | None = None
        rows: list[dict[str, Any]] = []
        for item in parts:
            page = int(item["page_num"])
            raw = _read_raw(cas, item["raw"], role="ANNOUNCEMENT_PAGE")
            try:
                summary, parsed = cninfo._parse_announcement_page(
                    raw,
                    target=_to_cninfo_target(target),
                    org_id=str(item["org_id"]),
                )
            except cninfo.CninfoDelistedDisclosureBlockedError as exc:
                raise CorporateActionEvidenceBlockedError(str(exc)) from exc
            if item["request"] != cninfo._announcement_request(
                _to_cninfo_target(target), str(item["org_id"]), page
            ):
                raise CorporateActionEvidenceBlockedError("CNINFO request contract drift")
            observed_page_count = max(
                1, math.ceil(int(summary["total"]) / cninfo.PAGE_SIZE)
            )
            if total is None:
                total = int(summary["total"])
                page_count = observed_page_count
            if (
                summary["total"] != total
                or observed_page_count != page_count
                or summary["reported_totalpages"] != observed_page_count - 1
                or summary["has_more"] is not (page < observed_page_count)
            ):
                raise CorporateActionEvidenceBlockedError("CNINFO replay totals changed")
            rows.extend(dict(row) for row in parsed)
        if page_count != len(parts) or total != len(rows):
            raise CorporateActionEvidenceBlockedError(
                "CNINFO replay coverage is incomplete"
            )
        if len({str(row["announcementId"]) for row in rows}) != len(rows):
            raise CorporateActionEvidenceBlockedError(
                "CNINFO replay contains duplicate announcement IDs"
            )
        output[code] = rows


def _replay_candidate_documents(
    cas: cninfo.CninfoDisclosureCAS,
    documents: Sequence[Mapping[str, Any]],
    target_by_code: Mapping[str, FrozenCorporateActionTarget],
) -> set[tuple[str, str]]:
    expected_fields = {"authority", "exchange", "code", "source_key", "raw"}
    keys: set[tuple[str, str]] = set()
    for document in documents:
        if set(document) != expected_fields:
            raise CorporateActionEvidenceBlockedError(
                "candidate document evidence schema drift"
            )
        code = str(document["code"])
        target = target_by_code.get(code)
        authority = str(document["authority"])
        source_key = str(document["source_key"])
        if (
            target is None
            or document["exchange"] != target.exchange
            or authority not in {SSE_SOURCE_AUTHORITY, CNINFO_SOURCE_AUTHORITY}
        ):
            raise CorporateActionEvidenceBlockedError(
                "candidate document escaped target/source scope"
            )
        raw = _read_raw(cas, document["raw"], role="SOURCE_DOCUMENT")
        if not raw.startswith(b"%PDF-"):
            raise CorporateActionEvidenceBlockedError(
                "candidate document is not a PDF"
            )
        key = (authority, source_key)
        if key in keys:
            raise CorporateActionEvidenceBlockedError(
                "duplicate candidate document identity"
            )
        keys.add(key)
    return keys


def _replay_candidate_document_gaps(
    cas: cninfo.CninfoDisclosureCAS,
    gaps: Sequence[Mapping[str, Any]],
    target_by_code: Mapping[str, FrozenCorporateActionTarget],
) -> set[tuple[str, str]]:
    expected_fields = {
        "authority",
        "exchange",
        "code",
        "source_key",
        "http_status",
        "reason",
        "raw",
    }
    keys: set[tuple[str, str]] = set()
    for gap in gaps:
        if set(gap) != expected_fields:
            raise CorporateActionEvidenceBlockedError(
                "candidate document gap schema drift"
            )
        code = str(gap["code"])
        target = target_by_code.get(code)
        authority = str(gap["authority"])
        source_key = str(gap["source_key"])
        status = gap["http_status"]
        reason = str(gap["reason"] or "")
        if (
            target is None
            or gap["exchange"] != target.exchange
            or authority != SSE_SOURCE_AUTHORITY
            or type(status) is not int
            or status < 100
            or status > 599
            or not reason
        ):
            raise CorporateActionEvidenceBlockedError(
                "candidate document gap identity is invalid"
            )
        raw = _read_raw(cas, gap["raw"], role="SOURCE_DOCUMENT_REJECTED")
        if raw.startswith(b"%PDF-"):
            raise CorporateActionEvidenceBlockedError(
                "rejected candidate unexpectedly contains a PDF"
            )
        key = (authority, source_key)
        if key in keys:
            raise CorporateActionEvidenceBlockedError(
                "duplicate candidate document gap identity"
            )
        keys.add(key)
    return keys


def _read_raw(
    cas: cninfo.CninfoDisclosureCAS,
    value: Mapping[str, Any],
    *,
    role: str,
) -> bytes:
    try:
        evidence = cninfo._raw_from_mapping(value)
        content, expected_path = cas.read_blob(
            evidence.content_hash,
            expected_path=evidence.object_path,
        )
    except cninfo.CninfoDelistedDisclosureBlockedError as exc:
        raise CorporateActionEvidenceBlockedError(str(exc)) from exc
    if evidence.role != role or evidence.byte_count != len(content):
        raise CorporateActionEvidenceBlockedError("raw source identity mismatch")
    return content


def _normalized_sse_catalog_record(
    target: FrozenCorporateActionTarget,
    row: Mapping[str, Any],
    short_name: str,
) -> dict[str, Any]:
    title = _display_title(row["TITLE"])
    source_key = str(row["URL"])
    return {
        "authority": SSE_SOURCE_AUTHORITY,
        "exchange": target.exchange,
        "code": target.code,
        "published_date": str(row["SSEDATE"]),
        "title": title,
        "normalized_title": _reconciliation_title(title, short_name),
        "url": _sse_document_url(source_key, target.code),
        "source_key": source_key,
        "action_candidate": _is_action_candidate(title),
    }


def _normalized_cninfo_catalog_record(
    target: FrozenCorporateActionTarget,
    row: Mapping[str, Any],
    short_name: str,
) -> dict[str, Any]:
    title = _display_title(row["announcementTitle"])
    announcement_id = str(row["announcementId"])
    published_at, _precision, _effective, _status = cninfo._announcement_time(
        int(row["announcementTime"])
    )
    return {
        "authority": CNINFO_SOURCE_AUTHORITY,
        "exchange": target.exchange,
        "code": target.code,
        "published_date": published_at[:10],
        "title": title,
        "normalized_title": _reconciliation_title(title, short_name),
        "url": cninfo._normalize_pdf_url(row["adjunctUrl"], announcement_id),
        "source_key": announcement_id,
        "action_candidate": _is_action_candidate(title),
    }


def _parse_sse_page(
    raw: bytes,
    *,
    target: FrozenCorporateActionTarget,
    page: int,
    page_size: int,
) -> tuple[dict[str, int], tuple[dict[str, Any], ...]]:
    prefix = f"{_SSE_CALLBACK}(".encode("ascii")
    if not raw.startswith(prefix) or not raw.endswith(b")"):
        raise CorporateActionEvidenceBlockedError("SSE response is not frozen JSONP")
    try:
        value = json.loads(raw[len(prefix) : -1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorporateActionEvidenceBlockedError("SSE JSONP payload is invalid") from exc
    if not isinstance(value, dict) or set(value) != _SSE_TOP_FIELDS:
        raise CorporateActionEvidenceBlockedError("SSE top-level schema drift")
    if (
        value.get("productId") != target.code[:6]
        or value.get("beginDate") != target.query_start
        or value.get("endDate") != target.query_end
        or value.get("isPagination") != "true"
        or value.get("jsonCallBack") != _SSE_CALLBACK
        or value.get("keyWord") != ""
        or value.get("reportType") != "ALL"
        or value.get("reportType2") != ""
        or value.get("securityType") != _SSE_SECURITY_TYPES
    ):
        raise CorporateActionEvidenceBlockedError("SSE query identity mismatch")
    page_help = value.get("pageHelp")
    if not isinstance(page_help, dict) or set(page_help) != _SSE_PAGE_FIELDS:
        raise CorporateActionEvidenceBlockedError("SSE page schema drift")
    strict_integer_fields = (
        "beginPage",
        "cacheSize",
        "pageCount",
        "pageNo",
        "pageSize",
        "total",
    )
    for field in strict_integer_fields:
        if type(page_help.get(field)) is not int:
            raise CorporateActionEvidenceBlockedError(
                f"SSE {field} is not a strict integer"
            )
    total = int(page_help["total"])
    page_count = int(page_help["pageCount"])
    if (
        total < 0
        or page_help["beginPage"] != page
        or page_help["pageNo"] != page
        or page_help["pageSize"] != page_size
        or page_help["cacheSize"] != 1
        or page_count != max(1, math.ceil(total / page_size))
    ):
        raise CorporateActionEvidenceBlockedError("SSE pagination semantics drift")
    rows = page_help.get("data")
    if not isinstance(rows, list):
        raise CorporateActionEvidenceBlockedError("SSE page data is not an array")
    expected_rows = (
        page_size if page < page_count else total - page_size * (page_count - 1)
    )
    if len(rows) != expected_rows:
        raise CorporateActionEvidenceBlockedError("SSE page row count mismatch")
    normalized: list[dict[str, Any]] = []
    start = date.fromisoformat(target.query_start)
    end = date.fromisoformat(target.query_end)
    for row in rows:
        if not isinstance(row, dict) or set(row) != _SSE_ROW_FIELDS:
            raise CorporateActionEvidenceBlockedError("SSE announcement row schema drift")
        published = _iso_date(row["SSEDATE"], "SSEDATE")
        title = _display_title(row["TITLE"])
        if (
            str(row["SECURITY_CODE"]) != target.code[:6]
            or published < start
            or published > end
            or "<" in title
            or ">" in title
        ):
            raise CorporateActionEvidenceBlockedError(
                "SSE announcement row escaped target scope"
            )
        _sse_document_url(str(row["URL"]), target.code)
        try:
            datetime.strptime(str(row["ADDDATE"]), "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise CorporateActionEvidenceBlockedError(
                "SSE ADDDATE is invalid"
            ) from exc
        normalized.append(dict(row))
    return {"total": total, "page_count": page_count}, tuple(normalized)


def _sse_request(
    target: FrozenCorporateActionTarget, page: int
) -> dict[str, str]:
    if page <= 0:
        raise CorporateActionEvidenceBlockedError("SSE page must be positive")
    return {
        "jsonCallBack": _SSE_CALLBACK,
        "isPagination": "true",
        "productId": target.code[:6],
        "keyWord": "",
        "securityType": _SSE_SECURITY_TYPES,
        "reportType2": "",
        "reportType": "ALL",
        "beginDate": target.query_start,
        "endDate": target.query_end,
        "pageHelp.pageSize": str(SSE_PAGE_SIZE),
        "pageHelp.pageCount": "50",
        "pageHelp.pageNo": str(page),
        # SSE uses beginPage as the data offset. pageNo alone repeats page one.
        "pageHelp.beginPage": str(page),
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": str(page),
    }


def _normalize_targets(
    values: Sequence[FrozenCorporateActionTarget],
) -> tuple[FrozenCorporateActionTarget, ...]:
    if not values:
        raise CorporateActionEvidenceBlockedError("no corporate-action targets")
    output: list[FrozenCorporateActionTarget] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, FrozenCorporateActionTarget):
            raise TypeError("targets must contain FrozenCorporateActionTarget values")
        exchange = str(value.exchange).upper()
        suffix = ".SH" if exchange == "SSE" else ".SZ" if exchange == "SZSE" else ""
        code = str(value.code).upper()
        start = _iso_date(value.query_start, "query_start")
        end = _iso_date(value.query_end, "query_end")
        entity = str(value.canonical_entity_id).strip()
        if (
            not suffix
            or not re.fullmatch(r"\d{6}" + re.escape(suffix), code)
            or not entity
            or start < AUDIT_START
            or end > AUDIT_END
            or end < start
            or code in seen
        ):
            raise CorporateActionEvidenceBlockedError(
                "corporate-action target is invalid or duplicated"
            )
        seen.add(code)
        output.append(
            FrozenCorporateActionTarget(
                canonical_entity_id=entity,
                exchange=exchange,
                code=code,
                query_start=start.isoformat(),
                query_end=end.isoformat(),
            )
        )
    return tuple(sorted(output, key=lambda item: (item.exchange, item.code)))


def _to_cninfo_target(
    target: FrozenCorporateActionTarget,
) -> cninfo.FrozenDisclosureTarget:
    return cninfo.FrozenDisclosureTarget(
        canonical_entity_id=target.canonical_entity_id,
        exchange=target.exchange,
        code=target.code,
        query_start=target.query_start,
        query_end=target.query_end,
    )


def _reference(
    digest: str,
    path: Path,
    content: bytes,
    manifest: Mapping[str, Any],
) -> CorporateActionEvidenceReference:
    statistics = manifest["statistics"]
    return CorporateActionEvidenceReference(
        manifest_sha256=digest,
        byte_count=len(content),
        cas_uri=f"sha256:{digest}",
        object_path=str(path),
        target_count=int(statistics["target_count"]),
        sse_dual_source_reconciled_count=int(
            statistics["sse_dual_source_reconciled_count"]
        ),
        zero_event_candidate_count=int(statistics["zero_event_candidate_count"]),
        event_candidate_count=int(statistics["event_candidate_count"]),
        ready=False,
    )


def _admit_response(
    response: Any,
    *,
    expected_host: str,
    expected_path: str,
    expected_media: str,
    magic: bytes,
    maximum: int,
) -> bytes:
    if int(getattr(response, "status_code", 0)) != 200:
        raise CorporateActionEvidenceBlockedError("official source HTTP status is not 200")
    observed_url = urlsplit(str(getattr(response, "url", "")))
    if (
        observed_url.scheme != "https"
        or observed_url.hostname != expected_host
        or observed_url.port not in (None, 443)
        or observed_url.path != expected_path
    ):
        raise CorporateActionEvidenceBlockedError("official response URL drift")
    media = str(getattr(response, "headers", {}).get("Content-Type") or "")
    if media.split(";", 1)[0].strip().lower() != expected_media:
        raise CorporateActionEvidenceBlockedError("official response media type drift")
    content = bytes(getattr(response, "content", b""))
    if not content or len(content) > maximum or not content.startswith(magic):
        raise CorporateActionEvidenceBlockedError(
            "official response is empty, oversized, or has invalid magic"
        )
    return content


def _sse_document_url(path: str, code: str) -> str:
    value = str(path or "").strip()
    admitted_patterns = (
        rf"/disclosure/listedinfo/announcement/c/(?:new/)?\d{{4}}-\d{{2}}-\d{{2}}/{re.escape(code[:6])}_[A-Za-z0-9_\-]+\.pdf",
        rf"/disclosure/listedinfo/bulletin/star/c/{re.escape(code[:6])}_[A-Za-z0-9_\-]+\.pdf",
    )
    if (
        not any(re.fullmatch(pattern, value, re.IGNORECASE) for pattern in admitted_patterns)
        or "%" in value
        or ".." in value.split("/")
    ):
        raise CorporateActionEvidenceBlockedError("SSE PDF path escaped admitted scope")
    return SSE_DOCUMENT_ORIGIN + value


def _display_title(value: Any) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    if not title:
        raise CorporateActionEvidenceBlockedError("announcement title is empty")
    return title


def _reconciliation_title(title: str, short_name: str) -> str:
    value = _display_title(title)
    name = re.sub(r"\s+", "", str(short_name or ""))
    if name and value.replace(" ", "").startswith(name):
        value = value[len(short_name.strip()) :].lstrip(" :\uff1a")
    return _display_title(value)


def _is_action_candidate(title: str) -> bool:
    value = _display_title(title)
    return any(marker in value for marker in _ACTION_TITLE_MARKERS)


def _iso_date(value: Any, label: str) -> date:
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise CorporateActionEvidenceBlockedError(f"{label} is not ISO date") from exc
    if parsed.isoformat() != text:
        raise CorporateActionEvidenceBlockedError(f"{label} is not canonical")
    return parsed


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


__all__ = [
    "CorporateActionEvidenceBlockedError",
    "CorporateActionEvidenceReference",
    "FrozenCorporateActionTarget",
    "capture_official_corporate_action_evidence",
    "replay_official_corporate_action_evidence",
]
