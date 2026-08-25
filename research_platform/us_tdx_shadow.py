"""Persistent, read-only TDX quote shadow qualification collector.

The collector records the fixed SPY plus 30-name cross-exchange sample used by
``evaluate_tdx_quote_qualification``.  It cannot submit or simulate orders.  A
database is permanently bound to one frozen XNYS calendar, one declared
20-session window, the fixed sample, and the collection policy hashes.

Each symbol/60-second slot is write-once.  Re-running a worker tick is
idempotent, while a second worker is rejected by a SQLite lease.  Final daily
opens must be supplied from the raw-bar pipeline after the close and are also
write-once.  Missing slots, openings, raw opens, timestamps, or market-state
integrity never get inferred; the existing qualification evaluator therefore
fails closed.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from .us_qualification import (
    REGULAR_SESSION_POLL_SLOTS,
    TDXDailySymbolEvidence,
    TDXQuoteQualificationDecision,
    TDX_QUALIFICATION_SAMPLE,
    TDX_REQUIRED_SESSIONS,
    evaluate_tdx_quote_qualification,
)
from .us_tdx import TQReadOnlyClient, USQuoteObservation


NY_TZ = ZoneInfo("America/New_York")
SCHEMA_VERSION = 3
INVALID_MARKET_STATES = frozenset(
    {
        "",
        "UNKNOWN",
        "CLOSED",
        "CLOSE",
        "HALTED",
        "SUSPENDED",
        "PREMARKET",
        "PRE_MARKET",
        "POSTMARKET",
        "POST_MARKET",
    }
)


class TDXShadowError(RuntimeError):
    """Base error for TDX shadow evidence collection."""


class TDXShadowBindingError(TDXShadowError):
    """The database or window does not match its frozen evidence binding."""


class TDXShadowScheduleError(TDXShadowError):
    """A collection attempt is outside the frozen qualification window."""


class TDXShadowLeaseError(TDXShadowError):
    """A different collector worker owns the active lease."""


class TDXShadowEvidenceError(TDXShadowError):
    """Raw/open evidence is incomplete, conflicting, or temporally invalid."""


class _QuoteClient(Protocol):
    def market_snapshot(
        self, code: str, *, fetched_at: datetime | None = None
    ) -> USQuoteObservation: ...


@dataclass(frozen=True)
class TDXShadowConfig:
    database_path: Path
    release_id: str
    manifest_sha256: str
    worker_id: str = "tdx-shadow-worker"
    poll_interval_seconds: int = 60
    maximum_source_latency_seconds: int = 90
    lease_seconds: int = 300
    market_open: time = time(9, 30)
    opening_capture_end: time = time(9, 35)
    market_close: time = time(16, 0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", Path(self.database_path))
        release_id = str(self.release_id).strip()
        manifest_sha256 = str(self.manifest_sha256).strip()
        if not _is_sha256(release_id):
            raise ValueError("release_id must be a lowercase 64-character SHA-256 digest")
        if not _is_sha256(manifest_sha256):
            raise ValueError(
                "manifest_sha256 must be a lowercase 64-character SHA-256 digest"
            )
        object.__setattr__(self, "release_id", release_id)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        worker_id = self.worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        object.__setattr__(self, "worker_id", worker_id)
        if self.poll_interval_seconds != 60:
            raise ValueError("TDX shadow polling is fixed at 60 seconds")
        if not 0 < self.maximum_source_latency_seconds <= 90:
            raise ValueError("maximum source latency must be in [1, 90] seconds")
        if self.lease_seconds <= self.poll_interval_seconds:
            raise ValueError("lease_seconds must exceed the polling interval")
        if not self.market_open < self.opening_capture_end < self.market_close:
            raise ValueError("invalid New York collection hours")
        if (
            self.market_open,
            self.opening_capture_end,
            self.market_close,
        ) != (time(9, 30), time(9, 35), time(16, 0)):
            raise ValueError("TDX qualification hours are fixed at 09:30/09:35/16:00 NY")
        regular_seconds = int(
            (
                datetime.combine(date.min, self.market_close)
                - datetime.combine(date.min, self.market_open)
            ).total_seconds()
        )
        if regular_seconds // self.poll_interval_seconds != REGULAR_SESSION_POLL_SLOTS:
            raise ValueError("collection hours must define exactly 390 poll slots")


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tdx_shadow_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version INTEGER NOT NULL,
    release_id TEXT NOT NULL CHECK(length(release_id) = 64),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64),
    calendar_hash TEXT NOT NULL,
    calendar_json TEXT NOT NULL,
    window_hash TEXT NOT NULL,
    window_json TEXT NOT NULL,
    sample_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tdx_shadow_sessions (
    session_date TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE,
    raw_reconciled INTEGER NOT NULL DEFAULT 0,
    raw_source_sha256 TEXT,
    raw_observed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tdx_shadow_slots (
    session_date TEXT NOT NULL REFERENCES tdx_shadow_sessions(session_date),
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    slot_index INTEGER NOT NULL,
    expected_at TEXT NOT NULL,
    fetched_at TEXT,
    source_at TEXT,
    captured INTEGER NOT NULL,
    fresh INTEGER NOT NULL,
    source_latency_seconds REAL,
    timezone_error INTEGER NOT NULL,
    future_timestamp_error INTEGER NOT NULL,
    market_state_error INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(session_date, symbol, slot_index),
    CHECK(slot_index >= 0 AND slot_index < 390)
);
CREATE INDEX IF NOT EXISTS idx_tdx_shadow_slots_session
ON tdx_shadow_slots(session_date, symbol);
CREATE TABLE IF NOT EXISTS tdx_shadow_openings (
    session_date TEXT NOT NULL REFERENCES tdx_shadow_sessions(session_date),
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_at TEXT NOT NULL,
    snapshot_open REAL NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY(session_date, symbol)
);
CREATE TABLE IF NOT EXISTS tdx_shadow_raw_opens (
    session_date TEXT NOT NULL REFERENCES tdx_shadow_sessions(session_date),
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    final_raw_open REAL NOT NULL,
    observed_at TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY(session_date, symbol)
);
CREATE TABLE IF NOT EXISTS tdx_shadow_lease (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    generation INTEGER NOT NULL
);
"""


class TDXShadowQualificationCollector:
    """Collect and freeze evidence for exactly one 20-session TDX test."""

    def __init__(
        self,
        config: TDXShadowConfig,
        *,
        frozen_xnys_sessions: Iterable[object],
        qualification_sessions: Iterable[object],
        quote_client: _QuoteClient | None = None,
    ) -> None:
        self.config = config
        self.calendar = _normalize_sessions(frozen_xnys_sessions, "frozen XNYS calendar")
        self.window = _normalize_sessions(qualification_sessions, "qualification window")
        if len(self.window) != TDX_REQUIRED_SESSIONS:
            raise TDXShadowBindingError(
                f"qualification window must contain exactly {TDX_REQUIRED_SESSIONS} sessions"
            )
        try:
            positions = [self.calendar.index(item) for item in self.window]
        except ValueError as exc:
            raise TDXShadowBindingError(
                "qualification window contains a session absent from the frozen calendar"
            ) from exc
        if positions != list(range(positions[0], positions[0] + len(self.window))):
            raise TDXShadowBindingError(
                "qualification window must be consecutive in the frozen XNYS calendar"
            )

        self.sample = tuple(TDX_QUALIFICATION_SAMPLE)
        sample_keys = [(item.symbol, item.exchange) for item in self.sample]
        if len(sample_keys) != 31 or len(sample_keys) != len(set(sample_keys)):
            raise TDXShadowBindingError("fixed TDX qualification sample is invalid")
        if {exchange for _, exchange in sample_keys} != {"NYSE", "NASDAQ"}:
            raise TDXShadowBindingError("fixed sample must span NYSE and NASDAQ")

        self.calendar_hash = _hash_json([item.isoformat() for item in self.calendar])
        self.window_hash = _hash_json([item.isoformat() for item in self.window])
        self.calendar_json = _json([item.isoformat() for item in self.calendar])
        self.window_json = _json([item.isoformat() for item in self.window])
        self.sample_hash = _hash_json(sample_keys)
        self.config_hash = _config_hash(config)
        self.quote_client = quote_client or TQReadOnlyClient()

        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(NY_TZ).isoformat()
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            metadata_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tdx_shadow_metadata)")
            }
            required_metadata_columns = {
                "release_id",
                "manifest_sha256",
                "calendar_hash",
                "calendar_json",
                "window_hash",
                "window_json",
                "sample_hash",
                "config_hash",
            }
            if not required_metadata_columns.issubset(metadata_columns):
                raise TDXShadowBindingError(
                    "legacy unbound TDX shadow database is unsafe; start a new bound database"
                )
            connection.execute(
                """INSERT OR IGNORE INTO tdx_shadow_metadata
                (singleton, schema_version, release_id, manifest_sha256,
                 calendar_hash, calendar_json, window_hash, window_json,
                 sample_hash, config_hash, created_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    SCHEMA_VERSION,
                    config.release_id,
                    config.manifest_sha256,
                    self.calendar_hash,
                    self.calendar_json,
                    self.window_hash,
                    self.window_json,
                    self.sample_hash,
                    self.config_hash,
                    now,
                ),
            )
            metadata = connection.execute(
                "SELECT * FROM tdx_shadow_metadata WHERE singleton=1"
            ).fetchone()
            expected = {
                "schema_version": SCHEMA_VERSION,
                "release_id": config.release_id,
                "manifest_sha256": config.manifest_sha256,
                "calendar_hash": self.calendar_hash,
                "calendar_json": self.calendar_json,
                "window_hash": self.window_hash,
                "window_json": self.window_json,
                "sample_hash": self.sample_hash,
                "config_hash": self.config_hash,
            }
            actual = {key: metadata[key] for key in expected}
            if actual != expected:
                raise TDXShadowBindingError(
                    "TDX shadow database belongs to a different calendar, window, sample, or policy"
                )
            for ordinal, session in enumerate(self.window):
                connection.execute(
                    """INSERT OR IGNORE INTO tdx_shadow_sessions
                    (session_date, ordinal, updated_at) VALUES (?, ?, ?)""",
                    (session.isoformat(), ordinal, now),
                )

    @classmethod
    def open_existing(
        cls,
        config: TDXShadowConfig,
        *,
        quote_client: _QuoteClient | None = None,
    ) -> "TDXShadowQualificationCollector":
        """Restore the originally frozen calendar/window from SQLite.

        A recurring worker must use this entry point after the first start;
        recalculating a window from the current date would silently move the
        qualification boundary.  Release, manifest, policy, and persisted
        calendar hashes are verified before normal construction resumes.
        """

        if not config.database_path.is_file():
            raise TDXShadowBindingError("TDX shadow database does not exist")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(config.database_path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tdx_shadow_metadata)")
            }
            required = {
                "schema_version",
                "release_id",
                "manifest_sha256",
                "calendar_hash",
                "calendar_json",
                "window_hash",
                "window_json",
                "sample_hash",
                "config_hash",
            }
            if not required.issubset(columns):
                raise TDXShadowBindingError(
                    "legacy or malformed TDX shadow database cannot be resumed"
                )
            row = connection.execute(
                "SELECT * FROM tdx_shadow_metadata WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise TDXShadowBindingError("TDX shadow binding metadata is missing")
            if int(row["schema_version"]) != SCHEMA_VERSION:
                raise TDXShadowBindingError("TDX shadow schema version mismatch")
            if (
                str(row["release_id"]) != config.release_id
                or str(row["manifest_sha256"]) != config.manifest_sha256
            ):
                raise TDXShadowBindingError(
                    "TDX shadow database belongs to a different PIT release or manifest"
                )
            if str(row["config_hash"]) != _config_hash(config):
                raise TDXShadowBindingError(
                    "TDX shadow database belongs to a different collection policy"
                )
            try:
                calendar_values = json.loads(str(row["calendar_json"]))
                window_values = json.loads(str(row["window_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TDXShadowBindingError(
                    "persisted frozen calendar/window JSON is invalid"
                ) from exc
            if not isinstance(calendar_values, list) or not isinstance(window_values, list):
                raise TDXShadowBindingError(
                    "persisted frozen calendar/window must be JSON arrays"
                )
            calendar = _normalize_sessions(calendar_values, "persisted XNYS calendar")
            window = _normalize_sessions(window_values, "persisted qualification window")
            if _hash_json([item.isoformat() for item in calendar]) != str(
                row["calendar_hash"]
            ):
                raise TDXShadowBindingError("persisted XNYS calendar hash mismatch")
            if _hash_json([item.isoformat() for item in window]) != str(row["window_hash"]):
                raise TDXShadowBindingError("persisted qualification window hash mismatch")
        except sqlite3.DatabaseError as exc:
            raise TDXShadowBindingError("TDX shadow database is unreadable") from exc
        finally:
            if connection is not None:
                connection.close()
        return cls(
            config,
            frozen_xnys_sessions=calendar,
            qualification_sessions=window,
            quote_client=quote_client,
        )

    def tick(self, *, now: datetime) -> dict[str, Any]:
        """Capture one write-once 60-second slot for all fixed sample names."""

        current = _aware_ny(now)
        session = current.date()
        if session not in self.window:
            raise TDXShadowScheduleError(
                f"{session.isoformat()} is outside the frozen 20-session window"
            )
        if current.time().replace(tzinfo=None) < self.config.market_open or current.time().replace(
            tzinfo=None
        ) >= self.config.market_close:
            return self.status(last_action="OUTSIDE_REGULAR_SESSION")

        elapsed = int(
            (
                current
                - datetime.combine(session, self.config.market_open, NY_TZ)
            ).total_seconds()
        )
        slot_index = elapsed // self.config.poll_interval_seconds
        if not 0 <= slot_index < REGULAR_SESSION_POLL_SLOTS:
            raise TDXShadowScheduleError("calculated poll slot is outside the regular session")

        self._acquire_lease(current)
        with self._connect() as connection:
            session_state = connection.execute(
                "SELECT raw_reconciled FROM tdx_shadow_sessions WHERE session_date=?",
                (session.isoformat(),),
            ).fetchone()
            if session_state is None:
                raise TDXShadowBindingError("qualification session row is missing")
            if bool(session_state["raw_reconciled"]):
                raise TDXShadowEvidenceError(
                    "a raw-reconciled session is sealed against additional quote slots"
                )
            existing = {
                str(row["symbol"])
                for row in connection.execute(
                    """SELECT symbol FROM tdx_shadow_slots
                    WHERE session_date=? AND slot_index=?""",
                    (session.isoformat(), slot_index),
                )
            }

        outcomes: list[dict[str, Any]] = []
        for instrument in self.sample:
            if instrument.symbol in existing:
                continue
            try:
                quote = self.quote_client.market_snapshot(
                    instrument.symbol, fetched_at=current
                )
                outcome = _assess_quote(
                    quote,
                    expected_symbol=instrument.symbol,
                    session=session,
                    now=current,
                    maximum_latency=self.config.maximum_source_latency_seconds,
                )
            except Exception as exc:
                outcome = {
                    "captured": 0,
                    "fresh": 0,
                    "fetched_at": None,
                    "source_at": None,
                    "source_latency_seconds": None,
                    "timezone_error": 0,
                    "future_timestamp_error": 0,
                    "market_state_error": 0,
                    "reason": f"QUOTE_ERROR:{type(exc).__name__}:{exc}",
                    "payload": {"error": f"{type(exc).__name__}: {exc}"},
                    "opening": None,
                }
            outcomes.append(
                {
                    **outcome,
                    "symbol": instrument.symbol,
                    "exchange": instrument.exchange,
                }
            )

        expected_at = datetime.combine(session, self.config.market_open, NY_TZ) + timedelta(
            seconds=slot_index * self.config.poll_interval_seconds
        )
        with self._transaction() as connection:
            for outcome in outcomes:
                payload_json = _json(outcome["payload"])
                payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                connection.execute(
                    """INSERT OR IGNORE INTO tdx_shadow_slots
                    (session_date, symbol, exchange, slot_index, expected_at,
                     fetched_at, source_at, captured, fresh, source_latency_seconds,
                     timezone_error, future_timestamp_error, market_state_error,
                     reason, payload_sha256, payload_json, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session.isoformat(),
                        outcome["symbol"],
                        outcome["exchange"],
                        slot_index,
                        expected_at.isoformat(),
                        outcome["fetched_at"],
                        outcome["source_at"],
                        int(outcome["captured"]),
                        int(outcome["fresh"]),
                        outcome["source_latency_seconds"],
                        int(outcome["timezone_error"]),
                        int(outcome["future_timestamp_error"]),
                        int(outcome["market_state_error"]),
                        outcome["reason"],
                        payload_sha256,
                        payload_json,
                        current.isoformat(),
                    ),
                )
                opening = outcome["opening"]
                if opening is not None and _within_opening_window(
                    _aware_ny(opening["observed_at"]), self.config
                ):
                    connection.execute(
                        """INSERT OR IGNORE INTO tdx_shadow_openings
                        (session_date, symbol, exchange, observed_at, source_at,
                         snapshot_open, payload_sha256)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            session.isoformat(),
                            outcome["symbol"],
                            outcome["exchange"],
                            opening["observed_at"],
                            opening["source_at"],
                            opening["snapshot_open"],
                            payload_sha256,
                        ),
                    )
            connection.execute(
                "UPDATE tdx_shadow_sessions SET updated_at=? WHERE session_date=?",
                (current.isoformat(), session.isoformat()),
            )
        return self.status(last_action=f"COLLECTED_SLOT_{slot_index}")

    def reconcile_raw_opens(
        self,
        session: date | str,
        raw_opens: Mapping[str, float] | Iterable[Mapping[str, Any]],
        *,
        observed_at: datetime,
        source_sha256: str,
    ) -> dict[str, Any]:
        """Freeze the final raw opens for one session after the market close.

        The call is atomic and requires the exact fixed sample.  Replaying the
        same source and values is idempotent; any changed value is a conflict.
        """

        session_day = _to_date(session)
        if session_day not in self.window:
            raise TDXShadowScheduleError(
                f"{session_day.isoformat()} is outside the frozen 20-session window"
            )
        observed = _aware_ny(observed_at)
        if observed < datetime.combine(session_day, self.config.market_close, NY_TZ):
            raise TDXShadowEvidenceError("final raw opens cannot be observed before close")
        source_hash = str(source_sha256).strip().lower()
        if not _is_sha256(source_hash):
            raise TDXShadowEvidenceError("source_sha256 must be a lowercase SHA256 digest")

        normalized = _normalize_raw_opens(raw_opens, session_day, self.sample)
        canonical = [
            {
                "session_date": session_day.isoformat(),
                "symbol": row["symbol"],
                "exchange": row["exchange"],
                "final_raw_open": row["final_raw_open"],
                "observed_at": observed.isoformat(),
                "source_sha256": source_hash,
            }
            for row in normalized
        ]

        already_reconciled = False
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT * FROM tdx_shadow_sessions WHERE session_date=?",
                (session_day.isoformat(),),
            ).fetchone()
            if state is None:
                raise TDXShadowBindingError("qualification session row is missing")
            if bool(state["raw_reconciled"]):
                existing = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT session_date, symbol, exchange, final_raw_open,
                        observed_at, source_sha256 FROM tdx_shadow_raw_opens
                        WHERE session_date=? ORDER BY symbol""",
                        (session_day.isoformat(),),
                    )
                ]
                if existing != sorted(canonical, key=lambda row: row["symbol"]):
                    raise TDXShadowEvidenceError(
                        "raw open evidence is immutable and conflicts with the frozen session"
                    )
                already_reconciled = True

            if not already_reconciled:
                for row in canonical:
                    payload_sha256 = _hash_json(row)
                    connection.execute(
                        """INSERT INTO tdx_shadow_raw_opens
                        (session_date, symbol, exchange, final_raw_open, observed_at,
                         source_sha256, payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["session_date"],
                            row["symbol"],
                            row["exchange"],
                            row["final_raw_open"],
                            row["observed_at"],
                            row["source_sha256"],
                            payload_sha256,
                        ),
                    )
                connection.execute(
                    """UPDATE tdx_shadow_sessions SET raw_reconciled=1,
                    raw_source_sha256=?, raw_observed_at=?, updated_at=?
                    WHERE session_date=?""",
                    (
                        source_hash,
                        observed.isoformat(),
                        observed.isoformat(),
                        session_day.isoformat(),
                    ),
                )
        action = "RAW_OPENS_ALREADY_RECONCILED" if already_reconciled else "RAW_OPENS_RECONCILED"
        return self.status(last_action=action)

    def build_evidence(self) -> tuple[TDXDailySymbolEvidence, ...]:
        """Return evaluator-ready evidence for atomically reconciled sessions."""

        rows: list[TDXDailySymbolEvidence] = []
        with self._connect() as connection:
            reconciled = {
                _to_date(row["session_date"])
                for row in connection.execute(
                    "SELECT session_date FROM tdx_shadow_sessions WHERE raw_reconciled=1"
                )
            }
            for session in self.window:
                if session not in reconciled:
                    continue
                for instrument in self.sample:
                    aggregates = connection.execute(
                        """SELECT COUNT(*) AS recorded,
                        COALESCE(SUM(captured), 0) AS captured,
                        COALESCE(SUM(fresh), 0) AS fresh,
                        MAX(CASE WHEN fresh=1 THEN source_latency_seconds END) AS max_latency,
                        COALESCE(SUM(timezone_error), 0) AS timezone_errors,
                        COALESCE(SUM(future_timestamp_error), 0) AS future_errors,
                        COALESCE(SUM(market_state_error), 0) AS market_errors
                        FROM tdx_shadow_slots WHERE session_date=? AND symbol=?""",
                        (session.isoformat(), instrument.symbol),
                    ).fetchone()
                    opening = connection.execute(
                        """SELECT * FROM tdx_shadow_openings
                        WHERE session_date=? AND symbol=?""",
                        (session.isoformat(), instrument.symbol),
                    ).fetchone()
                    raw_open = connection.execute(
                        """SELECT * FROM tdx_shadow_raw_opens
                        WHERE session_date=? AND symbol=?""",
                        (session.isoformat(), instrument.symbol),
                    ).fetchone()
                    if raw_open is None:
                        raise TDXShadowEvidenceError(
                            "reconciled session is missing a fixed-sample raw open"
                        )
                    placeholder = datetime.combine(session, self.config.market_open, NY_TZ)
                    rows.append(
                        TDXDailySymbolEvidence(
                            session=session,
                            symbol=instrument.symbol,
                            exchange=instrument.exchange,
                            expected_poll_slots=REGULAR_SESSION_POLL_SLOTS,
                            captured_poll_slots=int(aggregates["captured"]),
                            fresh_poll_slots=int(aggregates["fresh"]),
                            poll_interval_seconds=self.config.poll_interval_seconds,
                            maximum_source_latency_seconds=(
                                float(aggregates["max_latency"])
                                if aggregates["max_latency"] is not None
                                else float("inf")
                            ),
                            opening_observed_at=(
                                _aware_ny(opening["observed_at"])
                                if opening is not None
                                else placeholder
                            ),
                            opening_source_at=(
                                _aware_ny(opening["source_at"])
                                if opening is not None
                                else placeholder
                            ),
                            snapshot_open=(
                                float(opening["snapshot_open"])
                                if opening is not None
                                else float("nan")
                            ),
                            final_raw_open=float(raw_open["final_raw_open"]),
                            timezone_errors=int(aggregates["timezone_errors"]),
                            future_timestamp_errors=int(aggregates["future_errors"]),
                            market_state_errors=int(aggregates["market_errors"]),
                        )
                    )
        return tuple(rows)

    def evaluate(self) -> TDXQuoteQualificationDecision:
        return evaluate_tdx_quote_qualification(self.build_evidence(), self.calendar)

    def status(self, *, last_action: str = "STATUS") -> dict[str, Any]:
        evidence = self.build_evidence()
        decision = evaluate_tdx_quote_qualification(evidence, self.calendar)
        expected_slots_per_session = len(self.sample) * REGULAR_SESSION_POLL_SLOTS
        session_rows: list[dict[str, Any]] = []
        with self._connect() as connection:
            for session in self.window:
                row = connection.execute(
                    """SELECT raw_reconciled, raw_source_sha256, raw_observed_at
                    FROM tdx_shadow_sessions WHERE session_date=?""",
                    (session.isoformat(),),
                ).fetchone()
                counts = connection.execute(
                    """SELECT COUNT(*) AS recorded, COALESCE(SUM(captured), 0) AS captured,
                    COALESCE(SUM(fresh), 0) AS fresh FROM tdx_shadow_slots
                    WHERE session_date=?""",
                    (session.isoformat(),),
                ).fetchone()
                openings = connection.execute(
                    "SELECT COUNT(*) AS count FROM tdx_shadow_openings WHERE session_date=?",
                    (session.isoformat(),),
                ).fetchone()
                session_rows.append(
                    {
                        "session": session.isoformat(),
                        "recorded_slots": int(counts["recorded"]),
                        "captured_slots": int(counts["captured"]),
                        "fresh_slots": int(counts["fresh"]),
                        "expected_slots": expected_slots_per_session,
                        "opening_observations": int(openings["count"]),
                        "expected_opening_observations": len(self.sample),
                        "raw_reconciled": bool(row["raw_reconciled"]),
                        "raw_source_sha256": row["raw_source_sha256"],
                        "raw_observed_at": row["raw_observed_at"],
                    }
                )
        return {
            "mode": "READ_ONLY_SHADOW",
            "broker_writes_enabled": False,
            "last_action": last_action,
            "bindings": {
                "schema_version": SCHEMA_VERSION,
                "release_id": self.config.release_id,
                "manifest_sha256": self.config.manifest_sha256,
                "calendar": "XNYS",
                "calendar_hash": self.calendar_hash,
                "window_hash": self.window_hash,
                "sample_hash": self.sample_hash,
                "config_hash": self.config_hash,
                "qualification_sessions": [item.isoformat() for item in self.window],
            },
            "sessions": session_rows,
            "evidence_sha256": _hash_json(
                {
                    "release_id": self.config.release_id,
                    "manifest_sha256": self.config.manifest_sha256,
                    "calendar_hash": self.calendar_hash,
                    "window_hash": self.window_hash,
                    "sample_hash": self.sample_hash,
                    "evidence": [asdict(item) for item in evidence],
                }
            ),
            "decision": asdict(decision),
        }

    def _acquire_lease(self, now: datetime) -> None:
        expiry = now + timedelta(seconds=self.config.lease_seconds)
        with self._transaction() as connection:
            lease = connection.execute(
                "SELECT * FROM tdx_shadow_lease WHERE singleton=1"
            ).fetchone()
            if lease is None:
                connection.execute(
                    """INSERT INTO tdx_shadow_lease
                    (singleton, owner, acquired_at, expires_at, generation)
                    VALUES (1, ?, ?, ?, 1)""",
                    (self.config.worker_id, now.isoformat(), expiry.isoformat()),
                )
                return
            owner = str(lease["owner"])
            expires_at = _aware_ny(lease["expires_at"])
            if owner != self.config.worker_id and expires_at > now:
                raise TDXShadowLeaseError(
                    f"collector lease is owned by {owner} until {expires_at.isoformat()}"
                )
            connection.execute(
                """UPDATE tdx_shadow_lease SET owner=?, acquired_at=?, expires_at=?,
                generation=generation+1 WHERE singleton=1""",
                (self.config.worker_id, now.isoformat(), expiry.isoformat()),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.config.database_path, timeout=10.0)
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


def _normalize_sessions(values: Iterable[object], label: str) -> tuple[date, ...]:
    sessions = tuple(_to_date(value) for value in values)
    if not sessions:
        raise TDXShadowBindingError(f"{label} cannot be empty")
    if tuple(sorted(set(sessions))) != sessions:
        raise TDXShadowBindingError(f"{label} must be unique and increasing")
    return sessions


def _assess_quote(
    quote: USQuoteObservation,
    *,
    expected_symbol: str,
    session: date,
    now: datetime,
    maximum_latency: int,
) -> dict[str, Any]:
    payload = asdict(quote)
    fetched = _optional_aware_ny(quote.fetched_at)
    source = _optional_aware_ny(quote.source_at)
    timezone_error = int(
        fetched is None
        or (quote.source_at is not None and source is None)
        or (fetched is not None and fetched.date() != session)
        or (source is not None and source.date() != session)
    )
    future_error = int(
        fetched is not None
        and (
            fetched > now
            or (source is not None and (source > now or source > fetched))
        )
    )
    market_state = str(quote.market_status or "").strip().upper()
    identity_error = str(quote.code).strip().upper() != expected_symbol
    usable_price = any(
        _positive_or_none(value) is not None
        for value in (quote.last, quote.bid, quote.ask, quote.open)
    )
    market_error = int(
        identity_error or market_state in INVALID_MARKET_STATES or not usable_price
    )
    latency = (
        (now - source).total_seconds()
        if source is not None and source <= now
        else None
    )
    fetched_age = (
        (now - fetched).total_seconds()
        if fetched is not None and fetched <= now
        else None
    )
    fresh = int(
        source is not None
        and fetched is not None
        and not timezone_error
        and not future_error
        and not market_error
        and latency is not None
        and fetched_age is not None
        and 0 <= latency <= maximum_latency
        and 0 <= fetched_age <= maximum_latency
    )
    if timezone_error:
        reason = "TIMESTAMP_TIMEZONE_OR_SESSION_ERROR"
    elif future_error:
        reason = "FUTURE_OR_REVERSED_TIMESTAMP"
    elif market_error:
        reason = "MARKET_STATE_IDENTITY_OR_PRICE_ERROR"
    elif source is None:
        reason = "MISSING_SOURCE_TIMESTAMP"
    elif not fresh:
        reason = "STALE_QUOTE"
    else:
        reason = "FRESH"

    opening = None
    open_value = _positive_or_none(quote.open)
    if (
        fresh
        and open_value is not None
        and fetched is not None
        and source is not None
        and source.time().replace(tzinfo=None) >= time(9, 30)
    ):
        opening = {
            "observed_at": fetched.isoformat(),
            "source_at": source.isoformat(),
            "snapshot_open": open_value,
        }
    return {
        "captured": 1,
        "fresh": fresh,
        "fetched_at": fetched.isoformat() if fetched is not None else str(quote.fetched_at),
        "source_at": source.isoformat() if source is not None else (
            str(quote.source_at) if quote.source_at is not None else None
        ),
        "source_latency_seconds": latency,
        "timezone_error": timezone_error,
        "future_timestamp_error": future_error,
        "market_state_error": market_error,
        "reason": reason,
        "payload": payload,
        "opening": opening,
    }


def _within_opening_window(observed_at: datetime, config: TDXShadowConfig) -> bool:
    local_time = observed_at.astimezone(NY_TZ).time().replace(tzinfo=None)
    return config.market_open <= local_time <= config.opening_capture_end


def _normalize_raw_opens(
    values: Mapping[str, float] | Iterable[Mapping[str, Any]],
    session: date,
    sample: Sequence[Any],
) -> list[dict[str, Any]]:
    if isinstance(values, Mapping):
        source_rows = [
            {"symbol": symbol, "open": opening} for symbol, opening in values.items()
        ]
    else:
        source_rows = [dict(row) for row in values]
    exchange_by_symbol = {item.symbol: item.exchange for item in sample}
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in source_rows:
        symbol = str(row.get("symbol") or row.get("code") or "").strip().upper()
        if symbol in seen:
            raise TDXShadowEvidenceError(f"duplicate raw open symbol: {symbol}")
        seen.add(symbol)
        if symbol not in exchange_by_symbol:
            raise TDXShadowEvidenceError(f"raw open symbol is outside fixed sample: {symbol}")
        if row.get("session") is not None and _to_date(row["session"]) != session:
            raise TDXShadowEvidenceError(f"raw open has wrong session: {symbol}")
        if row.get("session_date") is not None and _to_date(row["session_date"]) != session:
            raise TDXShadowEvidenceError(f"raw open has wrong session: {symbol}")
        supplied_exchange = str(row.get("exchange") or exchange_by_symbol[symbol]).upper()
        if supplied_exchange != exchange_by_symbol[symbol]:
            raise TDXShadowEvidenceError(f"raw open has wrong exchange: {symbol}")
        opening = _positive_or_none(row.get("open"))
        if opening is None:
            raise TDXShadowEvidenceError(f"raw open must be positive and finite: {symbol}")
        normalized.append(
            {
                "symbol": symbol,
                "exchange": exchange_by_symbol[symbol],
                "final_raw_open": opening,
            }
        )
    expected = set(exchange_by_symbol)
    if seen != expected:
        missing = ",".join(sorted(expected - seen))
        raise TDXShadowEvidenceError(
            f"raw open reconciliation requires the exact fixed sample; missing={missing}"
        )
    return sorted(normalized, key=lambda row: row["symbol"])


def _optional_aware_ny(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(NY_TZ)


def _aware_ny(value: Any) -> datetime:
    result = _optional_aware_ny(value)
    if result is None:
        raise ValueError("timestamp must be timezone-aware")
    return result


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(NY_TZ).date()
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise TDXShadowBindingError(f"invalid session date: {value!r}") from exc


def _positive_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _json(value: Any) -> str:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _config_hash(config: TDXShadowConfig) -> str:
    # ``worker_id`` and the database path are intentionally excluded: a new
    # process may take over an expired lease without changing evidence policy.
    return _hash_json(
        {
            "poll_interval_seconds": config.poll_interval_seconds,
            "maximum_source_latency_seconds": config.maximum_source_latency_seconds,
            "lease_seconds": config.lease_seconds,
            "market_open": config.market_open.isoformat(),
            "opening_capture_end": config.opening_capture_end.isoformat(),
            "market_close": config.market_close.isoformat(),
        }
    )


__all__ = [
    "TDXShadowBindingError",
    "TDXShadowConfig",
    "TDXShadowError",
    "TDXShadowEvidenceError",
    "TDXShadowLeaseError",
    "TDXShadowQualificationCollector",
    "TDXShadowScheduleError",
]
