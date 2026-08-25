from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import pandas as pd

from research_platform.us_pit.hashing import sha256_file
from research_platform.us_pit.sec_identity_review import SECIdentityReviewService
from research_platform.us_pit.store import USPITStore


class SECIdentityReviewTests(unittest.TestCase):
    def _inputs(self, root: Path) -> tuple[Path, Path, Path]:
        unresolved = root / "unresolved.parquet"
        candidates = root / "candidates.parquet"
        identities = root / "identities.parquet"
        pd.DataFrame([
            {
                "event_id": "event-xrx",
                "effective_at": "2020-01-01T00:00:00Z",
                "security_id": "us_isin_us98421m1062",
                "ticker_at_announcement": "XRX",
                "review_reasons": "NAME_TICKER_CROSSCHECK_MISSING",
            },
            {
                "event_id": "event-mbc",
                "effective_at": "2022-12-15T00:00:00Z",
                "security_id": "",
                "ticker_at_announcement": "MBC",
                "review_reasons": "IDENTITY_UNRESOLVED",
            },
        ]).to_parquet(unresolved, index=False)
        pd.DataFrame([
            {"event_candidate_id": "event-xrx", "company_name": "Xerox Holdings Corp"},
            {"event_candidate_id": "event-mbc", "company_name": "MasterBrand Inc"},
        ]).to_parquet(candidates, index=False)
        pd.DataFrame([
            {
                "identity_candidate_key": "isin:US98421M1062",
                "cusip": "98421M106",
                "isin": "US98421M1062",
            },
        ]).to_parquet(identities, index=False)
        return unresolved, candidates, identities

    @staticmethod
    def _transport(url: str, _user_agent: str) -> tuple[bytes, str]:
        if "search-index" in url:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
            ticker = "XRX" if "XRX" in query else "MBC"
            payload = {
                "hits": {
                    "hits": [{
                        "_id": f"test:{ticker.lower()}.xml",
                        "_source": {
                            "adsh": "0000000000-26-000001",
                            "ciks": ["0000000001"],
                            "file_date": "2026-01-31",
                        },
                    }],
                },
            }
            return json.dumps(payload).encode("utf-8"), "application/json"
        if url.endswith("xrx.xml"):
            return (
                b"<holding><name>Xerox Holdings Corp</name><ticker>XRX</ticker>"
                b"<cusip>98421M106</cusip></holding>",
                "application/xml",
            )
        if url.endswith("mbc.xml"):
            return (
                b"<h4>Item C.1. Identification of investment.</h4>"
                b"<holding><name>MasterBrand Inc</name><title>MasterBrand Inc COMMON STOCK</title>"
                b"<ticker>MBC</ticker><cusip>57638P104</cusip><isin>US57638P1049</isin></holding>"
                b"<h4>Item C.2. Amount of each investment.</h4>",
                "application/xml",
            )
        raise AssertionError(f"unexpected URL: {url}")

    def test_freezes_exact_sec_identity_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            store = USPITStore(root / "store")
            result = SECIdentityReviewService(
                store,
                user_agent="Local Research contact@example.com",
                transport=self._transport,
                throttle_seconds=0,
            ).review(*inputs, root / "review")

            frame = pd.read_parquet(result.path / "sec_identity_crosschecks.parquet")
            self.assertEqual({"RESOLVED"}, set(frame["review_outcome"]))
            xrx = frame.loc[frame["ticker"].eq("XRX")].iloc[0]
            self.assertEqual("us_isin_us98421m1062", xrx["resolved_security_id"])
            self.assertEqual("98421M106", xrx["cusip"])
            mbc = frame.loc[frame["ticker"].eq("MBC")].iloc[0]
            self.assertEqual("us_isin_us57638p1049", mbc["resolved_security_id"])
            self.assertEqual("US57638P1049", mbc["isin"])
            self.assertFalse(result.manifest["direct_build_allowed"])
            self.assertEqual(2, result.manifest["resolved_count"])
            self.assertIsNotNone(result.source_batch)
            self.assertTrue(all(
                not dependency.metadata.get("eligible_for_historical_signal", True)
                for dependency in result.source_batch.dependencies
                if dependency.dataset == "security_identity_crosscheck"
            ))

    def test_blocks_when_issuer_name_is_not_in_same_record(self) -> None:
        def mismatched_transport(url: str, user_agent: str) -> tuple[bytes, str]:
            payload, media_type = self._transport(url, user_agent)
            if url.endswith("mbc.xml"):
                payload = payload.replace(b"MasterBrand Inc", b"Unrelated Issuer")
            return payload, media_type

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            result = SECIdentityReviewService(
                USPITStore(root / "store"),
                user_agent="Local Research contact@example.com",
                transport=mismatched_transport,
                throttle_seconds=0,
            ).review(*inputs, root / "review")
            frame = pd.read_parquet(result.path / "sec_identity_crosschecks.parquet")
            mbc = frame.loc[frame["ticker"].eq("MBC")].iloc[0]
            self.assertEqual("BLOCKED", mbc["review_outcome"])
            self.assertEqual("NO_EXACT_SEC_FILED_TICKER_IDENTIFIER_RECORD", mbc["review_reason"])

    def test_reviewed_seed_only_resolves_after_sec_document_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            seed = root / "seeds.json"
            seed.write_text(json.dumps({
                "identity_seeds": [{
                    "event_id": "event-mbc",
                    "identifier": "57638P104",
                    "review_source_url": "https://www.sec.gov/example",
                    "review_note": "reviewed official search result",
                }],
            }), encoding="utf-8")
            result = SECIdentityReviewService(
                USPITStore(root / "store"),
                user_agent="Local Research contact@example.com",
                transport=self._transport,
                throttle_seconds=0,
            ).review(*inputs, root / "review", reviewed_identity_seeds=seed)
            frame = pd.read_parquet(result.path / "sec_identity_crosschecks.parquet")
            mbc = frame.loc[frame["ticker"].eq("MBC")].iloc[0]
            self.assertEqual("RESOLVED", mbc["review_outcome"])
            self.assertEqual("57638P104", mbc["review_seed_identifier"])
            self.assertEqual(sha256_file(seed), result.manifest["input_sha256"]["reviewed_identity_seeds"])


if __name__ == "__main__":
    unittest.main()
