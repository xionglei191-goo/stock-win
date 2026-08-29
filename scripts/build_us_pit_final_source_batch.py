from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, ".")

from research_platform.us_pit.hashing import sha256_file, sha256_json
from research_platform.us_pit.models import SourceDependency, SourceRole
from research_platform.us_pit.store import USPITStore


ACTION_EVIDENCE = (
    {
        "action_id": "7f98c9e95873f75251fd8c7ea3d2b702c6506f4fdeb85feea2c3a193d907f327",
        "object_sha256": "bab42dac5a0a22803c80860994791dda3084c079f05911c901c5063628ad848a",
        "published_at": "2021-12-02T21:09:06+00:00",
        "phrases": (
            "automatically convert, on a one-for-one basis",
            "FRT Holdco REIT",
        ),
    },
    {
        "action_id": "8074b87e947659647e52ed60c2195bb1f5254e1f6158e516c071a8afced360b4",
        "object_sha256": "77c2ed9591502fb13ab140f076f9f9c477dcb765e3926e1fcf158f35251c581c",
        "published_at": "2024-10-01T13:21:52+00:00",
        "phrases": (
            "converted automatically into one share of common stock",
            "New BlackRock",
            "Old BlackRock",
        ),
    },
    {
        "action_id": "c4a7e86c2d85f511347f28fcb36d30a42c904bad05c6cfb18cd68abbdd3f32ce",
        "object_sha256": "2759fdba198abf1db8a2c7022e51a9d6e94de5ea4f5ade8aa60ba4c207bad0a3",
        "published_at": "2022-03-21T20:17:00+00:00",
        "phrases": (
            "one share of embecta common stock for every five shares",
            "under the ticker \u201cEMBC\u201d",
            "Nasdaq Global Select Market",
        ),
    },
    {
        "action_id": "5beb6710be6aa2a7916bb04f59456c3da226cb60370c0ae3ad45fa400133eb81",
        "object_sha256": "61d8c065b1aa134287dbe11cfdbfab2d6aa2774f87d134e9d48cb7e73821d60b",
        "published_at": "2025-06-30T10:05:24+00:00",
        "phrases": (
            "one share of Ralliant common stock for every three shares",
            "trading under the symbol \u201cRAL\u201d on the New York Stock Exchange",
        ),
    },
)


def _normalized_text(payload: bytes) -> str:
    raw = payload.decode("utf-8", errors="replace")
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consolidate reviewed lineage, unique fee evidence, and promoted action evidence."
    )
    parser.add_argument("--store", default="data/us_pit")
    parser.add_argument("--reviewed-workspace", required=True)
    parser.add_argument("--fee-source-batch", required=True)
    parser.add_argument("--approved-by", default="local-user")
    parser.add_argument("--approved-at", required=True)
    args = parser.parse_args()

    store = USPITStore(args.store)
    workspace = Path(args.reviewed_workspace).resolve()
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch_ids = manifest.get("source_batch_ids")
    if not isinstance(batch_ids, list) or not batch_ids:
        raise ValueError("reviewed workspace has no source lineage")

    unique: dict[tuple[str, str, str | None, str], SourceDependency] = {}
    for batch_id in batch_ids:
        for item in store.load_source_batch(str(batch_id)).dependencies:
            if item.dataset in {"regulatory_fee_sec", "regulatory_fee_finra"}:
                continue
            unique[(item.source_id, item.dataset, item.as_of_date, item.object_sha256)] = item

    fee_dependencies = [
        item
        for item in store.load_source_batch(args.fee_source_batch).dependencies
        if item.dataset in {"regulatory_fee_sec", "regulatory_fee_finra"}
    ]
    if not fee_dependencies:
        raise ValueError("fee source batch has no regulatory fee evidence")
    for item in fee_dependencies:
        unique[(item.source_id, item.dataset, item.as_of_date, item.object_sha256)] = item

    all_batches = store.list_source_batches()
    for specification in ACTION_EVIDENCE:
        digest = specification["object_sha256"]
        object_path = store.object_path(digest)
        if not object_path.is_file() or sha256_file(object_path) != digest:
            raise ValueError(f"action evidence object is missing or corrupt: {digest}")
        payload = object_path.read_bytes()
        normalized = _normalized_text(payload)
        missing = [phrase for phrase in specification["phrases"] if phrase not in normalized]
        if missing:
            raise ValueError(
                f"action evidence does not prove reviewed terms for {specification['action_id']}: {missing}"
            )
        candidates = [
            item
            for batch in all_batches
            for item in batch.dependencies
            if item.object_sha256 == digest
            and item.url.startswith("https://www.sec.gov/Archives/")
            and item.published_at is not None
        ]
        if not candidates:
            raise ValueError(f"no SEC dependency describes action evidence {digest}")
        original = sorted(candidates, key=lambda item: (item.dataset, item.source_version))[0]
        metadata = {
            **dict(original.metadata),
            "action_id": specification["action_id"],
            "publication_time_from_payload": True,
            "accepted_at_verified_in_payload": True,
            "accepted_at": specification["published_at"],
            "human_terms_reviewed": True,
            "required_phrases": list(specification["phrases"]),
            "required_phrases_sha256": sha256_json(list(specification["phrases"])),
            "approved_by": args.approved_by,
            "approved_at": args.approved_at,
            "signal_eligible": True,
            "eligible_for_historical_signal": True,
        }
        promoted = replace(
            original,
            source_id="sec_reviewed_corporate_action",
            source_version="official-action-review-source-v2",
            role=SourceRole.SIGNAL_INPUT,
            dataset="corporate_actions",
            published_at=specification["published_at"],
            metadata=metadata,
        )
        unique[(promoted.source_id, promoted.dataset, promoted.as_of_date, digest)] = promoted

    batch = store.write_source_batch(unique.values())
    print(
        json.dumps(
            {
                "batch_id": batch.batch_id,
                "dependency_count": len(batch.dependencies),
                "reviewed_manifest_sha256": sha256_file(manifest_path),
                "fee_source_batch_id": args.fee_source_batch,
                "promoted_action_evidence": [
                    {"action_id": item["action_id"], "object_sha256": item["object_sha256"]}
                    for item in ACTION_EVIDENCE
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
