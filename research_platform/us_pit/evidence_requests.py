from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from .action_review import _source_plain_text
from .hashing import canonical_json_bytes, sha256_file, sha256_json
from .models import SourceRole
from .store import USPITStore


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


def build_operator_transition_evidence_requests(
    store: USPITStore,
    normalization_dir: Path | str,
    transitions: list[dict[str, str]],
    output_dir: Path | str,
    *,
    proposed_by: str,
) -> EvidenceRequestResult:
    """Build an evidence queue from operator-proposed identity transitions.

    The operator only proposes the predecessor/successor pairing (typically
    derived from workspace anchor-reconciliation differences, i.e. from frozen
    anchor evidence).  No action type, date, or term may be proposed here:
    every fact must later be verified against a frozen SEC filing by the
    action-review workflow.  Identity metadata (names, ISIN, CUSIP, LEI, CIK,
    ticker) is resolved deterministically from the official normalization.
    """

    author = str(proposed_by or "").strip()
    if not author:
        raise ValueError("operator transition proposal requires proposed_by")
    if not isinstance(transitions, list) or not transitions:
        raise ValueError("transitions must be a non-empty list")
    normalization = Path(normalization_dir).resolve()
    identity_path = normalization / "security_identity_candidates.parquet"
    if not identity_path.is_file():
        raise ValueError("normalization identity candidates are missing")
    identities = pd.read_parquet(identity_path)
    identities["security_id"] = (
        identities["identity_candidate_key"].fillna("").astype(str).map(
            lambda value: "us_" + value.replace(":", "_").lower() if value else ""
        )
    )

    def identity_of(security_id: str) -> dict[str, str]:
        group = identities.loc[identities["security_id"].astype(str).eq(security_id)]
        if group.empty:
            raise ValueError(f"transition security is absent from normalization: {security_id}")
        row = group.sort_values(["as_of_date", "source_row_number"], kind="stable").iloc[-1]
        return {
            "name": _text(row.get("issuer_name")),
            "isin": _text(row.get("isin")),
            "cusip": _text(row.get("cusip")),
            "lei": _text(row.get("lei")),
            "cik": _text(row.get("cik")),
            "ticker": _text(row.get("ticker")),
        }

    def event_anchored_predecessor(
        transition: dict, index: int, successor_name: str
    ) -> dict[str, str]:
        """Frozen rule A' (R3): anchor a predecessor absent from normalization
        to an approved membership-event stable ID.  The frozen S&P evidence
        object must contain both the operator-supplied predecessor name and
        the normalization-resolved successor name verbatim."""
        digest = _text(transition.get("predecessor_event_evidence_sha256")).lower()
        predecessor_name = _text(transition.get("predecessor_name"))
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                f"transition {index} requires a valid predecessor_event_evidence_sha256"
            )
        if not predecessor_name:
            raise ValueError(f"transition {index} requires predecessor_name")
        dependencies = [
            item
            for batch in store.list_source_batches()
            for item in batch.dependencies
            if item.object_sha256 == digest
            and item.dataset == "membership_events"
            and item.role == SourceRole.SIGNAL_INPUT
        ]
        if not dependencies:
            raise ValueError(
                f"transition {index} predecessor event evidence is not a frozen "
                "SIGNAL_INPUT membership_events dependency"
            )
        object_path = store.object_path(digest)
        if not object_path.is_file():
            raise ValueError(f"transition {index} predecessor event object is missing")
        text = _source_plain_text(object_path.read_bytes()).upper()
        if predecessor_name.upper() not in text:
            raise ValueError(
                f"transition {index} predecessor name is absent from the frozen event evidence"
            )
        if not successor_name or successor_name.upper() not in text:
            raise ValueError(
                f"transition {index} successor name is absent from the frozen event evidence"
            )
        return {
            "name": predecessor_name,
            "isin": "",
            "cusip": "",
            "lei": "",
            "cik": "",
            "ticker": "",
            "event_anchored": "true",
            "event_evidence_sha256": digest,
        }

    rows: list[dict[str, Any]] = []
    for index, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            raise ValueError(f"transition {index} is not an object")
        anchor_date = _text(transition.get("anchor_date"))
        predecessor = _text(transition.get("predecessor_security_id"))
        successor = _text(transition.get("successor_security_id"))
        if not anchor_date or not predecessor or not successor:
            raise ValueError(f"transition {index} requires anchor_date and both security ids")
        if predecessor == successor:
            raise ValueError(f"transition {index} predecessor equals successor")
        suc = identity_of(successor)
        event_anchored = bool(
            _text(transition.get("predecessor_event_evidence_sha256"))
        )
        pre = (
            event_anchored_predecessor(transition, index, suc["name"])
            if event_anchored
            else identity_of(predecessor)
        )
        suc = identity_of(successor)
        request_identity = {
            "audit_id": "OPERATOR_PROPOSED",
            "anchor_date": anchor_date,
            "predecessor_security_id": predecessor,
            "successor_security_id": successor,
        }
        rows.append(
            {
                "request_id": sha256_json(request_identity),
                **request_identity,
                "predecessor_name": pre["name"],
                "successor_name": suc["name"],
                "predecessor_isin": pre["isin"],
                "successor_isin": suc["isin"],
                "predecessor_cusip": pre["cusip"],
                "successor_cusip": suc["cusip"],
                "predecessor_lei": pre["lei"],
                "successor_lei": suc["lei"],
                "predecessor_cik": pre["cik"],
                "successor_cik": suc["cik"],
                "predecessor_ticker": pre["ticker"],
                "successor_ticker": suc["ticker"],
                "match_basis": (
                    "OPERATOR_EVENT_ANCHORED_PAIR"
                    if event_anchored
                    else "OPERATOR_PROPOSED_ANCHOR_PAIR"
                ),
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
                "review_note": str(transition.get("note") or ""),
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
            "audit_id": "OPERATOR_PROPOSED",
            "proposed_by": author,
            "request_count": len(requests),
            "artifact_sha256": sha256_file(request_path),
            "status": "DATA_BLOCKED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "policy": {
                "identity_similarity_is_not_action_evidence": True,
                "official_source_required": True,
                "manual_action_terms_forbidden": True,
                "operator_proposes_pairing_only": True,
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
    "build_operator_transition_evidence_requests",
    "build_transition_evidence_requests",
]
