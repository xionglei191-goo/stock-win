from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import requests


PROTOCOL_VERSION = "szse-code-change-events-v1"
SOURCE_CONTRACT_ADMITTED = "SOURCE_CONTRACT_ADMITTED"
SOURCE_CONTRACT_UNADMITTED = "SOURCE_CONTRACT_UNADMITTED"

SZSE_DISCLOSURE_HOST = "disc.static.szse.cn"
PRIMARY_DISCLOSURE_URL = (
    "https://disc.static.szse.cn/download/disc/disk03/finalpage/"
    "2026-05-11/7c817bfa-0047-40ef-8fd9-cc6e879a709b.PDF"
)
SUPPORTING_DISCLOSURE_URL = (
    "https://disc.static.szse.cn/disc/disk03/finalpage/"
    "2025-01-23/446cadc3-e9af-4adb-912c-9f25d17a4607.PDF"
)
ALLOWED_DISCLOSURE_URLS = frozenset(
    {PRIMARY_DISCLOSURE_URL, SUPPORTING_DISCLOSURE_URL}
)

CANONICAL_ENTITY_ID = "CN:SZSE:300114"
OLD_CODE = "300114.SZ"
NEW_CODE = "302132.SZ"
OLD_NAME = "\u4e2d\u822a\u7535\u6d4b"
NEW_NAME = "\u4e2d\u822a\u6210\u98de"
EFFECTIVE_DATE = "2025-02-17"

MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_EXTRACTED_TEXT_CHARS = 20_000_000


class SZSECodeChangeBlockedError(RuntimeError):
    """The disclosure or its derived event cannot satisfy the frozen contract."""

    def __init__(self, message: str, *, status: str = "SOURCE_REJECTED") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RawPDFEvidence:
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
class PDFTextEvidence:
    engine: str
    engine_version: str
    page_count: int | None
    text_sha256: str
    raw_pdf_sha256: str
    recomputed_from_raw: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SZSECodeAliasInterval:
    canonical_entity_id: str
    exchange: str
    code_alias: str
    name: str
    valid_from: str | None
    valid_to: str | None
    effective_at: str
    event_type: str
    source_url: str
    source_hash: str
    retrieved_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SZSECodeChangeArtifact:
    ready: bool
    status: str
    detail: str
    raw_evidence: RawPDFEvidence
    text_evidence: PDFTextEvidence | None
    intervals: tuple[SZSECodeAliasInterval, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "ready": self.ready,
            "status": self.status,
            "detail": self.detail,
            "raw_evidence": self.raw_evidence.to_dict(),
            "text_evidence": (
                self.text_evidence.to_dict() if self.text_evidence else None
            ),
            "intervals": [item.to_dict() for item in self.intervals],
            "promotion_allowed": self.ready,
        }


@dataclass(frozen=True)
class _ExtractedText:
    text: str
    engine: str
    engine_version: str
    page_count: int


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalized_retrieved_at(value: str | None) -> str:
    text = value or datetime.now().astimezone().isoformat()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SZSECodeChangeBlockedError(
            f"retrieved_at is not ISO-8601: {text}"
        ) from exc
    if parsed.tzinfo is None:
        raise SZSECodeChangeBlockedError("retrieved_at must include a timezone")
    return parsed.isoformat()


def _iso_date(value: str, *, field_name: str) -> str:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SZSECodeChangeBlockedError(
            f"invalid {field_name}: {value!r}"
        ) from exc


def _validate_source_url(source_url: str, *, primary_only: bool = False) -> str:
    value = str(source_url or "")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SZSE_DISCLOSURE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise SZSECodeChangeBlockedError("disclosure URL is not an admitted SZSE URL")
    allowed = (
        frozenset({PRIMARY_DISCLOSURE_URL})
        if primary_only
        else ALLOWED_DISCLOSURE_URLS
    )
    if value not in allowed:
        raise SZSECodeChangeBlockedError("disclosure URL is not frozen in this protocol")
    return value


def _validate_raw_pdf(raw_pdf: bytes, expected_sha256: str | None = None) -> str:
    if not raw_pdf or len(raw_pdf) > MAX_PDF_BYTES:
        raise SZSECodeChangeBlockedError("SZSE disclosure PDF is empty or oversized")
    if not raw_pdf.startswith(b"%PDF-") or b"%%EOF" not in raw_pdf[-2048:]:
        raise SZSECodeChangeBlockedError("SZSE disclosure is not a strict PDF payload")
    digest = _sha256(raw_pdf)
    if expected_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise SZSECodeChangeBlockedError("expected PDF hash is not SHA-256")
        if digest != expected_sha256:
            raise SZSECodeChangeBlockedError("SZSE disclosure PDF hash mismatch")
    return digest


def _atomic_write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise SZSECodeChangeBlockedError(
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
                raise SZSECodeChangeBlockedError(
                    f"content-address collision or corruption: {path}"
                )
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class SZSEDisclosureCAS:
    """Immutable content-addressed storage for exact official PDF bytes."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def capture(
        self,
        raw_pdf: bytes,
        *,
        source_url: str,
        retrieved_at: str,
        content_type: str = "application/pdf",
        expected_sha256: str | None = None,
    ) -> RawPDFEvidence:
        source = _validate_source_url(source_url)
        retrieved = _normalized_retrieved_at(retrieved_at)
        media_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if media_type != "application/pdf":
            raise SZSECodeChangeBlockedError(
                f"unexpected SZSE disclosure content type: {content_type!r}"
            )
        digest = _validate_raw_pdf(raw_pdf, expected_sha256)
        path = self.root / "sha256" / digest[:2] / digest
        _atomic_write_exact(path, raw_pdf)
        if not path.is_file() or _sha256(path.read_bytes()) != digest:
            raise SZSECodeChangeBlockedError("SZSE disclosure CAS verification failed")
        return RawPDFEvidence(
            source_url=source,
            method="GET",
            retrieved_at=retrieved,
            content_sha256=digest,
            byte_count=len(raw_pdf),
            content_type=media_type,
            cas_uri=f"sha256:{digest}",
            object_path=str(path.resolve()),
        )


def _verify_raw_evidence(raw_pdf: bytes, evidence: RawPDFEvidence) -> str:
    source = _validate_source_url(evidence.source_url, primary_only=True)
    if evidence.method != "GET":
        raise SZSECodeChangeBlockedError("SZSE disclosure evidence is not GET-only")
    _normalized_retrieved_at(evidence.retrieved_at)
    digest = _validate_raw_pdf(raw_pdf, evidence.content_sha256)
    if evidence.byte_count != len(raw_pdf):
        raise SZSECodeChangeBlockedError("SZSE disclosure evidence byte count mismatch")
    if evidence.content_type != "application/pdf":
        raise SZSECodeChangeBlockedError("SZSE disclosure evidence content type mismatch")
    if evidence.cas_uri != f"sha256:{digest}":
        raise SZSECodeChangeBlockedError("SZSE disclosure evidence CAS URI mismatch")
    path = Path(evidence.object_path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise SZSECodeChangeBlockedError("SZSE disclosure CAS object is unavailable")
    persisted = path.read_bytes()
    if persisted != raw_pdf or _sha256(persisted) != digest:
        raise SZSECodeChangeBlockedError("SZSE disclosure CAS object was tampered")
    return source


def _extract_text_from_pdf(raw_pdf: bytes) -> _ExtractedText:
    try:
        import pypdf
        from pypdf import PdfReader
    except ImportError as exc:
        raise SZSECodeChangeBlockedError(
            "pypdf is unavailable; raw PDF text cannot be recomputed",
            status=SOURCE_CONTRACT_UNADMITTED,
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw_pdf), strict=True)
        if reader.is_encrypted:
            raise SZSECodeChangeBlockedError(
                "encrypted SZSE disclosure PDF is not admitted",
                status=SOURCE_CONTRACT_UNADMITTED,
            )
        page_count = len(reader.pages)
        if page_count <= 0 or page_count > MAX_PDF_PAGES:
            raise SZSECodeChangeBlockedError(
                "SZSE disclosure PDF page count is invalid",
                status=SOURCE_CONTRACT_UNADMITTED,
            )
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text is None:
                raise SZSECodeChangeBlockedError(
                    "a SZSE disclosure PDF page has no reproducible text layer",
                    status=SOURCE_CONTRACT_UNADMITTED,
                )
            pages.append(text)
    except SZSECodeChangeBlockedError:
        raise
    except Exception as exc:
        raise SZSECodeChangeBlockedError(
            "raw SZSE disclosure PDF text extraction failed closed",
            status=SOURCE_CONTRACT_UNADMITTED,
        ) from exc
    combined = "\n\f\n".join(pages)
    if not combined.strip() or len(combined) > MAX_EXTRACTED_TEXT_CHARS:
        raise SZSECodeChangeBlockedError(
            "SZSE disclosure extracted text is empty or oversized",
            status=SOURCE_CONTRACT_UNADMITTED,
        )
    return _ExtractedText(
        text=combined,
        engine="pypdf",
        engine_version=str(getattr(pypdf, "__version__", "UNKNOWN")),
        page_count=page_count,
    )


def _compact_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.replace("\x00", "")
    return re.sub(r"[\s\u200b\ufeff]+", "", normalized)


def _linked_change(text: str, old: str, new: str) -> bool:
    change = r"(?:变更|更改|调整|更名)"
    before_after = r"(?:原|变更前).{0,80}" + re.escape(old)
    before_after += r".{0,160}(?:新|变更后).{0,80}" + re.escape(new)
    return bool(
        re.search(re.escape(old) + r".{0,160}" + change + r".{0,160}" + re.escape(new), text)
        or re.search(change + r".{0,160}" + re.escape(old) + r".{0,160}" + re.escape(new), text)
        or re.search(before_after, text)
    )


def _validate_event_text(text: str) -> None:
    compact = _compact_text(text)
    if len(compact) < 40:
        raise SZSECodeChangeBlockedError("SZSE disclosure text is implausibly short")
    required = (OLD_NAME, NEW_NAME, "300114", "302132")
    missing = [token for token in required if token not in compact]
    if missing:
        raise SZSECodeChangeBlockedError(
            "SZSE disclosure is missing frozen identity facts: " + ", ".join(missing)
        )
    date_patterns = (
        r"2025年0?2月0?17日",
        r"2025[-/.]0?2[-/.]0?17",
    )
    if not any(re.search(pattern, compact) for pattern in date_patterns):
        raise SZSECodeChangeBlockedError(
            "SZSE disclosure is missing the frozen effective date"
        )
    if "证券代码" not in compact and "股票代码" not in compact:
        raise SZSECodeChangeBlockedError("SZSE disclosure lacks code-change semantics")
    if "证券简称" not in compact and "股票简称" not in compact:
        raise SZSECodeChangeBlockedError("SZSE disclosure lacks name-change semantics")
    if not _linked_change(compact, "300114", "302132"):
        raise SZSECodeChangeBlockedError(
            "SZSE disclosure does not link the old and new security codes"
        )
    if not _linked_change(compact, OLD_NAME, NEW_NAME):
        raise SZSECodeChangeBlockedError(
            "SZSE disclosure does not link the old and new security names"
        )


def _event_intervals(
    *, source_url: str, source_hash: str, retrieved_at: str
) -> tuple[SZSECodeAliasInterval, SZSECodeAliasInterval]:
    intervals = (
        SZSECodeAliasInterval(
            canonical_entity_id=CANONICAL_ENTITY_ID,
            exchange="SZSE",
            code_alias=OLD_CODE,
            name=OLD_NAME,
            # The event proves the upper bound only.  A listing source must
            # supply the old alias's lower bound during master integration.
            valid_from=None,
            valid_to=EFFECTIVE_DATE,
            effective_at=EFFECTIVE_DATE,
            event_type="SECURITY_CODE_CHANGE_OUT",
            source_url=source_url,
            source_hash=source_hash,
            retrieved_at=retrieved_at,
        ),
        SZSECodeAliasInterval(
            canonical_entity_id=CANONICAL_ENTITY_ID,
            exchange="SZSE",
            code_alias=NEW_CODE,
            name=NEW_NAME,
            valid_from=EFFECTIVE_DATE,
            valid_to=None,
            effective_at=EFFECTIVE_DATE,
            event_type="SECURITY_CODE_CHANGE_IN",
            source_url=source_url,
            source_hash=source_hash,
            retrieved_at=retrieved_at,
        ),
    )
    validate_alias_intervals(intervals)
    return intervals


def validate_alias_intervals(intervals: Sequence[SZSECodeAliasInterval]) -> None:
    values = tuple(intervals)
    if len(values) != 2:
        raise SZSECodeChangeBlockedError("code-change event must have two intervals")
    by_code = {item.code_alias: item for item in values}
    if set(by_code) != {OLD_CODE, NEW_CODE}:
        raise SZSECodeChangeBlockedError("code-change aliases do not match the protocol")
    old = by_code[OLD_CODE]
    new = by_code[NEW_CODE]
    if len({item.canonical_entity_id for item in values}) != 1:
        raise SZSECodeChangeBlockedError("code-change aliases do not share one entity")
    if old.canonical_entity_id != CANONICAL_ENTITY_ID:
        raise SZSECodeChangeBlockedError("code-change canonical entity is invalid")
    if any(item.exchange != "SZSE" for item in values):
        raise SZSECodeChangeBlockedError("code-change exchange is invalid")
    if old.name != OLD_NAME or new.name != NEW_NAME:
        raise SZSECodeChangeBlockedError("code-change security names are invalid")
    if old.event_type != "SECURITY_CODE_CHANGE_OUT" or new.event_type != (
        "SECURITY_CODE_CHANGE_IN"
    ):
        raise SZSECodeChangeBlockedError("code-change event directions are invalid")
    if old.valid_from is not None:
        raise SZSECodeChangeBlockedError(
            "event source must not backfill the old alias lower bound"
        )
    if new.valid_to is not None:
        raise SZSECodeChangeBlockedError("new code interval must remain open")
    old_end = _iso_date(old.valid_to or "", field_name="old valid_to")
    new_start = _iso_date(new.valid_from or "", field_name="new valid_from")
    if old_end > new_start:
        raise SZSECodeChangeBlockedError("code-change alias intervals overlap")
    if old_end < new_start:
        raise SZSECodeChangeBlockedError("code-change alias intervals contain a gap")
    if old_end != EFFECTIVE_DATE or new_start != EFFECTIVE_DATE:
        raise SZSECodeChangeBlockedError("code-change boundary is not atomic")
    if len({item.source_url for item in values}) != 1 or len(
        {item.source_hash for item in values}
    ) != 1 or len({item.retrieved_at for item in values}) != 1:
        raise SZSECodeChangeBlockedError(
            "code-change intervals do not share one evidence object"
        )
    for item in values:
        if item.effective_at != EFFECTIVE_DATE:
            raise SZSECodeChangeBlockedError("interval effective date is inconsistent")
        _validate_source_url(item.source_url, primary_only=True)
        if not re.fullmatch(r"[0-9a-f]{64}", item.source_hash):
            raise SZSECodeChangeBlockedError("interval source hash is not SHA-256")
        _normalized_retrieved_at(item.retrieved_at)


def parse_szse_code_change_pdf(
    raw_pdf: bytes,
    *,
    raw_evidence: RawPDFEvidence,
    extracted_text: str | None = None,
    extracted_text_sha256: str | None = None,
    extracted_from_raw_sha256: str | None = None,
) -> SZSECodeChangeArtifact:
    """Derive the frozen alias event, admitting it only from recomputed PDF text.

    Caller-supplied text is useful for audit/debugging, but it can never by
    itself promote this source.  It must be hash-linked to the exact raw PDF;
    when the raw text layer cannot be recomputed, the artifact remains
    ``SOURCE_CONTRACT_UNADMITTED``.
    """

    source_url = _verify_raw_evidence(raw_pdf, raw_evidence)
    raw_hash = raw_evidence.content_sha256
    supplied_text: str | None = None
    if extracted_text is not None:
        if extracted_from_raw_sha256 != raw_hash:
            raise SZSECodeChangeBlockedError(
                "supplied text is not hash-linked to the raw PDF"
            )
        supplied_digest = _sha256(extracted_text.encode("utf-8"))
        if extracted_text_sha256 != supplied_digest:
            raise SZSECodeChangeBlockedError("supplied extracted-text hash mismatch")
        supplied_text = extracted_text

    try:
        extracted = _extract_text_from_pdf(raw_pdf)
    except SZSECodeChangeBlockedError as exc:
        if exc.status != SOURCE_CONTRACT_UNADMITTED:
            raise
        if supplied_text is None:
            return SZSECodeChangeArtifact(
                ready=False,
                status=SOURCE_CONTRACT_UNADMITTED,
                detail=str(exc),
                raw_evidence=raw_evidence,
                text_evidence=None,
                intervals=(),
            )
        _validate_event_text(supplied_text)
        intervals = _event_intervals(
            source_url=source_url,
            source_hash=raw_hash,
            retrieved_at=raw_evidence.retrieved_at,
        )
        return SZSECodeChangeArtifact(
            ready=False,
            status=SOURCE_CONTRACT_UNADMITTED,
            detail=(
                "event facts validate in caller-supplied text, but the text "
                "could not be independently recomputed from the raw PDF"
            ),
            raw_evidence=raw_evidence,
            text_evidence=PDFTextEvidence(
                engine="CALLER_SUPPLIED",
                engine_version="UNATTESTED",
                page_count=None,
                text_sha256=_sha256(supplied_text.encode("utf-8")),
                raw_pdf_sha256=raw_hash,
                recomputed_from_raw=False,
            ),
            intervals=intervals,
        )

    if supplied_text is not None and _compact_text(supplied_text) != _compact_text(
        extracted.text
    ):
        raise SZSECodeChangeBlockedError(
            "supplied text differs from text recomputed from the raw PDF"
        )
    _validate_event_text(extracted.text)
    intervals = _event_intervals(
        source_url=source_url,
        source_hash=raw_hash,
        retrieved_at=raw_evidence.retrieved_at,
    )
    return SZSECodeChangeArtifact(
        ready=True,
        status=SOURCE_CONTRACT_ADMITTED,
        detail="official PDF text was recomputed and the frozen alias event validated",
        raw_evidence=raw_evidence,
        text_evidence=PDFTextEvidence(
            engine=extracted.engine,
            engine_version=extracted.engine_version,
            page_count=extracted.page_count,
            text_sha256=_sha256(extracted.text.encode("utf-8")),
            raw_pdf_sha256=raw_hash,
            recomputed_from_raw=True,
        ),
        intervals=intervals,
    )


class SZSECodeChangeClient:
    """GET-only client for the one frozen, official primary disclosure."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        cas: SZSEDisclosureCAS,
    ) -> None:
        self.session = session or requests.Session()
        self.cas = cas

    def fetch_primary(
        self,
        *,
        retrieved_at: str | None = None,
        expected_sha256: str | None = None,
    ) -> SZSECodeChangeArtifact:
        retrieved = _normalized_retrieved_at(retrieved_at)
        response = self.session.get(
            PRIMARY_DISCLOSURE_URL,
            headers={
                "Accept": "application/pdf",
                "User-Agent": "research-platform-security-master/1.0",
            },
            timeout=(5, 60),
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise SZSECodeChangeBlockedError(
                f"SZSE disclosure HTTP status is {response.status_code}"
            )
        if str(response.url) != PRIMARY_DISCLOSURE_URL:
            raise SZSECodeChangeBlockedError("SZSE disclosure redirected or changed URL")
        history = getattr(response, "history", ())
        if history:
            raise SZSECodeChangeBlockedError("SZSE disclosure redirect history is non-empty")
        evidence = self.cas.capture(
            bytes(response.content),
            source_url=PRIMARY_DISCLOSURE_URL,
            retrieved_at=retrieved,
            content_type=str(response.headers.get("Content-Type", "")),
            expected_sha256=expected_sha256,
        )
        return parse_szse_code_change_pdf(
            bytes(response.content), raw_evidence=evidence
        )
