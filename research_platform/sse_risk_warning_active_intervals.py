from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

from .sse_risk_warning_source import (
    PROTOCOL_VERSION as RISK_WARNING_PROTOCOL_VERSION,
    SOURCE_CONTRACT_ADMITTED,
    SSERiskWarningManifestStore,
    SSERiskWarningSourceBlockedError,
)
from .sse_risk_warning_transition_source import (
    FROZEN_TRANSITION,
    PROTOCOL_VERSION as TRANSITION_PROTOCOL_VERSION,
    SOURCE_STATUS as TRANSITION_SOURCE_STATUS,
    SSERiskWarningTransitionBlockedError,
    SSERiskWarningTransitionManifestStore,
)


PROTOCOL_VERSION = "sse-risk-warning-active-listing-intervals-v4"
SOURCE_STATUS = "SOURCE_CONTRACT_ADMITTED"
SOURCE_SCOPE = "SSE_CURRENT_RISK_WARNING_ACTIVE_LISTING_INTERVALS"
SOURCE_NAME = "sse_risk_warning_active_a_shares"

TRANSITION_BINDING_LAG = "LAG_IN_STATUS7"
TRANSITION_BINDING_CONVERGED = "CONVERGED_OUT_OF_STATUS7"
TRANSITION_BINDING_STATES = frozenset(
    {TRANSITION_BINDING_LAG, TRANSITION_BINDING_CONVERGED}
)

SSE_SHARE_LIST_PAGE_URL = "https://www.sse.com.cn/assortment/stock/list/share/"
SSE_QUERY_ENDPOINT = "https://query.sse.com.cn/commonQuery.do"
SSE_QUERY_HOST = "query.sse.com.cn"
SSE_SQL_ID = "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L"
SSE_COMPANY_STATUS = "7"
REQUEST_PAGE_SIZE = 2_000
MAX_PAGE_COUNT = 100
MAX_TOTAL_ROWS = 10_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_TOP_LEVEL_FIELDS = {
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
_PAGE_FIELDS = {
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
_ROW_FIELDS = {
    "AREA_NAME",
    "AREA_NAME_DESC",
    "A_STOCK_CODE",
    "B_STOCK_CODE",
    "COMPANY_ABBR",
    "COMPANY_ABBR_EN",
    "COMPANY_CODE",
    "CSRC_CODE",
    "CSRC_CODE_DESC",
    "DELIST_DATE",
    "FULL_NAME",
    "FULL_NAME_IN_ENGLISH",
    "LIST_BOARD",
    "LIST_DATE",
    "NUM",
    "PRODUCT_STATUS",
    "SEC_NAME_CN",
    "SEC_NAME_FULL",
    "STATE_CODE",
    "STATE_CODE_STOCK",
    "STOCK_TYPE",
}


class SSERiskWarningActiveIntervalsBlockedError(RuntimeError):
    """Official status-7 listing evidence failed its admitted contract."""

    def __init__(self, message: str, *, status: str = "SOURCE_REJECTED") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SSERiskWarningActiveInterval:
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
    name: str
    attributes: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["attributes"] = dict(sorted(dict(self.attributes).items()))
        return value


@dataclass(frozen=True)
class SSERiskWarningActiveRawEvidence:
    page_no: int
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
        value["response_summary"] = dict(
            sorted(dict(self.response_summary).items())
        )
        return value


@dataclass(frozen=True)
class SSERiskWarningActiveIntervalsArtifact:
    retrieved_at: str
    risk_warning_manifest_sha256: str
    transition_manifest_sha256: str
    transition_binding_state: str
    transition_code_alias: str
    transition_new_name: str
    transition_effective_date: str
    intervals: tuple[SSERiskWarningActiveInterval, ...]
    raw_responses: tuple[SSERiskWarningActiveRawEvidence, ...]
    source_snapshot_sha256: str
    logical_content_sha256: str
    risk_warning_code_count: int
    transition_lag_codes: tuple[str, ...]
    risk_warning_b_share_codes: tuple[str, ...]

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": False,
            "status": SOURCE_STATUS,
            "source_scope": SOURCE_SCOPE,
            "source_name": SOURCE_NAME,
            "allowed_use": "HISTORICAL_SECURITY_MASTER_LISTING_INTERVAL_EVIDENCE",
            "endpoint": SSE_QUERY_ENDPOINT,
            "company_status": SSE_COMPANY_STATUS,
            "method": "GET",
            "redirects_allowed": False,
            "pagination_mode": "SERVER_DECLARED_ALL_PAGES",
            "listing_date_origin": "SSE_STATUS_7_LIST_DATE_ONLY",
            "risk_warning_state_marker": "7|8",
            "transition_lag_state_marker": "7|4",
            "state_marker_4_allowed_only_for_fixed_transition": True,
            "risk_warning_manifest_protocol": RISK_WARNING_PROTOCOL_VERSION,
            "transition_manifest_protocol": TRANSITION_PROTOCOL_VERSION,
            "transition_binding_state": self.transition_binding_state,
            "transition_code_alias": self.transition_code_alias,
            "transition_new_name": self.transition_new_name,
            "transition_effective_date": self.transition_effective_date,
            "transition_evidence_may_only_explain_status_lag": True,
            "transition_evidence_may_create_listing_intervals": False,
            "caller_summary_trusted": False,
            "caller_ready_attestation_allowed": False,
            "historical_master_integration_allowed": True,
            "training_allowed": False,
            "label_generation_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        codes = [item.code_alias for item in self.intervals]
        state_marker_counts: dict[str, int] = {}
        for item in self.intervals:
            marker = (
                f"{item.attributes.get('state_code')}|"
                f"{item.attributes.get('state_code_stock')}"
            )
            state_marker_counts[marker] = state_marker_counts.get(marker, 0) + 1
        return {
            "interval_count": len(self.intervals),
            "risk_warning_interval_count": self.risk_warning_code_count,
            "transition_lag_interval_count": len(self.transition_lag_codes),
            "transition_lag_codes": list(self.transition_lag_codes),
            "risk_warning_b_share_excluded_count": len(
                self.risk_warning_b_share_codes
            ),
            "risk_warning_b_share_codes": list(self.risk_warning_b_share_codes),
            "page_count": len(self.raw_responses),
            "code_set_encoding": "canonical-json-sorted-suffixed-codes-utf8",
            "code_set_sha256": _sha256(_canonical_json_bytes(codes)),
            "risk_warning_manifest_sha256": self.risk_warning_manifest_sha256,
            "transition_manifest_sha256": self.transition_manifest_sha256,
            "transition_binding_state": self.transition_binding_state,
            "transition_code_alias": self.transition_code_alias,
            "transition_new_name": self.transition_new_name,
            "transition_effective_date": self.transition_effective_date,
            "state_marker_counts": dict(sorted(state_marker_counts.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "retrieved_at": self.retrieved_at,
            "risk_warning_manifest_sha256": self.risk_warning_manifest_sha256,
            "transition_manifest_sha256": self.transition_manifest_sha256,
            "transition_binding_state": self.transition_binding_state,
            "transition_code_alias": self.transition_code_alias,
            "transition_new_name": self.transition_new_name,
            "transition_effective_date": self.transition_effective_date,
            "intervals": [item.to_dict() for item in self.intervals],
            "raw_responses": [item.to_dict() for item in self.raw_responses],
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class SSERiskWarningActiveIntervalsManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str
    protocol_version: str = PROTOCOL_VERSION
    ready: bool = False
    status: str = SOURCE_STATUS
    training_allowed: bool = False
    trading_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _StatusRow:
    code: str
    listed_at: str
    company_code: str
    company_abbr: str
    security_name: str
    security_full_name: str
    legal_name: str
    b_stock_code: str | None
    list_board: str
    stock_type: str
    state_code: str
    state_code_stock: str
    row_number: int


@dataclass(frozen=True)
class _ParsedPage:
    page_no: int
    page_count: int
    total: int
    rows: tuple[_StatusRow, ...]


class SSERiskWarningActiveIntervalsCAS:
    """Immutable content-addressed storage for official pages and manifests."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        if not content:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "refusing to store an empty CAS blob"
            )
        digest = _sha256(content)
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(self.root, path, content)
        persisted = _stable_read(self.root, path, "SSE status-7 CAS object")
        if persisted != content or _sha256(persisted) != digest:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "CAS read-back verification failed"
            )
        return digest, path.resolve()

    def read_blob(self, digest: str) -> tuple[bytes, Path]:
        normalized = _strict_sha256(digest, "CAS")
        path = self.root / "sha256" / normalized[:2] / normalized
        content = _stable_read(self.root, path, "SSE status-7 CAS object")
        if _sha256(content) != normalized:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"CAS object hash mismatch: sha256:{normalized}"
            )
        return content, path.resolve()

    def capture(
        self,
        content: bytes,
        *,
        page_no: int,
        request_url: str,
        retrieved_at: str,
        content_type: str,
        http_status: int,
        response_summary: Mapping[str, Any],
        expected_sha256: str | None = None,
    ) -> SSERiskWarningActiveRawEvidence:
        digest = _verify_sha256(content, expected_sha256, f"page {page_no}")
        stored_digest, path = self.put_blob(content)
        if stored_digest != digest:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"page {page_no} CAS digest changed"
            )
        return SSERiskWarningActiveRawEvidence(
            page_no=page_no,
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


class SSERiskWarningActiveIntervalsManifestStore:
    """Seal and cold-replay status-7 evidence and both dependency manifests."""

    def __init__(
        self,
        cas: SSERiskWarningActiveIntervalsCAS,
        *,
        risk_warning_store: SSERiskWarningManifestStore,
        transition_store: SSERiskWarningTransitionManifestStore | None = None,
    ) -> None:
        if not isinstance(cas, SSERiskWarningActiveIntervalsCAS):
            raise TypeError("cas must be an SSERiskWarningActiveIntervalsCAS")
        if not isinstance(risk_warning_store, SSERiskWarningManifestStore):
            raise TypeError("risk_warning_store must be an SSERiskWarningManifestStore")
        if transition_store is not None and not isinstance(
            transition_store, SSERiskWarningTransitionManifestStore
        ):
            raise TypeError(
                "transition_store must be an SSERiskWarningTransitionManifestStore"
            )
        self.cas = cas
        self.risk_warning_store = risk_warning_store
        self.transition_store = transition_store

    def seal(
        self, artifact: SSERiskWarningActiveIntervalsArtifact
    ) -> SSERiskWarningActiveIntervalsManifestReference:
        payload = _manifest_payload(artifact)
        rebuilt = self._rebuild(payload)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != content:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "active-interval artifact is not reproducible from raw CAS bytes"
            )
        digest, path = self.cas.put_blob(content)
        return SSERiskWarningActiveIntervalsManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def replay(
        self, manifest_sha256: str
    ) -> SSERiskWarningActiveIntervalsArtifact:
        content, _path = self.cas.read_blob(manifest_sha256)
        payload = _decode_json_object(content, "active-interval manifest")
        if content != _canonical_json_bytes(payload):
            raise SSERiskWarningActiveIntervalsBlockedError(
                "active-interval manifest is not canonical JSON"
            )
        artifact = self._rebuild(payload)
        if content != _canonical_json_bytes(_manifest_payload(artifact)):
            raise SSERiskWarningActiveIntervalsBlockedError(
                "active-interval manifest does not replay exactly"
            )
        return artifact

    def _rebuild(
        self, payload: Mapping[str, Any]
    ) -> SSERiskWarningActiveIntervalsArtifact:
        expected_fields = {
            "intervals",
            "logical_content_sha256",
            "protocol_version",
            "raw_sources",
            "retrieved_at",
            "risk_warning_manifest_sha256",
            "source_contract",
            "source_snapshot_sha256",
            "statistics",
            "transition_binding_state",
            "transition_code_alias",
            "transition_effective_date",
            "transition_manifest_sha256",
            "transition_new_name",
        }
        if set(payload) != expected_fields:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "active-interval manifest schema drift detected"
            )
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "active-interval manifest protocol changed"
            )
        retrieved_at = _normalize_retrieved_at(payload.get("retrieved_at"))
        if retrieved_at != payload.get("retrieved_at"):
            raise SSERiskWarningActiveIntervalsBlockedError(
                "manifest retrieved_at is not canonical"
            )
        risk_manifest = _strict_sha256(
            payload.get("risk_warning_manifest_sha256"),
            "risk-warning manifest",
        )
        transition_manifest = _strict_sha256(
            payload.get("transition_manifest_sha256"), "transition manifest"
        )
        transition_binding_state = _normalize_transition_binding_state(
            payload.get("transition_binding_state")
        )
        transition_code_alias = _required_text(
            payload.get("transition_code_alias"), "transition_code_alias"
        )
        transition_new_name = _required_text(
            payload.get("transition_new_name"), "transition_new_name"
        )
        transition_effective_date = _parse_iso_date(
            payload.get("transition_effective_date"),
            "transition_effective_date",
        ).isoformat()
        sources = payload.get("raw_sources")
        if not isinstance(sources, list) or not sources:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "active-interval manifest raw source set is empty"
            )
        expected_source_fields = {
            "byte_count",
            "content_sha256",
            "content_type",
            "http_status",
            "method",
            "page_no",
            "request_url",
            "response_summary",
        }
        pages: list[tuple[_ParsedPage, SSERiskWarningActiveRawEvidence]] = []
        for expected_page, source in enumerate(sources, start=1):
            if not isinstance(source, dict) or set(source) != expected_source_fields:
                raise SSERiskWarningActiveIntervalsBlockedError(
                    "active-interval manifest source schema drift detected"
                )
            page_no = _strict_int(source.get("page_no"), "manifest page number")
            if page_no != expected_page:
                raise SSERiskWarningActiveIntervalsBlockedError(
                    "active-interval manifest page sequence changed"
                )
            request_url = str(source.get("request_url") or "")
            if request_url != build_page_request_url(page_no):
                raise SSERiskWarningActiveIntervalsBlockedError(
                    f"manifest request URL mismatch on page {page_no}"
                )
            if (
                source.get("method") != "GET"
                or source.get("content_type") != "application/json"
                or source.get("http_status") != 200
            ):
                raise SSERiskWarningActiveIntervalsBlockedError(
                    f"manifest transport contract changed on page {page_no}"
                )
            digest = _strict_sha256(source.get("content_sha256"), "page")
            raw, path = self.cas.read_blob(digest)
            if _strict_int(source.get("byte_count"), "manifest byte count") != len(raw):
                raise SSERiskWarningActiveIntervalsBlockedError(
                    f"manifest byte count mismatch on page {page_no}"
                )
            parsed = parse_status_page(raw, expected_page_no=page_no)
            summary = _page_summary(parsed)
            if source.get("response_summary") != summary:
                raise SSERiskWarningActiveIntervalsBlockedError(
                    f"manifest response summary mismatch on page {page_no}"
                )
            pages.append(
                (
                    parsed,
                    SSERiskWarningActiveRawEvidence(
                        page_no=page_no,
                        request_url=request_url,
                        method="GET",
                        retrieved_at=retrieved_at,
                        content_sha256=digest,
                        byte_count=len(raw),
                        content_type="application/json",
                        http_status=200,
                        cas_uri=f"sha256:{digest}",
                        object_path=str(path),
                        response_summary=summary,
                    ),
                )
            )
        artifact = _assemble_artifact(
            retrieved_at=retrieved_at,
            risk_warning_manifest_sha256=risk_manifest,
            risk_warning_store=self.risk_warning_store,
            transition_manifest_sha256=transition_manifest,
            transition_store=self.transition_store,
            parsed_pages=[item[0] for item in pages],
            evidence=[item[1] for item in pages],
        )
        if payload.get("intervals") != [
            item.to_dict() for item in artifact.intervals
        ]:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "manifest intervals do not match raw official pages"
            )
        if payload.get("source_snapshot_sha256") != artifact.source_snapshot_sha256:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "manifest source snapshot hash mismatch"
            )
        if payload.get("logical_content_sha256") != artifact.logical_content_sha256:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "manifest logical content hash mismatch"
            )
        if transition_binding_state != artifact.transition_binding_state:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "manifest transition binding state does not match raw official pages"
            )
        if (
            transition_code_alias != artifact.transition_code_alias
            or transition_new_name != artifact.transition_new_name
            or transition_effective_date != artifact.transition_effective_date
        ):
            raise SSERiskWarningActiveIntervalsBlockedError(
                "manifest transition identity does not match cold-replayed dependency"
            )
        if payload.get("source_contract") != artifact.source_contract:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "manifest source contract changed"
            )
        if payload.get("statistics") != artifact.statistics:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "manifest statistics do not match raw official pages"
            )
        return artifact


class SSERiskWarningActiveIntervalsClient:
    """GET-only official SSE status-7 catalogue reader."""

    def __init__(
        self,
        *,
        cas: SSERiskWarningActiveIntervalsCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(cas, SSERiskWarningActiveIntervalsCAS):
            raise TypeError("cas must be an SSERiskWarningActiveIntervalsCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)

    def fetch_current(
        self,
        *,
        risk_warning_manifest_sha256: str,
        risk_warning_store: SSERiskWarningManifestStore,
        transition_manifest_sha256: str,
        transition_store: SSERiskWarningTransitionManifestStore,
        retrieved_at: str | None = None,
        expected_page_hashes: Mapping[int, str] | None = None,
    ) -> SSERiskWarningActiveIntervalsArtifact:
        if not isinstance(risk_warning_store, SSERiskWarningManifestStore):
            raise TypeError("risk_warning_store must be an SSERiskWarningManifestStore")
        if not isinstance(transition_store, SSERiskWarningTransitionManifestStore):
            raise TypeError(
                "transition_store must be an SSERiskWarningTransitionManifestStore"
            )
        retrieved = _normalize_retrieved_at(retrieved_at)
        pending_hashes = dict(expected_page_hashes or {})
        if any(not isinstance(key, int) or isinstance(key, bool) or key < 1 for key in pending_hashes):
            raise SSERiskWarningActiveIntervalsBlockedError(
                "expected page hash keys must be positive integers"
            )

        parsed_pages: list[_ParsedPage] = []
        evidence: list[SSERiskWarningActiveRawEvidence] = []
        first = self._fetch_page(
            1,
            retrieved_at=retrieved,
            expected_sha256=pending_hashes.pop(1, None),
        )
        parsed_pages.append(first[0])
        evidence.append(first[1])
        page_count = first[0].page_count
        if page_count > MAX_PAGE_COUNT or first[0].total > MAX_TOTAL_ROWS:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "status-7 pagination exceeds admitted safety bounds"
            )
        for page_no in range(2, page_count + 1):
            parsed, captured = self._fetch_page(
                page_no,
                retrieved_at=retrieved,
                expected_sha256=pending_hashes.pop(page_no, None),
            )
            if parsed.page_count != page_count or parsed.total != first[0].total:
                raise SSERiskWarningActiveIntervalsBlockedError(
                    f"status-7 pagination metadata drifted on page {page_no}"
                )
            parsed_pages.append(parsed)
            evidence.append(captured)
        if pending_hashes:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"unused expected page hashes: {sorted(pending_hashes)}"
            )
        return _assemble_artifact(
            retrieved_at=retrieved,
            risk_warning_manifest_sha256=_strict_sha256(
                risk_warning_manifest_sha256, "risk-warning manifest"
            ),
            risk_warning_store=risk_warning_store,
            transition_manifest_sha256=_strict_sha256(
                transition_manifest_sha256, "transition manifest"
            ),
            transition_store=transition_store,
            parsed_pages=parsed_pages,
            evidence=evidence,
        )

    def _fetch_page(
        self,
        page_no: int,
        *,
        retrieved_at: str,
        expected_sha256: str | None,
    ) -> tuple[_ParsedPage, SSERiskWarningActiveRawEvidence]:
        request_url = build_page_request_url(page_no)
        raw, content_type, response_url, status_code = self._get(request_url)
        parsed = parse_status_page(
            raw,
            expected_page_no=page_no,
            expected_sha256=expected_sha256,
        )
        captured = self.cas.capture(
            raw,
            page_no=page_no,
            request_url=response_url,
            retrieved_at=retrieved_at,
            content_type=content_type,
            http_status=status_code,
            response_summary=_page_summary(parsed),
            expected_sha256=expected_sha256,
        )
        return parsed, captured

    def _get(self, request_url: str) -> tuple[bytes, str, str, int]:
        _validate_request_url(request_url)
        try:
            response = self.session.get(
                request_url,
                headers={
                    "User-Agent": (
                        "tdx-research-platform/"
                        "sse-risk-warning-active-listing-intervals-v1"
                    ),
                    "Referer": SSE_SHARE_LIST_PAGE_URL,
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "official SSE status-7 GET failed closed"
            ) from exc
        status = _strict_int(getattr(response, "status_code", None), "HTTP status")
        if status != 200:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"official SSE status-7 GET failed closed: HTTP {status}"
            )
        response_url = str(getattr(response, "url", "") or "")
        _validate_request_url(response_url)
        if response_url != request_url:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "official SSE status-7 response URL changed"
            )
        headers = getattr(response, "headers", {})
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].lower()
        if content_type != "application/json":
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"official SSE status-7 content type changed: {content_type!r}"
            )
        content = bytes(getattr(response, "content", b""))
        if not content or len(content) > MAX_RESPONSE_BYTES:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "official SSE status-7 response is empty or oversized"
            )
        return content, content_type, response_url, status


def build_page_request_url(page_no: int) -> str:
    page = _strict_int(page_no, "requested page number")
    if page < 1:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid requested page number: {page_no!r}"
        )
    query = (
        ("sqlId", SSE_SQL_ID),
        ("isPagination", "true"),
        ("STOCK_CODE", ""),
        ("CSRC_CODE", ""),
        ("REG_PROVINCE", ""),
        ("STOCK_TYPE", "1,8"),
        ("COMPANY_STATUS", SSE_COMPANY_STATUS),
        ("type", "inParams"),
        ("pageHelp.cacheSize", "1"),
        ("pageHelp.beginPage", str(page)),
        ("pageHelp.pageSize", str(REQUEST_PAGE_SIZE)),
        ("pageHelp.pageNo", str(page)),
    )
    url = f"{SSE_QUERY_ENDPOINT}?{urlencode(query)}"
    _validate_request_url(url)
    return url


def parse_status_page(
    raw_bytes: bytes,
    *,
    expected_page_no: int,
    expected_sha256: str | None = None,
) -> _ParsedPage:
    page_expected = _strict_int(expected_page_no, "expected page number")
    if page_expected < 1:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "expected page number must be positive"
        )
    if not raw_bytes or len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} is empty or oversized"
        )
    _verify_sha256(raw_bytes, expected_sha256, f"page {page_expected}")
    payload = _decode_json_object(raw_bytes, f"status-7 page {page_expected}")
    if set(payload) != _TOP_LEVEL_FIELDS:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} top-level schema drift detected"
        )
    if (
        payload.get("actionErrors") != []
        or payload.get("actionMessages") != []
        or payload.get("fieldErrors") != {}
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} contains API errors or messages"
        )
    if (
        payload.get("isPagination") != "true"
        or payload.get("jsonCallBack") is not None
        or payload.get("pageNo") is not None
        or payload.get("pageSize") is not None
        or payload.get("queryDate") != ""
        or payload.get("securityCode") != ""
        or payload.get("sqlId") != SSE_SQL_ID
        or payload.get("texts") is not None
        or payload.get("type") != "inParams"
        or payload.get("validateCode") != ""
        or not isinstance(payload.get("locale"), str)
        or not payload.get("locale")
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} response contract changed"
        )
    page = payload.get("pageHelp")
    result = payload.get("result")
    if not isinstance(page, dict) or set(page) != _PAGE_FIELDS:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} pageHelp schema drift detected"
        )
    if not isinstance(result, list) or not result:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} returned no result rows"
        )
    if result != page.get("data"):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} result and pageHelp.data diverged"
        )
    if any(
        page.get(field) is not None
        for field in (
            "endDate",
            "endPage",
            "objectResult",
            "searchDate",
            "sort",
            "startDate",
        )
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} pagination filters changed"
        )
    page_no = _strict_int(page.get("pageNo"), "response page number")
    page_count = _strict_int(page.get("pageCount"), "response page count")
    total = _strict_int(page.get("total"), "response total")
    if (
        _strict_int(page.get("beginPage"), "response beginPage") != page_expected
        or page_no != page_expected
        or _strict_int(page.get("cacheSize"), "response cacheSize") != 1
        or _strict_int(page.get("pageSize"), "response pageSize")
        != REQUEST_PAGE_SIZE
        or _strict_int(
            page.get("pageSizeWithOutLimit"),
            "response pageSizeWithOutLimit",
        )
        != REQUEST_PAGE_SIZE
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} pagination contract changed"
        )
    if (
        page_count < 1
        or page_no > page_count
        or total < 1
        or total > MAX_TOTAL_ROWS
        or len(result) > REQUEST_PAGE_SIZE
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} pagination is invalid"
        )
    expected_rows = (
        REQUEST_PAGE_SIZE
        if page_no < page_count
        else total - REQUEST_PAGE_SIZE * (page_count - 1)
    )
    if expected_rows <= 0 or len(result) != expected_rows:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page {page_expected} row count is incomplete: "
            f"rows={len(result)}, expected={expected_rows}"
        )

    rows: list[_StatusRow] = []
    previous_code = ""
    first_number = (page_no - 1) * REQUEST_PAGE_SIZE + 1
    for offset, row in enumerate(result):
        if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 page {page_expected} row schema drift detected"
            )
        code = _required_text(row.get("A_STOCK_CODE"), "A_STOCK_CODE")
        if not re.fullmatch(r"\d{6}", code) or not code.startswith(
            ("600", "601", "603", "605", "688", "689")
        ):
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 page contains unadmitted A-share code: {code!r}"
            )
        if previous_code and code <= previous_code:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "status-7 rows are not strictly ordered by A-share code"
            )
        previous_code = code
        company_code = _required_text(row.get("COMPANY_CODE"), "COMPANY_CODE")
        if company_code != code:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 company identity conflicts with A-share code: {code}"
            )
        listed_at = _parse_list_date(row.get("LIST_DATE"))
        if str(row.get("DELIST_DATE") or "").strip() != "-":
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 active row unexpectedly has DELIST_DATE: {code}"
            )
        list_board = _required_text(row.get("LIST_BOARD"), "LIST_BOARD")
        stock_type = _required_text(row.get("STOCK_TYPE"), "STOCK_TYPE")
        expected_board = ("2", "8") if code.startswith(("688", "689")) else ("1", "1")
        if (list_board, stock_type) != expected_board:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 board classification conflicts with code: {code}"
            )
        state_code = _required_text(row.get("STATE_CODE"), "STATE_CODE")
        state_code_stock = _required_text(
            row.get("STATE_CODE_STOCK"), "STATE_CODE_STOCK"
        )
        transition_raw_code = FROZEN_TRANSITION.code
        marker_is_admitted = state_code == SSE_COMPANY_STATUS and (
            state_code_stock == "8"
            or (code == transition_raw_code and state_code_stock == "4")
        )
        if not marker_is_admitted:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 state markers changed for {code}"
            )
        row_number = _strict_digit_text(row.get("NUM"), "NUM")
        if row_number != first_number + offset:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 NUM sequence changed at {code}"
            )
        b_value = _required_text(row.get("B_STOCK_CODE"), "B_STOCK_CODE")
        b_stock_code: str | None
        if b_value == "-":
            b_stock_code = None
        elif re.fullmatch(r"900\d{3}", b_value):
            b_stock_code = f"{b_value}.SH"
        else:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 row has invalid B-share code: {b_value!r}"
            )
        company_abbr = _bounded_text(row.get("COMPANY_ABBR"), "COMPANY_ABBR")
        security_name = _bounded_text(row.get("SEC_NAME_CN"), "SEC_NAME_CN")
        security_full_name = _bounded_text(
            row.get("SEC_NAME_FULL"), "SEC_NAME_FULL"
        )
        legal_name = _bounded_text(row.get("FULL_NAME"), "FULL_NAME", maximum=256)
        for field in (
            "AREA_NAME",
            "AREA_NAME_DESC",
            "COMPANY_ABBR_EN",
            "CSRC_CODE",
            "CSRC_CODE_DESC",
            "FULL_NAME_IN_ENGLISH",
            "PRODUCT_STATUS",
        ):
            _bounded_text(row.get(field), field, maximum=512, strip_result=False)
        rows.append(
            _StatusRow(
                code=f"{code}.SH",
                listed_at=listed_at,
                company_code=company_code,
                company_abbr=company_abbr,
                security_name=security_name,
                security_full_name=security_full_name,
                legal_name=legal_name,
                b_stock_code=b_stock_code,
                list_board=list_board,
                stock_type=stock_type,
                state_code=state_code,
                state_code_stock=state_code_stock,
                row_number=row_number,
            )
        )
    return _ParsedPage(
        page_no=page_no,
        page_count=page_count,
        total=total,
        rows=tuple(rows),
    )


def _assemble_artifact(
    *,
    retrieved_at: str,
    risk_warning_manifest_sha256: str,
    risk_warning_store: SSERiskWarningManifestStore,
    transition_manifest_sha256: str,
    transition_store: SSERiskWarningTransitionManifestStore,
    parsed_pages: Sequence[_ParsedPage],
    evidence: Sequence[SSERiskWarningActiveRawEvidence],
) -> SSERiskWarningActiveIntervalsArtifact:
    retrieved = _normalize_retrieved_at(retrieved_at)
    if not parsed_pages or len(parsed_pages) != len(evidence):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "status-7 parsed pages and evidence are incomplete"
        )
    page_count = parsed_pages[0].page_count
    total = parsed_pages[0].total
    if page_count != len(parsed_pages):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 page set is incomplete: {len(parsed_pages)}/{page_count}"
        )
    rows: list[_StatusRow] = []
    previous_code = ""
    for expected_page, (page, raw) in enumerate(
        zip(parsed_pages, evidence, strict=True), start=1
    ):
        if (
            page.page_no != expected_page
            or raw.page_no != expected_page
            or page.page_count != page_count
            or page.total != total
        ):
            raise SSERiskWarningActiveIntervalsBlockedError(
                "status-7 page sequence or metadata is inconsistent"
            )
        for row in page.rows:
            if previous_code and row.code <= previous_code:
                raise SSERiskWarningActiveIntervalsBlockedError(
                    "status-7 code order changed across pages"
                )
            previous_code = row.code
            rows.append(row)
    if len(rows) != total or len({item.code for item in rows}) != total:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 total or uniqueness closure failed: rows={len(rows)}, total={total}"
        )

    try:
        risk_artifact = risk_warning_store.replay(risk_warning_manifest_sha256)
    except (SSERiskWarningSourceBlockedError, OSError, ValueError, TypeError) as exc:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "admitted risk-warning manifest failed cold replay"
        ) from exc
    contract = risk_artifact.source_contract
    if (
        contract.get("ready") is not True
        or contract.get("status") != SOURCE_CONTRACT_ADMITTED
        or risk_artifact.to_dict().get("protocol_version")
        != RISK_WARNING_PROTOCOL_VERSION
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "risk-warning dependency is not an admitted source manifest"
        )
    risk_retrieved = datetime.fromisoformat(risk_artifact.retrieved_at)
    status_retrieved = datetime.fromisoformat(retrieved)
    if risk_retrieved > status_retrieved:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "risk-warning dependency was retrieved after the status-7 observation"
        )
    risk_a = {
        item.code: item
        for item in risk_artifact.securities
        if item.share_class == "A"
    }
    risk_b = sorted(
        item.code for item in risk_artifact.securities if item.share_class == "B"
    )
    if not risk_a:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "risk-warning dependency has no A-share codes"
        )
    row_by_code = {item.code: item for item in rows}
    status_codes = set(row_by_code)
    missing = sorted(set(risk_a) - status_codes)
    if missing:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"status-7 catalogue is missing admitted risk-warning codes: {missing[:20]}"
        )
    status_b = sorted(item.b_stock_code for item in rows if item.b_stock_code)
    if status_b != risk_b:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "status-7 B-share identities do not match the admitted risk-warning manifest"
        )
    for code, security in risk_a.items():
        row = row_by_code[code]
        if row.company_abbr != security.name:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"risk-warning name conflicts with status-7 identity: {code}"
            )
        if (
            row.state_code != SSE_COMPANY_STATUS
            or row.state_code_stock != "8"
        ):
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"risk-warning state marker conflicts with admitted status-7 identity: {code}"
            )

    extras = sorted(status_codes - set(risk_a))
    if not isinstance(transition_store, SSERiskWarningTransitionManifestStore):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "transition manifest was supplied without a replayable store"
        )
    try:
        transition_artifact = transition_store.replay(
            transition_manifest_sha256
        )
    except (SSERiskWarningTransitionBlockedError, OSError, ValueError, TypeError) as exc:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "transition manifest failed cold replay"
        ) from exc
    transition_contract = transition_artifact.source_contract
    transition_payload = transition_artifact.to_dict()
    if (
        transition_contract.get("status") != TRANSITION_SOURCE_STATUS
        or transition_contract.get("ready") is not False
        or transition_payload.get("protocol_version") != TRANSITION_PROTOCOL_VERSION
        or transition_contract.get("training_allowed") is not False
        or transition_contract.get("trading_allowed") is not False
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "transition dependency is not the admitted audit-only protocol"
        )
    transition = transition_artifact.transition
    transition_code = str(transition.code_alias)
    if not re.fullmatch(r"\d{6}\.SH", transition_code):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "transition dependency code_alias is not a canonical SSE code"
        )
    effective_date = _parse_iso_date(
        transition.effective_date, "transition effective_date"
    )
    transition_new_name = str(transition.new_name)
    if (
        transition_code != f"{FROZEN_TRANSITION.code}.SH"
        or str(transition.legal_name) != FROZEN_TRANSITION.legal_name
        or str(transition.old_name) != FROZEN_TRANSITION.old_name
        or transition_new_name != FROZEN_TRANSITION.new_name
        or effective_date.isoformat() != FROZEN_TRANSITION.effective_date
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "transition dependency does not match the fixed admitted transition"
        )
    observation_date = datetime.fromisoformat(retrieved).date()
    if effective_date > observation_date:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "transition evidence is not effective at the status-7 observation time"
        )
    if transition_code in risk_a:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "transition code remains in the admitted risk-warning set"
        )

    transition_codes: tuple[str, ...]
    if extras == [transition_code]:
        transition_binding_state = TRANSITION_BINDING_LAG
        row = row_by_code[transition_code]
        if (
            row.legal_name != transition.legal_name
            or row.company_abbr != transition.old_name
            or row.security_name != transition.new_name
            or row.security_full_name != transition.new_name
            or row.state_code != SSE_COMPANY_STATUS
            or row.state_code_stock != "4"
        ):
            raise SSERiskWarningActiveIntervalsBlockedError(
                "transition identity does not match the official status-7 row"
            )
        transition_codes = (transition_code,)
    elif not extras and transition_code not in status_codes:
        transition_binding_state = TRANSITION_BINDING_CONVERGED
        transition_codes = ()
    else:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"transition evidence does not exactly explain status-7 extras: {extras[:20]}"
        )

    source_snapshot_hash = _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": PROTOCOL_VERSION,
                "source_url": build_page_request_url(1),
                "page_hashes": [item.content_sha256 for item in evidence],
                "risk_warning_manifest_sha256": risk_warning_manifest_sha256,
                "transition_manifest_sha256": transition_manifest_sha256,
                "transition_binding_state": transition_binding_state,
                "transition_code_alias": transition_code,
                "transition_new_name": transition_new_name,
                "transition_effective_date": effective_date.isoformat(),
            }
        )
    )
    observation_date = datetime.fromisoformat(retrieved).date()
    intervals: list[SSERiskWarningActiveInterval] = []
    transition_set = set(transition_codes)
    for row in rows:
        if _parse_iso_date(row.listed_at, "LIST_DATE") > observation_date:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"status-7 LIST_DATE is after retrieval time: {row.code}"
            )
        role = (
            "ADMITTED_TRANSITION_STATUS_LAG"
            if row.code in transition_set
            else "CURRENT_RISK_WARNING"
        )
        intervals.append(
            SSERiskWarningActiveInterval(
                canonical_entity_id=f"CN:SSE:{row.company_code}",
                exchange="SSE",
                code_alias=row.code,
                board=("STAR" if row.code.startswith(("688", "689")) else "SSE_MAIN"),
                listed_at=row.listed_at,
                delisted_at=None,
                valid_from=row.listed_at,
                valid_to=None,
                event_type="ACTIVE_LISTING",
                source_url=build_page_request_url(1),
                source_hash=source_snapshot_hash,
                retrieved_at=retrieved,
                name=row.security_name,
                attributes={
                    "b_stock_code": row.b_stock_code or "",
                    "company_code": row.company_code,
                    "list_board": row.list_board,
                    "risk_binding_role": role,
                    "security_full_name": row.security_full_name,
                    "state_code": row.state_code,
                    "state_code_stock": row.state_code_stock,
                    "stock_type": row.stock_type,
                },
            )
        )
    logical_hash = _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": PROTOCOL_VERSION,
                "retrieved_at": retrieved,
                "risk_warning_manifest_sha256": risk_warning_manifest_sha256,
                "transition_manifest_sha256": transition_manifest_sha256,
                "transition_binding_state": transition_binding_state,
                "transition_code_alias": transition_code,
                "transition_new_name": transition_new_name,
                "transition_effective_date": effective_date.isoformat(),
                "source_snapshot_sha256": source_snapshot_hash,
                "intervals": [item.to_dict() for item in intervals],
            }
        )
    )
    return SSERiskWarningActiveIntervalsArtifact(
        retrieved_at=retrieved,
        risk_warning_manifest_sha256=risk_warning_manifest_sha256,
        transition_manifest_sha256=transition_manifest_sha256,
        transition_binding_state=transition_binding_state,
        transition_code_alias=transition_code,
        transition_new_name=transition_new_name,
        transition_effective_date=effective_date.isoformat(),
        intervals=tuple(intervals),
        raw_responses=tuple(evidence),
        source_snapshot_sha256=source_snapshot_hash,
        logical_content_sha256=logical_hash,
        risk_warning_code_count=len(risk_a),
        transition_lag_codes=transition_codes,
        risk_warning_b_share_codes=tuple(risk_b),
    )


def _page_summary(page: _ParsedPage) -> dict[str, Any]:
    return {
        "first_code": page.rows[0].code,
        "last_code": page.rows[-1].code,
        "page_count": page.page_count,
        "page_no": page.page_no,
        "row_count": len(page.rows),
        "total": page.total,
    }


def _manifest_payload(
    artifact: SSERiskWarningActiveIntervalsArtifact,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "retrieved_at": artifact.retrieved_at,
        "risk_warning_manifest_sha256": artifact.risk_warning_manifest_sha256,
        "transition_manifest_sha256": artifact.transition_manifest_sha256,
        "transition_binding_state": artifact.transition_binding_state,
        "transition_code_alias": artifact.transition_code_alias,
        "transition_new_name": artifact.transition_new_name,
        "transition_effective_date": artifact.transition_effective_date,
        "intervals": [item.to_dict() for item in artifact.intervals],
        "raw_sources": [
            {
                "page_no": item.page_no,
                "request_url": item.request_url,
                "method": item.method,
                "content_sha256": item.content_sha256,
                "byte_count": item.byte_count,
                "content_type": item.content_type,
                "http_status": item.http_status,
                "response_summary": dict(
                    sorted(dict(item.response_summary).items())
                ),
            }
            for item in artifact.raw_responses
        ],
        "source_snapshot_sha256": artifact.source_snapshot_sha256,
        "logical_content_sha256": artifact.logical_content_sha256,
        "source_contract": artifact.source_contract,
        "statistics": artifact.statistics,
    }


def _validate_request_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != SSE_QUERY_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/commonQuery.do"
        or parsed.fragment
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "official SSE status-7 request origin changed"
        )
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) != len(dict(query)):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "official SSE status-7 request has duplicate parameters"
        )
    values = dict(query)
    expected_keys = {
        "sqlId",
        "isPagination",
        "STOCK_CODE",
        "CSRC_CODE",
        "REG_PROVINCE",
        "STOCK_TYPE",
        "COMPANY_STATUS",
        "type",
        "pageHelp.cacheSize",
        "pageHelp.beginPage",
        "pageHelp.pageSize",
        "pageHelp.pageNo",
    }
    if set(values) != expected_keys:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "official SSE status-7 request parameter schema changed"
        )
    page = values.get("pageHelp.pageNo", "")
    if not re.fullmatch(r"[1-9]\d*", page):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "official SSE status-7 request page is invalid"
        )
    expected = dict(
        parse_qsl(build_page_request_url_unchecked(int(page)).split("?", 1)[1], keep_blank_values=True)
    )
    if values != expected:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "official SSE status-7 request parameters are unadmitted"
        )


def build_page_request_url_unchecked(page_no: int) -> str:
    query = (
        ("sqlId", SSE_SQL_ID),
        ("isPagination", "true"),
        ("STOCK_CODE", ""),
        ("CSRC_CODE", ""),
        ("REG_PROVINCE", ""),
        ("STOCK_TYPE", "1,8"),
        ("COMPANY_STATUS", SSE_COMPANY_STATUS),
        ("type", "inParams"),
        ("pageHelp.cacheSize", "1"),
        ("pageHelp.beginPage", str(page_no)),
        ("pageHelp.pageSize", str(REQUEST_PAGE_SIZE)),
        ("pageHelp.pageNo", str(page_no)),
    )
    return f"{SSE_QUERY_ENDPOINT}?{urlencode(query)}"


def _decode_json_object(raw_bytes: bytes, label: str) -> dict[str, Any]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"{label} is not UTF-8"
        ) from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except SSERiskWarningActiveIntervalsBlockedError:
        raise
    except json.JSONDecodeError as exc:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"{label} is invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"{label} must be a JSON object"
        )
    return value


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"duplicate JSON key in SSE status-7 evidence: {key!r}"
            )
        value[key] = item
    return value


def _normalize_retrieved_at(value: Any | None) -> str:
    if value is None:
        parsed = datetime.now().astimezone()
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"invalid retrieved_at timestamp: {value!r}"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "retrieved_at must include a UTC offset"
            )
    return parsed.isoformat(timespec="seconds")


def _parse_list_date(value: Any) -> str:
    text = _required_text(value, "LIST_DATE")
    if not re.fullmatch(r"\d{8}", text):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid SSE LIST_DATE: {text!r}"
        )
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid SSE LIST_DATE: {text!r}"
        ) from exc


def _parse_iso_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid {label}: {value!r}"
        ) from exc


def _normalize_transition_binding_state(value: Any) -> str:
    if not isinstance(value, str) or value not in TRANSITION_BINDING_STATES:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid transition binding state: {value!r}"
        )
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid {label}: {value!r}"
        )
    return value


def _bounded_text(
    value: Any,
    label: str,
    *,
    maximum: int = 128,
    strip_result: bool = True,
) -> str:
    if not isinstance(value, str):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid {label}: {value!r}"
        )
    result = value.strip() if strip_result else value
    if (
        not result.strip()
        or len(result) > maximum
        or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", result)
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid {label}: {value!r}"
        )
    return result


def _strict_digit_text(value: Any, label: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d*", value):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid {label}: {value!r}"
        )
    return int(value)


def _strict_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid {label}: {value!r}"
        )
    return value


def _strict_sha256(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"invalid {label} SHA-256"
        )
    return digest


def _verify_sha256(content: bytes, expected: str | None, label: str) -> str:
    actual = _sha256(content)
    if expected is None:
        return actual
    normalized = _strict_sha256(expected, f"expected {label}")
    if actual != normalized:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"{label} source hash mismatch: expected {normalized}, got {actual}"
        )
    return actual


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


def _path_is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(value.st_mode):
        return True
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    return bool(attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_safe_existing_chain(root: Path, target: Path) -> None:
    root_abs = root.absolute()
    target_abs = target.absolute()
    try:
        relative = target_abs.relative_to(root_abs)
    except ValueError as exc:
        raise SSERiskWarningActiveIntervalsBlockedError(
            "CAS path escapes its fixed root"
        ) from exc
    current = root_abs
    if current.exists() and _path_is_link_or_reparse(current):
        raise SSERiskWarningActiveIntervalsBlockedError(
            "CAS root is a link or reparse point"
        )
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _path_is_link_or_reparse(
            current
        ):
            raise SSERiskWarningActiveIntervalsBlockedError(
                "CAS path contains a link, junction, or reparse point"
            )


def _stable_read(root: Path, path: Path, label: str) -> bytes:
    _assert_safe_existing_chain(root, path)
    if not path.is_file() or _path_is_link_or_reparse(path):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"{label} is missing or unsafe"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"{label} cannot be opened as a stable file"
        ) from exc
    try:
        before = os.fstat(descriptor)
        attributes = int(getattr(before, "st_file_attributes", 0) or 0)
        if not stat.S_ISREG(before.st_mode) or (
            attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"{label} is not a plain regular file"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_fingerprint = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_fingerprint = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if (
        before_fingerprint != after_fingerprint
        or _path_is_link_or_reparse(path)
        or len(content) != before.st_size
    ):
        raise SSERiskWarningActiveIntervalsBlockedError(
            f"{label} changed during read"
        )
    return content


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    _assert_safe_existing_chain(root, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_existing_chain(root, path.parent)
    if path.exists():
        if _stable_read(root, path, "existing SSE status-7 CAS object") != content:
            raise SSERiskWarningActiveIntervalsBlockedError(
                f"content-address collision or corruption: {path}"
            )
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _assert_safe_existing_chain(root, temporary)
        if _stable_read(root, temporary, "SSE status-7 CAS temporary") != content:
            raise SSERiskWarningActiveIntervalsBlockedError(
                "CAS temporary changed during write"
            )
        if path.exists():
            if _stable_read(root, path, "existing SSE status-7 CAS object") != content:
                raise SSERiskWarningActiveIntervalsBlockedError(
                    f"content-address collision or corruption: {path}"
                )
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if _stable_read(
                    root, path, "concurrent SSE status-7 CAS object"
                ) != content:
                    raise SSERiskWarningActiveIntervalsBlockedError(
                        "concurrent CAS write produced different content"
                    )
            except OSError as exc:
                if path.exists() and _stable_read(
                    root, path, "concurrent SSE status-7 CAS object"
                ) == content:
                    pass
                else:
                    raise SSERiskWarningActiveIntervalsBlockedError(
                        "CAS object could not be published without overwrite"
                    ) from exc
    finally:
        if temporary.exists() and not _path_is_link_or_reparse(temporary):
            temporary.unlink()


__all__ = [
    "PROTOCOL_VERSION",
    "REQUEST_PAGE_SIZE",
    "SOURCE_SCOPE",
    "SOURCE_NAME",
    "SOURCE_STATUS",
    "SSE_COMPANY_STATUS",
    "SSE_SHARE_LIST_PAGE_URL",
    "TRANSITION_BINDING_CONVERGED",
    "TRANSITION_BINDING_LAG",
    "TRANSITION_BINDING_STATES",
    "SSERiskWarningActiveInterval",
    "SSERiskWarningActiveIntervalsArtifact",
    "SSERiskWarningActiveIntervalsBlockedError",
    "SSERiskWarningActiveIntervalsCAS",
    "SSERiskWarningActiveIntervalsClient",
    "SSERiskWarningActiveIntervalsManifestReference",
    "SSERiskWarningActiveIntervalsManifestStore",
    "build_page_request_url",
    "parse_status_page",
]
