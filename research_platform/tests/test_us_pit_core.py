from __future__ import annotations

import stat
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

from research_platform.us_pit import (
    EvidenceAuthority,
    EvidenceReference,
    LicenseClass,
    OverrideProposal,
    QualityPolicy,
    ReleaseStatus,
    SourceArtifact,
    SourceDependency,
    SourceRole,
    StaticSourceAdapter,
    SyncRequest,
    UNIVERSE_ID,
    USPITQualityValidator,
    USPITService,
    USPITStore,
)
from research_platform.us_pit.hashing import sha256_json
from research_platform.us_pit.quality import (
    REQUIRED_ARTIFACT_COLUMNS,
    frame_derivation_sha256,
)


SECURITY_ID = "us_isin_us0378331005"
DECISIONS = (pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-29"))


def _calendar_frame() -> pd.DataFrame:
    schedule = xcals.get_calendar("XNYS").schedule.loc["2024-01-26":"2024-03-01"]
    return pd.DataFrame(
        {
            "session_date": pd.DatetimeIndex(schedule.index).tz_localize(None).normalize(),
            "market_open": [pd.Timestamp(value).isoformat() for value in schedule["open"]],
            "market_close": [pd.Timestamp(value).isoformat() for value in schedule["close"]],
        }
    )


def _fee_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "effective_from": "2020-01-01",
                "effective_to": None,
                "commission_rate": 0.0005,
                "min_commission": 0.0,
                "slippage_rate": 0.0005,
                "sec_sell_fee_rate": 0.0,
                "finra_taf_per_share": 0.000166,
                "finra_taf_cap": 8.30,
                "fee_model_id": "us_equity_effective_fees_v1",
                "sec_evidence_url": "https://www.sec.gov/rules-regulations/fee-rate-advisories",
                "finra_evidence_url": "https://www.finra.org/rules-guidance/rule-filings/fee-schedule",
                "sec_evidence_sha256": "",
                "finra_evidence_sha256": "",
                "fee_derivation_sha256": "d" * 64,
            }
        ]
    )


def _empty_artifact(name: str) -> pd.DataFrame:
    return pd.DataFrame(columns=sorted(REQUIRED_ARTIFACT_COLUMNS[name]))


def _bars(identifier_column: str, identifier: str, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            identifier_column: [identifier] * len(sessions),
            "date": sessions,
            "Open": [100.0] * len(sessions),
            "High": [101.0] * len(sessions),
            "Low": [99.0] * len(sessions),
            "Close": [100.0] * len(sessions),
            "Volume": [1_000.0] * len(sessions),
        }
    )


def _ready_artifacts(sources: list[SourceDependency]) -> dict[str, pd.DataFrame]:
    signal_holdings = next(
        item
        for item in sources
        if item.dataset == "fund_holdings_observed"
        and item.role == SourceRole.SIGNAL_INPUT
    )
    validation_anchor = next(
        item
        for item in sources
        if item.dataset == "fund_holdings_observed"
        and item.role == SourceRole.VALIDATION_ANCHOR
    )
    benchmark_source = next(
        item for item in sources if item.dataset == "benchmark_total_return"
    )
    calendar = _calendar_frame()
    sessions = pd.DatetimeIndex(calendar["session_date"])
    decisions = DECISIONS
    raw = _bars("security_id", SECURITY_ID, sessions)
    signal_parts: list[pd.DataFrame] = []
    for decision in decisions:
        part = raw.loc[raw["date"] <= decision].copy()
        part.insert(0, "decision_date", decision)
        signal_parts.append(part)
    benchmarks = pd.concat(
        [_bars("symbol", symbol, sessions) for symbol in ("SPY", "BIL")],
        ignore_index=True,
    )
    benchmarks["adjustment"] = "none"
    benchmarks["TotalReturnClose"] = 100.0
    benchmarks["total_return_source_id"] = benchmark_source.source_id
    benchmarks["total_return_evidence_sha256"] = benchmark_source.object_sha256

    raw_source = next(item for item in sources if item.dataset == "bars_raw")
    raw_source.metadata["normalized_artifact_sha256"] = frame_derivation_sha256(raw)
    front_source = next(item for item in sources if item.dataset == "bars_vendor_front")
    front_source.metadata["normalized_artifact_sha256"] = frame_derivation_sha256(raw)
    sec_fee_source = next(item for item in sources if item.dataset == "regulatory_fee_sec")
    finra_fee_source = next(
        item for item in sources if item.dataset == "regulatory_fee_finra"
    )
    fees = _fee_frame()
    fees["sec_evidence_sha256"] = sec_fee_source.object_sha256
    fees["finra_evidence_sha256"] = finra_fee_source.object_sha256
    fee_source = next(item for item in sources if item.dataset == "execution_fee_schedule")
    fee_source.metadata["normalized_artifact_sha256"] = frame_derivation_sha256(fees)

    def holding_row(source: SourceDependency) -> dict[str, object]:
        return {
            "as_of_date": source.as_of_date,
            "published_at": source.published_at,
            "observed_at": source.observed_at,
            "url": source.url,
            "source_version": source.source_version,
            "content_sha256": source.object_sha256,
            "evidence_role": source.role.value,
            "security_id": SECURITY_ID,
        }

    return {
        "fund_holdings_observed": pd.DataFrame(
            [holding_row(signal_holdings), holding_row(validation_anchor)]
        ),
        "membership_events": _empty_artifact("membership_events"),
        "membership_monthly": pd.DataFrame(
            [
                {
                    "universe_id": UNIVERSE_ID,
                    "decision_date": decision,
                    "security_id": SECURITY_ID,
                }
                for decision in decisions
            ]
        ),
        "security_master": pd.DataFrame(
            [
                {
                    "security_id": SECURITY_ID,
                    "issuer_id": "apple-inc",
                    "primary_identifier_type": "ISIN",
                    "primary_identifier": "US0378331005",
                    "asset_class": "COMMON_EQUITY",
                }
            ]
        ),
        "identifiers": pd.DataFrame(
            [
                {
                    "security_id": SECURITY_ID,
                    "identifier_type": "ISIN",
                    "identifier_value": "US0378331005",
                    "valid_from": "1980-12-12",
                    "valid_to": None,
                }
            ]
        ),
        "listing_aliases": pd.DataFrame(
            [
                {
                    "security_id": SECURITY_ID,
                    "ticker": "AAPL",
                    "vendor_code": "AAPL.US",
                    "exchange": "XNAS",
                    "valid_from": "1980-12-12",
                    "valid_to": None,
                }
            ]
        ),
        "corporate_actions": _empty_artifact("corporate_actions"),
        "session_exceptions": _empty_artifact("session_exceptions"),
        "bars_raw": raw,
        "bars_vendor_front": raw.copy(),
        "bars_pit_signal": pd.concat(signal_parts, ignore_index=True),
        "benchmarks": benchmarks,
        "xnys_calendar": calendar,
        "execution_fee_schedule": fees,
        "bar_coverage": pd.DataFrame(
            [
                {
                    "decision_date": decision,
                    "security_id": SECURITY_ID,
                    "expected_sessions": int((sessions <= decision).sum()),
                    "raw_sessions": int((sessions <= decision).sum()),
                    "signal_sessions": int((sessions <= decision).sum()),
                    "explained_missing_sessions": 0,
                    "passed": True,
                }
                for decision in decisions
            ]
        ),
        "anchor_reconciliations": pd.DataFrame(
            [
                {
                    "anchor_date": "2024-01-31",
                    "status": "RECONCILED",
                    "unexplained_additions": 0,
                    "unexplained_removals": 0,
                    "source_id": validation_anchor.source_id,
                    "evidence_sha256": validation_anchor.object_sha256,
                }
            ]
        ),
        "lifecycle_reconciliations": pd.DataFrame(
            [
                {
                    "scope": "SECURITY",
                    "coverage_kind": "STATUS_SURVEILLANCE",
                    "current_through": "2024-02-29",
                    "action_id": None,
                    "security_id": SECURITY_ID,
                    "status": "RECONCILED",
                    # Kept for wire compatibility; quality derives the value.
                    "includes_delisted": False,
                    "source_id": next(
                        item.source_id for item in sources if item.dataset == "lifecycle_status"
                    ),
                    "evidence_sha256": next(
                        item.object_sha256 for item in sources if item.dataset == "lifecycle_status"
                    ),
                }
            ]
        ),
    }


class USPITCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = USPITStore(Path(self.temporary.name) / "us_pit")
        self.validator = USPITQualityValidator(
            QualityPolicy(min_decision_months=2, min_warmup_sessions=3)
        )
        self.service = USPITService(self.store, self.validator)

    def _source(
        self,
        *,
        license_class: LicenseClass = LicenseClass.OFFICIAL_PUBLIC,
        dataset: str = "membership_events",
        role: SourceRole = SourceRole.SIGNAL_INPUT,
        source_id: str = "official-fixture",
        observed_at: str = "2024-03-02T00:00:00+00:00",
        published_at: str | None = "2024-03-01T00:00:00+00:00",
        as_of_date: str | None = None,
        url: str = "https://official.example/source",
        metadata: dict[str, object] | None = None,
        payload: bytes | None = None,
    ) -> SourceDependency:
        reference = self.store.put_bytes(
            payload or f"{source_id}:{dataset}:{role.value}:{as_of_date}".encode()
        )
        return SourceDependency(
            source_id=source_id,
            source_version="1",
            role=role,
            license_class=license_class,
            object_sha256=reference.sha256,
            observed_at=observed_at,
            url=url,
            dataset=dataset,
            as_of_date=as_of_date,
            published_at=published_at,
            metadata=dict(metadata or {}),
        )

    def _sources(
        self,
        *,
        membership_license: LicenseClass = LicenseClass.OFFICIAL_PUBLIC,
    ) -> list[SourceDependency]:
        calendar = _calendar_frame()
        fees = _fee_frame()
        values = [
            self._source(
                license_class=membership_license,
                dataset="fund_holdings_observed",
                source_id="ishares-observed",
                observed_at="2024-01-31T18:00:00+00:00",
                published_at="2024-01-31T17:00:00+00:00",
                as_of_date="2024-01-31",
                url="https://official.example/ivv-signal",
            ),
            self._source(
                license_class=LicenseClass.LOCAL_VENDOR,
                dataset="bars_raw",
                source_id="tdx",
            ),
            self._source(
                dataset="fund_holdings_observed",
                role=SourceRole.VALIDATION_ANCHOR,
                source_id="sec",
                observed_at="2024-03-02T00:00:00+00:00",
                published_at="2024-02-15T00:00:00+00:00",
                as_of_date="2024-01-31",
                url="https://www.sec.gov/ivv-nport",
            ),
            self._source(
                dataset="benchmark_total_return",
                source_id="benchmark-official",
                observed_at="2024-03-02T00:00:00+00:00",
                published_at="2024-03-01T00:00:00+00:00",
            ),
            self._source(
                dataset="bars_vendor_front",
                source_id="tdx-front",
                license_class=LicenseClass.LOCAL_VENDOR,
            ),
            self._source(
                dataset="xnys_calendar",
                source_id="exchange-calendars-xnys",
                license_class=LicenseClass.PERMISSIVE,
                metadata={
                    "normalized_artifact_sha256": frame_derivation_sha256(calendar),
                    "calendar": "XNYS",
                },
            ),
            self._source(
                dataset="regulatory_fee_sec",
                source_id="sec-fee-raw",
                role=SourceRole.VALIDATION_ANCHOR,
                url="https://www.sec.gov/rules-regulations/fee-rate-advisories",
                metadata={
                    "fee_evidence_contract_version": 2,
                    "rate_entries": [
                        {
                            "effective_from": "2019-04-16",
                            "sec_sell_fee_rate": 0.0,
                        }
                    ],
                },
            ),
            self._source(
                dataset="regulatory_fee_finra",
                source_id="finra-fee-raw",
                role=SourceRole.VALIDATION_ANCHOR,
                url="https://www.finra.org/rules-guidance/rule-filings/fee-schedule",
                metadata={
                    "fee_evidence_contract_version": 2,
                    "rate_entries": [
                        {
                            "effective_from": "2020-01-01",
                            "finra_taf_per_share": 0.000166,
                            "finra_taf_cap": 8.30,
                        }
                    ],
                },
            ),
            self._source(
                dataset="execution_fee_schedule",
                source_id="sec-finra-fees",
                metadata={
                    "normalized_artifact_sha256": frame_derivation_sha256(fees),
                    "sec_url": "https://www.sec.gov/rules-regulations/fee-rate-advisories",
                    "finra_url": "https://www.finra.org/rules-guidance/rule-filings/fee-schedule",
                    "fee_evidence_contract_version": 2,
                },
            ),
            self._source(
                dataset="lifecycle_status",
                source_id="official-lifecycle-status",
                payload=b"lifecycle-summary-placeholder",
            ),
        ]
        raw_lifecycle = self._source(
            dataset="lifecycle_observation",
            source_id="sec-lifecycle-source",
            role=SourceRole.SIGNAL_INPUT,
            observed_at="2024-03-01T01:00:00+00:00",
            published_at="2024-03-01T00:00:00+00:00",
            url="https://www.sec.gov/Archives/lifecycle-fixture",
            payload=b"official lifecycle source fixture US0378331005",
        )
        records = [
            {
                "source_id": raw_lifecycle.source_id,
                "dataset": raw_lifecycle.dataset,
                "evidence_sha256": raw_lifecycle.object_sha256,
                "published_at": raw_lifecycle.published_at,
                "url": raw_lifecycle.url,
                "observations": [
                    {
                        "security_id": SECURITY_ID,
                        "identifier_type": "ISIN",
                        "identifier_value": SECURITY_ID.removeprefix("us_isin_").upper(),
                        "observed_status": "LISTED",
                        "evidence_locator": "fixture:1",
                        "observed_through": "2024-02-29",
                        "status_effective_at": "",
                        "evidence_excerpt": "official lifecycle source fixture",
                    }
                ],
            }
        ]
        lifecycle = values[-1]
        lifecycle.metadata.update(
            {
                "coverage_contract_version": 3,
                "coverage_kind": "TERMINATION_SURVEILLANCE",
                "current_through": "2024-02-29",
                "covered_security_ids": [SECURITY_ID],
                "covered_security_ids_sha256": sha256_json([SECURITY_ID]),
                "covered_security_count": 1,
                "source_records": records,
                "source_records_sha256": sha256_json(records),
                "source_record_count": 1,
                "source_dependency_object_sha256s": [raw_lifecycle.object_sha256],
                "coverage_derived_from_payload": True,
                "source_records_bound_to_cas": True,
                "observation_identifiers_verified_in_payload": True,
            }
        )
        values.append(raw_lifecycle)
        return values

    def test_ready_release_is_deterministic_and_loads_stable_id_dataset(self) -> None:
        sources = self._sources()
        first = self.service.build(
            _ready_artifacts(sources),
            sources=sources,
            created_at=datetime(2024, 3, 2, tzinfo=timezone.utc),
        )
        second = self.service.build(
            _ready_artifacts(sources),
            sources=sources,
            created_at=datetime(2024, 3, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(first.release_id, second.release_id)
        self.assertEqual(first.status, ReleaseStatus.DATA_READY)
        self.assertTrue(first.includes_delisted)
        self.assertEqual(self.service.validate(first.release_id).issues, ())
        dataset = first.to_backtest_dataset()
        self.assertEqual(dataset.members("2024-02-20"), frozenset({SECURITY_ID}))
        self.assertEqual(dataset.vendor_code(SECURITY_ID, "2024-02-20"), "AAPL.US")
        self.assertEqual(dataset.signal_bars("2024-02-20")[SECURITY_ID].index.max(), pd.Timestamp("2024-01-31"))
        self.assertAlmostEqual(dataset.fee_at("2024-02-20")["commission_rate"], 0.0005)
        self.assertAlmostEqual(dataset.fee_at("2024-02-20")["finra_taf_cap"], 8.30)

    def test_missing_artifacts_and_unattested_delistings_fail_closed(self) -> None:
        missing = self.service.build({}, sources=self._sources())
        self.assertEqual(missing.status, ReleaseStatus.DATA_BLOCKED)
        self.assertFalse(missing.includes_delisted)
        self.assertIn("MISSING_ARTIFACT", {issue.code for issue in missing.quality_report.issues})
        with self.assertRaisesRegex(ValueError, "not DATA_READY"):
            missing.to_backtest_dataset()

        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        artifacts["lifecycle_reconciliations"].loc[0, "includes_delisted"] = False
        derived = self.service.build(artifacts, sources=sources)
        self.assertEqual(derived.status, ReleaseStatus.DATA_READY)
        self.assertTrue(derived.includes_delisted)

        artifacts["lifecycle_reconciliations"] = _empty_artifact(
            "lifecycle_reconciliations"
        )
        unattested = self.service.build(artifacts, sources=sources)
        self.assertEqual(unattested.status, ReleaseStatus.DATA_BLOCKED)
        self.assertFalse(unattested.includes_delisted)

    def test_unlicensed_dependency_and_signal_time_travel_are_blocked(self) -> None:
        sources = self._sources(
            membership_license=LicenseClass.UNLICENSED_REFERENCE
        )
        artifacts = _ready_artifacts(sources)
        signal = artifacts["bars_pit_signal"].iloc[[0]].copy()
        signal["date"] = pd.Timestamp("2024-02-01")
        signal["decision_date"] = pd.Timestamp("2024-01-31")
        artifacts["bars_pit_signal"] = pd.concat(
            [artifacts["bars_pit_signal"], signal], ignore_index=True
        )
        release = self.service.build(
            artifacts,
            sources=sources,
        )
        codes = {issue.code for issue in release.quality_report.issues}
        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        self.assertIn("UNLICENSED_DEPENDENCY", codes)
        self.assertIn("SIGNAL_BAR_TIME_TRAVEL", codes)

    def test_unproven_publication_time_is_blocked(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        artifacts["fund_holdings_observed"].loc[0, "published_at"] = (
            "2024-02-01T00:00:00+00:00"
        )
        release = self.service.build(artifacts, sources=sources)
        self.assertIn(
            "UNPROVEN_SIGNAL_AVAILABILITY",
            {issue.code for issue in release.quality_report.issues},
        )

    def test_share_ratio_successor_requires_identity_and_effective_alias(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        evidence = self.store.put_bytes(b"split successor source")
        action_source = SourceDependency(
            source_id="reviewed-split",
            source_version="v1",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=evidence.sha256,
            observed_at="2024-01-15T18:00:00+00:00",
            published_at="2024-01-15T17:00:00+00:00",
            url="https://www.sec.gov/Archives/split-successor",
            dataset="corporate_actions",
        )
        artifacts["corporate_actions"] = pd.DataFrame(
            [{
                "action_id": "split-successor",
                "security_id": SECURITY_ID,
                "successor_security_id": "us_isin_us0378339999",
                "action_type": "SPLIT",
                "announced_at": "2024-01-15T12:00:00-05:00",
                "effective_at": "2024-01-31T09:30:00-05:00",
                "pay_date": None,
                "terms_verified": True,
                "source_id": action_source.source_id,
                "evidence_sha256": action_source.object_sha256,
                "split_ratio": 2.0,
            }]
        )
        release = self.service.build(artifacts, sources=[*sources, action_source])

        self.assertEqual(ReleaseStatus.DATA_BLOCKED, release.status)
        self.assertIn(
            "CORPORATE_ACTION_SUCCESSOR_IDENTITY_INVALID",
            {issue.code for issue in release.quality_report.issues},
        )

    def test_ambiguous_corporate_action_time_is_reported_not_raised(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        evidence = self.store.put_bytes(b"ambiguous split time source")
        action_source = SourceDependency(
            source_id="reviewed-split",
            source_version="v1",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=evidence.sha256,
            observed_at="2024-01-15T18:00:00+00:00",
            published_at="2024-01-15T17:00:00+00:00",
            url="https://www.sec.gov/Archives/ambiguous-split-time",
            dataset="corporate_actions",
        )
        artifacts["corporate_actions"] = pd.DataFrame(
            [{
                "action_id": "ambiguous-split-time",
                "security_id": SECURITY_ID,
                "successor_security_id": "us_isin_us0378339999",
                "action_type": "SPLIT",
                "announced_at": "2024-01-15T12:00:00-05:00",
                "effective_at": "2024-01-31T09:30:00",
                "pay_date": None,
                "terms_verified": True,
                "source_id": action_source.source_id,
                "evidence_sha256": action_source.object_sha256,
                "split_ratio": 2.0,
            }]
        )

        release = self.service.build(artifacts, sources=[*sources, action_source])

        self.assertEqual(ReleaseStatus.DATA_BLOCKED, release.status)
        self.assertIn(
            "CORPORATE_ACTION_NON_XNYS_SESSION",
            {issue.code for issue in release.quality_report.issues},
        )

        artifacts["xnys_calendar"] = pd.DataFrame()
        release_without_calendar = self.service.build(
            artifacts, sources=[*sources, action_source]
        )
        self.assertEqual(ReleaseStatus.DATA_BLOCKED, release_without_calendar.status)
        self.assertIn(
            "SCHEMA_MISMATCH",
            {issue.code for issue in release_without_calendar.quality_report.issues},
        )

    def test_next_open_uses_predecessor_then_verified_identity_successor(self) -> None:
        successor_id = "us_isin_us0378339999"
        decision = pd.Timestamp("2024-01-31")
        next_session = pd.Timestamp("2024-02-01")
        memberships = pd.DataFrame(
            [{"decision_date": decision, "security_id": SECURITY_ID}]
        )
        signal = pd.DataFrame(
            [{
                "decision_date": decision,
                "security_id": SECURITY_ID,
                "date": decision,
            }]
        )
        action = pd.DataFrame(
            [{
                "security_id": SECURITY_ID,
                "successor_security_id": successor_id,
                "action_type": "SPLIT",
                "effective_at": "2024-02-01T09:30:00-05:00",
                "terms_verified": True,
            }]
        )
        calendar = pd.DatetimeIndex([decision, next_session])

        def issues_for(
            raw_security_on_next: str | None,
            *,
            include_signal_close: bool = True,
        ) -> list[object]:
            raw_rows = [{"security_id": SECURITY_ID, "date": decision}]
            if raw_security_on_next is not None:
                raw_rows.append(
                    {"security_id": raw_security_on_next, "date": next_session}
                )
            signal_rows = signal if include_signal_close else signal.assign(
                date=pd.Timestamp("2024-01-30")
            )
            issues: list[object] = []
            self.validator._validate_decision_and_next_open(
                {
                    "bars_raw": pd.DataFrame(raw_rows),
                    "bars_pit_signal": signal_rows,
                    "corporate_actions": action,
                    "session_exceptions": _empty_artifact("session_exceptions"),
                },
                memberships,
                calendar,
                issues,
            )
            return issues

        self.assertEqual([], issues_for(SECURITY_ID))
        self.assertEqual([], issues_for(successor_id))
        self.assertEqual(
            ["MISSING_DECISION_EXECUTION_BAR"],
            [issue.code for issue in issues_for(None)],
        )
        signal_issue = issues_for(SECURITY_ID, include_signal_close=False)
        self.assertEqual(
            1,
            signal_issue[0].evidence["missing_signal_close"],
        )

    def test_unrelated_official_sources_cannot_certify_normalized_rows(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        unrelated_sources = [
            self._source(
                dataset="fund_holdings_observed",
                source_id="unrelated-signal",
                observed_at="2024-01-31T18:00:00+00:00",
                published_at="2024-01-31T17:00:00+00:00",
                as_of_date="2024-01-31",
                url="https://official.example/unrelated-signal",
            ),
            self._source(
                dataset="bars_raw",
                source_id="unrelated-bars",
                license_class=LicenseClass.LOCAL_VENDOR,
            ),
            self._source(
                dataset="fund_holdings_observed",
                source_id="unrelated-anchor",
                role=SourceRole.VALIDATION_ANCHOR,
                observed_at="2024-03-02T00:00:00+00:00",
                published_at="2024-02-15T00:00:00+00:00",
                as_of_date="2024-01-31",
            ),
            self._source(
                dataset="benchmark_total_return",
                source_id="unrelated-benchmark",
            ),
        ]

        release = self.service.build(artifacts, sources=unrelated_sources)

        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        codes = {issue.code for issue in release.quality_report.issues}
        self.assertIn("EVIDENCE_FOREIGN_KEY_BROKEN", codes)
        self.assertFalse(release.includes_delisted)

    def test_monthly_membership_must_equal_deterministic_replay(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        artifacts["membership_monthly"] = pd.concat(
            [
                artifacts["membership_monthly"],
                pd.DataFrame(
                    [
                        {
                            "universe_id": UNIVERSE_ID,
                            "decision_date": pd.Timestamp("2024-02-29"),
                            "security_id": "us_isin_fabricated0001",
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )

        release = self.service.build(artifacts, sources=sources)

        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        self.assertIn(
            "MEMBERSHIP_REPLAY_MISMATCH",
            {issue.code for issue in release.quality_report.issues},
        )

    def test_published_add_remove_events_are_replayed_after_the_baseline(self) -> None:
        second_security = "us_isin_us0000000002"
        sources = self._sources()
        event_source = self._source(
            dataset="membership_events",
            source_id="sp-announcement",
            observed_at="2024-03-02T00:00:00+00:00",
            published_at="2024-01-25T12:00:00+00:00",
        )
        sources.append(event_source)
        artifacts = _ready_artifacts(sources)

        for role in (SourceRole.SIGNAL_INPUT, SourceRole.VALIDATION_ANCHOR):
            row = artifacts["fund_holdings_observed"].loc[
                artifacts["fund_holdings_observed"]["evidence_role"].eq(role.value)
            ].iloc[0].copy()
            row["security_id"] = second_security
            artifacts["fund_holdings_observed"] = pd.concat(
                [artifacts["fund_holdings_observed"], pd.DataFrame([row])],
                ignore_index=True,
            )
        artifacts["membership_events"] = pd.DataFrame(
            [
                {
                    "event_id": "remove-second",
                    "security_id": second_security,
                    "event_type": "REMOVE",
                    "announced_at": "2024-01-25T12:00:00+00:00",
                    "effective_at": "2024-02-01T14:30:00+00:00",
                    "source_id": event_source.source_id,
                    "evidence_sha256": event_source.object_sha256,
                }
            ]
        )
        artifacts["membership_monthly"] = pd.concat(
            [
                artifacts["membership_monthly"],
                pd.DataFrame(
                    [
                        {
                            "universe_id": UNIVERSE_ID,
                            "decision_date": pd.Timestamp("2024-01-31"),
                            "security_id": second_security,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
        master = artifacts["security_master"].iloc[0].copy()
        master["security_id"] = second_security
        master["issuer_id"] = "second-issuer"
        master["primary_identifier"] = "US0000000002"
        artifacts["security_master"] = pd.concat(
            [artifacts["security_master"], pd.DataFrame([master])], ignore_index=True
        )
        identifier = artifacts["identifiers"].iloc[0].copy()
        identifier["security_id"] = second_security
        identifier["identifier_value"] = "US0000000002"
        artifacts["identifiers"] = pd.concat(
            [artifacts["identifiers"], pd.DataFrame([identifier])], ignore_index=True
        )
        alias = artifacts["listing_aliases"].iloc[0].copy()
        alias["security_id"] = second_security
        alias["ticker"] = "SECOND"
        alias["vendor_code"] = "SECOND.US"
        artifacts["listing_aliases"] = pd.concat(
            [artifacts["listing_aliases"], pd.DataFrame([alias])], ignore_index=True
        )
        for dataset in ("bars_raw", "bars_vendor_front", "bars_pit_signal"):
            rows = artifacts[dataset].copy()
            rows["security_id"] = second_security
            artifacts[dataset] = pd.concat(
                [artifacts[dataset], rows], ignore_index=True
            )
        coverage = artifacts["bar_coverage"].loc[
            pd.to_datetime(artifacts["bar_coverage"]["decision_date"])
            .dt.normalize()
            .eq(pd.Timestamp("2024-01-31"))
        ].copy()
        coverage["security_id"] = second_security
        artifacts["bar_coverage"] = pd.concat(
            [artifacts["bar_coverage"], coverage], ignore_index=True
        )
        lifecycle = artifacts["lifecycle_reconciliations"].iloc[0].copy()
        lifecycle["security_id"] = second_security
        artifacts["lifecycle_reconciliations"] = pd.concat(
            [artifacts["lifecycle_reconciliations"], pd.DataFrame([lifecycle])],
            ignore_index=True,
        )
        next(item for item in sources if item.dataset == "bars_raw").metadata[
            "normalized_artifact_sha256"
        ] = frame_derivation_sha256(artifacts["bars_raw"])
        next(item for item in sources if item.dataset == "bars_vendor_front").metadata[
            "normalized_artifact_sha256"
        ] = frame_derivation_sha256(artifacts["bars_vendor_front"])
        lifecycle_source = next(
            item for item in sources if item.dataset == "lifecycle_status"
        )
        covered_ids = sorted([SECURITY_ID, second_security])
        lifecycle_source.metadata["covered_security_ids"] = covered_ids
        lifecycle_source.metadata["covered_security_ids_sha256"] = sha256_json(
            covered_ids
        )
        lifecycle_source.metadata["covered_security_count"] = len(covered_ids)
        records = [dict(item) for item in lifecycle_source.metadata["source_records"]]
        records[0]["observations"] = [
            {
                "security_id": security_id,
                "identifier_type": "ISIN",
                "identifier_value": security_id.removeprefix("us_isin_").upper(),
                "observed_status": "LISTED",
                "evidence_locator": f"fixture:{security_id}",
                "observed_through": "2024-02-29",
                "status_effective_at": "",
                "evidence_excerpt": "official lifecycle source fixture",
            }
            for security_id in covered_ids
        ]
        lifecycle_source.metadata["source_records"] = records
        lifecycle_source.metadata["source_records_sha256"] = sha256_json(records)
        raw_lifecycle = next(
            item for item in sources if item.dataset == "lifecycle_observation"
        )
        replacement = self._source(
            dataset="lifecycle_observation",
            source_id=raw_lifecycle.source_id,
            role=raw_lifecycle.role,
            observed_at=raw_lifecycle.observed_at,
            published_at=raw_lifecycle.published_at,
            url=raw_lifecycle.url,
            payload=b"official lifecycle source fixture US0378331005 US0000000002",
        )
        sources[sources.index(raw_lifecycle)] = replacement
        records[0]["evidence_sha256"] = replacement.object_sha256
        lifecycle_source.metadata["source_records"] = records
        lifecycle_source.metadata["source_records_sha256"] = sha256_json(records)
        lifecycle_source.metadata["source_dependency_object_sha256s"] = [
            replacement.object_sha256
        ]

        release = self.service.build(artifacts, sources=sources)

        self.assertEqual(
            release.status, ReleaseStatus.DATA_READY, release.quality_report.to_dict()
        )
        self.assertTrue(release.quality_report.metrics["membership_replay_exact"])

    def test_build_rechecks_lifecycle_identifiers_against_cas_payload(self) -> None:
        sources = self._sources()
        raw = next(item for item in sources if item.dataset == "lifecycle_observation")
        summary = next(item for item in sources if item.dataset == "lifecycle_status")
        records = [dict(item) for item in summary.metadata["source_records"]]
        records[0] = dict(records[0])
        records[0]["observations"] = [dict(records[0]["observations"][0])]
        records[0]["observations"][0]["identifier_value"] = "US0000000000"
        summary.metadata["source_records"] = records
        summary.metadata["source_records_sha256"] = sha256_json(records)
        self.assertTrue(self.service.store.object_path(raw.object_sha256).is_file())
        with self.assertRaisesRegex(ValueError, "identifier is absent"):
            self.service.build(_ready_artifacts(sources), sources=sources)

    def test_anchor_status_and_zero_counts_cannot_hide_actual_set_difference(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        validation = artifacts["fund_holdings_observed"].loc[
            artifacts["fund_holdings_observed"]["evidence_role"].eq(
                SourceRole.VALIDATION_ANCHOR.value
            )
        ].iloc[0].to_dict()
        validation["security_id"] = "us_isin_anchor_extra0001"
        artifacts["fund_holdings_observed"] = pd.concat(
            [artifacts["fund_holdings_observed"], pd.DataFrame([validation])],
            ignore_index=True,
        )

        release = self.service.build(artifacts, sources=sources)

        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        codes = {issue.code for issue in release.quality_report.issues}
        self.assertIn("ANCHOR_RECONCILIATION_FAILED", codes)
        self.assertIn("ANCHOR_ATTESTATION_MISMATCH", codes)

    def test_global_lifecycle_true_attestation_is_not_an_unlock(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        lifecycle = artifacts["lifecycle_reconciliations"].iloc[0].copy()
        lifecycle["scope"] = "ALL_HISTORICAL_MEMBERS"
        lifecycle["security_id"] = None
        lifecycle["includes_delisted"] = True
        artifacts["lifecycle_reconciliations"] = pd.DataFrame([lifecycle])

        release = self.service.build(artifacts, sources=sources)

        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        self.assertFalse(release.includes_delisted)
        self.assertIn(
            "LIFECYCLE_RECONCILIATION_FAILED",
            {issue.code for issue in release.quality_report.issues},
        )

    def test_holding_evidence_cannot_substitute_for_termination_surveillance(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        holding = next(
            item
            for item in sources
            if item.dataset == "fund_holdings_observed"
            and item.role == SourceRole.SIGNAL_INPUT
        )
        artifacts["lifecycle_reconciliations"].loc[0, "source_id"] = holding.source_id
        artifacts["lifecycle_reconciliations"].loc[0, "evidence_sha256"] = (
            holding.object_sha256
        )

        release = self.service.build(artifacts, sources=sources)

        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        self.assertFalse(release.includes_delisted)
        self.assertIn(
            "LIFECYCLE_RECONCILIATION_FAILED",
            {issue.code for issue in release.quality_report.issues},
        )

    def test_lifecycle_surveillance_must_be_current_through_last_decision(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        artifacts["lifecycle_reconciliations"].loc[0, "current_through"] = "2024-01-31"

        release = self.service.build(artifacts, sources=sources)

        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        self.assertIn(
            "LIFECYCLE_RECONCILIATION_FAILED",
            {issue.code for issue in release.quality_report.issues},
        )

    def test_bar_coverage_counts_are_recomputed_from_rows(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        artifacts["bars_raw"] = artifacts["bars_raw"].loc[
            ~pd.to_datetime(artifacts["bars_raw"]["date"])
            .dt.normalize()
            .eq(pd.Timestamp("2024-02-15"))
        ].copy()
        next(item for item in sources if item.dataset == "bars_raw").metadata[
            "normalized_artifact_sha256"
        ] = frame_derivation_sha256(artifacts["bars_raw"])

        release = self.service.build(artifacts, sources=sources)

        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        issue = next(
            item
            for item in release.quality_report.issues
            if item.code == "INCOMPLETE_BAR_COVERAGE"
        )
        self.assertGreater(issue.evidence["attestation_mismatches"], 0)
        self.assertGreater(issue.evidence["failed_rows"], 0)

    def test_calendar_session_times_are_reconciled_to_xnys(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        artifacts["xnys_calendar"].loc[0, "market_close"] = "2024-01-26T20:00:00+00:00"
        next(item for item in sources if item.dataset == "xnys_calendar").metadata[
            "normalized_artifact_sha256"
        ] = frame_derivation_sha256(artifacts["xnys_calendar"])

        release = self.service.build(artifacts, sources=sources)

        self.assertEqual(release.status, ReleaseStatus.DATA_BLOCKED)
        self.assertIn(
            "XNYS_CALENDAR_MISMATCH",
            {issue.code for issue in release.quality_report.issues},
        )

    def test_fee_schedule_requires_taf_cap_and_exact_evidence_lineage(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        artifacts["execution_fee_schedule"] = artifacts[
            "execution_fee_schedule"
        ].drop(columns=["finra_taf_cap"])

        missing_cap = self.service.build(artifacts, sources=sources)

        self.assertEqual(missing_cap.status, ReleaseStatus.DATA_BLOCKED)
        self.assertIn(
            "SCHEMA_MISMATCH", {issue.code for issue in missing_cap.quality_report.issues}
        )

        artifacts = _ready_artifacts(sources)
        next(item for item in sources if item.dataset == "execution_fee_schedule").metadata[
            "normalized_artifact_sha256"
        ] = "f" * 64
        unbound = self.service.build(artifacts, sources=sources)
        self.assertEqual(unbound.status, ReleaseStatus.DATA_BLOCKED)
        self.assertIn(
            "FEE_LINEAGE_INVALID",
            {issue.code for issue in unbound.quality_report.issues},
        )

    def test_benchmark_total_return_is_required_and_evidence_backed(self) -> None:
        sources = self._sources()
        artifacts = _ready_artifacts(sources)
        artifacts["benchmarks"] = artifacts["benchmarks"].drop(
            columns=["TotalReturnClose"]
        )
        missing = self.service.build(artifacts, sources=sources)
        self.assertEqual(missing.status, ReleaseStatus.DATA_BLOCKED)
        self.assertIn("SCHEMA_MISMATCH", {issue.code for issue in missing.quality_report.issues})

        artifacts = _ready_artifacts(sources)
        artifacts["benchmarks"]["total_return_evidence_sha256"] = "f" * 64
        unbound = self.service.build(artifacts, sources=sources)
        self.assertEqual(unbound.status, ReleaseStatus.DATA_BLOCKED)
        self.assertIn(
            "EVIDENCE_FOREIGN_KEY_BROKEN",
            {issue.code for issue in unbound.quality_report.issues},
        )

    def test_release_tampering_is_detected(self) -> None:
        sources = self._sources()
        release = self.service.build(_ready_artifacts(sources), sources=sources)
        path = release.artifact_path("membership_monthly")
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        with path.open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.store.load_release(release.release_id)

    def test_sync_captures_raw_payload_and_provenance_without_network(self) -> None:
        observed = datetime(2024, 3, 2, tzinfo=timezone.utc)
        adapter = StaticSourceAdapter(
            "ishares-test",
            "v1",
            [
                SourceArtifact(
                    dataset="fund_holdings_observed",
                    payload=b"ticker,cusip\nAAPL,037833100\n",
                    media_type="text/csv",
                    url="https://official.example/holdings.csv",
                    observed_at=observed,
                    published_at=datetime(2024, 3, 1, 22, tzinfo=timezone.utc),
                    as_of_date=date(2024, 3, 1),
                    role=SourceRole.VALIDATION_ANCHOR,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                )
            ],
        )
        batch = self.service.sync(
            adapter,
            SyncRequest(date(2024, 3, 1), date(2024, 3, 1), observed),
        )
        self.assertTrue(batch.path.is_file())
        self.assertEqual(len(batch.dependencies), 1)
        dependency = batch.dependencies[0]
        self.assertTrue(self.store.object_path(dependency.object_sha256).is_file())
        self.assertEqual(dependency.source_id, "ishares-test")
        self.assertEqual(dependency.observed_at, observed.isoformat())

    def test_override_requires_hash_match_and_evidence_policy(self) -> None:
        primary_evidence = self.store.put_bytes(b"SEC filing evidence")
        proposal = OverrideProposal(
            override_id="fb_meta_alias",
            dataset="listing_aliases",
            record_key={"security_id": SECURITY_ID, "valid_from": "1980-12-12"},
            before={"ticker": "AAPL", "vendor_code": "AAPL.US"},
            after={
                "ticker": "AAPL",
                "vendor_code": "AAPL.US",
                "evidence_note": "SEC verified",
            },
            reason="Issuer-confirmed alias evidence",
            evidence=(
                EvidenceReference(
                    url="https://www.sec.gov/example",
                    authority=EvidenceAuthority.AUTHORITATIVE_PRIMARY,
                    content_sha256=primary_evidence.sha256,
                    source_id="sec",
                ),
            ),
            proposed_at="2024-03-02T00:00:00+00:00",
            proposed_by="local-user",
        )
        draft = self.store.propose_override(proposal)
        approved = self.store.approve_override(
            proposal.override_id,
            expected_sha256=draft.draft_sha256,
            approved_at="2024-03-02T00:05:00+00:00",
            approved_by="local-user",
            acknowledgement="I verified the primary filing and accept this local repair.",
        )
        self.assertTrue(approved.approved)
        transformed, applied = self.service.apply_approved_overrides(
            _ready_artifacts(self._sources()), [proposal.override_id]
        )
        self.assertEqual(transformed["listing_aliases"].loc[0, "evidence_note"], "SEC verified")
        self.assertEqual(applied[0]["draft_sha256"], draft.draft_sha256)

        changed = OverrideProposal(
            **{
                **proposal.__dict__,
                "after": {"ticker": "AAPL", "vendor_code": "AAPL.NQ"},
                "proposed_at": "2024-03-02T00:10:00+00:00",
            }
        )
        changed_state = self.store.propose_override(changed)
        self.assertFalse(changed_state.approved)
        with self.assertRaisesRegex(ValueError, "hash changed"):
            self.store.approve_override(
                proposal.override_id,
                expected_sha256=draft.draft_sha256,
                approved_at="2024-03-02T00:11:00+00:00",
                approved_by="local-user",
                acknowledgement="old revision",
            )

        secondary_evidence = self.store.put_bytes(b"secondary evidence")
        secondary_only = OverrideProposal(
            override_id="secondary_only",
            dataset="security_master",
            record_key={"security_id": SECURITY_ID},
            before=None,
            after={"issuer_id": "apple-inc"},
            reason="test",
            evidence=(
                EvidenceReference(
                    url="https://secondary.example/one",
                    authority=EvidenceAuthority.INDEPENDENT_SECONDARY,
                    content_sha256=secondary_evidence.sha256,
                    source_id="secondary",
                ),
            ),
            proposed_at="2024-03-02T00:00:00+00:00",
            proposed_by="local-user",
        )
        weak = self.store.propose_override(secondary_only)
        with self.assertRaisesRegex(ValueError, "two independent"):
            self.store.approve_override(
                secondary_only.override_id,
                expected_sha256=weak.draft_sha256,
                approved_at="2024-03-02T00:05:00+00:00",
                approved_by="local-user",
                acknowledgement="checked",
            )


if __name__ == "__main__":
    unittest.main()
