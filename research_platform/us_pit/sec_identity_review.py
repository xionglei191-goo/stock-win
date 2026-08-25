from __future__ import annotations

import gzip
import html
import json
import re
import shutil
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

import pandas as pd

from .hashing import canonical_json_bytes, sha256_file, sha256_json
from .models import LicenseClass, SourceDependency, SourceRole
from .official_normalize import _cusip_valid, _isin_valid
from .sources_official import _require_sec_user_agent
from .store import SourceBatch, USPITStore


SEC_IDENTITY_REVIEW_VERSION = "us-pit-sec-identity-crosscheck-v1"
SEC_IDENTITY_SOURCE_ID = "sec_filed_security_identity"
SEC_IDENTITY_SOURCE_VERSION = "sec-efts-filed-identity-v1"
SEC_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_ARCHIVE_ROOT = "https://www.sec.gov/Archives/edgar/data"

_TickerTransport = Callable[[str, str], tuple[bytes, str]]


@dataclass(frozen=True)
class SECIdentityReviewResult:
    path: Path
    manifest: Mapping[str, Any]
    source_batch: SourceBatch | None


def _default_transport(url: str, user_agent: str) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json,text/html,application/xhtml+xml,application/xml",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    payload = gzip.decompress(payload)
                media_type = response.headers.get_content_type()
                return payload, media_type
        except Exception as exc:  # pragma: no cover - exercised by live SEC only
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _plain_text(payload: bytes) -> str:
    value = payload.decode("utf-8", errors="replace")
    value = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _name_tokens(value: str) -> tuple[str, ...]:
    ignored = {
        "INC",
        "INCORPORATED",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "PLC",
        "LTD",
        "LIMITED",
        "HOLDINGS",
        "THE",
    }
    return tuple(
        token
        for token in re.findall(r"[A-Z0-9]+", value.upper())
        if len(token) >= 3 and token not in ignored
    )


def _ticker_present(value: str, ticker: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(ticker.upper())}(?![A-Z0-9])", value.upper()) is not None


def _identity_window(
    payload: bytes,
    *,
    ticker: str,
    issuer_name: str,
    expected_identifier: str | None,
) -> dict[str, str] | None:
    text = _plain_text(payload)
    upper = text.upper()
    identifiers: list[tuple[str, str]] = []
    if expected_identifier:
        identifiers.append((
            "ISIN" if _isin_valid(expected_identifier) else "CUSIP",
            expected_identifier,
        ))
    else:
        identifiers.extend(("ISIN", match) for match in re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", upper))
        identifiers.extend(("CUSIP", match) for match in re.findall(r"\b[0-9A-Z*@#]{9}\b", upper))

    tokens = _name_tokens(issuer_name)
    required_name_hits = min(2, len(tokens))
    for identifier_type, identifier in identifiers:
        if identifier_type == "ISIN" and not _isin_valid(identifier):
            continue
        if identifier_type == "CUSIP" and not _cusip_valid(identifier):
            continue
        for match in re.finditer(re.escape(identifier), upper):
            start = max(0, match.start() - 1_500)
            end = min(len(text), match.end() + 1_500)
            window = text[start:end]
            window_upper = window.upper()
            if not _ticker_present(window, ticker):
                continue
            if required_name_hits and sum(token in window_upper for token in tokens) < required_name_hits:
                continue
            isin_matches = [item for item in re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", window_upper) if _isin_valid(item)]
            cusip_matches = [item for item in re.findall(r"\b[0-9A-Z*@#]{9}\b", window_upper) if _cusip_valid(item)]
            isin = next((item for item in isin_matches if expected_identifier in {None, item}), "")
            cusip = next((item for item in cusip_matches if expected_identifier in {None, item}), "")
            if identifier_type == "ISIN":
                isin = identifier
                if isin.startswith("US") and _cusip_valid(isin[2:11]):
                    cusip = isin[2:11]
            else:
                cusip = identifier
                linked = next((item for item in isin_matches if item.startswith("US" + cusip)), "")
                if linked:
                    isin = linked
            return {
                "identifier_type": identifier_type,
                "identifier_value": identifier,
                "cusip": cusip,
                "isin": isin,
                "evidence_excerpt": window[:3_000],
            }
    return None


def _nport_equity_identity_record(
    payload: bytes,
    *,
    ticker: str,
    issuer_name: str,
) -> dict[str, str] | None:
    text = _plain_text(payload)
    marker = "Item C.1. Identification of investment."
    sections = text.split(marker)
    if len(sections) == 1:
        return None
    tokens = _name_tokens(issuer_name)
    required_name_hits = min(2, len(tokens))
    for section in sections[1:]:
        record = section.split("Item C.2.", 1)[0]
        upper = record.upper()
        if not _ticker_present(record, ticker):
            continue
        if required_name_hits and sum(token in upper for token in tokens) < required_name_hits:
            continue
        if not any(label in upper for label in ("COMMON STOCK", "COMMON SHARES", "ORDINARY SHARES")):
            continue
        isins = [item for item in re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", upper) if _isin_valid(item)]
        cusips = [
            item
            for item in re.findall(r"\b[0-9A-Z*@#]{9}\b", upper)
            if _cusip_valid(item) and item != "000000000"
        ]
        isin = isins[0] if isins else ""
        cusip = cusips[0] if cusips else ""
        if isin.startswith("US") and _cusip_valid(isin[2:11]):
            cusip = isin[2:11]
        if not isin and not cusip:
            continue
        return {
            "identifier_type": "ISIN" if isin else "CUSIP",
            "identifier_value": isin or cusip,
            "cusip": cusip,
            "isin": isin,
            "evidence_excerpt": record[:3_000],
        }
    return None


def _security_id(cusip: str, isin: str) -> str:
    if isin:
        return "us_isin_" + isin.lower()
    if cusip:
        return "us_cusip_" + cusip.lower()
    return ""


def _archive_urls(hit: Mapping[str, Any]) -> tuple[str, ...]:
    source = hit.get("_source")
    if not isinstance(source, Mapping):
        return ()
    accession = str(source.get("adsh") or "")
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        return ()
    hit_id = str(hit.get("_id") or "")
    if ":" not in hit_id:
        return ()
    filename = hit_id.split(":", 1)[1]
    if not filename or "/" in filename or "\\" in filename:
        return ()
    ciks = source.get("ciks")
    if not isinstance(ciks, list):
        return ()
    accession_path = accession.replace("-", "")
    return tuple(
        f"{SEC_ARCHIVE_ROOT}/{str(cik).lstrip('0') or '0'}/{accession_path}/{filename}"
        for cik in ciks
        if str(cik).strip()
    )


class SECIdentityReviewService:
    """Capture late SEC filings that cross-check ticker-to-stable-ID identity only."""

    def __init__(
        self,
        store: USPITStore | Path | str,
        *,
        user_agent: str | None = None,
        transport: _TickerTransport | None = None,
        throttle_seconds: float = 0.11,
    ) -> None:
        self.store = store if isinstance(store, USPITStore) else USPITStore(store)
        self.user_agent = _require_sec_user_agent(user_agent)
        self.transport = transport or _default_transport
        self.throttle_seconds = max(0.0, float(throttle_seconds))

    def _fetch(self, url: str) -> tuple[bytes, str]:
        try:
            return self.transport(url, self.user_agent)
        finally:
            if self.throttle_seconds:
                time.sleep(self.throttle_seconds)

    def review(
        self,
        unresolved_membership_events: Path | str,
        candidate_events: Path | str,
        identity_candidates: Path | str,
        output_dir: Path | str,
        *,
        reviewer: str = "codex-sec-identity-review",
        reviewed_identity_seeds: Path | str | None = None,
    ) -> SECIdentityReviewResult:
        unresolved_path = Path(unresolved_membership_events).resolve(strict=True)
        candidate_path = Path(candidate_events).resolve(strict=True)
        identity_path = Path(identity_candidates).resolve(strict=True)
        unresolved = pd.read_parquet(unresolved_path)
        candidates = pd.read_parquet(candidate_path).set_index("event_candidate_id", drop=False)
        identities = pd.read_parquet(identity_path).copy()
        identities["security_id"] = identities["identity_candidate_key"].fillna("").map(
            lambda value: "us_" + str(value).replace(":", "_").lower() if value else ""
        )
        required = {"event_id", "security_id", "ticker_at_announcement", "review_reasons"}
        if required - set(unresolved.columns):
            raise ValueError("unresolved membership review has the wrong schema")
        if unresolved["event_id"].duplicated().any():
            raise ValueError("unresolved membership review contains duplicate event IDs")
        if reviewer.strip() == "":
            raise ValueError("reviewer is required")
        seed_path = None if reviewed_identity_seeds is None else Path(reviewed_identity_seeds).resolve(strict=True)
        seeds: dict[str, dict[str, str]] = {}
        if seed_path is not None:
            document = json.loads(seed_path.read_text(encoding="utf-8"))
            items = document.get("identity_seeds") if isinstance(document, Mapping) else None
            if not isinstance(items, list):
                raise ValueError("reviewed identity seed file has no identity_seeds list")
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError("reviewed identity seed row must be an object")
                event_id = str(item.get("event_id") or "")
                identifier = str(item.get("identifier") or "").upper()
                source_url = str(item.get("review_source_url") or "")
                if not event_id or event_id in seeds:
                    raise ValueError("reviewed identity seed event IDs must be unique and non-empty")
                if not (_cusip_valid(identifier) or _isin_valid(identifier)):
                    raise ValueError(f"reviewed identity seed has an invalid identifier: {event_id}")
                if not source_url.startswith("https://www.sec.gov/"):
                    raise ValueError(f"reviewed identity seed must cite an SEC URL: {event_id}")
                seeds[event_id] = {
                    "identifier": identifier,
                    "review_source_url": source_url,
                    "review_note": str(item.get("review_note") or ""),
                }

        observed_at = datetime.now(timezone.utc).isoformat()
        dependencies: list[SourceDependency] = []
        rows: list[dict[str, Any]] = []
        cached: dict[tuple[str, str, str], tuple[dict[str, Any] | None, list[SourceDependency]]] = {}
        for event in unresolved.sort_values(["effective_at", "event_id"]).to_dict(orient="records"):
            event_id = str(event["event_id"])
            if event_id not in candidates.index:
                raise ValueError(f"membership event is absent from candidate package: {event_id}")
            candidate = candidates.loc[event_id]
            if isinstance(candidate, pd.DataFrame):
                raise ValueError(f"membership event candidate is ambiguous: {event_id}")
            ticker = str(event["ticker_at_announcement"]).strip().upper()
            issuer_name = str(candidate["company_name"]).strip()
            expected_security_id = str(event.get("security_id") or "").strip()
            expected_identifier = ""
            if expected_security_id:
                matching = identities.loc[identities["security_id"].eq(expected_security_id)]
                if not matching.empty:
                    cusips = [str(value).strip().upper() for value in matching["cusip"].dropna() if _cusip_valid(str(value).strip().upper())]
                    isins = [str(value).strip().upper() for value in matching["isin"].dropna() if _isin_valid(str(value).strip().upper())]
                    expected_identifier = cusips[0] if cusips else (isins[0] if isins else "")
            seed = seeds.get(event_id)
            if seed is not None:
                if expected_identifier and expected_identifier != seed["identifier"]:
                    raise ValueError(f"reviewed seed conflicts with directional identity: {event_id}")
                expected_identifier = seed["identifier"]
            query_identity = expected_identifier or issuer_name
            cache_key = (query_identity, ticker, issuer_name)
            if cache_key not in cached:
                cached[cache_key] = self._find_identity(
                    query_identity=query_identity,
                    ticker=ticker,
                    issuer_name=issuer_name,
                    expected_identifier=expected_identifier or None,
                    observed_at=observed_at,
                )
            match, match_dependencies = cached[cache_key]
            dependencies.extend(match_dependencies)
            base = {
                "event_id": event_id,
                "ticker": ticker,
                "issuer_name": issuer_name,
                "expected_security_id": expected_security_id,
                "expected_identifier": expected_identifier,
                "review_seed_identifier": "" if seed is None else seed["identifier"],
                "review_seed_source_url": "" if seed is None else seed["review_source_url"],
                "review_seed_note": "" if seed is None else seed["review_note"],
                "reviewer": reviewer.strip(),
                "reviewed_at": observed_at,
            }
            if match is None:
                rows.append({
                    **base,
                    "review_outcome": "BLOCKED",
                    "review_reason": "NO_EXACT_SEC_FILED_TICKER_IDENTIFIER_RECORD",
                    "resolved_security_id": "",
                    "identifier_type": "",
                    "identifier_value": "",
                    "cusip": "",
                    "isin": "",
                    "source_url": "",
                    "evidence_sha256": "",
                    "filing_date": "",
                    "accession_number": "",
                    "evidence_excerpt": "",
                })
                continue
            # A directional anchor may use an ISIN stable ID while the SEC row
            # exposes only its embedded CUSIP. The exact expected identifier was
            # already required in the same local filing record, so preserve that
            # stronger stable ID instead of downgrading it to a CUSIP-derived ID.
            resolved_security_id = expected_security_id or _security_id(match["cusip"], match["isin"])
            outcome = "RESOLVED"
            reason = ""
            rows.append({
                **base,
                "review_outcome": outcome,
                "review_reason": reason,
                "resolved_security_id": resolved_security_id,
                **match,
            })

        frame = pd.DataFrame(rows)
        unique_dependencies = {
            (item.source_id, item.object_sha256, item.url): item for item in dependencies
        }
        source_batch = (
            self.store.write_source_batch(unique_dependencies.values())
            if unique_dependencies
            else None
        )
        return self._publish(
            frame,
            unresolved_path,
            candidate_path,
            identity_path,
            output_dir,
            reviewer=reviewer.strip(),
            source_batch=source_batch,
            seed_path=seed_path,
        )

    def _find_identity(
        self,
        *,
        query_identity: str,
        ticker: str,
        issuer_name: str,
        expected_identifier: str | None,
        observed_at: str,
    ) -> tuple[dict[str, Any] | None, list[SourceDependency]]:
        dependencies: list[SourceDependency] = []
        # Deriving a new stable ID requires a structured N-PORT holding record.
        # N-PX and 13F tables are useful cross-checks only when the expected
        # identifier is already known from a directional source.
        forms = ("NPORT-P",) if expected_identifier is None else ("NPORT-P", "N-PX", "13F-HR")
        for form in forms:
            query = f"{query_identity} {ticker}"
            search_url = SEC_EFTS_URL + "?" + urllib.parse.urlencode(
                {"q": query, "dateRange": "all", "forms": form}
            )
            payload, media_type = self._fetch(search_url)
            search_object = self.store.put_bytes(payload, media_type=media_type)
            dependencies.append(SourceDependency(
                source_id="sec_efts_identity_search",
                source_version=SEC_IDENTITY_SOURCE_VERSION,
                role=SourceRole.REFERENCE_ONLY,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=search_object.sha256,
                observed_at=observed_at,
                url=search_url,
                dataset="security_identity_search_index",
                metadata={"query": query, "form": form, "raw_frozen": True},
            ))
            document = json.loads(payload)
            hits = document.get("hits", {}).get("hits", [])
            if not isinstance(hits, list):
                raise ValueError("SEC EFTS response has no hits list")
            for hit in hits[:20]:
                for source_url in _archive_urls(hit):
                    try:
                        source_payload, source_media_type = self._fetch(source_url)
                    except Exception:
                        continue
                    match = (
                        _nport_equity_identity_record(
                            source_payload,
                            ticker=ticker,
                            issuer_name=issuer_name,
                        )
                        if expected_identifier is None
                        else _identity_window(
                            source_payload,
                            ticker=ticker,
                            issuer_name=issuer_name,
                            expected_identifier=expected_identifier,
                        )
                    )
                    if match is None:
                        continue
                    source = hit.get("_source", {})
                    source_object = self.store.put_bytes(source_payload, media_type=source_media_type)
                    accession = str(source.get("adsh") or "")
                    filing_date = str(source.get("file_date") or "")
                    dependencies.append(SourceDependency(
                        source_id=SEC_IDENTITY_SOURCE_ID,
                        source_version=SEC_IDENTITY_SOURCE_VERSION,
                        role=SourceRole.VALIDATION_ANCHOR,
                        license_class=LicenseClass.OFFICIAL_PUBLIC,
                        object_sha256=source_object.sha256,
                        observed_at=observed_at,
                        url=source_url,
                        dataset="security_identity_crosscheck",
                        as_of_date=filing_date or None,
                        metadata={
                            "artifact_kind": "raw_sec_filed_identity_record",
                            "form": form,
                            "accession_number": accession,
                            "file_date": filing_date,
                            "ticker": ticker,
                            "issuer_name": issuer_name,
                            "identifier_type": match["identifier_type"],
                            "identifier_value": match["identifier_value"],
                            "cusip": match["cusip"],
                            "isin": match["isin"],
                            "raw_frozen": True,
                            "response_sha256": source_object.sha256,
                            "eligible_for_historical_signal": False,
                            "identity_crosscheck_only": True,
                        },
                    ))
                    return ({
                        **match,
                        "source_url": source_url,
                        "evidence_sha256": source_object.sha256,
                        "filing_date": filing_date,
                        "accession_number": accession,
                    }, dependencies)
        return None, dependencies

    def _publish(
        self,
        frame: pd.DataFrame,
        unresolved_path: Path,
        candidate_path: Path,
        identity_path: Path,
        output_dir: Path | str,
        *,
        reviewer: str,
        source_batch: SourceBatch | None,
        seed_path: Path | None,
    ) -> SECIdentityReviewResult:
        output = Path(output_dir).resolve()
        if output.exists():
            raise FileExistsError(f"SEC identity review already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = output.parent / f".{output.name}.{uuid4().hex}.staging"
        stage.mkdir()
        try:
            artifact = stage / "sec_identity_crosschecks.parquet"
            frame.to_parquet(artifact, index=False)
            resolved = int(frame["review_outcome"].eq("RESOLVED").sum())
            manifest = {
                "format_version": SEC_IDENTITY_REVIEW_VERSION,
                "reviewer": reviewer,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "input_sha256": {
                    "unresolved_membership_events": sha256_file(unresolved_path),
                    "membership_event_candidates": sha256_file(candidate_path),
                    "security_identity_candidates": sha256_file(identity_path),
                    "reviewed_identity_seeds": None if seed_path is None else sha256_file(seed_path),
                },
                "event_count": int(len(frame)),
                "resolved_count": resolved,
                "blocked_count": int(len(frame) - resolved),
                "source_batch_id": None if source_batch is None else source_batch.batch_id,
                "status": "REVIEWED" if resolved == len(frame) else "DATA_BLOCKED",
                "candidate_only": True,
                "direct_build_allowed": False,
                "policy": {
                    "sec_filing_is_identity_crosscheck_only": True,
                    "late_filing_never_backdates_signal_availability": True,
                    "ticker_and_identifier_share_local_record_window": True,
                    "raw_search_and_filing_objects_frozen": True,
                    "reviewed_seed_never_unlocks_without_sec_filing_match": True,
                },
                "artifact": {
                    "filename": artifact.name,
                    "row_count": int(len(frame)),
                    "sha256": sha256_file(artifact),
                },
            }
            manifest["review_id"] = sha256_json(manifest)
            (stage / "manifest.json").write_bytes(canonical_json_bytes(manifest))
            stage.replace(output)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return SECIdentityReviewResult(output, manifest, source_batch)


__all__ = [
    "SECIdentityReviewResult",
    "SECIdentityReviewService",
]
