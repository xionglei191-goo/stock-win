from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")

from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file, sha256_json
from research_platform.us_pit.store import USPITStore


AMENDMENTS = {
    "7f98c9e95873f75251fd8c7ea3d2b702c6506f4fdeb85feea2c3a193d907f327": (
        "bab42dac5a0a22803c80860994791dda3084c079f05911c901c5063628ad848a",
        "SEC 8-K accepted 2021-12-02 proves the announced FRT UPREIT holdco reorganization and one-for-one conversion before the effective session.",
    ),
    "8074b87e947659647e52ed60c2195bb1f5254e1f6158e516c071a8afced360b4": (
        "77c2ed9591502fb13ab140f076f9f9c477dcb765e3926e1fcf158f35251c581c",
        "SEC successor-issuer filing accepted 2024-10-01 09:21:52 ET proves each Old BlackRock share converted into one New BlackRock share before the 09:30 ET effective time.",
    ),
    "c4a7e86c2d85f511347f28fcb36d30a42c904bad05c6cfb18cd68abbdd3f32ce": (
        "2759fdba198abf1db8a2c7022e51a9d6e94de5ea4f5ade8aa60ba4c207bad0a3",
        "SEC 8-K accepted 2022-03-21 proves one EMBC share per five BDX shares and regular-way EMBC trading on Nasdaq from 2022-04-01; basis remains causally derived under preregistration v2.",
    ),
    "5beb6710be6aa2a7916bb04f59456c3da226cb60370c0ae3ad45fa400133eb81": (
        "61d8c065b1aa134287dbe11cfdbfab2d6aa2774f87d134e9d48cb7e73821d60b",
        "SEC 8-K accepted 2025-06-30 10:05:24 UTC proves completion, RAL NYSE trading, and one RAL share per three FTV shares before the decision close; basis remains causally derived under preregistration v2.",
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an immutable hash-bound action evidence amendment.")
    parser.add_argument("--store", default="data/us_pit")
    parser.add_argument("--base-review-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-batch", required=True)
    parser.add_argument("--approved-by", default="local-user")
    parser.add_argument("--approved-at", required=True)
    args = parser.parse_args()

    approved_at = datetime.fromisoformat(args.approved_at.replace("Z", "+00:00"))
    if approved_at.tzinfo is None:
        raise ValueError("approved-at must be timezone-aware")
    approved_at_text = approved_at.astimezone(timezone.utc).isoformat()
    source = Path(args.base_review_dir).resolve()
    target = Path(args.output_dir).resolve()
    if not source.is_dir() or target.exists() or target == source or source in target.parents:
        raise ValueError("base/output review directories are invalid or output already exists")

    source_actions_path = source / "corporate_actions.parquet"
    actions = pd.read_parquet(source_actions_path)
    dependencies = {
        (item.source_id, item.dataset, item.object_sha256): item
        for item in USPITStore(args.store).load_source_batch(args.source_batch).dependencies
    }
    changes: list[dict[str, str]] = []
    for action_id, (digest, note) in AMENDMENTS.items():
        mask = actions["action_id"].astype(str).eq(action_id)
        if int(mask.sum()) != 1:
            raise ValueError(f"review action is missing or ambiguous: {action_id}")
        key = ("sec_reviewed_corporate_action", "corporate_actions", digest)
        dependency = dependencies.get(key)
        if dependency is None:
            raise ValueError(f"source batch lacks promoted action evidence: {action_id}")
        metadata = dict(dependency.metadata)
        if (
            metadata.get("action_id") != action_id
            or metadata.get("human_terms_reviewed") is not True
            or metadata.get("approved_by") != args.approved_by
            or metadata.get("approved_at") != args.approved_at
        ):
            raise ValueError(f"promoted action approval does not match package: {action_id}")
        prior_digest = str(actions.loc[mask, "evidence_sha256"].iloc[0])
        actions.loc[mask, "source_id"] = "sec_reviewed_corporate_action"
        actions.loc[mask, "evidence_sha256"] = digest
        actions.loc[mask, "review_note"] = note
        changes.append(
            {
                "action_id": action_id,
                "prior_evidence_sha256": prior_digest,
                "evidence_sha256": digest,
                "source_id": "sec_reviewed_corporate_action",
                "review_note_sha256": sha256_json(note),
            }
        )

    target.mkdir(parents=True, exist_ok=False)
    try:
        for path in source.iterdir():
            if path.is_file():
                shutil.copy2(path, target / path.name)
        actions.to_parquet(target / "corporate_actions.parquet", index=False)
        manifest = {
            "format_version": "us-pit-action-evidence-amendment-v1",
            "base_review_dir": str(source),
            "base_corporate_actions_sha256": sha256_file(source_actions_path),
            "corporate_actions_sha256": sha256_file(target / "corporate_actions.parquet"),
            "source_batch_id": args.source_batch,
            "changes": sorted(changes, key=lambda item: item["action_id"]),
            "approved_by": args.approved_by,
            "approved_at": approved_at_text,
        }
        manifest["package_id"] = sha256_json(manifest)
        (target / "corporate_action_review_amendment_manifest.json").write_bytes(
            canonical_json_bytes(manifest)
        )
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
