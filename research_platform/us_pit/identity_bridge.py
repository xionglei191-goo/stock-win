from __future__ import annotations

"""Review-only identity suggestions for current IVV observations.

SEC N-PORT and late-observed iShares product-data rows contain stable
identifiers, while the observed current iShares file contains ticker/name but
may omit ISIN or CUSIP.  This module creates a deterministic *proposal* from
those evidence sets.
It never writes ``identity_review.parquet`` and can never make a release
buildable: every row remains ``REVIEW_REQUIRED`` until an operator cites and
approves independent identity evidence.
"""

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from .hashing import sha256_file, sha256_json


BRIDGE_FORMAT_VERSION = "us-pit-identity-bridge-v1"


@dataclass(frozen=True)
class IdentityBridgeResult:
    path: Path
    manifest: dict[str, Any]


def normalized_issuer_name(value: Any) -> str:
    text = "" if value is None or pd.isna(value) else str(value).upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"\bAND\s+CO(?:MPANY)?\b\.?\s*$", " ", text)
    text = re.sub(r"\bINTL\b", " INTERNATIONAL ", text)
    text = re.sub(r"\bMGT\b", " MANAGEMENT ", text)
    text = re.sub(
        r"\b(CLASS|COMMON|SHARES?|INC|INCORPORATED|CORP(ORATION)?|PLC|LTD|LIMITED|CO|COMPANY|NV)\b",
        " ",
        text,
    )
    text = re.sub(r"\bAND\b(?:\W*)$", " ", text)
    text = re.sub(r"[^A-Z0-9]", "", text)
    return text


_norm_name = normalized_issuer_name


def propose_identity_bridges(
    normalization_dir: Path | str,
    output_dir: Path | str,
    *,
    as_of_date: str | None = None,
) -> IdentityBridgeResult:
    source = Path(normalization_dir).resolve()
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("official normalization manifest not found")
    normalization = json.loads(manifest_path.read_text(encoding="utf-8"))
    normalization_id = str(normalization.get("normalization_id") or "")
    if source.name != normalization_id:
        raise ValueError("normalization directory identity mismatch")
    holdings_path = source / "fund_holdings_observed_candidate.parquet"
    identity_path = source / "security_identity_candidates.parquet"
    if not holdings_path.is_file() or not identity_path.is_file():
        raise ValueError("normalization candidate artifacts are incomplete")
    holdings = pd.read_parquet(holdings_path)
    identities = pd.read_parquet(identity_path)
    current = holdings.loc[
        holdings["source_id"].astype(str).eq("ishares_ivv_holdings")
        & holdings["ticker"].notna()
        & holdings["identity_candidate_key"].isna()
    ].copy()
    sec = identities.loc[
        identities["source_id"].astype(str).eq("sec_nport_ivv")
        & identities["identity_candidate_key"].notna()
    ].copy()
    ticker_values = (
        identities["ticker"]
        if "ticker" in identities.columns
        else pd.Series(pd.NA, index=identities.index, dtype="string")
    )
    api = identities.loc[
        identities["source_id"].astype(str).eq("ishares_ivv_holdings_api")
        & identities["identity_candidate_key"].notna()
        & ticker_values.notna()
    ].copy()
    if sec.empty and api.empty:
        raise ValueError("no official stable-identity anchors available")
    sec["_as_of"] = pd.to_datetime(sec["as_of_date"], errors="coerce")
    all_dates = pd.concat(
        [
            pd.to_datetime(sec["as_of_date"], errors="coerce"),
            pd.to_datetime(api["as_of_date"], errors="coerce"),
        ],
        ignore_index=True,
    ).dropna()
    cutoff = pd.Timestamp(as_of_date) if as_of_date else all_dates.max()
    sec = sec.loc[sec["_as_of"].notna() & (sec["_as_of"] <= cutoff)].copy()
    sec = sec.sort_values(["identity_candidate_key", "_as_of", "source_row_number"])
    sec = sec.drop_duplicates("identity_candidate_key", keep="last")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in sec.to_dict(orient="records"):
        key = _norm_name(row.get("issuer_name") or row.get("title"))
        if key:
            by_name.setdefault(key, []).append(row)
    api["_as_of"] = pd.to_datetime(api["as_of_date"], errors="coerce")
    api = api.loc[api["_as_of"].notna() & (api["_as_of"] <= cutoff)].copy()
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in api.to_dict(orient="records"):
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker:
            by_ticker.setdefault(ticker, []).append(row)

    rows: list[dict[str, Any]] = []
    for row in current.to_dict(orient="records"):
        current_name = row.get("issuer_name") or row.get("title")
        key = _norm_name(current_name)
        ticker = str(row.get("ticker") or "").strip().upper()
        ticker_matches = by_ticker.get(ticker, [])
        ticker_ids = {
            str(item.get("identity_candidate_key") or "") for item in ticker_matches
        } - {""}
        if len(ticker_ids) == 1:
            matches_for_id = [
                item for item in ticker_matches
                if str(item.get("identity_candidate_key") or "") in ticker_ids
            ]
            match = sorted(
                matches_for_id,
                key=lambda item: (str(item.get("as_of_date") or ""), int(item.get("source_row_number") or 0)),
            )[-1]
            status = "REVIEW_REQUIRED"
            basis = "OFFICIAL_ISHARES_TICKER_STABLE_ID_HISTORY"
            candidate = match
        elif len(ticker_ids) > 1:
            status = "AMBIGUOUS"
            basis = "TICKER_REUSED_ACROSS_STABLE_IDS"
            candidate = {}
        else:
            matches = by_name.get(key, [])
            if len(matches) == 1:
                match = matches[0]
                status = "REVIEW_REQUIRED"
                basis = "EXACT_NORMALIZED_ISSUER_NAME"
                candidate = match
            elif len(matches) > 1:
                status = "AMBIGUOUS"
                basis = "MULTIPLE_SEC_STABLE_ID_MATCHES"
                candidate = {}
            else:
                status = "UNRESOLVED"
                basis = "NO_OFFICIAL_STABLE_ID_MATCH"
                candidate = {}
        rows.append(
            {
                "holding_candidate_id": str(row["holding_candidate_id"]),
                "current_ticker": str(row.get("ticker") or "").upper(),
                "current_issuer_name": str(current_name or ""),
                "current_as_of_date": str(row.get("as_of_date") or ""),
                "current_source_id": str(row.get("source_id") or ""),
                "current_source_sha256": str(row.get("content_sha256") or ""),
                "historical_security_id": str(candidate.get("identity_candidate_key") or ""),
                "historical_isin": str(candidate.get("isin") or ""),
                "historical_cusip": str(candidate.get("cusip") or ""),
                "historical_as_of_date": str(candidate.get("as_of_date") or ""),
                "historical_source_id": str(candidate.get("source_id") or ""),
                "historical_source_sha256": str(candidate.get("content_sha256") or ""),
                "match_basis": basis,
                "status": status,
                "approved": False,
                "review_note": "Verify issuer/share class and cite independent identity evidence before approval.",
            }
        )
    frame = pd.DataFrame(rows)
    payload = {
        "format_version": BRIDGE_FORMAT_VERSION,
        "normalization_id": normalization_id,
        "normalization_manifest_sha256": sha256_file(manifest_path),
        "as_of_date": cutoff.date().isoformat(),
        "candidate_only": True,
        "direct_build_allowed": False,
        "status": "REVIEW_REQUIRED" if not frame.empty else "DATA_BLOCKED",
        "row_count": int(len(frame)),
        "matched_exact_name": int(
            frame["match_basis"].eq("EXACT_NORMALIZED_ISSUER_NAME").sum()
        ) if not frame.empty else 0,
        "matched_official_ticker": int(
            frame["match_basis"].eq("OFFICIAL_ISHARES_TICKER_STABLE_ID_HISTORY").sum()
        ) if not frame.empty else 0,
        "matched_total": int((frame["status"] == "REVIEW_REQUIRED").sum()) if not frame.empty else 0,
        "ambiguous": int((frame["status"] == "AMBIGUOUS").sum()) if not frame.empty else 0,
        "unresolved": int((frame["status"] == "UNRESOLVED").sum()) if not frame.empty else 0,
        "policy": {
            "sec_rows_are_validation_anchors": True,
            "name_match_is_not_identity_evidence": True,
            "approval_required": True,
        },
    }
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"identity bridge output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        frame.to_parquet(staging / "identity_bridge_candidates.parquet", index=False)
        payload["artifact_sha256"] = sha256_file(staging / "identity_bridge_candidates.parquet")
        payload["bridge_id"] = sha256_json(payload)
        (staging / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return IdentityBridgeResult(output, payload)


__all__ = [
    "BRIDGE_FORMAT_VERSION",
    "IdentityBridgeResult",
    "normalized_issuer_name",
    "propose_identity_bridges",
]
