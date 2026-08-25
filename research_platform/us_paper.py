"""Paper-only execution for the US momentum strategy.

This module is deliberately isolated from the platform's broker and live-order
code.  It owns a small SQLite database and can only create simulated fills.
Market observations are admitted using their *ingestion* time, so an opening
price first seen after the opening capture window can never be backfilled.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

from .us_market_time import ny_session_date


NY_TZ = ZoneInfo("America/New_York")
ACCOUNT_ID = "us_momentum_paper"
SCHEMA_VERSION = 5
CORPORATE_ACTION_TYPES = frozenset(
    {
        "SPLIT",
        "STOCK_DIVIDEND",
        "CASH_DIVIDEND",
        "TICKER_CHANGE",
        "CASH_MERGER",
        "DELISTING",
        "BANKRUPTCY",
        "STOCK_MERGER",
        "SPINOFF",
    }
)
RISK_SELL_REASONS = frozenset(
    {
        "US_PIT_MEMBERSHIP_REMOVAL",
        "US_MARKET_REGIME_EXIT",
        "US_TREND_EXIT",
        "US_ELIGIBILITY_EXIT",
        "US_FIXED_STOP",
        "US_FIXED_STOP_GAP",
        "US_MISSED_STOP_RECOVERY_OPEN",
        "US_UNRESOLVED_STOP_GAP_RECOVERY",
        "US_MISSED_INTRADAY_STOP_RECOVERY",
    }
)


class USPaperError(RuntimeError):
    """Base error for the isolated paper executor."""


class USPaperConflictError(USPaperError):
    """An idempotency key was reused with different content."""


class USPaperCausalityError(USPaperError):
    """An input was not yet observable at the stated processing time."""


class USPaperState(StrEnum):
    RUNNING = "RUNNING"
    DATA_DEGRADED = "DATA_DEGRADED"
    KILLED = "KILLED"


class USPaperTickState(StrEnum):
    IDLE = "IDLE"
    WAITING_OPEN = "WAITING_OPEN"
    OPEN_CAPTURED = "OPEN_CAPTURED"
    MONITORING = "MONITORING"
    SESSION_CLOSED = "SESSION_CLOSED"
    DATA_DEGRADED = "DATA_DEGRADED"
    KILLED = "KILLED"


@dataclass(frozen=True)
class USPaperConfig:
    """Configuration for a local, simulated US-equity sleeve."""

    database_path: Path
    initial_cash: float = 100_000.0
    market_open: time = time(9, 30)
    market_close: time = time(16, 0)
    open_capture_seconds: int = 300
    max_positions: int = 10
    max_symbol_weight: float = 0.10
    stop_ratio: float = 0.08
    slippage_rate: float = 0.0005
    commission_rate: float = 0.0005
    min_commission: float = 0.0
    sec_sell_fee_rate: float = 20.60 / 1_000_000
    finra_taf_per_share: float = 0.000195
    finra_taf_cap: float = 9.79
    allow_test_fixture_identity: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", Path(self.database_path))
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.open_capture_seconds <= 0:
            raise ValueError("open_capture_seconds must be positive")
        if self.max_positions <= 0:
            raise ValueError("max_positions must be positive")
        if not 0 < self.max_symbol_weight <= 1:
            raise ValueError("max_symbol_weight must be in (0, 1]")
        if not 0 < self.stop_ratio < 1:
            raise ValueError("stop_ratio must be in (0, 1)")
        if not isinstance(self.allow_test_fixture_identity, bool):
            raise ValueError("allow_test_fixture_identity must be boolean")
        for name in (
            "slippage_rate",
            "commission_rate",
            "min_commission",
            "sec_sell_fee_rate",
            "finra_taf_per_share",
            "finra_taf_cap",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS us_paper_account (
    account_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK(mode='PAPER'),
    strategy_id TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    pit_release_id TEXT,
    manifest_sha256 TEXT,
    degraded_reason TEXT NOT NULL DEFAULT '',
    killed_at TEXT,
    kill_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_periods (
    period_id TEXT PRIMARY KEY,
    period_key TEXT NOT NULL UNIQUE,
    decision_at TEXT NOT NULL,
    execution_session TEXT NOT NULL,
    signal_hash TEXT NOT NULL,
    pit_release_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    auto_approved_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_orders (
    order_id TEXT PRIMARY KEY,
    period_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    signal_id TEXT NOT NULL DEFAULT '',
    security_id TEXT NOT NULL,
    code TEXT NOT NULL,
    pit_release_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    order_kind TEXT NOT NULL CHECK(order_kind IN ('REBALANCE','STOP')),
    target_weight REAL NOT NULL DEFAULT 0,
    stop_ratio REAL NOT NULL DEFAULT 0,
    eligible_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    block_reason TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    filled_at TEXT,
    FOREIGN KEY(period_id) REFERENCES us_paper_periods(period_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_us_paper_period_security_side
ON us_paper_orders(period_id, security_id, side) WHERE period_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS us_paper_observations (
    observation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    security_id TEXT,
    code TEXT NOT NULL,
    pit_release_id TEXT,
    manifest_sha256 TEXT,
    session_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('OPEN','DAILY','INTRADAY')),
    event_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    status TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    payload_json TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_us_paper_observation_lookup
ON us_paper_observations(session_date, kind, code, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_us_paper_observation_grain
ON us_paper_observations(code, session_date, kind)
WHERE kind IN ('OPEN','DAILY');
CREATE TABLE IF NOT EXISTS us_paper_positions (
    security_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    pit_release_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    average_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    last_price REAL NOT NULL,
    entry_at TEXT NOT NULL,
    recovery_exit_pending INTEGER NOT NULL DEFAULT 0
        CHECK(recovery_exit_pending IN (0, 1)),
    recovery_reason TEXT NOT NULL DEFAULT '',
    recovery_detected_session TEXT,
    recovery_detected_at TEXT,
    recovery_observation_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_fills (
    fill_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    code TEXT NOT NULL,
    pit_release_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL,
    reason TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES us_paper_orders(order_id)
);
CREATE TABLE IF NOT EXISTS us_paper_sessions (
    session_date TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    open_deadline TEXT NOT NULL,
    degraded_reason TEXT NOT NULL DEFAULT '',
    opened_at TEXT,
    closed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_events (
    event_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_paper_corporate_actions (
    action_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    security_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    pay_date TEXT,
    verified INTEGER NOT NULL CHECK(verified IN (0, 1)),
    verified_at TEXT,
    evidence_sha256 TEXT NOT NULL,
    pit_release_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('PENDING','APPLIED','APPLIED_NO_POSITION','BLOCKED')),
    block_reason TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS us_paper_receivables (
    receivable_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    code TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount>=0),
    pay_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','PAID')),
    created_at TEXT NOT NULL,
    paid_at TEXT,
    UNIQUE(action_id, security_id),
    FOREIGN KEY(action_id) REFERENCES us_paper_corporate_actions(action_id)
);
CREATE TABLE IF NOT EXISTS us_paper_cash_ledger (
    cash_entry_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    action_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    code TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN
        ('CASH_IN_LIEU','DIVIDEND_PAYMENT','TERMINATION_PROCEEDS')),
    amount REAL NOT NULL CHECK(amount>=0),
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    FOREIGN KEY(action_id) REFERENCES us_paper_corporate_actions(action_id)
);
CREATE TRIGGER IF NOT EXISTS trg_us_paper_action_no_update_identity
BEFORE UPDATE OF action_id, idempotency_key, content_hash, security_id,
                 action_type, effective_date, pay_date, verified, verified_at,
                 evidence_sha256, pit_release_id, manifest_sha256, terms_json
ON us_paper_corporate_actions
BEGIN
    SELECT RAISE(ABORT, 'paper corporate-action evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_us_paper_period_provenance_immutable
BEFORE UPDATE OF pit_release_id, manifest_sha256 ON us_paper_periods
WHEN NEW.pit_release_id<>OLD.pit_release_id
  OR NEW.manifest_sha256<>OLD.manifest_sha256
BEGIN
    SELECT RAISE(ABORT, 'paper period provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_us_paper_order_identity_immutable
BEFORE UPDATE OF security_id, pit_release_id, manifest_sha256 ON us_paper_orders
WHEN NEW.security_id<>OLD.security_id
  OR NEW.pit_release_id<>OLD.pit_release_id
  OR NEW.manifest_sha256<>OLD.manifest_sha256
BEGIN
    SELECT RAISE(ABORT, 'paper order identity/provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_us_paper_position_identity_immutable
BEFORE UPDATE OF security_id, pit_release_id, manifest_sha256 ON us_paper_positions
WHEN NEW.security_id<>OLD.security_id
  OR NEW.pit_release_id<>OLD.pit_release_id
  OR NEW.manifest_sha256<>OLD.manifest_sha256
BEGIN
    SELECT RAISE(ABORT, 'paper position identity/provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_us_paper_fill_identity_immutable
BEFORE UPDATE OF security_id, pit_release_id, manifest_sha256 ON us_paper_fills
WHEN NEW.security_id<>OLD.security_id
  OR NEW.pit_release_id<>OLD.pit_release_id
  OR NEW.manifest_sha256<>OLD.manifest_sha256
BEGIN
    SELECT RAISE(ABORT, 'paper fill identity/provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_us_paper_observation_identity_immutable
BEFORE UPDATE OF security_id, pit_release_id, manifest_sha256 ON us_paper_observations
WHEN NEW.security_id IS NOT OLD.security_id
  OR NEW.pit_release_id IS NOT OLD.pit_release_id
  OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256
BEGIN
    SELECT RAISE(ABORT, 'paper observation identity/provenance is immutable');
END;
"""


class _USPaperStore:
    def __init__(self, config: USPaperConfig) -> None:
        self.config = config
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            self._prepare_schema(connection)
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            now = datetime.now(NY_TZ).isoformat()
            connection.execute(
                """INSERT OR IGNORE INTO us_paper_account
                (account_id, mode, strategy_id, config_hash, status, initial_cash,
                 cash, updated_at)
                VALUES (?, 'PAPER', 'us_momentum_v1', ?, ?, ?, ?, ?)""",
                (
                    ACCOUNT_ID,
                    _config_hash(config),
                    USPaperState.RUNNING.value,
                    config.initial_cash,
                    config.initial_cash,
                    now,
                ),
            )
            account = connection.execute(
                "SELECT config_hash FROM us_paper_account WHERE account_id=?",
                (ACCOUNT_ID,),
            ).fetchone()
            if str(account["config_hash"]) != _config_hash(config):
                raise USPaperConflictError(
                    "the paper database was created with a different execution config"
                )

    @staticmethod
    def _prepare_schema(connection: sqlite3.Connection) -> None:
        """Upgrade only a genuinely empty legacy database.

        Version 1 used the mutable vendor ticker as the position key and did
        not retain release provenance.  There is no honest way to reconstruct
        either value after fills have occurred, so non-empty V1 databases are
        deliberately rejected instead of guessing identities.
        """

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "us_paper_positions" not in tables:
            return
        position_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(us_paper_positions)")
        }
        period_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(us_paper_periods)")
        }
        order_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(us_paper_orders)")
        }
        fill_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(us_paper_fills)")
        }
        observation_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(us_paper_observations)")
        }
        identity_ready = (
            {"security_id", "pit_release_id", "manifest_sha256"}
            <= position_columns
            and {"pit_release_id", "manifest_sha256"} <= period_columns
            and {"security_id", "pit_release_id", "manifest_sha256"}
            <= order_columns
            and {"security_id", "pit_release_id", "manifest_sha256"}
            <= fill_columns
            and {"security_id", "pit_release_id", "manifest_sha256"}
            <= observation_columns
        )
        if identity_ready:
            # V4 is an additive, evidence-preserving migration.  Recovery
            # state is attached to the stable position key, so existing V3
            # positions can be upgraded without guessing identity/provenance.
            recovery_columns = {
                "recovery_exit_pending": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(recovery_exit_pending IN (0, 1))"
                ),
                "recovery_reason": "TEXT NOT NULL DEFAULT ''",
                "recovery_detected_session": "TEXT",
                "recovery_detected_at": "TEXT",
                "recovery_observation_id": "TEXT",
            }
            for column, declaration in recovery_columns.items():
                if column not in position_columns:
                    connection.execute(
                        f"ALTER TABLE us_paper_positions "
                        f"ADD COLUMN {column} {declaration}"
                    )
            # V5 admits many causal intraday quotes per session while OPEN and
            # DAILY remain single-grain evidence.
            connection.execute("DROP INDEX IF EXISTS idx_us_paper_observation_grain")
            return

        operational = (
            "us_paper_periods",
            "us_paper_orders",
            "us_paper_observations",
            "us_paper_positions",
            "us_paper_fills",
            "us_paper_sessions",
            "us_paper_events",
        )
        populated = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in operational
            if table in tables
        }
        if any(populated.values()):
            raise USPaperConflictError(
                "legacy paper database contains activity without stable security "
                "identity/release provenance; start a new database (the old file "
                "is left untouched)"
            )

        # Empty V1 files contain no economic/audit state, so a clean schema
        # replacement is deterministic.  Child tables are dropped first.
        for table in (
            "us_paper_fills",
            "us_paper_observations",
            "us_paper_orders",
            "us_paper_positions",
            "us_paper_periods",
            "us_paper_sessions",
            "us_paper_events",
            "us_paper_account",
        ):
            if table in tables:
                connection.execute(f"DROP TABLE {table}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def rows(self, query: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, values).fetchall()]


class USPaperExecutor:
    """Causal state machine that creates simulated fills only."""

    def __init__(self, config: USPaperConfig, store: _USPaperStore | None = None) -> None:
        self.config = config
        self._store = store or _USPaperStore(config)

    def apply_corporate_actions(
        self,
        session_date: str | date,
        actions: Iterable[Mapping[str, Any]],
        *,
        now: datetime,
        pit_release_id: str,
        manifest_sha256: str,
    ) -> list[dict[str, Any]]:
        """Apply verified actions before any quote/order work for a session.

        The ledger and every economic mutation share one SQLite transaction.
        Replaying the same evidence is therefore a no-op; reusing an action ID
        with different evidence is a hard conflict.
        """

        current = _aware(now)
        session_day = _date_value(session_date)
        if current.date() != session_day:
            raise USPaperCausalityError(
                "corporate-action processing must occur in its New York session"
            )
        release_id = _sha256_id(pit_release_id, "pit_release_id")
        manifest_hash = _sha256_id(manifest_sha256, "manifest_sha256")
        normalized = [
            self._normalize_corporate_action(
                source,
                session_day=session_day,
                now=current,
                pit_release_id=release_id,
                manifest_sha256=manifest_hash,
            )
            for source in actions
        ]
        with self._store.transaction() as connection:
            self._ensure_session(
                connection,
                session_day.isoformat(),
                datetime.combine(session_day, self.config.market_open, NY_TZ)
                + timedelta(seconds=self.config.open_capture_seconds),
                current,
            )
            for item in normalized:
                existing = connection.execute(
                    """SELECT * FROM us_paper_corporate_actions
                    WHERE action_id=? OR idempotency_key=? LIMIT 1""",
                    (item["action_id"], item["idempotency_key"]),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["action_id"]) != item["action_id"]
                        or str(existing["content_hash"]) != item["content_hash"]
                    ):
                        raise USPaperConflictError(
                            "corporate-action ID was reused with different evidence"
                        )
                    continue
                connection.execute(
                    """INSERT INTO us_paper_corporate_actions
                    (action_id, idempotency_key, content_hash, security_id,
                     action_type, effective_date, pay_date, verified, verified_at,
                     evidence_sha256, pit_release_id, manifest_sha256, terms_json,
                     status, block_reason, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)""",
                    (
                        item["action_id"],
                        item["idempotency_key"],
                        item["content_hash"],
                        item["security_id"],
                        item["action_type"],
                        item["effective_date"],
                        item.get("pay_date"),
                        int(item["verified"]),
                        item.get("verified_at"),
                        item["evidence_sha256"],
                        item["pit_release_id"],
                        item["manifest_sha256"],
                        item["terms_json"],
                        item["validation_error"],
                        current.isoformat(),
                    ),
                )

            pending = connection.execute(
                """SELECT * FROM us_paper_corporate_actions
                WHERE status='PENDING' AND effective_date<=?
                ORDER BY effective_date, action_id""",
                (session_day.isoformat(),),
            ).fetchall()
            for action in pending:
                self._apply_corporate_action_row(
                    connection,
                    action,
                    session_day=session_day,
                    now=current,
                )
            self._pay_due_receivables(connection, session_day, current)
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM us_paper_corporate_actions
                    WHERE effective_date<=? ORDER BY effective_date, action_id""",
                    (session_day.isoformat(),),
                ).fetchall()
            ]

    @staticmethod
    def _normalize_corporate_action(
        source: Mapping[str, Any],
        *,
        session_day: date,
        now: datetime,
        pit_release_id: str,
        manifest_sha256: str,
    ) -> dict[str, Any]:
        raw = dict(source)
        raw_hash = _hash(raw)
        errors: list[str] = []
        action_type = str(raw.get("action_type") or raw.get("type") or "").strip().upper()
        if action_type == "RENAME":
            action_type = "TICKER_CHANGE"
        if action_type not in CORPORATE_ACTION_TYPES:
            errors.append("UNKNOWN_CORPORATE_ACTION_TYPE")
            action_type = action_type or "UNKNOWN"
        try:
            security_id = _security_id(raw.get("security_id"))
        except (TypeError, ValueError):
            security_id = str(raw.get("security_id") or "__INVALID_SECURITY_ID__")
            errors.append("MISSING_OR_INVALID_SECURITY_ID")
        effective_source = raw.get(
            "effective_date", raw.get("effective_at", raw.get("ex_date"))
        )
        try:
            effective_date = _corporate_action_date(effective_source)
        except (TypeError, ValueError):
            effective_date = session_day
            errors.append("MISSING_OR_INVALID_EFFECTIVE_DATE")
        pay_source = raw.get("pay_date")
        pay_date: date | None = None
        if not _missing_scalar(pay_source):
            try:
                pay_date = _corporate_action_date(pay_source)
            except (TypeError, ValueError):
                errors.append("INVALID_PAY_DATE")
        verified = _verified_flag(raw.get("verified")) or _verified_flag(
            raw.get("terms_verified")
        )
        if not verified:
            errors.append("CORPORATE_ACTION_NOT_VERIFIED")
        verified_at: datetime | None = None
        try:
            verified_at = _aware(raw.get("verified_at", raw.get("announced_at")))
            if verified_at > now:
                errors.append("FUTURE_CORPORATE_ACTION_VERIFICATION")
        except (TypeError, ValueError):
            errors.append("MISSING_OR_INVALID_VERIFIED_AT")
        evidence = str(raw.get("evidence_sha256") or "").strip().lower()
        if not _is_sha256(evidence):
            errors.append("MISSING_OR_INVALID_ACTION_EVIDENCE")
        supplied_release = str(raw.get("pit_release_id") or pit_release_id).strip()
        supplied_manifest = str(
            raw.get("manifest_sha256") or manifest_sha256
        ).strip()
        if supplied_release != pit_release_id or supplied_manifest != manifest_sha256:
            errors.append("CORPORATE_ACTION_RELEASE_BINDING_MISMATCH")

        terms_source = raw.get("terms")
        if terms_source is None:
            excluded = {
                "action_id",
                "idempotency_key",
                "action_type",
                "type",
                "security_id",
                "effective_date",
                "effective_at",
                "ex_date",
                "pay_date",
                "verified",
                "terms_verified",
                "verified_at",
                "announced_at",
                "evidence_sha256",
                "pit_release_id",
                "manifest_sha256",
            }
            terms = {key: value for key, value in raw.items() if key not in excluded}
        elif isinstance(terms_source, Mapping):
            terms = dict(terms_source)
        else:
            terms = {}
            errors.append("INVALID_CORPORATE_ACTION_TERMS")
        action_id = str(raw.get("action_id") or f"usca_{raw_hash[:24]}").strip()
        if not action_id:
            action_id = f"usca_{raw_hash[:24]}"
        canonical = {
            "action_id": action_id,
            "security_id": security_id,
            "action_type": action_type,
            "effective_date": effective_date.isoformat(),
            "pay_date": pay_date.isoformat() if pay_date else None,
            "verified": verified,
            "verified_at": verified_at.isoformat() if verified_at else None,
            "evidence_sha256": evidence,
            "pit_release_id": supplied_release,
            "manifest_sha256": supplied_manifest,
            "terms": terms,
            "validation_error": ":".join(dict.fromkeys(errors)),
        }
        content_hash = _hash(canonical)
        return {
            **canonical,
            "idempotency_key": str(raw.get("idempotency_key") or action_id),
            "content_hash": content_hash,
            "terms_json": _json(terms),
        }

    def _apply_corporate_action_row(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        *,
        session_day: date,
        now: datetime,
    ) -> None:
        action_id = str(action["action_id"])
        action_type = str(action["action_type"])
        session_key = session_day.isoformat()
        if str(action["effective_date"]) < session_key:
            self._block_corporate_action(
                connection, action_id, session_key, "LATE_CORPORATE_ACTION", now
            )
            return
        validation_error = str(action["block_reason"] or "")
        if validation_error:
            self._block_corporate_action(
                connection, action_id, session_key, validation_error, now
            )
            return
        if now > datetime.combine(session_day, self.config.market_open, NY_TZ):
            self._block_corporate_action(
                connection, action_id, session_key, "LATE_CORPORATE_ACTION", now
            )
            return
        terms = json.loads(str(action["terms_json"]))
        error = self._corporate_action_terms_error(action_type, action, terms)
        if error:
            self._block_corporate_action(
                connection, action_id, session_key, error, now
            )
            return

        position = connection.execute(
            "SELECT * FROM us_paper_positions WHERE security_id=?",
            (action["security_id"],),
        ).fetchone()
        if position is None:
            connection.execute(
                """UPDATE us_paper_corporate_actions
                SET status='APPLIED_NO_POSITION', applied_at=? WHERE action_id=?""",
                (now.isoformat(), action_id),
            )
            return

        try:
            if action_type in {"SPLIT", "STOCK_DIVIDEND"}:
                self._apply_share_ratio_action(connection, action, position, terms, now)
            elif action_type == "CASH_DIVIDEND":
                amount_per_share = _first_numeric_term(
                    terms,
                    ("amount_per_share", "cash_amount", "cash_per_share"),
                    allow_zero=True,
                )
                amount = int(position["quantity"]) * amount_per_share
                receivable_id = f"uspr_{_hash('dividend:' + action_id)[:24]}"
                connection.execute(
                    """INSERT OR IGNORE INTO us_paper_receivables
                    (receivable_id, action_id, security_id, code, amount, pay_date,
                     status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
                    (
                        receivable_id,
                        action_id,
                        position["security_id"],
                        position["code"],
                        amount,
                        action["pay_date"],
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """UPDATE us_paper_positions SET stop_price=?, updated_at=?
                    WHERE security_id=?""",
                    (
                        max(
                            0.000001,
                            float(position["stop_price"]) - amount_per_share,
                        ),
                        now.isoformat(),
                        position["security_id"],
                    ),
                )
            elif action_type == "TICKER_CHANGE":
                # Alias migration is release/decision-owned.  The action only
                # proves that retaining the stable security is non-economic.
                pass
            elif action_type in {"CASH_MERGER", "DELISTING", "BANKRUPTCY"}:
                cash_per_share = _first_numeric_term(
                    terms,
                    ("cash_per_share", "cash_amount", "settlement_cash_per_share"),
                    allow_zero=True,
                )
                amount = int(position["quantity"]) * cash_per_share
                self._credit_action_cash(
                    connection,
                    action,
                    position,
                    amount=amount,
                    entry_type="TERMINATION_PROCEEDS",
                    now=now,
                )
                connection.execute(
                    "DELETE FROM us_paper_positions WHERE security_id=?",
                    (position["security_id"],),
                )
                self._cancel_replaced_security_orders(
                    connection, str(position["security_id"]), action_id
                )
            elif action_type == "STOCK_MERGER":
                self._apply_stock_merger(connection, action, position, terms, now)
            elif action_type == "SPINOFF":
                self._apply_spinoff(connection, action, position, terms, now)
            else:  # guarded by normalization, retained as a defensive close.
                raise USPaperConflictError("UNKNOWN_CORPORATE_ACTION_TYPE")
        except (KeyError, TypeError, ValueError, USPaperConflictError) as exc:
            self._block_corporate_action(
                connection,
                action_id,
                session_key,
                f"UNSAFE_CORPORATE_ACTION_TERMS:{type(exc).__name__}:{exc}",
                now,
            )
            return
        connection.execute(
            """UPDATE us_paper_corporate_actions
            SET status='APPLIED', block_reason='', applied_at=? WHERE action_id=?""",
            (now.isoformat(), action_id),
        )

    @staticmethod
    def _corporate_action_terms_error(
        action_type: str,
        action: sqlite3.Row,
        terms: Mapping[str, Any],
    ) -> str:
        def positive(name: str) -> bool:
            try:
                value = float(terms.get(name))
            except (TypeError, ValueError):
                return False
            return math.isfinite(value) and value > 0

        def positive_any(names: tuple[str, ...]) -> bool:
            return any(positive(name) for name in names)

        def nonnegative_any(names: tuple[str, ...]) -> bool:
            for name in names:
                try:
                    value = float(terms.get(name))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value >= 0:
                    return True
            return False

        if action_type in {"SPLIT", "STOCK_DIVIDEND"} and not positive_any(
            ("ratio", "split_ratio", "share_ratio")
        ):
            return "MISSING_OR_INVALID_SHARE_RATIO"
        if action_type in {"SPLIT", "STOCK_DIVIDEND"}:
            successor = str(terms.get("successor_security_id") or "").strip()
            if successor:
                try:
                    _security_id(successor)
                    _us_code(
                        terms.get("successor_code", terms.get("new_code"))
                    )
                except (TypeError, ValueError):
                    return "MISSING_OR_INVALID_SHARE_RATIO_SUCCESSOR"
        if action_type == "CASH_DIVIDEND":
            if not nonnegative_any(
                ("amount_per_share", "cash_amount", "cash_per_share")
            ):
                return "MISSING_OR_INVALID_DIVIDEND_AMOUNT"
            if action["pay_date"] is None:
                return "MISSING_DIVIDEND_PAY_DATE"
            if str(action["pay_date"]) < str(action["effective_date"]):
                return "DIVIDEND_PAY_DATE_PRECEDES_EX_DATE"
        if action_type in {"CASH_MERGER", "DELISTING", "BANKRUPTCY"}:
            if not nonnegative_any(
                ("cash_per_share", "cash_amount", "settlement_cash_per_share")
            ):
                return "MISSING_TERMINATION_CASH_TERMS"
        if action_type == "STOCK_MERGER":
            if not positive_any(("ratio", "share_ratio", "exchange_ratio")):
                return "MISSING_OR_INVALID_STOCK_MERGER_RATIO"
            try:
                _security_id(
                    terms.get("target_security_id", terms.get("successor_security_id"))
                )
                _us_code(
                    terms.get(
                        "target_code",
                        terms.get("successor_code", terms.get("new_code")),
                    )
                )
            except (TypeError, ValueError):
                return "MISSING_OR_INVALID_STOCK_MERGER_TARGET"
        if action_type == "SPINOFF":
            if not positive_any(("ratio", "share_ratio")):
                return "MISSING_OR_INVALID_SPINOFF_RATIO"
            try:
                _security_id(
                    terms.get("child_security_id", terms.get("successor_security_id"))
                )
                _us_code(
                    terms.get(
                        "child_code",
                        terms.get("successor_code", terms.get("new_code")),
                    )
                )
                allocation = float(terms.get("cost_basis_fraction"))
                if not math.isfinite(allocation) or not 0 <= allocation < 1:
                    raise ValueError
            except (TypeError, ValueError):
                return "MISSING_OR_INVALID_SPINOFF_TERMS"
        return ""

    def _apply_share_ratio_action(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        position: sqlite3.Row,
        terms: Mapping[str, Any],
        now: datetime,
    ) -> None:
        ratio = _first_numeric_term(
            terms, ("ratio", "split_ratio", "share_ratio")
        )
        exact_quantity = int(position["quantity"]) * ratio
        quantity = int(math.floor(exact_quantity + 1e-12))
        fraction = max(0.0, exact_quantity - quantity)
        cash_price = _positive_or_none(terms.get("cash_in_lieu_price"))
        if fraction > 1e-9 and cash_price is None:
            raise USPaperConflictError(
                "fractional split entitlement lacks verified cash-in-lieu price"
            )
        if fraction > 1e-9:
            self._credit_action_cash(
                connection,
                action,
                position,
                amount=fraction * float(cash_price),
                entry_type="CASH_IN_LIEU",
                now=now,
            )
        if quantity <= 0:
            connection.execute(
                "DELETE FROM us_paper_positions WHERE security_id=?",
                (position["security_id"],),
            )
        else:
            successor_value = str(
                terms.get("successor_security_id") or ""
            ).strip()
            if not successor_value or successor_value == str(position["security_id"]):
                connection.execute(
                    """UPDATE us_paper_positions SET quantity=?, average_price=?,
                    stop_price=?, last_price=?, updated_at=? WHERE security_id=?""",
                    (
                        quantity,
                        float(position["average_price"]) / ratio,
                        float(position["stop_price"]) / ratio,
                        float(position["last_price"]) / ratio,
                        now.isoformat(),
                        position["security_id"],
                    ),
                )
            else:
                successor = _security_id(successor_value)
                successor_code = _us_code(
                    terms.get("successor_code", terms.get("new_code"))
                )
                if connection.execute(
                    "SELECT 1 FROM us_paper_positions WHERE security_id=? OR code=?",
                    (successor, successor_code),
                ).fetchone() is not None:
                    raise USPaperConflictError(
                        "share-ratio successor already exists in the paper portfolio"
                    )
                old_id = str(position["security_id"])
                self._cancel_replaced_security_orders(
                    connection, old_id, str(action["action_id"])
                )
                connection.execute(
                    "DELETE FROM us_paper_positions WHERE security_id=?", (old_id,)
                )
                connection.execute(
                    """INSERT INTO us_paper_positions
                    (security_id, code, pit_release_id, manifest_sha256, quantity,
                     average_price, stop_price, last_price, entry_at,
                     recovery_exit_pending, recovery_reason,
                     recovery_detected_session, recovery_detected_at,
                     recovery_observation_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        successor,
                        successor_code,
                        action["pit_release_id"],
                        action["manifest_sha256"],
                        quantity,
                        float(position["average_price"]) / ratio,
                        float(position["stop_price"]) / ratio,
                        float(position["last_price"]) / ratio,
                        position["entry_at"],
                        position["recovery_exit_pending"],
                        position["recovery_reason"],
                        position["recovery_detected_session"],
                        position["recovery_detected_at"],
                        position["recovery_observation_id"],
                        now.isoformat(),
                    ),
                )

    def _apply_stock_merger(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        position: sqlite3.Row,
        terms: Mapping[str, Any],
        now: datetime,
    ) -> None:
        target_id = _security_id(
            terms.get("target_security_id", terms.get("successor_security_id"))
        )
        target_code = _us_code(
            terms.get(
                "target_code", terms.get("successor_code", terms.get("new_code"))
            )
        )
        if connection.execute(
            "SELECT 1 FROM us_paper_positions WHERE security_id=? OR code=?",
            (target_id, target_code),
        ).fetchone() is not None:
            raise USPaperConflictError(
                "stock-merger target already exists in the paper portfolio"
            )
        ratio = _first_numeric_term(
            terms, ("ratio", "share_ratio", "exchange_ratio")
        )
        exact_quantity = int(position["quantity"]) * ratio
        quantity = int(math.floor(exact_quantity + 1e-12))
        fraction = max(0.0, exact_quantity - quantity)
        cash_price = _positive_or_none(terms.get("cash_in_lieu_price"))
        if fraction > 1e-9 and cash_price is None:
            raise USPaperConflictError(
                "fractional stock-merger entitlement lacks cash-in-lieu price"
            )
        if fraction > 1e-9:
            self._credit_action_cash(
                connection,
                action,
                position,
                amount=fraction * float(cash_price),
                entry_type="CASH_IN_LIEU",
                now=now,
            )
        old_id = str(position["security_id"])
        self._cancel_replaced_security_orders(connection, old_id, str(action["action_id"]))
        connection.execute(
            "DELETE FROM us_paper_positions WHERE security_id=?", (old_id,)
        )
        if quantity > 0:
            connection.execute(
                """INSERT INTO us_paper_positions
                (security_id, code, pit_release_id, manifest_sha256, quantity,
                 average_price, stop_price, last_price, entry_at,
                 recovery_exit_pending, recovery_reason,
                 recovery_detected_session, recovery_detected_at,
                 recovery_observation_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    target_id,
                    target_code,
                    action["pit_release_id"],
                    action["manifest_sha256"],
                    quantity,
                    float(position["average_price"]) / ratio,
                    float(position["stop_price"]) / ratio,
                    float(position["last_price"]) / ratio,
                    position["entry_at"],
                    position["recovery_exit_pending"],
                    position["recovery_reason"],
                    position["recovery_detected_session"],
                    position["recovery_detected_at"],
                    position["recovery_observation_id"],
                    now.isoformat(),
                ),
            )

    def _apply_spinoff(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        position: sqlite3.Row,
        terms: Mapping[str, Any],
        now: datetime,
    ) -> None:
        child_id = _security_id(
            terms.get("child_security_id", terms.get("successor_security_id"))
        )
        child_code = _us_code(
            terms.get(
                "child_code", terms.get("successor_code", terms.get("new_code"))
            )
        )
        if connection.execute(
            "SELECT 1 FROM us_paper_positions WHERE security_id=? OR code=?",
            (child_id, child_code),
        ).fetchone() is not None:
            raise USPaperConflictError(
                "spinoff child already exists in the paper portfolio"
            )
        ratio = _first_numeric_term(terms, ("ratio", "share_ratio"))
        allocation = float(terms["cost_basis_fraction"])
        exact_quantity = int(position["quantity"]) * ratio
        quantity = int(math.floor(exact_quantity + 1e-12))
        fraction = max(0.0, exact_quantity - quantity)
        cash_price = _positive_or_none(terms.get("cash_in_lieu_price"))
        if fraction > 1e-9 and cash_price is None:
            raise USPaperConflictError(
                "fractional spinoff entitlement lacks cash-in-lieu price"
            )
        if fraction > 1e-9:
            self._credit_action_cash(
                connection,
                action,
                position,
                amount=fraction * float(cash_price),
                entry_type="CASH_IN_LIEU",
                now=now,
            )
        retained = 1.0 - allocation
        connection.execute(
            """UPDATE us_paper_positions SET average_price=?, stop_price=?,
            last_price=?, updated_at=? WHERE security_id=?""",
            (
                float(position["average_price"]) * retained,
                max(0.000001, float(position["stop_price"]) * retained),
                max(0.000001, float(position["last_price"]) * retained),
                now.isoformat(),
                position["security_id"],
            ),
        )
        if quantity <= 0:
            return
        connection.execute(
            """INSERT INTO us_paper_positions
            (security_id, code, pit_release_id, manifest_sha256, quantity,
             average_price, stop_price, last_price, entry_at,
             recovery_exit_pending, recovery_reason,
             recovery_detected_session, recovery_detected_at,
             recovery_observation_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', '', NULL, '', ?)""",
            (
                child_id,
                child_code,
                action["pit_release_id"],
                action["manifest_sha256"],
                quantity,
                float(position["average_price"]) * allocation / ratio,
                max(0.000001, float(position["stop_price"]) * allocation / ratio),
                max(0.000001, float(position["last_price"]) * allocation / ratio),
                position["entry_at"],
                now.isoformat(),
            ),
        )

    @staticmethod
    def _cancel_replaced_security_orders(
        connection: sqlite3.Connection, security_id: str, action_id: str
    ) -> None:
        connection.execute(
            """UPDATE us_paper_orders SET status='BLOCKED',
            block_reason=? WHERE security_id=? AND status='WAITING_OPEN'""",
            (f"CORPORATE_ACTION_REPLACED_SECURITY:{action_id}", security_id),
        )

    def _credit_action_cash(
        self,
        connection: sqlite3.Connection,
        action: sqlite3.Row,
        position: sqlite3.Row,
        *,
        amount: float,
        entry_type: str,
        now: datetime,
    ) -> None:
        if not math.isfinite(amount) or amount < 0:
            raise USPaperConflictError("corporate-action cash amount is invalid")
        idempotency = f"action-cash:{action['action_id']}:{entry_type}"
        if connection.execute(
            "SELECT 1 FROM us_paper_cash_ledger WHERE idempotency_key=?",
            (idempotency,),
        ).fetchone() is not None:
            return
        entry_id = f"uspcl_{_hash(idempotency)[:24]}"
        connection.execute(
            """INSERT INTO us_paper_cash_ledger
            (cash_entry_id, idempotency_key, action_id, security_id, code,
             entry_type, amount, occurred_at, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id,
                idempotency,
                action["action_id"],
                position["security_id"],
                position["code"],
                entry_type,
                amount,
                now.isoformat(),
                _json({"action_type": action["action_type"]}),
            ),
        )
        connection.execute(
            """UPDATE us_paper_account SET cash=cash+?, updated_at=?
            WHERE account_id=?""",
            (amount, now.isoformat(), ACCOUNT_ID),
        )

    def _pay_due_receivables(
        self,
        connection: sqlite3.Connection,
        session_day: date,
        now: datetime,
    ) -> None:
        rows = connection.execute(
            """SELECT r.*, a.action_type FROM us_paper_receivables r
            JOIN us_paper_corporate_actions a ON a.action_id=r.action_id
            WHERE r.status='PENDING' AND r.pay_date<=?
            ORDER BY r.pay_date, r.receivable_id""",
            (session_day.isoformat(),),
        ).fetchall()
        for row in rows:
            idempotency = f"dividend-payment:{row['receivable_id']}"
            if connection.execute(
                "SELECT 1 FROM us_paper_cash_ledger WHERE idempotency_key=?",
                (idempotency,),
            ).fetchone() is None:
                entry_id = f"uspcl_{_hash(idempotency)[:24]}"
                connection.execute(
                    """INSERT INTO us_paper_cash_ledger
                    (cash_entry_id, idempotency_key, action_id, security_id,
                     code, entry_type, amount, occurred_at, details_json)
                    VALUES (?, ?, ?, ?, ?, 'DIVIDEND_PAYMENT', ?, ?, ?)""",
                    (
                        entry_id,
                        idempotency,
                        row["action_id"],
                        row["security_id"],
                        row["code"],
                        row["amount"],
                        now.isoformat(),
                        _json({"receivable_id": row["receivable_id"]}),
                    ),
                )
                connection.execute(
                    """UPDATE us_paper_account SET cash=cash+?, updated_at=?
                    WHERE account_id=?""",
                    (row["amount"], now.isoformat(), ACCOUNT_ID),
                )
            connection.execute(
                """UPDATE us_paper_receivables SET status='PAID', paid_at=?
                WHERE receivable_id=?""",
                (now.isoformat(), row["receivable_id"]),
            )

    def _block_corporate_action(
        self,
        connection: sqlite3.Connection,
        action_id: str,
        session_key: str,
        reason: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """UPDATE us_paper_corporate_actions SET status='BLOCKED',
            block_reason=?, applied_at=? WHERE action_id=?""",
            (reason, now.isoformat(), action_id),
        )
        self._degrade(
            connection,
            session_key,
            f"CORPORATE_ACTION_BLOCKED:{action_id}:{reason}",
            now,
        )

    def record_observation(
        self,
        observation: Mapping[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        current = _aware(now)
        item = self._normalize_observation(observation, current)
        with self._store.transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM us_paper_observations
                WHERE idempotency_key=? OR observation_id=?
                   OR (? IN ('OPEN','DAILY')
                       AND code=? AND session_date=? AND kind=?)
                LIMIT 1""",
                (
                    item["idempotency_key"],
                    item["observation_id"],
                    item["kind"],
                    item["code"],
                    item["session_date"],
                    item["kind"],
                ),
            ).fetchone()
            if existing is None:
                identity = self._observation_identity(connection, item)
            else:
                # Identity is fixed at first ingestion.  Replays remain
                # idempotent even after the corresponding order later expires
                # or the held security changes ticker.
                identity = {
                    "security_id": existing["security_id"],
                    "pit_release_id": existing["pit_release_id"],
                    "manifest_sha256": existing["manifest_sha256"],
                }
            item.update(identity)
            canonical = json.loads(item["payload_json"])
            canonical.update(identity)
            item["content_hash"] = _hash(canonical)
            if not item["caller_idempotency"]:
                item["idempotency_key"] = item["content_hash"]
            if not item["caller_observation_id"]:
                item["observation_id"] = f"uspo_{item['content_hash'][:24]}"
            item["payload_json"] = _json(canonical)
            if existing is not None:
                if str(existing["content_hash"]) != item["content_hash"]:
                    raise USPaperConflictError(
                        "observation idempotency key was reused with different content"
                    )
                return dict(existing)
            connection.execute(
                """INSERT INTO us_paper_observations
                (observation_id, idempotency_key, content_hash, security_id, code,
                 pit_release_id, manifest_sha256, session_date, kind, event_at,
                 available_at, ingested_at, status, open, high, low, close,
                 payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item["observation_id"],
                    item["idempotency_key"],
                    item["content_hash"],
                    item.get("security_id"),
                    item["code"],
                    item.get("pit_release_id"),
                    item.get("manifest_sha256"),
                    item["session_date"],
                    item["kind"],
                    item["event_at"],
                    item["available_at"],
                    current.isoformat(),
                    item["status"],
                    item.get("open"),
                    item.get("high"),
                    item.get("low"),
                    item.get("close"),
                    item["payload_json"],
                ),
            )
            if item["status"] == "LATE_IGNORED":
                self._degrade(
                    connection,
                    item["session_date"],
                    "LATE_OPEN_NOT_BACKFILLED",
                    current,
                )
            return dict(
                connection.execute(
                    "SELECT * FROM us_paper_observations WHERE observation_id=?",
                    (item["observation_id"],),
                ).fetchone()
            )

    def execute_intraday_stop(
        self,
        observation: Mapping[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        """Atomically admit one fresh quote and execute an eligible stop.

        ``close`` is the causal sell reference (normally current bid, falling
        back to last only when the feed has no valid bid).  A later daily Low
        never enters this path.
        """

        current = _aware(now)
        item = self._normalize_observation(observation, current)
        if item["kind"] != "INTRADAY":
            raise ValueError("execute_intraday_stop requires kind=INTRADAY")
        with self._store.transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM us_paper_observations
                WHERE idempotency_key=? OR observation_id=? LIMIT 1""",
                (item["idempotency_key"], item["observation_id"]),
            ).fetchone()
            identity = (
                self._observation_identity(connection, item)
                if existing is None
                else {
                    "security_id": existing["security_id"],
                    "pit_release_id": existing["pit_release_id"],
                    "manifest_sha256": existing["manifest_sha256"],
                }
            )
            item.update(identity)
            canonical = json.loads(item["payload_json"])
            canonical.update(identity)
            item["content_hash"] = _hash(canonical)
            if not item["caller_idempotency"]:
                item["idempotency_key"] = item["content_hash"]
            if not item["caller_observation_id"]:
                item["observation_id"] = f"uspo_{item['content_hash'][:24]}"
            item["payload_json"] = _json(canonical)
            if existing is not None:
                if str(existing["content_hash"]) != item["content_hash"]:
                    raise USPaperConflictError(
                        "intraday quote idempotency key was reused with different content"
                    )
                stored = existing
            else:
                if item.get("security_id") is None:
                    raise USPaperConflictError(
                        "intraday stop quote is not bound to a held stable security"
                    )
                connection.execute(
                    """INSERT INTO us_paper_observations
                    (observation_id, idempotency_key, content_hash, security_id,
                     code, pit_release_id, manifest_sha256, session_date, kind,
                     event_at, available_at, ingested_at, status, open, high,
                     low, close, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'INTRADAY', ?, ?, ?,
                            'ACCEPTED', ?, ?, ?, ?, ?)""",
                    (
                        item["observation_id"],
                        item["idempotency_key"],
                        item["content_hash"],
                        item["security_id"],
                        item["code"],
                        item["pit_release_id"],
                        item["manifest_sha256"],
                        item["session_date"],
                        item["event_at"],
                        item["available_at"],
                        current.isoformat(),
                        item.get("open"),
                        item.get("high"),
                        item.get("low"),
                        item["close"],
                        item["payload_json"],
                    ),
                )
                stored = connection.execute(
                    "SELECT * FROM us_paper_observations WHERE observation_id=?",
                    (item["observation_id"],),
                ).fetchone()
            if stored["processed_at"]:
                fill = connection.execute(
                    """SELECT * FROM us_paper_fills
                    WHERE security_id=? AND reason='US_FIXED_STOP_INTRADAY_QUOTE'
                    AND substr(filled_at, 1, 10)=?
                    ORDER BY filled_at DESC LIMIT 1""",
                    (stored["security_id"], item["session_date"]),
                ).fetchone()
                return {
                    "observation": dict(stored),
                    "stop_triggered": fill is not None,
                    "fill": dict(fill) if fill is not None else None,
                }

            position = connection.execute(
                "SELECT * FROM us_paper_positions WHERE security_id=?",
                (stored["security_id"],),
            ).fetchone()
            fill: sqlite3.Row | None = None
            reference = _positive(stored["close"], "intraday sell reference")
            if position is not None and reference <= float(position["stop_price"]):
                self._fill_stop(
                    connection,
                    position,
                    reference,
                    "US_FIXED_STOP_INTRADAY_QUOTE",
                    str(item["session_date"]),
                    current,
                )
                fill = connection.execute(
                    """SELECT * FROM us_paper_fills
                    WHERE security_id=? AND reason='US_FIXED_STOP_INTRADAY_QUOTE'
                    AND substr(filled_at, 1, 10)=?
                    ORDER BY filled_at DESC LIMIT 1""",
                    (stored["security_id"], item["session_date"]),
                ).fetchone()
                self._event(
                    connection,
                    "INTRADAY_STOP_EXECUTED",
                    "HIGH",
                    _json(
                        {
                            "security_id": stored["security_id"],
                            "code": stored["code"],
                            "observation_id": stored["observation_id"],
                            "reference_price": reference,
                            "fill_id": fill["fill_id"] if fill is not None else None,
                            "reason": "US_FIXED_STOP_INTRADAY_QUOTE",
                        }
                    ),
                    current,
                )
            connection.execute(
                """UPDATE us_paper_observations SET processed_at=?
                WHERE observation_id=?""",
                (current.isoformat(), stored["observation_id"]),
            )
            stored = connection.execute(
                "SELECT * FROM us_paper_observations WHERE observation_id=?",
                (stored["observation_id"],),
            ).fetchone()
            return {
                "observation": dict(stored),
                "stop_triggered": fill is not None,
                "fill": dict(fill) if fill is not None else None,
            }

    @staticmethod
    def _observation_identity(
        connection: sqlite3.Connection,
        item: Mapping[str, Any],
    ) -> dict[str, str | None]:
        """Bind a quote to the stable identity visible at ingestion time."""

        code = str(item["code"])
        session_key = str(item["session_date"])
        order_rows = connection.execute(
            """SELECT security_id, pit_release_id, manifest_sha256
            FROM us_paper_orders WHERE code=? AND status='WAITING_OPEN'
            AND substr(eligible_at, 1, 10)=?""",
            (code, session_key),
        ).fetchall()
        position = connection.execute(
            """SELECT security_id, pit_release_id, manifest_sha256
            FROM us_paper_positions WHERE code=?""",
            (code,),
        ).fetchone()
        candidates = [tuple(row) for row in order_rows]
        if position is not None:
            candidates.append(tuple(position))
        security_ids = {str(row[0]) for row in candidates}
        if len(security_ids) > 1:
            raise USPaperConflictError(
                f"observation alias {code} maps to multiple stable securities"
            )
        claimed = item.get("claimed_security_id")
        if claimed is not None:
            stable_id = _security_id(claimed)
            if security_ids and stable_id not in security_ids:
                raise USPaperConflictError(
                    "observation security_id conflicts with current paper identity"
                )
            security_ids.add(stable_id)
        if not security_ids:
            return {
                "security_id": None,
                "pit_release_id": None,
                "manifest_sha256": None,
            }
        security_id = security_ids.pop()

        # The active rebalance order is the causal decision provenance.  A
        # position-only mark retains its entry provenance.
        bindings = {
            (str(row[1]), str(row[2]))
            for row in order_rows
            if str(row[0]) == security_id
        }
        if len(bindings) > 1:
            raise USPaperConflictError(
                "paper orders for one alias have conflicting release provenance"
            )
        if bindings:
            release_id, manifest_hash = bindings.pop()
        elif position is not None and str(position["security_id"]) == security_id:
            release_id = str(position["pit_release_id"])
            manifest_hash = str(position["manifest_sha256"])
        else:
            release_id = item.get("claimed_pit_release_id")
            manifest_hash = item.get("claimed_manifest_sha256")
            if release_id is None or manifest_hash is None:
                return {
                    "security_id": security_id,
                    "pit_release_id": None,
                    "manifest_sha256": None,
                }
            release_id = _sha256_id(release_id, "pit_release_id")
            manifest_hash = _sha256_id(manifest_hash, "manifest_sha256")
        claimed_release = item.get("claimed_pit_release_id")
        claimed_manifest = item.get("claimed_manifest_sha256")
        if claimed_release is not None and _sha256_id(
            claimed_release, "pit_release_id"
        ) != release_id:
            raise USPaperConflictError(
                "observation pit_release_id conflicts with paper provenance"
            )
        if claimed_manifest is not None and _sha256_id(
            claimed_manifest, "manifest_sha256"
        ) != manifest_hash:
            raise USPaperConflictError(
                "observation manifest_sha256 conflicts with paper provenance"
            )
        return {
            "security_id": security_id,
            "pit_release_id": release_id,
            "manifest_sha256": manifest_hash,
        }

    def tick(self, session_date: str | date, *, now: datetime) -> dict[str, Any]:
        current = _aware(now)
        session_day = _date_value(session_date)
        if current.date() != session_day:
            raise USPaperCausalityError("tick now must belong to session_date in New York")
        session_key = session_day.isoformat()
        open_at = datetime.combine(session_day, self.config.market_open, NY_TZ)
        close_at = datetime.combine(session_day, self.config.market_close, NY_TZ)
        deadline = open_at + timedelta(seconds=self.config.open_capture_seconds)

        with self._store.transaction() as connection:
            account = self._account(connection)
            self._ensure_session(connection, session_key, deadline, current)
            killed = account["status"] == USPaperState.KILLED.value

            required = self._required_open_codes(connection, session_key)
            accepted_open = self._accepted_observations(
                connection, session_key, "OPEN"
            )
            accepted_codes = set(accepted_open)

            if current < open_at:
                state = (
                    USPaperTickState.KILLED
                    if killed
                    else USPaperTickState.WAITING_OPEN
                    if required
                    else USPaperTickState.IDLE
                )
                self._set_session_state(connection, session_key, state.value, current)
                return self._tick_result(connection, session_key)

            if current > deadline:
                missing = sorted(required - accepted_codes)
                if missing:
                    connection.execute(
                        """UPDATE us_paper_orders SET status='EXPIRED',
                        block_reason='LATE_OPEN_NOT_BACKFILLED'
                        WHERE status='WAITING_OPEN' AND substr(eligible_at, 1, 10)=?
                        AND code IN (%s)"""
                        % ",".join("?" for _ in missing),
                        (session_key, *missing),
                    )
                    self._degrade(
                        connection,
                        session_key,
                        "MISSING_TIMELY_OPEN:" + ",".join(missing),
                        current,
                    )

            if accepted_open:
                waiting_orders = connection.execute(
                    """SELECT code, side FROM us_paper_orders
                    WHERE status='WAITING_OPEN'
                    AND substr(eligible_at, 1, 10)=?""",
                    (session_key,),
                ).fetchall()
                sell_codes = {
                    str(row["code"])
                    for row in waiting_orders
                    if str(row["side"]) == "SELL"
                }
                buy_codes = {
                    str(row["code"])
                    for row in waiting_orders
                    if str(row["side"]) == "BUY"
                }
                risk_codes: set[str] = set()
                for position in connection.execute(
                    "SELECT * FROM us_paper_positions"
                ).fetchall():
                    code = str(position["code"])
                    observation = accepted_open.get(code)
                    if observation is None:
                        continue
                    detected_session = str(
                        position["recovery_detected_session"] or ""
                    )
                    recovery_due = (
                        int(position["recovery_exit_pending"] or 0) == 1
                        and detected_session
                        and session_key > detected_session
                    )
                    opening = _positive(observation["open"], "open")
                    if recovery_due or opening <= float(position["stop_price"]):
                        risk_codes.add(code)

                # Risk exits are globally first, then other SELLs, then BUYs.
                # A BUY is not processed until every required opening quote is
                # present, because a not-yet-observed SELL/risk exit must never
                # be economically sequenced after it.
                missing_required = required - accepted_codes
                for code in sorted(
                    accepted_open,
                    key=lambda item: (
                        0
                        if item in risk_codes
                        else 1
                        if item in sell_codes
                        else 2,
                        item,
                    ),
                ):
                    if code in buy_codes and missing_required and current <= deadline:
                        continue
                    self._process_open(
                        connection,
                        code,
                        accepted_open[code],
                        session_key,
                        current,
                    )
                self._set_session_opened(connection, session_key, current)

            account = self._account(connection)
            session = self._session(connection, session_key)
            if current <= deadline:
                if account["status"] == USPaperState.KILLED.value:
                    state = USPaperTickState.KILLED
                elif accepted_open:
                    state = USPaperTickState.OPEN_CAPTURED
                else:
                    state = USPaperTickState.WAITING_OPEN if required else USPaperTickState.IDLE
                self._set_session_state(connection, session_key, state.value, current)
                return self._tick_result(connection, session_key)

            if current < close_at:
                state = (
                    USPaperTickState.KILLED
                    if account["status"] == USPaperState.KILLED.value
                    else USPaperTickState.DATA_DEGRADED
                    if account["status"] == USPaperState.DATA_DEGRADED.value
                    else USPaperTickState.MONITORING
                )
                self._set_session_state(connection, session_key, state.value, current)
                return self._tick_result(connection, session_key)

            daily = self._accepted_observations(connection, session_key, "DAILY")
            position_codes = {
                str(row["code"])
                for row in connection.execute(
                    "SELECT code FROM us_paper_positions"
                ).fetchall()
            }
            missing_daily = sorted(position_codes - set(daily))
            if missing_daily:
                self._degrade(
                    connection,
                    session_key,
                    "MISSING_DAILY_MARK:" + ",".join(missing_daily),
                    current,
                )
            for code in sorted(daily):
                self._process_daily(
                    connection,
                    code,
                    daily[code],
                    accepted_open.get(code),
                    session_key,
                    current,
                )

            account = self._account(connection)
            state = (
                USPaperTickState.KILLED
                if account["status"] == USPaperState.KILLED.value
                else USPaperTickState.DATA_DEGRADED
                if account["status"] == USPaperState.DATA_DEGRADED.value
                else USPaperTickState.SESSION_CLOSED
            )
            connection.execute(
                "UPDATE us_paper_sessions SET state=?, closed_at=?, updated_at=? WHERE session_date=?",
                (state.value, current.isoformat(), current.isoformat(), session_key),
            )
            return self._tick_result(connection, session_key)

    def _normalize_observation(
        self,
        source: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        code = _us_code(source.get("code"))
        kind = str(source.get("kind") or "").strip().upper()
        if kind not in {"OPEN", "DAILY", "INTRADAY"}:
            raise ValueError("observation kind must be OPEN, DAILY or INTRADAY")
        session_day = _date_value(source.get("session_date"))
        open_at = datetime.combine(session_day, self.config.market_open, NY_TZ)
        close_at = datetime.combine(session_day, self.config.market_close, NY_TZ)
        deadline = open_at + timedelta(seconds=self.config.open_capture_seconds)
        default_event = (
            open_at if kind == "OPEN" else close_at if kind == "DAILY" else now
        )
        event_at = _aware(source.get("event_at") or default_event)
        available_at = _aware(source.get("available_at") or event_at)
        if event_at.date() != session_day or available_at < event_at or available_at > now:
            raise USPaperCausalityError(
                "observation must be available, causal and in the stated session"
            )
        if kind == "OPEN" and event_at < open_at:
            raise USPaperCausalityError("OPEN observation predates the market open")
        if kind == "DAILY" and available_at < close_at:
            raise USPaperCausalityError("DAILY observation is unavailable before close")
        if kind == "INTRADAY" and not open_at <= event_at < close_at:
            raise USPaperCausalityError(
                "INTRADAY observation must belong to the regular session"
            )

        values = {
            name: _optional_price(source.get(name), name)
            for name in ("open", "high", "low", "close")
        }
        if kind == "OPEN" and values["open"] is None:
            raise ValueError("OPEN observation requires open")
        if kind == "INTRADAY" and values["close"] is None:
            raise ValueError("INTRADAY observation requires a current sell reference")
        if kind == "DAILY":
            if any(values[name] is None for name in ("open", "low", "close")):
                raise ValueError("DAILY observation requires open, low and close")
            high = values["high"]
            low = float(values["low"])
            opening = float(values["open"])
            closing = float(values["close"])
            if low > min(opening, closing) or (
                high is not None and float(high) < max(opening, closing)
            ):
                raise ValueError("invalid DAILY OHLC relationship")

        canonical = {
            "code": code,
            "session_date": session_day.isoformat(),
            "kind": kind,
            "event_at": event_at.isoformat(),
            "available_at": available_at.isoformat(),
            **values,
        }
        if kind == "DAILY":
            # DAILY source evidence is part of the immutable observation, not
            # transient runtime metadata.  Qualification can therefore replay
            # the TDX row hash independently of the derived OHLC columns.
            for field in (
                "source_schema",
                "source",
                "source_code",
                "frequency",
                "adjustment",
                "source_rows",
                "source_sha256",
            ):
                if field in source:
                    canonical[field] = json.loads(_json(source[field]))
        content_hash = _hash(canonical)
        caller_idempotency = bool(source.get("idempotency_key"))
        caller_observation_id = bool(source.get("observation_id"))
        idempotency_key = str(source.get("idempotency_key") or content_hash)
        observation_id = str(source.get("observation_id") or f"uspo_{content_hash[:24]}")
        status = "ACCEPTED"
        # First ingestion, not the caller's event timestamp, decides timeliness.
        if kind == "OPEN" and now > deadline:
            status = "LATE_IGNORED"
        return {
            **canonical,
            "content_hash": content_hash,
            "idempotency_key": idempotency_key,
            "observation_id": observation_id,
            "status": status,
            "payload_json": _json(canonical),
            "caller_idempotency": caller_idempotency,
            "caller_observation_id": caller_observation_id,
            "claimed_security_id": source.get("security_id"),
            "claimed_pit_release_id": source.get("pit_release_id"),
            "claimed_manifest_sha256": source.get("manifest_sha256"),
        }

    def _process_open(
        self,
        connection: sqlite3.Connection,
        code: str,
        observation: sqlite3.Row,
        session_key: str,
        now: datetime,
    ) -> None:
        if observation["processed_at"]:
            return
        opening = _positive(observation["open"], "open")
        position = connection.execute(
            "SELECT * FROM us_paper_positions WHERE code=?", (code,)
        ).fetchone()
        risk_exit_reason = ""
        if position is not None:
            recovery_pending = int(position["recovery_exit_pending"] or 0) == 1
            detected_session = str(position["recovery_detected_session"] or "")
            if recovery_pending and not detected_session:
                raise USPaperConflictError(
                    f"recovery exit for {position['security_id']} lacks detection session"
                )
            if recovery_pending and session_key > detected_session:
                risk_exit_reason = "US_MISSED_STOP_RECOVERY_OPEN"
            elif opening <= float(position["stop_price"]):
                risk_exit_reason = "US_FIXED_STOP_GAP"
            if risk_exit_reason:
                security_id = str(position["security_id"])
                self._fill_stop(
                    connection,
                    position,
                    opening,
                    risk_exit_reason,
                    session_key,
                    now,
                )
                # A risk exit always dominates any erroneous same-security BUY
                # staged for this opening observation.
                connection.execute(
                    """UPDATE us_paper_orders SET status='BLOCKED',
                    block_reason='RISK_EXIT_TAKES_PRECEDENCE'
                    WHERE security_id=? AND side='BUY' AND status='WAITING_OPEN'
                    AND substr(eligible_at, 1, 10)=?""",
                    (security_id, session_key),
                )
                if risk_exit_reason == "US_MISSED_STOP_RECOVERY_OPEN":
                    self._event(
                        connection,
                        "NEXT_OPEN_RECOVERY_EXIT_EXECUTED",
                        "HIGH",
                        _json(
                            {
                                "security_id": security_id,
                                "code": code,
                                "session": session_key,
                                "opening": opening,
                                "reason": risk_exit_reason,
                                "synthetic_intraday_fill": False,
                            }
                        ),
                        now,
                    )

        orders = connection.execute(
            """SELECT * FROM us_paper_orders
            WHERE code=? AND status='WAITING_OPEN'
            AND substr(eligible_at, 1, 10)=?
            ORDER BY CASE side WHEN 'SELL' THEN 0 ELSE 1 END, order_id""",
            (code, session_key),
        ).fetchall()
        for order in orders:
            if now < _aware(order["eligible_at"]):
                continue
            if now > _aware(order["expires_at"]):
                connection.execute(
                    "UPDATE us_paper_orders SET status='EXPIRED', block_reason='SIGNAL_EXPIRED' WHERE order_id=?",
                    (order["order_id"],),
                )
                continue
            if order["side"] == "SELL":
                current_position = connection.execute(
                    "SELECT * FROM us_paper_positions WHERE security_id=?",
                    (order["security_id"],),
                ).fetchone()
                if current_position is None:
                    connection.execute(
                        "UPDATE us_paper_orders SET status='SKIPPED', block_reason='NO_POSITION' WHERE order_id=?",
                        (order["order_id"],),
                    )
                else:
                    self._fill_order(
                        connection,
                        order,
                        int(current_position["quantity"]),
                        opening,
                        str(order["reason"] or "REBALANCE"),
                        now,
                    )
            else:
                account = self._account(connection)
                if account["status"] != USPaperState.RUNNING.value:
                    connection.execute(
                        """UPDATE us_paper_orders SET status='BLOCKED',
                        block_reason='DATA_DEGRADED' WHERE order_id=?""",
                        (order["order_id"],),
                    )
                    continue
                if connection.execute(
                    "SELECT 1 FROM us_paper_positions WHERE security_id=?",
                    (order["security_id"],),
                ).fetchone():
                    connection.execute(
                        "UPDATE us_paper_orders SET status='SKIPPED', block_reason='POSITION_EXISTS' WHERE order_id=?",
                        (order["order_id"],),
                    )
                    continue
                position_count = int(
                    connection.execute("SELECT COUNT(*) FROM us_paper_positions").fetchone()[0]
                )
                if position_count >= self.config.max_positions:
                    connection.execute(
                        "UPDATE us_paper_orders SET status='BLOCKED', block_reason='POSITION_LIMIT' WHERE order_id=?",
                        (order["order_id"],),
                    )
                    continue
                equity = self._equity(connection)
                target_weight = min(
                    float(order["target_weight"]), self.config.max_symbol_weight
                )
                execution = opening * (1 + self.config.slippage_rate)
                quantity = int(equity * target_weight / execution)
                quantity = self._affordable_quantity(
                    float(account["cash"]), quantity, execution
                )
                if quantity <= 0:
                    connection.execute(
                        "UPDATE us_paper_orders SET status='BLOCKED', block_reason='INSUFFICIENT_CASH' WHERE order_id=?",
                        (order["order_id"],),
                    )
                else:
                    self._fill_order(
                        connection,
                        order,
                        quantity,
                        opening,
                        str(order["reason"] or "US_MOMENTUM_ENTRY"),
                        now,
                    )
        connection.execute(
            "UPDATE us_paper_observations SET processed_at=? WHERE observation_id=?",
            (now.isoformat(), observation["observation_id"]),
        )

    def _process_daily(
        self,
        connection: sqlite3.Connection,
        code: str,
        observation: sqlite3.Row,
        open_observation: sqlite3.Row | None,
        session_key: str,
        now: datetime,
    ) -> None:
        if observation["processed_at"]:
            return
        position = connection.execute(
            "SELECT * FROM us_paper_positions WHERE code=?", (code,)
        ).fetchone()
        if position is not None:
            low = _positive(observation["low"], "low")
            close = _positive(observation["close"], "close")
            stop = float(position["stop_price"])
            opening = _positive(observation["open"], "open")
            if opening <= stop and open_observation is None:
                self._schedule_recovery_exit(
                    connection,
                    position,
                    session_key=session_key,
                    reason="US_UNRESOLVED_STOP_GAP_RECOVERY",
                    observation_id=str(observation["observation_id"]),
                    now=now,
                )
                self._degrade(
                    connection,
                    session_key,
                    f"UNRESOLVED_STOP_GAP:{code}",
                    now,
                )
            elif opening > stop and low <= stop:
                # A closing DAILY low only proves that a minute-level watcher
                # missed a stop. It cannot establish a causal intraday fill.
                # Keep the position, block new BUYs and require a next-session
                # recovery exit rather than manufacturing a stop-price fill.
                self._schedule_recovery_exit(
                    connection,
                    position,
                    session_key=session_key,
                    reason="US_MISSED_INTRADAY_STOP_RECOVERY",
                    observation_id=str(observation["observation_id"]),
                    now=now,
                )
                self._degrade(
                    connection,
                    session_key,
                    f"MISSED_INTRADAY_STOP:{code}",
                    now,
                )
            if connection.execute(
                "SELECT 1 FROM us_paper_positions WHERE code=?", (code,)
            ).fetchone():
                connection.execute(
                    "UPDATE us_paper_positions SET last_price=?, updated_at=? WHERE code=?",
                    (close, now.isoformat(), code),
                )
        connection.execute(
            "UPDATE us_paper_observations SET processed_at=? WHERE observation_id=?",
            (now.isoformat(), observation["observation_id"]),
        )

    @staticmethod
    def _schedule_recovery_exit(
        connection: sqlite3.Connection,
        position: sqlite3.Row,
        *,
        session_key: str,
        reason: str,
        observation_id: str,
        now: datetime,
    ) -> None:
        """Persist first evidence of a stop missed by the realtime watcher."""

        security_id = str(position["security_id"])
        already_pending = int(position["recovery_exit_pending"] or 0) == 1
        if not already_pending:
            connection.execute(
                """UPDATE us_paper_positions SET recovery_exit_pending=1,
                recovery_reason=?, recovery_detected_session=?,
                recovery_detected_at=?, recovery_observation_id=?, updated_at=?
                WHERE security_id=?""",
                (
                    reason,
                    session_key,
                    now.isoformat(),
                    observation_id,
                    now.isoformat(),
                    security_id,
                ),
            )
            USPaperExecutor._event(
                connection,
                "NEXT_OPEN_RECOVERY_EXIT_SCHEDULED",
                "CRITICAL",
                _json(
                    {
                        "security_id": security_id,
                        "code": position["code"],
                        "detected_session": session_key,
                        "observation_id": observation_id,
                        "reason": reason,
                        "execution_policy": "FIRST_SUBSEQUENT_TIMELY_OPEN",
                        "synthetic_intraday_fill": False,
                    }
                ),
                now,
            )

    def _fill_stop(
        self,
        connection: sqlite3.Connection,
        position: sqlite3.Row,
        reference_price: float,
        reason: str,
        session_key: str,
        now: datetime,
    ) -> None:
        code = str(position["code"])
        security_id = str(position["security_id"])
        idempotency = f"stop:{security_id}:{session_key}:{reason}"
        order = connection.execute(
            "SELECT * FROM us_paper_orders WHERE idempotency_key=?", (idempotency,)
        ).fetchone()
        if order is None:
            order_id = f"uspor_{_hash(idempotency)[:24]}"
            payload_hash = _hash(
                {
                    "security_id": security_id,
                    "code": code,
                    "pit_release_id": position["pit_release_id"],
                    "manifest_sha256": position["manifest_sha256"],
                    "reason": reason,
                    "session": session_key,
                }
            )
            connection.execute(
                """INSERT INTO us_paper_orders
                (order_id, period_id, idempotency_key, security_id, code,
                 pit_release_id, manifest_sha256, side, order_kind, target_weight,
                 stop_ratio, eligible_at, expires_at, status, reason, payload_hash,
                 created_at)
                VALUES (?, NULL, ?, ?, ?, ?, ?, 'SELL', 'STOP', 0, 0, ?, ?,
                        'WAITING_OPEN', ?, ?, ?)""",
                (
                    order_id,
                    idempotency,
                    security_id,
                    code,
                    position["pit_release_id"],
                    position["manifest_sha256"],
                    now.isoformat(),
                    now.isoformat(),
                    reason,
                    payload_hash,
                    now.isoformat(),
                ),
            )
            order = connection.execute(
                "SELECT * FROM us_paper_orders WHERE order_id=?", (order_id,)
            ).fetchone()
        self._fill_order(
            connection,
            order,
            int(position["quantity"]),
            reference_price,
            reason,
            now,
            exact_reference=reason == "US_FIXED_STOP",
        )

    def _fill_order(
        self,
        connection: sqlite3.Connection,
        order: sqlite3.Row,
        quantity: int,
        reference_price: float,
        reason: str,
        now: datetime,
        *,
        exact_reference: bool = False,
    ) -> None:
        if quantity <= 0 or order["status"] == "FILLED":
            return
        side = str(order["side"])
        if exact_reference:
            execution = reference_price * (1 - self.config.slippage_rate)
        else:
            execution = reference_price * (
                1 + self.config.slippage_rate if side == "BUY" else 1 - self.config.slippage_rate
            )
        value = execution * quantity
        fees = self._fees(side, value, quantity)
        account = self._account(connection)
        cash = float(account["cash"])
        if side == "BUY":
            cash -= value + fees
            stop_ratio = float(order["stop_ratio"] or self.config.stop_ratio)
            connection.execute(
                """INSERT INTO us_paper_positions
                (security_id, code, pit_release_id, manifest_sha256, quantity,
                 average_price, stop_price, last_price, entry_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order["security_id"],
                    order["code"],
                    order["pit_release_id"],
                    order["manifest_sha256"],
                    quantity,
                    execution,
                    execution * (1 - stop_ratio),
                    execution,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        else:
            position = connection.execute(
                "SELECT * FROM us_paper_positions WHERE security_id=?",
                (order["security_id"],),
            ).fetchone()
            if position is None:
                return
            quantity = min(quantity, int(position["quantity"]))
            value = execution * quantity
            fees = self._fees(side, value, quantity)
            cash += value - fees
            connection.execute(
                "DELETE FROM us_paper_positions WHERE security_id=?",
                (order["security_id"],),
            )
        fill_key = f"fill:{order['order_id']}:{side}:{reason}"
        fill_id = f"uspf_{_hash(fill_key)[:24]}"
        connection.execute(
            """INSERT OR IGNORE INTO us_paper_fills
            (fill_id, idempotency_key, order_id, security_id, code,
             pit_release_id, manifest_sha256, side, quantity, price, fees,
             reason, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fill_id,
                fill_key,
                order["order_id"],
                order["security_id"],
                order["code"],
                order["pit_release_id"],
                order["manifest_sha256"],
                side,
                quantity,
                execution,
                fees,
                reason,
                now.isoformat(),
            ),
        )
        connection.execute(
            "UPDATE us_paper_orders SET status='FILLED', filled_at=? WHERE order_id=?",
            (now.isoformat(), order["order_id"]),
        )
        connection.execute(
            "UPDATE us_paper_account SET cash=?, updated_at=? WHERE account_id=?",
            (cash, now.isoformat(), ACCOUNT_ID),
        )

    def _fees(self, side: str, value: float, quantity: int) -> float:
        commission = max(self.config.min_commission, value * self.config.commission_rate)
        if side == "BUY":
            return commission
        return (
            commission
            + value * self.config.sec_sell_fee_rate
            + min(self.config.finra_taf_cap, quantity * self.config.finra_taf_per_share)
        )

    def _affordable_quantity(self, cash: float, quantity: int, execution: float) -> int:
        result = max(0, quantity)
        while result > 0:
            value = result * execution
            if value + max(self.config.min_commission, value * self.config.commission_rate) <= cash:
                break
            result -= 1
        return result

    def _equity(self, connection: sqlite3.Connection) -> float:
        cash = float(self._account(connection)["cash"])
        positions = connection.execute(
            "SELECT quantity, last_price FROM us_paper_positions"
        ).fetchall()
        return cash + sum(int(row["quantity"]) * float(row["last_price"]) for row in positions)

    @staticmethod
    def _account(connection: sqlite3.Connection) -> sqlite3.Row:
        return connection.execute(
            "SELECT * FROM us_paper_account WHERE account_id=?", (ACCOUNT_ID,)
        ).fetchone()

    @staticmethod
    def _session(connection: sqlite3.Connection, session_key: str) -> sqlite3.Row:
        return connection.execute(
            "SELECT * FROM us_paper_sessions WHERE session_date=?", (session_key,)
        ).fetchone()

    def _ensure_session(
        self,
        connection: sqlite3.Connection,
        session_key: str,
        deadline: datetime,
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO us_paper_sessions
            (session_date, state, open_deadline, updated_at)
            VALUES (?, ?, ?, ?)""",
            (
                session_key,
                USPaperTickState.IDLE.value,
                deadline.isoformat(),
                now.isoformat(),
            ),
        )

    def _required_open_codes(
        self, connection: sqlite3.Connection, session_key: str
    ) -> set[str]:
        positions = {
            str(row[0])
            for row in connection.execute("SELECT code FROM us_paper_positions").fetchall()
        }
        orders = {
            str(row[0])
            for row in connection.execute(
                """SELECT code FROM us_paper_orders WHERE status='WAITING_OPEN'
                AND substr(eligible_at, 1, 10)=?""",
                (session_key,),
            ).fetchall()
        }
        return positions | orders

    @staticmethod
    def _accepted_observations(
        connection: sqlite3.Connection,
        session_key: str,
        kind: str,
    ) -> dict[str, sqlite3.Row]:
        rows = connection.execute(
            """SELECT * FROM us_paper_observations
            WHERE session_date=? AND kind=? AND status='ACCEPTED'
            ORDER BY ingested_at, observation_id""",
            (session_key, kind),
        ).fetchall()
        result: dict[str, sqlite3.Row] = {}
        for row in rows:
            result.setdefault(str(row["code"]), row)
        return result

    def _degrade(
        self,
        connection: sqlite3.Connection,
        session_key: str,
        reason: str,
        now: datetime,
    ) -> None:
        self._ensure_session(
            connection,
            session_key,
            datetime.combine(
                date.fromisoformat(session_key), self.config.market_open, NY_TZ
            )
            + timedelta(seconds=self.config.open_capture_seconds),
            now,
        )
        connection.execute(
            """UPDATE us_paper_account SET status=?, degraded_reason=?, updated_at=?
            WHERE account_id=? AND status<>?""",
            (
                USPaperState.DATA_DEGRADED.value,
                reason,
                now.isoformat(),
                ACCOUNT_ID,
                USPaperState.KILLED.value,
            ),
        )
        connection.execute(
            """UPDATE us_paper_sessions SET state=?, degraded_reason=?, updated_at=?
            WHERE session_date=?""",
            (USPaperTickState.DATA_DEGRADED.value, reason, now.isoformat(), session_key),
        )
        self._event(connection, "DATA_DEGRADED", "HIGH", reason, now)

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event_type: str,
        severity: str,
        details: str,
        now: datetime,
    ) -> None:
        key = f"{event_type}:{now.isoformat()}:{details}"
        event_id = f"uspe_{_hash(key)[:24]}"
        connection.execute(
            """INSERT OR IGNORE INTO us_paper_events
            (event_id, idempotency_key, event_type, severity, occurred_at, details_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, key, event_type, severity, now.isoformat(), _json({"detail": details})),
        )

    @staticmethod
    def _set_session_state(
        connection: sqlite3.Connection,
        session_key: str,
        state: str,
        now: datetime,
    ) -> None:
        current = connection.execute(
            "SELECT state FROM us_paper_sessions WHERE session_date=?", (session_key,)
        ).fetchone()
        if current is not None and current["state"] == USPaperTickState.DATA_DEGRADED.value:
            return
        connection.execute(
            "UPDATE us_paper_sessions SET state=?, updated_at=? WHERE session_date=?",
            (state, now.isoformat(), session_key),
        )

    @staticmethod
    def _set_session_opened(
        connection: sqlite3.Connection, session_key: str, now: datetime
    ) -> None:
        connection.execute(
            """UPDATE us_paper_sessions SET opened_at=COALESCE(opened_at, ?),
            updated_at=? WHERE session_date=?""",
            (now.isoformat(), now.isoformat(), session_key),
        )

    def _tick_result(
        self, connection: sqlite3.Connection, session_key: str
    ) -> dict[str, Any]:
        account = dict(self._account(connection))
        session = dict(self._session(connection, session_key))
        return {
            "mode": "PAPER",
            "paper_only": True,
            "state": session["state"],
            "account_status": account["status"],
            "session": session,
            "cash": float(account["cash"]),
            "positions": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM us_paper_positions ORDER BY code"
                ).fetchall()
            ],
            "fills": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM us_paper_fills ORDER BY filled_at, fill_id"
                ).fetchall()
            ],
        }


class USMomentumPaperService:
    """Monthly US-momentum planning plus the isolated paper executor."""

    def __init__(self, config: USPaperConfig) -> None:
        self.config = config
        self._store = _USPaperStore(config)
        self.executor = USPaperExecutor(config, self._store)

    def create_period(
        self,
        signals: Iterable[Any],
        *,
        now: datetime,
        execution_session: str | date | None = None,
        decision_at: datetime | None = None,
        pit_release_id: str | None = None,
        manifest_sha256: str | None = None,
        position_aliases: Mapping[str, str] | None = None,
        test_fixture_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = _aware(now)
        if test_fixture_identity and not self.config.allow_test_fixture_identity:
            raise ValueError(
                "explicit_test_fixture identity is disabled for this paper database"
            )
        normalized = [
            _normalize_signal(item, test_fixture_identity=test_fixture_identity)
            for item in signals
        ]
        if normalized:
            signal_releases = {item["pit_release_id"] for item in normalized}
            if len(signal_releases) != 1:
                raise USPaperConflictError(
                    "all signals in one paper period must use one PIT release"
                )
            inferred_release = signal_releases.pop()
            release_id = _sha256_id(
                pit_release_id or inferred_release, "pit_release_id"
            )
            if release_id != inferred_release:
                raise USPaperConflictError(
                    "pit_release_id does not match signal evidence"
                )
            evidence_manifests = {
                item["manifest_sha256"]
                for item in normalized
                if item["manifest_sha256"] is not None
            }
            if len(evidence_manifests) > 1:
                raise USPaperConflictError(
                    "all signals in one paper period must use one manifest"
                )
            inferred_manifest = next(iter(evidence_manifests), None)
            if manifest_sha256 is None and inferred_manifest is None:
                raise ValueError(
                    "manifest_sha256 is required for an auditable paper period"
                )
            manifest_hash = _sha256_id(
                manifest_sha256 or inferred_manifest, "manifest_sha256"
            )
            if inferred_manifest is not None and manifest_hash != inferred_manifest:
                raise USPaperConflictError(
                    "manifest_sha256 does not match signal evidence"
                )
            for item in normalized:
                item["manifest_sha256"] = manifest_hash
            periods = {item["generated_at"][:7] for item in normalized}
            if len(periods) != 1:
                raise ValueError("all signals in a paper period must share a decision month")
            period_key = periods.pop()
            for item in normalized:
                generated = _aware(item["generated_at"])
                available = _aware(item["available_at"])
                expires = _aware(item["valid_until"])
                if generated > current:
                    raise USPaperCausalityError("a signal cannot be planned before it is generated")
                if not generated < available <= expires:
                    raise USPaperCausalityError("signal availability interval is invalid")
            inferred_session = _aware(normalized[0]["available_at"]).date()
            if any(_aware(item["available_at"]).date() != inferred_session for item in normalized):
                raise ValueError("all signals must share one next-open execution session")
            chosen_session = _date_value(execution_session or inferred_session)
            if chosen_session != inferred_session:
                raise USPaperCausalityError("execution_session must match signal available_at")
            decision_time = max(_aware(item["generated_at"]) for item in normalized)
            if decision_at is not None and _aware(decision_at) != decision_time:
                raise USPaperConflictError(
                    "decision_at must equal the latest signal generation time"
                )
        else:
            if decision_at is None or execution_session is None:
                raise ValueError(
                    "an empty paper period requires decision_at and execution_session"
                )
            decision_time = _aware(decision_at)
            if decision_time > current:
                raise USPaperCausalityError(
                    "an empty decision period cannot be recorded before decision_at"
                )
            chosen_session = _date_value(execution_session)
            if chosen_session <= decision_time.date():
                raise USPaperCausalityError(
                    "an empty period execution_session must follow decision_at"
                )
            period_key = decision_time.strftime("%Y-%m")
            release_id = _sha256_id(pit_release_id, "pit_release_id")
            manifest_hash = _sha256_id(manifest_sha256, "manifest_sha256")

        requested_position_aliases = {
            _security_id(security_id): _us_code(code)
            for security_id, code in (position_aliases or {}).items()
        }
        if len(set(requested_position_aliases.values())) != len(
            requested_position_aliases
        ):
            raise USPaperConflictError(
                "position alias migration maps multiple stable securities to one ticker"
            )

        aliases_by_security: dict[str, set[str]] = {}
        for item in normalized:
            aliases_by_security.setdefault(item["security_id"], set()).add(item["code"])
        for security_id, code in requested_position_aliases.items():
            aliases_by_security.setdefault(security_id, set()).add(code)
        ambiguous = {
            security_id: sorted(codes)
            for security_id, codes in aliases_by_security.items()
            if len(codes) != 1
        }
        if ambiguous:
            raise USPaperConflictError(
                f"one stable security has multiple aliases in a period: {ambiguous}"
            )
        sides_by_security: dict[str, set[str]] = {}
        for item in normalized:
            sides_by_security.setdefault(item["security_id"], set()).add(item["side"])
        opposing = {
            security_id: sorted(sides)
            for security_id, sides in sides_by_security.items()
            if len(sides) != 1
        }
        if opposing:
            raise USPaperConflictError(
                f"one stable security has opposing sides in a period: {opposing}"
            )

        normalized.sort(
            key=lambda item: (
                item["side"] != "SELL",
                item["security_id"],
                item["code"],
                item["signal_id"],
            )
        )
        signal_hash = _hash(
            {
                "decision_at": decision_time.isoformat(),
                "execution_session": chosen_session.isoformat(),
                "pit_release_id": release_id,
                "manifest_sha256": manifest_hash,
                "position_aliases": sorted(requested_position_aliases.items()),
                "signals": normalized,
            }
        )
        period_id = f"uspp_{_hash(period_key)[:24]}"
        with self._store.transaction() as connection:
            account = connection.execute(
                "SELECT * FROM us_paper_account WHERE account_id=?", (ACCOUNT_ID,)
            ).fetchone()
            if account["status"] == USPaperState.KILLED.value:
                raise USPaperStateError("paper sleeve is killed")
            existing = connection.execute(
                "SELECT * FROM us_paper_periods WHERE period_key=?", (period_key,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["signal_hash"]) != signal_hash
                    or str(existing["pit_release_id"]) != release_id
                    or str(existing["manifest_sha256"]) != manifest_hash
                ):
                    raise USPaperConflictError(
                        "paper period already exists with different signals"
                    )
                return self._period(connection, str(existing["period_id"]))

            # A ticker rename is metadata, not a trade.  Move the alias on the
            # stable position key before creating orders for the new alias.
            for security_id in sorted(requested_position_aliases):
                if connection.execute(
                    "SELECT 1 FROM us_paper_positions WHERE security_id=?",
                    (security_id,),
                ).fetchone() is None:
                    raise USPaperConflictError(
                        f"position alias migration references an unheld security: {security_id}"
                    )
            for security_id, codes in sorted(aliases_by_security.items()):
                self._migrate_alias(
                    connection,
                    security_id=security_id,
                    new_code=next(iter(codes)),
                    pit_release_id=release_id,
                    manifest_sha256=manifest_hash,
                    now=current,
                )
            connection.execute(
                """INSERT INTO us_paper_periods
                (period_id, period_key, decision_at, execution_session, signal_hash,
                 pit_release_id, manifest_sha256, status, auto_approved_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'AUTO_APPROVED', ?, ?)""",
                (
                    period_id,
                    period_key,
                    decision_time.isoformat(),
                    chosen_session.isoformat(),
                    signal_hash,
                    release_id,
                    manifest_hash,
                    current.isoformat(),
                    current.isoformat(),
                ),
            )
            connection.execute(
                """UPDATE us_paper_account
                SET pit_release_id=?, manifest_sha256=?, updated_at=?
                WHERE account_id=?""",
                (release_id, manifest_hash, current.isoformat(), ACCOUNT_ID),
            )
            seen: set[tuple[str, str]] = set()
            for item in normalized:
                pair = (item["security_id"], item["side"])
                if pair in seen:
                    raise USPaperConflictError(
                        f"duplicate {item['side']} signal for {item['security_id']}"
                    )
                seen.add(pair)
                payload_hash = _hash(item)
                idempotency = (
                    f"rebalance:{period_key}:{item['security_id']}:{item['side']}"
                )
                order_id = f"uspor_{_hash(idempotency)[:24]}"
                connection.execute(
                    """INSERT INTO us_paper_orders
                    (order_id, period_id, idempotency_key, signal_id, security_id,
                     code, pit_release_id, manifest_sha256, side, order_kind,
                     target_weight, stop_ratio, eligible_at, expires_at, status,
                     reason, payload_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'REBALANCE', ?, ?, ?, ?,
                            'WAITING_OPEN', ?, ?, ?)""",
                    (
                        order_id,
                        period_id,
                        idempotency,
                        item["signal_id"],
                        item["security_id"],
                        item["code"],
                        release_id,
                        manifest_hash,
                        item["side"],
                        item["target_weight"],
                        item["stop_ratio"],
                        item["available_at"],
                        item["valid_until"],
                        item["reason"],
                        payload_hash,
                        current.isoformat(),
                    ),
                )
            return self._period(connection, period_id)

    def rename_alias(
        self,
        security_id: str,
        new_code: str,
        *,
        pit_release_id: str,
        manifest_sha256: str,
        now: datetime,
        expected_old_code: str | None = None,
    ) -> dict[str, Any]:
        """Atomically rename a held security without creating an order/fill.

        The caller must supply the release that proves the new alias.  Pending
        orders make an out-of-band rename ambiguous and are rejected; normal
        month-end flow performs this migration inside ``create_period`` before
        inserting its orders.
        """

        stable_id = _security_id(security_id)
        alias = _us_code(new_code)
        release_id = _sha256_id(pit_release_id, "pit_release_id")
        manifest_hash = _sha256_id(manifest_sha256, "manifest_sha256")
        current = _aware(now)
        expected = _us_code(expected_old_code) if expected_old_code else None
        with self._store.transaction() as connection:
            account = self.executor._account(connection)
            if account["status"] == USPaperState.KILLED.value:
                raise USPaperStateError("paper sleeve is killed")
            position = connection.execute(
                "SELECT * FROM us_paper_positions WHERE security_id=?",
                (stable_id,),
            ).fetchone()
            if position is None:
                raise USPaperStateError(f"no held position for {stable_id}")
            if expected is not None and str(position["code"]) != expected:
                raise USPaperConflictError(
                    "held alias no longer matches expected_old_code"
                )
            waiting = connection.execute(
                """SELECT 1 FROM us_paper_orders
                WHERE security_id=? AND status='WAITING_OPEN' LIMIT 1""",
                (stable_id,),
            ).fetchone()
            if waiting is not None:
                raise USPaperStateError(
                    "cannot rename a security with a pending paper order"
                )
            self._migrate_alias(
                connection,
                security_id=stable_id,
                new_code=alias,
                pit_release_id=release_id,
                manifest_sha256=manifest_hash,
                now=current,
            )
            return dict(
                connection.execute(
                    "SELECT * FROM us_paper_positions WHERE security_id=?",
                    (stable_id,),
                ).fetchone()
            )

    @staticmethod
    def _migrate_alias(
        connection: sqlite3.Connection,
        *,
        security_id: str,
        new_code: str,
        pit_release_id: str,
        manifest_sha256: str,
        now: datetime,
    ) -> None:
        position = connection.execute(
            "SELECT * FROM us_paper_positions WHERE security_id=?",
            (security_id,),
        ).fetchone()
        if position is None or str(position["code"]) == new_code:
            return
        stale_pending = connection.execute(
            """SELECT code FROM us_paper_orders
            WHERE security_id=? AND status='WAITING_OPEN' AND code<>?
            LIMIT 1""",
            (security_id, new_code),
        ).fetchone()
        if stale_pending is not None:
            raise USPaperStateError(
                "cannot migrate an alias while an order on the old alias is pending"
            )
        collision = connection.execute(
            "SELECT security_id FROM us_paper_positions WHERE code=?",
            (new_code,),
        ).fetchone()
        if collision is not None:
            raise USPaperConflictError(
                f"alias {new_code} is already held as {collision['security_id']}"
            )
        old_code = str(position["code"])
        connection.execute(
            """UPDATE us_paper_positions SET code=?, updated_at=?
            WHERE security_id=?""",
            (new_code, now.isoformat(), security_id),
        )
        USPaperExecutor._event(
            connection,
            "SECURITY_ALIAS_RENAMED",
            "INFO",
            _json(
                {
                    "security_id": security_id,
                    "old_code": old_code,
                    "new_code": new_code,
                    "pit_release_id": pit_release_id,
                    "manifest_sha256": manifest_sha256,
                    "trade_created": False,
                }
            ),
            now,
        )

    def observe(self, observation: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        return self.executor.record_observation(observation, now=now)

    def execute_intraday_stop(
        self, observation: Mapping[str, Any], *, now: datetime
    ) -> dict[str, Any]:
        return self.executor.execute_intraday_stop(observation, now=now)

    def apply_corporate_actions(
        self,
        session_date: str | date,
        actions: Iterable[Mapping[str, Any]],
        *,
        now: datetime,
        pit_release_id: str,
        manifest_sha256: str,
    ) -> list[dict[str, Any]]:
        return self.executor.apply_corporate_actions(
            session_date,
            actions,
            now=now,
            pit_release_id=pit_release_id,
            manifest_sha256=manifest_sha256,
        )

    def tick(
        self,
        session_date: str | date,
        *,
        now: datetime,
        observations: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        for observation in observations:
            self.observe(observation, now=now)
        return self.executor.tick(session_date, now=now)

    def kill(self, *, reason: str, now: datetime) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("kill reason is required")
        current = _aware(now)
        with self._store.transaction() as connection:
            account = connection.execute(
                "SELECT * FROM us_paper_account WHERE account_id=?", (ACCOUNT_ID,)
            ).fetchone()
            if account["status"] != USPaperState.KILLED.value:
                connection.execute(
                    """UPDATE us_paper_account SET status=?, killed_at=?, kill_reason=?,
                    updated_at=? WHERE account_id=?""",
                    (
                        USPaperState.KILLED.value,
                        current.isoformat(),
                        reason.strip(),
                        current.isoformat(),
                        ACCOUNT_ID,
                    ),
                )
                connection.execute(
                    """UPDATE us_paper_orders SET status='CANCELLED',
                    block_reason='KILL_SWITCH_BUY_CANCELLED'
                    WHERE status='WAITING_OPEN' AND side='BUY'"""
                )
                USPaperExecutor._event(
                    connection,
                    "KILL_SWITCH",
                    "CRITICAL",
                    _json(
                        {
                            "reason": reason.strip(),
                            "buy_policy": "CANCEL",
                            "sell_policy": "CONTINUE_WITH_FRESH_RELIABLE_QUOTES",
                            "risk_sell_classification": "ORDER_KIND_STOP_OR_REASON_ALLOWLIST",
                        }
                    ),
                    current,
                )
        return self.status()

    def acknowledge_data_recovery(self, *, note: str, now: datetime) -> dict[str, Any]:
        if not note.strip():
            raise ValueError("recovery note is required")
        current = _aware(now)
        with self._store.transaction() as connection:
            account = connection.execute(
                "SELECT * FROM us_paper_account WHERE account_id=?", (ACCOUNT_ID,)
            ).fetchone()
            if account["status"] == USPaperState.KILLED.value:
                raise USPaperStateError("a killed paper sleeve cannot be recovered")
            pending = connection.execute(
                """SELECT security_id, code FROM us_paper_positions
                WHERE recovery_exit_pending=1 ORDER BY security_id"""
            ).fetchall()
            if pending:
                codes = ",".join(str(row["code"]) for row in pending)
                raise USPaperStateError(
                    "data recovery cannot be acknowledged while next-open "
                    f"risk exits are pending: {codes}"
                )
            connection.execute(
                """UPDATE us_paper_account SET status=?, degraded_reason='', updated_at=?
                WHERE account_id=?""",
                (USPaperState.RUNNING.value, current.isoformat(), ACCOUNT_ID),
            )
            USPaperExecutor._event(
                connection, "DATA_RECOVERY_ACK", "INFO", note.strip(), current
            )
        return self.status()

    def status(self) -> dict[str, Any]:
        account = self._store.rows(
            "SELECT * FROM us_paper_account WHERE account_id=?", (ACCOUNT_ID,)
        )[0]
        positions = self._store.rows(
            "SELECT * FROM us_paper_positions ORDER BY code"
        )
        orders = self._store.rows(
            "SELECT * FROM us_paper_orders ORDER BY created_at, order_id"
        )
        for order in orders:
            order["risk_class"] = _sell_risk_class(order)
        return {
            "mode": "PAPER",
            "paper_only": True,
            "identity_contract": "STABLE_SECURITY_ID_V1",
            "binding": {
                "scope": "LATEST_DECISION_NOT_ACCOUNT_LOCK",
                "pit_release_id": account.get("pit_release_id"),
                "manifest_sha256": account.get("manifest_sha256"),
                "rolling_releases_allowed": True,
                "test_fixture_identity_enabled": self.config.allow_test_fixture_identity,
            },
            "account": account,
            "periods": self._store.rows(
                "SELECT * FROM us_paper_periods ORDER BY period_key DESC"
            ),
            "orders": orders,
            "positions": positions,
            "recovery_exits": [
                {
                    "security_id": row["security_id"],
                    "code": row["code"],
                    "reason": row["recovery_reason"],
                    "detected_session": row["recovery_detected_session"],
                    "detected_at": row["recovery_detected_at"],
                    "observation_id": row["recovery_observation_id"],
                    "status": "PENDING_NEXT_TIMELY_OPEN",
                }
                for row in positions
                if int(row.get("recovery_exit_pending") or 0) == 1
            ],
            "fills": self._store.rows(
                "SELECT * FROM us_paper_fills ORDER BY filled_at, fill_id"
            ),
            "events": self._store.rows(
                "SELECT * FROM us_paper_events ORDER BY occurred_at, event_id"
            ),
            "corporate_actions": self._store.rows(
                """SELECT * FROM us_paper_corporate_actions
                ORDER BY effective_date, action_id"""
            ),
            "receivables": self._store.rows(
                """SELECT * FROM us_paper_receivables
                ORDER BY pay_date, receivable_id"""
            ),
            "corporate_action_cash": self._store.rows(
                """SELECT * FROM us_paper_cash_ledger
                ORDER BY occurred_at, cash_entry_id"""
            ),
        }

    @staticmethod
    def _period(connection: sqlite3.Connection, period_id: str) -> dict[str, Any]:
        period = dict(
            connection.execute(
                "SELECT * FROM us_paper_periods WHERE period_id=?", (period_id,)
            ).fetchone()
        )
        period["orders"] = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM us_paper_orders WHERE period_id=? ORDER BY order_id",
                (period_id,),
            ).fetchall()
        ]
        period["paper_only"] = True
        return period


class USPaperStateError(USPaperError):
    """The requested operation is incompatible with the paper state."""


def _normalize_signal(
    value: Any,
    *,
    test_fixture_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    def field(name: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    code = _us_code(field("code"))
    side = str(field("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("paper signals must be BUY or SELL")
    target_weight = float(field("target_weight", 0.0) or 0.0)
    if not math.isfinite(target_weight) or not 0 <= target_weight <= 1:
        raise ValueError("target_weight must be in [0, 1]")
    evidence = field("evidence", {}) or {}
    if not isinstance(evidence, Mapping):
        raise ValueError("paper signal evidence must be a mapping")
    fixture = dict(test_fixture_identity or {})
    if fixture and fixture.get("explicit_test_fixture") is not True:
        raise ValueError(
            "test_fixture_identity requires explicit_test_fixture=True"
        )
    security_id = _security_id(
        evidence.get("security_id", fixture.get("security_id"))
    )
    pit_release_id = _sha256_id(
        evidence.get("pit_release_id", fixture.get("pit_release_id")),
        "pit_release_id",
    )
    manifest_value = evidence.get("manifest_sha256")
    manifest_sha256 = (
        _sha256_id(manifest_value, "manifest_sha256")
        if manifest_value is not None
        else None
    )
    stop_ratio = float(evidence.get("stop_ratio", 0.08))
    if side == "BUY" and not 0 < stop_ratio < 1:
        raise ValueError("BUY stop_ratio must be in (0, 1)")
    reasons = field("reason_codes", ()) or ()
    reason = str(reasons[0] if isinstance(reasons, (list, tuple)) and reasons else reasons or "")
    generated = _aware(field("generated_at"))
    available = _aware(field("available_at"))
    valid_until = _aware(field("valid_until"))
    signal_id = str(field("signal_id") or _hash({
        "security_id": security_id,
        "code": code,
        "side": side,
        "generated_at": generated.isoformat(),
    }))
    return {
        "signal_id": signal_id,
        "security_id": security_id,
        "code": code,
        "pit_release_id": pit_release_id,
        "manifest_sha256": manifest_sha256,
        "side": side,
        "target_weight": target_weight,
        "stop_ratio": stop_ratio if side == "BUY" else 0.0,
        "generated_at": generated.isoformat(),
        "available_at": available.isoformat(),
        "valid_until": valid_until.isoformat(),
        "reason": reason,
    }


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


def _corporate_action_date(value: Any) -> date:
    """Decode a corporate action using the shared New York session rule."""

    parsed = ny_session_date(value)
    if parsed is None or parsed != parsed:
        raise ValueError(f"invalid corporate-action date: {value!r}")
    return parsed.date()


def _missing_scalar(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in {"", "nat", "nan", "none", "null"}


def _verified_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    # numpy.bool_ and scalar integer values produced by Parquet readers.
    if type(value).__name__ == "bool_":
        return bool(value)
    if isinstance(value, int):
        return value == 1
    return str(value).strip().lower() in {"true", "1"}


def _us_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if not code.endswith(".US") or len(code) <= 3 or any(char.isspace() for char in code):
        raise ValueError(f"invalid US code: {value!r}")
    return code


def _security_id(value: Any) -> str:
    security_id = str(value or "").strip()
    lowered = security_id.lower()
    if (
        security_id != lowered
        or not lowered.startswith("us_")
        or lowered.endswith(".us")
        or len(lowered) <= 3
        or any(char.isspace() for char in lowered)
    ):
        raise ValueError(
            "security_id must be a lowercase, non-ticker stable US identifier"
        )
    return lowered


def _sha256_id(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if len(result) != 64 or result != result.lower() or any(
        char not in "0123456789abcdef" for char in result
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 value")
    return result


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _sell_risk_class(order: Mapping[str, Any]) -> str:
    if str(order.get("side") or "").upper() != "SELL":
        return "NOT_SELL"
    if str(order.get("order_kind") or "").upper() == "STOP":
        return "RISK_EXIT"
    reason = str(order.get("reason") or "").strip().upper()
    return "RISK_EXIT" if reason in RISK_SELL_REASONS else "NORMAL_EXIT"


def _optional_price(value: Any, name: str) -> float | None:
    if value is None:
        return None
    return _positive(value, name)


def _positive_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _first_numeric_term(
    terms: Mapping[str, Any],
    names: tuple[str, ...],
    *,
    allow_zero: bool = False,
) -> float:
    for name in names:
        try:
            result = float(terms.get(name))
        except (TypeError, ValueError):
            continue
        if math.isfinite(result) and (result >= 0 if allow_zero else result > 0):
            return result
    raise ValueError(f"missing numeric corporate-action term: {','.join(names)}")


def _positive(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _config_hash(config: USPaperConfig) -> str:
    return _hash(
        {
            "initial_cash": config.initial_cash,
            "market_open": config.market_open.isoformat(),
            "market_close": config.market_close.isoformat(),
            "open_capture_seconds": config.open_capture_seconds,
            "max_positions": config.max_positions,
            "max_symbol_weight": config.max_symbol_weight,
            "stop_ratio": config.stop_ratio,
            "slippage_rate": config.slippage_rate,
            "commission_rate": config.commission_rate,
            "min_commission": config.min_commission,
            "sec_sell_fee_rate": config.sec_sell_fee_rate,
            "finra_taf_per_share": config.finra_taf_per_share,
            "finra_taf_cap": config.finra_taf_cap,
            "allow_test_fixture_identity": config.allow_test_fixture_identity,
        }
    )


def _hash(value: Any) -> str:
    payload = value if isinstance(value, str) else _json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "USMomentumPaperService",
    "USPaperCausalityError",
    "USPaperConfig",
    "USPaperConflictError",
    "USPaperError",
    "USPaperExecutor",
    "USPaperState",
    "USPaperStateError",
    "USPaperTickState",
]
