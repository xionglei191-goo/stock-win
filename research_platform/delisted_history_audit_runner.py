from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from research_platform.delisted_history_quality import (
    DELISTED_HISTORY_SOURCE_INCOMPLETE,
    DelistedHistoryQualityBlockedError,
    READY,
    REQUIRED_DATASETS,
    _load_dataset,
    _SourceEvidenceError,
    _target_intervals,
    _verify_master,
    audit_delisted_history,
    load_verified_delisted_history_gate,
)
from research_platform.historical_security_master import (
    HistoricalSecurityMasterBlockedError,
    HistoricalSecurityMasterStore,
)


PROJECT_ID = "early_winner_v4"
INPUT_CAS_DIRECTORY = "delisted_history_inputs"
OUTPUT_DIRECTORY = "delisted_history_quality"

CURRENT_TRADING_CALENDAR_INDEX_SHA256 = (
    "f1cf94245e1e94ee90d8f447793b253dcce5d24afe77a2f18690037f967f2f11"
)
CURRENT_SSE_RAW_EXECUTION_BARS_INDEX_SHA256 = (
    "4444e219c7aa9f7db0fa238e1f1107d0c43a6b5e064430be0d55d3156d123dea"
)
CURRENT_PARTIAL_SOURCE_INDEX_DIGESTS: Mapping[str, str] = MappingProxyType(
    {
        "raw_execution_bars": CURRENT_SSE_RAW_EXECUTION_BARS_INDEX_SHA256,
        "trading_calendar": CURRENT_TRADING_CALENDAR_INDEX_SHA256,
    }
)


class DelistedHistoryAuditRunnerBlockedError(RuntimeError):
    """Raised before a source mapping can be treated as replayable evidence."""


def run_delisted_history_audit(
    *,
    runtime_dir: Path,
    source_index_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Publish and independently replay an audit bound to the current master.

    The caller supplies only dataset-to-digest identities. Object paths and both
    storage roots are derived from the fixed early-winner V4 runtime layout.
    Readiness is always recomputed by the quality auditor; no caller readiness
    or completeness assertion is accepted.
    """

    runtime_root = Path(runtime_dir).resolve()
    security_master_root = runtime_root / "security_master"
    input_cas_root = (
        runtime_root / "research" / PROJECT_ID / INPUT_CAS_DIRECTORY
    )
    output_root = runtime_root / "research" / PROJECT_ID / OUTPUT_DIRECTORY
    if not input_cas_root.is_dir():
        raise DelistedHistoryAuditRunnerBlockedError(
            f"fixed delisted-history input CAS is missing: {input_cas_root}"
        )

    digests = _normalize_source_index_digests(source_index_digests)
    try:
        release = HistoricalSecurityMasterStore(
            security_master_root
        ).load_current_release()
        master_records, master_identity = _cold_replay_current_master(
            release=release,
            security_master_root=security_master_root,
        )
        targets = _target_intervals(master_records)
        source_indexes = _preflight_source_indexes(
            digests=digests,
            input_cas_root=input_cas_root,
            master_snapshot_id=str(master_identity["snapshot_id"]),
            targets=targets,
        )
        published = audit_delisted_history(
            master_records=master_records,
            master_identity=master_identity,
            source_indexes=source_indexes,
            input_cas_root=input_cas_root,
            output_root=output_root,
        )
        gate = load_verified_delisted_history_gate(
            output_root=output_root,
            input_cas_root=input_cas_root,
            security_master_root=security_master_root,
            expected_master_gate=master_identity,
        )
    except DelistedHistoryAuditRunnerBlockedError:
        raise
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise DelistedHistoryAuditRunnerBlockedError(
            f"delisted-history audit runner failed closed: {exc}"
        ) from exc
    except HistoricalSecurityMasterBlockedError as exc:
        raise DelistedHistoryAuditRunnerBlockedError(
            f"current historical security master is not replayable: {exc}"
        ) from exc
    except (DelistedHistoryQualityBlockedError, _SourceEvidenceError) as exc:
        raise DelistedHistoryAuditRunnerBlockedError(
            f"delisted-history audit failed closed: {exc}"
        ) from exc

    if (
        gate.get("manifest_hash") != published.get("manifest_hash")
        or gate.get("report_hash") != published.get("report_hash")
        or int(gate.get("source_dataset_count", -1)) != len(digests)
        or gate.get("status") == "DELISTED_HISTORY_ARTIFACT_INVALID"
    ):
        raise DelistedHistoryAuditRunnerBlockedError(
            "published delisted-history audit failed independent gate replay"
        )

    partial = set(digests) != set(REQUIRED_DATASETS)
    if partial and (
        gate.get("ready") is True
        or gate.get("status") != DELISTED_HISTORY_SOURCE_INCOMPLETE
        or gate.get("promotion_blocked") is not True
    ):
        raise DelistedHistoryAuditRunnerBlockedError(
            "partial source set produced an inadmissible readiness result"
        )
    if gate.get("ready") is True and gate.get("status") != READY:
        raise DelistedHistoryAuditRunnerBlockedError(
            "derived audit readiness is internally inconsistent"
        )

    return {
        "status": str(gate["status"]),
        "ready": gate.get("ready") is True,
        "promotion_blocked": gate.get("promotion_blocked") is not False,
        "partial_source_set": partial,
        "audit_only": True,
        "no_training": True,
        "no_trading": True,
        "caller_ready_accepted": False,
        "readiness_source": "INDEPENDENT_FULL_CAS_REPLAY",
        "historical_security_master_snapshot": str(master_identity["snapshot_id"]),
        "source_index_digests": dict(digests),
        "source_dataset_count": len(digests),
        "input_cas_root": str(input_cas_root.resolve()),
        "output_root": str(output_root.resolve()),
        "manifest_hash": str(published["manifest_hash"]),
        "report_hash": str(published["report_hash"]),
        "gate": dict(gate),
    }


def run_current_partial_source_example(*, runtime_dir: Path) -> dict[str, Any]:
    """Run the currently known calendar plus partial SSE-bars audit on demand."""

    return run_delisted_history_audit(
        runtime_dir=runtime_dir,
        source_index_digests=CURRENT_PARTIAL_SOURCE_INDEX_DIGESTS,
    )


def _normalize_source_index_digests(
    value: Mapping[str, str],
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise DelistedHistoryAuditRunnerBlockedError(
            "source_index_digests must be a dataset-to-SHA-256 mapping"
        )
    normalized: dict[str, str] = {}
    for dataset, digest in value.items():
        if not isinstance(dataset, str) or dataset not in REQUIRED_DATASETS:
            raise DelistedHistoryAuditRunnerBlockedError(
                f"unknown delisted-history dataset: {dataset!r}"
            )
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise DelistedHistoryAuditRunnerBlockedError(
                f"{dataset} source index is not a canonical SHA-256 digest"
            )
        normalized[dataset] = digest
    if not normalized:
        raise DelistedHistoryAuditRunnerBlockedError(
            "source_index_digests must contain at least one verified dataset"
        )
    return dict(sorted(normalized.items()))


def _cold_replay_current_master(
    *,
    release: Mapping[str, Any],
    security_master_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(release, Mapping):
        raise DelistedHistoryAuditRunnerBlockedError(
            "current historical security master release is not an object"
        )
    snapshot_id = str(release.get("snapshot_id") or "")
    manifest_hash = str(release.get("manifest_hash") or "")
    manifest = release.get("manifest")
    quality_report = release.get("quality_report")
    if (
        re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None
        or manifest_hash != snapshot_id
        or not isinstance(manifest, Mapping)
        or not isinstance(quality_report, Mapping)
    ):
        raise DelistedHistoryAuditRunnerBlockedError(
            "current historical security master identity is invalid"
        )
    master_gate = quality_report.get("gate")
    if not isinstance(master_gate, Mapping) or (
        master_gate.get("ready") is not True
        or master_gate.get("status") != READY
        or master_gate.get("promotion_blocked") is not False
    ):
        raise DelistedHistoryAuditRunnerBlockedError(
            "current historical security master is not READY"
        )
    try:
        artifact = manifest["artifacts"]["security_master_jsonl"]
        content_hash = str(artifact["content_hash"])
        row_count = int(artifact["row_count"])
        object_path = Path(str(artifact["object_path"]))
        protocol_version = str(manifest["protocol_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DelistedHistoryAuditRunnerBlockedError(
            "current historical security master artifact identity is invalid"
        ) from exc
    expected_object_path = (
        security_master_root / "objects" / content_hash[:2] / content_hash
    )
    if (
        re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
        or object_path.resolve() != expected_object_path.resolve()
    ):
        raise DelistedHistoryAuditRunnerBlockedError(
            "current historical security master object escapes its fixed store"
        )
    try:
        lines = object_path.read_bytes().splitlines()
        records = [json.loads(line.decode("utf-8")) for line in lines]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DelistedHistoryAuditRunnerBlockedError(
            "current historical security master JSONL cannot be decoded"
        ) from exc
    if len(records) != row_count or any(not isinstance(row, dict) for row in records):
        raise DelistedHistoryAuditRunnerBlockedError(
            "current historical security master row count or schema is invalid"
        )
    identity = {
        "snapshot_id": snapshot_id,
        "manifest_hash": manifest_hash,
        "manifest_path": str(
            (
                security_master_root / "manifests" / f"{snapshot_id}.json"
            ).resolve()
        ),
        "protocol_version": protocol_version,
    }
    verified_records, verified_identity = _verify_master(records, identity)
    return verified_records, verified_identity


def _preflight_source_indexes(
    *,
    digests: Mapping[str, str],
    input_cas_root: Path,
    master_snapshot_id: str,
    targets: Any,
) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, str]] = {}
    for dataset, digest in sorted(digests.items()):
        object_path = input_cas_root / "sha256" / digest[:2] / digest
        identity = {
            "content_hash": digest,
            "object_path": str(object_path.resolve()),
        }
        try:
            loaded = _load_dataset(
                dataset,
                identity,
                input_cas_root,
                authoritative_master_snapshot_id=master_snapshot_id,
                authoritative_targets=targets,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            _SourceEvidenceError,
        ) as exc:
            raise DelistedHistoryAuditRunnerBlockedError(
                f"{dataset} source index failed cold replay: {exc}"
            ) from exc
        if (
            loaded.name != dataset
            or loaded.index_hash != digest
            or Path(loaded.index_object_path).resolve() != object_path.resolve()
        ):
            raise DelistedHistoryAuditRunnerBlockedError(
                f"{dataset} source index replay identity mismatch"
            )
        identities[dataset] = identity
    return identities
