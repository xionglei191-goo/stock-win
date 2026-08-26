"""Explicitly approve reviewed identity rows that satisfy every frozen
consistency precondition.

This script never fabricates identity data.  Every row's identity fields were
already frozen by the official normalization (iShares IVV API plus SEC N-PORT,
hash-bound via the normalization manifest).  Approval here asserts that a
reviewer checked, per row:

1. ``suggested_security_id`` equals ``us_isin_`` + lowercased ISIN;
2. an official identifier (ISIN or CUSIP) is present;
3. an official issuer name is present;
4. no unresolved normalization issue is attached;
5. the row's source is one of the allow-listed official sources.

Rows failing any condition remain explicitly unapproved.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pandas as pd

sys.path.insert(0, ".")
from research_platform.us_pit.hashing import sha256_file, sha256_json  # noqa: E402

ALLOWED_SOURCES = {"sec_nport_ivv", "ishares_ivv_holdings_api"}

NOTE_TEMPLATE = (
    "Identity approved from official normalization {normalization_id}: "
    "source={source}, as_of={as_of}, isin={isin}, cusip={cusip}."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", required=True)
    parser.add_argument("--normalization-id", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    base = Path(args.review_dir).resolve()
    output = Path(args.output_dir).resolve()
    if output.exists():
        raise SystemExit(f"output dir already exists: {output}")

    frame = pd.read_parquet(base / "identity_review.parquet")
    required = {
        "suggested_security_id", "issuer_name", "isin", "cusip",
        "source_id", "resolved_issue_ids", "approved",
    }
    missing = required - set(frame.columns)
    if missing:
        raise SystemExit(f"identity_review missing columns: {sorted(missing)}")

    approved_flags = []
    notes = []
    counts = {"approved": 0, "rejected": 0}
    reject_reasons: dict[str, int] = {}
    for _, row in frame.iterrows():
        reasons = []
        security_id = str(row.get("suggested_security_id") or "").strip()
        isin = str(row.get("isin") or "").strip()
        cusip = str(row.get("cusip") or "").strip()
        issuer = str(row.get("issuer_name") or "").strip()
        source = str(row.get("source_id") or "").strip()
        issues = str(row.get("resolved_issue_ids") or "").strip()

        expected = "us_isin_" + isin.lower() if isin else ""
        if not security_id:
            reasons.append("no_stable_id")
        elif isin and security_id != expected:
            reasons.append("stable_id_isin_mismatch")
        if not isin and not cusip:
            reasons.append("no_official_identifier")
        if not issuer:
            reasons.append("no_issuer_name")
        if issues:
            reasons.append("unresolved_issues_present")
        if source not in ALLOWED_SOURCES:
            reasons.append(f"source_not_allowlisted:{source}")

        if reasons:
            approved_flags.append(False)
            notes.append("")
            counts["rejected"] += 1
            for reason in reasons:
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            continue

        approved_flags.append(True)
        notes.append(
            NOTE_TEMPLATE.format(
                normalization_id=args.normalization_id,
                source=source,
                as_of=str(row.get("as_of_date") or ""),
                isin=isin,
                cusip=cusip,
            )
        )
        counts["approved"] += 1

    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        for path in sorted(base.iterdir()):
            if path.name == "identity_review.parquet":
                continue
            if path.is_file():
                shutil.copyfile(path, staging / path.name)
        approved_frame = frame.copy()
        approved_frame["approved"] = approved_flags
        approved_frame["review_note"] = notes
        approved_frame.to_parquet(staging / "identity_review.parquet", index=False)
        manifest = {
            "format_version": "us-pit-identity-batch-approval-v1",
            "base_review_dir": str(base),
            "base_identity_review_sha256": sha256_file(base / "identity_review.parquet"),
            "normalization_id": args.normalization_id,
            "counts": counts,
            "reject_reasons": reject_reasons,
            "approved_identity_review_sha256": sha256_file(
                staging / "identity_review.parquet"
            ),
        }
        manifest["approval_id"] = sha256_json(manifest)
        (staging / "identity_approval_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(json.dumps({
        "status": "IDENTITY_ROWS_APPROVED",
        "path": str(output),
        "approval_id": manifest["approval_id"],
        **counts,
    }, indent=2))


if __name__ == "__main__":
    main()
