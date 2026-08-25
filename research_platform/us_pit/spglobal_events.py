from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import exchange_calendars as xcals

from .hashing import canonical_json_bytes, sha256_file, sha256_json
from .identity_bridge import normalized_issuer_name
from .models import LicenseClass, SourceDependency, SourceRole, UNIVERSE_ID
from .sources_spglobal import (
    SPGLOBAL_EVENT_SOURCE_ID,
    _archive_links,
    parse_sp500_membership_announcement,
)
from .store import SourceBatch, USPITStore


SPGLOBAL_EVENT_CANDIDATE_VERSION = "us-pit-spglobal-event-candidates-v1"
SPGLOBAL_EVENT_REVIEW_VERSION = "us-pit-spglobal-event-review-v1"
SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION = "us-pit-spglobal-event-evidence-review-v1"
SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION_V2 = "us-pit-spglobal-event-evidence-review-v2"
SEC_IDENTITY_CROSSCHECK_VERSION = "us-pit-sec-identity-crosscheck-v1"
SPGLOBAL_EVENT_REPARSE_VERSION = "spglobal-table-narrative-parser-v4"


@dataclass(frozen=True)
class SPGlobalEventCandidateResult:
    path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class SPGlobalEventReviewResult:
    path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class SPGlobalEventEvidenceReviewResult:
    path: Path
    manifest: dict[str, Any]


def reparse_spglobal_event_probes(
    store: USPITStore,
    source_batch_ids: Iterable[str],
    *,
    start_date: date,
    end_date: date,
) -> SourceBatch:
    """Derive event dependencies from immutable, previously captured probes.

    The returned batch contains only derived ``membership_events`` dependencies.
    Callers must retain the original capture batch alongside it so archive and
    probe completeness remain independently auditable.
    """

    if end_date < start_date:
        raise ValueError("S&P event reparse end date precedes start date")
    batch_ids = tuple(sorted(set(str(item).strip() for item in source_batch_ids)))
    if not batch_ids or any(not item for item in batch_ids):
        raise ValueError("at least one S&P event source batch is required")
    dependencies = []
    for batch_id in batch_ids:
        dependencies.extend(store.load_source_batch(batch_id).dependencies)
    probes = {
        (item.url, item.object_sha256): item
        for item in dependencies
        if item.source_id == SPGLOBAL_EVENT_SOURCE_ID
        and item.dataset == "membership_event_probe"
        and dict(item.metadata).get("artifact_kind")
        == "raw_spglobal_sp500_candidate_announcement"
    }
    if not probes:
        raise ValueError("source batches contain no frozen S&P event probes")

    derived: list[SourceDependency] = []
    for item in probes.values():
        object_path = store.object_path(item.object_sha256)
        if not object_path.is_file() or sha256_file(object_path) != item.object_sha256:
            raise ValueError("frozen S&P event probe is missing or corrupt")
        metadata = dict(item.metadata)
        if metadata.get("raw_frozen") is not True:
            raise ValueError("S&P event probe is not marked as immutable raw evidence")
        if str(metadata.get("response_sha256") or "") != item.object_sha256:
            raise ValueError("S&P event probe response hash is inconsistent")
        try:
            parsed_events = parse_sp500_membership_announcement(object_path.read_bytes())
        except Exception as exc:
            raise ValueError(
                "frozen S&P event probe cannot be deterministically reparsed: "
                f"url={item.url}; sha256={item.object_sha256}; error={exc}"
            ) from exc
        events = tuple(
            event
            for event in parsed_events
            if start_date <= event.effective_date <= end_date
        )
        if not events:
            continue
        announced_at = events[0].announced_at
        if any(event.announced_at != announced_at for event in events):
            raise ValueError("one S&P probe has inconsistent publication timestamps")
        event_rows = [
            {
                "effective_date": event.effective_date.isoformat(),
                "event_type": event.event_type,
                "ticker": event.ticker,
                "company_name": event.company_name,
            }
            for event in events
        ]
        derived.append(
            SourceDependency(
                source_id=SPGLOBAL_EVENT_SOURCE_ID,
                source_version=SPGLOBAL_EVENT_REPARSE_VERSION,
                role=SourceRole.SIGNAL_INPUT,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=item.object_sha256,
                observed_at=item.observed_at,
                url=item.url,
                dataset="membership_events",
                as_of_date=max(event.effective_date for event in events).isoformat(),
                published_at=announced_at.isoformat(),
                metadata={
                    "artifact_kind": "raw_spglobal_sp500_membership_announcement",
                    "universe_id": UNIVERSE_ID,
                    "event_count": len(events),
                    "effective_start_date": min(
                        event.effective_date for event in events
                    ).isoformat(),
                    "effective_end_date": max(
                        event.effective_date for event in events
                    ).isoformat(),
                    "event_rows_sha256": sha256_json(event_rows),
                    "raw_frozen": True,
                    "publication_time_from_payload": True,
                    "eligible_for_historical_signal": True,
                    "response_sha256": item.object_sha256,
                    "derived_from_dataset": "membership_event_probe",
                    "parser_revision": SPGLOBAL_EVENT_REPARSE_VERSION,
                    "source_batch_ids": list(batch_ids),
                },
            )
        )
    if not derived:
        raise ValueError("frozen S&P probes yielded no explicit S&P 500 events")
    return store.write_source_batch(derived)


def _identity_suggestions(
    normalization_dir: Path,
    *,
    ticker: str,
    company_name: str,
    effective_date: date,
    event_type: str,
) -> tuple[pd.DataFrame, str]:
    path = normalization_dir / "security_identity_candidates.parquet"
    if not path.is_file():
        raise ValueError("official normalization identity candidates are missing")
    identities = pd.read_parquet(path)
    required = {
        "source_id", "ticker", "as_of_date", "identity_candidate_key",
        "isin", "cusip", "content_sha256", "source_row_number",
    }
    if not required.issubset(identities.columns):
        raise ValueError("official normalization identity schema is incomplete")
    values = identities.loc[
        identities["source_id"].astype(str).eq("ishares_ivv_holdings_api")
        & identities["ticker"].astype(str).str.upper().eq(ticker)
        & identities["identity_candidate_key"].notna()
    ].copy()
    values["_as_of"] = pd.to_datetime(values["as_of_date"], errors="coerce")
    values = values.loc[values["_as_of"].notna()].copy()
    target = pd.Timestamp(effective_date)
    if event_type == "ADD":
        directional = values.loc[values["_as_of"] >= target].copy()
        if not directional.empty:
            chosen_date = directional["_as_of"].min()
            values = directional.loc[directional["_as_of"].eq(chosen_date)]
            basis = "FIRST_OFFICIAL_MONTH_END_AT_OR_AFTER_ADD"
        else:
            basis = "NO_DIRECTIONAL_OFFICIAL_IDENTITY"
            values = values.iloc[0:0]
    else:
        directional = values.loc[values["_as_of"] < target].copy()
        if not directional.empty:
            chosen_date = directional["_as_of"].max()
            values = directional.loc[directional["_as_of"].eq(chosen_date)]
            basis = "LAST_OFFICIAL_MONTH_END_BEFORE_REMOVE"
        else:
            basis = "NO_DIRECTIONAL_OFFICIAL_IDENTITY"
            values = values.iloc[0:0]
    if not values.empty:
        distance = abs((values["_as_of"].iloc[0] - target).days)
        if distance > 45:
            values = values.iloc[0:0]
            basis = "DIRECTIONAL_IDENTITY_OUTSIDE_45_DAY_WINDOW"
    if not values.empty:
        return values, basis

    company_key = normalized_issuer_name(company_name)
    issuer = identities.get("issuer_name", pd.Series("", index=identities.index))
    title = identities.get("title", pd.Series("", index=identities.index))
    sec_values = identities.loc[
        identities["source_id"].astype(str).eq("sec_nport_ivv")
        & identities["identity_candidate_key"].notna()
        & (
            issuer.map(normalized_issuer_name).eq(company_key)
            | title.map(normalized_issuer_name).eq(company_key)
        )
    ].copy()
    sec_values["_as_of"] = pd.to_datetime(sec_values["as_of_date"], errors="coerce")
    sec_values = sec_values.loc[sec_values["_as_of"].notna()].copy()
    target = pd.Timestamp(effective_date)
    if event_type == "ADD":
        sec_values = sec_values.loc[sec_values["_as_of"] >= target]
        if not sec_values.empty:
            sec_values = sec_values.loc[
                sec_values["_as_of"].eq(sec_values["_as_of"].min())
            ]
    else:
        sec_values = sec_values.loc[sec_values["_as_of"] < target]
        if not sec_values.empty:
            sec_values = sec_values.loc[
                sec_values["_as_of"].eq(sec_values["_as_of"].max())
            ]
    if not sec_values.empty and abs((sec_values["_as_of"].iloc[0] - target).days) <= 120:
        return sec_values, "EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR"
    return values, basis


def build_spglobal_event_candidates(
    store: USPITStore,
    source_batch_ids: Iterable[str],
    normalization_dir: Path | str,
    output_dir: Path | str,
) -> SPGlobalEventCandidateResult:
    normalization = Path(normalization_dir).resolve()
    normalization_manifest_path = normalization / "manifest.json"
    if not normalization_manifest_path.is_file():
        raise ValueError("official normalization manifest is missing")
    normalization_manifest = json.loads(
        normalization_manifest_path.read_text(encoding="utf-8")
    )
    if normalization.name != str(normalization_manifest.get("normalization_id") or ""):
        raise ValueError("official normalization directory identity mismatch")

    batch_ids = tuple(sorted(set(str(item).strip() for item in source_batch_ids)))
    if not batch_ids or any(not item for item in batch_ids):
        raise ValueError("at least one S&P event source batch is required")
    dependencies = []
    for batch_id in batch_ids:
        dependencies.extend(store.load_source_batch(batch_id).dependencies)
    selected = [
        item
        for item in dependencies
        if item.source_id == SPGLOBAL_EVENT_SOURCE_ID
        and item.dataset == "membership_events"
        and str(dict(item.metadata).get("artifact_kind"))
        == "raw_spglobal_sp500_membership_announcement"
    ]
    if not selected:
        raise ValueError("source batches contain no official S&P 500 event announcements")

    # When the same announcement was parsed under multiple parser revisions,
    # only the highest revision's dependency is eligible for candidate build;
    # older revisions stay frozen in their original batches but never participate.
    def _revision_rank(item: SourceDependency) -> int:
        match = re.search(
            r"v(\d+)$", str(dict(item.metadata).get("parser_revision") or "")
        )
        return int(match.group(1)) if match else 0

    latest_by_url: dict[str, SourceDependency] = {}
    for item in selected:
        prior = latest_by_url.get(item.url)
        if prior is None or _revision_rank(item) > _revision_rank(prior):
            latest_by_url[item.url] = item
    selected = list(latest_by_url.values())

    archive_pages = [
        item
        for item in dependencies
        if item.source_id == SPGLOBAL_EVENT_SOURCE_ID
        and item.dataset == "membership_event_index"
        and dict(item.metadata).get("artifact_kind")
        == "raw_spglobal_press_archive_page"
    ]
    probes = [
        item
        for item in dependencies
        if item.source_id == SPGLOBAL_EVENT_SOURCE_ID
        and item.dataset == "membership_event_probe"
        and dict(item.metadata).get("artifact_kind")
        == "raw_spglobal_sp500_candidate_announcement"
    ]
    if not archive_pages or not probes:
        raise ValueError("S&P event source batch lacks complete archive/probe evidence")
    archive_urls: set[str] = set()
    years: dict[int, list[tuple[int, bool]]] = {}
    for item in archive_pages:
        metadata = dict(item.metadata)
        year = int(metadata.get("archive_year", 0))
        offset = int(metadata.get("archive_offset", -1))
        terminal = metadata.get("terminal_archive_page") is True
        page_size = int(metadata.get("archive_page_size", 0))
        link_count = int(metadata.get("archive_link_count", -1))
        if page_size != 100 or offset < 0 or offset % page_size or link_count < 0:
            raise ValueError("S&P archive pagination metadata is invalid")
        object_path = store.object_path(item.object_sha256)
        if not object_path.is_file() or sha256_file(object_path) != item.object_sha256:
            raise ValueError("S&P archive CAS object is missing or corrupt")
        discovered = set(_archive_links(object_path.read_bytes(), year=year))
        if len(discovered) != int(metadata.get("candidate_link_count", -1)):
            raise ValueError("S&P archive candidate link count is not reproducible")
        archive_urls.update(discovered)
        years.setdefault(year, []).append((offset, terminal))
    for year, pages in years.items():
        ordered = sorted(pages)
        expected = list(range(0, ordered[-1][0] + 100, 100))
        if [item[0] for item in ordered] != expected or sum(item[1] for item in ordered) != 1:
            raise ValueError(f"S&P archive pagination is incomplete for {year}")
        if ordered[-1][1] is not True:
            raise ValueError(f"S&P archive terminal page is missing for {year}")
    probe_urls = {item.url for item in probes}
    if archive_urls != probe_urls:
        raise ValueError("S&P archive candidate URLs and frozen probes disagree")
    selected_urls = {item.url for item in selected}
    if not selected_urls.issubset(probe_urls):
        raise ValueError("S&P event announcement lacks a frozen archive probe")

    rows: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], tuple[str, str]] = {}
    def dependency_priority(item: SourceDependency) -> tuple[str, str, int, str]:
        metadata = dict(item.metadata)
        parser_revision = str(metadata.get("parser_revision") or "")
        match = re.search(r"v(\d+)$", parser_revision)
        revision_rank = -int(match.group(1)) if match else 0
        return (
            item.published_at or "",
            item.url,
            revision_rank,
            item.source_version,
        )

    for dependency in sorted(selected, key=dependency_priority):
        if (
            dependency.role != SourceRole.SIGNAL_INPUT
            or dependency.license_class != LicenseClass.OFFICIAL_PUBLIC
            or dependency.published_at is None
            or dict(dependency.metadata).get("response_sha256")
            != dependency.object_sha256
        ):
            raise ValueError("S&P event dependency has invalid evidence policy")
        object_path = store.object_path(dependency.object_sha256)
        if not object_path.is_file() or sha256_file(object_path) != dependency.object_sha256:
            raise ValueError("S&P event source CAS object is missing or corrupt")
        events = parse_sp500_membership_announcement(object_path.read_bytes())
        metadata = dict(dependency.metadata)
        effective_start = date.fromisoformat(str(metadata["effective_start_date"]))
        effective_end = date.fromisoformat(str(metadata["effective_end_date"]))
        events = tuple(
            item
            for item in events
            if effective_start <= item.effective_date <= effective_end
        )
        if len(events) != int(metadata.get("event_count") or 0):
            raise ValueError("S&P event count disagrees with the captured dependency")
        for event in events:
            if event.announced_at.isoformat() != dependency.published_at:
                raise ValueError("S&P event publication time disagrees with dependency")
            effective_at = datetime.combine(
                event.effective_date,
                time(9, 30),
                ZoneInfo("America/New_York"),
            )
            semantic_key = (
                event.event_type,
                event.ticker,
                effective_at.isoformat(),
            )
            prior = seen.get(semantic_key)
            if prior is not None:
                if prior[1].strip().casefold() != event.company_name.strip().casefold():
                    raise ValueError("S&P announcements conflict for the same semantic event")
                continue
            event_id = sha256_json(
                {
                    "source_sha256": dependency.object_sha256,
                    "event_type": event.event_type,
                    "ticker": event.ticker,
                    "effective_at": effective_at.isoformat(),
                }
            )
            seen[semantic_key] = (event_id, event.company_name)
            matches, basis = _identity_suggestions(
                normalization,
                ticker=event.ticker,
                company_name=event.company_name,
                effective_date=event.effective_date,
                event_type=event.event_type,
            )
            security_ids = sorted(
                set(matches["identity_candidate_key"].astype(str))
            ) if not matches.empty else []
            if len(security_ids) == 1:
                matched = matches.loc[
                    matches["identity_candidate_key"].astype(str).eq(security_ids[0])
                ].sort_values(["as_of_date", "source_row_number"]).iloc[-1]
                status = "REVIEW_REQUIRED"
                suggested = "us_" + security_ids[0].replace(":", "_").lower()
            elif len(security_ids) > 1:
                matched = None
                status = "AMBIGUOUS"
                suggested = ""
                basis = "MULTIPLE_STABLE_IDS_AT_DIRECTIONAL_SNAPSHOT"
            else:
                matched = None
                status = "UNRESOLVED"
                suggested = ""
            rows.append(
                {
                    "event_candidate_id": event_id,
                    "universe_id": UNIVERSE_ID,
                    "event_type": event.event_type,
                    "announced_at": event.announced_at.isoformat(),
                    "effective_at": effective_at.isoformat(),
                    "company_name": event.company_name,
                    "ticker_at_announcement": event.ticker,
                    "suggested_security_id": suggested,
                    "identity_match_basis": basis,
                    "identity_as_of_date": "" if matched is None else str(matched["as_of_date"]),
                    "identity_source_sha256": "" if matched is None else str(matched["content_sha256"]),
                    "identity_source_row_number": None if matched is None else int(matched["source_row_number"]),
                    "source_id": dependency.source_id,
                    "source_version": dependency.source_version,
                    "evidence_sha256": dependency.object_sha256,
                    "source_url": dependency.url,
                    "status": status,
                    "approved": False,
                    "review_note": "Verify stable identity/share class, then explicitly approve this event.",
                }
            )

    frame = pd.DataFrame(rows).sort_values(
        ["effective_at", "event_type", "ticker_at_announcement"], kind="stable"
    ).reset_index(drop=True)
    payload = {
        "format_version": SPGLOBAL_EVENT_CANDIDATE_VERSION,
        "candidate_only": True,
        "direct_build_allowed": False,
        "status": "REVIEW_REQUIRED" if not frame.empty else "DATA_BLOCKED",
        "source_batch_ids": list(batch_ids),
        "normalization_id": normalization.name,
        "normalization_manifest_sha256": sha256_file(normalization_manifest_path),
        "row_count": int(len(frame)),
        "matched": int(frame["status"].eq("REVIEW_REQUIRED").sum()),
        "ambiguous": int(frame["status"].eq("AMBIGUOUS").sum()),
        "unresolved": int(frame["status"].eq("UNRESOLVED").sum()),
        "policy": {
            "publication_time_from_official_payload": True,
            "ticker_is_not_stable_identity": True,
            "late_observed_holdings_used_only_for_identity_suggestion": True,
            "explicit_approval_required": True,
            "archive_pagination_replayed": True,
            "all_candidate_urls_frozen": True,
        },
        "archive_years": sorted(years),
        "archive_page_count": len(archive_pages),
        "candidate_probe_count": len(probes),
        "archive_candidate_url_sha256": sha256_json(sorted(archive_urls)),
    }
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"S&P event candidate output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        artifact_path = staging / "membership_event_candidates.parquet"
        frame.to_parquet(artifact_path, index=False)
        payload["artifact_sha256"] = sha256_file(artifact_path)
        payload["candidate_set_id"] = sha256_json(payload)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(payload))
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return SPGlobalEventCandidateResult(output, payload)


def prepare_spglobal_event_review(
    candidate_dir: Path | str,
    output_dir: Path | str,
) -> SPGlobalEventReviewResult:
    """Create an editable, fail-closed review file from frozen candidates.

    This intentionally does not approve any row. The reviewed-workspace
    assembler independently requires every retained row to be approved with a
    non-empty note before it can enter a release.
    """

    candidate_root = Path(candidate_dir).resolve()
    manifest_path = candidate_root / "manifest.json"
    artifact_path = candidate_root / "membership_event_candidates.parquet"
    if not manifest_path.is_file() or not artifact_path.is_file():
        raise ValueError("S&P event candidate package is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("format_version") != SPGLOBAL_EVENT_CANDIDATE_VERSION
        or manifest.get("candidate_only") is not True
        or manifest.get("direct_build_allowed") is not False
        or manifest.get("artifact_sha256") != sha256_file(artifact_path)
    ):
        raise ValueError("S&P event candidate package failed integrity checks")
    candidate_identity = dict(manifest)
    candidate_set_id = str(candidate_identity.pop("candidate_set_id", ""))
    if candidate_set_id != sha256_json(candidate_identity):
        raise ValueError("S&P event candidate identity is invalid")

    candidates = pd.read_parquet(artifact_path)
    required = {
        "event_candidate_id",
        "event_type",
        "announced_at",
        "effective_at",
        "ticker_at_announcement",
        "suggested_security_id",
        "source_id",
        "evidence_sha256",
        "source_url",
        "status",
    }
    if not required.issubset(candidates.columns):
        raise ValueError("S&P event candidate schema is incomplete")
    rows = []
    for row in candidates.to_dict(orient="records"):
        rows.append(
            {
                "event_id": str(row["event_candidate_id"]),
                "security_id": str(row.get("suggested_security_id") or ""),
                "event_type": str(row["event_type"]),
                "announced_at": str(row["announced_at"]),
                "effective_at": str(row["effective_at"]),
                "source_id": str(row["source_id"]),
                "evidence_sha256": str(row["evidence_sha256"]),
                "approved": False,
                "review_note": "",
                "candidate_status": str(row["status"]),
                "ticker_at_announcement": str(row["ticker_at_announcement"]),
                "source_url": str(row["source_url"]),
            }
        )
    frame = pd.DataFrame(rows)
    review_identity = {
        "format_version": SPGLOBAL_EVENT_REVIEW_VERSION,
        "candidate_set_id": candidate_set_id,
        "candidate_manifest_sha256": sha256_file(manifest_path),
        "candidate_artifact_sha256": sha256_file(artifact_path),
        "row_count": int(len(frame)),
        "approved_rows": 0,
        "status": "REVIEW_REQUIRED",
        "direct_build_allowed": False,
    }
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"S&P event review output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        review_path = staging / "membership_events.parquet"
        frame.to_parquet(review_path, index=False)
        review_identity["artifact_sha256"] = sha256_file(review_path)
        review_identity["review_template_id"] = sha256_json(review_identity)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(review_identity))
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return SPGlobalEventReviewResult(output, review_identity)


def _load_sec_filed_identity_crosscheck(
    store: USPITStore,
    identity_crosscheck_dir: Path | str,
) -> dict[str, Any]:
    """Validate and index a frozen SEC-filed identity crosscheck package.

    The package is a review-only diagnostic artifact: it never creates event
    timing, membership state, or signal availability.  It only binds an
    announcement ticker to a stable ISIN/CUSIP identifier through an official
    SEC filing document that is already frozen in the CAS.
    """

    root = Path(identity_crosscheck_dir).resolve()
    manifest_path = root / "manifest.json"
    artifact_path = root / "sec_identity_crosschecks.parquet"
    if not manifest_path.is_file() or not artifact_path.is_file():
        raise ValueError("SEC identity crosscheck package is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
    artifact = manifest.get("artifact") if isinstance(manifest.get("artifact"), dict) else {}
    if (
        manifest.get("format_version") != SEC_IDENTITY_CROSSCHECK_VERSION
        or artifact.get("sha256") != sha256_file(artifact_path)
        or int(artifact.get("row_count", -1)) < 0
        or policy.get("late_filing_never_backdates_signal_availability") is not True
        or policy.get("raw_search_and_filing_objects_frozen") is not True
        or not str(manifest.get("reviewer") or "").strip()
    ):
        raise ValueError(
            "SEC identity crosscheck package failed integrity or policy checks"
        )
    frame = pd.read_parquet(artifact_path)
    required = {
        "event_id",
        "ticker",
        "issuer_name",
        "review_outcome",
        "resolved_security_id",
        "identifier_type",
        "identifier_value",
        "evidence_sha256",
        "accession_number",
        "filing_date",
        "source_url",
    }
    if required - set(frame.columns):
        raise ValueError("SEC identity crosscheck schema is incomplete")
    if frame["event_id"].astype(str).duplicated().any():
        raise ValueError("SEC identity crosscheck event IDs are not unique")
    rows: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict(orient="records"):
        if str(row["review_outcome"]) != "RESOLVED":
            continue
        evidence_sha256 = str(row["evidence_sha256"] or "")
        evidence_object = store.object_path(evidence_sha256)
        if (
            not evidence_object.is_file()
            or sha256_file(evidence_object) != evidence_sha256
        ):
            raise ValueError(
                "SEC identity crosscheck filing evidence failed CAS replay: "
                f"{row['event_id']}"
            )
        accession_number = str(row["accession_number"] or "").strip()
        filing_date = str(row["filing_date"] or "").strip()
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession_number):
            raise ValueError(
                "SEC identity crosscheck has an invalid accession number: "
                f"{row['event_id']}"
            )
        try:
            date.fromisoformat(filing_date)
        except ValueError as exc:
            raise ValueError(
                "SEC identity crosscheck has an invalid filing date: "
                f"{row['event_id']}"
            ) from exc
        rows[str(row["event_id"])] = row
    return {
        "root": root,
        "manifest": manifest,
        "manifest_sha256": sha256_file(manifest_path),
        "artifact_sha256": sha256_file(artifact_path),
        "rows": rows,
    }


def _stable_key_from_security_id(security_id: str) -> str:
    """Map ``us_isin_us0378331005`` to the normalization key ``isin:US0378331005``."""

    parts = security_id.split("_", 2)
    if len(parts) != 3 or parts[1] not in {"isin", "cusip"}:
        return ""
    return f"{parts[1]}:{parts[2].upper()}"


def _sec_filed_crosscheck_approval(
    store: USPITStore,
    *,
    crosscheck_index: dict[str, Any],
    identities: pd.DataFrame,
    event_id: str,
    ticker_at_announcement: str,
    suggested_security_id: str,
    verified_dual_source_keys: set[str],
) -> dict[str, Any] | None:
    """Return approval provenance when the V2 dual-official-source rule holds.

    Rule: a name-matched directional anchor may be approved only when (a) an
    exact-event RESOLVED crosscheck row exists whose stable ID equals the
    candidate's suggested ID, (b) its SEC filing evidence replays byte-exact
    from the CAS (verified at load time), and (c) the same stable identifier
    appears in captured ``sec_nport_ivv`` holdings identity candidates with
    intact content-addressed lineage.  Two independent official sources must
    therefore agree on issuer-to-identifier before the S&P announcement's
    ticker binding can be admitted.
    """

    candidate_row = crosscheck_index["rows"].get(event_id)
    if candidate_row is None:
        return None
    resolved_security_id = str(candidate_row["resolved_security_id"] or "")
    if resolved_security_id != suggested_security_id:
        return None
    if (
        str(candidate_row["ticker"] or "").strip().upper()
        != ticker_at_announcement.strip().upper()
    ):
        return None
    stable_key = _stable_key_from_security_id(resolved_security_id)
    if not stable_key:
        return None
    if stable_key not in verified_dual_source_keys:
        matches = identities.loc[
            identities["identity_candidate_key"].fillna("").astype(str).eq(stable_key)
            & identities["source_id"].astype(str).eq("sec_nport_ivv")
        ]
        verified = False
        for match in matches.to_dict(orient="records"):
            lineage_object = store.object_path(str(match["content_sha256"] or ""))
            if lineage_object.is_file() and sha256_file(lineage_object) == str(
                match["content_sha256"]
            ):
                verified = True
                break
        if not verified:
            return None
        verified_dual_source_keys.add(stable_key)
    return {
        "crosscheck_evidence_sha256": str(candidate_row["evidence_sha256"]),
        "crosscheck_accession_number": str(candidate_row["accession_number"]),
        "crosscheck_filing_date": str(candidate_row["filing_date"]),
        "crosscheck_identifier_type": str(candidate_row["identifier_type"]),
        "crosscheck_stable_key": stable_key,
    }


def review_spglobal_event_evidence(
    store: USPITStore,
    source_batch_ids: Iterable[str],
    candidate_dir: Path | str,
    normalization_dir: Path | str,
    output_dir: Path | str,
    *,
    reviewer: str = "codex-evidence-review",
    reviewed_at: datetime | None = None,
    identity_crosscheck_dir: Path | str | None = None,
) -> SPGlobalEventEvidenceReviewResult:
    """Approve only candidates reproducible from exact official ticker evidence.

    Name-only identity suggestions remain unresolved. This is deliberate: a
    matching issuer name does not establish share class or ticker continuity.
    The output is immutable and keeps rejected candidates in a separate gap
    artifact so downstream assembly cannot mistake them for approved events.

    V2 (``identity_crosscheck_dir``): a name-matched directional anchor may
    additionally be approved when a frozen SEC-filed identity crosscheck
    package supplies an exact-event RESOLVED row whose stable ID equals the
    candidate's suggested ID, its filing evidence replays byte-exact from the
    CAS, and the same identifier appears in captured ``sec_nport_ivv``
    holdings identity lineage.  Two independent official sources must agree;
    the crosscheck admits identity binding only and never event timing.
    """

    reviewer_value = str(reviewer).strip()
    if not reviewer_value:
        raise ValueError("membership event reviewer is required")
    reviewed = reviewed_at or datetime.now(timezone.utc)
    if reviewed.tzinfo is None or reviewed.utcoffset() is None:
        raise ValueError("membership event reviewed_at must be timezone-aware")
    reviewed_iso = reviewed.astimezone(timezone.utc).isoformat()

    candidate_root = Path(candidate_dir).resolve()
    candidate_manifest_path = candidate_root / "manifest.json"
    candidate_artifact_path = candidate_root / "membership_event_candidates.parquet"
    if not candidate_manifest_path.is_file() or not candidate_artifact_path.is_file():
        raise ValueError("S&P event candidate package is incomplete")
    candidate_manifest = json.loads(
        candidate_manifest_path.read_text(encoding="utf-8")
    )
    if (
        candidate_manifest.get("format_version")
        != SPGLOBAL_EVENT_CANDIDATE_VERSION
        or candidate_manifest.get("candidate_only") is not True
        or candidate_manifest.get("direct_build_allowed") is not False
        or candidate_manifest.get("artifact_sha256")
        != sha256_file(candidate_artifact_path)
    ):
        raise ValueError("S&P event candidate package failed integrity checks")
    candidate_identity = dict(candidate_manifest)
    candidate_set_id = str(candidate_identity.pop("candidate_set_id", ""))
    if candidate_set_id != sha256_json(candidate_identity):
        raise ValueError("S&P event candidate identity is invalid")

    normalization = Path(normalization_dir).resolve()
    normalization_manifest_path = normalization / "manifest.json"
    identity_path = normalization / "security_identity_candidates.parquet"
    if not normalization_manifest_path.is_file() or not identity_path.is_file():
        raise ValueError("official normalization identity evidence is incomplete")
    normalization_manifest = json.loads(
        normalization_manifest_path.read_text(encoding="utf-8")
    )
    if (
        normalization.name != candidate_manifest.get("normalization_id")
        or sha256_file(normalization_manifest_path)
        != candidate_manifest.get("normalization_manifest_sha256")
    ):
        raise ValueError("candidate package and normalization evidence disagree")
    identity_descriptor = dict(
        dict(normalization_manifest.get("artifacts") or {}).get(
            "security_identity_candidates"
        )
        or {}
    )
    if identity_descriptor.get("object_sha256") != sha256_file(identity_path):
        raise ValueError("normalized identity artifact failed integrity checks")

    batch_ids = tuple(sorted(set(str(item).strip() for item in source_batch_ids)))
    if not batch_ids or any(not item for item in batch_ids):
        raise ValueError("at least one S&P event source batch is required")
    dependencies: list[SourceDependency] = []
    for batch_id in batch_ids:
        dependencies.extend(store.load_source_batch(batch_id).dependencies)
    dependency_index: dict[tuple[str, str, str], SourceDependency] = {}
    for dependency in dependencies:
        if dependency.dataset != "membership_events":
            continue
        key = (
            dependency.source_id,
            dependency.source_version,
            dependency.object_sha256,
        )
        if key in dependency_index:
            raise ValueError("duplicate S&P event source dependency")
        dependency_index[key] = dependency

    candidates = pd.read_parquet(candidate_artifact_path)
    identities = pd.read_parquet(identity_path)
    identity_crosscheck = (
        None
        if identity_crosscheck_dir is None
        else _load_sec_filed_identity_crosscheck(store, identity_crosscheck_dir)
    )
    verified_dual_source_keys: set[str] = set()
    crosscheck_approved_count = 0
    required_candidate_columns = {
        "event_candidate_id",
        "event_type",
        "announced_at",
        "effective_at",
        "company_name",
        "ticker_at_announcement",
        "suggested_security_id",
        "identity_match_basis",
        "identity_as_of_date",
        "identity_source_sha256",
        "identity_source_row_number",
        "source_id",
        "source_version",
        "evidence_sha256",
        "source_url",
        "status",
    }
    required_identity_columns = {
        "identity_candidate_key",
        "ticker",
        "share_class",
        "as_of_date",
        "source_id",
        "content_sha256",
        "source_row_number",
    }
    if not required_candidate_columns.issubset(candidates.columns):
        raise ValueError("S&P event candidate schema is incomplete")
    if not required_identity_columns.issubset(identities.columns):
        raise ValueError("normalized identity schema is incomplete")
    if candidates["event_candidate_id"].astype(str).duplicated().any():
        raise ValueError("S&P event candidate IDs are not unique")

    calendar = xcals.get_calendar("XNYS")
    parsed_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    approved_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []
    allowed_identity_bases = {
        "FIRST_OFFICIAL_MONTH_END_AT_OR_AFTER_ADD",
        "LAST_OFFICIAL_MONTH_END_BEFORE_REMOVE",
        "EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR",
    }

    for row in candidates.to_dict(orient="records"):
        reasons: list[str] = []
        dependency_key = (
            str(row["source_id"]),
            str(row["source_version"]),
            str(row["evidence_sha256"]),
        )
        dependency = dependency_index.get(dependency_key)
        parsed_event = None
        if dependency is None:
            reasons.append("SOURCE_DEPENDENCY_NOT_FOUND")
        else:
            metadata = dict(dependency.metadata)
            if (
                dependency.source_id != SPGLOBAL_EVENT_SOURCE_ID
                or dependency.role != SourceRole.SIGNAL_INPUT
                or dependency.license_class != LicenseClass.OFFICIAL_PUBLIC
                or dependency.published_at is None
                or metadata.get("response_sha256") != dependency.object_sha256
                or metadata.get("raw_frozen") is not True
                or dependency.url != str(row["source_url"])
            ):
                reasons.append("SOURCE_POLICY_INVALID")
            object_path = store.object_path(dependency.object_sha256)
            if (
                not object_path.is_file()
                or sha256_file(object_path) != dependency.object_sha256
            ):
                reasons.append("SOURCE_CAS_INVALID")
            else:
                if dependency_key not in parsed_cache:
                    effective_start = date.fromisoformat(
                        str(metadata["effective_start_date"])
                    )
                    effective_end = date.fromisoformat(
                        str(metadata["effective_end_date"])
                    )
                    parsed = tuple(
                        event
                        for event in parse_sp500_membership_announcement(
                            object_path.read_bytes()
                        )
                        if effective_start <= event.effective_date <= effective_end
                    )
                    if len(parsed) != int(metadata.get("event_count") or 0):
                        raise ValueError(
                            "S&P event count disagrees with captured dependency"
                        )
                    parsed_cache[dependency_key] = {
                        sha256_json(
                            {
                                "source_sha256": dependency.object_sha256,
                                "event_type": event.event_type,
                                "ticker": event.ticker,
                                "effective_at": datetime.combine(
                                    event.effective_date,
                                    time(9, 30),
                                    ZoneInfo("America/New_York"),
                                ).isoformat(),
                            }
                        ): event
                        for event in parsed
                    }
                parsed_event = parsed_cache[dependency_key].get(
                    str(row["event_candidate_id"])
                )
                if parsed_event is None:
                    reasons.append("EVENT_NOT_REPRODUCIBLE_FROM_SOURCE")
                else:
                    effective_at = datetime.fromisoformat(str(row["effective_at"]))
                    if (
                        parsed_event.event_type != str(row["event_type"])
                        or parsed_event.ticker != str(row["ticker_at_announcement"])
                        or parsed_event.company_name != str(row["company_name"])
                        or parsed_event.announced_at.isoformat()
                        != str(row["announced_at"])
                        or dependency.published_at != str(row["announced_at"])
                        or effective_at.date() != parsed_event.effective_date
                    ):
                        reasons.append("EVENT_FIELDS_DISAGREE_WITH_SOURCE")
                    if not calendar.is_session(pd.Timestamp(effective_at.date())):
                        reasons.append("EFFECTIVE_DATE_NOT_XNYS_SESSION")
                    if parsed_event.announced_at >= effective_at:
                        reasons.append("EVENT_NOT_PUBLIC_BEFORE_EFFECTIVE_OPEN")

        basis = str(row["identity_match_basis"])
        identity_record = None
        identity_crosscheck_hashes: tuple[str, ...] = ()
        if str(row["status"]) != "REVIEW_REQUIRED":
            reasons.append("CANDIDATE_IDENTITY_UNRESOLVED")
        elif basis not in allowed_identity_bases:
            reasons.append("IDENTITY_MATCH_BASIS_NOT_APPROVABLE")
        else:
            row_number = pd.to_numeric(
                pd.Series([row["identity_source_row_number"]]), errors="coerce"
            ).iloc[0]
            matches = identities.loc[
                identities["content_sha256"].astype(str).eq(
                    str(row["identity_source_sha256"])
                )
                & pd.to_numeric(
                    identities["source_row_number"], errors="coerce"
                ).eq(row_number)
            ]
            if len(matches) != 1:
                reasons.append("IDENTITY_LINEAGE_NOT_UNIQUE")
            else:
                identity_record = matches.iloc[0]
                key = str(identity_record["identity_candidate_key"] or "")
                expected_security_id = "us_" + key.replace(":", "_").lower()
                if expected_security_id != str(row["suggested_security_id"]):
                    reasons.append("STABLE_ID_DERIVATION_MISMATCH")
                if (
                    basis != "EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR"
                    and str(identity_record["ticker"]).strip().upper()
                    != str(row["ticker_at_announcement"]).strip().upper()
                ):
                    reasons.append("OFFICIAL_TICKER_IDENTITY_MISMATCH")
                identity_as_of = date.fromisoformat(str(identity_record["as_of_date"]))
                effective_date = datetime.fromisoformat(
                    str(row["effective_at"])
                ).date()
                if str(row["event_type"]) == "ADD":
                    direction_ok = identity_as_of >= effective_date
                else:
                    direction_ok = identity_as_of < effective_date
                maximum_distance = (
                    120
                    if basis == "EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR"
                    else 45
                )
                if (
                    not direction_ok
                    or abs((identity_as_of - effective_date).days) > maximum_distance
                ):
                    reasons.append("IDENTITY_SNAPSHOT_DIRECTION_OR_DISTANCE_INVALID")
                if basis == "EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR":
                    same_identity = identities.loc[
                        identities["identity_candidate_key"].astype(str).eq(key)
                        & identities["ticker"].fillna("").astype(str).str.strip().ne("")
                    ]
                    official_tickers = {
                        value.strip().upper()
                        for value in same_identity["ticker"].astype(str)
                        if value.strip()
                    }
                    if official_tickers != {
                        str(row["ticker_at_announcement"]).strip().upper()
                    }:
                        reasons.append(
                            "NAME_MATCH_LACKS_UNIQUE_OFFICIAL_TICKER_CROSSCHECK"
                        )
                    elif not same_identity["source_id"].astype(str).eq(
                        "ishares_ivv_holdings_api"
                    ).any():
                        reasons.append("OFFICIAL_TICKER_CROSSCHECK_SOURCE_INVALID")
                    else:
                        identity_crosscheck_hashes = tuple(
                            sorted(set(same_identity["content_sha256"].astype(str)))
                        )
                        for digest in identity_crosscheck_hashes:
                            crosscheck_object = store.object_path(digest)
                            if (
                                not crosscheck_object.is_file()
                                or sha256_file(crosscheck_object) != digest
                            ):
                                reasons.append("IDENTITY_CROSSCHECK_CAS_INVALID")
                else:
                    same_snapshot = identities.loc[
                        identities["as_of_date"].astype(str).eq(
                            str(identity_record["as_of_date"])
                        )
                        & identities["ticker"].astype(str).str.upper().eq(
                            str(identity_record["ticker"]).upper()
                        )
                        & identities["identity_candidate_key"].notna()
                    ]
                    if (
                        same_snapshot["identity_candidate_key"]
                        .astype(str)
                        .nunique()
                        != 1
                    ):
                        reasons.append("TICKER_OR_SHARE_CLASS_IDENTITY_AMBIGUOUS")
                identity_object = store.object_path(
                    str(identity_record["content_sha256"])
                )
                if (
                    not identity_object.is_file()
                    or sha256_file(identity_object)
                    != str(identity_record["content_sha256"])
                ):
                    reasons.append("IDENTITY_SOURCE_CAS_INVALID")

        base = {
            "event_id": str(row["event_candidate_id"]),
            "security_id": str(row.get("suggested_security_id") or ""),
            "event_type": str(row["event_type"]),
            "announced_at": str(row["announced_at"]),
            "effective_at": str(row["effective_at"]),
            "source_id": str(row["source_id"]),
            "evidence_sha256": str(row["evidence_sha256"]),
            "ticker_at_announcement": str(row["ticker_at_announcement"]),
            "source_url": str(row["source_url"]),
            "reviewer": reviewer_value,
            "reviewed_at": reviewed_iso,
        }
        if reasons:
            sec_filed_approval = None
            if (
                identity_crosscheck is not None
                and basis == "EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR"
                and str(row["status"]) == "REVIEW_REQUIRED"
                and identity_record is not None
                and set(reasons)
                == {"NAME_MATCH_LACKS_UNIQUE_OFFICIAL_TICKER_CROSSCHECK"}
            ):
                sec_filed_approval = _sec_filed_crosscheck_approval(
                    store,
                    crosscheck_index=identity_crosscheck,
                    identities=identities,
                    event_id=str(row["event_candidate_id"]),
                    ticker_at_announcement=str(row["ticker_at_announcement"]),
                    suggested_security_id=str(row.get("suggested_security_id") or ""),
                    verified_dual_source_keys=verified_dual_source_keys,
                )
            if sec_filed_approval is not None:
                crosscheck_approved_count += 1
                approved_rows.append(
                    {
                        **base,
                        "approved": True,
                        "review_note": (
                            "CODEX_DIRECT_EVIDENCE_REVIEW_V2: exact frozen S&P "
                            "event, publication timestamp, XNYS effective session, "
                            "and directional official issuer identity verified; "
                            "ticker-to-stable-ID binding admitted through an "
                            "official SEC filed identity crosscheck whose filing "
                            "evidence replays byte-exact from the CAS and agrees "
                            "with captured sec_nport_ivv holdings lineage on the "
                            "same stable identifier; identity binding only, event "
                            "timing remains governed by the S&P announcement. "
                            f"crosscheck_evidence={sec_filed_approval['crosscheck_evidence_sha256']}:"
                            f"{sec_filed_approval['crosscheck_accession_number']}:"
                            f"{sec_filed_approval['crosscheck_filing_date']}."
                        ),
                        "candidate_status": str(row["status"]),
                        "identity_match_basis": basis,
                    }
                )
                continue
            unresolved_rows.append(
                {
                    **base,
                    "approved": False,
                    "candidate_status": str(row["status"]),
                    "identity_match_basis": basis,
                    "review_outcome": "BLOCKED",
                    "review_reasons": ";".join(sorted(set(reasons))),
                }
            )
            continue
        approved_rows.append(
            {
                **base,
                "approved": True,
                "review_note": (
                    "CODEX_DIRECT_EVIDENCE_REVIEW: exact frozen S&P event, "
                    "publication timestamp, XNYS effective session, and unique "
                    "directional official ticker-to-stable-ID lineage verified"
                    + (
                        " through independent SEC identifier and iShares ticker "
                        "cross-evidence"
                        if identity_crosscheck_hashes
                        else ""
                    )
                    + "; "
                    f"identity_source={identity_record['content_sha256']}:"
                    f"{int(identity_record['source_row_number'])}."
                ),
                "candidate_status": str(row["status"]),
                "identity_match_basis": basis,
            }
        )

    approved_frame = pd.DataFrame(approved_rows)
    unresolved_frame = pd.DataFrame(unresolved_rows)
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"S&P event evidence review already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        approved_path = staging / "membership_events.parquet"
        unresolved_path = staging / "unresolved_membership_events.parquet"
        approved_frame.to_parquet(approved_path, index=False)
        unresolved_frame.to_parquet(unresolved_path, index=False)
        manifest = {
            "format_version": (
                SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION_V2
                if identity_crosscheck is not None
                else SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION
            ),
            "candidate_set_id": candidate_set_id,
            "candidate_manifest_sha256": sha256_file(candidate_manifest_path),
            "candidate_artifact_sha256": sha256_file(candidate_artifact_path),
            "normalization_id": normalization.name,
            "normalization_manifest_sha256": sha256_file(
                normalization_manifest_path
            ),
            "identity_artifact_sha256": sha256_file(identity_path),
            "source_batch_ids": list(batch_ids),
            "reviewer": reviewer_value,
            "reviewed_at": reviewed_iso,
            "candidate_rows": int(len(candidates)),
            "approved_rows": int(len(approved_frame)),
            "blocked_rows": int(len(unresolved_frame)),
            "status": "REVIEWED" if unresolved_frame.empty else "DATA_BLOCKED",
            "direct_build_allowed": bool(unresolved_frame.empty),
            "policy": {
                "frozen_official_event_reparsed": True,
                "publication_precedes_effective_open": True,
                "effective_date_is_xnys_session": True,
                "stable_id_from_isin_or_cusip": True,
                "exact_official_ticker_required": True,
                "name_match_requires_independent_official_ticker_crosscheck": True,
                "late_holdings_crosscheck_validates_identity_only": True,
                "unresolved_rows_excluded_from_membership_events": True,
            },
            **(
                {
                    "identity_crosscheck_input": {
                        "format_version": identity_crosscheck["manifest"].get(
                            "format_version"
                        ),
                        "manifest_sha256": identity_crosscheck["manifest_sha256"],
                        "artifact_sha256": identity_crosscheck["artifact_sha256"],
                        "source_batch_id": identity_crosscheck["manifest"].get(
                            "source_batch_id"
                        ),
                        "crosscheck_reviewer": str(
                            identity_crosscheck["manifest"].get("reviewer") or ""
                        ),
                        "approved_via_sec_filed_crosscheck": int(
                            crosscheck_approved_count
                        ),
                    }
                }
                if identity_crosscheck is not None
                else {}
            ),
            "artifacts": {
                "membership_events": {
                    "filename": approved_path.name,
                    "row_count": int(len(approved_frame)),
                    "sha256": sha256_file(approved_path),
                },
                "unresolved_membership_events": {
                    "filename": unresolved_path.name,
                    "row_count": int(len(unresolved_frame)),
                    "sha256": sha256_file(unresolved_path),
                },
            },
        }
        manifest["review_id"] = sha256_json(manifest)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return SPGlobalEventEvidenceReviewResult(output, manifest)


__all__ = [
    "SPGLOBAL_EVENT_CANDIDATE_VERSION",
    "SPGLOBAL_EVENT_REVIEW_VERSION",
    "SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION",
    "SPGlobalEventCandidateResult",
    "SPGlobalEventEvidenceReviewResult",
    "SPGlobalEventReviewResult",
    "build_spglobal_event_candidates",
    "prepare_spglobal_event_review",
    "review_spglobal_event_evidence",
]
