from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlsplit

import requests


PROTOCOL_VERSION = "bse-current-delisting-official-evidence-v1"
SOURCE_CONTRACT_ADMITTED = "SOURCE_CONTRACT_ADMITTED"
SOURCE_REJECTED = "SOURCE_REJECTED"
SOURCE_STALE = "SOURCE_STALE"
EVIDENCE_COMPLETE = "BSE_CURRENT_DELISTING_EVIDENCE_COMPLETE"

BSE_HOST = "www.bse.cn"
BSE_CURRENT_CATALOGUE_PAGE_URL = "https://www.bse.cn/nq/listedcompany.html"
BSE_CURRENT_CATALOGUE_URL = "https://www.bse.cn/nqxxController/nqxxCnzq.do"
BSE_CURRENT_CATALOGUE_REFERER = BSE_CURRENT_CATALOGUE_PAGE_URL
BSE_CATALOGUE_PAGE_SIZE = 20
BSE_CATALOGUE_MINIMUM_ROWS = 300
BSE_CATALOGUE_MAXIMUM_PAGES = 1_000

MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_CHALLENGE_BYTES = 2 * 1024 * 1024
MAX_CATALOGUE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_CAS_OBJECT_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_PDF_TEXT_CHARS = 1_000_000
MAX_CURRENT_EVIDENCE_AGE = timedelta(minutes=15)
MAX_CAPTURE_SPAN = timedelta(minutes=10)
MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=5)

USER_AGENT = "tdx-research-platform/bse-current-delisting-v1"
PDF_MEDIA_TYPE = "application/pdf"
CATALOGUE_MEDIA_TYPE = "text/html;charset=utf-8"

CATALOGUE_PAGE_FIELDS = frozenset(
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

CATALOGUE_ROW_FIELDS = frozenset(
    {
        "fxssrq",
        "xxbldw",
        "xxbnsy",
        "xxcfgbz",
        "xxcqcx",
        "xxcyhbjq",
        "xxdqr",
        "xxdtjg",
        "xxdzdtjg",
        "xxdzztjg",
        "xxfcbj",
        "xxfxsgb",
        "xxghfl",
        "xxgprq",
        "xxgxsj",
        "xxhbzl",
        "xxhxcs",
        "xxhyzl",
        "xxisin",
        "xxjczq",
        "xxjgdw",
        "xxjsfl",
        "xxjsrq",
        "xxmbxl",
        "xxmgmz",
        "xxqtyw",
        "xxsbcs",
        "xxsldw",
        "xxsnsy",
        "xxssdq",
        "xxtpbz",
        "xxwltp",
        "xxxjxz",
        "xxyhsl",
        "xxywjc",
        "xxzbqs",
        "xxzgb",
        "xxzhbl",
        "xxzqdm",
        "xxzqjb",
        "xxzqjc",
        "xxzqqxr",
        "xxzrdw",
        "xxzrlx",
        "xxzrzt",
        "xxzsssl",
        "xxztjg",
        "xxzxsbsl",
    }
)


class BSECurrentDelistingBlockedError(RuntimeError):
    """The official evidence does not satisfy the frozen admission contract."""

    def __init__(self, message: str, *, status: str = SOURCE_REJECTED) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BSEDelistingNoticeSpec:
    code: str
    legal_name: str
    publication_date: str
    effective_date: str
    announcement_number: str
    source_url: str
    expected_sha256: str

    @property
    def code_alias(self) -> str:
        return f"{self.code}.BJ"


NOTICE_SPECS: tuple[BSEDelistingNoticeSpec, ...] = (
    BSEDelistingNoticeSpec(
        code="920305",
        legal_name="南京云创大数据科技股份有限公司",
        publication_date="2026-07-29",
        effective_date="2026-07-30",
        announcement_number="2026-076",
        source_url=(
            "https://www.bse.cn/disclosure/2026/2026-07-29/"
            "8a64d7d9ca1641609307b4f2aa5fecaf.pdf"
        ),
        expected_sha256=(
            "c82c73e29c5b90229677b4211d2d4ea4e86d3e4592836efa5d33eaf00ee47f38"
        ),
    ),
    BSEDelistingNoticeSpec(
        code="920680",
        legal_name="深圳市广道数字技术股份有限公司",
        publication_date="2025-12-31",
        effective_date="2026-01-05",
        announcement_number="2025-116",
        source_url=(
            "https://www.bse.cn/disclosure/2025/2025-12-31/"
            "8690dbac1d5c42db98e9c7388e562129.pdf"
        ),
        expected_sha256=(
            "8c5adf88d17af8e9fdf1c4a99a07ddacacf7ae1ed8b778baa43678f4a3e9a95c"
        ),
    ),
)


@dataclass(frozen=True)
class BlobReference:
    content_sha256: str
    byte_count: int
    cas_uri: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransportAttempt:
    attempt: int
    method: str
    request_url: str
    sent_cookie_names: tuple[str, ...]
    sent_cookie_value_sha256: str | None
    retrieved_at: str
    status_code: int
    response_url: str
    location: str | None
    content_type: str
    set_cookie_names: tuple[str, ...]
    set_cookie_value_sha256: str | None
    transport_audit_sha256: str
    body: BlobReference

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["sent_cookie_names"] = list(self.sent_cookie_names)
        value["set_cookie_names"] = list(self.set_cookie_names)
        return value


@dataclass(frozen=True)
class NoticeEvidence:
    code_alias: str
    legal_name: str
    publication_date: str
    effective_date: str
    announcement_number: str
    event_type: str
    source_url: str
    final_pdf: BlobReference
    transport_attempts: tuple[TransportAttempt, ...]
    extraction_engine: str
    extraction_engine_version: str
    page_count: int
    normalized_text_sha256: str
    matched_markers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["transport_attempts"] = [item.to_dict() for item in self.transport_attempts]
        value["matched_markers"] = list(self.matched_markers)
        return value


@dataclass(frozen=True)
class ParsedCataloguePage:
    page_number: int
    number_of_elements: int
    page_size: int
    total_elements: int
    total_pages: int
    first_page: bool
    last_page: bool
    common_xxjsrq: str
    codes: tuple[str, ...]
    code_set_sha256: str

    def summary(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "number_of_elements": self.number_of_elements,
            "page_size": self.page_size,
            "total_elements": self.total_elements,
            "total_pages": self.total_pages,
            "first_page": self.first_page,
            "last_page": self.last_page,
            "common_xxjsrq": self.common_xxjsrq,
            "first_code": self.codes[0],
            "last_code": self.codes[-1],
            "code_set_sha256": self.code_set_sha256,
        }


@dataclass(frozen=True)
class CataloguePageEvidence:
    page_number: int
    method: str
    request_url: str
    form_fields: tuple[tuple[str, str], ...]
    request_body_sha256: str
    retrieved_at: str
    status_code: int
    response_url: str
    location: str | None
    content_type: str
    raw_response: BlobReference
    response_summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "method": self.method,
            "request_url": self.request_url,
            "form_fields": [list(item) for item in self.form_fields],
            "request_body_sha256": self.request_body_sha256,
            "retrieved_at": self.retrieved_at,
            "status_code": self.status_code,
            "response_url": self.response_url,
            "location": self.location,
            "content_type": self.content_type,
            "raw_response": self.raw_response.to_dict(),
            "response_summary": dict(self.response_summary),
        }


@dataclass(frozen=True)
class DelistingEvent:
    code_alias: str
    legal_name: str
    exchange: str
    event_type: str
    effective_date: str
    source_url: str
    source_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BSECurrentDelistingArtifact:
    retrieved_at: str
    notices: tuple[NoticeEvidence, ...]
    catalogue_pages: tuple[CataloguePageEvidence, ...]
    catalogue_closure_probe: CataloguePageEvidence
    events: tuple[DelistingEvent, ...]
    catalogue_common_xxjsrq: str
    current_catalogue_code_set_sha256: str
    logical_content_sha256: str

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": True,
            "status": SOURCE_CONTRACT_ADMITTED,
            "scope": "BSE_FIXED_CURRENT_DELISTING_RECONCILIATION",
            "notice_transport": (
                "GET with no redirects; at most one byte-identical same-URL C3VK "
                "302 challenge retry"
            ),
            "catalogue_transport": (
                "POST read-only fixed-form query with complete zero-based pagination"
            ),
            "target_codes": sorted(spec.code_alias for spec in NOTICE_SPECS),
            "current_catalogue_is_reconciliation_only": True,
            "current_catalogue_contributes_historical_dates": False,
            "xxjsrq_semantics": "OPAQUE_SCHEMA_SENTINEL_ONLY",
            "historical_effective_dates_come_only_from_notice_pdfs": True,
            "trading_eligibility": False,
            "consumer_must_validate_freshness": True,
            "maximum_age_seconds": int(MAX_CURRENT_EVIDENCE_AGE.total_seconds()),
            "maximum_capture_span_seconds": int(MAX_CAPTURE_SPAN.total_seconds()),
            "maximum_future_clock_skew_seconds": int(
                MAX_FUTURE_CLOCK_SKEW.total_seconds()
            ),
            "audit_only": True,
        }

    @property
    def completeness(self) -> dict[str, Any]:
        return {
            "status": EVIDENCE_COMPLETE,
            "complete": True,
            "notice_count": len(self.notices),
            "event_count": len(self.events),
            "catalogue_page_count": len(self.catalogue_pages),
            "catalogue_total_elements": int(
                self.catalogue_pages[0].response_summary["total_elements"]
            ),
            "target_codes_absent_from_current_catalogue": sorted(
                item.code_alias for item in self.events
            ),
            "full_pagination_closed": True,
            "page_zero_closure_probe_matches": True,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        attempts = sum(len(item.transport_attempts) for item in self.notices)
        challenged = sum(len(item.transport_attempts) == 2 for item in self.notices)
        return {
            "target_count": len(self.events),
            "notice_transport_attempt_count": attempts,
            "notice_challenge_count": challenged,
            "catalogue_page_count": len(self.catalogue_pages),
            "catalogue_common_xxjsrq": self.catalogue_common_xxjsrq,
            "catalogue_code_set_sha256": self.current_catalogue_code_set_sha256,
        }

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "retrieved_at": self.retrieved_at,
            "notices": [item.to_dict() for item in self.notices],
            "catalogue_pages": [item.to_dict() for item in self.catalogue_pages],
            "catalogue_closure_probe": self.catalogue_closure_probe.to_dict(),
            "events": [item.to_dict() for item in self.events],
            "catalogue_common_xxjsrq": self.catalogue_common_xxjsrq,
            "current_catalogue_code_set_sha256": (
                self.current_catalogue_code_set_sha256
            ),
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
            "completeness": self.completeness,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class ManifestReference:
    manifest_sha256: str
    byte_count: int
    cas_uri: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _strict_json(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BSECurrentDelistingBlockedError(f"{label} is not UTF-8") from exc

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BSECurrentDelistingBlockedError(
                    f"{label} contains duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                BSECurrentDelistingBlockedError(
                    f"{label} contains invalid JSON constant: {value}"
                )
            ),
        )
    except BSECurrentDelistingBlockedError:
        raise
    except json.JSONDecodeError as exc:
        raise BSECurrentDelistingBlockedError(f"{label} is invalid JSON") from exc


def _normalize_timestamp(value: datetime | str | None = None) -> str:
    if value is None:
        parsed = datetime.now().astimezone()
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise BSECurrentDelistingBlockedError(
                f"invalid evidence timestamp: {value!r}"
            ) from exc
    else:
        raise BSECurrentDelistingBlockedError("evidence timestamp has invalid type")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BSECurrentDelistingBlockedError("evidence timestamp needs a timezone")
    return parsed.replace(microsecond=0).isoformat()


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BSECurrentDelistingBlockedError(f"invalid {label}: {value!r}")
    return value


def _valid_digest(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise BSECurrentDelistingBlockedError(f"invalid {label} SHA-256")
    return digest


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        metadata = os.lstat(path)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & int(flag))


def _assert_safe_existing_chain(root: Path, target: Path) -> None:
    root_abs = root.absolute()
    target_abs = target.absolute()
    try:
        relative = target_abs.relative_to(root_abs)
    except ValueError as exc:
        raise BSECurrentDelistingBlockedError("CAS path escapes its fixed root") from exc
    current = root_abs
    if current.exists() and _path_is_link_or_reparse(current):
        raise BSECurrentDelistingBlockedError("CAS root is a link or reparse point")
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _path_is_link_or_reparse(current):
            raise BSECurrentDelistingBlockedError(
                "CAS path contains a link, junction, or reparse point"
            )


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    _assert_safe_existing_chain(root, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_existing_chain(root, path.parent)
    if path.exists():
        persisted = _stable_read(root, path)
        if persisted != content:
            raise BSECurrentDelistingBlockedError(
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
        _assert_safe_existing_chain(root, temporary)
        os.replace(temporary, path)
        if _stable_read(root, path) != content:
            raise BSECurrentDelistingBlockedError("CAS write verification failed")
    finally:
        if temporary.exists() and not _path_is_link_or_reparse(temporary):
            temporary.unlink()


def _stable_read(
    root: Path,
    path: Path,
    *,
    maximum: int = MAX_CAS_OBJECT_BYTES,
) -> bytes:
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise BSECurrentDelistingBlockedError("CAS read bound is invalid")
    _assert_safe_existing_chain(root, path)
    if not path.is_file() or _path_is_link_or_reparse(path):
        raise BSECurrentDelistingBlockedError("CAS object is missing or unsafe")
    before = os.lstat(path)
    if before.st_size < 0 or before.st_size > maximum:
        raise BSECurrentDelistingBlockedError("CAS object is oversized")
    content = path.read_bytes()
    after = os.lstat(path)
    fingerprint_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        int(getattr(before, "st_file_attributes", 0)),
    )
    fingerprint_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        int(getattr(after, "st_file_attributes", 0)),
    )
    if fingerprint_before != fingerprint_after or _path_is_link_or_reparse(path):
        raise BSECurrentDelistingBlockedError("CAS object changed while being read")
    if len(content) > maximum:
        raise BSECurrentDelistingBlockedError("CAS object is oversized")
    return content


class BSECurrentDelistingCAS:
    """Fixed-root content-addressed store for raw responses and manifests."""

    def __init__(self, root: Path) -> None:
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = root_path.absolute()
        self.root = root_path

    def _blob_path(self, digest: str) -> Path:
        return self.root / "objects" / "sha256" / digest[:2] / digest

    def _manifest_path(self, digest: str) -> Path:
        return self.root / "manifests" / f"{digest}.json"

    def put_blob(self, content: bytes) -> BlobReference:
        if not isinstance(content, bytes):
            content = bytes(content)
        if len(content) > MAX_CAS_OBJECT_BYTES:
            raise BSECurrentDelistingBlockedError("CAS object is oversized")
        digest = _sha256(content)
        path = self._blob_path(digest)
        _atomic_write_exact(self.root, path, content)
        return BlobReference(digest, len(content), f"sha256:{digest}")

    def read_blob(self, reference: BlobReference) -> bytes:
        digest = _valid_digest(reference.content_sha256, "CAS object")
        if (
            isinstance(reference.byte_count, bool)
            or not isinstance(reference.byte_count, int)
            or reference.byte_count < 0
            or reference.cas_uri != f"sha256:{digest}"
        ):
            raise BSECurrentDelistingBlockedError("CAS reference metadata is invalid")
        content = _stable_read(
            self.root,
            self._blob_path(digest),
            maximum=MAX_CAS_OBJECT_BYTES,
        )
        if len(content) != reference.byte_count or _sha256(content) != digest:
            raise BSECurrentDelistingBlockedError("CAS object hash or size mismatch")
        return content

    def write_manifest(self, content: bytes) -> ManifestReference:
        if not content or len(content) > MAX_MANIFEST_BYTES:
            raise BSECurrentDelistingBlockedError("manifest is empty or oversized")
        digest = _sha256(content)
        _atomic_write_exact(self.root, self._manifest_path(digest), content)
        return ManifestReference(digest, len(content), f"sha256:{digest}")

    def read_manifest(self, digest: str) -> bytes:
        normalized = _valid_digest(digest, "manifest")
        content = _stable_read(
            self.root,
            self._manifest_path(normalized),
            maximum=MAX_MANIFEST_BYTES,
        )
        if _sha256(content) != normalized:
            raise BSECurrentDelistingBlockedError("manifest hash mismatch")
        return content


def _media_type(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BSECurrentDelistingBlockedError("response Content-Type is invalid")
    return value.split(";", 1)[0].strip().lower()


def _catalogue_content_type(value: Any) -> str:
    if not isinstance(value, str):
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue lacks Content-Type"
        )
    normalized = ";".join(item.strip().lower() for item in value.split(";"))
    if normalized != CATALOGUE_MEDIA_TYPE:
        raise BSECurrentDelistingBlockedError(
            f"BSE current catalogue Content-Type changed: {value!r}"
        )
    return normalized


def _catalogue_form_fields(page: int) -> tuple[tuple[str, str], ...]:
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        raise BSECurrentDelistingBlockedError("invalid catalogue page number")
    return (
        ("page", str(page)),
        ("typejb", "T"),
        ("xxfcbj[]", "2"),
        ("xxzqdm", ""),
        ("sortfield", "xxzqdm"),
        ("sorttype", "asc"),
    )


def _validate_notice_url(spec: BSEDelistingNoticeSpec) -> None:
    parsed = urlsplit(spec.source_url)
    expected_path = f"/disclosure/{spec.publication_date[:4]}/{spec.publication_date}/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != BSE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(expected_path)
        or not re.fullmatch(
            re.escape(expected_path) + r"[0-9a-f]{32}\.pdf", parsed.path
        )
    ):
        raise BSECurrentDelistingBlockedError("BSE notice URL is not canonical")


def _extract_pdf_text(raw_pdf: bytes) -> tuple[str, str, str, int]:
    if (
        not raw_pdf
        or len(raw_pdf) > MAX_PDF_BYTES
        or not raw_pdf.startswith(b"%PDF-")
        or b"%%EOF" not in raw_pdf[-4096:]
    ):
        raise BSECurrentDelistingBlockedError("BSE notice is not a strict PDF")
    try:
        import pypdf
        from pypdf import PdfReader
    except ImportError as exc:
        raise BSECurrentDelistingBlockedError(
            "pypdf is required to replay BSE notices"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw_pdf), strict=True)
        if reader.is_encrypted:
            raise BSECurrentDelistingBlockedError("encrypted BSE PDF is rejected")
        page_count = len(reader.pages)
        if page_count <= 0 or page_count > MAX_PDF_PAGES:
            raise BSECurrentDelistingBlockedError("BSE PDF page count is invalid")
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text is None:
                raise BSECurrentDelistingBlockedError(
                    "BSE PDF has no reproducible text layer"
                )
            pages.append(text)
    except BSECurrentDelistingBlockedError:
        raise
    except Exception as exc:
        raise BSECurrentDelistingBlockedError(
            "BSE PDF text extraction failed closed"
        ) from exc
    joined = "\n\f\n".join(pages)
    if not joined.strip() or len(joined) > MAX_PDF_TEXT_CHARS:
        raise BSECurrentDelistingBlockedError("BSE PDF text is empty or oversized")
    return joined, "pypdf", str(getattr(pypdf, "__version__", "UNKNOWN")), page_count


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).replace("\x00", ""))


def _chinese_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def parse_notice_pdf(
    raw_pdf: bytes,
    *,
    spec: BSEDelistingNoticeSpec,
) -> dict[str, Any]:
    _validate_notice_url(spec)
    digest = _sha256(raw_pdf)
    if digest != _valid_digest(spec.expected_sha256, "admitted notice"):
        raise BSECurrentDelistingBlockedError(
            f"BSE notice hash mismatch for {spec.code_alias}"
        )
    text, engine, engine_version, page_count = _extract_pdf_text(raw_pdf)
    compact = _compact_text(text)
    title = f"{spec.legal_name}关于公司股票终止上市暨摘牌的公告"
    code_marker = f"证券代码:{spec.code}"
    number_marker = f"公告编号:{spec.announcement_number}"
    effective_marker = (
        f"公司股票将于{_chinese_date(spec.effective_date)}"
        "被北京证券交易所终止上市并摘牌"
    )
    markers = (title, code_marker, number_marker, effective_marker)
    missing = [marker for marker in markers if _compact_text(marker) not in compact]
    if missing:
        raise BSECurrentDelistingBlockedError(
            f"BSE notice lacks identity/effective-date markers for {spec.code_alias}"
        )
    suffix = _compact_text("日被北京证券交易所终止上市并摘牌")
    matches = re.findall(
        rf"(20\d{{2}})年(\d{{1,2}})月(\d{{1,2}}){re.escape(suffix)}",
        compact,
    )
    parsed_dates: list[str] = []
    for year, month, day_value in matches:
        try:
            parsed_dates.append(
                date(int(year), int(month), int(day_value)).isoformat()
            )
        except ValueError as exc:
            raise BSECurrentDelistingBlockedError(
                "BSE notice contains an invalid effective date"
            ) from exc
    if parsed_dates != [spec.effective_date]:
        raise BSECurrentDelistingBlockedError(
            "BSE termination-and-delisting effective date is missing or ambiguous"
        )
    if date.fromisoformat(spec.effective_date) <= date.fromisoformat(
        spec.publication_date
    ):
        raise BSECurrentDelistingBlockedError(
            "BSE effective date must follow notice publication"
        )
    return {
        "code_alias": spec.code_alias,
        "legal_name": spec.legal_name,
        "publication_date": spec.publication_date,
        "effective_date": spec.effective_date,
        "announcement_number": spec.announcement_number,
        "event_type": "TERMINATED_LISTING",
        "source_url": spec.source_url,
        "content_sha256": digest,
        "extraction_engine": engine,
        "extraction_engine_version": engine_version,
        "page_count": page_count,
        "normalized_text_sha256": _sha256(compact.encode("utf-8")),
        "matched_markers": list(markers),
    }


def parse_current_catalogue_page(
    raw: bytes,
    *,
    request_page: int,
    minimum_rows: int | None = None,
) -> ParsedCataloguePage:
    admitted_minimum_rows = (
        BSE_CATALOGUE_MINIMUM_ROWS if minimum_rows is None else minimum_rows
    )
    if (
        isinstance(admitted_minimum_rows, bool)
        or not isinstance(admitted_minimum_rows, int)
        or admitted_minimum_rows <= 0
    ):
        raise BSECurrentDelistingBlockedError(
            "catalogue minimum-row contract is invalid"
        )
    if not raw or len(raw) > MAX_CATALOGUE_BYTES:
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue page is empty or oversized"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue page is not UTF-8"
        ) from exc
    match = re.fullmatch(r"null\((.*)\)", text, flags=re.DOTALL)
    if match is None:
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue wrapper changed"
        )
    value = _strict_json(match.group(1).encode("utf-8"), "BSE catalogue payload")
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue root schema changed"
        )
    page = value[0]
    if set(page) != CATALOGUE_PAGE_FIELDS:
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue page schema drift"
        )
    number = _strict_int(page["number"], "catalogue number")
    size = _strict_int(page["size"], "catalogue size", minimum=1)
    number_of_elements = _strict_int(
        page["numberOfElements"], "catalogue numberOfElements"
    )
    total_elements = _strict_int(
        page["totalElements"], "catalogue totalElements", minimum=1
    )
    total_pages = _strict_int(page["totalPages"], "catalogue totalPages", minimum=1)
    first_page = page["firstPage"]
    last_page = page["lastPage"]
    if not isinstance(first_page, bool) or not isinstance(last_page, bool):
        raise BSECurrentDelistingBlockedError(
            "BSE catalogue first/last flags are not booleans"
        )
    rows = page["content"]
    if not isinstance(rows, list) or not rows:
        raise BSECurrentDelistingBlockedError("BSE current catalogue page has no rows")
    if (
        number != request_page
        or size != BSE_CATALOGUE_PAGE_SIZE
        or number_of_elements != len(rows)
        or number_of_elements > size
        or total_elements < admitted_minimum_rows
        or total_pages != math.ceil(total_elements / size)
        or total_pages > BSE_CATALOGUE_MAXIMUM_PAGES
        or first_page is not (number == 0)
        or last_page is not (number == total_pages - 1)
        or page["sort"] is not None
        or (not last_page and number_of_elements != size)
        or (last_page and number_of_elements != total_elements - size * number)
    ):
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue pagination metadata is incomplete or inconsistent"
        )
    codes: list[str] = []
    common_row_dates: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != CATALOGUE_ROW_FIELDS:
            raise BSECurrentDelistingBlockedError(
                "BSE current catalogue row schema drift"
            )
        code = row["xxzqdm"]
        name = row["xxzqjc"]
        opaque_row_date = row["xxjsrq"]
        if (
            not isinstance(code, str)
            or not re.fullmatch(r"920\d{3}", code)
            or not isinstance(name, str)
            or not name.strip()
            or row["xxzqjb"] != "T"
            or row["xxfcbj"] != "2"
            or not isinstance(opaque_row_date, str)
            or not re.fullmatch(r"\d{8}", opaque_row_date)
        ):
            raise BSECurrentDelistingBlockedError(
                "BSE current catalogue row identity/status changed"
            )
        try:
            parsed_row_date = datetime.strptime(
                opaque_row_date, "%Y%m%d"
            ).date().isoformat()
        except ValueError as exc:
            raise BSECurrentDelistingBlockedError(
                "BSE current catalogue opaque xxjsrq value is invalid"
            ) from exc
        if parsed_row_date.replace("-", "") != opaque_row_date:
            raise BSECurrentDelistingBlockedError(
                "BSE current catalogue opaque xxjsrq value is noncanonical"
            )
        codes.append(code)
        common_row_dates.add(opaque_row_date)
    if len(set(codes)) != len(codes) or codes != sorted(codes):
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue page has duplicate or unsorted codes"
        )
    if len(common_row_dates) != 1:
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue page mixes opaque xxjsrq row values"
        )
    code_tuple = tuple(codes)
    return ParsedCataloguePage(
        page_number=number,
        number_of_elements=number_of_elements,
        page_size=size,
        total_elements=total_elements,
        total_pages=total_pages,
        first_page=first_page,
        last_page=last_page,
        common_xxjsrq=next(iter(common_row_dates)),
        codes=code_tuple,
        code_set_sha256=_sha256(_canonical_json_bytes(list(code_tuple))),
    )


def _blob_from_dict(value: Any) -> BlobReference:
    fields = {"content_sha256", "byte_count", "cas_uri"}
    if not isinstance(value, dict) or set(value) != fields:
        raise BSECurrentDelistingBlockedError("CAS reference schema drift")
    digest = _valid_digest(value["content_sha256"], "CAS reference")
    byte_count = _strict_int(value["byte_count"], "CAS byte_count")
    if value["cas_uri"] != f"sha256:{digest}":
        raise BSECurrentDelistingBlockedError("CAS URI does not match its hash")
    return BlobReference(digest, byte_count, value["cas_uri"])


def _response_content(response: Any) -> bytes:
    content = response.content
    return content if isinstance(content, bytes) else bytes(content)


def _transport_audit_sha256(
    *,
    attempt: int,
    request_url: str,
    sent_cookie_names: Sequence[str],
    sent_cookie_value_sha256: str | None,
    status_code: int,
    response_url: str,
    location: str | None,
    content_type: str,
    set_cookie_names: Sequence[str],
    set_cookie_value_sha256: str | None,
    body_sha256: str,
) -> str:
    return _sha256(
        _canonical_json_bytes(
            {
                "attempt": attempt,
                "method": "GET",
                "request_url": request_url,
                "sent_cookie_names": list(sent_cookie_names),
                "sent_cookie_value_sha256": sent_cookie_value_sha256,
                "status_code": status_code,
                "response_url": response_url,
                "location": location,
                "content_type": content_type,
                "set_cookie_names": list(set_cookie_names),
                "set_cookie_value_sha256": set_cookie_value_sha256,
                "body_sha256": body_sha256,
            }
        )
    )


def _assert_same_url_response(response: Any, expected_url: str, label: str) -> None:
    if str(response.url) != expected_url:
        raise BSECurrentDelistingBlockedError(f"{label} response URL changed")
    history = getattr(response, "history", ())
    if history:
        raise BSECurrentDelistingBlockedError(f"{label} followed a redirect")


def _parse_c3vk_challenge(response: Any, canonical_url: str) -> tuple[str, str]:
    if response.headers.get("Location") != canonical_url:
        raise BSECurrentDelistingBlockedError(
            "BSE C3VK challenge Location is not byte-identical to the canonical URL"
        )
    content_type = _media_type(response.headers.get("Content-Type"))
    if content_type not in {"", "text/html"}:
        raise BSECurrentDelistingBlockedError(
            "BSE C3VK challenge Content-Type changed"
        )
    header = response.headers.get("Set-Cookie")
    if not isinstance(header, str) or not header:
        raise BSECurrentDelistingBlockedError(
            "BSE C3VK challenge did not set its cookie"
        )
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception as exc:
        raise BSECurrentDelistingBlockedError(
            "BSE C3VK challenge cookie is invalid"
        ) from exc
    if set(cookie) != {"C3VK"}:
        raise BSECurrentDelistingBlockedError(
            "BSE challenge set an unexpected cookie"
        )
    morsel = cookie["C3VK"]
    value = morsel.value
    if not value or len(value) > 4096 or any(character in value for character in "\r\n;"):
        raise BSECurrentDelistingBlockedError("BSE C3VK cookie value is invalid")
    domain = morsel["domain"].lower()
    if domain not in {"", BSE_HOST, ".bse.cn"}:
        raise BSECurrentDelistingBlockedError("BSE C3VK cookie domain changed")
    if morsel["path"] not in {"", "/"}:
        raise BSECurrentDelistingBlockedError("BSE C3VK cookie path changed")
    if not morsel["secure"]:
        raise BSECurrentDelistingBlockedError("BSE C3VK cookie is not Secure")
    return value, _sha256(header.encode("utf-8"))


def _build_transport_attempt(
    *,
    cas: BSECurrentDelistingCAS,
    response: Any,
    attempt: int,
    request_url: str,
    retrieved_at: str,
    sent_cookie_value: str | None,
    challenge_cookie_value: str | None,
) -> TransportAttempt:
    body = _response_content(response)
    maximum = MAX_CHALLENGE_BYTES if int(response.status_code) == 302 else MAX_PDF_BYTES
    if len(body) > maximum:
        raise BSECurrentDelistingBlockedError("BSE notice response is oversized")
    set_cookie_header = response.headers.get("Set-Cookie")
    sent_names = ("C3VK",) if sent_cookie_value is not None else ()
    sent_value_hash = (
        _sha256(sent_cookie_value.encode("utf-8"))
        if sent_cookie_value is not None
        else None
    )
    set_names = ("C3VK",) if challenge_cookie_value is not None else ()
    set_value_hash = (
        _sha256(challenge_cookie_value.encode("utf-8"))
        if challenge_cookie_value is not None
        else None
    )
    content_type = _media_type(response.headers.get("Content-Type"))
    body_reference = cas.put_blob(body)
    audit_sha256 = _transport_audit_sha256(
        attempt=attempt,
        request_url=request_url,
        sent_cookie_names=sent_names,
        sent_cookie_value_sha256=sent_value_hash,
        status_code=int(response.status_code),
        response_url=str(response.url),
        location=response.headers.get("Location"),
        content_type=content_type,
        set_cookie_names=set_names,
        set_cookie_value_sha256=set_value_hash,
        body_sha256=body_reference.content_sha256,
    )
    return TransportAttempt(
        attempt=attempt,
        method="GET",
        request_url=request_url,
        sent_cookie_names=sent_names,
        sent_cookie_value_sha256=sent_value_hash,
        retrieved_at=_normalize_timestamp(retrieved_at),
        status_code=int(response.status_code),
        response_url=str(response.url),
        location=response.headers.get("Location"),
        content_type=content_type,
        set_cookie_names=set_names,
        set_cookie_value_sha256=set_value_hash,
        transport_audit_sha256=audit_sha256,
        body=body_reference,
    )


def _validate_transport_attempts(
    attempts: Sequence[TransportAttempt],
    *,
    spec: BSEDelistingNoticeSpec,
    cas: BSECurrentDelistingCAS,
) -> bytes:
    if len(attempts) not in {1, 2}:
        raise BSECurrentDelistingBlockedError(
            "BSE notice must use direct GET or one challenge retry"
        )
    for index, attempt in enumerate(attempts, start=1):
        if (
            attempt.attempt != index
            or attempt.method != "GET"
            or attempt.request_url != spec.source_url
            or attempt.response_url != spec.source_url
        ):
            raise BSECurrentDelistingBlockedError(
                "BSE notice transport audit identity changed"
            )
        _normalize_timestamp(attempt.retrieved_at)
        expected_audit = _transport_audit_sha256(
            attempt=attempt.attempt,
            request_url=attempt.request_url,
            sent_cookie_names=attempt.sent_cookie_names,
            sent_cookie_value_sha256=attempt.sent_cookie_value_sha256,
            status_code=attempt.status_code,
            response_url=attempt.response_url,
            location=attempt.location,
            content_type=attempt.content_type,
            set_cookie_names=attempt.set_cookie_names,
            set_cookie_value_sha256=attempt.set_cookie_value_sha256,
            body_sha256=attempt.body.content_sha256,
        )
        if attempt.transport_audit_sha256 != expected_audit:
            raise BSECurrentDelistingBlockedError(
                "BSE notice transport audit hash mismatch"
            )
        cas.read_blob(attempt.body)
    first = attempts[0]
    if len(attempts) == 1:
        if (
            first.status_code != 200
            or first.location is not None
            or first.content_type != PDF_MEDIA_TYPE
            or first.sent_cookie_names
            or first.sent_cookie_value_sha256 is not None
            or first.set_cookie_names
            or first.set_cookie_value_sha256 is not None
        ):
            raise BSECurrentDelistingBlockedError(
                "direct BSE PDF transport audit is invalid"
            )
    else:
        second = attempts[1]
        if (
            first.status_code != 302
            or first.location != spec.source_url
            or first.content_type not in {"", "text/html"}
            or first.sent_cookie_names
            or first.sent_cookie_value_sha256 is not None
            or first.set_cookie_names != ("C3VK",)
            or first.set_cookie_value_sha256 is None
            or second.status_code != 200
            or second.location is not None
            or second.content_type != PDF_MEDIA_TYPE
            or second.sent_cookie_names != ("C3VK",)
            or second.sent_cookie_value_sha256 != first.set_cookie_value_sha256
            or second.set_cookie_names
            or second.set_cookie_value_sha256 is not None
        ):
            raise BSECurrentDelistingBlockedError(
                "BSE C3VK one-retry transport audit is invalid"
            )
    final = cas.read_blob(attempts[-1].body)
    if not final.startswith(b"%PDF-"):
        raise BSECurrentDelistingBlockedError("final BSE notice response is not PDF")
    return final


def _validate_catalogue_pages(
    pages: Sequence[tuple[CataloguePageEvidence, ParsedCataloguePage]],
    closure: tuple[CataloguePageEvidence, ParsedCataloguePage],
) -> tuple[str, tuple[str, ...], str]:
    if not pages:
        raise BSECurrentDelistingBlockedError("BSE current catalogue has no pages")
    expected_total_pages = pages[0][1].total_pages
    expected_total_elements = pages[0][1].total_elements
    expected_xxjsrq = pages[0][1].common_xxjsrq
    if len(pages) != expected_total_pages:
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue page set is incomplete"
        )
    all_codes: list[str] = []
    for index, (evidence, parsed) in enumerate(pages):
        if (
            evidence.page_number != index
            or parsed.page_number != index
            or parsed.total_pages != expected_total_pages
            or parsed.total_elements != expected_total_elements
            or parsed.common_xxjsrq != expected_xxjsrq
        ):
            raise BSECurrentDelistingBlockedError(
                "BSE current catalogue pages disagree"
            )
        all_codes.extend(parsed.codes)
    if (
        len(all_codes) != expected_total_elements
        or len(set(all_codes)) != len(all_codes)
        or all_codes != sorted(all_codes)
    ):
        raise BSECurrentDelistingBlockedError(
            "BSE current catalogue full-page closure has gaps or duplicates"
        )
    target_codes = {spec.code for spec in NOTICE_SPECS}
    present = sorted(target_codes & set(all_codes))
    if present:
        raise BSECurrentDelistingBlockedError(
            f"delisted targets remain in the BSE current catalogue: {present}"
        )
    closure_evidence, closure_parsed = closure
    first_evidence, first_parsed = pages[0]
    if (
        closure_evidence.page_number != 0
        or closure_parsed.summary() != first_parsed.summary()
        or closure_evidence.raw_response != first_evidence.raw_response
    ):
        raise BSECurrentDelistingBlockedError(
            "BSE page-zero closure probe changed during pagination"
        )
    codes = tuple(all_codes)
    return (
        expected_xxjsrq,
        codes,
        _sha256(_canonical_json_bytes(list(codes))),
    )


def _catalogue_evidence_from_dict(
    value: Any,
    *,
    cas: BSECurrentDelistingCAS,
) -> tuple[CataloguePageEvidence, ParsedCataloguePage]:
    fields = {
        "page_number",
        "method",
        "request_url",
        "form_fields",
        "request_body_sha256",
        "retrieved_at",
        "status_code",
        "response_url",
        "location",
        "content_type",
        "raw_response",
        "response_summary",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise BSECurrentDelistingBlockedError("catalogue evidence schema drift")
    page_number = _strict_int(value["page_number"], "catalogue evidence page")
    raw_fields = value["form_fields"]
    if not isinstance(raw_fields, list) or any(
        not isinstance(item, list)
        or len(item) != 2
        or not all(isinstance(part, str) for part in item)
        for item in raw_fields
    ):
        raise BSECurrentDelistingBlockedError("catalogue form fields are invalid")
    form_fields = tuple((item[0], item[1]) for item in raw_fields)
    expected_fields = _catalogue_form_fields(page_number)
    request_body = urlencode(expected_fields).encode("ascii")
    if (
        value["method"] != "POST"
        or value["request_url"] != BSE_CURRENT_CATALOGUE_URL
        or form_fields != expected_fields
        or value["request_body_sha256"] != _sha256(request_body)
        or value["status_code"] != 200
        or isinstance(value["status_code"], bool)
        or value["response_url"] != BSE_CURRENT_CATALOGUE_URL
        or value["location"] is not None
        or value["content_type"] != CATALOGUE_MEDIA_TYPE
    ):
        raise BSECurrentDelistingBlockedError(
            "catalogue read-only POST transport contract changed"
        )
    retrieved_at = _normalize_timestamp(value["retrieved_at"])
    if retrieved_at != value["retrieved_at"]:
        raise BSECurrentDelistingBlockedError(
            "catalogue retrieved_at is noncanonical"
        )
    reference = _blob_from_dict(value["raw_response"])
    raw = cas.read_blob(reference)
    parsed = parse_current_catalogue_page(raw, request_page=page_number)
    summary = value["response_summary"]
    if not isinstance(summary, dict) or summary != parsed.summary():
        raise BSECurrentDelistingBlockedError(
            "catalogue caller summary does not match raw CAS bytes"
        )
    evidence = CataloguePageEvidence(
        page_number=page_number,
        method="POST",
        request_url=BSE_CURRENT_CATALOGUE_URL,
        form_fields=form_fields,
        request_body_sha256=value["request_body_sha256"],
        retrieved_at=retrieved_at,
        status_code=200,
        response_url=BSE_CURRENT_CATALOGUE_URL,
        location=None,
        content_type=CATALOGUE_MEDIA_TYPE,
        raw_response=reference,
        response_summary=parsed.summary(),
    )
    return evidence, parsed


def _attempt_from_dict(value: Any) -> TransportAttempt:
    fields = {
        "attempt",
        "method",
        "request_url",
        "sent_cookie_names",
        "sent_cookie_value_sha256",
        "retrieved_at",
        "status_code",
        "response_url",
        "location",
        "content_type",
        "set_cookie_names",
        "set_cookie_value_sha256",
        "transport_audit_sha256",
        "body",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise BSECurrentDelistingBlockedError("transport attempt schema drift")
    sent_names = value["sent_cookie_names"]
    set_names = value["set_cookie_names"]
    if (
        not isinstance(sent_names, list)
        or not all(isinstance(item, str) for item in sent_names)
        or not isinstance(set_names, list)
        or not all(isinstance(item, str) for item in set_names)
    ):
        raise BSECurrentDelistingBlockedError("transport cookie audit is invalid")
    for field in ("sent_cookie_value_sha256", "set_cookie_value_sha256"):
        if value[field] is not None:
            _valid_digest(value[field], field)
    return TransportAttempt(
        attempt=_strict_int(value["attempt"], "transport attempt", minimum=1),
        method=value["method"],
        request_url=value["request_url"],
        sent_cookie_names=tuple(sent_names),
        sent_cookie_value_sha256=value["sent_cookie_value_sha256"],
        retrieved_at=_normalize_timestamp(value["retrieved_at"]),
        status_code=_strict_int(value["status_code"], "transport status"),
        response_url=value["response_url"],
        location=value["location"],
        content_type=value["content_type"],
        set_cookie_names=tuple(set_names),
        set_cookie_value_sha256=value["set_cookie_value_sha256"],
        transport_audit_sha256=_valid_digest(
            value["transport_audit_sha256"], "transport audit"
        ),
        body=_blob_from_dict(value["body"]),
    )


def _notice_evidence_from_dict(
    value: Any,
    *,
    spec: BSEDelistingNoticeSpec,
    cas: BSECurrentDelistingCAS,
) -> NoticeEvidence:
    fields = {
        "code_alias",
        "legal_name",
        "publication_date",
        "effective_date",
        "announcement_number",
        "event_type",
        "source_url",
        "final_pdf",
        "transport_attempts",
        "extraction_engine",
        "extraction_engine_version",
        "page_count",
        "normalized_text_sha256",
        "matched_markers",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise BSECurrentDelistingBlockedError("notice evidence schema drift")
    raw_attempts = value["transport_attempts"]
    if not isinstance(raw_attempts, list):
        raise BSECurrentDelistingBlockedError("notice transport audit is invalid")
    attempts = tuple(_attempt_from_dict(item) for item in raw_attempts)
    raw_pdf = _validate_transport_attempts(attempts, spec=spec, cas=cas)
    final_pdf = _blob_from_dict(value["final_pdf"])
    if final_pdf != attempts[-1].body or cas.read_blob(final_pdf) != raw_pdf:
        raise BSECurrentDelistingBlockedError(
            "notice final PDF is not its last transport body"
        )
    parsed = parse_notice_pdf(raw_pdf, spec=spec)
    markers = value["matched_markers"]
    if not isinstance(markers, list) or not all(isinstance(item, str) for item in markers):
        raise BSECurrentDelistingBlockedError("notice marker audit is invalid")
    evidence = NoticeEvidence(
        code_alias=parsed["code_alias"],
        legal_name=parsed["legal_name"],
        publication_date=parsed["publication_date"],
        effective_date=parsed["effective_date"],
        announcement_number=parsed["announcement_number"],
        event_type=parsed["event_type"],
        source_url=parsed["source_url"],
        final_pdf=final_pdf,
        transport_attempts=attempts,
        extraction_engine=parsed["extraction_engine"],
        extraction_engine_version=parsed["extraction_engine_version"],
        page_count=parsed["page_count"],
        normalized_text_sha256=parsed["normalized_text_sha256"],
        matched_markers=tuple(parsed["matched_markers"]),
    )
    if evidence.to_dict() != value:
        raise BSECurrentDelistingBlockedError(
            "notice caller summary does not match raw PDF CAS bytes"
        )
    return evidence


def _all_capture_times(artifact: BSECurrentDelistingArtifact) -> list[datetime]:
    values: list[str] = []
    for notice in artifact.notices:
        values.extend(item.retrieved_at for item in notice.transport_attempts)
    values.extend(item.retrieved_at for item in artifact.catalogue_pages)
    values.append(artifact.catalogue_closure_probe.retrieved_at)
    return [datetime.fromisoformat(_normalize_timestamp(value)) for value in values]


def _build_artifact(
    *,
    notices: Sequence[NoticeEvidence],
    pages: Sequence[tuple[CataloguePageEvidence, ParsedCataloguePage]],
    closure: tuple[CataloguePageEvidence, ParsedCataloguePage],
) -> BSECurrentDelistingArtifact:
    expected_aliases = [spec.code_alias for spec in NOTICE_SPECS]
    if [item.code_alias for item in notices] != expected_aliases:
        raise BSECurrentDelistingBlockedError(
            "BSE notice set is incomplete, duplicated, or out of order"
        )
    common_xxjsrq, current_codes, code_set_sha256 = _validate_catalogue_pages(
        pages, closure
    )
    events = tuple(
        DelistingEvent(
            code_alias=item.code_alias,
            legal_name=item.legal_name,
            exchange="BSE",
            event_type=item.event_type,
            effective_date=item.effective_date,
            source_url=item.source_url,
            source_sha256=item.final_pdf.content_sha256,
        )
        for item in notices
    )
    capture_values: list[str] = []
    for item in notices:
        capture_values.extend(attempt.retrieved_at for attempt in item.transport_attempts)
    capture_values.extend(item[0].retrieved_at for item in pages)
    capture_values.append(closure[0].retrieved_at)
    parsed_times = [datetime.fromisoformat(_normalize_timestamp(item)) for item in capture_values]
    if max(parsed_times) - min(parsed_times) > MAX_CAPTURE_SPAN:
        raise BSECurrentDelistingBlockedError(
            "BSE current-delisting evidence was not captured in one bounded window"
        )
    retrieved_at = max(parsed_times).replace(microsecond=0).isoformat()
    logical = {
        "events": [item.to_dict() for item in events],
        "notice_evidence": [item.to_dict() for item in notices],
        "catalogue_page_hashes": [
            item[0].raw_response.content_sha256 for item in pages
        ],
        "catalogue_closure_hash": closure[0].raw_response.content_sha256,
        "catalogue_common_xxjsrq": common_xxjsrq,
        "catalogue_codes_sha256": code_set_sha256,
        "catalogue_row_count": len(current_codes),
    }
    return BSECurrentDelistingArtifact(
        retrieved_at=retrieved_at,
        notices=tuple(notices),
        catalogue_pages=tuple(item[0] for item in pages),
        catalogue_closure_probe=closure[0],
        events=events,
        catalogue_common_xxjsrq=common_xxjsrq,
        current_catalogue_code_set_sha256=code_set_sha256,
        logical_content_sha256=_sha256(_canonical_json_bytes(logical)),
    )


def _rebuild_from_manifest_payload(
    value: Any,
    *,
    cas: BSECurrentDelistingCAS,
) -> BSECurrentDelistingArtifact:
    fields = {
        "protocol_version",
        "retrieved_at",
        "notices",
        "catalogue_pages",
        "catalogue_closure_probe",
        "events",
        "catalogue_common_xxjsrq",
        "current_catalogue_code_set_sha256",
        "logical_content_sha256",
        "source_contract",
        "completeness",
        "statistics",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise BSECurrentDelistingBlockedError("manifest schema drift")
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise BSECurrentDelistingBlockedError("manifest protocol version changed")
    raw_notices = value["notices"]
    raw_pages = value["catalogue_pages"]
    if not isinstance(raw_notices, list) or not isinstance(raw_pages, list):
        raise BSECurrentDelistingBlockedError("manifest evidence collections are invalid")
    if len(raw_notices) != len(NOTICE_SPECS):
        raise BSECurrentDelistingBlockedError("manifest notice set is incomplete")
    notices = tuple(
        _notice_evidence_from_dict(item, spec=spec, cas=cas)
        for item, spec in zip(raw_notices, NOTICE_SPECS, strict=True)
    )
    pages = tuple(_catalogue_evidence_from_dict(item, cas=cas) for item in raw_pages)
    closure = _catalogue_evidence_from_dict(
        value["catalogue_closure_probe"], cas=cas
    )
    artifact = _build_artifact(notices=notices, pages=pages, closure=closure)
    if artifact.to_manifest_dict() != value:
        raise BSECurrentDelistingBlockedError(
            "manifest aggregate does not match raw CAS recomputation"
        )
    return artifact


class BSECurrentDelistingManifestStore:
    def __init__(self, cas: BSECurrentDelistingCAS) -> None:
        if not isinstance(cas, BSECurrentDelistingCAS):
            raise TypeError("cas must be BSECurrentDelistingCAS")
        self.cas = cas

    def seal(self, artifact: BSECurrentDelistingArtifact) -> ManifestReference:
        payload = artifact.to_manifest_dict()
        rebuilt = _rebuild_from_manifest_payload(payload, cas=self.cas)
        if rebuilt.to_manifest_dict() != payload:
            raise BSECurrentDelistingBlockedError(
                "BSE current-delisting artifact is not reproducible"
            )
        return self.cas.write_manifest(_canonical_json_bytes(payload))

    def replay(self, manifest_sha256: str) -> BSECurrentDelistingArtifact:
        content = self.cas.read_manifest(manifest_sha256)
        value = _strict_json(content, "BSE current-delisting manifest")
        if not isinstance(value, dict) or _canonical_json_bytes(value) != content:
            raise BSECurrentDelistingBlockedError("manifest is not canonical JSON")
        artifact = _rebuild_from_manifest_payload(value, cas=self.cas)
        if artifact.to_manifest_dict() != value:
            raise BSECurrentDelistingBlockedError(
                "manifest does not exactly replay"
            )
        return artifact


class BSECurrentDelistingClient:
    """Read-only collector for two fixed notices and the full current catalogue."""

    def __init__(
        self,
        *,
        cas: BSECurrentDelistingCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cas, BSECurrentDelistingCAS):
            raise TypeError("cas must be BSECurrentDelistingCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now().astimezone())

    def _observed_at(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise BSECurrentDelistingBlockedError("collector clock must return datetime")
        return _normalize_timestamp(value)

    def _fetch_notice(self, spec: BSEDelistingNoticeSpec) -> NoticeEvidence:
        _validate_notice_url(spec)
        headers = {
            "Accept": PDF_MEDIA_TYPE,
            "Referer": BSE_CURRENT_CATALOGUE_REFERER,
            "User-Agent": USER_AGENT,
        }
        try:
            first = self.session.get(
                spec.source_url,
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise BSECurrentDelistingBlockedError(
                f"BSE notice request failed for {spec.code_alias}"
            ) from exc
        _assert_same_url_response(first, spec.source_url, "BSE notice")
        first_status = _strict_int(first.status_code, "BSE notice HTTP status")
        attempts: list[TransportAttempt] = []
        if first_status == 302:
            cookie_value, _header_hash = _parse_c3vk_challenge(first, spec.source_url)
            attempts.append(
                _build_transport_attempt(
                    cas=self.cas,
                    response=first,
                    attempt=1,
                    request_url=spec.source_url,
                    retrieved_at=self._observed_at(),
                    sent_cookie_value=None,
                    challenge_cookie_value=cookie_value,
                )
            )
            retry_headers = dict(headers)
            retry_headers["Cookie"] = f"C3VK={cookie_value}"
            try:
                final = self.session.get(
                    spec.source_url,
                    headers=retry_headers,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise BSECurrentDelistingBlockedError(
                    f"BSE C3VK retry failed for {spec.code_alias}"
                ) from exc
            _assert_same_url_response(final, spec.source_url, "BSE notice retry")
            if _strict_int(final.status_code, "BSE notice retry status") != 200:
                raise BSECurrentDelistingBlockedError(
                    "BSE notice challenge used more than one retry or did not resolve"
                )
            if final.headers.get("Location") is not None:
                raise BSECurrentDelistingBlockedError(
                    "BSE notice retry returned another redirect"
                )
            if final.headers.get("Set-Cookie") is not None:
                raise BSECurrentDelistingBlockedError(
                    "BSE notice retry unexpectedly rotated its cookie"
                )
            attempts.append(
                _build_transport_attempt(
                    cas=self.cas,
                    response=final,
                    attempt=2,
                    request_url=spec.source_url,
                    retrieved_at=self._observed_at(),
                    sent_cookie_value=cookie_value,
                    challenge_cookie_value=None,
                )
            )
        elif first_status == 200:
            if first.headers.get("Location") is not None or first.headers.get(
                "Set-Cookie"
            ) is not None:
                raise BSECurrentDelistingBlockedError(
                    "direct BSE notice response has unexpected redirect/cookie headers"
                )
            attempts.append(
                _build_transport_attempt(
                    cas=self.cas,
                    response=first,
                    attempt=1,
                    request_url=spec.source_url,
                    retrieved_at=self._observed_at(),
                    sent_cookie_value=None,
                    challenge_cookie_value=None,
                )
            )
        else:
            raise BSECurrentDelistingBlockedError(
                f"BSE notice HTTP status is {first_status}"
            )
        if attempts[-1].content_type != PDF_MEDIA_TYPE:
            raise BSECurrentDelistingBlockedError(
                "BSE notice final Content-Type is not application/pdf"
            )
        raw_pdf = _validate_transport_attempts(attempts, spec=spec, cas=self.cas)
        parsed = parse_notice_pdf(raw_pdf, spec=spec)
        return NoticeEvidence(
            code_alias=parsed["code_alias"],
            legal_name=parsed["legal_name"],
            publication_date=parsed["publication_date"],
            effective_date=parsed["effective_date"],
            announcement_number=parsed["announcement_number"],
            event_type=parsed["event_type"],
            source_url=parsed["source_url"],
            final_pdf=attempts[-1].body,
            transport_attempts=tuple(attempts),
            extraction_engine=parsed["extraction_engine"],
            extraction_engine_version=parsed["extraction_engine_version"],
            page_count=parsed["page_count"],
            normalized_text_sha256=parsed["normalized_text_sha256"],
            matched_markers=tuple(parsed["matched_markers"]),
        )

    def _fetch_catalogue_page(
        self,
        page_number: int,
    ) -> tuple[CataloguePageEvidence, ParsedCataloguePage]:
        form_fields = _catalogue_form_fields(page_number)
        try:
            response = self.session.post(
                BSE_CURRENT_CATALOGUE_URL,
                data=list(form_fields),
                headers={
                    "Accept": "text/html",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": BSE_CURRENT_CATALOGUE_REFERER,
                    "User-Agent": USER_AGENT,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise BSECurrentDelistingBlockedError(
                f"BSE current catalogue page {page_number} request failed"
            ) from exc
        _assert_same_url_response(
            response, BSE_CURRENT_CATALOGUE_URL, "BSE current catalogue"
        )
        if (
            _strict_int(response.status_code, "catalogue HTTP status") != 200
            or response.headers.get("Location") is not None
        ):
            raise BSECurrentDelistingBlockedError(
                f"BSE current catalogue page {page_number} transport changed"
            )
        content_type = _catalogue_content_type(response.headers.get("Content-Type"))
        raw = _response_content(response)
        parsed = parse_current_catalogue_page(raw, request_page=page_number)
        reference = self.cas.put_blob(raw)
        request_body_sha256 = _sha256(urlencode(form_fields).encode("ascii"))
        evidence = CataloguePageEvidence(
            page_number=page_number,
            method="POST",
            request_url=BSE_CURRENT_CATALOGUE_URL,
            form_fields=form_fields,
            request_body_sha256=request_body_sha256,
            retrieved_at=self._observed_at(),
            status_code=200,
            response_url=BSE_CURRENT_CATALOGUE_URL,
            location=None,
            content_type=content_type,
            raw_response=reference,
            response_summary=parsed.summary(),
        )
        return evidence, parsed

    def fetch_current(self) -> BSECurrentDelistingArtifact:
        notices = tuple(self._fetch_notice(spec) for spec in NOTICE_SPECS)
        first = self._fetch_catalogue_page(0)
        pages: list[tuple[CataloguePageEvidence, ParsedCataloguePage]] = [first]
        for page_number in range(1, first[1].total_pages):
            pages.append(self._fetch_catalogue_page(page_number))
        closure = self._fetch_catalogue_page(0)
        artifact = _build_artifact(notices=notices, pages=pages, closure=closure)
        validate_current_delisting_freshness(artifact, now=self._clock())
        return artifact


def validate_current_delisting_freshness(
    artifact: BSECurrentDelistingArtifact,
    *,
    now: datetime | str | None = None,
    as_of: datetime | str | None = None,
    maximum_age: timedelta = MAX_CURRENT_EVIDENCE_AGE,
) -> None:
    if not isinstance(artifact, BSECurrentDelistingArtifact):
        raise TypeError("artifact must be BSECurrentDelistingArtifact")
    if maximum_age <= timedelta(0):
        raise ValueError("maximum_age must be positive")
    current = datetime.fromisoformat(_normalize_timestamp(now))
    decision = (
        datetime.fromisoformat(_normalize_timestamp(as_of))
        if as_of is not None
        else current
    )
    if decision > current + MAX_FUTURE_CLOCK_SKEW:
        raise BSECurrentDelistingBlockedError(
            "BSE current-delisting as_of is future-dated"
        )
    capture_times = _all_capture_times(artifact)
    if not capture_times:
        raise BSECurrentDelistingBlockedError("artifact has no capture timestamps")
    earliest = min(capture_times)
    latest = max(capture_times)
    if latest - earliest > MAX_CAPTURE_SPAN:
        raise BSECurrentDelistingBlockedError(
            "BSE current-delisting capture span is too wide"
        )
    if latest > current + MAX_FUTURE_CLOCK_SKEW:
        raise BSECurrentDelistingBlockedError(
            "BSE current-delisting evidence is future-dated"
        )
    if current - earliest > maximum_age:
        raise BSECurrentDelistingBlockedError(
            "BSE current-delisting evidence is stale", status=SOURCE_STALE
        )
    if latest > decision + MAX_FUTURE_CLOCK_SKEW:
        raise BSECurrentDelistingBlockedError(
            "BSE current-delisting evidence was captured after as_of"
        )
    if decision - earliest > maximum_age:
        raise BSECurrentDelistingBlockedError(
            "BSE current-delisting evidence is stale for as_of", status=SOURCE_STALE
        )
    expected_retrieved = latest.replace(microsecond=0).isoformat()
    if artifact.retrieved_at != expected_retrieved:
        raise BSECurrentDelistingBlockedError(
            "BSE current-delisting artifact was independently re-dated"
        )


__all__ = [
    "BSE_CURRENT_CATALOGUE_URL",
    "BSE_CURRENT_CATALOGUE_PAGE_URL",
    "BSE_CATALOGUE_MINIMUM_ROWS",
    "BSECurrentDelistingArtifact",
    "BSECurrentDelistingBlockedError",
    "BSECurrentDelistingCAS",
    "BSECurrentDelistingClient",
    "BSECurrentDelistingManifestStore",
    "BSEDelistingNoticeSpec",
    "BlobReference",
    "CataloguePageEvidence",
    "DelistingEvent",
    "EVIDENCE_COMPLETE",
    "ManifestReference",
    "NOTICE_SPECS",
    "NoticeEvidence",
    "PROTOCOL_VERSION",
    "SOURCE_CONTRACT_ADMITTED",
    "SOURCE_REJECTED",
    "SOURCE_STALE",
    "TransportAttempt",
    "parse_current_catalogue_page",
    "parse_notice_pdf",
    "validate_current_delisting_freshness",
]
