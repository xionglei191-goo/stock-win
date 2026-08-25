"""Merge multiple immutable US-PIT corporate-action approval packages into one
review-input workspace.

Every source package is re-validated against its own hash-bound manifest before
any row is copied.  All non-action review inputs are carried over byte-for-byte
from the base review directory.  The merge itself is recorded in a manifest so
the resulting directory has explicit, reproducible provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pandas as pd

import sys

sys.path.insert(0, ".")

from research_platform.us_pit.hashing import canonical_json_bytes, sha256_json

REQUIRED_PACKAGE_FILES = (
    "manifest.json",
    "corporate_actions.parquet",
    "review_decisions.parquet",
)
COPIED_INPUTS = (
    "identity_review.parquet",
    "membership_events.parquet",
    "session_exceptions.parquet",
    "lifecycle_reconciliations.parquet",
    "xnys_calendar.parquet",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package(root: Path) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    for name in REQUIRED_PACKAGE_FILES:
        if not (root / name).is_file():
            raise SystemExit(f"approval package {root} is missing {name}")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actions = pd.read_parquet(root / "corporate_actions.parquet")
    decisions = pd.read_parquet(root / "review_decisions.parquet")
    if manifest.get("status") != "REVIEW_APPROVED":
        raise SystemExit(f"package {root} is not REVIEW_APPROVED")
    if manifest.get("direct_build_allowed") is not False:
        raise SystemExit(f"package {root} allows direct build")
    expected_id = sha256_json(
        {k: v for k, v in manifest.items() if k != "approval_id"}
    )
    if manifest.get("approval_id") != expected_id:
        raise SystemExit(f"package {root} failed approval-id integrity")
    if manifest.get("corporate_actions_sha256") != sha256_file(
        root / "corporate_actions.parquet"
    ):
        raise SystemExit(f"package {root} corporate-actions digest mismatch")
    if manifest.get("review_decisions_sha256") != sha256_file(
        root / "review_decisions.parquet"
    ):
        raise SystemExit(f"package {root} review-decisions digest mismatch")
    if not str(manifest.get("source_batch_id") or ""):
        raise SystemExit(f"package {root} lacks a source batch binding")
    notes = decisions.loc[
        decisions["action_id"].fillna("").astype(str).ne("")
    ][["action_id", "review_note"]]
    actions = actions.merge(notes, on="action_id", how="left", validate="one_to_one")
    if actions["review_note"].fillna("").astype(str).str.strip().eq("").any():
        raise SystemExit(f"package {root} lacks a note for some action")
    actions["approved"] = True
    return manifest, actions, decisions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-review-dir", required=True)
    parser.add_argument("--approved-package", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    base = Path(args.base_review_dir).resolve()
    if not base.is_dir():
        raise SystemExit(f"base review dir not found: {base}")
    packages = [Path(item).resolve() for item in args.approved_package]

    merged_frames: list[pd.DataFrame] = []
    package_records = []
    seen_action_ids: set[str] = set()
    for package in packages:
        manifest, actions, _ = validate_package(package)
        duplicates = seen_action_ids.intersection(actions["action_id"].astype(str))
        if duplicates:
            raise SystemExit(f"duplicate action ids across packages: {sorted(duplicates)}")
        seen_action_ids.update(actions["action_id"].astype(str))
        merged_frames.append(actions)
        package_records.append(
            {
                "path": str(package),
                "manifest_sha256": sha256_file(package / "manifest.json"),
                "approval_id": manifest.get("approval_id"),
                "proposal_sha256": manifest.get("proposal_sha256"),
                "source_batch_id": manifest.get("source_batch_id"),
                "row_count": int(len(actions)),
            }
        )

    merged = pd.concat(merged_frames, ignore_index=True)
    output = Path(args.output_dir).resolve()
    if output.exists():
        raise SystemExit(f"output dir already exists: {output}")
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir(parents=True)

    try:
        for name in COPIED_INPUTS:
            source = base / name
            if source.is_file():
                shutil.copyfile(source, staging / name)
        # Any extra artifact tables from the base review dir are carried over
        # unchanged except corporate_actions, which is rebuilt from packages.
        for path in sorted(base.iterdir()):
            if path.name in COPIED_INPUTS or path.name == "corporate_actions.parquet":
                continue
            if path.is_file() and path.suffix == ".parquet":
                shutil.copyfile(path, staging / path.name)
        merged.to_parquet(staging / "corporate_actions.parquet", index=False)
        manifest = {
            "format_version": "us-pit-merged-action-approvals-v1",
            "base_review_dir": str(base),
            "base_review_manifest_sha256": (
                sha256_file(base / "review_template_manifest.json")
                if (base / "review_template_manifest.json").is_file()
                else None
            ),
            "packages": package_records,
            "merged_row_count": int(len(merged)),
            "merged_corporate_actions_sha256": sha256_file(
                staging / "corporate_actions.parquet"
            ),
            "carried_inputs": {
                name: sha256_file(staging / name)
                for name in COPIED_INPUTS
                if (staging / name).is_file()
            },
        }
        manifest["merge_id"] = sha256_json(manifest)
        (staging / "merge_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(json.dumps({
        "status": "MERGED",
        "path": str(output),
        "merge_id": manifest["merge_id"],
        "rows": int(len(merged)),
        "packages": len(package_records),
    }, indent=2))


if __name__ == "__main__":
    main()
