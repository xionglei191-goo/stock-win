from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROTOCOL_VERSION = "early-winner-delisted-history-quality-v4"
SOURCE_INDEX_PROTOCOL_VERSION = "early-winner-delisted-source-index-v3"
RAW_ENVELOPE_PROTOCOL_VERSION = "early-winner-delisted-raw-envelope-v1"
QUALITY_POLICY_VERSION = "early-winner-delisted-history-hard-gates-v4"
SOURCE_INDEX_AUTHORITY = "VERIFIED_PARTITION_RAW_ENVELOPES"
SSE_OFFICIAL_RAW_BARS_INDEX_AUTHORITY = (
    "SSE_OFFICIAL_DAILY_BARS_MANIFEST_BOUND_PARTITIONS"
)

AUDIT_START = "2018-01-01"
AUDIT_END = "2023-12-31"
DELISTED_HISTORY_SOURCE_INCOMPLETE = "DELISTED_HISTORY_SOURCE_INCOMPLETE"
DELISTED_HISTORY_QUALITY_REJECTED = "DELISTED_HISTORY_QUALITY_REJECTED"
READY = "READY"

MAX_FINDINGS = 500
MAX_MISSING_SAMPLE = 100
PRICE_TOLERANCE = 1e-6
MINIMUM_OPEN_SESSION_DENSITY = 0.40
MINIMUM_RAW_SESSION_DENSITY = 0.02
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

MASTER_RECORD_FIELDS = (
    "canonical_entity_id",
    "exchange",
    "code_alias",
    "board",
    "listed_at",
    "delisted_at",
    "valid_from",
    "valid_to",
    "event_type",
    "source_url",
    "source_hash",
    "retrieved_at",
    "name",
    "attributes",
)


@dataclass(frozen=True)
class DatasetContract:
    schema: tuple[str, ...]
    date_field: str
    code_scoped: bool = True
    interval_end_field: str | None = None
    rows_required_per_code_year: bool = False

    @property
    def schema_version(self) -> str:
        return f"{self.source_protocol_version}-schema-v1"

    @property
    def source_protocol_version(self) -> str:
        return "early-winner-delisted-" + self.name.replace("_", "-") + "-v1"

    @property
    def name(self) -> str:
        for name, contract in DATASET_CONTRACTS.items():
            if contract is self:
                return name
        raise RuntimeError("dataset contract is not registered")


DATASET_CONTRACTS: dict[str, DatasetContract] = {
    "raw_execution_bars": DatasetContract(
        (
            "exchange",
            "code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ),
        "trade_date",
    ),
    "adjusted_bars_factors": DatasetContract(
        (
            "exchange",
            "code",
            "trade_date",
            "front_open",
            "front_high",
            "front_low",
            "front_close",
            "adjustment_factor",
            "anchor_trade_date",
            "anchor_adjustment_factor",
        ),
        "trade_date",
    ),
    "trading_calendar": DatasetContract(
        ("exchange", "trade_date", "is_open"),
        "trade_date",
        code_scoped=False,
    ),
    "financial_reports": DatasetContract(
        (
            "exchange",
            "code",
            "period_end",
            "report_type",
            "revenue",
            "revenue_yoy",
            "net_profit",
            "net_profit_yoy",
            "gross_margin",
            "roe",
            "operating_cash_flow",
            "published_at",
            "effective_at",
            "source_document_hash",
        ),
        "effective_at",
        rows_required_per_code_year=True,
    ),
    "earnings_guidance_express": DatasetContract(
        (
            "exchange",
            "code",
            "event_id",
            "event_type",
            "period_end",
            "forecast_low",
            "forecast_high",
            "previous_value",
            "published_at",
            "effective_at",
            "source_document_hash",
        ),
        "effective_at",
    ),
    "gp15_price_limits": DatasetContract(
        (
            "exchange",
            "code",
            "trade_date",
            "limit_up",
            "limit_down",
            "published_at",
            "effective_at",
            "source_document_hash",
        ),
        "trade_date",
    ),
    "gp29_st_status": DatasetContract(
        (
            "exchange",
            "code",
            "valid_from",
            "valid_to",
            "status",
            "published_at",
            "effective_at",
            "source_document_hash",
        ),
        "valid_from",
        interval_end_field="valid_to",
    ),
    "gp30_corporate_actions": DatasetContract(
        (
            "exchange",
            "code",
            "event_id",
            "event_type",
            "ex_date",
            "ratio",
            "cash_amount",
            "published_at",
            "effective_at",
            "source_document_hash",
        ),
        "ex_date",
    ),
    "gp43_corporate_actions": DatasetContract(
        (
            "exchange",
            "code",
            "event_id",
            "event_type",
            "ex_date",
            "ratio",
            "cash_amount",
            "published_at",
            "effective_at",
            "source_document_hash",
        ),
        "ex_date",
    ),
    "industry_history": DatasetContract(
        (
            "exchange",
            "code",
            "industry_code",
            "valid_from",
            "valid_to",
            "published_at",
            "effective_at",
            "source_document_hash",
        ),
        "valid_from",
        interval_end_field="valid_to",
    ),
    "announcement_documents": DatasetContract(
        (
            "exchange",
            "code",
            "announcement_id",
            "announcement_type",
            "published_at",
            "effective_at",
            "url",
            "content_hash",
        ),
        "effective_at",
        rows_required_per_code_year=True,
    ),
    "suspension_status": DatasetContract(
        (
            "exchange",
            "code",
            "trade_date",
            "status",
            "published_at",
            "effective_at",
            "source_document_hash",
        ),
        "trade_date",
    ),
}

REQUIRED_DATASETS = tuple(DATASET_CONTRACTS)

# These are protocol identifiers, not caller-provided descriptive labels.  Every
# raw envelope is parsed and must use the authority admitted for its exchange.
RAW_AUTHORITY_BY_DATASET_EXCHANGE: dict[str, dict[str, str]] = {
    "raw_execution_bars": {
        "SSE": "SSE_OFFICIAL_DAILY_BARS",
        "SZSE": "SZSE_AUTHORIZED_DAILY_BARS",
    },
    "adjusted_bars_factors": {
        "SSE": "SSE_CORPORATE_ACTION_ADJUSTMENT_AUDIT",
        "SZSE": "SZSE_CORPORATE_ACTION_ADJUSTMENT_AUDIT",
    },
    "trading_calendar": {
        "SSE": "SSE_OFFICIAL_TRADING_CALENDAR",
        "SZSE": "SZSE_OFFICIAL_TRADING_CALENDAR",
    },
    "financial_reports": {
        "SSE": "SSE_OFFICIAL_DISCLOSURE",
        "SZSE": "CNINFO_SZSE_OFFICIAL_DISCLOSURE",
    },
    "earnings_guidance_express": {
        "SSE": "SSE_OFFICIAL_DISCLOSURE",
        "SZSE": "CNINFO_SZSE_OFFICIAL_DISCLOSURE",
    },
    "gp15_price_limits": {
        "SSE": "SSE_OFFICIAL_DAILY_STATUS",
        "SZSE": "SZSE_AUTHORIZED_DAILY_STATUS",
    },
    "gp29_st_status": {
        "SSE": "SSE_OFFICIAL_SECURITY_STATUS",
        "SZSE": "SZSE_OFFICIAL_SECURITY_STATUS",
    },
    "gp30_corporate_actions": {
        "SSE": "SSE_OFFICIAL_CORPORATE_ACTIONS_GP30",
        "SZSE": "SZSE_OFFICIAL_CORPORATE_ACTIONS_GP30",
    },
    "gp43_corporate_actions": {
        "SSE": "SSE_OFFICIAL_CORPORATE_ACTIONS_GP43",
        "SZSE": "SZSE_OFFICIAL_CORPORATE_ACTIONS_GP43",
    },
    "industry_history": {
        "SSE": "SSE_OFFICIAL_INDUSTRY_HISTORY",
        "SZSE": "SZSE_OFFICIAL_INDUSTRY_HISTORY",
    },
    "announcement_documents": {
        "SSE": "SSE_OFFICIAL_DISCLOSURE",
        "SZSE": "CNINFO_SZSE_OFFICIAL_DISCLOSURE",
    },
    "suspension_status": {
        "SSE": "SSE_OFFICIAL_SUSPENSION_STATUS",
        "SZSE": "SZSE_OFFICIAL_SUSPENSION_STATUS",
    },
}

ROW_SOURCE_HASH_FIELDS = {
    "financial_reports": "source_document_hash",
    "earnings_guidance_express": "source_document_hash",
    "gp15_price_limits": "source_document_hash",
    "gp29_st_status": "source_document_hash",
    "gp30_corporate_actions": "source_document_hash",
    "gp43_corporate_actions": "source_document_hash",
    "industry_history": "source_document_hash",
    "announcement_documents": "content_hash",
    "suspension_status": "source_document_hash",
}


class DelistedHistoryQualityBlockedError(RuntimeError):
    """The audit scope or its content-addressed identity cannot be trusted."""


class _SourceEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class _TargetInterval:
    canonical_entity_id: str
    exchange: str
    code: str
    start: date
    end_exclusive: date


@dataclass(frozen=True)
class _Partition:
    exchange: str
    year: int
    code: str
    query_start: date
    query_end: date
    row_count: int
    rows: tuple[Mapping[str, Any], ...]
    content_hash: str
    raw_source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class _LoadedDataset:
    name: str
    index_hash: str
    index_object_path: str
    index_byte_count: int
    source_protocol_version: str
    schema_version: str
    source_authority: str
    row_count: int
    partitions: Mapping[tuple[str, int, str], _Partition]
    rows: tuple[Mapping[str, Any], ...]
    raw_source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class _CSRCIndustryReplayPlan:
    snapshot_manifest_sha256s: tuple[str, ...]
    frozen_targets: tuple[Any, ...]
    evidence_target_count: int
    covered_target_count: int
    raw_authority: str


@dataclass(frozen=True)
class _Finding:
    code: str
    detail: str
    dataset: str = ""
    exchange: str = ""
    year: int | None = None
    security_code: str = ""
    severity: str = "CRITICAL"

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


def _is_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def _strict_nonnegative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise _SourceEvidenceError(f"{field} is not a non-negative integer")
    return value


def _iso_date(value: Any, field: str) -> date:
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO date: {text!r}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} is not canonical ISO date: {text!r}")
    return parsed


def _iso_datetime(value: Any, field: str) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not ISO-8601: {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _finite_number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} is not numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} is not finite")
    if positive and parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def _record_mapping(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        value = record.to_dict()
    elif is_dataclass(record):
        value = asdict(record)
    elif isinstance(record, Mapping):
        value = dict(record)
    else:
        raise DelistedHistoryQualityBlockedError(
            f"unsupported security-master record: {type(record)!r}"
        )
    if set(value) != set(MASTER_RECORD_FIELDS):
        raise DelistedHistoryQualityBlockedError(
            "security-master record schema does not match the frozen contract"
        )
    value["attributes"] = dict(sorted(dict(value.get("attributes") or {}).items()))
    return value


def _master_jsonl(records: Sequence[Any]) -> bytes:
    normalized = [_record_mapping(item) for item in records]
    normalized.sort(
        key=lambda item: (
            str(item["canonical_entity_id"]),
            str(item["valid_from"]),
            str(item["exchange"]),
            str(item["code_alias"]),
        )
    )
    if not normalized:
        return b""
    return b"\n".join(_canonical_json_bytes(item) for item in normalized) + b"\n"


def _validate_no_symlink(path: Path, label: str) -> None:
    current = path
    while True:
        if current.exists():
            metadata = os.lstat(current)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode) or (
                attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise DelistedHistoryQualityBlockedError(
                    f"{label} uses a symlink, junction, or reparse point"
                )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _read_exact_file(path: Path, label: str) -> bytes:
    _validate_no_symlink(path, label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DelistedHistoryQualityBlockedError(
            f"{label} cannot be opened as a stable file: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        before_attributes = int(getattr(before, "st_file_attributes", 0))
        if not stat.S_ISREG(before.st_mode) or (
            before_attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise DelistedHistoryQualityBlockedError(
                f"{label} is not a plain regular file: {path}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
        after = os.fstat(descriptor)
        after_attributes = int(getattr(after, "st_file_attributes", 0))
        if after_attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
            raise DelistedHistoryQualityBlockedError(
                f"{label} became a reparse point while it was being read"
            )
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
            raise DelistedHistoryQualityBlockedError(
                f"{label} changed while it was being read"
            )
    finally:
        os.close(descriptor)
    _validate_no_symlink(path, label)
    return content


def _verify_master(
    records: Sequence[Any], identity: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot_id = str(identity.get("snapshot_id") or "")
    manifest_hash = str(identity.get("manifest_hash") or snapshot_id)
    if not _is_sha256(snapshot_id) or manifest_hash != snapshot_id:
        raise DelistedHistoryQualityBlockedError(
            "security-master manifest identity is not a matching SHA-256"
        )
    manifest_path = Path(str(identity.get("manifest_path") or ""))
    if manifest_path.name != f"{snapshot_id}.json" or manifest_path.parent.name != "manifests":
        raise DelistedHistoryQualityBlockedError(
            "security-master manifest path does not match its identity"
        )
    manifest_bytes = _read_exact_file(manifest_path, "security-master manifest")
    if _sha256(manifest_bytes) != snapshot_id:
        raise DelistedHistoryQualityBlockedError(
            "security-master manifest content hash mismatch"
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DelistedHistoryQualityBlockedError(
            "security-master manifest is not valid UTF-8 JSON"
        ) from exc
    if _canonical_json_bytes(manifest) != manifest_bytes:
        raise DelistedHistoryQualityBlockedError(
            "security-master manifest is not canonical JSON"
        )
    protocol = str(manifest.get("protocol_version") or "")
    if not protocol:
        raise DelistedHistoryQualityBlockedError(
            "security-master manifest has no protocol version"
        )
    expected_protocol = str(identity.get("protocol_version") or protocol)
    if expected_protocol != protocol:
        raise DelistedHistoryQualityBlockedError(
            "security-master protocol identity mismatch"
        )
    try:
        artifact = dict(manifest["artifacts"]["security_master_jsonl"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DelistedHistoryQualityBlockedError(
            "security-master manifest has no master JSONL artifact"
        ) from exc
    content_hash = str(artifact.get("content_hash") or "")
    if not _is_sha256(content_hash):
        raise DelistedHistoryQualityBlockedError(
            "security-master artifact hash is invalid"
        )
    root = manifest_path.parent.parent
    expected_path = root / "objects" / content_hash[:2] / content_hash
    object_path = Path(str(artifact.get("object_path") or ""))
    if object_path.resolve() != expected_path.resolve():
        raise DelistedHistoryQualityBlockedError(
            "security-master artifact path does not match its content hash"
        )
    content = _read_exact_file(object_path, "security-master JSONL")
    if _sha256(content) != content_hash:
        raise DelistedHistoryQualityBlockedError(
            "security-master artifact content hash mismatch"
        )
    supplied = _master_jsonl(records)
    if supplied != content:
        raise DelistedHistoryQualityBlockedError(
            "supplied security-master records do not match the frozen artifact"
        )
    lines = content.splitlines()
    if int(artifact.get("row_count", -1)) != len(lines):
        raise DelistedHistoryQualityBlockedError(
            "security-master artifact row_count mismatch"
        )
    parsed: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DelistedHistoryQualityBlockedError(
                "security-master artifact contains invalid JSONL"
            ) from exc
        if _canonical_json_bytes(row) != line:
            raise DelistedHistoryQualityBlockedError(
                "security-master artifact contains non-canonical JSONL"
            )
        parsed.append(_record_mapping(row))
    return parsed, {
        "snapshot_id": snapshot_id,
        "manifest_hash": manifest_hash,
        "protocol_version": protocol,
        "security_master_content_hash": content_hash,
        "security_master_row_count": len(parsed),
        "manifest_path": str(manifest_path.resolve()),
        "security_master_object_path": str(object_path.resolve()),
        "security_master_byte_count": len(content),
    }


def _target_intervals(records: Sequence[Mapping[str, Any]]) -> tuple[_TargetInterval, ...]:
    audit_start = date.fromisoformat(AUDIT_START)
    audit_end_exclusive = date.fromisoformat(AUDIT_END) + timedelta(days=1)
    targets: list[_TargetInterval] = []
    seen: set[tuple[str, str, date, date]] = set()
    for row in records:
        exchange = str(row["exchange"]).upper()
        if exchange not in {"SSE", "SZSE"} or row.get("delisted_at") is None:
            continue
        code = str(row["code_alias"]).upper()
        suffix = ".SH" if exchange == "SSE" else ".SZ"
        if not re.fullmatch(r"\d{6}" + re.escape(suffix), code):
            raise DelistedHistoryQualityBlockedError(
                f"security-master contains invalid {exchange} alias: {code!r}"
            )
        listed = _iso_date(row["listed_at"], "listed_at")
        valid_from = _iso_date(row["valid_from"], "valid_from")
        delisted = _iso_date(row["delisted_at"], "delisted_at")
        valid_to = (
            _iso_date(row["valid_to"], "valid_to")
            if row.get("valid_to") is not None
            else delisted
        )
        start = max(audit_start, listed, valid_from)
        end_exclusive = min(audit_end_exclusive, delisted, valid_to)
        if start >= end_exclusive:
            continue
        key = (exchange, code, start, end_exclusive)
        if key in seen:
            raise DelistedHistoryQualityBlockedError(
                f"duplicate delisted security-master interval: {key}"
            )
        seen.add(key)
        targets.append(
            _TargetInterval(
                canonical_entity_id=str(row["canonical_entity_id"]),
                exchange=exchange,
                code=code,
                start=start,
                end_exclusive=end_exclusive,
            )
        )
    return tuple(sorted(targets, key=lambda item: (item.exchange, item.code, item.start)))


def _expected_cas_path(root: Path, content_hash: str) -> Path:
    return root / "sha256" / content_hash[:2] / content_hash


def _read_input_cas(
    root: Path,
    *,
    content_hash: str,
    object_path: Any,
    label: str,
) -> bytes:
    if not _is_sha256(content_hash):
        raise _SourceEvidenceError(f"{label} content_hash is not SHA-256")
    path = Path(str(object_path or ""))
    expected = _expected_cas_path(root, content_hash)
    if path.resolve() != expected.resolve():
        raise _SourceEvidenceError(f"{label} object path mismatch")
    try:
        content = _read_exact_file(path, label)
    except DelistedHistoryQualityBlockedError as exc:
        raise _SourceEvidenceError(str(exc)) from exc
    if _sha256(content) != content_hash:
        raise _SourceEvidenceError(f"{label} content hash mismatch")
    return content


def _partition_year_bounds(year: int) -> tuple[date, date]:
    start = max(date.fromisoformat(AUDIT_START), date(year, 1, 1))
    end = min(date.fromisoformat(AUDIT_END), date(year, 12, 31))
    return start, end


def _row_overlaps_partition(
    row: Mapping[str, Any], contract: DatasetContract, year: int
) -> bool:
    value = row[contract.date_field]
    if contract.date_field in {"effective_at", "published_at"}:
        start = _iso_datetime(value, contract.date_field).date()
    else:
        start = _iso_date(value, contract.date_field)
    if contract.interval_end_field:
        raw_end = row.get(contract.interval_end_field)
        end = (
            _iso_date(raw_end, contract.interval_end_field)
            if raw_end is not None
            else date.max
        )
        return start <= date(year, 12, 31) and end > date(year, 1, 1)
    return start.year == year


def _validate_partition_row(
    dataset: str,
    row: Mapping[str, Any],
    partition_exchange: str,
    partition_code: str,
    partition_year: int,
) -> None:
    contract = DATASET_CONTRACTS[dataset]
    if set(row) != set(contract.schema) or len(row) != len(contract.schema):
        raise _SourceEvidenceError(
            f"{dataset} row schema drift; expected {list(contract.schema)}"
        )
    exchange = str(row.get("exchange") or "").upper()
    if exchange != partition_exchange:
        raise _SourceEvidenceError(f"{dataset} row exchange does not match partition")
    if contract.code_scoped:
        code = str(row.get("code") or "").upper()
        if code != partition_code:
            raise _SourceEvidenceError(f"{dataset} row code does not match partition")
    if not _row_overlaps_partition(row, contract, partition_year):
        raise _SourceEvidenceError(f"{dataset} row does not belong to partition year")


def _parse_canonical_jsonl(content: bytes, dataset: str) -> tuple[dict[str, Any], ...]:
    if not content:
        return ()
    if not content.endswith(b"\n"):
        raise _SourceEvidenceError(f"{dataset} JSONL must end with a newline")
    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
        if not line:
            raise _SourceEvidenceError(f"{dataset} JSONL contains a blank row")
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _SourceEvidenceError(f"{dataset} JSONL is invalid") from exc
        if not isinstance(row, dict) or _canonical_json_bytes(row) != line:
            raise _SourceEvidenceError(f"{dataset} JSONL is not canonical")
        rows.append(row)
    return tuple(rows)


def _parse_raw_rows_envelope(
    content: bytes,
    *,
    dataset: str,
    exchange: str,
    year: int,
    code: str,
    authority: str,
) -> tuple[dict[str, Any], ...]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _SourceEvidenceError(f"{dataset} raw rows envelope is invalid") from exc
    if not isinstance(value, dict) or _canonical_json_bytes(value) != content:
        raise _SourceEvidenceError(
            f"{dataset} raw rows envelope is not canonical JSON"
        )
    if set(value) != {
        "protocol_version",
        "authority",
        "dataset",
        "exchange",
        "year",
        "code",
        "schema",
        "rows",
    }:
        raise _SourceEvidenceError(f"{dataset} raw rows envelope schema drift")
    contract = DATASET_CONTRACTS[dataset]
    if (
        value.get("protocol_version") != RAW_ENVELOPE_PROTOCOL_VERSION
        or value.get("authority") != authority
        or value.get("dataset") != dataset
        or value.get("exchange") != exchange
        or value.get("year") != year
        or value.get("code") != code
        or tuple(value.get("schema") or ()) != contract.schema
        or not isinstance(value.get("rows"), list)
    ):
        raise _SourceEvidenceError(f"{dataset} raw rows envelope identity mismatch")
    rows: list[dict[str, Any]] = []
    for row in value["rows"]:
        if not isinstance(row, dict):
            raise _SourceEvidenceError(f"{dataset} raw envelope row is invalid")
        _validate_partition_row(dataset, row, exchange, code, year)
        rows.append(row)
    return tuple(rows)


def _prepare_csrc_industry_replay(
    upstream_evidence: Any,
    input_cas_root: Path,
    *,
    authoritative_master_snapshot_id: str | None,
    authoritative_targets: Sequence[_TargetInterval] | None,
) -> _CSRCIndustryReplayPlan:
    from research_platform import csrc_industry_history_source as csrc

    expected_upstream_fields = {
        "kind",
        "adapter_protocol_version",
        "authority",
        "snapshot_manifests",
        "master_scope",
        "point_in_time_protocol",
        "integration_contract",
        "status",
    }
    if (
        not isinstance(upstream_evidence, dict)
        or set(upstream_evidence) != expected_upstream_fields
        or upstream_evidence.get("kind") != csrc.UPSTREAM_EVIDENCE_KIND
        or upstream_evidence.get("adapter_protocol_version")
        != csrc.QUALITY_ADAPTER_PROTOCOL_VERSION
        or upstream_evidence.get("authority")
        != csrc.OFFICIAL_INDUSTRY_RAW_AUTHORITY
        or upstream_evidence.get("status") != csrc.SOURCE_STATUS
    ):
        raise _SourceEvidenceError(
            "industry_history CSRC/CAPCO upstream protocol is not admitted"
        )
    if not authoritative_master_snapshot_id or authoritative_targets is None:
        raise _SourceEvidenceError(
            "industry_history CSRC/CAPCO replay has no verified master scope"
        )

    expected_point_in_time = {
        "publication_precision": "DATE_ONLY",
        "published_at_rule": "PUBLICATION_DATE_AT_23_59_59_ASIA_SHANGHAI",
        "available_from_rule": "NEXT_CALENDAR_DATE_AT_00_00_ASIA_SHANGHAI",
        "unchanged_classification_carries_forward": True,
        "missing_from_later_snapshot_does_not_imply_industry_CHANGE": True,
        "post_period_publications_may_not_backfill_prior_dates": True,
    }
    expected_integration_contract = {
        "required_upstream_kind": csrc.UPSTREAM_EVIDENCE_KIND,
        "required_exchange_raw_authorities": {
            "SSE": csrc.OFFICIAL_INDUSTRY_RAW_AUTHORITY,
            "SZSE": csrc.OFFICIAL_INDUSTRY_RAW_AUTHORITY,
        },
        "required_cold_replay_function": (
            "replay_industry_history_quality_index"
        ),
        "producer_promotion_claim_accepted": False,
    }
    if upstream_evidence.get("point_in_time_protocol") != expected_point_in_time:
        raise _SourceEvidenceError(
            "industry_history CSRC/CAPCO point-in-time protocol mismatch"
        )
    if (
        upstream_evidence.get("integration_contract")
        != expected_integration_contract
    ):
        raise _SourceEvidenceError(
            "industry_history CSRC/CAPCO integration contract mismatch"
        )

    frozen_targets = tuple(
        csrc.FrozenIndustryTarget(
            canonical_entity_id=target.canonical_entity_id,
            exchange=target.exchange,
            code=target.code,
            query_start=target.start.isoformat(),
            query_end=(target.end_exclusive - timedelta(days=1)).isoformat(),
        )
        for target in authoritative_targets
    )
    target_values = [target.to_dict() for target in frozen_targets]
    expected_scope_base = {
        "snapshot_id": authoritative_master_snapshot_id,
        "expected_target_count": csrc.EXPECTED_MASTER_TARGET_COUNT,
        "targets": target_values,
    }
    expected_scope_sha256 = _sha256(_canonical_json_bytes(expected_scope_base))
    master_scope = upstream_evidence.get("master_scope")
    expected_scope_fields = {
        "snapshot_id",
        "expected_target_count",
        "targets",
        "scope_sha256",
        "target_count",
        "evidence_target_count",
        "covered_target_count",
        "full_master_scope_present",
        "all_targets_covered",
    }
    if not isinstance(master_scope, dict) or set(master_scope) != expected_scope_fields:
        raise _SourceEvidenceError(
            "industry_history CSRC/CAPCO master scope schema drift"
        )
    target_count = _strict_nonnegative_int(
        master_scope.get("target_count"), "industry_history target_count"
    )
    evidence_target_count = _strict_nonnegative_int(
        master_scope.get("evidence_target_count"),
        "industry_history evidence_target_count",
    )
    covered_target_count = _strict_nonnegative_int(
        master_scope.get("covered_target_count"),
        "industry_history covered_target_count",
    )
    if (
        master_scope.get("snapshot_id") != authoritative_master_snapshot_id
        or master_scope.get("expected_target_count")
        != csrc.EXPECTED_MASTER_TARGET_COUNT
        or master_scope.get("targets") != target_values
        or master_scope.get("scope_sha256") != expected_scope_sha256
        or target_count != len(frozen_targets)
        or evidence_target_count > target_count
        or covered_target_count > evidence_target_count
        or master_scope.get("full_master_scope_present")
        is not (target_count == csrc.EXPECTED_MASTER_TARGET_COUNT)
        or master_scope.get("all_targets_covered")
        is not (covered_target_count == target_count)
    ):
        raise _SourceEvidenceError(
            "industry_history CSRC/CAPCO master scope identity mismatch"
        )

    manifest_values = upstream_evidence.get("snapshot_manifests")
    if not isinstance(manifest_values, list) or not manifest_values:
        raise _SourceEvidenceError(
            "industry_history CSRC/CAPCO snapshot manifests are missing"
        )
    expected_manifest_fields = {
        "snapshot_id",
        "content_hash",
        "object_path",
        "byte_count",
        "published_date",
        "logical_content_sha256",
    }
    manifest_hashes: list[str] = []
    seen_snapshot_ids: set[str] = set()
    seen_manifest_hashes: set[str] = set()
    previous_order: tuple[str, str] | None = None
    for binding in manifest_values:
        if not isinstance(binding, dict) or set(binding) != expected_manifest_fields:
            raise _SourceEvidenceError(
                "industry_history CSRC/CAPCO snapshot binding schema drift"
            )
        snapshot_id = str(binding.get("snapshot_id") or "")
        spec = csrc.OFFICIAL_SNAPSHOT_SPECS.get(snapshot_id)
        manifest_hash = str(binding.get("content_hash") or "")
        logical_hash = str(binding.get("logical_content_sha256") or "")
        published_date = str(binding.get("published_date") or "")
        order = (published_date, snapshot_id)
        if (
            spec is None
            or published_date != spec.published_date
            or snapshot_id in seen_snapshot_ids
            or manifest_hash in seen_manifest_hashes
            or not _is_sha256(logical_hash)
            or previous_order is not None
            and order <= previous_order
        ):
            raise _SourceEvidenceError(
                "industry_history CSRC/CAPCO snapshot binding identity mismatch"
            )
        manifest_content = _read_input_cas(
            input_cas_root,
            content_hash=manifest_hash,
            object_path=binding.get("object_path"),
            label=f"industry_history {snapshot_id} snapshot manifest",
        )
        if _strict_nonnegative_int(
            binding.get("byte_count"),
            f"industry_history {snapshot_id} snapshot byte_count",
        ) != len(manifest_content):
            raise _SourceEvidenceError(
                "industry_history CSRC/CAPCO snapshot byte_count mismatch"
            )
        seen_snapshot_ids.add(snapshot_id)
        seen_manifest_hashes.add(manifest_hash)
        manifest_hashes.append(manifest_hash)
        previous_order = order

    return _CSRCIndustryReplayPlan(
        snapshot_manifest_sha256s=tuple(manifest_hashes),
        frozen_targets=frozen_targets,
        evidence_target_count=evidence_target_count,
        covered_target_count=covered_target_count,
        raw_authority=csrc.OFFICIAL_INDUSTRY_RAW_AUTHORITY,
    )


def _load_dataset(
    dataset: str,
    identity: Mapping[str, Any],
    input_cas_root: Path,
    *,
    authoritative_master_snapshot_id: str | None = None,
    authoritative_targets: Sequence[_TargetInterval] | None = None,
) -> _LoadedDataset:
    contract = DATASET_CONTRACTS[dataset]
    index_hash = str(identity.get("content_hash") or "")
    index_bytes = _read_input_cas(
        input_cas_root,
        content_hash=index_hash,
        object_path=identity.get("object_path"),
        label=f"{dataset} index",
    )
    try:
        index = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _SourceEvidenceError(f"{dataset} index is not UTF-8 JSON") from exc
    if not isinstance(index, dict) or _canonical_json_bytes(index) != index_bytes:
        raise _SourceEvidenceError(f"{dataset} index is not canonical JSON")
    allowed_index_fields = {
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
        "upstream_evidence",
        # These are deliberately ignored.  A producer cannot promote itself.
        "ready",
        "complete",
    }
    if set(index).difference(allowed_index_fields):
        raise _SourceEvidenceError(f"{dataset} index schema drift")
    if index.get("protocol_version") != SOURCE_INDEX_PROTOCOL_VERSION:
        raise _SourceEvidenceError(f"{dataset} index protocol mismatch")
    if index.get("dataset") != dataset:
        raise _SourceEvidenceError(f"{dataset} index dataset mismatch")
    if index.get("source_protocol_version") != contract.source_protocol_version:
        raise _SourceEvidenceError(f"{dataset} source protocol mismatch")
    if index.get("schema_version") != contract.schema_version:
        raise _SourceEvidenceError(f"{dataset} schema version mismatch")
    if tuple(index.get("schema") or ()) != contract.schema:
        raise _SourceEvidenceError(f"{dataset} declared schema drift")
    source_authority = str(index.get("source_authority") or "").strip()
    sse_official_raw_bars_index = (
        dataset == "raw_execution_bars"
        and source_authority == SSE_OFFICIAL_RAW_BARS_INDEX_AUTHORITY
    )
    if source_authority != SOURCE_INDEX_AUTHORITY and not sse_official_raw_bars_index:
        raise _SourceEvidenceError(f"{dataset} source index authority is not admitted")
    if index.get("coverage_start") != AUDIT_START or index.get("coverage_end") != AUDIT_END:
        raise _SourceEvidenceError(f"{dataset} audit coverage boundary mismatch")
    partitions_value = index.get("partitions")
    if not isinstance(partitions_value, list):
        raise _SourceEvidenceError(f"{dataset} partitions are missing")
    partitions: dict[tuple[str, int, str], _Partition] = {}
    unique_rows: dict[bytes, Mapping[str, Any]] = {}
    raw_hashes: set[str] = set()
    raw_authorities: set[str] = set()
    official_calendar_artifact: Any | None = None
    official_calendar_manifest_hash = ""
    sse_official_bar_artifacts: dict[str, Any] = {}
    upstream_evidence = index.get("upstream_evidence")
    cninfo_announcement_index = False
    csrc_industry_plan: _CSRCIndustryReplayPlan | None = None
    if upstream_evidence is not None:
        if dataset == "trading_calendar":
            pass
        elif dataset == "announcement_documents":
            from research_platform import cninfo_announcement_quality_adapter as adapter
            from research_platform import cninfo_delisted_disclosures as cninfo

            if (
                not isinstance(upstream_evidence, dict)
                or upstream_evidence.get("kind") != adapter.UPSTREAM_EVIDENCE_KIND
                or upstream_evidence.get("adapter_protocol_version")
                != adapter.PROTOCOL_VERSION
            ):
                raise _SourceEvidenceError(
                    "announcement_documents CNINFO upstream protocol is not admitted"
                )
            if (
                not authoritative_master_snapshot_id
                or authoritative_targets is None
            ):
                raise _SourceEvidenceError(
                    "announcement_documents CNINFO replay has no verified master scope"
                )
            cninfo_evidence = upstream_evidence.get("cninfo")
            calendar_evidence = upstream_evidence.get(
                "official_trading_calendar"
            )
            if not isinstance(cninfo_evidence, dict) or not isinstance(
                calendar_evidence, dict
            ):
                raise _SourceEvidenceError(
                    "announcement_documents CNINFO upstream identities are missing"
                )
            frozen_targets = tuple(
                cninfo.FrozenDisclosureTarget(
                    canonical_entity_id=target.canonical_entity_id,
                    exchange=target.exchange,
                    code=target.code,
                    query_start=target.start.isoformat(),
                    query_end=(target.end_exclusive - timedelta(days=1)).isoformat(),
                )
                for target in authoritative_targets
                if target.exchange == "SZSE"
            )
            try:
                reference = (
                    adapter.replay_cninfo_announcement_documents_quality_index(
                        cas_root=input_cas_root,
                        source_index_sha256=index_hash,
                        cninfo_manifest_sha256=str(
                            cninfo_evidence.get("manifest_sha256") or ""
                        ),
                        calendar_manifest_sha256=str(
                            calendar_evidence.get("manifest_sha256") or ""
                        ),
                        authoritative_master_snapshot_id=(
                            authoritative_master_snapshot_id
                        ),
                        authoritative_targets=frozen_targets,
                    )
                )
            except adapter.CninfoAnnouncementQualityAdapterBlockedError as exc:
                raise _SourceEvidenceError(
                    "announcement_documents CNINFO index failed authoritative "
                    f"cold replay: {exc}"
                ) from exc
            expected_index_path = _expected_cas_path(
                input_cas_root, index_hash
            ).resolve()
            if (
                reference.content_hash != index_hash
                or Path(reference.object_path).resolve() != expected_index_path
                or reference.byte_count != len(index_bytes)
                or reference.master_snapshot_id
                != authoritative_master_snapshot_id
            ):
                raise _SourceEvidenceError(
                    "announcement_documents CNINFO replay identity mismatch"
                )
            cninfo_announcement_index = True
        elif dataset == "industry_history":
            csrc_industry_plan = _prepare_csrc_industry_replay(
                upstream_evidence,
                input_cas_root,
                authoritative_master_snapshot_id=(
                    authoritative_master_snapshot_id
                ),
                authoritative_targets=authoritative_targets,
            )
        else:
            raise _SourceEvidenceError(
                f"{dataset} cannot declare upstream evidence"
            )
    if dataset == "industry_history" and csrc_industry_plan is None:
        raise _SourceEvidenceError(
            "industry_history requires admitted CSRC/CAPCO upstream evidence"
        )
    total_rows = 0
    partition_fields = {
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
    raw_fields = {
        "content_hash",
        "object_path",
        "byte_count",
        "protocol_version",
        "authority",
        "role",
    }
    for raw_partition in partitions_value:
        if not isinstance(raw_partition, dict) or set(raw_partition) != partition_fields:
            raise _SourceEvidenceError(f"{dataset} partition schema drift")
        exchange = str(raw_partition["exchange"]).upper()
        if exchange not in {"SSE", "SZSE"}:
            raise _SourceEvidenceError(f"{dataset} partition exchange is invalid")
        if cninfo_announcement_index and exchange != "SZSE":
            raise _SourceEvidenceError(
                "CNINFO announcement index cannot declare SSE coverage"
            )
        try:
            year = int(raw_partition["year"])
        except (TypeError, ValueError) as exc:
            raise _SourceEvidenceError(f"{dataset} partition year is invalid") from exc
        if year < 2018 or year > 2023:
            raise _SourceEvidenceError(f"{dataset} partition year is out of range")
        code = str(raw_partition["code"]).upper()
        expected_suffix = ".SH" if exchange == "SSE" else ".SZ"
        if contract.code_scoped:
            if not re.fullmatch(r"\d{6}" + re.escape(expected_suffix), code):
                raise _SourceEvidenceError(f"{dataset} partition code is invalid")
        elif code != "*":
            raise _SourceEvidenceError(f"{dataset} calendar partition code must be '*'")
        key = (exchange, year, code)
        if key in partitions:
            raise _SourceEvidenceError(f"{dataset} contains a duplicate partition")
        query_start = _iso_date(raw_partition["query_start"], "query_start")
        query_end = _iso_date(raw_partition["query_end"], "query_end")
        expected_start, expected_end = _partition_year_bounds(year)
        if query_start != expected_start or query_end != expected_end:
            raise _SourceEvidenceError(
                f"{dataset} partition query boundary is incomplete"
            )
        content_hash = str(raw_partition["content_hash"])
        content = _read_input_cas(
            input_cas_root,
            content_hash=content_hash,
            object_path=raw_partition["object_path"],
            label=f"{dataset} {exchange}/{year}/{code}",
        )
        rows = _parse_canonical_jsonl(content, dataset)
        canonical_rows = tuple(_canonical_json_bytes(row) for row in rows)
        if len(set(canonical_rows)) != len(canonical_rows):
            raise _SourceEvidenceError(
                f"{dataset} partition contains an exact duplicate row"
            )
        try:
            row_count = int(raw_partition["row_count"])
        except (TypeError, ValueError) as exc:
            raise _SourceEvidenceError(f"{dataset} partition row_count is invalid") from exc
        if row_count != len(rows):
            raise _SourceEvidenceError(f"{dataset} partition row_count mismatch")
        raw_sources = raw_partition["raw_sources"]
        if not isinstance(raw_sources, list) or not raw_sources:
            raise _SourceEvidenceError(
                f"{dataset} partition has no independently hashed raw source"
            )
        partition_raw_hashes: list[str] = []
        document_hashes: set[str] = set()
        replayed_rows: tuple[dict[str, Any], ...] | None = None
        sse_official_manifest_hash = ""
        admitted_authority = (
            csrc_industry_plan.raw_authority
            if csrc_industry_plan is not None
            else RAW_AUTHORITY_BY_DATASET_EXCHANGE[dataset][exchange]
        )
        raw_authorities.add(admitted_authority)
        for raw_source in raw_sources:
            if not isinstance(raw_source, dict) or set(raw_source) != raw_fields:
                raise _SourceEvidenceError(f"{dataset} raw source schema drift")
            raw_hash = str(raw_source["content_hash"])
            raw_content = _read_input_cas(
                input_cas_root,
                content_hash=raw_hash,
                object_path=raw_source["object_path"],
                label=f"{dataset} raw source",
            )
            if int(raw_source["byte_count"]) != len(raw_content):
                raise _SourceEvidenceError(f"{dataset} raw source byte_count mismatch")
            if raw_source.get("authority") != admitted_authority:
                raise _SourceEvidenceError(
                    f"{dataset} raw source authority is not admitted for {exchange}"
                )
            role = str(raw_source.get("role") or "")
            raw_protocol = str(raw_source.get("protocol_version") or "")
            if role == "ROWS_ENVELOPE":
                if raw_protocol != RAW_ENVELOPE_PROTOCOL_VERSION:
                    raise _SourceEvidenceError(
                        f"{dataset} raw rows protocol mismatch"
                    )
                if replayed_rows is not None:
                    raise _SourceEvidenceError(
                        f"{dataset} partition has multiple rows envelopes"
                    )
                replayed_rows = _parse_raw_rows_envelope(
                    raw_content,
                    dataset=dataset,
                    exchange=exchange,
                    year=year,
                    code=code,
                    authority=admitted_authority,
                )
            elif role == "OFFICIAL_TRADING_CALENDAR_MANIFEST":
                if dataset != "trading_calendar":
                    raise _SourceEvidenceError(
                        f"{dataset} cannot use official calendar evidence"
                    )
                from research_platform import official_trading_calendar as official

                if raw_protocol != official.PROTOCOL_VERSION:
                    raise _SourceEvidenceError(
                        "trading_calendar official manifest protocol mismatch"
                    )
                if official_calendar_manifest_hash not in {"", raw_hash}:
                    raise _SourceEvidenceError(
                        "trading_calendar partitions bind different official manifests"
                    )
                official_calendar_manifest_hash = raw_hash
                if official_calendar_artifact is None:
                    try:
                        official_calendar_artifact = (
                            official.OfficialTradingCalendarManifestStore(
                                official.OfficialTradingCalendarCAS(input_cas_root)
                            ).replay(raw_hash)
                        )
                    except official.OfficialTradingCalendarBlockedError as exc:
                        raise _SourceEvidenceError(
                            f"trading_calendar official manifest failed cold replay: {exc}"
                        ) from exc
            elif role == "SSE_OFFICIAL_DAILY_BARS_MANIFEST":
                if (
                    dataset != "raw_execution_bars"
                    or exchange != "SSE"
                    or not sse_official_raw_bars_index
                ):
                    raise _SourceEvidenceError(
                        f"{dataset} cannot use SSE official bar evidence"
                    )
                from research_platform import sse_delisted_raw_bars as sse_bars

                if raw_protocol != sse_bars.PROTOCOL_VERSION:
                    raise _SourceEvidenceError(
                        "raw_execution_bars SSE official manifest protocol mismatch"
                    )
                if sse_official_manifest_hash:
                    raise _SourceEvidenceError(
                        "raw_execution_bars partition has multiple SSE official manifests"
                    )
                sse_official_manifest_hash = raw_hash
                try:
                    artifact = sse_bars.SSEDelistedRawBarsManifestStore(
                        sse_bars.SSEDelistedRawBarsCAS(input_cas_root)
                    ).replay(raw_hash)
                except sse_bars.OfficialHistoricalBarsBlockedError as exc:
                    raise _SourceEvidenceError(
                        "raw_execution_bars SSE official manifest failed cold "
                        f"replay: {exc}"
                    ) from exc
                sse_official_bar_artifacts[raw_hash] = artifact
            elif role == "SOURCE_DOCUMENT":
                if raw_protocol != f"{dataset}-source-document-v1":
                    raise _SourceEvidenceError(
                        f"{dataset} source-document protocol mismatch"
                    )
                if not raw_content:
                    raise _SourceEvidenceError(
                        f"{dataset} source document is empty"
                    )
                document_hashes.add(raw_hash)
            else:
                raise _SourceEvidenceError(f"{dataset} raw source role is invalid")
            raw_hashes.add(raw_hash)
            partition_raw_hashes.append(raw_hash)
        if replayed_rows is None:
            raise _SourceEvidenceError(
                f"{dataset} partition has no replayable raw rows envelope"
            )
        if tuple(_canonical_json_bytes(row) for row in replayed_rows) != tuple(
            _canonical_json_bytes(row) for row in rows
        ):
            raise _SourceEvidenceError(
                f"{dataset} normalized partition does not replay from raw bytes"
            )
        if sse_official_raw_bars_index:
            if exchange != "SSE":
                raise _SourceEvidenceError(
                    "SSE official raw-bars index cannot declare SZSE coverage"
                )
            if not sse_official_manifest_hash:
                raise _SourceEvidenceError(
                    "raw_execution_bars partition has no SSE official manifest"
                )
            artifact = sse_official_bar_artifacts[sse_official_manifest_hash]
            if artifact.code != code:
                raise _SourceEvidenceError(
                    "raw_execution_bars SSE manifest code does not match partition"
                )
            official_rows = tuple(
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
            )
            if tuple(_canonical_json_bytes(row) for row in official_rows) != tuple(
                _canonical_json_bytes(row) for row in rows
            ):
                raise _SourceEvidenceError(
                    "raw_execution_bars partition does not match SSE official manifest"
                )
        if dataset == "trading_calendar" and official_calendar_artifact is not None:
            official_partition_rows = tuple(
                {
                    "exchange": item.exchange,
                    "trade_date": item.trade_date,
                    "is_open": item.is_open,
                }
                for item in official_calendar_artifact.rows
                if item.exchange == exchange
                and item.trade_date.startswith(f"{year:04d}-")
            )
            if tuple(
                _canonical_json_bytes(row) for row in official_partition_rows
            ) != tuple(_canonical_json_bytes(row) for row in rows):
                raise _SourceEvidenceError(
                    "trading_calendar partition does not match official manifest"
                )
        if dataset == "adjusted_bars_factors" and rows:
            ordered_rows = sorted(rows, key=lambda item: str(item["trade_date"]))
            first = ordered_rows[0]
            anchor_date = _iso_date(first.get("anchor_trade_date"), "anchor_trade_date")
            first_date = _iso_date(first["trade_date"], "trade_date")
            _finite_number(
                first.get("anchor_adjustment_factor"),
                "anchor_adjustment_factor",
                positive=True,
            )
            if anchor_date >= first_date:
                raise _SourceEvidenceError(
                    "adjusted_bars_factors anchor must precede the first partition bar"
                )
            for later in ordered_rows[1:]:
                if (
                    later.get("anchor_trade_date") is not None
                    or later.get("anchor_adjustment_factor") is not None
                ):
                    raise _SourceEvidenceError(
                        "adjusted_bars_factors contains a non-leading factor anchor"
                    )
        for row in rows:
            _validate_partition_row(dataset, row, exchange, code, year)
            row_hash_field = ROW_SOURCE_HASH_FIELDS.get(dataset)
            if row_hash_field and str(row.get(row_hash_field) or "") not in document_hashes:
                raise _SourceEvidenceError(
                    f"{dataset} {row_hash_field} is not backed by a source document"
                )
            row_key = _canonical_json_bytes(row)
            unique_rows[row_key] = row
        total_rows += row_count
        partitions[key] = _Partition(
            exchange=exchange,
            year=year,
            code=code,
            query_start=query_start,
            query_end=query_end,
            row_count=row_count,
            rows=rows,
            content_hash=content_hash,
            raw_source_hashes=tuple(sorted(set(partition_raw_hashes))),
        )
    try:
        declared_total = int(index.get("row_count", -1))
    except (TypeError, ValueError) as exc:
        raise _SourceEvidenceError(f"{dataset} index row_count is invalid") from exc
    if declared_total != total_rows:
        raise _SourceEvidenceError(f"{dataset} index row_count mismatch")
    if csrc_industry_plan is not None:
        from research_platform import csrc_industry_history_source as csrc

        try:
            reference = csrc.replay_industry_history_quality_index(
                cas_root=input_cas_root,
                source_index_sha256=index_hash,
                snapshot_manifest_sha256s=(
                    csrc_industry_plan.snapshot_manifest_sha256s
                ),
                authoritative_master_snapshot_id=str(
                    authoritative_master_snapshot_id or ""
                ),
                authoritative_targets=csrc_industry_plan.frozen_targets,
            )
        except csrc.CSRCIndustryHistoryBlockedError as exc:
            raise _SourceEvidenceError(
                "industry_history CSRC/CAPCO index failed authoritative "
                f"cold replay: {exc}"
            ) from exc
        expected_index_path = _expected_cas_path(
            input_cas_root, index_hash
        ).resolve()
        if (
            reference.content_hash != index_hash
            or Path(reference.object_path).resolve() != expected_index_path
            or reference.byte_count != len(index_bytes)
            or reference.master_snapshot_id
            != authoritative_master_snapshot_id
            or reference.target_count != len(csrc_industry_plan.frozen_targets)
            or reference.evidence_target_count
            != csrc_industry_plan.evidence_target_count
            or reference.covered_target_count
            != csrc_industry_plan.covered_target_count
            or reference.partition_count != len(partitions)
            or reference.row_count != total_rows
        ):
            raise _SourceEvidenceError(
                "industry_history CSRC/CAPCO replay identity mismatch"
            )
    if (
        dataset == "trading_calendar"
        and upstream_evidence is not None
        and official_calendar_artifact is None
    ):
        raise _SourceEvidenceError(
            "trading_calendar upstream evidence has no official manifest source"
        )
    if official_calendar_artifact is not None:
        expected_fields = {
            "kind",
            "protocol_version",
            "manifest_sha256",
            "logical_content_sha256",
            "cas_uri",
            "object_path",
            "byte_count",
        }
        if not isinstance(upstream_evidence, dict) or set(upstream_evidence) != expected_fields:
            raise _SourceEvidenceError(
                "trading_calendar upstream evidence schema drift"
            )
        from research_platform import official_trading_calendar as official

        if (
            upstream_evidence.get("kind") != "OFFICIAL_TRADING_CALENDAR_V2"
            or upstream_evidence.get("protocol_version") != official.PROTOCOL_VERSION
            or upstream_evidence.get("manifest_sha256")
            != official_calendar_manifest_hash
            or upstream_evidence.get("logical_content_sha256")
            != official_calendar_artifact.logical_content_sha256
            or upstream_evidence.get("cas_uri")
            != f"sha256:{official_calendar_manifest_hash}"
        ):
            raise _SourceEvidenceError(
                "trading_calendar upstream evidence identity mismatch"
            )
        manifest_content = _read_input_cas(
            input_cas_root,
            content_hash=official_calendar_manifest_hash,
            object_path=upstream_evidence.get("object_path"),
            label="trading_calendar official manifest",
        )
        if int(upstream_evidence.get("byte_count", -1)) != len(manifest_content):
            raise _SourceEvidenceError(
                "trading_calendar official manifest byte_count mismatch"
            )
    return _LoadedDataset(
        name=dataset,
        index_hash=index_hash,
        index_object_path=str(
            _expected_cas_path(input_cas_root, index_hash).resolve()
        ),
        index_byte_count=len(index_bytes),
        source_protocol_version=contract.source_protocol_version,
        schema_version=contract.schema_version,
        source_authority="|".join(sorted(raw_authorities)),
        row_count=total_rows,
        partitions=partitions,
        rows=tuple(unique_rows.values()),
        raw_source_hashes=tuple(sorted(raw_hashes)),
    )


def _active_years(target: _TargetInterval) -> tuple[int, ...]:
    return tuple(range(target.start.year, (target.end_exclusive - timedelta(days=1)).year + 1))


def _business_dates(start: date, end_exclusive: date) -> Iterable[date]:
    current = start
    while current < end_exclusive:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _row_map(
    dataset: _LoadedDataset | None,
    keys: Sequence[str],
    findings: list[_Finding],
) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    result: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    if dataset is None:
        return result
    for row in dataset.rows:
        key = tuple(row.get(field) for field in keys)
        if key in result and result[key] != row:
            findings.append(
                _Finding(
                    code="DUPLICATE_DATA_GRAIN",
                    dataset=dataset.name,
                    detail=f"conflicting duplicate key: {key}",
                )
            )
        else:
            result[key] = row
    return result


def _validate_timestamp_pair(
    row: Mapping[str, Any],
    dataset: str,
    findings: list[_Finding],
    *,
    availability_date: date | None = None,
) -> None:
    try:
        published = _iso_datetime(row["published_at"], "published_at")
        effective = _iso_datetime(row["effective_at"], "effective_at")
        if effective < published:
            raise ValueError("effective_at precedes published_at")
        if availability_date is not None and effective.date() > availability_date:
            raise ValueError("effective_at is after the row becomes applicable")
    except (KeyError, ValueError) as exc:
        findings.append(
            _Finding(
                code="POINT_IN_TIME_FIELDS_INVALID",
                dataset=dataset,
                exchange=str(row.get("exchange") or ""),
                security_code=str(row.get("code") or ""),
                detail=str(exc),
            )
        )


def _validate_rows(
    loaded: Mapping[str, _LoadedDataset], findings: list[_Finding]
) -> None:
    for dataset, evidence in loaded.items():
        for row in evidence.rows:
            hash_field = ROW_SOURCE_HASH_FIELDS.get(dataset)
            if hash_field and not _is_sha256(row.get(hash_field)):
                findings.append(
                    _Finding(
                        code="ROW_SOURCE_HASH_INVALID",
                        dataset=dataset,
                        exchange=str(row.get("exchange") or ""),
                        security_code=str(row.get("code") or ""),
                        detail=f"{hash_field} is not SHA-256",
                    )
                )
            elif hash_field and str(row.get(hash_field)) not in set(
                evidence.raw_source_hashes
            ):
                findings.append(
                    _Finding(
                        code="ROW_SOURCE_OBJECT_MISSING",
                        dataset=dataset,
                        exchange=str(row.get("exchange") or ""),
                        security_code=str(row.get("code") or ""),
                        detail=f"{hash_field} is not backed by a verified partition raw object",
                    )
                )
            try:
                if dataset == "raw_execution_bars":
                    values = {
                        field: _finite_number(row[field], field, positive=True)
                        for field in ("open", "high", "low", "close")
                    }
                    volume = _finite_number(row["volume"], "volume")
                    amount = _finite_number(row["amount"], "amount")
                    if volume < 0 or amount < 0:
                        raise ValueError("volume and amount must be nonnegative")
                    if values["high"] < max(values["open"], values["close"], values["low"]):
                        raise ValueError("OHLC high is inconsistent")
                    if values["low"] > min(values["open"], values["close"], values["high"]):
                        raise ValueError("OHLC low is inconsistent")
                elif dataset == "adjusted_bars_factors":
                    for field in (
                        "front_open",
                        "front_high",
                        "front_low",
                        "front_close",
                        "adjustment_factor",
                    ):
                        _finite_number(row[field], field, positive=True)
                elif dataset == "trading_calendar":
                    if not isinstance(row["is_open"], bool):
                        raise ValueError("is_open must be boolean")
                    trade_date = _iso_date(row["trade_date"], "trade_date")
                    if row["is_open"] and trade_date.weekday() >= 5:
                        raise ValueError("weekend cannot be an open trading day")
                elif dataset == "financial_reports":
                    period_end = _iso_date(row["period_end"], "period_end")
                    if row["report_type"] not in {"ANNUAL", "Q1", "HALF_YEAR", "Q3"}:
                        raise ValueError("financial report_type is invalid")
                    for field in (
                        "revenue",
                        "revenue_yoy",
                        "net_profit",
                        "net_profit_yoy",
                        "gross_margin",
                        "roe",
                        "operating_cash_flow",
                    ):
                        _finite_number(row[field], field)
                    _validate_timestamp_pair(row, dataset, findings)
                    if _iso_datetime(row["published_at"], "published_at").date() < period_end:
                        raise ValueError("financial report is published before period_end")
                elif dataset == "earnings_guidance_express":
                    period_end = _iso_date(row["period_end"], "period_end")
                    if row["event_type"] not in {"GUIDANCE", "EXPRESS"}:
                        raise ValueError("earnings event_type is invalid")
                    for field in ("forecast_low", "forecast_high", "previous_value"):
                        _finite_number(row[field], field)
                    if float(row["forecast_low"]) > float(row["forecast_high"]):
                        raise ValueError("forecast_low exceeds forecast_high")
                    _validate_timestamp_pair(row, dataset, findings)
                    if _iso_datetime(row["published_at"], "published_at").date() < period_end:
                        raise ValueError("earnings event is published before period_end")
                elif dataset == "gp15_price_limits":
                    trade_date = _iso_date(row["trade_date"], "trade_date")
                    upper = _finite_number(row["limit_up"], "limit_up", positive=True)
                    lower = _finite_number(row["limit_down"], "limit_down", positive=True)
                    if upper <= lower:
                        raise ValueError("limit_up must exceed limit_down")
                    _validate_timestamp_pair(
                        row, dataset, findings, availability_date=trade_date
                    )
                elif dataset in {"gp29_st_status", "industry_history"}:
                    start = _iso_date(row["valid_from"], "valid_from")
                    if row.get("valid_to") is not None:
                        end = _iso_date(row["valid_to"], "valid_to")
                        if end <= start:
                            raise ValueError("valid_to must be after valid_from")
                    if dataset == "gp29_st_status" and row["status"] not in {
                        "NORMAL",
                        "ST",
                        "STAR_ST",
                        "DELISTING",
                    }:
                        raise ValueError("ST status is invalid")
                    _validate_timestamp_pair(
                        row, dataset, findings, availability_date=start
                    )
                elif dataset in {"gp30_corporate_actions", "gp43_corporate_actions"}:
                    ex_date = _iso_date(row["ex_date"], "ex_date")
                    _finite_number(row["ratio"], "ratio")
                    _finite_number(row["cash_amount"], "cash_amount")
                    if not str(row["event_id"]).strip() or not str(row["event_type"]).strip():
                        raise ValueError("corporate action event identity is missing")
                    _validate_timestamp_pair(
                        row, dataset, findings, availability_date=ex_date
                    )
                elif dataset == "announcement_documents":
                    _validate_timestamp_pair(row, dataset, findings)
                    if not str(row["announcement_id"]).strip() or not str(row["url"]).startswith(
                        "https://"
                    ):
                        raise ValueError("announcement identity or HTTPS URL is invalid")
                elif dataset == "suspension_status":
                    trade_date = _iso_date(row["trade_date"], "trade_date")
                    if row["status"] not in {"SUSPENDED", "TRADING"}:
                        raise ValueError("suspension status is invalid")
                    _validate_timestamp_pair(
                        row, dataset, findings, availability_date=trade_date
                    )
            except (KeyError, TypeError, ValueError) as exc:
                findings.append(
                    _Finding(
                        code="ROW_DOMAIN_INVALID",
                        dataset=dataset,
                        exchange=str(row.get("exchange") or ""),
                        security_code=str(row.get("code") or ""),
                        detail=str(exc),
                    )
                )


def _interval_matches(
    rows: Sequence[Mapping[str, Any]], target_date: date
) -> list[Mapping[str, Any]]:
    matches: list[Mapping[str, Any]] = []
    for row in rows:
        start = _iso_date(row["valid_from"], "valid_from")
        end = _iso_date(row["valid_to"], "valid_to") if row.get("valid_to") else date.max
        if start <= target_date < end:
            matches.append(row)
    return matches


def _corporate_action_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row["exchange"]),
        str(row["code"]),
        str(row["event_type"]),
        str(row["ex_date"]),
        float(row["ratio"]),
        float(row["cash_amount"]),
    )


_AUDITOR_SEAL = object()


class DelistedHistoryQualityCAS:
    """Immutable CAS for canonical audit reports and their manifests."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects" / "sha256"
        self.manifests = self.root / "manifests"
        self.current = self.root / "current.json"

    def _write_exact(self, path: Path, content: bytes) -> None:
        _validate_no_symlink(path.parent, "audit CAS parent")
        path.parent.mkdir(parents=True, exist_ok=True)
        _validate_no_symlink(path.parent, "audit CAS parent")
        if path.exists():
            if _read_exact_file(path, "existing audit CAS object") != content:
                raise DelistedHistoryQualityBlockedError(
                    f"content-address collision or corruption: {path}"
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
            metadata = os.fstat(descriptor)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if not stat.S_ISREG(metadata.st_mode) or (
                attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            ):
                os.close(descriptor)
                raise DelistedHistoryQualityBlockedError(
                    "audit CAS temporary is not a plain regular file"
                )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _validate_no_symlink(temporary, "audit CAS temporary")
            if _read_exact_file(temporary, "audit CAS temporary") != content:
                raise DelistedHistoryQualityBlockedError(
                    "audit CAS temporary verification failed"
                )
            if path.exists():
                if _read_exact_file(path, "existing audit CAS object") != content:
                    raise DelistedHistoryQualityBlockedError(
                        f"content-address collision or corruption: {path}"
                    )
            else:
                _validate_no_symlink(path.parent, "audit CAS parent")
                os.replace(temporary, path)
                _validate_no_symlink(path, "published audit CAS object")
                if _read_exact_file(path, "published audit CAS object") != content:
                    raise DelistedHistoryQualityBlockedError(
                        "published audit CAS object verification failed"
                    )
        finally:
            if temporary.exists():
                temporary.unlink()

    def _write_object(self, content: bytes) -> tuple[str, Path]:
        digest = _sha256(content)
        path = self.objects / digest[:2] / digest
        self._write_exact(path, content)
        if _sha256(_read_exact_file(path, "audit CAS object")) != digest:
            raise DelistedHistoryQualityBlockedError("audit CAS verification failed")
        return digest, path

    def _publish(
        self,
        report: Mapping[str, Any],
        *,
        master_identity: Mapping[str, Any],
        source_identities: Mapping[str, Mapping[str, Any]],
        input_cas_root: Path,
        _seal: object,
    ) -> dict[str, Any]:
        if _seal is not _AUDITOR_SEAL:
            raise DelistedHistoryQualityBlockedError(
                "only DelistedHistoryQualityAuditor may publish an audit release"
            )
        _validate_no_symlink(Path(input_cas_root), "delisted-history input CAS")
        _validate_no_symlink(self.root, "delisted-history audit output")
        report_bytes = _canonical_json_bytes(dict(report))
        report_hash, report_path = self._write_object(report_bytes)
        manifest = {
            "protocol_version": PROTOCOL_VERSION,
            "quality_policy_version": QUALITY_POLICY_VERSION,
            "master_identity": dict(master_identity),
            "input_cas_root": str(Path(input_cas_root).resolve()),
            "source_indexes": {
                name: {
                    "content_hash": str(value.get("content_hash") or ""),
                    "cas_uri": f"sha256:{value.get('content_hash')}",
                    "object_path": str(value.get("object_path") or ""),
                    "byte_count": int(value.get("byte_count", -1)),
                }
                for name, value in sorted(source_identities.items())
            },
            "artifacts": {
                "audit_report": {
                    "content_hash": report_hash,
                    "cas_uri": f"sha256:{report_hash}",
                    "byte_count": len(report_bytes),
                }
            },
        }
        manifest_bytes = _canonical_json_bytes(manifest)
        manifest_hash = _sha256(manifest_bytes)
        manifest_path = self.manifests / f"{manifest_hash}.json"
        self._write_exact(manifest_path, manifest_bytes)
        pointer = {
            "protocol_version": PROTOCOL_VERSION,
            "manifest_hash": manifest_hash,
            "manifest_path": str(manifest_path.resolve()),
        }
        pointer_bytes = _canonical_json_bytes(pointer)
        temporary = self.current.with_name(f".{self.current.name}.{uuid.uuid4().hex}.tmp")
        _validate_no_symlink(self.current.parent, "audit pointer parent")
        self.current.parent.mkdir(parents=True, exist_ok=True)
        _validate_no_symlink(self.current.parent, "audit pointer parent")
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(temporary, flags, 0o600)
            metadata = os.fstat(descriptor)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if not stat.S_ISREG(metadata.st_mode) or (
                attributes & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            ):
                os.close(descriptor)
                raise DelistedHistoryQualityBlockedError(
                    "audit pointer temporary is not a plain regular file"
                )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(pointer_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            _validate_no_symlink(temporary, "audit pointer temporary")
            if _read_exact_file(temporary, "audit pointer temporary") != pointer_bytes:
                raise DelistedHistoryQualityBlockedError(
                    "audit pointer temporary verification failed"
                )
            if self.current.exists():
                _validate_no_symlink(self.current, "existing audit pointer")
            _validate_no_symlink(self.current.parent, "audit pointer parent")
            os.replace(temporary, self.current)
            _validate_no_symlink(self.current, "published audit pointer")
            if _read_exact_file(self.current, "published audit pointer") != pointer_bytes:
                raise DelistedHistoryQualityBlockedError(
                    "published audit pointer verification failed"
                )
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "manifest_hash": manifest_hash,
            "manifest_path": str(manifest_path.resolve()),
            "report_hash": report_hash,
            "report_path": str(report_path.resolve()),
            "report": dict(report),
        }


class DelistedHistoryQualityAuditor:
    def __init__(self, *, input_cas_root: Path, output_root: Path) -> None:
        self.input_cas_root = Path(input_cas_root)
        self.store = DelistedHistoryQualityCAS(output_root)

    def audit(
        self,
        *,
        master_records: Sequence[Any],
        master_identity: Mapping[str, Any],
        source_indexes: Mapping[str, Mapping[str, Any]],
        _publish_release: bool = True,
    ) -> dict[str, Any]:
        verified_records, verified_master = _verify_master(
            master_records, master_identity
        )
        targets = _target_intervals(verified_records)
        findings: list[_Finding] = []
        loaded: dict[str, _LoadedDataset] = {}
        invalid_datasets: set[str] = set()
        for dataset in REQUIRED_DATASETS:
            identity = source_indexes.get(dataset)
            if identity is None:
                findings.append(
                    _Finding(
                        code="SOURCE_INDEX_MISSING",
                        dataset=dataset,
                        detail=f"required source index is absent: {dataset}",
                    )
                )
                continue
            try:
                loaded[dataset] = _load_dataset(
                    dataset,
                    identity,
                    self.input_cas_root,
                    authoritative_master_snapshot_id=verified_master[
                        "snapshot_id"
                    ],
                    authoritative_targets=targets,
                )
            except (_SourceEvidenceError, OSError, ValueError, TypeError) as exc:
                invalid_datasets.add(dataset)
                findings.append(
                    _Finding(
                        code="SOURCE_EVIDENCE_INVALID",
                        dataset=dataset,
                        detail=str(exc),
                    )
                )
        if not targets:
            findings.append(
                _Finding(
                    code="NO_DELISTED_TARGETS",
                    detail="the verified master has no SSE/SZSE terminated listing overlapping 2018-2023",
                )
            )

        coverage_rows: list[dict[str, Any]] = []
        required_partitions: set[tuple[str, str, int, str]] = set()
        for target in targets:
            for year in _active_years(target):
                for dataset, contract in DATASET_CONTRACTS.items():
                    code = target.code if contract.code_scoped else "*"
                    required_partitions.add((dataset, target.exchange, year, code))
        for dataset, exchange, year, code in sorted(required_partitions):
            evidence = loaded.get(dataset)
            partition = evidence.partitions.get((exchange, year, code)) if evidence else None
            covered = partition is not None
            coverage_rows.append(
                {
                    "dataset": dataset,
                    "exchange": exchange,
                    "year": year,
                    "code": code,
                    "required": True,
                    "covered": covered,
                    "row_count": partition.row_count if partition else 0,
                    "content_hash": partition.content_hash if partition else "",
                }
            )
            if not covered:
                findings.append(
                    _Finding(
                        code="PARTITION_COVERAGE_MISSING",
                        dataset=dataset,
                        exchange=exchange,
                        year=year,
                        security_code=code,
                        detail="required exchange/year/code partition is absent",
                    )
                )

        _validate_rows(loaded, findings)
        calendar_map = _row_map(
            loaded.get("trading_calendar"), ("exchange", "trade_date"), findings
        )
        raw_map = _row_map(
            loaded.get("raw_execution_bars"),
            ("exchange", "code", "trade_date"),
            findings,
        )
        adjusted_map = _row_map(
            loaded.get("adjusted_bars_factors"),
            ("exchange", "code", "trade_date"),
            findings,
        )
        limit_map = _row_map(
            loaded.get("gp15_price_limits"),
            ("exchange", "code", "trade_date"),
            findings,
        )
        suspension_map = _row_map(
            loaded.get("suspension_status"),
            ("exchange", "code", "trade_date"),
            findings,
        )
        targets_by_code: dict[tuple[str, str], list[_TargetInterval]] = defaultdict(
            list
        )
        for target in targets:
            targets_by_code[(target.exchange, target.code)].append(target)
        for dataset in ("raw_execution_bars", "adjusted_bars_factors"):
            evidence = loaded.get(dataset)
            if evidence is None:
                continue
            for row in evidence.rows:
                exchange = str(row["exchange"])
                code = str(row["code"])
                trade_date = _iso_date(row["trade_date"], "trade_date")
                intervals = targets_by_code.get((exchange, code), [])
                if not any(
                    interval.start <= trade_date < interval.end_exclusive
                    for interval in intervals
                ):
                    findings.append(
                        _Finding(
                            code="BAR_OUTSIDE_LISTING_INTERVAL",
                            dataset=dataset,
                            exchange=exchange,
                            year=trade_date.year,
                            security_code=code,
                            detail=(
                                f"{trade_date.isoformat()} is outside every audited "
                                "effective listing interval"
                            ),
                        )
                    )
                    continue
                calendar = calendar_map.get((exchange, trade_date.isoformat()))
                if calendar is None or calendar.get("is_open") is not True:
                    findings.append(
                        _Finding(
                            code="BAR_NOT_MARKET_SESSION",
                            dataset=dataset,
                            exchange=exchange,
                            year=trade_date.year,
                            security_code=code,
                            detail=(
                                f"{trade_date.isoformat()} is not an open session in "
                                "the verified exchange calendar"
                            ),
                        )
                    )
        st_by_code: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in loaded.get("gp29_st_status", _empty_dataset("gp29_st_status")).rows:
            st_by_code[(str(row["exchange"]), str(row["code"]))].append(row)
        industry_by_code: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in loaded.get("industry_history", _empty_dataset("industry_history")).rows:
            industry_by_code[(str(row["exchange"]), str(row["code"]))].append(row)

        raw_authority = loaded.get("raw_execution_bars")
        suspension_authority = loaded.get("suspension_status")
        status_independent = bool(
            raw_authority
            and suspension_authority
            and raw_authority.source_authority != suspension_authority.source_authority
            and set(raw_authority.raw_source_hashes).isdisjoint(
                suspension_authority.raw_source_hashes
            )
        )
        if raw_authority and suspension_authority and not status_independent:
            findings.append(
                _Finding(
                    code="SUSPENSION_EVIDENCE_NOT_INDEPENDENT",
                    dataset="suspension_status",
                    detail="suspension evidence shares authority or raw hashes with bar evidence",
                )
            )

        for target in targets:
            tradable_dates: list[date] = []
            year_sessions: dict[int, dict[str, int]] = defaultdict(
                lambda: {
                    "weekdays": 0,
                    "calendar_rows": 0,
                    "open_sessions": 0,
                    "raw_sessions": 0,
                    "independent_suspensions": 0,
                }
            )
            for target_date in _business_dates(target.start, target.end_exclusive):
                date_text = target_date.isoformat()
                year_sessions[target_date.year]["weekdays"] += 1
                calendar = calendar_map.get((target.exchange, date_text))
                if calendar is None:
                    findings.append(
                        _Finding(
                            code="TRADING_CALENDAR_DATE_MISSING",
                            dataset="trading_calendar",
                            exchange=target.exchange,
                            year=target_date.year,
                            security_code=target.code,
                            detail=f"no open/closed status for weekday {date_text}",
                        )
                    )
                    continue
                year_sessions[target_date.year]["calendar_rows"] += 1
                for dataset, rows in (
                    ("gp29_st_status", st_by_code[(target.exchange, target.code)]),
                    ("industry_history", industry_by_code[(target.exchange, target.code)]),
                ):
                    try:
                        matches = _interval_matches(rows, target_date)
                    except ValueError as exc:
                        findings.append(
                            _Finding(
                                code="INTERVAL_EVIDENCE_INVALID",
                                dataset=dataset,
                                exchange=target.exchange,
                                year=target_date.year,
                                security_code=target.code,
                                detail=str(exc),
                            )
                        )
                        matches = []
                    if len(matches) != 1:
                        findings.append(
                            _Finding(
                                code=(
                                    "INTERVAL_COVERAGE_MISSING"
                                    if not matches
                                    else "INTERVAL_COVERAGE_OVERLAP"
                                ),
                                dataset=dataset,
                                exchange=target.exchange,
                                year=target_date.year,
                                security_code=target.code,
                                detail=f"{date_text} has {len(matches)} applicable rows",
                            )
                        )
                if not bool(calendar["is_open"]):
                    continue
                year_sessions[target_date.year]["open_sessions"] += 1
                key = (target.exchange, target.code, date_text)
                raw = raw_map.get(key)
                adjusted = adjusted_map.get(key)
                suspension = suspension_map.get(key)
                if raw is None:
                    if not (
                        status_independent
                        and suspension is not None
                        and suspension.get("status") == "SUSPENDED"
                    ):
                        findings.append(
                            _Finding(
                                code="RAW_BAR_MISSING_UNEXPLAINED",
                                dataset="raw_execution_bars",
                                exchange=target.exchange,
                                year=target_date.year,
                                security_code=target.code,
                                detail=f"{date_text} is open and has neither a raw bar nor independent suspension evidence",
                            )
                        )
                    else:
                        year_sessions[target_date.year][
                            "independent_suspensions"
                        ] += 1
                    if adjusted is not None:
                        findings.append(
                            _Finding(
                                code="ADJUSTED_BAR_WITHOUT_RAW",
                                dataset="adjusted_bars_factors",
                                exchange=target.exchange,
                                year=target_date.year,
                                security_code=target.code,
                                detail=date_text,
                            )
                        )
                else:
                    tradable_dates.append(target_date)
                    year_sessions[target_date.year]["raw_sessions"] += 1
                    if suspension is not None and suspension.get("status") == "SUSPENDED":
                        findings.append(
                            _Finding(
                                code="BAR_STATUS_CONTRADICTION",
                                dataset="suspension_status",
                                exchange=target.exchange,
                                year=target_date.year,
                                security_code=target.code,
                                detail=f"{date_text} has both a bar and SUSPENDED status",
                            )
                        )
                    if adjusted is None:
                        findings.append(
                            _Finding(
                                code="ADJUSTED_BAR_MISSING",
                                dataset="adjusted_bars_factors",
                                exchange=target.exchange,
                                year=target_date.year,
                                security_code=target.code,
                                detail=date_text,
                            )
                        )
                    else:
                        factor = float(adjusted["adjustment_factor"])
                        for raw_field, front_field in (
                            ("open", "front_open"),
                            ("high", "front_high"),
                            ("low", "front_low"),
                            ("close", "front_close"),
                        ):
                            expected = float(raw[raw_field]) * factor
                            actual = float(adjusted[front_field])
                            if not math.isclose(
                                expected,
                                actual,
                                rel_tol=1e-9,
                                abs_tol=PRICE_TOLERANCE,
                            ):
                                findings.append(
                                    _Finding(
                                        code="ADJUSTMENT_CROSSCHECK_FAILED",
                                        dataset="adjusted_bars_factors",
                                        exchange=target.exchange,
                                        year=target_date.year,
                                        security_code=target.code,
                                        detail=f"{date_text} {front_field} != raw * factor",
                                    )
                                )
                    limit = limit_map.get(key)
                    if limit is None:
                        findings.append(
                            _Finding(
                                code="GP15_LIMIT_MISSING",
                                dataset="gp15_price_limits",
                                exchange=target.exchange,
                                year=target_date.year,
                                security_code=target.code,
                                detail=date_text,
                            )
                        )
                    elif not (
                        float(limit["limit_down"]) - PRICE_TOLERANCE
                        <= float(raw["close"])
                        <= float(limit["limit_up"]) + PRICE_TOLERANCE
                    ):
                        findings.append(
                            _Finding(
                                code="GP15_LIMIT_CROSSCHECK_FAILED",
                                dataset="gp15_price_limits",
                                exchange=target.exchange,
                                year=target_date.year,
                                security_code=target.code,
                                detail=f"{date_text} close is outside the price limits",
                            )
                        )
            self._check_adjustment_events(
                target, tradable_dates, raw_map, adjusted_map, loaded, findings
            )
            for year, statistics in sorted(year_sessions.items()):
                weekdays = statistics["weekdays"]
                open_sessions = statistics["open_sessions"]
                raw_sessions = statistics["raw_sessions"]
                open_density = open_sessions / weekdays if weekdays else 0.0
                raw_density = raw_sessions / open_sessions if open_sessions else 0.0
                if open_sessions < 1 or open_density < MINIMUM_OPEN_SESSION_DENSITY:
                    findings.append(
                        _Finding(
                            code="TRADING_CALENDAR_SESSION_DENSITY_FAILED",
                            dataset="trading_calendar",
                            exchange=target.exchange,
                            year=year,
                            security_code=target.code,
                            detail=(
                                f"open={open_sessions}, weekdays={weekdays}, "
                                f"density={open_density:.6f}"
                            ),
                        )
                    )
                if raw_sessions < 1 or raw_density < MINIMUM_RAW_SESSION_DENSITY:
                    findings.append(
                        _Finding(
                            code="RAW_SESSION_DENSITY_FAILED",
                            dataset="raw_execution_bars",
                            exchange=target.exchange,
                            year=year,
                            security_code=target.code,
                            detail=(
                                f"raw={raw_sessions}, open={open_sessions}, "
                                f"density={raw_density:.6f}; a full-year empty bar "
                                "history is never admitted"
                            ),
                        )
                    )

        for target in targets:
            for year in _active_years(target):
                for dataset, contract in DATASET_CONTRACTS.items():
                    if not contract.rows_required_per_code_year:
                        continue
                    evidence = loaded.get(dataset)
                    if evidence is None:
                        continue
                    rows = [
                        row
                        for row in evidence.rows
                        if row.get("exchange") == target.exchange
                        and row.get("code") == target.code
                        and _row_overlaps_partition(row, contract, year)
                        and target.start
                        <= _iso_datetime(
                            row[contract.date_field], contract.date_field
                        ).date()
                        < target.end_exclusive
                    ]
                    if not rows:
                        findings.append(
                            _Finding(
                                code="REQUIRED_ANNUAL_ROWS_MISSING",
                                dataset=dataset,
                                exchange=target.exchange,
                                year=year,
                                security_code=target.code,
                                detail="a content-addressed empty response does not prove required annual data",
                            )
                        )

        finding_counts = Counter(item.code for item in findings)
        missing_codes = {
            "SOURCE_INDEX_MISSING",
            "PARTITION_COVERAGE_MISSING",
            "NO_DELISTED_TARGETS",
            "REQUIRED_ANNUAL_ROWS_MISSING",
            "RAW_BAR_MISSING_UNEXPLAINED",
            "ADJUSTED_BAR_MISSING",
            "GP15_LIMIT_MISSING",
            "INTERVAL_COVERAGE_MISSING",
            "TRADING_CALENDAR_DATE_MISSING",
            "TRADING_CALENDAR_SESSION_DENSITY_FAILED",
            "RAW_SESSION_DENSITY_FAILED",
        }
        has_missing = any(item.code in missing_codes for item in findings)
        if not findings:
            status = READY
            detail = (
                "All overlapping SSE/SZSE terminated listings have complete, "
                "time-correct and cross-reconciled evidence for 2018-2023"
            )
        elif has_missing and not invalid_datasets:
            status = DELISTED_HISTORY_SOURCE_INCOMPLETE
            detail = "Required delisted-history evidence is absent or incomplete"
        elif has_missing:
            status = DELISTED_HISTORY_SOURCE_INCOMPLETE
            detail = "Required delisted-history evidence is missing and some supplied evidence is invalid"
        else:
            status = DELISTED_HISTORY_QUALITY_REJECTED
            detail = "Supplied delisted-history evidence failed one or more hard quality gates"
        ready = status == READY
        missing_sample = [
            item.to_dict() for item in findings if item.code in missing_codes
        ][:MAX_MISSING_SAMPLE]
        aggregate: dict[tuple[str, str, int], dict[str, int]] = defaultdict(
            lambda: {"required_codes": 0, "covered_codes": 0, "row_count": 0}
        )
        for row in coverage_rows:
            key = (row["dataset"], row["exchange"], row["year"])
            aggregate[key]["required_codes"] += 1
            aggregate[key]["covered_codes"] += int(row["covered"])
            aggregate[key]["row_count"] += int(row["row_count"])
        aggregate_rows = []
        for (dataset, exchange, year), values in sorted(aggregate.items()):
            required_codes = values["required_codes"]
            covered_codes = values["covered_codes"]
            aggregate_rows.append(
                {
                    "dataset": dataset,
                    "exchange": exchange,
                    "year": year,
                    **values,
                    "coverage_rate": (
                        covered_codes / required_codes if required_codes else 0.0
                    ),
                }
            )
        source_hashes = []
        for dataset in REQUIRED_DATASETS:
            evidence = loaded.get(dataset)
            source_hashes.append(
                {
                    "dataset": dataset,
                    "index_hash": evidence.index_hash if evidence else "",
                    "index_object_path": (
                        evidence.index_object_path if evidence else ""
                    ),
                    "index_byte_count": (
                        evidence.index_byte_count if evidence else 0
                    ),
                    "source_protocol_version": (
                        evidence.source_protocol_version if evidence else ""
                    ),
                    "schema_version": evidence.schema_version if evidence else "",
                    "source_authority": evidence.source_authority if evidence else "",
                    "row_count": evidence.row_count if evidence else 0,
                    "raw_source_hashes": (
                        list(evidence.raw_source_hashes) if evidence else []
                    ),
                }
            )
        report = {
            "protocol_version": PROTOCOL_VERSION,
            "quality_policy_version": QUALITY_POLICY_VERSION,
            "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
            "master_identity": verified_master,
            "target_scope": {
                "security_count": len(targets),
                "exchange_counts": dict(
                    sorted(Counter(item.exchange for item in targets).items())
                ),
                "codes": [item.code for item in targets],
            },
            "gate": {
                "ready": ready,
                "status": status,
                "detail": detail,
                "promotion_blocked": not ready,
                "caller_ready_and_complete_flags_ignored": True,
                "hard_failure_count": len(findings),
                "finding_counts": dict(sorted(finding_counts.items())),
            },
            "coverage": {
                "by_dataset_exchange_year": aggregate_rows,
                "by_dataset_exchange_year_code": coverage_rows,
                "missing_sample": missing_sample,
            },
            "source_hashes": source_hashes,
            "findings": [item.to_dict() for item in findings[:MAX_FINDINGS]],
            "findings_truncated": max(0, len(findings) - MAX_FINDINGS),
        }
        verified_source_identities = {
            dataset: {
                "content_hash": evidence.index_hash,
                "object_path": evidence.index_object_path,
                "byte_count": evidence.index_byte_count,
            }
            for dataset, evidence in sorted(loaded.items())
        }
        if not _publish_release:
            return {
                "report": report,
                "master_identity": verified_master,
                "source_identities": verified_source_identities,
            }
        return self.store._publish(
            report,
            master_identity=verified_master,
            source_identities=verified_source_identities,
            input_cas_root=self.input_cas_root,
            _seal=_AUDITOR_SEAL,
        )

    @staticmethod
    def _check_adjustment_events(
        target: _TargetInterval,
        tradable_dates: Sequence[date],
        raw_map: Mapping[tuple[Any, ...], Mapping[str, Any]],
        adjusted_map: Mapping[tuple[Any, ...], Mapping[str, Any]],
        loaded: Mapping[str, _LoadedDataset],
        findings: list[_Finding],
    ) -> None:
        gp30 = [
            row
            for row in loaded.get("gp30_corporate_actions", _empty_dataset("gp30_corporate_actions")).rows
            if row.get("exchange") == target.exchange and row.get("code") == target.code
        ]
        gp43 = [
            row
            for row in loaded.get("gp43_corporate_actions", _empty_dataset("gp43_corporate_actions")).rows
            if row.get("exchange") == target.exchange and row.get("code") == target.code
        ]
        try:
            keys30 = {_corporate_action_key(row) for row in gp30}
            keys43 = {_corporate_action_key(row) for row in gp43}
        except (KeyError, TypeError, ValueError) as exc:
            findings.append(
                _Finding(
                    code="CORPORATE_ACTION_INVALID",
                    dataset="gp30_corporate_actions/gp43_corporate_actions",
                    exchange=target.exchange,
                    security_code=target.code,
                    detail=str(exc),
                )
            )
            return
        if keys30 != keys43:
            findings.append(
                _Finding(
                    code="CORPORATE_ACTION_SOURCES_DISAGREE",
                    dataset="gp30_corporate_actions/gp43_corporate_actions",
                    exchange=target.exchange,
                    security_code=target.code,
                    detail=f"GP30-only={len(keys30 - keys43)}, GP43-only={len(keys43 - keys30)}",
                )
            )
        action_dates = {date.fromisoformat(str(item[3])) for item in keys30 & keys43}
        previous_factor: float | None = None
        if tradable_dates:
            first_key = (
                target.exchange,
                target.code,
                min(tradable_dates).isoformat(),
            )
            first_adjusted = adjusted_map.get(first_key)
            if first_adjusted is not None:
                previous_factor = float(first_adjusted["anchor_adjustment_factor"])
        factor_change_dates: set[date] = set()
        for target_date in sorted(tradable_dates):
            key = (target.exchange, target.code, target_date.isoformat())
            if key not in raw_map or key not in adjusted_map:
                continue
            factor = float(adjusted_map[key]["adjustment_factor"])
            if previous_factor is not None and not math.isclose(
                factor, previous_factor, rel_tol=1e-12, abs_tol=1e-12
            ):
                factor_change_dates.add(target_date)
            previous_factor = factor
        unexplained = sorted(factor_change_dates - action_dates)
        missing_change = sorted(action_dates - factor_change_dates)
        if unexplained:
            findings.append(
                _Finding(
                    code="FACTOR_CHANGE_WITHOUT_CORPORATE_ACTION",
                    dataset="adjusted_bars_factors",
                    exchange=target.exchange,
                    security_code=target.code,
                    detail=", ".join(item.isoformat() for item in unexplained[:20]),
                )
            )
        if missing_change:
            findings.append(
                _Finding(
                    code="CORPORATE_ACTION_WITHOUT_FACTOR_CHANGE",
                    dataset="adjusted_bars_factors",
                    exchange=target.exchange,
                    security_code=target.code,
                    detail=", ".join(item.isoformat() for item in missing_change[:20]),
                )
            )


def _empty_dataset(name: str) -> _LoadedDataset:
    contract = DATASET_CONTRACTS[name]
    return _LoadedDataset(
        name=name,
        index_hash="",
        index_object_path="",
        index_byte_count=0,
        source_protocol_version=contract.source_protocol_version,
        schema_version=contract.schema_version,
        source_authority="",
        row_count=0,
        partitions={},
        rows=(),
        raw_source_hashes=(),
    )


def _read_canonical_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    content = _read_exact_file(path, label)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DelistedHistoryQualityBlockedError(
            f"{label} is not UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict) or _canonical_json_bytes(value) != content:
        raise DelistedHistoryQualityBlockedError(
            f"{label} is not a canonical JSON object"
        )
    return value, content


def _master_records_from_identity(identity: Mapping[str, Any]) -> list[dict[str, Any]]:
    object_path = Path(str(identity.get("security_master_object_path") or ""))
    content = _read_exact_file(object_path, "security-master JSONL")
    expected_hash = str(identity.get("security_master_content_hash") or "")
    if _sha256(content) != expected_hash:
        raise DelistedHistoryQualityBlockedError(
            "security-master JSONL hash changed after audit publication"
        )
    if len(content) != int(identity.get("security_master_byte_count", -1)):
        raise DelistedHistoryQualityBlockedError(
            "security-master JSONL byte_count changed after audit publication"
        )
    records: list[dict[str, Any]] = []
    for line in content.splitlines():
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DelistedHistoryQualityBlockedError(
                "security-master JSONL is invalid"
            ) from exc
        if not isinstance(row, dict) or _canonical_json_bytes(row) != line:
            raise DelistedHistoryQualityBlockedError(
                "security-master JSONL is not canonical"
            )
        records.append(row)
    if len(records) != int(identity.get("security_master_row_count", -1)):
        raise DelistedHistoryQualityBlockedError(
            "security-master JSONL row_count changed after audit publication"
        )
    return records


def load_verified_delisted_history_gate(
    *,
    output_root: Path,
    input_cas_root: Path,
    security_master_root: Path,
    expected_master_gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Read, replay, and recompute a frozen audit without publishing new files."""

    output_root = Path(output_root)
    input_cas_root = Path(input_cas_root)
    security_master_root = Path(security_master_root)
    missing = {
        "ready": False,
        "status": DELISTED_HISTORY_SOURCE_INCOMPLETE,
        "detail": "No replayable delisted-history audit exists for the current master",
        "promotion_blocked": True,
        "protocol_version": PROTOCOL_VERSION,
        "quality_policy_version": QUALITY_POLICY_VERSION,
        "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
        "required_datasets": list(REQUIRED_DATASETS),
        "required_source_dataset_count": len(REQUIRED_DATASETS),
        "source_datasets": [],
        "missing_source_datasets": list(REQUIRED_DATASETS),
        "finding_counts": {},
        "historical_security_master_snapshot": str(
            expected_master_gate.get("snapshot_id") or ""
        ),
        "manifest_hash": "",
        "report_hash": "",
    }
    pointer_path = output_root / "current.json"
    if not pointer_path.is_file():
        return missing
    try:
        pointer, _ = _read_canonical_json_object(
            pointer_path, "delisted-history current pointer"
        )
        if set(pointer) != {"protocol_version", "manifest_hash", "manifest_path"}:
            raise DelistedHistoryQualityBlockedError("current pointer schema drift")
        if pointer.get("protocol_version") != PROTOCOL_VERSION:
            raise DelistedHistoryQualityBlockedError("current pointer protocol mismatch")
        manifest_hash = str(pointer.get("manifest_hash") or "")
        if not _is_sha256(manifest_hash):
            raise DelistedHistoryQualityBlockedError("manifest hash is invalid")
        manifest_path = Path(str(pointer.get("manifest_path") or ""))
        expected_manifest = output_root / "manifests" / f"{manifest_hash}.json"
        if manifest_path.resolve() != expected_manifest.resolve():
            raise DelistedHistoryQualityBlockedError("manifest path escapes audit root")
        manifest, manifest_bytes = _read_canonical_json_object(
            manifest_path, "delisted-history manifest"
        )
        if _sha256(manifest_bytes) != manifest_hash:
            raise DelistedHistoryQualityBlockedError("manifest hash mismatch")
        if (
            manifest.get("protocol_version") != PROTOCOL_VERSION
            or manifest.get("quality_policy_version") != QUALITY_POLICY_VERSION
        ):
            raise DelistedHistoryQualityBlockedError("manifest protocol mismatch")
        if Path(str(manifest.get("input_cas_root") or "")).resolve() != input_cas_root.resolve():
            raise DelistedHistoryQualityBlockedError("manifest input CAS root mismatch")
        master_identity = dict(manifest.get("master_identity") or {})
        snapshot_id = str(expected_master_gate.get("snapshot_id") or "")
        if (
            master_identity.get("snapshot_id") != snapshot_id
            or master_identity.get("manifest_hash")
            != expected_master_gate.get("manifest_hash")
        ):
            raise DelistedHistoryQualityBlockedError(
                "audit is not bound to the current security master"
            )
        expected_master_manifest = (
            security_master_root / "manifests" / f"{snapshot_id}.json"
        )
        if Path(str(master_identity.get("manifest_path") or "")).resolve() != expected_master_manifest.resolve():
            raise DelistedHistoryQualityBlockedError(
                "security-master manifest path is not the platform master store"
            )
        master_content_hash = str(
            master_identity.get("security_master_content_hash") or ""
        )
        expected_master_object = (
            security_master_root
            / "objects"
            / master_content_hash[:2]
            / master_content_hash
        )
        if Path(str(master_identity.get("security_master_object_path") or "")).resolve() != expected_master_object.resolve():
            raise DelistedHistoryQualityBlockedError(
                "security-master object path is not the platform master store"
            )
        raw_source_entries = manifest.get("source_indexes")
        if not isinstance(raw_source_entries, dict):
            raise DelistedHistoryQualityBlockedError(
                "manifest source_indexes schema drift"
            )
        source_entries = dict(raw_source_entries)
        unknown_datasets = set(source_entries).difference(REQUIRED_DATASETS)
        if unknown_datasets:
            raise DelistedHistoryQualityBlockedError(
                "manifest binds unknown source indexes: "
                + ", ".join(sorted(unknown_datasets))
            )
        source_indexes: dict[str, dict[str, Any]] = {}
        for dataset in REQUIRED_DATASETS:
            if dataset not in source_entries:
                continue
            raw_entry = source_entries[dataset]
            if not isinstance(raw_entry, dict):
                raise DelistedHistoryQualityBlockedError(
                    f"{dataset} manifest source identity schema drift"
                )
            entry = dict(raw_entry)
            if set(entry) != {"content_hash", "cas_uri", "object_path", "byte_count"}:
                raise DelistedHistoryQualityBlockedError(
                    f"{dataset} manifest source identity schema drift"
                )
            content_hash = str(entry.get("content_hash") or "")
            expected_path = _expected_cas_path(input_cas_root, content_hash)
            if (
                not _is_sha256(content_hash)
                or entry.get("cas_uri") != f"sha256:{content_hash}"
                or Path(str(entry.get("object_path") or "")).resolve()
                != expected_path.resolve()
            ):
                raise DelistedHistoryQualityBlockedError(
                    f"{dataset} source index CAS identity mismatch"
                )
            index_bytes = _read_input_cas(
                input_cas_root,
                content_hash=content_hash,
                object_path=entry["object_path"],
                label=f"{dataset} source index",
            )
            if len(index_bytes) != int(entry.get("byte_count", -1)):
                raise DelistedHistoryQualityBlockedError(
                    f"{dataset} source index byte_count mismatch"
                )
            source_indexes[dataset] = {
                "content_hash": content_hash,
                "object_path": str(expected_path.resolve()),
            }
        artifacts = dict(manifest.get("artifacts") or {})
        if set(artifacts) != {"audit_report"}:
            raise DelistedHistoryQualityBlockedError("audit artifact schema drift")
        report_identity = dict(artifacts["audit_report"])
        if set(report_identity) != {"content_hash", "cas_uri", "byte_count"}:
            raise DelistedHistoryQualityBlockedError("report identity schema drift")
        report_hash = str(report_identity.get("content_hash") or "")
        report_path = (
            output_root / "objects" / "sha256" / report_hash[:2] / report_hash
        )
        if (
            not _is_sha256(report_hash)
            or report_identity.get("cas_uri") != f"sha256:{report_hash}"
        ):
            raise DelistedHistoryQualityBlockedError("report CAS identity mismatch")
        report, report_bytes = _read_canonical_json_object(
            report_path, "delisted-history audit report"
        )
        if (
            _sha256(report_bytes) != report_hash
            or len(report_bytes) != int(report_identity.get("byte_count", -1))
        ):
            raise DelistedHistoryQualityBlockedError("report content identity mismatch")
        gate = dict(report.get("gate") or {})
        complete_source_set = set(source_entries) == set(REQUIRED_DATASETS)
        if not complete_source_set and (
            gate.get("ready") is True
            or gate.get("status") == READY
            or gate.get("promotion_blocked") is False
        ):
            raise DelistedHistoryQualityBlockedError(
                "READY audit manifest does not bind every required source index"
            )
        master_records = _master_records_from_identity(master_identity)
        derived = DelistedHistoryQualityAuditor(
            input_cas_root=input_cas_root,
            output_root=output_root,
        ).audit(
            master_records=master_records,
            master_identity=master_identity,
            source_indexes=source_indexes,
            _publish_release=False,
        )
        derived_report = dict(derived["report"])
        if _canonical_json_bytes(derived_report) != report_bytes:
            raise DelistedHistoryQualityBlockedError(
                "stored report does not match a full source replay"
            )
        ready = (
            complete_source_set
            and gate.get("ready") is True
            and gate.get("status") == READY
            and gate.get("promotion_blocked") is False
            and int(gate.get("hard_failure_count", -1)) == 0
        )
        return {
            **missing,
            "ready": ready,
            "status": str(gate.get("status") or DELISTED_HISTORY_QUALITY_REJECTED),
            "detail": str(gate.get("detail") or ""),
            "promotion_blocked": not ready,
            "manifest_hash": manifest_hash,
            "report_hash": report_hash,
            "hard_failure_count": int(gate.get("hard_failure_count", -1)),
            "target_security_count": int(
                dict(report.get("target_scope") or {}).get("security_count", 0)
            ),
            "coverage_partition_count": len(
                dict(report.get("coverage") or {}).get(
                    "by_dataset_exchange_year_code", []
                )
            ),
            "source_dataset_count": len(source_entries),
            "required_source_dataset_count": len(REQUIRED_DATASETS),
            "source_datasets": sorted(source_entries),
            "missing_source_datasets": sorted(
                set(REQUIRED_DATASETS).difference(source_entries)
            ),
            "finding_counts": dict(gate.get("finding_counts") or {}),
        }
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        DelistedHistoryQualityBlockedError,
        _SourceEvidenceError,
    ) as exc:
        return {
            **missing,
            "status": "DELISTED_HISTORY_ARTIFACT_INVALID",
            "detail": f"Delisted-history audit replay failed: {exc}",
        }


def audit_delisted_history(
    *,
    master_records: Sequence[Any],
    master_identity: Mapping[str, Any],
    source_indexes: Mapping[str, Mapping[str, Any]],
    input_cas_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Audit frozen delisted-history evidence without fetching or generating data."""

    return DelistedHistoryQualityAuditor(
        input_cas_root=input_cas_root, output_root=output_root
    ).audit(
        master_records=master_records,
        master_identity=master_identity,
        source_indexes=source_indexes,
    )
