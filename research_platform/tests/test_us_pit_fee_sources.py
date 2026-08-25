from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from research_platform.us_pit import SyncRequest
from research_platform.us_pit.sources_fees import (
    FINRA_2012_NOTICE_URL,
    FINRA_2020_FILING_URL,
    FINRA_TAF_URL,
    RegulatoryFeeEvidenceAdapter,
)
from research_platform.us_pit.sources_official import HTTPResponse, SourceFetchError


OBSERVED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


class _FeeTransport:
    def __init__(self, *, corrupt_url: str | None = None) -> None:
        self.corrupt_url = corrupt_url
        self.calls: list[str] = []

    @staticmethod
    def _sec_payload(url: str) -> bytes:
        values = {
            "2019-30": ("April 16, 2019", "20.70"),
            "2020-7": ("February 18, 2020", "22.10"),
            "2021-8": ("February 25, 2021", "5.10"),
            "2022-60": ("May 14, 2022", "22.90"),
            "2023-15": ("February 27, 2023", "8.00"),
            "2024-2": ("May 22, 2024", "27.80"),
            "2025-2": ("May 14, 2025", "0.00"),
            "2026-2": ("April 4, 2026", "20.60"),
        }
        for token, (effective, rate) in values.items():
            if token in url:
                return (
                    f"Section 31 fee starts on {effective} at ${rate} per million"
                ).encode()
        raise AssertionError(f"unexpected SEC URL: {url}")

    @staticmethod
    def _finra_payload(url: str) -> bytes:
        if url == FINRA_2012_NOTICE_URL:
            return b"2012 equity TAF $0.000119 per share up to $5.95 per trade"
        if url == FINRA_2020_FILING_URL:
            return (
                b"2022 $0.000130 $6.49; 2023 $0.000145 $7.27; "
                b"2024 $0.000166 $8.30"
            )
        if url == FINRA_TAF_URL:
            return b"2026 $0.000195 $9.79"
        raise AssertionError(f"unexpected FINRA URL: {url}")

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HTTPResponse:
        del headers, timeout
        self.calls.append(url)
        if self.corrupt_url == url:
            payload = b"official page without the declared rate"
        elif "sec.gov" in url:
            payload = self._sec_payload(url)
        else:
            payload = self._finra_payload(url)
        return HTTPResponse(
            url=url,
            status_code=200,
            content=payload,
            headers={"Content-Type": "text/html"},
        )


class RegulatoryFeeEvidenceTests(unittest.TestCase):
    def _adapter(self, transport: _FeeTransport) -> RegulatoryFeeEvidenceAdapter:
        return RegulatoryFeeEvidenceAdapter(
            sec_user_agent="Local PIT Research contact@example.com",
            transport=transport,
            clock=lambda: OBSERVED_AT,
            minimum_sec_request_interval_seconds=0,
        )

    def test_five_year_capture_freezes_each_effective_official_object(self) -> None:
        transport = _FeeTransport()
        artifacts = self._adapter(transport).fetch(
            SyncRequest(
                start_date=date(2019, 10, 1),
                end_date=date(2026, 7, 31),
                observed_at=OBSERVED_AT,
            )
        )

        self.assertEqual(8, sum(item.dataset == "regulatory_fee_sec" for item in artifacts))
        self.assertEqual(3, sum(item.dataset == "regulatory_fee_finra" for item in artifacts))
        self.assertTrue(all(item.metadata["fee_evidence_contract_version"] == 2 for item in artifacts))
        self.assertEqual(11, len(set(transport.calls)))

    def test_declared_rate_must_be_present_in_the_frozen_object(self) -> None:
        transport = _FeeTransport(
            corrupt_url="https://www.sec.gov/newsroom/press-releases/2022-60"
        )
        with self.assertRaisesRegex(SourceFetchError, "effective date and rate"):
            self._adapter(transport).fetch(
                SyncRequest(
                    start_date=date(2022, 1, 1),
                    end_date=date(2022, 12, 31),
                    observed_at=OBSERVED_AT,
                )
            )


if __name__ == "__main__":
    unittest.main()
