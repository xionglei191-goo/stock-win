from __future__ import annotations

import json
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from research_platform.us_pit import (
    LicenseClass,
    SourceDependency,
    SourceRole,
    ReviewWorkspaceError,
    USPITReviewWorkspaceAssembler,
    USPITStore,
    stable_security_id,
)
from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file, sha256_json
from research_platform.us_pit.official_normalize import OfficialNormalizationResult
from research_platform.us_pit.quality import REQUIRED_ARTIFACT_COLUMNS


def _empty(dataset: str) -> pd.DataFrame:
    return pd.DataFrame(columns=sorted(REQUIRED_ARTIFACT_COLUMNS[dataset]))


class ReviewWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = USPITStore(self.root / "store")

    def _dependency(
        self,
        *,
        dataset: str = "fund_holdings_observed",
        role: SourceRole = SourceRole.SIGNAL_INPUT,
        source_id: str = "ishares_ivv_holdings",
        as_of_date: str | None = "2024-01-31",
        published_at: str | None = "2024-01-31T20:00:00+00:00",
    ) -> SourceDependency:
        reference = self.store.put_bytes(f"{dataset}:{source_id}".encode())
        return SourceDependency(
            source_id=source_id,
            source_version="1",
            role=role,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=reference.sha256,
            observed_at="2024-01-31T20:01:00+00:00",
            published_at=published_at,
            as_of_date=as_of_date,
            url="https://official.example.com/evidence",
            dataset=dataset,
        )

    def _normalization(
        self,
        dependency: SourceDependency,
        *,
        issue: bool = False,
    ) -> OfficialNormalizationResult:
        normalization_id = "a" * 64
        root = self.store.root / "normalized" / "official" / normalization_id
        root.mkdir(parents=True)
        holding_id = "h" * 64
        holding = pd.DataFrame(
            [
                {
                    "holding_candidate_id": holding_id,
                    "fund_ticker": "IVV",
                    "as_of_date": dependency.as_of_date,
                    "published_at": dependency.published_at,
                    "observed_at": dependency.observed_at,
                    "eligible_from": dependency.observed_at,
                    "signal_eligible": True,
                    "source_id": dependency.source_id,
                    "source_version": dependency.source_version,
                    "content_sha256": dependency.object_sha256,
                    "evidence_role": dependency.role.value,
                    "license_class": dependency.license_class.value,
                    "url": dependency.url,
                    "source_row_number": 1,
                    "issuer_name": "Apple Inc",
                    "lei": "549300EXAMPLE",
                    "cik": "0000320193",
                    "title": "Common Stock",
                    "ticker": "AAPL",
                    "share_class": None,
                    "cusip_raw": "037833100",
                    "cusip": "037833100",
                    "isin_raw": "US0378331005",
                    "isin": "US0378331005",
                    "identity_candidate_key": "isin:US0378331005",
                    "asset_category": "Equity",
                    "currency": "USD",
                    "quantity": 1.0,
                    "market_value_usd": 100.0,
                    "weight_percent": 1.0,
                }
            ]
        )
        identity = holding[
            [
                "identity_candidate_key",
                "holding_candidate_id",
                "issuer_name",
                "lei",
                "cik",
                "title",
                "ticker",
                "share_class",
                "cusip_raw",
                "cusip",
                "isin_raw",
                "isin",
                "as_of_date",
                "observed_at",
                "source_id",
                "source_version",
                "content_sha256",
                "evidence_role",
                "url",
                "source_row_number",
            ]
        ].copy()
        issues = pd.DataFrame(
            [
                {
                    "issue_id": "i" * 64,
                    "severity": "HIGH",
                    "code": "IDENTITY_REVIEW_REQUIRED",
                    "source_id": dependency.source_id,
                    "content_sha256": dependency.object_sha256,
                    "source_row_number": 1,
                    "field": "identity",
                    "value": "AAPL",
                    "message": "review",
                    "requires_manual_review": True,
                }
            ]
            if issue
            else [],
            columns=[
                "issue_id",
                "severity",
                "code",
                "source_id",
                "content_sha256",
                "source_row_number",
                "field",
                "value",
                "message",
                "requires_manual_review",
            ],
        )
        frames = {
            "fund_holdings_observed_candidate": holding,
            "security_identity_candidates": identity,
            "normalization_issues": issues,
        }
        descriptors = {}
        for name, frame in frames.items():
            path = root / f"{name}.parquet"
            frame.to_parquet(path, index=False)
            descriptors[name] = {
                "filename": path.name,
                "object_sha256": sha256_file(path),
            }
        manifest = {
            "normalization_id": normalization_id,
            "candidate_only": True,
            "direct_build_allowed": False,
            "artifacts": descriptors,
        }
        (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        return OfficialNormalizationResult(normalization_id, root, manifest)

    @staticmethod
    def _calendar(path: Path) -> None:
        sessions = pd.bdate_range("2024-01-02", "2024-02-29")
        pd.DataFrame(
            {
                "session_date": sessions,
                "market_open": [f"{day.date()}T09:30:00-05:00" for day in sessions],
                "market_close": [f"{day.date()}T16:00:00-05:00" for day in sessions],
            }
        ).to_parquet(path, index=False)

    def test_stable_id_prefers_isin_and_rejects_ticker_identity(self) -> None:
        self.assertEqual(
            stable_security_id(isin="US0378331005", cusip="037833100"),
            "us_isin_us0378331005",
        )
        self.assertEqual(
            stable_security_id(cusip="037833100"),
            "us_cusip_037833100",
        )
        with self.assertRaisesRegex(ValueError, "ISIN or CUSIP"):
            stable_security_id()

    def test_evidence_rows_require_explicit_row_approval(self) -> None:
        dependency = self._dependency(dataset="membership_events")
        path = self.root / "membership_events.parquet"
        row = {
            "event_id": "event-1",
            "security_id": "us_isin_us0378331005",
            "event_type": "ADD",
            "announced_at": "2024-01-01T12:00:00-05:00",
            "effective_at": "2024-01-02T09:30:00-05:00",
            "source_id": dependency.source_id,
            "evidence_sha256": dependency.object_sha256,
            "approved": False,
            "review_note": "",
        }
        pd.DataFrame([row]).to_parquet(path, index=False)
        source_keys = {
            (dependency.source_id, dependency.dataset, dependency.object_sha256)
        }
        with self.assertRaisesRegex(
            Exception, "without explicit evidence review approval"
        ):
            USPITReviewWorkspaceAssembler._reviewed_evidence_table(
                path, "membership_events", source_keys
            )
        row["approved"] = True
        row["review_note"] = "Verified against the frozen official announcement."
        pd.DataFrame([row]).to_parquet(path, index=False)
        accepted = USPITReviewWorkspaceAssembler._reviewed_evidence_table(
            path, "membership_events", source_keys
        )
        self.assertNotIn("approved", accepted.columns)
        self.assertNotIn("review_note", accepted.columns)

    def test_cusip_only_history_and_later_isin_share_one_canonical_id(self) -> None:
        from research_platform.us_pit.review_workspace import (
            _candidate_identity_components,
        )

        values = pd.DataFrame(
            [
                {
                    "holding_candidate_id": "old",
                    "isin": None,
                    "cusip": "037833100",
                },
                {
                    "holding_candidate_id": "new",
                    "isin": "US0378331005",
                    "cusip": "037833100",
                },
            ]
        )
        resolved = _candidate_identity_components(values)
        self.assertEqual(resolved["old"], "us_isin_us0378331005")
        self.assertEqual(resolved["new"], "us_isin_us0378331005")

    def test_review_suffix_and_real_exchange_mic_create_cboe_alias(self) -> None:
        resolved = pd.DataFrame(
            [
                {
                    "security_id": "us_isin_us12503m1080",
                    "ticker": pd.NA,
                    "ticker_review": "CBOE",
                    "exchange": pd.NA,
                    "exchange_review": "Cboe BZX",
                    "valid_from_resolved": pd.Timestamp("2018-01-31"),
                    "valid_to_resolved": pd.NaT,
                },
                {
                    "security_id": "us_isin_us78462f1030",
                    "ticker": "SPY",
                    "exchange": "NYSE Arca",
                    "valid_from_resolved": pd.Timestamp("1993-01-29"),
                    "valid_to_resolved": pd.NaT,
                },
            ]
        )

        aliases = USPITReviewWorkspaceAssembler._listing_aliases(resolved)
        cboe = aliases.loc[
            aliases["security_id"].eq("us_isin_us12503m1080")
        ].iloc[0]
        spy = aliases.loc[
            aliases["security_id"].eq("us_isin_us78462f1030")
        ].iloc[0]
        self.assertEqual((cboe["vendor_code"], cboe["exchange"]), ("CBOE.US", "BATS"))
        self.assertEqual("ARCX", spy["exchange"])

    def test_reviewed_aliases_replace_automatic_lineage_intervals(self) -> None:
        resolved = pd.DataFrame(
            [
                {
                    "security_id": "us_isin_us3696043013",
                    "ticker": "GE",
                    "exchange": "NYSE",
                    "valid_from_resolved": pd.Timestamp("2021-08-31"),
                    "valid_to_resolved": pd.NaT,
                }
            ]
        )
        review = pd.DataFrame(
            [
                {
                    "security_id": "us_isin_us3696041033",
                    "ticker": "GE",
                    "vendor_code": "GE.US",
                    "exchange_mic": "XNYS",
                    "valid_from": "2018-08-17",
                    "valid_to": "2021-07-30",
                },
                {
                    "security_id": "us_isin_us3696043013",
                    "ticker": "GE",
                    "vendor_code": "GE.US",
                    "exchange_mic": "XNYS",
                    "valid_from": "2021-08-02",
                    "valid_to": None,
                },
            ]
        )

        aliases = USPITReviewWorkspaceAssembler._listing_aliases(resolved, review)
        self.assertEqual(2, len(aliases))
        successor = aliases.loc[
            aliases["security_id"].eq("us_isin_us3696043013")
        ].iloc[0]
        self.assertEqual(pd.Timestamp("2021-08-02"), successor["valid_from"])

    def test_listing_alias_review_requires_hash_bound_package(self) -> None:
        path = self.root / "listing_alias_review.parquet"
        manifest_path = self.root / "listing_alias_review_manifest.json"
        columns = [
            "alias_review_id", "binding_type", "security_id", "ticker",
            "vendor_code", "exchange_mic", "valid_from", "valid_to",
            "action_id", "evidence_source_id", "evidence_sha256", "approved",
            "review_note", "approved_by", "approved_at", "approval_id",
        ]
        pd.DataFrame(columns=columns).to_parquet(path, index=False)
        assembler = USPITReviewWorkspaceAssembler(self.store)
        with self.assertRaisesRegex(ReviewWorkspaceError, "package manifest"):
            assembler._read_listing_alias_review(
                path, manifest_path, (), pd.DataFrame()
            )

        identity = {
            "format_version": "us-pit-listing-alias-review-package-v1",
            "listing_alias_review_sha256": sha256_file(path),
            "row_count": 0,
            "approved_by": "reviewer",
            "approved_at": "2026-08-29T00:00:00+00:00",
        }
        manifest = {**identity, "package_id": sha256_json(identity)}
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        accepted = assembler._read_listing_alias_review(
            path, manifest_path, (), pd.DataFrame()
        )
        self.assertTrue(accepted.empty)

        manifest["row_count"] = 1
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        with self.assertRaisesRegex(ReviewWorkspaceError, "identity"):
            assembler._read_listing_alias_review(
                path, manifest_path, (), pd.DataFrame()
            )

    def test_listing_alias_intervals_reject_overlap(self) -> None:
        resolved = pd.DataFrame(
            [
                {
                    "security_id": "us_isin_us0378331005",
                    "ticker": "AAPL",
                    "exchange": "XNAS",
                    "valid_from_resolved": pd.Timestamp("2020-01-01"),
                    "valid_to_resolved": pd.NaT,
                }
            ]
        )
        review = pd.DataFrame(
            [
                {
                    "security_id": "us_isin_us0378331005",
                    "ticker": "AAPL",
                    "vendor_code": "AAPL.US",
                    "exchange_mic": "XNAS",
                    "valid_from": "2020-01-01",
                    "valid_to": "2024-01-31",
                },
                {
                    "security_id": "us_isin_us0378331005",
                    "ticker": "AAPL",
                    "vendor_code": "AAPL.US",
                    "exchange_mic": "XNAS",
                    "valid_from": "2024-01-31",
                    "valid_to": None,
                },
            ]
        )
        with self.assertRaisesRegex(ReviewWorkspaceError, "overlap"):
            USPITReviewWorkspaceAssembler._listing_aliases(resolved, review)

    def test_missing_review_produces_immutable_blocked_workspace(self) -> None:
        dependency = self._dependency()
        batch = self.store.write_source_batch([dependency])
        normalization = self._normalization(dependency)
        review = self.root / "review"
        review.mkdir()

        assembler = USPITReviewWorkspaceAssembler(self.store)
        result = assembler.assemble(
            normalization,
            review,
            self.root / "workspaces",
            decision_start=date(2024, 1, 1),
            decision_end=date(2024, 2, 29),
            source_batch_ids=[batch.batch_id],
        )
        repeated = assembler.assemble(
            normalization,
            review,
            self.root / "workspaces",
            decision_start=date(2024, 1, 1),
            decision_end=date(2024, 2, 29),
            source_batch_ids=[batch.batch_id],
        )

        self.assertEqual(repeated.workspace_id, result.workspace_id)
        self.assertEqual(result.status, "DATA_BLOCKED")
        gaps = json.loads((result.path / "gap_report.json").read_text("utf-8"))
        self.assertIn("IDENTITY_NOT_EXPLICITLY_APPROVED", gaps["counts"])
        self.assertTrue((result.path / "membership_monthly.parquet").is_file())
        self.assertEqual(len(list(result.path.glob("*.parquet"))), len(REQUIRED_ARTIFACT_COLUMNS))
        receipt = json.loads(
            (result.path / "manifest.cas.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            sha256_file(result.path / "manifest.json"),
            receipt["manifest_sha256"],
        )
        self.assertEqual(
            (result.path / "manifest.json").read_bytes(),
            self.store.object_path(receipt["cas_object_sha256"]).read_bytes(),
        )

        receipt_path = result.path / "manifest.cas.json"
        receipt_path.chmod(stat.S_IWRITE | stat.S_IREAD)
        receipt_path.write_text(
            json.dumps({**receipt, "manifest_size_bytes": 1}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ReviewWorkspaceError, "CAS receipt"):
            assembler.assemble(
                normalization,
                review,
                self.root / "workspaces",
                decision_start=date(2024, 1, 1),
                decision_end=date(2024, 2, 29),
                source_batch_ids=[batch.batch_id],
            )

    def test_validation_anchor_stable_id_does_not_require_a_trading_alias(self) -> None:
        dependency = self._dependency(
            role=SourceRole.VALIDATION_ANCHOR,
            source_id="sec_nport_ivv",
            published_at="2024-02-15T00:00:00+00:00",
        )
        batch = self.store.write_source_batch([dependency])
        normalization = self._normalization(dependency)
        holdings = pd.read_parquet(
            normalization.path / "fund_holdings_observed_candidate.parquet"
        )
        holdings["evidence_role"] = SourceRole.VALIDATION_ANCHOR.value
        holdings.to_parquet(
            normalization.path / "fund_holdings_observed_candidate.parquet",
            index=False,
        )
        identity = pd.read_parquet(
            normalization.path / "security_identity_candidates.parquet"
        )
        identity["evidence_role"] = SourceRole.VALIDATION_ANCHOR.value
        identity.to_parquet(
            normalization.path / "security_identity_candidates.parquet",
            index=False,
        )
        manifest_path = normalization.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["artifacts"]["fund_holdings_observed_candidate"][
            "object_sha256"
        ] = sha256_file(
            normalization.path / "fund_holdings_observed_candidate.parquet"
        )
        manifest["artifacts"]["security_identity_candidates"][
            "object_sha256"
        ] = sha256_file(
            normalization.path / "security_identity_candidates.parquet"
        )
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        normalization = OfficialNormalizationResult(
            normalization.normalization_id, normalization.path, manifest
        )
        review = self.root / "anchor-review"
        review.mkdir()

        result = USPITReviewWorkspaceAssembler(self.store).assemble(
            normalization,
            review,
            self.root / "anchor-workspaces",
            decision_start=date(2024, 1, 1),
            decision_end=date(2024, 2, 29),
            source_batch_ids=[batch.batch_id],
        )

        gaps = json.loads((result.path / "gap_report.json").read_text("utf-8"))
        self.assertNotIn("IDENTITY_NOT_EXPLICITLY_APPROVED", gaps["counts"])
        self.assertIn(
            "ISSUER_IDENTITY_NOT_EXPLICITLY_APPROVED", gaps["counts"]
        )
        self.assertEqual(
            1, len(pd.read_parquet(result.path / "security_master.parquet"))
        )
        self.assertTrue(
            pd.read_parquet(result.path / "listing_aliases.parquet").empty
        )

    def test_approved_identity_replays_membership_but_does_not_self_attest_lifecycle(self) -> None:
        dependency = self._dependency()
        batch = self.store.write_source_batch([dependency])
        normalization = self._normalization(dependency)
        review = self.root / "review"
        review.mkdir()
        pd.DataFrame(
            [
                {
                    "holding_candidate_id": "h" * 64,
                    "approved": True,
                    "issuer_id": "us_issuer_cik_0000320193",
                    "exchange": "XNAS",
                    "valid_from": "1980-12-12",
                    "valid_to": None,
                    "review_note": "Matched official identifiers and listing.",
                    "resolved_issue_ids": None,
                }
            ]
        ).to_parquet(review / "identity_review.parquet", index=False)

        result = USPITReviewWorkspaceAssembler(self.store).assemble(
            normalization,
            review,
            self.root / "workspaces",
            decision_start=date(2024, 1, 1),
            decision_end=date(2024, 2, 29),
            source_batch_ids=[batch.batch_id],
        )
        membership = pd.read_parquet(result.path / "membership_monthly.parquet")
        lifecycle = pd.read_parquet(result.path / "lifecycle_reconciliations.parquet")

        self.assertEqual(set(membership["security_id"]), {"us_isin_us0378331005"})
        self.assertEqual(len(membership), 2)
        self.assertTrue(lifecycle.empty)
        gaps = json.loads((result.path / "gap_report.json").read_text("utf-8"))
        self.assertIn("EMPTY_REQUIRED_ARTIFACT", gaps["counts"])

    def test_high_issue_requires_explicit_issue_id_resolution(self) -> None:
        dependency = self._dependency()
        batch = self.store.write_source_batch([dependency])
        normalization = self._normalization(dependency, issue=True)
        review = self.root / "review"
        review.mkdir()
        base = {
            "holding_candidate_id": "h" * 64,
            "approved": True,
            "issuer_id": "us_issuer_cik_0000320193",
            "exchange": "XNAS",
            "valid_from": "1980-12-12",
            "valid_to": None,
            "review_note": "Reviewed conflict against issuer filing.",
            "resolved_issue_ids": None,
        }
        pd.DataFrame([base]).to_parquet(review / "identity_review.parquet", index=False)
        assembler = USPITReviewWorkspaceAssembler(self.store)
        blocked = assembler.assemble(
            normalization,
            review,
            self.root / "blocked",
            decision_start=date(2024, 1, 1),
            decision_end=date(2024, 2, 29),
            source_batch_ids=[batch.batch_id],
        )
        gaps = json.loads((blocked.path / "gap_report.json").read_text("utf-8"))
        self.assertIn("IDENTITY_REVIEW_REQUIRED", gaps["counts"])

        base["resolved_issue_ids"] = "i" * 64
        pd.DataFrame([base]).to_parquet(review / "identity_review.parquet", index=False)
        resolved = assembler.assemble(
            normalization,
            review,
            self.root / "resolved",
            decision_start=date(2024, 1, 1),
            decision_end=date(2024, 2, 29),
            source_batch_ids=[batch.batch_id],
        )
        resolved_gaps = json.loads((resolved.path / "gap_report.json").read_text("utf-8"))
        self.assertNotIn("IDENTITY_REVIEW_REQUIRED", resolved_gaps["counts"])


if __name__ == "__main__":
    unittest.main()
