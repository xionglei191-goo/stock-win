from __future__ import annotations

import base64
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
from typing import Any, Mapping, Sequence
from uuid import uuid4

import numpy as np
import pandas as pd

from . import early_winner_v4_research as v4_module
from . import early_winner_v5_research as v5_module
from .strategies import early_winner_v6 as v6_wrapper_module
from .config import PlatformConfig
from .early_winner_research import ResearchDataBlockedError, _portfolio_metrics
from .early_winner_v4_research import (
    HOLDING_TRADING_DAYS,
    NON_OVERLAP_PHASES,
    PORTFOLIO_SIZE,
    RETURN_COLUMN,
    _assert_v4_pair_alignment,
    _evaluate_v4_pair,
    _worst_phase_excess,
    prepare_v4_labels,
)
from .early_winner_v5_research import (
    CLASSIFIER_RULE_HASH,
    EVENT_REPLAY_SCHEMA_VERSION,
    historical_universe_master_gate,
    read_historical_universe_master_gate,
    validate_event_provenance,
)
from .models import StrategyCategory
from .storage import Database
from .strategies.early_winner import HARD_NEGATIVE_EVENT_TYPES
from .strategies.early_winner_v6 import EarlyWinnerV6Strategy


PROJECT_ID = "early_winner_v6"
STRATEGY_ID = "early_winner_event_quiet_v6"
PROJECT_VERSION = "6.0.0-preregistered"
PROTOCOL_VERSION = "early-winner-v6-sealed-event-v1"
DESIGN_YEARS = tuple(range(2018, 2024))
FROZEN_VALIDATION_YEARS = (2024, 2025)
OBSERVATION_YEARS = (2026,)
V5_REJECTED_STATUS = "PREREGISTRATION_REJECTED"

MINIMUM_PHASE_PERIODS = 3
MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR = 2
MINIMUM_PHASE_INVESTED_PERIODS_COMBINED = 4
MAXIMUM_DRAWDOWN_GAP = 0.03
MAXIMUM_INDUSTRY_CANDIDATES = 5

MANIFEST_VERSION = "early-winner-v6-frozen-manifest-v1"
EVENT_RAW_REPLAY_SCHEMA_VERSION = "early-winner-v6-cninfo-raw-replay-v1"
EVENT_EFFECTIVE_RULE_VERSION = "cninfo-close-next-session-v1"
LABEL_SCHEMA_VERSION = "early-winner-v6-v4-label-input-v1"
RESULT_SCHEMA_VERSION = "early-winner-v6-frozen-result-v2"
LEDGER_SCHEMA_VERSION = "early-winner-v6-open-ledger-v1"
LOCKED_V4_IMPLEMENTATION_HASH = (
    "dcc629bb8f5daa467415d27a2813f2a28b2d8e410446e1e2b88c868f753c5f45"
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
LOCKED_V6_WRAPPER_HASH = (
    "7d2499435b849f28f415b69f70d159c35136ee5154e667b4e4413a7d9e09739c"
)
LOCKED_V6_CRITICAL_AST_HASH = (
    "8a3f5fb562423ab49e56ccf06828dd2b47fbd661e4e018d60ace942a89cb8a31"
)
V6_CRITICAL_AST_BUNDLE_VERSION = "early-winner-v6-normalized-module-ast-v1"

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
    "version": LABEL_SCHEMA_VERSION,
    "grain": ["asof", "code"],
    "decision_inputs": [
        "listed_days",
        "valid_days_20",
        "adv20",
        "suspended",
        "is_st",
        "is_quit",
        "return_60",
        "turnover_20",
        "price_to_ma60",
        "relative_return_60",
        "execution_status_complete",
        "close",
        "ma60",
    ],
    "outcome_inputs": [
        "entry_executable",
        RETURN_COLUMN,
        "planned_entry_time",
        "entry_time",
        "planned_exit_time",
        "exit_time",
    ],
    "derived": ["market_breadth_ma60", "v4_eligible", "target"],
    "target_policy": {
        "return_column": RETURN_COLUMN,
        "eligible_quantile": 0.90,
        "requires_positive_return": True,
        "entry_executable_used_for_label_only": True,
    },
    "evaluation_policy": {
        "holding_trading_days": HOLDING_TRADING_DAYS,
        "portfolio_size": PORTFOLIO_SIZE,
        "non_overlap_phases": NON_OVERLAP_PHASES,
        "rank_before_entry_executable": True,
        "unfilled_slot": "CASH_NO_REFILL",
        "paired_cycle": "JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY",
        "cost": "20BPS_ROUND_TRIP_PER_FILLED_SLOT; DOUBLE=40BPS",
        "drawdown": "CYCLE_ENDPOINT_NAV_INCLUDING_INITIAL_1.0",
    },
}
LABEL_SCHEMA_HASH = _hash_payload(LABEL_SCHEMA_SPEC)
EVALUATOR_COMPONENT_HASHES = {
    "early_winner_v4_research.py": LOCKED_V4_IMPLEMENTATION_HASH,
    "early_winner_research.py": LOCKED_EARLY_WINNER_RESEARCH_HASH,
    "strategies/early_winner.py": LOCKED_EARLY_WINNER_STRATEGY_HASH,
    "early_winner_v5_research.py": LOCKED_V5_RESEARCH_HASH,
    "strategies/early_winner_v6.py": LOCKED_V6_WRAPPER_HASH,
    V6_CRITICAL_AST_BUNDLE_VERSION: LOCKED_V6_CRITICAL_AST_HASH,
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
    "candidate_rule": {
        "required_event_score": ">0",
        "hard_negative": "EXCLUDE",
        "sort": [
            "selected_event_score_desc",
            "amount_ratio_asc",
            "selected_event_effective_at_desc",
            "code_asc",
        ],
        "industry_maximum": MAXIMUM_INDUSTRY_CANDIDATES,
        "portfolio_size": PORTFOLIO_SIZE,
        "unfilled_slot": "CASH_NO_REFILL",
    },
    "frozen_open": {
        "manifest_version": MANIFEST_VERSION,
        "formats": ["parquet"],
        "path_policy": "CONFIG_RUNTIME_RESEARCH_EARLY_WINNER_V6_FROZEN_NO_REPARSE",
        "per_shard_binding": [
            "year",
            "relative_path",
            "content_hash",
            "schema_hash",
            "row_count",
        ],
        "database_state_machine": [
            "SEALED",
            "CONSUMING",
            "RESULT_COMMITTED",
            "FAILED_CLOSED",
        ],
        "one_open_only": True,
    },
    "event_provenance": {
        "schema_version": EVENT_RAW_REPLAY_SCHEMA_VERSION,
        "source": "CNINFO_OFFICIAL",
        "raw_content_hash": "SHA256_RAW_BYTES",
        "raw_content_must_rehash": True,
        "announcement_security_binding": ["announcement_id", "security_code"],
        "effective_rule_version": EVENT_EFFECTIVE_RULE_VERSION,
        "effective_at_must_be_calendar_derived": True,
        "published_at_must_be_inside_event_window": True,
    },
    "dependency_lock": {
        "v4_implementation_hash": LOCKED_V4_IMPLEMENTATION_HASH,
        "v6_critical_ast_bundle_version": V6_CRITICAL_AST_BUNDLE_VERSION,
        "evaluator_component_hashes": EVALUATOR_COMPONENT_HASHES,
        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "dependency_versions": LOCKED_DEPENDENCY_VERSIONS,
        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
    },
    "assessment": {
        "result_schema": RESULT_SCHEMA_VERSION,
        "requires_consumed_database_audit": True,
        "result_artifact": "CONTENT_ADDRESSED_CANONICAL_JSON_ATOMIC_RENAME",
        "ranking_metrics_source": "ROW_LEVEL_TARGET_AND_SCORE_EVIDENCE",
        "portfolio_metrics_source": "CYCLE_SELECTION_FILLED_RETURN_EVIDENCE",
        "cycle_asof_year_must_match_partition": True,
        "minimum_phase_periods_per_year": MINIMUM_PHASE_PERIODS,
        "minimum_phase_invested_periods_per_year": (
            MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR
        ),
        "minimum_phase_invested_periods_combined": (
            MINIMUM_PHASE_INVESTED_PERIODS_COMBINED
        ),
        "maximum_drawdown_gap": MAXIMUM_DRAWDOWN_GAP,
        "ranking_gates": ["Precision@20", "PR-AUC"],
        "portfolio_gates": [
            "DOUBLE_COST_POSITIVE_EVERY_PHASE",
            "DOUBLE_COST_BEATS_PAIRED_RS60_EVERY_PHASE",
            "DRAWDOWN_GAP_NOT_WORSE_THAN_3PP_EVERY_PHASE",
        ],
        "any_change_requires": "V7",
    },
}
PROTOCOL_HASH = _hash_payload(PROTOCOL_SPEC)


class V6ProtocolChangeRequiresV7(ResearchDataBlockedError):
    pass


class FrozenManifestError(ResearchDataBlockedError):
    pass


class FrozenValidationAlreadyOpened(ResearchDataBlockedError):
    pass


class FrozenValidationAuditError(ResearchDataBlockedError):
    pass


def current_v4_implementation_hash() -> str:
    path = Path(str(v4_module.__file__)).resolve()
    return _hash_bytes(path.read_bytes())


def current_v6_critical_ast_hash(path: Path | None = None) -> str:
    """Hash the whole V6 module AST while normalizing only its self hash literal."""

    source_path = Path(path or __file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    normalized = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name)
            and target.id == "LOCKED_V6_CRITICAL_AST_HASH"
            for target in node.targets
        ):
            node.value = ast.Constant(value="<SELF_HASH_NORMALIZED>")
            normalized = True
            break
    if not normalized:
        raise V6ProtocolChangeRequiresV7(
            "V6 normalized module AST cannot locate its self-hash assignment"
        )
    payload = {
        "version": V6_CRITICAL_AST_BUNDLE_VERSION,
        "normalized_module_ast": ast.dump(
            tree, annotate_fields=True, include_attributes=False
        ),
    }
    return _hash_payload(payload)


def assert_locked_dependencies() -> None:
    package_dir = Path(str(v4_module.__file__)).resolve().parent
    actual_components = {
        "early_winner_v4_research.py": _hash_bytes(
            (package_dir / "early_winner_v4_research.py").read_bytes()
        ),
        "early_winner_research.py": _hash_bytes(
            (package_dir / "early_winner_research.py").read_bytes()
        ),
        "strategies/early_winner.py": _hash_bytes(
            (package_dir / "strategies" / "early_winner.py").read_bytes()
        ),
        "early_winner_v5_research.py": _hash_bytes(
            Path(str(v5_module.__file__)).resolve().read_bytes()
        ),
        "strategies/early_winner_v6.py": _hash_bytes(
            Path(str(v6_wrapper_module.__file__)).resolve().read_bytes()
        ),
        V6_CRITICAL_AST_BUNDLE_VERSION: current_v6_critical_ast_hash(),
    }
    if any(
        not hmac.compare_digest(actual_components[name], expected)
        for name, expected in EVALUATOR_COMPONENT_HASHES.items()
    ):
        raise V6ProtocolChangeRequiresV7(
            "V6 evaluator source bundle changed after preregistration; create V7"
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
        raise V6ProtocolChangeRequiresV7(
            "V6 evaluator dependency versions changed after preregistration; create V7"
        )


def frame_schema_hash(frame: pd.DataFrame) -> str:
    return _hash_payload(
        {
            "columns": [
                {"name": str(column), "dtype": str(frame[column].dtype)}
                for column in frame.columns
            ]
        }
    )


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
        raise FrozenManifestError(f"{field} is not canonical UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FrozenManifestError(f"{field} must be a JSON object")
    return value


def _gate_dict(gates: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = gates.get(name)
    return dict(value) if isinstance(value, Mapping) else {}


def frozen_validation_readiness(
    gates: Mapping[str, Any], *, protocol_hash: str = PROTOCOL_HASH
) -> dict[str, Any]:
    if protocol_hash != PROTOCOL_HASH:
        return {
            "ready": False,
            "status": "V7_REQUIRED",
            "detail": "V6 protocol changed after preregistration; create V7",
            "failures": ["protocol_hash changed"],
        }
    try:
        assert_locked_dependencies()
    except V6ProtocolChangeRequiresV7 as exc:
        return {
            "ready": False,
            "status": "V7_REQUIRED",
            "detail": str(exc),
            "failures": [str(exc)],
        }

    failures: list[str] = []
    required = {
        "preregistration",
        "historical_universe_master",
        "event_provenance",
        "trading_calendar",
        "execution_status",
        "label_snapshot",
        "frozen_snapshot",
    }
    missing = sorted(required - set(gates))
    failures.extend(f"missing gate: {name}" for name in missing)

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

    master = historical_universe_master_gate(
        gates.get("historical_universe_master"), through_year=2025
    )
    if not master["ready"]:
        failures.append("historical_universe_master is not ready through 2025")

    calendar = _gate_dict(gates, "trading_calendar")
    execution = _gate_dict(gates, "execution_status")
    labels = _gate_dict(gates, "label_snapshot")
    event = _gate_dict(gates, "event_provenance")
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
        failures.append("label_snapshot is not bound to the V6 label schema")
    if labels.get("evaluator_bundle_hash") != EVALUATOR_BUNDLE_HASH:
        failures.append("label_snapshot is not bound to the V6 evaluator bundle")

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

    return {
        "ready": not failures,
        "status": "READY_TO_SEAL" if not failures else "SEALED",
        "detail": (
            "All V6 immutable gates are ready for a single database-sealed open"
            if not failures
            else "; ".join(failures)
        ),
        "failures": failures,
        "protocol_hash": PROTOCOL_HASH,
        "snapshot_id": str(frozen.get("snapshot_id") or ""),
        "manifest_path": str(frozen.get("manifest_path") or ""),
        "manifest_hash": hashes.get("manifest", ""),
        "component_hashes": {
            "historical_universe_master_manifest_hash": hashes.get("master", ""),
            "event_provenance_snapshot_hash": hashes.get("event", ""),
            "event_raw_content_manifest_hash": hashes.get("event_raw", ""),
            "trading_calendar_content_hash": hashes.get("calendar", ""),
            "execution_status_content_hash": hashes.get("execution", ""),
            "label_snapshot_hash": hashes.get("labels", ""),
            "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
            "label_schema_hash": LABEL_SCHEMA_HASH,
            "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
        },
        "historical_universe_master": master,
    }


def v6_frozen_root(config: PlatformConfig) -> Path:
    return Path(config.runtime_dir).absolute() / "research" / PROJECT_ID / "frozen"


def v6_results_root(config: PlatformConfig) -> Path:
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
        raise FrozenManifestError(f"path is outside fixed V6 root: {target}") from exc
    for path in (root, *(root / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1))):
        if path.exists() and _is_link_or_reparse(path):
            raise FrozenManifestError(f"V6 path uses a symlink/reparse point: {path}")


def _fixed_root_file(config: PlatformConfig, candidate: Path, *, kind: str) -> Path:
    runtime = Path(config.runtime_dir).absolute()
    root = v6_frozen_root(config) if kind == "frozen" else v6_results_root(config)
    candidate = Path(candidate).absolute()
    # The configured runtime directory is the trust root.  Check every
    # existing component between it and the V6 root as well as the artifact
    # path; otherwise a junction at e.g. runtime/research could redirect an
    # apparently in-root frozen manifest outside the configured runtime.
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
        raise FrozenManifestError(f"V6 {kind} file is outside its fixed root")
    return candidate


def _read_file_once(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FrozenManifestError(f"cannot securely open V6 artifact {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FrozenManifestError(f"V6 artifact is not a regular file: {path}")
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
CREATE TABLE IF NOT EXISTS early_winner_v6_frozen_runs (
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
    state TEXT NOT NULL CHECK(state IN (
        'SEALED', 'CONSUMING', 'RESULT_COMMITTED', 'FAILED_CLOSED'
    )),
    open_nonce_hash TEXT,
    audit_id TEXT,
    runner_id TEXT,
    sealed_at TEXT NOT NULL,
    opened_at TEXT,
    finished_at TEXT,
    result_hash TEXT,
    result_schema_hash TEXT,
    result_path TEXT,
    result_byte_size INTEGER,
    artifact_hash TEXT,
    failure_detail_hash TEXT,
    lock_version INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, protocol_hash)
)
"""


class V6FrozenValidationLedger:
    def __init__(self, database: Database) -> None:
        self.database = database
        with self.database.connect() as connection:
            connection.execute(_LEDGER_SQL)
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(early_winner_v6_frozen_runs)"
                ).fetchall()
            }
            for name, declaration in (
                ("result_path", "TEXT"),
                ("result_byte_size", "INTEGER"),
                ("artifact_hash", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE early_winner_v6_frozen_runs ADD COLUMN {name} {declaration}"
                    )

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat()

    def seal(self, readiness: Mapping[str, Any]) -> dict[str, Any]:
        if readiness.get("ready") is not True:
            raise FrozenManifestError(str(readiness.get("detail") or "V6 gates are not ready"))
        run_id = f"ewv6_{uuid4().hex}"
        manifest_file = _fixed_root_file(
            self.database.config,
            Path(str(readiness["manifest_path"])),
            kind="frozen",
        )
        manifest_path = str(manifest_file.absolute())
        values = (
            run_id,
            PROJECT_ID,
            PROTOCOL_VERSION,
            PROTOCOL_HASH,
            str(readiness["snapshot_id"]),
            manifest_path,
            str(readiness["manifest_hash"]),
            EVALUATOR_BUNDLE_HASH,
            LABEL_SCHEMA_HASH,
            DEPENDENCY_LOCK_HASH,
            self._now(),
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM early_winner_v6_frozen_runs "
                "WHERE project_id=? AND protocol_hash=?",
                (PROJECT_ID, PROTOCOL_HASH),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO early_winner_v6_frozen_runs
                    (run_id, project_id, protocol_version, protocol_hash, snapshot_id,
                     manifest_path, manifest_hash, evaluator_bundle_hash,
                     label_schema_hash, dependency_lock_hash, state, sealed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SEALED', ?)""",
                    values,
                )
            else:
                immutable = {
                    "snapshot_id": str(readiness["snapshot_id"]),
                    "manifest_path": manifest_path,
                    "manifest_hash": str(readiness["manifest_hash"]),
                    "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
                    "label_schema_hash": LABEL_SCHEMA_HASH,
                    "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
                }
                if any(str(existing[field]) != expected for field, expected in immutable.items()):
                    raise FrozenValidationAlreadyOpened(
                        "V6 sealed registration already exists with different immutable evidence"
                    )
        return self.get()

    def get(self) -> dict[str, Any]:
        rows = self.database.query(
            "SELECT * FROM early_winner_v6_frozen_runs "
            "WHERE project_id=? AND protocol_hash=?",
            (PROJECT_ID, PROTOCOL_HASH),
        )
        if not rows:
            raise FrozenValidationAuditError("V6 frozen run has not been database-sealed")
        return rows[0]

    def claim_once(self, *, runner_id: str) -> dict[str, str]:
        nonce = uuid4().hex + uuid4().hex
        nonce_hash = _hash_bytes(nonce.encode("utf-8"))
        audit_id = f"ewv6_audit_{uuid4().hex}"
        opened_at = self._now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM early_winner_v6_frozen_runs "
                "WHERE project_id=? AND protocol_hash=?",
                (PROJECT_ID, PROTOCOL_HASH),
            ).fetchone()
            if row is None:
                raise FrozenValidationAuditError("V6 frozen run is not sealed")
            updated = connection.execute(
                """UPDATE early_winner_v6_frozen_runs
                SET state='CONSUMING', open_nonce_hash=?, audit_id=?, runner_id=?,
                    opened_at=?, lock_version=lock_version+1
                WHERE run_id=? AND state='SEALED' AND open_nonce_hash IS NULL""",
                (nonce_hash, audit_id, str(runner_id), opened_at, str(row["run_id"])),
            )
            if updated.rowcount != 1:
                raise FrozenValidationAlreadyOpened(
                    f"V6 frozen evidence was already opened; current state={row['state']}"
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
                """UPDATE early_winner_v6_frozen_runs
                SET state=?, result_hash=?, result_schema_hash=?, result_path=?,
                    result_byte_size=?, artifact_hash=?, failure_detail_hash=?,
                    finished_at=?, lock_version=lock_version+1
                WHERE project_id=? AND protocol_hash=? AND state='CONSUMING'
                  AND audit_id=? AND open_nonce_hash=?""",
                (
                    state,
                    artifact_hash,
                    _hash_payload({"schema": RESULT_SCHEMA_VERSION}) if artifact_hash else None,
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
                    "V6 frozen state transition lost its one-time CAS"
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
                "V6 result artifact changed before database commit"
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
                "V6 result is not bound to the committed database audit"
            )
        _require_hash(row.get("artifact_hash"), "ledger.artifact_hash")
        if str(row.get("result_hash") or "") != str(row.get("artifact_hash") or ""):
            raise FrozenValidationAuditError("V6 ledger result/artifact hashes differ")
        if int(row.get("result_byte_size") or 0) <= 0 or not str(
            row.get("result_path") or ""
        ):
            raise FrozenValidationAuditError("V6 ledger result artifact reference is incomplete")
        return row


def _manifest_payload_hash(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_payload_hash", None)
    return _hash_payload(payload)


def _logical_frame_hash(frame: pd.DataFrame) -> str:
    ordered = frame.sort_values(["asof", "code"], kind="mergesort").reset_index(drop=True)
    serialized = ordered.to_json(
        orient="records",
        date_format="iso",
        date_unit="ns",
        double_precision=15,
        force_ascii=False,
    )
    return _hash_bytes(serialized.encode("utf-8"))


def _sorted_row_key_hash(frame: pd.DataFrame) -> str:
    keys = sorted(
        (
            pd.Timestamp(asof).isoformat(),
            str(code),
        )
        for asof, code in zip(frame["asof"], frame["code"], strict=True)
    )
    return _hash_payload(keys)


def _expected_manifest_components(readiness: Mapping[str, Any]) -> dict[str, str]:
    components = readiness.get("component_hashes")
    if not isinstance(components, Mapping):
        raise FrozenManifestError("V6 readiness has no immutable component hashes")
    return {str(key): str(value) for key, value in components.items()}


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
    if not isinstance(components, Mapping) or dict(components) != _expected_manifest_components(
        readiness
    ):
        raise FrozenManifestError("manifest components are not bound to the ready gates")

    raw_shards = manifest.get("shards")
    if not isinstance(raw_shards, list) or len(raw_shards) != len(
        FROZEN_VALIDATION_YEARS
    ):
        raise FrozenManifestError("manifest must contain exactly two frozen shards")
    shards: list[dict[str, Any]] = []
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
    for raw in raw_shards:
        if not isinstance(raw, Mapping):
            raise FrozenManifestError("manifest shard must be an object")
        shard = dict(raw)
        missing = sorted(required - set(shard))
        if missing:
            raise FrozenManifestError(
                "manifest shard missing fields: " + ",".join(missing)
            )
        if str(shard.get("format")) != "parquet":
            raise FrozenManifestError("V6 accepts only parquet frozen shards")
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
    if years != FROZEN_VALIDATION_YEARS or len(set(years)) != len(years):
        raise FrozenManifestError("manifest frozen shard years are not unique 2024/2025")
    if int(manifest.get("total_rows", -1)) != sum(int(item["row_count"]) for item in shards):
        raise FrozenManifestError("manifest total_rows does not match shard rows")
    schema_hash = _require_hash(manifest.get("schema_hash"), "manifest.schema_hash")
    if any(str(item["schema_hash"]) != schema_hash for item in shards):
        raise FrozenManifestError("frozen shard schemas are not identical")
    return sorted(shards, key=lambda item: int(item["year"]))


def _read_bound_manifest_and_shards(
    ledger_row: Mapping[str, Any], *, readiness: Mapping[str, Any], config: PlatformConfig
) -> tuple[dict[str, Any], dict[int, pd.DataFrame], list[dict[str, Any]]]:
    manifest_path = _fixed_root_file(
        config, Path(str(ledger_row["manifest_path"])), kind="frozen"
    )
    raw_manifest = _read_file_once(manifest_path)
    actual_manifest_hash = _hash_bytes(raw_manifest)
    if not hmac.compare_digest(actual_manifest_hash, str(ledger_row["manifest_hash"])):
        raise FrozenManifestError("manifest file hash differs from the database seal")
    manifest = _json_no_duplicate_keys(raw_manifest, "V6 frozen manifest")
    descriptors = _validate_manifest_payload(manifest, readiness=readiness)
    root = manifest_path.resolve(strict=True).parent
    frames: dict[int, pd.DataFrame] = {}
    loaded_profiles: list[dict[str, Any]] = []
    for descriptor in descriptors:
        year = int(descriptor["year"])
        candidate_path = root / str(descriptor["relative_path"])
        path = _fixed_root_file(config, candidate_path, kind="frozen")
        if not path.resolve(strict=True).is_relative_to(root):
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
            raise FrozenManifestError(f"{year} shard cannot be decoded as parquet: {exc}") from exc
        required_columns = {"asof", "code"}
        if not required_columns.issubset(frame.columns):
            raise FrozenManifestError(f"{year} shard is missing asof/code grain")
        parsed_asof = pd.to_datetime(frame["asof"], errors="coerce")
        if parsed_asof.isna().any() or set(parsed_asof.dt.year.astype(int)) != {year}:
            raise FrozenManifestError(f"{year} shard contains an invalid decision year")
        duplicate_count = int(frame.duplicated(["asof", "code"]).sum())
        checks = {
            "row_count": int(len(frame)),
            "decision_date_count": int(parsed_asof.dt.normalize().nunique()),
            "code_count": int(frame["code"].astype(str).nunique()),
            "min_asof": parsed_asof.min().date().isoformat(),
            "max_asof": parsed_asof.max().date().isoformat(),
            "duplicate_grain_count": duplicate_count,
            "schema_hash": frame_schema_hash(frame),
            "logical_content_hash": _logical_frame_hash(frame),
            "sorted_row_key_hash": _sorted_row_key_hash(frame),
        }
        for field, actual in checks.items():
            expected = descriptor.get(field)
            if str(actual) != str(expected):
                raise FrozenManifestError(
                    f"{year} shard {field} differs from manifest: {actual} != {expected}"
                )
        frames[year] = frame
        loaded_profiles.append(
            {
                "year": year,
                "relative_path": str(descriptor["relative_path"]),
                "byte_size": len(raw),
                "content_hash": content_hash,
                **checks,
            }
        )
    return manifest, frames, loaded_profiles


def _as_event_records(value: Any) -> list[dict[str, Any]]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ResearchDataBlockedError("event_replay_records is invalid JSON") from exc
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise ResearchDataBlockedError("event_replay_records must be a list")
    if not all(isinstance(item, Mapping) for item in value):
        raise ResearchDataBlockedError("event_replay_records contains a non-object")
    return [dict(item) for item in value]


def v6_raw_event_replay_hash(
    *, code: str, asof: Any, records: Sequence[Mapping[str, Any]]
) -> str:
    normalized = []
    for record in records:
        normalized.append(
            {
                "announcement_id": str(record.get("announcement_id") or ""),
                "security_code": str(record.get("security_code") or ""),
                "event_hash": str(record.get("event_hash") or ""),
                "raw_content_sha256": str(record.get("raw_content_sha256") or ""),
                "published_at": str(record.get("published_at") or ""),
                "effective_at": str(record.get("effective_at") or ""),
                "published_after_close": bool(record.get("published_after_close")),
                "session_close_at": str(record.get("session_close_at") or ""),
                "next_trading_session_at": str(
                    record.get("next_trading_session_at") or ""
                ),
                "effective_rule_version": str(
                    record.get("effective_rule_version") or ""
                ),
                "effective_at_calendar_hash": str(
                    record.get("effective_at_calendar_hash") or ""
                ),
            }
        )
    normalized.sort(key=lambda item: (item["announcement_id"], item["event_hash"]))
    return _hash_payload(
        {
            "schema": EVENT_RAW_REPLAY_SCHEMA_VERSION,
            "code": str(code),
            "asof": pd.Timestamp(asof).isoformat(),
            "records": normalized,
        }
    )


def validate_v6_event_provenance(
    frame: pd.DataFrame, *, trading_calendar_hash: str
) -> dict[str, Any]:
    legacy = validate_event_provenance(frame)
    errors = list(legacy.get("errors", [])) if not legacy.get("ready") else []
    required_columns = {"event_replay_records", "v6_event_raw_replay_hash"}
    missing = sorted(required_columns - set(frame.columns))
    errors.extend(f"missing column: {column}" for column in missing)
    if missing:
        return {
            "ready": False,
            "status": "SCHEMA_INCOMPLETE",
            "detail": "V6 raw event provenance schema is incomplete",
            "errors": errors,
        }
    calendar_hash = _require_hash(trading_calendar_hash, "trading_calendar_hash")
    for index, row in frame.iterrows():
        grain = f"{row.get('asof')}:{row.get('code')}"
        try:
            decision_day = pd.Timestamp(row.get("asof")).normalize()
            window_start = decision_day - pd.Timedelta(days=30)
            window_end = decision_day + pd.Timedelta(hours=15)
            records = _as_event_records(row.get("event_replay_records"))
            for record in records:
                required = {
                    "announcement_id",
                    "security_code",
                    "raw_content_base64",
                    "raw_content_sha256",
                    "published_after_close",
                    "session_close_at",
                    "next_trading_session_at",
                    "effective_rule_version",
                    "effective_at_calendar_hash",
                }
                absent = sorted(required - set(record))
                if absent:
                    raise ResearchDataBlockedError(
                        "raw event record missing: " + ",".join(absent)
                    )
                if not str(record["announcement_id"]).strip():
                    raise ResearchDataBlockedError("announcement_id is empty")
                if str(record["security_code"]) != str(row.get("code")):
                    raise ResearchDataBlockedError("announcement is bound to another security")
                try:
                    raw_content = base64.b64decode(
                        str(record["raw_content_base64"]), validate=True
                    )
                except Exception as exc:
                    raise ResearchDataBlockedError(
                        "raw announcement content is not valid base64"
                    ) from exc
                raw_hash = _require_hash(
                    record["raw_content_sha256"], "raw_content_sha256"
                )
                if not hmac.compare_digest(raw_hash, _hash_bytes(raw_content)):
                    raise ResearchDataBlockedError(
                        "raw announcement content hash does not reproduce"
                    )
                event_hash = _require_hash(record.get("event_hash"), "event_hash")
                if not hmac.compare_digest(event_hash, raw_hash):
                    raise ResearchDataBlockedError(
                        "event_hash is not the raw announcement content hash"
                    )
                published = pd.Timestamp(record.get("published_at"))
                effective = pd.Timestamp(record.get("effective_at"))
                close_at = pd.Timestamp(record["session_close_at"])
                next_session = pd.Timestamp(record["next_trading_session_at"])
                if published < window_start or published > window_end:
                    raise ResearchDataBlockedError(
                        "announcement publication is outside the frozen 30-day window"
                    )
                after_close = bool(record["published_after_close"])
                if after_close != bool(published > close_at):
                    raise ResearchDataBlockedError("published_after_close does not reproduce")
                derived = next_session if after_close else published
                if effective != derived:
                    raise ResearchDataBlockedError(
                        "effective_at does not reproduce from publication/calendar"
                    )
                if record["effective_rule_version"] != EVENT_EFFECTIVE_RULE_VERSION:
                    raise ResearchDataBlockedError("effective_at rule version changed")
                if record["effective_at_calendar_hash"] != calendar_hash:
                    raise ResearchDataBlockedError(
                        "effective_at is not bound to the frozen trading calendar"
                    )
            expected_hash = v6_raw_event_replay_hash(
                code=str(row.get("code")), asof=row.get("asof"), records=records
            )
            actual_hash = _require_hash(
                row.get("v6_event_raw_replay_hash"), "v6_event_raw_replay_hash"
            )
            if not hmac.compare_digest(actual_hash, expected_hash):
                raise ResearchDataBlockedError("V6 raw event replay hash does not reproduce")
        except (ResearchDataBlockedError, TypeError, ValueError) as exc:
            errors.append(f"{grain} row {index}: {exc}")
    return {
        "ready": not errors,
        "status": "READY" if not errors else "REPLAY_FAILED",
        "detail": (
            "V6 event evidence rehashes, binds to the security, and reproduces timing"
            if not errors
            else "V6 event provenance failed closed"
        ),
        "errors": errors,
        "row_count": int(len(frame)),
    }


def _validate_frame_component_bindings(
    frame: pd.DataFrame, *, component_hashes: Mapping[str, Any]
) -> None:
    required = {
        "historical_universe_master_manifest_hash",
        "event_provenance_snapshot_hash",
        "event_raw_content_manifest_hash",
        "trading_calendar_content_hash",
        "execution_status_content_hash",
        "label_snapshot_hash",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ResearchDataBlockedError(
            "V6 frame component binding columns missing: " + ",".join(missing)
        )
    for column in sorted(required):
        expected = str(component_hashes.get(column) or "")
        _require_hash(expected, f"component_hashes.{column}")
        values = set(frame[column].fillna("").astype(str))
        if values != {expected}:
            raise ResearchDataBlockedError(
                f"V6 frame rows are not bound to {column}"
            )


def _hard_negative_present(row: pd.Series) -> bool:
    value = row.get("hard_negative_event_hashes")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return True
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, (list, tuple, set)) and len(value):
        return True
    return str(row.get("selected_event_type") or "").upper() in HARD_NEGATIVE_EVENT_TYPES


def _candidate_sort(frame: pd.DataFrame) -> pd.DataFrame:
    ranked = frame.copy()
    ranked["_event_score"] = pd.to_numeric(
        ranked["selected_event_score"], errors="coerce"
    )
    ranked["_amount_ratio"] = pd.to_numeric(ranked["amount_ratio"], errors="coerce")
    ranked["_event_effective"] = pd.to_datetime(
        ranked["selected_event_effective_at"], errors="coerce"
    )
    return ranked.sort_values(
        ["_event_score", "_amount_ratio", "_event_effective", "code"],
        ascending=[False, True, False, True],
        kind="mergesort",
    )


def prepare_v6_evaluation_frame(
    frame: pd.DataFrame,
    *,
    expected_year: int,
    component_hashes: Mapping[str, Any],
) -> pd.DataFrame:
    if int(expected_year) not in FROZEN_VALIDATION_YEARS:
        raise ResearchDataBlockedError("V6 frozen evaluator accepts only 2024 or 2025")
    if frame.empty:
        raise ResearchDataBlockedError(f"V6 {expected_year} frozen frame is empty")
    years = set(
        pd.to_datetime(frame.get("asof"), errors="coerce").dt.year.dropna().astype(int)
    )
    if years != {int(expected_year)}:
        raise ResearchDataBlockedError(
            f"V6 frozen evaluator expected {expected_year}; got {sorted(years)}"
        )
    _validate_frame_component_bindings(frame, component_hashes=component_hashes)
    calendar_hash = str(component_hashes["trading_calendar_content_hash"])
    provenance = validate_v6_event_provenance(
        frame, trading_calendar_hash=calendar_hash
    )
    if not provenance["ready"]:
        raise ResearchDataBlockedError(
            provenance["detail"] + ": " + "; ".join(provenance["errors"][:20])
        )

    # Always recompute labels and the decision universe. Precomputed
    # v4_eligible/target columns are intentionally ignored.
    data = prepare_v4_labels(frame.copy())
    required = {
        "asof",
        "code",
        "industry",
        "amount_ratio",
        "relative_return_60",
        "v4_eligible",
        "target",
        "entry_executable",
        RETURN_COLUMN,
        "planned_entry_time",
        "planned_exit_time",
        "exit_time",
        "label_window_matured",
    }
    missing = sorted(required - set(data.columns))
    if missing:
        raise ResearchDataBlockedError(
            "V6 frozen frame missing evaluation columns: " + ",".join(missing)
        )
    data["year"] = pd.to_datetime(data["asof"], errors="coerce").dt.year
    data["evaluation_period"] = data.groupby("asof")[
        "label_window_matured"
    ].transform("any").fillna(False).astype(bool)
    data["v6_evaluation_eligible"] = data["v4_eligible"].fillna(False).astype(bool)
    event_score = pd.to_numeric(data["selected_event_score"], errors="coerce")
    amount = pd.to_numeric(data["amount_ratio"], errors="coerce")
    effective = pd.to_datetime(data["selected_event_effective_at"], errors="coerce")
    hard_negative = data.apply(_hard_negative_present, axis=1)
    data["v6_candidate_eligible"] = (
        data["v6_evaluation_eligible"]
        & (event_score > 0.0)
        & amount.notna()
        & np.isfinite(amount)
        & effective.notna()
        & ~hard_negative
    )
    data["v6_selection_score"] = np.nan
    data["v6_metric_score"] = np.nan
    for _, group in data.groupby("asof", sort=False):
        base = group.loc[group["v6_evaluation_eligible"]]
        candidates = _candidate_sort(base.loc[base["v6_candidate_eligible"]])
        if not candidates.empty:
            values = np.arange(len(candidates), 0, -1, dtype=float)
            data.loc[candidates.index, "v6_selection_score"] = values
            data.loc[candidates.index, "v6_metric_score"] = values
        data.loc[base.index.difference(candidates.index), "v6_metric_score"] = 0.0
    return data


def _full_pool_metrics(frame: pd.DataFrame, score_column: str) -> dict[str, float | int]:
    from sklearn.metrics import average_precision_score

    mask = (
        frame["evaluation_period"].fillna(False).astype(bool)
        & frame["v6_evaluation_eligible"].fillna(False).astype(bool)
        & pd.to_numeric(frame["target"], errors="coerce").notna()
        & pd.to_numeric(frame[score_column], errors="coerce").notna()
    )
    labeled = frame.loc[mask]
    target = pd.to_numeric(labeled["target"], errors="coerce").astype(int)
    score = pd.to_numeric(labeled[score_column], errors="coerce")
    pr_auc = (
        float(average_precision_score(target, score))
        if len(labeled) and target.nunique() > 1
        else 0.0
    )
    weekly_ic: list[float] = []
    for _, group in labeled.groupby("asof", sort=True):
        if group[score_column].nunique() < 2 or group[RETURN_COLUMN].nunique() < 2:
            continue
        value = pd.to_numeric(group[score_column], errors="coerce").corr(
            pd.to_numeric(group[RETURN_COLUMN], errors="coerce"), method="spearman"
        )
        if pd.notna(value):
            weekly_ic.append(float(value))
    return {
        "pr_auc": pr_auc,
        "ic": float(np.mean(weekly_ic)) if weekly_ic else 0.0,
        "ranking_rows": int(len(labeled)),
        "ranking_weeks": int(labeled["asof"].nunique()),
    }


def evaluate_v6_frozen_frame(
    frame: pd.DataFrame,
    *,
    expected_year: int,
    component_hashes: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate one synthetic or sealed frozen year without entering V5 design code."""

    assert_locked_dependencies()
    prepared = prepare_v6_evaluation_frame(
        frame, expected_year=expected_year, component_hashes=component_hashes
    )
    return _evaluate_prepared_v6(prepared, expected_year=expected_year)


def _evaluate_prepared_v6(
    prepared: pd.DataFrame, *, expected_year: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate, baseline = _evaluate_v4_pair(
        prepared,
        candidate_score_column="v6_selection_score",
        baseline_score_column="relative_return_60",
        eligibility_column="v6_evaluation_eligible",
    )
    candidate.update(_full_pool_metrics(prepared, "v6_metric_score"))
    candidate["worst_phase_total_return_excess"] = _worst_phase_excess(
        candidate, baseline, "total_return"
    )
    candidate["worst_phase_double_cost_return_excess"] = _worst_phase_excess(
        candidate, baseline, "double_cost_return"
    )
    candidate["worst_phase_drawdown_gap"] = _worst_phase_excess(
        candidate, baseline, "max_drawdown"
    )
    for method, metrics in (("EVENT_QUIET", candidate), ("RS60", baseline)):
        metrics.update(
            {
                "year": int(expected_year),
                "method": method,
                "protocol_version": PROTOCOL_VERSION,
                "protocol_hash": PROTOCOL_HASH,
                "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
                "label_schema_hash": LABEL_SCHEMA_HASH,
                "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
                "lifecycle": "RESEARCH_ONLY",
            }
        )
    return candidate, baseline


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    number = _optional_float(value)
    return int(number) if number is not None else None


def _build_year_evidence(
    prepared: pd.DataFrame,
    *,
    year: int,
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    ranking_rows: list[dict[str, Any]] = []
    ordered = prepared.sort_values(["asof", "code"], kind="mergesort")
    for _, row in ordered.iterrows():
        ranking_rows.append(
            {
                "asof": str(row["asof"]),
                "code": str(row["code"]),
                "industry": str(row.get("industry") or ""),
                "evaluation_period": bool(row.get("evaluation_period", False)),
                "eligible": bool(row.get("v6_evaluation_eligible", False)),
                "candidate_selection_score": _optional_float(
                    row.get("v6_selection_score")
                ),
                "candidate_metric_score": _optional_float(row.get("v6_metric_score")),
                "baseline_score": _optional_float(row.get("relative_return_60")),
                "target": _optional_int(row.get("target")),
                "forward_return": _optional_float(row.get(RETURN_COLUMN)),
                "entry_executable": bool(row.get("entry_executable", False)),
            }
        )

    def method_cycles(
        metrics: Mapping[str, Any], *, score_column: str
    ) -> list[dict[str, Any]]:
        weeks, _, _ = v4_module._prepare_v4_weeks(
            prepared, score_column, "v6_evaluation_eligible"
        )
        week_map = {str(item["asof"]): item for item in weeks}
        phase_evidence: list[dict[str, Any]] = []
        for phase in metrics.get("phase_metrics", []):
            cycles: list[dict[str, Any]] = []
            for cycle in phase.get("cycles", []):
                asof = str(cycle["asof"])
                week = week_map.get(asof)
                if week is None:
                    raise FrozenValidationAuditError(
                        f"{year} evidence has no evaluation week for cycle {asof}"
                    )
                selections: list[dict[str, Any]] = []
                for rank, (_, selected) in enumerate(
                    week["selected"].iterrows(), start=1
                ):
                    forward_return = _optional_float(selected.get(RETURN_COLUMN))
                    executable = bool(selected.get("entry_executable", False))
                    filled = executable and forward_return is not None
                    selections.append(
                        {
                            "rank": rank,
                            "code": str(selected["code"]),
                            "industry": str(selected.get("industry") or ""),
                            "score": _optional_float(selected.get(score_column)),
                            "target": _optional_int(selected.get("target")),
                            "entry_executable": executable,
                            "forward_return": forward_return,
                            "filled": filled,
                            "filled_return": forward_return if filled else None,
                        }
                    )
                cycles.append(
                    {
                        "asof": asof,
                        "planned_entry_at": str(cycle["planned_entry_at"]),
                        "joint_capital_available_at": str(
                            cycle["joint_capital_available_at"]
                        ),
                        "selections": selections,
                    }
                )
            phase_evidence.append({"phase": int(phase["phase"]), "cycles": cycles})
        return phase_evidence

    evidence = {
        "schema_version": "early-winner-v6-row-cycle-evidence-v1",
        "year": int(year),
        "ranking_rows": ranking_rows,
        "methods": {
            "EVENT_QUIET": {
                "selection_score_field": "candidate_selection_score",
                "metric_score_field": "candidate_metric_score",
                "phases": method_cycles(
                    candidate, score_column="v6_selection_score"
                ),
            },
            "RS60": {
                "selection_score_field": "baseline_score",
                "metric_score_field": "baseline_score",
                "phases": method_cycles(
                    baseline, score_column="relative_return_60"
                ),
            },
        },
    }
    evidence["ranking_evidence_hash"] = _hash_payload(ranking_rows)
    evidence["cycle_evidence_hash"] = _hash_payload(evidence["methods"])
    return evidence


def _cycle_ledger_hash(metrics: Mapping[str, Any]) -> str:
    phases = metrics.get("phase_metrics", [])
    return _hash_payload(
        {
            "schema": "early-winner-v6-cycle-ledger-v1",
            "year": int(metrics.get("year", 0)),
            "method": str(metrics.get("method") or ""),
            "phases": phases,
        }
    )


def compute_v6_result_hash(result: Mapping[str, Any]) -> str:
    payload = dict(result)
    for field in ("result_hash", "artifact_hash", "result_path", "result_byte_size"):
        payload.pop(field, None)
    return _hash_payload(payload)


def _ensure_results_root(config: PlatformConfig) -> Path:
    runtime = Path(config.runtime_dir).absolute()
    runtime.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(runtime):
        raise FrozenValidationAuditError("V6 runtime directory is a link/reparse point")
    current = runtime
    for part in ("research", PROJECT_ID, "results"):
        current = current / part
        if current.exists() and _is_link_or_reparse(current):
            raise FrozenValidationAuditError(
                f"V6 result directory is a link/reparse point: {current}"
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
        raise FrozenValidationAuditError("V6 result hash bucket is a link/reparse point")
    bucket.mkdir(exist_ok=True)
    path = bucket / f"{artifact_hash}.json"
    if path.exists():
        path = _fixed_root_file(config, path, kind="results")
        existing = _read_file_once(path)
        if existing != raw:
            raise FrozenValidationAuditError(
                "V6 content-addressed result path contains different bytes"
            )
    else:
        temporary = bucket / f".{artifact_hash}.{uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(temporary, flags, 0o600)
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
                "V6 result bytes changed during atomic publication"
            )
    return {
        "result_path": str(path.absolute()),
        "result_byte_size": len(raw),
        "artifact_hash": artifact_hash,
    }


def _load_committed_result_artifact(
    database: Database,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger = V6FrozenValidationLedger(database)
    row = ledger.assert_committed()
    path = _fixed_root_file(
        database.config, Path(str(row["result_path"])), kind="results"
    )
    raw = _read_file_once(path)
    artifact_hash = _require_hash(row.get("artifact_hash"), "ledger.artifact_hash")
    if len(raw) != int(row["result_byte_size"]) or not hmac.compare_digest(
        _hash_bytes(raw), artifact_hash
    ):
        raise FrozenValidationAuditError(
            "V6 committed result artifact size/hash does not reproduce"
        )
    payload = _json_no_duplicate_keys(raw, "V6 committed result")
    if _canonical_json(payload).encode("utf-8") != raw:
        raise FrozenValidationAuditError("V6 result artifact is not canonical JSON")
    result_hash = compute_v6_result_hash(payload)
    if not hmac.compare_digest(result_hash, artifact_hash):
        raise FrozenValidationAuditError(
            "V6 result semantic hash differs from committed artifact hash"
        )
    expected = {
        "run_id": str(row["run_id"]),
        "audit_id": str(row["audit_id"]),
        "snapshot_id": str(row["snapshot_id"]),
        "manifest_hash": str(row["manifest_hash"]),
        "protocol_hash": PROTOCOL_HASH,
        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise FrozenValidationAuditError(
                f"V6 committed result {field} is not bound to its ledger"
            )
    return payload, row


def seal_v6_frozen_validation(
    *, database: Database, gates: Mapping[str, Any]
) -> dict[str, Any]:
    readiness = frozen_validation_readiness(gates)
    if readiness["status"] == "V7_REQUIRED":
        raise V6ProtocolChangeRequiresV7(readiness["detail"])
    return V6FrozenValidationLedger(database).seal(readiness)


def run_v6_frozen_validation_once(
    *,
    database: Database,
    gates: Mapping[str, Any],
    runner_id: str,
) -> dict[str, Any]:
    """Claim once before reading manifest/shards, then commit one bound result."""

    readiness = frozen_validation_readiness(gates)
    if readiness["status"] == "V7_REQUIRED":
        raise V6ProtocolChangeRequiresV7(readiness["detail"])
    if not readiness["ready"]:
        raise FrozenManifestError(readiness["detail"])
    ledger = V6FrozenValidationLedger(database)
    sealed = ledger.seal(readiness)
    claim = ledger.claim_once(runner_id=str(runner_id))
    try:
        # This is intentionally the first manifest/frozen shard read in this
        # function. The database state is already CONSUMING and cannot reset.
        manifest, frames, loaded_profiles = _read_bound_manifest_and_shards(
            sealed, readiness=readiness, config=database.config
        )
        yearly: dict[str, Any] = {}
        for year in FROZEN_VALIDATION_YEARS:
            prepared = prepare_v6_evaluation_frame(
                frames[year],
                expected_year=year,
                component_hashes=readiness["component_hashes"],
            )
            candidate, baseline = _evaluate_prepared_v6(
                prepared, expected_year=year
            )
            for metrics in (candidate, baseline):
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
                "evidence": _build_year_evidence(
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


def _unique_phase_map(metrics: Mapping[str, Any], *, label: str) -> dict[int, Mapping[str, Any]]:
    raw = metrics.get("phase_metrics")
    if not isinstance(raw, list):
        raise FrozenValidationAuditError(f"{label} phase_metrics is not a list")
    phases: dict[int, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping) or "phase" not in item:
            raise FrozenValidationAuditError(f"{label} contains an invalid phase")
        phase = int(item["phase"])
        if phase in phases:
            raise FrozenValidationAuditError(f"{label} contains duplicate phase {phase}")
        phases[phase] = item
    if set(phases) != set(range(NON_OVERLAP_PHASES)):
        raise FrozenValidationAuditError(f"{label} must contain exactly eight phases")
    return phases


def _ranking_frame_from_evidence(evidence: Mapping[str, Any], *, year: int) -> pd.DataFrame:
    rows = evidence.get("ranking_rows")
    if not isinstance(rows, list) or not rows:
        raise FrozenValidationAuditError(f"{year} ranking evidence is empty")
    if not hmac.compare_digest(
        _require_hash(evidence.get("ranking_evidence_hash"), "ranking_evidence_hash"),
        _hash_payload(rows),
    ):
        raise FrozenValidationAuditError(f"{year} ranking evidence hash does not reproduce")
    required = {
        "asof",
        "code",
        "industry",
        "evaluation_period",
        "eligible",
        "candidate_selection_score",
        "candidate_metric_score",
        "baseline_score",
        "target",
        "forward_return",
        "entry_executable",
    }
    for row in rows:
        if not isinstance(row, Mapping) or not required.issubset(row):
            raise FrozenValidationAuditError(f"{year} ranking evidence row is incomplete")
        if row["target"] not in {None, 0, 1}:
            raise FrozenValidationAuditError(f"{year} ranking target is not binary/null")
        for field in (
            "candidate_selection_score",
            "candidate_metric_score",
            "baseline_score",
            "forward_return",
        ):
            if row[field] is not None and not np.isfinite(float(row[field])):
                raise FrozenValidationAuditError(f"{year} ranking {field} is non-finite")
    frame = pd.DataFrame([dict(row) for row in rows])
    decisions = pd.to_datetime(frame["asof"], errors="coerce")
    if decisions.isna().any() or set(decisions.dt.year.astype(int)) != {int(year)}:
        raise FrozenValidationAuditError(f"{year} ranking evidence has wrong decision year")
    if int(frame.duplicated(["asof", "code"]).sum()):
        raise FrozenValidationAuditError(f"{year} ranking evidence has duplicate grain")
    return frame


def _evidence_selection(group: pd.DataFrame, score_field: str) -> pd.DataFrame:
    pool = group.loc[group["eligible"].astype(bool)].copy()
    score = pd.to_numeric(pool[score_field], errors="coerce")
    pool = pool.loc[score.notna() & np.isfinite(score)].copy()
    if pool.empty:
        return pool
    pool["_score"] = pd.to_numeric(pool[score_field], errors="coerce")
    ranked = pool.sort_values(["_score", "code"], ascending=[False, True], kind="mergesort")
    positions: list[Any] = []
    industries: dict[str, int] = {}
    for index, row in ranked.iterrows():
        industry = str(row.get("industry") or "")
        if industries.get(industry, 0) >= MAXIMUM_INDUSTRY_CANDIDATES:
            continue
        industries[industry] = industries.get(industry, 0) + 1
        positions.append(index)
        if len(positions) == PORTFOLIO_SIZE:
            break
    return ranked.loc[positions]


def _recompute_ranking_metrics_from_evidence(
    frame: pd.DataFrame, *, method: str
) -> dict[str, Any]:
    if method == "EVENT_QUIET":
        selection_field = "candidate_selection_score"
        metric_field = "candidate_metric_score"
    elif method == "RS60":
        selection_field = metric_field = "baseline_score"
    else:
        raise FrozenValidationAuditError(f"unsupported V6 evidence method: {method}")
    weekly_precision: list[float] = []
    weekly_ic: list[float] = []
    for _, group in frame.groupby("asof", sort=True):
        if not bool(group["evaluation_period"].astype(bool).any()):
            continue
        decision_pool = group.loc[group["eligible"].astype(bool)]
        if not decision_pool.empty:
            selected = _evidence_selection(group, selection_field)
            target = pd.to_numeric(selected["target"], errors="coerce")
            weekly_precision.append(float((target == 1).sum()) / PORTFOLIO_SIZE)
        labeled = decision_pool.loc[
            pd.to_numeric(decision_pool["target"], errors="coerce").notna()
            & pd.to_numeric(decision_pool["forward_return"], errors="coerce").notna()
            & pd.to_numeric(decision_pool[metric_field], errors="coerce").notna()
        ]
        if (
            len(labeled) >= 3
            and labeled[metric_field].nunique() > 1
            and labeled["forward_return"].nunique() > 1
        ):
            correlation = pd.to_numeric(labeled[metric_field], errors="coerce").corr(
                pd.to_numeric(labeled["forward_return"], errors="coerce"),
                method="spearman",
            )
            if pd.notna(correlation):
                weekly_ic.append(float(correlation))
    labeled_all = frame.loc[
        frame["evaluation_period"].astype(bool)
        & frame["eligible"].astype(bool)
        & pd.to_numeric(frame["target"], errors="coerce").notna()
        & pd.to_numeric(frame[metric_field], errors="coerce").notna()
    ]
    target = pd.to_numeric(labeled_all["target"], errors="coerce").astype(int)
    score = pd.to_numeric(labeled_all[metric_field], errors="coerce")
    from sklearn.metrics import average_precision_score

    pr_auc = (
        float(average_precision_score(target, score))
        if len(target) and target.nunique() > 1
        else 0.0
    )
    return {
        "precision_at_20": float(np.mean(weekly_precision)) if weekly_precision else 0.0,
        "pr_auc": pr_auc,
        "ic": float(np.mean(weekly_ic)) if weekly_ic else 0.0,
        "ranking_rows": int(len(labeled_all)),
        "ranking_weeks": int(labeled_all["asof"].nunique()),
    }


def _verify_phase_from_cycles(
    phase: Mapping[str, Any],
    *,
    year: int,
    label: str,
    ranking: pd.DataFrame,
    score_field: str,
) -> dict[str, Any]:
    cycles = phase.get("cycles")
    if not isinstance(cycles, list):
        raise FrozenValidationAuditError(f"{label} cycles is not a list")
    seen: set[str] = set()
    gross_returns: list[float] = []
    turnovers: list[float] = []
    recomputed_cycles: list[dict[str, Any]] = []
    filled_slots = 0
    invested = 0
    for cycle in cycles:
        if not isinstance(cycle, Mapping) or not isinstance(cycle.get("selections"), list):
            raise FrozenValidationAuditError(f"{label} cycle evidence is incomplete")
        asof = pd.Timestamp(cycle.get("asof"))
        if asof.year != int(year):
            raise FrozenValidationAuditError(
                f"{label} cycle asof {asof.date()} does not belong to {year}"
            )
        asof_key = str(cycle["asof"])
        if asof_key in seen:
            raise FrozenValidationAuditError(f"{label} contains duplicate cycle {asof_key}")
        seen.add(asof_key)
        group = ranking.loc[ranking["asof"].astype(str) == asof_key]
        if group.empty or not bool(group["evaluation_period"].astype(bool).any()):
            raise FrozenValidationAuditError(f"{label} cycle has no ranking evidence")
        expected = _evidence_selection(group, score_field)
        supplied = cycle["selections"]
        if len(supplied) != len(expected):
            raise FrozenValidationAuditError(f"{label} selected count does not reproduce")
        filled_returns: list[float] = []
        for rank, (item, (_, row)) in enumerate(zip(supplied, expected.iterrows(), strict=True), start=1):
            if not isinstance(item, Mapping):
                raise FrozenValidationAuditError(f"{label} selection is not an object")
            expected_return = _optional_float(row.get("forward_return"))
            expected_score = _optional_float(row.get(score_field))
            executable = bool(row.get("entry_executable"))
            filled = executable and expected_return is not None
            exact = {
                "rank": rank,
                "code": str(row["code"]),
                "industry": str(row.get("industry") or ""),
                "score": expected_score,
                "target": _optional_int(row.get("target")),
                "entry_executable": executable,
                "forward_return": expected_return,
                "filled": filled,
                "filled_return": expected_return if filled else None,
            }
            if dict(item) != exact:
                raise FrozenValidationAuditError(
                    f"{label} selection {rank} does not reproduce from ranking rows"
                )
            if filled:
                filled_returns.append(float(expected_return))
        filled = len(filled_returns)
        gross_return = float(sum(filled_returns)) / PORTFOLIO_SIZE
        turnover = float(filled) / PORTFOLIO_SIZE
        gross_returns.append(gross_return)
        turnovers.append(turnover)
        filled_slots += filled
        invested += int(filled > 0)
        recomputed_cycles.append(
            {
                "asof": asof_key,
                "planned_entry_at": str(cycle.get("planned_entry_at") or ""),
                "joint_capital_available_at": str(
                    cycle.get("joint_capital_available_at") or ""
                ),
                "selected_slots": len(expected),
                "filled_slots": filled,
                "cash_slots": PORTFOLIO_SIZE - filled,
                "gross_return": gross_return,
                "turnover": turnover,
            }
        )
    metrics = _portfolio_metrics(
        gross_returns,
        [],
        turnovers,
        periods_per_year=252.0 / HOLDING_TRADING_DAYS,
    )
    metrics.update(
        {
            "phase": int(phase["phase"]),
            "cycles": recomputed_cycles,
            "selected_slots": sum(item["selected_slots"] for item in recomputed_cycles),
            "filled_slots": filled_slots,
            "invested_periods": invested,
            "cash_slots": len(cycles) * PORTFOLIO_SIZE - filled_slots,
        }
    )
    return metrics


def _recompute_method_from_evidence(
    evidence: Mapping[str, Any], *, year: int, method: str
) -> dict[str, Any]:
    ranking = _ranking_frame_from_evidence(evidence, year=year)
    methods = evidence.get("methods")
    if not isinstance(methods, Mapping) or not isinstance(methods.get(method), Mapping):
        raise FrozenValidationAuditError(f"{year} evidence is missing method {method}")
    if not hmac.compare_digest(
        _require_hash(evidence.get("cycle_evidence_hash"), "cycle_evidence_hash"),
        _hash_payload(methods),
    ):
        raise FrozenValidationAuditError(f"{year} cycle evidence hash does not reproduce")
    method_evidence = methods[method]
    expected_fields = (
        ("candidate_selection_score", "candidate_metric_score")
        if method == "EVENT_QUIET"
        else ("baseline_score", "baseline_score")
    )
    if (
        method_evidence.get("selection_score_field"),
        method_evidence.get("metric_score_field"),
    ) != expected_fields:
        raise FrozenValidationAuditError(f"{year} {method} evidence score fields changed")
    phase_map = _unique_phase_map(
        {"phase_metrics": method_evidence.get("phases")},
        label=f"{year} {method} evidence",
    )
    phases = [
        _verify_phase_from_cycles(
            phase_map[phase],
            year=year,
            label=f"{year} {method} phase {phase}",
            ranking=ranking,
            score_field=expected_fields[0],
        )
        for phase in range(NON_OVERLAP_PHASES)
    ]
    ranking_metrics = _recompute_ranking_metrics_from_evidence(ranking, method=method)
    return {
        "year": int(year),
        "method": method,
        "phase_count": len(phases),
        "phase_metrics": phases,
        "min_phase_periods": min(int(item["periods"]) for item in phases),
        "min_phase_invested_periods": min(
            int(item["invested_periods"]) for item in phases
        ),
        "worst_phase_double_cost_return": min(
            float(item["double_cost_return"]) for item in phases
        ),
        "worst_phase_max_drawdown": min(float(item["max_drawdown"]) for item in phases),
        **ranking_metrics,
    }


def assess_v6_frozen_result(*, database: Database) -> dict[str, Any]:
    """Reload committed evidence and recompute every promotion metric from it."""

    assert_locked_dependencies()
    result, ledger_row = _load_committed_result_artifact(database)
    required_identity = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "project_id": PROJECT_ID,
        "lifecycle": "RESEARCH_ONLY",
        "protocol_version": PROTOCOL_VERSION,
        "protocol_hash": PROTOCOL_HASH,
        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
    }
    for field, expected in required_identity.items():
        if result.get(field) != expected:
            raise FrozenValidationAuditError(f"result.{field} is absent or changed")
    yearly = result.get("yearly")
    if not isinstance(yearly, Mapping) or set(str(key) for key in yearly) != {
        "2024",
        "2025",
    }:
        raise FrozenValidationAuditError("V6 result must contain exactly 2024 and 2025")

    sample_failures: list[str] = []
    performance_failures: list[str] = []
    combined_candidate = {phase: 0 for phase in range(NON_OVERLAP_PHASES)}
    combined_baseline = {phase: 0 for phase in range(NON_OVERLAP_PHASES)}
    recomputed_yearly: dict[str, Any] = {}
    for year in FROZEN_VALIDATION_YEARS:
        payload = yearly[str(year)]
        if not isinstance(payload, Mapping) or not isinstance(payload.get("evidence"), Mapping):
            raise FrozenValidationAuditError(f"{year} row/cycle evidence is missing")
        evidence = payload["evidence"]
        if int(evidence.get("year", 0)) != year:
            raise FrozenValidationAuditError(f"{year} evidence year is inconsistent")
        candidate = _recompute_method_from_evidence(
            evidence, year=year, method="EVENT_QUIET"
        )
        baseline = _recompute_method_from_evidence(evidence, year=year, method="RS60")
        recomputed_yearly[str(year)] = {
            "candidate": candidate,
            "baseline": baseline,
        }
        try:
            _assert_v4_pair_alignment(candidate, baseline)
        except ResearchDataBlockedError as exc:
            raise FrozenValidationAuditError(
                f"{year} evidence cycle alignment failed: {exc}"
            ) from exc
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
                sample_failures.append(f"{year} candidate phase {phase}: too few invested periods")
            if b_invested < MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR:
                sample_failures.append(f"{year} baseline phase {phase}: too few invested periods")
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
        "failure_policy": "ANY_CHANGE_REQUIRES_V7",
    }


def _v5_preregistration_disposition() -> dict[str, Any]:
    return {
        "status": V5_REJECTED_STATUS,
        "superseded_by": PROJECT_ID,
        "v5_protocol_results_immutable": True,
        "reasons": [
            "NO_FROZEN_YEAR_EVALUATION_ENTRY",
            "MANIFEST_SHARDS_NOT_CONTENT_BOUND",
            "NO_DATABASE_ATOMIC_ONE_TIME_CONSUME",
            "ASSESSMENT_NOT_BOUND_TO_SNAPSHOT_AUDIT_RESULT",
        ],
    }


class EarlyWinnerV6ResearchService:
    """Metadata/readiness facade only; no API or automatic frozen open is exposed."""

    def __init__(self, config: PlatformConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.strategy = EarlyWinnerV6Strategy()
        current = self.database.query(
            "SELECT project_id FROM research_projects WHERE project_id=?", (PROJECT_ID,)
        )
        if not current:
            self.database.upsert_research_project(
                project_id=PROJECT_ID,
                version=PROJECT_VERSION,
                name=self.strategy.metadata.name,
                description=self.strategy.metadata.description,
                status="BLOCKED_DATA",
                data_gates={
                    "preregistration": {
                        "ready": True,
                        "protocol_version": PROTOCOL_VERSION,
                        "protocol_hash": PROTOCOL_HASH,
                        "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
                        "label_schema_hash": LABEL_SCHEMA_HASH,
                        "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
                        "change_policy": "ANY_CHANGE_REQUIRES_V7",
                    }
                },
            )
        self.database.execute(
            """UPDATE research_projects
            SET version=?, name=?, description=?, category='research_project',
                lifecycle='RESEARCH_ONLY'
            WHERE project_id=?""",
            (
                PROJECT_VERSION,
                self.strategy.metadata.name,
                self.strategy.metadata.description,
                PROJECT_ID,
            ),
        )
        V6FrozenValidationLedger(self.database)

    def detail(self) -> dict[str, Any]:
        rows = self.database.query(
            "SELECT * FROM research_projects WHERE project_id=?", (PROJECT_ID,)
        )
        if not rows:
            raise KeyError(PROJECT_ID)
        project = dict(rows[0])
        try:
            stored_gates = json.loads(str(project.pop("data_gates_json", "{}")))
        except json.JSONDecodeError:
            stored_gates = {}
        raw_master = read_historical_universe_master_gate(self.config.runtime_dir)
        master = historical_universe_master_gate(raw_master, through_year=2025)
        try:
            ledger = V6FrozenValidationLedger(self.database).get()
            open_state = str(ledger["state"])
        except FrozenValidationAuditError:
            open_state = "NOT_SEALED"
        project.update(
            {
                "category": StrategyCategory.RESEARCH_PROJECT.value,
                "lifecycle": "RESEARCH_ONLY",
                "data_gates": stored_gates,
                "strategy": {
                    "strategy_id": STRATEGY_ID,
                    "version": PROJECT_VERSION,
                    "name": self.strategy.metadata.name,
                    "category": StrategyCategory.RESEARCH_PROJECT.value,
                    "lifecycle": "RESEARCH_ONLY",
                    "scan_enabled": False,
                    "backtest_enabled": False,
                },
                "protocol": PROTOCOL_SPEC,
                "protocol_hash": PROTOCOL_HASH,
                "design_years": list(DESIGN_YEARS),
                "frozen_validation_years": list(FROZEN_VALIDATION_YEARS),
                "observation_years": list(OBSERVATION_YEARS),
                "historical_universe_master": master,
                "frozen_open_state": open_state,
                "frozen_validation_opened": open_state
                in {"CONSUMING", "RESULT_COMMITTED", "FAILED_CLOSED"},
                "candidate_generation_enabled": False,
                "trade_signals_enabled": False,
                "promotion_allowed": False,
                "v5_disposition": _v5_preregistration_disposition(),
            }
        )
        return project


__all__ = [
    "DEPENDENCY_LOCK_HASH",
    "DESIGN_YEARS",
    "EVALUATOR_BUNDLE_HASH",
    "EVENT_EFFECTIVE_RULE_VERSION",
    "EVENT_RAW_REPLAY_SCHEMA_VERSION",
    "EarlyWinnerV6ResearchService",
    "EarlyWinnerV6Strategy",
    "FROZEN_VALIDATION_YEARS",
    "FrozenManifestError",
    "FrozenValidationAlreadyOpened",
    "FrozenValidationAuditError",
    "LABEL_SCHEMA_HASH",
    "LABEL_SCHEMA_SPEC",
    "LOCKED_V4_IMPLEMENTATION_HASH",
    "MANIFEST_VERSION",
    "OBSERVATION_YEARS",
    "PROJECT_ID",
    "PROTOCOL_HASH",
    "PROTOCOL_SPEC",
    "PROTOCOL_VERSION",
    "RESULT_SCHEMA_VERSION",
    "STRATEGY_ID",
    "V5_REJECTED_STATUS",
    "V6FrozenValidationLedger",
    "V6ProtocolChangeRequiresV7",
    "assess_v6_frozen_result",
    "compute_v6_result_hash",
    "evaluate_v6_frozen_frame",
    "frame_schema_hash",
    "frozen_validation_readiness",
    "prepare_v6_evaluation_frame",
    "run_v6_frozen_validation_once",
    "seal_v6_frozen_validation",
    "v6_raw_event_replay_hash",
    "validate_v6_event_provenance",
]
