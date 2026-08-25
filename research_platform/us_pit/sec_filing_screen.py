from __future__ import annotations

import html
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import pandas as pd

from .hashing import canonical_json_bytes, sha256_file, sha256_json
from .identity_bridge import normalized_issuer_name
from .store import USPITStore


SEC_FILING_SCREEN_VERSION = "us-pit-sec-filing-screen-v1"
SEC_FILING_RANK_VERSION = "us-pit-sec-filing-rank-v2"
EVENT_PATTERNS = {
    "MERGER": re.compile(r"\b(merger|business combination|scheme of arrangement)\b", re.I),
    "SPINOFF": re.compile(r"\b(spin[- ]?off|separation|distribution of shares)\b", re.I),
    "RENAME": re.compile(r"\b(name change|change (?:its|the) name|renamed|new corporate name)\b", re.I),
    "REORGANIZATION": re.compile(r"\b(reorganization|redomestication|reincorporation)\b", re.I),
    "DELISTING": re.compile(r"\b(delisting|deregister|cease to be listed|bankruptcy)\b", re.I),
    "SPLIT": re.compile(r"\b(stock split|reverse stock split|share split)\b", re.I),
}


@dataclass(frozen=True)
class SECFilingScreenResult:
    path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class SECFilingRankResult:
    path: Path
    manifest: dict[str, Any]


def _plain_text(payload: bytes) -> str:
    value = payload.decode("latin-1", errors="ignore")
    value = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _tokens(name: str, ticker: str) -> tuple[str, ...]:
    values: set[str] = set()
    normalized = normalized_issuer_name(name)
    if len(normalized) >= 5:
        values.add(normalized)
    ticker_value = str(ticker).strip().upper()
    if len(ticker_value) >= 2:
        values.add(ticker_value)
    return tuple(sorted(values))


def screen_sec_filing_candidates(
    store: USPITStore,
    source_batch_ids: Iterable[str],
    filing_candidate_dir: Path | str,
    evidence_request_dir: Path | str,
    output_dir: Path | str,
) -> SECFilingScreenResult:
    filing_root = Path(filing_candidate_dir).resolve()
    filing_path = filing_root / "sec_filing_candidates.parquet"
    filing_manifest_path = filing_root / "manifest.json"
    request_root = Path(evidence_request_dir).resolve()
    request_path = request_root / "corporate_action_evidence_requests.parquet"
    request_manifest_path = request_root / "manifest.json"
    if not all(path.is_file() for path in (
        filing_path, filing_manifest_path, request_path, request_manifest_path
    )):
        raise ValueError("SEC filing screen inputs are incomplete")
    filing_manifest = json.loads(filing_manifest_path.read_text(encoding="utf-8"))
    request_manifest = json.loads(request_manifest_path.read_text(encoding="utf-8"))
    if (
        filing_manifest.get("artifact_sha256") != sha256_file(filing_path)
        or request_manifest.get("artifact_sha256") != sha256_file(request_path)
        or filing_manifest.get("candidate_only") is not True
        or request_manifest.get("candidate_only") is not True
    ):
        raise ValueError("SEC filing screen input integrity failed")

    batch_ids = tuple(sorted(set(str(value).strip() for value in source_batch_ids)))
    dependencies = []
    for batch_id in batch_ids:
        dependencies.extend(store.load_source_batch(batch_id).dependencies)
    candidate_set_id = str(filing_manifest.get("candidate_set_id") or "")
    candidate_manifest_sha = sha256_file(filing_manifest_path)
    selected = [
        item for item in dependencies
        if item.source_id == "sec_corporate_action_filing_documents"
        and item.dataset == "corporate_action_source_document"
        and dict(item.metadata).get("candidate_set_id") == candidate_set_id
        and dict(item.metadata).get("candidate_manifest_sha256") == candidate_manifest_sha
    ]
    by_accession: dict[str, Any] = {}
    for dependency in selected:
        metadata = dict(dependency.metadata)
        accession = str(metadata.get("accession_number") or "")
        if not accession or accession in by_accession:
            raise ValueError("SEC filing source batches contain missing or duplicate accession")
        object_path = store.object_path(dependency.object_sha256)
        if not object_path.is_file() or sha256_file(object_path) != dependency.object_sha256:
            raise ValueError("SEC filing source object is missing or corrupt")
        if metadata.get("response_sha256") != dependency.object_sha256:
            raise ValueError("SEC filing source metadata does not bind raw content")
        by_accession[accession] = dependency
    filings = pd.read_parquet(filing_path)
    expected = set(
        filings.loc[filings["accession_number"].astype(str).ne(""), "accession_number"].astype(str)
    )
    if set(by_accession) != expected:
        raise ValueError("SEC filing source batches do not exactly cover candidate accessions")
    requests = pd.read_parquet(request_path).set_index("request_id", drop=False)

    rows: list[dict[str, Any]] = []
    unique_filings = filings.loc[
        filings["accession_number"].astype(str).ne("")
    ].drop_duplicates("accession_number")
    for filing in unique_filings.to_dict(orient="records"):
        accession = str(filing["accession_number"])
        dependency = by_accession[accession]
        plain = _plain_text(store.object_path(dependency.object_sha256).read_bytes())
        plain_upper = plain.upper()
        linked = filings.loc[filings["accession_number"].astype(str).eq(accession)]
        request_ids = tuple(sorted(set(linked["request_id"].astype(str))))
        identity_hits: set[str] = set()
        for request_id in request_ids:
            if request_id not in requests.index:
                raise ValueError("SEC filing candidate references an unknown evidence request")
            request = requests.loc[request_id]
            if isinstance(request, pd.DataFrame):
                raise ValueError("evidence request IDs are not unique")
            for side in ("predecessor", "successor"):
                for token in _tokens(request.get(f"{side}_name", ""), request.get(f"{side}_ticker", "")):
                    if token.upper() in plain_upper:
                        identity_hits.add(f"{side.upper()}:{token}")
                for field in ("isin", "cusip"):
                    value = str(request.get(f"{side}_{field}", "")).strip().upper()
                    if value and value in plain_upper:
                        identity_hits.add(f"{side.upper()}:{value}")
        event_hits = tuple(sorted(name for name, pattern in EVENT_PATTERNS.items() if pattern.search(plain)))
        if event_hits and identity_hits:
            relevance = "HIGH" if len(identity_hits) >= 2 else "MEDIUM"
        elif event_hits or identity_hits:
            relevance = "LOW"
        else:
            relevance = "NONE"
        context = ""
        positions = [match.start() for pattern in EVENT_PATTERNS.values() for match in pattern.finditer(plain)]
        if positions:
            start = max(0, min(positions) - 240)
            context = plain[start : start + 800]
        rows.append({
            "screen_id": sha256_json({
                "candidate_set_id": candidate_set_id,
                "accession_number": accession,
                "source_object_sha256": dependency.object_sha256,
            }),
            "accession_number": accession,
            "cik": str(dict(dependency.metadata).get("cik") or ""),
            "form": str(filing.get("form") or ""),
            "filing_date": str(filing.get("filing_date") or ""),
            "accepted_at": str(dict(dependency.metadata).get("accepted_at") or ""),
            "source_url": dependency.url,
            "source_object_sha256": dependency.object_sha256,
            "request_ids": "|".join(request_ids),
            "identity_hits": "|".join(sorted(identity_hits)),
            "event_keyword_hits": "|".join(event_hits),
            "relevance": relevance,
            "context_excerpt": context,
            "corporate_action_relevance_confirmed": False,
            "action_type": "",
            "terms_verified": False,
            "approved": False,
            "review_note": "",
        })
    frame = pd.DataFrame(rows)
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"SEC filing screen output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        artifact = staging / "sec_filing_screen.parquet"
        frame.to_parquet(artifact, index=False)
        counts = frame["relevance"].value_counts().to_dict()
        manifest = {
            "format_version": SEC_FILING_SCREEN_VERSION,
            "candidate_set_id": candidate_set_id,
            "filing_candidate_manifest_sha256": candidate_manifest_sha,
            "evidence_request_manifest_sha256": sha256_file(request_manifest_path),
            "source_batch_ids": list(batch_ids),
            "source_document_count": len(by_accession),
            "screen_row_count": len(frame),
            "relevance_counts": {str(key): int(value) for key, value in sorted(counts.items())},
            "artifact_sha256": sha256_file(artifact),
            "status": "REVIEW_REQUIRED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "policy": {
                "keyword_screen_is_not_action_evidence": True,
                "context_requires_human_review": True,
                "terms_must_not_be_inferred": True,
            },
        }
        manifest["screen_set_id"] = sha256_json(manifest)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return SECFilingScreenResult(output, manifest)


def rank_sec_filing_screen(
    screen_dir: Path | str,
    filing_candidate_dir: Path | str,
    evidence_request_dir: Path | str,
    output_dir: Path | str,
    *,
    per_request: int = 10,
) -> SECFilingRankResult:
    if per_request <= 0 or per_request > 25:
        raise ValueError("per_request must be between 1 and 25")
    screen_root = Path(screen_dir).resolve()
    filing_root = Path(filing_candidate_dir).resolve()
    request_root = Path(evidence_request_dir).resolve()
    screen_path = screen_root / "sec_filing_screen.parquet"
    screen_manifest_path = screen_root / "manifest.json"
    filing_path = filing_root / "sec_filing_candidates.parquet"
    filing_manifest_path = filing_root / "manifest.json"
    request_path = request_root / "corporate_action_evidence_requests.parquet"
    request_manifest_path = request_root / "manifest.json"
    if not all(path.is_file() for path in (
        screen_path, screen_manifest_path, filing_path, filing_manifest_path,
        request_path, request_manifest_path,
    )):
        raise ValueError("SEC filing rank inputs are incomplete")
    screen_manifest = json.loads(screen_manifest_path.read_text(encoding="utf-8"))
    filing_manifest = json.loads(filing_manifest_path.read_text(encoding="utf-8"))
    request_manifest = json.loads(request_manifest_path.read_text(encoding="utf-8"))
    if (
        screen_manifest.get("artifact_sha256") != sha256_file(screen_path)
        or filing_manifest.get("artifact_sha256") != sha256_file(filing_path)
        or request_manifest.get("artifact_sha256") != sha256_file(request_path)
        or screen_manifest.get("candidate_only") is not True
        or filing_manifest.get("candidate_only") is not True
        or request_manifest.get("candidate_only") is not True
    ):
        raise ValueError("SEC filing rank input integrity failed")
    if screen_manifest.get("candidate_set_id") != filing_manifest.get("candidate_set_id"):
        raise ValueError("SEC filing screen and filing candidates disagree")

    screens = pd.read_parquet(screen_path).set_index("accession_number", drop=False)
    filings = pd.read_parquet(filing_path)
    requests = pd.read_parquet(request_path)
    request_ids = set(requests["request_id"].astype(str))
    relevance_points = {"HIGH": 50, "MEDIUM": 30, "LOW": 10, "NONE": 0}
    form_points = {
        "S-4": 45, "S-4/A": 42, "PREM14A": 40, "DEFA14A": 35,
        "8-K": 30, "8-K/A": 27, "425": 20,
    }
    rows: list[dict[str, Any]] = []
    for filing in filings.loc[
        filings["accession_number"].astype(str).ne("")
    ].to_dict(orient="records"):
        request_id = str(filing.get("request_id") or "")
        accession = str(filing.get("accession_number") or "")
        if request_id not in request_ids or accession not in screens.index:
            raise ValueError("SEC filing rank encountered an unknown request or accession")
        screen = screens.loc[accession]
        if isinstance(screen, pd.DataFrame):
            raise ValueError("SEC filing screen accession is not unique")
        anchor = pd.Timestamp(str(filing.get("anchor_date"))).date()
        filed = pd.Timestamp(str(filing.get("filing_date"))).date()
        distance = abs((anchor - filed).days)
        identity_hits = tuple(filter(None, str(screen["identity_hits"]).split("|")))
        sides = {value.split(":", 1)[0] for value in identity_hits if ":" in value}
        event_hits = tuple(filter(None, str(screen["event_keyword_hits"]).split("|")))
        score = (
            relevance_points.get(str(screen["relevance"]), 0)
            + form_points.get(str(filing.get("form") or ""), 0)
            + (20 if {"PREDECESSOR", "SUCCESSOR"}.issubset(sides) else 8 if sides else 0)
            + min(24, len(event_hits) * 6)
            - min(30, distance // 30)
        )
        rows.append({
            "request_id": request_id,
            "accession_number": accession,
            "anchor_date": anchor.isoformat(),
            "filing_date": filed.isoformat(),
            "distance_days": distance,
            "form": str(filing.get("form") or ""),
            "items": str(filing.get("items") or ""),
            "source_url": str(screen["source_url"]),
            "source_object_sha256": str(screen["source_object_sha256"]),
            "accepted_at": str(screen["accepted_at"]),
            "relevance": str(screen["relevance"]),
            "identity_hits": str(screen["identity_hits"]),
            "event_keyword_hits": str(screen["event_keyword_hits"]),
            "context_excerpt": str(screen["context_excerpt"]),
            "rank_score": int(score),
            "corporate_action_relevance_confirmed": False,
            "action_type": "",
            "announced_at": "",
            "effective_at": "",
            "terms_verified": False,
            "approved": False,
            "review_note": "",
        })
    ranked = pd.DataFrame(rows)
    duplicate_key = ["request_id", "accession_number", "source_object_sha256"]
    duplicate_rows_removed = 0
    if not ranked.empty and ranked.duplicated(duplicate_key, keep=False).any():
        comparison_columns = [
            column for column in ranked.columns if column not in duplicate_key
        ]
        duplicates = ranked.loc[ranked.duplicated(duplicate_key, keep=False)]
        for _, group in duplicates.groupby(duplicate_key, sort=True, dropna=False):
            if any(
                group[column].astype(str).nunique(dropna=False) != 1
                for column in comparison_columns
            ):
                raise ValueError("SEC filing rank duplicate request/accession conflicts")
        before = len(ranked)
        ranked = ranked.drop_duplicates(duplicate_key, keep="first")
        duplicate_rows_removed = before - len(ranked)
    ranked = ranked.sort_values(
        ["request_id", "rank_score", "distance_days", "filing_date", "accession_number"],
        ascending=[True, False, True, False, True],
    )
    ranked["request_rank"] = ranked.groupby("request_id").cumcount() + 1
    ranked = ranked.loc[ranked["request_rank"] <= per_request].copy()
    ranked["review_candidate_id"] = ranked.apply(
        lambda row: sha256_json({
            "request_id": row["request_id"],
            "accession_number": row["accession_number"],
            "source_object_sha256": row["source_object_sha256"],
        }),
        axis=1,
    )
    ranked = ranked.sort_values(["request_id", "request_rank"])
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"SEC filing rank output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        artifact = staging / "corporate_action_filing_review.parquet"
        ranked.to_parquet(artifact, index=False)
        covered = int(ranked["request_id"].nunique()) if not ranked.empty else 0
        manifest = {
            "format_version": SEC_FILING_RANK_VERSION,
            "screen_set_id": str(screen_manifest.get("screen_set_id") or ""),
            "screen_manifest_sha256": sha256_file(screen_manifest_path),
            "filing_candidate_manifest_sha256": sha256_file(filing_manifest_path),
            "evidence_request_manifest_sha256": sha256_file(request_manifest_path),
            "request_count": len(request_ids),
            "covered_request_count": covered,
            "per_request_limit": per_request,
            "row_count": len(ranked),
            "duplicate_rows_removed": duplicate_rows_removed,
            "ranking_algorithm": "relevance-form-identity-event-distance-deduplicated-v2",
            "artifact_sha256": sha256_file(artifact),
            "status": "REVIEW_REQUIRED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "policy": {
                "ranking_is_not_action_evidence": True,
                "reviewer_must_verify_complete_submission": True,
                "terms_must_be_transcribed_from_frozen_evidence": True,
            },
        }
        manifest["review_set_id"] = sha256_json(manifest)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return SECFilingRankResult(output, manifest)


__all__ = [
    "EVENT_PATTERNS",
    "SEC_FILING_SCREEN_VERSION",
    "SEC_FILING_RANK_VERSION",
    "SECFilingRankResult",
    "SECFilingScreenResult",
    "screen_sec_filing_candidates",
    "rank_sec_filing_screen",
]
