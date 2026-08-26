"""Admit a blocked membership event whose identity is anchored to an approved
corporate action (frozen rule A' decision D1).

Admission requires, for every event:
- the event row exists in the frozen unresolved-events package (hash-checked
  against its manifest);
- an approved corporate-action package (hash-checked) contains an approved
  REORGANIZATION/STOCK_MERGER/TICKER_CHANGE/RENAME row whose
  ``security_id`` becomes the admitted event's stable ID;
- that action's frozen SEC evidence object names the event's announcement
  ticker and both entity names verbatim.

The output directory carries the base review inputs byte-for-byte except
``membership_events.parquet`` (base rows + admitted rows) and adds an
admission manifest with full provenance.
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
from research_platform.us_pit.action_review import _source_plain_text  # noqa: E402
from research_platform.us_pit.hashing import sha256_file, sha256_json  # noqa: E402
from research_platform.us_pit.store import USPITStore  # noqa: E402

BINDING_ACTION_TYPES = {"REORGANIZATION", "STOCK_MERGER", "TICKER_CHANGE", "RENAME"}


def _load_package(root: Path, actions_name: str, manifest_key: str) -> tuple[dict, pd.DataFrame]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    path = root / actions_name
    if not path.is_file() or manifest.get(manifest_key) != sha256_file(path):
        raise SystemExit(f"integrity failure for {path}")
    return manifest, pd.read_parquet(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", default="data/us_pit")
    parser.add_argument("--base-review-dir", required=True)
    parser.add_argument("--unresolved-events", required=True)
    parser.add_argument("--unresolved-manifest", required=True)
    parser.add_argument("--approved-package", action="append", required=True,
                        help="approved corporate-action packages whose predecessor IDs may anchor events")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    store = USPITStore(args.store_root)
    base = Path(args.base_review_dir).resolve()
    output = Path(args.output_dir).resolve()
    if output.exists():
        raise SystemExit(f"output dir already exists: {output}")

    unresolved_manifest = json.loads(
        Path(args.unresolved_manifest).read_text(encoding="utf-8")
    )
    unresolved_path = Path(args.unresolved_events)
    artifacts = unresolved_manifest.get("artifacts", {})
    expected = (
        artifacts.get("unresolved_membership_events", {}).get("sha256")
        or unresolved_manifest.get("unresolved_membership_events_sha256")
        or unresolved_manifest.get("artifact_sha256")
    )
    if not expected or expected != sha256_file(unresolved_path):
        raise SystemExit("unresolved events package failed integrity policy")

    approved_frames: list[pd.DataFrame] = []
    decisions_frames: list[pd.DataFrame] = []
    for package in args.approved_package:
        root = Path(package).resolve()
        manifest, actions = _load_package(root, "corporate_actions.parquet",
                                          "corporate_actions_sha256")
        _, decisions = _load_package(root, "review_decisions.parquet",
                                     "review_decisions_sha256")
        approved_frames.append(actions)
        decisions_frames.append(decisions)

    unresolved = pd.read_parquet(unresolved_path)

    def evidence_text(digest: str) -> str:
        path = store.object_path(digest.lower())
        if not path.is_file():
            raise SystemExit(f"frozen evidence object missing: {digest}")
        return _source_plain_text(path.read_bytes())

    all_actions = pd.concat(approved_frames, ignore_index=True)

    def decisions_for(action_id: str) -> pd.Series | None:
        for frame in decisions_frames:
            hit = frame.loc[frame["action_id"].astype(str).eq(action_id)]
            if len(hit):
                return hit.iloc[0]
        return None

    admissions: list[dict] = []
    admitted_rows: list[dict] = []
    for _, event in unresolved.iterrows():
        ticker = str(event.get("ticker_at_announcement", "") or "").strip().upper()
        if not ticker:
            continue
        binding = None
        for _, action in all_actions.iterrows():
            kind = str(action.get("action_type", "")).strip().upper()
            if kind not in BINDING_ACTION_TYPES:
                continue
            decision_row = decisions_for(str(action["action_id"]))
            if decision_row is None:
                continue
            excerpt = " ".join(
                str(decision_row.get("evidence_excerpt", "") or "").split()
            ).upper()
            predecessor_name = " ".join(
                str(decision_row.get("predecessor_name", "") or "").split()
            ).upper()
            successor_name = " ".join(
                str(decision_row.get("successor_name", "") or "").split()
            ).upper()
            if not excerpt or ticker not in excerpt:
                continue
            if predecessor_name and predecessor_name not in excerpt:
                continue
            if successor_name and successor_name not in excerpt:
                continue
            object_text = evidence_text(
                str(action.get("evidence_sha256", ""))
            ).upper()
            if excerpt[:80] not in object_text:
                continue
            binding = action
            break
        if binding is None:
            continue
        row = event.to_dict()
        row["security_id"] = str(binding["security_id"])
        row["approved"] = True
        row["review_note"] = (
            f"D1 action-anchored admission via action "
            f"{str(binding['action_id'])[:12]}…; bound to {row['security_id']}"
        )
        admitted_rows.append(row)
        admissions.append(
            {
                "event_id": str(event["event_id"]),
                "ticker_at_announcement": ticker,
                "bound_security_id": row["security_id"],
                "via_action_id": str(binding["action_id"]),
                "event_evidence_sha256": str(event.get("evidence_sha256", "")),
            }
        )
    if not admissions:
        raise SystemExit("no events satisfied the action-anchored admission contract")

    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        for path in sorted(base.iterdir()):
            if path.name == "membership_events.parquet":
                continue
            if path.is_file():
                shutil.copyfile(path, staging / path.name)
        base_events = pd.read_parquet(base / "membership_events.parquet")
        combined = pd.concat([base_events, pd.DataFrame(admitted_rows)], ignore_index=True)
        combined.to_parquet(staging / "membership_events.parquet", index=False)
        manifest = {
            "format_version": "us-pit-action-anchored-event-admission-v1",
            "base_review_dir": str(base),
            "base_membership_events_sha256": sha256_file(base / "membership_events.parquet"),
            "unresolved_events_sha256": sha256_file(unresolved_path),
            "approved_packages": [
                {"path": p, "manifest_sha256": sha256_file(Path(p) / "manifest.json")}
                for p in args.approved_package
            ],
            "admissions": admissions,
            "merged_event_count": int(len(combined)),
            "merged_membership_events_sha256": sha256_file(
                staging / "membership_events.parquet"
            ),
        }
        manifest["admission_id"] = sha256_json(manifest)
        (staging / "admission_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(json.dumps({
        "status": "ADMITTED",
        "path": str(output),
        "admission_id": manifest["admission_id"],
        "admitted": len(admissions),
    }, indent=2))


if __name__ == "__main__":
    main()
