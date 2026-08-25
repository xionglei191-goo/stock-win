"""Build paper-qualification evidence from the durable paper ledgers.

The pure :class:`~research_platform.us_qualification.PaperQualificationTracker`
is intentionally easy to unit test, but its dataclasses must never be populated
from operator-entered totals in production.  This module is the production
boundary: it opens the isolated paper and runtime SQLite databases read-only,
replays cash and positions from fills, derives daily equity from accepted raw
marks, and creates all replay hashes itself.

The builder is fail closed.  Missing BIL observations, missing held-security
marks/quotes, late observations, data-degraded sessions, kill events, calendar
conflicts, broken content hashes, or a replay/account mismatch force a
``PAPER_BLOCKED`` decision.  Merely not having reached 252 sessions, 12 cycles,
or 20 closed trades remains ``PAPER_COLLECTING`` when the evidence seen so far
is internally sound.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from .us_qualification import (
    PaperCycleEvidence,
    PaperQualificationDecision,
    PaperQualificationTracker,
    PaperSessionEvidence,
    PaperTradeEvidence,
)
from .us_paper_runtime import (
    DAILY_SOURCE_FREQUENCY,
    DAILY_SOURCE_SCHEMA,
    canonical_daily_source_sha256,
)
from .us_pit.hashing import sha256_file
from .us_pit.models import ReleaseManifest, ReleaseStatus, SourceRole, UNIVERSE_ID
from .us_pit.store import USPITRelease


NY_TZ = ZoneInfo("America/New_York")
BIL_BENCHMARK_CODE = "BILTR.US"
BIL_RAW_CODE = "BIL.US"
SHA256_LENGTH = 64
PAPER_TABLES = (
    "us_paper_account",
    "us_paper_periods",
    "us_paper_orders",
    "us_paper_observations",
    "us_paper_positions",
    "us_paper_fills",
    "us_paper_sessions",
    "us_paper_events",
    "us_paper_corporate_actions",
    "us_paper_receivables",
    "us_paper_cash_ledger",
)
RUNTIME_TABLES = (
    "us_paper_runtime_state",
    "us_paper_runtime_sessions",
    "us_paper_runtime_quotes",
    "us_paper_runtime_events",
)


class USPaperQualificationEvidenceError(RuntimeError):
    """The persistent evidence stores cannot be opened or interpreted."""


@dataclass(frozen=True)
class USPaperQualificationEvidence:
    """An immutable result directly consumable by ``register_paper``."""

    decision: PaperQualificationDecision
    evidence_sha256: str
    snapshot_sha256: str
    calendar_sha256: str
    release_id: str
    manifest_sha256: str
    release_lineage: Mapping[str, str]
    session_evidence: tuple[PaperSessionEvidence, ...]
    cycle_evidence: tuple[PaperCycleEvidence, ...]
    trade_evidence: tuple[PaperTradeEvidence, ...]
    integrity_failures: tuple[str, ...]

    def register(
        self,
        program: Any,
    ) -> Mapping[str, Any]:
        """Register exactly this derived decision and its persistent release binding."""

        return program.register_paper(
            self.decision,
            self.evidence_sha256,
            release_id=self.release_id,
            manifest_sha256=self.manifest_sha256,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": asdict(self.decision),
            "evidence_sha256": self.evidence_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "calendar_sha256": self.calendar_sha256,
            "release_id": self.release_id,
            "manifest_sha256": self.manifest_sha256,
            "release_lineage": dict(self.release_lineage),
            "session_evidence": [asdict(item) for item in self.session_evidence],
            "cycle_evidence": [asdict(item) for item in self.cycle_evidence],
            "trade_evidence": [asdict(item) for item in self.trade_evidence],
            "integrity_failures": list(self.integrity_failures),
            "derived_from_persistent_ledgers": True,
            "benchmark": BIL_BENCHMARK_CODE,
        }


@dataclass(frozen=True)
class _Snapshot:
    paper: Mapping[str, tuple[dict[str, Any], ...]]
    runtime: Mapping[str, tuple[dict[str, Any], ...]]
    sha256: str


@dataclass(frozen=True)
class _Replay:
    sessions: tuple[PaperSessionEvidence, ...]
    trades: tuple[PaperTradeEvidence, ...]
    output_hashes: Mapping[date, str]
    failures: tuple[str, ...]
    final_cash: float
    final_positions: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class _CorporateActionReleaseEvidence:
    """Verified corporate-action evidence scoped to one immutable release."""

    manifest_sha256: str
    source_ids_by_digest: Mapping[str, frozenset[str]]
    artifact_rows: tuple[Mapping[str, Any], ...]


class USPaperQualificationEvidenceBuilder:
    """Derive paper gates from two read-only SQLite ledgers and a frozen calendar."""

    def __init__(
        self,
        *,
        paper_database_path: Path,
        runtime_database_path: Path,
        frozen_xnys_sessions: Iterable[object],
        us_pit_root: Path | None = None,
        decision_archive_root: Path | None = None,
    ) -> None:
        self.paper_database_path = Path(paper_database_path)
        self.runtime_database_path = Path(runtime_database_path)
        self.us_pit_root = None if us_pit_root is None else Path(us_pit_root)
        self.decision_archive_root = (
            None if decision_archive_root is None else Path(decision_archive_root)
        )
        sessions = tuple(_day(item) for item in frozen_xnys_sessions)
        if not sessions or tuple(sorted(set(sessions))) != sessions:
            raise ValueError("frozen XNYS sessions must be unique and increasing")
        self.sessions = sessions
        self.session_set = frozenset(sessions)
        self.calendar_sha256 = _sha256([item.isoformat() for item in sessions])
        self._corporate_action_evidence_by_release: dict[
            str, _CorporateActionReleaseEvidence
        ] = {}

    def build(self) -> USPaperQualificationEvidence:
        snapshot = self._snapshot()
        failures: list[str] = []
        paper = snapshot.paper
        runtime = snapshot.runtime

        account = _one(paper["us_paper_account"], "paper account", failures)
        runtime_state = _one(runtime["us_paper_runtime_state"], "runtime state", failures)
        if account:
            if str(account.get("mode")) != "PAPER" or str(account.get("strategy_id")) != "us_momentum_v1":
                failures.append("INVALID_PAPER_ACCOUNT_IDENTITY")
            status = str(account.get("status") or "")
            if status == "KILLED" or account.get("killed_at") or account.get("kill_reason"):
                failures.append("KILL_SWITCH_PRESENT")
            if status != "RUNNING" or str(account.get("degraded_reason") or ""):
                failures.append("PAPER_ACCOUNT_NOT_HEALTHY")
        if runtime_state:
            if str(runtime_state.get("mode")) != "PAPER":
                failures.append("INVALID_RUNTIME_MODE")
            if str(runtime_state.get("calendar_hash") or "") != self.calendar_sha256:
                failures.append("FROZEN_CALENDAR_HASH_MISMATCH")
            release_id = str(runtime_state.get("release_id") or "")
            manifest_sha256 = str(runtime_state.get("manifest_sha256") or "")
            if not _is_sha256(release_id):
                failures.append("INVALID_RUNTIME_RELEASE_ID")
            if not _is_sha256(manifest_sha256):
                failures.append("INVALID_RUNTIME_MANIFEST_SHA256")
            expected_calendar = {
                "calendar": "XNYS",
                "sessions": [item.isoformat() for item in self.sessions],
                "source_hash": self.calendar_sha256,
                "frozen": True,
            }
            try:
                stored_calendar = json.loads(str(runtime_state.get("calendar_json") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                stored_calendar = None
            if stored_calendar != expected_calendar:
                failures.append("FROZEN_CALENDAR_PAYLOAD_MISMATCH")
            if str(runtime_state.get("status") or "") in {
                "KILLED",
                "PAPER_BLOCKED",
                "DATA_DEGRADED",
            } or str(runtime_state.get("blocked_reason") or ""):
                failures.append("RUNTIME_NOT_HEALTHY")

        release_lineage = self._validate_release_and_identity(
            paper, runtime_state, failures
        )
        completed_days = self._completed_days(paper, runtime, failures)
        self._validate_events(paper, runtime, failures)
        self._validate_corporate_actions(paper, completed_days, failures)
        self._validate_observations(paper["us_paper_observations"], completed_days, failures)
        self._validate_runtime_quotes(runtime["us_paper_runtime_quotes"], completed_days, failures)
        self._validate_orders_and_fills(paper, completed_days, failures)

        replay_a = self._replay(snapshot, completed_days)
        replay_b = self._replay(snapshot, completed_days)
        failures.extend(replay_a.failures)
        failures.extend(replay_b.failures)
        if replay_a.output_hashes != replay_b.output_hashes:
            failures.append("NON_DETERMINISTIC_SESSION_REPLAY")

        session_evidence = tuple(
            replace(item, replay_output_sha256=replay_b.output_hashes.get(item.session, ""))
            for item in replay_a.sessions
        )
        cycles = self._cycles(paper, completed_days, failures)
        self._validate_decision_replays(paper, cycles, failures)
        self._validate_final_account(snapshot, completed_days, replay_a, failures)

        tracker = PaperQualificationTracker(self.sessions)
        decision = tracker.evaluate(session_evidence, cycles, replay_a.trades)
        unique_failures = tuple(sorted(set(failures)))
        if unique_failures:
            gates = dict(decision.gates)
            gates["persistent_integrity"] = False
            decision = replace(
                decision,
                qualified=False,
                status="PAPER_BLOCKED",
                gates=gates,
                # The lifecycle state machine requires failures to name
                # exactly the false gates. Detailed row-level diagnostics
                # remain in integrity_failures and the hashed evidence body.
                failures=tuple(name for name, passed in gates.items() if not passed),
                metrics={
                    **dict(decision.metrics),
                    "persistent_integrity_failure_count": len(unique_failures),
                },
            )
        else:
            gates = dict(decision.gates)
            gates["persistent_integrity"] = True
            decision = replace(
                decision,
                # An initialized ledger with no completed session is waiting
                # for evidence, not corrupt evidence.  Once a session closes,
                # a missing BIL/raw/quote row is detected above and blocks.
                status="PAPER_COLLECTING" if not completed_days else decision.status,
                gates=gates,
            )

        payload = {
            "schema": "us-paper-qualification-evidence-v1",
            "snapshot_sha256": snapshot.sha256,
            "calendar_sha256": self.calendar_sha256,
            "release_id": str(runtime_state.get("release_id") or ""),
            "manifest_sha256": str(runtime_state.get("manifest_sha256") or ""),
            "release_lineage": release_lineage,
            "benchmark": BIL_BENCHMARK_CODE,
            "sessions": [asdict(item) for item in session_evidence],
            "cycles": [asdict(item) for item in cycles],
            "trades": [asdict(item) for item in replay_a.trades],
            "integrity_failures": unique_failures,
            "decision": asdict(decision),
        }
        return USPaperQualificationEvidence(
            decision=decision,
            evidence_sha256=_sha256(payload),
            snapshot_sha256=snapshot.sha256,
            calendar_sha256=self.calendar_sha256,
            release_id=str(runtime_state.get("release_id") or ""),
            manifest_sha256=str(runtime_state.get("manifest_sha256") or ""),
            release_lineage=release_lineage,
            session_evidence=session_evidence,
            cycle_evidence=cycles,
            trade_evidence=replay_a.trades,
            integrity_failures=unique_failures,
        )

    def _snapshot(self) -> _Snapshot:
        paper = self._read_database(self.paper_database_path, PAPER_TABLES)
        runtime = self._read_database(self.runtime_database_path, RUNTIME_TABLES)
        canonical = {
            "paper": paper,
            "runtime": runtime,
        }
        return _Snapshot(paper=paper, runtime=runtime, sha256=_sha256(canonical))

    @staticmethod
    def _read_database(
        path: Path,
        required_tables: Sequence[str],
    ) -> Mapping[str, tuple[dict[str, Any], ...]]:
        if not path.is_file():
            raise USPaperQualificationEvidenceError(f"evidence database does not exist: {path}")
        uri = path.resolve().as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            existing = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = sorted(set(required_tables) - existing)
            if missing:
                raise USPaperQualificationEvidenceError(
                    f"evidence database {path} is missing tables: {', '.join(missing)}"
                )
            result: dict[str, tuple[dict[str, Any], ...]] = {}
            for table in required_tables:
                # Table names are constants owned by this module.
                rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                result[table] = tuple(sorted(rows, key=_canonical_row_key))
            connection.rollback()
            return result
        except USPaperQualificationEvidenceError:
            raise
        except sqlite3.Error as exc:
            raise USPaperQualificationEvidenceError(
                f"cannot read evidence database {path}: {exc}"
            ) from exc
        finally:
            if "connection" in locals():
                connection.close()

    def _completed_days(
        self,
        paper: Mapping[str, tuple[dict[str, Any], ...]],
        runtime: Mapping[str, tuple[dict[str, Any], ...]],
        failures: list[str],
    ) -> tuple[date, ...]:
        paper_sessions = {_date(row.get("session_date")): row for row in paper["us_paper_sessions"]}
        runtime_sessions = {
            _date(row.get("session_date")): row for row in runtime["us_paper_runtime_sessions"]
        }
        for day in set(paper_sessions) | set(runtime_sessions):
            if day not in self.session_set:
                failures.append(f"SESSION_OUTSIDE_FROZEN_CALENDAR:{day.isoformat()}")
        closed = sorted(
            day for day, row in paper_sessions.items() if row.get("closed_at")
        )
        if not closed:
            return ()
        start, end = min(set(paper_sessions) | set(runtime_sessions)), closed[-1]
        expected = tuple(day for day in self.sessions if start <= day <= end)
        for day in expected:
            paper_row = paper_sessions.get(day)
            runtime_row = runtime_sessions.get(day)
            if paper_row is None:
                failures.append(f"MISSING_PAPER_SESSION:{day.isoformat()}")
                continue
            if runtime_row is None:
                failures.append(f"MISSING_RUNTIME_SESSION:{day.isoformat()}")
                continue
            if str(paper_row.get("state")) != "SESSION_CLOSED" or not paper_row.get("closed_at"):
                failures.append(f"PAPER_SESSION_NOT_CLOSED:{day.isoformat()}")
            if str(paper_row.get("degraded_reason") or ""):
                failures.append(f"PAPER_SESSION_DEGRADED:{day.isoformat()}")
            if str(runtime_row.get("calendar_hash") or "") != self.calendar_sha256:
                failures.append(f"RUNTIME_SESSION_CALENDAR_MISMATCH:{day.isoformat()}")
            if int(runtime_row.get("buy_blocked") or 0) or str(runtime_row.get("block_reason") or ""):
                failures.append(f"RUNTIME_SESSION_BLOCKED:{day.isoformat()}")
        return expected

    def _validate_release_and_identity(
        self,
        paper: Mapping[str, tuple[dict[str, Any], ...]],
        runtime_state: Mapping[str, Any],
        failures: list[str],
    ) -> Mapping[str, str]:
        release_id = str(runtime_state.get("release_id") or "")
        manifest_sha256 = str(runtime_state.get("manifest_sha256") or "")
        runtime_binding = (release_id, manifest_sha256)
        evidence_exists = any(
            paper[name]
            for name in (
                "us_paper_periods",
                "us_paper_orders",
                "us_paper_fills",
                "us_paper_positions",
                "us_paper_corporate_actions",
            )
        )
        latest_period = max(
            paper["us_paper_periods"],
            key=lambda row: (str(row.get("decision_at")), str(row.get("period_id"))),
            default=None,
        )
        for account in paper["us_paper_account"]:
            account_release = str(account.get("pit_release_id") or "")
            account_manifest = str(account.get("manifest_sha256") or "")
            if latest_period is not None and (
                account_release != str(latest_period.get("pit_release_id") or "")
                or account_manifest != str(latest_period.get("manifest_sha256") or "")
            ):
                failures.append("PAPER_ACCOUNT_RELEASE_BINDING_MISMATCH")
        release_bindings: set[tuple[str, str]] = set()
        periods = {
            str(row.get("period_id")): row for row in paper["us_paper_periods"]
        }
        orders = {
            str(row.get("order_id")): row for row in paper["us_paper_orders"]
        }
        for table in (
            "us_paper_periods",
            "us_paper_orders",
            "us_paper_positions",
            "us_paper_fills",
            "us_paper_corporate_actions",
        ):
            for row in paper[table]:
                identity = str(
                    row.get("period_id")
                    or row.get("order_id")
                    or row.get("fill_id")
                    or row.get("security_id")
                    or row.get("action_id")
                    or "unknown"
                )
                binding = (
                    str(row.get("pit_release_id") or ""),
                    str(row.get("manifest_sha256") or ""),
                )
                release_bindings.add(binding)
                if not all(_is_sha256(value) for value in binding):
                    failures.append(f"INVALID_ROW_RELEASE_BINDING:{table}:{identity}")
                if table != "us_paper_periods":
                    security_id = str(row.get("security_id") or "")
                    if not security_id.startswith("us_") or security_id.lower().endswith(".us"):
                        failures.append(f"UNSTABLE_SECURITY_ID:{table}:{identity}")
                if table == "us_paper_orders" and row.get("period_id"):
                    period = periods.get(str(row.get("period_id")))
                    if period is None or binding != (
                        str(period.get("pit_release_id") or ""),
                        str(period.get("manifest_sha256") or ""),
                    ):
                        failures.append(f"ORDER_PERIOD_LINEAGE_MISMATCH:{identity}")
                if table == "us_paper_fills":
                    order = orders.get(str(row.get("order_id")))
                    if order is None or binding != (
                        str(order.get("pit_release_id") or ""),
                        str(order.get("manifest_sha256") or ""),
                    ):
                        failures.append(f"FILL_ORDER_LINEAGE_MISMATCH:{identity}")
        for row in paper["us_paper_observations"]:
            security_id = row.get("security_id")
            if security_id is None:
                continue
            identity = str(row.get("observation_id") or "unknown")
            stable_id = str(security_id)
            if not stable_id.startswith("us_") or stable_id.lower().endswith(".us"):
                failures.append(f"UNSTABLE_SECURITY_ID:us_paper_observations:{identity}")
            binding = (
                str(row.get("pit_release_id") or ""),
                str(row.get("manifest_sha256") or ""),
            )
            release_bindings.add(binding)
            if not all(_is_sha256(value) for value in binding):
                failures.append(
                    f"INVALID_ROW_RELEASE_BINDING:us_paper_observations:{identity}"
                )
        if evidence_exists:
            release_bindings.add(runtime_binding)
        return self._verify_release_lineage(release_bindings, failures)

    def _verify_release_lineage(
        self,
        bindings: set[tuple[str, str]],
        failures: list[str],
    ) -> Mapping[str, str]:
        """Verify every rolling period release against the immutable PIT catalog."""

        if not bindings:
            self._corporate_action_evidence_by_release = {}
            return {}
        self._corporate_action_evidence_by_release = {}
        if self.us_pit_root is None:
            failures.append("PIT_RELEASE_LINEAGE_STORE_REQUIRED")
            return {}
        root = self.us_pit_root.resolve()
        catalog_path = root / "catalog.sqlite3"
        if not catalog_path.is_file():
            failures.append("PIT_RELEASE_CATALOG_MISSING")
            return {}
        catalog_rows: dict[str, Mapping[str, Any]] = {}
        try:
            uri = catalog_path.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=10.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                for row in connection.execute(
                    """SELECT release_id, manifest_sha256, status, universe_id,
                    manifest_path FROM us_pit_releases"""
                ):
                    catalog_rows[str(row["release_id"])] = dict(row)
        except sqlite3.Error:
            failures.append("PIT_RELEASE_CATALOG_UNREADABLE")
            return {}

        verified: dict[str, str] = {}
        for release_id, expected_manifest in sorted(bindings):
            if not _is_sha256(release_id) or not _is_sha256(expected_manifest):
                failures.append(f"INVALID_PIT_RELEASE_BINDING:{release_id}")
                continue
            row = catalog_rows.get(release_id)
            if row is None:
                failures.append(f"PIT_RELEASE_NOT_CATALOGED:{release_id}")
                continue
            if (
                str(row.get("manifest_sha256")) != expected_manifest
                or str(row.get("status")) != ReleaseStatus.DATA_READY.value
                or str(row.get("universe_id")) != UNIVERSE_ID
            ):
                failures.append(f"PIT_RELEASE_CATALOG_MISMATCH:{release_id}")
                continue
            manifest_path = root / str(row.get("manifest_path"))
            try:
                if manifest_path.resolve().parent != (root / "releases" / release_id).resolve():
                    raise ValueError("unsafe PIT manifest catalog path")
                if not manifest_path.is_file() or sha256_file(manifest_path) != expected_manifest:
                    raise ValueError("PIT manifest hash mismatch")
                manifest = ReleaseManifest.from_dict(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                )
                release = USPITRelease(manifest_path.parent, manifest)
                release.verify()
                if release.release_id != release_id or release.status != ReleaseStatus.DATA_READY:
                    raise ValueError("PIT release identity/status mismatch")
                source_ids_by_digest: dict[str, set[str]] = {}
                for source in manifest.sources:
                    if (
                        source.dataset == "corporate_actions"
                        and source.role == SourceRole.SIGNAL_INPUT
                    ):
                        source_object = (
                            root
                            / "raw"
                            / "sha256"
                            / source.object_sha256[:2]
                            / source.object_sha256
                        )
                        if (
                            not source_object.is_file()
                            or sha256_file(source_object) != source.object_sha256
                        ):
                            raise ValueError(
                                "PIT corporate-action source object is missing or corrupt"
                            )
                        source_ids_by_digest.setdefault(
                            source.object_sha256.lower(), set()
                        ).add(source.source_id)
                action_descriptor = manifest.artifacts.get("corporate_actions")
                if action_descriptor is None:
                    action_rows: tuple[Mapping[str, Any], ...] = ()
                else:
                    action_rows = tuple(
                        dict(item)
                        for item in release.load_frame("corporate_actions").to_dict(
                            orient="records"
                        )
                    )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                failures.append(f"PIT_RELEASE_VERIFICATION_FAILED:{release_id}")
                continue
            verified[release_id] = expected_manifest
            self._corporate_action_evidence_by_release[release_id] = (
                _CorporateActionReleaseEvidence(
                    manifest_sha256=expected_manifest,
                    source_ids_by_digest={
                        digest: frozenset(source_ids)
                        for digest, source_ids in source_ids_by_digest.items()
                    },
                    artifact_rows=action_rows,
                )
            )
        return verified

    @staticmethod
    def _validate_events(
        paper: Mapping[str, tuple[dict[str, Any], ...]],
        runtime: Mapping[str, tuple[dict[str, Any], ...]],
        failures: list[str],
    ) -> None:
        fills = {
            str(row.get("fill_id") or ""): row for row in paper["us_paper_fills"]
        }
        observations = {
            str(row.get("observation_id") or ""): row
            for row in paper["us_paper_observations"]
        }
        for row in paper["us_paper_events"]:
            event_type = str(row.get("event_type") or "")
            if event_type == "INTRADAY_STOP_EXECUTED" and _valid_paper_stop_event(
                row, fills, observations
            ):
                continue
            if event_type in {"DATA_DEGRADED", "KILL_SWITCH"} or str(
                row.get("severity")
            ).upper() in {"HIGH", "CRITICAL"}:
                failures.append(f"PAPER_RISK_EVENT:{row.get('event_type')}")
        for row in runtime["us_paper_runtime_events"]:
            event_type = str(row.get("event_type") or "")
            if event_type == "INTRADAY_STOP_BREACH" and _valid_runtime_stop_event(
                row, fills
            ):
                continue
            if str(row.get("severity")).upper() in {"HIGH", "CRITICAL"} or event_type in {
                "RUNTIME_KILL",
                "INTRADAY_STOP_BREACH",
                "TQ_PREFLIGHT_BLOCKED",
            }:
                failures.append(f"RUNTIME_RISK_EVENT:{row.get('event_type')}")

    def _validate_observations(
        self,
        rows: Sequence[Mapping[str, Any]],
        completed_days: Sequence[date],
        failures: list[str],
    ) -> None:
        day_set = set(completed_days)
        grains: set[tuple[str, date, str]] = set()
        for row in rows:
            day = _date(row.get("session_date"))
            if day not in day_set:
                continue
            code = str(row.get("code") or "")
            kind = str(row.get("kind") or "")
            grain = (code, day, kind)
            if grain in grains:
                failures.append(f"DUPLICATE_OBSERVATION:{code}:{day}:{kind}")
            grains.add(grain)
            canonical = {
                "security_id": row.get("security_id"),
                "code": code,
                "pit_release_id": row.get("pit_release_id"),
                "manifest_sha256": row.get("manifest_sha256"),
                "session_date": day.isoformat(),
                "kind": kind,
                "event_at": str(row.get("event_at")),
                "available_at": str(row.get("available_at")),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
            }
            try:
                payload = json.loads(str(row.get("payload_json") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, Mapping):
                for field in (
                    "source_schema",
                    "source",
                    "source_code",
                    "frequency",
                    "adjustment",
                    "source_rows",
                    "source_sha256",
                ):
                    if field in payload:
                        canonical[field] = payload[field]
            if str(row.get("content_hash") or "") != _sha256(canonical):
                failures.append(f"OBSERVATION_HASH_MISMATCH:{code}:{day}:{kind}")
            if payload != canonical:
                failures.append(f"OBSERVATION_PAYLOAD_MISMATCH:{code}:{day}:{kind}")
            if kind == "DAILY":
                self._validate_daily_provenance(
                    code=code,
                    day=day,
                    row=row,
                    payload=payload,
                    failures=failures,
                )
            if str(row.get("status")) != "ACCEPTED" or not row.get("processed_at"):
                failures.append(f"OBSERVATION_NOT_ACCEPTED:{code}:{day}:{kind}")
            try:
                event_at = _aware(row.get("event_at"))
                available_at = _aware(row.get("available_at"))
                ingested_at = _aware(row.get("ingested_at"))
                if event_at.date() != day or not event_at <= available_at <= ingested_at:
                    raise ValueError
                if kind == "DAILY" and available_at < datetime.combine(day, time(16), NY_TZ):
                    raise ValueError
                if kind == "OPEN" and ingested_at > datetime.combine(day, time(9, 35), NY_TZ):
                    raise ValueError
            except (TypeError, ValueError):
                failures.append(f"OBSERVATION_CAUSALITY_FAILURE:{code}:{day}:{kind}")

    def _validate_daily_provenance(
        self,
        *,
        code: str,
        day: date,
        row: Mapping[str, Any],
        payload: Any,
        failures: list[str],
    ) -> None:
        label = "BILTR" if code == BIL_BENCHMARK_CODE else f"RAW_DAILY:{code}"
        required = {
            "source_schema",
            "source",
            "source_code",
            "frequency",
            "adjustment",
            "source_rows",
            "source_sha256",
        }
        if not isinstance(payload, Mapping) or not required.issubset(payload):
            failures.append(f"{label}_PROVENANCE_MISSING:{day.isoformat()}")
            return
        source_schema = payload.get("source_schema")
        source = payload.get("source")
        source_code = payload.get("source_code")
        frequency = payload.get("frequency")
        adjustment = payload.get("adjustment")
        source_rows = payload.get("source_rows")
        source_sha256 = str(payload.get("source_sha256") or "")
        expected_adjustment = "front" if code == BIL_BENCHMARK_CODE else "none"
        expected_source_code = BIL_RAW_CODE if code == BIL_BENCHMARK_CODE else code
        if (
            source_schema != DAILY_SOURCE_SCHEMA
            or source != "TDX"
            or source_code != expected_source_code
            or frequency != DAILY_SOURCE_FREQUENCY
            or adjustment != expected_adjustment
        ):
            failures.append(f"{label}_PROVENANCE_INVALID:{day.isoformat()}")
        if not isinstance(source_rows, list):
            failures.append(f"{label}_SOURCE_ROWS_INVALID:{day.isoformat()}")
            return
        expected_source_sha256 = canonical_daily_source_sha256(
            source=str(source),
            source_code=str(source_code),
            adjustment=str(adjustment),
            source_rows=source_rows,
        )
        if (
            not _is_sha256(source_sha256)
            or source_sha256 != expected_source_sha256
        ):
            failures.append(f"{label}_SOURCE_HASH_MISMATCH:{day.isoformat()}")

        if code == BIL_BENCHMARK_CODE:
            self._validate_biltr_source_rows(
                day=day,
                row=row,
                source_rows=source_rows,
                failures=failures,
            )
            return
        self._validate_raw_daily_source_row(
            code=code,
            day=day,
            row=row,
            source_rows=source_rows,
            failures=failures,
        )

    def _daily_provenance_is_replayable(
        self,
        *,
        code: str,
        day: date,
        row: Mapping[str, Any],
    ) -> bool:
        try:
            payload = json.loads(str(row.get("payload_json") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        provenance_failures: list[str] = []
        self._validate_daily_provenance(
            code=code,
            day=day,
            row=row,
            payload=payload,
            failures=provenance_failures,
        )
        return not provenance_failures

    def _validate_biltr_source_rows(
        self,
        *,
        day: date,
        row: Mapping[str, Any],
        source_rows: Sequence[Any],
        failures: list[str],
    ) -> None:
        label = f"BILTR_SOURCE_ROWS_INVALID:{day.isoformat()}"
        if len(source_rows) != 2 or any(
            not isinstance(item, Mapping)
            or set(item) != {"session_date", "Close"}
            or type(item.get("session_date")) is not str
            or type(item.get("Close")) is not float
            for item in source_rows
        ):
            failures.append(label)
            return
        try:
            previous_day = _date(source_rows[0].get("session_date"))
            current_day = _date(source_rows[1].get("session_date"))
            previous_close = float(source_rows[0].get("Close"))
            current_close = float(source_rows[1].get("Close"))
            if not all(
                math.isfinite(value) and value > 0
                for value in (previous_close, current_close)
            ):
                raise ValueError
        except (TypeError, ValueError):
            failures.append(label)
            return
        try:
            position = self.sessions.index(day)
        except ValueError:
            failures.append(f"BILTR_SOURCE_SESSION_MISMATCH:{day.isoformat()}")
            return
        expected_previous = self.sessions[position - 1] if position > 0 else None
        if (
            current_day != day
            or previous_day >= day
            or (expected_previous is not None and previous_day != expected_previous)
        ):
            failures.append(f"BILTR_SOURCE_SESSION_MISMATCH:{day.isoformat()}")
        if not (
            _same_number(previous_close, row.get("open"))
            and _same_number(current_close, row.get("close"))
        ):
            failures.append(f"BILTR_SOURCE_CLOSE_MISMATCH:{day.isoformat()}")

    @staticmethod
    def _validate_raw_daily_source_row(
        *,
        code: str,
        day: date,
        row: Mapping[str, Any],
        source_rows: Sequence[Any],
        failures: list[str],
    ) -> None:
        label = f"RAW_DAILY:{code}_SOURCE_ROW_MISMATCH:{day.isoformat()}"
        expected_keys = {"session_date", "Open", "High", "Low", "Close"}
        if (
            len(source_rows) != 1
            or not isinstance(source_rows[0], Mapping)
            or set(source_rows[0]) != expected_keys
            or type(source_rows[0].get("session_date")) is not str
            or any(
                type(source_rows[0].get(field)) is not float
                for field in ("Open", "High", "Low", "Close")
            )
        ):
            failures.append(label)
            return
        source_row = source_rows[0]
        try:
            source_day = _date(source_row.get("session_date"))
        except (TypeError, ValueError):
            failures.append(label)
            return
        if source_day != day or not all(
            _same_number(source_row.get(source_name), row.get(column_name))
            for source_name, column_name in (
                ("Open", "open"),
                ("High", "high"),
                ("Low", "low"),
                ("Close", "close"),
            )
        ):
            failures.append(label)

    @staticmethod
    def _validate_runtime_quotes(
        rows: Sequence[Mapping[str, Any]],
        completed_days: Sequence[date],
        failures: list[str],
    ) -> None:
        day_set = set(completed_days)
        for row in rows:
            day = _date(row.get("session_date"))
            if day not in day_set:
                continue
            code = str(row.get("code") or "")
            if not _is_sha256(row.get("payload_hash")):
                failures.append(f"QUOTE_HASH_MISSING:{code}:{day}")
            if not int(row.get("admitted") or 0) or str(row.get("reason")) != "ACCEPTED":
                failures.append(f"QUOTE_NOT_ADMITTED:{code}:{day}")
                continue
            try:
                source_at = _aware(row.get("source_at"))
                fetched_at = _aware(row.get("fetched_at"))
                if source_at.date() != day or fetched_at.date() != day:
                    raise ValueError
                latency = (fetched_at - source_at).total_seconds()
                if latency < 0 or latency > 90:
                    raise ValueError
            except (TypeError, ValueError):
                failures.append(f"QUOTE_CAUSALITY_FAILURE:{code}:{day}")

    @staticmethod
    def _validate_orders_and_fills(
        paper: Mapping[str, tuple[dict[str, Any], ...]],
        completed_days: Sequence[date],
        failures: list[str],
    ) -> None:
        end = completed_days[-1] if completed_days else None
        orders = {str(row.get("order_id")): row for row in paper["us_paper_orders"]}
        fill_counts: dict[str, int] = {}
        for fill in paper["us_paper_fills"]:
            day = _aware(fill.get("filled_at")).date()
            if end is None or day > end:
                continue
            order_id = str(fill.get("order_id") or "")
            fill_counts[order_id] = fill_counts.get(order_id, 0) + 1
            order = orders.get(order_id)
            if order is None:
                failures.append(f"FILL_WITHOUT_ORDER:{fill.get('fill_id')}")
            elif str(order.get("code")) != str(fill.get("code")) or str(order.get("side")) != str(fill.get("side")):
                failures.append(f"FILL_ORDER_MISMATCH:{fill.get('fill_id')}")
            elif str(order.get("security_id") or "") != str(fill.get("security_id") or ""):
                failures.append(f"FILL_SECURITY_ID_MISMATCH:{fill.get('fill_id')}")
            if int(fill.get("quantity") or 0) <= 0 or not _positive_number(fill.get("price")):
                failures.append(f"INVALID_FILL:{fill.get('fill_id')}")
            if not _nonnegative_number(fill.get("fees")):
                failures.append(f"INVALID_FILL_FEES:{fill.get('fill_id')}")
            expected_fill_key = (
                f"fill:{order_id}:{fill.get('side')}:{fill.get('reason')}"
            )
            if (
                str(fill.get("idempotency_key") or "") != expected_fill_key
                or str(fill.get("fill_id") or "")
                != "uspf_" + _paper_hash(expected_fill_key)[:24]
            ):
                failures.append(f"FILL_IDEMPOTENCY_MISMATCH:{fill.get('fill_id')}")
        for order_id, order in orders.items():
            if not _is_sha256(order.get("payload_hash")):
                failures.append(f"ORDER_HASH_MISSING:{order_id}")
            status = str(order.get("status") or "")
            count = fill_counts.get(order_id, 0)
            if status == "FILLED" and count != 1:
                failures.append(f"FILLED_ORDER_REPLAY_MISMATCH:{order_id}")
            if status != "FILLED" and count:
                failures.append(f"NONFILLED_ORDER_HAS_FILL:{order_id}")
            reason = str(order.get("block_reason") or "").upper()
            if status in {"CANCELLED", "EXPIRED"} or any(
                token in reason for token in ("KILL", "LATE", "MISSING", "DEGRADED")
            ):
                failures.append(f"UNSAFE_ORDER_TERMINAL_STATE:{order_id}")
            if str(order.get("order_kind") or "") == "STOP":
                session_key = _aware(order.get("eligible_at")).date().isoformat()
                reason_value = str(order.get("reason") or "")
                expected_key = (
                    f"stop:{order.get('security_id')}:{session_key}:{reason_value}"
                )
                expected_payload = {
                    "security_id": order.get("security_id"),
                    "code": order.get("code"),
                    "pit_release_id": order.get("pit_release_id"),
                    "manifest_sha256": order.get("manifest_sha256"),
                    "reason": reason_value,
                    "session": session_key,
                }
                if (
                    str(order.get("idempotency_key") or "") != expected_key
                    or order_id != "uspor_" + _paper_hash(expected_key)[:24]
                    or str(order.get("payload_hash") or "")
                    != _sha256(expected_payload)
                    or str(order.get("side") or "") != "SELL"
                ):
                    failures.append(f"STOP_ORDER_REPLAY_MISMATCH:{order_id}")

    def _validate_corporate_actions(
        self,
        paper: Mapping[str, tuple[dict[str, Any], ...]],
        completed_days: Sequence[date],
        failures: list[str],
    ) -> None:
        """Validate the immutable action, receivable, and cash subledgers."""

        actions = {
            str(row.get("action_id") or ""): row
            for row in paper["us_paper_corporate_actions"]
        }
        if "" in actions:
            failures.append("CORPORATE_ACTION_ID_MISSING")
        end = completed_days[-1] if completed_days else None
        receivables_by_action: dict[str, list[Mapping[str, Any]]] = {}
        for row in paper["us_paper_receivables"]:
            action_id = str(row.get("action_id") or "")
            receivables_by_action.setdefault(action_id, []).append(row)
            action = actions.get(action_id)
            receivable_id = str(row.get("receivable_id") or "")
            expected_id = "uspr_" + _paper_hash("dividend:" + action_id)[:24]
            if action is None:
                failures.append(f"RECEIVABLE_WITHOUT_ACTION:{receivable_id}")
                continue
            if (
                str(action.get("action_type")) != "CASH_DIVIDEND"
                or receivable_id != expected_id
                or str(row.get("security_id")) != str(action.get("security_id"))
                or str(row.get("pay_date")) != str(action.get("pay_date"))
                or not _nonnegative_number(row.get("amount"))
            ):
                failures.append(f"INVALID_CORPORATE_RECEIVABLE:{receivable_id}")
            status = str(row.get("status") or "")
            paid_at = row.get("paid_at")
            if status == "PAID":
                try:
                    if not paid_at or _aware(paid_at).date() < _date(row.get("pay_date")):
                        raise ValueError
                except (TypeError, ValueError):
                    failures.append(f"INVALID_RECEIVABLE_PAYMENT_TIME:{receivable_id}")
            elif status == "PENDING":
                if paid_at:
                    failures.append(f"PENDING_RECEIVABLE_HAS_PAYMENT:{receivable_id}")
                if end is not None and _date(row.get("pay_date")) <= end:
                    failures.append(f"OVERDUE_CORPORATE_RECEIVABLE:{receivable_id}")
            else:
                failures.append(f"INVALID_RECEIVABLE_STATUS:{receivable_id}")

        cash_by_action: dict[str, list[Mapping[str, Any]]] = {}
        for row in paper["us_paper_cash_ledger"]:
            action_id = str(row.get("action_id") or "")
            cash_by_action.setdefault(action_id, []).append(row)
            action = actions.get(action_id)
            entry_id = str(row.get("cash_entry_id") or "")
            entry_type = str(row.get("entry_type") or "")
            idempotency = str(row.get("idempotency_key") or "")
            if action is None:
                failures.append(f"CORPORATE_CASH_WITHOUT_ACTION:{entry_id}")
                continue
            if entry_id != "uspcl_" + _paper_hash(idempotency)[:24]:
                failures.append(f"CORPORATE_CASH_ID_MISMATCH:{entry_id}")
            if (
                str(row.get("security_id")) != str(action.get("security_id"))
                or not _nonnegative_number(row.get("amount"))
            ):
                failures.append(f"INVALID_CORPORATE_CASH:{entry_id}")
            try:
                details = json.loads(str(row.get("details_json") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = None
            if entry_type == "DIVIDEND_PAYMENT":
                candidates = receivables_by_action.get(action_id, [])
                if len(candidates) != 1:
                    failures.append(f"DIVIDEND_CASH_RECEIVABLE_MISMATCH:{entry_id}")
                    continue
                receivable = candidates[0]
                expected_key = f"dividend-payment:{receivable.get('receivable_id')}"
                if (
                    idempotency != expected_key
                    or details
                    != {"receivable_id": receivable.get("receivable_id")}
                    or not math.isclose(
                        float(row.get("amount") or 0),
                        float(receivable.get("amount") or 0),
                        rel_tol=0,
                        abs_tol=1e-9,
                    )
                    or _aware(row.get("occurred_at")).date()
                    < _date(receivable.get("pay_date"))
                ):
                    failures.append(f"DIVIDEND_CASH_RECEIVABLE_MISMATCH:{entry_id}")
            elif entry_type in {"CASH_IN_LIEU", "TERMINATION_PROCEEDS"}:
                expected_key = f"action-cash:{action_id}:{entry_type}"
                if (
                    idempotency != expected_key
                    or details != {"action_type": action.get("action_type")}
                ):
                    failures.append(f"CORPORATE_CASH_PAYLOAD_MISMATCH:{entry_id}")
            else:
                failures.append(f"INVALID_CORPORATE_CASH_TYPE:{entry_id}")
            try:
                if _aware(row.get("occurred_at")).date() != _date(
                    action.get("effective_date")
                ) and entry_type != "DIVIDEND_PAYMENT":
                    raise ValueError
            except (TypeError, ValueError):
                failures.append(f"CORPORATE_CASH_TIME_MISMATCH:{entry_id}")

        for action_id, row in actions.items():
            try:
                terms = json.loads(str(row.get("terms_json") or ""))
                if not isinstance(terms, Mapping):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                terms = {}
                failures.append(f"CORPORATE_ACTION_TERMS_INVALID:{action_id}")
            release_id = str(row.get("pit_release_id") or "").strip().lower()
            manifest_sha256 = str(row.get("manifest_sha256") or "").strip().lower()
            evidence_sha256 = str(row.get("evidence_sha256") or "").strip().lower()
            release_evidence = self._corporate_action_evidence_by_release.get(
                release_id
            )
            if (
                release_evidence is None
                or release_evidence.manifest_sha256 != manifest_sha256
            ):
                failures.append(
                    f"CORPORATE_ACTION_RELEASE_EVIDENCE_MISSING:{action_id}"
                )
            else:
                allowed_source_ids = release_evidence.source_ids_by_digest.get(
                    evidence_sha256, frozenset()
                )
                if not allowed_source_ids:
                    failures.append(
                        f"CORPORATE_ACTION_EVIDENCE_NOT_IN_RELEASE:{action_id}"
                    )
                claimed_source_id = str(
                    row.get("source_id") or terms.get("source_id") or ""
                ).strip()
                if claimed_source_id and claimed_source_id not in allowed_source_ids:
                    failures.append(
                        f"CORPORATE_ACTION_SOURCE_MISMATCH:{action_id}"
                    )
                matching_rows = [
                    artifact_row
                    for artifact_row in release_evidence.artifact_rows
                    if str(artifact_row.get("action_id") or "").strip() == action_id
                    and str(artifact_row.get("security_id") or "").strip()
                    == str(row.get("security_id") or "").strip()
                    and str(artifact_row.get("evidence_sha256") or "")
                    .strip()
                    .lower()
                    == evidence_sha256
                ]
                if len(matching_rows) != 1:
                    failures.append(
                        f"CORPORATE_ACTION_ARTIFACT_MISMATCH:{action_id}"
                    )
                else:
                    artifact_source_id = str(
                        matching_rows[0].get("source_id") or ""
                    ).strip()
                    if (
                        not artifact_source_id
                        or artifact_source_id not in allowed_source_ids
                    ) or (
                        claimed_source_id
                        and artifact_source_id
                        and artifact_source_id != claimed_source_id
                    ):
                        failures.append(
                            f"CORPORATE_ACTION_SOURCE_MISMATCH:{action_id}"
                        )
            canonical = {
                "action_id": action_id,
                "security_id": row.get("security_id"),
                "action_type": row.get("action_type"),
                "effective_date": row.get("effective_date"),
                "pay_date": row.get("pay_date"),
                "verified": bool(row.get("verified")),
                "verified_at": row.get("verified_at"),
                "evidence_sha256": row.get("evidence_sha256"),
                "pit_release_id": row.get("pit_release_id"),
                "manifest_sha256": row.get("manifest_sha256"),
                "terms": dict(terms),
                "validation_error": row.get("block_reason") or "",
            }
            if str(row.get("content_hash") or "") != _sha256(canonical):
                failures.append(f"CORPORATE_ACTION_HASH_MISMATCH:{action_id}")
            status = str(row.get("status") or "")
            if (
                not int(row.get("verified") or 0)
                or not _is_sha256(row.get("evidence_sha256"))
                or not _is_sha256(row.get("pit_release_id"))
                or not _is_sha256(row.get("manifest_sha256"))
            ):
                failures.append(f"CORPORATE_ACTION_EVIDENCE_INVALID:{action_id}")
            try:
                verified_at = _aware(row.get("verified_at"))
                received_at = _aware(row.get("received_at"))
                if verified_at > received_at:
                    raise ValueError
                if status in {"APPLIED", "APPLIED_NO_POSITION", "BLOCKED"}:
                    applied_at = _aware(row.get("applied_at"))
                    if (
                        applied_at.date() != _date(row.get("effective_date"))
                        or applied_at.time() > time(9, 30)
                    ):
                        raise ValueError
            except (TypeError, ValueError):
                failures.append(f"CORPORATE_ACTION_CAUSALITY_FAILURE:{action_id}")
            if status == "BLOCKED" or str(row.get("block_reason") or ""):
                failures.append(f"CORPORATE_ACTION_BLOCKED:{action_id}")
            elif status == "PENDING":
                if end is not None and _date(row.get("effective_date")) <= end:
                    failures.append(f"CORPORATE_ACTION_NOT_APPLIED:{action_id}")
            elif status not in {"APPLIED", "APPLIED_NO_POSITION"}:
                failures.append(f"INVALID_CORPORATE_ACTION_STATUS:{action_id}")
            if status == "APPLIED_NO_POSITION" and (
                receivables_by_action.get(action_id) or cash_by_action.get(action_id)
            ):
                failures.append(f"NO_POSITION_ACTION_HAS_ECONOMIC_ROWS:{action_id}")
            if str(row.get("action_type")) == "CASH_DIVIDEND" and status == "APPLIED":
                if len(receivables_by_action.get(action_id, [])) != 1:
                    failures.append(f"DIVIDEND_RECEIVABLE_MISSING:{action_id}")

    def _replay(self, snapshot: _Snapshot, completed_days: Sequence[date]) -> _Replay:
        paper = snapshot.paper
        failures: list[str] = []
        account_rows = paper["us_paper_account"]
        initial_cash = float(account_rows[0].get("initial_cash")) if len(account_rows) == 1 else float("nan")
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            failures.append("INVALID_INITIAL_CASH")
            initial_cash = 0.0
        cash = initial_cash
        bil_equity = 100.0
        positions: dict[str, dict[str, Any]] = {}
        trades_open: dict[str, tuple[str, date]] = {}
        trades: list[PaperTradeEvidence] = []
        fills_by_day: dict[date, list[Mapping[str, Any]]] = {}
        orders = {
            str(row.get("order_id") or ""): row
            for row in paper["us_paper_orders"]
        }
        for row in paper["us_paper_fills"]:
            fills_by_day.setdefault(_aware(row.get("filled_at")).date(), []).append(row)
        actions_by_day: dict[date, list[Mapping[str, Any]]] = {}
        for row in paper["us_paper_corporate_actions"]:
            if str(row.get("status")) in {"APPLIED", "APPLIED_NO_POSITION"}:
                actions_by_day.setdefault(
                    _date(row.get("effective_date")), []
                ).append(row)
        receivables_by_action: dict[str, list[Mapping[str, Any]]] = {}
        for row in paper["us_paper_receivables"]:
            receivables_by_action.setdefault(
                str(row.get("action_id") or ""), []
            ).append(row)
        cash_by_action: dict[str, list[Mapping[str, Any]]] = {}
        cash_by_day: dict[date, list[Mapping[str, Any]]] = {}
        for row in paper["us_paper_cash_ledger"]:
            cash_by_action.setdefault(str(row.get("action_id") or ""), []).append(row)
            cash_by_day.setdefault(_aware(row.get("occurred_at")).date(), []).append(row)
        alias_events_by_day: dict[date, list[Mapping[str, Any]]] = {}
        for row in paper["us_paper_events"]:
            if str(row.get("event_type")) == "SECURITY_ALIAS_RENAMED":
                alias_events_by_day.setdefault(
                    _aware(row.get("occurred_at")).date(), []
                ).append(row)
        observations: dict[tuple[date, str, str], Mapping[str, Any]] = {}
        for row in paper["us_paper_observations"]:
            if str(row.get("status")) == "ACCEPTED":
                observations[(_date(row.get("session_date")), str(row.get("code")), str(row.get("kind")))] = row
        quotes_by_key: dict[tuple[date, str, str], list[Mapping[str, Any]]] = {}
        for row in snapshot.runtime["us_paper_runtime_quotes"]:
            key = (_date(row.get("session_date")), str(row.get("code")), str(row.get("purpose")))
            quotes_by_key.setdefault(key, []).append(row)

        evidence: list[PaperSessionEvidence] = []
        outputs: dict[date, str] = {}
        previous_output = _sha256({"initial_cash": initial_cash, "positions": {}})
        for day in completed_days:
            alias_events = sorted(
                alias_events_by_day.get(day, []),
                key=lambda row: (str(row.get("occurred_at")), str(row.get("event_id"))),
            )
            preopen_alias_events = [
                event
                for event in alias_events
                if _aware(event.get("occurred_at")).time() <= time(9, 30)
            ]
            postclose_alias_events = [
                event
                for event in alias_events
                if _aware(event.get("occurred_at")).time() >= time(16, 0)
            ]
            ambiguous_alias_events = [
                event
                for event in alias_events
                if event not in preopen_alias_events
                and event not in postclose_alias_events
            ]
            for event in ambiguous_alias_events:
                failures.append(
                    f"INTRADAY_ALIAS_CHANGE_UNREPLAYABLE:{event.get('event_id')}"
                )
            for event in preopen_alias_events:
                _apply_replay_alias_event(event, positions, failures)
            for action in sorted(
                actions_by_day.get(day, []), key=lambda row: str(row.get("action_id"))
            ):
                _apply_replay_corporate_action(
                    action=action,
                    positions=positions,
                    trades_open=trades_open,
                    trades=trades,
                    receivables=receivables_by_action.get(
                        str(action.get("action_id") or ""), []
                    ),
                    cash_entries=cash_by_action.get(
                        str(action.get("action_id") or ""), []
                    ),
                    day=day,
                    failures=failures,
                )
            for entry in sorted(
                cash_by_day.get(day, []),
                key=lambda row: (str(row.get("occurred_at")), str(row.get("cash_entry_id"))),
            ):
                cash += float(entry.get("amount") or 0.0)
            open_required = {str(item["code"]) for item in positions.values()}
            monitored_codes = set(open_required)
            day_fills = sorted(
                fills_by_day.get(day, []),
                key=lambda row: (str(row.get("filled_at")), str(row.get("fill_id"))),
            )
            for fill in day_fills:
                code = str(fill.get("code"))
                security_id = str(fill.get("security_id") or "")
                side = str(fill.get("side"))
                quantity = int(fill.get("quantity") or 0)
                value = quantity * float(fill.get("price") or 0)
                fees = float(fill.get("fees") or 0)
                if side == "BUY":
                    if security_id in positions:
                        failures.append(f"POSITION_DOUBLE_OPEN:{security_id}:{day}")
                    order = orders.get(str(fill.get("order_id") or ""), {})
                    price = float(fill.get("price") or 0.0)
                    stop_ratio = float(order.get("stop_ratio") or 0.0)
                    positions[security_id] = {
                        "security_id": security_id,
                        "code": code,
                        "quantity": quantity,
                        "average_price": price,
                        "stop_price": price * (1.0 - stop_ratio),
                        "last_price": price,
                        "entry_at": fill.get("filled_at"),
                        "pit_release_id": fill.get("pit_release_id"),
                        "manifest_sha256": fill.get("manifest_sha256"),
                    }
                    open_required.add(code)
                    monitored_codes.add(code)
                    cash -= value + fees
                    trades_open[security_id] = (str(fill.get("fill_id")), day)
                elif side == "SELL":
                    held = positions.get(security_id)
                    if held is None or int(held["quantity"]) != quantity or security_id not in trades_open:
                        failures.append(f"SELL_POSITION_REPLAY_MISMATCH:{security_id}:{day}")
                    cash += value - fees
                    prior = positions.pop(security_id, None)
                    open_required.add(code)
                    if _aware(fill.get("filled_at")).time() <= time(9, 35):
                        monitored_codes.discard(str((prior or {}).get("code") or code))
                    else:
                        monitored_codes.add(code)
                    opened = trades_open.pop(security_id, None)
                    if opened:
                        trades.append(
                            PaperTradeEvidence(
                                trade_id="uspt_" + _sha256([opened[0], fill.get("fill_id")])[:24],
                                opened_session=opened[1],
                                closed_session=day,
                            )
                        )
                else:
                    failures.append(f"INVALID_FILL_SIDE:{code}:{day}")

            bil_raw = observations.get((day, BIL_RAW_CODE, "DAILY"))
            bil_raw_replayable = bool(
                bil_raw is not None
                and self._daily_provenance_is_replayable(
                    code=BIL_RAW_CODE,
                    day=day,
                    row=bil_raw,
                )
            )
            if bil_raw is None or not _positive_number(bil_raw.get("close")):
                failures.append(f"MISSING_BIL_RAW:{day.isoformat()}")
            elif not bil_raw_replayable:
                failures.append(
                    f"DAILY_PROVENANCE_REPLAY_BLOCKED:{BIL_RAW_CODE}:{day.isoformat()}"
                )
            bil = observations.get((day, BIL_BENCHMARK_CODE, "DAILY"))
            bil_factor: float | None = None
            if (
                bil is None
                or not _positive_number(bil.get("open"))
                or not _positive_number(bil.get("close"))
            ):
                failures.append(
                    f"MISSING_BIL_TOTAL_RETURN_FACTOR:{day.isoformat()}"
                )
            elif not self._daily_provenance_is_replayable(
                code=BIL_BENCHMARK_CODE,
                day=day,
                row=bil,
            ):
                failures.append(
                    f"BILTR_PROVENANCE_REPLAY_BLOCKED:{day.isoformat()}"
                )
            else:
                bil_factor = float(bil["close"]) / float(bil["open"])
                if not math.isfinite(bil_factor) or bil_factor <= 0:
                    failures.append(
                        f"INVALID_BIL_TOTAL_RETURN_FACTOR:{day.isoformat()}"
                    )
                    bil_factor = None
            marks: dict[str, float] = {}
            for code in sorted(open_required):
                open_observation = observations.get((day, code, "OPEN"))
                if open_observation is None:
                    failures.append(f"MISSING_HELD_OPEN_OBSERVATION:{code}:{day.isoformat()}")
                open_quotes = quotes_by_key.get((day, code, "OPEN"), [])
                if not any(int(row.get("admitted") or 0) for row in open_quotes):
                    failures.append(f"MISSING_HELD_OPEN_QUOTE:{code}:{day.isoformat()}")
            for code in sorted(monitored_codes):
                raw = observations.get((day, code, "DAILY"))
                if raw is None or not _positive_number(raw.get("close")):
                    failures.append(f"MISSING_HELD_RAW:{code}:{day.isoformat()}")
                elif not self._daily_provenance_is_replayable(
                    code=code,
                    day=day,
                    row=raw,
                ):
                    failures.append(
                        f"DAILY_PROVENANCE_REPLAY_BLOCKED:{code}:{day.isoformat()}"
                    )
                intraday = quotes_by_key.get((day, code, "INTRADAY_STOP"), [])
                if not _complete_intraday_quote_coverage(intraday, day):
                    failures.append(f"INCOMPLETE_INTRADAY_QUOTE_COVERAGE:{code}:{day.isoformat()}")
            for security_id in sorted(positions):
                code = str(positions[security_id]["code"])
                raw = observations.get((day, code, "DAILY"))
                if (
                    raw is None
                    or not _positive_number(raw.get("close"))
                    or not self._daily_provenance_is_replayable(
                        code=code,
                        day=day,
                        row=raw,
                    )
                ):
                    continue
                marks[security_id] = float(raw["close"])
                positions[security_id]["last_price"] = float(raw["close"])

            if (
                bil is None
                or bil_raw is None
                or not bil_raw_replayable
                or bil_factor is None
                or len(marks) != len(positions)
            ):
                continue
            bil_equity *= bil_factor
            equity = cash + sum(
                int(positions[security_id]["quantity"]) * marks[security_id]
                for security_id in positions
            )
            if not math.isfinite(equity) or equity <= 0:
                failures.append(f"INVALID_REPLAY_EQUITY:{day.isoformat()}")
                continue
            session_input = {
                "session": day.isoformat(),
                "previous_output_sha256": previous_output,
                "paper_session": _rows_for_day(paper["us_paper_sessions"], day, "session_date"),
                "runtime_session": _rows_for_day(snapshot.runtime["us_paper_runtime_sessions"], day, "session_date"),
                "fills": day_fills,
                "corporate_actions": _rows_for_day(
                    paper["us_paper_corporate_actions"], day, "effective_date"
                ),
                "corporate_cash": [
                    row for row in paper["us_paper_cash_ledger"]
                    if _aware(row.get("occurred_at")).date() == day
                ],
                "receivables": [
                    row for row in paper["us_paper_receivables"]
                    if _date(row.get("created_at")) == day
                    or (row.get("paid_at") and _aware(row.get("paid_at")).date() == day)
                ],
                "observations": _rows_for_day(paper["us_paper_observations"], day, "session_date"),
                "quotes": _rows_for_day(snapshot.runtime["us_paper_runtime_quotes"], day, "session_date"),
            }
            input_hash = _sha256(session_input)
            output_hash = _sha256(
                {
                    "session": day.isoformat(),
                    "cash": cash,
                    "positions": positions,
                    "marks": marks,
                    "equity": equity,
                    "bil_total_return_factor": bil_factor,
                    "bil_equity": bil_equity,
                    "input_sha256": input_hash,
                }
            )
            outputs[day] = output_hash
            previous_output = output_hash
            evidence.append(
                PaperSessionEvidence(
                    session=day,
                    equity=equity,
                    bil_equity=bil_equity,
                    input_sha256=input_hash,
                    output_sha256=output_hash,
                    replay_output_sha256=output_hash,
                )
            )
            # Normal PIT alias migrations are committed by the post-close
            # decision coordinator.  They affect the next session, not the raw
            # mark that was already observed for this one.
            for event in postclose_alias_events:
                _apply_replay_alias_event(event, positions, failures)

        for security_id, (fill_id, opened) in sorted(trades_open.items()):
            trades.append(
                PaperTradeEvidence(
                    trade_id="uspt_" + _sha256([fill_id, security_id, opened.isoformat()])[:24],
                    opened_session=opened,
                    closed_session=None,
                )
            )
        return _Replay(
            tuple(evidence),
            tuple(trades),
            outputs,
            tuple(failures),
            cash,
            {security_id: dict(value) for security_id, value in positions.items()},
        )

    def _validate_decision_replays(
        self,
        paper: Mapping[str, tuple[dict[str, Any], ...]],
        cycles: Sequence[PaperCycleEvidence],
        failures: list[str],
    ) -> None:
        """Replay every completed monthly ranking from its immutable inputs."""

        if not cycles:
            return
        if self.decision_archive_root is None:
            failures.append("PAPER_DECISION_ARCHIVE_REQUIRED")
            return
        from .strategies.us_momentum import USMomentumParameters, USMomentumStrategy
        from .us_paper_decision import (
            USPaperDecisionAuditStore,
            USPaperDecisionCoordinator,
            _bars_hash,
            _source_code_sha256,
        )

        store = USPaperDecisionAuditStore(self.decision_archive_root)
        periods = {
            str(item.get("period_id")): item for item in paper["us_paper_periods"]
        }
        orders_by_period: dict[str, list[Mapping[str, Any]]] = {}
        for item in paper["us_paper_orders"]:
            orders_by_period.setdefault(str(item.get("period_id") or ""), []).append(item)

        for cycle in cycles:
            period = periods.get(cycle.cycle_id)
            if period is None:
                failures.append(f"DECISION_PERIOD_MISSING:{cycle.cycle_id}")
                continue
            decision_day = _aware(period.get("decision_at")).date()
            try:
                archive = store.load(decision_day)
                front = _archived_bars(
                    store.objects / f"{archive['front_object_sha256']}.json"
                )
                raw = _archived_bars(
                    store.objects / f"{archive['raw_object_sha256']}.json"
                )
                if str(archive.get("front_sha256")) != _bars_hash(front):
                    raise ValueError("archived front-bar semantic hash mismatch")
                if str(archive.get("raw_sha256")) != _bars_hash(raw):
                    raise ValueError("archived raw-bar semantic hash mismatch")
                if str(archive.get("strategy_id")) != "us_momentum_v1":
                    raise ValueError("decision archive strategy mismatch")
                if str(archive.get("strategy_version")) != str(
                    USMomentumStrategy.metadata.version
                ):
                    raise ValueError("decision archive strategy version mismatch")
                if str(archive.get("strategy_code_sha256")) != _source_code_sha256(
                    USMomentumStrategy
                ):
                    raise ValueError("decision archive strategy code hash mismatch")
                if str(
                    archive.get("decision_engine_code_sha256")
                ) != _source_code_sha256(USPaperDecisionCoordinator):
                    raise ValueError("decision archive engine code hash mismatch")
                parameters = USMomentumParameters(**dict(archive["strategy_parameters"]))
                positions = list(archive.get("positions") or [])
                names = dict(archive.get("names") or {})
                tradable = set(archive.get("tradable_codes") or [])
                expected = dict(archive["decision_output"])
                expected_signals = list(expected.get("signals") or [])

                def run_once() -> list[dict[str, Any]]:
                    scan = USMomentumStrategy(parameters).scan(
                        run_id=str((expected.get("audit") or {}).get("run_id") or ""),
                        front_bars=front,
                        raw_bars=raw,
                        names=names,
                        positions=positions,
                        runtime_state={},
                        backtest_mode=True,
                        asof=pd.Timestamp(decision_day),
                        is_rebalance_day=True,
                        tradable_codes=tradable,
                    )
                    return [
                        {
                            "code": str(item.code),
                            "side": str(item.side),
                            "target_weight": float(item.target_weight),
                            "reason_codes": list(item.reason_codes),
                            "evidence": _json_normalize(dict(item.evidence)),
                        }
                        for item in scan.signals
                    ]

                first = run_once()
                second = run_once()
                expected_core = [
                    {
                        "code": str(item.get("code")),
                        "side": str(item.get("side")),
                        "target_weight": float(item.get("target_weight") or 0),
                        "reason_codes": list(item.get("reason_codes") or []),
                        "evidence": _strategy_evidence(item.get("evidence") or {}),
                    }
                    for item in expected_signals
                    if "US_PIT_MEMBERSHIP_REMOVAL"
                    not in set(item.get("reason_codes") or [])
                ]
                if _sha256(first) != _sha256(second):
                    raise ValueError("strategy replay is non-deterministic")
                if _sha256(first) != _sha256(expected_core):
                    raise ValueError("strategy replay signal mismatch")
                orders = orders_by_period.get(cycle.cycle_id, [])
                normalized_signals = _validate_archived_signal_contract(
                    archive=archive,
                    period=period,
                    expected_signals=expected_signals,
                    strategy_signals=first,
                )
                _validate_rebalance_order_contract(
                    period=period,
                    orders=orders,
                    normalized_signals=normalized_signals,
                )
                archived_ids = {str(item.get("signal_id")) for item in expected_signals}
                ledger_ids = {str(item.get("signal_id")) for item in orders}
                if archived_ids != ledger_ids:
                    raise ValueError("archived signals differ from order ledger")
                if str(archive.get("release_id")) != str(period.get("pit_release_id")):
                    raise ValueError("decision archive release mismatch")
                if str(archive.get("manifest_sha256")) != str(
                    period.get("manifest_sha256")
                ):
                    raise ValueError("decision archive manifest mismatch")
            except Exception as exc:
                failures.append(
                    f"PAPER_DECISION_REPLAY_FAILED:{cycle.cycle_id}:"
                    f"{type(exc).__name__}:{exc}"
                )

    def _cycles(
        self,
        paper: Mapping[str, tuple[dict[str, Any], ...]],
        completed_days: Sequence[date],
        failures: list[str],
    ) -> tuple[PaperCycleEvidence, ...]:
        if not completed_days:
            return ()
        end = completed_days[-1]
        orders_by_period: dict[str, list[Mapping[str, Any]]] = {}
        for row in paper["us_paper_orders"]:
            if row.get("period_id"):
                orders_by_period.setdefault(str(row["period_id"]), []).append(row)
        result: list[PaperCycleEvidence] = []
        for period in sorted(paper["us_paper_periods"], key=lambda row: str(row.get("period_key"))):
            execution = _date(period.get("execution_session"))
            if execution > end:
                continue
            decision = _aware(period.get("decision_at")).date()
            rows = orders_by_period.get(str(period.get("period_id")), [])
            terminal = all(
                str(row.get("status")) in {"FILLED", "SKIPPED", "BLOCKED"}
                for row in rows
            )
            hashes = _is_sha256(period.get("signal_hash")) and all(
                _is_sha256(row.get("payload_hash")) for row in rows
            )
            chronology = decision in self.session_set and execution in self.session_set and execution > decision
            if not chronology:
                failures.append(f"CYCLE_CHRONOLOGY_FAILURE:{period.get('period_id')}")
            if str(period.get("status")) != "AUTO_APPROVED":
                failures.append(f"CYCLE_NOT_AUTO_APPROVED:{period.get('period_id')}")
            result.append(
                PaperCycleEvidence(
                    cycle_id=str(period.get("period_id")),
                    decision_session=decision,
                    execution_session=execution,
                    complete=execution in set(completed_days) and terminal,
                    replay_verified=bool(hashes and chronology and terminal),
                )
            )
        return tuple(result)

    @staticmethod
    def _validate_final_account(
        snapshot: _Snapshot,
        completed_days: Sequence[date],
        replay: _Replay,
        failures: list[str],
    ) -> None:
        if not completed_days:
            return
        end = completed_days[-1]
        later_fills = any(
            _aware(row.get("filled_at")).date() > end
            for row in snapshot.paper["us_paper_fills"]
        )
        later_actions = any(
            _date(row.get("effective_date")) > end
            and str(row.get("status")) in {"APPLIED", "APPLIED_NO_POSITION"}
            for row in snapshot.paper["us_paper_corporate_actions"]
        )
        later_cash = any(
            _aware(row.get("occurred_at")).date() > end
            for row in snapshot.paper["us_paper_cash_ledger"]
        )
        if later_fills or later_actions or later_cash or not replay.sessions:
            return
        account = snapshot.paper["us_paper_account"]
        if len(account) != 1:
            return
        if not math.isclose(
            replay.final_cash,
            float(account[0].get("cash") or 0),
            rel_tol=0,
            abs_tol=1e-6,
        ):
            failures.append("FINAL_CASH_REPLAY_MISMATCH")
        stored = {
            str(row.get("security_id") or ""): row
            for row in snapshot.paper["us_paper_positions"]
        }
        if set(replay.final_positions) != set(stored):
            failures.append("FINAL_POSITIONS_REPLAY_MISMATCH")
        for security_id in sorted(set(replay.final_positions) & set(stored)):
            expected = replay.final_positions[security_id]
            actual = stored[security_id]
            code = str(actual.get("code") or security_id)
            if str(actual.get("code") or "") != str(expected.get("code") or ""):
                failures.append(f"FINAL_ALIAS_REPLAY_MISMATCH:{security_id}")
            if int(actual.get("quantity") or 0) != int(
                expected.get("quantity") or 0
            ):
                failures.append(f"FINAL_QUANTITY_REPLAY_MISMATCH:{code}")
            for field, label in (
                ("average_price", "FINAL_COST_REPLAY_MISMATCH"),
                ("stop_price", "FINAL_STOP_REPLAY_MISMATCH"),
                ("last_price", "FINAL_MARK_REPLAY_MISMATCH"),
            ):
                if not math.isclose(
                    float(actual.get(field) or 0),
                    float(expected.get(field) or 0),
                    rel_tol=0,
                    abs_tol=1e-8,
                ):
                    failures.append(f"{label}:{code}")
            if (
                str(actual.get("pit_release_id") or "")
                != str(expected.get("pit_release_id") or "")
                or str(actual.get("manifest_sha256") or "")
                != str(expected.get("manifest_sha256") or "")
            ):
                failures.append(f"POSITION_ENTRY_LINEAGE_MISMATCH:{security_id}")
            try:
                if _aware(actual.get("entry_at")) != _aware(expected.get("entry_at")):
                    failures.append(f"POSITION_ENTRY_TIME_MISMATCH:{security_id}")
            except (TypeError, ValueError):
                failures.append(f"POSITION_ENTRY_TIME_MISMATCH:{security_id}")


def _valid_paper_stop_event(
    event: Mapping[str, Any],
    fills: Mapping[str, Mapping[str, Any]],
    observations: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Accept a HIGH stop event only when it binds a causal quote to its fill."""

    try:
        details = _event_details(event)
        fill = fills[str(details["fill_id"])]
        observation = observations[str(details["observation_id"])]
        reference = float(details["reference_price"])
        return bool(
            str(event.get("severity") or "").upper() == "HIGH"
            and str(fill.get("reason") or "") == "US_FIXED_STOP_INTRADAY_QUOTE"
            and str(fill.get("security_id") or "") == str(details["security_id"])
            and str(fill.get("code") or "") == str(details["code"])
            and str(observation.get("security_id") or "")
            == str(details["security_id"])
            and str(observation.get("code") or "") == str(details["code"])
            and str(observation.get("kind") or "") == "INTRADAY"
            and str(observation.get("status") or "") == "ACCEPTED"
            and observation.get("processed_at")
            and math.isclose(
                float(observation.get("close") or 0),
                reference,
                rel_tol=0,
                abs_tol=1e-12,
            )
            and _aware(observation.get("event_at"))
            <= _aware(observation.get("available_at"))
            <= _aware(observation.get("ingested_at"))
            <= _aware(observation.get("processed_at"))
            <= _aware(fill.get("filled_at"))
            <= _aware(event.get("occurred_at"))
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False


def _valid_runtime_stop_event(
    event: Mapping[str, Any],
    fills: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Recognize only the runtime's explicit fresh-quote simulated fill."""

    try:
        details = _event_details(event)
        fill = fills[str(details["fill_id"])]
        reference = float(details["sell_reference"])
        stop = float(details["stop_price"])
        last = float(details["last"])
        return bool(
            str(event.get("severity") or "").upper() == "HIGH"
            and details.get("action") == "SIMULATED_STOP_FILLED_FROM_FRESH_QUOTE"
            and str(fill.get("reason") or "") == "US_FIXED_STOP_INTRADAY_QUOTE"
            and str(fill.get("code") or "") == str(details["code"])
            and math.isfinite(reference)
            and reference > 0
            and math.isfinite(stop)
            and stop > 0
            and math.isfinite(last)
            and last <= stop
            and _aware(fill.get("filled_at")) <= _aware(event.get("occurred_at"))
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False


def _event_details(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = json.loads(str(event.get("details_json") or ""))
    if (
        isinstance(value, Mapping)
        and set(value) == {"detail"}
        and isinstance(value.get("detail"), str)
    ):
        value = json.loads(str(value["detail"]))
    if not isinstance(value, Mapping):
        raise ValueError("event details are not an object")
    return value


def _one(
    rows: Sequence[Mapping[str, Any]],
    label: str,
    failures: list[str],
) -> Mapping[str, Any]:
    if len(rows) != 1:
        failures.append(f"INVALID_{label.upper().replace(' ', '_')}_COUNT")
        return {}
    return rows[0]


def _rows_for_day(
    rows: Sequence[Mapping[str, Any]],
    day: date,
    column: str,
) -> list[Mapping[str, Any]]:
    return [row for row in rows if _date(row.get(column)) == day]


def _canonical_row_key(row: Mapping[str, Any]) -> str:
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _paper_hash(value: Any) -> str:
    """Mirror the executor's raw-string hash special case."""

    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return _sha256(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def _json_normalize(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _strategy_evidence(value: Mapping[str, Any]) -> Mapping[str, Any]:
    # Coordinator-only provenance fields are added after strategy.scan and are
    # intentionally excluded from the strategy-output comparison.
    excluded = {
        "security_id",
        "pit_release_id",
        "manifest_sha256",
        "paper_signal_contract",
    }
    return _json_normalize(
        {key: item for key, item in dict(value).items() if key not in excluded}
    )


def _validate_archived_signal_contract(
    *,
    archive: Mapping[str, Any],
    period: Mapping[str, Any],
    expected_signals: Sequence[Mapping[str, Any]],
    strategy_signals: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild coordinator-owned IDs/timing and the executor signal payloads."""

    release_id = str(archive.get("release_id") or "")
    manifest_sha256 = str(archive.get("manifest_sha256") or "")
    if not _is_sha256(release_id) or not _is_sha256(manifest_sha256):
        raise ValueError("decision archive release binding is invalid")
    decision_at = _aware(period.get("decision_at"))
    execution = _date(period.get("execution_session"))
    if str(archive.get("decision_date")) != decision_at.date().isoformat():
        raise ValueError("decision archive date differs from period")
    if str(archive.get("execution_date")) != execution.isoformat():
        raise ValueError("decision archive execution date differs from period")
    available_at = datetime.combine(execution, time(9, 20), NY_TZ)
    valid_until = datetime.combine(execution, time(9, 35), NY_TZ)

    security_id_by_code = {
        str(code): str(security_id)
        for code, security_id in dict(
            archive.get("security_id_by_code") or {}
        ).items()
    }
    position_aliases = {
        str(security_id): str(code)
        for security_id, code in dict(archive.get("position_aliases") or {}).items()
    }
    archived_positions = list(archive.get("positions") or [])
    expected_aliases = {
        str(item.get("security_id") or ""): str(item.get("code") or "")
        for item in archived_positions
    }
    if not all(expected_aliases) or position_aliases != expected_aliases:
        raise ValueError("decision archive position aliases are incomplete")

    strategy_pairs = {
        (str(item.get("code") or ""), str(item.get("side") or ""))
        for item in strategy_signals
    }
    tradable_codes = {str(item) for item in archive.get("tradable_codes") or []}
    expected_forced = {
        (str(item.get("security_id") or ""), str(item.get("code") or ""))
        for item in archived_positions
        if str(item.get("code") or "") not in tradable_codes
        and (str(item.get("code") or ""), "SELL") not in strategy_pairs
    }
    actual_forced: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    seen_signal_ids: set[str] = set()
    for item in expected_signals:
        code = str(item.get("code") or "")
        side = str(item.get("side") or "").upper()
        if not code.endswith(".US") or side not in {"BUY", "SELL"}:
            raise ValueError("archived paper signal identity is invalid")
        evidence = item.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("archived paper signal evidence is invalid")
        security_id = str(evidence.get("security_id") or "")
        if security_id_by_code.get(code) != security_id:
            raise ValueError("archived paper signal stable identity mismatch")
        if (
            str(evidence.get("pit_release_id") or "") != release_id
            or str(evidence.get("manifest_sha256") or "") != manifest_sha256
            or str(evidence.get("paper_signal_contract") or "")
            != "GENERATED_POST_CLOSE_APPROVABLE_0920_EXPIRES_0935"
        ):
            raise ValueError("archived paper signal provenance mismatch")
        if _aware(item.get("generated_at")) != decision_at:
            raise ValueError("archived paper signal generation time mismatch")
        if _aware(item.get("available_at")) != available_at:
            raise ValueError("archived paper signal approval time mismatch")
        if _aware(item.get("valid_until")) != valid_until:
            raise ValueError("archived paper signal expiry mismatch")
        try:
            target_weight = float(item.get("target_weight") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError("archived paper signal target weight is invalid") from exc
        if not math.isfinite(target_weight) or not 0 <= target_weight <= 1:
            raise ValueError("archived paper signal target weight is invalid")
        reasons = [str(value) for value in item.get("reason_codes") or []]
        forced = reasons == ["US_PIT_MEMBERSHIP_REMOVAL"]
        if forced:
            if side != "SELL" or target_weight != 0 or not bool(
                evidence.get("membership_verified")
            ):
                raise ValueError("archived membership-removal signal is invalid")
            actual_forced.add((security_id, code))
            id_payload = {
                "pit_release_id": release_id,
                "security_id": security_id,
                "code": code,
                "side": "SELL",
                "reason": "US_PIT_MEMBERSHIP_REMOVAL",
            }
        else:
            id_payload = {
                "pit_release_id": release_id,
                "security_id": security_id,
                "code": code,
                "side": side,
                "target_weight": target_weight,
                "reason_codes": reasons,
            }
        signal_id = str(item.get("signal_id") or "")
        if signal_id != "uspds_" + _sha256(id_payload)[:24]:
            raise ValueError("archived paper signal ID is not deterministic")
        if signal_id in seen_signal_ids:
            raise ValueError("archived paper signal ID is duplicated")
        seen_signal_ids.add(signal_id)
        try:
            stop_ratio = float(evidence.get("stop_ratio", 0.08))
        except (TypeError, ValueError) as exc:
            raise ValueError("archived paper stop ratio is invalid") from exc
        if side == "BUY" and (
            not math.isfinite(stop_ratio) or not 0 < stop_ratio < 1
        ):
            raise ValueError("archived paper stop ratio is invalid")
        normalized.append(
            {
                "signal_id": signal_id,
                "security_id": security_id,
                "code": code,
                "pit_release_id": release_id,
                "manifest_sha256": manifest_sha256,
                "side": side,
                "target_weight": target_weight,
                "stop_ratio": stop_ratio if side == "BUY" else 0.0,
                "generated_at": decision_at.isoformat(),
                "available_at": available_at.isoformat(),
                "valid_until": valid_until.isoformat(),
                "reason": reasons[0] if reasons else "",
            }
        )
    if actual_forced != expected_forced:
        raise ValueError("membership-removal signals were not exactly replayed")
    normalized.sort(
        key=lambda item: (
            item["side"] != "SELL",
            item["security_id"],
            item["code"],
            item["signal_id"],
        )
    )
    expected_period_hash = _sha256(
        {
            "decision_at": decision_at.isoformat(),
            "execution_session": execution.isoformat(),
            "pit_release_id": release_id,
            "manifest_sha256": manifest_sha256,
            "position_aliases": sorted(position_aliases.items()),
            "signals": normalized,
        }
    )
    if str(period.get("signal_hash") or "") != expected_period_hash:
        raise ValueError("paper period signal hash does not replay")
    return normalized


def _apply_replay_alias_event(
    event: Mapping[str, Any],
    positions: dict[str, dict[str, Any]],
    failures: list[str],
) -> None:
    try:
        details = _event_details(event)
    except (TypeError, ValueError, json.JSONDecodeError):
        failures.append(f"ALIAS_EVENT_PAYLOAD_INVALID:{event.get('event_id')}")
        return
    security_id = str(details.get("security_id") or "")
    position = positions.get(security_id)
    if (
        position is None
        or str(position.get("code")) != str(details.get("old_code"))
        or not str(details.get("new_code") or "").endswith(".US")
        or bool(details.get("trade_created"))
        or not _is_sha256(details.get("pit_release_id"))
        or not _is_sha256(details.get("manifest_sha256"))
    ):
        failures.append(f"ALIAS_EVENT_REPLAY_MISMATCH:{event.get('event_id')}")
        return
    position["code"] = str(details["new_code"])


def _apply_replay_corporate_action(
    *,
    action: Mapping[str, Any],
    positions: dict[str, dict[str, Any]],
    trades_open: dict[str, tuple[str, date]],
    trades: list[PaperTradeEvidence],
    receivables: Sequence[Mapping[str, Any]],
    cash_entries: Sequence[Mapping[str, Any]],
    day: date,
    failures: list[str],
) -> None:
    """Apply one verified action to the independent position replay."""

    action_id = str(action.get("action_id") or "")
    security_id = str(action.get("security_id") or "")
    status = str(action.get("status") or "")
    position = positions.get(security_id)
    if status == "APPLIED_NO_POSITION":
        if position is not None:
            failures.append(f"CORPORATE_NO_POSITION_REPLAY_MISMATCH:{action_id}")
        return
    if status != "APPLIED":
        return
    if position is None:
        failures.append(f"CORPORATE_POSITION_REPLAY_MISSING:{action_id}")
        return
    try:
        terms = json.loads(str(action.get("terms_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        failures.append(f"CORPORATE_ACTION_TERMS_INVALID:{action_id}")
        return
    action_type = str(action.get("action_type") or "")

    def numeric(names: Sequence[str], *, allow_zero: bool = False) -> float:
        for name in names:
            try:
                value = float(terms.get(name))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and (value >= 0 if allow_zero else value > 0):
                return value
        raise ValueError("missing corporate-action numeric term")

    def action_cash(entry_type: str) -> list[Mapping[str, Any]]:
        return [row for row in cash_entries if str(row.get("entry_type")) == entry_type]

    try:
        if action_type in {"SPLIT", "STOCK_DIVIDEND"}:
            ratio = numeric(("ratio", "split_ratio", "share_ratio"))
            exact = int(position["quantity"]) * ratio
            quantity = int(math.floor(exact + 1e-12))
            fraction = max(0.0, exact - quantity)
            cash_rows = action_cash("CASH_IN_LIEU")
            if fraction > 1e-9:
                price = numeric(("cash_in_lieu_price",))
                if len(cash_rows) != 1 or not math.isclose(
                    float(cash_rows[0].get("amount") or 0),
                    fraction * price,
                    rel_tol=0,
                    abs_tol=1e-8,
                ):
                    raise ValueError("split cash-in-lieu mismatch")
            elif cash_rows:
                raise ValueError("unexpected split cash-in-lieu")
            if quantity <= 0:
                positions.pop(security_id, None)
                opened = trades_open.pop(security_id, None)
                if opened:
                    trades.append(
                        PaperTradeEvidence(
                            trade_id="uspt_"
                            + _sha256([opened[0], "share-ratio", action_id])[:24],
                            opened_session=opened[1],
                            closed_session=day,
                        )
                    )
            else:
                position["quantity"] = quantity
                for field in ("average_price", "stop_price", "last_price"):
                    position[field] = float(position[field]) / ratio
                successor_id = str(
                    terms.get("successor_security_id") or ""
                ).strip()
                if successor_id and successor_id != security_id:
                    successor_code = str(
                        terms.get("successor_code")
                        or terms.get("new_code")
                        or ""
                    )
                    if (
                        not successor_id.startswith("us_")
                        or not successor_code.endswith(".US")
                        or successor_id in positions
                    ):
                        raise ValueError("share-ratio successor mismatch")
                    positions.pop(security_id, None)
                    position["security_id"] = successor_id
                    position["code"] = successor_code
                    position["pit_release_id"] = action.get("pit_release_id")
                    position["manifest_sha256"] = action.get("manifest_sha256")
                    positions[successor_id] = position
                    opened = trades_open.pop(security_id, None)
                    if opened:
                        trades_open[successor_id] = opened
        elif action_type == "CASH_DIVIDEND":
            amount_per_share = numeric(
                ("amount_per_share", "cash_amount", "cash_per_share"),
                allow_zero=True,
            )
            if len(receivables) != 1:
                raise ValueError("dividend receivable count mismatch")
            expected = int(position["quantity"]) * amount_per_share
            receivable = receivables[0]
            if (
                str(receivable.get("security_id")) != security_id
                or str(receivable.get("code")) != str(position.get("code"))
                or not math.isclose(
                    float(receivable.get("amount") or 0),
                    expected,
                    rel_tol=0,
                    abs_tol=1e-8,
                )
            ):
                raise ValueError("dividend receivable amount mismatch")
            position["stop_price"] = max(
                0.000001, float(position["stop_price"]) - amount_per_share
            )
        elif action_type == "TICKER_CHANGE":
            if receivables or cash_entries:
                raise ValueError("ticker change has economic rows")
        elif action_type in {"CASH_MERGER", "DELISTING", "BANKRUPTCY"}:
            cash_per_share = numeric(
                ("cash_per_share", "cash_amount", "settlement_cash_per_share"),
                allow_zero=True,
            )
            rows = action_cash("TERMINATION_PROCEEDS")
            expected = int(position["quantity"]) * cash_per_share
            if len(rows) != 1 or not math.isclose(
                float(rows[0].get("amount") or 0),
                expected,
                rel_tol=0,
                abs_tol=1e-8,
            ):
                raise ValueError("termination proceeds mismatch")
            positions.pop(security_id, None)
            opened = trades_open.pop(security_id, None)
            if opened:
                trades.append(
                    PaperTradeEvidence(
                        trade_id="uspt_"
                        + _sha256([opened[0], "corporate", action_id])[:24],
                        opened_session=opened[1],
                        closed_session=day,
                    )
                )
        elif action_type == "STOCK_MERGER":
            ratio = numeric(("ratio", "share_ratio", "exchange_ratio"))
            target_id = str(
                terms.get("target_security_id")
                or terms.get("successor_security_id")
                or ""
            )
            target_code = str(
                terms.get("target_code")
                or terms.get("successor_code")
                or terms.get("new_code")
                or ""
            )
            if (
                not target_id.startswith("us_")
                or not target_code.endswith(".US")
                or target_id in positions
            ):
                raise ValueError("stock-merger target mismatch")
            exact = int(position["quantity"]) * ratio
            quantity = int(math.floor(exact + 1e-12))
            fraction = max(0.0, exact - quantity)
            rows = action_cash("CASH_IN_LIEU")
            if fraction > 1e-9:
                price = numeric(("cash_in_lieu_price",))
                if len(rows) != 1 or not math.isclose(
                    float(rows[0].get("amount") or 0),
                    fraction * price,
                    rel_tol=0,
                    abs_tol=1e-8,
                ):
                    raise ValueError("stock-merger cash-in-lieu mismatch")
            elif rows:
                raise ValueError("unexpected stock-merger cash-in-lieu")
            positions.pop(security_id, None)
            opened = trades_open.pop(security_id, None)
            if quantity > 0:
                positions[target_id] = {
                    **position,
                    "security_id": target_id,
                    "code": target_code,
                    "quantity": quantity,
                    "average_price": float(position["average_price"]) / ratio,
                    "stop_price": float(position["stop_price"]) / ratio,
                    "last_price": float(position["last_price"]) / ratio,
                    "pit_release_id": action.get("pit_release_id"),
                    "manifest_sha256": action.get("manifest_sha256"),
                }
                if opened:
                    # A verified stock-for-stock merger changes the stable
                    # security identity but does not manufacture a completed
                    # strategy trade for the qualification minimum.
                    trades_open[target_id] = opened
        elif action_type == "SPINOFF":
            ratio = numeric(("ratio", "share_ratio"))
            child_id = str(
                terms.get("child_security_id")
                or terms.get("successor_security_id")
                or ""
            )
            child_code = str(
                terms.get("child_code")
                or terms.get("successor_code")
                or terms.get("new_code")
                or ""
            )
            allocation = numeric(("cost_basis_fraction",), allow_zero=True)
            if (
                not child_id.startswith("us_")
                or not child_code.endswith(".US")
                or child_id in positions
                or not 0 <= allocation < 1
            ):
                raise ValueError("spinoff child/terms mismatch")
            exact = int(position["quantity"]) * ratio
            quantity = int(math.floor(exact + 1e-12))
            fraction = max(0.0, exact - quantity)
            rows = action_cash("CASH_IN_LIEU")
            if fraction > 1e-9:
                price = numeric(("cash_in_lieu_price",))
                if len(rows) != 1 or not math.isclose(
                    float(rows[0].get("amount") or 0),
                    fraction * price,
                    rel_tol=0,
                    abs_tol=1e-8,
                ):
                    raise ValueError("spinoff cash-in-lieu mismatch")
            elif rows:
                raise ValueError("unexpected spinoff cash-in-lieu")
            retained = 1.0 - allocation
            parent_average = float(position["average_price"])
            parent_stop = float(position["stop_price"])
            parent_last = float(position["last_price"])
            position["average_price"] = parent_average * retained
            position["stop_price"] = max(0.000001, parent_stop * retained)
            position["last_price"] = max(0.000001, parent_last * retained)
            if quantity > 0:
                positions[child_id] = {
                    **position,
                    "security_id": child_id,
                    "code": child_code,
                    "quantity": quantity,
                    "average_price": parent_average * allocation / ratio,
                    "stop_price": max(0.000001, parent_stop * allocation / ratio),
                    "last_price": max(0.000001, parent_last * allocation / ratio),
                    "pit_release_id": action.get("pit_release_id"),
                    "manifest_sha256": action.get("manifest_sha256"),
                }
                opened = trades_open.get(security_id)
                if opened:
                    trades_open[child_id] = opened
        else:
            raise ValueError("unsupported corporate action in replay")
    except (KeyError, TypeError, ValueError) as exc:
        failures.append(
            f"CORPORATE_ACTION_REPLAY_MISMATCH:{action_id}:{type(exc).__name__}:{exc}"
        )


def _validate_rebalance_order_contract(
    *,
    period: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]],
    normalized_signals: Sequence[Mapping[str, Any]],
) -> None:
    """Bind every persisted rebalance order to its normalized archived signal."""

    by_signal = {str(item["signal_id"]): item for item in normalized_signals}
    if len(by_signal) != len(normalized_signals) or len(orders) != len(by_signal):
        raise ValueError("rebalance order count differs from archived signals")
    period_id = str(period.get("period_id") or "")
    period_key = str(period.get("period_key") or "")
    for order in orders:
        signal_id = str(order.get("signal_id") or "")
        signal = by_signal.get(signal_id)
        if signal is None:
            raise ValueError("rebalance order has no archived signal")
        if str(order.get("payload_hash") or "") != _sha256(signal):
            raise ValueError("rebalance order payload hash does not replay")
        expected_key = (
            f"rebalance:{period_key}:{signal['security_id']}:{signal['side']}"
        )
        if str(order.get("idempotency_key") or "") != expected_key:
            raise ValueError("rebalance order idempotency key mismatch")
        if str(order.get("order_id") or "") != "uspor_" + _paper_hash(expected_key)[:24]:
            raise ValueError("rebalance order ID is not deterministic")
        exact = {
            "period_id": period_id,
            "security_id": signal["security_id"],
            "code": signal["code"],
            "pit_release_id": signal["pit_release_id"],
            "manifest_sha256": signal["manifest_sha256"],
            "side": signal["side"],
            "order_kind": "REBALANCE",
            "eligible_at": signal["available_at"],
            "expires_at": signal["valid_until"],
            "reason": signal["reason"],
        }
        for field, value in exact.items():
            if str(order.get(field) or "") != str(value):
                raise ValueError(f"rebalance order field mismatch: {field}")
        for field in ("target_weight", "stop_ratio"):
            if not math.isclose(
                float(order.get(field) or 0.0),
                float(signal[field]),
                rel_tol=0,
                abs_tol=1e-12,
            ):
                raise ValueError(f"rebalance order field mismatch: {field}")


def _archived_bars(path: Path) -> dict[str, pd.DataFrame]:
    payload = path.read_bytes()
    expected = path.stem
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"archived bar object hash mismatch: {path.name}")
    value = json.loads(payload)
    output: dict[str, pd.DataFrame] = {}
    for code, item in value.items():
        columns = [str(column) for column in item["columns"]]
        frame = pd.DataFrame(item["data"], columns=columns)
        frame.index = pd.DatetimeIndex(pd.to_datetime(item["index"], errors="raise"))
        output[str(code)] = frame
    return output


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == SHA256_LENGTH and all(character in "0123456789abcdef" for character in text)


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.astimezone(NY_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _day(value: Any) -> date:
    return _date(value)


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("qualification timestamps must be timezone-aware")
    return parsed.astimezone(NY_TZ)


def _positive_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _same_number(left: Any, right: Any) -> bool:
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(left_number)
        and math.isfinite(right_number)
        and math.isclose(left_number, right_number, rel_tol=0, abs_tol=1e-12)
    )


def _nonnegative_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _complete_intraday_quote_coverage(
    rows: Sequence[Mapping[str, Any]],
    day: date,
) -> bool:
    """Prove the persisted 60-second stop-monitoring interval is complete.

    The runtime switches from opening capture to stop monitoring immediately
    after 09:35 and stops before 16:00.  We require at least one distinct quote
    per minute of the 09:36--15:59 interval, no gap over the 90-second freshness
    ceiling, and coverage of both interval edges.
    """

    timestamps: set[datetime] = set()
    for row in rows:
        if not int(row.get("admitted") or 0) or str(row.get("reason")) != "ACCEPTED":
            continue
        try:
            fetched = _aware(row.get("fetched_at"))
        except (TypeError, ValueError):
            continue
        if fetched.date() == day:
            timestamps.add(fetched)
    ordered = sorted(timestamps)
    required_slots = (15 * 60 + 59) - (9 * 60 + 36) + 1
    if len(ordered) < required_slots:
        return False
    first = datetime.combine(day, time(9, 36), NY_TZ)
    last = datetime.combine(day, time(15, 59), NY_TZ)
    if ordered[0] > first + timedelta(seconds=90) or ordered[-1] < last - timedelta(seconds=90):
        return False
    return all(
        0 < (right - left).total_seconds() <= 90
        for left, right in zip(ordered, ordered[1:])
    )


__all__ = [
    "BIL_BENCHMARK_CODE",
    "USPaperQualificationEvidence",
    "USPaperQualificationEvidenceBuilder",
    "USPaperQualificationEvidenceError",
]
