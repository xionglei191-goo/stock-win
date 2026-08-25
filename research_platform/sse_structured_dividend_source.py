from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit

import requests

from research_platform import cninfo_delisted_disclosures as cninfo


PROTOCOL_VERSION = "sse-structured-dividend-corroboration-v1"
MANIFEST_SCHEMA_VERSION = "sse-structured-dividend-manifest-v1"
SOURCE_AUTHORITY = "SSE_OFFICIAL_STRUCTURED_DIVIDEND_STATISTICS"
QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
QUERY_REFERER = (
    "https://www.sse.com.cn/market/stockdata/dividends/dividend/index_his.shtml"
)
SQL_ID = "COMMON_SSE_GP_SJTJ_FHSG_AGFH_L_NEW"
PAGE_SIZE = 25
MAX_PAGES_PER_YEAR = 100
AUDIT_START_YEAR = 2018
AUDIT_END_YEAR = 2023

_TOP_FIELDS = frozenset(
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
_PAGE_FIELDS = frozenset(
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
_ROW_FIELDS = frozenset(
    {
        "SECURITY_CODE_A",
        "RECORD_DATE_A",
        "EX_DIVIDEND_DATE_A",
        "DIVIDEND_PER_SHARE1_A",
        "DIVIDEND_PER_SHARE2_A",
        "EXCHANGE_RATE",
        "NUM",
        "COMPANY_CODE",
        "FULL_NAME",
        "DIVIDEND_DATE",
        "SECURITY_ABBR_A",
    }
)
_SOURCE_CONTRACT = {
    "ready": False,
    "status": "CORROBORATION_ONLY_INCOMPLETE",
    "authority": SOURCE_AUTHORITY,
    "anonymous_read_only": True,
    "official_origin": "query.sse.com.cn",
    "scope": "SSE_ONLY_2018_2023",
    "expected_delisted_target_count": 239,
    "full_target_scope_proven": False,
    "published_at_available": False,
    "effective_at_available_as_ex_dividend_date": True,
    "source_response_hash_available": True,
    "source_document_hash_available": False,
    "zero_event_inference_allowed": False,
    "structured_corporate_action_rows_emitted": 0,
    "gp30_eligible": False,
    "gp43_eligible": False,
    "adjustment_factor_eligible": False,
    "training_eligible": False,
    "trading_eligible": False,
    "caller_ready_ignored": True,
}


class SSEStructuredDividendBlockedError(RuntimeError):
    """The official SSE response failed the frozen corroboration contract."""


@dataclass(frozen=True)
class FrozenSSEDividendTarget:
    canonical_entity_id: str
    code: str
    start_year: int = AUDIT_START_YEAR
    end_year: int = AUDIT_END_YEAR


@dataclass(frozen=True)
class SSEStructuredDividendReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str
    object_path: str
    target_count: int
    corroboration_row_count: int
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_sse_structured_dividends(
    *,
    cas_root: Path,
    targets: Sequence[FrozenSSEDividendTarget],
    session: requests.Session | None = None,
    timeout_seconds: float = 30.0,
    clock: Callable[[], datetime] | None = None,
) -> SSEStructuredDividendReference:
    """Capture SSE cash-dividend statistics as corroboration, never GP rows."""

    client = _SSEStructuredDividendClient(
        cas=cninfo.CninfoDisclosureCAS(Path(cas_root)),
        session=session,
        timeout_seconds=timeout_seconds,
        clock=clock,
    )
    manifest = client.capture(targets)
    content = _canonical_json_bytes(manifest)
    digest, path = client.cas.put_blob(content)
    replayed = replay_sse_structured_dividends(
        cas_root=cas_root,
        manifest_sha256=digest,
    )
    if _canonical_json_bytes(replayed) != content:
        raise SSEStructuredDividendBlockedError(
            "published structured-dividend manifest failed cold replay"
        )
    statistics = manifest["statistics"]
    return SSEStructuredDividendReference(
        manifest_sha256=digest,
        byte_count=len(content),
        cas_uri=f"sha256:{digest}",
        object_path=str(path),
        target_count=int(statistics["target_count"]),
        corroboration_row_count=int(statistics["corroboration_row_count"]),
        ready=False,
    )


def replay_sse_structured_dividends(
    *,
    cas_root: Path,
    manifest_sha256: str,
) -> dict[str, Any]:
    """Rebuild the normalized evidence exclusively from immutable raw bytes."""

    cas = cninfo.CninfoDisclosureCAS(Path(cas_root))
    try:
        content, _path = cas.read_blob(manifest_sha256)
    except cninfo.CninfoDelistedDisclosureBlockedError as exc:
        raise SSEStructuredDividendBlockedError(str(exc)) from exc
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SSEStructuredDividendBlockedError(
            "structured-dividend manifest is not UTF-8 JSON"
        ) from exc
    if not isinstance(manifest, dict) or _canonical_json_bytes(manifest) != content:
        raise SSEStructuredDividendBlockedError(
            "structured-dividend manifest is not canonical JSON"
        )
    expected_fields = {
        "manifest_schema_version",
        "protocol_version",
        "targets",
        "pages",
        "corroboration_rows",
        "logical_content_sha256",
        "source_contract",
        "statistics",
        "quality_rows",
        "ready",
    }
    if set(manifest) != expected_fields:
        raise SSEStructuredDividendBlockedError(
            "structured-dividend manifest schema drift"
        )
    if (
        manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("protocol_version") != PROTOCOL_VERSION
        or manifest.get("source_contract") != _SOURCE_CONTRACT
        or manifest.get("quality_rows") != []
        or manifest.get("ready") is not False
    ):
        raise SSEStructuredDividendBlockedError(
            "structured-dividend manifest attempted to change its blocked contract"
        )
    try:
        targets = tuple(
            FrozenSSEDividendTarget(**dict(item)) for item in manifest["targets"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SSEStructuredDividendBlockedError("target schema drift") from exc
    rebuilt = _assemble_manifest(
        cas=cas,
        targets=targets,
        pages=manifest.get("pages"),
    )
    if _canonical_json_bytes(rebuilt) != content:
        raise SSEStructuredDividendBlockedError(
            "structured-dividend manifest does not replay from raw source bytes"
        )
    return rebuilt


class _SSEStructuredDividendClient:
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

    def capture(
        self, targets: Sequence[FrozenSSEDividendTarget]
    ) -> dict[str, Any]:
        frozen_targets = _normalize_targets(targets)
        pages: list[dict[str, Any]] = []
        for target_index, target in enumerate(frozen_targets):
            for year in range(target.start_year, target.end_year + 1):
                page = 1
                page_count: int | None = None
                while page_count is None or page <= max(1, page_count):
                    params = _query_params(target.code, year, page)
                    response = self.session.get(
                        QUERY_URL,
                        params=params,
                        headers={
                            "Accept": "application/json, text/javascript, */*; q=0.01",
                            "Referer": QUERY_REFERER,
                            "User-Agent": "Mozilla/5.0",
                        },
                        timeout=self.timeout_seconds,
                        allow_redirects=False,
                    )
                    content, media = _admit_response(response, expected_params=params)
                    parsed = _parse_page(
                        content,
                        target=target,
                        year=year,
                        page=page,
                    )
                    page_count = parsed["page_count"]
                    if page_count > MAX_PAGES_PER_YEAR:
                        raise SSEStructuredDividendBlockedError(
                            "SSE dividend response exceeded the page cap"
                        )
                    evidence = self.cas.capture(
                        content,
                        source_id=f"sse-dividend:{target.code}:{year}:{page}",
                        role="SSE_STRUCTURED_DIVIDEND_PAGE",
                        source_url=str(response.url),
                        method="GET",
                        retrieved_at=self.clock(),
                        content_type=media,
                    )
                    pages.append(
                        {
                            "target_index": target_index,
                            "year": year,
                            "page": page,
                            "response": evidence.to_dict(),
                        }
                    )
                    page += 1
        return _assemble_manifest(cas=self.cas, targets=frozen_targets, pages=pages)


def _assemble_manifest(
    *,
    cas: cninfo.CninfoDisclosureCAS,
    targets: Sequence[FrozenSSEDividendTarget],
    pages: Any,
) -> dict[str, Any]:
    frozen_targets = _normalize_targets(targets)
    if not isinstance(pages, list):
        raise SSEStructuredDividendBlockedError("captured pages are missing")

    grouped: dict[tuple[int, int], list[tuple[int, Mapping[str, Any]]]] = {}
    normalized_pages: list[dict[str, Any]] = []
    for page_item in pages:
        if not isinstance(page_item, dict) or set(page_item) != {
            "target_index",
            "year",
            "page",
            "response",
        }:
            raise SSEStructuredDividendBlockedError("captured page schema drift")
        target_index = _strict_int(page_item["target_index"], "target_index")
        if target_index >= len(frozen_targets):
            raise SSEStructuredDividendBlockedError("page target index escaped scope")
        target = frozen_targets[target_index]
        year = _strict_int(page_item["year"], "year")
        page_number = _strict_int(page_item["page"], "page", positive=True)
        if year < target.start_year or year > target.end_year:
            raise SSEStructuredDividendBlockedError("page year escaped target scope")
        response_value = _normalize_raw_evidence(
            cas,
            page_item["response"],
            source_id=f"sse-dividend:{target.code}:{year}:{page_number}",
            expected_params=_query_params(target.code, year, page_number),
        )
        key = (target_index, year)
        grouped.setdefault(key, []).append((page_number, response_value))
        normalized_pages.append(
            {
                "target_index": target_index,
                "year": year,
                "page": page_number,
                "response": dict(response_value),
            }
        )

    rows: list[dict[str, Any]] = []
    for target_index, target in enumerate(frozen_targets):
        for year in range(target.start_year, target.end_year + 1):
            group = sorted(grouped.get((target_index, year), []))
            if not group or [item[0] for item in group] != list(
                range(1, len(group) + 1)
            ):
                raise SSEStructuredDividendBlockedError(
                    "year pages are missing, duplicated, or non-contiguous"
                )
            page_count: int | None = None
            total: int | None = None
            group_rows: list[dict[str, Any]] = []
            for page_number, evidence in group:
                content, _path = cas.read_blob(
                    str(evidence["content_hash"]),
                    expected_path=evidence["object_path"],
                )
                parsed = _parse_page(
                    content,
                    target=target,
                    year=year,
                    page=page_number,
                )
                if page_count is None:
                    page_count = parsed["page_count"]
                    total = parsed["total"]
                elif (
                    page_count != parsed["page_count"]
                    or total != parsed["total"]
                ):
                    raise SSEStructuredDividendBlockedError(
                        "pagination totals changed within a target year"
                    )
                for row in parsed["rows"]:
                    group_rows.append(
                        {
                            "canonical_entity_id": target.canonical_entity_id,
                            "exchange": "SSE",
                            "code": target.code,
                            "record_date": row["record_date"],
                            "ex_dividend_date": row["ex_dividend_date"],
                            "gross_cash_per_share": row["gross_cash_per_share"],
                            "net_cash_per_share": row["net_cash_per_share"],
                            "dividend_date": row["dividend_date"],
                            "full_name": row["full_name"],
                            "security_abbreviation": row["security_abbreviation"],
                            "published_at": None,
                            "published_at_status": "UNAVAILABLE_FROM_SOURCE",
                            "effective_at": row["ex_dividend_date"],
                            "source_response_hash": evidence["content_hash"],
                            "source_document_hash": None,
                            "source_document_hash_status": "UNAVAILABLE_FROM_SOURCE",
                            "source_authority": SOURCE_AUTHORITY,
                            "quality_row_eligible": False,
                        }
                    )
            expected_pages = max(1, int(page_count or 0))
            if len(group) != expected_pages or len(group_rows) != total:
                raise SSEStructuredDividendBlockedError(
                    "captured pages do not cover the declared target-year total"
                )
            rows.extend(group_rows)

    normalized_pages.sort(key=lambda item: (item["target_index"], item["year"], item["page"]))
    rows.sort(
        key=lambda item: (
            item["canonical_entity_id"],
            item["record_date"],
            item["ex_dividend_date"],
            item["gross_cash_per_share"] or "",
        )
    )
    row_keys = [
        (
            item["canonical_entity_id"],
            item["record_date"],
            item["ex_dividend_date"],
            item["gross_cash_per_share"],
        )
        for item in rows
    ]
    if len(row_keys) != len(set(row_keys)):
        raise SSEStructuredDividendBlockedError("duplicate corroboration row")

    normalized_targets = [asdict(item) for item in frozen_targets]
    logical = {
        "targets": normalized_targets,
        "pages": normalized_pages,
        "corroboration_rows": rows,
    }
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "targets": normalized_targets,
        "pages": normalized_pages,
        "corroboration_rows": rows,
        "logical_content_sha256": _sha256(_canonical_json_bytes(logical)),
        "source_contract": dict(_SOURCE_CONTRACT),
        "statistics": {
            "expected_delisted_target_count": 239,
            "target_count": len(frozen_targets),
            "missing_target_count_lower_bound": max(0, 239 - len(frozen_targets)),
            "target_year_count": sum(
                item.end_year - item.start_year + 1 for item in frozen_targets
            ),
            "page_count": len(normalized_pages),
            "corroboration_row_count": len(rows),
            "quality_row_count": 0,
            "gp30_row_count": 0,
            "gp43_row_count": 0,
        },
        "quality_rows": [],
        "ready": False,
    }


def _normalize_targets(
    targets: Sequence[FrozenSSEDividendTarget],
) -> tuple[FrozenSSEDividendTarget, ...]:
    normalized: list[FrozenSSEDividendTarget] = []
    for value in targets:
        if not isinstance(value, FrozenSSEDividendTarget):
            raise TypeError("targets must contain FrozenSSEDividendTarget values")
        code = str(value.code).upper()
        if not re.fullmatch(r"\d{6}\.SH", code):
            raise SSEStructuredDividendBlockedError("target is not an SSE code")
        entity = str(value.canonical_entity_id).strip()
        if not entity or len(entity) > 200:
            raise SSEStructuredDividendBlockedError("canonical entity ID is invalid")
        start_year = _strict_int(value.start_year, "start_year")
        end_year = _strict_int(value.end_year, "end_year")
        if (
            start_year < AUDIT_START_YEAR
            or end_year > AUDIT_END_YEAR
            or end_year < start_year
        ):
            raise SSEStructuredDividendBlockedError(
                "target years escape the frozen 2018-2023 scope"
            )
        normalized.append(
            FrozenSSEDividendTarget(
                canonical_entity_id=entity,
                code=code,
                start_year=start_year,
                end_year=end_year,
            )
        )
    if not normalized:
        raise SSEStructuredDividendBlockedError("at least one target is required")
    keys = [(item.canonical_entity_id, item.code) for item in normalized]
    if len(keys) != len(set(keys)):
        raise SSEStructuredDividendBlockedError("duplicate target")
    return tuple(sorted(normalized, key=lambda item: (item.canonical_entity_id, item.code)))


def _query_params(code: str, year: int, page: int) -> dict[str, str]:
    return {
        "isPagination": "true",
        "pageHelp.pageSize": str(PAGE_SIZE),
        "pageHelp.pageNo": str(page),
        "pageHelp.beginPage": str(page),
        "pageHelp.endPage": str(page),
        "pageHelp.cacheSize": "1",
        "record_date_a": str(year),
        "security_code_a": code[:6],
        "sqlId": SQL_ID,
    }


def _admit_response(
    response: Any, *, expected_params: Mapping[str, str]
) -> tuple[bytes, str]:
    if int(getattr(response, "status_code", 0)) != 200:
        raise SSEStructuredDividendBlockedError("official SSE HTTP status is not 200")
    observed = urlsplit(str(getattr(response, "url", "")))
    observed_query = parse_qsl(observed.query, keep_blank_values=True)
    if (
        observed.scheme != "https"
        or observed.hostname != "query.sse.com.cn"
        or observed.port not in (None, 443)
        or observed.path != "/commonQuery.do"
        or len(observed_query) != len(expected_params)
        or dict(observed_query) != dict(expected_params)
    ):
        raise SSEStructuredDividendBlockedError("official SSE response URL drift")
    media = str(getattr(response, "headers", {}).get("Content-Type") or "")
    if media.split(";", 1)[0].strip().lower() != "application/json":
        raise SSEStructuredDividendBlockedError("official SSE media type drift")
    content = bytes(getattr(response, "content", b""))
    if not content or len(content) > 8_000_000 or not content.lstrip().startswith(b"{"):
        raise SSEStructuredDividendBlockedError(
            "official SSE response is empty, oversized, or not JSON"
        )
    return content, media


def _parse_page(
    content: bytes,
    *,
    target: FrozenSSEDividendTarget,
    year: int,
    page: int,
) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SSEStructuredDividendBlockedError("SSE page is not UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != _TOP_FIELDS:
        raise SSEStructuredDividendBlockedError("SSE page top-level schema drift")
    if (
        value["actionErrors"] != []
        or value["actionMessages"] != []
        or value["fieldErrors"] != {}
        or value["isPagination"] != "true"
        or value["jsonCallBack"] is not None
        or value["sqlId"] != SQL_ID
    ):
        raise SSEStructuredDividendBlockedError("SSE page reported an error or contract drift")
    page_help = value["pageHelp"]
    result = value["result"]
    if not isinstance(page_help, dict) or set(page_help) != _PAGE_FIELDS:
        raise SSEStructuredDividendBlockedError("SSE pageHelp schema drift")
    if not isinstance(result, list) or page_help["data"] != result:
        raise SSEStructuredDividendBlockedError("SSE result/pageHelp data mismatch")
    page_count = _strict_int(page_help["pageCount"], "pageCount")
    total = _strict_int(page_help["total"], "total")
    if (
        _strict_int(page_help["beginPage"], "beginPage", positive=True) != page
        or _strict_int(page_help["endPage"], "endPage", positive=True) != page
        or _strict_int(page_help["pageNo"], "pageNo", positive=True) != page
        or _strict_int(page_help["pageSize"], "pageSize", positive=True) != PAGE_SIZE
        or _strict_int(page_help["cacheSize"], "cacheSize", positive=True) != 1
        or page_count != (math.ceil(total / PAGE_SIZE) if total else 0)
        or len(result) > PAGE_SIZE
    ):
        raise SSEStructuredDividendBlockedError("SSE pagination semantics drift")
    normalized_rows: list[dict[str, Any]] = []
    for row in result:
        if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
            raise SSEStructuredDividendBlockedError("SSE dividend row schema drift")
        if row["SECURITY_CODE_A"] != target.code[:6]:
            raise SSEStructuredDividendBlockedError("SSE dividend row escaped code scope")
        record_date = _iso_date(row["RECORD_DATE_A"], "RECORD_DATE_A")
        ex_date = _iso_date(row["EX_DIVIDEND_DATE_A"], "EX_DIVIDEND_DATE_A")
        dividend_date = _iso_date(row["DIVIDEND_DATE"], "DIVIDEND_DATE")
        if record_date.year != year or ex_date < record_date:
            raise SSEStructuredDividendBlockedError("SSE dividend row escaped year/date scope")
        normalized_rows.append(
            {
                "record_date": record_date.isoformat(),
                "ex_dividend_date": ex_date.isoformat(),
                "gross_cash_per_share": _decimal_or_none(
                    row["DIVIDEND_PER_SHARE2_A"], "DIVIDEND_PER_SHARE2_A"
                ),
                "net_cash_per_share": _decimal_or_none(
                    row["DIVIDEND_PER_SHARE1_A"], "DIVIDEND_PER_SHARE1_A"
                ),
                "dividend_date": dividend_date.isoformat(),
                "full_name": _required_text(row["FULL_NAME"], "FULL_NAME"),
                "security_abbreviation": _required_text(
                    row["SECURITY_ABBR_A"], "SECURITY_ABBR_A"
                ),
            }
        )
    return {
        "page_count": page_count,
        "total": total,
        "rows": normalized_rows,
    }


def _normalize_raw_evidence(
    cas: cninfo.CninfoDisclosureCAS,
    value: Any,
    *,
    source_id: str,
    expected_params: Mapping[str, str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "source_id",
        "role",
        "source_url",
        "method",
        "retrieved_at",
        "content_hash",
        "byte_count",
        "content_type",
        "object_path",
    }:
        raise SSEStructuredDividendBlockedError("raw response evidence schema drift")
    if (
        value["source_id"] != source_id
        or value["role"] != "SSE_STRUCTURED_DIVIDEND_PAGE"
        or value["method"] != "GET"
    ):
        raise SSEStructuredDividendBlockedError("raw response identity drift")
    try:
        retrieved = datetime.fromisoformat(str(value["retrieved_at"]))
    except ValueError as exc:
        raise SSEStructuredDividendBlockedError("retrieved_at is not ISO-8601") from exc
    if retrieved.tzinfo is None or retrieved.utcoffset() is None:
        raise SSEStructuredDividendBlockedError("retrieved_at has no timezone")
    digest = str(value["content_hash"])
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SSEStructuredDividendBlockedError("raw response hash is invalid")
    content, path = cas.read_blob(digest, expected_path=value["object_path"])
    if _strict_int(value["byte_count"], "byte_count", positive=True) != len(content):
        raise SSEStructuredDividendBlockedError("raw response byte count mismatch")
    fake_response = type(
        "StoredResponse",
        (),
        {
            "status_code": 200,
            "url": value["source_url"],
            "headers": {"Content-Type": value["content_type"]},
            "content": content,
        },
    )()
    _admit_response(fake_response, expected_params=expected_params)
    normalized = dict(value)
    normalized["object_path"] = str(path)
    return normalized


def _strict_int(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SSEStructuredDividendBlockedError(f"{label} is not a strict integer")
    if value < (1 if positive else 0):
        raise SSEStructuredDividendBlockedError(f"{label} is outside its admitted range")
    return value


def _iso_date(value: Any, label: str) -> date:
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise SSEStructuredDividendBlockedError(f"{label} is not ISO date") from exc
    if parsed.isoformat() != text:
        raise SSEStructuredDividendBlockedError(f"{label} is not canonical")
    return parsed


def _decimal_or_none(value: Any, label: str) -> str | None:
    text = str(value or "").strip()
    if text in {"", "-", "--"}:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise SSEStructuredDividendBlockedError(f"{label} is not decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise SSEStructuredDividendBlockedError(f"{label} is outside its admitted range")
    rendered = format(parsed.normalize(), "f")
    return "0" if parsed == 0 else rendered


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 500:
        raise SSEStructuredDividendBlockedError(f"{label} is missing or oversized")
    return text


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


__all__ = [
    "FrozenSSEDividendTarget",
    "SSEStructuredDividendBlockedError",
    "SSEStructuredDividendReference",
    "capture_sse_structured_dividends",
    "replay_sse_structured_dividends",
]
