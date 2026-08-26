from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research_platform.us_pit.action_review import (
    approve_action_review,
    prepare_action_review,
    propose_action_review,
)
from research_platform.us_pit.hashing import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    sha256_json,
)
from research_platform.us_pit.models import LicenseClass, SourceDependency, SourceRole
from research_platform.us_pit.store import USPITStore


class USPITActionReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = USPITStore(self.root / "pit")
        self.source_payload = (
            b"Official filing evidence. The company changed its name and ticker at "
            b"the opening of trading on January 3, 2024. This transaction preserves "
            b"the same legal security identity for the reviewed common shares."
        )
        self.source = self.store.put_bytes(self.source_payload)
        self.original_observed_at = "2026-08-14T00:00:00+00:00"
        self.store.write_source_batch(
            (
                SourceDependency(
                    source_id="sec_corporate_action_filing_documents",
                    source_version="fixture-v1",
                    role=SourceRole.REFERENCE_ONLY,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    object_sha256=self.source.sha256,
                    observed_at=self.original_observed_at,
                    published_at="2024-01-02T18:00:00+00:00",
                    as_of_date="2024-01-02",
                    url="https://www.sec.gov/Archives/example.txt",
                    dataset="corporate_action_source_document",
                    metadata={
                        "accession_number": "0000000000-24-000001",
                        "response_sha256": self.source.sha256,
                        "accepted_at": "2024-01-02T18:00:00+00:00",
                        "artifact_kind": "sec_complete_submission",
                    },
                ),
            )
        )
        self.requests = self._request_package()
        self.ranked = self._ranked_package()

    def _request_package(self) -> Path:
        root = self.root / "requests"
        root.mkdir()
        frame = pd.DataFrame(
            [
                {
                    "request_id": "request-1",
                    "audit_id": "audit-1",
                    "anchor_date": "2024-01-31",
                    "predecessor_security_id": "us_isin_us0000000001",
                    "successor_security_id": "us_isin_us0000000001",
                    "predecessor_name": "Old Name Inc",
                    "successor_name": "New Name Inc",
                }
            ]
        )
        artifact = root / "corporate_action_evidence_requests.parquet"
        frame.to_parquet(artifact, index=False)
        manifest = {
            "artifact_sha256": sha256_file(artifact),
            "request_set_id": "a" * 64,
            "status": "DATA_BLOCKED",
            "candidate_only": True,
            "direct_build_allowed": False,
        }
        (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        return root

    def _ranked_package(self) -> Path:
        root = self.root / "ranked"
        root.mkdir()
        frame = pd.DataFrame(
            [
                {
                    "request_id": "request-1",
                    "review_candidate_id": "candidate-1",
                    "anchor_date": "2024-01-31",
                    "accession_number": "0000000000-24-000001",
                    "source_url": "https://www.sec.gov/Archives/example.txt",
                    "source_object_sha256": self.source.sha256,
                    "accepted_at": "2024-01-02T18:00:00+00:00",
                    "filing_date": "2024-01-02",
                }
            ]
        )
        artifact = root / "corporate_action_filing_review.parquet"
        frame.to_parquet(artifact, index=False)
        manifest = {
            "artifact_sha256": sha256_file(artifact),
            "review_set_id": "b" * 64,
            "request_count": 1,
            "status": "REVIEW_REQUIRED",
            "candidate_only": True,
            "direct_build_allowed": False,
        }
        (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        return root

    def _completed(self, template: Path, name: str = "completed.csv") -> Path:
        frame = pd.read_csv(template / "action_review.csv", dtype=str, keep_default_na=False)
        frame.loc[0, "disposition"] = "IDENTITY_CONTINUITY"
        frame.loc[0, "selected_review_candidate_id"] = "candidate-1"
        frame.loc[0, "action_type"] = "RENAME"
        frame.loc[0, "announced_at"] = "2024-01-02T13:00:00-05:00"
        frame.loc[0, "effective_at"] = "2024-01-03T09:30:00-05:00"
        frame.loc[0, "terms_verified"] = "true"
        frame.loc[0, "evidence_excerpt"] = (
            "The company changed its name and ticker at the opening of trading "
            "on January 3, 2024."
        )
        frame.loc[0, "review_note"] = "Verified the frozen complete submission."
        path = self.root / name
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        return path

    def test_template_is_unselected_and_nonbuildable(self) -> None:
        result = prepare_action_review(self.requests, self.ranked, self.root / "template")
        draft = pd.read_csv(result.path / "action_review.csv", keep_default_na=False)

        self.assertEqual(1, len(draft))
        self.assertEqual("", draft.iloc[0]["selected_review_candidate_id"])
        self.assertEqual("", draft.iloc[0]["action_type"])
        self.assertFalse(result.manifest["direct_build_allowed"])
        self.assertEqual(
            result.manifest["candidates_csv_sha256"],
            sha256_file(result.path / "corporate_action_filing_candidates.csv"),
        )
        guide = pd.read_csv(result.path / "action_review_guide.csv")
        gaps = json.loads(
            (result.path / "review_gaps.json").read_text(encoding="utf-8")
        )
        self.assertEqual("Old Name Inc", guide.iloc[0]["predecessor_name"])
        self.assertEqual(1, gaps["unresolved_request_count"])
        self.assertTrue(gaps["automatic_selection_forbidden"])

    def test_review_excerpt_matches_human_visible_html_text(self) -> None:
        source_path = self.store.object_path(self.source.sha256)
        source_path.chmod(0o600)
        html_payload = (
            b"<html><body>The company changed its name and ticker at the "
            b"opening of trading on January 3, 2024 &amp; retained its "
            b"stable legal identity.</body></html>"
        )
        source_path.write_bytes(html_payload)
        replacement = self.store.put_bytes(html_payload, media_type="text/html")
        dependency = SourceDependency(
            source_id="sec_corporate_action_filing_documents",
            source_version="fixture-v1",
            role=SourceRole.VALIDATION_ANCHOR,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=replacement.sha256,
            observed_at=self.original_observed_at,
            published_at="2024-01-02T18:00:00+00:00",
            url="https://www.sec.gov/Archives/example-html.txt",
            dataset="corporate_action_source_document",
            metadata={
                "accession_number": "0000000000-24-000002",
                "response_sha256": replacement.sha256,
                "accepted_at": "2024-01-02T18:00:00+00:00",
                "artifact_kind": "sec_complete_submission",
            },
        )
        self.store.write_source_batch([dependency])
        ranked = pd.read_parquet(
            self.ranked / "corporate_action_filing_review.parquet"
        )
        ranked.loc[0, "accession_number"] = "0000000000-24-000002"
        ranked.loc[0, "source_url"] = dependency.url
        ranked.loc[0, "source_object_sha256"] = replacement.sha256
        ranked.loc[0, "review_candidate_id"] = "candidate-html"
        ranked.to_parquet(
            self.ranked / "corporate_action_filing_review.parquet", index=False
        )
        manifest_path = self.ranked / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_sha256"] = sha256_file(
            self.ranked / "corporate_action_filing_review.parquet"
        )
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "html-template"
        ).path
        completed = pd.read_csv(
            self._completed(template, "html-completed.csv"),
            dtype=str,
            keep_default_na=False,
        )
        completed.loc[0, "selected_review_candidate_id"] = "candidate-html"
        completed.loc[0, "evidence_excerpt"] = (
            "The company changed its name and ticker at the opening of trading "
            "on January 3, 2024 & retained its stable legal identity."
        )
        completed.to_csv(self.root / "html-final.csv", index=False)
        result = propose_action_review(
            self.store,
            template,
            self.root / "html-final.csv",
            self.root / "html-proposal",
            proposed_by="reviewer",
        )
        self.assertEqual(1, result.manifest["review_row_count"])

    def test_changed_request_identity_and_missing_excerpt_are_rejected(self) -> None:
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "template"
        ).path
        changed = pd.read_csv(self._completed(template), dtype=str, keep_default_na=False)
        changed.loc[0, "predecessor_security_id"] = "us_isin_us9999999999"
        changed_path = self.root / "changed.csv"
        changed.to_csv(changed_path, index=False)
        with self.assertRaisesRegex(ValueError, "immutable evidence request identity"):
            propose_action_review(
                self.store,
                template,
                changed_path,
                self.root / "bad-proposal",
                proposed_by="reviewer",
            )

        missing = pd.read_csv(self._completed(template, "missing.csv"), dtype=str, keep_default_na=False)
        missing.loc[0, "evidence_excerpt"] = "This excerpt is not in the filing and is sufficiently long."
        missing_path = self.root / "missing-final.csv"
        missing.to_csv(missing_path, index=False)
        with self.assertRaisesRegex(ValueError, "absent from frozen SEC source"):
            propose_action_review(
                self.store,
                template,
                missing_path,
                self.root / "missing-proposal",
                proposed_by="reviewer",
            )

    def test_non_session_effective_date_is_rejected(self) -> None:
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "template"
        ).path
        completed = pd.read_csv(self._completed(template), dtype=str, keep_default_na=False)
        completed.loc[0, "effective_at"] = "2024-01-06T09:30:00-05:00"
        path = self.root / "saturday.csv"
        completed.to_csv(path, index=False)

        with self.assertRaisesRegex(ValueError, "not an explicit XNYS session"):
            propose_action_review(
                self.store,
                template,
                path,
                self.root / "bad-session",
                proposed_by="reviewer",
            )

    def test_filing_after_anchor_cannot_be_promoted_to_historical_signal_evidence(self) -> None:
        ranked = pd.read_parquet(
            self.ranked / "corporate_action_filing_review.parquet"
        )
        ranked.loc[0, "accepted_at"] = "2024-02-01T18:00:00+00:00"
        ranked.to_parquet(
            self.ranked / "corporate_action_filing_review.parquet", index=False
        )
        manifest_path = self.ranked / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_sha256"] = sha256_file(
            self.ranked / "corporate_action_filing_review.parquet"
        )
        manifest_path.write_bytes(canonical_json_bytes(manifest))

        with self.assertRaisesRegex(ValueError, "lack decision-time visible"):
            prepare_action_review(self.requests, self.ranked, self.root / "template")

    def test_action_after_anchor_cannot_explain_anchor_identity(self) -> None:
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "template"
        ).path
        completed = pd.read_csv(self._completed(template), dtype=str, keep_default_na=False)
        completed.loc[0, "announced_at"] = "2024-02-01T08:00:00-05:00"
        completed.loc[0, "effective_at"] = "2024-02-01T09:30:00-05:00"
        path = self.root / "after-anchor.csv"
        completed.to_csv(path, index=False)

        with self.assertRaisesRegex(ValueError, "after the reconciliation anchor"):
            propose_action_review(
                self.store,
                template,
                path,
                self.root / "after-anchor-proposal",
                proposed_by="reviewer",
            )

    def test_hash_approval_publishes_signal_evidence_and_formal_action(self) -> None:
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "template"
        ).path
        proposal = propose_action_review(
            self.store,
            template,
            self._completed(template),
            self.root / "proposal",
            proposed_by="reviewer",
            proposed_at=datetime(2024, 1, 4, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ValueError, "hash changed"):
            approve_action_review(
                self.store,
                proposal.path,
                self.root / "wrong",
                expected_sha256="0" * 64,
                approved_by="approver",
                acknowledgement="verified",
                approved_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
            )

        result = approve_action_review(
            self.store,
            proposal.path,
            self.root / "approved",
            expected_sha256=str(proposal.manifest["proposal_sha256"]),
            approved_by="approver",
            acknowledgement="verified",
            approved_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
        )
        actions = pd.read_parquet(result.path / "corporate_actions.parquet")
        dependency = result.source_batch.dependencies[0]

        self.assertEqual(1, len(actions))
        self.assertEqual("RENAME", actions.iloc[0]["action_type"])
        self.assertEqual(self.source.sha256, actions.iloc[0]["evidence_sha256"])
        self.assertEqual("corporate_actions", dependency.dataset)
        self.assertEqual("SIGNAL_INPUT", dependency.role.value)
        self.assertEqual(self.source.sha256, dependency.object_sha256)
        self.assertEqual(self.original_observed_at, dependency.observed_at)
        self.assertTrue(dependency.metadata["publication_time_from_payload"])
        self.assertTrue(dependency.metadata["accepted_at_verified_in_payload"])
        self.assertEqual(
            "2024-01-02T18:00:00+00:00",
            dependency.metadata["accepted_at"],
        )
        self.assertEqual(
            "2024-01-04T00:00:00+00:00",
            dependency.metadata["review_proposed_at"],
        )
        self.assertEqual(
            proposal.manifest["proposal_sha256"],
            dependency.metadata["review_proposal_sha256"],
        )
        self.assertEqual("approver", dependency.metadata["review_approved_by"])
        self.assertEqual(
            "2024-01-05T00:00:00+00:00",
            dependency.metadata["review_approved_at"],
        )
        self.assertEqual(
            sha256_bytes(b"verified"),
            dependency.metadata["review_acknowledgement_sha256"],
        )
        self.assertEqual(1, len(dependency.metadata["review_decisions"]))
        self.assertEqual("REVIEW_APPROVED", result.manifest["status"])

    def test_candidate_acceptance_must_match_frozen_submission_payload(self) -> None:
        ranked = pd.read_parquet(
            self.ranked / "corporate_action_filing_review.parquet"
        )
        ranked.loc[0, "accepted_at"] = "2024-01-03T18:00:00+00:00"
        ranked.to_parquet(
            self.ranked / "corporate_action_filing_review.parquet", index=False
        )
        manifest_path = self.ranked / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_sha256"] = sha256_file(
            self.ranked / "corporate_action_filing_review.parquet"
        )
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "template"
        ).path

        with self.assertRaisesRegex(ValueError, "captured catalog lineage"):
            propose_action_review(
                self.store,
                template,
                self._completed(template),
                self.root / "bad-acceptance-proposal",
                proposed_by="reviewer",
            )

    def test_one_transition_can_have_multiple_distinct_actions(self) -> None:
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "template"
        ).path
        completed = pd.read_csv(self._completed(template), dtype=str, keep_default_na=False)
        split = completed.iloc[0].copy()
        split["disposition"] = "ACTION_CONFIRMED"
        split["action_type"] = "SPLIT"
        split["effective_at"] = "2024-01-04T09:30:00-05:00"
        split["share_ratio"] = "2.0"
        split["review_note"] = "Verified a separate split in the same frozen filing."
        completed = pd.concat([completed, split.to_frame().T], ignore_index=True)
        path = self.root / "multi.csv"
        completed.to_csv(path, index=False)

        proposal = propose_action_review(
            self.store,
            template,
            path,
            self.root / "multi-proposal",
            proposed_by="reviewer",
            proposed_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(2, proposal.manifest["review_row_count"])
        frame = pd.read_parquet(proposal.path / "action_review.parquet")
        self.assertEqual({"RENAME", "SPLIT"}, set(frame["action_type"]))
        dependencies = proposal.manifest["source_dependencies"]
        self.assertEqual(1, len(dependencies))
        self.assertEqual(2, len(dependencies[0]["metadata"]["review_decisions"]))

    def test_distinct_securities_cannot_be_mixed_with_action_rows(self) -> None:
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "template"
        ).path
        completed = pd.read_csv(self._completed(template), dtype=str, keep_default_na=False)
        distinct = completed.iloc[0].copy()
        distinct["disposition"] = "DISTINCT_SECURITIES"
        for column in (
            "action_type",
            "announced_at",
            "effective_at",
            "pay_date",
            "share_ratio",
            "cash_per_share",
            "cost_basis_fraction",
        ):
            distinct[column] = ""
        distinct["terms_verified"] = "false"
        completed = pd.concat([completed, distinct.to_frame().T], ignore_index=True)
        path = self.root / "mixed.csv"
        completed.to_csv(path, index=False)

        with self.assertRaisesRegex(ValueError, "must be the only row"):
            propose_action_review(
                self.store,
                template,
                path,
                self.root / "mixed-proposal",
                proposed_by="reviewer",
            )


    def test_reorganization_one_to_one_succession_is_admitted(self) -> None:
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "reorg-template"
        ).path
        frame = pd.read_csv(
            template / "action_review.csv", dtype=str, keep_default_na=False
        )
        frame.loc[0, "disposition"] = "IDENTITY_CONTINUITY"
        frame.loc[0, "selected_review_candidate_id"] = "candidate-1"
        frame.loc[0, "action_type"] = "REORGANIZATION"
        frame.loc[0, "share_ratio"] = "1.0"
        frame.loc[0, "announced_at"] = "2024-01-02T13:00:00-05:00"
        frame.loc[0, "effective_at"] = "2024-01-03T09:30:00-05:00"
        frame.loc[0, "terms_verified"] = "true"
        frame.loc[0, "evidence_excerpt"] = (
            "The company changed its name and ticker at the opening of trading "
            "on January 3, 2024."
        )
        frame.loc[0, "review_note"] = (
            "Verified frozen submission: legal-entity continuation with a strict "
            "one-to-one share exchange."
        )
        path = self.root / "reorg-completed.csv"
        frame.to_csv(path, index=False, encoding="utf-8-sig")

        proposal = propose_action_review(
            self.store,
            template,
            path,
            self.root / "reorg-proposal",
            proposed_by="reviewer",
        )
        draft = pd.read_parquet(proposal.path / "action_review.parquet")
        self.assertEqual("REORGANIZATION", draft.iloc[0]["action_type"])
        self.assertEqual(1.0, float(draft.iloc[0]["share_ratio"]))

    def test_reorganization_rejects_non_one_to_one_ratio(self) -> None:
        template = prepare_action_review(
            self.requests, self.ranked, self.root / "reorg-bad-template"
        ).path
        frame = pd.read_csv(
            template / "action_review.csv", dtype=str, keep_default_na=False
        )
        frame.loc[0, "disposition"] = "IDENTITY_CONTINUITY"
        frame.loc[0, "selected_review_candidate_id"] = "candidate-1"
        frame.loc[0, "action_type"] = "REORGANIZATION"
        frame.loc[0, "share_ratio"] = "2.3348"
        frame.loc[0, "announced_at"] = "2024-01-02T13:00:00-05:00"
        frame.loc[0, "effective_at"] = "2024-01-03T09:30:00-05:00"
        frame.loc[0, "terms_verified"] = "true"
        frame.loc[0, "evidence_excerpt"] = (
            "The company changed its name and ticker at the opening of trading "
            "on January 3, 2024."
        )
        frame.loc[0, "review_note"] = "Ratio is not one-to-one."
        path = self.root / "reorg-bad.csv"
        frame.to_csv(path, index=False, encoding="utf-8-sig")

        with self.assertRaisesRegex(ValueError, "one-to-one"):
            propose_action_review(
                self.store,
                template,
                path,
                self.root / "reorg-bad-proposal",
                proposed_by="reviewer",
            )

    def test_reorganization_terms_validation_requires_stable_successor(self) -> None:
        from research_platform.us_pit.action_review import _validate_terms

        with self.assertRaisesRegex(ValueError, "stable successor"):
            _validate_terms("REORGANIZATION", 1.0, None, None, "", "")
        # One-to-one with a stable successor passes.
        _validate_terms("REORGANIZATION", 1.0, None, None, "us_isin_us0000000002", "")


if __name__ == "__main__":
    unittest.main()


class SpinoffMembershipContextTests(unittest.TestCase):
    """D2-B: SPINOFF may be approved without cost basis for membership
    replay, while execution consumers must refuse it."""

    def test_spinoff_without_cost_basis_admits_and_execution_gate_blocks(
        self,
    ) -> None:
        from research_platform.us_pit.action_review import (
            validate_execution_action_terms,
        )

        terms = {"ratio": 0.2, "cost_basis": None}
        # _validate_terms accepts the replay-context row:
        from research_platform.us_pit.action_review import _validate_terms

        _validate_terms("SPINOFF", terms["ratio"], None, terms["cost_basis"], "us_isin_s", "")
        with self.assertRaisesRegex(ValueError, "cost basis fraction must lie"):
            _validate_terms("SPINOFF", 0.2, None, 1.5, "us_isin_s", "")

        with self.assertRaisesRegex(ValueError, "requires cost_basis_fraction"):
            validate_execution_action_terms(
                {
                    "action_id": "a1",
                    "action_type": "SPINOFF",
                    "cost_basis_fraction": None,
                }
            )
        validate_execution_action_terms(
            {
                "action_id": "a2",
                "action_type": "SPINOFF",
                "cost_basis_fraction": 0.05,
            }
        )
        validate_execution_action_terms(
            {"action_id": "a3", "action_type": "REORGANIZATION"}
        )
