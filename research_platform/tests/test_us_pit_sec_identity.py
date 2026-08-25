from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from research_platform.__main__ import build_parser
from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file, sha256_json
from research_platform.us_pit.sec_identity_candidates import build_sec_cik_candidates
from research_platform.us_pit.sec_filing_candidates import (
    build_sec_filing_candidates,
    load_unique_candidate_ciks,
)
from research_platform.us_pit.service import USPITService
from research_platform.us_pit.sources import SyncRequest
from research_platform.us_pit.sources_official import HTTPResponse, SourceFetchError
from research_platform.us_pit.sources_sec_identity import (
    SECCompanyIdentityIndexAdapter,
    SECCompanySubmissionsAdapter,
    SECFilingDocumentsAdapter,
    captured_filing_accessions,
    rebind_existing_filing_documents,
)
from research_platform.us_pit.store import USPITStore


OBSERVED = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)


class _Transport:
    def __init__(self, payload: bytes, *, url: str = "https://www.sec.gov/files/company_tickers.json") -> None:
        self.payload = payload
        self.url = url
        self.headers: dict[str, str] | None = None

    def get(self, url: str, *, headers, timeout: float) -> HTTPResponse:
        self.headers = dict(headers)
        return HTTPResponse(
            url=self.url,
            status_code=200,
            content=self.payload,
            headers={"Content-Type": "application/json"},
        )


class _URLTransport:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def get(self, url: str, *, headers, timeout: float) -> HTTPResponse:
        self.calls.append(url)
        return HTTPResponse(
            url=url,
            status_code=200,
            content=self.payloads[url],
            headers={"Content-Type": "application/json"},
        )


class _RetryURLTransport(_URLTransport):
    def __init__(self, payloads: dict[str, bytes]) -> None:
        super().__init__(payloads)
        self.failures = 1

    def get(self, url: str, *, headers, timeout: float) -> HTTPResponse:
        if self.failures:
            self.failures -= 1
            raise SourceFetchError("transient response interruption")
        return super().get(url, headers=headers, timeout=timeout)


class _StatusRetryTransport(_URLTransport):
    def __init__(self, payloads: dict[str, bytes]) -> None:
        super().__init__(payloads)
        self.failures = 1

    def get(self, url: str, *, headers, timeout: float) -> HTTPResponse:
        self.calls.append(url)
        if self.failures:
            self.failures -= 1
            return HTTPResponse(
                url=url,
                status_code=503,
                content=b"temporarily unavailable",
                headers={"Retry-After": "0"},
            )
        return HTTPResponse(
            url=url,
            status_code=200,
            content=self.payloads[url],
            headers={"Content-Type": "text/plain"},
        )


def _company_index() -> bytes:
    return json.dumps({
        "0": {"cik_str": 123, "ticker": "NEW", "title": "New Corp"},
        "1": {"cik_str": 456, "ticker": "OTHER", "title": "Old Corp"},
        "2": {"cik_str": 456, "ticker": "OTHER-P", "title": "Old Corp"},
    }).encode("utf-8")


def _request_package(root: Path) -> Path:
    package = root / "requests"
    package.mkdir()
    frame = pd.DataFrame([{
        "request_id": "r" * 64,
        "anchor_date": "2024-12-31",
        "predecessor_security_id": "us_isin_old",
        "successor_security_id": "us_isin_new",
        "predecessor_name": "Old Corp",
        "successor_name": "New Corp",
        "predecessor_ticker": "OLD",
        "successor_ticker": "NEW",
    }])
    artifact = package / "corporate_action_evidence_requests.parquet"
    frame.to_parquet(artifact, index=False)
    manifest = {
        "request_set_id": "q" * 64,
        "artifact_sha256": sha256_file(artifact),
        "candidate_only": True,
        "direct_build_allowed": False,
    }
    (package / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return package


def _submission_payload(*, cik: str, include_file: bool = True) -> bytes:
    recent = {
        "accessionNumber": ["0000000123-24-000001", "0000000123-19-000002"],
        "filingDate": ["2024-12-15", "2019-01-01"],
        "reportDate": ["2024-12-14", "2018-12-31"],
        "acceptanceDateTime": ["2024-12-15T16:30:00.000Z", "2019-01-01T12:00:00.000Z"],
        "form": ["8-K", "10-K"],
        "items": ["1.01,2.01", ""],
        "primaryDocument": ["event.htm", "annual.htm"],
        "primaryDocDescription": ["CURRENT REPORT", "ANNUAL REPORT"],
    }
    files = [{"name": f"CIK{cik}-submissions-001.json"}] if include_file else []
    return json.dumps({"cik": str(int(cik)), "filings": {"recent": recent, "files": files}}).encode()


def _submission_shard() -> bytes:
    return json.dumps({
        "accessionNumber": ["0000000123-20-000003"],
        "filingDate": ["2020-01-15"],
        "reportDate": ["2020-01-14"],
        "acceptanceDateTime": ["2020-01-15T16:30:00.000Z"],
        "form": ["S-4"],
        "items": [""],
        "primaryDocument": ["merger.htm"],
        "primaryDocDescription": ["REGISTRATION STATEMENT"],
    }).encode()


def _filing_candidate_package(root: Path) -> Path:
    package = root / "filing-candidates"
    package.mkdir()
    accession = "0000000123-24-000001"
    frame = pd.DataFrame([{
        "accession_number": accession,
        "cik": "0000000123",
        "complete_submission_url": (
            "https://www.sec.gov/Archives/edgar/data/123/"
            f"{accession.replace('-', '')}/{accession}.txt"
        ),
        "form": "8-K",
        "filing_date": "2024-12-15",
        "accepted_at": "2024-12-15T21:30:00.000Z",
    }])
    artifact = package / "sec_filing_candidates.parquet"
    frame.to_parquet(artifact, index=False)
    manifest = {
        "candidate_set_id": "f" * 64,
        "artifact_sha256": sha256_file(artifact),
        "candidate_only": True,
        "direct_build_allowed": False,
    }
    (package / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return package


def _reidentified_filing_candidate_package(root: Path) -> Path:
    source = root / "filing-candidates"
    if not source.is_dir():
        source = _filing_candidate_package(root)
    target = root / "filing-candidates-v2"
    target.mkdir()
    frame = pd.read_parquet(source / "sec_filing_candidates.parquet")
    artifact = target / "sec_filing_candidates.parquet"
    frame.to_parquet(artifact, index=False)
    (target / "manifest.json").write_bytes(canonical_json_bytes({
        "candidate_set_id": "e" * 64,
        "artifact_sha256": sha256_file(artifact),
        "candidate_only": True,
        "direct_build_allowed": False,
    }))
    return target


def _complete_submission() -> bytes:
    return b"""<SEC-DOCUMENT>0000000123-24-000001.txt
<SEC-HEADER>
<ACCEPTANCE-DATETIME>20241215163000
ACCESSION NUMBER: 0000000123-24-000001
CENTRAL INDEX KEY: 0000000123
CONFORMED SUBMISSION TYPE: 8-K
</SEC-HEADER>
<DOCUMENT><TYPE>8-K<TEXT>transaction candidate only</TEXT></DOCUMENT>
"""


class SECIdentityCandidateTests(unittest.TestCase):
    def test_adapter_freezes_current_index_as_crosscheck_only(self) -> None:
        transport = _Transport(_company_index())
        adapter = SECCompanyIdentityIndexAdapter(
            user_agent="Research test@example.com",
            transport=transport,
            clock=lambda: OBSERVED,
        )
        artifacts = tuple(adapter.fetch(SyncRequest(date(2026, 8, 14), date(2026, 8, 14), OBSERVED)))
        self.assertEqual(1, len(artifacts))
        artifact = artifacts[0]
        self.assertEqual("CROSS_CHECK", artifact.role.value)
        self.assertTrue(artifact.metadata["current_snapshot_only"])
        self.assertFalse(artifact.metadata["historical_identity_authority"])
        self.assertFalse(artifact.metadata["corporate_action_evidence"])
        self.assertIn("test@example.com", transport.headers["User-Agent"])

    def test_adapter_rejects_schema_drift(self) -> None:
        adapter = SECCompanyIdentityIndexAdapter(
            user_agent="Research test@example.com",
            transport=_Transport(json.dumps({"0": {"ticker": "ABC"}}).encode()),
            clock=lambda: OBSERVED,
        )
        with self.assertRaisesRegex(SourceFetchError, "schema"):
            tuple(adapter.fetch(SyncRequest(date(2026, 8, 14), date(2026, 8, 14), OBSERVED)))

    def test_candidates_are_review_only_and_use_exact_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = USPITStore(root / "pit")
            service = USPITService(store)
            batch = service.sync(
                SECCompanyIdentityIndexAdapter(
                    user_agent="Research test@example.com",
                    transport=_Transport(_company_index()),
                    clock=lambda: OBSERVED,
                ),
                SyncRequest(date(2026, 8, 14), date(2026, 8, 14), OBSERVED),
            )
            result = build_sec_cik_candidates(
                store,
                [batch.batch_id],
                _request_package(root),
                root / "candidates",
            )
            frame = pd.read_parquet(result.path / "sec_cik_candidates.parquet")
            self.assertEqual({"0000000456", "0000000123"}, set(frame["candidate_cik"]))
            self.assertTrue(frame["current_snapshot_only"].all())
            self.assertFalse(frame["historical_identity_confirmed"].any())
            self.assertFalse(frame["corporate_action_evidence"].any())
            self.assertFalse(frame["approved"].any())
            self.assertFalse(result.manifest["direct_build_allowed"])
            old = frame.loc[frame["side"].eq("PREDECESSOR")].iloc[0]
            self.assertEqual("CANDIDATE", old["match_status"])
            self.assertEqual("OTHER|OTHER-P", old["candidate_ticker"])

    def test_unresolved_side_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = USPITStore(root / "pit")
            service = USPITService(store)
            payload = json.dumps({
                "0": {"cik_str": 123, "ticker": "OTHER", "title": "Other Corp"},
            }).encode()
            batch = service.sync(
                SECCompanyIdentityIndexAdapter(
                    user_agent="Research test@example.com",
                    transport=_Transport(payload),
                    clock=lambda: OBSERVED,
                ),
                SyncRequest(date(2026, 8, 14), date(2026, 8, 14), OBSERVED),
            )
            result = build_sec_cik_candidates(
                store, [batch.batch_id], _request_package(root), root / "candidates"
            )
            frame = pd.read_parquet(result.path / "sec_cik_candidates.parquet")
            self.assertEqual(2, len(frame))
            self.assertEqual({"UNRESOLVED"}, set(frame["match_status"]))
            self.assertEqual({""}, set(frame["candidate_cik"]))

    def test_cli_contract_is_explicit(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["us-pit", "sync-sec-company-index"])
        self.assertEqual("sync-sec-company-index", args.us_pit_command)
        args = parser.parse_args([
            "us-pit", "propose-sec-cik",
            "--source-batch", "a" * 64,
            "--evidence-request-dir", "requests",
            "--output-dir", "candidates",
        ])
        self.assertEqual(["a" * 64], args.source_batch)
        args = parser.parse_args([
            "us-pit", "screen-sec-filings",
            "--filing-candidate-dir", "filings",
            "--evidence-request-dir", "requests",
            "--output-dir", "screen",
        ])
        self.assertEqual([], args.source_batch)

    def test_submissions_freezes_main_and_historical_shard(self) -> None:
        cik = "0000000123"
        main = f"https://data.sec.gov/submissions/CIK{cik}.json"
        shard = f"https://data.sec.gov/submissions/CIK{cik}-submissions-001.json"
        transport = _URLTransport({
            main: _submission_payload(cik=cik),
            shard: _submission_shard(),
        })
        adapter = SECCompanySubmissionsAdapter(
            [cik],
            user_agent="Research test@example.com",
            transport=transport,
            clock=lambda: OBSERVED,
            minimum_request_interval_seconds=0,
        )
        artifacts = tuple(adapter.fetch(
            SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED)
        ))
        self.assertEqual(2, len(artifacts))
        self.assertEqual(
            {"company_submissions_main", "company_submissions_historical_shard"},
            {item.metadata["artifact_kind"] for item in artifacts},
        )
        self.assertTrue(all(item.metadata["discovery_only"] for item in artifacts))
        self.assertTrue(all(not item.metadata["corporate_action_terms_verified"] for item in artifacts))

    def test_filing_candidates_are_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = USPITStore(root / "pit")
            service = USPITService(store)
            company_batch = service.sync(
                SECCompanyIdentityIndexAdapter(
                    user_agent="Research test@example.com",
                    transport=_Transport(_company_index()),
                    clock=lambda: OBSERVED,
                ),
                SyncRequest(date(2026, 8, 14), date(2026, 8, 14), OBSERVED),
            )
            cik_result = build_sec_cik_candidates(
                store, [company_batch.batch_id], _request_package(root), root / "candidates"
            )
            ciks = load_unique_candidate_ciks(cik_result.path)
            payloads: dict[str, bytes] = {}
            for cik in ciks:
                main = f"https://data.sec.gov/submissions/CIK{cik}.json"
                payloads[main] = _submission_payload(cik=cik, include_file=False)
            submission_batch = service.sync(
                SECCompanySubmissionsAdapter(
                    ciks,
                    user_agent="Research test@example.com",
                    transport=_URLTransport(payloads),
                    clock=lambda: OBSERVED,
                    minimum_request_interval_seconds=0,
                ),
                SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED),
            )
            result = build_sec_filing_candidates(
                store, [submission_batch.batch_id], cik_result.path, root / "filings"
            )
            frame = pd.read_parquet(result.path / "sec_filing_candidates.parquet")
            selected = frame.loc[frame["accession_number"].astype(str).ne("")]
            self.assertGreaterEqual(len(selected), 1)
            self.assertEqual({"8-K"}, set(selected["form"]))
            self.assertFalse(frame["corporate_action_relevance_confirmed"].any())
            self.assertFalse(frame["action_terms_verified"].any())
            self.assertFalse(frame["approved"].any())
            self.assertFalse(result.manifest["direct_build_allowed"])
            self.assertEqual("sec-form-window-items-v2", result.manifest["discovery_algorithm"])

    def test_complete_submission_capture_verifies_identity_and_eastern_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _filing_candidate_package(root)
            url = pd.read_parquet(
                package / "sec_filing_candidates.parquet"
            ).iloc[0]["complete_submission_url"]
            adapter = SECFilingDocumentsAdapter(
                package,
                user_agent="Research test@example.com",
                transport=_URLTransport({url: _complete_submission()}),
                clock=lambda: OBSERVED,
                minimum_request_interval_seconds=0,
            )
            artifacts = tuple(adapter.fetch(
                SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED)
            ))
            self.assertEqual(1, len(artifacts))
            artifact = artifacts[0]
            self.assertEqual("2024-12-15T21:30:00+00:00", artifact.published_at.isoformat())
            self.assertFalse(artifact.metadata["corporate_action_relevance_confirmed"])
            self.assertFalse(artifact.metadata["corporate_action_terms_verified"])

    def test_complete_submission_capture_rejects_wrong_cik(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _filing_candidate_package(root)
            url = pd.read_parquet(
                package / "sec_filing_candidates.parquet"
            ).iloc[0]["complete_submission_url"]
            payload = _complete_submission().replace(
                b"CENTRAL INDEX KEY: 0000000123",
                b"CENTRAL INDEX KEY: 0000000999",
            )
            adapter = SECFilingDocumentsAdapter(
                package,
                user_agent="Research test@example.com",
                transport=_URLTransport({url: payload}),
                clock=lambda: OBSERVED,
                minimum_request_interval_seconds=0,
            )
            with self.assertRaisesRegex(SourceFetchError, "CIK"):
                tuple(adapter.fetch(
                    SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED)
                ))

    def test_complete_submission_capture_retries_transport_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _filing_candidate_package(root)
            url = pd.read_parquet(
                package / "sec_filing_candidates.parquet"
            ).iloc[0]["complete_submission_url"]
            transport = _RetryURLTransport({url: _complete_submission()})
            adapter = SECFilingDocumentsAdapter(
                package,
                user_agent="Research test@example.com",
                transport=transport,
                clock=lambda: OBSERVED,
                minimum_request_interval_seconds=0,
                retry_backoff_seconds=0,
            )
            artifacts = tuple(adapter.fetch(
                SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED)
            ))
            self.assertEqual(1, len(artifacts))
            self.assertEqual(1, len(transport.calls))

    def test_complete_submission_capture_retries_temporary_http_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _filing_candidate_package(root)
            url = pd.read_parquet(
                package / "sec_filing_candidates.parquet"
            ).iloc[0]["complete_submission_url"]
            transport = _StatusRetryTransport({url: _complete_submission()})
            adapter = SECFilingDocumentsAdapter(
                package,
                user_agent="Research test@example.com",
                transport=transport,
                clock=lambda: OBSERVED,
                minimum_request_interval_seconds=0,
                retry_backoff_seconds=0,
            )
            artifacts = tuple(adapter.fetch(
                SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED)
            ))
            self.assertEqual(1, len(artifacts))
            self.assertEqual(2, len(transport.calls))

    def test_captured_accessions_are_resumable_by_candidate_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _filing_candidate_package(root)
            url = pd.read_parquet(
                package / "sec_filing_candidates.parquet"
            ).iloc[0]["complete_submission_url"]
            store = USPITStore(root / "pit")
            service = USPITService(store)
            adapter = SECFilingDocumentsAdapter(
                package,
                user_agent="Research test@example.com",
                transport=_URLTransport({url: _complete_submission()}),
                clock=lambda: OBSERVED,
                minimum_request_interval_seconds=0,
            )
            batch = service.sync(
                adapter,
                SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED),
            )
            accessions, batches = captured_filing_accessions(
                store,
                candidate_set_id=adapter.candidate_set_id,
                candidate_manifest_sha256=adapter.candidate_manifest_sha256,
            )
            self.assertEqual({"0000000123-24-000001"}, accessions)
            self.assertEqual({batch.batch_id}, batches)

    def test_existing_cas_document_can_be_rebound_to_new_candidate_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = _filing_candidate_package(root)
            url = pd.read_parquet(first / "sec_filing_candidates.parquet").iloc[0][
                "complete_submission_url"
            ]
            store = USPITStore(root / "pit")
            service = USPITService(store)
            old_adapter = SECFilingDocumentsAdapter(
                first,
                user_agent="Research test@example.com",
                transport=_URLTransport({url: _complete_submission()}),
                clock=lambda: OBSERVED,
                minimum_request_interval_seconds=0,
            )
            old_batch = service.sync(
                old_adapter,
                SyncRequest(date(2019, 1, 1), date(2026, 8, 14), OBSERVED),
            )
            second = _reidentified_filing_candidate_package(root)
            new_adapter = SECFilingDocumentsAdapter(
                second,
                user_agent="Research test@example.com",
                transport=_URLTransport({}),
                clock=lambda: OBSERVED,
                minimum_request_interval_seconds=0,
            )
            rebound, batch_ids = rebind_existing_filing_documents(store, new_adapter)
            self.assertEqual({"0000000123-24-000001"}, rebound)
            self.assertEqual(1, len(batch_ids))
            new_batch = store.load_source_batch(batch_ids[0])
            self.assertEqual(
                old_batch.dependencies[0].object_sha256,
                new_batch.dependencies[0].object_sha256,
            )
            metadata = dict(new_batch.dependencies[0].metadata)
            self.assertEqual("e" * 64, metadata["candidate_set_id"])
            self.assertTrue(metadata["cas_rebound_without_network"])


if __name__ == "__main__":
    unittest.main()
