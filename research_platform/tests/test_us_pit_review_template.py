from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from research_platform.us_pit.hashing import sha256_file, sha256_json
from research_platform.us_pit.quality import REQUIRED_ARTIFACT_COLUMNS
from research_platform.us_pit.review_template import prepare_review_template


class USPITReviewTemplateTests(unittest.TestCase):
    def test_template_imports_only_an_integrity_checked_action_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalization = root / ("c" * 64)
            normalization.mkdir()
            holdings = pd.DataFrame(
                [{
                    "holding_candidate_id": "candidate-1",
                    "content_sha256": "1" * 64,
                    "source_row_number": 1,
                    "source_id": "sec_nport_ivv",
                    "as_of_date": "2021-06-30",
                    "eligible_from": "2021-08-01T00:00:00+00:00",
                    "signal_eligible": False,
                    "issuer_name": "Example", "title": "Example",
                    "ticker": "OLD", "exchange": "NYSE",
                }]
            )
            identities = pd.DataFrame(
                [{
                    "holding_candidate_id": "candidate-1",
                    "identity_candidate_key": "isin:US0000000001",
                    "isin": "US0000000001", "cusip": "000000001",
                }]
            )
            issues = pd.DataFrame(
                columns=["issue_id", "severity", "content_sha256", "source_row_number"]
            )
            artifacts = {}
            for name, frame in (
                ("fund_holdings_observed_candidate", holdings),
                ("security_identity_candidates", identities),
                ("normalization_issues", issues),
            ):
                path = normalization / f"{name}.parquet"
                frame.to_parquet(path, index=False)
                artifacts[name] = {"filename": path.name, "object_sha256": sha256_file(path)}
            (normalization / "manifest.json").write_text(
                json.dumps({
                    "normalization_id": normalization.name,
                    "candidate_only": True,
                    "direct_build_allowed": False,
                    "artifacts": artifacts,
                }), encoding="utf-8",
            )

            approval = root / "action-approval"
            approval.mkdir()
            action = {column: "" for column in REQUIRED_ARTIFACT_COLUMNS["corporate_actions"]}
            action.update({
                "action_id": "action-1",
                "security_id": "us_isin_us0000000001",
                "action_type": "RENAME",
                "announced_at": "2021-07-29T12:00:00-04:00",
                "effective_at": "2021-07-30T09:30:00-04:00",
                "terms_verified": True,
                "source_id": "sec_reviewed_corporate_action",
                "evidence_sha256": "2" * 64,
                "successor_security_id": "us_isin_us0000000001",
            })
            action_frame = pd.DataFrame([action])
            action_path = approval / "corporate_actions.parquet"
            action_frame.to_parquet(action_path, index=False)
            decisions = pd.DataFrame([{
                "action_id": "action-1",
                "review_note": "Verified against the frozen SEC submission.",
            }])
            decisions_path = approval / "review_decisions.parquet"
            decisions.to_parquet(decisions_path, index=False)
            approval_manifest = {
                "status": "REVIEW_APPROVED",
                "direct_build_allowed": False,
                "corporate_actions_sha256": sha256_file(action_path),
                "review_decisions_sha256": sha256_file(decisions_path),
                "source_batch_id": "batch-1",
                "proposal_sha256": "3" * 64,
            }
            approval_manifest["approval_id"] = sha256_json(approval_manifest)
            (approval / "manifest.json").write_text(
                json.dumps(approval_manifest), encoding="utf-8"
            )

            result = prepare_review_template(
                normalization, root / "out",
                decision_start=date(2021, 8, 1),
                decision_end=date(2021, 8, 31),
                action_review_dir=approval,
            )
            imported = pd.read_parquet(result.path / "corporate_actions.parquet")
            self.assertEqual(1, len(imported))
            self.assertTrue(bool(imported.iloc[0]["approved"]))
            self.assertEqual(
                "batch-1",
                result.manifest["linked_inputs"]["action_review"]["source_batch_id"],
            )

    def test_template_is_unapproved_and_exposes_missing_historical_baselines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalization = root / ("a" * 64)
            normalization.mkdir()
            holdings = pd.DataFrame(
                [
                    {
                        "holding_candidate_id": "candidate-1",
                        "content_sha256": "1" * 64,
                        "source_row_number": 1,
                        "source_id": "ishares_ivv_holdings",
                        "as_of_date": "2026-08-11",
                        "eligible_from": "2026-08-13T08:00:00+00:00",
                        "signal_eligible": True,
                        "issuer_name": "Apple Inc.",
                        "title": "Apple Inc.",
                        "ticker": "AAPL",
                        "exchange": "NASDAQ",
                    }
                ]
            )
            identities = pd.DataFrame(
                [
                    {
                        "holding_candidate_id": "candidate-1",
                        "identity_candidate_key": None,
                        "isin": None,
                        "cusip": None,
                    }
                ]
            )
            issues = pd.DataFrame(
                [
                    {
                        "issue_id": "issue-1",
                        "severity": "HIGH",
                        "content_sha256": "1" * 64,
                        "source_row_number": 1,
                    }
                ]
            )
            artifacts = {}
            for name, frame in (
                ("fund_holdings_observed_candidate", holdings),
                ("security_identity_candidates", identities),
                ("normalization_issues", issues),
            ):
                filename = f"{name}.parquet"
                frame.to_parquet(normalization / filename, index=False)
                artifacts[name] = {
                    "filename": filename,
                    "object_sha256": sha256_file(normalization / filename),
                }
            manifest = {
                "normalization_id": normalization.name,
                "candidate_only": True,
                "direct_build_allowed": False,
                "artifacts": artifacts,
            }
            (normalization / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            output = root / "review"
            result = prepare_review_template(
                normalization,
                output,
                decision_start=date(2021, 8, 1),
                decision_end=date(2026, 7, 31),
            )
            self.assertEqual("DATA_BLOCKED", result.manifest["status"])
            self.assertFalse(result.manifest["approved"])
            review = pd.read_parquet(output / "identity_review.parquet")
            self.assertFalse(bool(review.iloc[0]["approved"]))
            self.assertEqual("NASDAQ", review.iloc[0]["exchange"])
            gaps = json.loads((output / "review_gaps.json").read_text(encoding="utf-8"))
            self.assertEqual(60, result.manifest["decision_months"])
            self.assertEqual(60, gaps[1]["count"])
            self.assertEqual(
                sha256_file(output / "identity_review.parquet"),
                result.manifest["artifacts"]["identity_review.parquet"],
            )

    def test_template_links_unapproved_membership_review_and_blocking_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalization = root / ("b" * 64)
            normalization.mkdir()
            holdings = pd.DataFrame(
                [{
                    "holding_candidate_id": "candidate-1",
                    "content_sha256": "1" * 64,
                    "source_row_number": 1,
                    "source_id": "sec_nport_ivv",
                    "as_of_date": "2021-06-30",
                    "eligible_from": "2021-08-01T00:00:00+00:00",
                    "signal_eligible": False,
                    "issuer_name": "Apple Inc", "title": "Apple Inc",
                    "ticker": "AAPL", "exchange": "NASDAQ",
                }]
            )
            identities = pd.DataFrame(
                [{
                    "holding_candidate_id": "candidate-1",
                    "identity_candidate_key": "isin:US0378331005",
                    "isin": "US0378331005", "cusip": "037833100",
                }]
            )
            issues = pd.DataFrame(
                columns=["issue_id", "severity", "content_sha256", "source_row_number"]
            )
            artifacts = {}
            for name, frame in (
                ("fund_holdings_observed_candidate", holdings),
                ("security_identity_candidates", identities),
                ("normalization_issues", issues),
            ):
                path = normalization / f"{name}.parquet"
                frame.to_parquet(path, index=False)
                artifacts[name] = {"filename": path.name, "object_sha256": sha256_file(path)}
            (normalization / "manifest.json").write_text(
                json.dumps({
                    "normalization_id": normalization.name,
                    "candidate_only": True,
                    "direct_build_allowed": False,
                    "artifacts": artifacts,
                }), encoding="utf-8",
            )
            review = root / "membership-review"
            review.mkdir()
            membership = pd.DataFrame(
                [{
                    "event_id": "event-1", "security_id": "us_isin_us0378331005",
                    "event_type": "ADD", "announced_at": "2021-08-01T12:00:00Z",
                    "effective_at": "2021-08-02T13:30:00Z",
                    "source_id": "spglobal", "evidence_sha256": "2" * 64,
                    "approved": False, "review_note": "",
                }]
            )
            membership_path = review / "membership_events.parquet"
            membership.to_parquet(membership_path, index=False)
            (review / "manifest.json").write_text(json.dumps({
                "status": "REVIEW_REQUIRED", "direct_build_allowed": False,
                "artifact_sha256": sha256_file(membership_path),
            }), encoding="utf-8")
            audit = root / "audit"
            audit.mkdir()
            audit_value = {
                "audit_id": "3" * 64,
                "gap_counts": {"QUARTERLY_ANCHOR_RECONCILIATION_FAILED": 1},
            }
            audit_path = audit / "membership_audit.json"
            audit_path.write_text(json.dumps(audit_value), encoding="utf-8")
            (audit / "manifest.json").write_text(json.dumps({
                "audit_id": audit_value["audit_id"], "candidate_only": True,
                "direct_build_allowed": False,
                "membership_audit_sha256": sha256_file(audit_path),
            }), encoding="utf-8")
            result = prepare_review_template(
                normalization, root / "out",
                decision_start=date(2021, 8, 1), decision_end=date(2021, 8, 31),
                membership_review_dir=review, membership_audit_dir=audit,
            )
            copied = pd.read_parquet(result.path / "membership_events.parquet")
            self.assertEqual(1, len(copied))
            self.assertFalse(bool(copied.iloc[0]["approved"]))
            self.assertEqual(audit_value["audit_id"], result.manifest["linked_inputs"]["membership_audit"]["audit_id"])


if __name__ == "__main__":
    unittest.main()
