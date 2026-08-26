"""Bind issuer identities (us_issuer_cik_*) to reviewed identity rows using
the frozen SEC company index (ticker->CIK->title), the official current SEC
issuer list already captured in the PIT store.

Matching contract (frozen):
- canonical form: uppercase, strip punctuation, drop corporate-suffix and
  US state tokens;
- a security binds only when its normalized issuer name matches exactly one
  non-ambiguous canonical title in the index;
- the row keeps its frozen identity fields; only ``issuer_id`` is filled,
  with a review note citing the index object digest.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pandas as pd

sys.path.insert(0, ".")
from research_platform.us_pit.hashing import sha256_file, sha256_json  # noqa: E402
from research_platform.us_pit.store import USPITStore  # noqa: E402

SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|cos|co|company|companies|plc|ltd|"
    r"limited|the|holdings|holding|sa|nv|ag|ny|de|md|nj|va|pa)\b",
    re.I,
)


def canon(value: str) -> str:
    text = str(value).replace("&", " and ").replace("/", " ")
    text = re.sub(r"[^A-Za-z0-9\s]", " ", text)
    tokens = [t for t in re.split(r"\s+", text.upper()) if t]
    return " ".join(t for t in tokens if not SUFFIX.fullmatch(t))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", default="data/us_pit")
    parser.add_argument("--review-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    store = USPITStore(args.store_root)
    base = Path(args.review_dir).resolve()
    output = Path(args.output_dir).resolve()
    if output.exists():
        raise SystemExit(f"output dir already exists: {output}")

    index_obj = None
    for batch in store.list_source_batches():
        for dependency in batch.dependencies:
            if dependency.source_id == "sec_company_identity_index":
                index_obj = dependency
    if index_obj is None:
        raise SystemExit("frozen sec_company_identity_index not found")
    payload = json.loads(store.object_path(index_obj.object_sha256).read_bytes())
    index = pd.DataFrame([v for _, v in payload.items()])
    index["canon"] = index["title"].astype(str).map(canon)
    uniqueness = index.groupby("canon")["cik_str"].nunique()
    ambiguous = set(uniqueness[uniqueness > 1].index)
    lookup = {
        title: cik
        for title, cik in zip(index["canon"], index["cik_str"])
        if title not in ambiguous
    }
    index_digest = index_obj.object_sha256

    frame = pd.read_parquet(base / "identity_review.parquet")
    filled = 0
    skipped: dict[str, int] = {}
    issuer_ids: list[str] = []
    notes: list[str] = []
    for _, row in frame.iterrows():
        current = str(row.get("issuer_id") or "").strip()
        if current:
            issuer_ids.append(current)
            notes.append(str(row.get("review_note") or ""))
            continue
        name = str(row.get("issuer_name") or "")
        key = canon(name)
        cik = lookup.get(key) if key else None
        if cik is None:
            reason = "ambiguous_name" if key in ambiguous else "no_index_match"
            skipped[reason] = skipped.get(reason, 0) + 1
            issuer_ids.append("")
            notes.append(str(row.get("review_note") or ""))
            continue
        issuer_ids.append(f"us_issuer_cik_{int(cik):010d}")
        notes.append(
            f"Issuer CIK bound from frozen SEC company index "
            f"(sha256 {index_digest[:16]}…, canon={key!r})."
        )
        filled += 1

    # Propagate bindings across every row of the same stable security ID:
    # one ISIN can only have one issuer. Conflicting bindings abort loudly.
    by_security: dict[str, set[str]] = {}
    for idx_value, row in frame.iterrows():
        sid = str(row.get("suggested_security_id") or "").strip()
        value = str(issuer_ids[idx_value] or "").strip()
        if not sid:
            continue
        if value:
            by_security.setdefault(sid, set()).add(value)
    propagated = 0
    for idx_value, row in frame.iterrows():
        sid = str(row.get("suggested_security_id") or "").strip()
        if not sid or sid not in by_security:
            continue
        values = by_security[sid]
        if len(values) > 1:
            raise SystemExit(
                f"conflicting issuer bindings for {sid}: {sorted(values)}"
            )
        value = next(iter(values))
        if str(issuer_ids[idx_value] or "").strip() != value:
            issuer_ids[idx_value] = value
            propagated += 1

    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        for path in sorted(base.iterdir()):
            if path.name == "identity_review.parquet":
                continue
            if path.is_file():
                shutil.copyfile(path, staging / path.name)
        frame["issuer_id"] = issuer_ids
        frame["review_note"] = notes
        frame.to_parquet(staging / "identity_review.parquet", index=False)
        manifest = {
            "format_version": "us-pit-issuer-evidence-binding-v1",
            "evidence_source": "sec_company_identity_index",
            "evidence_object_sha256": index_digest,
            "base_review_dir": str(base),
            "base_identity_review_sha256": sha256_file(
                base / "identity_review.parquet"
            ),
            "securities_bound": filled,
            "rows_propagated": propagated,
            "skipped": skipped,
            "bound_identity_review_sha256": sha256_file(
                staging / "identity_review.parquet"
            ),
        }
        manifest["binding_id"] = sha256_json(manifest)
        (staging / "issuer_binding_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(json.dumps({
        "status": "ISSUER_IDS_BOUND",
        "path": str(output),
        "binding_id": manifest["binding_id"],
        "bound": filled,
        "skipped": skipped,
    }, indent=2))


if __name__ == "__main__":
    main()
