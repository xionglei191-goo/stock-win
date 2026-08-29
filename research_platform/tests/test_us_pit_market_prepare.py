from __future__ import annotations

import json
import base64
import hashlib
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import exchange_calendars as xcals
import pandas as pd

from research_platform.us_pit import (
    LicenseClass,
    SourceDependency,
    SourceRole,
    UNIVERSE_ID,
    USPITMarketPreparer,
    USPITService,
    USPITStore,
)
from research_platform.us_pit.hashing import (
    canonical_json_bytes,
    sha256_file,
    sha256_json,
)
from research_platform.us_pit.market_prepare import (
    BENCHMARK_CODES,
    REQUIRED_ARTIFACTS,
    _json_records,
    _raw_rpc_capture_records,
)
from research_platform.us_tdx import TQRawRPCEnvelope
from research_platform.us_pit.quality import REQUIRED_ARTIFACT_COLUMNS


SECURITY_ID = "us_isin_us0378331005"


class FakeHistoricalProvider:
    def __init__(self, values: dict[str, pd.DataFrame]) -> None:
        self.values = values
        self.calls: list[dict[str, Any]] = []

    def fetch_bars(
        self,
        codes: list[str],
        period: str,
        count: int,
        *,
        fields: tuple[str, ...],
        dividend_type: str,
        start_time: str | None = None,
        end_time: str | None = None,
        warmup_bars: int = 0,
    ) -> dict[str, pd.DataFrame]:
        self.calls.append(
            {
                "codes": tuple(codes),
                "period": period,
                "count": count,
                "fields": fields,
                "dividend_type": dividend_type,
                "start_time": start_time,
                "end_time": end_time,
                "warmup_bars": warmup_bars,
            }
        )
        return {
            code: self.values[code].copy()
            for code in codes
            if code in self.values
        }


class USPITMarketPreparerTests(unittest.TestCase):
    start = date(2020, 1, 1)
    end = date(2024, 12, 31)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = USPITStore(self.root / "pit")
        self.calendar = self._frozen_calendar()

    def _frozen_calendar(self) -> pd.DataFrame:
        calendar = xcals.get_calendar("XNYS")
        first = calendar.date_to_session(
            pd.Timestamp(self.start).to_period("M").start_time,
            direction="next",
        )
        warmup = calendar.sessions_window(first, -282)
        end_label = pd.Timestamp(self.end)
        next_label = (
            calendar.next_session(end_label)
            if calendar.is_session(end_label)
            else calendar.date_to_session(end_label, direction="next")
        )
        schedule = calendar.schedule.loc[
            str(pd.Timestamp(warmup[0]).date()) : str(pd.Timestamp(next_label).date())
        ]
        return pd.DataFrame(
            {
                "session_date": pd.DatetimeIndex(schedule.index)
                .tz_localize(None)
                .normalize(),
                "market_open": [
                    pd.Timestamp(value)
                    .tz_convert("America/New_York")
                    .isoformat()
                    for value in schedule["open"]
                ],
                "market_close": [
                    pd.Timestamp(value)
                    .tz_convert("America/New_York")
                    .isoformat()
                    for value in schedule["close"]
                ],
            }
        )

    def _bars(self, offset: float = 0.0) -> pd.DataFrame:
        sessions = pd.DatetimeIndex(self.calendar["session_date"])
        close = pd.Series(
            [100.0 + offset + index * 0.01 for index in range(len(sessions))],
            index=sessions,
        )
        return pd.DataFrame(
            {
                "Open": close - 0.10,
                "High": close + 0.50,
                "Low": close - 0.50,
                "Close": close,
                "Volume": 1_000.0,
                "Amount": close * 1_000.0,
            },
            index=sessions,
        )

    def _provider(self) -> FakeHistoricalProvider:
        return FakeHistoricalProvider(
            {
                "AAPL.US": self._bars(),
                BENCHMARK_CODES["SPY"]: self._bars(10.0),
                BENCHMARK_CODES["BIL"]: self._bars(-10.0),
            }
        )

    def _workspace(
        self,
        name: str,
        *,
        action: dict[str, Any] | None = None,
        action_dependency: SourceDependency | None = None,
        blocked: bool = False,
    ) -> Path:
        container = self.root / name
        container.mkdir()
        normalization_identity = {
            "format_version": "us-pit-official-normalization-v1",
            "source_batch_ids": [],
            "sources": [],
            "artifacts": {},
            "policy": {"fixture": True},
        }
        normalization_id = sha256_json(normalization_identity)
        normalization_root = (
            self.store.root / "normalized" / "official" / normalization_id
        )
        normalization_root.mkdir(parents=True, exist_ok=True)
        normalization_manifest_path = normalization_root / "manifest.json"
        if not normalization_manifest_path.is_file():
            normalization_manifest_path.write_bytes(
                canonical_json_bytes(
                    {
                        **normalization_identity,
                        "normalization_id": normalization_id,
                        "candidate_only": True,
                        "direct_build_allowed": False,
                    }
                )
            )
        frames = {
            dataset: pd.DataFrame(columns=sorted(columns))
            for dataset, columns in REQUIRED_ARTIFACT_COLUMNS.items()
        }
        sessions = pd.DatetimeIndex(self.calendar["session_date"])
        decision_window = sessions[
            (sessions >= pd.Timestamp(self.start))
            & (sessions <= pd.Timestamp(self.end))
        ]
        decisions = [
            pd.Timestamp(group.max()).normalize()
            for _, group in pd.Series(
                decision_window, index=decision_window
            ).groupby(decision_window.to_period("M"))
        ]
        frames["membership_monthly"] = pd.DataFrame(
            [
                {
                    "universe_id": UNIVERSE_ID,
                    "decision_date": decision,
                    "security_id": SECURITY_ID,
                }
                for decision in decisions
            ]
        )
        frames["security_master"] = pd.DataFrame(
            [
                {
                    "security_id": SECURITY_ID,
                    "issuer_id": "us_issuer_apple",
                    "primary_identifier_type": "ISIN",
                    "primary_identifier": "US0378331005",
                    "asset_class": "COMMON_EQUITY",
                }
            ]
        )
        frames["listing_aliases"] = pd.DataFrame(
            [
                {
                    "security_id": SECURITY_ID,
                    "ticker": "AAPL",
                    "vendor_code": "AAPL.US",
                    "exchange": "XNAS",
                    "valid_from": sessions.min(),
                    "valid_to": None,
                }
            ]
        )
        frames["xnys_calendar"] = self.calendar.copy()
        if action is not None:
            frames["corporate_actions"] = pd.DataFrame([action])

        evidence = self.store.put_bytes(b"fixture-membership-evidence")
        base_dependency = SourceDependency(
            source_id="fixture-membership",
            source_version="v1",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=evidence.sha256,
            observed_at="2019-01-01T00:00:00+00:00",
            published_at="2019-01-01T00:00:00+00:00",
            url="https://example.test/membership",
            dataset="membership_events",
            as_of_date="2019-01-01",
        )
        dependencies = [base_dependency]
        if action_dependency is not None:
            dependencies.append(action_dependency)
        batch = self.store.write_source_batch(dependencies)

        blocking_gaps = [{"code": "FIXTURE_BLOCKED"}] if blocked else []
        gap_report = {
            "status": "DATA_BLOCKED" if blocked else "REVIEW_READY",
            "counts": {"FIXTURE_BLOCKED": 1} if blocked else {},
            "blocking_gaps": blocking_gaps,
        }
        manifest_identity = {
            "format_version": "us-pit-reviewed-workspace-v1",
            "normalization_id": normalization_id,
            "normalization_manifest_sha256": sha256_file(
                normalization_manifest_path
            ),
            "source_batch_ids": [batch.batch_id],
            "decision_start": pd.Timestamp(decisions[0]).date().isoformat(),
            "decision_end": pd.Timestamp(decisions[-1]).date().isoformat(),
            "review_inputs": {},
            "artifacts": {
                dataset: sha256_json(_json_records(frame))
                for dataset, frame in frames.items()
            },
            "gap_report_sha256": sha256_json(gap_report),
        }
        workspace_id = sha256_json(manifest_identity)
        root = container / workspace_id
        root.mkdir()
        for dataset, frame in frames.items():
            frame.to_parquet(root / f"{dataset}.parquet", index=False)
        manifest = {
            **manifest_identity,
            "workspace_id": workspace_id,
            "status": "DATA_BLOCKED" if blocked else "REVIEW_READY",
            "direct_build_allowed": not blocked,
            "universe_id": UNIVERSE_ID,
        }
        (root / "gap_report.json").write_bytes(canonical_json_bytes(gap_report))
        manifest_bytes = canonical_json_bytes(manifest)
        (root / "manifest.json").write_bytes(manifest_bytes)
        manifest_object = self.store.put_bytes(
            manifest_bytes,
            media_type="application/vnd.us-pit.reviewed-workspace-manifest+json",
        )
        (root / "manifest.cas.json").write_bytes(
            canonical_json_bytes(
                {
                    "workspace_id": workspace_id,
                    "manifest_sha256": manifest_object.sha256,
                    "manifest_size_bytes": manifest_object.size_bytes,
                    "cas_object_sha256": manifest_object.sha256,
                }
            )
        )
        return root

    def _action_dependency(
        self,
        observed_at: str = "2022-12-15T12:00:00+00:00",
        *,
        published_at: str | None = None,
        publication_verified: bool = False,
    ) -> SourceDependency:
        evidence = self.store.put_bytes(b"fixture-corporate-action-evidence")
        return SourceDependency(
            source_id="fixture-actions",
            source_version="v1",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=evidence.sha256,
            observed_at=observed_at,
            published_at=published_at or observed_at,
            url="https://example.test/actions",
            dataset="corporate_actions",
            as_of_date="2022-12-15",
            metadata=(
                {
                    "publication_time_from_payload": True,
                    "accepted_at_verified_in_payload": True,
                    "accepted_at": published_at or observed_at,
                }
                if publication_verified
                else {}
            ),
        )

    def _prepare(
        self,
        input_dir: Path,
        output_name: str,
        provider: FakeHistoricalProvider,
    ):
        return USPITMarketPreparer(
            self.store,
            provider,
            tdx_source_version="fixture-tdx-v1",
            clock=lambda: datetime(2025, 1, 3, tzinfo=timezone.utc),
            allow_test_fee_evidence=True,
            allow_test_provider_capture=True,
        ).prepare(
            input_dir,
            self.root / output_name,
            start_date=self.start,
            end_date=self.end,
        )

    def test_raw_rpc_capture_preserves_exact_bytes_and_hashes(self) -> None:
        request = b'{"method":"get_market_data","params":{"fill_data":false}}'
        response = (
            b'{"result":{"Value":{"AAPL.US":{"Date":["20240102","20240102"],'
            b'"Open":[null,100.0],"Close":[99.0,101.0]}}}}'
        )
        fetched_at = datetime(2025, 1, 3, 12, 0, tzinfo=timezone.utc)
        records = _raw_rpc_capture_records(
            (
                TQRawRPCEnvelope(
                    method="get_market_data",
                    request_bytes=request,
                    response_bytes=response,
                    fetched_at=fetched_at,
                    value={},
                ),
            )
        )

        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual(request, base64.b64decode(record["request_base64"]))
        self.assertEqual(response, base64.b64decode(record["response_base64"]))
        self.assertEqual(hashlib.sha256(request).hexdigest(), record["request_sha256"])
        self.assertEqual(hashlib.sha256(response).hexdigest(), record["response_sha256"])
        self.assertEqual(fetched_at.isoformat(), record["received_at"])
        self.assertIn('"Open":[null,100.0]', record["response_utf8"])

    def test_ready_workspace_is_complete_auditable_and_directly_buildable(self) -> None:
        provider = self._provider()
        result = self._prepare(self._workspace("reviewed"), "complete", provider)

        self.assertTrue(result.ready, [item.to_dict() for item in result.gaps])
        self.assertEqual({"none", "front"}, {call["dividend_type"] for call in provider.calls})
        self.assertTrue(all(call["warmup_bars"] == 0 for call in provider.calls))
        self.assertEqual(
            pd.Timestamp(self.calendar["session_date"].min()).date().isoformat(),
            provider.calls[0]["start_time"],
        )
        self.assertEqual(
            set(REQUIRED_ARTIFACTS),
            {path.stem for path in result.output_dir.glob("*.parquet")},
        )
        manifest = json.loads(
            (result.output_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(REQUIRED_ARTIFACTS), set(manifest["artifacts"]))
        self.assertEqual([result.source_batch.batch_id], manifest["source_batch_ids"])
        dependencies = result.source_batch.dependencies
        benchmark = next(
            item for item in dependencies if item.dataset == "benchmark_total_return"
        )
        raw = next(item for item in dependencies if item.dataset == "bars_raw")
        self.assertNotEqual(raw.object_sha256, benchmark.object_sha256)
        for dataset in (
            "bars_raw",
            "bars_vendor_front",
            "benchmark_total_return",
            "xnys_calendar",
            "execution_fee_schedule",
        ):
            dependency = next(item for item in dependencies if item.dataset == dataset)
            self.assertEqual(64, len(dependency.metadata["normalized_artifact_sha256"]))
        release = USPITService(self.store).build_from_directory(
            result.output_dir,
            source_batch_ids=[result.source_batch.batch_id],
        )
        self.assertIsNotNone(release.release_id)

    def test_incomplete_stock_history_is_scope_c_excluded_without_fill(self) -> None:
        provider = self._provider()
        next_session = pd.Timestamp(self.calendar["session_date"].max())
        provider.values["AAPL.US"] = provider.values["AAPL.US"].drop(next_session)
        saturday = pd.Timestamp("2024-12-28")
        provider.values["AAPL.US"].loc[saturday] = provider.values["AAPL.US"].iloc[-1]

        result = self._prepare(self._workspace("reviewed-gaps"), "scope-excluded", provider)

        self.assertTrue(result.ready, [item.to_dict() for item in result.gaps])
        raw = pd.read_parquet(result.output_dir / "bars_raw.parquet")
        self.assertNotIn(SECURITY_ID, set(raw["security_id"].astype(str)))
        membership = pd.read_parquet(
            result.output_dir / "membership_monthly.parquet"
        )
        self.assertNotIn(
            SECURITY_ID, set(membership["security_id"].astype(str))
        )
        report = json.loads(
            (result.output_dir / "market_prepare_report.json").read_text("utf-8")
        )
        exclusion = next(
            item
            for item in report["excluded_market_data"]
            if item.get("vendor_code") == "AAPL.US"
        )
        self.assertEqual("SCOPE-C-QUALITY-v1", exclusion["rule_version"])
        self.assertIn("RAW_REQUIRED_SESSION_MISSING", exclusion["reason_counts"])
        release = USPITService(self.store).build_from_directory(
            result.output_dir,
            source_batch_ids=[result.source_batch.batch_id],
        )
        self.assertEqual(
            1, release.quality_report.metrics["scope_c_excluded_security_count"]
        )
        self.assertEqual(
            sha256_json([SECURITY_ID]),
            release.quality_report.metrics["scope_c_exclusion_set_sha256"],
        )
        self.assertNotIn(
            "INCOMPLETE_BAR_COVERAGE",
            {item.code for item in release.quality_report.issues},
        )

    def test_benchmark_history_cannot_be_scope_c_excluded(self) -> None:
        provider = self._provider()
        next_session = pd.Timestamp(self.calendar["session_date"].max())
        provider.values["SPY.US"] = provider.values["SPY.US"].drop(next_session)

        result = self._prepare(
            self._workspace("reviewed-benchmark-gap"),
            "blocked-benchmark-gap",
            provider,
        )

        self.assertEqual("DATA_BLOCKED", result.status)
        self.assertIn("BENCHMARK_SESSION_MISSING", {item.code for item in result.gaps})

    def test_split_is_causal_and_unverified_terms_block(self) -> None:
        dependency = self._action_dependency("2023-01-03T12:00:00+00:00")
        action = {
            "action_id": "aapl-split-fixture",
            "security_id": SECURITY_ID,
            "action_type": "SPLIT",
            "announced_at": "2023-01-03T12:00:00+00:00",
            "effective_at": "2023-01-03T23:30:00-05:00",
            "pay_date": None,
            "terms_verified": True,
            "source_id": dependency.source_id,
            "evidence_sha256": dependency.object_sha256,
            "split_ratio": 2.0,
        }
        baseline = self._prepare(self._workspace("reviewed-base"), "base", self._provider())
        adjusted = self._prepare(
            self._workspace(
                "reviewed-action",
                action=action,
                action_dependency=dependency,
            ),
            "adjusted",
            self._provider(),
        )
        self.assertTrue(adjusted.ready, [item.to_dict() for item in adjusted.gaps])
        plain = pd.read_parquet(baseline.output_dir / "bars_pit_signal.parquet")
        signal = pd.read_parquet(adjusted.output_dir / "bars_pit_signal.parquet")
        before_decision = pd.Timestamp("2022-12-30")
        pd.testing.assert_frame_equal(
            plain.loc[plain["decision_date"].eq(before_decision)].reset_index(drop=True),
            signal.loc[signal["decision_date"].eq(before_decision)].reset_index(drop=True),
        )
        after_decision = pd.Timestamp("2023-01-31")
        historical_day = pd.Timestamp("2022-12-30")
        plain_close = plain.loc[
            plain["decision_date"].eq(after_decision)
            & plain["date"].eq(historical_day),
            "Close",
        ].iloc[0]
        adjusted_close = signal.loc[
            signal["decision_date"].eq(after_decision)
            & signal["date"].eq(historical_day),
            "Close",
        ].iloc[0]
        self.assertAlmostEqual(plain_close / 2.0, adjusted_close)
        effective_day = pd.Timestamp("2023-01-03")
        plain_effective_close = plain.loc[
            plain["decision_date"].eq(after_decision)
            & plain["date"].eq(effective_day),
            "Close",
        ].iloc[0]
        adjusted_effective_close = signal.loc[
            signal["decision_date"].eq(after_decision)
            & signal["date"].eq(effective_day),
            "Close",
        ].iloc[0]
        self.assertAlmostEqual(plain_effective_close, adjusted_effective_close)

        action["terms_verified"] = False
        blocked = self._prepare(
            self._workspace(
                "reviewed-unverified",
                action=action,
                action_dependency=dependency,
            ),
            "blocked-action",
            self._provider(),
        )
        self.assertEqual("DATA_BLOCKED", blocked.status)
        self.assertIn(
            "UNVERIFIED_SIGNAL_CORPORATE_ACTION",
            {item.code for item in blocked.gaps},
        )

    def test_split_successor_identity_inherits_and_adjusts_predecessor_history(self) -> None:
        old_id = "us_cusip_86800u104"
        new_id = "us_cusip_86800u302"
        effective = pd.Timestamp("2024-10-01")
        decision = pd.Timestamp("2024-10-31")
        sessions = pd.DatetimeIndex(self.calendar["session_date"])
        old_rows = self._bars().loc[lambda frame: frame.index < effective].tail(40)
        new_rows = self._bars(5.0).loc[
            lambda frame: (frame.index >= effective) & (frame.index <= decision)
        ]
        raw = pd.concat(
            [
                old_rows.assign(security_id=old_id, date=old_rows.index),
                new_rows.assign(security_id=new_id, date=new_rows.index),
            ],
            ignore_index=True,
        )
        memberships = pd.DataFrame(
            [{"decision_date": decision, "security_id": new_id}]
        )
        evidence = self.store.put_bytes(b"verified split successor evidence")
        dependency = SourceDependency(
            source_id="fixture-actions",
            source_version="v1",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=evidence.sha256,
            observed_at="2024-09-26T20:00:00+00:00",
            published_at="2024-09-26T20:00:00+00:00",
            url="https://example.test/split-successor",
            dataset="corporate_actions",
        )
        actions = pd.DataFrame(
            [{
                "action_id": "split-successor",
                "security_id": old_id,
                "successor_security_id": new_id,
                "action_type": "SPLIT",
                "announced_at": "2024-09-26T16:00:00-04:00",
                "effective_at": "2024-10-01T09:30:00-04:00",
                "terms_verified": True,
                "source_id": dependency.source_id,
                "evidence_sha256": dependency.object_sha256,
                "split_ratio": 10.0,
            }]
        )
        gaps = []
        signal = USPITMarketPreparer._build_pit_signal_bars(
            raw,
            memberships,
            actions,
            self.calendar,
            (dependency,),
            gaps,
        )

        self.assertEqual([], [item.to_dict() for item in gaps])
        self.assertEqual({new_id}, set(signal["security_id"]))
        predecessor_day = old_rows.index[-1]
        inherited = signal.loc[signal["date"].eq(predecessor_day), "Close"].iloc[0]
        self.assertAlmostEqual(float(old_rows.loc[predecessor_day, "Close"]) / 10, inherited)
        self.assertTrue(set(new_rows.index).issubset(set(signal["date"])))

    def test_verified_publication_time_survives_later_local_capture(self) -> None:
        dependency = self._action_dependency(
            "2025-01-03T12:00:00+00:00",
            published_at="2023-01-03T12:00:00+00:00",
            publication_verified=True,
        )
        action = {
            "action_id": "verified-publication-split",
            "security_id": SECURITY_ID,
            "action_type": "SPLIT",
            "announced_at": "2023-01-03T12:00:00+00:00",
            "effective_at": "2023-01-04T09:30:00-05:00",
            "pay_date": None,
            "terms_verified": True,
            "source_id": dependency.source_id,
            "evidence_sha256": dependency.object_sha256,
            "split_ratio": 2.0,
        }
        result = self._prepare(
            self._workspace(
                "verified-publication",
                action=action,
                action_dependency=dependency,
            ),
            "verified-publication-output",
            self._provider(),
        )
        self.assertTrue(result.ready, [item.to_dict() for item in result.gaps])

    def test_unverified_late_capture_remains_blocking(self) -> None:
        dependency = self._action_dependency(
            "2025-01-03T12:00:00+00:00",
            published_at="2023-01-03T12:00:00+00:00",
            publication_verified=False,
        )
        action = {
            "action_id": "unverified-publication-split",
            "security_id": SECURITY_ID,
            "action_type": "SPLIT",
            "announced_at": "2023-01-03T12:00:00+00:00",
            "effective_at": "2023-01-04T09:30:00-05:00",
            "pay_date": None,
            "terms_verified": True,
            "source_id": dependency.source_id,
            "evidence_sha256": dependency.object_sha256,
            "split_ratio": 2.0,
        }
        result = self._prepare(
            self._workspace(
                "unverified-publication",
                action=action,
                action_dependency=dependency,
            ),
            "unverified-publication-output",
            self._provider(),
        )
        self.assertEqual("DATA_BLOCKED", result.status)
        self.assertIn(
            "CORPORATE_ACTION_EVIDENCE_LATE",
            {item.code for item in result.gaps},
        )

    def test_future_split_does_not_change_any_historical_signal(self) -> None:
        dependency = self._action_dependency()
        future_action = {
            "action_id": "future-aapl-split",
            "security_id": SECURITY_ID,
            "action_type": "SPLIT",
            "announced_at": "2022-12-15T12:00:00+00:00",
            "effective_at": "2025-01-15T14:30:00+00:00",
            "pay_date": None,
            "terms_verified": True,
            "source_id": dependency.source_id,
            "evidence_sha256": dependency.object_sha256,
            "split_ratio": 4.0,
        }
        plain = self._prepare(
            self._workspace("future-plain"), "future-plain-output", self._provider()
        )
        future = self._prepare(
            self._workspace(
                "future-action",
                action=future_action,
                action_dependency=dependency,
            ),
            "future-action-output",
            self._provider(),
        )

        self.assertTrue(future.ready, [item.to_dict() for item in future.gaps])
        pd.testing.assert_frame_equal(
            pd.read_parquet(plain.output_dir / "bars_pit_signal.parquet"),
            pd.read_parquet(future.output_dir / "bars_pit_signal.parquet"),
        )

    def test_blocked_input_workspace_cannot_become_market_ready(self) -> None:
        provider = self._provider()
        result = self._prepare(
            self._workspace("reviewed-blocked", blocked=True),
            "still-blocked",
            provider,
        )
        self.assertEqual("DATA_BLOCKED", result.status)
        self.assertIn("INPUT_REVIEW_WORKSPACE_BLOCKED", {item.code for item in result.gaps})
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        self.assertGreater(report["upstream_review_gaps"]["total"], 0)
        self.assertEqual(
            {"FIXTURE_BLOCKED": 1},
            report["upstream_review_gaps"]["counts"],
        )
        self.assertEqual([], provider.calls)
        release = USPITService(self.store).build_from_directory(
            result.output_dir,
            source_batch_ids=[result.source_batch.batch_id],
        )
        self.assertEqual("DATA_BLOCKED", release.status.value)
        self.assertFalse(release.quality_report.includes_delisted)

    def test_forged_review_manifest_is_structurally_blocked(self) -> None:
        workspace = self._workspace("reviewed-forged")
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["normalization_id"] = "f" * 64
        identity = {
            key: manifest[key]
            for key in (
                "format_version",
                "normalization_id",
                "normalization_manifest_sha256",
                "source_batch_ids",
                "decision_start",
                "decision_end",
                "review_inputs",
                "artifacts",
                "gap_report_sha256",
            )
        }
        manifest["workspace_id"] = sha256_json(identity)
        forged_root = workspace.parent / manifest["workspace_id"]
        workspace.rename(forged_root)
        manifest_path = forged_root / "manifest.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest))

        result = self._prepare(forged_root, "forged-output", self._provider())

        self.assertEqual("DATA_BLOCKED", result.status)
        self.assertIn(
            "REVIEW_WORKSPACE_MANIFEST_INVALID",
            {item.code for item in result.gaps},
        )

    def test_missing_inherited_cas_object_is_structurally_blocked(self) -> None:
        workspace = self._workspace("reviewed-cas-missing")
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        batch = self.store.load_source_batch(manifest["source_batch_ids"][0])
        object_path = self.store.object_path(batch.dependencies[0].object_sha256)
        object_path.chmod(0o600)
        object_path.unlink()

        result = self._prepare(workspace, "cas-missing-output", self._provider())

        self.assertEqual("DATA_BLOCKED", result.status)
        self.assertIn(
            "REVIEW_WORKSPACE_SOURCE_OBJECT_INVALID",
            {item.code for item in result.gaps},
        )


if __name__ == "__main__":
    unittest.main()
