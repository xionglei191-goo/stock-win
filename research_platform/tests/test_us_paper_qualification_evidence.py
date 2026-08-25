from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from research_platform.us_paper import USMomentumPaperService, USPaperConfig
from research_platform.us_paper_qualification import (
    USPaperQualificationEvidenceBuilder,
)
from research_platform.us_paper_runtime import (
    DAILY_SOURCE_FREQUENCY,
    DAILY_SOURCE_SCHEMA,
    FrozenXNYSSchedule,
    USPaperRuntime,
    USPaperRuntimeConfig,
    canonical_daily_source_sha256,
)
from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file
from research_platform.us_pit.models import (
    LicenseClass,
    QualityReport,
    ReleaseManifest,
    ReleaseStatus,
    SourceDependency,
    SourceRole,
    UNIVERSE_ID,
)
from research_platform.us_pit.store import JSON_MEDIA_TYPE, USPITStore
from research_platform.us_tdx import USQuoteObservation


NY = ZoneInfo("America/New_York")
HASH = "a" * 64
SECURITY_ID = "us_aapl_fixture"


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)


def _tdx_daily_bar(
    code: str,
    day: date,
    *,
    previous_day: date,
    opening: float,
    high: float,
    low: float,
    closing: float,
) -> dict[str, object]:
    adjustment = "front" if code == "BILTR.US" else "none"
    source_rows = (
        [
            {"session_date": previous_day.isoformat(), "Close": opening},
            {"session_date": day.isoformat(), "Close": closing},
        ]
        if code == "BILTR.US"
        else [
            {
                "session_date": day.isoformat(),
                "Open": opening,
                "High": high,
                "Low": low,
                "Close": closing,
            }
        ]
    )
    return {
        "code": code,
        "session_date": day.isoformat(),
        "observed_at": at(day, 17),
        "open": opening,
        "high": high,
        "low": low,
        "close": closing,
        "source_schema": DAILY_SOURCE_SCHEMA,
        "source": "TDX",
        "source_code": "BIL.US" if code == "BILTR.US" else code,
        "frequency": DAILY_SOURCE_FREQUENCY,
        "adjustment": adjustment,
        "source_rows": source_rows,
        "source_sha256": canonical_daily_source_sha256(
            source="TDX",
            source_code="BIL.US" if code == "BILTR.US" else code,
            adjustment=adjustment,
            source_rows=source_rows,
        ),
    }


class _ProgramRecorder:
    def __init__(self) -> None:
        self.call = None

    def register_paper(self, decision, evidence_hash, **kwargs):
        self.call = (decision, evidence_hash, kwargs)
        return {"state": decision.status}


class _QuoteClient:
    def market_snapshot(
        self, code: str, *, fetched_at: datetime | None = None
    ) -> USQuoteObservation:
        assert fetched_at is not None
        return USQuoteObservation(
            code=code,
            fetched_at=fetched_at,
            source_at=fetched_at - timedelta(seconds=10),
            market_status="TRADING",
            open=100.0,
            last=100.0,
            bid=99.99,
            ask=100.01,
            raw={"Open": 100.0, "Now": 100.0},
        )


class USPaperQualificationEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paper_path = root / "paper.sqlite"
        self.runtime_path = root / "runtime.sqlite"
        self.pit_root = root / "us_pit"
        store = USPITStore(self.pit_root)
        report = QualityReport(
            policy_version="us-pit-quality-v1",
            status=ReleaseStatus.DATA_READY,
            includes_delisted=True,
            issues=(),
            metrics={"decision_months": 60},
        )
        report_ref = store.put_bytes(
            canonical_json_bytes(report.to_dict()), media_type=JSON_MEDIA_TYPE
        )
        source_ref = store.put_bytes(b"qualification corporate action evidence")
        self.action_evidence_sha256 = source_ref.sha256
        action_ref = store.put_dataframe(
            pd.DataFrame(
                [
                    {
                        "action_id": "qualification-split",
                        "security_id": SECURITY_ID,
                        "action_type": "SPLIT",
                        "announced_at": "2026-01-02T08:00:00-05:00",
                        "effective_at": "2026-01-05",
                        "pay_date": None,
                        "terms_verified": True,
                        "source_id": "official-action",
                        "evidence_sha256": self.action_evidence_sha256,
                    }
                ]
            )
        )
        manifest = ReleaseManifest(
            universe_id=UNIVERSE_ID,
            created_at="2026-01-01T00:00:00+00:00",
            status=ReleaseStatus.DATA_READY,
            artifacts={
                "quality_report": store.descriptor("quality_report", report_ref),
                "corporate_actions": store.descriptor(
                    "corporate_actions", action_ref
                ),
            },
            sources=(
                SourceDependency(
                    source_id="official-action",
                    source_version="1",
                    role=SourceRole.SIGNAL_INPUT,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    object_sha256=self.action_evidence_sha256,
                    observed_at="2026-01-02T08:00:00-05:00",
                    url="https://official.example/corporate-action",
                    dataset="corporate_actions",
                    as_of_date="2026-01-05",
                    published_at="2026-01-02T08:00:00-05:00",
                ),
            ),
        )
        release = store.publish_release(
            manifest,
            {"quality_report": report_ref, "corporate_actions": action_ref},
        )
        self.release_id = release.release_id
        self.manifest_sha256 = sha256_file(release.path / "manifest.json")
        self.days = (date(2026, 1, 2), date(2026, 1, 5))
        self.schedule = FrozenXNYSSchedule(self.days)
        self.paper = USMomentumPaperService(
            USPaperConfig(database_path=self.paper_path)
        )
        self.runtime = USPaperRuntime(
            USPaperRuntimeConfig(
                state_database_path=self.runtime_path,
                release_id=self.release_id,
                manifest_sha256=self.manifest_sha256,
                worker_id="qualification-test",
            ),
            schedule=self.schedule,
            paper=self.paper,
            preflight=lambda: {"ready": True, "status": "READY"},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _close(self, day: date, bil_close: float | None) -> None:
        bars = []
        if bil_close is not None:
            position = self.days.index(day)
            previous_day = (
                self.days[position - 1]
                if position > 0
                else day - timedelta(days=1)
            )
            bars.extend(
                [
                    _tdx_daily_bar(
                        "BIL.US",
                        day,
                        previous_day=previous_day,
                        opening=bil_close,
                        high=bil_close,
                        low=bil_close,
                        closing=bil_close,
                    ),
                    _tdx_daily_bar(
                        "BILTR.US",
                        day,
                        previous_day=previous_day,
                        opening=max(0.01, bil_close - 0.01),
                        high=bil_close,
                        low=max(0.01, bil_close - 0.01),
                        closing=bil_close,
                    ),
                ]
            )
        self.runtime.tick(now=at(day, 17), daily_bars=bars)

    def _builder(self) -> USPaperQualificationEvidenceBuilder:
        return USPaperQualificationEvidenceBuilder(
            paper_database_path=self.paper_path,
            runtime_database_path=self.runtime_path,
            frozen_xnys_sessions=self.days,
            us_pit_root=self.pit_root,
        )

    def _publish_alternate_action_release(
        self,
    ) -> tuple[str, str, str]:
        store = USPITStore(self.pit_root)
        report = QualityReport(
            policy_version="us-pit-quality-v1",
            status=ReleaseStatus.DATA_READY,
            includes_delisted=True,
            issues=(),
            metrics={"decision_months": 60},
        )
        report_ref = store.put_bytes(
            canonical_json_bytes(report.to_dict()), media_type=JSON_MEDIA_TYPE
        )
        source_ref = store.put_bytes(b"alternate release corporate action evidence")
        action_ref = store.put_dataframe(
            pd.DataFrame(
                [
                    {
                        "action_id": "qualification-split",
                        "security_id": SECURITY_ID,
                        "action_type": "SPLIT",
                        "announced_at": "2026-01-02T08:00:00-05:00",
                        "effective_at": "2026-01-05",
                        "pay_date": None,
                        "terms_verified": True,
                        "source_id": "alternate-official-action",
                        "evidence_sha256": source_ref.sha256,
                    }
                ]
            )
        )
        manifest = ReleaseManifest(
            universe_id=UNIVERSE_ID,
            created_at="2026-01-02T00:00:00+00:00",
            status=ReleaseStatus.DATA_READY,
            artifacts={
                "quality_report": store.descriptor("quality_report", report_ref),
                "corporate_actions": store.descriptor(
                    "corporate_actions", action_ref
                ),
            },
            sources=(
                SourceDependency(
                    source_id="alternate-official-action",
                    source_version="1",
                    role=SourceRole.SIGNAL_INPUT,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    object_sha256=source_ref.sha256,
                    observed_at="2026-01-02T08:00:00-05:00",
                    url="https://official.example/alternate-corporate-action",
                    dataset="corporate_actions",
                    as_of_date="2026-01-05",
                    published_at="2026-01-02T08:00:00-05:00",
                ),
            ),
        )
        release = store.publish_release(
            manifest,
            {"quality_report": report_ref, "corporate_actions": action_ref},
        )
        return (
            release.release_id,
            sha256_file(release.path / "manifest.json"),
            source_ref.sha256,
        )

    def _apply_action(
        self,
        *,
        action_id: str,
        evidence_sha256: str,
        source_id: str = "official-action",
    ) -> None:
        self.paper.apply_corporate_actions(
            self.days[1],
            [
                {
                    "action_id": action_id,
                    "action_type": "SPLIT",
                    "security_id": SECURITY_ID,
                    "effective_date": self.days[1].isoformat(),
                    "verified": True,
                    "verified_at": at(self.days[1], 8),
                    "evidence_sha256": evidence_sha256,
                    "pit_release_id": self.release_id,
                    "manifest_sha256": self.manifest_sha256,
                    "terms": {"ratio": 2.0, "source_id": source_id},
                }
            ],
            now=at(self.days[1], 8, 30),
            pit_release_id=self.release_id,
            manifest_sha256=self.manifest_sha256,
        )

    def _daily_bars(
        self,
        day: date,
        values: Mapping[str, tuple[float, float, float, float]],
    ) -> list[dict[str, object]]:
        position = self.days.index(day)
        previous_day = (
            self.days[position - 1]
            if position > 0
            else day - timedelta(days=1)
        )
        return [
            _tdx_daily_bar(
                code,
                day,
                previous_day=previous_day,
                opening=ohlc[0],
                high=ohlc[1],
                low=ohlc[2],
                closing=ohlc[3],
            )
            for code, ohlc in values.items()
        ]

    def test_initialized_but_not_started_ledgers_remain_collecting(self) -> None:
        evidence = self._builder().build()

        self.assertEqual("PAPER_COLLECTING", evidence.decision.status)
        self.assertFalse(evidence.decision.qualified)
        self.assertEqual((), evidence.integrity_failures)

    def test_ready_release_lineage_is_hash_verified_from_read_only_catalog(self) -> None:
        failures = []
        lineage = self._builder()._verify_release_lineage(
            {(self.release_id, self.manifest_sha256)}, failures
        )

        self.assertEqual({self.release_id: self.manifest_sha256}, lineage)
        self.assertEqual([], failures)

    def test_release_lineage_without_pit_store_fails_closed(self) -> None:
        builder = USPaperQualificationEvidenceBuilder(
            paper_database_path=self.paper_path,
            runtime_database_path=self.runtime_path,
            frozen_xnys_sessions=self.days,
        )
        failures = []

        self.assertEqual(
            {},
            builder._verify_release_lineage(
                {(self.release_id, self.manifest_sha256)}, failures
            ),
        )
        self.assertEqual(["PIT_RELEASE_LINEAGE_STORE_REQUIRED"], failures)

    def test_corporate_action_evidence_matches_release_source_and_artifact(self) -> None:
        self._apply_action(
            action_id="qualification-split",
            evidence_sha256=self.action_evidence_sha256,
        )
        builder = self._builder()
        snapshot = builder._snapshot()
        failures: list[str] = []

        builder._verify_release_lineage(
            {(self.release_id, self.manifest_sha256)}, failures
        )
        builder._validate_corporate_actions(snapshot.paper, self.days, failures)

        self.assertEqual([], failures)

    def test_sha_shaped_action_evidence_absent_from_release_is_rejected(self) -> None:
        self._apply_action(
            action_id="missing-action",
            evidence_sha256="f" * 64,
        )
        builder = self._builder()
        snapshot = builder._snapshot()
        failures: list[str] = []

        builder._verify_release_lineage(
            {(self.release_id, self.manifest_sha256)}, failures
        )
        builder._validate_corporate_actions(snapshot.paper, self.days, failures)

        self.assertIn(
            "CORPORATE_ACTION_EVIDENCE_NOT_IN_RELEASE:missing-action", failures
        )
        self.assertIn(
            "CORPORATE_ACTION_ARTIFACT_MISMATCH:missing-action", failures
        )

    def test_action_evidence_from_a_different_ready_release_is_rejected(self) -> None:
        alternate_release_id, alternate_manifest, alternate_digest = (
            self._publish_alternate_action_release()
        )
        self._apply_action(
            action_id="qualification-split",
            evidence_sha256=alternate_digest,
            source_id="alternate-official-action",
        )
        builder = self._builder()
        snapshot = builder._snapshot()
        failures: list[str] = []

        builder._verify_release_lineage(
            {
                (self.release_id, self.manifest_sha256),
                (alternate_release_id, alternate_manifest),
            },
            failures,
        )
        builder._validate_corporate_actions(snapshot.paper, self.days, failures)

        self.assertIn(
            "CORPORATE_ACTION_EVIDENCE_NOT_IN_RELEASE:qualification-split",
            failures,
        )
        self.assertIn(
            "CORPORATE_ACTION_ARTIFACT_MISMATCH:qualification-split", failures
        )

    def test_sound_persistent_prefix_stays_collecting_and_is_replayable(self) -> None:
        self._close(self.days[0], 100.0)
        self._close(self.days[1], 101.0)

        first = self._builder().build()
        second = self._builder().build()

        self.assertFalse(first.decision.qualified)
        self.assertEqual("PAPER_COLLECTING", first.decision.status)
        self.assertEqual(2, first.decision.metrics["unique_sessions"])
        self.assertTrue(first.decision.gates["persistent_integrity"])
        self.assertTrue(first.decision.gates["replayable"])
        self.assertEqual((), first.integrity_failures)
        self.assertEqual(first.evidence_sha256, second.evidence_sha256)
        self.assertEqual(
            [item.output_sha256 for item in first.session_evidence],
            [item.replay_output_sha256 for item in first.session_evidence],
        )

    def test_missing_bil_raw_fails_closed(self) -> None:
        self._close(self.days[0], None)

        evidence = self._builder().build()

        self.assertEqual("PAPER_BLOCKED", evidence.decision.status)
        self.assertFalse(evidence.decision.qualified)
        self.assertIn(
            "MISSING_BIL_RAW:2026-01-02",
            evidence.integrity_failures,
        )

    def test_observation_hash_tamper_fails_closed(self) -> None:
        self._close(self.days[0], 100.0)
        with closing(sqlite3.connect(self.paper_path)) as connection:
            connection.execute(
                "UPDATE us_paper_observations SET content_hash=?",
                ("0" * 64,),
            )
            connection.commit()

        evidence = self._builder().build()

        self.assertEqual("PAPER_BLOCKED", evidence.decision.status)
        self.assertTrue(
            any(
                item.startswith("OBSERVATION_HASH_MISMATCH:BIL.US")
                for item in evidence.integrity_failures
            )
        )

    def test_biltr_missing_provenance_fails_closed_even_with_rehashed_payload(self) -> None:
        self._close(self.days[0], 100.0)
        with closing(sqlite3.connect(self.paper_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM us_paper_observations WHERE code='BILTR.US'"
            ).fetchone()
            payload = json.loads(row["payload_json"])
            for field in ("source", "adjustment", "source_rows", "source_sha256"):
                payload.pop(field)
            connection.execute(
                """UPDATE us_paper_observations
                SET payload_json=?, content_hash=? WHERE observation_id=?""",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    _canonical_hash(payload),
                    row["observation_id"],
                ),
            )
            connection.commit()

        evidence = self._builder().build()

        self.assertEqual("PAPER_BLOCKED", evidence.decision.status)
        self.assertIn(
            "BILTR_PROVENANCE_MISSING:2026-01-02",
            evidence.integrity_failures,
        )
        self.assertEqual((), evidence.session_evidence)
        self.assertEqual(0, evidence.decision.metrics["unique_sessions"])

    def test_biltr_source_row_tamper_fails_closed_after_observation_rehash(self) -> None:
        self._close(self.days[0], 100.0)
        with closing(sqlite3.connect(self.paper_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM us_paper_observations WHERE code='BILTR.US'"
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["source_rows"][0]["Close"] += 1.0
            # Rehash the outer observation while leaving the independently
            # recorded source digest untouched.
            connection.execute(
                """UPDATE us_paper_observations
                SET payload_json=?, content_hash=? WHERE observation_id=?""",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    _canonical_hash(payload),
                    row["observation_id"],
                ),
            )
            connection.commit()

        evidence = self._builder().build()

        self.assertEqual("PAPER_BLOCKED", evidence.decision.status)
        self.assertIn(
            "BILTR_SOURCE_HASH_MISMATCH:2026-01-02",
            evidence.integrity_failures,
        )
        self.assertIn(
            "BILTR_SOURCE_CLOSE_MISMATCH:2026-01-02",
            evidence.integrity_failures,
        )
        self.assertEqual((), evidence.session_evidence)

    def test_biltr_cannot_claim_a_different_front_adjusted_source(self) -> None:
        self._close(self.days[0], 100.0)
        with closing(sqlite3.connect(self.paper_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM us_paper_observations WHERE code='BILTR.US'"
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["source_code"] = "SPY.US"
            payload["source_sha256"] = canonical_daily_source_sha256(
                source="TDX",
                source_code="SPY.US",
                adjustment="front",
                source_rows=payload["source_rows"],
            )
            connection.execute(
                """UPDATE us_paper_observations
                SET payload_json=?, content_hash=? WHERE observation_id=?""",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    _canonical_hash(payload),
                    row["observation_id"],
                ),
            )
            connection.commit()

        evidence = self._builder().build()

        self.assertEqual("PAPER_BLOCKED", evidence.decision.status)
        self.assertIn(
            "BILTR_PROVENANCE_INVALID:2026-01-02",
            evidence.integrity_failures,
        )
        self.assertIn(
            "BILTR_PROVENANCE_REPLAY_BLOCKED:2026-01-02",
            evidence.integrity_failures,
        )
        self.assertEqual((), evidence.session_evidence)

    def test_biltr_previous_row_must_be_previous_frozen_xnys_session(self) -> None:
        self._close(self.days[0], 100.0)
        self._close(self.days[1], 101.0)
        with closing(sqlite3.connect(self.paper_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT * FROM us_paper_observations
                WHERE code='BILTR.US' AND session_date=?""",
                (self.days[1].isoformat(),),
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["source_rows"][0]["session_date"] = "2025-12-31"
            payload["source_sha256"] = canonical_daily_source_sha256(
                source="TDX",
                source_code="BIL.US",
                adjustment="front",
                source_rows=payload["source_rows"],
            )
            connection.execute(
                """UPDATE us_paper_observations
                SET payload_json=?, content_hash=? WHERE observation_id=?""",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    _canonical_hash(payload),
                    row["observation_id"],
                ),
            )
            connection.commit()

        evidence = self._builder().build()

        self.assertEqual("PAPER_BLOCKED", evidence.decision.status)
        self.assertIn(
            "BILTR_SOURCE_SESSION_MISMATCH:2026-01-05",
            evidence.integrity_failures,
        )
        self.assertEqual([self.days[0]], [item.session for item in evidence.session_evidence])

    def test_historical_kill_event_cannot_be_acknowledged_into_qualification(self) -> None:
        self._close(self.days[0], 100.0)
        self.runtime.kill(reason="qualification test", now=at(self.days[0], 18))

        evidence = self._builder().build()

        self.assertEqual("PAPER_BLOCKED", evidence.decision.status)
        self.assertIn("KILL_SWITCH_PRESENT", evidence.integrity_failures)
        self.assertTrue(
            any("KILL" in item for item in evidence.integrity_failures)
        )

    def test_held_position_without_full_minute_quote_ledger_fails_closed(self) -> None:
        decision = at(date(2026, 1, 1), 16, 15)
        self.paper.create_period(
            [
                {
                    "signal_id": "sig-aapl",
                    "security_id": "us_isin_us0378331005",
                    "code": "AAPL.US",
                    "pit_release_id": self.release_id,
                    "manifest_sha256": self.manifest_sha256,
                    "side": "BUY",
                    "target_weight": 0.10,
                    "generated_at": decision,
                    "available_at": at(self.days[0], 9, 30),
                    "valid_until": at(self.days[0], 9, 35),
                    "reason_codes": ["MONTHLY_MOMENTUM"],
                    "evidence": {
                        "stop_ratio": 0.08,
                        "security_id": "us_isin_us0378331005",
                        "pit_release_id": self.release_id,
                        "manifest_sha256": self.manifest_sha256,
                    },
                }
            ],
            now=decision + timedelta(minutes=1),
        )
        runtime = USPaperRuntime.open_existing(
            USPaperRuntimeConfig(
                state_database_path=self.runtime_path,
                release_id=self.release_id,
                manifest_sha256=self.manifest_sha256,
                worker_id="qualification-test",
            ),
            self.paper,
            lambda: {"ready": True, "status": "READY"},
            quote_client=_QuoteClient(),
        )
        runtime.tick(now=at(self.days[0], 9, 15))
        runtime.tick(now=at(self.days[0], 9, 31))
        runtime.tick(
            now=at(self.days[0], 17),
            daily_bars=self._daily_bars(
                self.days[0],
                {
                    code: (100.0, 101.0, 99.0, 100.0)
                    for code in ("AAPL.US", "BIL.US", "BILTR.US")
                },
            ),
        )

        evidence = self._builder().build()

        self.assertEqual("PAPER_BLOCKED", evidence.decision.status)
        self.assertIn(
            "INCOMPLETE_INTRADAY_QUOTE_COVERAGE:AAPL.US:2026-01-02",
            evidence.integrity_failures,
        )

    def test_registration_uses_only_derived_decision_and_hash(self) -> None:
        self._close(self.days[0], 100.0)
        evidence = self._builder().build()
        recorder = _ProgramRecorder()

        result = evidence.register(recorder)

        self.assertEqual({"state": "PAPER_COLLECTING"}, result)
        self.assertIs(recorder.call[0], evidence.decision)
        self.assertEqual(evidence.evidence_sha256, recorder.call[1])
        self.assertEqual(
            {
                "release_id": self.release_id,
                "manifest_sha256": self.manifest_sha256,
            },
            recorder.call[2],
        )

    def test_split_and_cash_ledger_are_included_in_account_replay(self) -> None:
        decision = at(date(2026, 1, 1), 16, 15)
        self.paper.create_period(
            [
                {
                    "signal_id": "sig-split-aapl",
                    "code": "AAPL.US",
                    "side": "BUY",
                    "target_weight": 0.10,
                    "generated_at": decision,
                    "available_at": at(self.days[0], 9, 20),
                    "valid_until": at(self.days[0], 9, 35),
                    "reason_codes": ["MONTHLY_MOMENTUM"],
                    "evidence": {
                        "stop_ratio": 0.08,
                        "security_id": SECURITY_ID,
                        "pit_release_id": self.release_id,
                        "manifest_sha256": self.manifest_sha256,
                    },
                }
            ],
            now=decision,
        )
        self.paper.tick(
            self.days[0],
            now=at(self.days[0], 9, 30),
            observations=[
                {
                    "code": "AAPL.US",
                    "session_date": self.days[0].isoformat(),
                    "kind": "OPEN",
                    "event_at": at(self.days[0], 9, 30),
                    "available_at": at(self.days[0], 9, 30),
                    "open": 100.0,
                }
            ],
        )
        self.runtime.tick(
            now=at(self.days[0], 17),
            daily_bars=self._daily_bars(
                self.days[0],
                {
                    code: (100.0, 100.0, 100.0, 100.0)
                    for code in ("AAPL.US", "BIL.US", "BILTR.US")
                },
            ),
        )
        self.paper.apply_corporate_actions(
            self.days[1],
            [
                {
                    "action_id": "qualification-split",
                    "action_type": "SPLIT",
                    "security_id": SECURITY_ID,
                    "effective_date": self.days[1].isoformat(),
                    "verified": True,
                    "verified_at": at(self.days[1], 8),
                    "evidence_sha256": self.action_evidence_sha256,
                    "pit_release_id": self.release_id,
                    "manifest_sha256": self.manifest_sha256,
                    "terms": {"ratio": 2.0, "source_id": "official-action"},
                }
            ],
            now=at(self.days[1], 8, 30),
            pit_release_id=self.release_id,
            manifest_sha256=self.manifest_sha256,
        )
        self.runtime.tick(
            now=at(self.days[1], 17),
            daily_bars=self._daily_bars(
                self.days[1],
                {
                    "AAPL.US": (50.0, 50.0, 50.0, 50.0),
                    "BIL.US": (100.0, 100.0, 100.0, 100.0),
                    "BILTR.US": (100.0, 100.0, 100.0, 100.0),
                },
            ),
        )
        builder = self._builder()
        snapshot = builder._snapshot()
        replay = builder._replay(snapshot, self.days)
        failures: list[str] = []
        builder._verify_release_lineage(
            {(self.release_id, self.manifest_sha256)}, failures
        )
        builder._validate_corporate_actions(snapshot.paper, self.days, failures)
        builder._validate_final_account(snapshot, self.days, replay, failures)
        status = self.paper.status()

        self.assertEqual([], failures)
        self.assertEqual(
            int(status["positions"][0]["quantity"]),
            int(replay.final_positions[SECURITY_ID]["quantity"]),
        )
        self.assertAlmostEqual(status["account"]["cash"], replay.final_cash)

    def test_causal_stop_event_is_accepted_but_unbound_event_is_blocked(self) -> None:
        decision = at(date(2026, 1, 1), 16, 15)
        self.paper.create_period(
            [
                {
                    "signal_id": "sig-stop-aapl",
                    "code": "AAPL.US",
                    "side": "BUY",
                    "target_weight": 0.10,
                    "generated_at": decision,
                    "available_at": at(self.days[0], 9, 20),
                    "valid_until": at(self.days[0], 9, 35),
                    "reason_codes": ["MONTHLY_MOMENTUM"],
                    "evidence": {
                        "stop_ratio": 0.08,
                        "security_id": SECURITY_ID,
                        "pit_release_id": self.release_id,
                        "manifest_sha256": self.manifest_sha256,
                    },
                }
            ],
            now=decision,
        )
        self.paper.tick(
            self.days[0],
            now=at(self.days[0], 9, 30),
            observations=[
                {
                    "code": "AAPL.US",
                    "session_date": self.days[0].isoformat(),
                    "kind": "OPEN",
                    "event_at": at(self.days[0], 9, 30),
                    "available_at": at(self.days[0], 9, 30),
                    "open": 100.0,
                }
            ],
        )
        result = self.paper.execute_intraday_stop(
            {
                "code": "AAPL.US",
                "security_id": SECURITY_ID,
                "pit_release_id": self.release_id,
                "manifest_sha256": self.manifest_sha256,
                "session_date": self.days[0].isoformat(),
                "kind": "INTRADAY",
                "event_at": at(self.days[0], 10),
                "available_at": at(self.days[0], 10),
                "close": 90.0,
            },
            now=at(self.days[0], 10),
        )
        fill = result["fill"]
        paper = {
            "us_paper_fills": tuple(self.paper.status()["fills"]),
            "us_paper_events": tuple(self.paper.status()["events"]),
            "us_paper_observations": tuple(
                self.paper.executor._store.rows(
                    "SELECT * FROM us_paper_observations"
                )
            ),
        }
        valid_runtime = {
            "event_type": "INTRADAY_STOP_BREACH",
            "severity": "HIGH",
            "occurred_at": at(self.days[0], 10).isoformat(),
            "details_json": json.dumps(
                {
                    "code": "AAPL.US",
                    "last": 90.0,
                    "bid": 90.0,
                    "sell_reference": 90.0,
                    "stop_price": 92.0,
                    "action": "SIMULATED_STOP_FILLED_FROM_FRESH_QUOTE",
                    "fill_id": fill["fill_id"],
                }
            ),
        }
        failures: list[str] = []
        USPaperQualificationEvidenceBuilder._validate_events(
            paper,
            {"us_paper_runtime_events": (valid_runtime,)},
            failures,
        )
        self.assertEqual([], failures)

        invalid_runtime = {
            **valid_runtime,
            "details_json": json.dumps(
                {
                    **json.loads(valid_runtime["details_json"]),
                    "fill_id": "missing-fill",
                }
            ),
        }
        failures = []
        USPaperQualificationEvidenceBuilder._validate_events(
            paper,
            {"us_paper_runtime_events": (invalid_runtime,)},
            failures,
        )
        self.assertIn("RUNTIME_RISK_EVENT:INTRADAY_STOP_BREACH", failures)


if __name__ == "__main__":
    unittest.main()
