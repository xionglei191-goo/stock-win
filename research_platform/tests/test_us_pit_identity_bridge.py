from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.us_pit.identity_bridge import propose_identity_bridges
from research_platform.us_pit.hashing import sha256_file


class IdentityBridgeTests(unittest.TestCase):
    def _normalization(self, root: Path) -> Path:
        source = root / "normid"
        source.mkdir()
        holdings = pd.DataFrame(
            [
                {
                    "holding_candidate_id": "h-current",
                    "source_id": "ishares_ivv_holdings",
                    "content_sha256": "a" * 64,
                    "ticker": "ABC",
                    "issuer_name": "Acme, Inc.",
                    "title": "Acme, Inc.",
                    "identity_candidate_key": None,
                    "as_of_date": "2026-08-11",
                }
            ]
        )
        identities = pd.DataFrame(
            [
                {
                    "holding_candidate_id": "h-sec",
                    "source_id": "sec_nport_ivv",
                    "content_sha256": "b" * 64,
                    "identity_candidate_key": "isin:US0000000001",
                    "isin": "US0000000001",
                    "cusip": "000000000",
                    "issuer_name": "ACME INC",
                    "title": "ACME INC",
                    "as_of_date": "2026-03-31",
                    "source_row_number": 1,
                }
            ]
        )
        holdings.to_parquet(source / "fund_holdings_observed_candidate.parquet", index=False)
        identities.to_parquet(source / "security_identity_candidates.parquet", index=False)
        manifest = {"normalization_id": "normid"}
        (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return source

    def test_bridge_is_review_only_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._normalization(Path(tmp))
            result = propose_identity_bridges(source, Path(tmp) / "bridge")
            self.assertTrue(result.manifest["candidate_only"])
            self.assertFalse(result.manifest["direct_build_allowed"])
            self.assertEqual(result.manifest["matched_exact_name"], 1)
            self.assertEqual(result.manifest["matched_total"], 1)
            frame = pd.read_parquet(result.path / "identity_bridge_candidates.parquet")
            self.assertEqual(frame.loc[0, "historical_security_id"], "isin:US0000000001")
            self.assertFalse(bool(frame.loc[0, "approved"]))
            self.assertEqual(
                result.manifest["artifact_sha256"],
                sha256_file(result.path / "identity_bridge_candidates.parquet"),
            )

    def test_ambiguous_name_never_becomes_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._normalization(Path(tmp))
            identities = pd.read_parquet(source / "security_identity_candidates.parquet")
            identities = pd.concat(
                [identities, identities.assign(holding_candidate_id="h-sec-2", identity_candidate_key="isin:US0000000002")],
                ignore_index=True,
            )
            identities.to_parquet(source / "security_identity_candidates.parquet", index=False)
            result = propose_identity_bridges(source, Path(tmp) / "bridge")
            frame = pd.read_parquet(result.path / "identity_bridge_candidates.parquet")
            self.assertEqual(frame.loc[0, "status"], "AMBIGUOUS")
            self.assertEqual(frame.loc[0, "historical_security_id"], "")

    def test_same_official_provider_ticker_history_precedes_name_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._normalization(Path(tmp))
            identities = pd.read_parquet(source / "security_identity_candidates.parquet")
            api = identities.iloc[[0]].assign(
                holding_candidate_id="h-api",
                source_id="ishares_ivv_holdings_api",
                ticker="ABC",
                issuer_name="Renamed Issuer",
                title="Renamed Issuer",
                identity_candidate_key="isin:US9999999999",
                isin="US9999999999",
                cusip="999999999",
                as_of_date="2026-07-31",
            )
            identities = pd.concat([identities, api], ignore_index=True)
            identities.to_parquet(
                source / "security_identity_candidates.parquet", index=False
            )

            result = propose_identity_bridges(source, Path(tmp) / "bridge")
            frame = pd.read_parquet(result.path / "identity_bridge_candidates.parquet")

            self.assertEqual(
                frame.loc[0, "match_basis"],
                "OFFICIAL_ISHARES_TICKER_STABLE_ID_HISTORY",
            )
            self.assertEqual(frame.loc[0, "historical_security_id"], "isin:US9999999999")
            self.assertEqual(result.manifest["matched_official_ticker"], 1)
            self.assertEqual(result.manifest["matched_exact_name"], 0)
            self.assertEqual(result.manifest["matched_total"], 1)
            self.assertFalse(bool(frame.loc[0, "approved"]))


if __name__ == "__main__":
    unittest.main()
