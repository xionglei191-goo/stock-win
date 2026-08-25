from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests


PROTOCOL_VERSION = "early-winner-sse-delisted-suspension-raw-evidence-v1"
SOURCE_STATUS = "PUBLICATION_TIME_UNRESOLVED"
SOURCE_SCOPE = "SSE_ONLY"

SSE_QUERY_ENDPOINT = "https://query.sse.com.cn/commonSoaQuery.do"
SSE_QUERY_HOST = "query.sse.com.cn"
SSE_PAGE_URL = "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/"
SSE_SQL_ID = "GW_PL_JYTS_TFPXX"
SSE_JSONP_CALLBACK = "jsonpCallbackSseDelistedSuspension"
PAGE_SIZE = 25
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ROWS_PER_QUERY = 10_000
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
    "controlType",
    "endStopDate",
    "endStopReason",
    "productCode",
    "productName",
    "startStopDate",
    "stopReason",
    "stopTime",
    "type",
}


class SSEDelistedSuspensionBlockedError(RuntimeError):
    """Official suspension evidence is incomplete, changed, or untrusted."""

    def __init__(self, message: str, *, status: str = "SOURCE_REJECTED") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SSEDelistedSuspensionTarget:
    canonical_entity_id: str
    code: str
    listed_at: str
    delisted_at: str
    valid_from: str
    valid_to: str
    query_start: str
    query_end: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SSEDelistedSuspensionEvent:
    code: str
    product_name: str
    control_type: str
    event_type: str
    start_stop_date: str
    end_stop_date: str
    stop_time: str
    stop_reason: str
    end_stop_reason: str
    full_day_candidate: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SSEDelistedSuspensionRawEvidence:
    request_key: str
    request_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    http_status: int
    cas_uri: str
    object_path: str
    request: Mapping[str, Any]
    response: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["request"] = dict(sorted(dict(self.request).items()))
        value["response"] = dict(sorted(dict(self.response).items()))
        return value


@dataclass(frozen=True)
class SSEDelistedSuspensionArtifact:
    retrieved_at: str
    coverage_start: str
    coverage_end: str
    master_binding: Mapping[str, Any]
    targets: tuple[SSEDelistedSuspensionTarget, ...]
    events: tuple[SSEDelistedSuspensionEvent, ...]
    raw_responses: tuple[SSEDelistedSuspensionRawEvidence, ...]
    logical_content_sha256: str

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": False,
            "status": SOURCE_STATUS,
            "source_scope": SOURCE_SCOPE,
            "allowed_use": "RAW_SSE_SUSPENSION_EVENT_EVIDENCE_ONLY",
            "publication_time_resolved": False,
            "retrieved_at_is_publication_time": False,
            "formal_suspension_status_allowed": False,
            "training_allowed": False,
            "label_generation_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
            "caller_attestation_allowed": False,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        return {
            "target_count": len(self.targets),
            "event_count": len(self.events),
            "raw_page_count": len(self.raw_responses),
            "full_day_candidate_count": sum(
                item.full_day_candidate for item in self.events
            ),
            "partial_day_event_count": sum(
                item.stop_time in {"930", "AM", "PM"} for item in self.events
            ),
            "publication_time_resolved_count": 0,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "retrieved_at": self.retrieved_at,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "master_binding": dict(sorted(dict(self.master_binding).items())),
            "targets": [item.to_dict() for item in self.targets],
            "events": [item.to_dict() for item in self.events],
            "raw_responses": [item.to_dict() for item in self.raw_responses],
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class SSEDelistedSuspensionManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str
    master_snapshot_id: str
    target_set_sha256: str
    ready: bool = False
    status: str = SOURCE_STATUS
    source_scope: str = SOURCE_SCOPE
    training_allowed: bool = False
    trading_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ParsedPage:
    events: tuple[SSEDelistedSuspensionEvent, ...]
    total: int
    page_count: int
    page_no: int
    page_size: int


class SSEDelistedSuspensionCAS:
    """Immutable, content-addressed storage for exact SSE response bytes."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def object_path(self, digest: str) -> Path:
        normalized = str(digest or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise SSEDelistedSuspensionBlockedError("invalid CAS SHA-256")
        return self.root / "sha256" / normalized[:2] / normalized

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        payload = bytes(content)
        if not payload:
            raise SSEDelistedSuspensionBlockedError("refusing to store empty CAS data")
        digest = _sha256(payload)
        path = self.object_path(digest)
        _atomic_write_exact(path, payload)
        replayed = _stable_read(path, "SSE suspension CAS object")
        if replayed != payload or _sha256(replayed) != digest:
            raise SSEDelistedSuspensionBlockedError("CAS write verification failed")
        return digest, path.resolve()

    def read_blob(self, digest: str) -> tuple[bytes, Path]:
        normalized = str(digest or "").lower()
        path = self.object_path(normalized)
        content = _stable_read(path, "SSE suspension CAS object")
        if _sha256(content) != normalized:
            raise SSEDelistedSuspensionBlockedError("CAS object hash mismatch")
        return content, path.resolve()

    def capture(
        self,
        content: bytes,
        *,
        request_key: str,
        request_url: str,
        retrieved_at: str,
        content_type: str,
        http_status: int,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        expected_sha256: str | None = None,
    ) -> SSEDelistedSuspensionRawEvidence:
        digest = _verify_sha256(content, expected_sha256, request_key)
        stored_digest, path = self.put_blob(content)
        if stored_digest != digest:
            raise SSEDelistedSuspensionBlockedError("raw response digest changed")
        return SSEDelistedSuspensionRawEvidence(
            request_key=request_key,
            request_url=request_url,
            method="GET",
            retrieved_at=_normalize_retrieved_at(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            http_status=http_status,
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
            request=dict(request),
            response=dict(response),
        )


class SSEDelistedSuspensionManifestStore:
    """Seal and cold-replay evidence without trusting normalized event rows."""

    def __init__(self, cas: SSEDelistedSuspensionCAS) -> None:
        if not isinstance(cas, SSEDelistedSuspensionCAS):
            raise TypeError("cas must be an SSEDelistedSuspensionCAS")
        self.cas = cas

    def seal(
        self, artifact: SSEDelistedSuspensionArtifact
    ) -> SSEDelistedSuspensionManifestReference:
        payload = _manifest_payload(artifact)
        rebuilt = self._rebuild(payload)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != content:
            raise SSEDelistedSuspensionBlockedError(
                "suspension artifact is not reproducible from raw CAS evidence"
            )
        digest, path = self.cas.put_blob(content)
        return SSEDelistedSuspensionManifestReference(
            manifest_sha256=digest,
            byte_count=len(content),
            cas_uri=f"sha256:{digest}",
            object_path=str(path),
            master_snapshot_id=str(artifact.master_binding["snapshot_id"]),
            target_set_sha256=str(artifact.master_binding["target_set_sha256"]),
        )

    def replay(self, manifest_sha256: str) -> SSEDelistedSuspensionArtifact:
        content, _path = self.cas.read_blob(manifest_sha256)
        payload = _decode_canonical_json_object(content, "suspension manifest")
        if _canonical_json_bytes(payload) != content:
            raise SSEDelistedSuspensionBlockedError(
                "suspension manifest is not canonical JSON"
            )
        artifact = self._rebuild(payload)
        if _canonical_json_bytes(_manifest_payload(artifact)) != content:
            raise SSEDelistedSuspensionBlockedError(
                "suspension manifest does not replay exactly"
            )
        return artifact

    def _rebuild(
        self, payload: Mapping[str, Any]
    ) -> SSEDelistedSuspensionArtifact:
        expected_fields = {
            "coverage_end",
            "coverage_start",
            "events",
            "logical_content_sha256",
            "master_binding",
            "protocol_version",
            "retrieved_at",
            "source_contract",
            "sources",
            "statistics",
            "targets",
        }
        if set(payload) != expected_fields:
            raise SSEDelistedSuspensionBlockedError("suspension manifest schema drift")
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise SSEDelistedSuspensionBlockedError(
                "suspension manifest protocol changed"
            )
        coverage_start, coverage_end = _validate_coverage(
            payload.get("coverage_start"), payload.get("coverage_end")
        )
        retrieved_at = _normalize_retrieved_at(payload.get("retrieved_at"))
        if retrieved_at != payload.get("retrieved_at"):
            raise SSEDelistedSuspensionBlockedError(
                "manifest retrieved_at is not canonical"
            )
        supplied_binding = payload.get("master_binding")
        if not isinstance(supplied_binding, dict):
            raise SSEDelistedSuspensionBlockedError("master binding is invalid")
        identity = {
            "snapshot_id": supplied_binding.get("snapshot_id"),
            "manifest_hash": supplied_binding.get("manifest_hash"),
            "manifest_path": supplied_binding.get("manifest_path"),
            "protocol_version": supplied_binding.get("protocol_version"),
        }
        master_binding, targets = _load_master_targets(
            identity,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )
        if supplied_binding != master_binding:
            raise SSEDelistedSuspensionBlockedError(
                "manifest master binding changed or no longer cold-replays"
            )
        if payload.get("targets") != [item.to_dict() for item in targets]:
            raise SSEDelistedSuspensionBlockedError(
                "manifest targets do not match the frozen security master"
            )
        expected_requests = _expected_query_requests(targets)
        sources = payload.get("sources")
        if not isinstance(sources, list) or not sources:
            raise SSEDelistedSuspensionBlockedError("manifest has no raw pages")
        evidence, events = _replay_sources(
            sources,
            cas=self.cas,
            expected_requests=expected_requests,
            retrieved_at=retrieved_at,
        )
        artifact = _assemble_artifact(
            retrieved_at=retrieved_at,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            master_binding=master_binding,
            targets=targets,
            events=events,
            evidence=evidence,
        )
        if payload.get("events") != [item.to_dict() for item in artifact.events]:
            raise SSEDelistedSuspensionBlockedError(
                "manifest events do not reproduce from exact raw pages"
            )
        if payload.get("logical_content_sha256") != artifact.logical_content_sha256:
            raise SSEDelistedSuspensionBlockedError("manifest logical hash mismatch")
        if payload.get("source_contract") != artifact.source_contract:
            raise SSEDelistedSuspensionBlockedError("manifest source contract drift")
        if payload.get("statistics") != artifact.statistics:
            raise SSEDelistedSuspensionBlockedError("manifest statistics drift")
        return artifact


class SSEDelistedSuspensionSourceClient:
    """GET-only collector for SSE delisted-target raw suspension events."""

    def __init__(
        self,
        *,
        cas: SSEDelistedSuspensionCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(cas, SSEDelistedSuspensionCAS):
            raise TypeError("cas must be an SSEDelistedSuspensionCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)

    def fetch(
        self,
        *,
        master_identity: Mapping[str, Any],
        coverage_start: str,
        coverage_end: str,
        retrieved_at: str | None = None,
        expected_hashes: Mapping[str, str] | None = None,
    ) -> SSEDelistedSuspensionArtifact:
        start, end = _validate_coverage(coverage_start, coverage_end)
        retrieved = _normalize_retrieved_at(retrieved_at)
        master_binding, targets = _load_master_targets(
            master_identity,
            coverage_start=start,
            coverage_end=end,
        )
        requests_to_make = _expected_query_requests(targets)
        pending_hashes = dict(expected_hashes or {})
        unknown = sorted(
            key
            for key in pending_hashes
            if not any(
                re.fullmatch(re.escape(base_key) + r":page=[1-9]\d*", key)
                for base_key in requests_to_make
            )
        )
        if unknown:
            raise SSEDelistedSuspensionBlockedError(
                f"unknown expected response hashes: {unknown}"
            )

        evidence: list[SSEDelistedSuspensionRawEvidence] = []
        events: list[SSEDelistedSuspensionEvent] = []
        for base_key, request in requests_to_make.items():
            page_no = 1
            expected_total: int | None = None
            expected_page_count: int | None = None
            query_event_count = 0
            while True:
                request_value = {**request, "page_no": page_no}
                request_key = _request_key(
                    request["code"],
                    request["query_start"],
                    request["query_end"],
                    page_no,
                )
                request_url = build_sse_delisted_suspension_request_url(
                    code=request["code"],
                    query_start=request["query_start"],
                    query_end=request["query_end"],
                    page_no=page_no,
                )
                raw, content_type, response_url, status_code = self._get(request_url)
                expected_hash = pending_hashes.pop(request_key, None)
                page = parse_sse_delisted_suspension_page(
                    raw,
                    code=request["code"],
                    query_start=request["query_start"],
                    query_end=request["query_end"],
                    page_no=page_no,
                    expected_sha256=expected_hash,
                )
                if expected_total is None:
                    expected_total = page.total
                    expected_page_count = page.page_count
                elif (
                    page.total != expected_total
                    or page.page_count != expected_page_count
                ):
                    raise SSEDelistedSuspensionBlockedError(
                        f"pagination totals changed for {base_key}"
                    )
                query_event_count += len(page.events)
                response_summary = _page_summary(page)
                evidence.append(
                    self.cas.capture(
                        raw,
                        request_key=request_key,
                        request_url=response_url,
                        retrieved_at=retrieved,
                        content_type=content_type,
                        http_status=status_code,
                        request=request_value,
                        response=response_summary,
                        expected_sha256=expected_hash,
                    )
                )
                events.extend(page.events)
                if page_no >= page.page_count:
                    break
                page_no += 1
            if expected_total is None or query_event_count != expected_total:
                raise SSEDelistedSuspensionBlockedError(
                    f"pagination did not retrieve every row for {base_key}"
                )
        if pending_hashes:
            raise SSEDelistedSuspensionBlockedError(
                f"unused expected response hashes: {sorted(pending_hashes)}"
            )
        return _assemble_artifact(
            retrieved_at=retrieved,
            coverage_start=start,
            coverage_end=end,
            master_binding=master_binding,
            targets=targets,
            events=events,
            evidence=evidence,
        )

    def _get(self, request_url: str) -> tuple[bytes, str, str, int]:
        _validate_request_url(request_url)
        try:
            response = self.session.get(
                request_url,
                headers={
                    "User-Agent": "tdx-research-platform/sse-suspension-raw-v1",
                    "Referer": SSE_PAGE_URL,
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SSEDelistedSuspensionBlockedError(
                "official SSE suspension GET failed closed"
            ) from exc
        status_code = _strict_int(getattr(response, "status_code", None), "HTTP status")
        if status_code != 200:
            raise SSEDelistedSuspensionBlockedError(
                f"official SSE suspension GET failed closed: HTTP {status_code}"
            )
        response_url = str(getattr(response, "url", "") or "")
        _validate_request_url(response_url)
        if response_url != request_url:
            raise SSEDelistedSuspensionBlockedError(
                "official SSE suspension response URL changed"
            )
        headers = getattr(response, "headers", {})
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].lower()
        if content_type != "application/json":
            raise SSEDelistedSuspensionBlockedError(
                f"official SSE suspension content type changed: {content_type!r}"
            )
        content = bytes(getattr(response, "content", b""))
        if not content or len(content) > MAX_RESPONSE_BYTES:
            raise SSEDelistedSuspensionBlockedError(
                "official SSE suspension response is empty or oversized"
            )
        return content, content_type, response_url, status_code


def build_sse_delisted_suspension_request_url(
    *,
    code: str,
    query_start: str,
    query_end: str,
    page_no: int,
) -> str:
    normalized_code = _normalize_sse_code(code)
    start, end = _validate_query_window(query_start, query_end)
    page = _strict_positive_int(page_no, "page_no")
    query = (
        ("jsonCallBack", SSE_JSONP_CALLBACK),
        ("isPagination", "true"),
        ("sqlId", SSE_SQL_ID),
        ("pageHelp.pageSize", str(PAGE_SIZE)),
        ("pageHelp.pageNo", str(page)),
        ("productCode", normalized_code.removesuffix(".SH")),
        ("keyWords", ""),
        ("startStopDate", start.replace("-", "")),
        ("endStopDate", end.replace("-", "")),
    )
    url = f"{SSE_QUERY_ENDPOINT}?{urlencode(query)}"
    _validate_request_url(url)
    return url


def parse_sse_delisted_suspension_page(
    raw_bytes: bytes,
    *,
    code: str,
    query_start: str,
    query_end: str,
    page_no: int,
    expected_sha256: str | None = None,
) -> _ParsedPage:
    normalized_code = _normalize_sse_code(code)
    start, end = _validate_query_window(query_start, query_end)
    requested_page = _strict_positive_int(page_no, "page_no")
    if not raw_bytes or len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise SSEDelistedSuspensionBlockedError("response is empty or oversized")
    _verify_sha256(raw_bytes, expected_sha256, "SSE suspension page")
    payload = _decode_jsonp(raw_bytes)
    if set(payload) != _TOP_LEVEL_FIELDS:
        raise SSEDelistedSuspensionBlockedError("SSE response top-level schema drift")
    if (
        payload.get("actionErrors") != []
        or payload.get("actionMessages") != []
        or payload.get("fieldErrors") != {}
    ):
        raise SSEDelistedSuspensionBlockedError(
            "SSE response contains API errors or messages"
        )
    if (
        payload.get("isPagination") != "true"
        or payload.get("jsonCallBack") != SSE_JSONP_CALLBACK
        or payload.get("sqlId") != SSE_SQL_ID
        or payload.get("pageNo") is not None
        or payload.get("pageSize") is not None
        or payload.get("queryDate") != ""
        or payload.get("securityCode") != ""
        or payload.get("texts") is not None
        or payload.get("type") != ""
        or payload.get("validateCode") != ""
        or not isinstance(payload.get("locale"), str)
        or not payload.get("locale")
    ):
        raise SSEDelistedSuspensionBlockedError("SSE response contract changed")

    page = payload.get("pageHelp")
    if not isinstance(page, dict) or set(page) != _PAGE_FIELDS:
        raise SSEDelistedSuspensionBlockedError("SSE pageHelp schema drift")
    if (
        _strict_int(page.get("cacheSize"), "cacheSize") != 1
        or page.get("endDate") is not None
        or page.get("endPage") is not None
        or page.get("objectResult") is not None
        or page.get("searchDate") is not None
        or page.get("sort") is not None
        or page.get("startDate") is not None
    ):
        raise SSEDelistedSuspensionBlockedError("SSE pageHelp metadata changed")
    total = _strict_nonnegative_int(page.get("total"), "total")
    page_count = _strict_nonnegative_int(page.get("pageCount"), "pageCount")
    observed_page = _strict_positive_int(page.get("pageNo"), "pageNo")
    page_size = _strict_positive_int(page.get("pageSize"), "pageSize")
    page_size_without_limit = _strict_positive_int(
        page.get("pageSizeWithOutLimit"), "pageSizeWithOutLimit"
    )
    begin_page = _strict_nonnegative_int(page.get("beginPage"), "beginPage")
    if total > MAX_TOTAL_ROWS_PER_QUERY:
        raise SSEDelistedSuspensionBlockedError("SSE total exceeds safety bound")
    expected_page_count = math.ceil(total / PAGE_SIZE) if total else 0
    if (
        observed_page != requested_page
        or page_size != PAGE_SIZE
        or page_size_without_limit != PAGE_SIZE
        or page_count != expected_page_count
        or begin_page != (1 if total else 0)
    ):
        raise SSEDelistedSuspensionBlockedError("SSE pagination contract changed")
    if total and requested_page > page_count:
        raise SSEDelistedSuspensionBlockedError("SSE returned an out-of-range page")
    result = payload.get("result")
    page_data = page.get("data")
    if not isinstance(result, list) or not isinstance(page_data, list):
        raise SSEDelistedSuspensionBlockedError("SSE result rows are invalid")
    if result != page_data:
        raise SSEDelistedSuspensionBlockedError(
            "SSE result and pageHelp.data diverged"
        )
    expected_row_count = (
        0
        if total == 0
        else min(PAGE_SIZE, total - (requested_page - 1) * PAGE_SIZE)
    )
    if len(result) != expected_row_count:
        raise SSEDelistedSuspensionBlockedError(
            "SSE page row count does not close pagination"
        )

    events: list[SSEDelistedSuspensionEvent] = []
    fingerprints: set[bytes] = set()
    for raw_row in result:
        event = _parse_event_row(
            raw_row,
            code=normalized_code,
            query_start=start,
            query_end=end,
        )
        fingerprint = _canonical_json_bytes(event.to_dict())
        if fingerprint in fingerprints:
            raise SSEDelistedSuspensionBlockedError(
                "SSE page contains duplicate suspension events"
            )
        fingerprints.add(fingerprint)
        events.append(event)
    return _ParsedPage(
        events=tuple(events),
        total=total,
        page_count=page_count,
        page_no=observed_page,
        page_size=page_size,
    )


def plan_sse_query_windows(
    query_start: str, query_end: str
) -> tuple[tuple[str, str], ...]:
    start, end = _validate_coverage(query_start, query_end)
    cursor = date.fromisoformat(start)
    terminal = date.fromisoformat(end)
    windows: list[tuple[str, str]] = []
    while cursor <= terminal:
        next_boundary = _add_years(cursor, 3)
        window_end = min(terminal, next_boundary - timedelta(days=1))
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return tuple(windows)


def _parse_event_row(
    row: Any,
    *,
    code: str,
    query_start: str,
    query_end: str,
) -> SSEDelistedSuspensionEvent:
    if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
        raise SSEDelistedSuspensionBlockedError("SSE suspension row schema drift")
    raw_code = _strict_text(row.get("productCode"), "productCode")
    if raw_code != code.removesuffix(".SH"):
        raise SSEDelistedSuspensionBlockedError(
            "SSE suspension row target code changed"
        )
    product_name = _bounded_text(row.get("productName"), "productName", 128)
    control_type = _bounded_text(row.get("controlType"), "controlType", 32)
    event_type = _bounded_text(row.get("type"), "type", 32)
    start_stop_date = _compact_date(row.get("startStopDate"), "startStopDate")
    raw_end = _bounded_text(row.get("endStopDate"), "endStopDate", 8)
    end_stop_date = (
        _compact_date(raw_end, "endStopDate") if raw_end else ""
    )
    stop_time = _bounded_text(row.get("stopTime"), "stopTime", 16)
    stop_reason = _bounded_text(row.get("stopReason"), "stopReason", 512)
    end_stop_reason = _bounded_text(
        row.get("endStopReason"), "endStopReason", 512
    )
    start_value = date.fromisoformat(start_stop_date)
    end_value = date.fromisoformat(end_stop_date) if end_stop_date else None
    if end_value is not None and end_value < start_value:
        raise SSEDelistedSuspensionBlockedError(
            "SSE suspension event has an inverted date interval"
        )
    query_start_value = date.fromisoformat(query_start)
    query_end_value = date.fromisoformat(query_end)
    if start_value > query_end_value or (
        end_value is not None and end_value < query_start_value
    ):
        raise SSEDelistedSuspensionBlockedError(
            "SSE suspension event does not overlap the requested window"
        )
    partial_day = stop_time in {"930", "AM", "PM"}
    full_day_candidate = (
        control_type == "TR"
        and not partial_day
        and (event_type == "LXTP" or stop_time == "WH")
    )
    return SSEDelistedSuspensionEvent(
        code=code,
        product_name=product_name,
        control_type=control_type,
        event_type=event_type,
        start_stop_date=start_stop_date,
        end_stop_date=end_stop_date,
        stop_time=stop_time,
        stop_reason=stop_reason,
        end_stop_reason=end_stop_reason,
        full_day_candidate=full_day_candidate,
    )


def _load_master_targets(
    identity: Mapping[str, Any],
    *,
    coverage_start: str,
    coverage_end: str,
) -> tuple[dict[str, Any], tuple[SSEDelistedSuspensionTarget, ...]]:
    snapshot_id = str(identity.get("snapshot_id") or "").lower()
    manifest_hash = str(identity.get("manifest_hash") or snapshot_id).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id) or manifest_hash != snapshot_id:
        raise SSEDelistedSuspensionBlockedError(
            "security-master identity is not a matching SHA-256"
        )
    manifest_path = Path(str(identity.get("manifest_path") or ""))
    if (
        manifest_path.name != f"{snapshot_id}.json"
        or manifest_path.parent.name != "manifests"
    ):
        raise SSEDelistedSuspensionBlockedError(
            "security-master manifest path does not match its identity"
        )
    manifest_bytes = _stable_read(manifest_path, "security-master manifest")
    if _sha256(manifest_bytes) != snapshot_id:
        raise SSEDelistedSuspensionBlockedError(
            "security-master manifest content hash mismatch"
        )
    manifest = _decode_canonical_json_object(manifest_bytes, "security-master manifest")
    if _canonical_json_bytes(manifest) != manifest_bytes:
        raise SSEDelistedSuspensionBlockedError(
            "security-master manifest is not canonical JSON"
        )
    protocol = str(manifest.get("protocol_version") or "")
    if not protocol or str(identity.get("protocol_version") or protocol) != protocol:
        raise SSEDelistedSuspensionBlockedError(
            "security-master protocol identity mismatch"
        )
    try:
        artifact = dict(manifest["artifacts"]["security_master_jsonl"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SSEDelistedSuspensionBlockedError(
            "security-master JSONL artifact is absent"
        ) from exc
    content_hash = str(artifact.get("content_hash") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise SSEDelistedSuspensionBlockedError(
            "security-master artifact hash is invalid"
        )
    root = manifest_path.parent.parent
    expected_path = root / "objects" / content_hash[:2] / content_hash
    object_path = Path(str(artifact.get("object_path") or ""))
    if object_path.resolve() != expected_path.resolve():
        raise SSEDelistedSuspensionBlockedError(
            "security-master artifact path does not match its hash"
        )
    content = _stable_read(object_path, "security-master JSONL")
    if _sha256(content) != content_hash:
        raise SSEDelistedSuspensionBlockedError(
            "security-master artifact content hash mismatch"
        )
    lines = content.splitlines()
    if _strict_nonnegative_int(artifact.get("row_count"), "master row_count") != len(lines):
        raise SSEDelistedSuspensionBlockedError(
            "security-master artifact row_count mismatch"
        )
    records: list[dict[str, Any]] = []
    for line in lines:
        row = _decode_canonical_json_object(line, "security-master JSONL row")
        if _canonical_json_bytes(row) != line:
            raise SSEDelistedSuspensionBlockedError(
                "security-master JSONL row is not canonical"
            )
        records.append(row)
    targets = _derive_targets(
        records,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    target_set_sha256 = _sha256(
        _canonical_json_bytes([item.to_dict() for item in targets])
    )
    binding = {
        "snapshot_id": snapshot_id,
        "manifest_hash": manifest_hash,
        "manifest_path": str(manifest_path.resolve()),
        "protocol_version": protocol,
        "security_master_content_hash": content_hash,
        "security_master_object_path": str(object_path.resolve()),
        "security_master_row_count": len(records),
        "target_set_sha256": target_set_sha256,
        "target_count": len(targets),
        "targets_derived_from_frozen_master": True,
        "caller_target_list_allowed": False,
    }
    return binding, targets


def _derive_targets(
    records: Sequence[Mapping[str, Any]],
    *,
    coverage_start: str,
    coverage_end: str,
) -> tuple[SSEDelistedSuspensionTarget, ...]:
    requested_start = date.fromisoformat(coverage_start)
    requested_end = date.fromisoformat(coverage_end)
    targets: list[SSEDelistedSuspensionTarget] = []
    seen: set[tuple[str, str, date, date]] = set()
    for row in records:
        if str(row.get("exchange") or "").upper() != "SSE":
            continue
        if row.get("delisted_at") in {None, ""}:
            continue
        code = _normalize_sse_code(row.get("code_alias"))
        canonical_entity_id = _strict_text(
            row.get("canonical_entity_id"), "canonical_entity_id"
        )
        listed_at = _iso_date(row.get("listed_at"), "listed_at")
        delisted_at = _iso_date(row.get("delisted_at"), "delisted_at")
        valid_from = _iso_date(row.get("valid_from"), "valid_from")
        valid_to = _iso_date(
            row.get("valid_to") if row.get("valid_to") is not None else delisted_at,
            "valid_to",
        )
        interval_start = max(
            requested_start,
            date.fromisoformat(listed_at),
            date.fromisoformat(valid_from),
        )
        interval_end = min(
            requested_end,
            date.fromisoformat(delisted_at),
            date.fromisoformat(valid_to),
        )
        if interval_start > interval_end:
            continue
        key = (canonical_entity_id, code, interval_start, interval_end)
        if key in seen:
            raise SSEDelistedSuspensionBlockedError(
                f"duplicate SSE delisted target interval: {key}"
            )
        seen.add(key)
        targets.append(
            SSEDelistedSuspensionTarget(
                canonical_entity_id=canonical_entity_id,
                code=code,
                listed_at=listed_at,
                delisted_at=delisted_at,
                valid_from=valid_from,
                valid_to=valid_to,
                query_start=interval_start.isoformat(),
                query_end=interval_end.isoformat(),
            )
        )
    ordered = tuple(
        sorted(
            targets,
            key=lambda item: (
                item.code,
                item.query_start,
                item.query_end,
                item.canonical_entity_id,
            ),
        )
    )
    if not ordered:
        raise SSEDelistedSuspensionBlockedError(
            "frozen master has no SSE delisted targets in the requested coverage"
        )
    return ordered


def _expected_query_requests(
    targets: Sequence[SSEDelistedSuspensionTarget],
) -> dict[str, dict[str, str]]:
    requests_by_key: dict[str, dict[str, str]] = {}
    for target in targets:
        for query_start, query_end in plan_sse_query_windows(
            target.query_start, target.query_end
        ):
            key = _base_request_key(target.code, query_start, query_end)
            if key in requests_by_key:
                raise SSEDelistedSuspensionBlockedError(
                    f"duplicate target query window: {key}"
                )
            requests_by_key[key] = {
                "code": target.code,
                "query_start": query_start,
                "query_end": query_end,
            }
    return dict(sorted(requests_by_key.items()))


def _replay_sources(
    sources: Sequence[Any],
    *,
    cas: SSEDelistedSuspensionCAS,
    expected_requests: Mapping[str, Mapping[str, str]],
    retrieved_at: str,
) -> tuple[
    tuple[SSEDelistedSuspensionRawEvidence, ...],
    tuple[SSEDelistedSuspensionEvent, ...],
]:
    expected_source_fields = {
        "byte_count",
        "content_sha256",
        "content_type",
        "http_status",
        "method",
        "request",
        "request_key",
        "request_url",
        "response",
    }
    grouped: dict[str, list[tuple[int, _ParsedPage]]] = {}
    evidence: list[SSEDelistedSuspensionRawEvidence] = []
    events: list[SSEDelistedSuspensionEvent] = []
    seen_request_keys: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != expected_source_fields:
            raise SSEDelistedSuspensionBlockedError("raw source schema drift")
        request = source.get("request")
        response = source.get("response")
        if not isinstance(request, dict) or set(request) != {
            "code",
            "page_no",
            "query_end",
            "query_start",
        }:
            raise SSEDelistedSuspensionBlockedError("raw source request schema drift")
        code = _normalize_sse_code(request.get("code"))
        query_start, query_end = _validate_query_window(
            request.get("query_start"), request.get("query_end")
        )
        page_no = _strict_positive_int(request.get("page_no"), "page_no")
        base_key = _base_request_key(code, query_start, query_end)
        if expected_requests.get(base_key) != {
            "code": code,
            "query_start": query_start,
            "query_end": query_end,
        }:
            raise SSEDelistedSuspensionBlockedError(
                "raw source is not an expected master-derived query"
            )
        request_key = _request_key(code, query_start, query_end, page_no)
        if source.get("request_key") != request_key or request_key in seen_request_keys:
            raise SSEDelistedSuspensionBlockedError(
                "raw source request key is changed or duplicated"
            )
        seen_request_keys.add(request_key)
        request_url = build_sse_delisted_suspension_request_url(
            code=code,
            query_start=query_start,
            query_end=query_end,
            page_no=page_no,
        )
        if (
            source.get("request_url") != request_url
            or source.get("method") != "GET"
            or source.get("content_type") != "application/json"
            or source.get("http_status") != 200
        ):
            raise SSEDelistedSuspensionBlockedError("raw source identity changed")
        digest = str(source.get("content_sha256") or "").lower()
        raw, raw_path = cas.read_blob(digest)
        if _strict_positive_int(source.get("byte_count"), "byte_count") != len(raw):
            raise SSEDelistedSuspensionBlockedError("raw source byte count mismatch")
        page = parse_sse_delisted_suspension_page(
            raw,
            code=code,
            query_start=query_start,
            query_end=query_end,
            page_no=page_no,
            expected_sha256=digest,
        )
        if response != _page_summary(page):
            raise SSEDelistedSuspensionBlockedError(
                "raw source response summary mismatch"
            )
        grouped.setdefault(base_key, []).append((page_no, page))
        events.extend(page.events)
        evidence.append(
            SSEDelistedSuspensionRawEvidence(
                request_key=request_key,
                request_url=request_url,
                method="GET",
                retrieved_at=retrieved_at,
                content_sha256=digest,
                byte_count=len(raw),
                content_type="application/json",
                http_status=200,
                cas_uri=f"sha256:{digest}",
                object_path=str(raw_path),
                request=dict(request),
                response=dict(response),
            )
        )
    if set(grouped) != set(expected_requests):
        raise SSEDelistedSuspensionBlockedError(
            "raw source set does not cover every master-derived query window"
        )
    for base_key, pages in grouped.items():
        ordered = sorted(pages)
        first = ordered[0][1]
        expected_numbers = list(range(1, first.page_count + 1)) or [1]
        if [number for number, _page in ordered] != expected_numbers:
            raise SSEDelistedSuspensionBlockedError(
                f"raw source pagination is incomplete for {base_key}"
            )
        if any(
            page.total != first.total or page.page_count != first.page_count
            for _number, page in ordered
        ):
            raise SSEDelistedSuspensionBlockedError(
                f"raw source pagination totals changed for {base_key}"
            )
        if sum(len(page.events) for _number, page in ordered) != first.total:
            raise SSEDelistedSuspensionBlockedError(
                f"raw source pagination row closure failed for {base_key}"
            )
    return tuple(evidence), tuple(events)


def _assemble_artifact(
    *,
    retrieved_at: str,
    coverage_start: str,
    coverage_end: str,
    master_binding: Mapping[str, Any],
    targets: Sequence[SSEDelistedSuspensionTarget],
    events: Sequence[SSEDelistedSuspensionEvent],
    evidence: Sequence[SSEDelistedSuspensionRawEvidence],
) -> SSEDelistedSuspensionArtifact:
    ordered_targets = tuple(
        sorted(
            targets,
            key=lambda item: (
                item.code,
                item.query_start,
                item.query_end,
                item.canonical_entity_id,
            ),
        )
    )
    unique_events: dict[tuple[str, str, str, str, str, str], SSEDelistedSuspensionEvent] = {}
    for event in events:
        natural_key = (
            event.code,
            event.start_stop_date,
            event.end_stop_date,
            event.control_type,
            event.event_type,
            event.stop_time,
        )
        previous = unique_events.get(natural_key)
        if previous is None:
            unique_events[natural_key] = event
        elif previous != event:
            raise SSEDelistedSuspensionBlockedError(
                "conflicting suspension event content across query windows or pages"
            )
    ordered_events = tuple(
        sorted(
            unique_events.values(),
            key=lambda item: (
                item.code,
                item.start_stop_date,
                item.end_stop_date,
                item.control_type,
                item.event_type,
                item.stop_time,
                item.stop_reason,
                item.end_stop_reason,
                item.product_name,
            ),
        )
    )
    ordered_evidence = tuple(
        sorted(evidence, key=lambda item: item.request_key)
    )
    if len({item.request_key for item in ordered_evidence}) != len(ordered_evidence):
        raise SSEDelistedSuspensionBlockedError("duplicate raw request keys")
    logical_hash = _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": PROTOCOL_VERSION,
                "coverage_start": coverage_start,
                "coverage_end": coverage_end,
                "master_binding": dict(sorted(dict(master_binding).items())),
                "targets": [item.to_dict() for item in ordered_targets],
                "events": [item.to_dict() for item in ordered_events],
                "raw_hashes": [
                    {
                        "request_key": item.request_key,
                        "content_sha256": item.content_sha256,
                    }
                    for item in ordered_evidence
                ],
            }
        )
    )
    return SSEDelistedSuspensionArtifact(
        retrieved_at=_normalize_retrieved_at(retrieved_at),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        master_binding=dict(sorted(dict(master_binding).items())),
        targets=ordered_targets,
        events=ordered_events,
        raw_responses=ordered_evidence,
        logical_content_sha256=logical_hash,
    )


def _manifest_payload(artifact: SSEDelistedSuspensionArtifact) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "retrieved_at": artifact.retrieved_at,
        "coverage_start": artifact.coverage_start,
        "coverage_end": artifact.coverage_end,
        "master_binding": dict(sorted(dict(artifact.master_binding).items())),
        "targets": [item.to_dict() for item in artifact.targets],
        "events": [item.to_dict() for item in artifact.events],
        "logical_content_sha256": artifact.logical_content_sha256,
        "source_contract": artifact.source_contract,
        "statistics": artifact.statistics,
        "sources": [
            {
                "request_key": item.request_key,
                "request_url": item.request_url,
                "method": item.method,
                "content_sha256": item.content_sha256,
                "byte_count": item.byte_count,
                "content_type": item.content_type,
                "http_status": item.http_status,
                "request": dict(sorted(dict(item.request).items())),
                "response": dict(sorted(dict(item.response).items())),
            }
            for item in artifact.raw_responses
        ],
    }


def _page_summary(page: _ParsedPage) -> dict[str, int]:
    return {
        "total": page.total,
        "page_count": page.page_count,
        "page_no": page.page_no,
        "page_size": page.page_size,
        "row_count": len(page.events),
    }


def _validate_request_url(url: str) -> None:
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
        raise SSEDelistedSuspensionBlockedError(
            "official SSE suspension request origin changed"
        )
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) != len(dict(query)):
        raise SSEDelistedSuspensionBlockedError(
            "official SSE suspension request has duplicate parameters"
        )
    if [key for key, _value in query] != [
        "jsonCallBack",
        "isPagination",
        "sqlId",
        "pageHelp.pageSize",
        "pageHelp.pageNo",
        "productCode",
        "keyWords",
        "startStopDate",
        "endStopDate",
    ]:
        raise SSEDelistedSuspensionBlockedError(
            "official SSE suspension request parameter schema changed"
        )
    values = dict(query)
    if (
        values["jsonCallBack"] != SSE_JSONP_CALLBACK
        or values["isPagination"] != "true"
        or values["sqlId"] != SSE_SQL_ID
        or values["pageHelp.pageSize"] != str(PAGE_SIZE)
        or values["keyWords"] != ""
        or not re.fullmatch(r"\d{6}", values["productCode"])
    ):
        raise SSEDelistedSuspensionBlockedError(
            "official SSE suspension request parameters are unadmitted"
        )
    _strict_positive_int(values["pageHelp.pageNo"], "pageHelp.pageNo")
    _validate_query_window(values["startStopDate"], values["endStopDate"])


def _validate_query_window(start: Any, end: Any) -> tuple[str, str]:
    normalized_start = _iso_date(start, "query_start")
    normalized_end = _iso_date(end, "query_end")
    start_value = date.fromisoformat(normalized_start)
    end_value = date.fromisoformat(normalized_end)
    if start_value > end_value:
        raise SSEDelistedSuspensionBlockedError("query window is inverted")
    if end_value > _add_years(start_value, 3):
        raise SSEDelistedSuspensionBlockedError(
            "SSE suspension query window cannot exceed three years"
        )
    return normalized_start, normalized_end


def _validate_coverage(start: Any, end: Any) -> tuple[str, str]:
    normalized_start = _iso_date(start, "coverage_start")
    normalized_end = _iso_date(end, "coverage_end")
    if date.fromisoformat(normalized_start) > date.fromisoformat(normalized_end):
        raise SSEDelistedSuspensionBlockedError("coverage window is inverted")
    return normalized_start, normalized_end


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _base_request_key(code: str, start: str, end: str) -> str:
    return f"{code}:{start}:{end}"


def _request_key(code: str, start: str, end: str, page_no: int) -> str:
    return f"{_base_request_key(code, start, end)}:page={page_no}"


def _normalize_sse_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if re.fullmatch(r"\d{6}", text):
        text = f"{text}.SH"
    if not re.fullmatch(r"\d{6}\.SH", text):
        raise SSEDelistedSuspensionBlockedError(f"invalid SSE code: {text!r}")
    return text


def _iso_date(value: Any, label: str) -> str:
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise SSEDelistedSuspensionBlockedError(f"invalid {label}: {text!r}")


def _compact_date(value: Any, label: str) -> str:
    text = _bounded_text(value, label, 8)
    if not re.fullmatch(r"\d{8}", text):
        raise SSEDelistedSuspensionBlockedError(f"invalid {label}: {text!r}")
    return _iso_date(text, label)


def _strict_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SSEDelistedSuspensionBlockedError(f"missing {label}")
    return text


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if value is None or not isinstance(value, str):
        raise SSEDelistedSuspensionBlockedError(f"invalid {label}")
    if len(value) > maximum or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise SSEDelistedSuspensionBlockedError(f"invalid {label}")
    return value


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise SSEDelistedSuspensionBlockedError(f"invalid {label}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SSEDelistedSuspensionBlockedError(f"invalid {label}") from exc
    if str(value).strip() not in {str(parsed), f"+{parsed}"}:
        raise SSEDelistedSuspensionBlockedError(f"invalid {label}")
    return parsed


def _strict_nonnegative_int(value: Any, label: str) -> int:
    parsed = _strict_int(value, label)
    if parsed < 0:
        raise SSEDelistedSuspensionBlockedError(f"invalid {label}")
    return parsed


def _strict_positive_int(value: Any, label: str) -> int:
    parsed = _strict_int(value, label)
    if parsed <= 0:
        raise SSEDelistedSuspensionBlockedError(f"invalid {label}")
    return parsed


def _normalize_retrieved_at(value: Any) -> str:
    text = str(value or datetime.now().astimezone().isoformat())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SSEDelistedSuspensionBlockedError(
            "retrieved_at is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SSEDelistedSuspensionBlockedError(
            "retrieved_at must include a timezone"
        )
    canonical = parsed.isoformat()
    if value is not None and canonical != text:
        raise SSEDelistedSuspensionBlockedError(
            "retrieved_at is not canonical ISO-8601"
        )
    return canonical


def _verify_sha256(content: bytes, expected: str | None, label: str) -> str:
    digest = _sha256(content)
    if expected is not None:
        normalized = str(expected).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized) or normalized != digest:
            raise SSEDelistedSuspensionBlockedError(
                f"{label} response hash mismatch"
            )
    return digest


def _decode_jsonp(raw_bytes: bytes) -> dict[str, Any]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SSEDelistedSuspensionBlockedError("SSE JSONP is not UTF-8") from exc
    prefix = f"{SSE_JSONP_CALLBACK}("
    if not text.startswith(prefix) or not text.endswith(")"):
        raise SSEDelistedSuspensionBlockedError("SSE JSONP wrapper changed")
    return _decode_json_object_text(text[len(prefix) : -1], "SSE JSONP payload")


def _decode_canonical_json_object(raw_bytes: bytes, label: str) -> dict[str, Any]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SSEDelistedSuspensionBlockedError(f"{label} is not UTF-8") from exc
    return _decode_json_object_text(text, label)


def _decode_json_object_text(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except SSEDelistedSuspensionBlockedError:
        raise
    except json.JSONDecodeError as exc:
        raise SSEDelistedSuspensionBlockedError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise SSEDelistedSuspensionBlockedError(f"{label} must be an object")
    return value


def _reject_duplicate_json_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SSEDelistedSuspensionBlockedError(
                f"duplicate JSON key in SSE suspension evidence: {key!r}"
            )
        value[key] = item
    return value


def _reject_nonfinite_json_constant(value: str) -> None:
    raise SSEDelistedSuspensionBlockedError(
        f"non-finite JSON value in SSE suspension evidence: {value}"
    )


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


def _validate_no_reparse(path: Path, label: str) -> None:
    current = path
    while True:
        if current.exists():
            metadata = os.lstat(current)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode) or (
                attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise SSEDelistedSuspensionBlockedError(
                    f"{label} uses a symlink, junction, or reparse point"
                )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _stable_read(path: Path, label: str) -> bytes:
    _validate_no_reparse(path, label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SSEDelistedSuspensionBlockedError(
            f"{label} cannot be opened as a stable file"
        ) from exc
    try:
        before = os.fstat(descriptor)
        attributes = int(getattr(before, "st_file_attributes", 0))
        if not stat.S_ISREG(before.st_mode) or (
            attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SSEDelistedSuspensionBlockedError(
                f"{label} is not a plain regular file"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_identity != after_identity or len(content) != before.st_size:
            raise SSEDelistedSuspensionBlockedError(f"{label} changed while read")
    finally:
        os.close(descriptor)
    _validate_no_reparse(path, label)
    return content


def _atomic_write_exact(path: Path, content: bytes) -> None:
    _validate_no_reparse(path.parent, "SSE suspension CAS parent")
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_no_reparse(path.parent, "SSE suspension CAS parent")
    if path.exists():
        if _stable_read(path, "existing SSE suspension CAS object") != content:
            raise SSEDelistedSuspensionBlockedError(
                "content-address collision or corruption"
            )
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if _stable_read(temporary, "SSE suspension CAS temporary") != content:
            raise SSEDelistedSuspensionBlockedError(
                "SSE suspension CAS temporary verification failed"
            )
        if path.exists():
            if _stable_read(path, "existing SSE suspension CAS object") != content:
                raise SSEDelistedSuspensionBlockedError(
                    "content-address collision or corruption"
                )
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "PAGE_SIZE",
    "PROTOCOL_VERSION",
    "SOURCE_SCOPE",
    "SOURCE_STATUS",
    "SSE_JSONP_CALLBACK",
    "SSE_PAGE_URL",
    "SSE_QUERY_ENDPOINT",
    "SSE_SQL_ID",
    "SSEDelistedSuspensionArtifact",
    "SSEDelistedSuspensionBlockedError",
    "SSEDelistedSuspensionCAS",
    "SSEDelistedSuspensionEvent",
    "SSEDelistedSuspensionManifestReference",
    "SSEDelistedSuspensionManifestStore",
    "SSEDelistedSuspensionRawEvidence",
    "SSEDelistedSuspensionSourceClient",
    "SSEDelistedSuspensionTarget",
    "build_sse_delisted_suspension_request_url",
    "parse_sse_delisted_suspension_page",
    "plan_sse_query_windows",
]
