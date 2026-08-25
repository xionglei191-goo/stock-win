from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone

from research_platform.us_pit.sources import SyncRequest
from research_platform.us_pit.tdx_current_master import (
    TDXCurrentUSMasterAdapter,
    resolve_current_tdx_alias,
    tdx_current_codes,
)
from research_platform.us_tdx import TQRawRPCEnvelope


class _Client:
    def call_raw(self, method, params):
        payload = {
            "id": 1,
            "result": {
                "ErrorId": "0",
                "Value": [
                    {"Code": "AAPL.US", "Name": "Apple"},
                    {"Code": "SPY.US", "Name": "SPY"},
                ],
            },
        }
        return TQRawRPCEnvelope(
            method=method,
            request_bytes=json.dumps({"method": method, "params": params}).encode(),
            response_bytes=json.dumps(payload).encode(),
            fetched_at=datetime(2026, 8, 13, 10, tzinfo=timezone.utc),
            value=payload["result"]["Value"],
        )


class TDXCurrentUSMasterTests(unittest.TestCase):
    def test_capture_is_cross_check_only_and_raw_response_is_replayable(self) -> None:
        observed = datetime(2026, 8, 13, 10, 1, tzinfo=timezone.utc)
        artifact = TDXCurrentUSMasterAdapter(_Client()).fetch(
            SyncRequest(date(2026, 8, 13), date(2026, 8, 13), observed)
        )[0]
        self.assertEqual(artifact.role.value, "CROSS_CHECK")
        self.assertFalse(artifact.metadata["membership_authority"])
        self.assertEqual(artifact.metadata["row_count"], 2)
        self.assertEqual(
            tdx_current_codes(artifact.payload),
            {"AAPL.US": "Apple", "SPY.US": "SPY"},
        )

    def test_duplicate_or_non_us_rows_fail_closed(self) -> None:
        class BadClient(_Client):
            def call_raw(self, method, params):
                envelope = super().call_raw(method, params)
                return TQRawRPCEnvelope(
                    method=envelope.method,
                    request_bytes=envelope.request_bytes,
                    response_bytes=envelope.response_bytes,
                    fetched_at=envelope.fetched_at,
                    value=[{"Code": "000001.SZ", "Name": "bad"}],
                )

        with self.assertRaisesRegex(ValueError, "non-US"):
            TDXCurrentUSMasterAdapter(BadClient()).fetch(
                SyncRequest(
                    date(2026, 8, 13),
                    date(2026, 8, 13),
                    datetime(2026, 8, 13, 10, 1, tzinfo=timezone.utc),
                )
            )

    def test_share_class_punctuation_requires_exact_tdx_evidence(self) -> None:
        current = {"BRK.B.US": "Berkshire B", "BF.B.US": "Brown Forman B"}
        self.assertEqual(resolve_current_tdx_alias("BRKB", current), "BRK.B.US")
        self.assertEqual(resolve_current_tdx_alias("BFB", current), "BF.B.US")
        with self.assertRaisesRegex(ValueError, "not uniquely evidenced"):
            resolve_current_tdx_alias("MISSING", current)


if __name__ == "__main__":
    unittest.main()
