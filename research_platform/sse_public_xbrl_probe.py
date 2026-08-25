from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import requests


PROTOCOL_VERSION = "sse-public-xbrl-evidence-probe-v1"
MANIFEST_SCHEMA_VERSION = "sse-public-xbrl-evidence-manifest-v1"
AUDIT_START_YEAR = 2018
AUDIT_END_YEAR = 2023

SSE_PAGE_URL = "https://www.sse.com.cn/disclosure/listedinfo/listedcompanies/"
SSE_PAGE_SCRIPT_URL = (
    "https://www.sse.com.cn/xhtml/home/2021public/querySearch/"
    "search_listed_companies.js"
)
SSE_QUERY_URL = "https://query.sse.com.cn/commonSoaQuery.do"
SSE_DETAIL_SQL_ID = "COMMON_SSE_PL_XBRL_YJGL_XQ"

SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"
SEMANTICS_UNVERIFIED = "STRUCTURED_FINANCIAL_SEMANTICS_UNVERIFIED"
COVERAGE_MISSING = "OFFICIAL_PAGE_COVERAGE_MISSING"

MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024

SCRIPT_MEDIA_TYPES = frozenset(
    {"application/javascript", "text/javascript", "application/x-javascript"}
)
JSON_MEDIA_TYPE = "application/json"
SCRIPT_CONTRACT_MARKERS = (
    "COMMON_SSE_PL_XBRL_YJGL_XQ",
    "commonSoaQuery.do",
    "reportYear",
    "stockId",
)
RESPONSE_FIELDS = frozenset(
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
ROW_FIELDS = frozenset(
    {
        "assteration",
        "growthRate",
        "REPORT_PERIOD_ID",
        "REPORT_YEAR",
        "S2010_0380",
        "S2010_0690",
        "S2020_0010",
        "S2090_0040",
        "S2090_0050",
        "S2090_0060",
        "S2090_0090",
        "S2090_0130",
        "STOCK_ID",
    }
)
REPORT_PERIOD_IDS = frozenset({"1000", "5000"})
_BUILDER_SEAL = object()


class SsePublicXbrlProbeBlockedError(RuntimeError):
    """The evidence probe drifted outside its frozen, non-admitted contract."""

    def __init__(self, message: str, *, status: str = SOURCE_INCOMPLETE) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RawProbeEvidence:
    source_id: str
    source_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    http_status: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SsePublicXbrlProbeArtifact:
    code: str
    start_year: int
    end_year: int
    retrieved_at: str
    page_script: RawProbeEvidence
    query_response: RawProbeEvidence
    observed_row_count: int
    observed_years: tuple[int, ...]
    observed_report_period_ids: tuple[str, ...]
    logical_content_sha256: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _BUILDER_SEAL:
            raise TypeError("probe artifacts must be rebuilt from raw CAS evidence")

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "authority": "SSE_PUBLIC_XBRL_OVERVIEW_PAGE",
            "ready": False,
            "status": (
                COVERAGE_MISSING if self.observed_row_count == 0 else SEMANTICS_UNVERIFIED
            ),
            "scope": "EVIDENCE_PROBE_ONLY",
            "frozen_years": [self.start_year, self.end_year],
            "anonymous_page_contract_observed": True,
            "protected_or_catalog_api_calls": 0,
            "quality_dataset_eligibility": False,
            "training_eligibility": False,
            "trading_eligibility": False,
            "caller_ready_rejected": True,
        }

    @property
    def dataset_gates(self) -> dict[str, Any]:
        coverage_reason = (
            [COVERAGE_MISSING] if self.observed_row_count == 0 else []
        )
        return {
            "financial_reports": {
                "ready": False,
                "status": SEMANTICS_UNVERIFIED,
                "rows_emitted": 0,
                "observed_source_rows": self.observed_row_count,
                "blocked_by": [
                    *coverage_reason,
                    "SOURCE_DOCUMENT_HASH_UNAVAILABLE",
                    "PUBLISHED_AT_UNAVAILABLE_IN_VALUE_RESPONSE",
                    "EFFECTIVE_AT_UNRESOLVED",
                    "GROSS_MARGIN_UNAVAILABLE",
                    "FIELD_UNITS_AND_ACCOUNTING_SCOPE_UNVERIFIED",
                    "Q1_AND_Q3_COVERAGE_NOT_EXPOSED_BY_PAGE",
                    "PDF_VALUE_RECONCILIATION_REQUIRED",
                ],
            },
            "earnings_guidance_express": {
                "ready": False,
                "status": SOURCE_INCOMPLETE,
                "rows_emitted": 0,
                "blocked_by": [
                    "PAGE_CONTRACT_DOES_NOT_EXPOSE_GUIDANCE_OR_EXPRESS_EVENTS"
                ],
            },
        }

    @property
    def statistics(self) -> dict[str, Any]:
        return {
            "observed_row_count": self.observed_row_count,
            "observed_year_count": len(self.observed_years),
            "financial_report_rows_emitted": 0,
            "earnings_guidance_express_rows_emitted": 0,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "code": self.code,
            "start_year": self.start_year,
            "end_year": self.end_year,
            "retrieved_at": self.retrieved_at,
            "page_script": self.page_script.to_dict(),
            "query_response": self.query_response.to_dict(),
            "observed_row_count": self.observed_row_count,
            "observed_years": list(self.observed_years),
            "observed_report_period_ids": list(self.observed_report_period_ids),
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
            "dataset_gates": self.dataset_gates,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class SsePublicXbrlProbeManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str


class SsePublicXbrlProbeCAS:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        _prepare_root(self.root)

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        if not content:
            raise SsePublicXbrlProbeBlockedError("refusing to store empty evidence")
        digest = _sha256(content)
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(self.root, path, content)
        if _stable_read(self.root, path) != content:
            raise SsePublicXbrlProbeBlockedError("CAS read-back verification failed")
        return digest, path

    def read_blob(self, digest: str) -> tuple[bytes, Path]:
        normalized = str(digest).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise SsePublicXbrlProbeBlockedError("invalid CAS SHA-256")
        path = self.root / "sha256" / normalized[:2] / normalized
        content = _stable_read(self.root, path)
        if _sha256(content) != normalized:
            raise SsePublicXbrlProbeBlockedError("CAS object hash mismatch")
        return content, path

    def capture(
        self,
        content: bytes,
        *,
        source_id: str,
        source_url: str,
        method: str,
        retrieved_at: str,
        content_type: str,
        maximum: int,
    ) -> RawProbeEvidence:
        if not 0 < len(content) <= maximum:
            raise SsePublicXbrlProbeBlockedError("source body is empty or oversized")
        digest, path = self.put_blob(content)
        return RawProbeEvidence(
            source_id=source_id,
            source_url=source_url,
            method=method,
            retrieved_at=_normalize_timestamp(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            http_status=200,
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )


class SsePublicXbrlProbeClient:
    """Probe the anonymous SSE page endpoint without admitting its values."""

    def __init__(
        self,
        *,
        cas: SsePublicXbrlProbeCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cas, SsePublicXbrlProbeCAS):
            raise TypeError("cas must be a SsePublicXbrlProbeCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(
        self,
        *,
        code: str,
        start_year: int = AUDIT_START_YEAR,
        end_year: int = AUDIT_END_YEAR,
        caller_ready: bool = False,
    ) -> SsePublicXbrlProbeArtifact:
        normalized_code = _normalize_code(code)
        if (
            type(start_year) is not int
            or type(end_year) is not int
            or start_year != AUDIT_START_YEAR
            or end_year != AUDIT_END_YEAR
        ):
            raise SsePublicXbrlProbeBlockedError(
                "probe years must remain frozen at 2018-2023"
            )
        if caller_ready is not False:
            raise SsePublicXbrlProbeBlockedError(
                "evidence probe cannot be promoted by caller-ready input",
                status=SEMANTICS_UNVERIFIED,
            )
        observed_at = _normalize_timestamp(self._clock())
        script_response = self._request(
            "GET",
            SSE_PAGE_SCRIPT_URL,
            headers={
                "Accept": "application/javascript, text/javascript, */*; q=0.01",
                "Referer": SSE_PAGE_URL,
                "User-Agent": "tdx-research-platform/sse-public-xbrl-probe-v1",
            },
        )
        script_type = _media_type(script_response.headers.get("Content-Type"))
        if script_type not in SCRIPT_MEDIA_TYPES:
            raise SsePublicXbrlProbeBlockedError("SSE page script media type changed")
        script_content = bytes(script_response.content)
        _validate_script(script_content)
        script_evidence = self.cas.capture(
            script_content,
            source_id="SSE_LISTED_COMPANIES_PAGE_SCRIPT",
            source_url=SSE_PAGE_SCRIPT_URL,
            method="GET",
            retrieved_at=observed_at,
            content_type=script_type,
            maximum=MAX_SCRIPT_BYTES,
        )

        years = ",".join(str(year) for year in range(start_year, end_year + 1))
        query_response = self._request(
            "POST",
            SSE_QUERY_URL,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": SSE_PAGE_URL,
                "User-Agent": "tdx-research-platform/sse-public-xbrl-probe-v1",
            },
            data={
                "isPagination": "false",
                "sqlId": SSE_DETAIL_SQL_ID,
                "stockId": normalized_code[:6],
                "reportYear": years,
            },
        )
        query_type = _media_type(query_response.headers.get("Content-Type"))
        if query_type != JSON_MEDIA_TYPE:
            raise SsePublicXbrlProbeBlockedError("SSE query media type changed")
        query_content = bytes(query_response.content)
        parsed = _parse_query_response(
            query_content,
            code=normalized_code,
            start_year=start_year,
            end_year=end_year,
        )
        query_evidence = self.cas.capture(
            query_content,
            source_id=f"SSE_PUBLIC_XBRL_OVERVIEW_{normalized_code}",
            source_url=SSE_QUERY_URL,
            method="POST",
            retrieved_at=observed_at,
            content_type=query_type,
            maximum=MAX_RESPONSE_BYTES,
        )
        return _build_artifact(
            code=normalized_code,
            start_year=start_year,
            end_year=end_year,
            retrieved_at=observed_at,
            page_script=script_evidence,
            query_response=query_evidence,
            parsed=parsed,
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        _validate_url(url)
        try:
            if method == "GET":
                response = self.session.get(
                    url,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                    **kwargs,
                )
            else:
                response = self.session.post(
                    url,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                    **kwargs,
                )
        except requests.RequestException as exc:
            raise SsePublicXbrlProbeBlockedError(
                f"SSE public XBRL page is unavailable: {exc}"
            ) from exc
        if response.status_code != 200:
            raise SsePublicXbrlProbeBlockedError(
                f"SSE public XBRL page returned HTTP {response.status_code}"
            )
        if getattr(response, "history", ()) or response.url != url:
            raise SsePublicXbrlProbeBlockedError("SSE public XBRL request redirected")
        if response.headers.get("Location"):
            raise SsePublicXbrlProbeBlockedError("SSE public XBRL returned Location")
        return response


class SsePublicXbrlProbeManifestStore:
    def __init__(self, cas: SsePublicXbrlProbeCAS) -> None:
        if not isinstance(cas, SsePublicXbrlProbeCAS):
            raise TypeError("cas must be a SsePublicXbrlProbeCAS")
        self.cas = cas

    def seal(
        self, artifact: SsePublicXbrlProbeArtifact
    ) -> SsePublicXbrlProbeManifestReference:
        if not isinstance(artifact, SsePublicXbrlProbeArtifact):
            raise TypeError("artifact must be an SsePublicXbrlProbeArtifact")
        payload = {"manifest_schema_version": MANIFEST_SCHEMA_VERSION, **artifact.to_dict()}
        rebuilt = _rebuild_from_manifest(payload, cas=self.cas)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(
            {"manifest_schema_version": MANIFEST_SCHEMA_VERSION, **rebuilt.to_dict()}
        ) != content:
            raise SsePublicXbrlProbeBlockedError("manifest is not reproducible")
        digest, path = self.cas.put_blob(content)
        return SsePublicXbrlProbeManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
        )

    def replay(self, manifest_sha256: str) -> SsePublicXbrlProbeArtifact:
        content, _path = self.cas.read_blob(manifest_sha256)
        if len(content) > MAX_MANIFEST_BYTES:
            raise SsePublicXbrlProbeBlockedError("manifest is oversized")
        payload = _strict_json_object(content, label="probe manifest")
        if content != _canonical_json_bytes(payload):
            raise SsePublicXbrlProbeBlockedError("manifest is not canonical JSON")
        rebuilt = _rebuild_from_manifest(payload, cas=self.cas)
        expected = _canonical_json_bytes(
            {"manifest_schema_version": MANIFEST_SCHEMA_VERSION, **rebuilt.to_dict()}
        )
        if expected != content:
            raise SsePublicXbrlProbeBlockedError("manifest aggregate changed")
        return rebuilt


@dataclass(frozen=True)
class _ParsedResponse:
    row_count: int
    years: tuple[int, ...]
    report_period_ids: tuple[str, ...]


def _parse_query_response(
    content: bytes, *, code: str, start_year: int, end_year: int
) -> _ParsedResponse:
    if not 0 < len(content) <= MAX_RESPONSE_BYTES:
        raise SsePublicXbrlProbeBlockedError("query response is empty or oversized")
    payload = _strict_json_object(content, label="SSE XBRL query response")
    if set(payload) != RESPONSE_FIELDS:
        raise SsePublicXbrlProbeBlockedError("SSE XBRL response schema drift")
    if (
        payload["sqlId"] != SSE_DETAIL_SQL_ID
        or payload["isPagination"] != "false"
        or payload["actionErrors"] != []
        or payload["actionMessages"] != []
        or payload["fieldErrors"] != {}
    ):
        raise SsePublicXbrlProbeBlockedError("SSE XBRL response status changed")
    rows = payload["result"]
    if not isinstance(rows, list):
        raise SsePublicXbrlProbeBlockedError("SSE XBRL result is not a list")
    identities: set[tuple[int, str]] = set()
    years: set[int] = set()
    periods: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_FIELDS:
            raise SsePublicXbrlProbeBlockedError("SSE XBRL row schema drift")
        if row["STOCK_ID"] != code[:6]:
            raise SsePublicXbrlProbeBlockedError("SSE XBRL row stock identity changed")
        if not isinstance(row["REPORT_YEAR"], str) or not re.fullmatch(
            r"\d{4}", row["REPORT_YEAR"]
        ):
            raise SsePublicXbrlProbeBlockedError("SSE XBRL report year is invalid")
        year = int(row["REPORT_YEAR"])
        period = row["REPORT_PERIOD_ID"]
        if not start_year <= year <= end_year or period not in REPORT_PERIOD_IDS:
            raise SsePublicXbrlProbeBlockedError(
                "SSE XBRL row escapes the frozen report scope"
            )
        identity = (year, period)
        if identity in identities:
            raise SsePublicXbrlProbeBlockedError("duplicate SSE XBRL report identity")
        identities.add(identity)
        years.add(year)
        periods.add(period)
    return _ParsedResponse(len(rows), tuple(sorted(years)), tuple(sorted(periods)))


def _build_artifact(
    *,
    code: str,
    start_year: int,
    end_year: int,
    retrieved_at: str,
    page_script: RawProbeEvidence,
    query_response: RawProbeEvidence,
    parsed: _ParsedResponse,
) -> SsePublicXbrlProbeArtifact:
    logical = _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": PROTOCOL_VERSION,
                "code": code,
                "start_year": start_year,
                "end_year": end_year,
                "source_hashes": [
                    page_script.content_sha256,
                    query_response.content_sha256,
                ],
                "observed_row_count": parsed.row_count,
                "observed_years": list(parsed.years),
                "observed_report_period_ids": list(parsed.report_period_ids),
            }
        )
    )
    return SsePublicXbrlProbeArtifact(
        code=code,
        start_year=start_year,
        end_year=end_year,
        retrieved_at=_normalize_timestamp(retrieved_at),
        page_script=page_script,
        query_response=query_response,
        observed_row_count=parsed.row_count,
        observed_years=parsed.years,
        observed_report_period_ids=parsed.report_period_ids,
        logical_content_sha256=logical,
        _seal=_BUILDER_SEAL,
    )


def _rebuild_from_manifest(
    payload: Mapping[str, Any], *, cas: SsePublicXbrlProbeCAS
) -> SsePublicXbrlProbeArtifact:
    expected = {
        "manifest_schema_version",
        "protocol_version",
        "code",
        "start_year",
        "end_year",
        "retrieved_at",
        "page_script",
        "query_response",
        "observed_row_count",
        "observed_years",
        "observed_report_period_ids",
        "logical_content_sha256",
        "source_contract",
        "dataset_gates",
        "statistics",
    }
    if set(payload) != expected:
        raise SsePublicXbrlProbeBlockedError("manifest schema drift")
    if (
        payload["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION
        or payload["protocol_version"] != PROTOCOL_VERSION
        or payload["start_year"] != AUDIT_START_YEAR
        or payload["end_year"] != AUDIT_END_YEAR
    ):
        raise SsePublicXbrlProbeBlockedError("manifest protocol or scope drift")
    code = _normalize_code(payload["code"])
    retrieved_at = _normalize_timestamp(payload["retrieved_at"])
    script = _raw_from_manifest(
        payload["page_script"],
        cas=cas,
        source_id="SSE_LISTED_COMPANIES_PAGE_SCRIPT",
        source_url=SSE_PAGE_SCRIPT_URL,
        method="GET",
        media_types=SCRIPT_MEDIA_TYPES,
        maximum=MAX_SCRIPT_BYTES,
    )
    response = _raw_from_manifest(
        payload["query_response"],
        cas=cas,
        source_id=f"SSE_PUBLIC_XBRL_OVERVIEW_{code}",
        source_url=SSE_QUERY_URL,
        method="POST",
        media_types=frozenset({JSON_MEDIA_TYPE}),
        maximum=MAX_RESPONSE_BYTES,
    )
    script_bytes, _path = cas.read_blob(script.content_sha256)
    _validate_script(script_bytes)
    response_bytes, _path = cas.read_blob(response.content_sha256)
    parsed = _parse_query_response(
        response_bytes,
        code=code,
        start_year=AUDIT_START_YEAR,
        end_year=AUDIT_END_YEAR,
    )
    rebuilt = _build_artifact(
        code=code,
        start_year=AUDIT_START_YEAR,
        end_year=AUDIT_END_YEAR,
        retrieved_at=retrieved_at,
        page_script=script,
        query_response=response,
        parsed=parsed,
    )
    if rebuilt.to_dict() != {key: value for key, value in payload.items() if key != "manifest_schema_version"}:
        raise SsePublicXbrlProbeBlockedError("manifest aggregate is not raw-derived")
    return rebuilt


def _raw_from_manifest(
    value: Any,
    *,
    cas: SsePublicXbrlProbeCAS,
    source_id: str,
    source_url: str,
    method: str,
    media_types: frozenset[str],
    maximum: int,
) -> RawProbeEvidence:
    fields = {
        "source_id",
        "source_url",
        "method",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "http_status",
        "cas_uri",
        "object_path",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SsePublicXbrlProbeBlockedError("raw evidence schema drift")
    if (
        value["source_id"] != source_id
        or value["source_url"] != source_url
        or value["method"] != method
        or value["content_type"] not in media_types
        or value["http_status"] != 200
        or type(value["byte_count"]) is not int
        or not 0 < value["byte_count"] <= maximum
        or not isinstance(value["content_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["content_sha256"])
        or value["cas_uri"] != f"sha256:{value['content_sha256']}"
    ):
        raise SsePublicXbrlProbeBlockedError("raw evidence metadata is invalid")
    content, path = cas.read_blob(value["content_sha256"])
    if len(content) != value["byte_count"] or str(path) != value["object_path"]:
        raise SsePublicXbrlProbeBlockedError("raw evidence CAS binding changed")
    return RawProbeEvidence(
        source_id=value["source_id"],
        source_url=value["source_url"],
        method=value["method"],
        retrieved_at=_normalize_timestamp(value["retrieved_at"]),
        content_sha256=value["content_sha256"],
        byte_count=value["byte_count"],
        content_type=value["content_type"],
        http_status=value["http_status"],
        cas_uri=value["cas_uri"],
        object_path=value["object_path"],
    )


def _normalize_code(value: Any) -> str:
    code = str(value).strip().upper()
    if re.fullmatch(r"\d{6}", code):
        code += ".SH"
    if not re.fullmatch(r"6\d{5}\.SH", code):
        raise SsePublicXbrlProbeBlockedError("probe requires an SSE A-share code")
    return code


def _validate_script(content: bytes) -> None:
    if not 0 < len(content) <= MAX_SCRIPT_BYTES:
        raise SsePublicXbrlProbeBlockedError("SSE page script is empty or oversized")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SsePublicXbrlProbeBlockedError("SSE page script is not UTF-8") from exc
    if any(marker not in text for marker in SCRIPT_CONTRACT_MARKERS):
        raise SsePublicXbrlProbeBlockedError("SSE page script contract changed")


def _validate_url(value: str) -> None:
    parsed = urlsplit(value)
    allowed = {
        SSE_PAGE_SCRIPT_URL: ("www.sse.com.cn", "/xhtml/home/2021public/querySearch/search_listed_companies.js"),
        SSE_QUERY_URL: ("query.sse.com.cn", "/commonSoaQuery.do"),
    }
    if value not in allowed:
        raise SsePublicXbrlProbeBlockedError("URL is outside the fixed SSE contract")
    host, path = allowed[value]
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.path != path
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SsePublicXbrlProbeBlockedError("SSE URL contract drift")


def _strict_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    if not content.startswith(b"{") or content.startswith(b"\xef\xbb\xbf"):
        raise SsePublicXbrlProbeBlockedError(f"{label} is not strict UTF-8 JSON")

    def reject_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SsePublicXbrlProbeBlockedError(
                    f"{label} contains duplicate key {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_raise_invalid_number(label, token)),
        )
    except SsePublicXbrlProbeBlockedError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SsePublicXbrlProbeBlockedError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise SsePublicXbrlProbeBlockedError(f"{label} is not an object")
    return value


def _raise_invalid_number(label: str, token: str) -> None:
    raise SsePublicXbrlProbeBlockedError(
        f"{label} contains non-finite number {token}"
    )


def _normalize_timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SsePublicXbrlProbeBlockedError("invalid timestamp") from exc
    else:
        raise SsePublicXbrlProbeBlockedError("invalid timestamp type")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SsePublicXbrlProbeBlockedError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _media_type(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _prepare_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    current = root
    while True:
        value = os.lstat(current)
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise SsePublicXbrlProbeBlockedError("CAS root is unsafe")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _stable_read(root: Path, path: Path) -> bytes:
    root = Path(os.path.abspath(os.fspath(root)))
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SsePublicXbrlProbeBlockedError("CAS path escapes root") from exc
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SsePublicXbrlProbeBlockedError("CAS object is unsafe")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(
        getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        handle_before = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        handle_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.lstat(path)
    fingerprint = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
    )
    if not (
        fingerprint(before)
        == fingerprint(handle_before)
        == fingerprint(handle_after)
        == fingerprint(after)
    ):
        raise SsePublicXbrlProbeBlockedError("CAS object changed during read")
    return b"".join(chunks)


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _stable_read(root, path) != content:
            raise SsePublicXbrlProbeBlockedError("immutable CAS collision")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | int(getattr(os, "O_BINARY", 0))
            | int(getattr(os, "O_NOFOLLOW", 0)),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _stable_read(root, path) != content:
                raise SsePublicXbrlProbeBlockedError("immutable CAS collision")
        if _stable_read(root, path) != content:
            raise SsePublicXbrlProbeBlockedError("published CAS verification failed")
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "AUDIT_END_YEAR",
    "AUDIT_START_YEAR",
    "COVERAGE_MISSING",
    "PROTOCOL_VERSION",
    "SEMANTICS_UNVERIFIED",
    "SSE_DETAIL_SQL_ID",
    "SSE_PAGE_SCRIPT_URL",
    "SSE_PAGE_URL",
    "SSE_QUERY_URL",
    "SsePublicXbrlProbeArtifact",
    "SsePublicXbrlProbeBlockedError",
    "SsePublicXbrlProbeCAS",
    "SsePublicXbrlProbeClient",
    "SsePublicXbrlProbeManifestReference",
    "SsePublicXbrlProbeManifestStore",
]
