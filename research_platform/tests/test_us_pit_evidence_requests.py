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
