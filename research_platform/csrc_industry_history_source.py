from __future__ import annotations

import hashlib
import html
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

import requests

from research_platform import cninfo_delisted_disclosures as cninfo
from research_platform.cninfo_delisted_disclosures import CninfoDisclosureCAS
from research_platform.delisted_history_quality import (
    AUDIT_END,
    AUDIT_START,
    DATASET_CONTRACTS,
    RAW_ENVELOPE_PROTOCOL_VERSION,
    SOURCE_INDEX_AUTHORITY,
    SOURCE_INDEX_PROTOCOL_VERSION,
)
from research_platform.historical_security_master import (
    HISTORICAL_SECURITY_MASTER_STORE_ROOT,
    HistoricalSecurityMasterBlockedError,
    HistoricalSecurityMasterStore,
    SecurityMasterRecord,
    validate_security_master_records,
)


PROTOCOL_VERSION = "csrc-capco-industry-history-source-v1"
QUALITY_ADAPTER_PROTOCOL_VERSION = "csrc-capco-industry-history-quality-adapter-v1"
UPSTREAM_EVIDENCE_KIND = "CSRC_CAPCO_OFFICIAL_INDUSTRY_RESULTS_V1"
SOURCE_STATUS = "PARTIAL_OFFICIAL_INDUSTRY_HISTORY_NOT_READY"
SOURCE_SCOPE = "FROZEN_REGULATOR_QUARTERLY_SNAPSHOTS"
DATASET = "industry_history"
EXPECTED_MASTER_TARGET_COUNT = 239
OFFICIAL_INDUSTRY_RAW_AUTHORITY = (
    "CSRC_CAPCO_OFFICIAL_INDUSTRY_CLASSIFICATION_RESULTS"
)

CSRC_HOST = "www.csrc.gov.cn"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 300
MAX_ASSIGNMENTS = 20_000
TIMEZONE_OFFSET = "+08:00"


class CSRCIndustryHistoryBlockedError(RuntimeError):
    """Official industry-history evidence is absent, changed, or incomplete."""

    def __init__(self, message: str, *, status: str = "SOURCE_REJECTED") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class OfficialSnapshotSpec:
    snapshot_id: str
    period_label: str
    page_url: str
    pdf_url: str
    published_date: str
    expected_pdf_sha256: str
    minimum_assignment_count: int = 2_000


OFFICIAL_SNAPSHOT_SPECS: dict[str, OfficialSnapshotSpec] = {
    "2017Q3": OfficialSnapshotSpec(
        snapshot_id="2017Q3",
        period_label="2017年3季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1452005/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1452005/1452005/files/"
            "1616066688397_26144.pdf"
        ),
        published_date="2017-11-14",
        expected_pdf_sha256=(
            "395a3e3d523e91fe609663c6d715ac04794285b8f77b5c83c66ac416998ba16d"
        ),
    ),
    "2017Q4": OfficialSnapshotSpec(
        snapshot_id="2017Q4",
        period_label="2017年4季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1452004/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1452004/1452004/files/"
            "1616066687576_64273.pdf"
        ),
        published_date="2018-01-19",
        expected_pdf_sha256=(
            "6868765d6383c7ca0a30ec51d5d1de50fab16226b782aad4c4097deceeaaf384"
        ),
    ),
    "2018Q1": OfficialSnapshotSpec(
        snapshot_id="2018Q1",
        period_label="2018年1季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1452003/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1452003/1452003/files/"
            "1616066686330_79503.pdf"
        ),
        published_date="2018-05-21",
        expected_pdf_sha256=(
            "b886c1cbce2cff74fa26cb4cccec6ac3b510c8bd8fbd9e8ab727c9e5dd6e0260"
        ),
    ),
    "2018Q2": OfficialSnapshotSpec(
        snapshot_id="2018Q2",
        period_label="2018年2季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1452002/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1452002/1452002/files/"
            "1616066685168_98160.pdf"
        ),
        published_date="2018-07-30",
        expected_pdf_sha256=(
            "6ae0bae953ff6efa8745ceec3aef2e3a38e453342272e42513d1dc1fe40e53b4"
        ),
    ),
    "2018Q3": OfficialSnapshotSpec(
        snapshot_id="2018Q3",
        period_label="2018年3季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1452001/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1452001/1452001/files/"
            "1616066683399_15715.pdf"
        ),
        published_date="2018-11-02",
        expected_pdf_sha256=(
            "153c6647ba84378343b12a35e8aded12369a6cc66d2d1122de544e9b4cb436d3"
        ),
    ),
    "2018Q4": OfficialSnapshotSpec(
        snapshot_id="2018Q4",
        period_label="2018年4季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1452000/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1452000/1452000/files/"
            "1616066681842_11369.pdf"
        ),
        published_date="2019-02-12",
        expected_pdf_sha256=(
            "a783b90505bec4a971d3ac78ce8dba67fd9afbf0087544951642648b25276210"
        ),
    ),
    "2019Q1": OfficialSnapshotSpec(
        snapshot_id="2019Q1",
        period_label="2019年1季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1451999/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1451999/1451999/files/"
            "1616066680642_59642.pdf"
        ),
        published_date="2019-04-19",
        expected_pdf_sha256=(
            "5a7eb8297bb8b30ac93de2e95ec71ba81380c1630857a0bbbd87c882ca207fec"
        ),
    ),
    "2019Q2": OfficialSnapshotSpec(
        snapshot_id="2019Q2",
        period_label="2019年2季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1451998/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1451998/1451998/files/"
            "1616066679501_12987.pdf"
        ),
        published_date="2019-07-11",
        expected_pdf_sha256=(
            "6df3c6b27cea15de75dc121cc569caa9e6142e0d3bbca713500b9bc509db6679"
        ),
    ),
    "2019Q3": OfficialSnapshotSpec(
        snapshot_id="2019Q3",
        period_label="2019年3季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1451997/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1451997/1451997/files/"
            "1616066678064_24727.pdf"
        ),
        published_date="2019-10-28",
        expected_pdf_sha256=(
            "5d6599b2e99c04acbc8f5258380e3464e9d64284bff5288192fd0a8230700d78"
        ),
    ),
    "2019Q4": OfficialSnapshotSpec(
        snapshot_id="2019Q4",
        period_label="2019年4季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1451996/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1451996/1451996/files/"
            "1616066676808_31329.pdf"
        ),
        published_date="2020-01-10",
        expected_pdf_sha256=(
            "c7a85040406ab9832ed6e1a531ac0ac09399c022d8aaf28a109b8aa0d6a6ea14"
        ),
    ),
    "2020Q1": OfficialSnapshotSpec(
        snapshot_id="2020Q1",
        period_label="2020年1季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1451995/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1451995/1451995/files/"
            "1616066675411_69786.pdf"
        ),
        published_date="2020-04-14",
        expected_pdf_sha256=(
            "88bc5dc855ec8282a9275ec92a30577d598f35db44fd36e276c7687c7636d119"
        ),
    ),
    "2020Q2": OfficialSnapshotSpec(
        snapshot_id="2020Q2",
        period_label="2020年2季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1451994/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1451994/1451994/files/"
            "1616066674038_28028.pdf"
        ),
        published_date="2020-07-14",
        expected_pdf_sha256=(
            "84e1e0382561a4627c6ad7076a57b1d3b51e88192f19b898db0bccaaed10a546"
        ),
    ),
    "2020Q3": OfficialSnapshotSpec(
        snapshot_id="2020Q3",
        period_label="2020年3季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1447171/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1447171/1447171/files/"
            "1616066672849_81070.pdf"
        ),
        published_date="2020-11-05",
        expected_pdf_sha256=(
            "8fd171823b3593fe0407fa3f8d83caf6809bff0e5a0c89af93e20743745784cb"
        ),
    ),
    "2020Q4": OfficialSnapshotSpec(
        snapshot_id="2020Q4",
        period_label="2020年4季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1447170/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1447170/1447170/files/"
            "1616066671097_61196.pdf"
        ),
        published_date="2021-01-25",
        expected_pdf_sha256=(
            "755f6dbaa58ecca733f9d3ed501dad12484db164bf4c5e21931e90df3feb9e97"
        ),
    ),
    "2021Q1": OfficialSnapshotSpec(
        snapshot_id="2021Q1",
        period_label="2021年1季度上市公司行业分类结果",
        page_url=(
            "https://www.csrc.gov.cn/csrc/c100103/"
            "c29a6845e0d0b4912adcc1cdfa5f679eb/content.shtml"
        ),
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/"
            "c29a6845e0d0b4912adcc1cdfa5f679eb/"
            "29a6845e0d0b4912adcc1cdfa5f679eb/files/1629362473789_90940.pdf"
        ),
        published_date="2021-04-14",
        expected_pdf_sha256=(
            "5023cc4d76e81047559fdfc8042b92c7e1ea2d7bc6bfbf2ee97c8884742661b0"
        ),
    ),
    "2021Q2": OfficialSnapshotSpec(
        snapshot_id="2021Q2",
        period_label="2021年2季度上市公司行业分类结果",
        page_url=(
            "https://www.csrc.gov.cn/csrc/c100103/"
            "cd627990af13041d18fac0a41daf5cb00/content.shtml"
        ),
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/"
            "cd627990af13041d18fac0a41daf5cb00/"
            "d627990af13041d18fac0a41daf5cb00/files/1635996889420_16207.pdf"
        ),
        published_date="2021-07-19",
        expected_pdf_sha256=(
            "54469b0d93c1721818c14adf0fdb68c1eff2443d9f31d45f2b709ee4eb6a002c"
        ),
    ),
    "2021Q3": OfficialSnapshotSpec(
        snapshot_id="2021Q3",
        period_label="2021年3季度上市公司行业分类结果",
        page_url="https://www.csrc.gov.cn/csrc/c100103/c1558619/content.shtml",
        pdf_url=(
            "https://www.csrc.gov.cn/csrc/c100103/c1558619/1558619/files/"
            "1638277734844_11692.pdf"
        ),
        published_date="2021-11-10",
        expected_pdf_sha256=(
            "0e8af6662353037135bb8e8a08a409dcebea9c47f71e7e25b48fe66a24f8c62b"
        ),
    ),
}


@dataclass(frozen=True)
class RawEvidence:
    source_id: str
    role: str
    source_url: str
    method: str
    retrieved_at: str
    content_hash: str
    byte_count: int
    content_type: str
    object_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IndustryAssignment:
    exchange: str
    code: str
    industry_code: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class OfficialIndustrySnapshot:
    snapshot_id: str
    period_label: str
    published_date: str
    available_from: str
    page: RawEvidence
    pdf: RawEvidence
    assignments: tuple[IndustryAssignment, ...]
    extraction_engine: str
    extraction_engine_version: str
    page_count: int
    normalized_text_sha256: str
    logical_content_sha256: str
    source_authority: str = OFFICIAL_INDUSTRY_RAW_AUTHORITY

    @property
    def source_contract(self) -> dict[str, Any]:
        return {
            "ready": False,
            "status": SOURCE_STATUS,
            "scope": SOURCE_SCOPE,
            "authority": self.source_authority,
            "availability_rule": "DATE_ONLY_PUBLICATION_PLUS_ONE_CALENDAR_DAY",
            "current_classification_backfill_allowed": False,
            "caller_ready_attestation_allowed": False,
            "training_allowed": False,
            "trading_allowed": False,
            "promotion_allowed": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "snapshot_id": self.snapshot_id,
            "period_label": self.period_label,
            "published_date": self.published_date,
            "available_from": self.available_from,
            "page": self.page.to_dict(),
            "pdf": self.pdf.to_dict(),
            "assignments": [item.to_dict() for item in self.assignments],
            "extraction_engine": self.extraction_engine,
            "extraction_engine_version": self.extraction_engine_version,
            "page_count": self.page_count,
            "normalized_text_sha256": self.normalized_text_sha256,
            "logical_content_sha256": self.logical_content_sha256,
            "source_contract": self.source_contract,
        }


@dataclass(frozen=True)
class SnapshotManifestReference:
    content_hash: str
    object_path: str
    byte_count: int
    snapshot_id: str
    assignment_count: int
    ready: bool = False
    status: str = SOURCE_STATUS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FrozenIndustryTarget:
    canonical_entity_id: str
    exchange: str
    code: str
    query_start: str
    query_end: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class IndustryHistoryQualityIndexReference:
    content_hash: str
    object_path: str
    byte_count: int
    master_snapshot_id: str
    target_count: int
    evidence_target_count: int
    covered_target_count: int
    partition_count: int
    row_count: int
    upstream_kind: str = UPSTREAM_EVIDENCE_KIND
    ready: bool = False
    complete: bool = False
    status: str = SOURCE_STATUS

    def to_source_identity(self) -> dict[str, str]:
        return {
            "content_hash": self.content_hash,
            "object_path": self.object_path,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuthoritativeIndustryScope:
    master_snapshot_id: str
    master_content_sha256: str
    master_row_count: int
    targets: tuple[FrozenIndustryTarget, ...]
    scope_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "targets": [item.to_dict() for item in self.targets],
        }


def load_authoritative_industry_scope(
    *,
    master_store_root: Path = HISTORICAL_SECURITY_MASTER_STORE_ROOT,
    expected_snapshot_id: str | None = None,
) -> AuthoritativeIndustryScope:
    """Cold-replay the promoted master and derive the fixed 239-target scope."""

    root = Path(master_store_root).absolute()
    try:
        release = HistoricalSecurityMasterStore(root).load_current_release()
    except (HistoricalSecurityMasterBlockedError, OSError, ValueError) as exc:
        raise CSRCIndustryHistoryBlockedError(
            f"authoritative security master failed cold replay: {exc}"
        ) from exc
    snapshot_id = _sha256_identity(release.get("snapshot_id"), "master snapshot")
    if expected_snapshot_id is not None and snapshot_id != _sha256_identity(
        expected_snapshot_id, "expected master snapshot"
    ):
        raise CSRCIndustryHistoryBlockedError(
            "promoted security-master snapshot changed"
        )
    try:
        metadata = dict(release["manifest"]["artifacts"]["security_master_jsonl"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CSRCIndustryHistoryBlockedError(
            "security-master release has no JSONL artifact"
        ) from exc
    master_hash = _sha256_identity(metadata.get("content_hash"), "master content")
    expected_path = root / "objects" / master_hash[:2] / master_hash
    object_path = Path(str(metadata.get("object_path") or ""))
    if object_path.resolve() != expected_path.resolve():
        raise CSRCIndustryHistoryBlockedError("security-master object path mismatch")
    try:
        raw = cninfo._stable_read(root, object_path)
    except (cninfo.CninfoDelistedDisclosureBlockedError, OSError) as exc:
        raise CSRCIndustryHistoryBlockedError(
            f"security-master JSONL cannot be read stably: {exc}"
        ) from exc
    if _sha256(raw) != master_hash:
        raise CSRCIndustryHistoryBlockedError("security-master JSONL hash mismatch")
    records = _parse_master_jsonl(raw)
    if metadata.get("row_count") != len(records):
        raise CSRCIndustryHistoryBlockedError("security-master row count mismatch")
    try:
        validate_security_master_records(records)
    except HistoricalSecurityMasterBlockedError as exc:
        raise CSRCIndustryHistoryBlockedError(
            f"security-master records failed validation: {exc}"
        ) from exc
    targets = _derive_industry_targets(records)
    if len(targets) != EXPECTED_MASTER_TARGET_COUNT:
        raise CSRCIndustryHistoryBlockedError(
            "authoritative industry target count changed: "
            f"expected {EXPECTED_MASTER_TARGET_COUNT}, got {len(targets)}"
        )
    scope_value = {
        "master_snapshot_id": snapshot_id,
        "master_content_sha256": master_hash,
        "master_row_count": len(records),
        "targets": [item.to_dict() for item in targets],
    }
    return AuthoritativeIndustryScope(
        master_snapshot_id=snapshot_id,
        master_content_sha256=master_hash,
        master_row_count=len(records),
        targets=targets,
        scope_sha256=_sha256(_canonical_json_bytes(scope_value)),
    )


def capture_official_industry_snapshot(
    *,
    cas_root: Path,
    snapshot_id: str,
    session: requests.Session | None = None,
    timeout_seconds: float = 60.0,
    retrieved_at: datetime | str | None = None,
) -> SnapshotManifestReference:
    """Capture one allowlisted CSRC page/PDF pair and seal its parsed snapshot."""

    spec = OFFICIAL_SNAPSHOT_SPECS.get(str(snapshot_id))
    if spec is None:
        raise CSRCIndustryHistoryBlockedError("snapshot_id is not allowlisted")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    client = session or requests.Session()
    observed_at = _canonical_datetime(retrieved_at)
    page_bytes, page_type = _get_exact(
        client, spec.page_url, timeout_seconds, "text/html", MAX_PAGE_BYTES
    )
    _validate_page_contract(page_bytes, spec)
    pdf_bytes, pdf_type = _get_exact(
        client, spec.pdf_url, timeout_seconds, "application/pdf", MAX_PDF_BYTES
    )
    if _sha256(pdf_bytes) != spec.expected_pdf_sha256:
        raise CSRCIndustryHistoryBlockedError(
            f"{spec.snapshot_id} official PDF hash changed"
        )
    snapshot = _build_snapshot(
        spec=spec,
        page_bytes=page_bytes,
        page_type=page_type,
        pdf_bytes=pdf_bytes,
        pdf_type=pdf_type,
        cas=CSRCIndustryHistoryCAS(cas_root),
        retrieved_at=observed_at,
    )
    return _store_snapshot_manifest(snapshot, CSRCIndustryHistoryCAS(cas_root))


def replay_official_industry_snapshot(
    *, cas_root: Path, manifest_sha256: str
) -> OfficialIndustrySnapshot:
    cas = CSRCIndustryHistoryCAS(cas_root)
    manifest_bytes, _path = cas.read_blob(manifest_sha256)
    manifest = _strict_canonical_object(manifest_bytes, "industry snapshot manifest")
    snapshot_id = str(manifest.get("snapshot_id") or "")
    spec = OFFICIAL_SNAPSHOT_SPECS.get(snapshot_id)
    if spec is None:
        raise CSRCIndustryHistoryBlockedError("manifest snapshot is not allowlisted")
    page_value = manifest.get("page")
    pdf_value = manifest.get("pdf")
    if not isinstance(page_value, dict) or not isinstance(pdf_value, dict):
        raise CSRCIndustryHistoryBlockedError("manifest raw evidence is missing")
    page = _evidence_from_dict(page_value, role="REGULATOR_PAGE", spec=spec, cas=cas)
    pdf = _evidence_from_dict(pdf_value, role="INDUSTRY_RESULT_PDF", spec=spec, cas=cas)
    page_bytes, _ = cas.read_blob(page.content_hash, expected_path=page.object_path)
    pdf_bytes, _ = cas.read_blob(pdf.content_hash, expected_path=pdf.object_path)
    _validate_page_contract(page_bytes, spec)
    if _sha256(pdf_bytes) != spec.expected_pdf_sha256:
        raise CSRCIndustryHistoryBlockedError("replayed official PDF hash changed")
    source_contract = manifest.get("source_contract")
    if not isinstance(source_contract, dict):
        raise CSRCIndustryHistoryBlockedError("snapshot source contract is missing")
    frozen_authority = str(source_contract.get("authority") or "")
    if frozen_authority not in {
        OFFICIAL_INDUSTRY_RAW_AUTHORITY,
        "CSRC_OFFICIAL_INDUSTRY_CLASSIFICATION_RESULTS",
    }:
        raise CSRCIndustryHistoryBlockedError(
            "snapshot source authority is not admitted"
        )
    rebuilt = _build_snapshot(
        spec=spec,
        page_bytes=page_bytes,
        page_type=page.content_type,
        pdf_bytes=pdf_bytes,
        pdf_type=pdf.content_type,
        cas=cas,
        retrieved_at=page.retrieved_at,
        source_authority=frozen_authority,
    )
    if rebuilt.to_dict() != manifest:
        raise CSRCIndustryHistoryBlockedError(
            "snapshot manifest does not match raw CAS recomputation"
        )
    return rebuilt


def build_industry_history_quality_index(
    *,
    cas_root: Path,
    snapshot_manifest_sha256s: Sequence[str],
    authoritative_master_snapshot_id: str,
    authoritative_targets: Sequence[FrozenIndustryTarget],
) -> IndustryHistoryQualityIndexReference:
    """Build an audit-only source index; no input can make it READY."""

    cas = CSRCIndustryHistoryCAS(cas_root)
    master_snapshot_id = _sha256_identity(
        authoritative_master_snapshot_id, "master_snapshot_id"
    )
    targets = _normalize_targets(authoritative_targets)
    if not snapshot_manifest_sha256s:
        raise CSRCIndustryHistoryBlockedError("no official snapshot manifests")
    snapshots: list[OfficialIndustrySnapshot] = []
    seen_snapshots: set[str] = set()
    manifest_bindings: list[dict[str, Any]] = []
    for digest in snapshot_manifest_sha256s:
        normalized = _sha256_identity(digest, "snapshot manifest SHA-256")
        snapshot = replay_official_industry_snapshot(
            cas_root=cas_root, manifest_sha256=normalized
        )
        if snapshot.snapshot_id in seen_snapshots:
            raise CSRCIndustryHistoryBlockedError("duplicate snapshot manifest")
        seen_snapshots.add(snapshot.snapshot_id)
        manifest_bytes, manifest_path = cas.read_blob(normalized)
        snapshots.append(snapshot)
        manifest_bindings.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "content_hash": normalized,
                "object_path": str(manifest_path),
                "byte_count": len(manifest_bytes),
                "published_date": snapshot.published_date,
                "logical_content_sha256": snapshot.logical_content_sha256,
            }
        )
    snapshots.sort(key=lambda item: (item.available_from, item.snapshot_id))
    if [item.available_from for item in snapshots] != sorted(
        item.available_from for item in snapshots
    ):
        raise CSRCIndustryHistoryBlockedError("snapshot chronology is invalid")

    rows_by_code, evidence_codes, covered_codes = _build_interval_rows(
        targets, snapshots
    )
    contract = DATASET_CONTRACTS[DATASET]
    pdf_by_hash = {item.pdf.content_hash: item.pdf for item in snapshots}
    partitions: list[dict[str, Any]] = []
    total_rows = 0
    for target in targets:
        for year in _target_years(target):
            rows = [
                row
                for row in rows_by_code.get(target.code, ())
                if _interval_overlaps_year(row, year)
            ]
            rows.sort(key=lambda item: (str(item["valid_from"]), str(item["industry_code"])))
            normalized = _canonical_jsonl(rows)
            normalized_hash, normalized_path = cas.put_blob_allow_empty(normalized)
            authority = OFFICIAL_INDUSTRY_RAW_AUTHORITY
            envelope = {
                "protocol_version": RAW_ENVELOPE_PROTOCOL_VERSION,
                "authority": authority,
                "dataset": DATASET,
                "exchange": target.exchange,
                "year": year,
                "code": target.code,
                "schema": list(contract.schema),
                "rows": rows,
            }
            envelope_bytes = _canonical_json_bytes(envelope)
            envelope_hash, envelope_path = cas.put_blob(envelope_bytes)
            raw_sources = [
                {
                    "content_hash": envelope_hash,
                    "object_path": str(envelope_path),
                    "byte_count": len(envelope_bytes),
                    "protocol_version": RAW_ENVELOPE_PROTOCOL_VERSION,
                    "authority": authority,
                    "role": "ROWS_ENVELOPE",
                }
            ]
            for document_hash in sorted(
                {str(row["source_document_hash"]) for row in rows}
            ):
                evidence = pdf_by_hash.get(document_hash)
                if evidence is None:
                    raise CSRCIndustryHistoryBlockedError(
                        "row is not bound to a replayed official PDF"
                    )
                raw_sources.append(
                    {
                        "content_hash": evidence.content_hash,
                        "object_path": evidence.object_path,
                        "byte_count": evidence.byte_count,
                        "protocol_version": f"{DATASET}-source-document-v1",
                        "authority": authority,
                        "role": "SOURCE_DOCUMENT",
                    }
                )
            partitions.append(
                {
                    "exchange": target.exchange,
                    "year": year,
                    "code": target.code,
                    "query_start": f"{year:04d}-01-01",
                    "query_end": f"{year:04d}-12-31",
                    "content_hash": normalized_hash,
                    "object_path": str(normalized_path),
                    "row_count": len(rows),
                    "raw_sources": raw_sources,
                }
            )
            total_rows += len(rows)

    target_scope = {
        "snapshot_id": master_snapshot_id,
        "expected_target_count": EXPECTED_MASTER_TARGET_COUNT,
        "targets": [item.to_dict() for item in targets],
    }
    target_scope_sha256 = _sha256(_canonical_json_bytes(target_scope))
    index = {
        "protocol_version": SOURCE_INDEX_PROTOCOL_VERSION,
        "dataset": DATASET,
        "source_protocol_version": contract.source_protocol_version,
        "schema_version": contract.schema_version,
        "schema": list(contract.schema),
        "source_authority": SOURCE_INDEX_AUTHORITY,
        "coverage_start": AUDIT_START,
        "coverage_end": AUDIT_END,
        "row_count": total_rows,
        "partitions": partitions,
        "upstream_evidence": {
            "kind": UPSTREAM_EVIDENCE_KIND,
            "adapter_protocol_version": QUALITY_ADAPTER_PROTOCOL_VERSION,
            "authority": "CSRC_CAPCO_OFFICIAL_INDUSTRY_CLASSIFICATION_RESULTS",
            "snapshot_manifests": sorted(
                manifest_bindings, key=lambda item: (item["published_date"], item["snapshot_id"])
            ),
            "master_scope": {
                **target_scope,
                "scope_sha256": target_scope_sha256,
                "target_count": len(targets),
                "evidence_target_count": len(evidence_codes),
                "covered_target_count": len(covered_codes),
                "full_master_scope_present": len(targets) == EXPECTED_MASTER_TARGET_COUNT,
                "all_targets_covered": len(covered_codes) == len(targets),
            },
            "point_in_time_protocol": {
                "publication_precision": "DATE_ONLY",
                "published_at_rule": "PUBLICATION_DATE_AT_23_59_59_ASIA_SHANGHAI",
                "available_from_rule": "NEXT_CALENDAR_DATE_AT_00_00_ASIA_SHANGHAI",
                "unchanged_classification_carries_forward": True,
                "missing_from_later_snapshot_does_not_imply_industry_CHANGE": True,
                "post_period_publications_may_not_backfill_prior_dates": True,
            },
            "integration_contract": {
                "required_upstream_kind": UPSTREAM_EVIDENCE_KIND,
                "required_exchange_raw_authorities": {
                    "SSE": OFFICIAL_INDUSTRY_RAW_AUTHORITY,
                    "SZSE": OFFICIAL_INDUSTRY_RAW_AUTHORITY,
                },
                "required_cold_replay_function": (
                    "replay_industry_history_quality_index"
                ),
                "producer_promotion_claim_accepted": False,
            },
            "status": SOURCE_STATUS,
        },
        "ready": False,
        "complete": False,
    }
    index_bytes = _canonical_json_bytes(index)
    index_hash, index_path = cas.put_blob(index_bytes)
    return IndustryHistoryQualityIndexReference(
        content_hash=index_hash,
        object_path=str(index_path),
        byte_count=len(index_bytes),
        master_snapshot_id=master_snapshot_id,
        target_count=len(targets),
        evidence_target_count=len(evidence_codes),
        covered_target_count=len(covered_codes),
        partition_count=len(partitions),
        row_count=total_rows,
    )


def replay_industry_history_quality_index(
    *,
    cas_root: Path,
    source_index_sha256: str,
    snapshot_manifest_sha256s: Sequence[str],
    authoritative_master_snapshot_id: str,
    authoritative_targets: Sequence[FrozenIndustryTarget],
) -> IndustryHistoryQualityIndexReference:
    cas = CSRCIndustryHistoryCAS(cas_root)
    observed, observed_path = cas.read_blob(source_index_sha256)
    _strict_canonical_object(observed, "industry-history source index")
    rebuilt = build_industry_history_quality_index(
        cas_root=cas_root,
        snapshot_manifest_sha256s=snapshot_manifest_sha256s,
        authoritative_master_snapshot_id=authoritative_master_snapshot_id,
        authoritative_targets=authoritative_targets,
    )
    if (
        rebuilt.content_hash != source_index_sha256
        or Path(rebuilt.object_path) != observed_path
        or rebuilt.byte_count != len(observed)
    ):
        raise CSRCIndustryHistoryBlockedError(
            "industry-history source index failed cold replay"
        )
    return rebuilt


class CSRCIndustryHistoryCAS:
    """Exact-byte CAS wrapper using the repository's hardened stable reads."""

    def __init__(self, root: Path) -> None:
        self._cas = CninfoDisclosureCAS(Path(root))

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        try:
            return self._cas.put_blob(content)
        except Exception as exc:
            raise CSRCIndustryHistoryBlockedError(str(exc)) from exc

    def put_blob_allow_empty(self, content: bytes) -> tuple[str, Path]:
        if content:
            return self.put_blob(content)
        digest = _sha256(content)
        path = self._cas.root / "sha256" / digest[:2] / digest
        cninfo._atomic_write_exact(self._cas.root, path, content)
        persisted = cninfo._stable_read(self._cas.root, path)
        if persisted != content or _sha256(persisted) != digest:
            raise CSRCIndustryHistoryBlockedError("empty CAS object is corrupt")
        return digest, path

    def read_blob(
        self, digest: str, *, expected_path: Any | None = None
    ) -> tuple[bytes, Path]:
        try:
            return self._cas.read_blob(digest, expected_path=expected_path)
        except Exception as exc:
            raise CSRCIndustryHistoryBlockedError(str(exc)) from exc


def _build_snapshot(
    *,
    spec: OfficialSnapshotSpec,
    page_bytes: bytes,
    page_type: str,
    pdf_bytes: bytes,
    pdf_type: str,
    cas: CSRCIndustryHistoryCAS,
    retrieved_at: str,
    source_authority: str = OFFICIAL_INDUSTRY_RAW_AUTHORITY,
) -> OfficialIndustrySnapshot:
    _validate_page_contract(page_bytes, spec)
    assignments, engine, engine_version, page_count, text_hash = (
        _extract_pdf_assignments(pdf_bytes)
    )
    if len(assignments) < spec.minimum_assignment_count:
        raise CSRCIndustryHistoryBlockedError(
            f"{spec.snapshot_id} assignment count is below frozen floor: {len(assignments)}"
        )
    page_hash, page_path = cas.put_blob(page_bytes)
    pdf_hash, pdf_path = cas.put_blob(pdf_bytes)
    if pdf_hash != spec.expected_pdf_sha256:
        raise CSRCIndustryHistoryBlockedError("official PDF identity mismatch")
    page = RawEvidence(
        source_id=f"CSRC_INDUSTRY_PAGE_{spec.snapshot_id}",
        role="REGULATOR_PAGE",
        source_url=spec.page_url,
        method="GET",
        retrieved_at=retrieved_at,
        content_hash=page_hash,
        byte_count=len(page_bytes),
        content_type=page_type,
        object_path=str(page_path),
    )
    pdf = RawEvidence(
        source_id=f"CSRC_INDUSTRY_PDF_{spec.snapshot_id}",
        role="INDUSTRY_RESULT_PDF",
        source_url=spec.pdf_url,
        method="GET",
        retrieved_at=retrieved_at,
        content_hash=pdf_hash,
        byte_count=len(pdf_bytes),
        content_type=pdf_type,
        object_path=str(pdf_path),
    )
    available_from = (date.fromisoformat(spec.published_date) + timedelta(days=1)).isoformat()
    logical_rows = [item.to_dict() for item in assignments]
    logical_hash = _sha256(
        _canonical_json_bytes(
            {
                "snapshot_id": spec.snapshot_id,
                "published_date": spec.published_date,
                "available_from": available_from,
                "assignments": logical_rows,
            }
        )
    )
    return OfficialIndustrySnapshot(
        snapshot_id=spec.snapshot_id,
        period_label=spec.period_label,
        published_date=spec.published_date,
        available_from=available_from,
        page=page,
        pdf=pdf,
        assignments=assignments,
        extraction_engine=engine,
        extraction_engine_version=engine_version,
        page_count=page_count,
        normalized_text_sha256=text_hash,
        logical_content_sha256=logical_hash,
        source_authority=source_authority,
    )


def _extract_pdf_assignments(
    content: bytes,
) -> tuple[tuple[IndustryAssignment, ...], str, str, int, str]:
    if not content.startswith(b"%PDF") or len(content) > MAX_PDF_BYTES:
        raise CSRCIndustryHistoryBlockedError("industry result is not an admitted PDF")
    try:
        import pypdf
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=True)
    except Exception as exc:
        raise CSRCIndustryHistoryBlockedError("industry PDF cannot be opened") from exc
    if not 0 < len(reader.pages) <= MAX_PDF_PAGES:
        raise CSRCIndustryHistoryBlockedError("industry PDF page count is invalid")
    texts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise CSRCIndustryHistoryBlockedError("industry PDF text extraction failed") from exc
        texts.append(_normalize_text(text))
    joined = "\n\f\n".join(texts)
    ordered = _parse_assignment_texts(texts)
    return (
        ordered,
        "pypdf",
        str(pypdf.__version__),
        len(reader.pages),
        _sha256(joined.encode("utf-8")),
    )


def _parse_assignment_texts(
    page_texts: Sequence[str],
) -> tuple[IndustryAssignment, ...]:
    """Parse regulator tables whose section label may wrap onto the next row."""

    assignments: dict[tuple[str, str], IndustryAssignment] = {}
    section = ""
    major = ""
    normalized_pages = [
        [re.sub(r"\s+", " ", line).strip() for line in page.splitlines()]
        for page in page_texts
    ]
    for lines in normalized_pages:
        for position, value in enumerate(lines):
            if not value or "上市公司代码" in value:
                continue
            major_match = re.search(
                r"(?:^|\s)(\d{2})(?:\s|$).*?(?<!\d)(\d{6})(?!\d)", value
            )
            if major_match:
                major = major_match.group(1)
                observed_section = re.search(r"\(([A-Z])\)", value)
                if observed_section is None:
                    for following in lines[position + 1 : position + 3]:
                        if re.search(
                            r"(?:^|\s)\d{2}(?:\s|$).*?(?<!\d)\d{6}(?!\d)",
                            following,
                        ):
                            break
                        observed_section = re.search(r"\(([A-Z])\)", following)
                        if observed_section is not None:
                            break
                if observed_section is not None:
                    section = observed_section.group(1)
                codes = re.findall(r"(?<!\d)(\d{6})(?!\d)", value)
            else:
                observed_section = re.search(r"\(([A-Z])\)", value)
                if observed_section is not None:
                    section = observed_section.group(1)
                codes = re.findall(r"(?<!\d)(\d{6})(?!\d)", value)
            if not section or not major:
                continue
            for code in codes:
                exchange = (
                    "SSE"
                    if code.startswith("6")
                    else "SZSE"
                    if code[0] in "03"
                    else ""
                )
                if not exchange:
                    continue
                alias = f"{code}.{'SH' if exchange == 'SSE' else 'SZ'}"
                assignment = IndustryAssignment(exchange, alias, f"{section}{major}")
                key = (exchange, alias)
                previous = assignments.setdefault(key, assignment)
                if previous != assignment:
                    raise CSRCIndustryHistoryBlockedError(
                        f"conflicting industry assignments for {alias}"
                    )
                if len(assignments) > MAX_ASSIGNMENTS:
                    raise CSRCIndustryHistoryBlockedError(
                        "industry assignment count is unsafe"
                    )
    return tuple(
        sorted(assignments.values(), key=lambda item: (item.exchange, item.code))
    )


def _build_interval_rows(
    targets: Sequence[FrozenIndustryTarget],
    snapshots: Sequence[OfficialIndustrySnapshot],
) -> tuple[
    dict[str, tuple[dict[str, Any], ...]], set[str], set[str]
]:
    mappings = {
        snapshot.snapshot_id: {item.code: item for item in snapshot.assignments}
        for snapshot in snapshots
    }
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    evidence: set[str] = set()
    covered: set[str] = set()
    for target in targets:
        start = date.fromisoformat(target.query_start)
        end_exclusive = date.fromisoformat(target.query_end) + timedelta(days=1)
        available = [
            snapshot
            for snapshot in snapshots
            if date.fromisoformat(snapshot.available_from) <= start
            and target.code in mappings[snapshot.snapshot_id]
        ]
        if available:
            baseline = available[-1]
            interval_start = start
            covered.add(target.code)
        else:
            later = [
                snapshot
                for snapshot in snapshots
                if start < date.fromisoformat(snapshot.available_from) < end_exclusive
                and target.code in mappings[snapshot.snapshot_id]
            ]
            if not later:
                result[target.code] = ()
                continue
            baseline = later[0]
            interval_start = date.fromisoformat(baseline.available_from)
        current_code = mappings[baseline.snapshot_id][target.code].industry_code
        changes: list[tuple[date, str, OfficialIndustrySnapshot]] = [
            (interval_start, current_code, baseline)
        ]
        for snapshot in snapshots:
            effective = date.fromisoformat(snapshot.available_from)
            assignment = mappings[snapshot.snapshot_id].get(target.code)
            if (
                effective <= interval_start
                or effective >= end_exclusive
                or assignment is None
            ):
                continue
            if assignment.industry_code != current_code:
                current_code = assignment.industry_code
                changes.append((effective, current_code, snapshot))
        rows: list[dict[str, Any]] = []
        for position, (valid_from, industry_code, source) in enumerate(changes):
            valid_to = changes[position + 1][0] if position + 1 < len(changes) else end_exclusive
            rows.append(
                {
                    "exchange": target.exchange,
                    "code": target.code,
                    "industry_code": industry_code,
                    "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat(),
                    "published_at": f"{source.published_date}T23:59:59{TIMEZONE_OFFSET}",
                    "effective_at": f"{source.available_from}T00:00:00{TIMEZONE_OFFSET}",
                    "source_document_hash": source.pdf.content_hash,
                }
            )
        result[target.code] = tuple(rows)
        evidence.add(target.code)
    return result, evidence, covered


def _store_snapshot_manifest(
    snapshot: OfficialIndustrySnapshot, cas: CSRCIndustryHistoryCAS
) -> SnapshotManifestReference:
    content = _canonical_json_bytes(snapshot.to_dict())
    digest, path = cas.put_blob(content)
    return SnapshotManifestReference(
        content_hash=digest,
        object_path=str(path),
        byte_count=len(content),
        snapshot_id=snapshot.snapshot_id,
        assignment_count=len(snapshot.assignments),
    )


def _get_exact(
    session: requests.Session,
    url: str,
    timeout_seconds: float,
    expected_type: str,
    maximum_bytes: int,
) -> tuple[bytes, str]:
    _validate_official_url(url)
    try:
        response = session.get(
            url,
            headers={
                "Accept": expected_type,
                "User-Agent": "tdx-research-platform/1.0 (+read-only-audit)",
            },
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise CSRCIndustryHistoryBlockedError(
            f"official source GET failed: {exc}", status="SOURCE_UNAVAILABLE"
        ) from exc
    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
    content = bytes(response.content)
    if (
        response.status_code != 200
        or response.url != url
        or content_type != expected_type
        or not content
        or len(content) > maximum_bytes
    ):
        raise CSRCIndustryHistoryBlockedError("official source response contract changed")
    return content, content_type


def _validate_page_contract(content: bytes, spec: OfficialSnapshotSpec) -> None:
    if not content or len(content) > MAX_PAGE_BYTES:
        raise CSRCIndustryHistoryBlockedError("regulator page is empty or oversized")
    try:
        text = html.unescape(content.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as exc:
        raise CSRCIndustryHistoryBlockedError("regulator page is not UTF-8") from exc
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)
    if spec.period_label not in plain:
        raise CSRCIndustryHistoryBlockedError("regulator page title changed")
    dates = re.findall(r"日期\s*[：:]\s*(\d{4}-\d{2}-\d{2})", plain)
    if dates != [spec.published_date]:
        raise CSRCIndustryHistoryBlockedError("regulator publication date changed")
    links = {
        urljoin(spec.page_url, match)
        for match in re.findall(r"(?:href|src)=[\"']([^\"']+)[\"']", text, re.I)
    }
    if spec.pdf_url not in links:
        raise CSRCIndustryHistoryBlockedError("regulator page no longer binds official PDF")


def _evidence_from_dict(
    value: Mapping[str, Any],
    *,
    role: str,
    spec: OfficialSnapshotSpec,
    cas: CSRCIndustryHistoryCAS,
) -> RawEvidence:
    if set(value) != set(RawEvidence.__dataclass_fields__):
        raise CSRCIndustryHistoryBlockedError("raw-evidence schema drift")
    evidence = RawEvidence(**dict(value))
    expected_url = spec.page_url if role == "REGULATOR_PAGE" else spec.pdf_url
    expected_type = "text/html" if role == "REGULATOR_PAGE" else "application/pdf"
    if (
        evidence.role != role
        or evidence.source_url != expected_url
        or evidence.method != "GET"
        or evidence.content_type != expected_type
        or _canonical_datetime(evidence.retrieved_at) != evidence.retrieved_at
        or evidence.byte_count <= 0
    ):
        raise CSRCIndustryHistoryBlockedError("raw-evidence identity changed")
    content, _ = cas.read_blob(evidence.content_hash, expected_path=evidence.object_path)
    if len(content) != evidence.byte_count:
        raise CSRCIndustryHistoryBlockedError("raw-evidence byte count changed")
    return evidence


def _normalize_targets(
    values: Sequence[FrozenIndustryTarget],
) -> tuple[FrozenIndustryTarget, ...]:
    if not values:
        raise CSRCIndustryHistoryBlockedError("no authoritative industry targets")
    output: list[FrozenIndustryTarget] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, FrozenIndustryTarget):
            raise TypeError("targets must contain FrozenIndustryTarget")
        exchange = str(value.exchange).upper()
        suffix = ".SH" if exchange == "SSE" else ".SZ" if exchange == "SZSE" else ""
        code = str(value.code).upper()
        start = _iso_date(value.query_start, "query_start")
        end = _iso_date(value.query_end, "query_end")
        if (
            not suffix
            or not re.fullmatch(r"\d{6}" + re.escape(suffix), code)
            or start < date.fromisoformat(AUDIT_START)
            or end > date.fromisoformat(AUDIT_END)
            or end < start
            or not str(value.canonical_entity_id).strip()
            or code in seen
        ):
            raise CSRCIndustryHistoryBlockedError("authoritative target scope is invalid")
        seen.add(code)
        output.append(
            FrozenIndustryTarget(
                str(value.canonical_entity_id).strip(),
                exchange,
                code,
                start.isoformat(),
                end.isoformat(),
            )
        )
    return tuple(sorted(output, key=lambda item: (item.exchange, item.code)))


def _parse_master_jsonl(content: bytes) -> tuple[SecurityMasterRecord, ...]:
    if not content or not content.endswith(b"\n"):
        raise CSRCIndustryHistoryBlockedError(
            "security-master JSONL is empty or unterminated"
        )
    expected_fields = set(SecurityMasterRecord.__dataclass_fields__)
    records: list[SecurityMasterRecord] = []
    for line in content.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CSRCIndustryHistoryBlockedError(
                "security-master JSONL contains invalid JSON"
            ) from exc
        if (
            not isinstance(value, dict)
            or set(value) != expected_fields
            or _canonical_json_bytes(value) != line
            or not isinstance(value.get("attributes"), dict)
        ):
            raise CSRCIndustryHistoryBlockedError(
                "security-master JSONL row schema drift"
            )
        records.append(SecurityMasterRecord(**value))
    return tuple(records)


def _derive_industry_targets(
    records: Sequence[SecurityMasterRecord],
) -> tuple[FrozenIndustryTarget, ...]:
    audit_start = date.fromisoformat(AUDIT_START)
    audit_end_exclusive = date.fromisoformat(AUDIT_END) + timedelta(days=1)
    targets: list[FrozenIndustryTarget] = []
    seen: set[str] = set()
    for record in records:
        if record.exchange not in {"SSE", "SZSE"} or record.delisted_at is None:
            continue
        listed = date.fromisoformat(record.listed_at)
        valid_from = date.fromisoformat(record.valid_from)
        delisted = date.fromisoformat(record.delisted_at)
        valid_to = date.fromisoformat(record.valid_to) if record.valid_to else delisted
        start = max(audit_start, listed, valid_from)
        end_exclusive = min(audit_end_exclusive, delisted, valid_to)
        if start >= end_exclusive:
            continue
        if record.code_alias in seen:
            raise CSRCIndustryHistoryBlockedError(
                f"duplicate authoritative target: {record.code_alias}"
            )
        seen.add(record.code_alias)
        targets.append(
            FrozenIndustryTarget(
                canonical_entity_id=record.canonical_entity_id,
                exchange=record.exchange,
                code=record.code_alias,
                query_start=start.isoformat(),
                query_end=(end_exclusive - timedelta(days=1)).isoformat(),
            )
        )
    return tuple(sorted(targets, key=lambda item: (item.exchange, item.code)))


def _target_years(target: FrozenIndustryTarget) -> range:
    return range(
        date.fromisoformat(target.query_start).year,
        date.fromisoformat(target.query_end).year + 1,
    )


def _interval_overlaps_year(row: Mapping[str, Any], year: int) -> bool:
    start = date.fromisoformat(str(row["valid_from"]))
    end = date.fromisoformat(str(row["valid_to"]))
    return start <= date(year, 12, 31) and end > date(year, 1, 1)


def _validate_official_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != CSRC_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CSRCIndustryHistoryBlockedError("source URL is outside CSRC HTTPS scope")


def _canonical_datetime(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise CSRCIndustryHistoryBlockedError("retrieved_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CSRCIndustryHistoryBlockedError("retrieved_at lacks timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _iso_date(value: Any, field_name: str) -> date:
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise CSRCIndustryHistoryBlockedError(f"{field_name} is invalid") from exc
    if parsed.isoformat() != text:
        raise CSRCIndustryHistoryBlockedError(f"{field_name} is not canonical")
    return parsed


def _sha256_identity(value: Any, field_name: str) -> str:
    text = str(value or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise CSRCIndustryHistoryBlockedError(f"{field_name} is not SHA-256")
    return text


def _normalize_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n"))


def _strict_canonical_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CSRCIndustryHistoryBlockedError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or _canonical_json_bytes(value) != content:
        raise CSRCIndustryHistoryBlockedError(f"{label} is not canonical JSON")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


__all__ = [
    "AuthoritativeIndustryScope",
    "CSRCIndustryHistoryBlockedError",
    "CSRCIndustryHistoryCAS",
    "EXPECTED_MASTER_TARGET_COUNT",
    "FrozenIndustryTarget",
    "IndustryHistoryQualityIndexReference",
    "OFFICIAL_SNAPSHOT_SPECS",
    "OFFICIAL_INDUSTRY_RAW_AUTHORITY",
    "PROTOCOL_VERSION",
    "QUALITY_ADAPTER_PROTOCOL_VERSION",
    "SOURCE_STATUS",
    "SnapshotManifestReference",
    "UPSTREAM_EVIDENCE_KIND",
    "build_industry_history_quality_index",
    "capture_official_industry_snapshot",
    "load_authoritative_industry_scope",
    "replay_industry_history_quality_index",
    "replay_official_industry_snapshot",
]
