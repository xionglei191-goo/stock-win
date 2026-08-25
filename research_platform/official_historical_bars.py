from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import requests


PROTOCOL_VERSION = "cn-official-historical-bars-v2"
REQUIRES_AUTHORIZED_SZSI_HISTORY = "REQUIRES_AUTHORIZED_SZSI_HISTORY"
SOURCE_CONTRACT_UNADMITTED = "SOURCE_CONTRACT_UNADMITTED"
BSE_STATUS_CONTRACT_UNADMITTED = SOURCE_CONTRACT_UNADMITTED
PRICE_ADJUSTMENT_CONTRACT_UNADMITTED = (
    "PRICE_ADJUSTMENT_AND_CORPORATE_ACTION_CONTRACT_UNADMITTED"
)

SSE_DAYK_ENDPOINT = "https://yunhq.sse.com.cn:32042/v1/sh1/dayk/{code}"
SSE_DAYK_HOST = "yunhq.sse.com.cn"
SSE_DAYK_PORT = 32042
SSE_DAYK_SELECT = "date,open,high,low,close,volume,amount"
SSE_DAYK_FIELDS = tuple(SSE_DAYK_SELECT.split(","))
SSE_JSONP_CALLBACK = "jsonpCallback"

BSE_KLINE_ENDPOINT = (
    "https://www.bse.cn/companyEchartsController/getKLine/list/{code}.do"
)
BSE_KLINE_HOST = "www.bse.cn"
BSE_KLINE_FIELDS = (
    "jsrq",
    "jrkp",
    "jrsp",
    "drzd",
    "drzx",
    "zrsp",
    "cjl",
    "cjje",
)

SZSE_PUBLIC_HISTORY_ENDPOINT = "https://www.szse.cn/api/market/ssjjhq/getHistoryData"
SZSE_PUBLIC_HISTORY_HOST = "www.szse.cn"

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_SSE_TOTAL_ROWS = 100_000


class OfficialHistoricalBarsBlockedError(RuntimeError):
    """An official response did not meet the frozen admission contract."""

    def __init__(self, message: str, *, status: str = "SOURCE_REJECTED") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class OfficialDailyBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    previous_close: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawResponseEvidence:
    source_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    cas_uri: str
    object_path: str | None
    persisted: bool
    request: Mapping[str, Any]
    response: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["request"] = dict(sorted(dict(self.request).items()))
        value["response"] = dict(sorted(dict(self.response).items()))
        return value


@dataclass(frozen=True)
class OfficialBarsArtifact:
    exchange: str
    code: str
    source_url: str
    bars: tuple[OfficialDailyBar, ...]
    raw_responses: tuple[RawResponseEvidence, ...]
    logical_content_sha256: str
    pagination: Mapping[str, Any]

    @property
    def usage_gate(self) -> dict[str, Any]:
        """Keep raw exchange bars out of labels until adjustment semantics are proven."""

        return {
            "ready": False,
            "status": PRICE_ADJUSTMENT_CONTRACT_UNADMITTED,
            "allowed_use": "RAW_SOURCE_AUDIT_ONLY",
            "feature_generation_allowed": False,
            "label_generation_allowed": False,
            "execution_backtest_allowed": False,
            "requires": [
                "DOCUMENTED_OR_CROSS_VALIDATED_ADJUSTMENT_SEMANTICS",
                "CORPORATE_ACTION_FACTOR_RECONCILIATION",
                "EXECUTION_PRICE_REFERENCE_VALIDATION",
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "exchange": self.exchange,
            "code": self.code,
            "source_url": self.source_url,
            "bars": [item.to_dict() for item in self.bars],
            "raw_responses": [item.to_dict() for item in self.raw_responses],
            "logical_content_sha256": self.logical_content_sha256,
            "pagination": dict(self.pagination),
            "usage_gate": self.usage_gate,
        }


@dataclass(frozen=True)
class SZSEPublicHistoryProbe:
    code: str
    ready: bool
    status: str
    detail: str
    observed_code: Any
    observed_message: str
    data_present: bool
    raw_response: RawResponseEvidence

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "promotion_blocked": True,
            "raw_response": self.raw_response.to_dict(),
        }


@dataclass(frozen=True)
class _SSEPage:
    code: str
    total: int
    request_begin: int
    request_end: int
    normalized_begin: int
    normalized_end: int
    response_begin: int
    response_end: int
    response_interval_semantics: str
    bars: tuple[OfficialDailyBar, ...]


class RawResponseCAS:
    """Small immutable content-addressed store for official raw responses."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def capture(
        self,
        content: bytes,
        *,
        source_url: str,
        method: str,
        retrieved_at: str,
        content_type: str,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        expected_sha256: str | None = None,
    ) -> RawResponseEvidence:
        digest = _verify_hash(content, expected_sha256, "official response")
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(path, content)
        if _sha256(path.read_bytes()) != digest:
            raise OfficialHistoricalBarsBlockedError("raw response CAS verification failed")
        return RawResponseEvidence(
            source_url=source_url,
            method=method,
            retrieved_at=_retrieved_at(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            cas_uri=f"sha256:{digest}",
            object_path=str(path.resolve()),
            persisted=True,
            request=dict(request),
            response=dict(response),
        )


def _atomic_write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise OfficialHistoricalBarsBlockedError(
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
                raise OfficialHistoricalBarsBlockedError(
                    f"content-address collision or corruption: {path}"
                )
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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


def _verify_hash(content: bytes, expected: str | None, label: str) -> str:
    digest = _sha256(content)
    if expected is None:
        return digest
    normalized = str(expected).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise OfficialHistoricalBarsBlockedError(f"invalid expected {label} SHA-256")
    if digest != normalized:
        raise OfficialHistoricalBarsBlockedError(
            f"{label} hash mismatch: expected {normalized}, got {digest}"
        )
    return digest


def _retrieved_at(value: str | None = None) -> str:
    text = value or datetime.now().astimezone().isoformat()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OfficialHistoricalBarsBlockedError(
            f"retrieved_at is not ISO-8601: {text!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise OfficialHistoricalBarsBlockedError("retrieved_at must include a timezone")
    return parsed.isoformat()


def _strict_int(value: Any, label: str, *, allow_negative: bool = False) -> int:
    if isinstance(value, bool):
        raise OfficialHistoricalBarsBlockedError(f"invalid {label}: {value!r}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise OfficialHistoricalBarsBlockedError(f"invalid {label}: {value!r}") from exc
    if str(value).strip() not in {str(result), f"+{result}"}:
        raise OfficialHistoricalBarsBlockedError(f"invalid {label}: {value!r}")
    if not allow_negative and result < 0:
        raise OfficialHistoricalBarsBlockedError(f"invalid {label}: {value!r}")
    return result


def _decimal(value: Any, label: str, *, positive: bool) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise OfficialHistoricalBarsBlockedError(f"invalid numeric {label}: {value!r}")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise OfficialHistoricalBarsBlockedError(
            f"invalid numeric {label}: {value!r}"
        ) from exc
    if not result.is_finite():
        raise OfficialHistoricalBarsBlockedError(f"non-finite numeric {label}")
    if (positive and result <= 0) or (not positive and result < 0):
        relation = "positive" if positive else "non-negative"
        raise OfficialHistoricalBarsBlockedError(f"{label} must be {relation}")
    converted = float(result)
    if not math.isfinite(converted):
        raise OfficialHistoricalBarsBlockedError(f"numeric {label} overflows float")
    return result


def _iso_date(value: Any, label: str) -> str:
    text = str(value or "").strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise OfficialHistoricalBarsBlockedError(f"invalid {label}: {text!r}")


def _normalize_code(code: str, exchange: str) -> str:
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[exchange]
    text = str(code or "").strip().upper()
    if text.endswith(f".{suffix}"):
        text = text[:-3]
    if not re.fullmatch(r"\d{6}", text):
        raise OfficialHistoricalBarsBlockedError(f"invalid {exchange} code: {code!r}")
    return text


def _validate_origin(url: str, *, host: str, port: int = 443) -> None:
    parsed = urlparse(url)
    try:
        observed_port = parsed.port or 443
    except ValueError as exc:
        raise OfficialHistoricalBarsBlockedError("official source URL has invalid port") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != host
        or observed_port != port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OfficialHistoricalBarsBlockedError(
            f"official source origin changed: {url!r}"
        )


def _decode_json(raw_bytes: bytes, label: str) -> Any:
    if not raw_bytes or len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise OfficialHistoricalBarsBlockedError(f"{label} is empty or oversized")
    try:
        return json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialHistoricalBarsBlockedError(f"{label} is not valid JSON") from exc


def _decode_jsonp(raw_bytes: bytes, callback: str) -> Any:
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$.]*", callback):
        raise OfficialHistoricalBarsBlockedError("invalid SSE JSONP callback")
    if not raw_bytes or len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise OfficialHistoricalBarsBlockedError("SSE response is empty or oversized")
    try:
        text = raw_bytes.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise OfficialHistoricalBarsBlockedError("SSE JSONP is not UTF-8") from exc
    match = re.fullmatch(
        rf"{re.escape(callback)}\s*\(\s*(.*)\s*\)\s*;?",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise OfficialHistoricalBarsBlockedError("SSE JSONP callback wrapper changed")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise OfficialHistoricalBarsBlockedError("SSE JSONP payload is invalid") from exc


def _make_bar(
    *,
    date_value: Any,
    open_value: Any,
    high_value: Any,
    low_value: Any,
    close_value: Any,
    volume_value: Any,
    amount_value: Any,
    previous_close_value: Any | None = None,
    label: str,
) -> OfficialDailyBar:
    parsed_date = _iso_date(date_value, f"{label} date")
    opened = _decimal(open_value, f"{label} open", positive=True)
    high = _decimal(high_value, f"{label} high", positive=True)
    low = _decimal(low_value, f"{label} low", positive=True)
    close = _decimal(close_value, f"{label} close", positive=True)
    volume = _decimal(volume_value, f"{label} volume", positive=False)
    amount = _decimal(amount_value, f"{label} amount", positive=False)
    previous_close = (
        _decimal(previous_close_value, f"{label} previous_close", positive=True)
        if previous_close_value is not None
        else None
    )
    if high < max(opened, low, close):
        raise OfficialHistoricalBarsBlockedError(f"{label} high violates OHLC range")
    if low > min(opened, high, close):
        raise OfficialHistoricalBarsBlockedError(f"{label} low violates OHLC range")
    return OfficialDailyBar(
        date=parsed_date,
        open=float(opened),
        high=float(high),
        low=float(low),
        close=float(close),
        volume=float(volume),
        amount=float(amount),
        previous_close=float(previous_close) if previous_close is not None else None,
    )


def _validate_bar_sequence(bars: Sequence[OfficialDailyBar], label: str) -> None:
    if not bars:
        raise OfficialHistoricalBarsBlockedError(f"{label} contains no bars")
    dates = [item.date for item in bars]
    if len(dates) != len(set(dates)):
        raise OfficialHistoricalBarsBlockedError(f"{label} contains duplicate dates")
    if dates != sorted(dates):
        raise OfficialHistoricalBarsBlockedError(
            f"{label} dates are not strictly increasing"
        )


def _logical_hash(exchange: str, code: str, bars: Sequence[OfficialDailyBar]) -> str:
    return _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": PROTOCOL_VERSION,
                "exchange": exchange,
                "code": code,
                "bars": [item.to_dict() for item in bars],
            }
        )
    )


def _unpersisted_evidence(
    raw_bytes: bytes,
    *,
    source_url: str,
    method: str,
    retrieved_at: str,
    content_type: str,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    expected_sha256: str | None,
) -> RawResponseEvidence:
    digest = _verify_hash(raw_bytes, expected_sha256, "official response")
    return RawResponseEvidence(
        source_url=source_url,
        method=method,
        retrieved_at=_retrieved_at(retrieved_at),
        content_sha256=digest,
        byte_count=len(raw_bytes),
        content_type=content_type,
        cas_uri=f"sha256:{digest}",
        object_path=None,
        persisted=False,
        request=dict(request),
        response=dict(response),
    )


def parse_sse_dayk_page(
    raw_bytes: bytes,
    *,
    code: str,
    request_begin: int,
    request_end: int,
    callback: str = SSE_JSONP_CALLBACK,
    expected_sha256: str | None = None,
) -> _SSEPage:
    """Parse one SSE half-open page, including negative-index probe semantics."""

    normalized_code = _normalize_code(code, "SSE")
    _verify_hash(raw_bytes, expected_sha256, "SSE response")
    payload = _decode_jsonp(raw_bytes, callback)
    required = {"code", "total", "begin", "end", "kline"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise OfficialHistoricalBarsBlockedError("SSE dayk schema drift detected")
    if str(payload["code"]).strip() != normalized_code:
        raise OfficialHistoricalBarsBlockedError("SSE response code does not match request")
    total = _strict_int(payload["total"], "SSE total")
    if total <= 0 or total > MAX_SSE_TOTAL_ROWS:
        raise OfficialHistoricalBarsBlockedError(f"SSE total outside admitted range: {total}")
    begin = _strict_int(request_begin, "SSE request begin", allow_negative=True)
    end = _strict_int(request_end, "SSE request end", allow_negative=True)
    normalized_begin = max(0, total + begin) if begin < 0 else min(begin, total)
    normalized_end = max(0, total + end) if end < 0 else min(end, total)
    if normalized_begin >= normalized_end:
        raise OfficialHistoricalBarsBlockedError("SSE request interval is empty or reversed")
    response_begin = _strict_int(payload["begin"], "SSE response begin", allow_negative=True)
    response_end = _strict_int(payload["end"], "SSE response end", allow_negative=True)
    if (response_begin, response_end) == (begin, end):
        semantics = "REQUEST_ECHO_HALF_OPEN"
    elif (response_begin, response_end) == (normalized_begin, normalized_end):
        semantics = "DATA_BOUND_NORMALIZED_HALF_OPEN"
    else:
        raise OfficialHistoricalBarsBlockedError(
            "SSE pagination begin/end drift detected"
        )
    rows = payload["kline"]
    if not isinstance(rows, list):
        raise OfficialHistoricalBarsBlockedError("SSE kline must be an array")
    expected_count = normalized_end - normalized_begin
    if len(rows) != expected_count:
        raise OfficialHistoricalBarsBlockedError(
            f"SSE page row range mismatch: {len(rows)} != {expected_count}"
        )
    bars: list[OfficialDailyBar] = []
    for position, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(SSE_DAYK_FIELDS):
            raise OfficialHistoricalBarsBlockedError("SSE kline schema drift detected")
        bars.append(
            _make_bar(
                date_value=row[0],
                open_value=row[1],
                high_value=row[2],
                low_value=row[3],
                close_value=row[4],
                volume_value=row[5],
                amount_value=row[6],
                label=f"SSE row {position}",
            )
        )
    _validate_bar_sequence(bars, "SSE page")
    return _SSEPage(
        code=normalized_code,
        total=total,
        request_begin=begin,
        request_end=end,
        normalized_begin=normalized_begin,
        normalized_end=normalized_end,
        response_begin=response_begin,
        response_end=response_end,
        response_interval_semantics=semantics,
        bars=tuple(bars),
    )


def parse_bse_kline_response(
    raw_bytes: bytes,
    *,
    code: str,
    expected_status: Any | None = None,
    expected_sha256: str | None = None,
) -> tuple[tuple[OfficialDailyBar, ...], Any, str]:
    if expected_status is None:
        raise OfficialHistoricalBarsBlockedError(
            "BSE success status has not been manually admitted",
            status=BSE_STATUS_CONTRACT_UNADMITTED,
        )
    _normalize_code(code, "BSE")
    _verify_hash(raw_bytes, expected_sha256, "BSE response")
    payload = _decode_json(raw_bytes, "BSE response")
    if not isinstance(payload, dict) or not {"status", "msg", "data"}.issubset(payload):
        raise OfficialHistoricalBarsBlockedError("BSE kline schema drift detected")
    observed_status = payload["status"]
    if isinstance(observed_status, (dict, list)) or observed_status is None:
        raise OfficialHistoricalBarsBlockedError("BSE status is not a scalar")
    if type(observed_status) is not type(expected_status) or observed_status != expected_status:
        raise OfficialHistoricalBarsBlockedError(
            f"BSE status mismatch: observed={observed_status!r}"
        )
    rows = payload["data"]
    if not isinstance(rows, list) or not rows:
        raise OfficialHistoricalBarsBlockedError("BSE data is missing or empty")
    bars: list[OfficialDailyBar] = []
    expected_fields = set(BSE_KLINE_FIELDS)
    for position, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise OfficialHistoricalBarsBlockedError("BSE kline schema drift detected")
        bars.append(
            _make_bar(
                date_value=row["jsrq"],
                open_value=row["jrkp"],
                high_value=row["drzd"],
                low_value=row["drzx"],
                close_value=row["jrsp"],
                previous_close_value=row["zrsp"],
                volume_value=row["cjl"],
                amount_value=row["cjje"],
                label=f"BSE row {position}",
            )
        )
    _validate_bar_sequence(bars, "BSE response")
    return tuple(bars), observed_status, str(payload.get("msg") or "")


def observe_bse_status_from_bytes(
    raw_bytes: bytes, *, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Read-only admission evidence; it never declares the response successful."""

    digest = _verify_hash(raw_bytes, expected_sha256, "BSE response")
    payload = _decode_json(raw_bytes, "BSE response")
    if not isinstance(payload, dict) or "status" not in payload:
        raise OfficialHistoricalBarsBlockedError("BSE status field is missing")
    observed = payload["status"]
    if isinstance(observed, (dict, list)) or observed is None:
        raise OfficialHistoricalBarsBlockedError("BSE status is not a scalar")
    return {
        "ready": False,
        "status": BSE_STATUS_CONTRACT_UNADMITTED,
        "observed_status": observed,
        "observed_status_text": str(observed),
        "content_sha256": digest,
        "promotion_blocked": True,
    }


def parse_szse_public_history_probe(
    raw_bytes: bytes,
    *,
    code: str,
    raw_response: RawResponseEvidence,
    expected_sha256: str | None = None,
) -> SZSEPublicHistoryProbe:
    normalized_code = _normalize_code(code, "SZSE")
    _verify_hash(raw_bytes, expected_sha256, "SZSE response")
    payload = _decode_json(raw_bytes, "SZSE response")
    if not isinstance(payload, dict) or "code" not in payload:
        raise OfficialHistoricalBarsBlockedError("SZSE public history schema drift detected")
    observed_code = payload.get("code")
    message = str(payload.get("message") or payload.get("msg") or "")
    data_present = not _is_effectively_empty(payload.get("data"))
    if str(observed_code) != "0":
        detail = (
            f"SZSE public history endpoint rejected {normalized_code}: "
            f"code={observed_code!r}, message={message!r}"
        )
    elif not data_present:
        detail = f"SZSE public history endpoint returned no data for {normalized_code}"
    else:
        detail = (
            "SZSE public response contains data, but no authorized SZSI historical "
            "schema has been admitted"
        )
    return SZSEPublicHistoryProbe(
        code=f"{normalized_code}.SZ",
        ready=False,
        status=REQUIRES_AUTHORIZED_SZSI_HISTORY,
        detail=detail,
        observed_code=observed_code,
        observed_message=message,
        data_present=data_present,
        raw_response=raw_response,
    )


def _is_effectively_empty(value: Any) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, Mapping):
        return not value or all(_is_effectively_empty(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return not value or all(_is_effectively_empty(item) for item in value)
    return False


class OfficialHistoricalBarsClient:
    """Read-only clients for official exchange history endpoints."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        cas: RawResponseCAS | None = None,
        sse_endpoint: str = SSE_DAYK_ENDPOINT,
        bse_endpoint: str = BSE_KLINE_ENDPOINT,
        szse_endpoint: str = SZSE_PUBLIC_HISTORY_ENDPOINT,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.cas = cas
        self.sse_endpoint = sse_endpoint
        self.bse_endpoint = bse_endpoint
        self.szse_endpoint = szse_endpoint

    def fetch_sse(
        self,
        code: str,
        *,
        page_size: int = 500,
        retrieved_at: str | None = None,
        expected_page_hashes: Mapping[str, str] | None = None,
    ) -> OfficialBarsArtifact:
        normalized_code = _normalize_code(code, "SSE")
        if page_size <= 0 or page_size > 5_000:
            raise OfficialHistoricalBarsBlockedError("SSE page_size outside admitted range")
        retrieved = _retrieved_at(retrieved_at)
        url = self.sse_endpoint.format(code=normalized_code)
        _validate_origin(url, host=SSE_DAYK_HOST, port=SSE_DAYK_PORT)
        expected_hashes = dict(expected_page_hashes or {})
        bars: list[OfficialDailyBar] = []
        evidence: list[RawResponseEvidence] = []
        begin = 0
        total: int | None = None
        interval_semantics: set[str] = set()
        while total is None or begin < total:
            end = begin + page_size if total is None else min(begin + page_size, total)
            params = {
                "callback": SSE_JSONP_CALLBACK,
                "select": SSE_DAYK_SELECT,
                "begin": begin,
                "end": end,
            }
            raw, content_type, response_url = self._get(
                url,
                params=params,
                host=SSE_DAYK_HOST,
                port=SSE_DAYK_PORT,
                content_types=(
                    "application/javascript",
                    "text/javascript",
                    "application/json",
                    "text/plain",
                ),
                headers={"Referer": "https://www.sse.com.cn/"},
            )
            page_key = f"{begin}:{end}"
            expected_hash = expected_hashes.pop(page_key, None)
            page = parse_sse_dayk_page(
                raw,
                code=normalized_code,
                request_begin=begin,
                request_end=end,
                callback=SSE_JSONP_CALLBACK,
                expected_sha256=expected_hash,
            )
            if total is None:
                total = page.total
            elif page.total != total:
                raise OfficialHistoricalBarsBlockedError("SSE total changed across pages")
            interval_semantics.add(page.response_interval_semantics)
            page_response = {
                "code": page.code,
                "total": page.total,
                "begin": page.response_begin,
                "end": page.response_end,
                "interval": "HALF_OPEN",
                "interval_semantics": page.response_interval_semantics,
                "row_count": len(page.bars),
            }
            evidence.append(
                self._capture(
                    raw,
                    source_url=response_url,
                    method="GET",
                    retrieved_at=retrieved,
                    content_type=content_type,
                    request=params,
                    response=page_response,
                    expected_sha256=expected_hash,
                )
            )
            bars.extend(page.bars)
            begin = page.normalized_end
        if expected_hashes:
            raise OfficialHistoricalBarsBlockedError(
                f"unused expected SSE page hashes: {sorted(expected_hashes)}"
            )
        if total is None or len(bars) != total:
            raise OfficialHistoricalBarsBlockedError("SSE full-history coverage is incomplete")
        _validate_bar_sequence(bars, "SSE full history")
        suffixed_code = f"{normalized_code}.SH"
        return OfficialBarsArtifact(
            exchange="SSE",
            code=suffixed_code,
            source_url=url,
            bars=tuple(bars),
            raw_responses=tuple(evidence),
            logical_content_sha256=_logical_hash("SSE", suffixed_code, bars),
            pagination={
                "supported": True,
                "interval": "HALF_OPEN",
                "page_size": page_size,
                "page_count": len(evidence),
                "total": total,
                "response_interval_semantics": sorted(interval_semantics),
                "complete": True,
            },
        )

    def fetch_bse(
        self,
        code: str,
        *,
        expected_status: Any | None = None,
        retrieved_at: str | None = None,
        expected_sha256: str | None = None,
    ) -> OfficialBarsArtifact:
        if expected_status is None:
            raise OfficialHistoricalBarsBlockedError(
                "BSE success status has not been manually admitted",
                status=BSE_STATUS_CONTRACT_UNADMITTED,
            )
        normalized_code = _normalize_code(code, "BSE")
        retrieved = _retrieved_at(retrieved_at)
        url = self.bse_endpoint.format(code=normalized_code)
        _validate_origin(url, host=BSE_KLINE_HOST)
        params = {"type": "dayKline", "xxfcbj": 2, "begin": -6, "end": -1}
        raw, content_type, response_url = self._get(
            url,
            params=params,
            host=BSE_KLINE_HOST,
            port=443,
            content_types=("application/json", "text/json", "text/plain"),
            headers={"Referer": "https://www.bse.cn/"},
        )
        bars, observed_status, message = parse_bse_kline_response(
            raw,
            code=normalized_code,
            expected_status=expected_status,
            expected_sha256=expected_sha256,
        )
        response = {
            "observed_status": observed_status,
            "message": message,
            "row_count": len(bars),
            "pagination_supported": False,
        }
        evidence = self._capture(
            raw,
            source_url=response_url,
            method="GET",
            retrieved_at=retrieved,
            content_type=content_type,
            request=params,
            response=response,
            expected_sha256=expected_sha256,
        )
        suffixed_code = f"{normalized_code}.BJ"
        return OfficialBarsArtifact(
            exchange="BSE",
            code=suffixed_code,
            source_url=url,
            bars=bars,
            raw_responses=(evidence,),
            logical_content_sha256=_logical_hash("BSE", suffixed_code, bars),
            pagination={
                "supported": False,
                "server_behavior": "FULL_RESPONSE_IGNORES_BEGIN_END",
                "requested_begin": -6,
                "requested_end": -1,
                "page_count": 1,
                "total": len(bars),
                "complete": True,
            },
        )

    def observe_bse_status(
        self,
        code: str,
        *,
        retrieved_at: str | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        normalized_code = _normalize_code(code, "BSE")
        retrieved = _retrieved_at(retrieved_at)
        url = self.bse_endpoint.format(code=normalized_code)
        _validate_origin(url, host=BSE_KLINE_HOST)
        params = {"type": "dayKline", "xxfcbj": 2, "begin": -6, "end": -1}
        raw, content_type, response_url = self._get(
            url,
            params=params,
            host=BSE_KLINE_HOST,
            port=443,
            content_types=("application/json", "text/json", "text/plain"),
            headers={"Referer": "https://www.bse.cn/"},
        )
        observed = observe_bse_status_from_bytes(raw, expected_sha256=expected_sha256)
        evidence = self._capture(
            raw,
            source_url=response_url,
            method="GET",
            retrieved_at=retrieved,
            content_type=content_type,
            request=params,
            response={"observed_status": observed["observed_status"]},
            expected_sha256=expected_sha256,
        )
        return {**observed, "raw_response": evidence.to_dict()}

    def probe_szse(
        self,
        code: str,
        *,
        retrieved_at: str | None = None,
        expected_sha256: str | None = None,
    ) -> SZSEPublicHistoryProbe:
        normalized_code = _normalize_code(code, "SZSE")
        retrieved = _retrieved_at(retrieved_at)
        url = self.szse_endpoint
        _validate_origin(url, host=SZSE_PUBLIC_HISTORY_HOST)
        params = {"cycleType": 32, "marketId": 1, "code": normalized_code}
        raw, content_type, response_url = self._get(
            url,
            params=params,
            host=SZSE_PUBLIC_HISTORY_HOST,
            port=443,
            content_types=("application/json", "text/json", "text/plain"),
            headers={"Referer": "https://www.szse.cn/"},
        )
        payload = _decode_json(raw, "SZSE response")
        response_summary = {
            "observed_code": payload.get("code") if isinstance(payload, dict) else None,
            "data_present": (
                not _is_effectively_empty(payload.get("data"))
                if isinstance(payload, dict)
                else False
            ),
        }
        evidence = self._capture(
            raw,
            source_url=response_url,
            method="GET",
            retrieved_at=retrieved,
            content_type=content_type,
            request=params,
            response=response_summary,
            expected_sha256=expected_sha256,
        )
        return parse_szse_public_history_probe(
            raw,
            code=normalized_code,
            raw_response=evidence,
            expected_sha256=expected_sha256,
        )

    def _capture(
        self,
        raw: bytes,
        *,
        source_url: str,
        method: str,
        retrieved_at: str,
        content_type: str,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        expected_sha256: str | None,
    ) -> RawResponseEvidence:
        if self.cas is not None:
            return self.cas.capture(
                raw,
                source_url=source_url,
                method=method,
                retrieved_at=retrieved_at,
                content_type=content_type,
                request=request,
                response=response,
                expected_sha256=expected_sha256,
            )
        return _unpersisted_evidence(
            raw,
            source_url=source_url,
            method=method,
            retrieved_at=retrieved_at,
            content_type=content_type,
            request=request,
            response=response,
            expected_sha256=expected_sha256,
        )

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, Any],
        host: str,
        port: int,
        content_types: Sequence[str],
        headers: Mapping[str, str],
    ) -> tuple[bytes, str, str]:
        _validate_origin(url, host=host, port=port)
        request_headers = {
            "User-Agent": "tdx-research-platform/official-historical-bars-v2",
            **dict(headers),
        }
        response = self.session.get(
            url,
            params=dict(params),
            headers=request_headers,
            timeout=self.timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise OfficialHistoricalBarsBlockedError(
                f"official GET failed closed: HTTP {response.status_code}"
            )
        response_url = str(response.url)
        _validate_origin(response_url, host=host, port=port)
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower()
        if content_type not in content_types:
            raise OfficialHistoricalBarsBlockedError(
                f"official response content type changed: {content_type!r}"
            )
        content = bytes(response.content)
        if not content or len(content) > MAX_RESPONSE_BYTES:
            raise OfficialHistoricalBarsBlockedError("official response is empty or oversized")
        return content, content_type, response_url
