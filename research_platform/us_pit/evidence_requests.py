from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from .hashing import canonical_json_bytes, sha256_file, sha256_json


EVIDENCE_REQUEST_VERSION = "us-pit-evidence-request-v2"


@dataclass(frozen=True)
class EvidenceRequestResult:
    path: Path
    manifest: dict[str, Any]


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def build_transition_evidence_requests(
    membership_audit_dir: Path | str,
    output_dir: Path | str,
) -> EvidenceRequestResult:
    """Turn diagnostic identity transitions into a non-buildable evidence queue."""

    audit_root = Path(membership_audit_dir).resolve()
    audit_path = audit_root / "membership_audit.json"
    audit_manifest_path = audit_root / "manifest.json"
    transitions_path = audit_root / "identity_transition_candidates.parquet"
    if not all(path.is_file() for path in (audit_path, audit_manifest_path, transitions_path)):
        raise ValueError("membership audit package is incomplete")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit_manifest = json.loads(audit_manifest_path.read_text(encoding="utf-8"))
    if (
        audit_manifest.get("membership_audit_sha256") != sha256_file(audit_path)
        or audit_manifest.get("identity_transition_candidates_sha256")
        != sha256_file(transitions_path)
        or audit_manifest.get("candidate_only") is not True
        or audit_manifest.get("direct_build_allowed") is not False
    ):
        raise ValueError("membership audit package failed integrity policy")
    transitions = pd.read_parquet(transitions_path)
    rows: list[dict[str, Any]] = []
    for row in transitions.to_dict(orient="records"):
        request_identity = {
            "audit_id": _text(audit.get("audit_id", "")),
            "anchor_date": _text(row.get("anchor_date", "")),
            "predecessor_security_id": _text(row.get("predecessor_security_id", "")),
            "successor_security_id": _text(row.get("successor_security_id", "")),
        }
        rows.append(
            {
                "request_id": sha256_json(request_identity),
                **request_identity,
                "predecessor_name": _text(row.get("predecessor_name", "")),
                "successor_name": _text(row.get("successor_name", "")),
                "predecessor_isin": _text(row.get("predecessor_isin", "")),
                "successor_isin": _text(row.get("successor_isin", "")),
                "predecessor_cusip": _text(row.get("predecessor_cusip", "")),
                "successor_cusip": _text(row.get("successor_cusip", "")),
                "predecessor_lei": _text(row.get("predecessor_lei", "")),
                "successor_lei": _text(row.get("successor_lei", "")),
                "predecessor_cik": _text(row.get("predecessor_cik", "")),
                "successor_cik": _text(row.get("successor_cik", "")),
                "predecessor_ticker": _text(row.get("predecessor_ticker", "")),
                "successor_ticker": _text(row.get("successor_ticker", "")),
                "match_basis": _text(row.get("match_basis", "")),
                "required_authorities": "SEC|EXCHANGE|ISSUER",
                "required_facts": (
                    "action_type;announced_at;effective_at;terms;successor_identity;"
                    "last_trade_or_settlement"
                ),
                "accepted_action_types": (
                    "TICKER_CHANGE|RENAME|SPLIT|STOCK_DIVIDEND|CASH_MERGER|"
                    "STOCK_MERGER|SPINOFF|DELISTING|BANKRUPTCY|REORGANIZATION"
                ),
                "status": "EVIDENCE_REQUIRED",
                "approved": False,
                "evidence_sha256": "",
                "source_url": "",
                "review_note": "",
            }
        )
    requests = pd.DataFrame(rows)
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"evidence request output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        request_path = staging / "corporate_action_evidence_requests.parquet"
        requests.to_parquet(request_path, index=False)
        manifest = {
            "format_version": EVIDENCE_REQUEST_VERSION,
            "audit_id": _text(audit.get("audit_id", "")),
            "audit_manifest_sha256": sha256_file(audit_manifest_path),
            "request_count": len(requests),
            "artifact_sha256": sha256_file(request_path),
            "status": "DATA_BLOCKED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "policy": {
                "identity_similarity_is_not_action_evidence": True,
                "official_source_required": True,
                "manual_action_terms_forbidden": True,
            },
        }
        manifest["request_set_id"] = sha256_json(manifest)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return EvidenceRequestResult(output, manifest)


__all__ = [
    "EVIDENCE_REQUEST_VERSION",
    "EvidenceRequestResult",
    "build_transition_evidence_requests",
]
