from __future__ import annotations

import ast
import hashlib
import hmac
import io
import json
import os
import re
import stat
import sys
from datetime import datetime
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd

from . import delisted_history_quality as delisted_module
from . import early_winner_research as early_winner_module
from . import early_winner_v4_research as v4_module
from . import early_winner_v5_research as v5_module
from . import early_winner_v6_research as v6_module
from . import historical_security_master as master_module
from .config import PlatformConfig
from .delisted_history_quality import load_verified_delisted_history_gate
from .early_winner_research import ResearchDataBlockedError
from .historical_security_master import load_historical_universe_master_gate
from .storage import Database
from .strategies import early_winner as early_winner_strategy_module
from .strategies import early_winner_v7 as v7_wrapper_module
from .strategies.early_winner_v7 import EarlyWinnerV7Strategy


PROJECT_ID = "early_winner_v7"
STRATEGY_ID = "early_winner_event_quiet_v7"
PROJECT_VERSION = "7.0.0-preregistered"
PROTOCOL_VERSION = "early-winner-v7-pit-sealed-event-v1"
DESIGN_YEARS = tuple(range(2018, 2024))
FROZEN_VALIDATION_YEARS = (2024, 2025)
OBSERVATION_YEARS = (2026,)
V6_RETIRED_STATUS = "PROTOCOL_CHANGED_REQUIRES_V7"

MINIMUM_PHASE_PERIODS = v6_module.MINIMUM_PHASE_PERIODS
MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR = (
    v6_module.MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR
)
MINIMUM_PHASE_INVESTED_PERIODS_COMBINED = (
    v6_module.MINIMUM_PHASE_INVESTED_PERIODS_COMBINED
)
MAXIMUM_DRAWDOWN_GAP = v6_module.MAXIMUM_DRAWDOWN_GAP
MANIFEST_VERSION = "early-winner-v7-frozen-manifest-v1"
RESULT_SCHEMA_VERSION = "early-winner-v7-frozen-result-v1"
LEDGER_SCHEMA_VERSION = "early-winner-v7-open-ledger-v1"
EVIDENCE_SCHEMA_VERSION = "early-winner-v7-row-cycle-evidence-v1"

EVENT_RAW_REPLAY_SCHEMA_VERSION = v6_module.EVENT_RAW_REPLAY_SCHEMA_VERSION
EVENT_EFFECTIVE_RULE_VERSION = v6_module.EVENT_EFFECTIVE_RULE_VERSION
EVENT_REPLAY_SCHEMA_VERSION = v6_module.EVENT_REPLAY_SCHEMA_VERSION
CLASSIFIER_RULE_HASH = v6_module.CLASSIFIER_RULE_HASH
RETURN_COLUMN = v6_module.RETURN_COLUMN
NON_OVERLAP_PHASES = v6_module.NON_OVERLAP_PHASES

# These values are sealed only after the complete V7 source is in place.  The
# normalized self hash ignores only LOCKED_V7_CRITICAL_AST_HASH itself.
LOCKED_V4_IMPLEMENTATION_HASH = (
    "6f5dbf3704d03e8a7e52ea834d52090854c8171901d528418a6526be4cdbe03f"
)
LOCKED_EARLY_WINNER_RESEARCH_HASH = (
    "b875ecfd54dfa5742d562841d6bdf0c2976b18322c5b914879a677d42925148d"
)
LOCKED_EARLY_WINNER_STRATEGY_HASH = (
    "2e038c8570aaf6be7ac81a4f2477c504c006561e27e7a10fb0e6f81bcc308447"
)
LOCKED_V5_RESEARCH_HASH = (
    "7e43dfcf17c2011cb3bed202d78d7ea91bd83bf4744e30b5e0d14ee05d0eb5db"
)
LOCKED_V6_RESEARCH_HASH = (
    "4d8ab68de348197a960228725c6ae488c1b7f501952555502b2d225522790fd8"
)
LOCKED_HISTORICAL_SECURITY_MASTER_HASH = (
    "92e224c61dd9fff5146b24c7fe23ba64c07d2caf94a73a29f93528802bd90d05"
)
LOCKED_DELISTED_HISTORY_QUALITY_HASH = (
    "49b58d18fa9169b771754260fa7dc1578ce7b2e2c5c149ea9defef5aa15a847f"
)
LOCKED_V7_WRAPPER_HASH = (
    "8400aace901d4b7f236d366bdcbade299440394a7659adf396180a35e4952658"
)
LOCKED_V7_CRITICAL_AST_HASH = (
    "d0ca55bfd1d4d8d62d5209361a61cf173d74194c5519999595ed7c5843df31ca"
)
V7_CRITICAL_AST_BUNDLE_VERSION = "early-winner-v7-normalized-module-ast-v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_hash(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_PATTERN.fullmatch(text):
        raise ResearchDataBlockedError(f"{field} must be a lowercase SHA-256")
    return text


LABEL_SCHEMA_SPEC: dict[str, Any] = {
    "version": "early-winner-v7-v4-label-input-v1",
    "base_v6_label_schema_hash": v6_module.LABEL_SCHEMA_HASH,
    "base_v4_label_schema": dict(v6_module.LABEL_SCHEMA_SPEC),
    "point_in_time_quality_bindings": [
        "historical_universe_master_manifest_hash",
        "delisted_history_manifest_hash",
        "delisted_history_report_hash",
    ],
    "quality_gate_policy": {
        "historical_security_master": "LOAD_AND_VERIFY_CURRENT_RELEASE",
        "delisted_history": "FULL_CAS_REPLAY_BOUND_TO_CURRENT_MASTER",
        "caller_ready_summaries": "IGNORED",
    },
}
LABEL_SCHEMA_VERSION = str(LABEL_SCHEMA_SPEC["version"])
LABEL_SCHEMA_HASH = _hash_payload(LABEL_SCHEMA_SPEC)

EVALUATOR_COMPONENT_HASHES = {
    "early_winner_v4_research.py": LOCKED_V4_IMPLEMENTATION_HASH,
    "early_winner_research.py": LOCKED_EARLY_WINNER_RESEARCH_HASH,
    "strategies/early_winner.py": LOCKED_EARLY_WINNER_STRATEGY_HASH,
    "early_winner_v5_research.py": LOCKED_V5_RESEARCH_HASH,
    "early_winner_v6_research.py": LOCKED_V6_RESEARCH_HASH,
    "historical_security_master.py": LOCKED_HISTORICAL_SECURITY_MASTER_HASH,
    "delisted_history_quality.py": LOCKED_DELISTED_HISTORY_QUALITY_HASH,
    "strategies/early_winner_v7.py": LOCKED_V7_WRAPPER_HASH,
    V7_CRITICAL_AST_BUNDLE_VERSION: LOCKED_V7_CRITICAL_AST_HASH,
}
EVALUATOR_BUNDLE_HASH = _hash_payload(EVALUATOR_COMPONENT_HASHES)
LOCKED_DEPENDENCY_VERSIONS = {
    "python": "3.14.0",
    "numpy": "2.4.6",
    "pandas": "3.0.5",
    "scipy": "1.18.0",
    "scikit-learn": "1.9.0",
    "pyarrow": "23.0.1",
}
DEPENDENCY_LOCK_HASH = _hash_payload(LOCKED_DEPENDENCY_VERSIONS)

PROTOCOL_SPEC: dict[str, Any] = {
    "protocol_version": PROTOCOL_VERSION,
    "lifecycle": "RESEARCH_ONLY",
    "design_years": list(DESIGN_YEARS),
    "frozen_validation_years": list(FROZEN_VALIDATION_YEARS),
    "observation_years": list(OBSERVATION_YEARS),
    "v6_disposition": V6_RETIRED_STATUS,
    "candidate_rule": dict(v6_module.PROTOCOL_SPEC["candidate_rule"]),
    "point_in_time_gates": {
        "historical_master": "CURRENT_VERIFIED_STORE_READY_THROUGH_2025",
        "delisted_history": "CURRENT_MASTER_BOUND_FULL_REPLAY_READY",
        "source": "AUTHORITATIVE_LOCAL_ARTIFACTS_NOT_CALLER_SUMMARY",
    },
    "frozen_open": {
        "manifest_version": MANIFEST_VERSION,
        "formats": ["parquet"],
        "path_policy": "CONFIG_RUNTIME_RESEARCH_EARLY_WINNER_V7_NO_REPARSE",
        "database_state_machine": [
            "SEALED",
            "CONSUMING",
            "RESULT_COMMITTED",
            "FAILED_CLOSED",
        ],
        "one_open_only": True,
        "claim_before_first_manifest_read": True,
    },
    "event_provenance": dict(v6_module.PROTOCOL_SPEC["event_provenance"]),
    "dependency_lock": {
        "evaluator_component_hashes": EVALUATOR_COMPONENT_HASHES,
        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "dependency_versions": LOCKED_DEPENDENCY_VERSIONS,
        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
    },
    "assessment": {
        **dict(v6_module.PROTOCOL_SPEC["assessment"]),
        "result_schema": RESULT_SCHEMA_VERSION,
        "row_cycle_evidence_schema": EVIDENCE_SCHEMA_VERSION,
        "any_change_requires": "V8",
    },
}
PROTOCOL_HASH = _hash_payload(PROTOCOL_SPEC)


class V7ProtocolChangeRequiresV8(ResearchDataBlockedError):
    pass


class FrozenManifestError(ResearchDataBlockedError):
    pass


class FrozenValidationAlreadyOpened(ResearchDataBlockedError):
    pass


class FrozenValidationAuditError(ResearchDataBlockedError):
    pass


def current_v7_critical_ast_hash(path: Path | None = None) -> str:
    source_path = Path(path or __file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    normalized = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name)
            and target.id == "LOCKED_V7_CRITICAL_AST_HASH"
            for target in node.targets
        ):
            node.value = ast.Constant(value="<SELF_HASH_NORMALIZED>")
            normalized = True
            break
    if not normalized:
        raise V7ProtocolChangeRequiresV8(
            "V7 normalized module AST cannot locate its self-hash assignment"
        )
    return _hash_payload(
        {
            "version": V7_CRITICAL_AST_BUNDLE_VERSION,
            "normalized_module_ast": ast.dump(
                tree, annotate_fields=True, include_attributes=False
            ),
        }
    )


def _actual_component_hashes() -> dict[str, str]:
    return {
        "early_winner_v4_research.py": _hash_bytes(
            Path(str(v4_module.__file__)).resolve().read_bytes()
        ),
        "early_winner_research.py": _hash_bytes(
            Path(str(early_winner_module.__file__)).resolve().read_bytes()
        ),
        "strategies/early_winner.py": _hash_bytes(
            Path(str(early_winner_strategy_module.__file__)).resolve().read_bytes()
        ),
        "early_winner_v5_research.py": _hash_bytes(
            Path(str(v5_module.__file__)).resolve().read_bytes()
        ),
        "early_winner_v6_research.py": _hash_bytes(
            Path(str(v6_module.__file__)).resolve().read_bytes()
        ),
        "historical_security_master.py": _hash_bytes(
            Path(str(master_module.__file__)).resolve().read_bytes()
        ),
        "delisted_history_quality.py": _hash_bytes(
            Path(str(delisted_module.__file__)).resolve().read_bytes()
        ),
        "strategies/early_winner_v7.py": _hash_bytes(
            Path(str(v7_wrapper_module.__file__)).resolve().read_bytes()
        ),
        V7_CRITICAL_AST_BUNDLE_VERSION: current_v7_critical_ast_hash(),
    }


def assert_locked_dependencies() -> None:
    actual = _actual_component_hashes()
    if any(
        not hmac.compare_digest(actual[name], expected)
        for name, expected in EVALUATOR_COMPONENT_HASHES.items()
    ):
        raise V7ProtocolChangeRequiresV8(
            "V7 evaluator/data-audit source bundle changed after preregistration; create V8"
        )
    actual_versions = {
        "python": ".".join(str(item) for item in sys.version_info[:3]),
        "numpy": distribution_version("numpy"),
        "pandas": distribution_version("pandas"),
        "scipy": distribution_version("scipy"),
        "scikit-learn": distribution_version("scikit-learn"),
        "pyarrow": distribution_version("pyarrow"),
    }
    if actual_versions != LOCKED_DEPENDENCY_VERSIONS:
        raise V7ProtocolChangeRequiresV8(
            "V7 evaluator dependency versions changed after preregistration; create V8"
        )


def frame_schema_hash(frame: pd.DataFrame) -> str:
    return v6_module.frame_schema_hash(frame)


def _logical_frame_hash(frame: pd.DataFrame) -> str:
    return v6_module._logical_frame_hash(frame)


def _sorted_row_key_hash(frame: pd.DataFrame) -> str:
    return v6_module._sorted_row_key_hash(frame)


def _manifest_payload_hash(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_payload_hash", None)
    return _hash_payload(payload)


def _gate_dict(gates: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = gates.get(name)
    return dict(value) if isinstance(value, Mapping) else {}


def _load_authoritative_quality_gates(config: PlatformConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_master = load_historical_universe_master_gate(config.runtime_dir)
    master = v5_module.historical_universe_master_gate(raw_master, through_year=2025)
    delisted = load_verified_delisted_history_gate(
        output_root=(
            config.runtime_dir
            / "research"
            / v4_module.PROJECT_ID
            / "delisted_history_quality"
        ),
        input_cas_root=(
            config.runtime_dir
            / "research"
            / v4_module.PROJECT_ID
            / "delisted_history_inputs"
        ),
        security_master_root=config.runtime_dir / "security_master",
        expected_master_gate=master,
    )
    return master, delisted


def frozen_validation_readiness(
    config: PlatformConfig,
    gates: Mapping[str, Any],
    *,
    protocol_hash: str = PROTOCOL_HASH,
) -> dict[str, Any]:
    """Recompute V7 readiness from current verified stores and caller evidence.

    A caller-provided master or delisted ``ready`` value is deliberately ignored.
    """

    if protocol_hash != PROTOCOL_HASH:
        return {
            "ready": False,
            "status": "V8_REQUIRED",
            "detail": "V7 protocol changed after preregistration; create V8",
            "failures": ["protocol_hash changed"],
        }
    try:
        assert_locked_dependencies()
    except V7ProtocolChangeRequiresV8 as exc:
        return {
            "ready": False,
            "status": "V8_REQUIRED",
            "detail": str(exc),
            "failures": [str(exc)],
        }

    failures: list[str] = []
    try:
        master, delisted = _load_authoritative_quality_gates(config)
    except Exception as exc:
        master = {"ready": False, "status": "ARTIFACT_INVALID", "detail": str(exc)}
        delisted = {"ready": False, "status": "ARTIFACT_INVALID", "detail": str(exc)}
    if master.get("ready") is not True or master.get("promotion_blocked") is True:
        failures.append("current historical_universe_master is not READY through 2025")
    if (
        delisted.get("ready") is not True
        or delisted.get("status") != delisted_module.READY
        or delisted.get("promotion_blocked") is not False
    ):
        failures.append("current delisted_history_quality full replay is not READY")
    if str(delisted.get("historical_security_master_snapshot") or "") != str(
        master.get("snapshot_id") or ""
    ):
        failures.append("delisted_history_quality is not bound to the current master")

    required = {
        "preregistration",
        "event_provenance",
        "trading_calendar",
        "execution_status",
        "label_snapshot",
        "frozen_snapshot",
    }
    failures.extend(f"missing gate: {name}" for name in sorted(required - set(gates)))
    preregistration = _gate_dict(gates, "preregistration")
    expected_preregistration = {
        "ready": True,
        "protocol_version": PROTOCOL_VERSION,
        "protocol_hash": PROTOCOL_HASH,
        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
    }
    for field, expected in expected_preregistration.items():
        if preregistration.get(field) != expected:
            failures.append(f"preregistration.{field} is absent or changed")

    event = _gate_dict(gates, "event_provenance")
    calendar = _gate_dict(gates, "trading_calendar")
    execution = _gate_dict(gates, "execution_status")
    labels = _gate_dict(gates, "label_snapshot")
    frozen = _gate_dict(gates, "frozen_snapshot")
    for name, gate in (
        ("event_provenance", event),
        ("trading_calendar", calendar),
        ("execution_status", execution),
        ("label_snapshot", labels),
        ("frozen_snapshot", frozen),
    ):
        if gate.get("ready") is not True:
            failures.append(f"{name} is not ready")

    hashes: dict[str, str] = {}
    for name, gate, field in (
        ("master", master, "manifest_hash"),
        ("delisted_manifest", delisted, "manifest_hash"),
        ("delisted_report", delisted, "report_hash"),
        ("event", event, "snapshot_hash"),
        ("event_raw", event, "raw_content_manifest_hash"),
        ("calendar", calendar, "content_hash"),
        ("execution", execution, "content_hash"),
        ("labels", labels, "snapshot_hash"),
        ("manifest", frozen, "manifest_hash"),
    ):
        try:
            hashes[name] = _require_hash(gate.get(field), f"{name}.{field}")
        except ResearchDataBlockedError as exc:
            failures.append(str(exc))

    expected_event = {
        "schema_version": EVENT_RAW_REPLAY_SCHEMA_VERSION,
        "legacy_selection_schema_version": EVENT_REPLAY_SCHEMA_VERSION,
        "classifier_rule_hash": CLASSIFIER_RULE_HASH,
        "source": "CNINFO_OFFICIAL",
        "content_hash_algorithm": "SHA256_RAW_BYTES",
        "raw_content_rehash_passed": True,
        "announcement_security_binding_passed": True,
        "effective_at_calendar_derived_passed": True,
        "effective_rule_version": EVENT_EFFECTIVE_RULE_VERSION,
    }
    for field, expected in expected_event.items():
        if event.get(field) != expected:
            failures.append(f"event_provenance.{field} is absent or changed")
    if hashes.get("calendar") and event.get("trading_calendar_hash") != hashes["calendar"]:
        failures.append("event_provenance is not bound to the trading calendar")
    if labels.get("return_column") != RETURN_COLUMN:
        failures.append(f"label_snapshot must use {RETURN_COLUMN}")
    if labels.get("label_schema_hash") != LABEL_SCHEMA_HASH:
        failures.append("label_snapshot is not bound to the V7 label schema")
    if labels.get("evaluator_bundle_hash") != EVALUATOR_BUNDLE_HASH:
        failures.append("label_snapshot is not bound to the V7 evaluator bundle")

    try:
        frozen_years = tuple(int(year) for year in frozen.get("years", ()))
    except (TypeError, ValueError):
        frozen_years = ()
    if frozen_years != FROZEN_VALIDATION_YEARS:
        failures.append("frozen_snapshot must contain exactly 2024 and 2025")
    if frozen.get("sealed") is not True:
        failures.append("frozen_snapshot is not sealed")
    if frozen.get("protocol_hash") != PROTOCOL_HASH:
        failures.append("frozen_snapshot protocol hash is absent or changed")
    if not str(frozen.get("snapshot_id") or "").strip():
        failures.append("frozen_snapshot.snapshot_id is missing")
    if not str(frozen.get("manifest_path") or "").strip():
        failures.append("frozen_snapshot.manifest_path is missing")

    components = {
        "historical_universe_master_manifest_hash": hashes.get("master", ""),
        "delisted_history_manifest_hash": hashes.get("delisted_manifest", ""),
        "delisted_history_report_hash": hashes.get("delisted_report", ""),
        "event_provenance_snapshot_hash": hashes.get("event", ""),
        "event_raw_content_manifest_hash": hashes.get("event_raw", ""),
        "trading_calendar_content_hash": hashes.get("calendar", ""),
        "execution_status_content_hash": hashes.get("execution", ""),
        "label_snapshot_hash": hashes.get("labels", ""),
        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
    }
    return {
        "ready": not failures,
        "status": "READY_TO_SEAL" if not failures else "BLOCKED_DATA",
        "detail": (
            "All V7 immutable gates are ready for one database-sealed open"
            if not failures
            else "; ".join(failures)
        ),
        "failures": failures,
        "protocol_hash": PROTOCOL_HASH,
        "snapshot_id": str(frozen.get("snapshot_id") or ""),
        "manifest_path": str(frozen.get("manifest_path") or ""),
        "manifest_hash": hashes.get("manifest", ""),
        "component_hashes": components,
        "historical_universe_master": master,
        "delisted_history_quality": delisted,
    }


def v7_frozen_root(config: PlatformConfig) -> Path:
    return Path(config.runtime_dir).absolute() / "research" / PROJECT_ID / "frozen"


def v7_results_root(config: PlatformConfig) -> Path:
    return Path(config.runtime_dir).absolute() / "research" / PROJECT_ID / "results"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(stat.S_ISLNK(metadata.st_mode)) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _assert_path_chain_has_no_links(root: Path, target: Path) -> None:
    root = root.absolute()
    target = target.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise FrozenManifestError(f"path is outside fixed V7 root: {target}") from exc
    paths = (
        root,
        *(
            root / Path(*relative.parts[:index])
            for index in range(1, len(relative.parts) + 1)
        ),
    )
    for path in paths:
        if path.exists() and _is_link_or_reparse(path):
            raise FrozenManifestError(f"V7 path uses a symlink/reparse point: {path}")


def _fixed_root_file(config: PlatformConfig, candidate: Path, *, kind: str) -> Path:
    runtime = Path(config.runtime_dir).absolute()
    root = v7_frozen_root(config) if kind == "frozen" else v7_results_root(config)
    candidate = Path(candidate).absolute()
    _assert_path_chain_has_no_links(runtime, root)
    _assert_path_chain_has_no_links(root, candidate)
    resolved_runtime = runtime.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if (
        not resolved_root.is_relative_to(resolved_runtime)
        or not resolved.is_relative_to(resolved_root)
        or not resolved.is_file()
    ):
        raise FrozenManifestError(f"V7 {kind} file is outside its fixed root")
    return candidate


def _read_file_once(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FrozenManifestError(f"cannot securely open V7 artifact {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FrozenManifestError(f"V7 artifact is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS early_winner_v7_frozen_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    evaluator_bundle_hash TEXT NOT NULL,
    label_schema_hash TEXT NOT NULL,
    dependency_lock_hash TEXT NOT NULL,
    master_manifest_hash TEXT NOT NULL,
    delisted_manifest_hash TEXT NOT NULL,
    delisted_report_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'SEALED', 'CONSUMING', 'RESULT_COMMITTED', 'FAILED_CLOSED'
    )),
    open_nonce_hash TEXT,
    audit_id TEXT,
    runner_id TEXT,
    sealed_at TEXT NOT NULL,
    opened_at TEXT,
    finished_at TEXT,
    result_path TEXT,
    result_byte_size INTEGER,
    artifact_hash TEXT,
    failure_detail_hash TEXT,
    lock_version INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, protocol_hash)
)
"""


class V7FrozenValidationLedger:
    def __init__(self, database: Database) -> None:
        self.database = database
        with self.database.connect() as connection:
            connection.execute(_LEDGER_SQL)

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat()

    def seal(self, readiness: Mapping[str, Any]) -> dict[str, Any]:
        if readiness.get("ready") is not True:
            raise FrozenManifestError(
                str(readiness.get("detail") or "V7 gates are not ready")
            )
        components = dict(readiness.get("component_hashes") or {})
        manifest_file = _fixed_root_file(
            self.database.config,
            Path(str(readiness["manifest_path"])),
            kind="frozen",
        )
        manifest_path = str(manifest_file.absolute())
        immutable = {
            "snapshot_id": str(readiness["snapshot_id"]),
            "manifest_path": manifest_path,
            "manifest_hash": _require_hash(
                readiness.get("manifest_hash"), "readiness.manifest_hash"
            ),
            "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
            "label_schema_hash": LABEL_SCHEMA_HASH,
            "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
            "master_manifest_hash": _require_hash(
                components.get("historical_universe_master_manifest_hash"),
                "components.historical_universe_master_manifest_hash",
            ),
            "delisted_manifest_hash": _require_hash(
                components.get("delisted_history_manifest_hash"),
                "components.delisted_history_manifest_hash",
            ),
            "delisted_report_hash": _require_hash(
                components.get("delisted_history_report_hash"),
                "components.delisted_history_report_hash",
            ),
        }
        run_id = f"ewv7_{uuid4().hex}"
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM early_winner_v7_frozen_runs "
                "WHERE project_id=? AND protocol_hash=?",
                (PROJECT_ID, PROTOCOL_HASH),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO early_winner_v7_frozen_runs
                    (run_id, project_id, protocol_version, protocol_hash, snapshot_id,
                     manifest_path, manifest_hash, evaluator_bundle_hash,
                     label_schema_hash, dependency_lock_hash, master_manifest_hash,
                     delisted_manifest_hash, delisted_report_hash, state, sealed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SEALED', ?)""",
                    (
                        run_id,
                        PROJECT_ID,
                        PROTOCOL_VERSION,
                        PROTOCOL_HASH,
                        immutable["snapshot_id"],
                        immutable["manifest_path"],
                        immutable["manifest_hash"],
                        immutable["evaluator_bundle_hash"],
                        immutable["label_schema_hash"],
                        immutable["dependency_lock_hash"],
                        immutable["master_manifest_hash"],
                        immutable["delisted_manifest_hash"],
                        immutable["delisted_report_hash"],
                        self._now(),
                    ),
                )
            elif any(str(existing[field]) != value for field, value in immutable.items()):
                raise FrozenValidationAlreadyOpened(
                    "V7 sealed registration already exists with different immutable evidence"
                )
        return self.get()

    def get(self) -> dict[str, Any]:
        rows = self.database.query(
            "SELECT * FROM early_winner_v7_frozen_runs "
            "WHERE project_id=? AND protocol_hash=?",
            (PROJECT_ID, PROTOCOL_HASH),
        )
        if not rows:
            raise FrozenValidationAuditError("V7 frozen run has not been database-sealed")
        return rows[0]

    def claim_once(self, *, runner_id: str) -> dict[str, str]:
        nonce = uuid4().hex + uuid4().hex
        nonce_hash = _hash_bytes(nonce.encode("utf-8"))
        audit_id = f"ewv7_audit_{uuid4().hex}"
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM early_winner_v7_frozen_runs "
                "WHERE project_id=? AND protocol_hash=?",
                (PROJECT_ID, PROTOCOL_HASH),
            ).fetchone()
            if row is None:
                raise FrozenValidationAuditError("V7 frozen run is not sealed")
            updated = connection.execute(
                """UPDATE early_winner_v7_frozen_runs
                SET state='CONSUMING', open_nonce_hash=?, audit_id=?, runner_id=?,
                    opened_at=?, lock_version=lock_version+1
                WHERE run_id=? AND state='SEALED' AND open_nonce_hash IS NULL""",
                (
                    nonce_hash,
                    audit_id,
                    str(runner_id),
                    self._now(),
                    str(row["run_id"]),
                ),
            )
            if updated.rowcount != 1:
                raise FrozenValidationAlreadyOpened(
                    f"V7 frozen evidence was already opened; current state={row['state']}"
                )
            run_id = str(row["run_id"])
        return {"run_id": run_id, "audit_id": audit_id, "open_nonce": nonce}

    def _transition(
        self,
        *,
        audit_id: str,
        open_nonce: str,
        state: str,
        result_path: str | None = None,
        result_byte_size: int | None = None,
        artifact_hash: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        nonce_hash = _hash_bytes(open_nonce.encode("utf-8"))
        failure_hash = (
            _hash_bytes(str(failure_detail).encode("utf-8")) if failure_detail else None
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE early_winner_v7_frozen_runs
                SET state=?, result_path=?, result_byte_size=?, artifact_hash=?,
                    failure_detail_hash=?, finished_at=?, lock_version=lock_version+1
                WHERE project_id=? AND protocol_hash=? AND state='CONSUMING'
                  AND audit_id=? AND open_nonce_hash=?""",
                (
                    state,
                    result_path,
                    result_byte_size,
                    artifact_hash,
                    failure_hash,
                    self._now(),
                    PROJECT_ID,
                    PROTOCOL_HASH,
                    audit_id,
                    nonce_hash,
                ),
            )
            if updated.rowcount != 1:
                raise FrozenValidationAuditError(
                    "V7 frozen state transition lost its one-time CAS"
                )

    def commit_result(
        self,
        *,
        audit_id: str,
        open_nonce: str,
        result_path: Path,
        result_byte_size: int,
        artifact_hash: str,
    ) -> None:
        artifact_hash = _require_hash(artifact_hash, "artifact_hash")
        path = _fixed_root_file(self.database.config, result_path, kind="results")
        raw = _read_file_once(path)
        if len(raw) != int(result_byte_size) or not hmac.compare_digest(
            _hash_bytes(raw), artifact_hash
        ):
            raise FrozenValidationAuditError(
                "V7 result artifact changed before database commit"
            )
        self._transition(
            audit_id=audit_id,
            open_nonce=open_nonce,
            state="RESULT_COMMITTED",
            result_path=str(path.absolute()),
            result_byte_size=len(raw),
            artifact_hash=artifact_hash,
        )

    def fail_closed(self, *, audit_id: str, open_nonce: str, detail: str) -> None:
        self._transition(
            audit_id=audit_id,
            open_nonce=open_nonce,
            state="FAILED_CLOSED",
            failure_detail=detail,
        )

    def assert_committed(self) -> dict[str, Any]:
        row = self.get()
        expected = {
            "state": "RESULT_COMMITTED",
            "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
            "label_schema_hash": LABEL_SCHEMA_HASH,
            "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
        }
        if any(str(row.get(field) or "") != value for field, value in expected.items()):
            raise FrozenValidationAuditError(
                "V7 result is not bound to the committed database audit"
            )
        _require_hash(row.get("artifact_hash"), "ledger.artifact_hash")
        if int(row.get("result_byte_size") or 0) <= 0 or not str(
            row.get("result_path") or ""
        ):
            raise FrozenValidationAuditError(
                "V7 ledger result artifact reference is incomplete"
            )
        return row


def _json_no_duplicate_keys(raw: bytes, field: str) -> dict[str, Any]:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FrozenManifestError(f"{field} contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenManifestError(f"{field} is not UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FrozenManifestError(f"{field} must be a JSON object")
    return value


def _validate_manifest_payload(
    manifest: Mapping[str, Any], *, readiness: Mapping[str, Any]
) -> list[dict[str, Any]]:
    expected_top = {
        "manifest_version": MANIFEST_VERSION,
        "project_id": PROJECT_ID,
        "protocol_version": PROTOCOL_VERSION,
        "protocol_hash": PROTOCOL_HASH,
        "snapshot_id": str(readiness["snapshot_id"]),
        "sealed": True,
        "frozen_years": list(FROZEN_VALIDATION_YEARS),
        "timezone": "Asia/Shanghai",
        "decision_boundary": "WEEK_LAST_TRADING_SESSION_CLOSE",
        "row_grain": ["asof", "code"],
        "return_column": RETURN_COLUMN,
        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
    }
    for field, expected in expected_top.items():
        if manifest.get(field) != expected:
            raise FrozenManifestError(f"manifest.{field} is absent or changed")
    payload_hash = _require_hash(
        manifest.get("manifest_payload_hash"), "manifest.manifest_payload_hash"
    )
    if not hmac.compare_digest(payload_hash, _manifest_payload_hash(manifest)):
        raise FrozenManifestError("manifest canonical payload hash does not reproduce")
    components = manifest.get("components")
    if not isinstance(components, Mapping) or dict(components) != dict(
        readiness.get("component_hashes") or {}
    ):
        raise FrozenManifestError("manifest components are not bound to the ready gates")

    raw_shards = manifest.get("shards")
    if not isinstance(raw_shards, list) or len(raw_shards) != 2:
        raise FrozenManifestError("manifest must contain exactly two frozen shards")
    required = {
        "year",
        "relative_path",
        "format",
        "byte_size",
        "content_hash",
        "schema_hash",
        "logical_content_hash",
        "row_count",
        "decision_date_count",
        "code_count",
        "min_asof",
        "max_asof",
        "sorted_row_key_hash",
        "duplicate_grain_count",
    }
    shards: list[dict[str, Any]] = []
    for raw in raw_shards:
        if not isinstance(raw, Mapping):
            raise FrozenManifestError("manifest shard must be an object")
        shard = dict(raw)
        missing = sorted(required - set(shard))
        if missing:
            raise FrozenManifestError(
                "manifest shard missing fields: " + ",".join(missing)
            )
        if shard.get("format") != "parquet":
            raise FrozenManifestError("V7 accepts only parquet frozen shards")
        for field in (
            "content_hash",
            "schema_hash",
            "logical_content_hash",
            "sorted_row_key_hash",
        ):
            _require_hash(shard.get(field), f"manifest.shard.{field}")
        try:
            year = int(shard["year"])
            byte_size = int(shard["byte_size"])
            row_count = int(shard["row_count"])
            duplicate_count = int(shard["duplicate_grain_count"])
        except (TypeError, ValueError) as exc:
            raise FrozenManifestError("manifest shard numeric field is invalid") from exc
        if year not in FROZEN_VALIDATION_YEARS:
            raise FrozenManifestError(f"manifest shard has forbidden year: {year}")
        if byte_size <= 0 or row_count <= 0 or duplicate_count != 0:
            raise FrozenManifestError("manifest shard size/rows/duplicates are invalid")
        relative = Path(str(shard["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise FrozenManifestError("manifest shard path is not a safe relative path")
        shards.append(shard)
    years = tuple(sorted(int(item["year"]) for item in shards))
    if years != FROZEN_VALIDATION_YEARS or len(set(years)) != 2:
        raise FrozenManifestError("manifest frozen shard years are not unique 2024/2025")
    if int(manifest.get("total_rows", -1)) != sum(
        int(item["row_count"]) for item in shards
    ):
        raise FrozenManifestError("manifest total_rows does not match shard rows")
    schema_hash = _require_hash(manifest.get("schema_hash"), "manifest.schema_hash")
    if any(str(item["schema_hash"]) != schema_hash for item in shards):
        raise FrozenManifestError("frozen shard schemas are not identical")
    return sorted(shards, key=lambda item: int(item["year"]))


def _read_bound_manifest_and_shards(
    ledger_row: Mapping[str, Any],
    *,
    readiness: Mapping[str, Any],
    config: PlatformConfig,
) -> tuple[dict[str, Any], dict[int, pd.DataFrame], list[dict[str, Any]]]:
    manifest_path = _fixed_root_file(
        config, Path(str(ledger_row["manifest_path"])), kind="frozen"
    )
    raw_manifest = _read_file_once(manifest_path)
    if not hmac.compare_digest(
        _hash_bytes(raw_manifest), str(ledger_row["manifest_hash"])
    ):
        raise FrozenManifestError("manifest file hash differs from the database seal")
    manifest = _json_no_duplicate_keys(raw_manifest, "V7 frozen manifest")
    if _canonical_json(manifest).encode("utf-8") != raw_manifest:
        raise FrozenManifestError("V7 frozen manifest is not canonical JSON")
    descriptors = _validate_manifest_payload(manifest, readiness=readiness)
    manifest_directory = manifest_path.resolve(strict=True).parent
    frames: dict[int, pd.DataFrame] = {}
    profiles: list[dict[str, Any]] = []
    for descriptor in descriptors:
        year = int(descriptor["year"])
        path = _fixed_root_file(
            config,
            manifest_directory / str(descriptor["relative_path"]),
            kind="frozen",
        )
        if not path.resolve(strict=True).is_relative_to(manifest_directory):
            raise FrozenManifestError("frozen shard resolves outside manifest directory")
        raw = _read_file_once(path)
        if len(raw) != int(descriptor["byte_size"]):
            raise FrozenManifestError(f"{year} shard byte_size differs from manifest")
        content_hash = _hash_bytes(raw)
        if not hmac.compare_digest(content_hash, str(descriptor["content_hash"])):
            raise FrozenManifestError(f"{year} shard content hash differs from manifest")
        try:
            frame = pd.read_parquet(io.BytesIO(raw))
        except Exception as exc:
            raise FrozenManifestError(f"{year} shard is not readable parquet: {exc}") from exc
        if frame_schema_hash(frame) != str(descriptor["schema_hash"]):
            raise FrozenManifestError(f"{year} shard schema hash differs from manifest")
        if _logical_frame_hash(frame) != str(descriptor["logical_content_hash"]):
            raise FrozenManifestError(f"{year} shard logical hash differs from manifest")
        if len(frame) != int(descriptor["row_count"]):
            raise FrozenManifestError(f"{year} shard row_count differs from manifest")
        if "asof" not in frame or "code" not in frame:
            raise FrozenManifestError(f"{year} shard has no asof/code grain")
        dates = pd.to_datetime(frame["asof"], errors="coerce")
        if dates.isna().any() or set(dates.dt.year.astype(int)) != {year}:
            raise FrozenManifestError(f"{year} shard decision dates are invalid")
        actual = {
            "decision_date_count": int(dates.dt.normalize().nunique()),
            "code_count": int(frame["code"].astype(str).nunique()),
            "min_asof": dates.min().date().isoformat(),
            "max_asof": dates.max().date().isoformat(),
            "sorted_row_key_hash": _sorted_row_key_hash(frame),
            "duplicate_grain_count": int(frame.duplicated(["asof", "code"]).sum()),
        }
        if any(descriptor.get(field) != value for field, value in actual.items()):
            raise FrozenManifestError(f"{year} shard profile differs from manifest")
        frames[year] = frame
        profiles.append(
            {
                "year": year,
                "content_hash": content_hash,
                "logical_content_hash": str(descriptor["logical_content_hash"]),
                "schema_hash": str(descriptor["schema_hash"]),
                "row_count": len(frame),
            }
        )
    return manifest, frames, profiles


_V7_FRAME_BINDINGS = {
    "historical_universe_master_manifest_hash",
    "delisted_history_manifest_hash",
    "delisted_history_report_hash",
    "event_provenance_snapshot_hash",
    "event_raw_content_manifest_hash",
    "trading_calendar_content_hash",
    "execution_status_content_hash",
    "label_snapshot_hash",
}


def prepare_v7_evaluation_frame(
    frame: pd.DataFrame,
    *,
    expected_year: int,
    component_hashes: Mapping[str, Any],
) -> pd.DataFrame:
    missing = sorted(_V7_FRAME_BINDINGS - set(frame.columns))
    if missing:
        raise ResearchDataBlockedError(
            "V7 frame component binding columns missing: " + ",".join(missing)
        )
    for column in sorted(_V7_FRAME_BINDINGS):
        expected = _require_hash(
            component_hashes.get(column), f"component_hashes.{column}"
        )
        if set(frame[column].fillna("").astype(str)) != {expected}:
            raise ResearchDataBlockedError(f"V7 frame rows are not bound to {column}")
    prepared = v6_module.prepare_v6_evaluation_frame(
        frame,
        expected_year=expected_year,
        component_hashes=component_hashes,
    )
    prepared["v7_evaluation_eligible"] = prepared[
        "v6_evaluation_eligible"
    ].astype(bool)
    prepared["v7_candidate_eligible"] = prepared["v6_candidate_eligible"].astype(bool)
    prepared["v7_selection_score"] = prepared["v6_selection_score"]
    prepared["v7_metric_score"] = prepared["v6_metric_score"]
    return prepared


def _bind_v7_metric_identity(metrics: dict[str, Any], *, year: int) -> None:
    metrics.update(
        {
            "year": int(year),
            "protocol_version": PROTOCOL_VERSION,
            "protocol_hash": PROTOCOL_HASH,
            "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
            "label_schema_hash": LABEL_SCHEMA_HASH,
            "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
            "lifecycle": "RESEARCH_ONLY",
        }
    )


def evaluate_v7_frozen_frame(
    frame: pd.DataFrame,
    *,
    expected_year: int,
    component_hashes: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    assert_locked_dependencies()
    prepared = prepare_v7_evaluation_frame(
        frame, expected_year=expected_year, component_hashes=component_hashes
    )
    candidate, baseline = v6_module._evaluate_prepared_v6(
        prepared, expected_year=expected_year
    )
    _bind_v7_metric_identity(candidate, year=expected_year)
    _bind_v7_metric_identity(baseline, year=expected_year)
    return candidate, baseline


def _build_v7_year_evidence(
    prepared: pd.DataFrame,
    *,
    year: int,
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = v6_module._build_year_evidence(
        prepared,
        year=year,
        candidate=candidate,
        baseline=baseline,
    )
    evidence["schema_version"] = EVIDENCE_SCHEMA_VERSION
    evidence["v6_evidence_algorithm_hash"] = _hash_bytes(
        Path(str(v6_module.__file__)).resolve().read_bytes()
    )
    return evidence


def _cycle_ledger_hash(metrics: Mapping[str, Any]) -> str:
    return _hash_payload(
        {
            "schema": "early-winner-v7-cycle-ledger-v1",
            "year": int(metrics.get("year", 0)),
            "method": str(metrics.get("method") or ""),
            "phases": metrics.get("phase_metrics", []),
        }
    )


def _ensure_results_root(config: PlatformConfig) -> Path:
    runtime = Path(config.runtime_dir).absolute()
    runtime.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(runtime):
        raise FrozenValidationAuditError("V7 runtime directory is a link/reparse point")
    current = runtime
    for part in ("research", PROJECT_ID, "results"):
        current = current / part
        if current.exists() and _is_link_or_reparse(current):
            raise FrozenValidationAuditError(
                f"V7 result directory is a link/reparse point: {current}"
            )
        current.mkdir(exist_ok=True)
    return current


def _persist_result_artifact(
    config: PlatformConfig, payload: Mapping[str, Any]
) -> dict[str, Any]:
    raw = _canonical_json(dict(payload)).encode("utf-8")
    artifact_hash = _hash_bytes(raw)
    root = _ensure_results_root(config)
    bucket = root / artifact_hash[:2]
    if bucket.exists() and _is_link_or_reparse(bucket):
        raise FrozenValidationAuditError("V7 result hash bucket is a reparse point")
    bucket.mkdir(exist_ok=True)
    path = bucket / f"{artifact_hash}.json"
    if path.exists():
        path = _fixed_root_file(config, path, kind="results")
        if _read_file_once(path) != raw:
            raise FrozenValidationAuditError(
                "V7 content-addressed result path contains different bytes"
            )
    else:
        temporary = bucket / f".{artifact_hash}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            view = memoryview(raw)
            offset = 0
            while offset < len(view):
                offset += os.write(descriptor, view[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        path = _fixed_root_file(config, path, kind="results")
        if _read_file_once(path) != raw:
            raise FrozenValidationAuditError(
                "V7 result bytes changed during atomic publication"
            )
    return {
        "result_path": str(path.absolute()),
        "result_byte_size": len(raw),
        "artifact_hash": artifact_hash,
    }


def seal_v7_frozen_validation(
    *, database: Database, gates: Mapping[str, Any]
) -> dict[str, Any]:
    readiness = frozen_validation_readiness(database.config, gates)
    if readiness["status"] == "V8_REQUIRED":
        raise V7ProtocolChangeRequiresV8(readiness["detail"])
    return V7FrozenValidationLedger(database).seal(readiness)


def run_v7_frozen_validation_once(
    *,
    database: Database,
    gates: Mapping[str, Any],
    runner_id: str,
) -> dict[str, Any]:
    """Claim once before the first manifest byte is read, then fail closed."""

    readiness = frozen_validation_readiness(database.config, gates)
    if readiness["status"] == "V8_REQUIRED":
        raise V7ProtocolChangeRequiresV8(readiness["detail"])
    if not readiness["ready"]:
        raise FrozenManifestError(readiness["detail"])
    ledger = V7FrozenValidationLedger(database)
    sealed = ledger.seal(readiness)
    claim = ledger.claim_once(runner_id=str(runner_id))
    try:
        manifest, frames, loaded_profiles = _read_bound_manifest_and_shards(
            sealed, readiness=readiness, config=database.config
        )
        yearly: dict[str, Any] = {}
        for year in FROZEN_VALIDATION_YEARS:
            prepared = prepare_v7_evaluation_frame(
                frames[year],
                expected_year=year,
                component_hashes=readiness["component_hashes"],
            )
            candidate, baseline = v6_module._evaluate_prepared_v6(
                prepared, expected_year=year
            )
            for metrics in (candidate, baseline):
                _bind_v7_metric_identity(metrics, year=year)
                metrics.update(
                    {
                        "snapshot_id": readiness["snapshot_id"],
                        "manifest_hash": readiness["manifest_hash"],
                        "audit_id": claim["audit_id"],
                    }
                )
                metrics["cycle_ledger_hash"] = _cycle_ledger_hash(metrics)
            yearly[str(year)] = {
                "reported_metrics": {
                    "candidate": candidate,
                    "baseline": baseline,
                },
                "evidence": _build_v7_year_evidence(
                    prepared,
                    year=year,
                    candidate=candidate,
                    baseline=baseline,
                ),
            }
        result: dict[str, Any] = {
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "project_id": PROJECT_ID,
            "lifecycle": "RESEARCH_ONLY",
            "run_id": claim["run_id"],
            "audit_id": claim["audit_id"],
            "snapshot_id": readiness["snapshot_id"],
            "manifest_hash": readiness["manifest_hash"],
            "manifest_payload_hash": manifest["manifest_payload_hash"],
            "protocol_version": PROTOCOL_VERSION,
            "protocol_hash": PROTOCOL_HASH,
            "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
            "label_schema_hash": LABEL_SCHEMA_HASH,
            "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
            "dependency_versions": LOCKED_DEPENDENCY_VERSIONS,
            "component_hashes": readiness["component_hashes"],
            "loaded_shards": loaded_profiles,
            "yearly": yearly,
            "created_at": datetime.now().astimezone().isoformat(),
            "promotion_allowed": False,
            "trade_signals_enabled": False,
        }
        artifact = _persist_result_artifact(database.config, result)
        ledger.commit_result(
            audit_id=claim["audit_id"],
            open_nonce=claim["open_nonce"],
            result_path=Path(str(artifact["result_path"])),
            result_byte_size=int(artifact["result_byte_size"]),
            artifact_hash=str(artifact["artifact_hash"]),
        )
        return {
            **artifact,
            "run_id": claim["run_id"],
            "audit_id": claim["audit_id"],
            "snapshot_id": readiness["snapshot_id"],
            "state": "RESULT_COMMITTED",
        }
    except Exception as exc:
        try:
            ledger.fail_closed(
                audit_id=claim["audit_id"],
                open_nonce=claim["open_nonce"],
                detail=f"{type(exc).__name__}:{exc}",
            )
        except FrozenValidationAuditError:
            pass
        raise


def _load_committed_result_artifact(
    database: Database,
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = V7FrozenValidationLedger(database).assert_committed()
    path = _fixed_root_file(
        database.config, Path(str(row["result_path"])), kind="results"
    )
    raw = _read_file_once(path)
    artifact_hash = _require_hash(row.get("artifact_hash"), "ledger.artifact_hash")
    if len(raw) != int(row["result_byte_size"]) or not hmac.compare_digest(
        _hash_bytes(raw), artifact_hash
    ):
        raise FrozenValidationAuditError(
            "V7 committed result artifact size/hash does not reproduce"
        )
    payload = _json_no_duplicate_keys(raw, "V7 committed result")
    if _canonical_json(payload).encode("utf-8") != raw:
        raise FrozenValidationAuditError("V7 result artifact is not canonical JSON")
    expected = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "project_id": PROJECT_ID,
        "lifecycle": "RESEARCH_ONLY",
        "run_id": str(row["run_id"]),
        "audit_id": str(row["audit_id"]),
        "snapshot_id": str(row["snapshot_id"]),
        "manifest_hash": str(row["manifest_hash"]),
        "protocol_version": PROTOCOL_VERSION,
        "protocol_hash": PROTOCOL_HASH,
        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
        "promotion_allowed": False,
        "trade_signals_enabled": False,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise FrozenValidationAuditError(
                f"V7 committed result {field} is not bound to its ledger"
            )
    components = dict(payload.get("component_hashes") or {})
    ledger_bindings = {
        "historical_universe_master_manifest_hash": "master_manifest_hash",
        "delisted_history_manifest_hash": "delisted_manifest_hash",
        "delisted_history_report_hash": "delisted_report_hash",
    }
    for component, field in ledger_bindings.items():
        if components.get(component) != str(row[field]):
            raise FrozenValidationAuditError(
                f"V7 committed result {component} is not bound to its ledger"
            )
    return payload, row


def _unique_phase_map(
    metrics: Mapping[str, Any], *, label: str
) -> dict[int, Mapping[str, Any]]:
    try:
        return v6_module._unique_phase_map(metrics, label=label)
    except v6_module.FrozenValidationAuditError as exc:
        raise FrozenValidationAuditError(str(exc)) from exc


def assess_v7_frozen_result(*, database: Database) -> dict[str, Any]:
    """Reload committed row/cycle evidence and recompute every decision metric."""

    assert_locked_dependencies()
    result, ledger_row = _load_committed_result_artifact(database)
    yearly = result.get("yearly")
    if not isinstance(yearly, Mapping) or set(str(key) for key in yearly) != {
        "2024",
        "2025",
    }:
        raise FrozenValidationAuditError("V7 result must contain exactly 2024 and 2025")

    sample_failures: list[str] = []
    performance_failures: list[str] = []
    combined_candidate = {phase: 0 for phase in range(NON_OVERLAP_PHASES)}
    combined_baseline = {phase: 0 for phase in range(NON_OVERLAP_PHASES)}
    recomputed_yearly: dict[str, Any] = {}
    for year in FROZEN_VALIDATION_YEARS:
        payload = yearly[str(year)]
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("evidence"), Mapping
        ):
            raise FrozenValidationAuditError(f"{year} row/cycle evidence is missing")
        evidence = payload["evidence"]
        if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
            raise FrozenValidationAuditError(f"{year} evidence schema changed")
        if evidence.get("v6_evidence_algorithm_hash") != LOCKED_V6_RESEARCH_HASH:
            raise FrozenValidationAuditError(f"{year} evidence algorithm binding changed")
        try:
            candidate = v6_module._recompute_method_from_evidence(
                evidence, year=year, method="EVENT_QUIET"
            )
            baseline = v6_module._recompute_method_from_evidence(
                evidence, year=year, method="RS60"
            )
            v4_module._assert_v4_pair_alignment(candidate, baseline)
        except (v6_module.FrozenValidationAuditError, ResearchDataBlockedError) as exc:
            raise FrozenValidationAuditError(
                f"{year} evidence does not reproduce: {exc}"
            ) from exc
        recomputed_yearly[str(year)] = {
            "candidate": candidate,
            "baseline": baseline,
        }
        candidate_phases = _unique_phase_map(candidate, label=f"{year} candidate")
        baseline_phases = _unique_phase_map(baseline, label=f"{year} baseline")
        for phase in range(NON_OVERLAP_PHASES):
            c_phase = candidate_phases[phase]
            b_phase = baseline_phases[phase]
            c_periods = int(c_phase["periods"])
            b_periods = int(b_phase["periods"])
            c_invested = int(c_phase["invested_periods"])
            b_invested = int(b_phase["invested_periods"])
            if c_periods < MINIMUM_PHASE_PERIODS:
                sample_failures.append(f"{year} candidate phase {phase}: too few periods")
            if b_periods < MINIMUM_PHASE_PERIODS:
                sample_failures.append(f"{year} baseline phase {phase}: too few periods")
            if c_invested < MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR:
                sample_failures.append(
                    f"{year} candidate phase {phase}: too few invested periods"
                )
            if b_invested < MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR:
                sample_failures.append(
                    f"{year} baseline phase {phase}: too few invested periods"
                )
            combined_candidate[phase] += c_invested
            combined_baseline[phase] += b_invested
            c_double = float(c_phase["double_cost_return"])
            b_double = float(b_phase["double_cost_return"])
            if c_double <= 0.0:
                performance_failures.append(
                    f"{year} phase {phase}: double-cost return is not positive"
                )
            if c_double <= b_double:
                performance_failures.append(
                    f"{year} phase {phase}: did not beat paired RS60"
                )
            if float(c_phase["max_drawdown"]) < (
                float(b_phase["max_drawdown"]) - MAXIMUM_DRAWDOWN_GAP
            ):
                performance_failures.append(
                    f"{year} phase {phase}: drawdown gap exceeds 3pp"
                )
        for field in ("precision_at_20", "pr_auc"):
            candidate_value = float(candidate[field])
            baseline_value = float(baseline[field])
            if not (
                np.isfinite(candidate_value)
                and np.isfinite(baseline_value)
                and 0.0 <= candidate_value <= 1.0
                and 0.0 <= baseline_value <= 1.0
            ):
                performance_failures.append(f"{year}: invalid {field}")
            elif candidate_value <= baseline_value:
                performance_failures.append(f"{year}: {field} did not beat RS60")
    for phase in range(NON_OVERLAP_PHASES):
        if combined_candidate[phase] < MINIMUM_PHASE_INVESTED_PERIODS_COMBINED:
            sample_failures.append(f"combined candidate phase {phase}: too few periods")
        if combined_baseline[phase] < MINIMUM_PHASE_INVESTED_PERIODS_COMBINED:
            sample_failures.append(f"combined baseline phase {phase}: too few periods")
    status = (
        "INCONCLUSIVE_SAMPLE"
        if sample_failures
        else "VALIDATION_REJECTED"
        if performance_failures
        else "OBSERVATION_ONLY"
    )
    return {
        "project_id": PROJECT_ID,
        "status": status,
        "lifecycle": "RESEARCH_ONLY",
        "snapshot_id": result["snapshot_id"],
        "audit_id": result["audit_id"],
        "artifact_hash": str(ledger_row["artifact_hash"]),
        "result_path": str(ledger_row["result_path"]),
        "result_byte_size": int(ledger_row["result_byte_size"]),
        "sample_gate_passed": not sample_failures,
        "performance_gate_passed": not performance_failures,
        "sample_failures": sample_failures,
        "performance_failures": performance_failures,
        "recomputed_yearly": recomputed_yearly,
        "promotion_allowed": False,
        "trade_signals_enabled": False,
        "failure_policy": "ANY_CHANGE_REQUIRES_V8",
    }


def register_v7_project(database: Database) -> None:
    """Register metadata only; never read data or open frozen evidence."""

    strategy = EarlyWinnerV7Strategy()
    rows = database.query(
        "SELECT project_id FROM research_projects WHERE project_id=?", (PROJECT_ID,)
    )
    if not rows:
        database.upsert_research_project(
            project_id=PROJECT_ID,
            version=PROJECT_VERSION,
            name=strategy.metadata.name,
            description=strategy.metadata.description,
            status="BLOCKED_DATA",
            data_gates={
                "preregistration": {
                    "ready": True,
                    "protocol_version": PROTOCOL_VERSION,
                    "protocol_hash": PROTOCOL_HASH,
                    "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
                    "label_schema_hash": LABEL_SCHEMA_HASH,
                    "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
                    "change_policy": "ANY_CHANGE_REQUIRES_V8",
                }
            },
        )
    database.execute(
        """UPDATE research_projects
        SET version=?, name=?, description=?, category='research_project',
            lifecycle='RESEARCH_ONLY'
        WHERE project_id=?""",
        (
            PROJECT_VERSION,
            strategy.metadata.name,
            strategy.metadata.description,
            PROJECT_ID,
        ),
    )
    V7FrozenValidationLedger(database)


__all__ = [
    "CLASSIFIER_RULE_HASH",
    "DEPENDENCY_LOCK_HASH",
    "EVALUATOR_BUNDLE_HASH",
    "EVALUATOR_COMPONENT_HASHES",
    "EVENT_EFFECTIVE_RULE_VERSION",
    "EVENT_RAW_REPLAY_SCHEMA_VERSION",
    "LABEL_SCHEMA_HASH",
    "MANIFEST_VERSION",
    "PROJECT_ID",
    "PROTOCOL_HASH",
    "PROTOCOL_SPEC",
    "PROTOCOL_VERSION",
    "EarlyWinnerV7Strategy",
    "FrozenManifestError",
    "FrozenValidationAlreadyOpened",
    "FrozenValidationAuditError",
    "V7FrozenValidationLedger",
    "V7ProtocolChangeRequiresV8",
    "assess_v7_frozen_result",
    "assert_locked_dependencies",
    "current_v7_critical_ast_hash",
    "evaluate_v7_frozen_frame",
    "frame_schema_hash",
    "frozen_validation_readiness",
    "prepare_v7_evaluation_frame",
    "register_v7_project",
    "run_v7_frozen_validation_once",
    "seal_v7_frozen_validation",
    "v7_frozen_root",
]
