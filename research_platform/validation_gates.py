"""Independent validation gates for calendar, factor-source and corporate-action evidence.

Three fail-closed gates are supported:

1. ``trading_calendar_quality_index`` -- cold-replays a registered official
   trading-calendar manifest (V2) and rebuilds the quality-gate index (V3),
   comparing the content hash against the registered expectation.
2. ``adjusted_bar_factor_source`` -- rebuilds the frozen capability assessment
   and verifies its fail-closed contract and pinned logical content hash.
3. ``corporate_action_evidence`` -- cold-replays a registered corporate-action
   evidence manifest from its content-addressed store.

Semantics: a missing reference file is reported as ``ok=False`` but never
blocks; it means the artifact has not been captured/materialized yet.  A
registered artifact that fails replay or hash comparison always reports
``blocking=True`` and must stop backtests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adjusted_bar_factor_source_assessment import (
    SOURCE_STATUS,
    build_frozen_adjusted_bar_factor_source_capability_assessment,
)
from .official_corporate_action_sources import (
    CorporateActionEvidenceBlockedError,
    replay_official_corporate_action_evidence,
)
from .official_trading_calendar_quality_adapter import (
    build_official_trading_calendar_quality_index,
)

GATES_DIRNAME = "validation_gates"
CALENDAR_REFERENCE_FILENAME = "trading_calendar_quality_index.json"
CORPORATE_ACTION_REFERENCE_FILENAME = "corporate_action_evidence.json"

# Pinned logical content hash of the frozen adjusted-bar factor source
# capability assessment.  Any deviation means the frozen audit scope drifted.
PINNED_FACTOR_ASSESSMENT_SHA256 = (
    "107d6cbd9e0b8dc0ab1f073124bc7dae2103623e52c69dbbe4997a16d10d0abe"
)


class ValidationGateBlockedError(RuntimeError):
    """Raised when a registered validation artifact fails verification."""


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    blocking: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "blocking": self.blocking,
            "detail": self.detail,
        }


def _resolve_artifact_root(value: str, repository_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(repository_root) / path


def _read_reference(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _calendar_gate(reference_path: Path, repository_root: Path) -> GateResult:
    name = "trading_calendar_quality_index"
    reference = _read_reference(reference_path)
    if reference is None:
        return GateResult(
            name=name,
            ok=False,
            blocking=False,
            detail="official calendar quality index has not been registered yet",
        )
    cas_root = reference.get("cas_root")
    manifest_sha256 = reference.get("manifest_sha256")
    if not isinstance(cas_root, str) or not isinstance(manifest_sha256, str):
        return GateResult(
            name=name,
            ok=False,
            blocking=True,
            detail="registered calendar reference is malformed",
        )
    try:
        index = build_official_trading_calendar_quality_index(
            cas_root=_resolve_artifact_root(cas_root, repository_root),
            manifest_sha256=manifest_sha256,
        )
    except Exception as exc:  # noqa: BLE001 - gate must classify any failure
        return GateResult(
            name=name,
            ok=False,
            blocking=True,
            detail=f"registered calendar index failed cold replay: {exc}",
        )
    expected = reference.get("expected_index_content_sha256")
    if isinstance(expected, str) and expected != index.content_hash:
        return GateResult(
            name=name,
            ok=False,
            blocking=True,
            detail=(
                "calendar index content hash drift: "
                f"expected {expected}, rebuilt {index.content_hash}"
            ),
        )
    return GateResult(
        name=name,
        ok=True,
        blocking=False,
        detail=f"index {index.content_hash} from manifest {manifest_sha256}",
    )


def _factor_assessment_gate() -> GateResult:
    name = "adjusted_bar_factor_source"
    try:
        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
    except Exception as exc:  # noqa: BLE001 - tampering must be classified
        return GateResult(
            name=name,
            ok=False,
            blocking=True,
            detail=f"frozen factor-source assessment failed to rebuild: {exc}",
        )
    fail_closed = (
        not artifact.ready
        and artifact.quality_rows_emitted == 0
        and artifact.quality_row_count == 0
        and not artifact.training_allowed
        and not artifact.trading_allowed
        and not artifact.promotion_allowed
    )
    if not fail_closed or artifact.logical_content_sha256 != PINNED_FACTOR_ASSESSMENT_SHA256:
        return GateResult(
            name=name,
            ok=False,
            blocking=True,
            detail="frozen factor-source assessment violated its fail-closed contract",
        )
    return GateResult(
        name=name,
        ok=True,
        blocking=False,
        detail=f"{SOURCE_STATUS}; fail-closed contract intact",
    )


def _corporate_action_gate(reference_path: Path, repository_root: Path) -> GateResult:
    name = "corporate_action_evidence"
    reference = _read_reference(reference_path)
    if reference is None:
        return GateResult(
            name=name,
            ok=False,
            blocking=False,
            detail="corporate-action evidence manifest has not been registered yet",
        )
    cas_root = reference.get("cas_root")
    manifest_sha256 = reference.get("manifest_sha256")
    if not isinstance(cas_root, str) or not isinstance(manifest_sha256, str):
        return GateResult(
            name=name,
            ok=False,
            blocking=True,
            detail="registered corporate-action reference is malformed",
        )
    try:
        replay_official_corporate_action_evidence(
            cas_root=_resolve_artifact_root(cas_root, repository_root),
            manifest_sha256=manifest_sha256,
        )
    except CorporateActionEvidenceBlockedError as exc:
        return GateResult(
            name=name,
            ok=False,
            blocking=True,
            detail=f"registered corporate-action evidence failed replay: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - gate must classify any failure
        return GateResult(
            name=name,
            ok=False,
            blocking=True,
            detail=f"registered corporate-action evidence failed replay: {exc}",
        )
    return GateResult(
        name=name,
        ok=True,
        blocking=False,
        detail=f"manifest {manifest_sha256} replays exactly from raw source bytes",
    )


def run_validation_gates(repository_root: Path) -> list[GateResult]:
    """Run every independent validation gate; never raises."""

    root = Path(repository_root)
    gates_dir = root / "data" / GATES_DIRNAME
    return [
        _calendar_gate(gates_dir / CALENDAR_REFERENCE_FILENAME, root),
        _factor_assessment_gate(),
        _corporate_action_gate(gates_dir / CORPORATE_ACTION_REFERENCE_FILENAME, root),
    ]


def ensure_backtest_allowed(repository_root: Path) -> list[GateResult]:
    """Block when any registered validation artifact fails verification.

    Missing artifacts never block; corrupted or drifted ones always do.
    Returns the full gate results so callers can report them.
    """

    results = run_validation_gates(repository_root)
    blocked = [item for item in results if not item.ok and item.blocking]
    if blocked:
        raise ValidationGateBlockedError(
            "; ".join(f"{item.name}: {item.detail}" for item in blocked)
        )
    return results


__all__ = [
    "CALENDAR_REFERENCE_FILENAME",
    "CORPORATE_ACTION_REFERENCE_FILENAME",
    "GATES_DIRNAME",
    "GateResult",
    "PINNED_FACTOR_ASSESSMENT_SHA256",
    "ValidationGateBlockedError",
    "ensure_backtest_allowed",
    "run_validation_gates",
]
