from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.us_pit.alias_crosscheck import crosscheck_current_aliases
from research_platform.us_pit.models import LicenseClass, SourceDependency, SourceRole
from research_platform.us_pit.store import USPITStore


def _rpc_payload() -> bytes:
    response = {
        "result": {
            "ErrorId": "0",
            "Value": [
                {"Code": "AAPL.US", "Name": "Apple"},
                {"Code": "BRK.B.US", "Name": "Berkshire B"},
            ],
        }
    }
    return json.dumps(
        {
            "method": "get_stock_list",
            "request_utf8": json.dumps(
                {"method": "get_stock_list", "params": {"market": "103"}}
            ),
            "response_utf8": json.dumps(response),
            "fetched_at": "2026-08-13T10:00:00+00:00",
        }
    ).encode()


class CurrentAliasCrosscheckTests(unittest.TestCase):
    def test_current_aliases_are_hash_bound_but_not_historical_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = USPITStore(root / "store")
            reference = store.put_bytes(_rpc_payload(), media_type="application/json")
            batch = store.write_source_batch(
                [
                    SourceDependency(
                        source_id="tdx_us_security_master_current",
                        source_version="tq-market-103-raw-v1",
                        role=SourceRole.CROSS_CHECK,
                        license_class=LicenseClass.LOCAL_VENDOR,
                        object_sha256=reference.sha256,
                        observed_at="2026-08-13T10:00:00+00:00",
                        url="http://127.0.0.1:17709/get_stock_list",
                        dataset="us_security_master_current",
                    )
                ]
            )
            normalization = root / "norm"
            normalization.mkdir()
            pd.DataFrame(
                [
                    {
                        "holding_candidate_id": "a",
                        "source_id": "ishares_ivv_holdings",
                        "signal_eligible": True,
                        "ticker": "AAPL",
                        "issuer_name": "APPLE",
                        "as_of_date": "2026-08-11",
                        "observed_at": "2026-08-13T10:00:00+00:00",
                        "content_sha256": "a" * 64,
                    },
                    {
                        "holding_candidate_id": "b",
                        "source_id": "ishares_ivv_holdings",
                        "signal_eligible": True,
                        "ticker": "BRKB",
                        "issuer_name": "BERKSHIRE B",
                        "as_of_date": "2026-08-11",
                        "observed_at": "2026-08-13T10:00:00+00:00",
                        "content_sha256": "a" * 64,
                    },
                ]
            ).to_parquet(normalization / "fund_holdings_observed_candidate.parquet")
            (normalization / "manifest.json").write_text(
                json.dumps({"normalization_id": "norm"}), encoding="utf-8"
            )
            result = crosscheck_current_aliases(
                store, normalization, batch.batch_id, root / "result"
            )
            self.assertEqual(result.manifest["status"], "CROSSCHECK_READY")
            self.assertEqual(result.manifest["verified_count"], 2)
            self.assertFalse(result.manifest["historical_membership_authority"])
            frame = pd.read_parquet(result.path / "current_alias_crosscheck.parquet")
            self.assertEqual(set(frame["vendor_code"]), {"AAPL.US", "BRK.B.US"})


if __name__ == "__main__":
    unittest.main()
