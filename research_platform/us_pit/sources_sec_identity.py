from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from time import monotonic, sleep
from typing import Any, Callable, Iterable

from .models import LicenseClass, SourceRole
from .sources import SourceAdapter, SourceArtifact, SyncRequest
from .hashing import sha256_file
from .store import USPITStore
from .sources_official import (
    HTTPTransport,
    RequestsHTTPTransport,
    SourceConfigurationError,
    SourceFetchError,
    _header,
    _require_host,
    _require_payload,
    _require_sec_user_agent,
    _sha256,
    _validate_observation_time,
)


SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_ROOT = "https://data.sec.gov/submissions"


def captured_filing_accessions(
    store: USPITStore,
    *,
    candidate_set_id: str,
    candidate_manifest_sha256: str,
) -> tuple[set[str], set[str]]:
    captured: set[str] = set()
    batch_ids: set[str] = set()
    for source_batch in store.list_source_batches():
        for dependency in source_batch.dependencies:
            metadata = dict(dependency.metadata)
            if (
                dependency.source_id == "sec_corporate_action_filing_documents"
                and metadata.get("candidate_set_id") == candidate_set_id
                and metadata.get("candidate_manifest_sha256")
                == candidate_manifest_sha256
            ):
                accession = str(metadata.get("accession_number") or "")
                if accession:
                    captured.add(accession)
                    batch_ids.add(source_batch.batch_id)
    return captured, batch_ids


def rebind_existing_filing_documents(
    store: USPITStore,
    adapter: "SECFilingDocumentsAdapter",
    *,
    chunk_size: int = 25,
) -> tuple[set[str], tuple[str, ...]]:
    if chunk_size <= 0 or chunk_size > 100:
        raise ValueError("SEC filing rebind chunk size must be between 1 and 100")
    already, _batch_ids = captured_filing_accessions(
        store,
        candidate_set_id=adapter.candidate_set_id,
        candidate_manifest_sha256=adapter.candidate_manifest_sha256,
    )
    records = {record["accession_number"]: record for record in adapter.records}
    reusable: dict[str, Any] = {}
    for source_batch in store.list_source_batches():
        for dependency in source_batch.dependencies:
            metadata = dict(dependency.metadata)
            accession = str(metadata.get("accession_number") or "")
            record = records.get(accession)
            if (
                dependency.source_id != adapter.source_id
                or dependency.dataset != "corporate_action_source_document"
                or record is None
                or accession in already
            ):
                continue
            try:
                indexed = datetime.fromisoformat(record["accepted_at"].replace("Z", "+00:00"))
                captured = datetime.fromisoformat(str(metadata.get("accepted_at") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if (
                dependency.url != record["url"]
                or str(metadata.get("cik") or "").zfill(10) != record["cik"]
                or str(metadata.get("form") or "").upper() != record["form"]
                or indexed.astimezone(timezone.utc) != captured.astimezone(timezone.utc)
            ):
                continue
            object_path = store.object_path(dependency.object_sha256)
            if (
                not object_path.is_file()
                or sha256_file(object_path) != dependency.object_sha256
                or metadata.get("response_sha256") != dependency.object_sha256
            ):
                raise ValueError("reusable SEC filing document is missing or corrupt")
            previous = reusable.get(accession)
            if previous is not None and previous.object_sha256 != dependency.object_sha256:
                raise ValueError("reusable SEC filing accession has conflicting source objects")
            reusable[accession] = dependency
    rebound_ids: list[str] = []
    ordered = sorted(reusable)
    for offset in range(0, len(ordered), chunk_size):
        dependencies = []
        for accession in ordered[offset : offset + chunk_size]:
            dependency = reusable[accession]
            metadata = dict(dependency.metadata)
            metadata.update({
                "candidate_set_id": adapter.candidate_set_id,
                "candidate_manifest_sha256": adapter.candidate_manifest_sha256,
                "cas_rebound_without_network": True,
                "rebound_from_candidate_set_id": str(
                    dict(dependency.metadata).get("candidate_set_id") or ""
                ),
            })
            dependencies.append(replace(dependency, metadata=metadata))
        rebound_ids.append(store.write_source_batch(dependencies).batch_id)
    return set(ordered), tuple(rebound_ids)


class SECCompanyIdentityIndexAdapter(SourceAdapter):
    """Freeze SEC's current company index as a search aid, never PIT identity truth."""

    source_id = "sec_company_identity_index"
    source_version = "sec-company-tickers-current-v1"

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        transport: HTTPTransport | None = None,
        timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        observation_tolerance: timedelta = timedelta(minutes=5),
        endpoint: str = SEC_COMPANY_TICKERS_URL,
    ) -> None:
        self.user_agent = _require_sec_user_agent(user_agent)
        self.transport = transport or RequestsHTTPTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observation_tolerance = observation_tolerance
        self.endpoint = endpoint
        if self.timeout_seconds <= 0:
            raise SourceConfigurationError("timeout_seconds must be positive")
        _require_host(self.endpoint, "www.sec.gov", source="SEC company index")

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        _validate_observation_time(
            request.observed_at,
            clock=self.clock,
            tolerance=self.observation_tolerance,
        )
        response = self.transport.get(
            self.endpoint,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=self.timeout_seconds,
        )
        _require_host(response.url or self.endpoint, "www.sec.gov", source="SEC company index")
        payload = _require_payload(response, source="SEC company index")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceFetchError("SEC company index is not valid JSON") from exc
        if not isinstance(value, dict) or not value:
            raise SourceFetchError("SEC company index is empty or has an invalid shape")
        rows = list(value.values())
        required = {"cik_str", "ticker", "title"}
        if any(not isinstance(row, dict) or set(row) != required for row in rows):
            raise SourceFetchError("SEC company index row schema changed")
        if any(
            not str(row["cik_str"]).isdigit()
            or not str(row["ticker"]).strip()
            or not str(row["title"]).strip()
            for row in rows
        ):
            raise SourceFetchError("SEC company index contains an invalid identity row")
        yield SourceArtifact(
            dataset="security_identity_index",
            payload=payload,
            media_type=_header(response.headers, "Content-Type") or "application/json",
            url=response.url or self.endpoint,
            observed_at=request.observed_at,
            as_of_date=request.observed_at.date(),
            role=SourceRole.CROSS_CHECK,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            metadata={
                "artifact_kind": "current_company_search_index",
                "company_count": len(rows),
                "response_sha256": _sha256(payload),
                "current_snapshot_only": True,
                "historical_identity_authority": False,
                "corporate_action_evidence": False,
                "signal_eligible": False,
            },
        )


def _submission_rows(value: dict[str, Any], *, source: str) -> int:
    filings = value.get("filings") if source == "main" else value
    if not isinstance(filings, dict):
        raise SourceFetchError("SEC submissions filings object is missing")
    recent = filings.get("recent") if source == "main" else filings
    if not isinstance(recent, dict):
        raise SourceFetchError("SEC submissions recent filing index is missing")
    required = (
        "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime",
        "form", "primaryDocument",
    )
    if any(not isinstance(recent.get(field), list) for field in required):
        raise SourceFetchError("SEC submissions filing arrays changed schema")
    lengths = {len(recent[field]) for field in required}
    if len(lengths) != 1:
        raise SourceFetchError("SEC submissions filing arrays have conflicting lengths")
    return lengths.pop()


class SECCompanySubmissionsAdapter(SourceAdapter):
    """Freeze SEC filing indexes for an explicit reviewed CIK candidate set."""

    source_id = "sec_company_submissions"
    source_version = "sec-submissions-index-v1"

    def __init__(
        self,
        ciks: Iterable[str],
        *,
        user_agent: str | None = None,
        transport: HTTPTransport | None = None,
        timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        observation_tolerance: timedelta = timedelta(minutes=5),
        submissions_root: str = SEC_SUBMISSIONS_ROOT,
        minimum_request_interval_seconds: float = 0.11,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        normalized = tuple(sorted(set(str(value).strip().zfill(10) for value in ciks)))
        if not normalized or any(len(value) != 10 or not value.isdigit() for value in normalized):
            raise SourceConfigurationError("SEC submissions require explicit 10-digit CIKs")
        self.ciks = normalized
        self.user_agent = _require_sec_user_agent(user_agent)
        self.transport = transport or RequestsHTTPTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observation_tolerance = observation_tolerance
        self.submissions_root = submissions_root.rstrip("/")
        self.minimum_request_interval_seconds = float(minimum_request_interval_seconds)
        self.monotonic_clock = monotonic_clock
        self.sleeper = sleeper
        self._last_request_started: float | None = None
        if self.timeout_seconds <= 0:
            raise SourceConfigurationError("timeout_seconds must be positive")
        if self.minimum_request_interval_seconds < 0:
            raise SourceConfigurationError(
                "minimum_request_interval_seconds must not be negative"
            )
        _require_host(self.submissions_root, "data.sec.gov", source="SEC submissions")

    def _get_json(self, url: str) -> tuple[bytes, dict[str, Any], HTTPResponse]:
        if self._last_request_started is not None:
            elapsed = self.monotonic_clock() - self._last_request_started
            remaining = self.minimum_request_interval_seconds - elapsed
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request_started = self.monotonic_clock()
        response = self.transport.get(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=self.timeout_seconds,
        )
        _require_host(response.url or url, "data.sec.gov", source="SEC submissions")
        payload = _require_payload(response, source="SEC submissions")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceFetchError("SEC submissions response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise SourceFetchError("SEC submissions response has an invalid shape")
        return payload, value, response

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        _validate_observation_time(
            request.observed_at,
            clock=self.clock,
            tolerance=self.observation_tolerance,
        )
        artifacts: list[SourceArtifact] = []
        for cik in self.ciks:
            main_url = f"{self.submissions_root}/CIK{cik}.json"
            payload, value, response = self._get_json(main_url)
            returned_cik = str(value.get("cik") or "").zfill(10)
            if returned_cik != cik:
                raise SourceFetchError("SEC submissions response CIK does not match the request")
            filing_count = _submission_rows(value, source="main")
            files = value.get("filings", {}).get("files", [])
            if not isinstance(files, list) or any(
                not isinstance(item, dict)
                or not re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", str(item.get("name") or ""))
                for item in files
            ):
                raise SourceFetchError("SEC submissions historical file index changed schema")
            artifacts.append(SourceArtifact(
                dataset="corporate_action_filing_index",
                payload=payload,
                media_type=_header(response.headers, "Content-Type") or "application/json",
                url=response.url or main_url,
                observed_at=request.observed_at,
                as_of_date=request.observed_at.date(),
                role=SourceRole.REFERENCE_ONLY,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                metadata={
                    "artifact_kind": "company_submissions_main",
                    "cik": cik,
                    "filing_count": filing_count,
                    "response_sha256": _sha256(payload),
                    "discovery_only": True,
                    "corporate_action_terms_verified": False,
                    "signal_eligible": False,
                },
            ))
            for item in files:
                name = str(item["name"])
                shard_url = f"{self.submissions_root}/{name}"
                shard_payload, shard_value, shard_response = self._get_json(shard_url)
                shard_count = _submission_rows(shard_value, source="shard")
                artifacts.append(SourceArtifact(
                    dataset="corporate_action_filing_index",
                    payload=shard_payload,
                    media_type=_header(shard_response.headers, "Content-Type") or "application/json",
                    url=shard_response.url or shard_url,
                    observed_at=request.observed_at,
                    as_of_date=request.observed_at.date(),
                    role=SourceRole.REFERENCE_ONLY,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    metadata={
                        "artifact_kind": "company_submissions_historical_shard",
                        "cik": cik,
                        "file_name": name,
                        "filing_count": shard_count,
                        "response_sha256": _sha256(shard_payload),
                        "discovery_only": True,
                        "corporate_action_terms_verified": False,
                        "signal_eligible": False,
                    },
                ))
        return tuple(artifacts)


class SECFilingDocumentsAdapter(SourceAdapter):
    """Freeze deduplicated complete submissions selected by a review-only queue."""

    source_id = "sec_corporate_action_filing_documents"
    source_version = "sec-complete-submission-discovery-v1"

    def __init__(
        self,
        filing_candidate_dir: Path | str,
        *,
        accessions: Iterable[str] | None = None,
        user_agent: str | None = None,
        transport: HTTPTransport | None = None,
        timeout_seconds: float = 120.0,
        clock: Callable[[], datetime] | None = None,
        observation_tolerance: timedelta = timedelta(minutes=5),
        minimum_request_interval_seconds: float = 0.11,
        max_transport_attempts: int = 5,
        retry_backoff_seconds: float = 0.5,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        import pandas as pd

        root = Path(filing_candidate_dir).resolve()
        artifact = root / "sec_filing_candidates.parquet"
        manifest_path = root / "manifest.json"
        if not artifact.is_file() or not manifest_path.is_file():
            raise SourceConfigurationError("SEC filing candidate package is incomplete")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("artifact_sha256") != sha256_file(artifact)
            or manifest.get("candidate_only") is not True
            or manifest.get("direct_build_allowed") is not False
        ):
            raise SourceConfigurationError(
                "SEC filing candidate package failed integrity policy"
            )
        frame = pd.read_parquet(artifact)
        required = {
            "accession_number", "cik", "complete_submission_url", "form",
            "filing_date", "accepted_at",
        }
        if not required.issubset(frame.columns):
            raise SourceConfigurationError("SEC filing candidate schema is incomplete")
        values = frame.loc[frame["accession_number"].astype(str).ne("")].copy()
        records: dict[str, dict[str, str]] = {}
        for row in values.to_dict(orient="records"):
            accession = str(row["accession_number"]).strip()
            record = {
                "accession_number": accession,
                "cik": str(row["cik"]).strip().zfill(10),
                "url": str(row["complete_submission_url"]).strip(),
                "form": str(row["form"]).strip().upper(),
                "filing_date": str(row["filing_date"]).strip(),
                "accepted_at": str(row["accepted_at"]).strip(),
            }
            previous = records.get(accession)
            if previous is not None and previous != record:
                raise SourceConfigurationError(
                    "SEC filing candidate duplicate accession conflicts"
                )
            records[accession] = record
        if not records:
            raise SourceConfigurationError("SEC filing candidate package has no documents")
        selected_accessions = (
            tuple(sorted(records))
            if accessions is None
            else tuple(sorted(set(str(value).strip() for value in accessions)))
        )
        if not selected_accessions or any(value not in records for value in selected_accessions):
            raise SourceConfigurationError(
                "SEC filing document selection contains no records or unknown accessions"
            )
        self.records = tuple(records[key] for key in selected_accessions)
        self.candidate_set_id = str(manifest.get("candidate_set_id") or "")
        self.candidate_manifest_sha256 = sha256_file(manifest_path)
        self.user_agent = _require_sec_user_agent(user_agent)
        self.transport = transport or RequestsHTTPTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observation_tolerance = observation_tolerance
        self.minimum_request_interval_seconds = float(minimum_request_interval_seconds)
        self.max_transport_attempts = int(max_transport_attempts)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.monotonic_clock = monotonic_clock
        self.sleeper = sleeper
        self._last_request_started: float | None = None
        if (
            self.timeout_seconds <= 0
            or self.minimum_request_interval_seconds < 0
            or self.max_transport_attempts <= 0
            or self.retry_backoff_seconds < 0
        ):
            raise SourceConfigurationError("SEC filing document timing policy is invalid")
        for record in self.records:
            _require_host(record["url"], "www.sec.gov", source="SEC filing document")
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", record["accession_number"]):
                raise SourceConfigurationError("SEC filing accession is invalid")
            if len(record["cik"]) != 10 or not record["cik"].isdigit():
                raise SourceConfigurationError("SEC filing CIK is invalid")

    def _get(self, url: str) -> HTTPResponse:
        last_error: SourceFetchError | None = None
        for attempt in range(self.max_transport_attempts):
            if self._last_request_started is not None:
                elapsed = self.monotonic_clock() - self._last_request_started
                remaining = self.minimum_request_interval_seconds - elapsed
                if remaining > 0:
                    self.sleeper(remaining)
            if attempt:
                self.sleeper(self.retry_backoff_seconds * (2 ** (attempt - 1)))
            self._last_request_started = self.monotonic_clock()
            try:
                response = self.transport.get(
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "text/plain, application/octet-stream",
                        "Accept-Encoding": "gzip, deflate",
                    },
                    timeout=self.timeout_seconds,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt + 1 >= self.max_transport_attempts:
                        return response
                    retry_after = _header(response.headers, "Retry-After")
                    if retry_after:
                        try:
                            self.sleeper(min(30.0, max(0.0, float(retry_after))))
                        except ValueError:
                            pass
                    continue
                return response
            except SourceFetchError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        _validate_observation_time(
            request.observed_at,
            clock=self.clock,
            tolerance=self.observation_tolerance,
        )
        for record in self.records:
            response = self._get(record["url"])
            _require_host(response.url or record["url"], "www.sec.gov", source="SEC filing document")
            payload = _require_payload(response, source="SEC filing document")
            text = payload[:2_000_000].decode("latin-1", errors="ignore")
            accession = record["accession_number"]
            if accession not in text:
                raise SourceFetchError("SEC complete submission does not contain its accession")
            cik_patterns = {
                record["cik"],
                str(int(record["cik"])),
            }
            if not any(
                re.search(rf"CENTRAL INDEX KEY:\s*0*{re.escape(cik)}\b", text, re.IGNORECASE)
                for cik in cik_patterns
            ):
                raise SourceFetchError("SEC complete submission CIK does not match candidate")
            accepted_marker = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", text, re.IGNORECASE)
            if accepted_marker is None:
                raise SourceFetchError("SEC complete submission has no acceptance timestamp")
            try:
                indexed_accepted = datetime.fromisoformat(
                    record["accepted_at"].replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise SourceFetchError("SEC submissions acceptance timestamp is invalid") from exc
            if indexed_accepted.tzinfo is None:
                raise SourceFetchError("SEC submissions acceptance timestamp has no timezone")
            indexed_local = indexed_accepted.astimezone(ZoneInfo("America/New_York"))
            if indexed_local.strftime("%Y%m%d%H%M%S") != accepted_marker.group(1):
                raise SourceFetchError("SEC complete submission acceptance timestamp conflicts")
            accepted = datetime.strptime(
                accepted_marker.group(1), "%Y%m%d%H%M%S"
            ).replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
            yield SourceArtifact(
                dataset="corporate_action_source_document",
                payload=payload,
                media_type=_header(response.headers, "Content-Type") or "text/plain",
                url=response.url or record["url"],
                observed_at=request.observed_at,
                as_of_date=date.fromisoformat(record["filing_date"]),
                published_at=accepted,
                role=SourceRole.REFERENCE_ONLY,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                metadata={
                    "artifact_kind": "sec_complete_submission",
                    "candidate_set_id": self.candidate_set_id,
                    "candidate_manifest_sha256": self.candidate_manifest_sha256,
                    "accession_number": accession,
                    "cik": record["cik"],
                    "form": record["form"],
                    "accepted_at": accepted.isoformat(),
                    "response_sha256": _sha256(payload),
                    "corporate_action_relevance_confirmed": False,
                    "corporate_action_terms_verified": False,
                    "signal_eligible": False,
                },
            )


__all__ = [
    "SEC_COMPANY_TICKERS_URL",
    "SEC_SUBMISSIONS_ROOT",
    "SECCompanyIdentityIndexAdapter",
    "SECCompanySubmissionsAdapter",
    "SECFilingDocumentsAdapter",
    "captured_filing_accessions",
    "rebind_existing_filing_documents",
]
