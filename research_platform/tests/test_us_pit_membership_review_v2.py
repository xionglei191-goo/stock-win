from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from research_platform.us_pit.service import USPITService
from research_platform.tests.test_us_pit_spglobal_events import (
    OBSERVED,
    _Transport,
)
from research_platform.us_pit.sources_spglobal import (
    SPGlobalSP500MembershipEventAdapter,
)
from research_platform.us_pit.spglobal_events import (
    SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION,
    SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION_V2,
    build_spglobal_event_candidates,
    review_spglobal_event_evidence,
)
from research_platform.us_pit.sources import SyncRequest
from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file

_ANNOUNCEMENT_LINK = "https://press.spglobal.com/2021-12-03-Apple-Set-to-Join-S-P-500"


def _normalization_with_sec_nport_anchor(root: Path, service: USPITService) -> Path:
    """One name-only sec_nport_ivv identity row; no iShares ticker crosscheck."""

    normalization = root / "norm"
    normalization.mkdir(exist_ok=True)
    store = service.store
    sec_source = store.put_bytes(b"sec-nport-ivv-apple-holding")
    frame = pd.DataFrame(
        [
            {
                "source_id": "sec_nport_ivv",
                "ticker": None,
                "share_class": "Common Stock",
                "as_of_date": "2022-01-31",
                "identity_candidate_key": "isin:US0378331005",
                "isin": "US0378331005",
                "cusip": "037833100",
                "content_sha256": sec_source.sha256,
                "source_row_number": 7,
                "issuer_name": "Apple Inc",
                "title": "Apple Inc",
            }
        ]
    )
    identity_path = normalization / "security_identity_candidates.parquet"
    frame.to_parquet(identity_path, index=False)
    (normalization / "manifest.json").write_bytes(
        canonical_json_bytes(
            {
                "normalization_id": "norm",
                "artifacts": {
                    "security_identity_candidates": {
                        "object_sha256": sha256_file(identity_path)
                    }
                },
            }
        )
    )
    return normalization


def _build_candidates(root: Path, service: USPITService, normalization: Path):
    batch = service.sync(
        SPGlobalSP500MembershipEventAdapter(
            transport=_Transport(_ANNOUNCEMENT_LINK),
            clock=lambda: OBSERVED,
            minimum_request_interval_seconds=0,
        ),
        SyncRequest(date(2021, 1, 1), date(2021, 12, 31), OBSERVED),
    )
    result = build_spglobal_event_candidates(
        service.store, [batch.batch_id], normalization, root / "events"
    )
    return [batch.batch_id], result


def _write_crosscheck_package(
    root: Path,
    store,
    *,
    event_id: str,
    resolved_security_id: str,
    ticker: str,
    evidence_bytes: bytes | None = None,
    accession_number: str = "0001234567-21-000001",
    filing_date: str = "2021-11-24",
    outcome: str = "RESOLVED",
    corrupt_evidence_digest: bool = False,
) -> Path:
    package = root / "crosscheck"
    package.mkdir(exist_ok=True)
    evidence = store.put_bytes(evidence_bytes or b"official-sec-nport-filing")
    evidence_sha256 = (
        "f" * 64 if corrupt_evidence_digest else evidence.sha256
    )
    frame = pd.DataFrame(
        [
            {
                "event_id": event_id,
                "ticker": ticker,
                "issuer_name": "Apple Inc",
                "expected_security_id": resolved_security_id,
                "expected_identifier": "US0378331005",
                "review_seed_identifier": "",
                "review_seed_source_url": "",
                "review_seed_note": "",
                "reviewer": "codex-sec-identity-review",
                "reviewed_at": OBSERVED.isoformat(),
                "review_outcome": outcome,
                "review_reason": "" if outcome == "RESOLVED" else "NO_EXACT_SEC_FILED_TICKER_IDENTIFIER_RECORD",
                "resolved_security_id": resolved_security_id,
                "identifier_type": "ISIN",
                "identifier_value": "US0378331005",
                "cusip": "037833100",
                "isin": "US0378331005",
                "source_url": "https://www.sec.gov/Archives/edgar/data/example.xml",
                "evidence_sha256": evidence_sha256,
                "filing_date": filing_date,
                "accession_number": accession_number,
                "evidence_excerpt": "COMMON STOCK US0378331005 AAPL Apple Inc",
            }
        ]
    )
    artifact = package / "sec_identity_crosschecks.parquet"
    frame.to_parquet(artifact, index=False)
    manifest = {
        "format_version": "us-pit-sec-identity-crosscheck-v1",
        "artifact": {
            "filename": artifact.name,
            "row_count": int(len(frame)),
            "sha256": sha256_file(artifact),
        },
        "resolved_count": 0,
        "blocked_count": 0,
        "candidate_only": True,
        "direct_build_allowed": False,
        "policy": {
            "late_filing_never_backdates_signal_availability": True,
            "raw_search_and_filing_objects_frozen": True,
            "sec_filing_is_identity_crosscheck_only": True,
            "ticker_and_identifier_share_local_record_window": True,
        },
        "source_batch_id": "example-batch",
        "status": "DATA_BLOCKED",
        "reviewer": "codex-sec-identity-review",
    }
    (package / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return package


class SecFiledIdentityCrosscheckReviewTests(unittest.TestCase):
    def _prepare(self, temporary: str):
        root = Path(temporary)
        service = USPITService(root / "pit")
        normalization = _normalization_with_sec_nport_anchor(root, service)
        batch_ids, candidates = _build_candidates(root, service, normalization)
        frame = pd.read_parquet(
            candidates.path / "membership_event_candidates.parquet"
        )
        apple = frame.loc[
            frame["ticker_at_announcement"].eq("AAPL")
            & frame["identity_match_basis"].eq(
                "EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR"
            )
        ]
        self.assertEqual(1, len(apple))
        event_id = str(apple.iloc[0]["event_candidate_id"])
        suggested = str(apple.iloc[0]["suggested_security_id"])
        return root, service, batch_ids, candidates, event_id, suggested

    def test_v2_admits_name_matched_anchor_via_dual_official_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, service, batch_ids, candidates, event_id, suggested = self._prepare(
                temporary
            )
            blocked = review_spglobal_event_evidence(
                service.store,
                batch_ids,
                candidates.path,
                root / "norm",
                root / "baseline-review",
                reviewed_at=OBSERVED,
            )
            self.assertEqual(0, blocked.manifest["approved_rows"])
            self.assertEqual(2, blocked.manifest["blocked_rows"])  # AAPL + OLD Corp
            self.assertEqual(
                SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION,
                blocked.manifest["format_version"],
            )

            package = _write_crosscheck_package(
                root,
                service.store,
                event_id=event_id,
                resolved_security_id=suggested,
                ticker="AAPL",
            )
            reviewed = review_spglobal_event_evidence(
                service.store,
                batch_ids,
                candidates.path,
                root / "norm",
                root / "v2-review",
                reviewed_at=OBSERVED,
                identity_crosscheck_dir=package,
            )
            self.assertEqual(1, reviewed.manifest["approved_rows"])
            self.assertEqual(1, reviewed.manifest["blocked_rows"])  # OLD Corp stays
            self.assertFalse(reviewed.manifest["direct_build_allowed"])
            self.assertEqual(
                SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION_V2,
                reviewed.manifest["format_version"],
            )
            crosscheck_input = reviewed.manifest["identity_crosscheck_input"]
            self.assertEqual(1, crosscheck_input["approved_via_sec_filed_crosscheck"])
            approved = pd.read_parquet(reviewed.path / "membership_events.parquet")
            self.assertIn("CODEX_DIRECT_EVIDENCE_REVIEW_V2", approved.iloc[0]["review_note"])
            self.assertIn(suggested, str(approved.iloc[0]["security_id"]))

    def test_tampered_filing_evidence_rejects_the_whole_crosscheck_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, service, batch_ids, candidates, event_id, suggested = self._prepare(
                temporary
            )
            package = _write_crosscheck_package(
                root,
                service.store,
                event_id=event_id,
                resolved_security_id=suggested,
                ticker="AAPL",
            )
            package = _write_crosscheck_package(
                root,
                service.store,
                event_id=event_id,
                resolved_security_id=suggested,
                ticker="AAPL",
                corrupt_evidence_digest=True,
            )
            with self.assertRaises(ValueError):
                review_spglobal_event_evidence(
                    service.store,
                    batch_ids,
                    candidates.path,
                    root / "norm",
                    root / "v2-review-tampered",
                    reviewed_at=OBSERVED,
                    identity_crosscheck_dir=package,
                )

    def test_security_id_mismatch_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, service, batch_ids, candidates, event_id, _suggested = self._prepare(
                temporary
            )
            package = _write_crosscheck_package(
                root,
                service.store,
                event_id=event_id,
                resolved_security_id="us_isin_us9999999999",
                ticker="AAPL",
            )
            reviewed = review_spglobal_event_evidence(
                service.store,
                batch_ids,
                candidates.path,
                root / "norm",
                root / "v2-review-mismatch",
                reviewed_at=OBSERVED,
                identity_crosscheck_dir=package,
            )
            self.assertEqual(0, reviewed.manifest["approved_rows"])
            self.assertEqual(2, reviewed.manifest["blocked_rows"])

    def test_blocked_crosscheck_row_never_admits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, service, batch_ids, candidates, event_id, suggested = self._prepare(
                temporary
            )
            package = _write_crosscheck_package(
                root,
                service.store,
                event_id=event_id,
                resolved_security_id=suggested,
                ticker="AAPL",
                outcome="BLOCKED",
            )
            reviewed = review_spglobal_event_evidence(
                service.store,
                batch_ids,
                candidates.path,
                root / "norm",
                root / "v2-review-blocked-row",
                reviewed_at=OBSERVED,
                identity_crosscheck_dir=package,
            )
            self.assertEqual(0, reviewed.manifest["approved_rows"])


if __name__ == "__main__":
    unittest.main()
