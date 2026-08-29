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


CBOE_SECURITY_ID = "us_isin_us12503m1080"
GE_PREDECESSOR_ID = "us_isin_us3696041033"
GE_SUCCESSOR_ID = "us_isin_us3696043013"
GE_ACTION_ID = "8a1c8f85e361ddb53bf0465c62d2e4a92873b894e927a8f0cbd376a991942cb7"
EMBC_SECURITY_ID = "us_isin_us29082k1051"
EMBC_ACTION_ID = "c4a7e86c2d85f511347f28fcb36d30a42c904bad05c6cfb18cd68abbdd3f32ce"
SPLIT_LINEAGES = (
    (GE_ACTION_ID, GE_PREDECESSOR_ID, GE_SUCCESSOR_ID, "GE", "XNYS", "2018-08-17", "2021-07-30", "2021-08-02"),
    ("ec16f6f52c439bf1454b0693bd586aed95f97db0c0b3bceac969c329230f4550", "us_isin_us2166484020", "us_isin_us2166485019", "COO", "XNAS", "2021-08-31", "2024-02-16", "2024-02-20"),
    ("a06558591f02ab5ad13ae1640dd7ef6cf5eecad3043760e07c28038a4f2823be", "us_isin_us0404131064", "us_isin_us0404132054", "ANET", "XNYS", "2021-08-31", "2024-12-03", "2024-12-04"),
    ("0440086ada570c39a9f99b95e9efd8f4065a48c9ad7783bc24c14c08a1fc9b88", "us_isin_us5128071082", "us_isin_us5128073062", "LRCX", "XNAS", "2021-08-31", "2024-10-02", "2024-10-03"),
    ("d59ef8f9fcf9d811d97520c51a677ad7aec2ae4956ab97eb1a7150393286d425", "us_isin_us86800u1043", "us_isin_us86800u3023", "SMCI", "XNAS", "2024-03-28", "2024-09-30", "2024-10-01"),
    ("694d2f594472c2795e8cf4fdea404f9031893422c54cd30f17f5e1175bcf0a7c", "us_isin_je00bj1f3079", "us_isin_je00bv7dq550", "AMCR", "XNYS", "2021-08-31", "2026-01-14", "2026-01-15"),
)


def _row(
    *,
    binding_type: str,
    security_id: str,
    ticker: str,
    exchange_mic: str,
    valid_from: str,
    valid_to: str | None,
    action_id: str,
    evidence_source_id: str,
    evidence_sha256: str,
    note: str,
    approved_by: str,
    approved_at: str,
) -> dict[str, object]:
    candidate = {
        "binding_type": binding_type,
        "security_id": security_id,
        "ticker": ticker,
        "vendor_code": f"{ticker}.US",
        "exchange_mic": exchange_mic,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "action_id": action_id,
        "evidence_source_id": evidence_source_id,
        "evidence_sha256": evidence_sha256,
    }
    alias_review_id = sha256_json(
        {"format_version": "us-pit-listing-alias-candidate-v1", **candidate}
    )
    identity = {
        "format_version": "us-pit-listing-alias-review-v1",
        "alias_review_id": alias_review_id,
        **candidate,
        "approved": True,
        "review_note": note,
        "approved_by": approved_by,
        "approved_at": approved_at,
    }
    return {**identity, "approval_id": sha256_json(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build hash-bound CBOE, split-lineage, and EMBC listing alias review rows."
        )
    )
    parser.add_argument("--store", default="data/us_pit")
    parser.add_argument("--base-review-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cboe-source-batch", required=True)
    parser.add_argument("--action-source-batch", required=True)
    parser.add_argument("--approved-by", default="local-user")
    parser.add_argument("--approved-at")
    args = parser.parse_args()

    approved_at = (
        datetime.now(timezone.utc)
        if args.approved_at is None
        else datetime.fromisoformat(args.approved_at.replace("Z", "+00:00"))
    )
    if approved_at.tzinfo is None:
        raise ValueError("approved-at must be timezone-aware")
    approved_at_text = approved_at.astimezone(timezone.utc).isoformat()

    source = Path(args.base_review_dir).resolve()
    target = Path(args.output_dir).resolve()
    if not source.is_dir() or target == source or source in target.parents:
        raise ValueError("base/output review directories are invalid")
    if target.exists():
        raise FileExistsError(
            "output review directory already exists; choose a new immutable target"
        )

    store = USPITStore(args.store)
    cboe_candidates = [
        item
        for item in store.load_source_batch(args.cboe_source_batch).dependencies
        if item.source_id == "sec_listing_alias_evidence"
        and item.dataset == "listing_alias_evidence"
    ]
    if len(cboe_candidates) != 1:
        raise ValueError("CBOE source batch must contain one listing-alias dependency")
    cboe = cboe_candidates[0]
    cboe_object = store.object_path(cboe.object_sha256)
    if (
        not cboe_object.is_file()
        or sha256_file(cboe_object) != cboe.object_sha256
    ):
        raise ValueError("CBOE evidence object is missing or corrupt")

    actions = pd.read_parquet(source / "corporate_actions.parquet")
    action_batch = store.load_source_batch(args.action_source_batch)
    lineage_actions: list[tuple[tuple[str, ...], pd.Series]] = []
    for specification in SPLIT_LINEAGES:
        action_id, predecessor, successor, *_ = specification
        matched = actions.loc[actions["action_id"].astype(str).eq(action_id)]
        if len(matched) != 1:
            raise ValueError(
                f"approved split action is missing or ambiguous: {action_id}"
            )
        action = matched.iloc[0]
        if (
            str(action["security_id"]) != predecessor
            or str(action["successor_security_id"]) != successor
            or str(action["action_type"]).upper() != "SPLIT"
            or str(action["terms_verified"]).casefold() not in {"true", "1"}
        ):
            raise ValueError(f"split terms do not match frozen lineage: {action_id}")
        evidence = [
            item
            for item in action_batch.dependencies
            if item.source_id == str(action["source_id"])
            and item.dataset == "corporate_actions"
            and item.object_sha256 == str(action["evidence_sha256"])
        ]
        if len(evidence) != 1:
            raise ValueError(
                f"action batch lacks exact split evidence: {action_id}"
            )
        lineage_actions.append((specification, action))
    embc_rows = actions.loc[actions["action_id"].astype(str).eq(EMBC_ACTION_ID)]
    if len(embc_rows) != 1:
        raise ValueError("approved BDX/EMBC spinoff action is missing or ambiguous")
    embc_action = embc_rows.iloc[0]
    if (
        str(embc_action["successor_security_id"]) != EMBC_SECURITY_ID
        or str(embc_action["action_type"]).upper() != "SPINOFF"
        or str(embc_action["terms_verified"]).casefold() not in {"true", "1"}
    ):
        raise ValueError("BDX/EMBC action terms do not match the frozen lineage")
    action_candidates = [
        item
        for item in action_batch.dependencies
        if item.source_id == str(embc_action["source_id"])
        and item.dataset == "corporate_actions"
        and item.object_sha256 == str(embc_action["evidence_sha256"])
    ]
    if len(action_candidates) != 1:
        raise ValueError("action source batch must contain the exact EMBC listing evidence")
    embc_evidence = action_candidates[0]
    embc_object = store.object_path(embc_evidence.object_sha256)
    if not embc_object.is_file() or sha256_file(embc_object) != embc_evidence.object_sha256:
        raise ValueError("EMBC evidence object is missing or corrupt")

    rows = [
        _row(
            binding_type="IDENTITY_ALIAS",
            security_id=CBOE_SECURITY_ID,
            ticker="CBOE",
            exchange_mic="BATS",
            valid_from="2018-01-31",
            valid_to=None,
            action_id="",
            evidence_source_id=cboe.source_id,
            evidence_sha256=cboe.object_sha256,
            note=(
                "SEC 2017 Form 10-K states that Cboe Global Markets common stock "
                "was listed on Cboe BZX under trading symbol CBOE as of 2018-01-31."
            ),
            approved_by=args.approved_by,
            approved_at=approved_at_text,
        ),
    ]
    for specification, action in lineage_actions:
        (
            action_id,
            predecessor,
            successor,
            ticker,
            exchange_mic,
            predecessor_from,
            predecessor_to,
            successor_from,
        ) = specification
        rows.extend(
            [
                _row(
                    binding_type="ACTION_LINEAGE_ALIAS",
                    security_id=predecessor,
                    ticker=ticker,
                    exchange_mic=exchange_mic,
                    valid_from=predecessor_from,
                    valid_to=predecessor_to,
                    action_id=action_id,
                    evidence_source_id=str(action["source_id"]),
                    evidence_sha256=str(action["evidence_sha256"]),
                    note=(
                        f"Approved split binds predecessor {predecessor} to "
                        f"{ticker}.US through the prior XNYS session."
                    ),
                    approved_by=args.approved_by,
                    approved_at=approved_at_text,
                ),
                _row(
                    binding_type="ACTION_LINEAGE_ALIAS",
                    security_id=successor,
                    ticker=ticker,
                    exchange_mic=exchange_mic,
                    valid_from=successor_from,
                    valid_to=None,
                    action_id=action_id,
                    evidence_source_id=str(action["source_id"]),
                    evidence_sha256=str(action["evidence_sha256"]),
                    note=(
                        f"Approved split binds successor {successor} to "
                        f"{ticker}.US from the effective XNYS session."
                    ),
                    approved_by=args.approved_by,
                    approved_at=approved_at_text,
                ),
            ]
        )
    rows.append(
        _row(
            binding_type="IDENTITY_ALIAS",
            security_id=EMBC_SECURITY_ID,
            ticker="EMBC",
            exchange_mic="XNAS",
            valid_from="2022-04-01",
            valid_to=None,
            action_id="",
            evidence_source_id=embc_evidence.source_id,
            evidence_sha256=embc_evidence.object_sha256,
            note=(
                "SEC 8-K accepted 2022-03-21 states that regular-way Embecta "
                "trading would begin 2022-04-01 on Nasdaq under ticker EMBC."
            ),
            approved_by=args.approved_by,
            approved_at=approved_at_text,
        )
    )
    shutil.copytree(source, target)
    frame = pd.DataFrame(rows)
    frame.to_parquet(target / "listing_alias_review.parquet", index=False)
    manifest = {
        "format_version": "us-pit-listing-alias-review-package-v1",
        "base_review_dir": str(source),
        "cboe_source_batch_id": args.cboe_source_batch,
        "action_source_batch_id": args.action_source_batch,
        "listing_alias_review_sha256": sha256_file(
            target / "listing_alias_review.parquet"
        ),
        "row_count": len(frame),
        "approved_by": args.approved_by,
        "approved_at": approved_at_text,
    }
    manifest["package_id"] = sha256_json(manifest)
    (target / "listing_alias_review_manifest.json").write_bytes(
        canonical_json_bytes(manifest)
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
