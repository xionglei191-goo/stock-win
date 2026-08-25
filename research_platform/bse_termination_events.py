from __future__ import annotations

import hashlib
import html.parser
import io
import json
import math
import os
import re
import stat
import unicodedata
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests


PROTOCOL_VERSION = "bse-termination-event-ledger-v2"
SOURCE_CONTRACT_ADMITTED = "SOURCE_CONTRACT_ADMITTED"
SOURCE_CONTRACT_UNADMITTED = "SOURCE_CONTRACT_UNADMITTED"
SOURCE_REJECTED = "SOURCE_REJECTED"
TERMINATION_CLASSIFICATION_INCOMPLETE = "TERMINATION_CLASSIFICATION_INCOMPLETE"
TERMINATION_EFFECTIVE_DATE_INCOMPLETE = "TERMINATION_EFFECTIVE_DATE_INCOMPLETE"
SOURCE_COMPLETE = "SOURCE_COMPLETE"

BSE_TERMINATION_ENDPOINT = (
    "https://www.bse.cn/disclosureInfoController/stockInfoResult.do"
)
BSE_TERMINATION_HOST = "www.bse.cn"
BSE_DISCLOSURE_TYPE = "9506"
BSE_MARKET_LAYER = "2"
BSE_SITE_ID = "6"
BSE_KEYWORD = "\u7ec8\u6b62\u4e0a\u5e02"
BSE_PAGE_SIZE = 20
BSE_REQUIRED_FIELDS = (
    "companyCd",
    "xxfcbj",
    "companyName",
    "disclosureTitle",
    "disclosurePostTitle",
    "destFilePath",
    "publishDate",
    "fileExt",
    "isNewThree",
    "xxzrlx",
    "infoId",
)
BSE_LIST_INFO_FIELDS = frozenset(
    {
        "content",
        "firstPage",
        "lastPage",
        "number",
        "numberOfElements",
        "size",
        "sort",
        "totalElements",
        "totalPages",
    }
)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TARGET_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_NOTICE_PDF_BYTES = 16 * 1024 * 1024
MAX_NOTICE_PDF_PAGES = 50
MAX_NOTICE_TEXT_CHARS = 2_000_000
MAX_PAGES = 10_000
USER_AGENT = "stock-research-platform-bse-termination-ledger/1.0"


class BSETerminationEventBlockedError(RuntimeError):
    """An official source or derived event failed the frozen admission contract."""

    def __init__(self, message: str, *, status: str = SOURCE_REJECTED) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SourceBlobEvidence:
    source_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawPageEvidence:
    source_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    cas_uri: str
    object_path: str
    request: Mapping[str, Any]
    response: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["request"] = dict(self.request)
        value["response"] = dict(self.response)
        return value


@dataclass(frozen=True)
class BSETerminationRecord:
    code_alias: str
    company_name: str
    legal_name: str
    disclosure_title: str
    disclosure_post_title: str
    notice_date: str
    notice_url: str
    list_page_url: str
    list_page_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BSETerminationPage:
    number: int
    size: int
    total_elements: int
    total_pages: int
    first_page: bool
    last_page: bool
    records: tuple[BSETerminationRecord, ...]
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "records": [item.to_dict() for item in self.records],
        }


@dataclass(frozen=True)
class TargetListingEvidence:
    exchange: str
    code_alias: str
    legal_name: str
    listing_date: str
    source_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BSETerminationNoticeEvidence:
    code_alias: str
    legal_name: str
    notice_date: str
    termination_effective_date: str
    source_url: str
    method: str
    retrieved_at: str
    content_sha256: str
    byte_count: int
    content_type: str
    cas_uri: str
    object_path: str
    extraction_engine: str
    extraction_engine_version: str
    page_count: int
    text_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BSETerminationEvent:
    canonical_entity_id: str
    source_exchange: str
    source_code_alias: str
    legal_name: str
    termination_notice_date: str
    termination_notice_url: str
    termination_effective_date: str | None
    termination_evidence_url: str | None
    termination_evidence_sha256: str | None
    classification: str
    target_exchange: str | None
    target_code_alias: str | None
    target_listing_date: str | None
    target_evidence_url: str | None
    target_evidence_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManifestEvidence:
    manifest_sha256: str
    cas_uri: str
    object_path: str
    byte_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BSETerminationLedgerArtifact:
    query: Mapping[str, Any]
    records: tuple[BSETerminationRecord, ...]
    events: tuple[BSETerminationEvent, ...]
    raw_pages: tuple[RawPageEvidence, ...]
    termination_notice_evidence: tuple[BSETerminationNoticeEvidence, ...]
    target_evidence: tuple[TargetListingEvidence, ...]
    completeness: Mapping[str, Any]
    logical_content_sha256: str
    manifest: ManifestEvidence | None = None

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "query": dict(self.query),
            "records": [item.to_dict() for item in self.records],
            "events": [item.to_dict() for item in self.events],
            "raw_pages": [item.to_dict() for item in self.raw_pages],
            "termination_notice_evidence": [
                item.to_dict() for item in self.termination_notice_evidence
            ],
            "target_evidence": [item.to_dict() for item in self.target_evidence],
            "completeness": dict(self.completeness),
            "logical_content_sha256": self.logical_content_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.to_manifest_dict()
        value["manifest"] = self.manifest.to_dict() if self.manifest else None
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


def _verify_hash(content: bytes, expected: str | None, label: str) -> str:
    digest = _sha256(content)
    if expected is None:
        return digest
    normalized = str(expected).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise BSETerminationEventBlockedError(f"invalid expected {label} SHA-256")
    if digest != normalized:
        raise BSETerminationEventBlockedError(
            f"{label} hash mismatch: expected {normalized}, got {digest}"
        )
    return digest


def _retrieved_at(value: str | None) -> str:
    text = value or datetime.now().astimezone().isoformat()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BSETerminationEventBlockedError(
            f"retrieved_at is not ISO-8601: {text!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise BSETerminationEventBlockedError("retrieved_at must include a timezone")
    return parsed.isoformat()


def _iso_date(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise BSETerminationEventBlockedError(
            f"invalid {label}: {value!r}"
        ) from exc


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BSETerminationEventBlockedError(f"invalid {label}: {value!r}")
    return value


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise BSETerminationEventBlockedError(f"invalid {label}: {value!r}")
    return value


def _normalized_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\x00", "")
    return re.sub(r"\s+", "", text)


def _nonempty_text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise BSETerminationEventBlockedError(f"invalid {label}: {value!r}")
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise BSETerminationEventBlockedError(f"invalid {label}: {value!r}")
    return text


def _content_type(value: Any, *, list_response: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BSETerminationEventBlockedError("official response content type is missing")
    parts = [item.strip().lower() for item in value.split(";")]
    if parts[0] != "text/html":
        raise BSETerminationEventBlockedError(
            f"unexpected official response content type: {value!r}"
        )
    parameters = {item for item in parts[1:] if item}
    admitted_parameters = (
        parameters == {"charset=utf-8"}
        if list_response
        else parameters in (set(), {"charset=utf-8"})
    )
    if not admitted_parameters:
        label = "BSE list" if list_response else "target listing"
        raise BSETerminationEventBlockedError(
            f"{label} response content-type parameters are not admitted"
        )
    return "text/html;charset=utf-8" if parameters else "text/html"


def _atomic_write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _unsafe_cas_path(path) or path.read_bytes() != content:
            raise BSETerminationEventBlockedError(
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
            if _unsafe_cas_path(path) or path.read_bytes() != content:
                raise BSETerminationEventBlockedError(
                    f"content-address collision or corruption: {path}"
                )
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _unsafe_cas_path(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return True
    try:
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
    except OSError:
        return True
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _read_cas_object(
    *,
    object_path: str,
    expected_sha256: str,
    expected_byte_count: int,
    label: str,
    maximum: int,
) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BSETerminationEventBlockedError(f"invalid {label} CAS hash")
    path = Path(object_path)
    if not path.is_absolute() or _unsafe_cas_path(path):
        raise BSETerminationEventBlockedError(f"{label} CAS object is unavailable")
    content = path.read_bytes()
    if (
        expected_byte_count != len(content)
        or not content
        or len(content) > maximum
        or _sha256(content) != expected_sha256
    ):
        raise BSETerminationEventBlockedError(f"{label} CAS object was tampered")
    return content


class BSEEventLedgerStore:
    """Immutable content-addressed storage for source bytes and ledger manifests."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def capture_blob(
        self,
        content: bytes,
        *,
        source_url: str,
        retrieved_at: str,
        content_type: str,
        expected_sha256: str | None = None,
    ) -> SourceBlobEvidence:
        digest = _verify_hash(content, expected_sha256, "official document")
        path = self.root / "objects" / digest[:2] / digest
        _atomic_write_exact(path, content)
        if _sha256(path.read_bytes()) != digest:
            raise BSETerminationEventBlockedError("source CAS verification failed")
        return SourceBlobEvidence(
            source_url=source_url,
            method="GET",
            retrieved_at=_retrieved_at(retrieved_at),
            content_sha256=digest,
            byte_count=len(content),
            content_type=content_type,
            cas_uri=f"sha256:{digest}",
            object_path=str(path.resolve()),
        )

    def _assert_object_path(self, *, content_sha256: str, object_path: str) -> None:
        expected = (
            self.root / "objects" / content_sha256[:2] / content_sha256
        ).resolve()
        supplied = Path(object_path)
        try:
            resolved = supplied.resolve(strict=True)
        except OSError as exc:
            raise BSETerminationEventBlockedError(
                "source CAS object is unavailable from this store"
            ) from exc
        if (
            not supplied.is_absolute()
            or object_path != str(expected)
            or resolved != expected
            or _unsafe_cas_path(supplied)
        ):
            raise BSETerminationEventBlockedError(
                "source CAS object is outside the declared ledger store"
            )

    def publish(self, artifact: BSETerminationLedgerArtifact) -> ManifestEvidence:
        if artifact.manifest is not None:
            raise BSETerminationEventBlockedError("ledger manifest is already attached")
        supplied = artifact.to_manifest_dict()
        recomputed = _rebuild_artifact_from_manifest_value(supplied, store=self)
        if recomputed.to_manifest_dict() != supplied:
            raise BSETerminationEventBlockedError(
                "caller-supplied ledger aggregate does not match CAS recomputation"
            )
        content = _canonical_json_bytes(recomputed.to_manifest_dict())
        digest = _sha256(content)
        path = self.root / "manifests" / f"{digest}.json"
        _atomic_write_exact(path, content)
        if _sha256(path.read_bytes()) != digest:
            raise BSETerminationEventBlockedError("ledger manifest CAS verification failed")
        return ManifestEvidence(
            manifest_sha256=digest,
            cas_uri=f"sha256:{digest}",
            object_path=str(path.resolve()),
            byte_count=len(content),
        )

    def verify_manifest(self, evidence: ManifestEvidence) -> Mapping[str, Any]:
        if (
            not isinstance(evidence.manifest_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence.manifest_sha256)
            or evidence.cas_uri != f"sha256:{evidence.manifest_sha256}"
            or not isinstance(evidence.object_path, str)
            or not evidence.object_path
            or isinstance(evidence.byte_count, bool)
            or not isinstance(evidence.byte_count, int)
            or evidence.byte_count <= 0
        ):
            raise BSETerminationEventBlockedError(
                "manifest evidence metadata is invalid"
            )
        path = Path(evidence.object_path)
        expected_path = (self.root / "manifests" / f"{evidence.manifest_sha256}.json").resolve()
        try:
            resolved_path = path.resolve(strict=True)
        except OSError as exc:
            raise BSETerminationEventBlockedError(
                "manifest CAS object is unavailable"
            ) from exc
        if (
            not path.is_absolute()
            or evidence.object_path != str(expected_path)
            or resolved_path != expected_path
            or _unsafe_cas_path(path)
        ):
            raise BSETerminationEventBlockedError("manifest CAS object is unavailable")
        content = path.read_bytes()
        if len(content) != evidence.byte_count or _sha256(content) != evidence.manifest_sha256:
            raise BSETerminationEventBlockedError("manifest CAS object was tampered")
        value = _strict_json(content, "ledger manifest")
        if _canonical_json_bytes(value) != content:
            raise BSETerminationEventBlockedError("ledger manifest is not canonical JSON")
        recomputed = _rebuild_artifact_from_manifest_value(value, store=self)
        if recomputed.to_manifest_dict() != value:
            raise BSETerminationEventBlockedError(
                "ledger manifest aggregate does not match raw CAS recomputation"
            )
        return recomputed.to_manifest_dict()


def _request_pairs(start_date: str, end_date: str, page: int) -> list[tuple[str, str]]:
    return [
        ("disclosureType", BSE_DISCLOSURE_TYPE),
        ("page", str(page)),
        ("isNewThree", ""),
        ("xxfcbj[]", BSE_MARKET_LAYER),
        ("xxggfl", ""),
        ("startTime", start_date),
        ("endTime", end_date),
        ("keyword", BSE_KEYWORD),
        ("siteId", BSE_SITE_ID),
        *(("needFields[]", field) for field in BSE_REQUIRED_FIELDS),
    ]


def _validate_list_endpoint(endpoint: str) -> str:
    parsed = urlsplit(str(endpoint or ""))
    if (
        parsed.scheme != "https"
        or parsed.hostname != BSE_TERMINATION_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path != "/disclosureInfoController/stockInfoResult.do"
        or parsed.query
        or parsed.fragment
    ):
        raise BSETerminationEventBlockedError("BSE list endpoint origin changed")
    return endpoint


def build_bse_termination_request_url(
    *, start_date: str, end_date: str, page: int, endpoint: str = BSE_TERMINATION_ENDPOINT
) -> str:
    start = _iso_date(start_date, "start date")
    end = _iso_date(end_date, "end date")
    if start > end:
        raise BSETerminationEventBlockedError("BSE query date range is reversed")
    page_number = _strict_int(page, "request page")
    base = _validate_list_endpoint(endpoint)
    return f"{base}?{urlencode(_request_pairs(start, end, page_number), doseq=True)}"


def _validate_request_url(
    source_url: str, *, start_date: str, end_date: str, page: int
) -> str:
    expected = build_bse_termination_request_url(
        start_date=start_date,
        end_date=end_date,
        page=page,
    )
    parsed = urlsplit(source_url)
    expected_parsed = urlsplit(expected)
    if (
        parsed.scheme != "https"
        or parsed.hostname != BSE_TERMINATION_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path != expected_parsed.path
        or parsed.fragment
        or parse_qsl(parsed.query, keep_blank_values=True)
        != parse_qsl(expected_parsed.query, keep_blank_values=True)
    ):
        raise BSETerminationEventBlockedError("BSE request URL contract drift")
    return expected


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BSETerminationEventBlockedError(
                f"official JSON contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise BSETerminationEventBlockedError(f"official JSON contains {value}")


def _strict_json(raw_bytes: bytes, label: str) -> Any:
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BSETerminationEventBlockedError(f"{label} is not strict UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except BSETerminationEventBlockedError:
        raise
    except (TypeError, ValueError) as exc:
        raise BSETerminationEventBlockedError(f"{label} is invalid JSON") from exc


def _decode_bse_wrapper(raw_bytes: bytes) -> Any:
    if not raw_bytes or len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise BSETerminationEventBlockedError("BSE list response is empty or oversized")
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BSETerminationEventBlockedError("BSE list response is not strict UTF-8") from exc
    if not text.startswith("null(") or not text.endswith(")"):
        raise BSETerminationEventBlockedError("BSE list JSONP wrapper drift")
    inner = text[5:-1]
    if not inner or inner != inner.strip():
        raise BSETerminationEventBlockedError("BSE list JSONP wrapper whitespace drift")
    return _strict_json(inner.encode("utf-8"), "BSE list payload")


def _notice_url(path: Any) -> str:
    value = _nonempty_text(path, "BSE disclosure path", maximum=500)
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or ".." in parsed.path.split("/")
        or not parsed.path.lower().endswith(".pdf")
        or not (
            parsed.path.startswith("/disclosure/")
            or parsed.path.startswith("/uploads/")
        )
    ):
        raise BSETerminationEventBlockedError(
            f"invalid BSE disclosure path: {value!r}"
        )
    return f"https://{BSE_TERMINATION_HOST}{parsed.path}"


def _extract_legal_name(title: str) -> str:
    compact = _normalized_text(title)
    matched = re.fullmatch(r"\u5173\u4e8e(.+?)\u80a1\u7968\u7ec8\u6b62\u4e0a\u5e02\u7684\u516c\u544a", compact)
    if matched is None:
        raise BSETerminationEventBlockedError(
            "BSE termination title does not identify the legal company"
        )
    legal_name = matched.group(1)
    if len(legal_name) < 6 or len(legal_name) > 100 or not legal_name.endswith(
        "\u80a1\u4efd\u6709\u9650\u516c\u53f8"
    ):
        raise BSETerminationEventBlockedError(
            "BSE termination title contains an invalid legal company name"
        )
    return legal_name


def _parse_record(
    row: Any,
    *,
    start_date: str,
    end_date: str,
    source_url: str,
    content_sha256: str,
) -> BSETerminationRecord:
    if not isinstance(row, dict) or set(row) != set(BSE_REQUIRED_FIELDS):
        raise BSETerminationEventBlockedError("BSE termination row schema drift")
    raw_code = row["companyCd"]
    if not isinstance(raw_code, str) or not re.fullmatch(r"\d{6}", raw_code):
        raise BSETerminationEventBlockedError(f"invalid BSE company code: {raw_code!r}")
    if row["xxfcbj"] != BSE_MARKET_LAYER:
        raise BSETerminationEventBlockedError("BSE market-layer marker drift")
    company_name = _nonempty_text(row["companyName"], "BSE company name", maximum=100)
    title = _nonempty_text(row["disclosureTitle"], "BSE title", maximum=300)
    post_title = row["disclosurePostTitle"]
    if not isinstance(post_title, str) or len(post_title) > 100 or "\x00" in post_title:
        raise BSETerminationEventBlockedError("invalid BSE disclosure post-title")
    notice_date = _iso_date(row["publishDate"], "BSE notice date")
    if not (start_date <= notice_date <= end_date):
        raise BSETerminationEventBlockedError("BSE notice date is outside query range")
    if row["fileExt"] != "pdf":
        raise BSETerminationEventBlockedError("BSE disclosure is not a PDF")
    if row["isNewThree"] != 3 or isinstance(row["isNewThree"], bool):
        raise BSETerminationEventBlockedError("BSE venue identity marker drift")
    if row["xxzrlx"] != "B":
        raise BSETerminationEventBlockedError("BSE security identity marker drift")
    _strict_int(row["infoId"], "BSE infoId")
    legal_name = _extract_legal_name(title + post_title)
    return BSETerminationRecord(
        code_alias=f"{raw_code}.BJ",
        company_name=company_name,
        legal_name=legal_name,
        disclosure_title=title,
        disclosure_post_title=post_title,
        notice_date=notice_date,
        notice_url=_notice_url(row["destFilePath"]),
        list_page_url=source_url,
        list_page_sha256=content_sha256,
    )


def parse_bse_termination_page(
    raw_bytes: bytes,
    *,
    start_date: str,
    end_date: str,
    request_page: int,
    source_url: str,
    expected_sha256: str | None = None,
) -> BSETerminationPage:
    start = _iso_date(start_date, "start date")
    end = _iso_date(end_date, "end date")
    if start > end:
        raise BSETerminationEventBlockedError("BSE query date range is reversed")
    page_request = _strict_int(request_page, "request page")
    canonical_url = _validate_request_url(
        source_url, start_date=start, end_date=end, page=page_request
    )
    digest = _verify_hash(raw_bytes, expected_sha256, "BSE list response")
    payload = _decode_bse_wrapper(raw_bytes)
    if not isinstance(payload, list) or len(payload) != 1:
        raise BSETerminationEventBlockedError("BSE list payload root schema drift")
    root = payload[0]
    if not isinstance(root, dict) or set(root) != {"listInfo", "status"}:
        raise BSETerminationEventBlockedError("BSE list payload schema drift")
    if isinstance(root["status"], bool) or root["status"] != 0:
        raise BSETerminationEventBlockedError(
            f"BSE list response status mismatch: {root['status']!r}"
        )
    listing = root["listInfo"]
    if not isinstance(listing, dict) or set(listing) != BSE_LIST_INFO_FIELDS:
        raise BSETerminationEventBlockedError("BSE pagination schema drift")
    if listing["sort"] is not None:
        raise BSETerminationEventBlockedError("BSE pagination sort contract drift")
    number = _strict_int(listing["number"], "BSE page number")
    count = _strict_int(listing["numberOfElements"], "BSE page element count")
    size = _strict_int(listing["size"], "BSE page size", minimum=1)
    total = _strict_int(listing["totalElements"], "BSE total elements")
    total_pages = _strict_int(listing["totalPages"], "BSE total pages")
    first = _strict_bool(listing["firstPage"], "BSE firstPage")
    last = _strict_bool(listing["lastPage"], "BSE lastPage")
    content = listing["content"]
    if not isinstance(content, list):
        raise BSETerminationEventBlockedError("BSE page content is not a list")
    if number != page_request:
        raise BSETerminationEventBlockedError("BSE response page number drift")
    if size != BSE_PAGE_SIZE:
        raise BSETerminationEventBlockedError("BSE response page size drift")
    if count != len(content) or count > size or total < count:
        raise BSETerminationEventBlockedError("BSE page element metadata mismatch")
    expected_pages = math.ceil(total / size) if total else 0
    if total_pages != expected_pages or total_pages > MAX_PAGES:
        raise BSETerminationEventBlockedError("BSE total-pages metadata mismatch")
    if total_pages == 0:
        metadata_ok = number == 0 and first and last and count == 0
    else:
        metadata_ok = (
            number < total_pages
            and first == (number == 0)
            and last == (number == total_pages - 1)
            and count == (size if not last else total - size * number)
        )
    if not metadata_ok:
        raise BSETerminationEventBlockedError("BSE page boundary metadata mismatch")
    records = tuple(
        _parse_record(
            row,
            start_date=start,
            end_date=end,
            source_url=canonical_url,
            content_sha256=digest,
        )
        for row in content
    )
    identities = [(item.code_alias, item.notice_date, item.notice_url) for item in records]
    if len(set(identities)) != len(identities):
        raise BSETerminationEventBlockedError("duplicate BSE termination rows in page")
    dates = [item.notice_date for item in records]
    if dates != sorted(dates, reverse=True):
        raise BSETerminationEventBlockedError("BSE termination rows are not date-descending")
    return BSETerminationPage(
        number=number,
        size=size,
        total_elements=total,
        total_pages=total_pages,
        first_page=first,
        last_page=last,
        records=records,
        content_sha256=digest,
    )


class _VisibleTextParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)


def _target_origin(source_url: str, exchange: str) -> str:
    parsed = urlsplit(str(source_url or ""))
    expected_suffix = {"SSE": "SH", "SZSE": "SZ"}.get(exchange)
    if expected_suffix is None:
        raise BSETerminationEventBlockedError(
            f"unsupported target exchange: {exchange!r}"
        )
    host = "www.sse.com.cn" if exchange == "SSE" else "www.szse.cn"
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise BSETerminationEventBlockedError("target listing evidence origin changed")
    if exchange == "SSE":
        valid_path = bool(
            re.fullmatch(
                r"/disclosure/announcement/listing/c/c_\d{8}_\d+\.shtml",
                parsed.path,
            )
        )
    else:
        valid_path = bool(
            re.fullmatch(
                r"/(?:disclosure/notice/company|English/about/news/listings/main)/"
                r"t\d{8}_\d+\.html",
                parsed.path,
            )
        )
    if not valid_path:
        raise BSETerminationEventBlockedError(
            "target listing evidence path is not an admitted official page"
        )
    return expected_suffix


def _html_visible_text(raw_bytes: bytes) -> str:
    if not raw_bytes or len(raw_bytes) > MAX_TARGET_DOCUMENT_BYTES:
        raise BSETerminationEventBlockedError(
            "target listing document is empty or oversized"
        )
    try:
        source = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BSETerminationEventBlockedError(
            "target listing document is not strict UTF-8"
        ) from exc
    parser = _VisibleTextParser()
    try:
        parser.feed(source)
        parser.close()
    except Exception as exc:
        raise BSETerminationEventBlockedError(
            "target listing HTML could not be parsed"
        ) from exc
    text = " ".join(parser.parts)
    if len(_normalized_text(text)) < 20:
        raise BSETerminationEventBlockedError(
            "target listing document has insufficient visible evidence"
        )
    return text


def _date_is_visible(text: str, listing_date: str) -> bool:
    day = date.fromisoformat(listing_date)
    variants = {
        day.isoformat(),
        day.strftime("%Y/%m/%d"),
        day.strftime("%Y.%m.%d"),
        f"{day.year}\u5e74{day.month}\u6708{day.day}\u65e5",
        day.strftime("%B %d, %Y"),
        day.strftime("%d %B %Y"),
    }
    normalized = unicodedata.normalize("NFKC", text).lower()
    compact = _normalized_text(text).lower()
    return any(
        item.lower() in normalized or _normalized_text(item).lower() in compact
        for item in variants
    )


def _validate_target_document_facts(
    raw_bytes: bytes,
    *,
    exchange: str,
    target_code: str,
    legal_name: str,
    listing_date: str,
    source_url: str,
    content_type: str,
    expected_sha256: str | None,
) -> tuple[str, str, str, str, str, str]:
    normalized_exchange = str(exchange or "").upper()
    suffix = _target_origin(source_url, normalized_exchange)
    raw_code = str(target_code or "").strip().upper()
    matched = re.fullmatch(r"(\d{6})(?:\.(SH|SZ))?", raw_code)
    if matched is None or (matched.group(2) is not None and matched.group(2) != suffix):
        raise BSETerminationEventBlockedError(
            f"invalid target listing code: {target_code!r}"
        )
    code = matched.group(1)
    name = _nonempty_text(legal_name, "target legal name", maximum=100)
    day = _iso_date(listing_date, "target listing date")
    media_type = _content_type(content_type, list_response=False)
    digest = _verify_hash(raw_bytes, expected_sha256, "target listing document")
    visible = _html_visible_text(raw_bytes)
    compact = _normalized_text(visible)
    if _normalized_text(name) not in compact:
        raise BSETerminationEventBlockedError(
            "target listing evidence does not contain the same legal company identity"
        )
    if not re.search(rf"(?<!\d){re.escape(code)}(?!\d)", visible):
        raise BSETerminationEventBlockedError(
            "target listing evidence does not contain the target code"
        )
    if not _date_is_visible(visible, day):
        raise BSETerminationEventBlockedError(
            "target listing evidence does not contain the target listing date"
        )
    lower = visible.lower()
    if not (
        "\u4e0a\u5e02" in visible
        or "\u8f6c\u677f" in visible
        or "listing" in lower
        or "listed" in lower
    ):
        raise BSETerminationEventBlockedError(
            "target official page does not assert a listing event"
        )
    return normalized_exchange, code, suffix, name, day, media_type


def parse_target_listing_evidence(
    raw_bytes: bytes,
    *,
    exchange: str,
    target_code: str,
    legal_name: str,
    listing_date: str,
    source_url: str,
    retrieved_at: str,
    content_type: str,
    store: BSEEventLedgerStore,
    expected_sha256: str | None = None,
) -> TargetListingEvidence:
    digest = _verify_hash(raw_bytes, expected_sha256, "target listing document")
    normalized_exchange, code, suffix, name, day, media_type = (
        _validate_target_document_facts(
            raw_bytes,
            exchange=exchange,
            target_code=target_code,
            legal_name=legal_name,
            listing_date=listing_date,
            source_url=source_url,
            content_type=content_type,
            expected_sha256=digest,
        )
    )
    blob = store.capture_blob(
        raw_bytes,
        source_url=source_url,
        retrieved_at=retrieved_at,
        content_type=media_type,
        expected_sha256=digest,
    )
    return TargetListingEvidence(
        exchange=normalized_exchange,
        code_alias=f"{code}.{suffix}",
        legal_name=name,
        listing_date=day,
        source_url=source_url,
        method=blob.method,
        retrieved_at=blob.retrieved_at,
        content_sha256=blob.content_sha256,
        byte_count=blob.byte_count,
        content_type=blob.content_type,
        cas_uri=blob.cas_uri,
        object_path=blob.object_path,
    )


def _verify_target_listing_evidence(evidence: TargetListingEvidence) -> None:
    if evidence.method != "GET":
        raise BSETerminationEventBlockedError(
            "target listing evidence is not GET-only"
        )
    _retrieved_at(evidence.retrieved_at)
    if evidence.cas_uri != f"sha256:{evidence.content_sha256}":
        raise BSETerminationEventBlockedError("target listing evidence CAS URI mismatch")
    content = _read_cas_object(
        object_path=evidence.object_path,
        expected_sha256=evidence.content_sha256,
        expected_byte_count=evidence.byte_count,
        label="target listing evidence",
        maximum=MAX_TARGET_DOCUMENT_BYTES,
    )
    normalized_exchange, code, suffix, name, day, media_type = (
        _validate_target_document_facts(
            content,
            exchange=evidence.exchange,
            target_code=evidence.code_alias,
            legal_name=evidence.legal_name,
            listing_date=evidence.listing_date,
            source_url=evidence.source_url,
            content_type=evidence.content_type,
            expected_sha256=evidence.content_sha256,
        )
    )
    if (
        evidence.exchange != normalized_exchange
        or evidence.code_alias != f"{code}.{suffix}"
        or evidence.legal_name != name
        or evidence.listing_date != day
        or evidence.content_type != media_type
    ):
        raise BSETerminationEventBlockedError(
            "target listing evidence metadata is not canonical"
        )


class TargetListingEvidenceClient:
    """GET-only collector for one explicit SSE/SZSE official listing page."""

    def __init__(
        self,
        *,
        store: BSEEventLedgerStore,
        session: requests.Session | None = None,
        source_contract_status: str = SOURCE_CONTRACT_UNADMITTED,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.session = session or requests.Session()
        self.source_contract_status = source_contract_status
        self.timeout_seconds = float(timeout_seconds)

    def fetch(
        self,
        *,
        exchange: str,
        target_code: str,
        legal_name: str,
        listing_date: str,
        source_url: str,
        retrieved_at: str | None = None,
        expected_sha256: str | None = None,
    ) -> TargetListingEvidence:
        if self.source_contract_status != SOURCE_CONTRACT_ADMITTED:
            raise BSETerminationEventBlockedError(
                "target-listing source contract has not been explicitly admitted",
                status=SOURCE_CONTRACT_UNADMITTED,
            )
        normalized_exchange = str(exchange or "").upper()
        _target_origin(source_url, normalized_exchange)
        response = self.session.get(
            source_url,
            headers={
                "Accept": "text/html",
                "Referer": (
                    "https://www.sse.com.cn/"
                    if normalized_exchange == "SSE"
                    else "https://www.szse.cn/"
                ),
                "User-Agent": USER_AGENT,
            },
            timeout=self.timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise BSETerminationEventBlockedError(
                f"target listing HTTP status is {response.status_code}"
            )
        if str(response.url) != source_url:
            raise BSETerminationEventBlockedError(
                "target listing response URL changed"
            )
        raw = response.content
        if not isinstance(raw, bytes):
            raw = bytes(raw)
        return parse_target_listing_evidence(
            raw,
            exchange=normalized_exchange,
            target_code=target_code,
            legal_name=legal_name,
            listing_date=listing_date,
            source_url=source_url,
            retrieved_at=_retrieved_at(retrieved_at),
            content_type=response.headers.get("Content-Type", ""),
            store=self.store,
            expected_sha256=expected_sha256,
        )


def _extract_pdf_text(raw_pdf: bytes) -> tuple[str, str, str, int]:
    if (
        not raw_pdf
        or len(raw_pdf) > MAX_NOTICE_PDF_BYTES
        or not raw_pdf.startswith(b"%PDF-")
        or b"%%EOF" not in raw_pdf[-4096:]
    ):
        raise BSETerminationEventBlockedError(
            "BSE termination notice is not a strict PDF payload"
        )
    try:
        import pypdf
        from pypdf import PdfReader
    except ImportError as exc:
        raise BSETerminationEventBlockedError(
            "pypdf is unavailable; BSE termination notice cannot be recomputed",
            status=SOURCE_CONTRACT_UNADMITTED,
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw_pdf), strict=True)
        if reader.is_encrypted:
            raise BSETerminationEventBlockedError(
                "encrypted BSE termination notice is not admitted"
            )
        page_count = len(reader.pages)
        if page_count <= 0 or page_count > MAX_NOTICE_PDF_PAGES:
            raise BSETerminationEventBlockedError(
                "BSE termination notice page count is invalid"
            )
        pages: list[str] = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text is None:
                raise BSETerminationEventBlockedError(
                    "BSE termination notice has no reproducible text layer"
                )
            pages.append(page_text)
    except BSETerminationEventBlockedError:
        raise
    except Exception as exc:
        raise BSETerminationEventBlockedError(
            "BSE termination notice text extraction failed closed"
        ) from exc
    text = "\n\f\n".join(pages)
    if not text.strip() or len(text) > MAX_NOTICE_TEXT_CHARS:
        raise BSETerminationEventBlockedError(
            "BSE termination notice text is empty or oversized"
        )
    return (
        text,
        "pypdf",
        str(getattr(pypdf, "__version__", "UNKNOWN")),
        page_count,
    )


def _termination_effective_date_from_text(
    text: str,
    *,
    code: str,
    legal_name: str,
    notice_date: str,
) -> str:
    compact = _normalized_text(text)
    if _normalized_text(legal_name) not in compact:
        raise BSETerminationEventBlockedError(
            "BSE termination notice does not contain the same legal company identity"
        )
    if not re.search(rf"(?<!\d){re.escape(code)}(?!\d)", compact):
        raise BSETerminationEventBlockedError(
            "BSE termination notice does not contain its source code"
        )
    candidates = re.findall(
        r"\u81ea(20\d{2})\u5e74(\d{1,2})\u6708(\d{1,2})\u65e5\u8d77\u7ec8\u6b62\u5176\u80a1\u7968\u4e0a\u5e02",
        compact,
    )
    if len(candidates) != 1:
        raise BSETerminationEventBlockedError(
            "BSE termination effective date is missing or ambiguous"
        )
    year, month, day = (int(item) for item in candidates[0])
    try:
        effective = date(year, month, day).isoformat()
    except ValueError as exc:
        raise BSETerminationEventBlockedError(
            "BSE termination effective date is invalid"
        ) from exc
    if effective <= notice_date:
        raise BSETerminationEventBlockedError(
            "BSE termination effective date must follow the notice date"
        )
    return effective


def parse_bse_termination_notice_evidence(
    raw_pdf: bytes,
    *,
    record: BSETerminationRecord,
    retrieved_at: str,
    content_type: str,
    store: BSEEventLedgerStore,
    expected_sha256: str | None = None,
) -> BSETerminationNoticeEvidence:
    if record.notice_url is None:
        raise BSETerminationEventBlockedError(
            "BSE termination record has no notice URL"
        )
    parsed = urlsplit(record.notice_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != BSE_TERMINATION_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not parsed.path.lower().endswith(".pdf")
        or not (
            parsed.path.startswith("/disclosure/")
            or parsed.path.startswith("/uploads/")
        )
    ):
        raise BSETerminationEventBlockedError(
            "BSE termination notice origin changed"
        )
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "application/pdf":
        raise BSETerminationEventBlockedError(
            "BSE termination notice content type is not application/pdf"
        )
    digest = _verify_hash(raw_pdf, expected_sha256, "BSE termination notice")
    text, engine, engine_version, page_count = _extract_pdf_text(raw_pdf)
    code = record.code_alias.split(".", 1)[0]
    effective = _termination_effective_date_from_text(
        text,
        code=code,
        legal_name=record.legal_name,
        notice_date=record.notice_date,
    )
    blob = store.capture_blob(
        raw_pdf,
        source_url=record.notice_url,
        retrieved_at=retrieved_at,
        content_type=media_type,
        expected_sha256=digest,
    )
    return BSETerminationNoticeEvidence(
        code_alias=record.code_alias,
        legal_name=record.legal_name,
        notice_date=record.notice_date,
        termination_effective_date=effective,
        source_url=record.notice_url,
        method="GET",
        retrieved_at=blob.retrieved_at,
        content_sha256=blob.content_sha256,
        byte_count=blob.byte_count,
        content_type=blob.content_type,
        cas_uri=blob.cas_uri,
        object_path=blob.object_path,
        extraction_engine=engine,
        extraction_engine_version=engine_version,
        page_count=page_count,
        text_sha256=_sha256(text.encode("utf-8")),
    )


def _verify_termination_notice_evidence(
    evidence: BSETerminationNoticeEvidence,
    *,
    record: BSETerminationRecord,
) -> None:
    if evidence.method != "GET":
        raise BSETerminationEventBlockedError(
            "BSE termination notice evidence is not GET-only"
        )
    _retrieved_at(evidence.retrieved_at)
    if evidence.cas_uri != f"sha256:{evidence.content_sha256}":
        raise BSETerminationEventBlockedError(
            "BSE termination notice CAS URI mismatch"
        )
    raw_pdf = _read_cas_object(
        object_path=evidence.object_path,
        expected_sha256=evidence.content_sha256,
        expected_byte_count=evidence.byte_count,
        label="BSE termination notice",
        maximum=MAX_NOTICE_PDF_BYTES,
    )
    if evidence.content_type != "application/pdf":
        raise BSETerminationEventBlockedError(
            "BSE termination notice content type metadata drift"
        )
    text, engine, engine_version, page_count = _extract_pdf_text(raw_pdf)
    effective = _termination_effective_date_from_text(
        text,
        code=record.code_alias.split(".", 1)[0],
        legal_name=record.legal_name,
        notice_date=record.notice_date,
    )
    if (
        evidence.code_alias != record.code_alias
        or evidence.legal_name != record.legal_name
        or evidence.notice_date != record.notice_date
        or evidence.termination_effective_date != effective
        or evidence.source_url != record.notice_url
        or evidence.extraction_engine != engine
        or evidence.extraction_engine_version != engine_version
        or evidence.page_count != page_count
        or evidence.text_sha256 != _sha256(text.encode("utf-8"))
    ):
        raise BSETerminationEventBlockedError(
            "BSE termination notice evidence metadata mismatch"
        )


class BSETerminationNoticeEvidenceClient:
    """GET-only collector for a termination notice already selected by the list."""

    def __init__(
        self,
        *,
        store: BSEEventLedgerStore,
        session: requests.Session | None = None,
        source_contract_status: str = SOURCE_CONTRACT_UNADMITTED,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.session = session or requests.Session()
        self.source_contract_status = source_contract_status
        self.timeout_seconds = float(timeout_seconds)

    def fetch(
        self,
        *,
        record: BSETerminationRecord,
        retrieved_at: str | None = None,
        expected_sha256: str | None = None,
    ) -> BSETerminationNoticeEvidence:
        if self.source_contract_status != SOURCE_CONTRACT_ADMITTED:
            raise BSETerminationEventBlockedError(
                "BSE termination-notice source contract has not been admitted",
                status=SOURCE_CONTRACT_UNADMITTED,
            )
        response = self.session.get(
            record.notice_url,
            headers={
                "Accept": "application/pdf",
                "Referer": "https://www.bse.cn/",
                "User-Agent": USER_AGENT,
            },
            timeout=self.timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise BSETerminationEventBlockedError(
                f"BSE termination-notice HTTP status is {response.status_code}"
            )
        if str(response.url) != record.notice_url:
            raise BSETerminationEventBlockedError(
                "BSE termination-notice response URL changed"
            )
        raw = response.content
        if not isinstance(raw, bytes):
            raw = bytes(raw)
        return parse_bse_termination_notice_evidence(
            raw,
            record=record,
            retrieved_at=_retrieved_at(retrieved_at),
            content_type=response.headers.get("Content-Type", ""),
            store=self.store,
            expected_sha256=expected_sha256,
        )


def classify_bse_terminations(
    records: Sequence[BSETerminationRecord],
    termination_notice_evidence: Sequence[BSETerminationNoticeEvidence],
    target_evidence: Sequence[TargetListingEvidence],
) -> tuple[BSETerminationEvent, ...]:
    by_source_code: dict[str, BSETerminationNoticeEvidence] = {}
    for evidence in termination_notice_evidence:
        if evidence.code_alias in by_source_code:
            raise BSETerminationEventBlockedError(
                "duplicate BSE termination notice evidence"
            )
        by_source_code[evidence.code_alias] = evidence
    by_identity: dict[str, TargetListingEvidence] = {}
    target_codes: set[str] = set()
    for evidence in target_evidence:
        _verify_target_listing_evidence(evidence)
        identity = _normalized_text(evidence.legal_name)
        if identity in by_identity:
            raise BSETerminationEventBlockedError(
                "ambiguous target listing evidence for one legal entity"
            )
        if evidence.code_alias in target_codes:
            raise BSETerminationEventBlockedError(
                "duplicate target code in listing evidence"
            )
        by_identity[identity] = evidence
        target_codes.add(evidence.code_alias)
    events: list[BSETerminationEvent] = []
    source_codes: set[str] = set()
    for record in records:
        if record.code_alias in source_codes:
            raise BSETerminationEventBlockedError(
                "duplicate BSE source code in termination ledger"
            )
        source_codes.add(record.code_alias)
        termination_evidence = by_source_code.get(record.code_alias)
        if termination_evidence is not None:
            _verify_termination_notice_evidence(
                termination_evidence,
                record=record,
            )
        identity = _normalized_text(record.legal_name)
        evidence = by_identity.get(identity)
        entity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        common = {
            "canonical_entity_id": f"CN:LEGAL_ENTITY:SHA256:{entity_hash}",
            "source_exchange": "BSE",
            "source_code_alias": record.code_alias,
            "legal_name": record.legal_name,
            "termination_notice_date": record.notice_date,
            "termination_notice_url": record.notice_url,
            "termination_effective_date": (
                termination_evidence.termination_effective_date
                if termination_evidence
                else None
            ),
            "termination_evidence_url": (
                termination_evidence.source_url if termination_evidence else None
            ),
            "termination_evidence_sha256": (
                termination_evidence.content_sha256
                if termination_evidence
                else None
            ),
        }
        if evidence is None:
            events.append(
                BSETerminationEvent(
                    **common,
                    classification="TERMINATION_UNCLASSIFIED",
                    target_exchange=None,
                    target_code_alias=None,
                    target_listing_date=None,
                    target_evidence_url=None,
                    target_evidence_sha256=None,
                )
            )
            continue
        if evidence.listing_date < record.notice_date:
            raise BSETerminationEventBlockedError(
                "target listing date precedes the BSE termination notice"
            )
        if (
            termination_evidence is not None
            and evidence.listing_date
            < termination_evidence.termination_effective_date
        ):
            raise BSETerminationEventBlockedError(
                "target listing date precedes the BSE termination effective date"
            )
        events.append(
            BSETerminationEvent(
                **common,
                classification="TRANSFER",
                target_exchange=evidence.exchange,
                target_code_alias=evidence.code_alias,
                target_listing_date=evidence.listing_date,
                target_evidence_url=evidence.source_url,
                target_evidence_sha256=evidence.content_sha256,
            )
        )
    unused = sorted(
        set(by_identity) - {_normalized_text(record.legal_name) for record in records}
    )
    if unused:
        raise BSETerminationEventBlockedError(
            "target listing evidence is not bound to a BSE termination identity"
        )
    unused_termination = sorted(set(by_source_code) - source_codes)
    if unused_termination:
        raise BSETerminationEventBlockedError(
            "BSE termination notice evidence is not bound to a list record"
        )
    return tuple(events)


def _recompute_records_from_raw_pages(
    raw_pages: Sequence[RawPageEvidence],
    *,
    start_date: str,
    end_date: str,
) -> tuple[BSETerminationRecord, ...]:
    if not raw_pages:
        raise BSETerminationEventBlockedError(
            "BSE ledger has no raw page evidence"
        )
    records: list[BSETerminationRecord] = []
    total_pages: int | None = None
    total_elements: int | None = None
    for expected_page, evidence in enumerate(raw_pages):
        if evidence.method != "GET":
            raise BSETerminationEventBlockedError(
                "BSE raw page evidence is not GET-only"
            )
        _retrieved_at(evidence.retrieved_at)
        if evidence.cas_uri != f"sha256:{evidence.content_sha256}":
            raise BSETerminationEventBlockedError("BSE raw page CAS URI mismatch")
        request = dict(evidence.request)
        if set(request) != {
            "page",
            "start_date",
            "end_date",
            "disclosure_type",
            "keyword",
            "market_layer",
            "site_id",
            "required_fields",
        }:
            raise BSETerminationEventBlockedError("BSE raw page request schema drift")
        if (
            request["page"] != expected_page
            or isinstance(request["page"], bool)
            or request["start_date"] != start_date
            or request["end_date"] != end_date
            or request["disclosure_type"] != BSE_DISCLOSURE_TYPE
            or request["keyword"] != BSE_KEYWORD
            or request["market_layer"] != BSE_MARKET_LAYER
            or request["site_id"] != BSE_SITE_ID
            or request["required_fields"] != list(BSE_REQUIRED_FIELDS)
        ):
            raise BSETerminationEventBlockedError(
                "BSE raw page request metadata mismatch"
            )
        expected_url = _validate_request_url(
            evidence.source_url,
            start_date=start_date,
            end_date=end_date,
            page=expected_page,
        )
        if evidence.source_url != expected_url:
            raise BSETerminationEventBlockedError(
                "BSE raw page request URL is not canonical"
            )
        media_type = _content_type(evidence.content_type, list_response=True)
        if evidence.content_type != media_type:
            raise BSETerminationEventBlockedError(
                "BSE raw page content type is not canonical"
            )
        raw = _read_cas_object(
            object_path=evidence.object_path,
            expected_sha256=evidence.content_sha256,
            expected_byte_count=evidence.byte_count,
            label="BSE raw page",
            maximum=MAX_RESPONSE_BYTES,
        )
        parsed = parse_bse_termination_page(
            raw,
            start_date=start_date,
            end_date=end_date,
            request_page=expected_page,
            source_url=evidence.source_url,
            expected_sha256=evidence.content_sha256,
        )
        response = dict(evidence.response)
        expected_response = {
            "number": parsed.number,
            "number_of_elements": len(parsed.records),
            "size": parsed.size,
            "total_elements": parsed.total_elements,
            "total_pages": parsed.total_pages,
            "first_page": parsed.first_page,
            "last_page": parsed.last_page,
        }
        if response != expected_response:
            raise BSETerminationEventBlockedError(
                "BSE raw page response metadata mismatch"
            )
        if total_pages is None:
            total_pages = parsed.total_pages
            total_elements = parsed.total_elements
        elif (
            parsed.total_pages != total_pages
            or parsed.total_elements != total_elements
        ):
            raise BSETerminationEventBlockedError(
                "BSE raw page pagination totals are inconsistent"
            )
        records.extend(parsed.records)
    expected_page_count = total_pages if total_pages else 1
    if len(raw_pages) != expected_page_count:
        raise BSETerminationEventBlockedError(
            "BSE raw page evidence does not close pagination"
        )
    if len(records) != total_elements:
        raise BSETerminationEventBlockedError(
            "BSE raw page evidence total count mismatch"
        )
    return tuple(records)


def _build_artifact(
    *,
    start_date: str,
    end_date: str,
    records: Sequence[BSETerminationRecord],
    raw_pages: Sequence[RawPageEvidence],
    termination_notice_evidence: Sequence[BSETerminationNoticeEvidence],
    target_evidence: Sequence[TargetListingEvidence],
) -> BSETerminationLedgerArtifact:
    recomputed_records = _recompute_records_from_raw_pages(
        raw_pages,
        start_date=start_date,
        end_date=end_date,
    )
    if [item.to_dict() for item in records] != [
        item.to_dict() for item in recomputed_records
    ]:
        raise BSETerminationEventBlockedError(
            "derived BSE termination records do not match raw CAS pages"
        )
    normalized_target_evidence = tuple(
        sorted(
            target_evidence,
            key=lambda item: (item.exchange, item.code_alias, item.source_url),
        )
    )
    normalized_termination_evidence = tuple(
        sorted(
            termination_notice_evidence,
            key=lambda item: item.code_alias,
        )
    )
    events = classify_bse_terminations(
        records,
        normalized_termination_evidence,
        normalized_target_evidence,
    )
    transfers = [item for item in events if item.classification == "TRANSFER"]
    unclassified = [
        item for item in events if item.classification == "TERMINATION_UNCLASSIFIED"
    ]
    missing_effective_dates = sorted(
        item.source_code_alias
        for item in events
        if item.termination_effective_date is None
    )
    complete = not unclassified and not missing_effective_dates
    if unclassified:
        completeness_status = TERMINATION_CLASSIFICATION_INCOMPLETE
    elif missing_effective_dates:
        completeness_status = TERMINATION_EFFECTIVE_DATE_INCOMPLETE
    else:
        completeness_status = SOURCE_COMPLETE
    completeness = {
        "ready": complete,
        "status": completeness_status,
        "promotion_blocked": not complete,
        "scope": {
            "start_date": start_date,
            "end_date": end_date,
            "disclosure_type": BSE_DISCLOSURE_TYPE,
            "keyword": BSE_KEYWORD,
            "market_layer": BSE_MARKET_LAYER,
        },
        "source_pagination_complete": True,
        "termination_count": len(events),
        "transfer_count": len(transfers),
        "delist_count": 0,
        "unclassified_count": len(unclassified),
        "unclassified_codes": sorted(item.source_code_alias for item in unclassified),
        "missing_effective_date_count": len(missing_effective_dates),
        "missing_effective_date_codes": missing_effective_dates,
        "classification_rule": (
            "BSE termination notices never imply DELIST or TRANSFER; TRANSFER requires "
            "a target-exchange official listing page with matching legal identity, "
            "target code, and listing date"
        ),
    }
    query = {
        "endpoint": BSE_TERMINATION_ENDPOINT,
        "method": "GET",
        "start_date": start_date,
        "end_date": end_date,
        "disclosure_type": BSE_DISCLOSURE_TYPE,
        "keyword": BSE_KEYWORD,
        "market_layer": BSE_MARKET_LAYER,
        "site_id": BSE_SITE_ID,
        "page_size": BSE_PAGE_SIZE,
        "required_fields": list(BSE_REQUIRED_FIELDS),
    }
    logical = {
        "query": query,
        "records": [item.to_dict() for item in records],
        "events": [item.to_dict() for item in events],
        "raw_page_hashes": [item.content_sha256 for item in raw_pages],
        "termination_notice_hashes": [
            item.content_sha256 for item in normalized_termination_evidence
        ],
        "target_evidence_hashes": [
            item.content_sha256 for item in normalized_target_evidence
        ],
        "completeness": completeness,
    }
    return BSETerminationLedgerArtifact(
        query=query,
        records=tuple(records),
        events=events,
        raw_pages=tuple(raw_pages),
        termination_notice_evidence=normalized_termination_evidence,
        target_evidence=normalized_target_evidence,
        completeness=completeness,
        logical_content_sha256=_sha256(_canonical_json_bytes(logical)),
    )


def _manifest_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BSETerminationEventBlockedError(
            f"invalid ledger manifest {label}"
        )
    return value


def _raw_page_from_manifest(
    value: Any,
    *,
    store: BSEEventLedgerStore,
) -> RawPageEvidence:
    expected_fields = {
        "source_url",
        "method",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "cas_uri",
        "object_path",
        "request",
        "response",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise BSETerminationEventBlockedError(
            "ledger manifest raw-page schema drift"
        )
    for key in expected_fields - {"byte_count", "request", "response"}:
        _manifest_string(value[key], f"raw page {key}")
    byte_count = _strict_int(value["byte_count"], "manifest raw page byte count", minimum=1)
    if not isinstance(value["request"], dict) or not isinstance(value["response"], dict):
        raise BSETerminationEventBlockedError(
            "ledger manifest raw-page metadata is invalid"
        )
    evidence = RawPageEvidence(
        source_url=value["source_url"],
        method=value["method"],
        retrieved_at=value["retrieved_at"],
        content_sha256=value["content_sha256"],
        byte_count=byte_count,
        content_type=value["content_type"],
        cas_uri=value["cas_uri"],
        object_path=value["object_path"],
        request=value["request"],
        response=value["response"],
    )
    store._assert_object_path(
        content_sha256=evidence.content_sha256,
        object_path=evidence.object_path,
    )
    return evidence


def _target_evidence_from_manifest(
    value: Any,
    *,
    store: BSEEventLedgerStore,
) -> TargetListingEvidence:
    expected_fields = {
        "exchange",
        "code_alias",
        "legal_name",
        "listing_date",
        "source_url",
        "method",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "cas_uri",
        "object_path",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise BSETerminationEventBlockedError(
            "ledger manifest target-evidence schema drift"
        )
    for key in expected_fields - {"byte_count"}:
        _manifest_string(value[key], f"target evidence {key}")
    byte_count = _strict_int(
        value["byte_count"], "manifest target evidence byte count", minimum=1
    )
    evidence = TargetListingEvidence(
        exchange=value["exchange"],
        code_alias=value["code_alias"],
        legal_name=value["legal_name"],
        listing_date=value["listing_date"],
        source_url=value["source_url"],
        method=value["method"],
        retrieved_at=value["retrieved_at"],
        content_sha256=value["content_sha256"],
        byte_count=byte_count,
        content_type=value["content_type"],
        cas_uri=value["cas_uri"],
        object_path=value["object_path"],
    )
    store._assert_object_path(
        content_sha256=evidence.content_sha256,
        object_path=evidence.object_path,
    )
    return evidence


def _termination_evidence_from_manifest(
    value: Any,
    *,
    store: BSEEventLedgerStore,
) -> BSETerminationNoticeEvidence:
    expected_fields = {
        "code_alias",
        "legal_name",
        "notice_date",
        "termination_effective_date",
        "source_url",
        "method",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "cas_uri",
        "object_path",
        "extraction_engine",
        "extraction_engine_version",
        "page_count",
        "text_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise BSETerminationEventBlockedError(
            "ledger manifest termination-evidence schema drift"
        )
    for key in expected_fields - {"byte_count", "page_count"}:
        _manifest_string(value[key], f"termination evidence {key}")
    byte_count = _strict_int(
        value["byte_count"], "manifest termination evidence byte count", minimum=1
    )
    page_count = _strict_int(
        value["page_count"], "manifest termination evidence page count", minimum=1
    )
    evidence = BSETerminationNoticeEvidence(
        code_alias=value["code_alias"],
        legal_name=value["legal_name"],
        notice_date=value["notice_date"],
        termination_effective_date=value["termination_effective_date"],
        source_url=value["source_url"],
        method=value["method"],
        retrieved_at=value["retrieved_at"],
        content_sha256=value["content_sha256"],
        byte_count=byte_count,
        content_type=value["content_type"],
        cas_uri=value["cas_uri"],
        object_path=value["object_path"],
        extraction_engine=value["extraction_engine"],
        extraction_engine_version=value["extraction_engine_version"],
        page_count=page_count,
        text_sha256=value["text_sha256"],
    )
    store._assert_object_path(
        content_sha256=evidence.content_sha256,
        object_path=evidence.object_path,
    )
    return evidence


def _rebuild_artifact_from_manifest_value(
    value: Any,
    *,
    store: BSEEventLedgerStore,
) -> BSETerminationLedgerArtifact:
    expected_fields = {
        "protocol_version",
        "query",
        "records",
        "events",
        "raw_pages",
        "termination_notice_evidence",
        "target_evidence",
        "completeness",
        "logical_content_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise BSETerminationEventBlockedError("ledger manifest schema drift")
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise BSETerminationEventBlockedError("ledger manifest protocol drift")
    query = value["query"]
    query_fields = {
        "endpoint",
        "method",
        "start_date",
        "end_date",
        "disclosure_type",
        "keyword",
        "market_layer",
        "site_id",
        "page_size",
        "required_fields",
    }
    if not isinstance(query, dict) or set(query) != query_fields:
        raise BSETerminationEventBlockedError("ledger manifest query schema drift")
    start_date = _iso_date(query["start_date"], "manifest query start date")
    end_date = _iso_date(query["end_date"], "manifest query end date")
    if start_date > end_date:
        raise BSETerminationEventBlockedError(
            "ledger manifest query date range is reversed"
        )
    if (
        query["endpoint"] != BSE_TERMINATION_ENDPOINT
        or query["method"] != "GET"
        or query["disclosure_type"] != BSE_DISCLOSURE_TYPE
        or query["keyword"] != BSE_KEYWORD
        or query["market_layer"] != BSE_MARKET_LAYER
        or query["site_id"] != BSE_SITE_ID
        or query["page_size"] != BSE_PAGE_SIZE
        or isinstance(query["page_size"], bool)
        or query["required_fields"] != list(BSE_REQUIRED_FIELDS)
    ):
        raise BSETerminationEventBlockedError(
            "ledger manifest query contract mismatch"
        )
    raw_values = value["raw_pages"]
    termination_values = value["termination_notice_evidence"]
    target_values = value["target_evidence"]
    if (
        not isinstance(raw_values, list)
        or not isinstance(termination_values, list)
        or not isinstance(target_values, list)
    ):
        raise BSETerminationEventBlockedError(
            "ledger manifest evidence collections are invalid"
        )
    raw_pages = tuple(
        _raw_page_from_manifest(item, store=store) for item in raw_values
    )
    termination_notice_evidence = tuple(
        _termination_evidence_from_manifest(item, store=store)
        for item in termination_values
    )
    target_evidence = tuple(
        _target_evidence_from_manifest(item, store=store)
        for item in target_values
    )
    records = _recompute_records_from_raw_pages(
        raw_pages,
        start_date=start_date,
        end_date=end_date,
    )
    return _build_artifact(
        start_date=start_date,
        end_date=end_date,
        records=records,
        raw_pages=raw_pages,
        termination_notice_evidence=termination_notice_evidence,
        target_evidence=target_evidence,
    )


class BSETerminationEventClient:
    """GET-only strict BSE termination ledger collector with explicit admission."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        store: BSEEventLedgerStore,
        source_contract_status: str = SOURCE_CONTRACT_UNADMITTED,
        endpoint: str = BSE_TERMINATION_ENDPOINT,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.session = session or requests.Session()
        self.store = store
        self.source_contract_status = source_contract_status
        self.endpoint = endpoint
        self.timeout_seconds = float(timeout_seconds)

    def fetch(
        self,
        *,
        start_date: str,
        end_date: str,
        retrieved_at: str | None = None,
        termination_notice_evidence: Sequence[
            BSETerminationNoticeEvidence
        ] = (),
        target_evidence: Sequence[TargetListingEvidence] = (),
        expected_page_hashes: Mapping[int, str] | None = None,
    ) -> BSETerminationLedgerArtifact:
        if self.source_contract_status != SOURCE_CONTRACT_ADMITTED:
            raise BSETerminationEventBlockedError(
                "BSE termination-list source contract has not been explicitly admitted",
                status=SOURCE_CONTRACT_UNADMITTED,
            )
        _validate_list_endpoint(self.endpoint)
        start = _iso_date(start_date, "start date")
        end = _iso_date(end_date, "end date")
        if start > end:
            raise BSETerminationEventBlockedError("BSE query date range is reversed")
        observed_at = _retrieved_at(retrieved_at)
        expected_hashes = dict(expected_page_hashes or {})
        pages: list[BSETerminationPage] = []
        evidences: list[RawPageEvidence] = []
        page_number = 0
        expected_total_pages: int | None = None
        expected_total_elements: int | None = None
        while True:
            request_url = build_bse_termination_request_url(
                start_date=start,
                end_date=end,
                page=page_number,
                endpoint=self.endpoint,
            )
            response = self.session.get(
                request_url,
                headers={
                    "Accept": "text/html",
                    "Referer": "https://www.bse.cn/disclosure/vocational.html",
                    "User-Agent": USER_AGENT,
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise BSETerminationEventBlockedError(
                    f"BSE termination-list HTTP status is {response.status_code}"
                )
            if str(response.url) != request_url:
                raise BSETerminationEventBlockedError(
                    "BSE termination-list response URL changed"
                )
            media_type = _content_type(
                response.headers.get("Content-Type"), list_response=True
            )
            raw = response.content
            if not isinstance(raw, bytes):
                raw = bytes(raw)
            expected_hash = expected_hashes.get(page_number)
            parsed = parse_bse_termination_page(
                raw,
                start_date=start,
                end_date=end,
                request_page=page_number,
                source_url=request_url,
                expected_sha256=expected_hash,
            )
            if expected_total_pages is None:
                expected_total_pages = parsed.total_pages
                expected_total_elements = parsed.total_elements
            elif (
                parsed.total_pages != expected_total_pages
                or parsed.total_elements != expected_total_elements
            ):
                raise BSETerminationEventBlockedError(
                    "BSE pagination totals changed between pages"
                )
            blob = self.store.capture_blob(
                raw,
                source_url=request_url,
                retrieved_at=observed_at,
                content_type=media_type,
                expected_sha256=parsed.content_sha256,
            )
            evidences.append(
                RawPageEvidence(
                    **blob.to_dict(),
                    request={
                        "page": page_number,
                        "start_date": start,
                        "end_date": end,
                        "disclosure_type": BSE_DISCLOSURE_TYPE,
                        "keyword": BSE_KEYWORD,
                        "market_layer": BSE_MARKET_LAYER,
                        "site_id": BSE_SITE_ID,
                        "required_fields": list(BSE_REQUIRED_FIELDS),
                    },
                    response={
                        "number": parsed.number,
                        "number_of_elements": len(parsed.records),
                        "size": parsed.size,
                        "total_elements": parsed.total_elements,
                        "total_pages": parsed.total_pages,
                        "first_page": parsed.first_page,
                        "last_page": parsed.last_page,
                    },
                )
            )
            pages.append(parsed)
            if parsed.total_pages == 0 or parsed.last_page:
                break
            page_number += 1
            if page_number >= MAX_PAGES:
                raise BSETerminationEventBlockedError(
                    "BSE pagination exceeds admitted maximum"
                )
        if expected_page_hashes is not None and set(expected_hashes) != {
            item.number for item in pages
        }:
            raise BSETerminationEventBlockedError(
                "expected BSE page hashes do not cover exactly the fetched pages"
            )
        records = tuple(record for page in pages for record in page.records)
        if expected_total_elements != len(records):
            raise BSETerminationEventBlockedError(
                "BSE full-pagination element count mismatch"
            )
        identities = [
            (item.code_alias, item.notice_date, item.notice_url) for item in records
        ]
        if len(set(identities)) != len(identities):
            raise BSETerminationEventBlockedError(
                "duplicate BSE termination rows across pages"
            )
        dates = [item.notice_date for item in records]
        if dates != sorted(dates, reverse=True):
            raise BSETerminationEventBlockedError(
                "BSE full ledger is not date-descending"
            )
        artifact = _build_artifact(
            start_date=start,
            end_date=end,
            records=records,
            raw_pages=evidences,
            termination_notice_evidence=termination_notice_evidence,
            target_evidence=target_evidence,
        )
        manifest = self.store.publish(artifact)
        return replace(artifact, manifest=manifest)


__all__ = [
    "BSEEventLedgerStore",
    "BSETerminationNoticeEvidence",
    "BSETerminationNoticeEvidenceClient",
    "BSETerminationEvent",
    "BSETerminationEventBlockedError",
    "BSETerminationEventClient",
    "BSETerminationLedgerArtifact",
    "BSETerminationPage",
    "BSETerminationRecord",
    "BSE_KEYWORD",
    "BSE_PAGE_SIZE",
    "BSE_REQUIRED_FIELDS",
    "BSE_TERMINATION_ENDPOINT",
    "ManifestEvidence",
    "PROTOCOL_VERSION",
    "RawPageEvidence",
    "SOURCE_COMPLETE",
    "SOURCE_CONTRACT_ADMITTED",
    "SOURCE_CONTRACT_UNADMITTED",
    "SourceBlobEvidence",
    "TERMINATION_CLASSIFICATION_INCOMPLETE",
    "TERMINATION_EFFECTIVE_DATE_INCOMPLETE",
    "TargetListingEvidence",
    "TargetListingEvidenceClient",
    "build_bse_termination_request_url",
    "classify_bse_terminations",
    "parse_bse_termination_page",
    "parse_bse_termination_notice_evidence",
    "parse_target_listing_evidence",
]
