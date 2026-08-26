from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.us_pit.evidence_requests import (
    build_transition_evidence_requests,
)
from research_platform.us_pit.hashing import sha256_file


class USPITEvidenceRequestTests(unittest.TestCase):
    def test_transition_queue_is_non_buildable_and_never_infers_action_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = root / "audit"
            audit.mkdir()
            audit_value = {"audit_id": "a" * 64}
            audit_path = audit / "membership_audit.json"
            audit_path.write_text(json.dumps(audit_value), encoding="utf-8")
            transitions = pd.DataFrame(
                [{
                    "anchor_date": "2024-12-31",
                    "predecessor_security_id": "us_isin_old",
                    "successor_security_id": "us_isin_new",
                    "predecessor_name": "Old Corp", "successor_name": "New Corp",
                    "predecessor_isin": "US0000000001", "successor_isin": "US0000000002",
                    "predecessor_cusip": "000000001", "successor_cusip": "000000002",
                    "predecessor_lei": "OLDLEI", "successor_lei": "NEWLEI",
                    "predecessor_cik": "", "successor_cik": "",
                    "predecessor_ticker": "OLD", "successor_ticker": "NEW",
                    "match_basis": "SAME_NORMALIZED_ISSUER",
                }]
            )
            transitions_path = audit / "identity_transition_candidates.parquet"
            transitions.to_parquet(transitions_path, index=False)
            (audit / "manifest.json").write_text(json.dumps({
                "membership_audit_sha256": sha256_file(audit_path),
                "identity_transition_candidates_sha256": sha256_file(transitions_path),
                "candidate_only": True, "direct_build_allowed": False,
            }), encoding="utf-8")
            result = build_transition_evidence_requests(audit, root / "requests")
            frame = pd.read_parquet(
                result.path / "corporate_action_evidence_requests.parquet"
            )
            self.assertEqual("EVIDENCE_REQUIRED", frame.iloc[0]["status"])
            self.assertEqual("", frame.iloc[0]["evidence_sha256"])
            self.assertFalse(bool(frame.iloc[0]["approved"]))
            self.assertFalse(result.manifest["direct_build_allowed"])
            self.assertEqual("OLD", frame.iloc[0]["predecessor_ticker"])

    def test_missing_candidate_fields_are_stable_empty_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = root / "audit"
            audit.mkdir()
            audit_path = audit / "membership_audit.json"
            audit_path.write_text(json.dumps({"audit_id": "b" * 64}), encoding="utf-8")
            transitions_path = audit / "identity_transition_candidates.parquet"
            pd.DataFrame([{
                "anchor_date": "2025-01-31",
                "predecessor_security_id": "us_isin_old",
                "successor_security_id": "us_isin_new",
                "predecessor_cik": pd.NA,
                "successor_cik": None,
            }]).to_parquet(transitions_path, index=False)
            (audit / "manifest.json").write_text(json.dumps({
                "membership_audit_sha256": sha256_file(audit_path),
                "identity_transition_candidates_sha256": sha256_file(transitions_path),
                "candidate_only": True,
                "direct_build_allowed": False,
            }), encoding="utf-8")
            result = build_transition_evidence_requests(audit, root / "requests")
            frame = pd.read_parquet(
                result.path / "corporate_action_evidence_requests.parquet"
            )
            self.assertEqual("", frame.iloc[0]["predecessor_cik"])
            self.assertEqual("", frame.iloc[0]["successor_cik"])


if __name__ == "__main__":
    unittest.main()


class OperatorEventAnchoredTransitionTests(unittest.TestCase):
    """Frozen rule A' (R3): a predecessor absent from normalization may be
    anchored to an approved membership-event stable ID only through frozen
    dual-name S&P evidence."""

    def _normalization(self, root: Path) -> Path:
        normalization_id = "n" * 64
        normalization = root / normalization_id
        normalization.mkdir(parents=True)
        (normalization / "manifest.json").write_text(
            json.dumps({"normalization_id": normalization_id}), encoding="utf-8"
        )
        pd.DataFrame(
            [
                {
                    "source_id": "ishares_ivv_holdings_api",
                    "ticker": "IR",
                    "as_of_date": "2020-04-30",
                    "identity_candidate_key": "ISIN:US45687V1061",
                    "isin": "US45687V1061",
                    "cusip": "45687V106",
                    "content_sha256": "x" * 64,
                    "source_row_number": 7,
                    "issuer_name": "Ingersoll Rand Inc",
                    "title": "Ingersoll Rand Inc",
                    "lei": "",
                    "cik": "",
                }
            ]
        ).to_parquet(
            normalization / "security_identity_candidates.parquet", index=False
        )
        return normalization

    def _store_with_event(self, root: Path, payload: bytes) -> tuple:
        from research_platform.us_pit.models import (
            LicenseClass,
            SourceDependency,
            SourceRole,
        )
        from research_platform.us_pit.store import USPITStore

        store = USPITStore(root / "pit")
        reference = store.put_bytes(payload, media_type="text/html")
        dependency = SourceDependency(
            source_id="spglobal_sp500_membership_events",
            source_version="test-v1",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=reference.sha256,
            observed_at="2026-08-14T00:00:00+00:00",
            published_at="2020-02-27T21:00:00+00:00",
            as_of_date="2020-03-03",
            url="https://press.spglobal.com/gardner-denver",
            dataset="membership_events",
            metadata={"publication_time_from_payload": True},
        )
        batch = store.write_source_batch([dependency])
        return store, reference.sha256

    def test_event_anchored_transition_requires_dual_names_in_frozen_evidence(
        self,
    ) -> None:
        from research_platform.us_pit.evidence_requests import (
            build_operator_transition_evidence_requests,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = (
                b"Gardner Denver Holdings Set to Join S&P 500 ... "
                b"Gardner Denver Holdings will change its name to "
                b"Ingersoll Rand Inc."
            )
            store, digest = self._store_with_event(root, payload)
            transitions = [
                {
                    "anchor_date": "2020-03-31",
                    "predecessor_security_id": "us_isin_us36467w1099",
                    "successor_security_id": "us_isin_us45687v1061",
                    "predecessor_event_evidence_sha256": digest,
                    "predecessor_name": "Gardner Denver Holdings",
                    "note": "pre-index rename continuation",
                }
            ]
            result = build_operator_transition_evidence_requests(
                store,
                self._normalization(root),
                transitions,
                root / "requests",
                proposed_by="oxalpha",
            )
            frame = pd.read_parquet(
                result.path / "corporate_action_evidence_requests.parquet"
            )
            self.assertEqual("OPERATOR_EVENT_ANCHORED_PAIR", frame.iloc[0]["match_basis"])
            self.assertEqual(
                "Gardner Denver Holdings", frame.iloc[0]["predecessor_name"]
            )
            self.assertEqual("", frame.iloc[0]["predecessor_isin"])
            self.assertFalse(bool(frame.iloc[0]["approved"]))

    def test_event_anchored_transition_rejects_absent_predecessor_name(
        self,
    ) -> None:
        from research_platform.us_pit.evidence_requests import (
            build_operator_transition_evidence_requests,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b"Unrelated announcement without the predecessor name."
            store, digest = self._store_with_event(root, payload)
            transitions = [
                {
                    "anchor_date": "2020-03-31",
                    "predecessor_security_id": "us_isin_us36467w1099",
                    "successor_security_id": "us_isin_us45687v1061",
                    "predecessor_event_evidence_sha256": digest,
                    "predecessor_name": "Gardner Denver Holdings",
                }
            ]
            with self.assertRaisesRegex(ValueError, "predecessor name is absent"):
                build_operator_transition_evidence_requests(
                    store,
                    self._normalization(root),
                    transitions,
                    root / "requests",
                    proposed_by="oxalpha",
                )

    def test_event_anchored_transition_rejects_non_membership_evidence(
        self,
    ) -> None:
        from research_platform.us_pit.evidence_requests import (
            build_operator_transition_evidence_requests,
        )
        from research_platform.us_pit.models import (
            LicenseClass,
            SourceDependency,
            SourceRole,
        )
        from research_platform.us_pit.store import USPITStore

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = USPITStore(root / "pit")
            reference = store.put_bytes(b"Gardner Denver Holdings Ingersoll Rand Inc", media_type="text/plain")
            dependency = SourceDependency(
                source_id="some_other_source",
                source_version="v1",
                role=SourceRole.SIGNAL_INPUT,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=reference.sha256,
                observed_at="2026-08-14T00:00:00+00:00",
                published_at="2020-02-27T21:00:00+00:00",
                as_of_date="2020-03-03",
                url="https://example.com/not-membership",
                dataset="something_else",
                metadata={"publication_time_from_payload": True},
            )
            store.write_source_batch([dependency])
            transitions = [
                {
                    "anchor_date": "2020-03-31",
                    "predecessor_security_id": "us_isin_us36467w1099",
                    "successor_security_id": "us_isin_us45687v1061",
                    "predecessor_event_evidence_sha256": reference.sha256,
                    "predecessor_name": "Gardner Denver Holdings",
                }
            ]
            with self.assertRaisesRegex(ValueError, "frozen"):
                build_operator_transition_evidence_requests(
                    store,
                    self._normalization(root),
                    transitions,
                    root / "requests",
                    proposed_by="oxalpha",
                )


if __name__ == "__main__":
    unittest.main()
