from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from research_platform.us_qualification import TDX_QUALIFICATION_SAMPLE
from research_platform.us_tdx import USQuoteObservation
from research_platform.us_tdx_shadow import (
    TDXShadowBindingError,
    TDXShadowConfig,
    TDXShadowEvidenceError,
    TDXShadowLeaseError,
    TDXShadowQualificationCollector,
    TDXShadowScheduleError,
)


NY = ZoneInfo("America/New_York")
SOURCE_HASH = "a" * 64
RELEASE_ID = "b" * 64
MANIFEST_SHA256 = "c" * 64


def _weekdays(start: date, count: int) -> tuple[date, ...]:
    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


class _QuoteClient:
    def __init__(self, factory=None) -> None:
        self.factory = factory or _fresh_quote
        self.calls: list[tuple[str, datetime]] = []

    def market_snapshot(
        self, code: str, *, fetched_at: datetime | None = None
    ) -> USQuoteObservation:
        assert fetched_at is not None
        self.calls.append((code, fetched_at))
        return self.factory(code, fetched_at)


def _fresh_quote(code: str, fetched_at: datetime) -> USQuoteObservation:
    return USQuoteObservation(
        code=code,
        fetched_at=fetched_at,
        source_at=fetched_at - timedelta(seconds=20),
        market_status="TRADING",
        open=100.0,
        last=100.25,
        bid=100.2,
        ask=100.3,
        raw={"Open": 100.0, "Now": 100.25},
    )


def _raw_opens(value: float = 100.0) -> dict[str, float]:
    return {item.symbol: value for item in TDX_QUALIFICATION_SAMPLE}


class TDXShadowQualificationCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "tdx-shadow.db"
        self.calendar = _weekdays(date(2026, 8, 3), 25)
        self.window = self.calendar[:20]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def collector(
        self,
        *,
        client: _QuoteClient | None = None,
        worker_id: str = "worker-a",
        calendar=None,
        window=None,
        release_id: str = RELEASE_ID,
        manifest_sha256: str = MANIFEST_SHA256,
    ) -> TDXShadowQualificationCollector:
        return TDXShadowQualificationCollector(
            TDXShadowConfig(
                database_path=self.database_path,
                release_id=release_id,
                manifest_sha256=manifest_sha256,
                worker_id=worker_id,
            ),
            frozen_xnys_sessions=calendar or self.calendar,
            qualification_sessions=window or self.window,
            quote_client=client or _QuoteClient(),
        )

    def test_slot_collection_is_fixed_sample_read_only_and_idempotent(self) -> None:
        client = _QuoteClient()
        collector = self.collector(client=client)
        now = datetime.combine(self.window[0], datetime.min.time(), NY).replace(
            hour=9, minute=31
        )

        first = collector.tick(now=now)
        second = collector.tick(now=now)

        self.assertEqual(31, len(client.calls))
        self.assertEqual("READ_ONLY_SHADOW", first["mode"])
        self.assertFalse(first["broker_writes_enabled"])
        self.assertEqual(RELEASE_ID, first["bindings"]["release_id"])
        self.assertEqual(MANIFEST_SHA256, first["bindings"]["manifest_sha256"])
        self.assertEqual("COLLECTED_SLOT_1", second["last_action"])
        first_session = second["sessions"][0]
        self.assertEqual(31, first_session["recorded_slots"])
        self.assertEqual(31, first_session["fresh_slots"])
        self.assertEqual(31 * 390, first_session["expected_slots"])

    def test_raw_open_reconciliation_produces_existing_evaluator_evidence(self) -> None:
        collector = self.collector()
        session = self.window[0]
        now = datetime.combine(session, datetime.min.time(), NY).replace(
            hour=9, minute=31
        )
        collector.tick(now=now)

        status = collector.reconcile_raw_opens(
            session,
            _raw_opens(),
            observed_at=now.replace(hour=16, minute=5),
            source_sha256=SOURCE_HASH,
        )
        replay = collector.reconcile_raw_opens(
            session,
            _raw_opens(),
            observed_at=now.replace(hour=16, minute=5),
            source_sha256=SOURCE_HASH,
        )
        evidence = collector.build_evidence()

        self.assertEqual(31, len(evidence))
        self.assertTrue(all(item.session == session for item in evidence))
        self.assertTrue(all(item.captured_poll_slots == 1 for item in evidence))
        self.assertTrue(all(item.fresh_poll_slots == 1 for item in evidence))
        self.assertTrue(
            all(item.maximum_source_latency_seconds == 20.0 for item in evidence)
        )
        self.assertTrue(all(item.snapshot_open == 100.0 for item in evidence))
        self.assertTrue(all(item.final_raw_open == 100.0 for item in evidence))
        self.assertEqual("PAPER_BLOCKED", status["decision"]["status"])
        self.assertIn("twenty_consecutive_xnys_sessions", status["decision"]["failures"])
        self.assertEqual("RAW_OPENS_ALREADY_RECONCILED", replay["last_action"])
        self.assertEqual(64, len(status["evidence_sha256"]))

    def test_raw_open_set_is_atomic_exact_and_immutable(self) -> None:
        collector = self.collector()
        session = self.window[0]
        observed = datetime.combine(session, datetime.min.time(), NY).replace(
            hour=16, minute=5
        )
        incomplete = _raw_opens()
        incomplete.pop("SPY.US")

        with self.assertRaises(TDXShadowEvidenceError):
            collector.reconcile_raw_opens(
                session,
                incomplete,
                observed_at=observed,
                source_sha256=SOURCE_HASH,
            )
        self.assertFalse(collector.status()["sessions"][0]["raw_reconciled"])

        collector.reconcile_raw_opens(
            session,
            _raw_opens(),
            observed_at=observed,
            source_sha256=SOURCE_HASH,
        )
        with self.assertRaises(TDXShadowEvidenceError):
            collector.tick(now=observed.replace(hour=9, minute=31))
        changed = _raw_opens()
        changed["SPY.US"] = 101.0
        with self.assertRaises(TDXShadowEvidenceError):
            collector.reconcile_raw_opens(
                session,
                changed,
                observed_at=observed,
                source_sha256=SOURCE_HASH,
            )

    def test_future_and_closed_market_observations_are_permanent_errors(self) -> None:
        def invalid(code: str, fetched_at: datetime) -> USQuoteObservation:
            quote = _fresh_quote(code, fetched_at)
            return USQuoteObservation(
                code=quote.code,
                fetched_at=quote.fetched_at,
                source_at=fetched_at + timedelta(seconds=1),
                market_status="CLOSED",
                open=quote.open,
                last=quote.last,
                bid=quote.bid,
                ask=quote.ask,
                raw=quote.raw,
            )

        collector = self.collector(client=_QuoteClient(invalid))
        session = self.window[0]
        now = datetime.combine(session, datetime.min.time(), NY).replace(
            hour=9, minute=31
        )
        collector.tick(now=now)
        collector.reconcile_raw_opens(
            session,
            _raw_opens(),
            observed_at=now.replace(hour=16, minute=5),
            source_sha256=SOURCE_HASH,
        )

        evidence = collector.build_evidence()
        self.assertTrue(all(item.fresh_poll_slots == 0 for item in evidence))
        self.assertTrue(all(item.future_timestamp_errors == 1 for item in evidence))
        self.assertTrue(all(item.market_state_errors == 1 for item in evidence))
        decision = collector.evaluate()
        self.assertIn("timestamp_and_market_state_integrity", decision.failures)

    def test_calendar_window_and_database_hashes_are_fail_closed(self) -> None:
        with self.assertRaises(TDXShadowBindingError):
            self.collector(window=self.window[:19])
        nonconsecutive = self.window[:10] + self.window[11:20] + (self.calendar[20],)
        with self.assertRaises(TDXShadowBindingError):
            self.collector(window=nonconsecutive)

        self.collector()
        extended_calendar = self.calendar + (_weekdays(self.calendar[-1] + timedelta(days=1), 1)[0],)
        with self.assertRaises(TDXShadowBindingError):
            self.collector(calendar=extended_calendar)

    def test_database_is_permanently_bound_to_active_pit_release_and_manifest(self) -> None:
        original = self.collector()

        with self.assertRaises(TDXShadowBindingError):
            self.collector(release_id="d" * 64)
        with self.assertRaises(TDXShadowBindingError):
            self.collector(manifest_sha256="e" * 64)

        other = TDXShadowQualificationCollector(
            TDXShadowConfig(
                database_path=Path(self.tempdir.name) / "other-release.db",
                release_id="d" * 64,
                manifest_sha256=MANIFEST_SHA256,
            ),
            frozen_xnys_sessions=self.calendar,
            qualification_sessions=self.window,
            quote_client=_QuoteClient(),
        )
        self.assertNotEqual(
            original.status()["evidence_sha256"],
            other.status()["evidence_sha256"],
        )

        with self.assertRaises(ValueError):
            TDXShadowConfig(
                database_path=Path(self.tempdir.name) / "invalid-release.db",
                release_id="not-a-release",
                manifest_sha256=MANIFEST_SHA256,
            )
        with self.assertRaises(ValueError):
            TDXShadowConfig(
                database_path=Path(self.tempdir.name) / "invalid-manifest.db",
                release_id=RELEASE_ID,
                manifest_sha256="F" * 64,
            )

    def test_window_uses_the_startup_frozen_schedule_not_release_history(self) -> None:
        # The collector receives a freshly frozen forward XNYS schedule.  It
        # does not require those dates to exist in a historical PIT release.
        startup_frozen_schedule = _weekdays(date(2027, 1, 4), 25)
        startup_window = startup_frozen_schedule[:20]

        collector = self.collector(
            calendar=startup_frozen_schedule,
            window=startup_window,
        )

        self.assertEqual(
            [item.isoformat() for item in startup_window],
            collector.status()["bindings"]["qualification_sessions"],
        )

    def test_new_process_reopens_original_window_and_collects_next_day(self) -> None:
        first_client = _QuoteClient()
        original = self.collector(client=first_client, worker_id="worker-day-one")
        day_one = datetime.combine(
            self.window[0], datetime.min.time(), NY
        ).replace(hour=9, minute=31)
        original.tick(now=day_one)

        second_client = _QuoteClient()
        resumed = TDXShadowQualificationCollector.open_existing(
            TDXShadowConfig(
                database_path=self.database_path,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
                worker_id="worker-day-two",
            ),
            quote_client=second_client,
        )
        day_two = datetime.combine(
            self.window[1], datetime.min.time(), NY
        ).replace(hour=9, minute=31)
        status = resumed.tick(now=day_two)

        self.assertEqual(self.calendar, resumed.calendar)
        self.assertEqual(self.window, resumed.window)
        self.assertEqual(31, len(second_client.calls))
        self.assertEqual(31, status["sessions"][1]["fresh_slots"])
        self.assertEqual(
            original.status()["bindings"]["calendar_hash"],
            status["bindings"]["calendar_hash"],
        )

    def test_open_existing_rejects_release_policy_or_calendar_tampering(self) -> None:
        self.collector()

        with self.assertRaises(TDXShadowBindingError):
            TDXShadowQualificationCollector.open_existing(
                TDXShadowConfig(
                    database_path=self.database_path,
                    release_id="d" * 64,
                    manifest_sha256=MANIFEST_SHA256,
                )
            )
        with self.assertRaises(TDXShadowBindingError):
            TDXShadowQualificationCollector.open_existing(
                TDXShadowConfig(
                    database_path=self.database_path,
                    release_id=RELEASE_ID,
                    manifest_sha256=MANIFEST_SHA256,
                    maximum_source_latency_seconds=80,
                )
            )
        with self.assertRaises(TDXShadowBindingError):
            TDXShadowQualificationCollector.open_existing(
                TDXShadowConfig(
                    database_path=self.database_path,
                    release_id=RELEASE_ID,
                    manifest_sha256=MANIFEST_SHA256,
                    lease_seconds=600,
                )
            )

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE tdx_shadow_metadata SET calendar_json=? WHERE singleton=1",
                ('["2026-08-03"]',),
            )
            connection.commit()
        with self.assertRaises(TDXShadowBindingError):
            TDXShadowQualificationCollector.open_existing(
                TDXShadowConfig(
                    database_path=self.database_path,
                    release_id=RELEASE_ID,
                    manifest_sha256=MANIFEST_SHA256,
                )
            )

    def test_active_sqlite_lease_rejects_second_worker(self) -> None:
        first_client = _QuoteClient()
        first = self.collector(client=first_client, worker_id="worker-a")
        second = self.collector(client=_QuoteClient(), worker_id="worker-b")
        now = datetime.combine(self.window[0], datetime.min.time(), NY).replace(
            hour=9, minute=31
        )
        first.tick(now=now)

        with self.assertRaises(TDXShadowLeaseError):
            second.tick(now=now)

    def test_outside_window_or_hours_never_queries_tdx(self) -> None:
        client = _QuoteClient()
        collector = self.collector(client=client)
        before_open = datetime.combine(
            self.window[0], datetime.min.time(), NY
        ).replace(hour=9, minute=29)

        status = collector.tick(now=before_open)
        self.assertEqual("OUTSIDE_REGULAR_SESSION", status["last_action"])
        self.assertEqual([], client.calls)
        outside = datetime.combine(
            self.calendar[-1], datetime.min.time(), NY
        ).replace(hour=9, minute=31)
        with self.assertRaises(TDXShadowScheduleError):
            collector.tick(now=outside)


if __name__ == "__main__":
    unittest.main()
