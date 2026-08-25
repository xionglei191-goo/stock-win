from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from .hashing import sha256_file, sha256_json
from .store import USPITStore
from .tdx_current_master import resolve_current_tdx_alias, tdx_current_codes


FORMAT_VERSION = "us-pit-current-alias-crosscheck-v1"


@dataclass(frozen=True)
class CurrentAliasCrosscheckResult:
    path: Path
    manifest: dict[str, Any]


def crosscheck_current_aliases(
    store: USPITStore | Path | str,
    normalization_dir: Path | str,
    tdx_source_batch_id: str,
    output_dir: Path | str,
) -> CurrentAliasCrosscheckResult:
    pit = store if isinstance(store, USPITStore) else USPITStore(store)
    source = Path(normalization_dir).resolve()
    normalization_manifest_path = source / "manifest.json"
    if not normalization_manifest_path.is_file():
        raise ValueError("official normalization manifest not found")
    normalization_manifest = json.loads(
        normalization_manifest_path.read_text(encoding="utf-8")
    )
    normalization_id = str(normalization_manifest.get("normalization_id") or "")
    if source.name != normalization_id:
        raise ValueError("normalization directory identity mismatch")
    batch = pit.load_source_batch(tdx_source_batch_id)
    matches = [
        item
        for item in batch.dependencies
        if item.dataset == "us_security_master_current"
        and item.source_id == "tdx_us_security_master_current"
    ]
    if len(matches) != 1:
        raise ValueError("TDX source batch must contain one current US master")
    dependency = matches[0]
    current_codes = tdx_current_codes(
        pit.object_path(dependency.object_sha256).read_bytes()
    )
    holdings = pd.read_parquet(
        source / "fund_holdings_observed_candidate.parquet"
    )
    current = holdings.loc[
        holdings["source_id"].astype(str).eq("ishares_ivv_holdings")
        & holdings["signal_eligible"].fillna(False).astype(bool)
        & holdings["ticker"].notna()
    ].copy()
    if current.empty:
        raise ValueError("normalization contains no current observed IVV equities")
    observed = pd.to_datetime(current["observed_at"], utc=True, errors="coerce")
    if observed.isna().any():
        raise ValueError("current iShares holdings contain an invalid observed_at")
    latest_observed_at = observed.max()
    current = current.loc[observed.eq(latest_observed_at)].copy()
    source_hashes = sorted(set(current["content_sha256"].astype(str)))
    if len(source_hashes) != 1:
        raise ValueError("latest iShares observation is not uniquely evidenced")
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for row in current.to_dict(orient="records"):
        ticker = str(row["ticker"]).upper()
        try:
            vendor_code = resolve_current_tdx_alias(ticker, current_codes)
        except ValueError as exc:
            failures.append({"ticker": ticker, "reason": str(exc)})
            vendor_code = ""
        rows.append(
            {
                "holding_candidate_id": str(row["holding_candidate_id"]),
                "ticker": ticker,
                "vendor_code": vendor_code,
                "issuer_name": str(row.get("issuer_name") or ""),
                "as_of_date": str(row.get("as_of_date") or ""),
                "ishares_source_sha256": str(row.get("content_sha256") or ""),
                "tdx_source_sha256": dependency.object_sha256,
                "tdx_name": current_codes.get(vendor_code, ""),
                "status": "VERIFIED_CURRENT_ALIAS" if vendor_code else "UNRESOLVED",
                "historical_membership_authority": False,
                "historical_alias_authority": False,
            }
        )
    frame = pd.DataFrame(rows).sort_values(
        ["vendor_code", "holding_candidate_id"], kind="stable"
    )
    duplicate_vendor = frame.loc[
        frame["vendor_code"].ne("") & frame["vendor_code"].duplicated(False)
    ]
    if not duplicate_vendor.empty:
        failures.extend(
            {"ticker": str(row.ticker), "reason": "DUPLICATE_CURRENT_VENDOR_ALIAS"}
            for row in duplicate_vendor.itertuples(index=False)
        )
    status = "CROSSCHECK_READY" if not failures else "DATA_BLOCKED"
    manifest = {
        "format_version": FORMAT_VERSION,
        "normalization_id": normalization_id,
        "normalization_manifest_sha256": sha256_file(normalization_manifest_path),
        "tdx_source_batch_id": batch.batch_id,
        "tdx_source_sha256": dependency.object_sha256,
        "status": status,
        "row_count": len(frame),
        "verified_count": int((frame["status"] == "VERIFIED_CURRENT_ALIAS").sum()),
        "selected_observed_at": latest_observed_at.isoformat(),
        "selected_ishares_source_sha256": source_hashes[0],
        "failures": failures,
        "current_only": True,
        "historical_membership_authority": False,
        "historical_alias_authority": False,
    }
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"alias cross-check output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        frame.to_parquet(staging / "current_alias_crosscheck.parquet", index=False)
        manifest["artifact_sha256"] = sha256_file(
            staging / "current_alias_crosscheck.parquet"
        )
        manifest["crosscheck_id"] = sha256_json(manifest)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return CurrentAliasCrosscheckResult(output, manifest)


__all__ = ["CurrentAliasCrosscheckResult", "crosscheck_current_aliases"]
