from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from research_platform.delisted_history_quality import (
    AUDIT_END,
    AUDIT_START,
    DATASET_CONTRACTS,
    RAW_AUTHORITY_BY_DATASET_EXCHANGE,
    RAW_ENVELOPE_PROTOCOL_VERSION,
    SOURCE_INDEX_AUTHORITY,
    SOURCE_INDEX_PROTOCOL_VERSION,
)
from research_platform.official_trading_calendar import (
    PROTOCOL_VERSION as OFFICIAL_CALENDAR_PROTOCOL_VERSION,
    OfficialTradingCalendarCAS,
    OfficialTradingCalendarManifestStore,
)


DATASET = "trading_calendar"
OFFICIAL_MANIFEST_ROLE = "OFFICIAL_TRADING_CALENDAR_MANIFEST"
UPSTREAM_EVIDENCE_KIND = "OFFICIAL_TRADING_CALENDAR_V2"


@dataclass(frozen=True)
class TradingCalendarQualityIndexReference:
    content_hash: str
    object_path: str
    byte_count: int
    official_manifest_sha256: str
    official_protocol_version: str
    official_logical_content_sha256: str
    copied_cas_object_count: int = 0

    def to_source_identity(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "object_path": self.object_path,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_official_trading_calendar_quality_index(
    *,
    cas_root: Path,
    manifest_sha256: str,
) -> TradingCalendarQualityIndexReference:
    """Build the quality-gate calendar index from a cold-replayed V2 manifest."""

    cas = OfficialTradingCalendarCAS(Path(cas_root))
    manifest_bytes, manifest_path = cas.read_blob(manifest_sha256)
    artifact = OfficialTradingCalendarManifestStore(cas).replay(manifest_sha256)
    contract = DATASET_CONTRACTS[DATASET]

    partitions: list[dict[str, Any]] = []
    total_rows = 0
    for exchange in ("SSE", "SZSE"):
        authority = RAW_AUTHORITY_BY_DATASET_EXCHANGE[DATASET][exchange]
        for year in range(2018, 2024):
            rows = [
                {
                    "exchange": row.exchange,
                    "trade_date": row.trade_date,
                    "is_open": row.is_open,
                }
                for row in artifact.rows
                if row.exchange == exchange
                and row.trade_date.startswith(f"{year:04d}-")
            ]
            expected_count = 366 if year in {2020} else 365
            if len(rows) != expected_count:
                raise ValueError(
                    f"official calendar {exchange}/{year} partition is incomplete"
                )
            normalized_bytes = _canonical_jsonl(rows)
            normalized_hash, normalized_path = cas.put_blob(normalized_bytes)
            envelope = {
                "protocol_version": RAW_ENVELOPE_PROTOCOL_VERSION,
                "authority": authority,
                "dataset": DATASET,
                "exchange": exchange,
                "year": year,
                "code": "*",
                "schema": list(contract.schema),
                "rows": rows,
            }
            envelope_bytes = _canonical_json_bytes(envelope)
            envelope_hash, envelope_path = cas.put_blob(envelope_bytes)
            partitions.append(
                {
                    "exchange": exchange,
                    "year": year,
                    "code": "*",
                    "query_start": f"{year:04d}-01-01",
                    "query_end": f"{year:04d}-12-31",
                    "content_hash": normalized_hash,
                    "object_path": str(normalized_path),
                    "row_count": len(rows),
                    "raw_sources": [
                        {
                            "content_hash": envelope_hash,
                            "object_path": str(envelope_path),
                            "byte_count": len(envelope_bytes),
                            "protocol_version": RAW_ENVELOPE_PROTOCOL_VERSION,
                            "authority": authority,
                            "role": "ROWS_ENVELOPE",
                        },
                        {
                            "content_hash": manifest_sha256,
                            "object_path": str(manifest_path),
                            "byte_count": len(manifest_bytes),
                            "protocol_version": OFFICIAL_CALENDAR_PROTOCOL_VERSION,
                            "authority": authority,
                            "role": OFFICIAL_MANIFEST_ROLE,
                        },
                    ],
                }
            )
            total_rows += len(rows)

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
            "protocol_version": OFFICIAL_CALENDAR_PROTOCOL_VERSION,
            "manifest_sha256": manifest_sha256,
            "logical_content_sha256": artifact.logical_content_sha256,
            "cas_uri": f"sha256:{manifest_sha256}",
            "object_path": str(manifest_path),
            "byte_count": len(manifest_bytes),
        },
    }
    index_bytes = _canonical_json_bytes(index)
    index_hash, index_path = cas.put_blob(index_bytes)
    return TradingCalendarQualityIndexReference(
        content_hash=index_hash,
        object_path=str(index_path),
        byte_count=len(index_bytes),
        official_manifest_sha256=manifest_sha256,
        official_protocol_version=OFFICIAL_CALENDAR_PROTOCOL_VERSION,
        official_logical_content_sha256=artifact.logical_content_sha256,
    )


def materialize_official_trading_calendar_quality_index(
    *,
    source_cas_root: Path,
    target_cas_root: Path,
    manifest_sha256: str,
) -> TradingCalendarQualityIndexReference:
    """Copy a verified V2 CAS closure, replay it in target, then build V3 index.

    The source manifest and every raw dependency are read by content hash.  No
    source-side ``object_path`` is trusted, and the index is generated only
    after the complete closure cold-replays from the target CAS root.
    """

    source_root = _lexical_absolute(source_cas_root)
    target_root = _lexical_absolute(target_cas_root)
    if source_root == target_root:
        raise ValueError("source and target calendar CAS roots must differ")
    manifest_hash = _strict_sha256(manifest_sha256, "calendar manifest")
    source_cas = OfficialTradingCalendarCAS(source_root)
    target_cas = OfficialTradingCalendarCAS(target_root)

    manifest_bytes, _source_manifest_path = source_cas.read_blob(manifest_hash)
    source_artifact = OfficialTradingCalendarManifestStore(source_cas).replay(
        manifest_hash
    )
    dependency_hashes = sorted(
        {
            item.content_sha256
            for item in (
                *source_artifact.raw_sources,
                *source_artifact.catalog_sources,
            )
        }
    )
    if len(dependency_hashes) != (
        len(source_artifact.raw_sources) + len(source_artifact.catalog_sources)
    ):
        raise ValueError("official calendar manifest contains duplicate dependencies")

    copied_hashes: list[str] = []
    for digest in dependency_hashes:
        content, _source_path = source_cas.read_blob(digest)
        copied_digest, _target_path = target_cas.put_blob(content)
        if copied_digest != digest:
            raise ValueError("calendar CAS dependency hash changed during copy")
        copied_hashes.append(copied_digest)
    copied_manifest_hash, _target_manifest_path = target_cas.put_blob(manifest_bytes)
    if copied_manifest_hash != manifest_hash:
        raise ValueError("calendar manifest hash changed during copy")
    copied_hashes.append(copied_manifest_hash)

    target_manifest_bytes, _ = target_cas.read_blob(manifest_hash)
    if target_manifest_bytes != manifest_bytes:
        raise ValueError("target calendar manifest bytes differ from source")
    target_artifact = OfficialTradingCalendarManifestStore(target_cas).replay(
        manifest_hash
    )
    if (
        target_artifact.logical_content_sha256
        != source_artifact.logical_content_sha256
        or len(target_artifact.rows) != len(source_artifact.rows)
        or target_artifact.statistics != source_artifact.statistics
        or target_artifact.source_contract != source_artifact.source_contract
    ):
        raise ValueError("target calendar cold replay differs from source replay")
    reference = build_official_trading_calendar_quality_index(
        cas_root=target_root,
        manifest_sha256=manifest_hash,
    )
    return TradingCalendarQualityIndexReference(
        content_hash=reference.content_hash,
        object_path=reference.object_path,
        byte_count=reference.byte_count,
        official_manifest_sha256=reference.official_manifest_sha256,
        official_protocol_version=reference.official_protocol_version,
        official_logical_content_sha256=(
            reference.official_logical_content_sha256
        ),
        copied_cas_object_count=len(copied_hashes),
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"\n".join(_canonical_json_bytes(row) for row in rows) + b"\n"


def _strict_sha256(value: str, label: str) -> str:
    digest = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"invalid {label} SHA-256")
    return digest


def _lexical_absolute(path: Path) -> Path:
    return Path(path).absolute()
