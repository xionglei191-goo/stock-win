from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pandas as pd

from research_platform.us_pit.membership_replay import replay_causal_membership
from research_platform.us_pit.models import (
    LicenseClass,
    SourceDependency,
    SourceRole,
)


def _anchor(digest: str, report: str, accepted: str) -> SourceDependency:
    return SourceDependency(
        source_id="sec_nport_ivv",
        source_version="1",
        role=SourceRole.VALIDATION_ANCHOR,
        license_class=LicenseClass.OFFICIAL_PUBLIC,
        object_sha256=digest,
        observed_at="2026-08-14T00:00:00+00:00",
        published_at=accepted,
        as_of_date=report,
        url="https://www.sec.gov/Archives/edgar/data/example.txt",
        dataset="fund_holdings_observed",
        metadata={
            "artifact_kind": "raw_complete_edgar_submission",
            "series_id_verified_in_payload": True,
            "eligible_for_historical_signal": False,
            "accepted_at": accepted,
        },
    )


class CausalMembershipReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        sessions = pd.bdate_range("2021-06-30", "2021-09-30")
        self.calendar = pd.DataFrame(
            {
                "session_date": sessions,
                "market_close": [
                    f"{day.date()}T16:00:00-04:00" for day in sessions
                ],
            }
        )
        self.first = "a" * 64
        self.second = "b" * 64
        self.event_hash = "c" * 64
        self.sources = (
            _anchor(self.first, "2021-06-30", "2021-08-27T20:00:00+00:00"),
            _anchor(self.second, "2021-09-30", "2021-11-26T20:00:00+00:00"),
            SourceDependency(
                source_id="spglobal_sp500_membership_events",
                source_version="1",
                role=SourceRole.SIGNAL_INPUT,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=self.event_hash,
                observed_at="2026-08-14T00:00:00+00:00",
                published_at="2021-09-03T19:00:00-04:00",
                as_of_date="2021-09-20",
                url="https://press.spglobal.com/event",
                dataset="membership_events",
                metadata={"publication_time_from_payload": True},
            ),
        )
        self.holdings = pd.DataFrame(
            [
                {
                    "as_of_date": "2021-06-30",
                    "content_sha256": self.first,
                    "evidence_role": "VALIDATION_ANCHOR",
                    "security_id": "us_isin_a",
                },
                {
                    "as_of_date": "2021-09-30",
                    "content_sha256": self.second,
                    "evidence_role": "VALIDATION_ANCHOR",
                    "security_id": "us_isin_a",
                },
                {
                    "as_of_date": "2021-09-30",
                    "content_sha256": self.second,
                    "evidence_role": "VALIDATION_ANCHOR",
                    "security_id": "us_isin_b",
                },
            ]
        )
        self.events = pd.DataFrame(
            [
                {
                    "event_id": "event-add-b",
                    "security_id": "us_isin_b",
                    "event_type": "ADD",
                    "announced_at": "2021-09-03T19:00:00-04:00",
                    "effective_at": "2021-09-20T09:30:00-04:00",
                    "source_id": "spglobal_sp500_membership_events",
                    "evidence_sha256": self.event_hash,
                }
            ]
        )

    def test_late_sec_anchor_is_used_only_after_acceptance_and_reconciliation(self) -> None:
        result = replay_causal_membership(
            self.holdings,
            self.events,
            [pd.Timestamp("2021-08-31"), pd.Timestamp("2021-09-30")],
            self.sources,
            self.calendar,
        )
        self.assertEqual(1, result.reconciled_anchor_count)
        self.assertEqual(
            frozenset({"us_isin_a"}), result.replayed[pd.Timestamp("2021-08-31")]
        )
        self.assertEqual(
            frozenset({"us_isin_a", "us_isin_b"}),
            result.replayed[pd.Timestamp("2021-09-30")],
        )
        self.assertEqual((), result.gaps)

    def test_event_source_version_disambiguates_two_derivations_of_one_object(self) -> None:
        events = self.events.copy()
        events["source_version"] = "2"
        v2 = SourceDependency(
            source_id="spglobal_sp500_membership_events",
            source_version="2",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=self.event_hash,
            observed_at="2026-08-14T00:00:00+00:00",
            published_at="2021-09-03T19:00:00-04:00",
            as_of_date="2021-09-20",
            url="https://press.spglobal.com/event",
            dataset="membership_events",
            metadata={"publication_time_from_payload": True},
        )
        result = replay_causal_membership(
            self.holdings,
            events,
            [pd.Timestamp("2021-08-31"), pd.Timestamp("2021-09-30")],
            self.sources + (v2,),
            self.calendar,
        )
        self.assertNotIn(
            "UNPROVEN_MEMBERSHIP_EVENT", {item["code"] for item in result.gaps}
        )
        self.assertEqual(
            frozenset({"us_isin_a", "us_isin_b"}),
            result.replayed[pd.Timestamp("2021-09-30")],
        )

    def test_anchor_is_not_self_certifying_without_the_next_anchor(self) -> None:
        result = replay_causal_membership(
            self.holdings.iloc[:1],
            self.events.iloc[:0],
            [pd.Timestamp("2021-08-31")],
            self.sources[:1],
            self.calendar,
        )
        self.assertNotIn(pd.Timestamp("2021-08-31"), result.replayed)
        self.assertIn(
            "MISSING_DECISION_TIME_BASELINE",
            {item["code"] for item in result.gaps},
        )

    def test_non_xnys_effective_date_is_a_blocking_gap(self) -> None:
        events = self.events.copy()
        events.loc[0, "effective_at"] = "2021-09-18T09:30:00-04:00"
        result = replay_causal_membership(
            self.holdings,
            events,
            [pd.Timestamp("2021-08-31")],
            self.sources,
            self.calendar,
        )
        self.assertIn(
            "EVENT_EFFECTIVE_DATE_NOT_XNYS_SESSION",
            {item["code"] for item in result.gaps},
        )

    def test_weekend_quarter_end_uses_prior_actual_session_for_anchor_only(self) -> None:
        second = _anchor(
            self.second, "2021-10-02", "2021-11-26T20:00:00+00:00"
        )
        holdings = self.holdings.copy()
        holdings.loc[holdings["content_sha256"].eq(self.second), "as_of_date"] = (
            "2021-10-02"
        )
        result = replay_causal_membership(
            holdings,
            self.events,
            [pd.Timestamp("2021-08-31")],
            (self.sources[0], second, self.sources[2]),
            self.calendar,
        )
        self.assertNotIn(
            "MEMBERSHIP_BASELINE_TIME_INVALID",
            {item["code"] for item in result.gaps},
        )
        self.assertEqual(1, result.reconciled_anchor_count)

    def test_verified_identity_action_migrates_membership_stable_id(self) -> None:
        action_hash = "d" * 64
        action_source = SourceDependency(
            source_id="reviewed-corporate-actions",
            source_version="1",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=action_hash,
            observed_at="2026-08-14T18:00:00+00:00",
            published_at="2021-08-15T18:00:00+00:00",
            as_of_date="2021-09-20",
            url="https://www.sec.gov/Archives/identity-action",
            dataset="corporate_actions",
            metadata={
                "publication_time_from_payload": True,
                "accepted_at_verified_in_payload": True,
                "accepted_at": "2021-08-15T18:00:00+00:00",
            },
        )
        holdings = self.holdings.iloc[[0, 2]].copy()
        holdings.loc[
            holdings["content_sha256"].eq(self.second), "security_id"
        ] = "us_isin_successor"
        actions = pd.DataFrame(
            [{
                "action_id": "rename-with-new-identifier",
                "security_id": "us_isin_a",
                "successor_security_id": "us_isin_successor",
                "action_type": "RENAME",
                "announced_at": "2021-08-15T14:00:00-04:00",
                "effective_at": "2021-09-20T09:30:00-04:00",
                "terms_verified": True,
                "source_id": action_source.source_id,
                "evidence_sha256": action_hash,
            }]
        )

        result = replay_causal_membership(
            holdings,
            self.events.iloc[:0],
            [pd.Timestamp("2021-08-31"), pd.Timestamp("2021-09-30")],
            (self.sources[0], self.sources[1], action_source),
            self.calendar,
            actions,
        )

        self.assertEqual(1, result.reconciled_anchor_count)
        self.assertEqual(
            frozenset({"us_isin_a"}), result.replayed[pd.Timestamp("2021-08-31")]
        )
        self.assertEqual(
            frozenset({"us_isin_successor"}),
            result.replayed[pd.Timestamp("2021-09-30")],
        )
        self.assertEqual((), result.gaps)

    def test_unproven_identity_action_cannot_reconcile_anchor(self) -> None:
        holdings = self.holdings.iloc[[0, 2]].copy()
        holdings.loc[
            holdings["content_sha256"].eq(self.second), "security_id"
        ] = "us_isin_successor"
        actions = pd.DataFrame(
            [{
                "action_id": "unproven-rename",
                "security_id": "us_isin_a",
                "successor_security_id": "us_isin_successor",
                "action_type": "RENAME",
                "announced_at": "2021-08-15T14:00:00-04:00",
                "effective_at": "2021-09-20T09:30:00-04:00",
                "terms_verified": True,
                "source_id": "missing-source",
                "evidence_sha256": "e" * 64,
            }]
        )

        result = replay_causal_membership(
            holdings,
            self.events.iloc[:0],
            [pd.Timestamp("2021-08-31")],
            self.sources[:2],
            self.calendar,
            actions,
        )

        self.assertEqual(0, result.reconciled_anchor_count)
        self.assertIn(
            "UNPROVEN_MEMBERSHIP_IDENTITY_ACTION",
            {item["code"] for item in result.gaps},
        )

    def test_unverified_payload_acceptance_cannot_backdate_late_observation(self) -> None:
        action_hash = "f" * 64
        source = SourceDependency(
            source_id="reviewed-corporate-actions",
            source_version="1",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=action_hash,
            observed_at="2026-08-14T18:00:00+00:00",
            published_at="2021-08-15T18:00:00+00:00",
            as_of_date="2021-09-20",
            url="https://www.sec.gov/Archives/unverified-acceptance",
            dataset="corporate_actions",
            metadata={"publication_time_from_payload": True},
        )
        actions = pd.DataFrame(
            [{
                "action_id": "unverified-payload-time",
                "security_id": "us_isin_a",
                "successor_security_id": "us_isin_successor",
                "action_type": "RENAME",
                "announced_at": "2021-08-15T14:00:00-04:00",
                "effective_at": "2021-09-20T09:30:00-04:00",
                "terms_verified": True,
                "source_id": source.source_id,
                "evidence_sha256": action_hash,
            }]
        )

        result = replay_causal_membership(
            self.holdings.iloc[[0]],
            self.events.iloc[:0],
            [pd.Timestamp("2021-08-31")],
            (self.sources[0], source),
            self.calendar,
            actions,
        )

        self.assertIn(
            "MEMBERSHIP_IDENTITY_ACTION_INVALID",
            {item["code"] for item in result.gaps},
        )


if __name__ == "__main__":
    unittest.main()
