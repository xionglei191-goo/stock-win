from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

import pandas as pd

from research_platform.us_promotion import PromotionDecision
from research_platform.us_program import (
    USMomentumProgram,
    USProgramEvidenceError,
    USProgramStateError,
)
from research_platform.us_qualification import (
    PaperQualificationDecision,
    TDXQuoteQualificationDecision,
)
from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file
from research_platform.us_pit.models import (
    QualityReport,
    ReleaseManifest,
    ReleaseStatus,
    UNIVERSE_ID,
)
from research_platform.us_pit.store import JSON_MEDIA_TYPE, USPITStore


RELEASE_ID = "a" * 64
MANIFEST_SHA256 = "b" * 64
HISTORICAL_SHA256 = "c" * 64
TDX_SHA256 = "d" * 64
PAPER_SHA256 = "e" * 64


def _release(*, status: str = "DATA_READY", quality_status: str | None = None):
    return {
        "release_id": RELEASE_ID,
        "manifest_sha256": MANIFEST_SHA256,
        "universe_id": "sp500_ivv_proxy_v1",
        "status": status,
        "quality_report": {
            "status": quality_status or status,
            "hard_failures": [],
            "metrics": {"quality_contract_revision": 3},
        },
    }


def _historical(qualified: bool = True):
    decision = PromotionDecision(
        qualified=qualified,
        status="BACKTEST_QUALIFIED" if qualified else "HISTORICAL_FAILED",
        gates={"oos": qualified},
        failures=() if qualified else ("oos",),
    )
    return {
        "freeze_sha256": "1" * 64,
        "run_sha256": {"base": "2" * 64, "stress": "3" * 64},
        "decision": decision,
    }


def _tdx(qualified: bool = True):
    return TDXQuoteQualificationDecision(
        qualified=qualified,
        status="TDX_QUALIFIED" if qualified else "PAPER_BLOCKED",
        gates={"twenty_sessions": qualified},
        failures=() if qualified else ("twenty_sessions",),
        metrics={"sessions": 20 if qualified else 19},
    )


def _paper(*, qualified: bool, blocked: bool = False):
    status = "PAPER_QUALIFIED" if qualified else (
        "PAPER_BLOCKED" if blocked else "PAPER_COLLECTING"
    )
    return PaperQualificationDecision(
        qualified=qualified,
        status=status,
        gates={"sessions": qualified, "replayable": True},
        failures=() if qualified else ("sessions",),
        metrics={"sessions": 252 if qualified else 100},
    )


def _pit_release(
    store: USPITStore,
    rows: list[tuple[str, str]],
    *,
    status: ReleaseStatus = ReleaseStatus.DATA_READY,
    mutation: tuple[str, str, object] | None = None,
):
    membership = pd.DataFrame(
        [
            {
                "universe_id": UNIVERSE_ID,
                "decision_date": decision_date,
                "security_id": security_id,
            }
            for decision_date, security_id in rows
        ]
    )
    dates = sorted({item[0] for item in rows})
    securities = sorted({item[1] for item in rows})
    evidence_sha256 = "f" * 64
    bars = pd.DataFrame(
        [
            {
                "security_id": security_id,
                "date": decision_date,
                "Open": 100.0,
                "High": 101.0,
                "Low": 99.0,
                "Close": 100.0,
                "Volume": 1000.0,
            }
            for security_id in securities
            for decision_date in dates
        ]
    )
    frames: dict[str, pd.DataFrame] = {
        "fund_holdings_observed": pd.DataFrame(
            [
                {
                    "as_of_date": decision_date,
                    "published_at": f"{decision_date}T15:00:00-05:00",
                    "observed_at": f"{decision_date}T15:01:00-05:00",
                    "url": "https://example.test/observed",
                    "source_version": decision_date,
                    "content_sha256": evidence_sha256,
                    "evidence_role": "SIGNAL_INPUT",
                    "security_id": security_id,
                }
                for decision_date, security_id in rows
            ]
        ),
        "membership_events": pd.DataFrame(
            columns=(
                "event_id",
                "security_id",
                "event_type",
                "announced_at",
                "effective_at",
                "source_id",
                "evidence_sha256",
            )
        ),
        "membership_monthly": membership,
        "security_master": pd.DataFrame(
            [
                {
                    "security_id": security_id,
                    "issuer_id": f"issuer-{security_id}",
                    "primary_identifier_type": "ISIN",
                    "primary_identifier": f"ISIN-{security_id}",
                    "asset_class": "COMMON_EQUITY",
                }
                for security_id in securities
            ]
        ),
        "identifiers": pd.DataFrame(
            [
                {
                    "security_id": security_id,
                    "identifier_type": "ISIN",
                    "identifier_value": f"ISIN-{security_id}",
                    "valid_from": "2000-01-01",
                    "valid_to": None,
                }
                for security_id in securities
            ]
        ),
        "listing_aliases": pd.DataFrame(
            [
                {
                    "security_id": security_id,
                    "ticker": security_id.removeprefix("us_").upper(),
                    "vendor_code": f"{security_id.removeprefix('us_').upper()}.US",
                    "exchange": "XNAS",
                    "valid_from": "2000-01-01",
                    "valid_to": None,
                }
                for security_id in securities
            ]
        ),
        "corporate_actions": pd.DataFrame(
            [
                {
                    "action_id": "action-aapl-split",
                    "security_id": "us_aapl",
                    "action_type": "SPLIT",
                    "announced_at": "2025-12-01T12:00:00+00:00",
                    "effective_at": "2025-12-15",
                    "pay_date": None,
                    "terms_verified": True,
                    "source_id": "official-action",
                    "evidence_sha256": evidence_sha256,
                }
            ]
        ),
        "session_exceptions": pd.DataFrame(
            [
                {
                    "security_id": "us_aapl",
                    "session_date": dates[0],
                    "exception_type": "HALT",
                    "verified": True,
                    "source_id": "official-exception",
                    "evidence_sha256": evidence_sha256,
                }
            ],
            columns=(
                "security_id",
                "session_date",
                "exception_type",
                "verified",
                "source_id",
                "evidence_sha256",
            ),
        ),
        "bars_raw": bars,
        "bars_vendor_front": bars.copy(),
        "bars_pit_signal": pd.DataFrame(
            [
                {
                    "decision_date": decision_date,
                    "security_id": security_id,
                    "date": decision_date,
                    "Open": 100.0,
                    "High": 101.0,
                    "Low": 99.0,
                    "Close": 100.0,
                    "Volume": 1000.0,
                }
                for decision_date, security_id in rows
            ]
        ),
        "benchmarks": pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "date": decision_date,
                    "Open": 100.0,
                    "High": 101.0,
                    "Low": 99.0,
                    "Close": 100.0,
                    "Volume": 1000.0,
                    "adjustment": "none",
                    "TotalReturnClose": 100.0,
                    "total_return_source_id": "official-benchmark",
                    "total_return_evidence_sha256": evidence_sha256,
                }
                for symbol in ("SPY", "BIL")
                for decision_date in dates
            ]
        ),
        "xnys_calendar": pd.DataFrame(
            [
                {
                    "session_date": decision_date,
                    "market_open": f"{decision_date}T09:30:00-05:00",
                    "market_close": f"{decision_date}T16:00:00-05:00",
                }
                for decision_date in dates
            ]
        ),
        "execution_fee_schedule": pd.DataFrame(
            [
                {
                    "effective_from": "2020-01-01",
                    "effective_to": None,
                    "commission_rate": 0.0005,
                    "slippage_rate": 0.0005,
                    "sec_sell_fee_rate": 0.0,
                    "finra_taf_per_share": 0.0,
                }
            ]
        ),
    }
    if mutation is not None:
        artifact, column, value = mutation
        if artifact not in frames or frames[artifact].empty:
            raise AssertionError(f"invalid fixture mutation artifact: {artifact}")
        frames[artifact] = frames[artifact].copy()
        frames[artifact].loc[frames[artifact].index[0], column] = value

    report = QualityReport(
        policy_version="us-pit-quality-v1",
        status=status,
        includes_delisted=True,
        issues=(),
        metrics={
            "decision_months": len({item[0] for item in rows}),
            "quality_contract_revision": 3,
        },
    )
    report_ref = store.put_bytes(
        canonical_json_bytes(report.to_dict()), media_type=JSON_MEDIA_TYPE
    )
    objects = {
        name: store.put_dataframe(frame) for name, frame in frames.items()
    }
    objects["quality_report"] = report_ref
    manifest = ReleaseManifest(
        universe_id=UNIVERSE_ID,
        created_at=f"2026-{len(rows):02d}-01T00:00:00+00:00",
        status=status,
        artifacts={name: store.descriptor(name, reference) for name, reference in objects.items()},
        sources=(),
    )
    return store.publish_release(manifest, objects)


class USMomentumProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Path(self.temporary.name) / "us_program.sqlite3"
        self.program = USMomentumProgram(self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _register_ready(self) -> None:
        self.program.register_data_release(_release())

    def _register_historical(self) -> None:
        self.program.register_historical(
            _historical(),
            HISTORICAL_SHA256,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )

    def _register_tdx(self) -> None:
        self.program.register_tdx(
            _tdx(),
            TDX_SHA256,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )

    def _activate_release(self, release) -> tuple[str, str]:
        release_id = release.release_id
        manifest_sha256 = sha256_file(release.path / "manifest.json")
        self.program.register_data_release(release)
        self.program.register_historical(
            _historical(),
            HISTORICAL_SHA256,
            release_id=release_id,
            manifest_sha256=manifest_sha256,
        )
        self.program.register_tdx(
            _tdx(),
            TDX_SHA256,
            release_id=release_id,
            manifest_sha256=manifest_sha256,
        )
        self.program.start_paper_collection(
            release_id=release_id,
            manifest_sha256=manifest_sha256,
        )
        return release_id, manifest_sha256

    def test_initial_state_is_fail_closed_and_broker_is_immutable(self) -> None:
        status = self.program.status()
        self.assertEqual("DATA_BLOCKED", status["state"])
        self.assertFalse(status["broker_writes_enabled"])
        self.assertFalse(status["real_broker_order_entrypoints"])
        self.assertEqual("PAPER_ONLY", status["execution_mode"])
        with self.assertRaises(AttributeError):
            self.program.broker_writes_enabled = True  # type: ignore[misc]

    def test_complete_happy_path_requires_all_gates(self) -> None:
        self._register_ready()
        self.assertEqual("DATA_READY", self.program.status()["state"])
        self._register_historical()
        self.assertEqual("BACKTEST_QUALIFIED", self.program.status()["state"])
        self._register_tdx()
        self.assertEqual("BACKTEST_QUALIFIED", self.program.status()["state"])
        collecting = self.program.start_paper_collection(
            release_id=RELEASE_ID, manifest_sha256=MANIFEST_SHA256
        )
        self.assertEqual("PAPER_COLLECTING", collecting["state"])

        still_collecting = self.program.register_paper(
            _paper(qualified=False),
            "4" * 64,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual("PAPER_COLLECTING", still_collecting["state"])
        qualified = self.program.register_paper(
            _paper(qualified=True),
            PAPER_SHA256,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual("PAPER_QUALIFIED", qualified["state"])
        self.assertTrue(qualified["data_ready"])
        self.assertTrue(qualified["historical_qualified"])
        self.assertTrue(qualified["tdx_qualified"])
        self.assertTrue(qualified["paper_qualified"])
        self.assertFalse(qualified["broker_writes_enabled"])

        events = self.program.events()
        self.assertEqual(6, len(events))
        self.assertEqual(
            [
                "REGISTER_DATA_RELEASE",
                "REGISTER_HISTORICAL_DECISION",
                "REGISTER_TDX_DECISION",
                "START_PAPER_COLLECTION",
                "REGISTER_PAPER_DECISION",
                "REGISTER_PAPER_DECISION",
            ],
            [event["action"] for event in events],
        )
        self.assertTrue(all(len(event["payload_sha256"]) == 64 for event in events))

    def test_data_ready_requires_matching_derived_quality_report(self) -> None:
        with self.assertRaisesRegex(USProgramEvidenceError, "quality report"):
            self.program.register_data_release(
                _release(status="DATA_READY", quality_status="DATA_BLOCKED")
            )
        self.assertEqual("DATA_BLOCKED", self.program.status()["state"])

    def test_blocked_release_can_be_replaced_only_before_data_ready(self) -> None:
        blocked = dict(_release(status="DATA_BLOCKED"))
        self.program.register_data_release(blocked)
        self.assertEqual("DATA_BLOCKED", self.program.status()["state"])
        ready = dict(_release())
        ready["release_id"] = "9" * 64
        ready["manifest_sha256"] = "8" * 64
        state = self.program.register_data_release(ready)
        self.assertEqual("DATA_READY", state["state"])
        with self.assertRaises(USProgramStateError):
            self.program.register_data_release(
                {
                    **_release(),
                    "release_id": "7" * 64,
                    "manifest_sha256": "6" * 64,
                }
            )

    def test_sequence_rejects_tdx_and_paper_start_too_early(self) -> None:
        self._register_ready()
        with self.assertRaisesRegex(USProgramStateError, "BACKTEST_QUALIFIED"):
            self.program.register_tdx(
                _tdx(),
                TDX_SHA256,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
            )
        self._register_historical()
        with self.assertRaisesRegex(USProgramStateError, "TDX qualification"):
            self.program.start_paper_collection(
                release_id=RELEASE_ID, manifest_sha256=MANIFEST_SHA256
            )

    def test_historical_failure_is_fail_closed(self) -> None:
        self._register_ready()
        state = self.program.register_historical(
            _historical(False),
            HISTORICAL_SHA256,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual("HISTORICAL_FAILED", state["state"])
        with self.assertRaises(USProgramStateError):
            self.program.register_tdx(
                _tdx(),
                TDX_SHA256,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
            )

    def test_tdx_failure_blocks_paper(self) -> None:
        self._register_ready()
        self._register_historical()
        state = self.program.register_tdx(
            _tdx(False),
            TDX_SHA256,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual("PAPER_BLOCKED", state["state"])
        with self.assertRaises(USProgramStateError):
            self.program.start_paper_collection(
                release_id=RELEASE_ID, manifest_sha256=MANIFEST_SHA256
            )

    def test_paper_integrity_failure_blocks_without_fabricating_qualification(self) -> None:
        self._register_ready()
        self._register_historical()
        self._register_tdx()
        self.program.start_paper_collection(
            release_id=RELEASE_ID, manifest_sha256=MANIFEST_SHA256
        )
        state = self.program.register_paper(
            _paper(qualified=False, blocked=True),
            PAPER_SHA256,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual("PAPER_BLOCKED", state["state"])
        self.assertFalse(state["paper_qualified"])

    def test_same_evidence_is_idempotent_and_conflicting_payload_is_rejected(self) -> None:
        self._register_ready()
        self._register_historical()
        before = self.program.status()
        again = self.program.register_historical(
            _historical(),
            HISTORICAL_SHA256,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual(before["version"], again["version"])
        self.assertEqual(before["event_count"], again["event_count"])
        with self.assertRaisesRegex(USProgramEvidenceError, "conflicting"):
            self.program.register_historical(
                _historical(False),
                HISTORICAL_SHA256,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
            )

    def test_release_and_manifest_are_bound_at_every_gate(self) -> None:
        self._register_ready()
        with self.assertRaisesRegex(USProgramEvidenceError, "active PIT release"):
            self.program.register_historical(
                _historical(),
                HISTORICAL_SHA256,
                release_id="f" * 64,
                manifest_sha256=MANIFEST_SHA256,
            )
        with self.assertRaisesRegex(USProgramEvidenceError, "active PIT release"):
            self.program.register_historical(
                _historical(),
                HISTORICAL_SHA256,
                release_id=RELEASE_ID,
                manifest_sha256="f" * 64,
            )

    def test_malformed_hash_and_incoherent_decision_are_rejected(self) -> None:
        self._register_ready()
        with self.assertRaisesRegex(USProgramEvidenceError, "SHA-256"):
            self.program.register_historical(
                _historical(),
                "not-a-hash",
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
            )
        incoherent = PromotionDecision(
            qualified=True,
            status="BACKTEST_QUALIFIED",
            gates={"oos": False},
            failures=("oos",),
        )
        with self.assertRaisesRegex(USProgramEvidenceError, "qualified flag"):
            self.program.register_historical(
                {"decision": incoherent},
                HISTORICAL_SHA256,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
            )

    def test_event_log_is_append_only(self) -> None:
        self._register_ready()
        connection = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM us_program_events")
        finally:
            connection.close()

    def test_manual_state_string_update_is_detected(self) -> None:
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "UPDATE us_program_state SET state = 'PAPER_QUALIFIED'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(USProgramStateError, "without qualification evidence"):
            self.program.status()

    def test_paper_release_admission_appends_two_months_and_survives_restart(self) -> None:
        store = USPITStore(Path(self.temporary.name) / "us_pit")
        base = _pit_release(
            store,
            [("2026-01-30", "us_aapl"), ("2026-02-27", "us_aapl")],
        )
        march = _pit_release(
            store,
            [
                ("2026-01-30", "us_aapl"),
                ("2026-02-27", "us_aapl"),
                ("2026-03-31", "us_msft"),
            ],
        )
        april = _pit_release(
            store,
            [
                ("2026-01-30", "us_aapl"),
                ("2026-02-27", "us_aapl"),
                ("2026-03-31", "us_msft"),
                ("2026-04-30", "us_nvda"),
            ],
        )
        base_id, base_manifest = self._activate_release(base)

        march_state = self.program.admit_paper_release(march)
        april_state = self.program.admit_paper_release(april)
        repeated = self.program.admit_paper_release(april)

        self.assertEqual(base_id, april_state["release_id"])
        self.assertEqual(base_manifest, april_state["manifest_sha256"])
        self.assertEqual(april.release_id, april_state["paper_decision_release_id"])
        self.assertEqual(3, april_state["paper_release_admission_count"])
        self.assertEqual(
            march.release_id,
            april_state["paper_release_admission"]["old_release_id"],
        )
        audit_payload = april_state["paper_release_admission"]["payload"]
        self.assertEqual(
            audit_payload["old_historical_input_aggregate_sha256"],
            audit_payload["candidate_historical_input_aggregate_sha256"],
        )
        self.assertEqual(
            set(audit_payload["old_historical_input_prefix_sha256"]),
            {
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
            },
        )
        self.assertEqual(
            audit_payload["admitted_historical_input_cutoff"], "2026-04-30"
        )
        self.assertEqual(
            april_state["paper_release_admission_count"],
            repeated["paper_release_admission_count"],
        )
        restarted = USMomentumProgram(self.db).status()
        self.assertEqual(april.release_id, restarted["paper_decision_release_id"])
        self.assertEqual(3, len(self.program.paper_release_admissions()))

    def test_paper_release_admission_rejects_changed_old_month_and_not_ready(self) -> None:
        store = USPITStore(Path(self.temporary.name) / "us_pit")
        base = _pit_release(
            store,
            [("2026-01-30", "us_aapl"), ("2026-02-27", "us_aapl")],
        )
        changed = _pit_release(
            store,
            [
                ("2026-01-30", "us_aapl"),
                ("2026-02-27", "us_msft"),
                ("2026-03-31", "us_msft"),
            ],
        )
        blocked = _pit_release(
            store,
            [
                ("2026-01-30", "us_aapl"),
                ("2026-02-27", "us_aapl"),
                ("2026-03-31", "us_msft"),
            ],
            status=ReleaseStatus.DATA_BLOCKED,
        )
        self._activate_release(base)

        with self.assertRaisesRegex(
            USProgramEvidenceError, "modified previously admitted|append"
        ):
            self.program.admit_paper_release(changed)
        with self.assertRaisesRegex(USProgramEvidenceError, "DATA_READY"):
            self.program.admit_paper_release(blocked)
        self.assertEqual(1, self.program.status()["paper_release_admission_count"])

    def test_paper_release_admission_rejects_any_old_decision_input_rewrite(self) -> None:
        store = USPITStore(Path(self.temporary.name) / "us_pit")
        base_rows = [
            ("2026-01-30", "us_aapl"),
            ("2026-02-27", "us_aapl"),
        ]
        candidate_rows = [*base_rows, ("2026-03-31", "us_msft")]
        base = _pit_release(store, base_rows)
        self._activate_release(base)
        mutations = (
            ("bars_raw", "Close", 123.0),
            ("bars_vendor_front", "Close", 124.0),
            ("bars_pit_signal", "Close", 125.0),
            ("benchmarks", "TotalReturnClose", 126.0),
            ("listing_aliases", "ticker", "AAPX"),
            ("identifiers", "identifier_value", "REWRITTEN"),
            ("security_master", "issuer_id", "rewritten-issuer"),
            ("corporate_actions", "action_type", "CASH_DIVIDEND"),
            ("session_exceptions", "exception_type", "DELISTED"),
            ("xnys_calendar", "market_close", "2026-01-30T15:59:00-05:00"),
            ("execution_fee_schedule", "commission_rate", 0.0099),
        )

        for artifact, column, value in mutations:
            with self.subTest(artifact=artifact):
                candidate = _pit_release(
                    store,
                    candidate_rows,
                    mutation=(artifact, column, value),
                )
                with self.assertRaisesRegex(
                    USProgramEvidenceError,
                    f"historical inputs: .*{artifact}",
                ):
                    self.program.admit_paper_release(candidate)
        self.assertEqual(1, self.program.status()["paper_release_admission_count"])


if __name__ == "__main__":
    unittest.main()
