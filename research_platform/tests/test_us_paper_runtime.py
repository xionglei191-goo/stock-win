from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from zoneinfo import ZoneInfo

from research_platform.us_paper import USMomentumPaperService, USPaperConfig
from research_platform.us_paper_runtime import (
    DAILY_SOURCE_FREQUENCY,
    DAILY_SOURCE_SCHEMA,
    FrozenXNYSSchedule,
    USPaperRuntime,
    USPaperRuntimeConfig,
    USPaperRuntimeError,
    USPaperRuntimeLeaseError,
    USPaperRuntimeScheduleError,
    canonical_daily_source_sha256,
    install_windows_task,
    remove_windows_task,
    windows_task_status,
    windows_task_scheduler_spec,
)
from research_platform.us_tdx import USQuoteObservation


NY = ZoneInfo("America/New_York")
SESSION = date(2026, 8, 12)
RELEASE_ID = "1" * 64
MANIFEST_SHA256 = "2" * 64
RELEASE_ID_2 = "3" * 64
MANIFEST_SHA256_2 = "4" * 64
RELEASE_ID_3 = "5" * 64
MANIFEST_SHA256_3 = "6" * 64
SECURITY_ID = "us_aapl_fixture"


def _tdx_daily_bar(
    code: str,
    *,
    observed_at: datetime,
    opening: float,
    high: float,
    low: float,
    closing: float,
) -> dict[str, object]:
    adjustment = "front" if code == "BILTR.US" else "none"
    source_rows = (
        [
            {
                "session_date": (SESSION - timedelta(days=1)).isoformat(),
                "Close": opening,
            },
            {"session_date": SESSION.isoformat(), "Close": closing},
        ]
        if code == "BILTR.US"
        else [
            {
                "session_date": SESSION.isoformat(),
                "Open": opening,
                "High": high,
                "Low": low,
                "Close": closing,
            }
        ]
    )
    return {
        "code": code,
        "session_date": SESSION,
        "observed_at": observed_at,
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


class _QuoteClient:
    def __init__(self, factory):
        self.factory = factory
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


def _ready_preflight():
    return {"ready": True, "status": "READY"}


def _release_admission(
    old_release_id: str,
    old_manifest_sha256: str,
    release_id: str,
    manifest_sha256: str,
    membership_prefix_sha256: str,
) -> dict[str, object]:
    payload = {
        "admission_type": "ROLL_FORWARD",
        "old_release_id": old_release_id,
        "old_manifest_sha256": old_manifest_sha256,
        "release_id": release_id,
        "manifest_sha256": manifest_sha256,
        "old_membership_artifact_sha256": "b" * 64,
        "membership_artifact_sha256": "c" * 64,
        "membership_prefix_sha256": membership_prefix_sha256,
        "old_max_decision_date": "2026-01-30",
        "max_decision_date": "2026-02-27",
        "old_row_count": 1,
        "row_count": 2,
        "new_decision_dates": ["2026-02-27"],
        "catalog_verified": True,
        "manifest_verified": True,
        "cas_verified": True,
    }
    canonical = lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    admission_key = hashlib.sha256(
        canonical(
            {
                "program_id": "us_momentum_v1",
                "old_release_id": old_release_id,
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
                "membership_prefix_sha256": membership_prefix_sha256,
            }
        ).encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "admission_key": admission_key,
        "payload_sha256": hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest(),
        "payload": payload,
    }


class USPaperRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paper = USMomentumPaperService(
            USPaperConfig(database_path=root / "paper.db")
        )
        self.state_path = root / "runtime.db"
        self.schedule = FrozenXNYSSchedule((SESSION,))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def runtime(
        self,
        *,
        worker_id: str = "worker-a",
        client: _QuoteClient | None = None,
        preflight=_ready_preflight,
    ) -> USPaperRuntime:
        return USPaperRuntime(
            USPaperRuntimeConfig(
                state_database_path=self.state_path,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
                worker_id=worker_id,
            ),
            schedule=self.schedule,
            paper=self.paper,
            quote_client=client or _QuoteClient(_fresh_quote),
            preflight=preflight,
        )

    def create_buy_period(self) -> None:
        decision = datetime(2026, 8, 11, 16, 15, tzinfo=NY)
        self.paper.create_period(
            [
                {
                    "signal_id": "sig-aapl-buy",
                    "code": "AAPL.US",
                    "side": "BUY",
                    "target_weight": 0.10,
                    "generated_at": decision,
                    "available_at": datetime(2026, 8, 12, 9, 30, tzinfo=NY),
                    "valid_until": datetime(2026, 8, 12, 9, 35, tzinfo=NY),
                    "reason_codes": ["MONTHLY_MOMENTUM"],
                    "evidence": {
                        "stop_ratio": 0.08,
                        "security_id": SECURITY_ID,
                        "pit_release_id": RELEASE_ID,
                        "manifest_sha256": MANIFEST_SHA256,
                    },
                }
            ],
            now=decision + timedelta(minutes=1),
        )

    def stage(self, runtime: USPaperRuntime) -> None:
        result = runtime.tick(now=datetime(2026, 8, 12, 9, 15, tzinfo=NY))
        self.assertEqual("AUTO_APPROVED", result["sessions"][0]["approval_state"])
        self.assertEqual("STAGED", result["sessions"][0]["staging_state"])

    def test_preflight_failure_is_fail_closed_but_heartbeat_is_recorded(self) -> None:
        client = _QuoteClient(_fresh_quote)
        runtime = self.runtime(
            client=client,
            preflight=lambda: {"ready": False, "detail": "TdxW not running"},
        )

        status = runtime.tick(now=datetime(2026, 8, 12, 9, 31, tzinfo=NY))

        self.assertEqual("PAPER_BLOCKED", status["runtime"]["status"])
        self.assertEqual(1, status["runtime"]["heartbeat_seq"])
        self.assertEqual([], client.calls)
        self.assertFalse(status["broker_writes_enabled"])

    def test_fresh_open_is_admitted_only_after_timely_staging(self) -> None:
        self.create_buy_period()
        client = _QuoteClient(_fresh_quote)
        runtime = self.runtime(client=client)
        self.stage(runtime)

        status = runtime.tick(now=datetime(2026, 8, 12, 9, 31, tzinfo=NY))

        self.assertEqual(1, len(client.calls))
        self.assertEqual(1, len(status["paper"]["fills"]))
        self.assertEqual("BUY", status["paper"]["fills"][0]["side"])
        self.assertEqual(1, status["quotes"][0]["admitted"])
        self.assertEqual("ACCEPTED", status["quotes"][0]["reason"])

    def test_late_open_is_never_queried_or_backfilled(self) -> None:
        self.create_buy_period()
        client = _QuoteClient(_fresh_quote)
        runtime = self.runtime(client=client)
        self.stage(runtime)

        status = runtime.tick(now=datetime(2026, 8, 12, 9, 36, tzinfo=NY))

        self.assertEqual([], client.calls)
        self.assertEqual([], status["paper"]["fills"])
        order = status["paper"]["orders"][0]
        self.assertEqual("EXPIRED", order["status"])
        self.assertEqual("LATE_OPEN_NOT_BACKFILLED", order["block_reason"])

    def test_missing_source_timestamp_blocks_buy(self) -> None:
        self.create_buy_period()

        def no_source(code: str, fetched_at: datetime) -> USQuoteObservation:
            quote = _fresh_quote(code, fetched_at)
            return USQuoteObservation(
                code=quote.code,
                fetched_at=quote.fetched_at,
                source_at=None,
                market_status=quote.market_status,
                open=quote.open,
                last=quote.last,
                bid=quote.bid,
                ask=quote.ask,
                raw=quote.raw,
            )

        runtime = self.runtime(client=_QuoteClient(no_source))
        self.stage(runtime)

        status = runtime.tick(now=datetime(2026, 8, 12, 9, 31, tzinfo=NY))

        self.assertEqual([], status["paper"]["fills"])
        self.assertEqual("DATA_DEGRADED", status["runtime"]["status"])
        self.assertEqual(0, status["quotes"][0]["admitted"])
        self.assertEqual("MISSING_SOURCE_TIMESTAMP", status["quotes"][0]["reason"])

    def test_future_quote_timestamp_is_rejected(self) -> None:
        self.create_buy_period()

        def future(code: str, fetched_at: datetime) -> USQuoteObservation:
            quote = _fresh_quote(code, fetched_at)
            return USQuoteObservation(
                code=quote.code,
                fetched_at=fetched_at + timedelta(seconds=1),
                source_at=fetched_at + timedelta(seconds=1),
                market_status=quote.market_status,
                open=quote.open,
                last=quote.last,
                bid=quote.bid,
                ask=quote.ask,
                raw=quote.raw,
            )

        runtime = self.runtime(client=_QuoteClient(future))
        self.stage(runtime)
        status = runtime.tick(now=datetime(2026, 8, 12, 9, 31, tzinfo=NY))

        self.assertEqual([], status["paper"]["fills"])
        self.assertEqual("FUTURE_OR_REVERSED_TIMESTAMP", status["quotes"][0]["reason"])

    def test_sqlite_lease_allows_one_worker_and_exposes_heartbeat(self) -> None:
        runtime_a = self.runtime(worker_id="worker-a")
        runtime_b = USPaperRuntime.open_existing(
            USPaperRuntimeConfig(
                state_database_path=self.state_path,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
                worker_id="worker-b",
            ),
            self.paper,
            _ready_preflight,
            quote_client=_QuoteClient(_fresh_quote),
        )
        now = datetime(2026, 8, 12, 9, 0, tzinfo=NY)

        status = runtime_a.tick(now=now)
        with self.assertRaises(USPaperRuntimeLeaseError):
            runtime_b.tick(now=now + timedelta(seconds=1))

        self.assertEqual("worker-a", status["lease"]["owner"])
        self.assertEqual("worker-a", status["runtime"]["worker_id"])
        self.assertEqual(now.isoformat(), status["runtime"]["heartbeat_at"])

    def test_open_existing_restores_calendar_and_allows_expired_lease_takeover(self) -> None:
        runtime_a = self.runtime(worker_id="worker-a")
        first = datetime(2026, 8, 12, 9, 0, tzinfo=NY)
        runtime_a.tick(now=first)
        config_b = USPaperRuntimeConfig(
            state_database_path=self.state_path,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
            worker_id="worker-b",
        )

        with self.assertRaises(USPaperRuntimeError):
            USPaperRuntime(
                config_b,
                schedule=FrozenXNYSSchedule((SESSION,)),
                paper=self.paper,
                preflight=_ready_preflight,
            )
        runtime_b = USPaperRuntime.open_existing(
            config_b,
            self.paper,
            _ready_preflight,
            quote_client=_QuoteClient(_fresh_quote),
        )
        status = runtime_b.tick(now=first + timedelta(seconds=121))

        self.assertEqual([SESSION.isoformat()], status["calendar"]["sessions"])
        self.assertEqual(RELEASE_ID, status["bindings"]["release_id"])
        self.assertEqual(MANIFEST_SHA256, status["bindings"]["manifest_sha256"])
        self.assertEqual("worker-b", status["lease"]["owner"])
        self.assertEqual(2, status["lease"]["generation"])

    def test_open_existing_rejects_policy_binding_or_calendar_tampering(self) -> None:
        self.runtime()
        changed_policy = USPaperRuntimeConfig(
            state_database_path=self.state_path,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
            worker_id="replacement-worker",
            quote_max_age_seconds=80,
        )
        with self.assertRaisesRegex(USPaperRuntimeError, "execution policy"):
            USPaperRuntime.open_existing(
                changed_policy,
                self.paper,
                _ready_preflight,
            )

        import sqlite3

        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "UPDATE us_paper_runtime_state SET calendar_json=? WHERE singleton=1",
                ('{"calendar":"XNYS","frozen":true,"sessions":[]}',),
            )
            connection.commit()
        finally:
            connection.close()
        valid_policy = USPaperRuntimeConfig(
            state_database_path=self.state_path,
            release_id=RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
            worker_id="replacement-worker",
        )
        with self.assertRaisesRegex(USPaperRuntimeError, "calendar metadata"):
            USPaperRuntime.open_existing(
                valid_policy,
                self.paper,
                _ready_preflight,
            )

    def test_runtime_config_requires_lowercase_sha256_bindings(self) -> None:
        with self.assertRaisesRegex(ValueError, "release_id"):
            USPaperRuntimeConfig(
                state_database_path=self.state_path,
                release_id="A" * 64,
                manifest_sha256=MANIFEST_SHA256,
                worker_id="worker",
            )

    def test_raw_daily_bar_is_accepted_after_close_without_tq_backfill(self) -> None:
        self.create_buy_period()
        runtime = self.runtime(client=_QuoteClient(_fresh_quote))
        self.stage(runtime)
        runtime.tick(now=datetime(2026, 8, 12, 9, 31, tzinfo=NY))
        close_tick = datetime(2026, 8, 12, 16, 1, tzinfo=NY)

        status = runtime.tick(
            now=close_tick,
            daily_bars=[
                _tdx_daily_bar(
                    "AAPL.US",
                    observed_at=close_tick,
                    opening=100.0,
                    high=106.0,
                    low=98.0,
                    closing=104.0,
                ),
                _tdx_daily_bar(
                    "BIL.US",
                    observed_at=close_tick,
                    opening=91.0,
                    high=91.0,
                    low=91.0,
                    closing=91.0,
                ),
                _tdx_daily_bar(
                    "BILTR.US",
                    observed_at=close_tick,
                    opening=90.95,
                    high=91.0,
                    low=90.95,
                    closing=91.0,
                ),
            ],
        )

        self.assertEqual(104.0, status["paper"]["positions"][0]["last_price"])
        purposes = [item["purpose"] for item in status["quotes"]]
        self.assertEqual(["OPEN"], purposes)
        connection = sqlite3.connect(self.paper.config.database_path)
        connection.row_factory = sqlite3.Row
        try:
            biltr = dict(
                connection.execute(
                    "SELECT * FROM us_paper_observations WHERE code='BILTR.US'"
                ).fetchone()
            )
        finally:
            connection.close()
        payload = json.loads(biltr["payload_json"])
        self.assertEqual("front", payload["adjustment"])
        self.assertEqual("TDX", payload["source"])
        self.assertEqual(2, len(payload["source_rows"]))
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            biltr["content_hash"],
        )

    def test_daily_source_provenance_is_required_and_hash_checked(self) -> None:
        runtime = self.runtime()
        close_tick = datetime(2026, 8, 12, 16, 1, tzinfo=NY)
        missing = {
            "code": "BIL.US",
            "session_date": SESSION,
            "observed_at": close_tick,
            "open": 91.0,
            "high": 91.0,
            "low": 91.0,
            "close": 91.0,
        }
        with self.assertRaisesRegex(ValueError, "canonical TDX"):
            runtime.tick(now=close_tick, daily_bars=[missing])

        tampered = _tdx_daily_bar(
            "BILTR.US",
            observed_at=close_tick,
            opening=90.95,
            high=91.0,
            low=90.95,
            closing=91.0,
        )
        tampered["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source_sha256 mismatch"):
            runtime.tick(now=close_tick, daily_bars=[tampered])

    def test_close_without_bil_raw_is_data_degraded(self) -> None:
        runtime = self.runtime()

        status = runtime.tick(
            now=datetime(2026, 8, 12, 16, 1, tzinfo=NY),
        )

        self.assertEqual("DATA_DEGRADED", status["runtime"]["status"])
        self.assertIn("MISSING_BIL_BENCHMARK", status["runtime"]["blocked_reason"])

    def test_intraday_stop_breach_fills_from_fresh_quote_idempotently(self) -> None:
        self.create_buy_period()

        def quote(code: str, fetched_at: datetime) -> USQuoteObservation:
            value = _fresh_quote(code, fetched_at)
            if fetched_at.hour >= 10:
                return USQuoteObservation(
                    code=code,
                    fetched_at=fetched_at,
                    source_at=fetched_at - timedelta(seconds=10),
                    market_status="TRADING",
                    open=100.0,
                    last=80.0,
                    bid=79.9,
                    ask=80.1,
                    raw={"Open": 100.0, "Now": 80.0},
                )
            return value

        runtime = self.runtime(client=_QuoteClient(quote))
        self.stage(runtime)
        runtime.tick(now=datetime(2026, 8, 12, 9, 31, tzinfo=NY))

        status = runtime.tick(now=datetime(2026, 8, 12, 10, 0, tzinfo=NY))
        duplicate = runtime.tick(now=datetime(2026, 8, 12, 10, 0, tzinfo=NY))

        self.assertEqual(2, len(status["paper"]["fills"]))
        self.assertEqual("SELL", status["paper"]["fills"][-1]["side"])
        self.assertEqual(
            "US_FIXED_STOP_INTRADAY_QUOTE",
            status["paper"]["fills"][-1]["reason"],
        )
        self.assertEqual([], status["paper"]["positions"])
        self.assertEqual(2, len(duplicate["paper"]["fills"]))
        events = [item for item in status["events"] if item["event_type"] == "INTRADAY_STOP_BREACH"]
        self.assertEqual(1, len(events))
        self.assertEqual("RUNNING", status["runtime"]["status"])

    def test_kill_cancels_buys_but_quote_gated_risk_sell_continues(self) -> None:
        self.create_buy_period()
        runtime = self.runtime()

        status = runtime.kill(
            reason="operator emergency",
            now=datetime(2026, 8, 12, 9, 0, tzinfo=NY),
        )

        self.assertEqual("KILLED", status["runtime"]["status"])
        self.assertEqual("KILLED", status["paper"]["account"]["status"])
        self.assertTrue(
            status["kill_policy"]["selective_risk_sell_continuation_supported"]
        )
        self.assertEqual("CANCELLED", status["paper"]["orders"][0]["status"])

    def test_runtime_applies_corporate_action_before_open_risk_processing(self) -> None:
        self.create_buy_period()
        runtime = self.runtime()
        self.stage(runtime)
        runtime.tick(now=datetime(2026, 8, 12, 9, 31, tzinfo=NY))
        position = runtime.status()["paper"]["positions"][0]
        quantity = int(position["quantity"])

        next_session = date(2026, 8, 13)
        schedule = FrozenXNYSSchedule((SESSION, next_session))
        # This focused test uses a fresh runtime ledger with both sessions;
        # the paper DB itself proves action-before-open ordering.
        other_state = self.state_path.with_name("runtime-actions.db")
        runtime2 = USPaperRuntime(
            USPaperRuntimeConfig(
                state_database_path=other_state,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
                worker_id="worker-actions",
            ),
            schedule=schedule,
            paper=self.paper,
            quote_client=_QuoteClient(_fresh_quote),
            preflight=_ready_preflight,
        )
        action = {
            "action_id": "runtime-split",
            "action_type": "SPLIT",
            "security_id": SECURITY_ID,
            "effective_date": next_session.isoformat(),
            "verified": True,
            "verified_at": datetime(2026, 8, 13, 8, 0, tzinfo=NY),
            "evidence_sha256": "a" * 64,
            "pit_release_id": RELEASE_ID,
            "manifest_sha256": MANIFEST_SHA256,
            "terms": {"ratio": 2.0},
        }
        status = runtime2.tick(
            now=datetime(2026, 8, 13, 9, 0, tzinfo=NY),
            corporate_actions=[action],
        )
        adjusted = status["paper"]["positions"][0]
        self.assertEqual(quantity * 2, adjusted["quantity"])
        self.assertAlmostEqual(float(position["stop_price"]) / 2, adjusted["stop_price"])

    def test_schedule_rejects_non_session_and_hash_tampering(self) -> None:
        runtime = self.runtime()
        with self.assertRaises(USPaperRuntimeScheduleError):
            runtime.tick(now=datetime(2026, 8, 13, 9, 0, tzinfo=NY))
        with self.assertRaises(ValueError):
            FrozenXNYSSchedule((SESSION,), source_hash="0" * 64)

    def test_task_scheduler_output_is_inert_and_paper_status_has_no_write_path(self) -> None:
        spec = windows_task_scheduler_spec(
            python_executable="py",
            project_root=Path(self.tempdir.name),
        )
        runtime = self.runtime()
        status = runtime.status()

        self.assertFalse(spec["registered"])
        self.assertEqual(60, spec["trigger"]["interval_seconds"])
        self.assertIn("us-paper tick", spec["action"]["arguments"])
        self.assertFalse(status["broker_writes_enabled"])
        self.assertFalse(hasattr(runtime, "place_order"))
        self.assertFalse(hasattr(runtime, "submit_order"))

    def test_windows_task_management_uses_only_the_paper_tick_action(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if "/Query" in command and sum("/Query" in item for item in calls) == 1:
                return CompletedProcess(command, 1, "", "not found")
            return CompletedProcess(command, 0, "ok", "")

        spec = windows_task_scheduler_spec(
            python_executable="C:\\Python\\python.exe",
            project_root=Path(self.tempdir.name),
        )
        installed = install_windows_task(spec, runner=runner)
        self.assertTrue(installed["registered"])
        self.assertFalse(installed["broker_writes_enabled"])
        self.assertEqual("schtasks.exe", calls[0][0])
        self.assertIn("/XML", calls[0])

        missing = windows_task_status(runner=runner)
        self.assertFalse(missing["registered"])
        removed = remove_windows_task(runner=runner)
        self.assertTrue(removed["removed"])
        self.assertIn("/Delete", calls[-1])

    def test_release_admission_chain_survives_restart_and_keeps_base_policy(self) -> None:
        runtime = self.runtime()
        first = _release_admission(
            RELEASE_ID,
            MANIFEST_SHA256,
            RELEASE_ID_2,
            MANIFEST_SHA256_2,
            "8" * 64,
        )
        second = _release_admission(
            RELEASE_ID_2,
            MANIFEST_SHA256_2,
            RELEASE_ID_3,
            MANIFEST_SHA256_3,
            "a" * 64,
        )

        runtime.admit_paper_release(first)
        admitted = runtime.admit_paper_release(second)
        self.assertEqual(RELEASE_ID, admitted["bindings"]["release_id"])
        self.assertEqual(RELEASE_ID_3, admitted["decision_release"]["release_id"])
        self.assertEqual(3, len(admitted["release_admissions"]))

        restarted = USPaperRuntime.open_existing(
            USPaperRuntimeConfig(
                state_database_path=self.state_path,
                release_id=RELEASE_ID,
                manifest_sha256=MANIFEST_SHA256,
                worker_id="restarted-worker",
            ),
            self.paper,
            _ready_preflight,
            quote_client=_QuoteClient(_fresh_quote),
        )
        self.assertEqual(RELEASE_ID_3, restarted.current_decision_binding()["release_id"])
        repeated = restarted.admit_paper_release(second)
        self.assertEqual(3, len(repeated["release_admissions"]))

    def test_release_admission_rejects_fork_and_unadmitted_period_blocks_tick(self) -> None:
        runtime = self.runtime()
        with self.assertRaisesRegex(USPaperRuntimeError, "current head"):
            runtime.admit_paper_release(
                _release_admission(
                    "f" * 64,
                    MANIFEST_SHA256,
                    RELEASE_ID_2,
                    MANIFEST_SHA256_2,
                    "8" * 64,
                )
            )

        decision = datetime(2026, 8, 11, 16, 15, tzinfo=NY)
        self.paper.create_period(
            [
                {
                    "signal_id": "sig-unadmitted",
                    "code": "AAPL.US",
                    "side": "BUY",
                    "target_weight": 0.10,
                    "generated_at": decision,
                    "available_at": datetime(2026, 8, 12, 9, 30, tzinfo=NY),
                    "valid_until": datetime(2026, 8, 12, 9, 35, tzinfo=NY),
                    "reason_codes": ["MONTHLY_MOMENTUM"],
                    "evidence": {
                        "stop_ratio": 0.08,
                        "security_id": SECURITY_ID,
                        "pit_release_id": RELEASE_ID_2,
                        "manifest_sha256": MANIFEST_SHA256_2,
                    },
                }
            ],
            now=decision + timedelta(minutes=1),
        )
        status = runtime.tick(now=datetime(2026, 8, 12, 9, 15, tzinfo=NY))
        self.assertEqual("PAPER_BLOCKED", status["runtime"]["status"])
        self.assertIn("UNADMITTED_PERIOD_RELEASE", status["runtime"]["blocked_reason"])


if __name__ == "__main__":
    unittest.main()
