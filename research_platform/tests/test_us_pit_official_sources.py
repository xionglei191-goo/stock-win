from __future__ import annotations

import os
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from unittest.mock import patch

from research_platform.us_pit import LicenseClass, SourceRole, SyncRequest
from research_platform.us_pit.sources_official import (
    AKShareUSCrossCheckAdapter,
    HTTPResponse,
    ISharesIVVObservedSnapshotAdapter,
    ISharesIVVHistoricalReconciliationAdapter,
    MarketEvidencePayload,
    SECNPortIVVAdapter,
    SourceConfigurationError,
    SourceFetchError,
    SourcePolicyError,
    TDXUSMarketEvidenceAdapter,
)


OBSERVED_AT = datetime(2026, 8, 12, 14, 30, tzinfo=timezone.utc)


SEC_ACCESSION = "0001752724-25-119791"


def _sec_listing(*accessions: str) -> bytes:
    links = "".join(
        (
            '<a href="/Archives/edgar/data/1100663/'
            f'{accession.replace("-", "")}/{accession}-index.html">Documents</a>'
        )
        for accession in accessions
    )
    return (
        "<!doctype html><html><body>"
        "Series: S000004310 iShares Core S&amp;P 500 ETF"
        f"{links}</body></html>"
    ).encode("utf-8")


def _sec_listing_htm(*accessions: str) -> bytes:
    return _sec_listing(*accessions).replace(b"-index.html", b"-index.htm")


def _nport_filing(
    *,
    accession: str = SEC_ACCESSION,
    series_id: str = "S000004310",
    report_date: str = "20250331",
    filing_date: str = "20250527",
    accepted_at: str = "20250527104112",
    form: str = "NPORT-P",
) -> bytes:
    return f"""<SEC-DOCUMENT>{accession}.txt
<SEC-HEADER>
<ACCEPTANCE-DATETIME>{accepted_at}
ACCESSION NUMBER: {accession}
CONFORMED SUBMISSION TYPE: {form}
CONFORMED PERIOD OF REPORT: {report_date}
FILED AS OF DATE: {filing_date}
FILER:
  CENTRAL INDEX KEY: 0001100663
<SERIES>
<SERIES-ID>{series_id}
</SERIES>
</SEC-HEADER>
<DOCUMENT><TYPE>{form}
<TEXT><XML><edgarSubmission><formData><genInfo>
<regCik>0001100663</regCik><seriesId>{series_id}</seriesId>
</genInfo></formData></edgarSubmission></XML></TEXT></DOCUMENT>
""".encode("ascii")


class _FakeHTTPTransport:
    def __init__(
        self,
        payload: bytes | Callable[[str], bytes],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> HTTPResponse:
        self.calls.append((url, dict(headers), timeout))
        payload = self.payload(url) if callable(self.payload) else self.payload
        return HTTPResponse(
            url=url,
            status_code=self.status_code,
            content=payload,
            headers=self.headers,
        )


def _ishares_product_payload(
    snapshot_date: date,
    *,
    row_count: int = 500,
    mismatched: str | None = None,
) -> bytes:
    arrays = {
        "ticker": [f"T{index}" for index in range(row_count)],
        "issueName": [f"Issuer {index}" for index in range(row_count)],
        "assetClass": ["Equity"] * row_count,
        "cusip": ["037833100"] * row_count,
        "isin": ["US0378331005"] * row_count,
        "exchange": ["NASDAQ"] * row_count,
        "currencyCode": ["USD"] * row_count,
        "unitsHeld": [100.0] * row_count,
        "marketValue": [1000.0] * row_count,
        "holdingPercent": [0.2] * row_count,
    }
    if mismatched is not None:
        arrays[mismatched] = arrays[mismatched][:-1]
    points = {
        name: {"value": values, "formattedValue": list(values)}
        for name, values in arrays.items()
    }
    points["asOfDate"] = {
        "value": snapshot_date.strftime("%Y%m%d"),
        "formattedValue": snapshot_date.strftime("%b %d, %Y"),
    }
    return json.dumps(
        {
            "productId": 239726,
            "componentsByNameMap": {
                "holdings": {
                    "containersByNameMap": {
                        "all": {"dataPointsByNameMap": points}
                    }
                }
            },
        }
    ).encode()


def _sec_transport(
    filing: bytes | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> _FakeHTTPTransport:
    def payload(url: str) -> bytes:
        if "browse-edgar" in url:
            return _sec_listing(SEC_ACCESSION)
        if url.endswith(f"/{SEC_ACCESSION}.txt"):
            return filing if filing is not None else _nport_filing()
        raise AssertionError(f"unexpected SEC URL: {url}")

    return _FakeHTTPTransport(payload, headers=headers)


class _FakeMarketProvider:
    def __init__(self, *payloads: MarketEvidencePayload) -> None:
        self.payloads = payloads
        self.requests: list[SyncRequest] = []

    def fetch(self, request: SyncRequest) -> tuple[MarketEvidencePayload, ...]:
        self.requests.append(request)
        return self.payloads


def _request(
    start_date: date = date(2025, 1, 1),
    end_date: date = date(2025, 3, 31),
    observed_at: datetime = OBSERVED_AT,
) -> SyncRequest:
    return SyncRequest(
        start_date=start_date,
        end_date=end_date,
        observed_at=observed_at,
    )


class OfficialSourceAdapterTests(unittest.TestCase):
    def test_sec_user_agent_is_required_and_sent_with_contact(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "research_platform.us_pit.sources_official.winreg", None
        ):
            with self.assertRaisesRegex(SourceConfigurationError, "SEC_USER_AGENT"):
                SECNPortIVVAdapter()

        transport = _sec_transport()
        adapter = SECNPortIVVAdapter(
            user_agent="Local PIT Research contact@example.com",
            transport=transport,
            clock=lambda: OBSERVED_AT,
            minimum_request_interval_seconds=0,
        )
        artifacts = tuple(adapter.fetch(_request()))

        self.assertEqual(len(artifacts), 2)
        self.assertEqual(
            transport.calls[0][1]["User-Agent"],
            "Local PIT Research contact@example.com",
        )
        self.assertIn("CIK=S000004310", transport.calls[0][0])
        self.assertNotIn("form-n-port-data-sets", transport.calls[0][0])

    def test_sec_exact_filing_is_an_official_validation_anchor(self) -> None:
        payload = _nport_filing()
        transport = _sec_transport(
            payload,
            headers={
                "Last-Modified": "Tue, 30 Jun 2026 16:00:00 GMT",
                "ETag": '"nport-v1"',
            },
        )
        adapter = SECNPortIVVAdapter(
            user_agent="Local PIT Research contact@example.com",
            transport=transport,
            clock=lambda: OBSERVED_AT,
            minimum_request_interval_seconds=0,
        )

        artifacts = tuple(adapter.fetch(_request()))
        listing, artifact = artifacts

        self.assertEqual(listing.metadata["artifact_kind"], "edgar_series_listing")
        self.assertEqual(artifact.payload, payload)
        self.assertEqual(artifact.dataset, "fund_holdings_observed")
        self.assertEqual(artifact.role, SourceRole.VALIDATION_ANCHOR)
        self.assertEqual(artifact.license_class, LicenseClass.OFFICIAL_PUBLIC)
        self.assertEqual(artifact.observed_at, OBSERVED_AT)
        self.assertEqual(
            artifact.published_at,
            datetime(2025, 5, 27, 14, 41, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(artifact.as_of_date, date(2025, 3, 31))
        self.assertEqual(artifact.metadata["registrant_cik"], "0001100663")
        self.assertEqual(artifact.metadata["series_id"], "S000004310")
        self.assertEqual(artifact.metadata["accession_number"], SEC_ACCESSION)
        self.assertTrue(artifact.metadata["selection_applied"])
        self.assertTrue(artifact.metadata["series_id_verified_in_payload"])
        self.assertFalse(artifact.metadata["membership_reconstruction_performed"])
        self.assertFalse(artifact.metadata["eligible_for_historical_signal"])
        self.assertEqual(len(artifact.metadata["response_sha256"]), 64)

    def test_sec_accepts_the_live_edgar_index_htm_suffix(self) -> None:
        def payload(url: str) -> bytes:
            if "browse-edgar" in url:
                return _sec_listing_htm(SEC_ACCESSION)
            return _nport_filing()

        adapter = SECNPortIVVAdapter(
            user_agent="Local PIT Research contact@example.com",
            transport=_FakeHTTPTransport(payload),
            clock=lambda: OBSERVED_AT,
            minimum_request_interval_seconds=0,
        )
        self.assertEqual(2, len(tuple(adapter.fetch(_request()))))

    def test_sec_rejects_ambiguous_series_and_http_failure_without_partial_evidence(self) -> None:
        for transport in (
            _sec_transport(_nport_filing(series_id="S000099999")),
            _FakeHTTPTransport(_sec_listing(SEC_ACCESSION), status_code=503),
        ):
            adapter = SECNPortIVVAdapter(
                user_agent="Local PIT Research contact@example.com",
                transport=transport,
                clock=lambda: OBSERVED_AT,
                minimum_request_interval_seconds=0,
            )
            with self.assertRaises(SourceFetchError):
                tuple(adapter.fetch(_request()))

    def test_sec_fails_when_no_verified_filing_is_in_report_window(self) -> None:
        adapter = SECNPortIVVAdapter(
            user_agent="Local PIT Research contact@example.com",
            transport=_sec_transport(),
            clock=lambda: OBSERVED_AT,
            minimum_request_interval_seconds=0,
        )
        with self.assertRaisesRegex(SourceFetchError, "requested report window"):
            tuple(
                adapter.fetch(
                    _request(date(2024, 1, 1), date(2024, 3, 31))
                )
            )

    def test_capture_time_cannot_be_backdated(self) -> None:
        adapter = SECNPortIVVAdapter(
            user_agent="Local PIT Research contact@example.com",
            transport=_sec_transport(),
            clock=lambda: OBSERVED_AT,
            observation_tolerance=timedelta(seconds=30),
            minimum_request_interval_seconds=0,
        )
        with self.assertRaisesRegex(SourcePolicyError, "backdating"):
            tuple(
                adapter.fetch(
                    _request(observed_at=OBSERVED_AT - timedelta(days=365))
                )
            )

    def test_ishares_current_snapshot_is_eligible_only_from_observed_at(self) -> None:
        payload = b"Fund Holdings as of,Aug 11 2026\nTicker,Name,CUSIP,ISIN\nAAPL,Apple,037833100,US0378331005\n"
        adapter = ISharesIVVObservedSnapshotAdapter(
            transport=_FakeHTTPTransport(payload, headers={"Content-Type": "text/csv"}),
            clock=lambda: OBSERVED_AT,
        )

        artifact = tuple(
            adapter.fetch(_request(date(2025, 10, 1), date(2025, 12, 31)))
        )[0]

        self.assertEqual(artifact.role, SourceRole.SIGNAL_INPUT)
        self.assertEqual(artifact.license_class, LicenseClass.OFFICIAL_PUBLIC)
        self.assertEqual(artifact.as_of_date, date(2026, 8, 11))
        self.assertEqual(artifact.published_at, OBSERVED_AT)
        self.assertEqual(artifact.metadata["eligible_from"], OBSERVED_AT.isoformat())
        self.assertTrue(artifact.metadata["eligible_for_historical_signal"])
        self.assertEqual(
            artifact.metadata["availability_basis"], "first-local-observation"
        )
        self.assertFalse(artifact.metadata["membership_reconstruction_performed"])

    def test_ishares_historical_asof_response_is_reconciliation_only(self) -> None:
        snapshot_date = date(2025, 12, 31)
        transport = _FakeHTTPTransport(
            b"Fund Holdings as of,Dec 31 2025\n"
            b"Ticker,Name,CUSIP,ISIN\nAAPL,Apple,x,y\n"
        )
        adapter = ISharesIVVObservedSnapshotAdapter(
            historical_as_of_dates=(snapshot_date,),
            transport=transport,
            clock=lambda: OBSERVED_AT,
        )

        artifact = tuple(
            adapter.fetch(_request(date(2025, 10, 1), date(2025, 12, 31)))
        )[0]

        self.assertIn("asOfDate=20251231", transport.calls[0][0])
        self.assertEqual(artifact.as_of_date, snapshot_date)
        self.assertEqual(artifact.role, SourceRole.VALIDATION_ANCHOR)
        self.assertEqual(
            artifact.metadata["observation_mode"], "historical_as_of_reconciliation"
        )
        self.assertFalse(artifact.metadata["historical_publication_time_proven"])
        self.assertIsNone(artifact.metadata["eligible_from"])

    def test_ishares_rejects_a_successful_html_challenge_response(self) -> None:
        adapter = ISharesIVVObservedSnapshotAdapter(
            transport=_FakeHTTPTransport(b"<!doctype html><title>challenge</title>"),
            clock=lambda: OBSERVED_AT,
        )
        with self.assertRaisesRegex(SourceFetchError, "HTML"):
            tuple(adapter.fetch(_request()))

    def test_ishares_current_snapshot_rejects_future_and_stale_asof(self) -> None:
        for as_of, message in (
            ("Aug 13 2026", "after the observation"),
            ("Jul 01 2026", "too stale"),
        ):
            payload = (
                f"Fund Holdings as of,{as_of}\n"
                "Ticker,Name,CUSIP,ISIN\nAAPL,Apple,037833100,US0378331005\n"
            ).encode()
            adapter = ISharesIVVObservedSnapshotAdapter(
                transport=_FakeHTTPTransport(payload),
                clock=lambda: OBSERVED_AT,
            )
            with self.assertRaisesRegex(SourceFetchError, message):
                tuple(adapter.fetch(_request()))

    def test_ishares_product_data_freezes_exact_date_as_validation_only(self) -> None:
        snapshot_date = date(2026, 3, 31)
        payload = _ishares_product_payload(snapshot_date)
        transport = _FakeHTTPTransport(
            payload, headers={"Content-Type": "application/json"}
        )
        adapter = ISharesIVVHistoricalReconciliationAdapter(
            (snapshot_date,), transport=transport, clock=lambda: OBSERVED_AT
        )

        artifact = tuple(adapter.fetch(_request(date(2026, 1, 1), date(2026, 7, 31))))[0]

        self.assertIn("component=holdings.all", transport.calls[0][0])
        self.assertIn("asOfDate=20260331", transport.calls[0][0])
        self.assertEqual(artifact.payload, payload)
        self.assertEqual(artifact.as_of_date, snapshot_date)
        self.assertEqual(artifact.published_at, OBSERVED_AT)
        self.assertEqual(artifact.role, SourceRole.VALIDATION_ANCHOR)
        self.assertFalse(artifact.metadata["eligible_for_historical_signal"])
        self.assertEqual(artifact.metadata["row_count"], 500)

    def test_ishares_product_data_rejects_wrong_date_and_misaligned_arrays(self) -> None:
        snapshot_date = date(2026, 3, 31)
        for payload, message in (
            (_ishares_product_payload(date(2026, 2, 27)), "as-of date"),
            (_ishares_product_payload(snapshot_date, mismatched="isin"), "different lengths"),
            (b"<!doctype html><title>challenge</title>", "valid JSON"),
        ):
            adapter = ISharesIVVHistoricalReconciliationAdapter(
                (snapshot_date,),
                transport=_FakeHTTPTransport(payload),
                clock=lambda: OBSERVED_AT,
            )
            with self.assertRaisesRegex(SourceFetchError, message):
                tuple(adapter.fetch(_request(date(2026, 1, 1), date(2026, 7, 31))))

    def test_tdx_is_primary_read_only_market_evidence_but_not_membership(self) -> None:
        provider = _FakeMarketProvider(
            MarketEvidencePayload(
                dataset="bars_raw",
                payload=b"ticker,date,open\nAAPL,2025-12-31,200\n",
                media_type="text/csv",
                url="http://127.0.0.1:17709/tqcenter/get_market_data",
                as_of_date=date(2025, 12, 31),
            )
        )
        artifact = tuple(
            TDXUSMarketEvidenceAdapter(provider, source_version="tdxw-2026.08").fetch(
                _request()
            )
        )[0]

        self.assertEqual(artifact.dataset, "bars_raw")
        self.assertEqual(artifact.role, SourceRole.SIGNAL_INPUT)
        self.assertEqual(artifact.license_class, LicenseClass.LOCAL_VENDOR)
        self.assertTrue(artifact.metadata["read_only"])
        self.assertFalse(artifact.metadata["membership_authority"])

    def test_akshare_is_pinned_cross_check_and_cannot_overwrite_tdx(self) -> None:
        cross_check = _FakeMarketProvider(
            MarketEvidencePayload(
                dataset="bars_cross_check",
                payload=b"ticker,date,open\nAAPL,2025-12-31,200\n",
                media_type="text/csv",
                url="akshare://stock_us_daily",
            )
        )
        artifact = tuple(
            AKShareUSCrossCheckAdapter(cross_check, package_version="1.17.87").fetch(
                _request()
            )
        )[0]

        self.assertEqual(artifact.role, SourceRole.CROSS_CHECK)
        self.assertEqual(artifact.license_class, LicenseClass.PERMISSIVE)
        self.assertFalse(artifact.metadata["may_override_tdx"])
        self.assertFalse(artifact.metadata["eligible_for_signal"])
        self.assertEqual(artifact.metadata["package_version"], "1.17.87")

        overwrite = _FakeMarketProvider(
            MarketEvidencePayload(
                dataset="bars_raw",
                payload=b"forbidden",
                media_type="text/csv",
                url="akshare://stock_us_daily",
            )
        )
        with self.assertRaisesRegex(SourcePolicyError, "cannot overwrite TDX"):
            tuple(
                AKShareUSCrossCheckAdapter(overwrite, package_version="1.17.87").fetch(
                    _request()
                )
            )


if __name__ == "__main__":
    unittest.main()
