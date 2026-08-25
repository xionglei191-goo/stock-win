from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests


PROTOCOL_VERSION = "sse-risk-warning-transition-audit-evidence-v1"
SOURCE_STATUS = "AUDIT_ONLY_NOT_INTEGRATED"
SOURCE_SCOPE = "SSE_SINGLE_FROZEN_TRANSITION"

SSE_INDEX_HOST = "www.sse.com.cn"
SSE_PDF_HOST = "static.sse.com.cn"
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_CHALLENGE_BYTES = 64 * 1024
MAX_PDF_PAGES = 20
MAX_PDF_TEXT_CHARS = 500_000
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_ACW_POSITION_LIST = (
    0x0F,
    0x23,
    0x1D,
    0x18,
    0x21,
    0x10,
    0x01,
    0x26,
    0x0A,
    0x09,
    0x13,
    0x1F,
    0x28,
    0x1B,
    0x16,
    0x17,
    0x19,
    0x0D,
    0x06,
    0x0B,
    0x27,
    0x12,
    0x14,
    0x08,
    0x0E,
    0x15,
    0x20,
    0x1A,
    0x02,
    0x1E,
    0x07,
    0x04,
    0x11,
    0x05,
    0x03,
    0x1C,
    0x22,
    0x25,
    0x0C,
    0x24,
)
_ACW_MASK = "3000176000856006061501533003690027800375"
_ACW_MASK_BASE64 = "MzAwMDE3NjAwMDg1NjAwNjA2MTUwMTUzMzAwMzY5MDAyNzgwMDM3NQ=="


class SSERiskWarningTransitionBlockedError(RuntimeError):
    """Frozen SSE transition evidence changed, conflicted, or failed replay."""

    def __init__(self, message: str, *, status: str = "SOURCE_REJECTED") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SSERiskWarningTransitionSpec:
    code: str
    legal_name: str
    old_name: str
    new_name: str
    risk_started_at: str
    suspension_date: str
    effective_date: str
    publication_date: str
    announcement_number: str
    announcement_title: str
    index_url: str
    pdf_url: str
    expected_index_sha256: str
    expected_pdf_sha256: str


FROZEN_TRANSITION = SSERiskWarningTransitionSpec(
    code="688646",
    legal_name="武汉逸飞激光股份有限公司",
    old_name="ST逸飞",
    new_name="逸飞激光",
    risk_started_at="2025-05-06",
    suspension_date="2026-08-12",
    effective_date="2026-08-13",
    publication_date="2026-08-12",
    announcement_number="2026-038",
    announcement_title="逸飞激光关于撤销其他风险警示暨停牌的公告",
    index_url="https://www.sse.com.cn/js/common/stocks/new/688646.js",
    pdf_url=(
        "https://static.sse.com.cn/disclosure/listedinfo/announcement/c/new/"
        "2026-08-12/688646_20260812_HF3O.pdf"
    ),
    expected_index_sha256=(
        "3eab4e4fc7b142baa36bcdf3c79caa299c020ecd45f1e09000798c7e3ab9c9e2"
    ),
    expected_pdf_sha256=(
        "cedfa804b3ff2abe114a1750b7f51e4914eb9dcef99ace3f4b0f86a6697ad7fb"
    ),
)


@dataclass(frozen=True)
class BlobReference:
    sha256: str
    byte_count: int
    cas_uri: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawEvidence:
    source_id: str
    method: str
    request_url: str
    response_url: str
    retrieved_at: str
    http_status: int
    content_type: str
    body: BlobReference

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "method": self.method,
            "request_url": self.request_url,
            "response_url": self.response_url,
            "retrieved_at": self.retrieved_at,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "body": self.body.to_dict(),
        }


@dataclass(frozen=True)
class RiskWarningTransition:
    code_alias: str
    legal_name: str
    old_name: str
    new_name: str
    risk_started_at: str
    suspension_date: str
    effective_date: str
    publication_date: str
    announcement_number: str
    announcement_title: str
    index_url: str
    pdf_url: str
    index_sha256: str
    pdf_sha256: str
    index_entry_sha256: str
    extraction_engine: str
    extraction_engine_version: str
    page_count: int
    normalized_text_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SSERiskWarningTransitionArtifact:
    retrieved_at: str
    transition: RiskWarningTransition
    raw_evidence: tuple[RawEvidence, RawEvidence]
    logical_content_sha256: str

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": False,
            "status": SOURCE_STATUS,
            "source_scope": SOURCE_SCOPE,
            "allowed_use": "AUDIT_EVIDENCE_FOR_FUTURE_SECURITY_MASTER_INTEGRATION",
            "fixed_source_count": 2,
            "method": "GET",
            "redirects_allowed": False,
            "caller_summary_trusted": False,
            "caller_ready_attestation_allowed": False,
            "historical_master_integration_allowed": False,
            "training_allowed": False,
            "label_generation_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
        }

    @property
    def statistics(self) -> dict[str, Any]:
        return {
            "transition_count": 1,
            "raw_source_count": 2,
            "pdf_page_count": self.transition.page_count,
            "ready_transition_count": 0,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "retrieved_at": self.retrieved_at,
            "transition": self.transition.to_dict(),
            "raw_evidence": [item.to_dict() for item in self.raw_evidence],
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
            "statistics": self.statistics,
        }


@dataclass(frozen=True)
class SSERiskWarningTransitionManifestReference:
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


class SSERiskWarningTransitionCAS:
    """Immutable exact-byte CAS with reparse-point and TOCTOU protection."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()

    def object_path(self, digest: str) -> Path:
        normalized = _valid_digest(digest, "CAS")
        return self.root / "sha256" / normalized[:2] / normalized

    def put_blob(self, content: bytes) -> BlobReference:
        payload = bytes(content)
        if not payload:
            raise SSERiskWarningTransitionBlockedError("refusing to store empty CAS data")
        digest = _sha256(payload)
        path = self.object_path(digest)
        _atomic_write_exact(self.root, path, payload)
        replayed = _stable_read(self.root, path, "SSE transition CAS object")
        if replayed != payload or _sha256(replayed) != digest:
            raise SSERiskWarningTransitionBlockedError("CAS write verification failed")
        return BlobReference(
            sha256=digest,
            byte_count=len(payload),
            cas_uri=f"sha256:{digest}",
            object_path=str(path.resolve()),
        )

    def read_blob(self, reference: BlobReference | str) -> bytes:
        if isinstance(reference, BlobReference):
            digest = _valid_digest(reference.sha256, "CAS reference")
            path = self.object_path(digest)
            if (
                reference.byte_count <= 0
                or reference.cas_uri != f"sha256:{digest}"
                or Path(reference.object_path).resolve() != path.resolve()
            ):
                raise SSERiskWarningTransitionBlockedError(
                    "CAS reference metadata is inconsistent"
                )
            expected_size = reference.byte_count
        else:
            digest = _valid_digest(reference, "CAS")
            path = self.object_path(digest)
            expected_size = None
        content = _stable_read(self.root, path, "SSE transition CAS object")
        if _sha256(content) != digest or (
            expected_size is not None and len(content) != expected_size
        ):
            raise SSERiskWarningTransitionBlockedError("CAS object hash or size mismatch")
        return content


class SSERiskWarningTransitionManifestStore:
    """Seal and cold-replay without trusting derived transition fields."""

    def __init__(self, cas: SSERiskWarningTransitionCAS) -> None:
        if not isinstance(cas, SSERiskWarningTransitionCAS):
            raise TypeError("cas must be an SSERiskWarningTransitionCAS")
        self.cas = cas

    def seal(
        self, artifact: SSERiskWarningTransitionArtifact
    ) -> SSERiskWarningTransitionManifestReference:
        payload = _manifest_payload(artifact)
        rebuilt = self._rebuild(payload)
        content = _canonical_json_bytes(payload)
        if _canonical_json_bytes(_manifest_payload(rebuilt)) != content:
            raise SSERiskWarningTransitionBlockedError(
                "transition artifact does not reproduce from raw evidence"
            )
        reference = self.cas.put_blob(content)
        return SSERiskWarningTransitionManifestReference(
            manifest_sha256=reference.sha256,
            byte_count=reference.byte_count,
            cas_uri=reference.cas_uri,
            object_path=reference.object_path,
        )

    def replay(self, manifest_sha256: str) -> SSERiskWarningTransitionArtifact:
        content = self.cas.read_blob(manifest_sha256)
        payload = _decode_canonical_object(content, "transition manifest")
        if content != _canonical_json_bytes(payload):
            raise SSERiskWarningTransitionBlockedError(
                "transition manifest is not canonical JSON"
            )
        artifact = self._rebuild(payload)
        if content != _canonical_json_bytes(_manifest_payload(artifact)):
            raise SSERiskWarningTransitionBlockedError(
                "transition manifest does not cold-replay exactly"
            )
        return artifact

    def _rebuild(
        self, payload: Mapping[str, Any]
    ) -> SSERiskWarningTransitionArtifact:
        expected_fields = {
            "logical_content_sha256",
            "protocol_version",
            "raw_evidence",
            "retrieved_at",
            "source_contract",
            "statistics",
            "transition",
        }
        if set(payload) != expected_fields:
            raise SSERiskWarningTransitionBlockedError("transition manifest schema drift")
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise SSERiskWarningTransitionBlockedError("transition protocol changed")
        retrieved_at = _normalize_timestamp(payload.get("retrieved_at"))
        if retrieved_at != payload.get("retrieved_at"):
            raise SSERiskWarningTransitionBlockedError(
                "manifest retrieved_at is not canonical"
            )
        raw_items = payload.get("raw_evidence")
        if not isinstance(raw_items, list) or len(raw_items) != 2:
            raise SSERiskWarningTransitionBlockedError(
                "manifest must bind exactly two raw sources"
            )
        evidence = tuple(_decode_raw_evidence(item) for item in raw_items)
        _validate_evidence_pair(evidence, retrieved_at=retrieved_at)
        index_raw = self.cas.read_blob(evidence[0].body)
        pdf_raw = self.cas.read_blob(evidence[1].body)
        artifact = build_transition_artifact(
            index_raw=index_raw,
            pdf_raw=pdf_raw,
            index_evidence=evidence[0],
            pdf_evidence=evidence[1],
            spec=FROZEN_TRANSITION,
        )
        if payload.get("transition") != artifact.transition.to_dict():
            raise SSERiskWarningTransitionBlockedError(
                "derived transition does not reproduce from raw bytes"
            )
        if payload.get("logical_content_sha256") != artifact.logical_content_sha256:
            raise SSERiskWarningTransitionBlockedError("logical hash mismatch")
        if payload.get("source_contract") != artifact.source_contract:
            raise SSERiskWarningTransitionBlockedError("source contract drift")
        if payload.get("statistics") != artifact.statistics:
            raise SSERiskWarningTransitionBlockedError("statistics drift")
        return artifact


class SSERiskWarningTransitionClient:
    """GET-only collector for the single admitted 688646 transition."""

    def __init__(
        self,
        *,
        cas: SSERiskWarningTransitionCAS,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        clock: Any | None = None,
    ) -> None:
        if not isinstance(cas, SSERiskWarningTransitionCAS):
            raise TypeError("cas must be an SSERiskWarningTransitionCAS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.cas = cas
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now().astimezone())

    def fetch(self) -> SSERiskWarningTransitionArtifact:
        retrieved_at = _normalize_timestamp(self._clock())
        index_evidence = self._fetch_one(
            source_id="SSE_688646_FIXED_ANNOUNCEMENT_INDEX",
            request_url=FROZEN_TRANSITION.index_url,
            expected_hash=FROZEN_TRANSITION.expected_index_sha256,
            expected_content_type="application/javascript",
            maximum_bytes=MAX_INDEX_BYTES,
            retrieved_at=retrieved_at,
        )
        pdf_evidence = self._fetch_one(
            source_id="SSE_688646_FIXED_NOTICE_PDF",
            request_url=FROZEN_TRANSITION.pdf_url,
            expected_hash=FROZEN_TRANSITION.expected_pdf_sha256,
            expected_content_type="application/pdf",
            maximum_bytes=MAX_PDF_BYTES,
            retrieved_at=retrieved_at,
        )
        return build_transition_artifact(
            index_raw=self.cas.read_blob(index_evidence.body),
            pdf_raw=self.cas.read_blob(pdf_evidence.body),
            index_evidence=index_evidence,
            pdf_evidence=pdf_evidence,
            spec=FROZEN_TRANSITION,
        )

    def _fetch_one(
        self,
        *,
        source_id: str,
        request_url: str,
        expected_hash: str,
        expected_content_type: str,
        maximum_bytes: int,
        retrieved_at: str,
    ) -> RawEvidence:
        _validate_source_url(source_id, request_url)
        request_headers = {
            "Accept": expected_content_type,
            "Referer": "https://www.sse.com.cn/",
            "User-Agent": "tdx-research-platform/sse-risk-transition-audit-v1",
        }
        response = self._get(request_url, headers=request_headers, source_id=source_id)
        response_url = str(getattr(response, "url", ""))
        status_code = _strict_int(
            getattr(response, "status_code", None), "HTTP status"
        )
        headers = getattr(response, "headers", {})
        if (
            response_url != request_url
            or status_code != 200
            or headers.get("Location") is not None
        ):
            raise SSERiskWarningTransitionBlockedError(
                f"{source_id} redirected or changed transport"
            )
        content_type = _media_type(headers.get("Content-Type"))
        raw = bytes(getattr(response, "content", b""))
        if expected_content_type == "application/pdf" and content_type == "text/html":
            cookie_value = _parse_acw_sc_v2_challenge(raw)
            retry_headers = dict(request_headers)
            retry_headers["Cookie"] = f"acw_sc__v2={cookie_value}"
            response = self._get(
                request_url,
                headers=retry_headers,
                source_id=f"{source_id} challenge retry",
            )
            response_url = str(getattr(response, "url", ""))
            status_code = _strict_int(
                getattr(response, "status_code", None), "HTTP status"
            )
            headers = getattr(response, "headers", {})
            if (
                response_url != request_url
                or status_code != 200
                or headers.get("Location") is not None
            ):
                raise SSERiskWarningTransitionBlockedError(
                    f"{source_id} challenge retry redirected or changed transport"
                )
            content_type = _media_type(headers.get("Content-Type"))
            raw = bytes(getattr(response, "content", b""))
        if content_type != expected_content_type:
            raise SSERiskWarningTransitionBlockedError(
                f"{source_id} Content-Type changed: {content_type!r}"
            )
        if not raw or len(raw) > maximum_bytes:
            raise SSERiskWarningTransitionBlockedError(
                f"{source_id} response is empty or oversized"
            )
        _verify_hash(raw, expected_hash, source_id)
        reference = self.cas.put_blob(raw)
        return RawEvidence(
            source_id=source_id,
            method="GET",
            request_url=request_url,
            response_url=response_url,
            retrieved_at=retrieved_at,
            http_status=status_code,
            content_type=content_type,
            body=reference,
        )

    def _get(
        self,
        request_url: str,
        *,
        headers: Mapping[str, str],
        source_id: str,
    ) -> Any:
        try:
            return self.session.get(
                request_url,
                headers=dict(headers),
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SSERiskWarningTransitionBlockedError(
                f"{source_id} GET failed closed"
            ) from exc


def parse_fixed_index(
    raw: bytes,
    *,
    spec: SSERiskWarningTransitionSpec = FROZEN_TRANSITION,
) -> dict[str, Any]:
    _validate_spec(spec)
    _verify_hash(raw, spec.expected_index_sha256, "SSE announcement index")
    if not raw or len(raw) > MAX_INDEX_BYTES:
        raise SSERiskWarningTransitionBlockedError(
            "SSE announcement index is empty or oversized"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SSERiskWarningTransitionBlockedError(
            "SSE announcement index is not UTF-8"
        ) from exc
    function_match = re.fullmatch(
        rf"//staticDate=(?P<static_date>[^\r\n]+)\r?\n"
        rf"function get_{re.escape(spec.code)}\(\)\{{\r?\n"
        r"var _t = new Array\(\);\r?\n(?P<body>.*)\r?\nreturn _t;\r?\n\}",
        text,
        flags=re.DOTALL,
    )
    if function_match is None:
        raise SSERiskWarningTransitionBlockedError(
            "SSE announcement index envelope changed"
        )
    blocks = re.findall(r"_t\.push\(\{(?P<body>.*?)\}\);", function_match.group("body"), re.DOTALL)
    if not blocks:
        raise SSERiskWarningTransitionBlockedError(
            "SSE announcement index has no entries"
        )
    entries = [_parse_index_entry(block) for block in blocks]
    matches = [
        item
        for item in entries
        if item["bulletin_file_url"] == urlsplit(spec.pdf_url).path
    ]
    if len(matches) != 1:
        raise SSERiskWarningTransitionBlockedError(
            "fixed transition PDF is missing or duplicated in the index"
        )
    entry = matches[0]
    expected = {
        "stock_code": spec.code,
        "SECURITY_NAME": spec.old_name,
        "bulletin_date": spec.publication_date,
        "bulletin_year": spec.publication_date[:4],
        "bulletin_title": spec.announcement_title,
        "bulletin_file_url": urlsplit(spec.pdf_url).path,
    }
    if any(entry.get(key) != value for key, value in expected.items()):
        raise SSERiskWarningTransitionBlockedError(
            "fixed transition index identity or title changed"
        )
    canonical_entry = _canonical_json_bytes(entry)
    return {
        "static_date": function_match.group("static_date"),
        "entry": entry,
        "entry_sha256": _sha256(canonical_entry),
        "entry_count": len(entries),
    }


def parse_fixed_notice_pdf(
    raw_pdf: bytes,
    *,
    spec: SSERiskWarningTransitionSpec = FROZEN_TRANSITION,
) -> dict[str, Any]:
    _validate_spec(spec)
    _verify_hash(raw_pdf, spec.expected_pdf_sha256, "SSE transition notice")
    text, engine, engine_version, page_count = _extract_pdf_text(raw_pdf)
    compact = _compact_text(text)
    markers = _notice_markers(spec)
    missing = sorted(name for name, marker in markers.items() if marker not in compact)
    if missing:
        raise SSERiskWarningTransitionBlockedError(
            f"SSE transition notice lacks required markers: {missing}"
        )
    if not (
        date.fromisoformat(spec.risk_started_at)
        < date.fromisoformat(spec.suspension_date)
        < date.fromisoformat(spec.effective_date)
    ):
        raise SSERiskWarningTransitionBlockedError(
            "SSE transition dates are not strictly ordered"
        )
    return {
        "code_alias": f"{spec.code}.SH",
        "legal_name": spec.legal_name,
        "old_name": spec.old_name,
        "new_name": spec.new_name,
        "risk_started_at": spec.risk_started_at,
        "suspension_date": spec.suspension_date,
        "effective_date": spec.effective_date,
        "publication_date": spec.publication_date,
        "announcement_number": spec.announcement_number,
        "announcement_title": spec.announcement_title,
        "extraction_engine": engine,
        "extraction_engine_version": engine_version,
        "page_count": page_count,
        "normalized_text_sha256": _sha256(compact.encode("utf-8")),
    }


def _notice_markers(spec: SSERiskWarningTransitionSpec) -> dict[str, str]:
    old_name = _compact_text(spec.old_name)
    new_name = _compact_text(spec.new_name)
    legal_name = _compact_text(spec.legal_name)
    return {
        "code": f"证券代码:{spec.code}",
        "legal_name": legal_name,
        "title": _compact_text("关于撤销其他风险警示暨停牌的公告"),
        "announcement_number": f"公告编号:{spec.announcement_number}",
        "old_name": f"证券简称:{old_name}",
        "new_name": f"撤销后A股简称为{new_name}",
        "risk_started_at": (
            f"公司股票自{_chinese_date(spec.risk_started_at)}起被实施其他风险警示"
        ),
        "suspension_date": f"停牌日期为{_chinese_date(spec.suspension_date)}",
        "effective_date": f"撤销起始日为{_chinese_date(spec.effective_date)}",
        "rename": f"证券简称:由“{old_name}”变更为“{new_name}”",
        "resume": (
            f"自{_chinese_date(spec.effective_date)}起复牌交易并撤销其他风险警示"
        ),
    }


def build_transition_artifact(
    *,
    index_raw: bytes,
    pdf_raw: bytes,
    index_evidence: RawEvidence,
    pdf_evidence: RawEvidence,
    spec: SSERiskWarningTransitionSpec = FROZEN_TRANSITION,
) -> SSERiskWarningTransitionArtifact:
    evidence = (index_evidence, pdf_evidence)
    if index_evidence.retrieved_at != pdf_evidence.retrieved_at:
        raise SSERiskWarningTransitionBlockedError(
            "fixed transition sources are not bound to one capture timestamp"
        )
    _validate_evidence_pair(evidence, retrieved_at=index_evidence.retrieved_at)
    index = parse_fixed_index(index_raw, spec=spec)
    parsed = parse_fixed_notice_pdf(pdf_raw, spec=spec)
    transition = RiskWarningTransition(
        **parsed,
        index_url=spec.index_url,
        pdf_url=spec.pdf_url,
        index_sha256=_sha256(index_raw),
        pdf_sha256=_sha256(pdf_raw),
        index_entry_sha256=str(index["entry_sha256"]),
    )
    logical = _sha256(_canonical_json_bytes(transition.to_dict()))
    return SSERiskWarningTransitionArtifact(
        retrieved_at=index_evidence.retrieved_at,
        transition=transition,
        raw_evidence=evidence,
        logical_content_sha256=logical,
    )


def _parse_index_entry(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    position = 0
    pattern = re.compile(
        r"\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
        r'"(?P<value>(?:[^"\\]|\\.)*)"\s*(?:,|\Z)',
        re.DOTALL,
    )
    while position < len(block):
        match = pattern.match(block, position)
        if match is None:
            if not block[position:].strip():
                break
            raise SSERiskWarningTransitionBlockedError(
                "SSE announcement index entry grammar changed"
            )
        key = match.group("key")
        if key in fields:
            raise SSERiskWarningTransitionBlockedError(
                "SSE announcement index contains duplicate fields"
            )
        try:
            value = json.loads(f'"{match.group("value")}"')
        except json.JSONDecodeError as exc:
            raise SSERiskWarningTransitionBlockedError(
                "SSE announcement index contains an invalid string"
            ) from exc
        fields[key] = value
        position = match.end()
    required = {
        "SECURITY_NAME",
        "bulletin_date",
        "bulletin_file_url",
        "bulletin_title",
        "bulletin_year",
        "stock_code",
    }
    if not required.issubset(fields):
        raise SSERiskWarningTransitionBlockedError(
            "SSE announcement index entry schema drift"
        )
    return fields


def _parse_acw_sc_v2_challenge(raw: bytes) -> str:
    if not raw or len(raw) > MAX_CHALLENGE_BYTES:
        raise SSERiskWarningTransitionBlockedError(
            "SSE PDF anti-bot challenge is empty or oversized"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SSERiskWarningTransitionBlockedError(
            "SSE PDF anti-bot challenge is not UTF-8"
        ) from exc
    stripped = text.strip()
    match = re.search(r"var arg1='(?P<arg1>[0-9A-F]{40})';", stripped)
    required_tokens = (
        "<html><script>",
        "var mask=_0x1e8e('0x0');",
        _ACW_MASK_BASE64,
        '_0x4818("acw_sc__v2", arg1);document.location.reload()',
        "</script></html>",
    )
    literal_positions = "var posList=[" + ",".join(
        hex(item) for item in _ACW_POSITION_LIST
    ) + "];"
    if (
        match is None
        or any(token not in stripped for token in required_tokens)
        or literal_positions not in stripped
        or len(re.findall(r"var arg1='[0-9A-F]{40}';", stripped)) != 1
    ):
        raise SSERiskWarningTransitionBlockedError(
            "SSE PDF anti-bot challenge contract changed"
        )
    arg1 = match.group("arg1")
    reordered = [""] * len(_ACW_POSITION_LIST)
    for source_index, character in enumerate(arg1, start=1):
        try:
            target_index = _ACW_POSITION_LIST.index(source_index)
        except ValueError as exc:
            raise SSERiskWarningTransitionBlockedError(
                "SSE PDF anti-bot position contract is invalid"
            ) from exc
        reordered[target_index] = character
    arg2 = "".join(reordered)
    if len(arg2) != 40 or any(not item for item in reordered):
        raise SSERiskWarningTransitionBlockedError(
            "SSE PDF anti-bot challenge did not reorder exactly"
        )
    cookie = "".join(
        f"{int(arg2[index:index + 2], 16) ^ int(_ACW_MASK[index:index + 2], 16):02x}"
        for index in range(0, 40, 2)
    )
    if not re.fullmatch(r"[0-9a-f]{40}", cookie):
        raise SSERiskWarningTransitionBlockedError(
            "SSE PDF anti-bot cookie derivation failed"
        )
    return cookie


def _extract_pdf_text(raw_pdf: bytes) -> tuple[str, str, str, int]:
    if (
        not raw_pdf
        or len(raw_pdf) > MAX_PDF_BYTES
        or not raw_pdf.startswith(b"%PDF-")
        or b"%%EOF" not in raw_pdf[-4096:]
    ):
        raise SSERiskWarningTransitionBlockedError("SSE notice is not a strict PDF")
    try:
        import pypdf
        from pypdf import PdfReader
    except ImportError as exc:
        raise SSERiskWarningTransitionBlockedError(
            "pypdf is required for SSE notice replay"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw_pdf), strict=True)
        if reader.is_encrypted:
            raise SSERiskWarningTransitionBlockedError("encrypted SSE PDF is rejected")
        page_count = len(reader.pages)
        if page_count <= 0 or page_count > MAX_PDF_PAGES:
            raise SSERiskWarningTransitionBlockedError("SSE PDF page count is invalid")
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text is None:
                raise SSERiskWarningTransitionBlockedError(
                    "SSE PDF has no reproducible text layer"
                )
            pages.append(text)
    except SSERiskWarningTransitionBlockedError:
        raise
    except Exception as exc:
        raise SSERiskWarningTransitionBlockedError(
            "SSE PDF text extraction failed closed"
        ) from exc
    joined = "\n\f\n".join(pages)
    if not joined.strip() or len(joined) > MAX_PDF_TEXT_CHARS:
        raise SSERiskWarningTransitionBlockedError("SSE PDF text is empty or oversized")
    return joined, "pypdf", str(getattr(pypdf, "__version__", "UNKNOWN")), page_count


def _validate_spec(spec: SSERiskWarningTransitionSpec) -> None:
    if not isinstance(spec, SSERiskWarningTransitionSpec):
        raise TypeError("spec must be SSERiskWarningTransitionSpec")
    if not re.fullmatch(r"688\d{3}", spec.code):
        raise SSERiskWarningTransitionBlockedError("invalid STAR Market code")
    for value, label in (
        (spec.risk_started_at, "risk_started_at"),
        (spec.suspension_date, "suspension_date"),
        (spec.effective_date, "effective_date"),
        (spec.publication_date, "publication_date"),
    ):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise SSERiskWarningTransitionBlockedError(
                f"invalid {label}: {value!r}"
            ) from exc
    if spec.publication_date != spec.suspension_date:
        raise SSERiskWarningTransitionBlockedError(
            "publication and suspension dates differ from the admitted event"
        )
    _valid_digest(spec.expected_index_sha256, "index")
    _valid_digest(spec.expected_pdf_sha256, "PDF")
    _validate_source_url("SSE_688646_FIXED_ANNOUNCEMENT_INDEX", spec.index_url)
    _validate_source_url("SSE_688646_FIXED_NOTICE_PDF", spec.pdf_url)


def _validate_source_url(source_id: str, url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise SSERiskWarningTransitionBlockedError(
            f"{source_id} URL is not canonical HTTPS"
        )
    if source_id.endswith("INDEX"):
        if parsed.hostname != SSE_INDEX_HOST or parsed.path != (
            "/js/common/stocks/new/688646.js"
        ):
            raise SSERiskWarningTransitionBlockedError(
                "SSE transition index URL changed"
            )
    elif source_id.endswith("PDF"):
        if parsed.hostname != SSE_PDF_HOST or parsed.path != (
            "/disclosure/listedinfo/announcement/c/new/2026-08-12/"
            "688646_20260812_HF3O.pdf"
        ):
            raise SSERiskWarningTransitionBlockedError(
                "SSE transition PDF URL changed"
            )
    else:
        raise SSERiskWarningTransitionBlockedError("unknown transition source ID")


def _validate_evidence_pair(
    evidence: tuple[RawEvidence, ...], *, retrieved_at: str
) -> None:
    expected = (
        (
            "SSE_688646_FIXED_ANNOUNCEMENT_INDEX",
            FROZEN_TRANSITION.index_url,
            "application/javascript",
            FROZEN_TRANSITION.expected_index_sha256,
        ),
        (
            "SSE_688646_FIXED_NOTICE_PDF",
            FROZEN_TRANSITION.pdf_url,
            "application/pdf",
            FROZEN_TRANSITION.expected_pdf_sha256,
        ),
    )
    if len(evidence) != len(expected):
        raise SSERiskWarningTransitionBlockedError(
            "transition evidence source count changed"
        )
    for item, admitted in zip(evidence, expected, strict=True):
        source_id, url, content_type, expected_hash = admitted
        if (
            item.source_id != source_id
            or item.method != "GET"
            or item.request_url != url
            or item.response_url != url
            or item.retrieved_at != retrieved_at
            or item.http_status != 200
            or item.content_type != content_type
            or item.body.sha256 != expected_hash
        ):
            raise SSERiskWarningTransitionBlockedError(
                f"{source_id} evidence metadata changed"
            )


def _decode_raw_evidence(value: Any) -> RawEvidence:
    expected_fields = {
        "body",
        "content_type",
        "http_status",
        "method",
        "request_url",
        "response_url",
        "retrieved_at",
        "source_id",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise SSERiskWarningTransitionBlockedError("raw evidence schema drift")
    body = value.get("body")
    if not isinstance(body, dict) or set(body) != {
        "byte_count",
        "cas_uri",
        "object_path",
        "sha256",
    }:
        raise SSERiskWarningTransitionBlockedError("blob reference schema drift")
    return RawEvidence(
        source_id=_required_string(value["source_id"], "source_id"),
        method=_required_string(value["method"], "method"),
        request_url=_required_string(value["request_url"], "request_url"),
        response_url=_required_string(value["response_url"], "response_url"),
        retrieved_at=_normalize_timestamp(value["retrieved_at"]),
        http_status=_strict_int(value["http_status"], "http_status"),
        content_type=_required_string(value["content_type"], "content_type"),
        body=BlobReference(
            sha256=_valid_digest(body["sha256"], "blob"),
            byte_count=_strict_int(body["byte_count"], "byte_count"),
            cas_uri=_required_string(body["cas_uri"], "cas_uri"),
            object_path=_required_string(body["object_path"], "object_path"),
        ),
    )


def _manifest_payload(
    artifact: SSERiskWarningTransitionArtifact,
) -> dict[str, Any]:
    if not isinstance(artifact, SSERiskWarningTransitionArtifact):
        raise TypeError("artifact must be SSERiskWarningTransitionArtifact")
    return artifact.to_dict()


def _compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\x00", "")
    normalized = normalized.replace(":", ":").replace("：", ":")
    normalized = normalized.replace("“", "“").replace("”", "”")
    return re.sub(r"\s+", "", normalized)


def _chinese_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def _normalize_timestamp(value: Any) -> str:
    text = value.isoformat() if isinstance(value, datetime) else str(value or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SSERiskWarningTransitionBlockedError(
            f"timestamp is not ISO-8601: {text!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise SSERiskWarningTransitionBlockedError("timestamp must include timezone")
    return parsed.replace(microsecond=0).isoformat()


def _media_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text.split(";", 1)[0].strip()


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise SSERiskWarningTransitionBlockedError(f"invalid {label}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SSERiskWarningTransitionBlockedError(f"invalid {label}") from exc
    return result


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SSERiskWarningTransitionBlockedError(f"invalid {label}")
    return value


def _verify_hash(content: bytes, expected: str, label: str) -> str:
    digest = _sha256(content)
    if digest != _valid_digest(expected, label):
        raise SSERiskWarningTransitionBlockedError(f"{label} SHA-256 mismatch")
    return digest


def _valid_digest(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise SSERiskWarningTransitionBlockedError(f"invalid {label} SHA-256")
    return text


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


def _decode_canonical_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SSERiskWarningTransitionBlockedError(
            f"{label} is not UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SSERiskWarningTransitionBlockedError(f"{label} is not an object")
    return value


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
        raise SSERiskWarningTransitionBlockedError(
            "CAS path escapes its fixed root"
        ) from exc
    current = root_abs
    if current.exists() and _path_is_link_or_reparse(current):
        raise SSERiskWarningTransitionBlockedError(
            "CAS root is a link or reparse point"
        )
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _path_is_link_or_reparse(
            current
        ):
            raise SSERiskWarningTransitionBlockedError(
                "CAS path contains a link, junction, or reparse point"
            )


def _stable_read(root: Path, path: Path, label: str) -> bytes:
    _assert_safe_existing_chain(root, path)
    if not path.is_file() or _path_is_link_or_reparse(path):
        raise SSERiskWarningTransitionBlockedError(f"{label} is missing or reparse")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SSERiskWarningTransitionBlockedError(
            f"{label} cannot be opened as a stable file"
        ) from exc
    try:
        before = os.fstat(descriptor)
        attributes = int(getattr(before, "st_file_attributes", 0) or 0)
        if not stat.S_ISREG(before.st_mode) or (
            attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SSERiskWarningTransitionBlockedError(
                f"{label} is not a plain regular file"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    fingerprint_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    fingerprint_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if fingerprint_before != fingerprint_after or _path_is_link_or_reparse(path):
        raise SSERiskWarningTransitionBlockedError(f"{label} changed during read")
    if len(content) != before.st_size:
        raise SSERiskWarningTransitionBlockedError(f"{label} size changed during read")
    return content


def _atomic_write_exact(root: Path, path: Path, content: bytes) -> None:
    _assert_safe_existing_chain(root, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_existing_chain(root, path.parent)
    if path.exists():
        if _stable_read(root, path, "existing SSE transition CAS object") != content:
            raise SSERiskWarningTransitionBlockedError(
                "existing CAS object differs from immutable content"
            )
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_safe_existing_chain(root, temporary)
        if _path_is_link_or_reparse(temporary):
            raise SSERiskWarningTransitionBlockedError(
                "CAS temporary became a reparse point"
            )
        if _stable_read(root, temporary, "SSE transition CAS temporary") != content:
            raise SSERiskWarningTransitionBlockedError("CAS temporary changed")
        try:
            os.replace(temporary, path)
        except FileExistsError:
            if _stable_read(root, path, "existing SSE transition CAS object") != content:
                raise SSERiskWarningTransitionBlockedError(
                    "concurrent CAS write produced different content"
                )
    finally:
        if temporary.exists() and not _path_is_link_or_reparse(temporary):
            temporary.unlink()


__all__ = [
    "FROZEN_TRANSITION",
    "PROTOCOL_VERSION",
    "SOURCE_SCOPE",
    "SOURCE_STATUS",
    "SSERiskWarningTransitionArtifact",
    "SSERiskWarningTransitionBlockedError",
    "SSERiskWarningTransitionCAS",
    "SSERiskWarningTransitionClient",
    "SSERiskWarningTransitionManifestReference",
    "SSERiskWarningTransitionManifestStore",
    "SSERiskWarningTransitionSpec",
    "build_transition_artifact",
    "parse_fixed_index",
    "parse_fixed_notice_pdf",
]
