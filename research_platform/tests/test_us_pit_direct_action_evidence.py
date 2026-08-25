from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.us_pit.direct_action_evidence import (
    DIRECT_ACTION_REVIEW_VERSION,
    DirectActionEvidenceReviewService,
)
from research_platform.us_pit.store import USPITStore


class DirectActionEvidenceReviewTests(unittest.TestCase):
    def _inputs(self, root: Path, phrase: str = "one corresponding share") -> tuple[Path, Path]:
        blocked = root / "blocked.parquet"
        pd.DataFrame([{
            "request_id": "a" * 64,
            "anchor_date": "2022-03-31",
            "predecessor_security_id": "us_isin_old",
            "successor_security_id": "us_isin_new",
            "predecessor_name": "Old Issuer",
            "successor_name": "New Issuer",
        }]).to_parquet(blocked, index=False)
        spec = root / "spec.json"
        spec.write_text(json.dumps({
            "format_version": DIRECT_ACTION_REVIEW_VERSION,
            "reviewer": "codex-test-review",
            "events": [{
                "request_id": "a" * 64,
                "evidence_status": "TERMS_COMPLETE_MODEL_GAP",
                "model_blocker": "stable identity continuity",
                "review_conclusion": "one-for-one holding-company reorganization",
                "action_legs": [{"action_type": "IDENTITY_CONTINUITY", "share_ratio": 1.0}],
                "sources": [{
                    "url": "https://www.sec.gov/Archives/test.htm",
                    "published_at": "2022-01-01T12:00:00-05:00",
                    "required_phrases": [phrase],
                }],
            }],
        }), encoding="utf-8")
        return blocked, spec

    @staticmethod
    def _transport(_url: str, _user_agent: str) -> tuple[bytes, str]:
        return b"<p>Each share became one corresponding share in New Issuer.</p>", "text/html"

    def test_freezes_verified_candidate_only_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked, spec = self._inputs(root)
            result = DirectActionEvidenceReviewService(
                USPITStore(root / "store"),
                user_agent="Local Research contact@example.com",
                transport=self._transport,
                throttle_seconds=0,
            ).review(blocked, spec, root / "review")
            frame = pd.read_parquet(result.path / "action_evidence_matrix.parquet")
            self.assertEqual("TERMS_COMPLETE_MODEL_GAP", frame.iloc[0]["evidence_status"])
            self.assertTrue(frame.iloc[0]["all_required_phrases_verified"])
            self.assertFalse(result.manifest["direct_build_allowed"])
            self.assertFalse(result.manifest["human_approval_claimed"])
            self.assertFalse(result.manifest["formal_action_rows_emitted"])
            self.assertEqual(1, result.manifest["terms_complete_count"])
            self.assertIsNotNone(result.source_batch)
            dependency = result.source_batch.dependencies[0]
            self.assertEqual("VALIDATION_ANCHOR", dependency.role.value)
            self.assertFalse(dependency.metadata["eligible_for_historical_signal"])

    def test_downgrades_when_required_phrase_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked, spec = self._inputs(root, phrase="not present")
            result = DirectActionEvidenceReviewService(
                USPITStore(root / "store"),
                user_agent="Local Research contact@example.com",
                transport=self._transport,
                throttle_seconds=0,
            ).review(blocked, spec, root / "review")
            frame = pd.read_parquet(result.path / "action_evidence_matrix.parquet")
            self.assertEqual("TERMS_PARTIAL", frame.iloc[0]["evidence_status"])
            self.assertFalse(frame.iloc[0]["terms_complete"])

    def test_records_capture_failure_without_discarding_review_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked, spec = self._inputs(root)

            def failing_transport(_url: str, _user_agent: str) -> tuple[bytes, str]:
                raise TimeoutError("official source timed out")

            result = DirectActionEvidenceReviewService(
                USPITStore(root / "store"),
                user_agent="Local Research contact@example.com",
                transport=failing_transport,
                throttle_seconds=0,
            ).review(blocked, spec, root / "review")
            frame = pd.read_parquet(result.path / "action_evidence_matrix.parquet")
            records = json.loads(frame.iloc[0]["source_records_json"])
            self.assertEqual("TERMS_PARTIAL", frame.iloc[0]["evidence_status"])
            self.assertFalse(frame.iloc[0]["all_required_phrases_verified"])
            self.assertEqual("TimeoutError: source capture failed", records[0]["capture_gap"])
            self.assertEqual(1, result.manifest["terms_partial_count"])
            self.assertIsNone(result.source_batch)

    def test_requires_exact_blocked_request_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked, spec = self._inputs(root)
            payload = json.loads(spec.read_text(encoding="utf-8"))
            payload["events"] = []
            spec.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "requires events"):
                DirectActionEvidenceReviewService(
                    USPITStore(root / "store"),
                    user_agent="Local Research contact@example.com",
                    transport=self._transport,
                    throttle_seconds=0,
                ).review(blocked, spec, root / "review")


if __name__ == "__main__":
    unittest.main()
