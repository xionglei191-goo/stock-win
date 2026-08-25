from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from research_platform import cninfo_delisted_disclosures as cninfo
from research_platform import official_trading_calendar as official_calendar
from research_platform.delisted_history_quality import (
    AUDIT_END,
    AUDIT_START,
    DATASET_CONTRACTS,
    RAW_AUTHORITY_BY_DATASET_EXCHANGE,
    RAW_ENVELOPE_PROTOCOL_VERSION,
    SOURCE_INDEX_AUTHORITY,
    SOURCE_INDEX_PROTOCOL_VERSION,
)


PROTOCOL_VERSION = "cninfo-announcement-documents-quality-adapter-v2"
EFFECTIVE_AT_PROTOCOL_VERSION = "cninfo-announcement-effective-at-v2"
UPSTREAM_EVIDENCE_KIND = "CNINFO_SZSE_ANNOUNCEMENTS_WITH_OFFICIAL_CALENDAR_V1"

DATASET = "announcement_documents"
EXCHANGE = "SZSE"
TIMEZONE_NAME = "Asia/Shanghai"
STANDARD_MARKET_CLOSE = time(15, 0)
CNINFO_MANIFEST_ROLE = "CNINFO_DELISTED_DISCLOSURE_MANIFEST"
OFFICIAL_CALENDAR_MANIFEST_ROLE = "OFFICIAL_TRADING_CALENDAR_MANIFEST"
SOURCE_DOCUMENT_PROTOCOL_VERSION = f"{DATASET}-source-document-v1"
OVERALL_STATUS = "SZSE_ONLY_OVERALL_DELISTED_HISTORY_INCOMPLETE"

_CHINA = ZoneInfo(TIMEZONE_NAME)


class CninfoAnnouncementQualityAdapterBlockedError(RuntimeError):
    """CNINFO announcement evidence cannot be admitted to a quality index."""


@dataclass(frozen=True)
class AnnouncementDocumentsQualityIndexReference:
    content_hash: str
    object_path: str
    byte_count: int
    cninfo_manifest_sha256: str
    cninfo_logical_content_sha256: str
    calendar_manifest_sha256: str
    calendar_logical_content_sha256: str
    master_snapshot_id: str
    master_scope_sha256: str
    partition_count: int
    row_count: int
    empty_partition_count: int
    szse_required_annual_coverage_complete: bool
    ready: bool = False
    complete: bool = False
    overall_status: str = OVERALL_STATUS

    def to_source_identity(self) -> dict[str, str]:
        return {
            "content_hash": self.content_hash,
            "object_path": self.object_path,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_cninfo_announcement_documents_quality_index(
    *,
    cas_root: Path,
    cninfo_manifest_sha256: str,
    calendar_manifest_sha256: str,
    authoritative_master_snapshot_id: str,
    authoritative_targets: Sequence[cninfo.FrozenDisclosureTarget],
) -> AnnouncementDocumentsQualityIndexReference:
    """Build a SZSE-only announcement index from two cold-replayed manifests.

    ``authoritative_master_snapshot_id`` and ``authoritative_targets`` must come
    from the separately verified historical security-master release.  There is
    deliberately no caller-supplied ``ready`` flag: identity and exact scope are
    compared with the cold-replayed CNINFO manifest instead.

    Effective-time protocol:

    * DATE_ONLY publications become effective at 00:00 China time on the first
      official open session on or after the published date.
    * Precise timestamps on an official open session before the standard 15:00
      close retain the source timestamp.
    * Precise timestamps at/after 15:00, or on a closed date, become effective
      at 00:00 on the next official open session.  This is fail-closed when the
      admitted calendar has no such session.
    * Each target interval is inclusive at both ends.  Publications and their
      derived effective dates must both remain inside that exact interval.
      Only calendar years intersecting the interval receive partitions.

    This adapter emits no structured financial or earnings values.  Its source
    index always remains overall-incomplete because SSE disclosures require an
    independent SSE official source.
    """

    root = Path(cas_root)
    disclosure_cas = cninfo.CninfoDisclosureCAS(root)
    try:
        cninfo_manifest_bytes, cninfo_manifest_path = disclosure_cas.read_blob(
            cninfo_manifest_sha256
        )
        disclosure = cninfo.CninfoDelistedDisclosureManifestStore(
            disclosure_cas
        ).replay(cninfo_manifest_sha256)
    except cninfo.CninfoDelistedDisclosureBlockedError as exc:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            f"CNINFO manifest failed cold replay: {exc}"
        ) from exc

    calendar_cas = official_calendar.OfficialTradingCalendarCAS(root)
    try:
        calendar_manifest_bytes, calendar_manifest_path = calendar_cas.read_blob(
            calendar_manifest_sha256
        )
        calendar = official_calendar.OfficialTradingCalendarManifestStore(
            calendar_cas
        ).replay(calendar_manifest_sha256)
    except official_calendar.OfficialTradingCalendarBlockedError as exc:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            f"official calendar manifest failed cold replay: {exc}"
        ) from exc

    snapshot_id = _sha256_identity(
        authoritative_master_snapshot_id, "authoritative master snapshot"
    )
    targets = _normalize_authoritative_targets(authoritative_targets)
    if disclosure.master_snapshot_id != snapshot_id:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "CNINFO master_snapshot_id does not match authoritative master"
        )
    if tuple(item.to_dict() for item in disclosure.targets) != tuple(
        item.to_dict() for item in targets
    ):
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "CNINFO targets do not match authoritative master scope"
        )

    calendar_rows = _calendar_rows(calendar)
    effective_rows = _announcement_rows(disclosure, targets, calendar_rows)
    documents_by_id = {
        str(item["announcement_id"]): dict(item["raw"])
        for item in disclosure.documents
    }
    if len(documents_by_id) != len(disclosure.documents):
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "CNINFO replay contains duplicate source documents"
        )

    contract = DATASET_CONTRACTS[DATASET]
    authority = RAW_AUTHORITY_BY_DATASET_EXCHANGE[DATASET][EXCHANGE]
    partitions: list[dict[str, Any]] = []
    total_rows = 0
    empty_partitions = 0
    for target in targets:
        target_rows = [row for row in effective_rows if row["code"] == target.code]
        for year in _target_years(target):
            rows = [
                row
                for row in target_rows
                if datetime.fromisoformat(str(row["effective_at"])).year == year
            ]
            rows.sort(
                key=lambda row: (
                    str(row["effective_at"]),
                    str(row["published_at"]),
                    str(row["announcement_id"]),
                )
            )
            normalized_bytes = _canonical_jsonl(rows)
            normalized_hash, normalized_path = _put_derived_blob(
                disclosure_cas, normalized_bytes
            )
            envelope = {
                "protocol_version": RAW_ENVELOPE_PROTOCOL_VERSION,
                "authority": authority,
                "dataset": DATASET,
                "exchange": EXCHANGE,
                "year": year,
                "code": target.code,
                "schema": list(contract.schema),
                "rows": rows,
            }
            envelope_bytes = _canonical_json_bytes(envelope)
            envelope_hash, envelope_path = _put_derived_blob(
                disclosure_cas, envelope_bytes
            )
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
            for row in rows:
                announcement_id = str(row["announcement_id"])
                raw = documents_by_id.get(announcement_id)
                if raw is None or raw.get("content_hash") != row["content_hash"]:
                    raise CninfoAnnouncementQualityAdapterBlockedError(
                        "announcement row is not bound to its replayed CNINFO PDF"
                    )
                raw_sources.append(
                    {
                        "content_hash": str(raw["content_hash"]),
                        "object_path": str(raw["object_path"]),
                        "byte_count": int(raw["byte_count"]),
                        "protocol_version": SOURCE_DOCUMENT_PROTOCOL_VERSION,
                        "authority": authority,
                        "role": "SOURCE_DOCUMENT",
                    }
                )
            partitions.append(
                {
                    "exchange": EXCHANGE,
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
            empty_partitions += not rows

    master_scope = {
        "snapshot_id": snapshot_id,
        "targets": [item.to_dict() for item in targets],
    }
    master_scope_sha256 = _sha256(_canonical_json_bytes(master_scope))
    annual_complete = empty_partitions == 0
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
            "adapter_protocol_version": PROTOCOL_VERSION,
            "cninfo": {
                "authority": cninfo.SOURCE_AUTHORITY,
                "protocol_version": cninfo.PROTOCOL_VERSION,
                "manifest_sha256": cninfo_manifest_sha256,
                "logical_content_sha256": disclosure.logical_content_sha256,
                "cas_uri": f"sha256:{cninfo_manifest_sha256}",
                "object_path": str(cninfo_manifest_path),
                "byte_count": len(cninfo_manifest_bytes),
            },
            "official_trading_calendar": {
                "authority": "SSE_SZSE_OFFICIAL_TRADING_CALENDAR",
                "protocol_version": official_calendar.PROTOCOL_VERSION,
                "manifest_sha256": calendar_manifest_sha256,
                "logical_content_sha256": calendar.logical_content_sha256,
                "cas_uri": f"sha256:{calendar_manifest_sha256}",
                "object_path": str(calendar_manifest_path),
                "byte_count": len(calendar_manifest_bytes),
            },
            "master_scope": {
                **master_scope,
                "scope_sha256": master_scope_sha256,
                "target_count": len(targets),
                "caller_ready_accepted": False,
                "target_interval_semantics": "CLOSED_START_AND_END",
            },
            "effective_at_protocol": {
                "protocol_version": EFFECTIVE_AT_PROTOCOL_VERSION,
                "timezone": TIMEZONE_NAME,
                "date_only_rule": "FIRST_OPEN_ON_OR_AFTER_DATE_AT_00:00",
                "precise_open_pre_close_rule": "PRESERVE_SOURCE_TIMESTAMP",
                "closed_or_at_after_close_rule": "NEXT_OPEN_DATE_AT_00:00",
                "standard_market_close": STANDARD_MARKET_CLOSE.isoformat(),
                "calendar_day_resolution_only": True,
                "missing_next_session_fails_closed": True,
                "publication_must_be_inside_target_interval": True,
                "effective_at_must_be_inside_target_interval": True,
            },
            "coverage": {
                "exchange": EXCHANGE,
                "partition_count": len(partitions),
                "empty_partition_count": empty_partitions,
                "partition_year_rule": (
                    "ONLY_YEARS_INTERSECTING_TARGET_CLOSED_INTERVAL"
                ),
                "annual_partition_boundaries_remain_calendar_years": True,
                "szse_required_annual_rows_complete": annual_complete,
                "sse_source_present": False,
                "overall_status": OVERALL_STATUS,
            },
        },
        # The generic loader ignores producer promotion claims.  These fixed
        # false values make the adapter's limited scope explicit to humans too.
        "ready": False,
        "complete": False,
    }
    index_bytes = _canonical_json_bytes(index)
    index_hash, index_path = _put_derived_blob(disclosure_cas, index_bytes)
    return AnnouncementDocumentsQualityIndexReference(
        content_hash=index_hash,
        object_path=str(index_path),
        byte_count=len(index_bytes),
        cninfo_manifest_sha256=cninfo_manifest_sha256,
        cninfo_logical_content_sha256=disclosure.logical_content_sha256,
        calendar_manifest_sha256=calendar_manifest_sha256,
        calendar_logical_content_sha256=calendar.logical_content_sha256,
        master_snapshot_id=snapshot_id,
        master_scope_sha256=master_scope_sha256,
        partition_count=len(partitions),
        row_count=total_rows,
        empty_partition_count=empty_partitions,
        szse_required_annual_coverage_complete=annual_complete,
    )


def replay_cninfo_announcement_documents_quality_index(
    *,
    cas_root: Path,
    source_index_sha256: str,
    cninfo_manifest_sha256: str,
    calendar_manifest_sha256: str,
    authoritative_master_snapshot_id: str,
    authoritative_targets: Sequence[cninfo.FrozenDisclosureTarget],
) -> AnnouncementDocumentsQualityIndexReference:
    """Cold replay an index and reject even internally consistent rewrites."""

    cas = cninfo.CninfoDisclosureCAS(Path(cas_root))
    try:
        observed, observed_path = cas.read_blob(source_index_sha256)
    except cninfo.CninfoDelistedDisclosureBlockedError as exc:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            f"announcement source index failed CAS replay: {exc}"
        ) from exc
    try:
        parsed = json.loads(observed.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "announcement source index is not UTF-8 JSON"
        ) from exc
    if not isinstance(parsed, dict) or _canonical_json_bytes(parsed) != observed:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "announcement source index is not canonical JSON"
        )

    rebuilt = build_cninfo_announcement_documents_quality_index(
        cas_root=cas_root,
        cninfo_manifest_sha256=cninfo_manifest_sha256,
        calendar_manifest_sha256=calendar_manifest_sha256,
        authoritative_master_snapshot_id=authoritative_master_snapshot_id,
        authoritative_targets=authoritative_targets,
    )
    if (
        rebuilt.content_hash != source_index_sha256
        or Path(rebuilt.object_path) != observed_path
        or rebuilt.byte_count != len(observed)
    ):
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "announcement source index does not cold replay exactly"
        )
    return rebuilt


def _normalize_authoritative_targets(
    values: Sequence[cninfo.FrozenDisclosureTarget],
) -> tuple[cninfo.FrozenDisclosureTarget, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "authoritative targets are invalid"
        )
    normalized: list[cninfo.FrozenDisclosureTarget] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, cninfo.FrozenDisclosureTarget):
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "authoritative targets must be FrozenDisclosureTarget values"
            )
        try:
            start = date.fromisoformat(str(value.query_start))
            end = date.fromisoformat(str(value.query_end))
        except ValueError as exc:
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "authoritative target interval is not canonical ISO date"
            ) from exc
        if (
            value.exchange != EXCHANGE
            or not re.fullmatch(r"\d{6}\.SZ", value.code)
            or start.isoformat() != value.query_start
            or end.isoformat() != value.query_end
            or start < date.fromisoformat(AUDIT_START)
            or end > date.fromisoformat(AUDIT_END)
            or end < start
            or not str(value.canonical_entity_id).strip()
        ):
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "authoritative target is outside the frozen SZSE 2018-2023 scope"
            )
        if value.code in seen:
            raise CninfoAnnouncementQualityAdapterBlockedError(
                f"duplicate authoritative target code: {value.code}"
            )
        seen.add(value.code)
        normalized.append(value)
    if not normalized:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "authoritative target scope is empty"
        )
    normalized.sort(
        key=lambda item: (
            item.exchange,
            item.code,
            item.canonical_entity_id,
            item.query_start,
            item.query_end,
        )
    )
    return tuple(normalized)


def _target_years(target: cninfo.FrozenDisclosureTarget) -> range:
    start = date.fromisoformat(target.query_start)
    end = date.fromisoformat(target.query_end)
    return range(start.year, end.year + 1)


def _calendar_rows(
    artifact: official_calendar.OfficialTradingCalendarArtifact,
) -> Mapping[date, bool]:
    rows: dict[date, bool] = {}
    for row in artifact.rows:
        if row.exchange != EXCHANGE:
            continue
        observed = date.fromisoformat(row.trade_date)
        if observed in rows:
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "official calendar contains duplicate SZSE dates"
            )
        rows[observed] = row.is_open
    expected_start = date(2017, 1, 1)
    expected_end = date(2023, 12, 31)
    expected_count = (expected_end - expected_start).days + 1
    if (
        len(rows) != expected_count
        or min(rows, default=None) != expected_start
        or max(rows, default=None) != expected_end
    ):
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "official calendar lacks the complete SZSE 2017-2023 horizon"
        )
    return rows


def _announcement_rows(
    artifact: cninfo.CninfoDelistedDisclosureArtifact,
    targets: Sequence[cninfo.FrozenDisclosureTarget],
    calendar_rows: Mapping[date, bool],
) -> list[dict[str, Any]]:
    contract = DATASET_CONTRACTS[DATASET]
    targets_by_code = {target.code: target for target in targets}
    if len(targets_by_code) != len(targets):
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "authoritative target scope contains duplicate codes"
        )
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for source in artifact.normalized_announcements:
        if source.get("exchange") != EXCHANGE:
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "CNINFO quality adapter cannot emit SSE announcement coverage"
            )
        code = str(source.get("code") or "")
        target = targets_by_code.get(code)
        if target is None:
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "CNINFO announcement is outside authoritative target scope"
            )
        target_start = date.fromisoformat(target.query_start)
        target_end = date.fromisoformat(target.query_end)
        announcement_id = str(source.get("announcement_id") or "")
        if not announcement_id or announcement_id in seen_ids:
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "CNINFO announcement identity is missing or duplicated"
            )
        seen_ids.add(announcement_id)
        published = _published_datetime(source)
        precision = str(source.get("publication_precision") or "")
        effective = _effective_datetime(
            published=published,
            precision=precision,
            calendar_rows=calendar_rows,
        )
        if not (target_start <= published.date() <= target_end):
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "CNINFO published_at falls outside authoritative target interval"
            )
        if not (target_start <= effective.date() <= target_end):
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "derived effective_at falls outside authoritative target interval"
            )
        row = {
            "exchange": EXCHANGE,
            "code": code,
            "announcement_id": announcement_id,
            "announcement_type": str(source.get("announcement_type") or ""),
            "published_at": published.isoformat(),
            "effective_at": effective.isoformat(),
            "url": str(source["url"]),
            "content_hash": str(source["content_hash"]),
        }
        if tuple(row) != contract.schema:
            raise CninfoAnnouncementQualityAdapterBlockedError(
                "announcement row schema does not match the frozen contract"
            )
        rows.append(row)
    return rows


def _published_datetime(source: Mapping[str, Any]) -> datetime:
    text = str(source.get("published_at") or "")
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "CNINFO published_at is not ISO-8601"
        ) from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "CNINFO published_at has no timezone"
        )
    local = value.astimezone(_CHINA).replace(microsecond=0)
    if local.isoformat() != text:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "CNINFO published_at is not canonical China time"
        )
    return local


def _effective_datetime(
    *,
    published: datetime,
    precision: str,
    calendar_rows: Mapping[date, bool],
) -> datetime:
    if precision == "DATE_ONLY":
        effective_date = _first_open_date(
            calendar_rows, published.date(), inclusive=True
        )
        return datetime.combine(effective_date, time.min, tzinfo=_CHINA)
    if precision != "TIMESTAMP":
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "CNINFO publication precision is not admitted"
        )
    if calendar_rows.get(published.date()) is True and published.time().replace(
        tzinfo=None
    ) < STANDARD_MARKET_CLOSE:
        return published
    effective_date = _first_open_date(
        calendar_rows, published.date() + timedelta(days=1), inclusive=True
    )
    return datetime.combine(effective_date, time.min, tzinfo=_CHINA)


def _first_open_date(
    rows: Mapping[date, bool], start: date, *, inclusive: bool
) -> date:
    current = start if inclusive else start + timedelta(days=1)
    horizon = max(rows, default=current)
    while current <= horizon:
        if rows.get(current) is True:
            return current
        current += timedelta(days=1)
    raise CninfoAnnouncementQualityAdapterBlockedError(
        "official calendar has no next open session for effective_at"
    )


def _sha256_identity(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise CninfoAnnouncementQualityAdapterBlockedError(
            f"{label} is not SHA-256"
        )
    return normalized


def _put_derived_blob(
    cas: cninfo.CninfoDisclosureCAS, content: bytes
) -> tuple[str, Path]:
    if content:
        return cas.put_blob(content)
    # Canonical JSONL for an empty partition is exactly zero bytes.  The CNINFO
    # CAS rejects empty *source* responses, so use its hardened atomic/stable
    # primitives for this derived object without weakening source admission.
    digest = _sha256(content)
    path = cas.root / "sha256" / digest[:2] / digest
    cninfo._atomic_write_exact(cas.root, path, content)
    persisted = cninfo._stable_read(cas.root, path)
    if persisted != content or _sha256(persisted) != digest:
        raise CninfoAnnouncementQualityAdapterBlockedError(
            "derived empty JSONL failed CAS verification"
        )
    return digest, path


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


__all__ = [
    "AnnouncementDocumentsQualityIndexReference",
    "CninfoAnnouncementQualityAdapterBlockedError",
    "CNINFO_MANIFEST_ROLE",
    "DATASET",
    "EFFECTIVE_AT_PROTOCOL_VERSION",
    "OFFICIAL_CALENDAR_MANIFEST_ROLE",
    "OVERALL_STATUS",
    "PROTOCOL_VERSION",
    "UPSTREAM_EVIDENCE_KIND",
    "build_cninfo_announcement_documents_quality_index",
    "replay_cninfo_announcement_documents_quality_index",
]
