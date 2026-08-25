from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import pandas as pd

from .hashing import canonical_json_bytes, sha256_file, sha256_json
from .store import USPITStore


SEC_FILING_CANDIDATE_VERSION = "us-pit-sec-filing-candidates-v2"
RELEVANT_FORMS = frozenset({
    "8-K", "8-K/A", "S-4", "S-4/A", "425", "DEFA14A", "PREM14A",
    "SC 13E3", "SC 13E3/A", "10-12B", "10-12B/A", "10-12G", "10-12G/A",
})
RELEVANT_8K_ITEMS = frozenset({"1.01", "2.01", "3.03", "5.03", "8.01"})
FILING_DISCOVERY_ALGORITHM = "sec-form-window-items-v2"


@dataclass(frozen=True)
class SECFilingCandidateResult:
    path: Path
    manifest: dict[str, Any]


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def load_unique_candidate_ciks(candidate_dir: Path | str) -> tuple[str, ...]:
    root = Path(candidate_dir).resolve()
    artifact = root / "sec_cik_candidates.parquet"
    manifest_path = root / "manifest.json"
    if not artifact.is_file() or not manifest_path.is_file():
        raise ValueError("SEC CIK candidate package is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("artifact_sha256") != sha256_file(artifact)
        or manifest.get("candidate_only") is not True
        or manifest.get("direct_build_allowed") is not False
    ):
        raise ValueError("SEC CIK candidate package failed integrity policy")
    frame = pd.read_parquet(artifact)
    values = frame.loc[frame["match_status"].astype(str).eq("CANDIDATE"), "candidate_cik"]
    ciks = tuple(sorted(set(values.astype(str).str.zfill(10))))
    if not ciks:
        raise ValueError("SEC CIK candidate package has no unique CIK candidates")
    return ciks


def _filing_rows(payload: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    values = payload.get("filings", {}).get("recent") if kind == "company_submissions_main" else payload
    if not isinstance(values, dict):
        raise ValueError("SEC submissions object has no filing arrays")
    required = (
        "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime",
        "form", "items", "primaryDocument", "primaryDocDescription",
    )
    arrays = {field: values.get(field) for field in required}
    if any(not isinstance(value, list) for value in arrays.values()):
        raise ValueError("SEC submissions filing schema is incomplete")
    lengths = {len(value) for value in arrays.values()}
    if len(lengths) != 1:
        raise ValueError("SEC submissions filing arrays have conflicting lengths")
    return [
        {field: arrays[field][index] for field in required}
        for index in range(lengths.pop())
    ]


def build_sec_filing_candidates(
    store: USPITStore,
    source_batch_ids: Iterable[str],
    cik_candidate_dir: Path | str,
    output_dir: Path | str,
    *,
    before_days: int = 365,
    after_days: int = 93,
) -> SECFilingCandidateResult:
    if before_days <= 0 or after_days < 0:
        raise ValueError("SEC filing discovery window is invalid")
    ciks = set(load_unique_candidate_ciks(cik_candidate_dir))
    cik_root = Path(cik_candidate_dir).resolve()
    cik_manifest_path = cik_root / "manifest.json"
    cik_frame = pd.read_parquet(cik_root / "sec_cik_candidates.parquet")
    request_sides = cik_frame.loc[
        cik_frame["match_status"].astype(str).eq("CANDIDATE")
    ].copy()

    batch_ids = tuple(sorted(set(_text(item) for item in source_batch_ids)))
    dependencies = []
    for batch_id in batch_ids:
        dependencies.extend(store.load_source_batch(batch_id).dependencies)
    selected = [
        item for item in dependencies
        if item.source_id == "sec_company_submissions"
        and item.dataset == "corporate_action_filing_index"
        and _text(dict(item.metadata).get("cik")) in ciks
    ]
    covered = {_text(dict(item.metadata).get("cik")) for item in selected}
    if covered != ciks:
        raise ValueError("SEC submissions source batch does not cover every unique CIK candidate")

    filing_index: dict[str, dict[str, dict[str, Any]]] = {cik: {} for cik in ciks}
    for dependency in selected:
        path = store.object_path(dependency.object_sha256)
        if not path.is_file() or sha256_file(path) != dependency.object_sha256:
            raise ValueError("SEC submissions raw object is missing or corrupt")
        metadata = dict(dependency.metadata)
        if metadata.get("response_sha256") != dependency.object_sha256:
            raise ValueError("SEC submissions metadata does not bind the raw object")
        cik = _text(metadata.get("cik"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in _filing_rows(payload, _text(metadata.get("artifact_kind"))):
            accession = _text(row.get("accessionNumber"))
            if not accession:
                continue
            previous = filing_index[cik].get(accession)
            normalized = {key: _text(value) for key, value in row.items()}
            if previous is not None and previous != normalized:
                raise ValueError("SEC submissions duplicate accession has conflicting metadata")
            filing_index[cik][accession] = normalized

    output_rows: list[dict[str, Any]] = []
    for side in request_sides.to_dict(orient="records"):
        cik = _text(side.get("candidate_cik"))
        anchor = date.fromisoformat(_text(side.get("anchor_date")))
        start = anchor - timedelta(days=before_days)
        end = anchor + timedelta(days=after_days)
        matched = 0
        for filing in sorted(filing_index[cik].values(), key=lambda item: (item["filingDate"], item["accessionNumber"])):
            form = filing["form"].upper()
            items = frozenset(
                value.strip()
                for value in filing["items"].replace(";", ",").split(",")
                if value.strip()
            )
            try:
                filing_date = date.fromisoformat(filing["filingDate"])
            except ValueError as exc:
                raise ValueError("SEC submissions filing date is invalid") from exc
            if form not in RELEVANT_FORMS or not start <= filing_date <= end:
                continue
            if form in {"8-K", "8-K/A"} and not (items & RELEVANT_8K_ITEMS):
                continue
            matched += 1
            accession = filing["accessionNumber"]
            accession_compact = accession.replace("-", "")
            archive_url = (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{accession_compact}/{accession}.txt"
            )
            identity = {
                "request_id": _text(side.get("request_id")),
                "side": _text(side.get("side")),
                "cik": cik,
                "accession_number": accession,
            }
            output_rows.append({
                "filing_candidate_id": sha256_json(identity),
                **identity,
                "anchor_date": anchor.isoformat(),
                "security_id": _text(side.get("security_id")),
                "query_name": _text(side.get("query_name")),
                "query_ticker": _text(side.get("query_ticker")),
                "form": form,
                "filing_date": filing["filingDate"],
                "report_date": filing["reportDate"],
                "accepted_at": filing["acceptanceDateTime"],
                "primary_document": filing["primaryDocument"],
                "primary_document_description": filing["primaryDocDescription"],
                "items": filing["items"],
                "complete_submission_url": archive_url,
                "discovery_basis": "FORM_AND_ANCHOR_WINDOW",
                "corporate_action_relevance_confirmed": False,
                "action_terms_verified": False,
                "approved": False,
                "review_note": "",
            })
        if matched == 0:
            identity = {
                "request_id": _text(side.get("request_id")),
                "side": _text(side.get("side")),
                "cik": cik,
                "accession_number": "",
            }
            output_rows.append({
                "filing_candidate_id": sha256_json(identity),
                **identity,
                "anchor_date": anchor.isoformat(),
                "security_id": _text(side.get("security_id")),
                "query_name": _text(side.get("query_name")),
                "query_ticker": _text(side.get("query_ticker")),
                "form": "",
                "filing_date": "",
                "report_date": "",
                "accepted_at": "",
                "primary_document": "",
                "primary_document_description": "",
                "items": "",
                "complete_submission_url": "",
                "discovery_basis": "NO_RELEVANT_FORM_IN_WINDOW",
                "corporate_action_relevance_confirmed": False,
                "action_terms_verified": False,
                "approved": False,
                "review_note": "",
            })
    frame = pd.DataFrame(output_rows)
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"SEC filing candidate output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        artifact = staging / "sec_filing_candidates.parquet"
        frame.to_parquet(artifact, index=False)
        found = int(frame["accession_number"].astype(str).ne("").sum())
        manifest = {
            "format_version": SEC_FILING_CANDIDATE_VERSION,
            "cik_candidate_manifest_sha256": sha256_file(cik_manifest_path),
            "source_batch_ids": list(batch_ids),
            "unique_cik_count": len(ciks),
            "request_side_count": len(request_sides),
            "filing_candidate_count": found,
            "unresolved_side_count": int(frame["accession_number"].astype(str).eq("").sum()),
            "discovery_window_days": {"before": before_days, "after": after_days},
            "discovery_algorithm": FILING_DISCOVERY_ALGORITHM,
            "relevant_8k_items": sorted(RELEVANT_8K_ITEMS),
            "artifact_sha256": sha256_file(artifact),
            "status": "REVIEW_REQUIRED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "policy": {
                "form_type_is_not_action_evidence": True,
                "filing_metadata_is_not_action_terms": True,
                "complete_submission_must_be_frozen_and_reviewed": True,
            },
        }
        manifest["candidate_set_id"] = sha256_json(manifest)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return SECFilingCandidateResult(output, manifest)


__all__ = [
    "RELEVANT_FORMS",
    "RELEVANT_8K_ITEMS",
    "SEC_FILING_CANDIDATE_VERSION",
    "SECFilingCandidateResult",
    "build_sec_filing_candidates",
    "load_unique_candidate_ciks",
]
