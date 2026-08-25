"""Fail-closed runtime coordinator for the US momentum paper sleeve.

The coordinator is intentionally a thin, local process boundary around
``USMomentumPaperService``.  It performs deployment preflight checks, owns a
single-worker SQLite lease, applies a frozen-session schedule and admits only
causal TQ observations.  It has no live-order or account integration.

Daily OHLC observations are supplied by the caller's immutable raw-data
pipeline.  A TQ snapshot is never promoted into a closing daily bar.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol
from zoneinfo import ZoneInfo

from .us_paper import NY_TZ, USMomentumPaperService, USPaperState
from .us_tdx import TQPreflight, TQReadOnlyClient, USQuoteObservation, check_tq_preflight


UTC = ZoneInfo("UTC")
RUNTIME_SCHEMA_VERSION = 3
DAILY_SOURCE_SCHEMA = "us-paper-tdx-daily-v1"
DAILY_SOURCE_FREQUENCY = "1d"


class USPaperRuntimeError(RuntimeError):
    """Base error for the local US paper coordinator."""


class USPaperRuntimeLeaseError(USPaperRuntimeError):
    """Another worker owns the non-expired runtime lease."""


class USPaperRuntimeScheduleError(USPaperRuntimeError):
    """A tick does not belong to the frozen XNYS schedule."""


class _QuoteClient(Protocol):
    def market_snapshot(
        self, code: str, *, fetched_at: datetime | None = None
    ) -> USQuoteObservation: ...


@dataclass(frozen=True)
class FrozenXNYSSchedule:
    """A release-owned, immutable list of XNYS trading sessions."""

    sessions: tuple[date, ...]
    source_hash: str = ""

    def __post_init__(self) -> None:
        normalized = tuple(_date_value(item) for item in self.sessions)
        if not normalized:
            raise ValueError("frozen XNYS schedule cannot be empty")
        if tuple(sorted(set(normalized))) != normalized:
            raise ValueError("frozen XNYS sessions must be unique and increasing")
        object.__setattr__(self, "sessions", normalized)
        calculated = _hash([item.isoformat() for item in normalized])
        if self.source_hash and self.source_hash.lower() != calculated:
            raise ValueError("frozen XNYS schedule hash mismatch")
        object.__setattr__(self, "source_hash", calculated)

    def contains(self, session: date | str) -> bool:
        return _date_value(session) in self.sessions

    def as_dict(self) -> dict[str, Any]:
        return {
            "calendar": "XNYS",
            "sessions": [item.isoformat() for item in self.sessions],
            "source_hash": self.source_hash,
            "frozen": True,
        }


@dataclass(frozen=True)
class USPaperRuntimeConfig:
    state_database_path: Path
    release_id: str
    manifest_sha256: str
    worker_id: str
    poll_seconds: int = 60
    lease_seconds: int = 120
    quote_max_age_seconds: int = 90
    auto_approval_deadline: time = time(9, 20)
    staging_deadline: time = time(9, 25)
    market_open: time = time(9, 30)
    open_capture_end: time = time(9, 35)
    market_close: time = time(16, 0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_database_path", Path(self.state_database_path))
        release_id = str(self.release_id).strip()
        manifest_sha256 = str(self.manifest_sha256).strip()
        if not _is_sha256(release_id):
            raise ValueError(
                "release_id must be a lowercase 64-character SHA-256 digest"
            )
        if not _is_sha256(manifest_sha256):
            raise ValueError(
                "manifest_sha256 must be a lowercase 64-character SHA-256 digest"
            )
        object.__setattr__(self, "release_id", release_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        worker = self.worker_id.strip()
        if not worker:
            raise ValueError("worker_id is required")
        object.__setattr__(self, "worker_id", worker)
        if self.poll_seconds != 60:
            raise ValueError("US paper polling interval is fixed at 60 seconds")
        if self.lease_seconds <= self.poll_seconds:
            raise ValueError("lease_seconds must exceed the polling interval")
        if not 0 < self.quote_max_age_seconds <= 90:
            raise ValueError("quote_max_age_seconds must be in [1, 90]")
        if not (
            self.auto_approval_deadline
            <= self.staging_deadline
            < self.market_open
            < self.open_capture_end
            < self.market_close
        ):
            raise ValueError("invalid New York paper-session deadlines")


RUNTIME_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS us_paper_runtime_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK(mode='PAPER'),
    release_id TEXT NOT NULL CHECK(length(release_id)=64),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
    calendar_hash TEXT NOT NULL,
    calendar_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    blocked_reason TEXT NOT NULL DEFAULT '',
    last_tick_at TEXT,
    heartbeat_at TEXT,
    heartbeat_seq INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_runtime_lease (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_runtime_sessions (
    session_date TEXT PRIMARY KEY,
    calendar_hash TEXT NOT NULL,
    approval_state TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    approved_at TEXT,
    staging_state TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    staged_at TEXT,
    buy_blocked INTEGER NOT NULL DEFAULT 0,
    block_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_runtime_quotes (
    quote_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    session_date TEXT NOT NULL,
    purpose TEXT NOT NULL,
    source_at TEXT,
    fetched_at TEXT NOT NULL,
    admitted INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_us_paper_runtime_quote_identity
ON us_paper_runtime_quotes(code, session_date, purpose, fetched_at, payload_hash);
CREATE TABLE IF NOT EXISTS us_paper_runtime_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_runtime_release_admissions (
    admission_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    admission_key TEXT NOT NULL UNIQUE CHECK(length(admission_key)=64),
    admission_type TEXT NOT NULL CHECK(admission_type IN ('BASE', 'ROLL_FORWARD')),
    old_release_id TEXT,
    old_manifest_sha256 TEXT,
    release_id TEXT NOT NULL CHECK(length(release_id)=64),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
    membership_prefix_sha256 TEXT,
    program_admission_key TEXT,
    admitted_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    CHECK(old_release_id IS NULL OR length(old_release_id)=64),
    CHECK(old_manifest_sha256 IS NULL OR length(old_manifest_sha256)=64),
    CHECK(membership_prefix_sha256 IS NULL OR length(membership_prefix_sha256)=64),
    CHECK(program_admission_key IS NULL OR length(program_admission_key)=64)
);
CREATE TRIGGER IF NOT EXISTS us_paper_runtime_release_admissions_no_update
BEFORE UPDATE ON us_paper_runtime_release_admissions
BEGIN
    SELECT RAISE(ABORT, 'us_paper_runtime_release_admissions is append-only');
END;
CREATE TRIGGER IF NOT EXISTS us_paper_runtime_release_admissions_no_delete
BEFORE DELETE ON us_paper_runtime_release_admissions
BEGIN
    SELECT RAISE(ABORT, 'us_paper_runtime_release_admissions is append-only');
END;
"""


class USPaperRuntime:
    """Coordinate one paper-only worker against frozen XNYS sessions."""

    def __init__(
        self,
        config: USPaperRuntimeConfig,
        *,
        schedule: FrozenXNYSSchedule,
        paper: USMomentumPaperService,
        quote_client: _QuoteClient | None = None,
        preflight: Callable[[], TQPreflight | Mapping[str, Any]] = check_tq_preflight,
        _resume_existing: bool = False,
    ) -> None:
        self.config = config
        self.schedule = schedule
        self.paper = paper
        self.quote_client = quote_client or TQReadOnlyClient()
        self._preflight = preflight
        if _runtime_binding_exists(config.state_database_path) and not _resume_existing:
            raise USPaperRuntimeError(
                "runtime database is already initialized; use open_existing() to resume it"
            )
        config.state_database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(RUNTIME_SCHEMA)
            state_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(us_paper_runtime_state)"
                )
            }
            required_state_columns = {
                "schema_version",
                "mode",
                "release_id",
                "manifest_sha256",
                "calendar_hash",
                "calendar_json",
                "config_hash",
                "created_at",
            }
            if not required_state_columns.issubset(state_columns):
                raise USPaperRuntimeError(
                    "legacy unbound paper runtime database is unsafe; start a new bound database"
                )
            now = datetime.now(NY_TZ).isoformat()
            config_hash = _runtime_config_hash(config)
            calendar_json = _json(schedule.as_dict())
            connection.execute(
                """INSERT OR IGNORE INTO us_paper_runtime_state
                (singleton, schema_version, mode, release_id, manifest_sha256,
                 calendar_hash, calendar_json, config_hash, status, created_at,
                 updated_at)
                VALUES (1, ?, 'PAPER', ?, ?, ?, ?, ?, 'STARTING', ?, ?)""",
                (
                    RUNTIME_SCHEMA_VERSION,
                    config.release_id,
                    config.manifest_sha256,
                    schedule.source_hash,
                    calendar_json,
                    config_hash,
                    now,
                    now,
                ),
            )
            state = connection.execute(
                "SELECT * FROM us_paper_runtime_state WHERE singleton=1"
            ).fetchone()
            _validate_runtime_binding(state, config=config, schedule=schedule)
            base_key = _hash(
                {
                    "admission_type": "BASE",
                    "release_id": config.release_id,
                    "manifest_sha256": config.manifest_sha256,
                }
            )
            base_details = {
                "admission_type": "BASE",
                "release_id": config.release_id,
                "manifest_sha256": config.manifest_sha256,
                "policy_calendar_frozen": True,
            }
            connection.execute(
                """INSERT OR IGNORE INTO us_paper_runtime_release_admissions(
                       admission_key, admission_type, release_id,
                       manifest_sha256, admitted_at, details_json
                   ) VALUES (?, 'BASE', ?, ?, ?, ?)""",
                (
                    base_key,
                    config.release_id,
                    config.manifest_sha256,
                    now,
                    _json(base_details),
                ),
            )
            _validate_runtime_release_ledger(connection, config=config)

    @classmethod
    def open_existing(
        cls,
        config: USPaperRuntimeConfig,
        paper: USMomentumPaperService,
        preflight: Callable[[], TQPreflight | Mapping[str, Any]] | None = None,
        *,
        quote_client: _QuoteClient | None = None,
    ) -> "USPaperRuntime":
        """Resume the exact calendar and release binding frozen in SQLite.

        Recurring workers must use this entry point.  In particular, callers
        cannot rebuild a calendar from the current date when a new process
        starts; the complete persisted calendar is decoded and hash-checked
        before the runtime is opened.
        """

        if not config.state_database_path.is_file():
            raise USPaperRuntimeError("paper runtime database does not exist")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(config.state_database_path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(us_paper_runtime_state)"
                )
            }
            required = {
                "schema_version",
                "mode",
                "release_id",
                "manifest_sha256",
                "calendar_hash",
                "calendar_json",
                "config_hash",
                "created_at",
            }
            if not required.issubset(columns):
                raise USPaperRuntimeError(
                    "legacy or malformed paper runtime database cannot be resumed"
                )
            row = connection.execute(
                "SELECT * FROM us_paper_runtime_state WHERE singleton=1"
            ).fetchone()
            schedule = _validate_runtime_binding(row, config=config)
            _validate_runtime_release_ledger(connection, config=config)
        except sqlite3.DatabaseError as exc:
            raise USPaperRuntimeError("paper runtime database is unreadable") from exc
        finally:
            if connection is not None:
                connection.close()
        return cls(
            config,
            schedule=schedule,
            paper=paper,
            quote_client=quote_client,
            preflight=preflight if preflight is not None else check_tq_preflight,
            _resume_existing=True,
        )

    def current_decision_binding(self) -> dict[str, Any]:
        """Return the explicit append-only PIT binding for the next period."""

        with self._connect() as connection:
            rows = _validate_runtime_release_ledger(connection, config=self.config)
            head = rows[-1]
            return {
                "release_id": str(head["release_id"]),
                "manifest_sha256": str(head["manifest_sha256"]),
                "admission_key": str(head["admission_key"]),
                "admission_seq": int(head["admission_seq"]),
            }

    def admit_paper_release(self, admission: Mapping[str, Any]) -> dict[str, Any]:
        """Mirror one programme-verified rolling release into the runtime ledger."""

        payload = admission.get("payload")
        payload_sha256 = str(admission.get("payload_sha256") or "")
        if (
            not isinstance(payload, Mapping)
            or not _is_sha256(payload_sha256)
            or _hash(payload) != payload_sha256
        ):
            raise USPaperRuntimeError(
                "runtime release admission lacks an intact programme audit payload"
            )
        admission_key = str(admission.get("admission_key") or "")
        old_release_id = str(admission.get("old_release_id") or "")
        old_manifest = str(admission.get("old_manifest_sha256") or "")
        release_id = str(admission.get("release_id") or "")
        manifest = str(admission.get("manifest_sha256") or "")
        prefix_sha256 = str(admission.get("membership_prefix_sha256") or "")
        if not all(
            _is_sha256(value)
            for value in (
                admission_key,
                old_release_id,
                old_manifest,
                release_id,
                manifest,
                prefix_sha256,
            )
        ):
            raise USPaperRuntimeError(
                "runtime release admission requires verified SHA-256 bindings"
            )
        if str(admission.get("admission_type")) != "ROLL_FORWARD":
            raise USPaperRuntimeError("runtime accepts only ROLL_FORWARD admissions")
        expected_key = _hash(
            {
                "program_id": "us_momentum_v1",
                "old_release_id": old_release_id,
                "release_id": release_id,
                "manifest_sha256": manifest,
                "membership_prefix_sha256": prefix_sha256,
            }
        )
        if admission_key != expected_key:
            raise USPaperRuntimeError(
                "runtime release admission key does not match programme audit"
            )
        expected_payload_fields = {
            "admission_type": "ROLL_FORWARD",
            "old_release_id": old_release_id,
            "old_manifest_sha256": old_manifest,
            "release_id": release_id,
            "manifest_sha256": manifest,
            "membership_prefix_sha256": prefix_sha256,
        }
        if any(payload.get(key) != value for key, value in expected_payload_fields.items()):
            raise USPaperRuntimeError(
                "runtime release admission payload conflicts with its bindings"
            )
        if not all(
            payload.get(key) is True
            for key in ("catalog_verified", "manifest_verified", "cas_verified")
        ):
            raise USPaperRuntimeError(
                "runtime release admission was not fully storage-verified"
            )
        details = {
            "admission_type": "ROLL_FORWARD",
            "program_admission_key": admission_key,
            "old_release_id": old_release_id,
            "old_manifest_sha256": old_manifest,
            "release_id": release_id,
            "manifest_sha256": manifest,
            "membership_prefix_sha256": prefix_sha256,
            "old_membership_artifact_sha256": admission.get(
                "old_membership_artifact_sha256"
            ),
            "membership_artifact_sha256": admission.get(
                "membership_artifact_sha256"
            ),
            "old_max_decision_date": admission.get("old_max_decision_date"),
            "max_decision_date": admission.get("max_decision_date"),
            "old_row_count": admission.get("old_row_count"),
            "row_count": admission.get("row_count"),
            "program_payload_sha256": payload_sha256,
        }
        already_admitted = False
        with self._transaction() as connection:
            rows = _validate_runtime_release_ledger(connection, config=self.config)
            head = rows[-1]
            if str(head["release_id"]) == release_id:
                if (
                    str(head["manifest_sha256"]) != manifest
                    or str(head["program_admission_key"] or "") != admission_key
                ):
                    raise USPaperRuntimeError(
                        "runtime release admission conflicts with existing head"
                    )
                already_admitted = True
            elif (
                str(head["release_id"]) != old_release_id
                or str(head["manifest_sha256"]) != old_manifest
            ):
                raise USPaperRuntimeError(
                    "runtime release admission does not extend its current head"
                )
            if not already_admitted:
                now = datetime.now(NY_TZ).isoformat()
                connection.execute(
                    """INSERT INTO us_paper_runtime_release_admissions(
                           admission_key, admission_type,
                           old_release_id, old_manifest_sha256,
                           release_id, manifest_sha256,
                           membership_prefix_sha256, program_admission_key,
                           admitted_at, details_json
                       ) VALUES (?, 'ROLL_FORWARD', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        admission_key,
                        old_release_id,
                        old_manifest,
                        release_id,
                        manifest,
                        prefix_sha256,
                        admission_key,
                        now,
                        _json(details),
                    ),
                )
            _validate_runtime_release_ledger(connection, config=self.config)
        return self.status()

    def _validate_paper_period_bindings(
        self, paper_status: Mapping[str, Any]
    ) -> str | None:
        with self._connect() as connection:
            rows = _validate_runtime_release_ledger(connection, config=self.config)
        admitted = {
            (str(row["release_id"]), str(row["manifest_sha256"])) for row in rows
        }
        for period in paper_status.get("periods", []):
            binding = (
                str(period.get("pit_release_id") or ""),
                str(period.get("manifest_sha256") or ""),
            )
            if binding not in admitted:
                return f"UNADMITTED_PERIOD_RELEASE:{binding[0]}:{binding[1]}"
        return None

    def tick(
        self,
        *,
        now: datetime,
        daily_bars: Iterable[Mapping[str, Any]] = (),
        corporate_actions: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Run one deterministic 60-second worker iteration.

        ``now`` must be timezone-aware.  It is normalized to New York time,
        and its local date must exist in the injected frozen schedule.
        """

        current = _aware(now)
        session_day = current.date()
        if not self.schedule.contains(session_day):
            raise USPaperRuntimeScheduleError(
                f"{session_day.isoformat()} is absent from the frozen XNYS schedule"
            )

        self._acquire_and_heartbeat(current)
        with self._connect() as connection:
            admitted = _validate_runtime_release_ledger(
                connection, config=self.config
            )[-1]
        try:
            self.paper.apply_corporate_actions(
                session_day,
                tuple(corporate_actions),
                now=current,
                pit_release_id=str(admitted["release_id"]),
                manifest_sha256=str(admitted["manifest_sha256"]),
            )
        except Exception as exc:
            detail = f"CORPORATE_ACTION_LEDGER_ERROR:{type(exc).__name__}:{exc}"
            self._set_runtime("PAPER_BLOCKED", detail, current)
            self._event(
                "CORPORATE_ACTION_LEDGER_ERROR",
                "CRITICAL",
                {"detail": detail},
                current,
            )
            return self.status()

        preflight = self._run_preflight()
        paper_status = self.paper.status()
        release_error = self._validate_paper_period_bindings(paper_status)
        if release_error is not None:
            self._set_runtime("PAPER_BLOCKED", release_error, current)
            self._event(
                "UNADMITTED_PERIOD_RELEASE",
                "CRITICAL",
                {"detail": release_error},
                current,
            )
            return self.status()
        killed = str(paper_status["account"]["status"]) == USPaperState.KILLED.value
        if not _preflight_ready(preflight):
            detail = _preflight_detail(preflight)
            state = "KILLED" if killed else "PAPER_BLOCKED"
            reason = (
                "RISK_EXECUTION_BLOCKED_TQ_PREFLIGHT:" + detail
                if killed
                else detail
            )
            self._set_runtime(state, reason, current)
            self._event(
                "TQ_PREFLIGHT_BLOCKED_WHILE_KILLED"
                if killed
                else "TQ_PREFLIGHT_BLOCKED",
                "CRITICAL",
                {"detail": detail, "risk_exits_executed": False},
                current,
            )
            return self.status()

        session = self._prepare_session(current, paper_status)
        positions = {
            str(item["code"]): item for item in paper_status.get("positions", [])
        }
        orders = [
            item
            for item in paper_status.get("orders", [])
            if str(item.get("status")) == "WAITING_OPEN"
            and str(item.get("eligible_at", ""))[:10] == session_day.isoformat()
        ]

        observations: list[Mapping[str, Any]] = []
        open_at = datetime.combine(session_day, self.config.market_open, NY_TZ)
        capture_end = datetime.combine(
            session_day, self.config.open_capture_end, NY_TZ
        )
        close_at = datetime.combine(session_day, self.config.market_close, NY_TZ)

        if open_at <= current <= capture_end:
            staged = session["staging_state"] == "STAGED"
            sell_codes = {
                str(item["code"])
                for item in orders
                if str(item.get("side")) == "SELL" and staged
            }
            buy_codes = {
                str(item["code"])
                for item in orders
                if str(item.get("side")) == "BUY" and staged
            }
            position_codes = set(positions)
            snapshots = self._snapshots(
                position_codes | sell_codes | buy_codes,
                purpose="OPEN",
                session_day=session_day,
                now=current,
            )
            missing_positions = sorted(position_codes - set(snapshots))
            if missing_positions:
                reason = "MISSING_HELD_QUOTE:" + ",".join(missing_positions)
                self._block_buys(session_day, reason, current)
                buy_codes.clear()
            missing_buys = sorted(buy_codes - set(snapshots))
            if missing_buys:
                self._block_buys(
                    session_day,
                    "INVALID_OR_MISSING_BUY_QUOTE:" + ",".join(missing_buys),
                    current,
                )
                buy_codes.clear()
            missing_risk_sells = sorted(
                str(item["code"])
                for item in orders
                if str(item.get("side")) == "SELL"
                and str(item.get("risk_class")) == "RISK_EXIT"
                and str(item["code"]) not in snapshots
            )
            missing_normal_sells = sorted(
                str(item["code"])
                for item in orders
                if str(item.get("side")) == "SELL"
                and str(item.get("risk_class")) != "RISK_EXIT"
                and str(item["code"]) not in snapshots
            )
            if missing_risk_sells:
                self._block_buys(
                    session_day,
                    "INVALID_OR_MISSING_RISK_SELL_QUOTE:"
                    + ",".join(missing_risk_sells),
                    current,
                )
            if missing_normal_sells:
                self._block_buys(
                    session_day,
                    "INVALID_OR_MISSING_NORMAL_SELL_QUOTE:"
                    + ",".join(missing_normal_sells),
                    current,
                )
            admitted = position_codes | sell_codes | buy_codes
            for code in sorted(admitted & set(snapshots)):
                quote = snapshots[code]
                observations.append(
                    {
                        "code": code,
                        "session_date": session_day.isoformat(),
                        "kind": "OPEN",
                        "event_at": quote.source_at,
                        "available_at": quote.fetched_at,
                        "open": quote.open,
                        "idempotency_key": _quote_observation_key(
                            quote, "OPEN", session_day
                        ),
                    }
                )

        elif capture_end < current < close_at and positions:
            snapshots = self._snapshots(
                set(positions),
                purpose="INTRADAY_STOP",
                session_day=session_day,
                now=current,
            )
            missing = sorted(set(positions) - set(snapshots))
            if missing:
                self._block_buys(
                    session_day,
                    "MISSING_HELD_QUOTE:" + ",".join(missing),
                    current,
                )
            for code, quote in snapshots.items():
                last = quote.last
                stop = _optional_positive(positions[code].get("stop_price"))
                if last is not None and stop is not None and last <= stop:
                    bid = _optional_positive(quote.bid)
                    reference = bid if bid is not None else float(last)
                    try:
                        result = self.paper.execute_intraday_stop(
                            {
                                "code": code,
                                "security_id": positions[code].get("security_id"),
                                "pit_release_id": positions[code].get("pit_release_id"),
                                "manifest_sha256": positions[code].get(
                                    "manifest_sha256"
                                ),
                                "session_date": session_day.isoformat(),
                                "kind": "INTRADAY",
                                "event_at": quote.source_at,
                                "available_at": quote.fetched_at,
                                "close": reference,
                                "idempotency_key": _quote_observation_key(
                                    quote, "INTRADAY_STOP", session_day
                                ),
                            },
                            now=current,
                        )
                    except Exception as exc:
                        reason = (
                            f"INTRADAY_STOP_EXECUTION_ERROR:{code}:"
                            f"{type(exc).__name__}:{exc}"
                        )
                        self._block_buys(session_day, reason, current)
                        self._event(
                            "INTRADAY_STOP_EXECUTION_ERROR",
                            "CRITICAL",
                            {"code": code, "detail": reason},
                            current,
                        )
                    else:
                        self._event(
                            "INTRADAY_STOP_BREACH",
                            "HIGH",
                            {
                                "code": code,
                                "last": last,
                                "bid": quote.bid,
                                "sell_reference": reference,
                                "stop_price": stop,
                                "action": "SIMULATED_STOP_FILLED_FROM_FRESH_QUOTE",
                                "fill_id": (
                                    result["fill"]["fill_id"]
                                    if result.get("fill")
                                    else None
                                ),
                            },
                            current,
                        )

        if current >= close_at:
            observations.extend(
                self._normalize_daily_bars(daily_bars, session_day, current)
            )

        # Qualification requires both the raw BIL mark and a causal daily
        # total-return factor (BILTR open=prior adjusted close,
        # close=current adjusted close). Raw BIL alone omits distributions and
        # can falsely lower the promotion hurdle.
        if current >= close_at:
            daily_codes = {
                str(item.get("code", "")).upper()
                for item in observations
                if str(item.get("kind", "")).upper() == "DAILY"
            }
            missing_benchmarks = sorted(
                {"BIL.US", "BILTR.US"} - daily_codes
            )
            if missing_benchmarks:
                self._block_buys(
                    session_day,
                    "MISSING_BIL_BENCHMARK:" + ",".join(missing_benchmarks),
                    current,
                )

        result = self.paper.tick(
            session_day,
            now=current,
            observations=observations,
        )
        runtime_status = (
            "KILLED"
            if str(result.get("account_status")) == USPaperState.KILLED.value
            else "DATA_DEGRADED"
            if str(result.get("account_status")) == USPaperState.DATA_DEGRADED.value
            or self._session_buy_blocked(session_day)
            else "RUNNING"
        )
        reason = ""
        if runtime_status == "KILLED":
            reason = "BUY_DISABLED_RISK_EXITS_REMAIN_ACTIVE"
        elif runtime_status != "RUNNING":
            reason = self._session_block_reason(session_day)
            if not reason:
                result_session = result.get("session")
                if isinstance(result_session, Mapping):
                    reason = str(result_session.get("degraded_reason") or "")
            if not reason:
                reason = "PAPER_EXECUTOR_DATA_DEGRADED"
        self._set_runtime(runtime_status, reason, current)
        return self.status()

    def kill(self, *, reason: str, now: datetime) -> dict[str, Any]:
        """Disable BUYs permanently while retaining quote-gated risk exits."""

        if not reason.strip():
            raise ValueError("kill reason is required")
        current = _aware(now)
        self.paper.kill(reason=reason, now=current)
        self._set_runtime(
            "KILLED", "BUY_DISABLED_RISK_EXITS_REMAIN_ACTIVE", current
        )
        self._event(
            "RUNTIME_KILL",
            "CRITICAL",
            {
                "reason": reason.strip(),
                "behavior": "BUY_CANCELLED_SELLS_REMAIN_QUOTE_GATED",
                "selective_risk_sell_continuation_supported": True,
            },
            current,
        )
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            state = dict(
                connection.execute(
                    "SELECT * FROM us_paper_runtime_state WHERE singleton=1"
                ).fetchone()
            )
            lease = connection.execute(
                "SELECT * FROM us_paper_runtime_lease WHERE singleton=1"
            ).fetchone()
            sessions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM us_paper_runtime_sessions ORDER BY session_date DESC"
                ).fetchall()
            ]
            quotes = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM us_paper_runtime_quotes ORDER BY recorded_at, quote_id"
                ).fetchall()
            ]
            events = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM us_paper_runtime_events ORDER BY occurred_at, event_id"
                ).fetchall()
            ]
            release_rows = _validate_runtime_release_ledger(
                connection, config=self.config
            )
            releases = [dict(row) for row in release_rows]
            decision_release = releases[-1]
        return {
            "mode": "PAPER",
            "paper_only": True,
            "broker_writes_enabled": False,
            "bindings": {
                "release_id": self.config.release_id,
                "manifest_sha256": self.config.manifest_sha256,
                "config_hash": _runtime_config_hash(self.config),
                "calendar_hash": self.schedule.source_hash,
                "schema_version": RUNTIME_SCHEMA_VERSION,
            },
            "decision_release": {
                "release_id": decision_release["release_id"],
                "manifest_sha256": decision_release["manifest_sha256"],
                "admission_key": decision_release["admission_key"],
                "admission_seq": decision_release["admission_seq"],
            },
            "release_admissions": releases,
            "calendar": self.schedule.as_dict(),
            "runtime": state,
            "lease": dict(lease) if lease is not None else None,
            "schedule": self.schedule.as_dict(),
            "sessions": sessions,
            "quotes": quotes,
            "events": events,
            "paper": self.paper.status(),
            "poll_seconds": self.config.poll_seconds,
            "kill_policy": {
                "behavior": "PERMANENT_BUY_DISABLE_QUOTE_GATED_SELL_CONTINUATION",
                "selective_risk_sell_continuation_supported": True,
                "risk_classification": "STOP_ORDER_OR_EXPLICIT_REASON_ALLOWLIST",
                "normal_sell_policy": "ALLOWED_AS_EXPOSURE_REDUCTION",
            },
        }

    def _prepare_session(
        self, current: datetime, paper_status: Mapping[str, Any]
    ) -> dict[str, Any]:
        session_day = current.date()
        session_key = session_day.isoformat()
        periods = [
            item
            for item in paper_status.get("periods", [])
            if str(item.get("execution_session")) == session_key
        ]
        approval_deadline = datetime.combine(
            session_day, self.config.auto_approval_deadline, NY_TZ
        )
        staging_deadline = datetime.combine(
            session_day, self.config.staging_deadline, NY_TZ
        )
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO us_paper_runtime_sessions
                (session_date, calendar_hash, updated_at)
                VALUES (?, ?, ?)""",
                (session_key, self.schedule.source_hash, current.isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM us_paper_runtime_sessions WHERE session_date=?",
                (session_key,),
            ).fetchone()
            if periods and row["approval_state"] == "NOT_REQUIRED":
                if current <= approval_deadline:
                    connection.execute(
                        """UPDATE us_paper_runtime_sessions SET
                        approval_state='AUTO_APPROVED', approved_at=?, updated_at=?
                        WHERE session_date=?""",
                        (current.isoformat(), current.isoformat(), session_key),
                    )
                else:
                    self._block_session_tx(
                        connection,
                        session_key,
                        "APPROVAL_DEADLINE_MISSED",
                        current,
                        approval_state="MISSED",
                    )
            row = connection.execute(
                "SELECT * FROM us_paper_runtime_sessions WHERE session_date=?",
                (session_key,),
            ).fetchone()
            if row["approval_state"] == "AUTO_APPROVED" and row["staging_state"] == "NOT_REQUIRED":
                if current <= staging_deadline:
                    connection.execute(
                        """UPDATE us_paper_runtime_sessions SET
                        staging_state='STAGED', staged_at=?, updated_at=?
                        WHERE session_date=?""",
                        (current.isoformat(), current.isoformat(), session_key),
                    )
                else:
                    self._block_session_tx(
                        connection,
                        session_key,
                        "STAGING_DEADLINE_MISSED",
                        current,
                        staging_state="MISSED",
                    )
            return dict(
                connection.execute(
                    "SELECT * FROM us_paper_runtime_sessions WHERE session_date=?",
                    (session_key,),
                ).fetchone()
            )

    def _snapshots(
        self,
        codes: set[str],
        *,
        purpose: str,
        session_day: date,
        now: datetime,
    ) -> dict[str, USQuoteObservation]:
        accepted: dict[str, USQuoteObservation] = {}
        for code in sorted(codes):
            try:
                quote = self.quote_client.market_snapshot(code, fetched_at=now)
                reason = _validate_quote(
                    quote,
                    session_day=session_day,
                    now=now,
                    purpose=purpose,
                    max_age_seconds=self.config.quote_max_age_seconds,
                    market_open=self.config.market_open,
                )
            except Exception as exc:
                quote = None
                reason = f"QUOTE_ERROR:{type(exc).__name__}:{exc}"
            admitted = reason == "ACCEPTED"
            self._record_quote(
                code=code,
                quote=quote,
                purpose=purpose,
                session_day=session_day,
                admitted=admitted,
                reason=reason,
                now=now,
            )
            if admitted and quote is not None:
                accepted[code] = quote
        return accepted

    def _normalize_daily_bars(
        self,
        values: Iterable[Mapping[str, Any]],
        session_day: date,
        now: datetime,
    ) -> list[dict[str, Any]]:
        close_at = datetime.combine(session_day, self.config.market_close, NY_TZ)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in values:
            code = str(source.get("code") or "").strip().upper()
            if not code.endswith(".US") or code in seen:
                raise ValueError("raw daily bars require unique .US codes")
            bar_day = _date_value(source.get("session_date", session_day))
            if bar_day != session_day:
                raise ValueError("raw daily bar is from a different session")
            observed_at = _aware(source.get("observed_at", now))
            if observed_at < close_at or observed_at > now:
                raise ValueError("raw daily bar must be observed after close and by now")
            opening = _positive(source.get("open"), "open")
            high = _positive(source.get("high"), "high")
            low = _positive(source.get("low"), "low")
            closing = _positive(source.get("close"), "close")
            if low > min(opening, closing) or high < max(opening, closing) or low > high:
                raise ValueError("invalid raw daily OHLC relationship")
            provenance = _normalize_tdx_daily_provenance(
                source,
                code=code,
                session_day=session_day,
                opening=opening,
                high=high,
                low=low,
                closing=closing,
            )
            seen.add(code)
            result.append(
                {
                    "code": code,
                    "session_date": session_day.isoformat(),
                    "kind": "DAILY",
                    "event_at": close_at,
                    "available_at": observed_at,
                    "open": opening,
                    "high": high,
                    "low": low,
                    "close": closing,
                    **provenance,
                    "idempotency_key": _hash(
                        {
                            "kind": "RAW_DAILY",
                            "code": code,
                            "session_date": session_day.isoformat(),
                            "observed_at": observed_at.isoformat(),
                            "ohlc": [opening, high, low, closing],
                            "source_sha256": provenance["source_sha256"],
                        }
                    ),
                }
            )
        return result

    def _run_preflight(self) -> TQPreflight | Mapping[str, Any]:
        try:
            return self._preflight()
        except Exception as exc:
            return {
                "ready": False,
                "status": "PAPER_BLOCKED",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _acquire_and_heartbeat(self, now: datetime) -> None:
        expires = now + timedelta(seconds=self.config.lease_seconds)
        with self._transaction() as connection:
            lease = connection.execute(
                "SELECT * FROM us_paper_runtime_lease WHERE singleton=1"
            ).fetchone()
            if lease is not None:
                owner = str(lease["owner"])
                expiry = _aware(lease["expires_at"])
                if owner != self.config.worker_id and expiry > now:
                    raise USPaperRuntimeLeaseError(
                        f"runtime lease is owned by {owner} until {expiry.isoformat()}"
                    )
                generation = int(lease["generation"]) + 1
                acquired = (
                    str(lease["acquired_at"])
                    if owner == self.config.worker_id
                    else now.isoformat()
                )
                connection.execute(
                    """UPDATE us_paper_runtime_lease SET owner=?, acquired_at=?,
                    expires_at=?, generation=? WHERE singleton=1""",
                    (
                        self.config.worker_id,
                        acquired,
                        expires.isoformat(),
                        generation,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO us_paper_runtime_lease
                    (singleton, owner, acquired_at, expires_at, generation)
                    VALUES (1, ?, ?, ?, 1)""",
                    (
                        self.config.worker_id,
                        now.isoformat(),
                        expires.isoformat(),
                    ),
                )
            connection.execute(
                """UPDATE us_paper_runtime_state SET heartbeat_at=?,
                heartbeat_seq=heartbeat_seq+1, worker_id=?, last_tick_at=?, updated_at=?
                WHERE singleton=1""",
                (
                    now.isoformat(),
                    self.config.worker_id,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    def _set_runtime(self, status: str, reason: str, now: datetime) -> None:
        with self._transaction() as connection:
            connection.execute(
                """UPDATE us_paper_runtime_state SET status=?, blocked_reason=?,
                last_tick_at=?, updated_at=? WHERE singleton=1""",
                (status, reason, now.isoformat(), now.isoformat()),
            )

    def _record_quote(
        self,
        *,
        code: str,
        quote: USQuoteObservation | None,
        purpose: str,
        session_day: date,
        admitted: bool,
        reason: str,
        now: datetime,
    ) -> None:
        payload = asdict(quote) if quote is not None else {"code": code}
        payload_hash = _hash(payload)
        fetched_at = quote.fetched_at if quote is not None else now
        source_at = quote.source_at if quote is not None else None
        identity = {
            "code": code,
            "session": session_day.isoformat(),
            "purpose": purpose,
            "fetched_at": fetched_at.isoformat(),
            "payload_hash": payload_hash,
        }
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO us_paper_runtime_quotes
                (quote_id, code, session_date, purpose, source_at, fetched_at,
                 admitted, reason, payload_hash, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "usprq_" + _hash(identity)[:24],
                    code,
                    session_day.isoformat(),
                    purpose,
                    source_at.isoformat() if source_at is not None else None,
                    fetched_at.isoformat(),
                    int(admitted),
                    reason,
                    payload_hash,
                    now.isoformat(),
                ),
            )

    def _block_buys(self, session_day: date, reason: str, now: datetime) -> None:
        with self._transaction() as connection:
            self._block_session_tx(
                connection, session_day.isoformat(), reason, now
            )
        self._event("BUY_GATE_BLOCKED", "HIGH", {"reason": reason}, now)

    @staticmethod
    def _block_session_tx(
        connection: sqlite3.Connection,
        session_key: str,
        reason: str,
        now: datetime,
        *,
        approval_state: str | None = None,
        staging_state: str | None = None,
    ) -> None:
        sets = ["buy_blocked=1", "block_reason=?", "updated_at=?"]
        values: list[Any] = [reason, now.isoformat()]
        if approval_state is not None:
            sets.append("approval_state=?")
            values.append(approval_state)
        if staging_state is not None:
            sets.append("staging_state=?")
            values.append(staging_state)
        values.append(session_key)
        connection.execute(
            f"UPDATE us_paper_runtime_sessions SET {', '.join(sets)} WHERE session_date=?",
            tuple(values),
        )

    def _session_buy_blocked(self, session_day: date) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT buy_blocked FROM us_paper_runtime_sessions WHERE session_date=?",
                (session_day.isoformat(),),
            ).fetchone()
        return bool(row and row["buy_blocked"])

    def _session_block_reason(self, session_day: date) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT block_reason FROM us_paper_runtime_sessions WHERE session_date=?",
                (session_day.isoformat(),),
            ).fetchone()
        return str(row["block_reason"] if row else "")

    def _event(
        self,
        event_type: str,
        severity: str,
        details: Mapping[str, Any],
        now: datetime,
    ) -> None:
        canonical = {
            "type": event_type,
            "severity": severity,
            "occurred_at": now.isoformat(),
            "details": dict(details),
        }
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO us_paper_runtime_events
                (event_id, event_type, severity, occurred_at, details_json)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    "uspre_" + _hash(canonical)[:24],
                    event_type,
                    severity,
                    now.isoformat(),
                    _json(details),
                ),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.config.state_database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection


def windows_task_scheduler_spec(
    *,
    python_executable: str,
    project_root: str | Path,
    task_name: str = "ResearchPlatform-USMomentum-Paper",
) -> dict[str, Any]:
    """Return the audited paper-only Task Scheduler specification."""

    executable = str(python_executable).strip()
    root = str(Path(project_root).resolve())
    name = task_name.strip()
    if not executable or not name:
        raise ValueError("python_executable and task_name are required")
    arguments = "-m research_platform us-paper tick"
    return {
        "registered": False,
        "task_name": name,
        "trigger": {"type": "DAILY_REPEATING", "interval_seconds": 60},
        "action": {
            "program": executable,
            "arguments": arguments,
            "working_directory": root,
        },
        "settings": {
            "run_only_if_network_available": True,
            "multiple_instances": "IGNORE_NEW",
            "restart_interval_seconds": 60,
        },
        "paper_only": True,
        "broker_writes_enabled": False,
        "operator_note": "Registration is allowed only after PAPER_COLLECTING is active.",
    }


def install_windows_task(
    spec: Mapping[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Register the reviewed 60-second paper worker using Task Scheduler XML."""

    normalized = _validate_task_spec(spec)
    action = normalized["action"]
    start_boundary = datetime.now().astimezone().replace(microsecond=0).isoformat()
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>US momentum paper-only worker; no broker writes.</Description></RegistrationInfo>
  <Triggers><TimeTrigger><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>{escape(start_boundary)}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled><ExecutionTimeLimit>PT5M</ExecutionTimeLimit></Settings>
  <Actions Context="Author"><Exec><Command>{escape(action['program'])}</Command><Arguments>{escape(action['arguments'])}</Arguments><WorkingDirectory>{escape(action['working_directory'])}</WorkingDirectory></Exec></Actions>
</Task>"""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-16",
            suffix=".xml",
            prefix="us-paper-task-",
            delete=False,
        ) as handle:
            handle.write(xml)
            temporary_path = Path(handle.name)
        completed = runner(
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                normalized["task_name"],
                "/XML",
                str(temporary_path),
                "/F",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise USPaperRuntimeError(
            "Task Scheduler registration failed: "
            + str(completed.stderr or completed.stdout).strip()
        )
    return {
        **normalized,
        "registered": True,
        "scheduler_output": str(completed.stdout or "").strip(),
    }


def windows_task_status(
    task_name: str = "ResearchPlatform-USMomentum-Paper",
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    name = _task_name(task_name)
    completed = runner(
        ["schtasks.exe", "/Query", "/TN", name, "/FO", "LIST", "/V"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "task_name": name,
        "registered": completed.returncode == 0,
        "scheduler_output": str(completed.stdout or completed.stderr or "").strip(),
        "paper_only": True,
        "broker_writes_enabled": False,
    }


def remove_windows_task(
    task_name: str = "ResearchPlatform-USMomentum-Paper",
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    name = _task_name(task_name)
    before = windows_task_status(name, runner=runner)
    if not before["registered"]:
        return {**before, "removed": False, "reason": "TASK_NOT_REGISTERED"}
    completed = runner(
        ["schtasks.exe", "/Delete", "/TN", name, "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise USPaperRuntimeError(
            "Task Scheduler removal failed: "
            + str(completed.stderr or completed.stdout).strip()
        )
    return {
        "task_name": name,
        "registered": False,
        "removed": True,
        "scheduler_output": str(completed.stdout or "").strip(),
        "paper_only": True,
        "broker_writes_enabled": False,
    }


def _task_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 120 or any(char in name for char in "\r\n\0"):
        raise ValueError("Task Scheduler task_name is invalid")
    return name


def _validate_task_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise ValueError("Task Scheduler specification must be a mapping")
    action = spec.get("action")
    trigger = spec.get("trigger")
    if not isinstance(action, Mapping) or not isinstance(trigger, Mapping):
        raise ValueError("Task Scheduler action and trigger are required")
    if int(trigger.get("interval_seconds") or 0) != 60:
        raise ValueError("US paper worker interval is fixed at 60 seconds")
    program = str(action.get("program") or "").strip()
    arguments = str(action.get("arguments") or "").strip()
    working_directory = str(action.get("working_directory") or "").strip()
    if not program or arguments != "-m research_platform us-paper tick":
        raise ValueError("Task Scheduler action is not the paper-only worker")
    root = Path(working_directory).resolve()
    if not root.is_dir():
        raise ValueError("Task Scheduler working directory does not exist")
    return {
        **dict(spec),
        "task_name": _task_name(spec.get("task_name")),
        "action": {
            "program": program,
            "arguments": arguments,
            "working_directory": str(root),
        },
        "paper_only": True,
        "broker_writes_enabled": False,
    }


def _validate_quote(
    quote: USQuoteObservation,
    *,
    session_day: date,
    now: datetime,
    purpose: str,
    max_age_seconds: int,
    market_open: time,
) -> str:
    if quote.source_at is None:
        return "MISSING_SOURCE_TIMESTAMP"
    try:
        fetched = _aware(quote.fetched_at)
        source = _aware(quote.source_at)
    except ValueError:
        return "INVALID_TIMEZONE"
    if fetched > now or source > now or source > fetched:
        return "FUTURE_OR_REVERSED_TIMESTAMP"
    if fetched.date() != session_day or source.date() != session_day:
        return "WRONG_SESSION_TIMESTAMP"
    if (now - fetched).total_seconds() > max_age_seconds:
        return "STALE_FETCH_TIMESTAMP"
    if (now - source).total_seconds() > max_age_seconds:
        return "STALE_SOURCE_TIMESTAMP"
    market_status = str(quote.market_status or "").strip().upper()
    if not market_status or market_status in {
        "UNKNOWN",
        "CLOSED",
        "CLOSE",
        "HALTED",
        "SUSPENDED",
        "PREMARKET",
        "PRE_MARKET",
    }:
        return "INVALID_MARKET_STATUS"
    if purpose == "OPEN":
        open_at = datetime.combine(session_day, market_open, NY_TZ)
        if source < open_at:
            return "PREOPEN_SOURCE_TIMESTAMP"
        if quote.open is None or _optional_positive(quote.open) is None:
            return "MISSING_OPEN"
    elif purpose == "INTRADAY_STOP":
        if quote.last is None or _optional_positive(quote.last) is None:
            return "MISSING_LAST"
    else:
        return "UNKNOWN_PURPOSE"
    return "ACCEPTED"


def _preflight_ready(value: TQPreflight | Mapping[str, Any]) -> bool:
    if isinstance(value, TQPreflight):
        return value.ready
    return bool(value.get("ready", False))


def _preflight_detail(value: TQPreflight | Mapping[str, Any]) -> str:
    if isinstance(value, TQPreflight):
        failed = [item for item in value.checks if not item.ok]
        return failed[0].detail if failed else "TQ preflight did not report READY"
    return str(value.get("error") or value.get("detail") or "TQ preflight blocked")


def _quote_observation_key(
    quote: USQuoteObservation, kind: str, session_day: date
) -> str:
    return _hash(
        {
            "kind": kind,
            "code": quote.code,
            "session_date": session_day.isoformat(),
            "source_at": quote.source_at.isoformat() if quote.source_at else None,
            "fetched_at": quote.fetched_at.isoformat(),
            "open": quote.open,
            "last": quote.last,
            "bid": quote.bid,
            "ask": quote.ask,
        }
    )


def _runtime_config_hash(config: USPaperRuntimeConfig) -> str:
    return _hash(
        {
            "poll_seconds": config.poll_seconds,
            "lease_seconds": config.lease_seconds,
            "quote_max_age_seconds": config.quote_max_age_seconds,
            "auto_approval_deadline": config.auto_approval_deadline.isoformat(),
            "staging_deadline": config.staging_deadline.isoformat(),
            "market_open": config.market_open.isoformat(),
            "open_capture_end": config.open_capture_end.isoformat(),
            "market_close": config.market_close.isoformat(),
        }
    )


def _runtime_binding_exists(path: Path) -> bool:
    """Return whether ``path`` already contains initialized runtime metadata."""

    if not path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=10.0)
        table = connection.execute(
            """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='us_paper_runtime_state'"""
        ).fetchone()
        if table is None:
            return False
        return (
            connection.execute(
                "SELECT 1 FROM us_paper_runtime_state WHERE singleton=1"
            ).fetchone()
            is not None
        )
    except sqlite3.DatabaseError as exc:
        raise USPaperRuntimeError("paper runtime database is unreadable") from exc
    finally:
        if connection is not None:
            connection.close()


def _validate_runtime_binding(
    row: sqlite3.Row | None,
    *,
    config: USPaperRuntimeConfig,
    schedule: FrozenXNYSSchedule | None = None,
) -> FrozenXNYSSchedule:
    """Validate persisted policy/release bindings and return its calendar."""

    if row is None:
        raise USPaperRuntimeError("paper runtime binding metadata is missing")
    try:
        schema_version = int(row["schema_version"])
        mode = str(row["mode"])
        release_id = str(row["release_id"])
        manifest_sha256 = str(row["manifest_sha256"])
        calendar_hash = str(row["calendar_hash"])
        calendar_json = str(row["calendar_json"])
        config_hash = str(row["config_hash"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise USPaperRuntimeError("paper runtime binding metadata is malformed") from exc
    if schema_version != RUNTIME_SCHEMA_VERSION:
        raise USPaperRuntimeError("paper runtime schema version mismatch")
    if mode != "PAPER":
        raise USPaperRuntimeError("paper runtime mode binding mismatch")
    if release_id != config.release_id or manifest_sha256 != config.manifest_sha256:
        raise USPaperRuntimeError(
            "paper runtime database belongs to a different PIT release or manifest"
        )
    if config_hash != _runtime_config_hash(config):
        raise USPaperRuntimeError(
            "paper runtime database belongs to a different execution policy"
        )
    if not _is_sha256(calendar_hash):
        raise USPaperRuntimeError("persisted frozen XNYS calendar hash is invalid")
    try:
        payload = json.loads(calendar_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise USPaperRuntimeError(
            "persisted frozen XNYS calendar JSON is invalid"
        ) from exc
    if not isinstance(payload, Mapping):
        raise USPaperRuntimeError("persisted frozen XNYS calendar must be a JSON object")
    if (
        payload.get("calendar") != "XNYS"
        or payload.get("frozen") is not True
        or payload.get("source_hash") != calendar_hash
        or not isinstance(payload.get("sessions"), list)
    ):
        raise USPaperRuntimeError("persisted frozen XNYS calendar metadata is invalid")
    try:
        persisted = FrozenXNYSSchedule(
            tuple(_date_value(item) for item in payload["sessions"]),
            source_hash=calendar_hash,
        )
    except (TypeError, ValueError) as exc:
        raise USPaperRuntimeError(
            "persisted frozen XNYS calendar content/hash mismatch"
        ) from exc
    if calendar_json != _json(persisted.as_dict()):
        raise USPaperRuntimeError("persisted frozen XNYS calendar JSON is not canonical")
    if schedule is not None and schedule.as_dict() != persisted.as_dict():
        raise USPaperRuntimeError(
            "runtime database belongs to a different frozen XNYS calendar"
        )
    return persisted


def _validate_runtime_release_ledger(
    connection: sqlite3.Connection,
    *,
    config: USPaperRuntimeConfig,
) -> list[sqlite3.Row]:
    """Validate the append-only, single-chain decision-release ledger."""

    table = connection.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='us_paper_runtime_release_admissions'"""
    ).fetchone()
    if table is None:
        raise USPaperRuntimeError(
            "runtime has no admitted PIT release ledger; start a new bound database"
        )
    rows = connection.execute(
        """SELECT * FROM us_paper_runtime_release_admissions
           ORDER BY admission_seq"""
    ).fetchall()
    if not rows:
        raise USPaperRuntimeError("runtime admitted PIT release ledger is empty")
    seen: set[tuple[str, str]] = set()
    previous: sqlite3.Row | None = None
    for expected_seq, row in enumerate(rows, start=1):
        try:
            sequence = int(row["admission_seq"])
            admission_key = str(row["admission_key"])
            admission_type = str(row["admission_type"])
            release_id = str(row["release_id"])
            manifest = str(row["manifest_sha256"])
            details = json.loads(str(row["details_json"]))
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise USPaperRuntimeError(
                "runtime admitted PIT release ledger is malformed"
            ) from exc
        if sequence != expected_seq or not all(
            _is_sha256(value) for value in (admission_key, release_id, manifest)
        ):
            raise USPaperRuntimeError(
                "runtime admitted PIT release ledger identity is invalid"
            )
        if not isinstance(details, Mapping):
            raise USPaperRuntimeError(
                "runtime admitted PIT release details are malformed"
            )
        binding = (release_id, manifest)
        if binding in seen:
            raise USPaperRuntimeError(
                "runtime admitted PIT release ledger contains a duplicate binding"
            )
        seen.add(binding)
        if previous is None:
            if (
                admission_type != "BASE"
                or row["old_release_id"] is not None
                or release_id != config.release_id
                or manifest != config.manifest_sha256
            ):
                raise USPaperRuntimeError(
                    "runtime admitted PIT release base conflicts with frozen binding"
                )
        else:
            prefix = str(row["membership_prefix_sha256"] or "")
            program_key = str(row["program_admission_key"] or "")
            if (
                admission_type != "ROLL_FORWARD"
                or not _is_sha256(prefix)
                or not _is_sha256(program_key)
                or admission_key != program_key
                or str(row["old_release_id"]) != str(previous["release_id"])
                or str(row["old_manifest_sha256"])
                != str(previous["manifest_sha256"])
            ):
                raise USPaperRuntimeError(
                    "runtime admitted PIT release chain is invalid"
                )
        if (
            str(details.get("release_id") or "") != release_id
            or str(details.get("manifest_sha256") or "") != manifest
            or str(details.get("admission_type") or "") != admission_type
        ):
            raise USPaperRuntimeError(
                "runtime admitted PIT release details conflict with ledger"
            )
        previous = row
    return list(rows)


def canonical_daily_source_sha256(
    *,
    source: str,
    source_code: str,
    adjustment: str,
    source_rows: Iterable[Mapping[str, Any]],
) -> str:
    """Hash the exact TDX rows behind one admitted DAILY observation."""

    return _hash(
        {
            "source_schema": DAILY_SOURCE_SCHEMA,
            "source": source,
            "source_code": source_code,
            "frequency": DAILY_SOURCE_FREQUENCY,
            "adjustment": adjustment,
            "source_rows": [dict(row) for row in source_rows],
        }
    )


def _normalize_tdx_daily_provenance(
    value: Mapping[str, Any],
    *,
    code: str,
    session_day: date,
    opening: float,
    high: float,
    low: float,
    closing: float,
) -> dict[str, Any]:
    source = str(value.get("source") or "").strip().upper()
    source_schema = str(value.get("source_schema") or "").strip()
    source_code = str(value.get("source_code") or "").strip().upper()
    frequency = str(value.get("frequency") or "").strip().lower()
    adjustment = str(value.get("adjustment") or "").strip().lower()
    expected_adjustment = "front" if code == "BILTR.US" else "none"
    expected_source_code = "BIL.US" if code == "BILTR.US" else code
    if (
        source_schema != DAILY_SOURCE_SCHEMA
        or source != "TDX"
        or source_code != expected_source_code
        or frequency != DAILY_SOURCE_FREQUENCY
        or adjustment != expected_adjustment
    ):
        raise ValueError(
            f"{code} DAILY requires canonical TDX {expected_source_code} "
            f"{expected_adjustment} provenance"
        )
    raw_rows = value.get("source_rows")
    if not isinstance(raw_rows, (list, tuple)):
        raise ValueError(f"{code} DAILY requires canonical source_rows")

    normalized_rows: list[dict[str, Any]] = []
    if code == "BILTR.US":
        if len(raw_rows) != 2:
            raise ValueError("BILTR.US DAILY requires two TDX front source rows")
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping) or set(raw_row) != {
                "session_date",
                "Close",
            }:
                raise ValueError(
                    "BILTR.US source rows require only session_date and Close"
                )
            normalized_rows.append(
                {
                    "session_date": _date_value(raw_row.get("session_date")).isoformat(),
                    "Close": _positive(raw_row.get("Close"), "source Close"),
                }
            )
        previous_day = _date_value(normalized_rows[0]["session_date"])
        current_day = _date_value(normalized_rows[1]["session_date"])
        if previous_day >= session_day or current_day != session_day:
            raise ValueError(
                "BILTR.US source rows must be a prior session and the current session"
            )
        if not math.isclose(
            float(normalized_rows[0]["Close"]), opening, rel_tol=0, abs_tol=1e-12
        ) or not math.isclose(
            float(normalized_rows[1]["Close"]), closing, rel_tol=0, abs_tol=1e-12
        ):
            raise ValueError("BILTR.US source closes do not reproduce open/close")
    else:
        if len(raw_rows) != 1:
            raise ValueError(f"{code} raw DAILY requires one TDX source row")
        raw_row = raw_rows[0]
        expected_keys = {"session_date", "Open", "High", "Low", "Close"}
        if not isinstance(raw_row, Mapping) or set(raw_row) != expected_keys:
            raise ValueError(
                f"{code} raw source row requires session_date/Open/High/Low/Close"
            )
        normalized_rows.append(
            {
                "session_date": _date_value(raw_row.get("session_date")).isoformat(),
                "Open": _positive(raw_row.get("Open"), "source Open"),
                "High": _positive(raw_row.get("High"), "source High"),
                "Low": _positive(raw_row.get("Low"), "source Low"),
                "Close": _positive(raw_row.get("Close"), "source Close"),
            }
        )
        expected_row = {
            "session_date": session_day.isoformat(),
            "Open": opening,
            "High": high,
            "Low": low,
            "Close": closing,
        }
        if normalized_rows[0] != expected_row:
            raise ValueError(f"{code} raw source row does not reproduce DAILY OHLC")

    expected_sha256 = canonical_daily_source_sha256(
        source=source,
        source_code=source_code,
        adjustment=adjustment,
        source_rows=normalized_rows,
    )
    supplied_sha256 = str(value.get("source_sha256") or "").strip()
    if not _is_sha256(supplied_sha256) or supplied_sha256 != expected_sha256:
        raise ValueError(f"{code} DAILY source_sha256 mismatch")
    return {
        "source_schema": source_schema,
        "source": source,
        "source_code": source_code,
        "frequency": frequency,
        "adjustment": adjustment,
        "source_rows": normalized_rows,
        "source_sha256": expected_sha256,
    }


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid datetime: {value!r}") from exc
    if result.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return result.astimezone(NY_TZ)


def _date_value(value: Any) -> date:
    if isinstance(value, datetime):
        return _aware(value).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid date: {value!r}") from exc


def _positive(value: Any, name: str) -> float:
    result = _optional_positive(value)
    if result is None:
        raise ValueError(f"{name} must be positive and finite")
    return result


def _optional_positive(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _hash(value: Any) -> str:
    payload = value if isinstance(value, str) else _json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


__all__ = [
    "DAILY_SOURCE_FREQUENCY",
    "DAILY_SOURCE_SCHEMA",
    "FrozenXNYSSchedule",
    "USPaperRuntime",
    "USPaperRuntimeConfig",
    "USPaperRuntimeError",
    "USPaperRuntimeLeaseError",
    "USPaperRuntimeScheduleError",
    "canonical_daily_source_sha256",
    "install_windows_task",
    "remove_windows_task",
    "windows_task_status",
    "windows_task_scheduler_spec",
]
