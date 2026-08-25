from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import exchange_calendars as xcals
import pandas as pd

from .hashing import sha256_file, sha256_json
from .official_normalize import OfficialNormalizationResult
from .quality import REQUIRED_ARTIFACT_COLUMNS


REVIEW_TEMPLATE_VERSION = "us-pit-review-template-v1"


@dataclass(frozen=True)
class ReviewTemplateResult:
    path: Path
    manifest: dict[str, Any]


def prepare_review_template(
    normalization_dir: Path | str,
    output_dir: Path | str,
    *,
    decision_start: date,
    decision_end: date,
    membership_review_dir: Path | str | None = None,
    membership_audit_dir: Path | str | None = None,
    action_review_dir: Path | str | None = None,
) -> ReviewTemplateResult:
    """Create an immutable-by-content, explicitly unapproved review workspace.

    This helper never approves identities or manufactures index events.  It
    exists to make every unresolved input visible in a stable Parquet schema
    before the reviewed-workspace assembler freezes the operator's evidence.
    """

    source = Path(normalization_dir).resolve()
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("official normalization manifest not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = OfficialNormalizationResult(
        str(manifest.get("normalization_id") or ""), source, manifest
    )
    if source.name != result.normalization_id:
        raise ValueError("official normalization directory identity mismatch")
    holdings = result.load_frame("fund_holdings_observed_candidate")
    identities = result.load_frame("security_identity_candidates")
    issues = result.load_frame("normalization_issues")
    if decision_start > decision_end:
        raise ValueError("decision_start must not be after decision_end")

    issue_ids: dict[tuple[str, int], list[str]] = {}
    for row in issues.to_dict(orient="records"):
        key = (str(row["content_sha256"]), int(row["source_row_number"]))
        issue_ids.setdefault(key, []).append(str(row["issue_id"]))
    identity_by_candidate = identities.set_index("holding_candidate_id", drop=False)
    rows: list[dict[str, Any]] = []
    for holding in holdings.to_dict(orient="records"):
        candidate_id = str(holding["holding_candidate_id"])
        identity = identity_by_candidate.loc[candidate_id]
        if isinstance(identity, pd.DataFrame):
            raise ValueError("identity candidate key is duplicated")
        stable_key = _text(identity.get("identity_candidate_key")).strip()
        suggested_security_id = (
            "us_" + stable_key.replace(":", "_").lower() if stable_key else ""
        )
        key = (str(holding["content_sha256"]), int(holding["source_row_number"]))
        rows.append(
            {
                "holding_candidate_id": candidate_id,
                "approved": False,
                "issuer_id": "",
                "exchange": _text(holding.get("exchange")),
                "valid_from": str(holding.get("as_of_date") or ""),
                "valid_to": "",
                "review_note": "",
                "resolved_issue_ids": ",".join(sorted(issue_ids.get(key, []))),
                "suggested_security_id": suggested_security_id,
                "source_id": str(holding.get("source_id") or ""),
                "as_of_date": str(holding.get("as_of_date") or ""),
                "issuer_name": _text(holding.get("issuer_name")),
                "title": _text(holding.get("title")),
                "ticker": _text(holding.get("ticker")),
                "isin": _text(identity.get("isin")),
                "cusip": _text(identity.get("cusip")),
            }
        )
    identity_review = pd.DataFrame(rows)

    decisions = _decision_month_ends(decision_start, decision_end)
    signal_holdings = holdings.loc[
        holdings["signal_eligible"].fillna(False).astype(bool)
    ].copy()
    eligible_from = pd.to_datetime(
        signal_holdings.get("eligible_from"), errors="coerce", utc=True
    )
    unavailable_decisions = [
        day.isoformat()
        for day in decisions
        if signal_holdings.empty
        or not bool((eligible_from <= pd.Timestamp(day, tz="UTC")).any())
    ]
    high_issues = issues.loc[issues["severity"].astype(str).eq("HIGH")]
    gaps = [
        {
            "code": "IDENTITY_REVIEW_REQUIRED",
            "severity": "HIGH",
            "count": int(len(high_issues)),
            "detail": "Every HIGH identity issue requires cited evidence and explicit approval.",
        },
        {
            "code": "DECISION_TIME_MEMBERSHIP_BASELINE_MISSING",
            "severity": "CRITICAL",
            "count": len(unavailable_decisions),
            "decision_dates": unavailable_decisions,
            "detail": "Validation-only N-PORT anchors cannot be backdated into signal membership.",
        },
        {
            "code": "MONTHLY_MEMBERSHIP_EVENT_CHAIN_REQUIRED",
            "severity": "CRITICAL",
            "count": len(decisions),
            "detail": "Freeze official ADD/REMOVE announcements and deterministically replay every decision month.",
        },
    ]
    membership_events = pd.DataFrame(
        columns=[
            *sorted(REQUIRED_ARTIFACT_COLUMNS["membership_events"]),
            "approved", "review_note",
        ]
    )
    linked_inputs: dict[str, Any] = {}
    if membership_review_dir is not None:
        review_root = Path(membership_review_dir).resolve()
        review_manifest_path = review_root / "manifest.json"
        review_path = review_root / "membership_events.parquet"
        if not review_manifest_path.is_file() or not review_path.is_file():
            raise ValueError("membership review package is incomplete")
        review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
        if (
            review_manifest.get("status") != "REVIEW_REQUIRED"
            or review_manifest.get("direct_build_allowed") is not False
            or review_manifest.get("artifact_sha256") != sha256_file(review_path)
        ):
            raise ValueError("membership review package failed integrity policy")
        membership_events = pd.read_parquet(review_path)
        required_review_columns = (
            REQUIRED_ARTIFACT_COLUMNS["membership_events"]
            | {"approved", "review_note"}
        )
        if not required_review_columns.issubset(membership_events.columns):
            raise ValueError("membership review schema is incomplete")
        if membership_events["approved"].fillna(False).astype(bool).any():
            raise ValueError("prepare-review only accepts unapproved membership rows")
        linked_inputs["membership_review"] = {
            "manifest_sha256": sha256_file(review_manifest_path),
            "artifact_sha256": sha256_file(review_path),
            "row_count": len(membership_events),
        }
        gaps[-1] = {
            "code": "MONTHLY_MEMBERSHIP_EVENT_REVIEW_REQUIRED",
            "severity": "CRITICAL",
            "count": int(len(membership_events)),
            "detail": (
                "Captured official ADD/REMOVE rows are present but every row "
                "requires explicit evidence-backed approval."
            ),
        }
    if membership_audit_dir is not None:
        audit_root = Path(membership_audit_dir).resolve()
        audit_manifest_path = audit_root / "manifest.json"
        audit_path = audit_root / "membership_audit.json"
        if not audit_manifest_path.is_file() or not audit_path.is_file():
            raise ValueError("membership audit package is incomplete")
        audit_manifest = json.loads(audit_manifest_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if (
            audit_manifest.get("membership_audit_sha256") != sha256_file(audit_path)
            or audit_manifest.get("candidate_only") is not True
            or audit_manifest.get("direct_build_allowed") is not False
            or audit.get("audit_id") != audit_manifest.get("audit_id")
        ):
            raise ValueError("membership audit package failed integrity policy")
        linked_inputs["membership_audit"] = {
            "manifest_sha256": sha256_file(audit_manifest_path),
            "audit_sha256": sha256_file(audit_path),
            "audit_id": audit["audit_id"],
        }
        gaps.append(
            {
                "code": "MEMBERSHIP_CAUSAL_REPLAY_BLOCKED",
                "severity": "CRITICAL",
                "count": int(sum(dict(audit.get("gap_counts", {})).values())),
                "gap_counts": dict(audit.get("gap_counts", {})),
                "audit_id": audit["audit_id"],
                "detail": "Resolve every frozen causal replay gap before approval.",
            }
        )

    corporate_actions = pd.DataFrame(
        columns=[
            *sorted(REQUIRED_ARTIFACT_COLUMNS["corporate_actions"]),
            "approved", "review_note",
        ]
    )
    if action_review_dir is not None:
        action_root = Path(action_review_dir).resolve()
        action_manifest_path = action_root / "manifest.json"
        action_path = action_root / "corporate_actions.parquet"
        decision_path = action_root / "review_decisions.parquet"
        if not all(
            path.is_file()
            for path in (action_manifest_path, action_path, decision_path)
        ):
            raise ValueError("approved action review package is incomplete")
        action_manifest = json.loads(
            action_manifest_path.read_text(encoding="utf-8")
        )
        if (
            action_manifest.get("status") != "REVIEW_APPROVED"
            or action_manifest.get("direct_build_allowed") is not False
            or action_manifest.get("approval_id")
            != sha256_json(
                {
                    key: value
                    for key, value in action_manifest.items()
                    if key != "approval_id"
                }
            )
            or action_manifest.get("corporate_actions_sha256")
            != sha256_file(action_path)
            or action_manifest.get("review_decisions_sha256")
            != sha256_file(decision_path)
            or not str(action_manifest.get("source_batch_id") or "")
        ):
            raise ValueError("approved action review package failed integrity policy")
        corporate_actions = pd.read_parquet(action_path)
        action_decisions = pd.read_parquet(decision_path)
        required_action_columns = REQUIRED_ARTIFACT_COLUMNS["corporate_actions"]
        if not required_action_columns.issubset(corporate_actions.columns):
            raise ValueError("approved action review schema is incomplete")
        if corporate_actions["action_id"].duplicated().any():
            raise ValueError("approved action review contains duplicate action IDs")
        decision_notes = action_decisions.loc[
            action_decisions["action_id"].fillna("").astype(str).ne("")
        ][["action_id", "review_note"]].copy()
        if (
            decision_notes["action_id"].duplicated().any()
            or decision_notes["review_note"].fillna("").astype(str).str.strip().eq("").any()
        ):
            raise ValueError("approved action review notes are incomplete")
        corporate_actions = corporate_actions.merge(
            decision_notes, on="action_id", how="left", validate="one_to_one"
        )
        if corporate_actions["review_note"].fillna("").astype(str).str.strip().eq("").any():
            raise ValueError("approved action review lacks a note for an action")
        corporate_actions["approved"] = True
        linked_inputs["action_review"] = {
            "manifest_sha256": sha256_file(action_manifest_path),
            "corporate_actions_sha256": sha256_file(action_path),
            "review_decisions_sha256": sha256_file(decision_path),
            "proposal_sha256": action_manifest.get("proposal_sha256"),
            "approval_id": action_manifest.get("approval_id"),
            "source_batch_id": action_manifest["source_batch_id"],
            "row_count": len(corporate_actions),
        }

    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"review template output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        identity_review.to_parquet(staging / "identity_review.parquet", index=False)
        membership_events.to_parquet(
            staging / "membership_events.parquet", index=False
        )
        corporate_actions.to_parquet(
            staging / "corporate_actions.parquet", index=False
        )
        for dataset in ("session_exceptions", "lifecycle_reconciliations"):
            pd.DataFrame(
                columns=sorted(REQUIRED_ARTIFACT_COLUMNS[dataset])
            ).to_parquet(staging / f"{dataset}.parquet", index=False)
        (staging / "review_gaps.json").write_text(
            json.dumps(gaps, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        artifact_hashes = {
            path.name: sha256_file(path)
            for path in sorted(staging.iterdir())
            if path.is_file()
        }
        identity = {
            "format_version": REVIEW_TEMPLATE_VERSION,
            "normalization_id": result.normalization_id,
            "normalization_manifest_sha256": sha256_file(manifest_path),
            "decision_start": decision_start.isoformat(),
            "decision_end": decision_end.isoformat(),
            "decision_months": len(decisions),
            "status": "DATA_BLOCKED",
            "approved": False,
            "artifacts": artifact_hashes,
            "blocking_gaps_sha256": sha256_json(gaps),
            "linked_inputs": linked_inputs,
        }
        identity["review_template_id"] = sha256_json(identity)
        (staging / "review_template_manifest.json").write_text(
            json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        staging.replace(output)
        return ReviewTemplateResult(output, identity)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _decision_month_ends(start: date, end: date) -> tuple[date, ...]:
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    dates = [pd.Timestamp(item).tz_localize(None).date() for item in sessions]
    result = [
        day
        for index, day in enumerate(dates)
        if index == len(dates) - 1 or dates[index + 1].month != day.month
    ]
    return tuple(result)


def _text(value: Any) -> str:
    return "" if value is None or pd.isna(value) else str(value)


__all__ = ["REVIEW_TEMPLATE_VERSION", "ReviewTemplateResult", "prepare_review_template"]
