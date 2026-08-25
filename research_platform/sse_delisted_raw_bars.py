from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

import requests

from research_platform.delisted_history_quality import (
    AUDIT_END,
    AUDIT_START,
    DATASET_CONTRACTS,
    MASTER_RECORD_FIELDS,
    RAW_AUTHORITY_BY_DATASET_EXCHANGE,
    RAW_ENVELOPE_PROTOCOL_VERSION,
    SOURCE_INDEX_PROTOCOL_VERSION,
    SSE_OFFICIAL_RAW_BARS_INDEX_AUTHORITY,
)
from research_platform.historical_security_master import (
    HistoricalSecurityMasterStore,
)
from research_platform.official_historical_bars import (
    PROTOCOL_VERSION as OFFICIAL_BARS_PROTOCOL_VERSION,
    SSE_DAYK_ENDPOINT,
    SSE_DAYK_FIELDS,
    SSE_DAYK_PORT,
    SSE_DAYK_SELECT,
    SSE_JSONP_CALLBACK,
    OfficialBarsArtifact,
    OfficialDailyBar,
    OfficialHistoricalBarsClient,
    OfficialHistoricalBarsBlockedError,
    RawResponseEvidence,
    parse_sse_dayk_page,
)


PROTOCOL_VERSION = "early-winner-sse-delisted-raw-execution-bars-v1"
DATASET = "raw_execution_bars"
OFFICIAL_MANIFEST_ROLE = "SSE_OFFICIAL_DAILY_BARS_MANIFEST"
PARTIAL_SOURCE_STATUS = "SSE_RAW_EXECUTION_BARS_PARTIAL_SOURCE_ONLY"
BULK_CAPTURE_PROTOCOL_VERSION = "early-winner-sse-delisted-raw-bars-bulk-v1"
CUTOFF_CAPTURE_PROTOCOL_VERSION = "early-winner-sse-dayk-cutoff-capability-v1"
CUTOFF_CAPTURE_CONTRACT_UNADMITTED = (
    "SSE_DAYK_SERVER_SIDE_CUTOFF_CONTRACT_UNADMITTED"
)
EXPECTED_CURRENT_SSE_TARGET_COUNT = 99
BULK_CAPTURE_POINTER_NAME = "capture_current.json"
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

PARSER_CONTRACT = {
    "callback": SSE_JSONP_CALLBACK,
    "fields": list(SSE_DAYK_FIELDS),
    "interval": "HALF_OPEN",
    "official_bars_protocol_version": OFFICIAL_BARS_PROTOCOL_VERSION,
    "select": SSE_DAYK_SELECT,
}
PARSER_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(
        PARSER_CONTRACT,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class SSEDelistedRawBarsManifestReference:
    manifest_sha256: str
    object_path: str
    byte_count: int
    code: str
    logical_content_sha256: str
    raw_page_hashes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "raw_page_hashes": list(self.raw_page_hashes),
        }


@dataclass(frozen=True)
class SSEDelistedRawBarsQualityIndexReference:
    content_hash: str
    object_path: str
    byte_count: int
    manifest_sha256s: tuple[str, ...]
    codes: tuple[str, ...]
    row_count: int
    partition_count: int
    source_quality_index_sha256: str = ""
    copied_cas_object_count: int = 0
    ready: bool = False
    status: str = PARTIAL_SOURCE_STATUS
    promotion_blocked: bool = True

    def to_source_identity(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "object_path": self.object_path,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "manifest_sha256s": list(self.manifest_sha256s),
            "codes": list(self.codes),
        }


@dataclass(frozen=True)
class SSEDayKCutoffCaptureAssessment:
    protocol_version: str
    endpoint: str
    cutoff_date: str
    admitted_query_parameters: tuple[str, ...]
    pagination_interval: str
    pagination_basis: str
    total_and_rows_share_response_envelope: bool
    server_side_date_bound_admitted: bool
    metadata_only_boundary_probe_admitted: bool
    zero_post_cutoff_response_rows_guaranteed: bool
    safe: bool
    status: str
    detail: str
    promotion_blocked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "admitted_query_parameters": list(self.admitted_query_parameters),
        }


@dataclass(frozen=True)
class SSEDelistedRawBarsBulkCaptureResult:
    snapshot_id: str
    security_master_content_hash: str
    target_codes: tuple[str, ...]
    eligible_codes: tuple[str, ...]
    deferred_codes: tuple[str, ...]
    manifest_sha256s: tuple[str, ...]
    resumed_codes: tuple[str, ...]
    captured_codes: tuple[str, ...]
    failed_codes: tuple[str, ...]
    checkpoint_sha256: str
    checkpoint_path: str
    checkpoint_pointer_path: str
    quality_index_sha256: str
    deferred_capture_assessment: SSEDayKCutoffCaptureAssessment
    eligible_capture_complete: bool
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "target_codes": list(self.target_codes),
            "eligible_codes": list(self.eligible_codes),
            "deferred_codes": list(self.deferred_codes),
            "manifest_sha256s": list(self.manifest_sha256s),
            "resumed_codes": list(self.resumed_codes),
            "captured_codes": list(self.captured_codes),
            "failed_codes": list(self.failed_codes),
            "deferred_capture_assessment": (
                self.deferred_capture_assessment.to_dict()
            ),
        }


class SSEDelistedRawBarsCAS:
    """Stable, reparse-safe CAS shared by raw JSONP, manifests and partitions."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def object_path(self, content_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", str(content_hash or "")):
            raise OfficialHistoricalBarsBlockedError("CAS hash is not SHA-256")
        return self.root / "sha256" / content_hash[:2] / content_hash

    def put_blob(self, content: bytes) -> tuple[str, Path]:
        payload = bytes(content)
        digest = _sha256(payload)
        path = self.object_path(digest)
        _atomic_write_exact(path, payload)
        replayed = _stable_read(path, "SSE delisted raw-bars CAS object")
        if replayed != payload or _sha256(replayed) != digest:
            raise OfficialHistoricalBarsBlockedError("CAS write verification failed")
        return digest, path.resolve()

    def read_blob(self, content_hash: str) -> tuple[bytes, Path]:
        path = self.object_path(content_hash)
        content = _stable_read(path, "SSE delisted raw-bars CAS object")
        if _sha256(content) != content_hash:
            raise OfficialHistoricalBarsBlockedError("CAS object hash mismatch")
        return content, path.resolve()

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
        digest = _sha256(content)
        if expected_sha256 is not None and digest != str(expected_sha256).lower():
            raise OfficialHistoricalBarsBlockedError(
                f"official response hash mismatch: expected {expected_sha256}, got {digest}"
            )
        stored_hash, path = self.put_blob(content)
        return RawResponseEvidence(
            source_url=str(source_url),
            method=str(method),
            retrieved_at=str(retrieved_at),
            content_sha256=stored_hash,
            byte_count=len(content),
            content_type=str(content_type),
            cas_uri=f"sha256:{stored_hash}",
            object_path=str(path),
            persisted=True,
            request=dict(request),
            response=dict(response),
        )


class SSEDelistedRawBarsManifestStore:
    def __init__(self, cas: SSEDelistedRawBarsCAS) -> None:
        self.cas = cas

    def seal(
        self, artifact: OfficialBarsArtifact
    ) -> SSEDelistedRawBarsManifestReference:
        manifest = _manifest_from_artifact(artifact, self.cas)
        manifest_bytes = _canonical_json_bytes(manifest)
        manifest_sha256, path = self.cas.put_blob(manifest_bytes)
        replayed = self.replay(manifest_sha256)
        if replayed.logical_content_sha256 != artifact.logical_content_sha256:
            raise OfficialHistoricalBarsBlockedError(
                "sealed manifest did not replay to the supplied artifact"
            )
        return SSEDelistedRawBarsManifestReference(
            manifest_sha256=manifest_sha256,
            object_path=str(path),
            byte_count=len(manifest_bytes),
            code=artifact.code,
            logical_content_sha256=artifact.logical_content_sha256,
            raw_page_hashes=tuple(
                str(item["content_sha256"]) for item in manifest["raw_pages"]
            ),
        )

    def replay(self, manifest_sha256: str) -> OfficialBarsArtifact:
        content, _ = self.cas.read_blob(manifest_sha256)
        try:
            manifest = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OfficialHistoricalBarsBlockedError(
                "SSE delisted raw-bars manifest is invalid"
            ) from exc
        if not isinstance(manifest, dict) or _canonical_json_bytes(manifest) != content:
            raise OfficialHistoricalBarsBlockedError(
                "SSE delisted raw-bars manifest is not canonical JSON"
            )
        return _artifact_from_manifest(manifest, self.cas)


def build_sse_delisted_raw_bars_quality_index(
    *,
    cas_root: Path,
    manifest_sha256s: Sequence[str],
) -> SSEDelistedRawBarsQualityIndexReference:
    """Build only the SSE raw-execution-bars source index from cold replay."""

    cas = SSEDelistedRawBarsCAS(Path(cas_root))
    store = SSEDelistedRawBarsManifestStore(cas)
    manifests = tuple(str(item).lower() for item in manifest_sha256s)
    if not manifests or len(set(manifests)) != len(manifests):
        raise OfficialHistoricalBarsBlockedError(
            "SSE manifest list must be non-empty and unique"
        )
    artifacts: list[tuple[str, Path, bytes, OfficialBarsArtifact]] = []
    seen_codes: set[str] = set()
    for manifest_sha256 in manifests:
        manifest_bytes, manifest_path = cas.read_blob(manifest_sha256)
        artifact = store.replay(manifest_sha256)
        if artifact.exchange != "SSE" or not artifact.code.endswith(".SH"):
            raise OfficialHistoricalBarsBlockedError(
                "SSE quality adapter cannot ingest a non-SSE artifact"
            )
        if artifact.code in seen_codes:
            raise OfficialHistoricalBarsBlockedError(
                f"duplicate SSE artifact code: {artifact.code}"
            )
        seen_codes.add(artifact.code)
        artifacts.append((manifest_sha256, manifest_path, manifest_bytes, artifact))

    contract = DATASET_CONTRACTS[DATASET]
    authority = RAW_AUTHORITY_BY_DATASET_EXCHANGE[DATASET]["SSE"]
    partitions: list[dict[str, Any]] = []
    total_rows = 0
    for manifest_sha256, manifest_path, manifest_bytes, artifact in sorted(
        artifacts, key=lambda item: item[3].code
    ):
        for year in range(2018, 2024):
            rows = [
                {
                    "exchange": "SSE",
                    "code": artifact.code,
                    "trade_date": bar.date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "amount": bar.amount,
                }
                for bar in artifact.bars
                if bar.date.startswith(f"{year:04d}-")
            ]
            normalized = _canonical_jsonl(rows)
            normalized_hash, normalized_path = cas.put_blob(normalized)
            envelope = {
                "protocol_version": RAW_ENVELOPE_PROTOCOL_VERSION,
                "authority": authority,
                "dataset": DATASET,
                "exchange": "SSE",
                "year": year,
                "code": artifact.code,
                "schema": list(contract.schema),
                "rows": rows,
            }
            envelope_bytes = _canonical_json_bytes(envelope)
            envelope_hash, envelope_path = cas.put_blob(envelope_bytes)
            partitions.append(
                {
                    "exchange": "SSE",
                    "year": year,
                    "code": artifact.code,
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
                            "protocol_version": PROTOCOL_VERSION,
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
        "source_authority": SSE_OFFICIAL_RAW_BARS_INDEX_AUTHORITY,
        "coverage_start": AUDIT_START,
        "coverage_end": AUDIT_END,
        "row_count": total_rows,
        "partitions": partitions,
        "ready": False,
        "complete": False,
    }
    index_bytes = _canonical_json_bytes(index)
    index_hash, index_path = cas.put_blob(index_bytes)
    return SSEDelistedRawBarsQualityIndexReference(
        content_hash=index_hash,
        object_path=str(index_path),
        byte_count=len(index_bytes),
        manifest_sha256s=manifests,
        codes=tuple(sorted(seen_codes)),
        row_count=total_rows,
        partition_count=len(partitions),
    )


def materialize_sse_delisted_raw_bars_quality_index(
    *,
    source_cas_root: Path,
    target_cas_root: Path,
    quality_index_sha256: str,
) -> SSEDelistedRawBarsQualityIndexReference:
    """Materialize a V3 SSE quality-index closure into an independent CAS.

    Every source object is opened through its content hash.  Embedded source
    paths are ignored, official manifests are rebound to the target CAS, and a
    new quality index is accepted only after the central quality loader can
    cold-replay it from the target root alone.
    """

    source_root = _lexical_absolute(source_cas_root)
    target_root = _lexical_absolute(target_cas_root)
    if source_root == target_root:
        raise ValueError("source and target SSE raw-bars CAS roots must differ")
    source_index_hash = _strict_sha256(quality_index_sha256, "quality index")
    source_cas = SSEDelistedRawBarsCAS(source_root)
    target_cas = SSEDelistedRawBarsCAS(target_root)

    source_index_bytes, _ = source_cas.read_blob(source_index_hash)
    source_index = _parse_source_quality_index(source_index_bytes)
    copied_hashes: set[str] = set()
    _copy_cas_blob(
        source_cas,
        target_cas,
        source_index_hash,
        copied_hashes,
    )

    source_partitions: dict[tuple[str, int, str], dict[str, Any]] = {}
    manifest_hashes: set[str] = set()
    manifest_by_code: dict[str, str] = {}
    for partition in source_index["partitions"]:
        key, envelope_source, manifest_source = _validate_source_partition(
            partition,
            source_cas=source_cas,
        )
        if key in source_partitions:
            raise OfficialHistoricalBarsBlockedError(
                "SSE source quality index contains a duplicate partition"
            )
        source_partitions[key] = partition
        _copy_cas_blob(
            source_cas,
            target_cas,
            str(partition["content_hash"]),
            copied_hashes,
        )
        _copy_cas_blob(
            source_cas,
            target_cas,
            str(envelope_source["content_hash"]),
            copied_hashes,
        )
        manifest_hash = str(manifest_source["content_hash"])
        _copy_cas_blob(
            source_cas,
            target_cas,
            manifest_hash,
            copied_hashes,
        )
        manifest_hashes.add(manifest_hash)
        existing = manifest_by_code.setdefault(key[2], manifest_hash)
        if existing != manifest_hash:
            raise OfficialHistoricalBarsBlockedError(
                "SSE source quality index binds one code to multiple manifests"
            )

    expected_partition_keys = {
        ("SSE", year, code)
        for code in manifest_by_code
        for year in range(2018, 2024)
    }
    if set(source_partitions) != expected_partition_keys:
        raise OfficialHistoricalBarsBlockedError(
            "SSE source quality index has incomplete year partitions"
        )
    if int(source_index["row_count"]) != sum(
        int(partition["row_count"])
        for partition in source_partitions.values()
    ):
        raise OfficialHistoricalBarsBlockedError(
            "SSE source quality index row_count mismatch"
        )

    target_manifest_by_source: dict[str, str] = {}
    target_manifests_by_code: list[tuple[str, str]] = []
    target_store = SSEDelistedRawBarsManifestStore(target_cas)
    for source_manifest_hash in sorted(manifest_hashes):
        manifest_bytes, _ = source_cas.read_blob(source_manifest_hash)
        manifest = _parse_canonical_json_object(
            manifest_bytes,
            "SSE source official manifest",
        )
        raw_pages = manifest.get("raw_pages")
        if not isinstance(raw_pages, list) or not raw_pages:
            raise OfficialHistoricalBarsBlockedError(
                "SSE source official manifest has no raw pages"
            )
        rebound_pages: list[dict[str, Any]] = []
        for raw_page in raw_pages:
            if not isinstance(raw_page, dict):
                raise OfficialHistoricalBarsBlockedError(
                    "SSE source official manifest raw page is invalid"
                )
            rebound = dict(raw_page)
            raw_hash = _strict_sha256(
                str(rebound.get("content_sha256") or ""),
                "raw page",
            )
            _content, target_path = _copy_cas_blob(
                source_cas,
                target_cas,
                raw_hash,
                copied_hashes,
            )
            rebound["object_path"] = str(target_path)
            rebound_pages.append(rebound)
        rebound_manifest = dict(manifest)
        rebound_manifest["raw_pages"] = rebound_pages
        rebound_bytes = _canonical_json_bytes(rebound_manifest)
        target_manifest_hash, _ = target_cas.put_blob(rebound_bytes)
        artifact = target_store.replay(target_manifest_hash)
        target_manifest_by_source[source_manifest_hash] = target_manifest_hash
        target_manifests_by_code.append((artifact.code, target_manifest_hash))

    if len({code for code, _ in target_manifests_by_code}) != len(
        target_manifests_by_code
    ):
        raise OfficialHistoricalBarsBlockedError(
            "SSE materialized manifests contain duplicate codes"
        )
    if {
        code: source_hash for code, source_hash in manifest_by_code.items()
    } != {
        code: source_hash
        for code, target_hash in target_manifests_by_code
        for source_hash, mapped_hash in target_manifest_by_source.items()
        if mapped_hash == target_hash
    }:
        raise OfficialHistoricalBarsBlockedError(
            "SSE materialized manifest code identity differs from source index"
        )

    built = build_sse_delisted_raw_bars_quality_index(
        cas_root=target_root,
        manifest_sha256s=[
            digest for _code, digest in sorted(target_manifests_by_code)
        ],
    )
    target_index_bytes, _ = target_cas.read_blob(built.content_hash)
    target_index = _parse_source_quality_index(target_index_bytes)
    _compare_materialized_quality_indexes(
        source_index=source_index,
        target_index=target_index,
        manifest_hash_map=target_manifest_by_source,
    )

    from research_platform.delisted_history_quality import (
        _SourceEvidenceError,
        _load_dataset,
    )

    try:
        loaded = _load_dataset(
            DATASET,
            built.to_source_identity(),
            target_root,
        )
    except _SourceEvidenceError as exc:
        raise OfficialHistoricalBarsBlockedError(
            "materialized SSE quality index failed target-only cold replay: "
            f"{exc}"
        ) from exc
    if (
        loaded.row_count != built.row_count
        or len(loaded.partitions) != built.partition_count
    ):
        raise OfficialHistoricalBarsBlockedError(
            "materialized SSE quality index cold-replay statistics mismatch"
        )
    return SSEDelistedRawBarsQualityIndexReference(
        content_hash=built.content_hash,
        object_path=built.object_path,
        byte_count=built.byte_count,
        manifest_sha256s=built.manifest_sha256s,
        codes=built.codes,
        row_count=built.row_count,
        partition_count=built.partition_count,
        source_quality_index_sha256=source_index_hash,
        copied_cas_object_count=len(copied_hashes),
    )


def _parse_source_quality_index(content: bytes) -> dict[str, Any]:
    index = _parse_canonical_json_object(content, "SSE source quality index")
    expected_fields = {
        "protocol_version",
        "dataset",
        "source_protocol_version",
        "schema_version",
        "schema",
        "source_authority",
        "coverage_start",
        "coverage_end",
        "row_count",
        "partitions",
        "ready",
        "complete",
    }
    contract = DATASET_CONTRACTS[DATASET]
    if set(index) != expected_fields:
        raise OfficialHistoricalBarsBlockedError(
            "SSE source quality index schema drift"
        )
    if (
        index.get("protocol_version") != SOURCE_INDEX_PROTOCOL_VERSION
        or index.get("dataset") != DATASET
        or index.get("source_protocol_version") != contract.source_protocol_version
        or index.get("schema_version") != contract.schema_version
        or tuple(index.get("schema") or ()) != contract.schema
        or index.get("source_authority")
        != SSE_OFFICIAL_RAW_BARS_INDEX_AUTHORITY
        or index.get("coverage_start") != AUDIT_START
        or index.get("coverage_end") != AUDIT_END
        or index.get("ready") is not False
        or index.get("complete") is not False
        or not isinstance(index.get("partitions"), list)
        or not index["partitions"]
    ):
        raise OfficialHistoricalBarsBlockedError(
            "SSE source quality index identity mismatch"
        )
    _strict_nonnegative_int(index.get("row_count"), "quality index row_count")
    return index


def _validate_source_partition(
    partition: Any,
    *,
    source_cas: SSEDelistedRawBarsCAS,
) -> tuple[tuple[str, int, str], dict[str, Any], dict[str, Any]]:
    fields = {
        "exchange",
        "year",
        "code",
        "query_start",
        "query_end",
        "content_hash",
        "object_path",
        "row_count",
        "raw_sources",
    }
    if not isinstance(partition, dict) or set(partition) != fields:
        raise OfficialHistoricalBarsBlockedError(
            "SSE source quality partition schema drift"
        )
    year = _strict_nonnegative_int(partition.get("year"), "partition year")
    code = str(partition.get("code") or "")
    if (
        partition.get("exchange") != "SSE"
        or year not in range(2018, 2024)
        or not re.fullmatch(r"\d{6}\.SH", code)
        or partition.get("query_start") != f"{year:04d}-01-01"
        or partition.get("query_end") != f"{year:04d}-12-31"
    ):
        raise OfficialHistoricalBarsBlockedError(
            "SSE source quality partition identity mismatch"
        )
    _strict_sha256(str(partition.get("content_hash") or ""), "partition")
    _strict_nonnegative_int(partition.get("row_count"), "partition row_count")
    raw_sources = partition.get("raw_sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != 2:
        raise OfficialHistoricalBarsBlockedError(
            "SSE source quality partition raw-source cardinality mismatch"
        )
    raw_fields = {
        "content_hash",
        "object_path",
        "byte_count",
        "protocol_version",
        "authority",
        "role",
    }
    by_role: dict[str, dict[str, Any]] = {}
    authority = RAW_AUTHORITY_BY_DATASET_EXCHANGE[DATASET]["SSE"]
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict) or set(raw_source) != raw_fields:
            raise OfficialHistoricalBarsBlockedError(
                "SSE source quality raw-source schema drift"
            )
        role = str(raw_source.get("role") or "")
        if role in by_role:
            raise OfficialHistoricalBarsBlockedError(
                "SSE source quality partition has duplicate raw-source roles"
            )
        expected_protocol = (
            RAW_ENVELOPE_PROTOCOL_VERSION
            if role == "ROWS_ENVELOPE"
            else PROTOCOL_VERSION
            if role == OFFICIAL_MANIFEST_ROLE
            else ""
        )
        if (
            not expected_protocol
            or raw_source.get("protocol_version") != expected_protocol
            or raw_source.get("authority") != authority
        ):
            raise OfficialHistoricalBarsBlockedError(
                "SSE source quality raw-source identity mismatch"
            )
        digest = _strict_sha256(
            str(raw_source.get("content_hash") or ""),
            "raw source",
        )
        content, _ = source_cas.read_blob(digest)
        if _strict_nonnegative_int(
            raw_source.get("byte_count"), "raw-source byte_count"
        ) != len(content):
            raise OfficialHistoricalBarsBlockedError(
                "SSE source quality raw-source byte_count mismatch"
            )
        by_role[role] = raw_source
    if set(by_role) != {"ROWS_ENVELOPE", OFFICIAL_MANIFEST_ROLE}:
        raise OfficialHistoricalBarsBlockedError(
            "SSE source quality partition raw-source roles are incomplete"
        )
    return (
        ("SSE", year, code),
        by_role["ROWS_ENVELOPE"],
        by_role[OFFICIAL_MANIFEST_ROLE],
    )


def _compare_materialized_quality_indexes(
    *,
    source_index: Mapping[str, Any],
    target_index: Mapping[str, Any],
    manifest_hash_map: Mapping[str, str],
) -> None:
    header_fields = {
        "protocol_version",
        "dataset",
        "source_protocol_version",
        "schema_version",
        "schema",
        "source_authority",
        "coverage_start",
        "coverage_end",
        "row_count",
        "ready",
        "complete",
    }
    if {field: source_index[field] for field in header_fields} != {
        field: target_index[field] for field in header_fields
    }:
        raise OfficialHistoricalBarsBlockedError(
            "materialized SSE quality index header differs from source"
        )

    def normalized_partition(
        partition: Mapping[str, Any],
        *,
        translate_manifest: bool,
    ) -> dict[str, Any]:
        raw_sources: list[dict[str, Any]] = []
        for raw_source in partition["raw_sources"]:
            content_hash = str(raw_source["content_hash"])
            if translate_manifest and raw_source["role"] == OFFICIAL_MANIFEST_ROLE:
                content_hash = str(manifest_hash_map.get(content_hash) or "")
            raw_sources.append(
                {
                    key: value
                    for key, value in raw_source.items()
                    if key not in {"object_path", "byte_count", "content_hash"}
                }
                | {"content_hash": content_hash}
            )
        return {
            key: value
            for key, value in partition.items()
            if key not in {"object_path", "raw_sources"}
        } | {"raw_sources": sorted(raw_sources, key=lambda item: item["role"])}

    def partition_map(
        index: Mapping[str, Any],
        *,
        translate_manifest: bool,
    ) -> dict[tuple[str, int, str], dict[str, Any]]:
        return {
            (
                str(partition["exchange"]),
                int(partition["year"]),
                str(partition["code"]),
            ): normalized_partition(
                partition,
                translate_manifest=translate_manifest,
            )
            for partition in index["partitions"]
        }

    if partition_map(source_index, translate_manifest=True) != partition_map(
        target_index,
        translate_manifest=False,
    ):
        raise OfficialHistoricalBarsBlockedError(
            "materialized SSE quality partitions differ from source evidence"
        )


def _copy_cas_blob(
    source_cas: SSEDelistedRawBarsCAS,
    target_cas: SSEDelistedRawBarsCAS,
    content_hash: str,
    copied_hashes: set[str],
) -> tuple[bytes, Path]:
    digest = _strict_sha256(content_hash, "CAS object")
    content, _ = source_cas.read_blob(digest)
    copied_digest, target_path = target_cas.put_blob(content)
    if copied_digest != digest:
        raise OfficialHistoricalBarsBlockedError(
            "SSE CAS object hash changed during materialization"
        )
    copied_hashes.add(digest)
    return content, target_path


def _parse_canonical_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialHistoricalBarsBlockedError(f"{label} is invalid") from exc
    if not isinstance(value, dict) or _canonical_json_bytes(value) != content:
        raise OfficialHistoricalBarsBlockedError(
            f"{label} is not canonical JSON"
        )
    return value


def _strict_sha256(value: str, label: str) -> str:
    digest = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise OfficialHistoricalBarsBlockedError(f"invalid SSE {label} SHA-256")
    return digest


def _strict_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OfficialHistoricalBarsBlockedError(f"invalid SSE {label}")
    return value


def _lexical_absolute(path: Path) -> Path:
    return Path(path).absolute()


def assess_sse_dayk_cutoff_capture(
    *,
    cutoff_date: str = AUDIT_END,
) -> SSEDayKCutoffCaptureAssessment:
    """Return the fail-closed capability decision for a dated SSE capture.

    The admitted ``dayk`` request has only row-position pagination.  Its
    ``total`` metadata is returned in the same envelope as ``kline`` rows, so
    neither a forward probe nor a negative-index probe can discover the
    2023-12-31 row boundary without first receiving a row on the unknown side
    of that boundary.  A client-side date filter therefore cannot satisfy a
    zero-post-cutoff-response-row contract.
    """

    try:
        parsed_cutoff = date.fromisoformat(str(cutoff_date))
    except ValueError as exc:
        raise ValueError("cutoff_date must be an ISO-8601 calendar date") from exc
    normalized_cutoff = parsed_cutoff.isoformat()
    return SSEDayKCutoffCaptureAssessment(
        protocol_version=CUTOFF_CAPTURE_PROTOCOL_VERSION,
        endpoint=SSE_DAYK_ENDPOINT,
        cutoff_date=normalized_cutoff,
        admitted_query_parameters=("callback", "select", "begin", "end"),
        pagination_interval="HALF_OPEN",
        pagination_basis="ROW_POSITION",
        total_and_rows_share_response_envelope=True,
        server_side_date_bound_admitted=False,
        metadata_only_boundary_probe_admitted=False,
        zero_post_cutoff_response_rows_guaranteed=False,
        safe=False,
        status=CUTOFF_CAPTURE_CONTRACT_UNADMITTED,
        detail=(
            "SSE dayk admits only callback/select and positional begin/end; "
            "it has no admitted server-side date bound or metadata-only total "
            "probe. Locating the cutoff index for a security with later bars "
            "would require receiving at least one row whose date may exceed "
            f"{normalized_cutoff}."
        ),
        promotion_blocked=True,
    )


def require_sse_dayk_cutoff_capture_contract(
    *,
    cutoff_date: str = AUDIT_END,
) -> None:
    """Fail before network access when an SSE dated-capture contract is absent."""

    assessment = assess_sse_dayk_cutoff_capture(cutoff_date=cutoff_date)
    if not assessment.safe:
        raise OfficialHistoricalBarsBlockedError(
            assessment.detail,
            status=assessment.status,
        )


def capture_current_sse_delisted_raw_bars(
    *,
    security_master_root: Path,
    cas_root: Path,
    seed_manifest_sha256s: Sequence[str] = (),
    session: requests.Session | None = None,
    page_size: int = 500,
    timeout_seconds: float = 30.0,
    request_delay_seconds: float = 0.25,
    max_attempts_per_code: int = 2,
    max_new_captures: int | None = None,
) -> SSEDelistedRawBarsBulkCaptureResult:
    """Capture the frozen current-master SSE delisted target set, resumably.

    This orchestration is intentionally limited to the fixed SSE official
    ``dayk`` endpoint used by :class:`OfficialHistoricalBarsClient`.  It never
    reads an account or submits an order.  Every completed code is sealed and
    checkpointed before the next code is requested, and every resumed manifest
    is cold-replayed from CAS before it can be skipped.
    """

    if isinstance(page_size, bool) or not isinstance(page_size, int) or not (
        1 <= page_size <= 5_000
    ):
        raise ValueError("page_size must be an integer from 1 through 5000")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < float(timeout_seconds) <= 300
    ):
        raise ValueError("timeout_seconds must be in (0, 300]")
    if (
        isinstance(request_delay_seconds, bool)
        or not isinstance(request_delay_seconds, (int, float))
        or not 0 <= float(request_delay_seconds) <= 60
    ):
        raise ValueError("request_delay_seconds must be in [0, 60]")
    if (
        isinstance(max_attempts_per_code, bool)
        or not isinstance(max_attempts_per_code, int)
        or not 1 <= max_attempts_per_code <= 5
    ):
        raise ValueError("max_attempts_per_code must be an integer from 1 through 5")
    if max_new_captures is not None and (
        isinstance(max_new_captures, bool)
        or not isinstance(max_new_captures, int)
        or max_new_captures < 0
    ):
        raise ValueError("max_new_captures must be a non-negative integer")

    master = _load_current_sse_delisted_targets(Path(security_master_root))
    target_codes = tuple(master["target_codes"])
    eligible_codes = tuple(master["eligible_codes"])
    deferred_codes = tuple(master["deferred_codes"])
    if len(target_codes) != EXPECTED_CURRENT_SSE_TARGET_COUNT:
        raise OfficialHistoricalBarsBlockedError(
            "current published master SSE delisted target count changed: "
            f"{len(target_codes)} != {EXPECTED_CURRENT_SSE_TARGET_COUNT}"
        )

    cas = SSEDelistedRawBarsCAS(Path(cas_root))
    manifest_store = SSEDelistedRawBarsManifestStore(cas)
    pointer_path = (
        cas.root
        / "capture_checkpoints"
        / f"{master['snapshot_id']}.json"
    )
    state = _load_or_create_bulk_capture_state(
        cas=cas,
        pointer_path=pointer_path,
        master=master,
        target_codes=target_codes,
        page_size=page_size,
    )
    manifests = dict(state["manifests"])
    failures = dict(state["failures"])

    for supplied_hash in seed_manifest_sha256s:
        digest = _strict_sha256(str(supplied_hash), "seed manifest")
        artifact = manifest_store.replay(digest)
        if artifact.code not in eligible_codes:
            raise OfficialHistoricalBarsBlockedError(
                "seed manifest code is outside the pre-2024 capture set: "
                f"{artifact.code}"
            )
        existing = manifests.get(artifact.code)
        if existing is not None and existing != digest:
            raise OfficialHistoricalBarsBlockedError(
                f"seed manifest conflicts with checkpoint for {artifact.code}"
            )
        manifests[artifact.code] = digest
        failures.pop(artifact.code, None)

    resumed_codes: list[str] = []
    for code, digest in sorted(manifests.items()):
        artifact = manifest_store.replay(digest)
        if artifact.code != code or code not in eligible_codes:
            raise OfficialHistoricalBarsBlockedError(
                f"checkpoint manifest identity mismatch for {code}"
            )
        resumed_codes.append(code)

    state = _publish_bulk_capture_state(
        cas=cas,
        pointer_path=pointer_path,
        previous=state,
        manifests=manifests,
        failures=failures,
        quality_index_sha256=str(state.get("quality_index_sha256") or ""),
    )
    captured_codes: list[str] = []
    attempted_count = 0
    client = OfficialHistoricalBarsClient(
        session=session,
        timeout_seconds=float(timeout_seconds),
        cas=cas,  # type: ignore[arg-type]
    )
    for position, code in enumerate(eligible_codes):
        if code in manifests:
            continue
        if max_new_captures is not None and attempted_count >= max_new_captures:
            break
        attempted_count += 1
        last_error: Exception | None = None
        for attempt in range(1, max_attempts_per_code + 1):
            try:
                artifact = client.fetch_sse(
                    code,
                    page_size=page_size,
                    retrieved_at=datetime.now().astimezone().isoformat(),
                )
                reference = manifest_store.seal(artifact)
                manifests[code] = reference.manifest_sha256
                failures.pop(code, None)
                captured_codes.append(code)
                state = _publish_bulk_capture_state(
                    cas=cas,
                    pointer_path=pointer_path,
                    previous=state,
                    manifests=manifests,
                    failures=failures,
                    quality_index_sha256="",
                )
                last_error = None
                break
            except (OfficialHistoricalBarsBlockedError, requests.RequestException, OSError) as exc:
                last_error = exc
                if attempt < max_attempts_per_code:
                    time.sleep(max(1.0, float(request_delay_seconds)))
        if last_error is not None:
            previous_failure = failures.get(code)
            prior_attempts = (
                int(previous_failure.get("attempt_count", 0))
                if isinstance(previous_failure, Mapping)
                else 0
            )
            failures[code] = {
                "attempt_count": prior_attempts + max_attempts_per_code,
                "last_attempted_at": datetime.now().astimezone().isoformat(),
                "error_type": type(last_error).__name__,
                "detail": str(last_error)[:2_000],
            }
            state = _publish_bulk_capture_state(
                cas=cas,
                pointer_path=pointer_path,
                previous=state,
                manifests=manifests,
                failures=failures,
                quality_index_sha256="",
            )
        if (
            request_delay_seconds
            and position < len(eligible_codes) - 1
            and (
                max_new_captures is None
                or attempted_count < max_new_captures
            )
        ):
            time.sleep(float(request_delay_seconds))

    quality_index_sha256 = ""
    if manifests:
        quality_index = build_sse_delisted_raw_bars_quality_index(
            cas_root=cas.root,
            manifest_sha256s=[manifests[code] for code in sorted(manifests)],
        )
        quality_index_sha256 = quality_index.content_hash
    state = _publish_bulk_capture_state(
        cas=cas,
        pointer_path=pointer_path,
        previous=state,
        manifests=manifests,
        failures=failures,
        quality_index_sha256=quality_index_sha256,
    )
    checkpoint_sha256 = str(state["checkpoint_sha256"])
    checkpoint_path = cas.object_path(checkpoint_sha256)
    failed_codes = tuple(
        code for code in eligible_codes if code not in manifests and code in failures
    )
    eligible_capture_complete = len(manifests) == len(eligible_codes)
    deferred_capture_assessment = assess_sse_dayk_cutoff_capture(
        cutoff_date=AUDIT_END
    )
    return SSEDelistedRawBarsBulkCaptureResult(
        snapshot_id=str(master["snapshot_id"]),
        security_master_content_hash=str(master["security_master_content_hash"]),
        target_codes=target_codes,
        eligible_codes=eligible_codes,
        deferred_codes=deferred_codes,
        manifest_sha256s=tuple(manifests[code] for code in sorted(manifests)),
        resumed_codes=tuple(resumed_codes),
        captured_codes=tuple(captured_codes),
        failed_codes=failed_codes,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_path=str(checkpoint_path.resolve()),
        checkpoint_pointer_path=str(pointer_path.resolve()),
        quality_index_sha256=quality_index_sha256,
        deferred_capture_assessment=deferred_capture_assessment,
        eligible_capture_complete=eligible_capture_complete,
        complete=eligible_capture_complete and not deferred_codes,
    )


def _load_current_sse_delisted_targets(
    security_master_root: Path,
) -> dict[str, Any]:
    release = HistoricalSecurityMasterStore(security_master_root).load_current_release()
    snapshot_id = _strict_sha256(str(release.get("snapshot_id") or ""), "master snapshot")
    manifest = release.get("manifest")
    if not isinstance(manifest, Mapping):
        raise OfficialHistoricalBarsBlockedError("current security-master manifest is invalid")
    try:
        artifact = dict(manifest["artifacts"]["security_master_jsonl"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OfficialHistoricalBarsBlockedError(
            "current security master has no JSONL artifact"
        ) from exc
    content_hash = _strict_sha256(
        str(artifact.get("content_hash") or ""), "security master content"
    )
    expected_path = security_master_root / "objects" / content_hash[:2] / content_hash
    object_path = Path(str(artifact.get("object_path") or ""))
    if object_path.resolve() != expected_path.resolve():
        raise OfficialHistoricalBarsBlockedError(
            "security-master JSONL path does not match its content hash"
        )
    content = _stable_read(object_path, "security-master JSONL")
    if _sha256(content) != content_hash:
        raise OfficialHistoricalBarsBlockedError("security-master JSONL hash mismatch")
    lines = content.splitlines()
    if int(artifact.get("row_count", -1)) != len(lines):
        raise OfficialHistoricalBarsBlockedError("security-master JSONL row count mismatch")

    audit_start = date.fromisoformat(AUDIT_START)
    audit_end_exclusive = date.fromisoformat(AUDIT_END) + timedelta(days=1)
    code_delisted_at: dict[str, date] = {}
    for line in lines:
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OfficialHistoricalBarsBlockedError(
                "security-master JSONL contains invalid JSON"
            ) from exc
        if (
            not isinstance(row, dict)
            or set(row) != set(MASTER_RECORD_FIELDS)
            or _canonical_json_bytes(row) != line
        ):
            raise OfficialHistoricalBarsBlockedError(
                "security-master JSONL schema or canonical form changed"
            )
        if str(row["exchange"]).upper() != "SSE" or row.get("delisted_at") is None:
            continue
        code = str(row["code_alias"]).upper()
        if not re.fullmatch(r"\d{6}\.SH", code):
            raise OfficialHistoricalBarsBlockedError(
                f"security-master contains invalid SSE alias: {code!r}"
            )
        try:
            listed = date.fromisoformat(str(row["listed_at"]))
            valid_from = date.fromisoformat(str(row["valid_from"]))
            delisted = date.fromisoformat(str(row["delisted_at"]))
            valid_to = (
                date.fromisoformat(str(row["valid_to"]))
                if row.get("valid_to") is not None
                else delisted
            )
        except ValueError as exc:
            raise OfficialHistoricalBarsBlockedError(
                f"security-master contains invalid SSE interval for {code}"
            ) from exc
        start = max(audit_start, listed, valid_from)
        end_exclusive = min(audit_end_exclusive, delisted, valid_to)
        if start < end_exclusive:
            if code in code_delisted_at:
                raise OfficialHistoricalBarsBlockedError(
                    f"security-master has duplicate SSE delisted target: {code}"
                )
            code_delisted_at[code] = delisted
    target_codes = tuple(sorted(code_delisted_at))
    audit_end = date.fromisoformat(AUDIT_END)
    eligible_codes = tuple(
        code for code in target_codes if code_delisted_at[code] <= audit_end
    )
    deferred_codes = tuple(
        code for code in target_codes if code_delisted_at[code] > audit_end
    )
    if set(eligible_codes) & set(deferred_codes) or (
        set(eligible_codes) | set(deferred_codes)
    ) != set(target_codes):
        raise OfficialHistoricalBarsBlockedError(
            "SSE target capture-boundary classification is inconsistent"
        )
    return {
        "snapshot_id": snapshot_id,
        "security_master_content_hash": content_hash,
        "target_codes": target_codes,
        "target_codes_sha256": _sha256(_canonical_json_bytes(list(target_codes))),
        "eligible_codes": eligible_codes,
        "eligible_codes_sha256": _sha256(
            _canonical_json_bytes(list(eligible_codes))
        ),
        "deferred_codes": deferred_codes,
        "deferred_codes_sha256": _sha256(
            _canonical_json_bytes(list(deferred_codes))
        ),
    }


def _load_or_create_bulk_capture_state(
    *,
    cas: SSEDelistedRawBarsCAS,
    pointer_path: Path,
    master: Mapping[str, Any],
    target_codes: Sequence[str],
    page_size: int,
) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat()
    base = {
        "protocol_version": BULK_CAPTURE_PROTOCOL_VERSION,
        "snapshot_id": master["snapshot_id"],
        "security_master_content_hash": master["security_master_content_hash"],
        "audit_start": AUDIT_START,
        "audit_end": AUDIT_END,
        "target_codes": list(target_codes),
        "target_codes_sha256": master["target_codes_sha256"],
        "eligible_codes": list(master["eligible_codes"]),
        "eligible_codes_sha256": master["eligible_codes_sha256"],
        "deferred_codes": list(master["deferred_codes"]),
        "deferred_codes_sha256": master["deferred_codes_sha256"],
        "page_size": page_size,
        "endpoint": SSE_DAYK_ENDPOINT,
        "method": "GET",
        "manifests": {},
        "failures": {},
        "quality_index_sha256": "",
        "created_at": now,
        "updated_at": now,
    }
    if not pointer_path.exists():
        return base
    pointer_bytes = _stable_read(pointer_path, "SSE bulk-capture pointer")
    pointer = _parse_canonical_json_object(pointer_bytes, "SSE bulk-capture pointer")
    if set(pointer) != {
        "protocol_version",
        "snapshot_id",
        "checkpoint_sha256",
        "object_path",
    }:
        raise OfficialHistoricalBarsBlockedError("SSE bulk-capture pointer schema drift")
    digest = _strict_sha256(str(pointer.get("checkpoint_sha256") or ""), "checkpoint")
    content, object_path = cas.read_blob(digest)
    if (
        pointer.get("protocol_version") != BULK_CAPTURE_PROTOCOL_VERSION
        or pointer.get("snapshot_id") != master["snapshot_id"]
        or Path(str(pointer.get("object_path") or "")).resolve() != object_path.resolve()
    ):
        raise OfficialHistoricalBarsBlockedError("SSE bulk-capture pointer identity mismatch")
    state = _parse_canonical_json_object(content, "SSE bulk-capture checkpoint")
    expected_fields = set(base)
    if set(state) != expected_fields:
        raise OfficialHistoricalBarsBlockedError("SSE bulk-capture checkpoint schema drift")
    frozen_fields = (
        "protocol_version",
        "snapshot_id",
        "security_master_content_hash",
        "audit_start",
        "audit_end",
        "target_codes",
        "target_codes_sha256",
        "eligible_codes",
        "eligible_codes_sha256",
        "deferred_codes",
        "deferred_codes_sha256",
        "page_size",
        "endpoint",
        "method",
    )
    if any(state.get(field) != base[field] for field in frozen_fields):
        raise OfficialHistoricalBarsBlockedError(
            "SSE bulk-capture checkpoint no longer matches the current master or request contract"
        )
    manifests = state.get("manifests")
    failures = state.get("failures")
    if not isinstance(manifests, dict) or not isinstance(failures, dict):
        raise OfficialHistoricalBarsBlockedError("SSE bulk-capture checkpoint maps are invalid")
    eligible_codes = set(state["eligible_codes"])
    if not set(manifests).issubset(eligible_codes) or not set(failures).issubset(
        eligible_codes
    ):
        raise OfficialHistoricalBarsBlockedError("SSE bulk-capture checkpoint code set drift")
    if set(manifests) & set(failures):
        raise OfficialHistoricalBarsBlockedError(
            "SSE bulk-capture checkpoint marks a code both complete and failed"
        )
    for code, digest_value in manifests.items():
        _strict_sha256(str(digest_value), f"checkpoint manifest for {code}")
    quality_hash = str(state.get("quality_index_sha256") or "")
    if quality_hash:
        cas.read_blob(_strict_sha256(quality_hash, "checkpoint quality index"))
    return state


def _publish_bulk_capture_state(
    *,
    cas: SSEDelistedRawBarsCAS,
    pointer_path: Path,
    previous: Mapping[str, Any],
    manifests: Mapping[str, str],
    failures: Mapping[str, Mapping[str, Any]],
    quality_index_sha256: str,
) -> dict[str, Any]:
    state = dict(previous)
    state.pop("checkpoint_sha256", None)
    state["manifests"] = dict(sorted(manifests.items()))
    state["failures"] = {
        code: dict(value) for code, value in sorted(failures.items())
    }
    state["quality_index_sha256"] = str(quality_index_sha256)
    state["updated_at"] = datetime.now().astimezone().isoformat()
    content = _canonical_json_bytes(state)
    digest, object_path = cas.put_blob(content)
    pointer = {
        "protocol_version": BULK_CAPTURE_PROTOCOL_VERSION,
        "snapshot_id": state["snapshot_id"],
        "checkpoint_sha256": digest,
        "object_path": str(object_path),
    }
    _atomic_replace_exact(
        pointer_path,
        _canonical_json_bytes(pointer),
        label="SSE bulk-capture pointer",
    )
    replayed = _parse_canonical_json_object(
        _stable_read(pointer_path, "published SSE bulk-capture pointer"),
        "published SSE bulk-capture pointer",
    )
    if replayed != pointer:
        raise OfficialHistoricalBarsBlockedError(
            "published SSE bulk-capture pointer verification failed"
        )
    return {**state, "checkpoint_sha256": digest}


def _atomic_replace_exact(path: Path, content: bytes, *, label: str) -> None:
    _validate_no_reparse(path.parent, f"{label} parent")
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_no_reparse(path.parent, f"{label} parent")
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
        if _stable_read(temporary, f"{label} temporary") != content:
            raise OfficialHistoricalBarsBlockedError(
                f"{label} temporary verification failed"
            )
        if path.exists():
            _validate_no_reparse(path, f"existing {label}")
        os.replace(temporary, path)
        if _stable_read(path, f"published {label}") != content:
            raise OfficialHistoricalBarsBlockedError(
                f"published {label} verification failed"
            )
    finally:
        if temporary.exists():
            temporary.unlink()


def _manifest_from_artifact(
    artifact: OfficialBarsArtifact,
    cas: SSEDelistedRawBarsCAS,
) -> dict[str, Any]:
    _validate_sse_artifact_identity(artifact)
    raw_pages = [item.to_dict() for item in artifact.raw_responses]
    if not raw_pages:
        raise OfficialHistoricalBarsBlockedError("SSE artifact has no raw pages")
    for raw_page in raw_pages:
        content_hash = str(raw_page.get("content_sha256") or "")
        content, expected_path = cas.read_blob(content_hash)
        if (
            raw_page.get("persisted") is not True
            or raw_page.get("cas_uri") != f"sha256:{content_hash}"
            or Path(str(raw_page.get("object_path") or "")).resolve()
            != expected_path.resolve()
            or int(raw_page.get("byte_count", -1)) != len(content)
        ):
            raise OfficialHistoricalBarsBlockedError(
                "SSE raw page CAS identity mismatch"
            )
    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "official_bars_protocol_version": OFFICIAL_BARS_PROTOCOL_VERSION,
        "parser_contract": PARSER_CONTRACT,
        "parser_contract_sha256": PARSER_CONTRACT_SHA256,
        "dataset": DATASET,
        "exchange": "SSE",
        "code": artifact.code,
        "source_url": artifact.source_url,
        "scope": {
            "allowed_use": "UNADJUSTED_RAW_EXECUTION_BAR_EVIDENCE_ONLY",
            "adjusted_features_allowed": False,
            "corporate_action_inference_allowed": False,
            "label_generation_allowed": False,
            "promotion_ready": False,
            "szse_coverage": False,
        },
        "pagination": dict(artifact.pagination),
        "normalized": {
            "row_count": len(artifact.bars),
            "logical_content_sha256": artifact.logical_content_sha256,
        },
        "raw_pages": raw_pages,
    }
    replayed = _artifact_from_manifest(manifest, cas)
    if tuple(item.to_dict() for item in replayed.bars) != tuple(
        item.to_dict() for item in artifact.bars
    ):
        raise OfficialHistoricalBarsBlockedError(
            "SSE artifact bars do not replay from the raw JSONP pages"
        )
    return manifest


def _artifact_from_manifest(
    manifest: Mapping[str, Any],
    cas: SSEDelistedRawBarsCAS,
) -> OfficialBarsArtifact:
    expected_fields = {
        "protocol_version",
        "official_bars_protocol_version",
        "parser_contract",
        "parser_contract_sha256",
        "dataset",
        "exchange",
        "code",
        "source_url",
        "scope",
        "pagination",
        "normalized",
        "raw_pages",
    }
    if set(manifest) != expected_fields:
        raise OfficialHistoricalBarsBlockedError("SSE manifest schema drift")
    code = str(manifest.get("code") or "")
    source_url = SSE_DAYK_ENDPOINT.format(code=code.removesuffix(".SH"))
    if (
        manifest.get("protocol_version") != PROTOCOL_VERSION
        or manifest.get("official_bars_protocol_version")
        != OFFICIAL_BARS_PROTOCOL_VERSION
        or manifest.get("parser_contract") != PARSER_CONTRACT
        or manifest.get("parser_contract_sha256") != PARSER_CONTRACT_SHA256
        or manifest.get("dataset") != DATASET
        or manifest.get("exchange") != "SSE"
        or not re.fullmatch(r"\d{6}\.SH", code)
        or manifest.get("source_url") != source_url
    ):
        raise OfficialHistoricalBarsBlockedError("SSE manifest identity mismatch")
    scope = manifest.get("scope")
    if scope != {
        "allowed_use": "UNADJUSTED_RAW_EXECUTION_BAR_EVIDENCE_ONLY",
        "adjusted_features_allowed": False,
        "corporate_action_inference_allowed": False,
        "label_generation_allowed": False,
        "promotion_ready": False,
        "szse_coverage": False,
    }:
        raise OfficialHistoricalBarsBlockedError("SSE manifest scope drift")
    raw_pages = manifest.get("raw_pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise OfficialHistoricalBarsBlockedError("SSE manifest has no raw pages")

    bars: list[OfficialDailyBar] = []
    evidence: list[RawResponseEvidence] = []
    total: int | None = None
    next_begin = 0
    interval_semantics: set[str] = set()
    page_size: int | None = None
    raw_evidence_fields = {
        "source_url",
        "method",
        "retrieved_at",
        "content_sha256",
        "byte_count",
        "content_type",
        "cas_uri",
        "object_path",
        "persisted",
        "request",
        "response",
    }
    for position, raw_page in enumerate(raw_pages):
        if not isinstance(raw_page, dict) or set(raw_page) != raw_evidence_fields:
            raise OfficialHistoricalBarsBlockedError("SSE raw page schema drift")
        content_hash = str(raw_page.get("content_sha256") or "")
        content, expected_path = cas.read_blob(content_hash)
        request = raw_page.get("request")
        response = raw_page.get("response")
        if not isinstance(request, dict) or set(request) != {
            "callback",
            "select",
            "begin",
            "end",
        }:
            raise OfficialHistoricalBarsBlockedError("SSE raw page request drift")
        if not isinstance(response, dict) or set(response) != {
            "code",
            "total",
            "begin",
            "end",
            "interval",
            "interval_semantics",
            "row_count",
        }:
            raise OfficialHistoricalBarsBlockedError("SSE raw page response drift")
        if (
            raw_page.get("method") != "GET"
            or raw_page.get("persisted") is not True
            or raw_page.get("cas_uri") != f"sha256:{content_hash}"
            or Path(str(raw_page.get("object_path") or "")).resolve()
            != expected_path.resolve()
            or int(raw_page.get("byte_count", -1)) != len(content)
            or request.get("callback") != SSE_JSONP_CALLBACK
            or request.get("select") != SSE_DAYK_SELECT
            or str(raw_page.get("content_type") or "").lower()
            not in {
                "application/javascript",
                "text/javascript",
                "application/json",
                "text/plain",
            }
        ):
            raise OfficialHistoricalBarsBlockedError("SSE raw page identity mismatch")
        _strict_retrieved_at(raw_page.get("retrieved_at"))
        _validate_sse_response_url(
            str(raw_page.get("source_url") or ""),
            source_url=source_url,
            request=request,
        )
        begin = _strict_manifest_int(request.get("begin"), "request begin")
        end = _strict_manifest_int(request.get("end"), "request end")
        if begin != next_begin or end <= begin:
            raise OfficialHistoricalBarsBlockedError(
                "SSE raw page pagination is not contiguous"
            )
        if page_size is None:
            page_size = end - begin
        elif end - begin > page_size:
            raise OfficialHistoricalBarsBlockedError("SSE raw page size increased")
        page = parse_sse_dayk_page(
            content,
            code=code,
            request_begin=begin,
            request_end=end,
            callback=SSE_JSONP_CALLBACK,
            expected_sha256=content_hash,
        )
        if total is None:
            total = page.total
        elif page.total != total:
            raise OfficialHistoricalBarsBlockedError("SSE total changed across pages")
        expected_response = {
            "code": page.code,
            "total": page.total,
            "begin": page.response_begin,
            "end": page.response_end,
            "interval": "HALF_OPEN",
            "interval_semantics": page.response_interval_semantics,
            "row_count": len(page.bars),
        }
        if response != expected_response:
            raise OfficialHistoricalBarsBlockedError(
                f"SSE raw page {position} response metadata mismatch"
            )
        evidence.append(
            RawResponseEvidence(
                source_url=str(raw_page["source_url"]),
                method="GET",
                retrieved_at=str(raw_page["retrieved_at"]),
                content_sha256=content_hash,
                byte_count=len(content),
                content_type=str(raw_page["content_type"]),
                cas_uri=f"sha256:{content_hash}",
                object_path=str(expected_path),
                persisted=True,
                request=dict(request),
                response=dict(response),
            )
        )
        bars.extend(page.bars)
        next_begin = page.normalized_end
        interval_semantics.add(page.response_interval_semantics)
    if total is None or next_begin != total or len(bars) != total:
        raise OfficialHistoricalBarsBlockedError(
            "SSE manifest pagination coverage is incomplete"
        )
    dates = [item.date for item in bars]
    if len(dates) != len(set(dates)) or dates != sorted(dates):
        raise OfficialHistoricalBarsBlockedError(
            "SSE manifest bars are not globally unique and ordered"
        )
    pagination = manifest.get("pagination")
    expected_pagination = {
        "supported": True,
        "interval": "HALF_OPEN",
        "page_size": page_size,
        "page_count": len(raw_pages),
        "total": total,
        "response_interval_semantics": sorted(interval_semantics),
        "complete": True,
    }
    if pagination != expected_pagination:
        raise OfficialHistoricalBarsBlockedError("SSE manifest pagination drift")
    logical_hash = _official_logical_hash(code, bars)
    normalized = manifest.get("normalized")
    if normalized != {
        "row_count": len(bars),
        "logical_content_sha256": logical_hash,
    }:
        raise OfficialHistoricalBarsBlockedError(
            "SSE manifest normalized identity mismatch"
        )
    return OfficialBarsArtifact(
        exchange="SSE",
        code=code,
        source_url=source_url,
        bars=tuple(bars),
        raw_responses=tuple(evidence),
        logical_content_sha256=logical_hash,
        pagination=expected_pagination,
    )


def _validate_sse_artifact_identity(artifact: OfficialBarsArtifact) -> None:
    if (
        artifact.exchange != "SSE"
        or not re.fullmatch(r"\d{6}\.SH", artifact.code)
        or artifact.source_url
        != SSE_DAYK_ENDPOINT.format(code=artifact.code.removesuffix(".SH"))
    ):
        raise OfficialHistoricalBarsBlockedError(
            "only official SSE artifacts are admitted"
        )
    if artifact.usage_gate.get("label_generation_allowed") is not False:
        raise OfficialHistoricalBarsBlockedError(
            "SSE raw bars unexpectedly permit label generation"
        )


def _official_logical_hash(
    code: str, bars: Sequence[OfficialDailyBar]
) -> str:
    return _sha256(
        _canonical_json_bytes(
            {
                "protocol_version": OFFICIAL_BARS_PROTOCOL_VERSION,
                "exchange": "SSE",
                "code": code,
                "bars": [item.to_dict() for item in bars],
            }
        )
    )


def _strict_manifest_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise OfficialHistoricalBarsBlockedError(f"invalid SSE {label}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OfficialHistoricalBarsBlockedError(f"invalid SSE {label}") from exc
    if str(value).strip() not in {str(parsed), f"+{parsed}"} or parsed < 0:
        raise OfficialHistoricalBarsBlockedError(f"invalid SSE {label}")
    return parsed


def _validate_sse_response_url(
    value: str,
    *,
    source_url: str,
    request: Mapping[str, Any],
) -> None:
    observed = urlsplit(value)
    expected = urlsplit(source_url)
    if (
        observed.scheme != expected.scheme
        or observed.netloc != expected.netloc
        or observed.path != expected.path
        or observed.fragment
    ):
        raise OfficialHistoricalBarsBlockedError("SSE raw page source URL drift")
    if not observed.query:
        raise OfficialHistoricalBarsBlockedError(
            "SSE raw page source URL is missing the request query"
        )
    query = parse_qs(observed.query, keep_blank_values=True, strict_parsing=True)
    expected_query = {
        "callback": [str(request["callback"])],
        "select": [str(request["select"])],
        "begin": [str(request["begin"])],
        "end": [str(request["end"])],
    }
    if query != expected_query:
        raise OfficialHistoricalBarsBlockedError(
            "SSE raw page source query does not match request metadata"
        )


def _strict_retrieved_at(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OfficialHistoricalBarsBlockedError(
            "SSE raw page retrieved_at is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OfficialHistoricalBarsBlockedError(
            "SSE raw page retrieved_at must include a timezone"
        )
    if parsed.isoformat() != text:
        raise OfficialHistoricalBarsBlockedError(
            "SSE raw page retrieved_at is not canonical ISO-8601"
        )
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


def _canonical_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    if not rows:
        return b""
    return b"\n".join(_canonical_json_bytes(dict(row)) for row in rows) + b"\n"


def _validate_no_reparse(path: Path, label: str) -> None:
    current = path
    while True:
        if current.exists():
            metadata = os.lstat(current)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode) or (
                attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise OfficialHistoricalBarsBlockedError(
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
        raise OfficialHistoricalBarsBlockedError(
            f"{label} cannot be opened as a stable file"
        ) from exc
    try:
        before = os.fstat(descriptor)
        attributes = int(getattr(before, "st_file_attributes", 0))
        if not stat.S_ISREG(before.st_mode) or (
            attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OfficialHistoricalBarsBlockedError(
                f"{label} is not a plain regular file"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_before != identity_after or len(content) != before.st_size:
            raise OfficialHistoricalBarsBlockedError(f"{label} changed while read")
    finally:
        os.close(descriptor)
    _validate_no_reparse(path, label)
    return content


def _atomic_write_exact(path: Path, content: bytes) -> None:
    _validate_no_reparse(path.parent, "SSE delisted raw-bars CAS parent")
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_no_reparse(path.parent, "SSE delisted raw-bars CAS parent")
    if path.exists():
        if _stable_read(path, "existing SSE raw-bars CAS object") != content:
            raise OfficialHistoricalBarsBlockedError(
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
        if _stable_read(temporary, "SSE raw-bars CAS temporary") != content:
            raise OfficialHistoricalBarsBlockedError(
                "SSE raw-bars CAS temporary verification failed"
            )
        if path.exists():
            if _stable_read(path, "existing SSE raw-bars CAS object") != content:
                raise OfficialHistoricalBarsBlockedError(
                    "content-address collision or corruption"
                )
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
