"""Auditable, fail-closed lifecycle for the US momentum programme.

This module is deliberately separate from both the research scanner and the
paper account.  It records *qualification evidence*, not orders, and therefore
cannot enable a broker.  Every transition is transactional, evidence is bound
to one immutable PIT release, and the event log is append-only.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum, StrEnum
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .us_pit.models import QUALITY_CONTRACT_REVISION

PROGRAM_ID = "us_momentum_v1"
UNIVERSE_ID = "sp500_ivv_proxy_v1"
SHA256_LENGTH = 64

_ROLLING_INPUT_ARTIFACTS = (
    "fund_holdings_observed",
    "membership_events",
    "membership_monthly",
    "security_master",
    "identifiers",
    "listing_aliases",
    "corporate_actions",
    "session_exceptions",
    "bars_raw",
    "bars_vendor_front",
    "bars_pit_signal",
    "benchmarks",
    "xnys_calendar",
    "execution_fee_schedule",
)


class USProgramState(StrEnum):
    DATA_BLOCKED = "DATA_BLOCKED"
    DATA_READY = "DATA_READY"
    BACKTEST_QUALIFIED = "BACKTEST_QUALIFIED"
    PAPER_COLLECTING = "PAPER_COLLECTING"
    PAPER_QUALIFIED = "PAPER_QUALIFIED"
    HISTORICAL_FAILED = "HISTORICAL_FAILED"
    PAPER_BLOCKED = "PAPER_BLOCKED"


class USProgramStateError(ValueError):
    """Raised when an attempted transition violates the locked sequence."""


class USProgramEvidenceError(ValueError):
    """Raised when evidence is malformed, inconsistent, or conflicts."""


_TERMINAL_STATES = frozenset(
    {
        USProgramState.PAPER_QUALIFIED,
        USProgramState.HISTORICAL_FAILED,
        USProgramState.PAPER_BLOCKED,
    }
)


def _is_sha256(value: object) -> bool:
    text = str(value)
    return len(text) == SHA256_LENGTH and all(
        character in "0123456789abcdef" for character in text
    )


def _require_sha256(value: object, field: str) -> str:
    text = str(value)
    if not _is_sha256(text):
        raise USProgramEvidenceError(
            f"{field} must be a lowercase 64-character SHA-256 digest"
        )
    return text


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise USProgramEvidenceError("qualification evidence cannot contain NaN/Infinity")
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_json_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if is_dataclass(value):
        return _json_value(asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if hasattr(value, "__dict__"):
        return _json_value(vars(value))
    raise USProgramEvidenceError(
        f"unsupported qualification evidence value: {type(value)!r}"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_membership_value(value: Any) -> Any:
    """Return a stable JSON value for one immutable membership cell."""

    if value is None:
        return None
    try:
        if bool(value != value):  # NaN/NaT, without importing numpy.
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _membership_snapshot(release: object) -> dict[str, Any]:
    """Hash a release's complete, ordered monthly-membership history."""

    loader = getattr(release, "load_frame", None)
    if not callable(loader):
        raise USProgramEvidenceError(
            "rolling paper admission requires a readable membership_monthly artifact"
        )
    try:
        frame = loader("membership_monthly")
    except Exception as exc:
        raise USProgramEvidenceError(
            "rolling paper admission cannot read membership_monthly"
        ) from exc
    required = {"universe_id", "decision_date", "security_id"}
    columns = tuple(str(item) for item in frame.columns)
    if not required.issubset(columns) or frame.empty:
        raise USProgramEvidenceError(
            "rolling paper admission requires non-empty keyed membership_monthly"
        )
    normalized: list[dict[str, Any]] = []
    keys: set[tuple[str, str]] = set()
    for source in frame.to_dict(orient="records"):
        row = {
            column: _normalize_membership_value(source.get(column))
            for column in columns
        }
        try:
            decision_date = date.fromisoformat(str(row["decision_date"])[:10])
        except (TypeError, ValueError) as exc:
            raise USProgramEvidenceError(
                "membership_monthly contains an invalid decision_date"
            ) from exc
        row["decision_date"] = decision_date.isoformat()
        security_id = str(row["security_id"] or "").strip()
        if not security_id:
            raise USProgramEvidenceError(
                "membership_monthly contains an empty security_id"
            )
        row["security_id"] = security_id
        key = (decision_date.isoformat(), security_id)
        if key in keys:
            raise USProgramEvidenceError(
                "membership_monthly contains duplicate decision/security keys"
            )
        keys.add(key)
        normalized.append(row)
    normalized.sort(
        key=lambda row: (
            str(row["decision_date"]),
            str(row["security_id"]),
            _canonical_json(row),
        )
    )
    dates = tuple(sorted({str(row["decision_date"]) for row in normalized}))
    payload = {"columns": list(columns), "rows": normalized}
    return {
        "columns": columns,
        "rows": tuple(normalized),
        "row_count": len(normalized),
        "decision_dates": dates,
        "max_decision_date": dates[-1],
        "prefix_sha256": _sha256_json(payload),
    }


def _canonical_frame_snapshot(frame: Any, *, artifact: str) -> dict[str, Any]:
    """Canonicalize a historical frame slice independent of row/dtype ordering."""

    import pandas as pd

    date_columns = {
        "as_of_date",
        "decision_date",
        "date",
        "session_date",
        "valid_from",
        "valid_to",
        "pay_date",
        "effective_from",
        "effective_to",
    }
    timestamp_columns = {
        "published_at",
        "observed_at",
        "announced_at",
        "effective_at",
        "market_open",
        "market_close",
    }

    def canonical_cell(column: str, value: Any) -> Any:
        normalized = _normalize_membership_value(value)
        if normalized is None:
            return None
        if column not in date_columns | timestamp_columns:
            return normalized
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise USProgramEvidenceError(
                f"rolling paper admission artifact {artifact} has invalid {column}"
            ) from exc
        if pd.isna(timestamp):
            return None
        if column in date_columns:
            return timestamp.date().isoformat()
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.isoformat()

    columns = tuple(sorted(str(item) for item in frame.columns))
    rows = [
        {
            column: canonical_cell(column, source.get(column))
            for column in columns
        }
        for source in frame.to_dict(orient="records")
    ]
    rows.sort(key=_canonical_json)
    payload = {
        "artifact": artifact,
        "columns": list(columns),
        "rows": rows,
    }
    return {
        "columns": columns,
        "rows": tuple(rows),
        "row_count": len(rows),
        "sha256": _sha256_json(payload),
    }


def _date_mask(
    frame: Any,
    column: str,
    cutoff: date,
    *,
    include_missing: bool = False,
) -> Any:
    """Return a strict date-at-or-before mask for one temporal column."""

    import pandas as pd

    if column not in frame.columns:
        raise USProgramEvidenceError(
            f"rolling paper admission artifact is missing {column}"
        )
    values = pd.to_datetime(frame[column], errors="coerce", utc=True)
    invalid = frame[column].notna() & values.isna()
    if bool(invalid.any()):
        raise USProgramEvidenceError(
            f"rolling paper admission artifact contains invalid {column}"
        )
    mask = values.dt.date <= cutoff
    if include_missing:
        mask = mask | frame[column].isna()
    return mask.fillna(include_missing)


def _historical_input_snapshot(
    release: object,
    *,
    cutoff: date | None = None,
    historical_security_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Hash every table that could have influenced decisions through ``cutoff``.

    Rows for securities that were not members by the old cutoff are permitted
    as warm-up data for a newly admitted month.  Global inputs (calendar,
    benchmarks and fees) and all old-member inputs remain immutable.
    """

    loader = getattr(release, "load_frame", None)
    if not callable(loader):
        raise USProgramEvidenceError(
            "rolling paper admission requires readable PIT parquet artifacts"
        )
    try:
        membership = loader("membership_monthly")
    except Exception as exc:
        raise USProgramEvidenceError(
            "rolling paper admission cannot read membership_monthly"
        ) from exc
    required_membership = {"decision_date", "security_id", "universe_id"}
    if membership.empty or not required_membership.issubset(membership.columns):
        raise USProgramEvidenceError(
            "rolling paper admission requires keyed membership_monthly"
        )
    import pandas as pd

    membership_dates = pd.to_datetime(
        membership["decision_date"], errors="coerce", utc=True
    )
    if bool(membership_dates.isna().any()):
        raise USProgramEvidenceError(
            "membership_monthly contains an invalid decision_date"
        )
    effective_cutoff = cutoff or max(membership_dates.dt.date)
    member_prefix = membership.loc[membership_dates.dt.date <= effective_cutoff].copy()
    derived_members = frozenset(
        str(item).strip() for item in member_prefix["security_id"] if str(item).strip()
    )
    if not derived_members:
        raise USProgramEvidenceError(
            "rolling paper admission historical member set is empty"
        )
    if historical_security_ids is not None and derived_members != historical_security_ids:
        raise USProgramEvidenceError(
            "candidate changed the historical membership security set"
        )
    old_members = historical_security_ids or derived_members

    frames: dict[str, Any] = {"membership_monthly": membership}
    for artifact in _ROLLING_INPUT_ARTIFACTS:
        if artifact == "membership_monthly":
            continue
        try:
            frames[artifact] = loader(artifact)
        except Exception as exc:
            raise USProgramEvidenceError(
                f"rolling paper admission cannot read required artifact {artifact}"
            ) from exc

    snapshots: dict[str, dict[str, Any]] = {}
    for artifact in _ROLLING_INPUT_ARTIFACTS:
        frame = frames[artifact]
        if artifact == "membership_monthly":
            selected = frame.loc[
                _date_mask(frame, "decision_date", effective_cutoff)
            ].copy()
        elif artifact == "bars_pit_signal":
            selected = frame.loc[
                _date_mask(frame, "decision_date", effective_cutoff)
            ].copy()
        elif artifact in {"bars_raw", "bars_vendor_front"}:
            if "security_id" not in frame.columns:
                raise USProgramEvidenceError(
                    f"rolling paper admission artifact {artifact} lacks security_id"
                )
            selected = frame.loc[
                frame["security_id"].astype(str).isin(old_members)
                & _date_mask(frame, "date", effective_cutoff)
            ].copy()
        elif artifact == "benchmarks":
            selected = frame.loc[_date_mask(frame, "date", effective_cutoff)].copy()
        elif artifact == "xnys_calendar":
            selected = frame.loc[
                _date_mask(frame, "session_date", effective_cutoff)
            ].copy()
        elif artifact == "security_master":
            if "security_id" not in frame.columns:
                raise USProgramEvidenceError(
                    "rolling paper admission security_master lacks security_id"
                )
            selected = frame.loc[
                frame["security_id"].astype(str).isin(old_members)
            ].copy()
        elif artifact in {"identifiers", "listing_aliases"}:
            if "security_id" not in frame.columns:
                raise USProgramEvidenceError(
                    f"rolling paper admission artifact {artifact} lacks security_id"
                )
            selected = frame.loc[
                frame["security_id"].astype(str).isin(old_members)
                & _date_mask(
                    frame, "valid_from", effective_cutoff, include_missing=True
                )
            ].copy()
        elif artifact in {"membership_events", "corporate_actions"}:
            announced = _date_mask(
                frame, "announced_at", effective_cutoff, include_missing=True
            )
            effective = _date_mask(
                frame, "effective_at", effective_cutoff, include_missing=True
            )
            temporal = announced | effective
            if artifact == "corporate_actions":
                if "security_id" not in frame.columns:
                    raise USProgramEvidenceError(
                        "rolling paper admission corporate_actions lacks security_id"
                    )
                temporal = temporal & frame["security_id"].astype(str).isin(
                    old_members
                )
            selected = frame.loc[temporal].copy()
        elif artifact == "session_exceptions":
            if "security_id" not in frame.columns:
                raise USProgramEvidenceError(
                    "rolling paper admission session_exceptions lacks security_id"
                )
            selected = frame.loc[
                frame["security_id"].astype(str).isin(old_members)
                & _date_mask(frame, "session_date", effective_cutoff)
            ].copy()
        elif artifact == "execution_fee_schedule":
            selected = frame.loc[
                _date_mask(
                    frame, "effective_from", effective_cutoff, include_missing=True
                )
            ].copy()
        elif artifact == "fund_holdings_observed":
            selected = frame.loc[
                _date_mask(frame, "observed_at", effective_cutoff)
            ].copy()
        else:  # pragma: no cover - the tuple and routing are deliberately closed.
            raise AssertionError(f"unrouted rolling input artifact: {artifact}")
        snapshots[artifact] = _canonical_frame_snapshot(
            selected, artifact=artifact
        )

    prefix_hashes = {
        artifact: str(snapshots[artifact]["sha256"])
        for artifact in _ROLLING_INPUT_ARTIFACTS
    }
    row_counts = {
        artifact: int(snapshots[artifact]["row_count"])
        for artifact in _ROLLING_INPUT_ARTIFACTS
    }
    member_ids_sha256 = _sha256_json(sorted(old_members))
    aggregate = _sha256_json(
        {
            "cutoff": effective_cutoff.isoformat(),
            "historical_member_ids_sha256": member_ids_sha256,
            "artifact_prefix_sha256": prefix_hashes,
            "artifact_prefix_row_counts": row_counts,
        }
    )
    return {
        "cutoff": effective_cutoff.isoformat(),
        "historical_security_ids": old_members,
        "historical_member_ids_sha256": member_ids_sha256,
        "artifact_prefix_sha256": prefix_hashes,
        "artifact_prefix_row_counts": row_counts,
        "aggregate_prefix_sha256": aggregate,
    }


def _verify_release_storage(release: object) -> tuple[object, dict[str, Any]]:
    """Verify release directory, catalog rows, and every referenced CAS object."""

    release_path_value = _value(release, "path")
    if release_path_value is None:
        raise USProgramEvidenceError(
            "rolling paper admission requires a cataloged local PIT release"
        )
    release_path = Path(release_path_value).resolve()
    release_id = _require_sha256(_value(release, "release_id"), "release_id")
    if release_path.name != release_id or release_path.parent.name != "releases":
        raise USProgramEvidenceError("PIT release path is outside the release catalog")
    root = release_path.parent.parent
    try:
        from .us_pit.store import USPITStore

        loaded = USPITStore(root).load_release(release_id)
        loaded.verify()
    except Exception as exc:
        raise USProgramEvidenceError(
            "PIT release failed manifest/catalog verification"
        ) from exc
    manifest_path = loaded.path / "manifest.json"
    manifest_sha256 = _sha256_file(manifest_path)
    try:
        with closing(sqlite3.connect(root / "catalog.sqlite3")) as connection:
            connection.row_factory = sqlite3.Row
            catalog = connection.execute(
                """SELECT release_id, manifest_sha256, status, universe_id,
                          manifest_path
                   FROM us_pit_releases WHERE release_id=?""",
                (release_id,),
            ).fetchone()
            artifact_rows = connection.execute(
                """SELECT artifact_name, object_sha256
                   FROM us_pit_release_artifacts WHERE release_id=?""",
                (release_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise USProgramEvidenceError("PIT release catalog is unreadable") from exc
    if catalog is None:
        raise USProgramEvidenceError("PIT release is absent from the local catalog")
    expected_manifest_path = f"releases/{release_id}/manifest.json"
    if (
        str(catalog["manifest_sha256"]) != manifest_sha256
        or str(catalog["status"]) != _status_value(loaded.status)
        or str(catalog["universe_id"]) != str(loaded.universe_id)
        or str(catalog["manifest_path"]) != expected_manifest_path
    ):
        raise USProgramEvidenceError("PIT release catalog binding is inconsistent")
    catalog_artifacts = {
        str(row["artifact_name"]): str(row["object_sha256"])
        for row in artifact_rows
    }
    expected_artifacts = {
        str(name): str(descriptor.object_sha256)
        for name, descriptor in loaded.manifest.artifacts.items()
    }
    if catalog_artifacts != expected_artifacts:
        raise USProgramEvidenceError("PIT artifact catalog binding is inconsistent")
    referenced = set(expected_artifacts.values()) | {
        str(source.object_sha256) for source in loaded.manifest.sources
    }
    for digest in sorted(referenced):
        if not _is_sha256(digest):
            raise USProgramEvidenceError("PIT release references an invalid CAS digest")
        object_path = root / "raw" / "sha256" / digest[:2] / digest
        if not object_path.is_file() or _sha256_file(object_path) != digest:
            raise USProgramEvidenceError(
                f"PIT release CAS object is missing or corrupt: {digest}"
            )
    membership = _membership_snapshot(loaded)
    historical_inputs = _historical_input_snapshot(loaded)
    descriptor = loaded.manifest.artifacts.get("membership_monthly")
    if descriptor is None:
        raise USProgramEvidenceError("PIT release has no membership_monthly artifact")
    return loaded, {
        "release_path": str(loaded.path.resolve()),
        "pit_root": str(root.resolve()),
        "membership_artifact_sha256": str(descriptor.object_sha256),
        "membership_prefix_sha256": str(membership["prefix_sha256"]),
        "membership_row_count": int(membership["row_count"]),
        "membership_max_decision_date": str(membership["max_decision_date"]),
        "historical_input_cutoff": str(historical_inputs["cutoff"]),
        "historical_member_ids_sha256": str(
            historical_inputs["historical_member_ids_sha256"]
        ),
        "historical_input_prefix_sha256": dict(
            historical_inputs["artifact_prefix_sha256"]
        ),
        "historical_input_prefix_row_counts": dict(
            historical_inputs["artifact_prefix_row_counts"]
        ),
        "historical_input_aggregate_sha256": str(
            historical_inputs["aggregate_prefix_sha256"]
        ),
    }


def _value(source: object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _status_value(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _decision_payload(
    decision: object,
    *,
    decision_type: str,
) -> dict[str, Any]:
    qualified = _value(decision, "qualified")
    status = _value(decision, "status")
    gates = _value(decision, "gates")
    failures = _value(decision, "failures")
    if type(qualified) is not bool:  # bool is intentional; integers are rejected.
        raise USProgramEvidenceError(f"{decision_type} decision.qualified must be bool")
    if status is None:
        raise USProgramEvidenceError(f"{decision_type} decision.status is required")
    status_text = _status_value(status)
    if not isinstance(gates, Mapping) or not gates:
        raise USProgramEvidenceError(f"{decision_type} decision.gates must be non-empty")
    normalized_gates: dict[str, bool] = {}
    for name, passed in gates.items():
        if type(passed) is not bool:
            raise USProgramEvidenceError(
                f"{decision_type} gate {name!r} must be bool"
            )
        normalized_gates[str(name)] = passed
    if not isinstance(failures, (tuple, list)):
        raise USProgramEvidenceError(
            f"{decision_type} decision.failures must be a tuple/list"
        )
    normalized_failures = tuple(str(item) for item in failures)
    false_gates = tuple(name for name, passed in normalized_gates.items() if not passed)
    if len(normalized_failures) != len(set(normalized_failures)):
        raise USProgramEvidenceError(f"{decision_type} failures contain duplicates")
    if set(normalized_failures) != set(false_gates):
        raise USProgramEvidenceError(
            f"{decision_type} failures must exactly match failed gates"
        )
    if qualified != (not false_gates):
        raise USProgramEvidenceError(
            f"{decision_type} qualified flag conflicts with gate results"
        )

    allowed: set[str]
    expected_qualified: str
    if decision_type == "historical":
        allowed = {"BACKTEST_QUALIFIED", "HISTORICAL_FAILED"}
        expected_qualified = "BACKTEST_QUALIFIED"
    elif decision_type == "tdx":
        allowed = {"TDX_QUALIFIED", "PAPER_BLOCKED"}
        expected_qualified = "TDX_QUALIFIED"
    elif decision_type == "paper":
        allowed = {"PAPER_QUALIFIED", "PAPER_COLLECTING", "PAPER_BLOCKED"}
        expected_qualified = "PAPER_QUALIFIED"
    else:  # pragma: no cover - private call sites use a closed set.
        raise AssertionError(f"unknown decision type: {decision_type}")
    if status_text not in allowed:
        raise USProgramEvidenceError(
            f"invalid {decision_type} decision status: {status_text}"
        )
    if qualified and status_text != expected_qualified:
        raise USProgramEvidenceError(
            f"qualified {decision_type} decision must have status {expected_qualified}"
        )
    if not qualified and status_text == expected_qualified:
        raise USProgramEvidenceError(
            f"unqualified {decision_type} decision cannot have status {status_text}"
        )
    if decision_type == "historical" and not qualified and status_text != "HISTORICAL_FAILED":
        raise USProgramEvidenceError(
            "unqualified historical decision must have status HISTORICAL_FAILED"
        )
    if decision_type == "tdx" and not qualified and status_text != "PAPER_BLOCKED":
        raise USProgramEvidenceError(
            "unqualified TDX decision must have status PAPER_BLOCKED"
        )

    payload = {
        "qualified": qualified,
        "status": status_text,
        "gates": normalized_gates,
        "failures": list(normalized_failures),
    }
    metrics = _value(decision, "metrics")
    if metrics is not None:
        if not isinstance(metrics, Mapping):
            raise USProgramEvidenceError(f"{decision_type} decision.metrics must be a mapping")
        payload["metrics"] = _json_value(metrics)
    return payload


class USMomentumProgram:
    """SQLite-backed lifecycle gate for ``us_momentum_v1``.

    There is deliberately no method, flag, or database column that can enable
    real-broker writes.  ``broker_writes_enabled`` is a read-only class
    invariant and is included in every status response.
    """

    __slots__ = ("database_path", "program_id")

    @property
    def broker_writes_enabled(self) -> bool:
        return False

    @property
    def real_broker_order_entrypoints(self) -> bool:
        return False

    def __init__(self, database_path: Path | str, *, program_id: str = PROGRAM_ID) -> None:
        self.database_path = Path(database_path)
        self.program_id = str(program_id).strip()
        if self.program_id != PROGRAM_ID:
            raise ValueError(f"only {PROGRAM_ID} is supported")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        states = ",".join(f"'{item.value}'" for item in USProgramState)
        with closing(self._connect()) as connection:
            connection.executescript(
                f"""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS us_program_state (
                    program_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN ({states})),
                    release_id TEXT,
                    manifest_sha256 TEXT,
                    data_evidence_sha256 TEXT,
                    data_payload_json TEXT,
                    historical_evidence_sha256 TEXT,
                    historical_payload_json TEXT,
                    tdx_evidence_sha256 TEXT,
                    tdx_payload_json TEXT,
                    paper_evidence_sha256 TEXT,
                    paper_payload_json TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    CHECK(release_id IS NULL OR length(release_id) = 64),
                    CHECK(manifest_sha256 IS NULL OR length(manifest_sha256) = 64)
                );
                CREATE TABLE IF NOT EXISTS us_program_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE CHECK(length(event_key) = 64),
                    program_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_state TEXT NOT NULL CHECK(from_state IN ({states})),
                    to_state TEXT NOT NULL CHECK(to_state IN ({states})),
                    release_id TEXT NOT NULL CHECK(length(release_id) = 64),
                    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64),
                    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256) = 64),
                    evidence_type TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    FOREIGN KEY(program_id) REFERENCES us_program_state(program_id)
                );
                CREATE INDEX IF NOT EXISTS idx_us_program_events_program
                    ON us_program_events(program_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_us_program_events_evidence
                    ON us_program_events(program_id, evidence_type, evidence_sha256);
                CREATE TABLE IF NOT EXISTS us_program_paper_release_admissions (
                    admission_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    admission_key TEXT NOT NULL UNIQUE CHECK(length(admission_key) = 64),
                    program_id TEXT NOT NULL,
                    admission_type TEXT NOT NULL
                        CHECK(admission_type IN ('BASE', 'ROLL_FORWARD')),
                    old_release_id TEXT,
                    old_manifest_sha256 TEXT,
                    release_id TEXT NOT NULL CHECK(length(release_id) = 64),
                    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64),
                    old_membership_artifact_sha256 TEXT,
                    membership_artifact_sha256 TEXT,
                    membership_prefix_sha256 TEXT,
                    old_max_decision_date TEXT,
                    max_decision_date TEXT,
                    old_row_count INTEGER,
                    row_count INTEGER,
                    release_path TEXT,
                    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                    payload_json TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    FOREIGN KEY(program_id) REFERENCES us_program_state(program_id),
                    CHECK(old_release_id IS NULL OR length(old_release_id) = 64),
                    CHECK(old_manifest_sha256 IS NULL OR length(old_manifest_sha256) = 64),
                    CHECK(old_membership_artifact_sha256 IS NULL OR length(old_membership_artifact_sha256) = 64),
                    CHECK(membership_artifact_sha256 IS NULL OR length(membership_artifact_sha256) = 64),
                    CHECK(membership_prefix_sha256 IS NULL OR length(membership_prefix_sha256) = 64)
                );
                CREATE INDEX IF NOT EXISTS idx_us_program_paper_release_head
                    ON us_program_paper_release_admissions(program_id, admission_seq);
                CREATE TRIGGER IF NOT EXISTS us_program_events_no_update
                BEFORE UPDATE ON us_program_events
                BEGIN
                    SELECT RAISE(ABORT, 'us_program_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS us_program_events_no_delete
                BEFORE DELETE ON us_program_events
                BEGIN
                    SELECT RAISE(ABORT, 'us_program_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS us_program_paper_release_admissions_no_update
                BEFORE UPDATE ON us_program_paper_release_admissions
                BEGIN
                    SELECT RAISE(ABORT, 'us_program_paper_release_admissions is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS us_program_paper_release_admissions_no_delete
                BEFORE DELETE ON us_program_paper_release_admissions
                BEGIN
                    SELECT RAISE(ABORT, 'us_program_paper_release_admissions is append-only');
                END;
                """
            )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT OR IGNORE INTO us_program_state(program_id, state, updated_at)
                VALUES (?, ?, ?)
                """,
                (self.program_id, USProgramState.DATA_BLOCKED.value, now),
            )
            connection.commit()

    @staticmethod
    def _row_state(row: sqlite3.Row) -> USProgramState:
        try:
            return USProgramState(str(row["state"]))
        except ValueError as exc:
            raise USProgramStateError("stored US programme state is invalid") from exc

    def _load_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM us_program_state WHERE program_id = ?",
            (self.program_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - protected by initialization.
            raise USProgramStateError("US programme state row is missing")
        state = self._row_state(row)
        audit = connection.execute(
            """
            SELECT COUNT(*) AS event_count,
                   (SELECT to_state FROM us_program_events
                    WHERE program_id = ? ORDER BY event_id DESC LIMIT 1) AS audit_state
            FROM us_program_events WHERE program_id = ?
            """,
            (self.program_id, self.program_id),
        ).fetchone()
        event_count = int(audit["event_count"])
        if int(row["version"]) != event_count:
            raise USProgramStateError("programme state version does not match its audit log")
        if event_count == 0:
            if state != USProgramState.DATA_BLOCKED:
                raise USProgramStateError(
                    "programme state was changed without qualification evidence"
                )
        elif str(audit["audit_state"]) != state.value:
            raise USProgramStateError(
                "programme state does not match the append-only audit head"
            )
        return row

    @staticmethod
    def _decode_payload(value: object) -> dict[str, Any] | None:
        if value is None:
            return None
        decoded = json.loads(str(value))
        if not isinstance(decoded, dict):
            raise USProgramEvidenceError("stored programme evidence is corrupt")
        return decoded

    def _paper_admission_rows(
        self, connection: sqlite3.Connection
    ) -> list[sqlite3.Row]:
        rows = connection.execute(
            """SELECT * FROM us_program_paper_release_admissions
               WHERE program_id=? ORDER BY admission_seq""",
            (self.program_id,),
        ).fetchall()
        previous: sqlite3.Row | None = None
        for expected_seq, row in enumerate(rows, start=1):
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict) or _sha256_json(payload) != row["payload_sha256"]:
                raise USProgramEvidenceError("paper release admission audit is corrupt")
            release_id = str(row["release_id"])
            manifest_sha256 = str(row["manifest_sha256"])
            if (
                int(row["admission_seq"]) != expected_seq
                or not _is_sha256(str(row["admission_key"]))
                or not _is_sha256(release_id)
                or not _is_sha256(manifest_sha256)
                or payload.get("admission_type") != row["admission_type"]
                or payload.get("release_id") != release_id
                or payload.get("manifest_sha256") != manifest_sha256
            ):
                raise USProgramEvidenceError("paper release admission identity is corrupt")
            if previous is None:
                if str(row["admission_type"]) != "BASE" or row["old_release_id"] is not None:
                    raise USProgramEvidenceError("paper release admission base is invalid")
                expected_key = _sha256_json(
                    {
                        "program_id": self.program_id,
                        "admission_type": "BASE",
                        "release_id": release_id,
                        "manifest_sha256": manifest_sha256,
                    }
                )
            else:
                if str(row["admission_type"]) != "ROLL_FORWARD":
                    raise USProgramEvidenceError("paper release admission chain is invalid")
                if (
                    str(row["old_release_id"]) != str(previous["release_id"])
                    or str(row["old_manifest_sha256"])
                    != str(previous["manifest_sha256"])
                ):
                    raise USProgramEvidenceError("paper release admission chain was forked")
                prefix_sha256 = str(row["membership_prefix_sha256"] or "")
                if not _is_sha256(prefix_sha256):
                    raise USProgramEvidenceError(
                        "paper release admission prefix hash is invalid"
                    )
                expected_key = _sha256_json(
                    {
                        "program_id": self.program_id,
                        "old_release_id": str(row["old_release_id"]),
                        "release_id": release_id,
                        "manifest_sha256": manifest_sha256,
                        "membership_prefix_sha256": prefix_sha256,
                    }
                )
                if (
                    payload.get("old_release_id") != str(row["old_release_id"])
                    or payload.get("old_manifest_sha256")
                    != str(row["old_manifest_sha256"])
                    or payload.get("membership_prefix_sha256") != prefix_sha256
                ):
                    raise USProgramEvidenceError(
                        "paper release admission payload conflicts with audit columns"
                    )
            if str(row["admission_key"]) != expected_key:
                raise USProgramEvidenceError("paper release admission key is corrupt")
            previous = row
        return list(rows)

    @staticmethod
    def _admission_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in row.keys()
            if key not in {"program_id", "payload_json"}
        } | {"payload": json.loads(str(row["payload_json"]))}

    def _status_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        state = self._row_state(row)
        historical = self._decode_payload(row["historical_payload_json"])
        tdx = self._decode_payload(row["tdx_payload_json"])
        paper = self._decode_payload(row["paper_payload_json"])
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM us_program_events WHERE program_id = ?",
                (self.program_id,),
            ).fetchone()[0]
        )
        admissions = self._paper_admission_rows(connection)
        admission = admissions[-1] if admissions else None
        if state in {USProgramState.PAPER_COLLECTING, USProgramState.PAPER_QUALIFIED}:
            if admission is None:
                raise USProgramStateError(
                    "paper programme has no admitted PIT decision release"
                )
        return {
            "program_id": self.program_id,
            "universe_id": UNIVERSE_ID,
            "state": state.value,
            "release_id": row["release_id"],
            "manifest_sha256": row["manifest_sha256"],
            "paper_decision_release_id": (
                admission["release_id"] if admission is not None else row["release_id"]
            ),
            "paper_decision_manifest_sha256": (
                admission["manifest_sha256"]
                if admission is not None
                else row["manifest_sha256"]
            ),
            "paper_release_admission_count": len(admissions),
            "paper_release_admission": (
                self._admission_dict(admission) if admission is not None else None
            ),
            "data_evidence_sha256": row["data_evidence_sha256"],
            "historical_evidence_sha256": row["historical_evidence_sha256"],
            "tdx_evidence_sha256": row["tdx_evidence_sha256"],
            "paper_evidence_sha256": row["paper_evidence_sha256"],
            "data_ready": state != USProgramState.DATA_BLOCKED
            and row["data_payload_json"] is not None
            and bool(self._decode_payload(row["data_payload_json"])["ready"]),
            "historical_qualified": bool(
                historical is not None and historical["decision"]["qualified"]
            ),
            "tdx_qualified": bool(tdx is not None and tdx["decision"]["qualified"]),
            "paper_qualified": bool(
                paper is not None and paper["decision"]["qualified"]
            ),
            "broker_writes_enabled": False,
            "real_broker_order_entrypoints": False,
            "execution_mode": "PAPER_ONLY",
            "terminal": state in _TERMINAL_STATES,
            "version": int(row["version"]),
            "event_count": event_count,
            "updated_at": str(row["updated_at"]),
        }

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = self._load_row(connection)
            return self._status_from_row(connection, row)

    def events(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_key, action, from_state, to_state,
                       release_id, manifest_sha256, evidence_sha256,
                       evidence_type, payload_sha256, payload_json, occurred_at
                FROM us_program_events
                WHERE program_id = ? ORDER BY event_id
                """,
                (self.program_id,),
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "payload_json"},
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def paper_release_admissions(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            self._load_row(connection)
            return [
                self._admission_dict(row)
                for row in self._paper_admission_rows(connection)
            ]

    def _require_release_binding(
        self,
        row: sqlite3.Row,
        release_id: str,
        manifest_sha256: str,
    ) -> tuple[str, str]:
        release_digest = _require_sha256(release_id, "release_id")
        manifest_digest = _require_sha256(manifest_sha256, "manifest_sha256")
        if (
            row["release_id"] != release_digest
            or row["manifest_sha256"] != manifest_digest
        ):
            raise USProgramEvidenceError(
                "qualification evidence does not match the active PIT release/manifest"
            )
        return release_digest, manifest_digest

    def _existing_event(
        self,
        connection: sqlite3.Connection,
        *,
        evidence_type: str,
        evidence_sha256: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM us_program_events
            WHERE program_id = ? AND evidence_type = ? AND evidence_sha256 = ?
            ORDER BY event_id DESC LIMIT 1
            """,
            (self.program_id, evidence_type, evidence_sha256),
        ).fetchone()

    def _idempotent_or_conflict(
        self,
        connection: sqlite3.Connection,
        *,
        evidence_type: str,
        evidence_sha256: str,
        release_id: str,
        manifest_sha256: str,
        payload_json: str,
    ) -> bool:
        existing = self._existing_event(
            connection,
            evidence_type=evidence_type,
            evidence_sha256=evidence_sha256,
        )
        if existing is None:
            return False
        same = (
            existing["release_id"] == release_id
            and existing["manifest_sha256"] == manifest_sha256
            and existing["payload_json"] == payload_json
        )
        if not same:
            raise USProgramEvidenceError(
                f"conflicting {evidence_type} payload for an existing evidence hash"
            )
        return True

    def _write_transition(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        action: str,
        to_state: USProgramState,
        release_id: str,
        manifest_sha256: str,
        evidence_type: str,
        evidence_sha256: str,
        payload: Mapping[str, Any],
        state_columns: Mapping[str, object],
    ) -> dict[str, Any]:
        payload_json = _canonical_json(payload)
        if self._idempotent_or_conflict(
            connection,
            evidence_type=evidence_type,
            evidence_sha256=evidence_sha256,
            release_id=release_id,
            manifest_sha256=manifest_sha256,
            payload_json=payload_json,
        ):
            return self._status_from_row(connection, self._load_row(connection))

        now = datetime.now(timezone.utc).isoformat()
        from_state = self._row_state(row)
        payload_sha256 = _sha256_json(payload)
        event_key = _sha256_json(
            {
                "program_id": self.program_id,
                "action": action,
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
                "evidence_type": evidence_type,
                "evidence_sha256": evidence_sha256,
            }
        )
        connection.execute(
            """
            INSERT INTO us_program_events(
                event_key, program_id, action, from_state, to_state,
                release_id, manifest_sha256, evidence_sha256, evidence_type,
                payload_sha256, payload_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                self.program_id,
                action,
                from_state.value,
                to_state.value,
                release_id,
                manifest_sha256,
                evidence_sha256,
                evidence_type,
                payload_sha256,
                payload_json,
                now,
            ),
        )
        assignments = ["state = ?", "version = version + 1", "updated_at = ?"]
        values: list[object] = [to_state.value, now]
        for column, value in state_columns.items():
            if column not in {
                "release_id",
                "manifest_sha256",
                "data_evidence_sha256",
                "data_payload_json",
                "historical_evidence_sha256",
                "historical_payload_json",
                "tdx_evidence_sha256",
                "tdx_payload_json",
                "paper_evidence_sha256",
                "paper_payload_json",
            }:
                raise AssertionError(f"unsafe state column: {column}")
            assignments.append(f"{column} = ?")
            values.append(value)
        values.append(self.program_id)
        connection.execute(
            f"UPDATE us_program_state SET {', '.join(assignments)} WHERE program_id = ?",
            values,
        )
        return self._status_from_row(connection, self._load_row(connection))

    @staticmethod
    def _release_facts(
        release: object,
        supplied_manifest_sha256: str | None,
    ) -> tuple[str, str, str, dict[str, Any]]:
        verifier = getattr(release, "verify", None)
        if callable(verifier):
            verifier()
        release_id = _require_sha256(_value(release, "release_id"), "release_id")
        universe = str(_value(release, "universe_id", UNIVERSE_ID))
        if universe != UNIVERSE_ID:
            raise USProgramEvidenceError(
                f"US momentum release universe must be {UNIVERSE_ID}"
            )
        status = _status_value(_value(release, "status"))
        if status not in {"DATA_READY", "DATA_BLOCKED"}:
            raise USProgramEvidenceError(f"invalid PIT release status: {status}")

        manifest_sha256 = supplied_manifest_sha256
        release_path = _value(release, "path")
        if release_path is not None:
            manifest_path = Path(release_path) / "manifest.json"
            if not manifest_path.is_file():
                raise USProgramEvidenceError("PIT release manifest.json is missing")
            actual_manifest_sha256 = _sha256_file(manifest_path)
            if manifest_sha256 is not None and manifest_sha256 != actual_manifest_sha256:
                raise USProgramEvidenceError("supplied PIT manifest hash does not match manifest.json")
            manifest_sha256 = actual_manifest_sha256
        if manifest_sha256 is None:
            manifest_sha256 = _value(release, "manifest_sha256")
        manifest_digest = _require_sha256(manifest_sha256, "manifest_sha256")

        quality = _value(release, "quality_report")
        if callable(quality):
            quality = quality()
        if quality is not None:
            quality_status = _status_value(_value(quality, "status"))
            if quality_status != status:
                raise USProgramEvidenceError(
                    "PIT manifest status conflicts with its quality report"
                )
            hard_failures = _value(quality, "hard_failures", ())
            if status == "DATA_READY" and hard_failures:
                raise USProgramEvidenceError(
                    "DATA_READY release still contains Critical/High quality failures"
                )
            metrics = _value(quality, "metrics", {}) or {}
            if status == "DATA_READY" and int(
                metrics.get("quality_contract_revision", -1)
            ) != QUALITY_CONTRACT_REVISION:
                raise USProgramEvidenceError(
                    "DATA_READY release uses an obsolete PIT quality contract"
                )
        elif status == "DATA_READY":
            raise USProgramEvidenceError(
                "DATA_READY registration requires the derived quality report"
            )
        facts = {
            "release_id": release_id,
            "manifest_sha256": manifest_digest,
            "universe_id": universe,
            "status": status,
            "ready": status == "DATA_READY",
        }
        if status == "DATA_READY" and release_path is not None:
            verified_release, storage_facts = _verify_release_storage(release)
            verified_manifest = _sha256_file(Path(_value(verified_release, "path")) / "manifest.json")
            if verified_manifest != manifest_digest:
                raise USProgramEvidenceError(
                    "verified PIT manifest hash changed during registration"
                )
            facts.update(storage_facts)
        return release_id, manifest_digest, status, facts

    def register_data_release(
        self,
        release: object,
        *,
        manifest_sha256: str | None = None,
    ) -> dict[str, Any]:
        release_id, manifest_digest, status, payload = self._release_facts(
            release, manifest_sha256
        )
        evidence_sha256 = manifest_digest
        payload_json = _canonical_json(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._load_row(connection)
            if self._idempotent_or_conflict(
                connection,
                evidence_type="data_release",
                evidence_sha256=evidence_sha256,
                release_id=release_id,
                manifest_sha256=manifest_digest,
                payload_json=payload_json,
            ):
                result = self._status_from_row(connection, row)
                connection.commit()
                return result
            current = self._row_state(row)
            if current != USProgramState.DATA_BLOCKED:
                raise USProgramStateError(
                    f"data release can only be registered from DATA_BLOCKED, not {current.value}"
                )
            target = (
                USProgramState.DATA_READY
                if status == "DATA_READY"
                else USProgramState.DATA_BLOCKED
            )
            result = self._write_transition(
                connection,
                row,
                action="REGISTER_DATA_RELEASE",
                to_state=target,
                release_id=release_id,
                manifest_sha256=manifest_digest,
                evidence_type="data_release",
                evidence_sha256=evidence_sha256,
                payload=payload,
                state_columns={
                    "release_id": release_id,
                    "manifest_sha256": manifest_digest,
                    "data_evidence_sha256": evidence_sha256,
                    "data_payload_json": payload_json,
                    "historical_evidence_sha256": None,
                    "historical_payload_json": None,
                    "tdx_evidence_sha256": None,
                    "tdx_payload_json": None,
                    "paper_evidence_sha256": None,
                    "paper_payload_json": None,
                },
            )
            connection.commit()
            return result

    def register_historical(
        self,
        result: object,
        evidence_hash: str,
        *,
        release_id: str,
        manifest_sha256: str,
    ) -> dict[str, Any]:
        evidence_sha256 = _require_sha256(evidence_hash, "historical evidence_hash")
        decision = _value(result, "decision", result)
        decision_payload = _decision_payload(decision, decision_type="historical")
        freeze_sha256 = _value(result, "freeze_sha256")
        if freeze_sha256 is not None:
            _require_sha256(freeze_sha256, "historical freeze_sha256")
        run_hashes = _value(result, "run_sha256")
        if run_hashes is not None:
            if not isinstance(run_hashes, Mapping) or not run_hashes:
                raise USProgramEvidenceError("historical run_sha256 must be non-empty")
            for name, digest in run_hashes.items():
                _require_sha256(digest, f"historical run_sha256[{name}]")
        payload = {
            "decision": decision_payload,
            "result_sha256": _sha256_json(result),
        }
        payload_json = _canonical_json(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._load_row(connection)
            release_digest, manifest_digest = self._require_release_binding(
                row, release_id, manifest_sha256
            )
            if self._idempotent_or_conflict(
                connection,
                evidence_type="historical",
                evidence_sha256=evidence_sha256,
                release_id=release_digest,
                manifest_sha256=manifest_digest,
                payload_json=payload_json,
            ):
                output = self._status_from_row(connection, row)
                connection.commit()
                return output
            current = self._row_state(row)
            if current != USProgramState.DATA_READY:
                raise USProgramStateError(
                    f"historical evidence requires DATA_READY, not {current.value}"
                )
            target = (
                USProgramState.BACKTEST_QUALIFIED
                if decision_payload["qualified"]
                else USProgramState.HISTORICAL_FAILED
            )
            output = self._write_transition(
                connection,
                row,
                action="REGISTER_HISTORICAL_DECISION",
                to_state=target,
                release_id=release_digest,
                manifest_sha256=manifest_digest,
                evidence_type="historical",
                evidence_sha256=evidence_sha256,
                payload=payload,
                state_columns={
                    "historical_evidence_sha256": evidence_sha256,
                    "historical_payload_json": payload_json,
                },
            )
            connection.commit()
            return output

    def register_tdx(
        self,
        decision: object,
        evidence_hash: str,
        *,
        release_id: str,
        manifest_sha256: str,
    ) -> dict[str, Any]:
        evidence_sha256 = _require_sha256(evidence_hash, "TDX evidence_hash")
        decision_payload = _decision_payload(decision, decision_type="tdx")
        payload = {"decision": decision_payload}
        payload_json = _canonical_json(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._load_row(connection)
            release_digest, manifest_digest = self._require_release_binding(
                row, release_id, manifest_sha256
            )
            if self._idempotent_or_conflict(
                connection,
                evidence_type="tdx",
                evidence_sha256=evidence_sha256,
                release_id=release_digest,
                manifest_sha256=manifest_digest,
                payload_json=payload_json,
            ):
                output = self._status_from_row(connection, row)
                connection.commit()
                return output
            current = self._row_state(row)
            if current != USProgramState.BACKTEST_QUALIFIED:
                raise USProgramStateError(
                    f"TDX qualification requires BACKTEST_QUALIFIED, not {current.value}"
                )
            historical = self._decode_payload(row["historical_payload_json"])
            if historical is None or not historical["decision"]["qualified"]:
                raise USProgramStateError("historical qualification evidence is missing")
            target = (
                USProgramState.BACKTEST_QUALIFIED
                if decision_payload["qualified"]
                else USProgramState.PAPER_BLOCKED
            )
            output = self._write_transition(
                connection,
                row,
                action="REGISTER_TDX_DECISION",
                to_state=target,
                release_id=release_digest,
                manifest_sha256=manifest_digest,
                evidence_type="tdx",
                evidence_sha256=evidence_sha256,
                payload=payload,
                state_columns={
                    "tdx_evidence_sha256": evidence_sha256,
                    "tdx_payload_json": payload_json,
                },
            )
            connection.commit()
            return output

    def _ensure_base_paper_admission(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> None:
        existing = self._paper_admission_rows(connection)
        if existing:
            head = existing[-1]
            if (
                str(head["release_id"]) != str(row["release_id"])
                or str(head["manifest_sha256"]) != str(row["manifest_sha256"])
            ):
                raise USProgramEvidenceError(
                    "paper release admission base conflicts with qualified release"
                )
            return
        data = self._decode_payload(row["data_payload_json"]) or {}
        payload = {
            "admission_type": "BASE",
            "release_id": str(row["release_id"]),
            "manifest_sha256": str(row["manifest_sha256"]),
            "membership_artifact_sha256": data.get(
                "membership_artifact_sha256"
            ),
            "membership_prefix_sha256": data.get("membership_prefix_sha256"),
            "membership_row_count": data.get("membership_row_count"),
            "membership_max_decision_date": data.get(
                "membership_max_decision_date"
            ),
            "historical_input_cutoff": data.get("historical_input_cutoff"),
            "historical_member_ids_sha256": data.get(
                "historical_member_ids_sha256"
            ),
            "historical_input_prefix_sha256": data.get(
                "historical_input_prefix_sha256"
            ),
            "historical_input_prefix_row_counts": data.get(
                "historical_input_prefix_row_counts"
            ),
            "historical_input_aggregate_sha256": data.get(
                "historical_input_aggregate_sha256"
            ),
            "admitted_historical_input_cutoff": data.get(
                "historical_input_cutoff"
            ),
            "admitted_historical_member_ids_sha256": data.get(
                "historical_member_ids_sha256"
            ),
            "admitted_historical_input_prefix_sha256": data.get(
                "historical_input_prefix_sha256"
            ),
            "admitted_historical_input_prefix_row_counts": data.get(
                "historical_input_prefix_row_counts"
            ),
            "admitted_historical_input_aggregate_sha256": data.get(
                "historical_input_aggregate_sha256"
            ),
        }
        payload_sha256 = _sha256_json(payload)
        admission_key = _sha256_json(
            {
                "program_id": self.program_id,
                "admission_type": "BASE",
                "release_id": row["release_id"],
                "manifest_sha256": row["manifest_sha256"],
            }
        )
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """INSERT INTO us_program_paper_release_admissions(
                   admission_key, program_id, admission_type,
                   release_id, manifest_sha256,
                   membership_artifact_sha256, membership_prefix_sha256,
                   max_decision_date, row_count, release_path,
                   payload_sha256, payload_json, admitted_at
               ) VALUES (?, ?, 'BASE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                admission_key,
                self.program_id,
                row["release_id"],
                row["manifest_sha256"],
                data.get("membership_artifact_sha256"),
                data.get("membership_prefix_sha256"),
                data.get("membership_max_decision_date"),
                data.get("membership_row_count"),
                data.get("release_path"),
                payload_sha256,
                _canonical_json(payload),
                now,
            ),
        )

    def admit_paper_release(
        self,
        release: object,
        *,
        manifest_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Append a verified PIT release to the paper decision lineage.

        The historical qualification remains bound to the original release.
        Only monthly decision data may roll forward, and the complete prior
        ``membership_monthly`` history must remain a byte-equivalent canonical
        row prefix.  No wall-clock/latest-release selection is involved.
        """

        candidate_id, candidate_manifest, candidate_status, candidate_facts = (
            self._release_facts(release, manifest_sha256)
        )
        if candidate_status != "DATA_READY":
            raise USProgramEvidenceError(
                "paper release admission requires a DATA_READY candidate"
            )
        candidate_loaded, candidate_storage = _verify_release_storage(release)
        if candidate_storage["membership_prefix_sha256"] != candidate_facts.get(
            "membership_prefix_sha256"
        ):
            raise USProgramEvidenceError("candidate membership verification is unstable")

        with closing(self._connect()) as connection:
            row = self._load_row(connection)
            current_state = self._row_state(row)
            if current_state not in {
                USProgramState.PAPER_COLLECTING,
                USProgramState.PAPER_QUALIFIED,
            }:
                raise USProgramStateError(
                    "paper release admission requires PAPER_COLLECTING or "
                    f"PAPER_QUALIFIED, not {current_state.value}"
                )
            admissions = self._paper_admission_rows(connection)
            if not admissions:
                raise USProgramEvidenceError("paper release admission base is missing")
            head = admissions[-1]
            if str(head["release_id"]) == candidate_id:
                if str(head["manifest_sha256"]) != candidate_manifest:
                    raise USProgramEvidenceError(
                        "candidate release identity conflicts with admitted manifest"
                    )
                return self._status_from_row(connection, row)
            old_path_value = head["release_path"]
            if not old_path_value:
                raise USProgramEvidenceError(
                    "qualified base release lacks verifiable membership lineage"
                )
            candidate_root = Path(candidate_storage["pit_root"]).resolve()
            old_path = Path(str(old_path_value)).resolve()
            if old_path.parent.parent != candidate_root:
                raise USProgramEvidenceError(
                    "candidate and admitted releases belong to different PIT catalogs"
                )
            try:
                from .us_pit.store import USPITStore

                old_release = USPITStore(candidate_root).load_release(
                    str(head["release_id"])
                )
            except Exception as exc:
                raise USProgramEvidenceError(
                    "current admitted release cannot be reloaded from the PIT catalog"
                ) from exc
            old_loaded, old_storage = _verify_release_storage(old_release)
            actual_old_manifest = _sha256_file(old_loaded.path / "manifest.json")
            if actual_old_manifest != str(head["manifest_sha256"]):
                raise USProgramEvidenceError(
                    "current admitted release manifest no longer matches its audit"
                )
            old_membership = _membership_snapshot(old_loaded)
            new_membership = _membership_snapshot(candidate_loaded)
            if old_membership["columns"] != new_membership["columns"]:
                raise USProgramEvidenceError(
                    "candidate membership_monthly schema changed"
                )
            if head["membership_artifact_sha256"] and (
                str(head["membership_artifact_sha256"])
                != str(old_storage["membership_artifact_sha256"])
            ):
                raise USProgramEvidenceError(
                    "current admitted membership artifact no longer matches its audit"
                )
            old_dates = tuple(old_membership["decision_dates"])
            new_dates = tuple(new_membership["decision_dates"])
            if len(new_dates) <= len(old_dates) or new_dates[: len(old_dates)] != old_dates:
                raise USProgramEvidenceError(
                    "candidate must append at least one later certified membership month"
                )
            old_max = str(old_membership["max_decision_date"])
            old_rows = tuple(old_membership["rows"])
            candidate_prefix_rows = tuple(
                item
                for item in new_membership["rows"]
                if str(item["decision_date"]) <= old_max
            )
            prefix_payload = {
                "columns": list(old_membership["columns"]),
                "rows": list(candidate_prefix_rows),
            }
            prefix_sha256 = _sha256_json(prefix_payload)
            if candidate_prefix_rows != old_rows or prefix_sha256 != str(
                old_membership["prefix_sha256"]
            ):
                raise USProgramEvidenceError(
                    "candidate modified previously admitted membership rows"
                )
            old_inputs = _historical_input_snapshot(
                old_loaded,
                cutoff=date.fromisoformat(old_max),
            )
            candidate_prefix_inputs = _historical_input_snapshot(
                candidate_loaded,
                cutoff=date.fromisoformat(old_max),
                historical_security_ids=frozenset(
                    old_inputs["historical_security_ids"]
                ),
            )
            old_prefix_hashes = dict(old_inputs["artifact_prefix_sha256"])
            candidate_prefix_hashes = dict(
                candidate_prefix_inputs["artifact_prefix_sha256"]
            )
            old_prefix_counts = dict(old_inputs["artifact_prefix_row_counts"])
            candidate_prefix_counts = dict(
                candidate_prefix_inputs["artifact_prefix_row_counts"]
            )
            changed_inputs = [
                artifact
                for artifact in _ROLLING_INPUT_ARTIFACTS
                if (
                    old_prefix_hashes[artifact]
                    != candidate_prefix_hashes[artifact]
                    or old_prefix_counts[artifact]
                    != candidate_prefix_counts[artifact]
                )
            ]
            if changed_inputs:
                raise USProgramEvidenceError(
                    "candidate modified previously admitted historical inputs: "
                    + ", ".join(changed_inputs)
                )
            if (
                str(old_inputs["aggregate_prefix_sha256"])
                != str(candidate_prefix_inputs["aggregate_prefix_sha256"])
            ):
                raise USProgramEvidenceError(
                    "candidate historical input aggregate prefix changed"
                )
            head_payload = json.loads(str(head["payload_json"]))
            expected_old_aggregate = head_payload.get(
                "admitted_historical_input_aggregate_sha256"
            ) or head_payload.get("historical_input_aggregate_sha256")
            if (
                expected_old_aggregate is not None
                and str(expected_old_aggregate)
                != str(old_inputs["aggregate_prefix_sha256"])
            ):
                raise USProgramEvidenceError(
                    "current admitted historical inputs no longer match their audit"
                )
            candidate_full_inputs = _historical_input_snapshot(candidate_loaded)
            payload = {
                "admission_type": "ROLL_FORWARD",
                "old_release_id": str(head["release_id"]),
                "old_manifest_sha256": str(head["manifest_sha256"]),
                "release_id": candidate_id,
                "manifest_sha256": candidate_manifest,
                "old_membership_artifact_sha256": old_storage[
                    "membership_artifact_sha256"
                ],
                "membership_artifact_sha256": candidate_storage[
                    "membership_artifact_sha256"
                ],
                "membership_prefix_sha256": prefix_sha256,
                "old_max_decision_date": old_max,
                "max_decision_date": str(new_membership["max_decision_date"]),
                "old_row_count": int(old_membership["row_count"]),
                "row_count": int(new_membership["row_count"]),
                "new_decision_dates": list(new_dates[len(old_dates) :]),
                "historical_input_cutoff": old_max,
                "old_historical_member_ids_sha256": old_inputs[
                    "historical_member_ids_sha256"
                ],
                "candidate_historical_member_ids_sha256": candidate_prefix_inputs[
                    "historical_member_ids_sha256"
                ],
                "old_historical_input_prefix_sha256": old_prefix_hashes,
                "candidate_historical_input_prefix_sha256": candidate_prefix_hashes,
                "old_historical_input_prefix_row_counts": old_prefix_counts,
                "candidate_historical_input_prefix_row_counts": candidate_prefix_counts,
                "old_historical_input_aggregate_sha256": old_inputs[
                    "aggregate_prefix_sha256"
                ],
                "candidate_historical_input_aggregate_sha256": (
                    candidate_prefix_inputs["aggregate_prefix_sha256"]
                ),
                "admitted_historical_input_cutoff": candidate_full_inputs[
                    "cutoff"
                ],
                "admitted_historical_member_ids_sha256": candidate_full_inputs[
                    "historical_member_ids_sha256"
                ],
                "admitted_historical_input_prefix_sha256": dict(
                    candidate_full_inputs["artifact_prefix_sha256"]
                ),
                "admitted_historical_input_prefix_row_counts": dict(
                    candidate_full_inputs["artifact_prefix_row_counts"]
                ),
                "admitted_historical_input_aggregate_sha256": candidate_full_inputs[
                    "aggregate_prefix_sha256"
                ],
                "catalog_verified": True,
                "manifest_verified": True,
                "cas_verified": True,
            }
            admission_key = _sha256_json(
                {
                    "program_id": self.program_id,
                    "old_release_id": payload["old_release_id"],
                    "release_id": candidate_id,
                    "manifest_sha256": candidate_manifest,
                    "membership_prefix_sha256": prefix_sha256,
                }
            )
            payload_sha256 = _sha256_json(payload)

            connection.execute("BEGIN IMMEDIATE")
            locked_row = self._load_row(connection)
            locked_admissions = self._paper_admission_rows(connection)
            locked_head = locked_admissions[-1] if locked_admissions else None
            if (
                locked_head is None
                or str(locked_head["release_id"]) != str(head["release_id"])
                or str(locked_head["manifest_sha256"])
                != str(head["manifest_sha256"])
            ):
                raise USProgramEvidenceError(
                    "paper release admission head changed during verification"
                )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """INSERT INTO us_program_paper_release_admissions(
                       admission_key, program_id, admission_type,
                       old_release_id, old_manifest_sha256,
                       release_id, manifest_sha256,
                       old_membership_artifact_sha256,
                       membership_artifact_sha256, membership_prefix_sha256,
                       old_max_decision_date, max_decision_date,
                       old_row_count, row_count, release_path,
                       payload_sha256, payload_json, admitted_at
                   ) VALUES (?, ?, 'ROLL_FORWARD', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    admission_key,
                    self.program_id,
                    payload["old_release_id"],
                    payload["old_manifest_sha256"],
                    candidate_id,
                    candidate_manifest,
                    payload["old_membership_artifact_sha256"],
                    payload["membership_artifact_sha256"],
                    prefix_sha256,
                    old_max,
                    payload["max_decision_date"],
                    payload["old_row_count"],
                    payload["row_count"],
                    candidate_storage["release_path"],
                    payload_sha256,
                    _canonical_json(payload),
                    now,
                ),
            )
            result = self._status_from_row(connection, locked_row)
            connection.commit()
            return result

    def start_paper_collection(
        self,
        *,
        release_id: str,
        manifest_sha256: str,
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._load_row(connection)
            release_digest, manifest_digest = self._require_release_binding(
                row, release_id, manifest_sha256
            )
            data = self._decode_payload(row["data_payload_json"])
            historical = self._decode_payload(row["historical_payload_json"])
            tdx = self._decode_payload(row["tdx_payload_json"])
            if not (
                data is not None
                and data["ready"]
                and historical is not None
                and historical["decision"]["qualified"]
                and tdx is not None
                and tdx["decision"]["qualified"]
            ):
                raise USProgramStateError(
                    "PAPER_COLLECTING requires DATA_READY, historical qualification, "
                    "and TDX qualification"
                )
            evidence_sha256 = _sha256_json(
                {
                    "data": row["data_evidence_sha256"],
                    "historical": row["historical_evidence_sha256"],
                    "tdx": row["tdx_evidence_sha256"],
                }
            )
            payload = {
                "data_evidence_sha256": row["data_evidence_sha256"],
                "historical_evidence_sha256": row["historical_evidence_sha256"],
                "tdx_evidence_sha256": row["tdx_evidence_sha256"],
            }
            payload_json = _canonical_json(payload)
            if self._idempotent_or_conflict(
                connection,
                evidence_type="paper_start",
                evidence_sha256=evidence_sha256,
                release_id=release_digest,
                manifest_sha256=manifest_digest,
                payload_json=payload_json,
            ):
                output = self._status_from_row(connection, row)
                connection.commit()
                return output
            current = self._row_state(row)
            if current != USProgramState.BACKTEST_QUALIFIED:
                raise USProgramStateError(
                    f"paper collection requires BACKTEST_QUALIFIED, not {current.value}"
                )
            self._ensure_base_paper_admission(connection, row)
            output = self._write_transition(
                connection,
                row,
                action="START_PAPER_COLLECTION",
                to_state=USProgramState.PAPER_COLLECTING,
                release_id=release_digest,
                manifest_sha256=manifest_digest,
                evidence_type="paper_start",
                evidence_sha256=evidence_sha256,
                payload=payload,
                state_columns={},
            )
            connection.commit()
            return output

    def register_paper(
        self,
        decision: object,
        evidence_hash: str,
        *,
        release_id: str,
        manifest_sha256: str,
    ) -> dict[str, Any]:
        evidence_sha256 = _require_sha256(evidence_hash, "paper evidence_hash")
        decision_payload = _decision_payload(decision, decision_type="paper")
        payload = {"decision": decision_payload}
        payload_json = _canonical_json(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._load_row(connection)
            release_digest, manifest_digest = self._require_release_binding(
                row, release_id, manifest_sha256
            )
            if self._idempotent_or_conflict(
                connection,
                evidence_type="paper",
                evidence_sha256=evidence_sha256,
                release_id=release_digest,
                manifest_sha256=manifest_digest,
                payload_json=payload_json,
            ):
                output = self._status_from_row(connection, row)
                connection.commit()
                return output
            current = self._row_state(row)
            if current != USProgramState.PAPER_COLLECTING:
                raise USProgramStateError(
                    f"paper qualification requires PAPER_COLLECTING, not {current.value}"
                )
            if decision_payload["qualified"]:
                target = USProgramState.PAPER_QUALIFIED
            elif decision_payload["status"] == "PAPER_BLOCKED":
                target = USProgramState.PAPER_BLOCKED
            else:
                target = USProgramState.PAPER_COLLECTING
            output = self._write_transition(
                connection,
                row,
                action="REGISTER_PAPER_DECISION",
                to_state=target,
                release_id=release_digest,
                manifest_sha256=manifest_digest,
                evidence_type="paper",
                evidence_sha256=evidence_sha256,
                payload=payload,
                state_columns={
                    "paper_evidence_sha256": evidence_sha256,
                    "paper_payload_json": payload_json,
                },
            )
            connection.commit()
            return output


__all__ = [
    "PROGRAM_ID",
    "UNIVERSE_ID",
    "USMomentumProgram",
    "USProgramEvidenceError",
    "USProgramState",
    "USProgramStateError",
]
