from __future__ import annotations

import json
import html
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import exchange_calendars as xcals
import pandas as pd

from research_platform.us_market_time import ny_session_date, utc_instant

from .hashing import canonical_json_bytes, sha256_bytes, sha256_file, sha256_json
from .models import LicenseClass, SourceDependency, SourceRole
from .store import SourceBatch, USPITStore


ACTION_REVIEW_TEMPLATE_VERSION = "us-pit-action-review-template-v1"
ACTION_REVIEW_PROPOSAL_VERSION = "us-pit-action-review-proposal-v1"
ACTION_REVIEW_APPROVAL_VERSION = "us-pit-action-review-approval-v1"
ACTION_SOURCE_ID = "sec_reviewed_corporate_action"
ACTION_SOURCE_VERSION = "sec-reviewed-corporate-action-v1"
ACTION_TYPES = frozenset(
    {
        "SPLIT",
        "STOCK_DIVIDEND",
        "CASH_DIVIDEND",
        "TICKER_CHANGE",
        "RENAME",
        "CASH_MERGER",
        "STOCK_MERGER",
        "SPINOFF",
        "DELISTING",
        "BANKRUPTCY",
        "REORGANIZATION",
    }
)
DISPOSITIONS = frozenset(
    {"ACTION_CONFIRMED", "IDENTITY_CONTINUITY", "DISTINCT_SECURITIES"}
)
EDITABLE_COLUMNS = (
    "request_id",
    "anchor_date",
    "predecessor_security_id",
    "successor_security_id",
    "predecessor_name",
    "successor_name",
    "disposition",
    "selected_review_candidate_id",
    "action_type",
    "announced_at",
    "effective_at",
    "pay_date",
    "share_ratio",
    "cash_per_share",
    "cost_basis_fraction",
    "terms_verified",
    "evidence_excerpt",
    "review_note",
)


@dataclass(frozen=True)
class ActionReviewResult:
    path: Path
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class ActionReviewApprovalResult:
    path: Path
    manifest: Mapping[str, Any]
    source_batch: SourceBatch


def prepare_action_review(
    evidence_request_dir: Path | str,
    ranked_review_dir: Path | str,
    output_dir: Path | str,
) -> ActionReviewResult:
    """Create a one-row-per-transition draft without selecting or inferring facts."""

    request_root = Path(evidence_request_dir).resolve()
    rank_root = Path(ranked_review_dir).resolve()
    request_manifest, requests = _load_package(
        request_root,
        "corporate_action_evidence_requests.parquet",
        expected_status="DATA_BLOCKED",
    )
    rank_manifest, candidates = _load_package(
        rank_root,
        "corporate_action_filing_review.parquet",
        expected_status="REVIEW_REQUIRED",
    )
    if request_manifest.get("request_set_id") is None:
        raise ValueError("evidence request package has no request_set_id")
    if rank_manifest.get("request_count") != len(requests):
        raise ValueError("ranked SEC review request count conflicts with evidence queue")
    required_candidate_columns = {
        "request_id",
        "review_candidate_id",
        "anchor_date",
        "accepted_at",
        "source_object_sha256",
        "source_url",
        "filing_date",
        "accession_number",
    }
    missing_candidate_columns = required_candidate_columns - set(candidates.columns)
    if missing_candidate_columns:
        raise ValueError(
            "ranked SEC review is missing columns: "
            + ", ".join(sorted(missing_candidate_columns))
        )
    request_ids = set(requests["request_id"].astype(str))
    if set(candidates["request_id"].astype(str)) != request_ids:
        raise ValueError("ranked SEC review does not cover the exact evidence request set")
    duplicate_count = int(candidates["review_candidate_id"].duplicated().sum())
    if duplicate_count:
        comparison_columns = [
            column for column in candidates.columns if column != "request_rank"
        ]
        for _, group in candidates.loc[
            candidates["review_candidate_id"].duplicated(keep=False)
        ].groupby("review_candidate_id", sort=True):
            if any(
                group[column].astype(str).nunique(dropna=False) != 1
                for column in comparison_columns
            ):
                raise ValueError("ranked SEC review duplicate candidate conflicts")
        candidates = (
            candidates.sort_values(["review_candidate_id", "request_rank"])
            .drop_duplicates("review_candidate_id", keep="first")
            .reset_index(drop=True)
        )
    accepted = pd.to_datetime(candidates["accepted_at"], errors="coerce", utc=True)
    anchors = pd.to_datetime(candidates["anchor_date"], errors="coerce")
    if accepted.isna().any() or anchors.isna().any():
        raise ValueError("ranked SEC review contains invalid acceptance or anchor dates")
    anchor_cutoffs = anchors.dt.tz_localize("America/New_York") + pd.Timedelta(
        hours=23, minutes=59, seconds=59
    )
    candidates["visible_by_anchor"] = accepted <= anchor_cutoffs.dt.tz_convert("UTC")
    visibility = {
        str(request_id): {
            "candidate_count": int(len(group)),
            "visible_by_anchor_count": int(group["visible_by_anchor"].sum()),
        }
        for request_id, group in candidates.groupby("request_id", sort=True)
    }
    if any(item["visible_by_anchor_count"] == 0 for item in visibility.values()):
        raise ValueError("one or more evidence requests lack decision-time visible SEC candidates")

    rows: list[dict[str, Any]] = []
    for request in requests.sort_values(["anchor_date", "request_id"]).to_dict(
        orient="records"
    ):
        rows.append(
            {
                "request_id": _text(request.get("request_id")),
                "anchor_date": _text(request.get("anchor_date")),
                "predecessor_security_id": _text(
                    request.get("predecessor_security_id")
                ),
                "successor_security_id": _text(request.get("successor_security_id")),
                "predecessor_name": _text(request.get("predecessor_name")),
                "successor_name": _text(request.get("successor_name")),
                "disposition": "",
                "selected_review_candidate_id": "",
                "action_type": "",
                "announced_at": "",
                "effective_at": "",
                "pay_date": "",
                "share_ratio": "",
                "cash_per_share": "",
                "cost_basis_fraction": "",
                "terms_verified": False,
                "evidence_excerpt": "",
                "review_note": "",
            }
        )
    draft = pd.DataFrame(rows, columns=EDITABLE_COLUMNS)
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"action review template already exists: {output}")
    stage = output.parent / f".{output.name}.{uuid4().hex}.staging"
    output.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir()
    try:
        draft_path = stage / "action_review.csv"
        candidate_path = stage / "corporate_action_filing_candidates.parquet"
        candidate_csv_path = stage / "corporate_action_filing_candidates.csv"
        guide_path = stage / "action_review_guide.csv"
        request_path = stage / "evidence_requests.parquet"
        gap_path = stage / "review_gaps.json"
        draft.to_csv(draft_path, index=False, encoding="utf-8-sig")
        candidates.to_parquet(candidate_path, index=False)
        candidates.to_csv(candidate_csv_path, index=False, encoding="utf-8-sig")
        requests.to_parquet(request_path, index=False)
        guide = candidates.merge(
            requests[
                [
                    "request_id",
                    "predecessor_security_id",
                    "successor_security_id",
                    "predecessor_name",
                    "successor_name",
                ]
            ],
            on="request_id",
            how="left",
            validate="many_to_one",
        )
        guide_sort_columns = ["anchor_date", "request_id"]
        guide_sort_columns.append(
            "request_rank"
            if "request_rank" in guide.columns
            else "review_candidate_id"
        )
        guide = guide.sort_values(guide_sort_columns)
        guide.to_csv(guide_path, index=False, encoding="utf-8-sig")
        gaps = {
            "status": "REVIEW_REQUIRED",
            "unresolved_request_count": len(draft),
            "candidate_count": len(candidates),
            "decision_time_visible_candidate_count": int(
                candidates["visible_by_anchor"].sum()
            ),
            "requests": [
                {
                    "request_id": str(request_id),
                    **visibility[str(request_id)],
                }
                for request_id in sorted(request_ids)
            ],
            "automatic_selection_forbidden": True,
            "direct_build_allowed": False,
        }
        gap_path.write_bytes(canonical_json_bytes(gaps))
        manifest = {
            "format_version": ACTION_REVIEW_TEMPLATE_VERSION,
            "request_set_id": request_manifest["request_set_id"],
            "request_manifest_sha256": sha256_file(request_root / "manifest.json"),
            "review_set_id": rank_manifest.get("review_set_id"),
            "rank_manifest_sha256": sha256_file(rank_root / "manifest.json"),
            "request_count": len(draft),
            "candidate_count": len(candidates),
            "duplicate_candidate_rows_removed": duplicate_count,
            "decision_time_visibility": visibility,
            "draft_sha256": sha256_file(draft_path),
            "candidates_sha256": sha256_file(candidate_path),
            "candidates_csv_sha256": sha256_file(candidate_csv_path),
            "guide_sha256": sha256_file(guide_path),
            "requests_sha256": sha256_file(request_path),
            "review_gaps_sha256": sha256_file(gap_path),
            "status": "REVIEW_REQUIRED",
            "candidate_only": True,
            "direct_build_allowed": False,
            "policy": {
                "automatic_selection_forbidden": True,
                "terms_must_be_transcribed_from_frozen_sec_document": True,
                "two_phase_hash_approval_required": True,
            },
        }
        manifest["template_id"] = sha256_json(manifest)
        (stage / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        stage.replace(output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return ActionReviewResult(output, manifest)


def propose_action_review(
    store: USPITStore,
    template_dir: Path | str,
    completed_csv: Path | str,
    output_dir: Path | str,
    *,
    proposed_by: str,
    proposed_at: datetime | None = None,
) -> ActionReviewResult:
    """Validate and freeze a completed review draft; this does not approve it."""

    root = Path(template_dir).resolve()
    manifest_path = root / "manifest.json"
    candidate_path = root / "corporate_action_filing_candidates.parquet"
    request_path = root / "evidence_requests.parquet"
    if not manifest_path.is_file() or not candidate_path.is_file() or not request_path.is_file():
        raise ValueError("action review template is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("format_version") != ACTION_REVIEW_TEMPLATE_VERSION
        or manifest.get("candidates_sha256") != sha256_file(candidate_path)
        or manifest.get("requests_sha256") != sha256_file(request_path)
        or manifest.get("candidate_only") is not True
        or manifest.get("direct_build_allowed") is not False
    ):
        raise ValueError("action review template failed integrity policy")
    author = proposed_by.strip()
    if not author:
        raise ValueError("action review proposed_by is required")
    proposed = proposed_at or datetime.now(timezone.utc)
    if proposed.tzinfo is None:
        raise ValueError("action review proposed_at must be timezone-aware")
    completed_path = Path(completed_csv).resolve(strict=True)
    if not completed_path.is_file():
        raise ValueError("completed action review must be a regular CSV file")
    draft = pd.read_csv(completed_path, dtype=str, keep_default_na=False)
    candidates = pd.read_parquet(candidate_path)
    requests = pd.read_parquet(request_path)
    normalized, dependencies = _validate_completed_review(
        store, draft, candidates, requests, observed_at=proposed
    )

    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"action review proposal already exists: {output}")
    stage = output.parent / f".{output.name}.{uuid4().hex}.staging"
    output.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir()
    try:
        draft_path = stage / "action_review.parquet"
        normalized.to_parquet(draft_path, index=False)
        proposal = {
            "format_version": ACTION_REVIEW_PROPOSAL_VERSION,
            "template_id": manifest["template_id"],
            "template_manifest_sha256": sha256_file(manifest_path),
            "review_sha256": sha256_file(draft_path),
            "review_row_count": len(normalized),
            "source_dependencies": [item.to_dict() for item in dependencies],
            "proposed_by": author,
            "proposed_at": proposed.astimezone(timezone.utc).isoformat(),
            "status": "REVIEW_PROPOSED",
            "approved": False,
            "direct_build_allowed": False,
        }
        proposal["proposal_sha256"] = sha256_json(proposal)
        (stage / "manifest.json").write_bytes(canonical_json_bytes(proposal))
        stage.replace(output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return ActionReviewResult(output, proposal)


def approve_action_review(
    store: USPITStore,
    proposal_dir: Path | str,
    output_dir: Path | str,
    *,
    expected_sha256: str,
    approved_by: str,
    acknowledgement: str,
    approved_at: datetime | None = None,
) -> ActionReviewApprovalResult:
    """Revalidate an immutable proposal and publish reviewed action evidence."""

    root = Path(proposal_dir).resolve()
    manifest_path = root / "manifest.json"
    review_path = root / "action_review.parquet"
    if not manifest_path.is_file() or not review_path.is_file():
        raise ValueError("action review proposal is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("format_version") != ACTION_REVIEW_PROPOSAL_VERSION
        or manifest.get("review_sha256") != sha256_file(review_path)
        or manifest.get("proposal_sha256") != expected_sha256
        or sha256_json({key: value for key, value in manifest.items() if key != "proposal_sha256"})
        != expected_sha256
    ):
        raise ValueError("action review proposal hash changed; approval rejected")
    approver = approved_by.strip()
    acknowledgement_text = acknowledgement.strip()
    if not approver or not acknowledgement_text:
        raise ValueError("action review approver and acknowledgement are required")
    approval_time = approved_at or datetime.now(timezone.utc)
    if approval_time.tzinfo is None:
        raise ValueError("action review approved_at must be timezone-aware")
    if utc_instant(approval_time) < utc_instant(manifest["proposed_at"]):
        raise ValueError("action review approval cannot predate proposal")

    review = pd.read_parquet(review_path)
    dependency_values = tuple(
        SourceDependency.from_dict(item) for item in manifest["source_dependencies"]
    )
    _revalidate_proposal(store, review, dependency_values)
    approved_dependencies = tuple(
        SourceDependency(
            source_id=item.source_id,
            source_version=item.source_version,
            role=item.role,
            license_class=item.license_class,
            object_sha256=item.object_sha256,
            observed_at=item.observed_at,
            published_at=item.published_at,
            as_of_date=item.as_of_date,
            url=item.url,
            dataset=item.dataset,
            metadata={
                **dict(item.metadata),
                "review_proposal_sha256": expected_sha256,
                "review_approved_by": approver,
                "review_approved_at": approval_time.astimezone(
                    timezone.utc
                ).isoformat(),
                "review_acknowledgement_sha256": sha256_bytes(
                    acknowledgement_text.encode("utf-8")
                ),
            },
        )
        for item in dependency_values
    )
    source_batch = store.write_source_batch(approved_dependencies)
    actions = review.loc[~review["disposition"].eq("DISTINCT_SECURITIES")].copy()
    formal_columns = [
        "action_id",
        "security_id",
        "action_type",
        "announced_at",
        "effective_at",
        "pay_date",
        "terms_verified",
        "source_id",
        "evidence_sha256",
        "successor_security_id",
        "share_ratio",
        "cash_per_share",
        "cost_basis_fraction",
    ]
    actions = actions[formal_columns]

    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"action review approval already exists: {output}")
    stage = output.parent / f".{output.name}.{uuid4().hex}.staging"
    output.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir()
    try:
        artifact = stage / "corporate_actions.parquet"
        decisions = stage / "review_decisions.parquet"
        actions.to_parquet(artifact, index=False)
        review.to_parquet(decisions, index=False)
        approval = {
            "format_version": ACTION_REVIEW_APPROVAL_VERSION,
            "proposal_sha256": expected_sha256,
            "proposal_manifest_sha256": sha256_file(manifest_path),
            "source_batch_id": source_batch.batch_id,
            "corporate_actions_sha256": sha256_file(artifact),
            "review_decisions_sha256": sha256_file(decisions),
            "review_count": len(review),
            "action_count": len(actions),
            "approved_by": approver,
            "approved_at": approval_time.astimezone(timezone.utc).isoformat(),
            "acknowledgement": acknowledgement_text,
            "status": "REVIEW_APPROVED",
            "direct_build_allowed": False,
        }
        approval["approval_id"] = sha256_json(approval)
        (stage / "manifest.json").write_bytes(canonical_json_bytes(approval))
        stage.replace(output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return ActionReviewApprovalResult(output, approval, source_batch)


def _load_package(
    root: Path,
    artifact_name: str,
    *,
    expected_status: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    manifest_path = root / "manifest.json"
    artifact_path = root / artifact_name
    if not manifest_path.is_file() or not artifact_path.is_file():
        raise ValueError(f"review package is incomplete: {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("artifact_sha256") != sha256_file(artifact_path)
        or manifest.get("status") != expected_status
        or manifest.get("candidate_only") is not True
        or manifest.get("direct_build_allowed") is not False
    ):
        raise ValueError(f"review package failed integrity policy: {root}")
    return manifest, pd.read_parquet(artifact_path)


def _validate_completed_review(
    store: USPITStore,
    review: pd.DataFrame,
    candidates: pd.DataFrame,
    requests: pd.DataFrame,
    *,
    observed_at: datetime,
) -> tuple[pd.DataFrame, tuple[SourceDependency, ...]]:
    missing = set(EDITABLE_COLUMNS) - set(review.columns)
    if missing:
        raise ValueError("completed action review is missing columns: " + ", ".join(sorted(missing)))
    if review["request_id"].eq("").any():
        raise ValueError("completed action review requires non-empty request_id values")
    immutable_columns = (
        "request_id",
        "anchor_date",
        "predecessor_security_id",
        "successor_security_id",
        "predecessor_name",
        "successor_name",
    )
    expected = requests[list(immutable_columns)].fillna("").astype(str).set_index(
        "request_id", drop=False
    )
    if set(review["request_id"].astype(str)) != set(expected.index.astype(str)):
        raise ValueError("completed review does not cover the exact evidence request set")
    for raw in review.to_dict(orient="records"):
        frozen = expected.loc[_text(raw.get("request_id"))]
        if isinstance(frozen, pd.DataFrame) or any(
            _text(raw.get(column)) != _text(frozen[column])
            for column in immutable_columns
        ):
            raise ValueError("completed review changed immutable evidence request identity")
    for request_id, group in review.groupby("request_id", sort=True):
        dispositions = group["disposition"].astype(str).str.strip().str.upper()
        distinct_count = int(dispositions.eq("DISTINCT_SECURITIES").sum())
        if distinct_count and (len(group) != 1 or distinct_count != 1):
            raise ValueError(
                f"DISTINCT_SECURITIES must be the only row for request {request_id}"
            )
    candidate_by_id = candidates.set_index("review_candidate_id", drop=False)
    rows: list[dict[str, Any]] = []
    dependency_reviews: dict[str, list[dict[str, str]]] = {}
    captured_documents = _captured_action_documents(store)
    calendar = xcals.get_calendar("XNYS")
    for raw in review.to_dict(orient="records"):
        disposition = _text(raw.get("disposition")).upper()
        if disposition not in DISPOSITIONS:
            raise ValueError(f"review disposition is unresolved: {raw.get('request_id')}")
        candidate_id = _text(raw.get("selected_review_candidate_id"))
        if not candidate_id or candidate_id not in candidate_by_id.index:
            raise ValueError("every review decision must select one frozen SEC candidate")
        candidate = candidate_by_id.loc[candidate_id]
        if isinstance(candidate, pd.DataFrame):
            raise ValueError("selected SEC candidate is ambiguous")
        if _text(candidate["request_id"]) != _text(raw.get("request_id")):
            raise ValueError("selected SEC candidate belongs to another evidence request")
        digest = _text(candidate["source_object_sha256"]).lower()
        object_path = store.object_path(digest)
        if not object_path.is_file() or sha256_file(object_path) != digest:
            raise ValueError("selected SEC source object is absent or corrupt in CAS")
        captured = captured_documents.get(digest)
        if captured is None:
            raise ValueError("selected SEC source object lacks captured catalog lineage")
        captured_dependency, captured_batch_ids = captured
        captured_accepted = _aware_iso(
            captured_dependency.metadata.get("accepted_at"), required=True
        )
        captured_published = _aware_iso(
            captured_dependency.published_at, required=True
        )
        candidate_accepted = _aware_iso(candidate["accepted_at"], required=True)
        if (
            captured_dependency.url != _text(candidate["source_url"])
            or _text(captured_dependency.metadata.get("accession_number"))
            != _text(candidate["accession_number"])
            or utc_instant(captured_accepted) != utc_instant(captured_published)
            or utc_instant(candidate_accepted) != utc_instant(captured_published)
            or captured_dependency.metadata.get("artifact_kind")
            != "sec_complete_submission"
            or captured_dependency.metadata.get("response_sha256") != digest
        ):
            raise ValueError("selected SEC source object conflicts with captured catalog lineage")
        excerpt = _normalize_text(raw.get("evidence_excerpt"))
        source_text = _source_plain_text(object_path.read_bytes())
        if len(excerpt) < 40 or excerpt not in source_text:
            raise ValueError("review evidence excerpt is too short or absent from frozen SEC source")
        note = _text(raw.get("review_note"))
        if not note:
            raise ValueError("every review decision requires a review_note")

        action_type = _text(raw.get("action_type")).upper()
        if disposition in {"ACTION_CONFIRMED", "IDENTITY_CONTINUITY"}:
            if action_type not in ACTION_TYPES:
                raise ValueError("confirmed action/identity continuity requires a supported action_type")
        elif action_type:
            raise ValueError("DISTINCT_SECURITIES review cannot assert an action_type")
        terms_verified = _bool(raw.get("terms_verified"))
        if disposition != "DISTINCT_SECURITIES" and not terms_verified:
            raise ValueError("confirmed action terms must be explicitly verified")

        announced_at = _aware_iso(raw.get("announced_at"), required=disposition != "DISTINCT_SECURITIES")
        effective_at = _aware_iso(raw.get("effective_at"), required=disposition != "DISTINCT_SECURITIES")
        if announced_at and effective_at and utc_instant(announced_at) > utc_instant(effective_at):
            raise ValueError("action announcement cannot follow effective time")
        accepted_at = candidate_accepted
        anchor_cutoff = pd.Timestamp(_text(raw.get("anchor_date")), tz="America/New_York")
        anchor_cutoff += pd.Timedelta(hours=23, minutes=59, seconds=59)
        if utc_instant(accepted_at) > anchor_cutoff.tz_convert("UTC"):
            raise ValueError("selected SEC filing was not public by the reconciliation anchor")
        if effective_at:
            session = ny_session_date(effective_at)
            if pd.isna(session) or not calendar.is_session(session):
                raise ValueError("action effective_at is not an explicit XNYS session")
        anchor_day = pd.Timestamp(_text(raw.get("anchor_date"))).normalize()
        if announced_at and ny_session_date(announced_at) > anchor_day:
            raise ValueError("action announcement is after the reconciliation anchor")
        if effective_at and ny_session_date(effective_at) > anchor_day:
            raise ValueError("action effective time is after the reconciliation anchor")
        pay_date = _date_text(raw.get("pay_date"))
        ratio = _number(raw.get("share_ratio"))
        cash = _number(raw.get("cash_per_share"))
        cost_basis = _number(raw.get("cost_basis_fraction"))
        successor = _text(raw.get("successor_security_id"))
        predecessor = _text(raw.get("predecessor_security_id"))
        if action_type in {"TICKER_CHANGE", "RENAME"} and predecessor != successor:
            raise ValueError("ticker/name continuity requires one stable security_id")
        if disposition == "DISTINCT_SECURITIES" and any(
            (
                announced_at,
                effective_at,
                pay_date,
                ratio is not None,
                cash is not None,
                cost_basis is not None,
                terms_verified,
            )
        ):
            raise ValueError("DISTINCT_SECURITIES cannot carry corporate-action terms")
        _validate_terms(action_type, ratio, cash, cost_basis, successor, pay_date)
        action_id = (
            ""
            if disposition == "DISTINCT_SECURITIES"
            else sha256_json(
                {
                    "request_id": _text(raw.get("request_id")),
                    "security_id": predecessor,
                    "action_type": action_type,
                    "effective_at": effective_at,
                    "evidence_sha256": digest,
                }
            )
        )
        dependency_reviews.setdefault(digest, []).append(
            {
                "request_id": _text(raw.get("request_id")),
                "review_candidate_id": candidate_id,
                "accession_number": _text(candidate["accession_number"]),
                "evidence_excerpt_sha256": sha256_bytes(excerpt.encode("utf-8")),
                "disposition": disposition,
                "action_type": action_type,
            }
        )
        rows.append(
            {
                **{column: _text(raw.get(column)) for column in EDITABLE_COLUMNS},
                "disposition": disposition,
                "action_id": action_id,
                "security_id": predecessor,
                "action_type": action_type,
                "announced_at": announced_at,
                "effective_at": effective_at,
                "pay_date": pay_date,
                "terms_verified": terms_verified,
                "source_id": ACTION_SOURCE_ID,
                "evidence_sha256": digest,
                "successor_security_id": successor,
                "share_ratio": ratio,
                "cash_per_share": cash,
                "cost_basis_fraction": cost_basis,
                "source_url": _text(candidate["source_url"]),
                "accession_number": _text(candidate["accession_number"]),
                "review_decision_id": sha256_json(
                    {
                        "request_id": _text(raw.get("request_id")),
                        "candidate_id": candidate_id,
                        "disposition": disposition,
                        "action_type": action_type,
                        "effective_at": effective_at,
                        "successor_security_id": successor,
                    }
                ),
            }
        )
    result = pd.DataFrame(rows)
    if result["review_decision_id"].duplicated().any():
        raise ValueError("completed review contains duplicate action decisions")
    action_ids = result.loc[result["action_id"].ne(""), "action_id"]
    if action_ids.duplicated().any():
        raise ValueError("completed review contains duplicate corporate actions")
    dependencies: list[SourceDependency] = []
    for digest in sorted(dependency_reviews):
        captured_dependency, captured_batch_ids = captured_documents[digest]
        reviews = sorted(
            dependency_reviews[digest],
            key=lambda item: (
                item["request_id"],
                item["review_candidate_id"],
                item["action_type"],
            ),
        )
        dependencies.append(
            SourceDependency(
                source_id=ACTION_SOURCE_ID,
                source_version=ACTION_SOURCE_VERSION,
                role=SourceRole.SIGNAL_INPUT,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                object_sha256=digest,
                observed_at=captured_dependency.observed_at,
                published_at=captured_dependency.published_at,
                as_of_date=captured_dependency.as_of_date,
                url=captured_dependency.url,
                dataset="corporate_actions",
                metadata={
                    "eligible_for_historical_signal": True,
                    "publication_time_from_payload": True,
                    "accepted_at": captured_dependency.published_at,
                    "accepted_at_verified_in_payload": True,
                    "artifact_kind": "reviewed_sec_complete_submission",
                    "review_decisions": reviews,
                    "human_terms_reviewed": True,
                    "review_proposed_at": observed_at.astimezone(timezone.utc).isoformat(),
                    "captured_source_id": captured_dependency.source_id,
                    "captured_source_batch_ids": list(captured_batch_ids),
                },
            )
        )
    return result, tuple(dependencies)


def _revalidate_proposal(
    store: USPITStore,
    review: pd.DataFrame,
    dependencies: tuple[SourceDependency, ...],
) -> None:
    by_hash = {item.object_sha256: item for item in dependencies}
    for row in review.to_dict(orient="records"):
        digest = _text(row.get("evidence_sha256"))
        dependency = by_hash.get(digest)
        object_path = store.object_path(digest)
        excerpt = _normalize_text(row.get("evidence_excerpt"))
        if (
            dependency is None
            or dependency.dataset != "corporate_actions"
            or dependency.role != SourceRole.SIGNAL_INPUT
            or not object_path.is_file()
            or sha256_file(object_path) != digest
            or excerpt not in _source_plain_text(object_path.read_bytes())
        ):
            raise ValueError("action review proposal evidence changed before approval")


def _captured_action_documents(
    store: USPITStore,
) -> dict[str, tuple[SourceDependency, tuple[str, ...]]]:
    grouped: dict[str, list[tuple[str, SourceDependency]]] = {}
    for batch in store.list_source_batches():
        for dependency in batch.dependencies:
            if (
                dependency.source_id == "sec_corporate_action_filing_documents"
                and dependency.dataset == "corporate_action_source_document"
            ):
                grouped.setdefault(dependency.object_sha256, []).append(
                    (batch.batch_id, dependency)
                )
    result: dict[str, tuple[SourceDependency, tuple[str, ...]]] = {}
    for digest, values in grouped.items():
        identities = {
            (
                item.url,
                _text(item.metadata.get("accession_number")),
                item.published_at,
            )
            for _, item in values
        }
        if len(identities) != 1:
            raise ValueError("captured SEC source document has conflicting catalog lineage")
        selected = min(values, key=lambda value: utc_instant(value[1].observed_at))
        result[digest] = (
            selected[1],
            tuple(sorted({batch_id for batch_id, _ in values})),
        )
    return result


def _validate_terms(
    action_type: str,
    ratio: float | None,
    cash: float | None,
    cost_basis: float | None,
    successor: str,
    pay_date: str,
) -> None:
    if not action_type:
        return
    if action_type in {"SPLIT", "STOCK_DIVIDEND"} and (ratio is None or ratio <= 0):
        raise ValueError(f"{action_type} requires a positive share_ratio")
    if action_type == "CASH_DIVIDEND" and (cash is None or cash < 0 or not pay_date):
        raise ValueError("CASH_DIVIDEND requires nonnegative cash_per_share and pay_date")
    if action_type in {"CASH_MERGER", "DELISTING", "BANKRUPTCY"} and (
        cash is None or cash < 0
    ):
        raise ValueError(f"{action_type} requires a nonnegative cash settlement")
    if action_type == "STOCK_MERGER" and (
        ratio is None or ratio <= 0 or not successor.startswith("us_")
    ):
        raise ValueError("STOCK_MERGER requires share_ratio and stable successor")
    if action_type == "REORGANIZATION" and (
        ratio is None
        or ratio != 1.0
        or not successor.startswith("us_")
    ):
        raise ValueError(
            "REORGANIZATION requires a strict one-to-one share_ratio and a stable successor"
        )
    if action_type == "SPINOFF" and (
        ratio is None
        or ratio <= 0
        or not successor.startswith("us_")
    ):
        raise ValueError("SPINOFF requires ratio and stable successor")
    if action_type == "SPINOFF" and cost_basis is not None and not 0 <= cost_basis < 1:
        raise ValueError("SPINOFF cost basis fraction must lie in [0, 1) when provided")


def _aware_iso(value: Any, *, required: bool) -> str:
    text = _text(value)
    if not text:
        if required:
            raise ValueError("timezone-aware action timestamp is required")
        return ""
    timestamp = pd.Timestamp(text)
    if timestamp.tzinfo is None:
        raise ValueError("action timestamp must be timezone-aware")
    return timestamp.isoformat()


def validate_execution_action_terms(action: Mapping[str, Any]) -> None:
    """Execution-context gate for approved corporate actions (D2-B).

    Membership replay may admit a SPINOFF without a cost-basis fraction,
    but any order/execution consumer must refuse it: trading-grade spinoff
    handling needs the tax-cost allocation.  Call this before using an
    action row to mutate positions, cash, or cost basis.
    """
    kind = str(action.get("action_type", "")).strip().upper()
    if kind != "SPINOFF":
        return
    raw = action.get("cost_basis_fraction")
    if raw is None or pd.isna(raw) or str(raw).strip() == "":
        raise ValueError(
            "execution use of SPINOFF action "
            f"{str(action.get('action_id', ''))!r} requires cost_basis_fraction"
        )


def _date_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return pd.Timestamp(text).date().isoformat()


def _number(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    number = float(text)
    if not pd.notna(number):
        raise ValueError("action numeric term is invalid")
    return number


def _bool(value: Any) -> bool:
    return _text(value).casefold() in {"1", "true", "yes"}


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip()


def _source_plain_text(payload: bytes) -> str:
    value = payload.decode("latin-1", errors="ignore")
    value = re.sub(
        r"<script\b.*?</script>|<style\b.*?</style>",
        " ",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(r"<[^>]+>", " ", value)
    return _normalize_text(html.unescape(value))


def _text(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


__all__ = [
    "ACTION_REVIEW_APPROVAL_VERSION",
    "ACTION_REVIEW_PROPOSAL_VERSION",
    "ACTION_REVIEW_TEMPLATE_VERSION",
    "ActionReviewApprovalResult",
    "ActionReviewResult",
    "approve_action_review",
    "prepare_action_review",
    "propose_action_review",
]
