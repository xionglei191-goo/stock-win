from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file
from research_platform.us_pit.sec_filing_screen import (
    rank_sec_filing_screen,
    screen_sec_filing_candidates,
)
from research_platform.us_pit.service import USPITService
from research_platform.us_pit.sources import SyncRequest
from research_platform.us_pit.sources_official import HTTPResponse
from research_platform.us_pit.sources_sec_identity import SECFilingDocumentsAdapter
from research_platform.us_pit.store import USPITStore


OBSERVED = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)


class _Transport:
    def __init__(self, url: str, payload: bytes) -> None:
        self.url = url
        self.payload = payload

    def get(self, url: str, *, headers, timeout: float) -> HTTPResponse:
        return HTTPResponse(url=self.url, status_code=200, content=self.payload, headers={})


def _inputs(root: Path) -> tuple[Path, Path, str]:
    accession = "0000000123-24-000001"
    url = (
        "https://www.sec.gov/Archives/edgar/data/123/"
        f"{accession.replace('-', '')}/{accession}.txt"
    )
    filing = root / "filings"
    filing.mkdir()
    filing_frame = pd.DataFrame([{
        "request_id": "r" * 64,
        "side": "SUCCESSOR",
        "cik": "0000000123",
        "accession_number": accession,
        "anchor_date": "2024-12-31",
        "security_id": "us_isin_new",
        "query_name": "New Corp",
        "query_ticker": "NEW",
        "form": "8-K",
        "filing_date": "2024-12-15",
        "report_date": "2024-12-14",
        "accepted_at": "2024-12-15T21:30:00.000Z",
        "primary_document": "event.htm",
        "primary_document_description": "CURRENT REPORT",
        "items": "1.01,2.01",
        "complete_submission_url": url,
    }])
    filing_path = filing / "sec_filing_candidates.parquet"
    filing_frame.to_parquet(filing_path, index=False)
    filing_manifest = {
        "candidate_set_id": "c" * 64,
        "artifact_sha256": sha256_file(filing_path),
        "candidate_only": True,
        "direct_build_allowed": False,
    }
    (filing / "manifest.json").write_bytes(canonical_json_bytes(filing_manifest))

    requests = root / "requests"
    requests.mkdir()
    request_frame = pd.DataFrame([{
        "request_id": "r" * 64,
        "predecessor_name": "Old Corp",
        "successor_name": "New Corp",
        "predecessor_ticker": "OLD",
        "successor_ticker": "NEW",
        "predecessor_isin": "US0000000001",
        "successor_isin": "US0000000002",
        "predecessor_cusip": "000000001",
        "successor_cusip": "000000002",
    }])
    request_path = requests / "corporate_action_evidence_requests.parquet"
    request_frame.to_parquet(request_path, index=False)
    (requests / "manifest.json").write_bytes(canonical_json_bytes({
        "artifact_sha256": sha256_file(request_path),
        "candidate_only": True,
        "direct_build_allowed": False,
    }))
    return filing, requests, url


class SECFilingScreenTests(unittest.TestCase):
    def test_screen_is_unapproved_even_when_identity_and_merger_keywords_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            filing, requests, url = _inputs(root)
            payload = b"""<SEC-DOCUMENT>0000000123-24-000001.txt
<SEC-HEADER><ACCEPTANCE-DATETIME>20241215163000
ACCESSION NUMBER: 0000000123-24-000001
CENTRAL INDEX KEY: 0000000123</SEC-HEADER>
<TEXT>New Corp entered into a merger agreement with Old Corp.</TEXT>
"""
            store = USPITStore(root / "pit")
            batch = USPITService(store).sync(
                SECFilingDocumentsAdapter(
                    filing,
                    user_agent="Research test@example.com",
                    transport=_Transport(url, payload),
                    clock=lambda: OBSERVED,
                    minimum_request_interval_seconds=0,
                ),
                SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED),
            )
            result = screen_sec_filing_candidates(
                store, [batch.batch_id], filing, requests, root / "screen"
            )
            frame = pd.read_parquet(result.path / "sec_filing_screen.parquet")
            self.assertEqual("HIGH", frame.iloc[0]["relevance"])
            self.assertIn("MERGER", frame.iloc[0]["event_keyword_hits"])
            self.assertEqual("", frame.iloc[0]["action_type"])
            self.assertFalse(bool(frame.iloc[0]["terms_verified"]))
            self.assertFalse(bool(frame.iloc[0]["approved"]))
            self.assertFalse(result.manifest["direct_build_allowed"])

            ranked = rank_sec_filing_screen(
                result.path, filing, requests, root / "ranked", per_request=1
            )
            review = pd.read_parquet(
                ranked.path / "corporate_action_filing_review.parquet"
            )
            self.assertEqual(1, len(review))
            self.assertEqual(1, int(review.iloc[0]["request_rank"]))
            self.assertFalse(bool(review.iloc[0]["terms_verified"]))
            self.assertFalse(bool(review.iloc[0]["approved"]))
            self.assertFalse(ranked.manifest["direct_build_allowed"])

    def test_missing_source_batch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            filing, requests, _url = _inputs(root)
            store = USPITStore(root / "pit")
            with self.assertRaisesRegex(ValueError, "exactly cover"):
                screen_sec_filing_candidates(
                    store, [], filing, requests, root / "screen"
                )

    def test_rank_deduplicates_same_request_accession_before_top_n(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            filing, requests, url = _inputs(root)
            filing_path = filing / "sec_filing_candidates.parquet"
            values = pd.read_parquet(filing_path)
            values = pd.concat([values, values], ignore_index=True)
            values.to_parquet(filing_path, index=False)
            manifest_path = filing / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifact_sha256"] = sha256_file(filing_path)
            manifest_path.write_bytes(canonical_json_bytes(manifest))
            payload = b"""<SEC-DOCUMENT>0000000123-24-000001.txt
<SEC-HEADER><ACCEPTANCE-DATETIME>20241215163000
ACCESSION NUMBER: 0000000123-24-000001
CENTRAL INDEX KEY: 0000000123</SEC-HEADER>
<TEXT>New Corp entered into a merger agreement with Old Corp.</TEXT>
"""
            store = USPITStore(root / "pit")
            batch = USPITService(store).sync(
                SECFilingDocumentsAdapter(
                    filing,
                    user_agent="Research test@example.com",
                    transport=_Transport(url, payload),
                    clock=lambda: OBSERVED,
                    minimum_request_interval_seconds=0,
                ),
                SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED),
            )
            screen = screen_sec_filing_candidates(
                store, [batch.batch_id], filing, requests, root / "screen"
            )

            ranked = rank_sec_filing_screen(
                screen.path, filing, requests, root / "ranked", per_request=10
            )
            review = pd.read_parquet(
                ranked.path / "corporate_action_filing_review.parquet"
            )

            self.assertEqual(1, len(review))
            self.assertEqual(1, ranked.manifest["duplicate_rows_removed"])
            self.assertEqual(1, int(review.iloc[0]["request_rank"]))


if __name__ == "__main__":
    unittest.main()
