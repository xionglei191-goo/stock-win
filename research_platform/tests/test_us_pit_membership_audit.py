from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from research_platform.__main__ import build_parser
from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file, sha256_json
from research_platform.us_pit.membership_audit import audit_membership_candidates
from research_platform.us_pit.membership_replay import MembershipReplayResult
from research_platform.us_pit.store import USPITStore
from research_platform.us_pit.models import (
    LicenseClass,
    SourceDependency,
    SourceRole,
)


class MembershipCandidateAuditTests(unittest.TestCase):
    def test_audit_is_candidate_only_and_never_emits_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = USPITStore(root / "pit")
            normalization = root / "normalization"
            normalization.mkdir()
            holdings = pd.DataFrame(
                [
                    {
                        "source_id": "sec_nport_ivv",
                        "as_of_date": "2021-06-30",
                        "content_sha256": "a" * 64,
                        "evidence_role": "VALIDATION_ANCHOR",
                        "identity_candidate_key": "isin:US0378331005",
                    }
                ]
            )
            holdings.to_parquet(
                normalization / "fund_holdings_observed_candidate.parquet", index=False
            )
            pd.DataFrame(
                [
                    {
                        "source_id": "sec_nport_ivv",
                        "identity_candidate_key": "isin:US0378331005",
                        "issuer_name": "Apple Inc",
                        "lei": pd.NA,
                        "isin": "US0378331005",
                        "cusip": "037833100",
                        "as_of_date": "2021-06-30",
                        "source_row_number": 1,
                    }
                ]
            ).to_parquet(
                normalization / "security_identity_candidates.parquet", index=False
            )
            (normalization / "manifest.json").write_bytes(
                canonical_json_bytes(
                    {
                        "normalization_id": "normalization",
                        "candidate_only": True,
                    }
                )
            )
            candidates = root / "candidates"
            candidates.mkdir()
            event_frame = pd.DataFrame(
                [
                    {
                        "event_candidate_id": "event-1",
                        "suggested_security_id": "",
                        "ticker_at_announcement": "MBC",
                        "event_type": "ADD",
                        "announced_at": "2021-08-01T12:00:00-04:00",
                        "effective_at": "2021-08-02T09:30:00-04:00",
                        "source_id": "spglobal_sp500_membership_events",
                        "evidence_sha256": "b" * 64,
                        "company_name": "MBC Holdings",
                        "source_url": "https://official.example.com/mbc",
                    }
                ]
            )
            event_path = candidates / "membership_event_candidates.parquet"
            event_frame.to_parquet(event_path, index=False)
            candidate_manifest = {
                "candidate_only": True,
                "direct_build_allowed": False,
                "artifact_sha256": sha256_file(event_path),
            }
            candidate_manifest["candidate_set_id"] = sha256_json(candidate_manifest)
            (candidates / "manifest.json").write_bytes(
                canonical_json_bytes(candidate_manifest)
            )
            object_ref = store.put_bytes(b"audit fixture")
            batch = store.write_source_batch(
                [
                    SourceDependency(
                        source_id="fixture",
                        source_version="1",
                        role=SourceRole.VALIDATION_ANCHOR,
                        license_class=LicenseClass.OFFICIAL_PUBLIC,
                        object_sha256=object_ref.sha256,
                        observed_at="2021-08-31T20:00:00+00:00",
                        published_at="2021-08-31T19:00:00+00:00",
                        url="https://official.example.com/fixture",
                        dataset="fixture",
                    )
                ]
            )
            replay = MembershipReplayResult(
                memberships=pd.DataFrame(),
                replayed={pd.Timestamp("2021-08-31"): frozenset()},
                gaps=(
                    {
                        "code": "MEMBERSHIP_EVENT_STATE_CONFLICT",
                        "decision_date": "2021-08-31",
                        "event_ids": ["event-1"],
                    },
                    {
                        "code": "MEMBERSHIP_EVENT_STATE_CONFLICT",
                        "decision_date": "2021-09-30",
                        "event_ids": ["event-1"],
                    },
                    {
                        "code": "QUARTERLY_ANCHOR_RECONCILIATION_FAILED",
                        "anchor_date": "2021-08-31",
                        "extra": ["us_isin_us0378331005"],
                        "missing": ["us_isin_us5949181045"],
                        "conflicting_event_ids": [],
                    },
                ),
                reconciled_anchor_count=0,
            )
            with patch(
                "research_platform.us_pit.membership_audit.replay_causal_membership",
                return_value=replay,
            ):
                result = audit_membership_candidates(
                    store,
                    normalization,
                    candidates,
                    [batch.batch_id],
                    root / "audit",
                    decision_start="2021-08-01",
                    decision_end="2021-08-31",
                )
            self.assertEqual("DATA_BLOCKED", result.report["status"])
            self.assertTrue(result.report["candidate_only"])
            self.assertFalse(result.report["direct_build_allowed"])
            self.assertFalse((result.path / "membership_monthly.parquet").exists())
            self.assertEqual(1, result.report["unresolved_identity_events"])
            self.assertEqual(1, result.report["membership_event_conflict_root_count"])
            root_cause = result.report["membership_event_conflict_roots"][0]
            self.assertEqual("event-1", root_cause["event_id"])
            self.assertEqual(2, root_cause["affected_decision_count"])
            self.assertEqual(2, result.report["residual_membership_event_requests"])
            self.assertEqual(
                {"ADD": 1, "REMOVE": 1},
                result.report["residual_membership_event_counts"],
            )
            requests = pd.read_parquet(
                result.path / "residual_membership_event_requests.parquet"
            )
            self.assertEqual({"ADD", "REMOVE"}, set(requests["event_type"]))
            self.assertFalse(requests["approved"].any())

    def test_cli_requires_explicit_inputs(self) -> None:
        args = build_parser().parse_args(
            [
                "us-pit", "audit-membership",
                "--normalization-dir", "norm",
                "--candidate-dir", "events",
                "--source-batch", "a" * 64,
                "--output-dir", "audit",
            ]
        )
        self.assertEqual("audit-membership", args.us_pit_command)


if __name__ == "__main__":
    unittest.main()
