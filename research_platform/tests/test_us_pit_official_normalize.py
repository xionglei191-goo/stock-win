from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research_platform.__main__ import build_parser
from research_platform.us_pit import LicenseClass, SourceDependency, SourceRole
from research_platform.us_pit.hashing import sha256_file
from research_platform.us_pit.official_normalize import (
    OfficialHoldingsNormalizationService,
    OfficialNormalizationError,
)
from research_platform.us_pit.sources_official import SourcePolicyError
from research_platform.us_pit.store import USPITStore


SEC_ACCESSION = "0001752724-25-119791"
OBSERVED_AT = "2025-06-01T12:00:00+00:00"
ACCEPTED_AT = "2025-05-27T14:41:12+00:00"


def _sec_payload(*investments: str) -> bytes:
    return f"""<SEC-DOCUMENT>{SEC_ACCESSION}.txt
<SEC-HEADER>
<ACCEPTANCE-DATETIME>20250527104112
ACCESSION NUMBER: {SEC_ACCESSION}
CONFORMED SUBMISSION TYPE: NPORT-P
CONFORMED PERIOD OF REPORT: 20250331
FILED AS OF DATE: 20250527
FILER:
  CENTRAL INDEX KEY: 0001100663
<SERIES><SERIES-ID>S000004310</SERIES>
</SEC-HEADER>
<DOCUMENT><TYPE>NPORT-P
<TEXT><XML><edgarSubmission><formData><genInfo>
<regCik>0001100663</regCik><seriesId>S000004310</seriesId>
</genInfo><fundInfo><invstOrSecs>{''.join(investments)}</invstOrSecs></fundInfo>
</formData></edgarSubmission></XML></TEXT></DOCUMENT>
""".encode("ascii")


def _investment(
    *,
    name: str,
    title: str,
    ticker: str,
    cusip: str = "",
    isin: str = "",
    asset_category: str = "EC",
    lei: str = "5493001KJTIIGC8Y1R12",
    cik: str = "1652044",
) -> str:
    identifiers = ""
    if cusip:
        identifiers += f"<cusip>{cusip}</cusip>"
    if isin:
        identifiers += f'<isin value="{isin}" />'
    return f"""<invstOrSec><name>{name}</name><title>{title}</title>
<lei>{lei}</lei><cik>{cik}</cik><identifiers>{identifiers}<ticker>{ticker}</ticker></identifiers>
<balance>100</balance><valUSD>12345.67</valUSD><pctVal>1.2</pctVal>
<assetCat>{asset_category}</assetCat><curCd>USD</curCd></invstOrSec>"""


def _sec_dependency(store: USPITStore, payload: bytes, **changes: object) -> SourceDependency:
    reference = store.put_bytes(payload, media_type="text/plain")
    values: dict[str, object] = {
        "source_id": "sec_nport_ivv",
        "source_version": "sec-edgar-ivv-nport-raw-v2",
        "role": SourceRole.VALIDATION_ANCHOR,
        "license_class": LicenseClass.OFFICIAL_PUBLIC,
        "object_sha256": reference.sha256,
        "observed_at": OBSERVED_AT,
        "url": (
            "https://www.sec.gov/Archives/edgar/data/1100663/"
            f"{SEC_ACCESSION.replace('-', '')}/{SEC_ACCESSION}.txt"
        ),
        "dataset": "fund_holdings_observed",
        "as_of_date": "2025-03-31",
        "published_at": ACCEPTED_AT,
        "metadata": {
            "artifact_kind": "raw_complete_edgar_submission",
            "accession_number": SEC_ACCESSION,
            "registrant_cik": "0001100663",
            "series_id": "S000004310",
            "eligible_for_historical_signal": False,
            "response_sha256": reference.sha256,
        },
    }
    values.update(changes)
    return SourceDependency(**values)  # type: ignore[arg-type]


def _ishares_payload(*rows: str) -> bytes:
    return (
        'iShares Core S&P 500 ETF Fund Holdings as of,"Aug 12, 2026"\n'
        "Ticker,Name,Asset Class,Weight (%),Price,Quantity,Market Value,Notional Value,Sector,SEDOL,ISIN,CUSIP,Exchange,Currency\n"
        + "\n".join(rows)
        + "\n"
    ).encode("utf-8")


def _ishares_dependency(
    store: USPITStore,
    payload: bytes,
    *,
    historical: bool = False,
) -> SourceDependency:
    reference = store.put_bytes(payload, media_type="text/csv")
    observed_at = "2026-08-13T02:00:00+00:00"
    return SourceDependency(
        source_id="ishares_ivv_holdings",
        source_version="ishares-ivv-observed-raw-v1",
        role=SourceRole.VALIDATION_ANCHOR if historical else SourceRole.SIGNAL_INPUT,
        license_class=LicenseClass.OFFICIAL_PUBLIC,
        object_sha256=reference.sha256,
        observed_at=observed_at,
        url="https://www.ishares.com/us/products/239726/example.ajax",
        dataset="fund_holdings_observed",
        as_of_date="2026-08-12",
        published_at=None if historical else observed_at,
        metadata={
            "artifact_kind": "raw_observed_holdings_csv",
            "observation_mode": (
                "historical_as_of_reconciliation" if historical else "current"
            ),
            "historical_publication_time_proven": False,
            "eligible_from": None if historical else observed_at,
            "eligible_for_historical_signal": not historical,
            "response_sha256": reference.sha256,
        },
    )


def _ishares_api_payload(snapshot_date: str, rows: list[dict[str, object]]) -> bytes:
    fields = (
        "ticker", "issueName", "assetClass", "cusip", "isin", "exchange",
        "currencyCode", "unitsHeld", "marketValue", "holdingPercent",
    )
    padded = rows + [
        {
            "ticker": f"DUMMY{index}",
            "issueName": f"Dummy {index}",
            "assetClass": "Equity",
            "cusip": "037833100",
            "isin": "US0378331005",
            "exchange": "NASDAQ",
            "currencyCode": "USD",
            "unitsHeld": 1,
            "marketValue": 1,
            "holdingPercent": 0.001,
        }
        for index in range(400 - len(rows))
    ]
    points = {
        field: {
            "value": [row.get(field) for row in padded],
            "formattedValue": [row.get(field) for row in padded],
        }
        for field in fields
    }
    points["asOfDate"] = {
        "value": snapshot_date.replace("-", ""),
        "formattedValue": datetime.strptime(snapshot_date, "%Y-%m-%d").strftime("%b %d, %Y"),
    }
    return json.dumps(
        {
            "productId": 239726,
            "componentsByNameMap": {"holdings": {"containersByNameMap": {"all": {"dataPointsByNameMap": points}}}},
        }
    ).encode()


def _ishares_api_dependency(store: USPITStore, payload: bytes, snapshot_date: str) -> SourceDependency:
    reference = store.put_bytes(payload, media_type="application/json")
    observed_at = "2026-08-13T02:00:00+00:00"
    return SourceDependency(
        source_id="ishares_ivv_holdings_api",
        source_version="ishares-ivv-product-data-raw-v1",
        role=SourceRole.VALIDATION_ANCHOR,
        license_class=LicenseClass.OFFICIAL_PUBLIC,
        object_sha256=reference.sha256,
        observed_at=observed_at,
        url="https://www.ishares.com/varnish-api/example",
        dataset="fund_holdings_observed",
        as_of_date=snapshot_date,
        published_at=observed_at,
        metadata={
            "artifact_kind": "raw_historical_holdings_product_data_json",
            "observation_mode": "historical_as_of_reconciliation",
            "historical_publication_time_proven": False,
            "eligible_for_historical_signal": False,
            "eligible_from": None,
            "response_sha256": reference.sha256,
        },
    )


class OfficialHoldingsNormalizationTests(unittest.TestCase):
    def test_ishares_product_data_preserves_identity_but_not_signal_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            payload = _ishares_api_payload(
                "2026-03-31",
                [
                    {
                        "ticker": "AAPL",
                        "issueName": "APPLE INC",
                        "assetClass": "Equity",
                        "cusip": "037833100",
                        "isin": "US0378331005",
                        "exchange": "NASDAQ",
                        "currencyCode": "USD",
                        "unitsHeld": 100,
                        "marketValue": 20000,
                        "holdingPercent": 7.0,
                    }
                ],
            )
            dependency = _ishares_api_dependency(store, payload, "2026-03-31")
            batch = store.write_source_batch([dependency])

            result = OfficialHoldingsNormalizationService(store).normalize([batch.batch_id])
            holdings = result.load_frame("fund_holdings_observed_candidate")
            apple = holdings.loc[holdings["ticker"].eq("AAPL")].iloc[0]

            self.assertEqual(apple["identity_candidate_key"], "isin:US0378331005")
            self.assertEqual(apple["exchange"], "NASDAQ")
            self.assertFalse(bool(apple["signal_eligible"]))
            self.assertTrue(pd.isna(apple["eligible_from"]))

    def test_latest_sec_amendment_supersedes_same_report_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            original_payload = _sec_payload(
                _investment(
                    name="Apple Inc",
                    title="Common Stock",
                    ticker="AAPL",
                    cusip="037833100",
                    isin="US0378331005",
                )
            )
            amendment_accession = "0001752724-25-129999"
            amendment_payload = _sec_payload(
                _investment(
                    name="Microsoft Corp",
                    title="Common Stock",
                    ticker="MSFT",
                    cusip="594918104",
                    isin="US5949181045",
                )
            ).replace(
                SEC_ACCESSION.encode(), amendment_accession.encode()
            ).replace(b"CONFORMED SUBMISSION TYPE: NPORT-P", b"CONFORMED SUBMISSION TYPE: NPORT-P/A").replace(
                b"<DOCUMENT><TYPE>NPORT-P", b"<DOCUMENT><TYPE>NPORT-P/A"
            ).replace(b"<ACCEPTANCE-DATETIME>20250527104112", b"<ACCEPTANCE-DATETIME>20250528104112").replace(
                b"FILED AS OF DATE: 20250527", b"FILED AS OF DATE: 20250528"
            )
            original = _sec_dependency(store, original_payload)
            amendment = _sec_dependency(
                store,
                amendment_payload,
                object_sha256=store.put_bytes(amendment_payload).sha256,
                url=(
                    "https://www.sec.gov/Archives/edgar/data/1100663/"
                    f"{amendment_accession.replace('-', '')}/{amendment_accession}.txt"
                ),
                published_at="2025-05-28T14:41:12+00:00",
                metadata={
                    **dict(original.metadata),
                    "form": "NPORT-P/A",
                    "accession_number": amendment_accession,
                    "response_sha256": store.put_bytes(amendment_payload).sha256,
                },
            )
            batch = store.write_source_batch([original, amendment])

            result = OfficialHoldingsNormalizationService(store).normalize(
                [batch.batch_id]
            )
            holdings = result.load_frame("fund_holdings_observed_candidate")
            self.assertEqual(list(holdings["ticker"]), ["MSFT"])

    def test_earliest_on_time_original_wins_over_later_amendment_for_reconciliation(self) -> None:
        # N-PORT-SUPERSEDE-v2 (approved): when both the on-time original
        # NPORT-P and a later NPORT-P/A exist for one report date, the
        # earliest-available original is the reconciliation basis.  A late
        # amendment must never advance the anchor's available window, so
        # normalization keeps the original (Apple) rather than the later
        # amendment (Microsoft).
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            original_payload = _sec_payload(
                _investment(
                    name="Apple Inc",
                    title="Common Stock",
                    ticker="AAPL",
                    cusip="037833100",
                    isin="US0378331005",
                )
            )
            amendment_accession = "0002071691-26-019999"
            amendment_payload = _sec_payload(
                _investment(
                    name="Microsoft Corp",
                    title="Common Stock",
                    ticker="MSFT",
                    cusip="594918104",
                    isin="US5949181045",
                )
            ).replace(
                SEC_ACCESSION.encode(), amendment_accession.encode()
            ).replace(b"CONFORMED SUBMISSION TYPE: NPORT-P", b"CONFORMED SUBMISSION TYPE: NPORT-P/A").replace(
                b"<DOCUMENT><TYPE>NPORT-P", b"<DOCUMENT><TYPE>NPORT-P/A"
            )
            original = _sec_dependency(
                store,
                original_payload,
                as_of_date="2025-03-31",
                metadata={
                    **dict(_sec_dependency(store, original_payload).metadata),
                    "form": "NPORT-P",
                },
            )
            amendment = _sec_dependency(
                store,
                amendment_payload,
                as_of_date="2025-03-31",
                published_at="2026-07-13T14:48:14+00:00",
                object_sha256=store.put_bytes(amendment_payload).sha256,
                url=(
                    "https://www.sec.gov/Archives/edgar/data/1100663/"
                    f"{amendment_accession.replace('-', '')}/{amendment_accession}.txt"
                ),
                metadata={
                    **dict(_sec_dependency(store, original_payload).metadata),
                    "form": "NPORT-P/A",
                    "accession_number": amendment_accession,
                    "response_sha256": store.put_bytes(amendment_payload).sha256,
                },
            )
            batch = store.write_source_batch([original, amendment])

            result = OfficialHoldingsNormalizationService(store).normalize(
                [batch.batch_id]
            )
            holdings = result.load_frame("fund_holdings_observed_candidate")
            # Supersede-v2 must retain the on-time original (Apple), not the
            # later amendment (Microsoft).
            self.assertEqual(list(holdings["ticker"]), ["AAPL"])

    def test_sec_keeps_distinct_share_classes_and_never_becomes_signal_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            payload = _sec_payload(
                _investment(
                    name="Alphabet Inc",
                    title="Class A Common Stock",
                    ticker="GOOGL",
                    cusip="02079K305",
                    isin="US02079K3059",
                ),
                _investment(
                    name="Alphabet Inc",
                    title="Class C Common Stock",
                    ticker="GOOG",
                    cusip="02079K107",
                    isin="US02079K1079",
                ),
                _investment(
                    name="US Treasury",
                    title="Treasury Note",
                    ticker="",
                    cusip="91282CJL6",
                    asset_category="DBT",
                ),
            )
            dependency = _sec_dependency(store, payload)
            batch = store.write_source_batch([dependency])

            result = OfficialHoldingsNormalizationService(store).normalize(
                [batch.batch_id]
            )
            holdings = result.load_frame("fund_holdings_observed_candidate")

            self.assertEqual(list(holdings["ticker"]), ["GOOGL", "GOOG"])
            self.assertEqual(set(holdings["share_class"]), {"A", "C"})
            self.assertEqual(holdings["identity_candidate_key"].nunique(), 2)
            self.assertFalse(holdings["signal_eligible"].any())
            self.assertTrue(holdings["eligible_from"].isna().all())
            self.assertEqual(set(holdings["evidence_role"]), {"VALIDATION_ANCHOR"})
            self.assertEqual(set(holdings["lei"]), {"5493001KJTIIGC8Y1R12"})
            self.assertEqual(set(holdings["cik"]), {"0001652044"})
            issues = result.load_frame("normalization_issues")
            self.assertIn("NON_COMMON_EQUITY_FILTERED", set(issues["code"]))

    def test_long_word_after_class_is_not_misread_as_a_share_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            payload = _sec_payload(
                _investment(
                    name="LyondellBasell Industries NV Class LyondellBasell",
                    title="LyondellBasell Industries NV Class LyondellBasell",
                    ticker="LYB",
                    cusip="N53745100",
                    isin="NL0009434992",
                )
            )
            batch = store.write_source_batch([_sec_dependency(store, payload)])
            result = OfficialHoldingsNormalizationService(store).normalize(
                [batch.batch_id]
            )
            holdings = result.load_frame("fund_holdings_observed_candidate")
            self.assertTrue(pd.isna(holdings.iloc[0]["share_class"]))

    def test_missing_identifier_is_preserved_as_a_high_review_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            payload = _sec_payload(
                _investment(
                    name="Unknown Corp",
                    title="Class A Common Stock",
                    ticker="UNKNOWN",
                )
            )
            batch = store.write_source_batch([_sec_dependency(store, payload)])

            result = OfficialHoldingsNormalizationService(store).normalize(
                [batch.batch_id]
            )
            holdings = result.load_frame("fund_holdings_observed_candidate")
            issues = result.load_frame("normalization_issues")

            self.assertTrue(pd.isna(holdings.loc[0, "identity_candidate_key"]))
            missing = issues.loc[issues["code"].eq("MISSING_STABLE_IDENTIFIER")]
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing.iloc[0]["severity"], "HIGH")
            self.assertTrue(bool(missing.iloc[0]["requires_manual_review"]))
            self.assertEqual(result.manifest["release_status"], "DATA_BLOCKED")

    def test_late_sec_filing_cannot_be_relabelled_as_historical_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            payload = _sec_payload(
                _investment(
                    name="Apple Inc",
                    title="Common Stock",
                    ticker="AAPL",
                    cusip="037833100",
                    isin="US0378331005",
                )
            )
            dependency = _sec_dependency(
                store,
                payload,
                role=SourceRole.SIGNAL_INPUT,
            )
            batch = store.write_source_batch([dependency])

            with self.assertRaisesRegex(SourcePolicyError, "validation anchor"):
                OfficialHoldingsNormalizationService(store).normalize([batch.batch_id])

    def test_ishares_current_eligibility_and_hash_lineage_are_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            payload = _ishares_payload(
                "AAPL,APPLE INC,Equity,7.12,225.00,100,22500,22500,Information Technology,2046251,US0378331005,037833100,NASDAQ,USD"
            )
            dependency = _ishares_dependency(store, payload)
            batch = store.write_source_batch([dependency])
            service = OfficialHoldingsNormalizationService(store)

            result = service.normalize([batch.batch_id])
            repeated = service.normalize([batch.batch_id])
            holdings = result.load_frame("fund_holdings_observed_candidate")

            self.assertEqual(repeated.normalization_id, result.normalization_id)
            self.assertEqual(holdings.loc[0, "content_sha256"], dependency.object_sha256)
            self.assertEqual(holdings.loc[0, "eligible_from"], dependency.observed_at)
            self.assertTrue(bool(holdings.loc[0, "signal_eligible"]))
            self.assertEqual(holdings.loc[0, "exchange"], "NASDAQ")
            self.assertEqual(result.manifest["status"], "REVIEW_REQUIRED")
            self.assertFalse(result.manifest["direct_build_allowed"])
            self.assertEqual(
                result.manifest["sources"][0]["object_sha256"],
                dependency.object_sha256,
            )
            for descriptor in result.manifest["artifacts"].values():
                self.assertEqual(
                    sha256_file(result.path / descriptor["filename"]),
                    descriptor["object_sha256"],
                )
            manifest = json.loads((result.path / "manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["normalization_id"], result.normalization_id)

    def test_corrupt_source_object_and_cli_contract_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "pit")
            payload = _ishares_payload(
                "AAPL,APPLE INC,Equity,7.12,225.00,100,22500,22500,Information Technology,2046251,US0378331005,037833100,NASDAQ,USD"
            )
            dependency = _ishares_dependency(store, payload)
            batch = store.write_source_batch([dependency])
            object_path = store.object_path(dependency.object_sha256)
            object_path.chmod(0o600)
            object_path.write_bytes(b"tampered")

            with self.assertRaisesRegex(
                OfficialNormalizationError, "missing or corrupt"
            ):
                OfficialHoldingsNormalizationService(store).normalize([batch.batch_id])

        args = build_parser().parse_args(
            ["us-pit", "normalize-official", "--source-batch", "a" * 64]
        )
        self.assertEqual(args.us_pit_command, "normalize-official")
        self.assertEqual(args.source_batch, ["a" * 64])
        reconciliation = build_parser().parse_args(
            [
                "us-pit",
                "sync-ishares-reconciliation",
                "--start",
                "2021-08-01",
                "--end",
                "2026-07-31",
            ]
        )
        self.assertEqual(
            reconciliation.us_pit_command, "sync-ishares-reconciliation"
        )


if __name__ == "__main__":
    unittest.main()
