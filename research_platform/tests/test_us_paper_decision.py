from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research_platform.us_paper import USMomentumPaperService, USPaperConfig
from research_platform.us_paper_decision import (
    TDXCurrentUSBarSource,
    USPaperBarBundle,
    USPaperDecisionCoordinator,
    USPaperDecisionAuditStore,
    _source_code_sha256,
)
from research_platform.us_paper_qualification import (
    USPaperQualificationEvidenceBuilder,
)
from research_platform.us_pit import QualityReport, ReleaseStatus, USBacktestDataset
from research_platform.strategies.us_momentum import USMomentumStrategy
from research_platform.us_qualification import PaperCycleEvidence


NY = ZoneInfo("America/New_York")
APPLE = "us_apple_fixture"
MICROSOFT = "us_microsoft_fixture"
DECISION = date(2025, 1, 31)
EXECUTION = date(2025, 2, 3)
MANIFEST_SHA256 = "e" * 64


def at(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), NY)


def bars(index: pd.DatetimeIndex, start: float = 50.0, end: float = 160.0) -> pd.DataFrame:
    close = np.linspace(start, end, len(index))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(len(index), 2_000_000.0),
        },
        index=index,
    )


def sessions() -> pd.DatetimeIndex:
    history = pd.bdate_range(end=DECISION.isoformat(), periods=300)
    return history.append(pd.DatetimeIndex([pd.Timestamp(EXECUTION)]))


def dataset(*, members: set[str] | None = None, include_microsoft: bool = False) -> USBacktestDataset:
    all_sessions = sessions()
    history = all_sessions[:-1]
    current_members = members if members is not None else {APPLE}
    aliases = [
        {
            "security_id": APPLE,
            "vendor_code": "AAPL.US",
            "valid_from": history[0],
            "valid_to": pd.NaT,
        }
    ]
    signal = {APPLE: bars(history)}
    raw = {APPLE: bars(history)}
    front = {APPLE: bars(history)}
    masters = [{"security_id": APPLE, "name": "Apple"}]
    if include_microsoft:
        aliases.append(
            {
                "security_id": MICROSOFT,
                "vendor_code": "MSFT.US",
                "valid_from": history[0],
                "valid_to": pd.NaT,
            }
        )
        signal[MICROSOFT] = bars(history, 60.0, 190.0)
        raw[MICROSOFT] = signal[MICROSOFT]
        front[MICROSOFT] = signal[MICROSOFT]
        masters.append({"security_id": MICROSOFT, "name": "Microsoft"})
    calendar = pd.DataFrame(
        {
            "session_date": all_sessions,
            "market_open": [at(item.date(), 9, 30) for item in all_sessions],
            "market_close": [at(item.date(), 16, 0) for item in all_sessions],
        }
    )
    quality = QualityReport(
        policy_version="us-pit-quality-v1",
        status=ReleaseStatus.DATA_READY,
        includes_delisted=True,
        issues=(),
    )
    spy = bars(history, 100.0, 170.0)
    return USBacktestDataset(
        release_id="d" * 64,
        universe_id="sp500_ivv_proxy_v1",
        quality_report=quality,
        membership_by_date={pd.Timestamp(DECISION): frozenset(current_members)},
        security_master=pd.DataFrame(masters),
        identifiers=pd.DataFrame(),
        listing_aliases=pd.DataFrame(aliases),
        corporate_actions=pd.DataFrame(),
        session_exceptions=pd.DataFrame(),
        calendar=calendar,
        fee_schedule=pd.DataFrame(),
        raw_bars=raw,
        vendor_front_bars=front,
        signal_bars_by_decision={pd.Timestamp(DECISION): signal},
        benchmark_bars={"SPY.US": spy},
    )


class FakeSource:
    def __init__(
        self,
        frame_by_code: Mapping[str, pd.DataFrame],
        *,
        observed_at: datetime | None = None,
        omit_raw: set[str] | None = None,
    ) -> None:
        self.frames = dict(frame_by_code)
        self.observed_at = observed_at or at(DECISION, 16, 15)
        self.omit_raw = omit_raw or set()
        self.calls: list[tuple[tuple[str, ...], date, int]] = []

    def fetch(self, codes: Sequence[str], *, asof: date, count: int) -> USPaperBarBundle:
        self.calls.append((tuple(codes), asof, count))
        front = {code: self.frames[code] for code in codes if code in self.frames}
        raw = {
            code: self.frames[code]
            for code in codes
            if code in self.frames and code not in self.omit_raw
        }
        return USPaperBarBundle(front, raw, self.observed_at)


class FakeProvider:
    cache_reads = False

    def __init__(self, frame_by_code: Mapping[str, pd.DataFrame]) -> None:
        self.frames = dict(frame_by_code)
        self.calls: list[dict[str, Any]] = []

    def fetch_bars(
        self,
        codes: list[str],
        period: str,
        count: int,
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        self.calls.append(
            {"codes": codes, "period": period, "count": count, **kwargs}
        )
        return {code: self.frames[code] for code in codes}


class USPaperDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paper = USMomentumPaperService(
            USPaperConfig(
                database_path=Path(temporary.name) / "us-paper.sqlite",
                commission_rate=0.0,
                slippage_rate=0.0,
                sec_sell_fee_rate=0.0,
                finra_taf_per_share=0.0,
            )
        )
        self.archive_root = Path(temporary.name) / "decision-archive"

    def test_real_month_end_creates_causal_next_session_period(self) -> None:
        release = dataset()
        history = sessions()[:-1]
        source = FakeSource(
            {"AAPL.US": bars(history), "SPY.US": bars(history, 100.0, 170.0)}
        )
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
            audit_store=USPaperDecisionAuditStore(self.archive_root),
        )

        result = coordinator.decide(DECISION, now=at(DECISION, 16, 15))

        self.assertEqual(result["status"], "PERIOD_CREATED")
        self.assertTrue(result["paper_only"])
        self.assertFalse(result["broker_writes_enabled"])
        self.assertEqual(result["execution_session"], EXECUTION.isoformat())
        self.assertEqual(len(result["signals"]), 1)
        signal = result["signals"][0]
        self.assertEqual(signal["side"], "BUY")
        self.assertEqual(signal["generated_at"], at(DECISION, 16, 15))
        self.assertEqual(signal["available_at"], at(EXECUTION, 9, 20))
        self.assertEqual(signal["valid_until"], at(EXECUTION, 9, 35))
        order = result["period"]["orders"][0]
        self.assertTrue(order["eligible_at"].endswith("09:20:00-05:00"))
        self.assertTrue(order["expires_at"].endswith("09:35:00-05:00"))
        self.assertEqual(result["audit"]["tdx_source"], "TDX")
        self.assertEqual(result["audit"]["signal_emission_adapter"], "US_MOMENTUM_PAPER_ONLY")

    def test_duplicate_month_is_idempotent_without_refetch(self) -> None:
        release = dataset()
        history = sessions()[:-1]
        source = FakeSource(
            {"AAPL.US": bars(history), "SPY.US": bars(history, 100.0, 170.0)}
        )
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
        )
        first = coordinator.decide(DECISION, now=at(DECISION, 16, 15))
        second = coordinator.decide(DECISION, now=at(DECISION, 16, 20))

        self.assertEqual(first["period"]["period_id"], second["period"]["period_id"])
        self.assertEqual(second["status"], "PERIOD_EXISTS")
        self.assertEqual(len(source.calls), 1)

    def test_successful_decision_freezes_complete_replay_bundle(self) -> None:
        release = dataset()
        history = sessions()[:-1]
        source = FakeSource(
            {"AAPL.US": bars(history), "SPY.US": bars(history, 100.0, 170.0)}
        )
        store = USPaperDecisionAuditStore(self.archive_root)
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
            audit_store=store,
        )

        result = coordinator.decide(DECISION, now=at(DECISION, 16, 15))
        archived = store.load(DECISION)

        self.assertEqual("PERIOD_CREATED", result["status"])
        self.assertEqual(
            result["decision_archive"]["bundle_sha256"],
            json.loads(
                (self.archive_root / "periods" / f"{DECISION}.json").read_text()
            )["bundle_sha256"],
        )
        self.assertEqual(release.release_id, archived["release_id"])
        self.assertEqual(
            result["audit"]["front_sha256"], archived["front_sha256"]
        )
        self.assertEqual(
            result["audit"]["raw_sha256"], archived["raw_sha256"]
        )
        self.assertEqual(
            _source_code_sha256(USMomentumStrategy),
            archived["strategy_code_sha256"],
        )
        self.assertEqual(
            _source_code_sha256(USPaperDecisionCoordinator),
            archived["decision_engine_code_sha256"],
        )
        self.assertEqual({}, archived["position_aliases"])
        self.assertEqual(
            {"AAPL.US": APPLE}, archived["security_id_by_code"]
        )

    def test_archive_pointer_identity_tamper_is_rejected(self) -> None:
        release = dataset()
        history = sessions()[:-1]
        source = FakeSource(
            {"AAPL.US": bars(history), "SPY.US": bars(history, 100.0, 170.0)}
        )
        store = USPaperDecisionAuditStore(self.archive_root)
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
            audit_store=store,
        )
        coordinator.decide(DECISION, now=at(DECISION, 16, 15))
        pointer_path = self.archive_root / "periods" / f"{DECISION}.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["release_id"] = "0" * 64
        pointer_path.chmod(0o666)
        pointer_path.write_text(
            json.dumps(
                pointer,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(Exception, "pointer release mismatch"):
            store.load(DECISION)

    def test_qualification_replay_rejects_changed_strategy_code_hash(self) -> None:
        release = dataset()
        history = sessions()[:-1]
        source = FakeSource(
            {"AAPL.US": bars(history), "SPY.US": bars(history, 100.0, 170.0)}
        )
        store = USPaperDecisionAuditStore(self.archive_root)
        result = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
            audit_store=store,
        ).decide(DECISION, now=at(DECISION, 16, 15))
        status = self.paper.status()
        paper = {
            "us_paper_periods": tuple(status["periods"]),
            "us_paper_orders": tuple(status["orders"]),
        }
        cycle = PaperCycleEvidence(
            cycle_id=result["period"]["period_id"],
            decision_session=DECISION,
            execution_session=EXECUTION,
            complete=True,
            replay_verified=True,
        )
        builder = USPaperQualificationEvidenceBuilder(
            paper_database_path=self.archive_root / "unused-paper.sqlite",
            runtime_database_path=self.archive_root / "unused-runtime.sqlite",
            frozen_xnys_sessions=tuple(item.date() for item in sessions()),
            decision_archive_root=self.archive_root,
        )
        failures: list[str] = []
        builder._validate_decision_replays(paper, (cycle,), failures)
        self.assertEqual([], failures)

        tampered_paper = {
            **paper,
            "us_paper_periods": (
                {**paper["us_paper_periods"][0], "signal_hash": "0" * 64},
            ),
        }
        failures = []
        builder._validate_decision_replays(tampered_paper, (cycle,), failures)
        self.assertTrue(
            any("period signal hash does not replay" in item for item in failures),
            failures,
        )

        pointer_path = self.archive_root / "periods" / f"{DECISION}.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        manifest_path = (
            self.archive_root / "objects" / f"{pointer['bundle_sha256']}.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["strategy_code_sha256"] = "0" * 64
        payload = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        (self.archive_root / "objects" / f"{digest}.json").write_bytes(payload)
        pointer["bundle_sha256"] = digest
        pointer_path.chmod(0o666)
        pointer_path.write_text(
            json.dumps(
                pointer,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        failures = []
        builder._validate_decision_replays(paper, (cycle,), failures)
        self.assertTrue(
            any("strategy code hash mismatch" in item for item in failures),
            failures,
        )

    def test_only_real_month_end_and_five_minute_slots_can_fetch(self) -> None:
        release = dataset()
        history = sessions()[:-1]
        source = FakeSource(
            {"AAPL.US": bars(history), "SPY.US": bars(history)}
        )
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
        )

        not_session = coordinator.decide("2025-01-30", now=at(DECISION, 16, 15))
        early = coordinator.decide(DECISION, now=at(DECISION, 16, 14))
        off_slot = coordinator.decide(DECISION, now=at(DECISION, 16, 16))

        self.assertEqual(not_session["status"], "NOT_MONTH_END")
        self.assertEqual(early["status"], "WAITING_CLOSE_DATA")
        self.assertEqual(off_slot["status"], "WAITING_RETRY_SLOT")
        self.assertTrue(off_slot["next_retry_at"].endswith("16:20:00-05:00"))
        self.assertEqual(source.calls, [])

    def test_missing_tdx_raw_bar_fails_closed_and_schedules_retry(self) -> None:
        release = dataset()
        history = sessions()[:-1]
        source = FakeSource(
            {"AAPL.US": bars(history), "SPY.US": bars(history)},
            omit_raw={"AAPL.US"},
        )
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
        )

        result = coordinator.decide(DECISION, now=at(DECISION, 16, 15))

        self.assertEqual(result["status"], "RETRY_SCHEDULED")
        self.assertIn("missing front=[]", result["reason"])
        self.assertEqual(result["next_retry_at"], at(DECISION, 16, 20).isoformat())
        self.assertEqual(self.paper.status()["periods"], [])

    def test_removed_holding_is_fetched_and_forced_to_sell(self) -> None:
        # Establish a genuine paper position before the current month-end.
        prior_generated = at(date(2024, 12, 31), 16, 15)
        prior_execution = date(2025, 1, 2)
        self.paper.create_period(
            [
                {
                    "signal_id": "prior-aapl-buy",
                    "code": "AAPL.US",
                    "side": "BUY",
                    "target_weight": 0.10,
                    "generated_at": prior_generated,
                    "available_at": at(prior_execution, 9, 20),
                    "valid_until": at(prior_execution, 9, 35),
                    "reason_codes": ("TEST_PRIOR_POSITION",),
                    "evidence": {
                        "stop_ratio": 0.08,
                        "security_id": APPLE,
                        "pit_release_id": "c" * 64,
                        "manifest_sha256": MANIFEST_SHA256,
                    },
                }
            ],
            now=prior_generated,
        )
        self.paper.tick(
            prior_execution,
            now=at(prior_execution, 9, 30, 10),
            observations=[
                {
                    "code": "AAPL.US",
                    "session_date": prior_execution,
                    "kind": "OPEN",
                    "event_at": at(prior_execution, 9, 30),
                    "available_at": at(prior_execution, 9, 30),
                    "open": 100.0,
                }
            ],
        )
        release = dataset(members={MICROSOFT}, include_microsoft=True)
        history = sessions()[:-1]
        source = FakeSource(
            {
                "AAPL.US": bars(history),
                "MSFT.US": bars(history, 60.0, 190.0),
                "SPY.US": bars(history, 100.0, 170.0),
            }
        )
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
            audit_store=USPaperDecisionAuditStore(self.archive_root),
        )

        result = coordinator.decide(DECISION, now=at(DECISION, 16, 15))

        self.assertEqual(result["status"], "PERIOD_CREATED")
        self.assertIn("AAPL.US", source.calls[0][0])
        sell = [item for item in result["signals"] if item["code"] == "AAPL.US"]
        self.assertEqual(len(sell), 1)
        self.assertEqual(sell[0]["side"], "SELL")
        self.assertEqual(sell[0]["reason_codes"], ("US_PIT_MEMBERSHIP_REMOVAL",))
        self.assertEqual(result["period"]["orders"][0]["side"], "SELL")
        status = self.paper.status()
        paper = {
            "us_paper_periods": tuple(status["periods"]),
            "us_paper_orders": tuple(status["orders"]),
        }
        failures: list[str] = []
        USPaperQualificationEvidenceBuilder(
            paper_database_path=self.archive_root / "unused-paper.sqlite",
            runtime_database_path=self.archive_root / "unused-runtime.sqlite",
            frozen_xnys_sessions=tuple(item.date() for item in sessions()),
            decision_archive_root=self.archive_root,
        )._validate_decision_replays(
            paper,
            (
                PaperCycleEvidence(
                    cycle_id=result["period"]["period_id"],
                    decision_session=DECISION,
                    execution_session=EXECUTION,
                    complete=True,
                    replay_verified=True,
                ),
            ),
            failures,
        )
        self.assertEqual([], failures)

    def test_persisted_stable_id_allows_atomic_fb_to_meta_alias_migration(self) -> None:
        prior_generated = at(date(2024, 12, 31), 16, 15)
        prior_execution = date(2025, 1, 2)
        self.paper.create_period(
            [
                {
                    "signal_id": "prior-fb-buy",
                    "code": "FB.US",
                    "side": "BUY",
                    "target_weight": 0.10,
                    "generated_at": prior_generated,
                    "available_at": at(prior_execution, 9, 20),
                    "valid_until": at(prior_execution, 9, 35),
                    "reason_codes": ("TEST_PRIOR_POSITION",),
                    "evidence": {
                        "stop_ratio": 0.08,
                        "security_id": APPLE,
                        "pit_release_id": "c" * 64,
                        "manifest_sha256": MANIFEST_SHA256,
                    },
                }
            ],
            now=prior_generated,
        )
        self.paper.tick(
            prior_execution,
            now=at(prior_execution, 9, 30, 10),
            observations=[
                {
                    "code": "FB.US",
                    "session_date": prior_execution,
                    "kind": "OPEN",
                    "event_at": at(prior_execution, 9, 30),
                    "available_at": at(prior_execution, 9, 30),
                    "open": 100.0,
                }
            ],
        )

        release = dataset()
        history = sessions()[:-1]
        release.listing_aliases.drop(release.listing_aliases.index, inplace=True)
        release.listing_aliases.loc[0] = {
            "security_id": APPLE,
            "vendor_code": "FB.US",
            "valid_from": history[0],
            "valid_to": pd.Timestamp("2025-01-15"),
        }
        release.listing_aliases.loc[1] = {
            "security_id": APPLE,
            "vendor_code": "META.US",
            "valid_from": pd.Timestamp("2025-01-16"),
            "valid_to": pd.NaT,
        }
        source = FakeSource(
            {"META.US": bars(history), "SPY.US": bars(history, 100.0, 170.0)}
        )
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
        )

        result = coordinator.decide(DECISION, now=at(DECISION, 16, 15))

        self.assertEqual("PERIOD_CREATED", result["status"])
        self.assertEqual((), tuple(result["signals"]))
        self.assertNotIn("FB.US", source.calls[0][0])
        self.assertIn("META.US", source.calls[0][0])
        status = self.paper.status()
        self.assertEqual(APPLE, status["positions"][0]["security_id"])
        self.assertEqual("META.US", status["positions"][0]["code"])
        self.assertEqual(1, len(status["fills"]))
        self.assertEqual([], result["period"]["orders"])
        self.assertEqual(
            1,
            len(
                [
                    row
                    for row in status["events"]
                    if row["event_type"] == "SECURITY_ALIAS_RENAMED"
                ]
            ),
        )

    def test_persisted_identity_conflict_blocks_alias_migration(self) -> None:
        prior_generated = at(date(2024, 12, 31), 16, 15)
        prior_execution = date(2025, 1, 2)
        self.paper.create_period(
            [
                {
                    "signal_id": "prior-fb-buy-conflict",
                    "code": "FB.US",
                    "side": "BUY",
                    "target_weight": 0.10,
                    "generated_at": prior_generated,
                    "available_at": at(prior_execution, 9, 20),
                    "valid_until": at(prior_execution, 9, 35),
                    "reason_codes": ("TEST_PRIOR_POSITION",),
                    "evidence": {
                        "stop_ratio": 0.08,
                        "security_id": APPLE,
                        "pit_release_id": "c" * 64,
                        "manifest_sha256": MANIFEST_SHA256,
                    },
                }
            ],
            now=prior_generated,
        )
        self.paper.tick(
            prior_execution,
            now=at(prior_execution, 9, 30, 10),
            observations=[
                {
                    "code": "FB.US",
                    "session_date": prior_execution,
                    "kind": "OPEN",
                    "event_at": at(prior_execution, 9, 30),
                    "available_at": at(prior_execution, 9, 30),
                    "open": 100.0,
                }
            ],
        )
        release = dataset()
        history = sessions()[:-1]
        release.listing_aliases.drop(release.listing_aliases.index, inplace=True)
        release.listing_aliases.loc[0] = {
            "security_id": APPLE,
            "vendor_code": "META.US",
            "valid_from": pd.Timestamp("2025-01-16"),
            "valid_to": pd.NaT,
        }
        release.listing_aliases.loc[1] = {
            "security_id": MICROSOFT,
            "vendor_code": "FB.US",
            "valid_from": pd.Timestamp("2025-01-16"),
            "valid_to": pd.NaT,
        }
        source = FakeSource(
            {"META.US": bars(history), "SPY.US": bars(history)}
        )
        coordinator = USPaperDecisionCoordinator(
            dataset=release,
            paper=self.paper,
            bar_source=source,
            manifest_sha256=MANIFEST_SHA256,
        )

        result = coordinator.decide(DECISION, now=at(DECISION, 16, 15))

        self.assertEqual("PAPER_BLOCKED", result["status"])
        self.assertIn("persisted alias FB.US now belongs", result["reason"])
        self.assertEqual([], source.calls)
        self.assertEqual("FB.US", self.paper.status()["positions"][0]["code"])

    def test_tdx_source_requests_front_and_raw_without_cache(self) -> None:
        history = sessions()[:-1]
        provider = FakeProvider(
            {"AAPL.US": bars(history), "SPY.US": bars(history)}
        )
        source = TDXCurrentUSBarSource(
            provider, clock=lambda: at(DECISION, 16, 15)
        )
        bundle = source.fetch(
            ["SPY.US", "AAPL.US"], asof=DECISION, count=1300
        )

        self.assertEqual(bundle.source_id, "TDX")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["dividend_type"], "front")
        self.assertEqual(provider.calls[1]["dividend_type"], "none")
        self.assertEqual(provider.calls[0]["end_time"], "2025-01-31 23:59:59")


if __name__ == "__main__":
    unittest.main()
