from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import exchange_calendars as xcals
import pandas as pd

from .hashing import canonical_json_bytes, sha256_file, sha256_json
from .membership_replay import replay_causal_membership
from .identity_bridge import normalized_issuer_name
from .models import SourceDependency
from .store import USPITStore


MEMBERSHIP_AUDIT_VERSION = "us-pit-membership-audit-v2"


def _text(value: Any) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


@dataclass(frozen=True)
class MembershipAuditResult:
    path: Path
    report: dict[str, Any]


def _calendar(start: str, end: str) -> pd.DataFrame:
    schedule = xcals.get_calendar("XNYS").schedule.loc[start:end]
    return pd.DataFrame(
        {
            "session_date": pd.DatetimeIndex(schedule.index)
            .tz_localize(None)
            .normalize(),
            "market_close": [
                pd.Timestamp(value)
                .tz_convert("America/New_York")
                .isoformat()
                for value in schedule["close"]
            ],
        }
    )


def audit_membership_candidates(
    store: USPITStore,
    normalization_dir: Path | str,
    event_candidate_dir: Path | str,
    source_batch_ids: Iterable[str],
    output_dir: Path | str,
    *,
    decision_start: str = "2021-08-01",
    decision_end: str = "2026-07-31",
) -> MembershipAuditResult:
    """Audit unapproved candidates without producing buildable membership."""

    normalization = Path(normalization_dir).resolve()
    candidates = Path(event_candidate_dir).resolve()
    normalization_manifest = normalization / "manifest.json"
    candidate_manifest = candidates / "manifest.json"
    holdings_path = normalization / "fund_holdings_observed_candidate.parquet"
    events_path = candidates / "membership_event_candidates.parquet"
    if not all(
        path.is_file()
        for path in (normalization_manifest, candidate_manifest, holdings_path, events_path)
    ):
        raise ValueError("membership audit inputs are incomplete")
    normalized_meta = json.loads(normalization_manifest.read_text(encoding="utf-8"))
    candidate_meta = json.loads(candidate_manifest.read_text(encoding="utf-8"))
    if (
        normalized_meta.get("candidate_only") is not True
        or candidate_meta.get("candidate_only") is not True
        or candidate_meta.get("direct_build_allowed") is not False
        or candidate_meta.get("artifact_sha256") != sha256_file(events_path)
    ):
        raise ValueError("membership audit inputs failed candidate-only integrity policy")
    candidate_identity = dict(candidate_meta)
    candidate_set_id = str(candidate_identity.pop("candidate_set_id", ""))
    if candidate_set_id != sha256_json(candidate_identity):
        raise ValueError("membership event candidate identity is invalid")

    batch_ids = tuple(sorted(set(str(item).strip() for item in source_batch_ids)))
    if not batch_ids or any(not item for item in batch_ids):
        raise ValueError("membership audit requires explicit source batches")
    sources: list[SourceDependency] = []
    for batch_id in batch_ids:
        sources.extend(store.load_source_batch(batch_id).dependencies)

    holdings = pd.read_parquet(holdings_path)
    holdings = holdings.loc[holdings["source_id"].astype(str).eq("sec_nport_ivv")].copy()
    holdings["security_id"] = (
        "us_"
        + holdings["identity_candidate_key"]
        .fillna("")
        .astype(str)
        .str.replace(":", "_", regex=False)
        .str.lower()
    )
    holdings = holdings[
        ["as_of_date", "content_sha256", "evidence_role", "security_id"]
    ]
    event_candidates = pd.read_parquet(events_path)
    if "source_version" not in event_candidates.columns:
        event_candidates["source_version"] = ""
    unresolved = event_candidates.loc[
        event_candidates["suggested_security_id"].fillna("").astype(str).eq("")
    ].copy()
    event_candidates["security_id"] = event_candidates["suggested_security_id"].where(
        event_candidates["suggested_security_id"].fillna("").astype(str).ne(""),
        "us_unresolved_" + event_candidates["ticker_at_announcement"].astype(str).str.lower(),
    )
    events = event_candidates.rename(
        columns={"event_candidate_id": "event_id"}
    )[
        [
            "event_id", "security_id", "event_type", "announced_at",
            "effective_at", "source_id", "source_version", "evidence_sha256",
        ]
    ]
    calendar = _calendar("2019-10-01", decision_end)
    sessions = pd.DatetimeIndex(calendar["session_date"])
    window = sessions[
        (sessions >= pd.Timestamp(decision_start))
        & (sessions <= pd.Timestamp(decision_end))
    ]
    decisions = pd.DatetimeIndex(
        [
            group.max()
            for _, group in pd.Series(window, index=window).groupby(
                window.to_period("M")
            )
        ]
    )
    replay = replay_causal_membership(
        holdings, events, decisions, tuple(sources), calendar
    )
    event_details = {
        str(row["event_candidate_id"]): {
            "ticker": _text(row.get("ticker_at_announcement")),
            "company_name": _text(row.get("company_name")),
            "event_type": _text(row.get("event_type")),
            "announced_at": _text(row.get("announced_at")),
            "effective_at": _text(row.get("effective_at")),
            "source_url": _text(row.get("source_url")),
            "evidence_sha256": _text(row.get("evidence_sha256")),
            "suggested_security_id": _text(row.get("suggested_security_id")),
        }
        for row in event_candidates.to_dict(orient="records")
    }
    enriched_gaps = []
    for gap in replay.gaps:
        enriched = dict(gap)
        event_ids = []
        if enriched.get("event_id"):
            event_ids.append(str(enriched["event_id"]))
        event_ids.extend(str(value) for value in enriched.get("event_ids", []))
        details = [event_details[event_id] for event_id in event_ids if event_id in event_details]
        if details:
            enriched["event_details"] = details
        enriched_gaps.append(enriched)
    gap_counts: dict[str, int] = {}
    for gap in enriched_gaps:
        code = str(gap.get("code", "UNKNOWN"))
        gap_counts[code] = gap_counts.get(code, 0) + 1

    event_conflict_roots: dict[str, dict[str, Any]] = {}
    for gap in enriched_gaps:
        if gap.get("code") != "MEMBERSHIP_EVENT_STATE_CONFLICT":
            continue
        decision_date = _text(gap.get("decision_date"))
        details_by_id = {
            str(detail.get("event_id", "")): detail
            for detail in gap.get("event_details", [])
            if isinstance(detail, dict)
        }
        for event_id in (str(value) for value in gap.get("event_ids", [])):
            root = event_conflict_roots.setdefault(
                event_id,
                {
                    "event_id": event_id,
                    "affected_decision_dates": [],
                    "event_detail": details_by_id.get(event_id, {}),
                },
            )
            if decision_date and decision_date not in root["affected_decision_dates"]:
                root["affected_decision_dates"].append(decision_date)
            if not root["event_detail"]:
                matching = [
                    item for item in gap.get("event_details", [])
                    if isinstance(item, dict)
                ]
                if len(matching) == 1:
                    root["event_detail"] = matching[0]
    conflict_root_rows = []
    for event_id in sorted(event_conflict_roots):
        root = event_conflict_roots[event_id]
        dates = sorted(root["affected_decision_dates"])
        conflict_root_rows.append(
            {
                "event_id": event_id,
                "affected_decision_count": len(dates),
                "first_affected_decision": dates[0] if dates else "",
                "last_affected_decision": dates[-1] if dates else "",
                "affected_decision_dates": dates,
                "event_detail": root["event_detail"],
            }
        )

    identity_candidates = pd.read_parquet(
        normalization / "security_identity_candidates.parquet"
    )
    identity_candidates = identity_candidates.loc[
        identity_candidates["source_id"].astype(str).eq("sec_nport_ivv")
    ].copy()
    identity_candidates["security_id"] = (
        "us_"
        + identity_candidates["identity_candidate_key"]
        .fillna("")
        .astype(str)
        .str.replace(":", "_", regex=False)
        .str.lower()
    )
    identity_candidates["issuer_name_key"] = identity_candidates[
        "issuer_name"
    ].map(normalized_issuer_name)
    identity_candidates["as_of_day"] = pd.to_datetime(
        identity_candidates["as_of_date"], errors="coerce"
    ).dt.normalize()

    def identity_as_of(security_id: str, anchor_day: pd.Timestamp) -> pd.Series | None:
        group = identity_candidates.loc[
            identity_candidates["security_id"].astype(str).eq(security_id)
            & identity_candidates["as_of_day"].le(anchor_day)
        ]
        if group.empty:
            return None
        return group.sort_values(
            ["as_of_day", "source_row_number"], kind="stable"
        ).iloc[-1]
    transition_rows: list[dict[str, Any]] = []
    for gap in replay.gaps:
        if gap.get("code") != "QUARTERLY_ANCHOR_RECONCILIATION_FAILED":
            continue
        old_ids = [str(value) for value in gap.get("extra", [])]
        new_ids = [str(value) for value in gap.get("missing", [])]
        anchor_day = pd.Timestamp(str(gap.get("anchor_date", ""))).normalize()
        for old_id in old_ids:
            old = identity_as_of(old_id, anchor_day)
            if old is None:
                continue
            matches = []
            for new_id in new_ids:
                new = identity_as_of(new_id, anchor_day)
                if new is None:
                    continue
                old_lei = _text(old.get("lei"))
                new_lei = _text(new.get("lei"))
                same_lei = bool(
                    old_lei
                    and new_lei
                    and old_lei.casefold() == new_lei.casefold()
                )
                same_name = bool(
                    _text(old.get("issuer_name_key"))
                    and _text(old.get("issuer_name_key"))
                    == _text(new.get("issuer_name_key"))
                )
                if same_lei or same_name:
                    matches.append((new_id, new, "SAME_LEI" if same_lei else "SAME_NORMALIZED_ISSUER"))
            if len(matches) != 1:
                continue
            new_id, new, basis = matches[0]
            transition_rows.append(
                {
                    "anchor_date": str(gap.get("anchor_date", "")),
                    "predecessor_security_id": old_id,
                    "successor_security_id": new_id,
                    "predecessor_name": _text(old.get("issuer_name")),
                    "successor_name": _text(new.get("issuer_name")),
                    "predecessor_isin": _text(old.get("isin")),
                    "successor_isin": _text(new.get("isin")),
                    "predecessor_cusip": _text(old.get("cusip")),
                    "successor_cusip": _text(new.get("cusip")),
                    "predecessor_lei": _text(old.get("lei")),
                    "successor_lei": _text(new.get("lei")),
                    "predecessor_cik": _text(old.get("cik")),
                    "successor_cik": _text(new.get("cik")),
                    "predecessor_ticker": _text(old.get("ticker")),
                    "successor_ticker": _text(new.get("ticker")),
                    "match_basis": basis,
                    "suggested_action_type": "",
                    "status": "REVIEW_REQUIRED",
                    "approved": False,
                    "review_note": (
                        "Obtain a frozen issuer/SEC/exchange corporate-action document; "
                        "do not infer transaction type or terms from anchor identity alone."
                    ),
                }
            )
    transitions = pd.DataFrame(transition_rows).drop_duplicates(
        ["anchor_date", "predecessor_security_id", "successor_security_id"]
    ) if transition_rows else pd.DataFrame(
        columns=[
            "anchor_date", "predecessor_security_id", "successor_security_id",
            "predecessor_name", "successor_name", "predecessor_isin",
            "successor_isin", "predecessor_cusip", "successor_cusip",
            "predecessor_lei", "successor_lei", "predecessor_cik",
            "successor_cik", "predecessor_ticker", "successor_ticker",
            "match_basis", "suggested_action_type", "status", "approved",
            "review_note",
        ]
    )
    transition_maps: dict[str, dict[str, str]] = {}
    for row in transitions.to_dict(orient="records"):
        transition_maps.setdefault(str(row["anchor_date"]), {})[
            str(row["predecessor_security_id"])
        ] = str(row["successor_security_id"])
    residual_event_rows: list[dict[str, Any]] = []
    for gap in replay.gaps:
        if gap.get("code") != "QUARTERLY_ANCHOR_RECONCILIATION_FAILED":
            continue
        anchor_date = str(gap.get("anchor_date", ""))
        anchor_day = pd.Timestamp(anchor_date).normalize()
        mapping = transition_maps.get(anchor_date, {})
        transformed_extra = {
            mapping.get(str(security_id), str(security_id))
            for security_id in gap.get("extra", [])
        }
        expected = {str(value) for value in gap.get("missing", [])}
        residuals = (
            ("REMOVE", sorted(transformed_extra - expected)),
            ("ADD", sorted(expected - transformed_extra)),
        )
        for event_type, security_ids in residuals:
            for security_id in security_ids:
                identity = identity_as_of(security_id, anchor_day)
                residual_event_rows.append(
                    {
                        "request_id": sha256_json(
                            {
                                "anchor_date": anchor_date,
                                "event_type": event_type,
                                "security_id": security_id,
                            }
                        ),
                        "anchor_date": anchor_date,
                        "event_type": event_type,
                        "security_id": security_id,
                        "issuer_name": "" if identity is None else _text(identity.get("issuer_name")),
                        "ticker": "" if identity is None else _text(identity.get("ticker")),
                        "isin": "" if identity is None else _text(identity.get("isin")),
                        "cusip": "" if identity is None else _text(identity.get("cusip")),
                        "status": "EVIDENCE_REQUIRED",
                        "approved": False,
                        "required_evidence": (
                            "Freeze a decision-time-visible S&P 500 ADD/REMOVE announcement; "
                            "identity similarity or a later fund holding is not sufficient."
                        ),
                    }
                )
    residual_event_requests = pd.DataFrame(residual_event_rows).drop_duplicates(
        ["anchor_date", "event_type", "security_id"]
    ) if residual_event_rows else pd.DataFrame(
        columns=[
            "request_id", "anchor_date", "event_type", "security_id",
            "issuer_name", "ticker", "isin", "cusip", "status", "approved",
            "required_evidence",
        ]
    )
    report = {
        "format_version": MEMBERSHIP_AUDIT_VERSION,
        "status": "REVIEW_REQUIRED" if not replay.gaps and unresolved.empty else "DATA_BLOCKED",
        "candidate_only": True,
        "direct_build_allowed": False,
        "normalization_id": str(normalized_meta.get("normalization_id", "")),
        "normalization_manifest_sha256": sha256_file(normalization_manifest),
        "candidate_set_id": candidate_set_id,
        "candidate_manifest_sha256": sha256_file(candidate_manifest),
        "source_batch_ids": list(batch_ids),
        "decision_start": decision_start,
        "decision_end": decision_end,
        "decision_months": len(decisions),
        "replayed_months": len(replay.replayed),
        "reconciled_anchor_intervals": replay.reconciled_anchor_count,
        "unresolved_identity_events": int(len(unresolved)),
        "identity_transition_candidates": int(len(transitions)),
        "residual_membership_event_requests": int(len(residual_event_requests)),
        "residual_membership_event_counts": {
            str(key): int(value)
            for key, value in residual_event_requests["event_type"].value_counts().items()
        },
        "gap_counts": gap_counts,
        "membership_event_conflict_root_count": len(conflict_root_rows),
        "membership_event_conflict_roots": conflict_root_rows,
        "gaps": enriched_gaps,
        "policy": {
            "unapproved_candidates_used_for_diagnostics_only": True,
            "placeholder_identity_never_buildable": True,
            "historical_ishares_not_signal_input": True,
        },
    }
    report["audit_id"] = sha256_json(report)
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"membership audit output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        (staging / "membership_audit.json").write_bytes(canonical_json_bytes(report))
        unresolved.to_parquet(staging / "unresolved_identity_events.parquet", index=False)
        transitions.to_parquet(
            staging / "identity_transition_candidates.parquet", index=False
        )
        residual_event_requests.to_parquet(
            staging / "residual_membership_event_requests.parquet", index=False
        )
        (staging / "manifest.json").write_bytes(
            canonical_json_bytes(
                {
                    "audit_id": report["audit_id"],
                    "membership_audit_sha256": sha256_file(
                        staging / "membership_audit.json"
                    ),
                    "unresolved_identity_sha256": sha256_file(
                        staging / "unresolved_identity_events.parquet"
                    ),
                    "identity_transition_candidates_sha256": sha256_file(
                        staging / "identity_transition_candidates.parquet"
                    ),
                    "residual_membership_event_requests_sha256": sha256_file(
                        staging / "residual_membership_event_requests.parquet"
                    ),
                    "status": report["status"],
                    "candidate_only": True,
                    "direct_build_allowed": False,
                }
            )
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return MembershipAuditResult(output, report)


__all__ = [
    "MEMBERSHIP_AUDIT_VERSION",
    "MembershipAuditResult",
    "audit_membership_candidates",
]
