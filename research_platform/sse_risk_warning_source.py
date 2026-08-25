from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests


PROTOCOL_VERSION = "sse-risk-warning-current-list-v1"
SOURCE_CONTRACT_ADMITTED = "SOURCE_CONTRACT_ADMITTED"
SOURCE_CONTRACT_UNADMITTED = "SOURCE_CONTRACT_UNADMITTED"

SSE_RISK_PLATE_PAGE_URL = (
    "https://www.sse.com.cn/disclosure/listedinfo/riskplate/"
)
SSE_RISK_PLATE_SCRIPT_URL = (
    "https://www.sse.com.cn/xhtml/home/2021public/querySearch/"
    "search_listedCompanyInfo_2021.js"
)
SSE_QUERY_ENDPOINT = "https://query.sse.com.cn/commonSoaQuery.do"
SSE_QUERY_HOST = "query.sse.com.cn"

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_ROWS = 10_000


class SSERiskWarningSourceBlockedError(RuntimeError):
    """The official list did not satisfy the admitted read-only contract."""

    def __init__(self, message: str, *, status: str = "SOURCE_REJECTED") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SSERiskWarningSourceSpec:
    source_id: str
    market_segment: str
    callback: str
    sql_id: str
    query_items: tuple[tuple[str, str], ...]
    code_field: str
    name_field: str
    admitted_code_prefixes: tuple[str, ...]
    response_type: str


SOURCE_SPECS: tuple[SSERiskWarningSourceSpec, ...] = (
    SSERiskWarningSourceSpec(
        source_id="MAIN_BOARD_RISK_WARNING",
        market_segment="MAIN_BOARD",
        callback="jsonpCallbackSseRiskMain",
        sql_id="PL_SSGSXX_FXJSBGPLB",
        query_items=(("domesticIndicator", "S"), ("productType", "0")),
        code_field="INSTRUMENT_ID",
        name_field="INSTRUMENT_SHORT",
        admitted_code_prefixes=("600", "601", "603", "605", "900"),
        response_type="",
    ),
    SSERiskWarningSourceSpec(
        source_id="STAR_MARKET_RISK_WARNING",
        market_segment="STAR_MARKET",
        callback="jsonpCallbackSseRiskStar",
        sql_id="SSE_PL_SSGSXX_KCBFXTS_L",
        query_items=(("type", "0"),),
        code_field="secCode",
        name_field="secNameCn",
        admitted_code_prefixes=("688", "689"),
        response_type="0",
    ),
)


@dataclass(frozen=True)
class SSERiskWarningSecurity:
    code: str
    name: str
    market_segment: str
    share_class: str
    source_id: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SSERiskWarningRawEvidence:
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
        value["response_summary"] = dict(
            sorted(dict(self.response_summary).items())
        )
        return value


@dataclass(frozen=True)
class SSERiskWarningListArtifact:
    retrieved_at: str
    securities: tuple[SSERiskWarningSecurity, ...]
    raw_responses: tuple[SSERiskWarningRawEvidence, ...]
    logical_content_sha256: str
    source_totals: Mapping[str, int]

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": True,
            "status": SOURCE_CONTRACT_ADMITTED,
            "scope": "CURRENT_RISK_WARNING_SECURITIES",
            "excludes": ["DELISTING_PERIOD_SECURITIES"],
            "page_url": SSE_RISK_PLATE_PAGE_URL,
            "ui_script_url": SSE_RISK_PLATE_SCRIPT_URL,
            "endpoint": SSE_QUERY_ENDPOINT,
            "method": "GET",
            "transport": "JSONP",
            "pagination_mode": "SERVER_DECLARED_UNPAGINATED_FULL_RESPONSE",
            "truncation_closed_by": [
                "isPagination=false",
                "pageCount=1 AND pageNo=1",
                "total=pageSize=pageSizeWithOutLimit=result_count",
                "pageHelp.data EXACTLY EQUALS result",
            ],
            "pagination_transition_policy": SOURCE_CONTRACT_UNADMITTED,
            "caller_attestation_allowed": False,
            "audit_only": True,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        a_share_codes = sorted(
            item.code for item in self.securities if item.share_class == "A"
        )
        return {
            "total_rows": len(self.securities),
            "a_share_rows": len(a_share_codes),
            "b_share_rows_excluded_from_a_share_set": sum(
                item.share_class == "B" for item in self.securities
            ),
            "main_board_rows": sum(
                item.market_segment == "MAIN_BOARD" for item in self.securities
            ),
            "star_market_rows": sum(
                item.market_segment == "STAR_MARKET" for item in self.securities
            ),
            "a_share_code_set_encoding": (
                "canonical-json-sorted-suffixed-codes-utf8"
            ),
            "a_share_code_set_sha256": _sha256(
                _canonical_json_bytes(a_share_codes)
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "retrieved_at": self.retrieved_at,
            "securities": [item.to_dict() for item in self.securities],
            "raw_responses": [item.to_dict() for item in self.raw_responses],
            "logical_content_sha256": self.logical_content_sha256,
            "source_totals": dict(sorted(self.source_totals.items())),
            "source_contract": self.source_contract,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class SSERiskWarningManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ParsedSource:
    securities: tuple[SSERiskWarningSecurity, ...]
    total: int
    page_size: int
    page_size_without_limit: int


class SSERiskWarningRawCAS:
    """Immutable content-addressed storage for the exact official bytes."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

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
    ) -> SSERiskWarningRawEvidence:
        digest = _verify_sha256(content, expected_sha256, source_id)
        stored_digest, path = self.put_blob(content)
        if stored_digest != digest:
            raise SSERiskWarningSourceBlockedError(
                f"{source_id} raw response CAS digest changed"
            )
        return SSERiskWarningRawEvidence(
            source_id=source_id,
            request_url=request_url,
            method="GET",
            retrieved_at=_normalize_retrieved_at(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            http_status=http_status,
            cas_uri=f"sha256:{digest}",
            object_path=str(path.resolve()),
            response_summary=dict(response_summary),
        )

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        if not content:
            raise SSERiskWarningSourceBlockedError("refusing to store an empty CAS blob")
        digest = _sha256(content)
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(path, content)
        persisted = path.read_bytes()
        if persisted != content or _sha256(persisted) != digest:
            raise SSERiskWarningSourceBlockedError("CAS read-back verification failed")
        return digest, path.resolve()

    def read_blob(self, digest: str) -> tuple[bytes, Path]:
        normalized = str(digest).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise SSERiskWarningSourceBlockedError("invalid CAS SHA-256")
        path = self.root / "sha256" / normalized[:2] / normalized
        if not path.is_file() or path.is_symlink():
            raise SSERiskWarningSourceBlockedError(
                f"CAS object is missing or unsafe: sha256:{normalized}"
            )
        content = path.read_bytes()
        if _sha256(content) != normalized:
            raise SSERiskWarningSourceBlockedError(
                f"CAS object hash mismatch: sha256:{normalized}"
            )
        return content, path.resolve()


class SSERiskWarningManifestStore:
    """Seal and replay the two-source artifact without trusting derived rows."""

    def __init__(self, cas: SSERiskWarningRawCAS) -> None:
        if not isinstance(cas, SSERiskWarningRawCAS):
            raise TypeError("cas must be an SSERiskWarningRawCAS")
        self.cas = cas

    def seal(
        self, artifact: SSERiskWarningListArtifact
    ) -> SSERiskWarningManifestReference:
        payload = _manifest_payload(artifact)
        rebuilt = self._rebuild_from_manifest_payload(payload)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != content:
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning artifact is not reproducible from raw CAS bytes"
            )
        digest, path = self.cas.put_blob(content)
        return SSERiskWarningManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def replay(self, manifest_sha256: str) -> SSERiskWarningListArtifact:
        content, _path = self.cas.read_blob(manifest_sha256)
        payload = _decode_canonical_json_object(content, "risk-warning manifest")
        if content != _canonical_json_bytes(payload):
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest is not canonical JSON"
            )
        rebuilt = self._rebuild_from_manifest_payload(payload)
        if content != _canonical_json_bytes(_manifest_payload(rebuilt)):
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest does not replay exactly"
            )
        return rebuilt

    def _rebuild_from_manifest_payload(
        self, payload: Mapping[str, Any]
    ) -> SSERiskWarningListArtifact:
        expected_manifest_fields = {
            "logical_content_sha256",
            "protocol_version",
            "retrieved_at",
            "securities",
            "source_contract",
            "source_totals",
            "sources",
            "statistics",
        }
        if set(payload) != expected_manifest_fields:
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest schema drift detected"
            )
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest protocol changed"
            )
        retrieved_at = _normalize_retrieved_at(payload.get("retrieved_at"))
        if retrieved_at != payload.get("retrieved_at"):
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest retrieved_at is not canonical"
            )
        sources = payload.get("sources")
        if not isinstance(sources, list) or len(sources) != len(SOURCE_SPECS):
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest source set is incomplete"
            )

        securities: list[SSERiskWarningSecurity] = []
        evidence: list[SSERiskWarningRawEvidence] = []
        totals: dict[str, int] = {}
        seen_codes: set[str] = set()
        expected_source_fields = {
            "byte_count",
            "content_sha256",
            "content_type",
            "http_status",
            "method",
            "request_url",
            "response_summary",
            "source_id",
        }
        for spec, source in zip(SOURCE_SPECS, sources, strict=True):
            if not isinstance(source, dict) or set(source) != expected_source_fields:
                raise SSERiskWarningSourceBlockedError(
                    "SSE risk-warning manifest source schema drift detected"
                )
            if source.get("source_id") != spec.source_id:
                raise SSERiskWarningSourceBlockedError(
                    "SSE risk-warning manifest source order or identity changed"
                )
            request_url = str(source.get("request_url") or "")
            if request_url != build_source_request_url(spec):
                raise SSERiskWarningSourceBlockedError(
                    f"{spec.source_id} manifest request URL mismatch"
                )
            if (
                source.get("method") != "GET"
                or source.get("content_type") != "application/json"
                or source.get("http_status") != 200
            ):
                raise SSERiskWarningSourceBlockedError(
                    f"{spec.source_id} manifest transport contract changed"
                )
            digest = str(source.get("content_sha256") or "").lower()
            raw, raw_path = self.cas.read_blob(digest)
            if _strict_int(source.get("byte_count"), "byte_count") != len(raw):
                raise SSERiskWarningSourceBlockedError(
                    f"{spec.source_id} manifest byte count mismatch"
                )
            parsed = parse_source_response(
                raw,
                spec=spec,
                expected_sha256=digest,
            )
            summary = _response_summary(parsed)
            if source.get("response_summary") != summary:
                raise SSERiskWarningSourceBlockedError(
                    f"{spec.source_id} manifest response summary mismatch"
                )
            for security in parsed.securities:
                if security.code in seen_codes:
                    raise SSERiskWarningSourceBlockedError(
                        f"duplicate SSE risk-warning code during replay: "
                        f"{security.code}"
                    )
                seen_codes.add(security.code)
                securities.append(security)
            totals[spec.source_id] = parsed.total
            evidence.append(
                SSERiskWarningRawEvidence(
                    source_id=spec.source_id,
                    request_url=request_url,
                    method="GET",
                    retrieved_at=retrieved_at,
                    content_sha256=digest,
                    byte_count=len(raw),
                    content_type="application/json",
                    http_status=200,
                    cas_uri=f"sha256:{digest}",
                    object_path=str(raw_path),
                    response_summary=summary,
                )
            )
        artifact = _assemble_artifact(
            retrieved_at=retrieved_at,
            securities=securities,
            evidence=evidence,
            source_totals=totals,
        )
        if payload.get("securities") != [
            item.to_dict() for item in artifact.securities
        ]:
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest securities do not match raw bytes"
            )
        if payload.get("source_totals") != dict(sorted(totals.items())):
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest source totals do not match raw bytes"
            )
        if payload.get("logical_content_sha256") != artifact.logical_content_sha256:
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest logical hash mismatch"
            )
        if payload.get("source_contract") != artifact.source_contract:
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest source contract changed"
            )
        if payload.get("statistics") != artifact.statistics:
            raise SSERiskWarningSourceBlockedError(
                "SSE risk-warning manifest statistics do not match raw bytes"
            )
        return artifact


class SSERiskWarningSourceClient:
    """GET-only reader for the two official current risk-warning lists."""

    def __init__(
        self,
        *,
        cas: SSERiskWarningRawCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(cas, SSERiskWarningRawCAS):
            raise TypeError("cas must be an SSERiskWarningRawCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)

    def fetch_current(
        self,
        *,
        retrieved_at: str | None = None,
        expected_hashes: Mapping[str, str] | None = None,
    ) -> SSERiskWarningListArtifact:
        retrieved = _normalize_retrieved_at(retrieved_at)
        pending_hashes = dict(expected_hashes or {})
        unknown_hash_keys = sorted(
            set(pending_hashes) - {spec.source_id for spec in SOURCE_SPECS}
        )
        if unknown_hash_keys:
            raise SSERiskWarningSourceBlockedError(
                f"unknown expected source hashes: {unknown_hash_keys}"
            )

        securities: list[SSERiskWarningSecurity] = []
        evidence: list[SSERiskWarningRawEvidence] = []
        source_totals: dict[str, int] = {}
        seen_codes: set[str] = set()
        for spec in SOURCE_SPECS:
            request_url = build_source_request_url(spec)
            raw, content_type, response_url, status_code = self._get(request_url)
            expected_hash = pending_hashes.pop(spec.source_id, None)
            _verify_sha256(raw, expected_hash, spec.source_id)
            parsed = parse_source_response(
                raw,
                spec=spec,
                expected_sha256=expected_hash,
            )
            for security in parsed.securities:
                if security.code in seen_codes:
                    raise SSERiskWarningSourceBlockedError(
                        f"duplicate SSE risk-warning code across sources: "
                        f"{security.code}"
                    )
                seen_codes.add(security.code)
                securities.append(security)
            source_totals[spec.source_id] = parsed.total
            evidence.append(
                self.cas.capture(
                    raw,
                    source_id=spec.source_id,
                    request_url=response_url,
                    retrieved_at=retrieved,
                    content_type=content_type,
                    http_status=status_code,
                    response_summary=_response_summary(parsed),
                    expected_sha256=expected_hash,
                )
            )
        if pending_hashes:
            raise SSERiskWarningSourceBlockedError(
                f"unused expected source hashes: {sorted(pending_hashes)}"
            )
        return _assemble_artifact(
            retrieved_at=retrieved,
            securities=securities,
            evidence=evidence,
            source_totals=source_totals,
        )

    def _get(self, request_url: str) -> tuple[bytes, str, str, int]:
        _validate_official_request_url(request_url)
        try:
            response = self.session.get(
                request_url,
                headers={
                    "User-Agent": "tdx-research-platform/sse-risk-warning-v1",
                    "Referer": SSE_RISK_PLATE_PAGE_URL,
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SSERiskWarningSourceBlockedError(
                "official SSE risk-warning GET failed closed"
            ) from exc
        status_code = _strict_int(
            getattr(response, "status_code", None), "HTTP status"
        )
        if status_code != 200:
            raise SSERiskWarningSourceBlockedError(
                f"official SSE risk-warning GET failed closed: HTTP {status_code}"
            )
        response_url = str(getattr(response, "url", "") or "")
        _validate_official_request_url(response_url)
        if response_url != request_url:
            raise SSERiskWarningSourceBlockedError(
                "official SSE risk-warning response URL changed"
            )
        headers = getattr(response, "headers", {})
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].lower()
        if content_type != "application/json":
            raise SSERiskWarningSourceBlockedError(
                "official SSE risk-warning content type changed: "
                f"{content_type!r}"
            )
        content = bytes(getattr(response, "content", b""))
        if not content or len(content) > MAX_RESPONSE_BYTES:
            raise SSERiskWarningSourceBlockedError(
                "official SSE risk-warning response is empty or oversized"
            )
        return content, content_type, response_url, status_code


def _response_summary(parsed: _ParsedSource) -> dict[str, Any]:
    return {
        "page_count": 1,
        "page_size": parsed.page_size,
        "page_size_without_limit": parsed.page_size_without_limit,
        "pagination": "UNPAGINATED_FULL_RESPONSE",
        "row_count": len(parsed.securities),
        "total": parsed.total,
    }


def _assemble_artifact(
    *,
    retrieved_at: str,
    securities: Sequence[SSERiskWarningSecurity],
    evidence: Sequence[SSERiskWarningRawEvidence],
    source_totals: Mapping[str, int],
) -> SSERiskWarningListArtifact:
    sorted_securities = sorted(securities, key=lambda item: item.code)
    if len({item.code for item in sorted_securities}) != len(sorted_securities):
        raise SSERiskWarningSourceBlockedError(
            "duplicate SSE risk-warning code in combined artifact"
        )
    if [item.source_id for item in evidence] != [
        spec.source_id for spec in SOURCE_SPECS
    ]:
        raise SSERiskWarningSourceBlockedError(
            "SSE risk-warning evidence source set is incomplete or reordered"
        )
    expected_total_keys = {spec.source_id for spec in SOURCE_SPECS}
    if set(source_totals) != expected_total_keys:
        raise SSERiskWarningSourceBlockedError(
            "SSE risk-warning source total set is incomplete"
        )
    logical_hash = _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": PROTOCOL_VERSION,
                "records": [item.to_dict() for item in sorted_securities],
                "raw_hashes": {
                    item.source_id: item.content_sha256 for item in evidence
                },
            }
        )
    )
    return SSERiskWarningListArtifact(
        retrieved_at=_normalize_retrieved_at(retrieved_at),
        securities=tuple(sorted_securities),
        raw_responses=tuple(evidence),
        logical_content_sha256=logical_hash,
        source_totals=dict(sorted(source_totals.items())),
    )


def _manifest_payload(artifact: SSERiskWarningListArtifact) -> dict[str, Any]:
    return {
        "logical_content_sha256": artifact.logical_content_sha256,
        "protocol_version": PROTOCOL_VERSION,
        "retrieved_at": artifact.retrieved_at,
        "securities": [item.to_dict() for item in artifact.securities],
        "source_contract": artifact.source_contract,
        "source_totals": dict(sorted(artifact.source_totals.items())),
        "statistics": artifact.statistics,
        "sources": [
            {
                "byte_count": item.byte_count,
                "content_sha256": item.content_sha256,
                "content_type": item.content_type,
                "http_status": item.http_status,
                "method": item.method,
                "request_url": item.request_url,
                "response_summary": dict(
                    sorted(dict(item.response_summary).items())
                ),
                "source_id": item.source_id,
            }
            for item in artifact.raw_responses
        ],
    }


def build_source_request_url(spec: SSERiskWarningSourceSpec) -> str:
    if spec not in SOURCE_SPECS:
        raise SSERiskWarningSourceBlockedError(
            "unadmitted SSE risk-warning source specification",
            status=SOURCE_CONTRACT_UNADMITTED,
        )
    query_items = (
        ("jsonCallBack", spec.callback),
        ("sqlId", spec.sql_id),
        *spec.query_items,
    )
    url = f"{SSE_QUERY_ENDPOINT}?{urlencode(query_items)}"
    _validate_official_request_url(url)
    return url


def parse_source_response(
    raw_bytes: bytes,
    *,
    spec: SSERiskWarningSourceSpec,
    expected_sha256: str | None = None,
) -> _ParsedSource:
    if spec not in SOURCE_SPECS:
        raise SSERiskWarningSourceBlockedError(
            "unadmitted SSE risk-warning source specification",
            status=SOURCE_CONTRACT_UNADMITTED,
        )
    if not raw_bytes or len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} response is empty or oversized"
        )
    _verify_sha256(raw_bytes, expected_sha256, spec.source_id)
    payload = _decode_jsonp(raw_bytes, callback=spec.callback)
    expected_top_level = {
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
    if set(payload) != expected_top_level:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} top-level schema drift detected"
        )
    if (
        payload.get("actionErrors") != []
        or payload.get("actionMessages") != []
        or payload.get("fieldErrors") != {}
    ):
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} response contains API errors or messages"
        )
    if payload.get("jsonCallBack") != spec.callback:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} JSONP callback echo mismatch"
        )
    if payload.get("sqlId") != spec.sql_id:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} SQL contract mismatch"
        )
    if payload.get("isPagination") != "false":
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} pagination contract changed",
            status=SOURCE_CONTRACT_UNADMITTED,
        )
    if payload.get("pageNo") is not None or payload.get("pageSize") is not None:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} top-level pagination metadata changed",
            status=SOURCE_CONTRACT_UNADMITTED,
        )
    if (
        payload.get("queryDate") != ""
        or payload.get("securityCode") != ""
        or payload.get("texts") is not None
        or payload.get("type") != spec.response_type
        or payload.get("validateCode") != ""
        or not isinstance(payload.get("locale"), str)
        or not payload.get("locale")
    ):
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} response filter metadata changed"
        )

    page = payload.get("pageHelp")
    expected_page_fields = {
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
    if not isinstance(page, dict) or set(page) != expected_page_fields:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} pageHelp schema drift detected"
        )
    if _strict_int(page.get("beginPage"), "beginPage") != 0:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} beginPage contract changed",
            status=SOURCE_CONTRACT_UNADMITTED,
        )
    if _strict_int(page.get("cacheSize"), "cacheSize") != 1:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} cacheSize contract changed"
        )
    page_count = _strict_int(page.get("pageCount"), "pageCount")
    page_no = _strict_int(page.get("pageNo"), "pageNo")
    if page_count != 1 or page_no != 1:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} became paginated; no official page parameter "
            "contract is admitted",
            status=SOURCE_CONTRACT_UNADMITTED,
        )
    if page.get("endPage") is not None:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} endPage contract changed",
            status=SOURCE_CONTRACT_UNADMITTED,
        )
    if any(
        page.get(field) is not None
        for field in (
            "endDate",
            "objectResult",
            "searchDate",
            "sort",
            "startDate",
        )
    ):
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} page filter metadata changed"
        )

    result = payload.get("result")
    page_data = page.get("data")
    if not isinstance(result, list) or not isinstance(page_data, list):
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} result rows are invalid"
        )
    if not result:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} returned an empty list"
        )
    if result != page_data:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} result and pageHelp.data diverged"
        )
    total = _strict_int(page.get("total"), "total")
    page_size = _strict_int(page.get("pageSize"), "pageSize")
    page_size_without_limit = _strict_int(
        page.get("pageSizeWithOutLimit"), "pageSizeWithOutLimit"
    )
    if total > MAX_SOURCE_ROWS:
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} total exceeds the admitted safety bound"
        )
    if not (
        total
        == page_size
        == page_size_without_limit
        == len(result)
    ):
        raise SSERiskWarningSourceBlockedError(
            f"{spec.source_id} full-response upper-limit closure failed"
        )

    expected_row_fields = {spec.code_field, spec.name_field}
    records: list[SSERiskWarningSecurity] = []
    seen_codes: set[str] = set()
    previous_code = ""
    for row in result:
        if not isinstance(row, dict) or set(row) != expected_row_fields:
            raise SSERiskWarningSourceBlockedError(
                f"{spec.source_id} row schema drift detected"
            )
        code = _strict_text(row.get(spec.code_field), "security code")
        name = _strict_text(row.get(spec.name_field), "security name")
        if not re.fullmatch(r"\d{6}", code) or not code.startswith(
            spec.admitted_code_prefixes
        ):
            raise SSERiskWarningSourceBlockedError(
                f"{spec.source_id} contains an unadmitted security code: {code!r}"
            )
        if len(name) > 64 or re.search(r"[\x00-\x1f\x7f]", name):
            raise SSERiskWarningSourceBlockedError(
                f"{spec.source_id} contains an invalid security name"
            )
        if code in seen_codes:
            raise SSERiskWarningSourceBlockedError(
                f"{spec.source_id} contains duplicate code: {code}"
            )
        if previous_code and code <= previous_code:
            raise SSERiskWarningSourceBlockedError(
                f"{spec.source_id} code order changed or is non-unique"
            )
        seen_codes.add(code)
        previous_code = code
        records.append(
            SSERiskWarningSecurity(
                code=f"{code}.SH",
                name=name,
                market_segment=spec.market_segment,
                share_class="B" if code.startswith("900") else "A",
                source_id=spec.source_id,
            )
        )
    return _ParsedSource(
        securities=tuple(records),
        total=total,
        page_size=page_size,
        page_size_without_limit=page_size_without_limit,
    )


def _decode_canonical_json_object(raw_bytes: bytes, label: str) -> dict[str, Any]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SSERiskWarningSourceBlockedError(f"{label} is not UTF-8") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except SSERiskWarningSourceBlockedError:
        raise
    except json.JSONDecodeError as exc:
        raise SSERiskWarningSourceBlockedError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SSERiskWarningSourceBlockedError(f"{label} must be an object")
    return payload


def _reject_duplicate_json_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SSERiskWarningSourceBlockedError(
                f"duplicate JSON key in SSE risk-warning evidence: {key!r}"
            )
        value[key] = item
    return value


def _decode_jsonp(raw_bytes: bytes, *, callback: str) -> dict[str, Any]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SSERiskWarningSourceBlockedError(
            "SSE risk-warning JSONP is not UTF-8"
        ) from exc
    prefix = f"{callback}("
    if not text.startswith(prefix) or not text.endswith(")"):
        raise SSERiskWarningSourceBlockedError(
            "SSE risk-warning JSONP wrapper changed"
        )
    json_text = text[len(prefix) : -1]

    try:
        payload = json.loads(json_text, object_pairs_hook=_reject_duplicate_json_keys)
    except SSERiskWarningSourceBlockedError:
        raise
    except json.JSONDecodeError as exc:
        raise SSERiskWarningSourceBlockedError(
            "SSE risk-warning JSONP payload is invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise SSERiskWarningSourceBlockedError(
            "SSE risk-warning JSONP payload must be an object"
        )
    return payload


def _validate_official_request_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != SSE_QUERY_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/commonSoaQuery.do"
        or parsed.fragment
    ):
        raise SSERiskWarningSourceBlockedError(
            "official SSE risk-warning request origin changed"
        )
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) != len(dict(query)):
        raise SSERiskWarningSourceBlockedError(
            "official SSE risk-warning request has duplicate parameters"
        )
    admitted_queries = {
        tuple(
            (
                ("jsonCallBack", spec.callback),
                ("sqlId", spec.sql_id),
                *spec.query_items,
            )
        )
        for spec in SOURCE_SPECS
    }
    if tuple(query) not in admitted_queries:
        raise SSERiskWarningSourceBlockedError(
            "official SSE risk-warning request parameters are unadmitted",
            status=SOURCE_CONTRACT_UNADMITTED,
        )


def _normalize_retrieved_at(value: str | None) -> str:
    if value is None:
        parsed = datetime.now().astimezone()
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SSERiskWarningSourceBlockedError(
                f"invalid retrieved_at timestamp: {value!r}"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SSERiskWarningSourceBlockedError(
                "retrieved_at must include a UTC offset"
            )
    return parsed.isoformat(timespec="seconds")


def _strict_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SSERiskWarningSourceBlockedError(f"invalid {label}: {value!r}")
    return value


def _strict_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SSERiskWarningSourceBlockedError(f"invalid {label}: {value!r}")
    if value < 0:
        raise SSERiskWarningSourceBlockedError(f"invalid {label}: {value!r}")
    return value


def _verify_sha256(
    content: bytes,
    expected_sha256: str | None,
    label: str,
) -> str:
    actual = _sha256(content)
    if expected_sha256 is None:
        return actual
    expected = str(expected_sha256).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SSERiskWarningSourceBlockedError(
            f"invalid expected {label} SHA-256"
        )
    if actual != expected:
        raise SSERiskWarningSourceBlockedError(
            f"{label} source hash mismatch: expected {expected}, got {actual}"
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


def _atomic_write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise SSERiskWarningSourceBlockedError(
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
            if path.is_symlink() or path.read_bytes() != content:
                raise SSERiskWarningSourceBlockedError(
                    f"content-address collision or corruption: {path}"
                )
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
