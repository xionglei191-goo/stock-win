from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import pandas as pd

from .hashing import canonical_json_bytes, sha256_file, sha256_json
from .identity_bridge import normalized_issuer_name
from .store import USPITStore


SEC_CIK_CANDIDATE_VERSION = "us-pit-sec-cik-candidates-v1"


@dataclass(frozen=True)
class SECCIKCandidateResult:
    path: Path
    manifest: dict[str, Any]


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def build_sec_cik_candidates(
    store: USPITStore,
    source_batch_ids: Iterable[str],
    evidence_request_dir: Path | str,
    output_dir: Path | str,
    *,
    normalization_dir: Path | str | None = None,
) -> SECCIKCandidateResult:
    request_root = Path(evidence_request_dir).resolve()
    request_path = request_root / "corporate_action_evidence_requests.parquet"
    request_manifest_path = request_root / "manifest.json"
    if not request_path.is_file() or not request_manifest_path.is_file():
        raise ValueError("evidence request package is incomplete")
    request_manifest = json.loads(request_manifest_path.read_text(encoding="utf-8"))
    if (
        request_manifest.get("artifact_sha256") != sha256_file(request_path)
        or request_manifest.get("candidate_only") is not True
        or request_manifest.get("direct_build_allowed") is not False
    ):
        raise ValueError("evidence request package failed integrity policy")

    batch_ids = tuple(sorted(set(_text(item) for item in source_batch_ids)))
    if not batch_ids or any(not item for item in batch_ids):
        raise ValueError("at least one SEC company-index source batch is required")
    dependencies = []
    for batch_id in batch_ids:
        dependencies.extend(store.load_source_batch(batch_id).dependencies)
    selected = [
        item for item in dependencies
        if item.source_id == "sec_company_identity_index"
        and item.dataset == "security_identity_index"
    ]
    if not selected:
        raise ValueError("source batches contain no SEC company identity index")
    dependency = max(selected, key=lambda item: item.observed_at)
    source_path = store.object_path(dependency.object_sha256)
    if not source_path.is_file() or sha256_file(source_path) != dependency.object_sha256:
        raise ValueError("SEC company identity object is missing or corrupt")
    if dict(dependency.metadata).get("response_sha256") != dependency.object_sha256:
        raise ValueError("SEC company identity metadata does not bind the raw object")
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    index = pd.DataFrame(list(raw.values()))
    index["cik"] = index["cik_str"].map(lambda value: str(int(value)).zfill(10))
    index["ticker_norm"] = index["ticker"].astype(str).str.upper().str.strip()
    index["name_norm"] = index["title"].map(normalized_issuer_name)

    identities = pd.DataFrame()
    normalization_manifest_sha256 = ""
    normalization_id = ""
    if normalization_dir is not None:
        normalization_root = Path(normalization_dir).resolve()
        normalization_manifest_path = normalization_root / "manifest.json"
        identity_path = normalization_root / "security_identity_candidates.parquet"
        if not normalization_manifest_path.is_file() or not identity_path.is_file():
            raise ValueError("official normalization package is incomplete")
        normalization_manifest = json.loads(
            normalization_manifest_path.read_text(encoding="utf-8")
        )
        identity_descriptor = dict(
            dict(normalization_manifest.get("artifacts") or {}).get(
                "security_identity_candidates"
            )
            or {}
        )
        if (
            normalization_root.name != str(normalization_manifest.get("normalization_id") or "")
            or identity_descriptor.get("object_sha256") != sha256_file(identity_path)
            or normalization_manifest.get("candidate_only") is not True
            or normalization_manifest.get("direct_build_allowed") is not False
        ):
            raise ValueError("official normalization package failed integrity policy")
        identities = pd.read_parquet(identity_path)
        normalization_manifest_sha256 = sha256_file(normalization_manifest_path)
        normalization_id = str(normalization_manifest.get("normalization_id") or "")

    requests = pd.read_parquet(request_path)
    rows: list[dict[str, Any]] = []
    for request in requests.to_dict(orient="records"):
        for side in ("predecessor", "successor"):
            ticker = _text(request.get(f"{side}_ticker")).upper()
            name = _text(request.get(f"{side}_name"))
            ticker_matches = index.loc[index["ticker_norm"].eq(ticker)] if ticker else index.iloc[0:0]
            if not ticker_matches.empty:
                matches = ticker_matches
                basis = "CURRENT_TICKER_EXACT"
            else:
                name_key = normalized_issuer_name(name)
                matches = index.loc[index["name_norm"].eq(name_key)] if name_key else index.iloc[0:0]
                basis = "CURRENT_NORMALIZED_NAME_EXACT"
            if matches.empty and not identities.empty:
                isin = _text(request.get(f"{side}_isin")).upper()
                cusip = _text(request.get(f"{side}_cusip")).upper()
                values = identities.loc[
                    identities["source_id"].astype(str).eq("ishares_ivv_holdings_api")
                    & identities["ticker"].notna()
                    & identities["ticker"].astype(str).str.strip().ne("")
                    & (
                        identities["isin"].astype(str).str.upper().eq(isin)
                        | identities["cusip"].astype(str).str.upper().eq(cusip)
                    )
                ].copy()
                values["_as_of"] = pd.to_datetime(values["as_of_date"], errors="coerce")
                values = values.loc[values["_as_of"].notna()]
                anchor = pd.Timestamp(_text(request.get("anchor_date")))
                if side == "successor":
                    directional = values.loc[values["_as_of"] >= anchor]
                    if not directional.empty:
                        values = directional.loc[
                            directional["_as_of"].eq(directional["_as_of"].min())
                        ]
                else:
                    directional = values.loc[values["_as_of"] < anchor]
                    if not directional.empty:
                        values = directional.loc[
                            directional["_as_of"].eq(directional["_as_of"].max())
                        ]
                official_tickers = tuple(sorted(set(values["ticker"].astype(str).str.upper())))
                if len(official_tickers) == 1:
                    matches = index.loc[index["ticker_norm"].eq(official_tickers[0])]
                    if not matches.empty:
                        basis = "FROZEN_HOLDING_TICKER_TO_CURRENT_SEC_INDEX"
            if matches.empty:
                candidate_records: list[dict[str, Any]] = [{}]
                status = "UNRESOLVED"
            else:
                matches = matches.sort_values(["cik", "ticker_norm"]).copy()
                unique_ciks = tuple(sorted(set(matches["cik"].astype(str))))
                status = "CANDIDATE" if len(unique_ciks) == 1 else "AMBIGUOUS"
                candidate_records = []
                for cik, values in matches.groupby("cik", sort=True):
                    candidate_records.append({
                        "cik": str(cik),
                        "ticker": "|".join(sorted(set(values["ticker"].astype(str)))),
                        "title": "|".join(sorted(set(values["title"].astype(str)))),
                    })
            for candidate in candidate_records:
                identity = {
                    "request_id": _text(request.get("request_id")),
                    "side": side.upper(),
                    "candidate_cik": _text(candidate.get("cik")),
                    "candidate_ticker": _text(candidate.get("ticker")),
                }
                rows.append({
                    "candidate_id": sha256_json(identity),
                    **identity,
                    "anchor_date": _text(request.get("anchor_date")),
                    "security_id": _text(request.get(f"{side}_security_id")),
                    "query_ticker": ticker,
                    "query_name": name,
                    "candidate_title": _text(candidate.get("title")),
                    "match_basis": basis,
                    "match_status": status,
                    "source_id": dependency.source_id,
                    "source_version": dependency.source_version,
                    "source_object_sha256": dependency.object_sha256,
                    "source_observed_at": dependency.observed_at,
                    "current_snapshot_only": True,
                    "historical_identity_confirmed": False,
                    "corporate_action_evidence": False,
                    "approved": False,
                    "review_note": "",
                })
    frame = pd.DataFrame(rows)
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"SEC CIK candidate output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        artifact_path = staging / "sec_cik_candidates.parquet"
        frame.to_parquet(artifact_path, index=False)
        counts = frame["match_status"].value_counts().to_dict() if not frame.empty else {}
        manifest = {
            "format_version": SEC_CIK_CANDIDATE_VERSION,
            "request_set_id": _text(request_manifest.get("request_set_id")),
            "request_manifest_sha256": sha256_file(request_manifest_path),
            "source_batch_ids": list(batch_ids),
            "source_object_sha256": dependency.object_sha256,
            "normalization_id": normalization_id,
            "normalization_manifest_sha256": normalization_manifest_sha256,
            "row_count": len(frame),
            "match_counts": {str(key): int(value) for key, value in sorted(counts.items())},
            "artifact_sha256": sha256_file(artifact_path),
            "status": "REVIEW_REQUIRED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "policy": {
                "current_company_index_is_not_historical_identity": True,
                "cik_candidate_is_not_action_evidence": True,
                "approval_default": False,
            },
        }
        manifest["candidate_set_id"] = sha256_json(manifest)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return SECCIKCandidateResult(output, manifest)


__all__ = [
    "SEC_CIK_CANDIDATE_VERSION",
    "SECCIKCandidateResult",
    "build_sec_cik_candidates",
]
