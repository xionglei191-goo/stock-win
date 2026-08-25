from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import pandas as pd

from .models import SourceDependency, SourceRole, UNIVERSE_ID


@dataclass(frozen=True)
class MembershipReplayResult:
    memberships: pd.DataFrame
    replayed: Mapping[pd.Timestamp, frozenset[str]]
    gaps: tuple[dict[str, Any], ...]
    reconciled_anchor_count: int


def _source_available_at(source: SourceDependency) -> pd.Timestamp:
    metadata = dict(source.metadata)
    published = pd.to_datetime(source.published_at, errors="coerce", utc=True)
    observed = pd.to_datetime(source.observed_at, errors="coerce", utc=True)
    publication_verified = bool(
        metadata.get("publication_time_from_payload") is True
        or (
            source.source_id == "sec_nport_ivv"
            and metadata.get("series_id_verified_in_payload") is True
            and metadata.get("accepted_at") == source.published_at
        )
    )
    if publication_verified and not pd.isna(published):
        return published
    if pd.isna(published) or pd.isna(observed):
        return pd.NaT
    return max(published, observed)


def _is_causal_sec_anchor(source: SourceDependency) -> bool:
    metadata = dict(source.metadata)
    return bool(
        source.source_id == "sec_nport_ivv"
        and source.dataset == "fund_holdings_observed"
        and source.role == SourceRole.VALIDATION_ANCHOR
        and metadata.get("artifact_kind") == "raw_complete_edgar_submission"
        and metadata.get("series_id_verified_in_payload") is True
        and metadata.get("eligible_for_historical_signal") is False
        and metadata.get("accepted_at") == source.published_at
    )


def replay_causal_membership(
    holdings: pd.DataFrame,
    events: pd.DataFrame,
    decisions: Iterable[pd.Timestamp],
    sources: Iterable[SourceDependency],
    calendar: pd.DataFrame,
    corporate_actions: pd.DataFrame | None = None,
) -> MembershipReplayResult:
    """Replay membership using only evidence causally public by each close.

    A late SEC N-PORT filing is never backdated. Consecutive official anchors
    must reconcile exactly through captured S&P events before the earlier
    anchor can seed decisions after its actual acceptance timestamp.
    """

    columns = ["decision_date", "security_id", "universe_id"]
    source_values = tuple(sources)
    source_by_hash: dict[str, list[SourceDependency]] = {}
    for source in source_values:
        source_by_hash.setdefault(source.object_sha256, []).append(source)

    cal = calendar.copy()
    cal["session_date"] = pd.to_datetime(cal.get("session_date"), errors="coerce").dt.normalize()
    cal["market_close_utc"] = pd.to_datetime(
        cal.get("market_close"), errors="coerce", utc=True
    )
    if cal["session_date"].isna().any() or cal["market_close_utc"].isna().any():
        return MembershipReplayResult(
            pd.DataFrame(columns=columns), {},
            ({"code": "INVALID_XNYS_CALENDAR", "detail": "calendar rows are invalid"},), 0,
        )
    close_by_day = dict(
        zip(cal["session_date"], cal["market_close_utc"], strict=True)
    )

    def close_at_or_before(day: pd.Timestamp) -> pd.Timestamp | None:
        candidates = cal.loc[
            cal["session_date"].le(pd.Timestamp(day).normalize()),
            "market_close_utc",
        ]
        return None if candidates.empty else pd.Timestamp(candidates.iloc[-1])

    prepared_events = events.copy()
    if prepared_events.empty:
        prepared_events = pd.DataFrame(
            columns=[
                "event_id", "security_id", "event_type", "announced_at",
                "effective_at", "source_id", "evidence_sha256",
            ]
        )
    prepared_events["announced_utc"] = pd.to_datetime(
        prepared_events["announced_at"], errors="coerce", utc=True
    )
    prepared_events["effective_utc"] = pd.to_datetime(
        prepared_events["effective_at"], errors="coerce", utc=True
    )
    prepared_events["available_utc"] = pd.NaT
    prepared_events["available_utc"] = pd.to_datetime(
        prepared_events["available_utc"], utc=True
    )
    gaps: list[dict[str, Any]] = []
    for index, row in prepared_events.iterrows():
        expected_source_version = str(row.get("source_version", "")).strip()
        matching = [
            source
            for source in source_by_hash.get(
                str(row.get("evidence_sha256", "")).strip().lower(), []
            )
            if source.dataset == "membership_events"
            and source.role == SourceRole.SIGNAL_INPUT
            and source.source_id == str(row.get("source_id", "")).strip()
            and (
                not expected_source_version
                or source.source_version == expected_source_version
            )
        ]
        if len(matching) != 1:
            gaps.append(
                {"code": "UNPROVEN_MEMBERSHIP_EVENT", "event_id": str(row.get("event_id", ""))}
            )
            continue
        available = _source_available_at(matching[0])
        if pd.isna(available):
            gaps.append(
                {"code": "UNPROVEN_EVENT_AVAILABILITY", "event_id": str(row.get("event_id", ""))}
            )
            continue
        prepared_events.at[index, "available_utc"] = available
        effective_day = pd.Timestamp(row["effective_utc"]).tz_convert(
            "America/New_York"
        ).tz_localize(None).normalize()
        if effective_day not in close_by_day:
            gaps.append(
                {
                    "code": "EVENT_EFFECTIVE_DATE_NOT_XNYS_SESSION",
                    "event_id": str(row.get("event_id", "")),
                    "effective_date": effective_day.date().isoformat(),
                }
            )

    prepared_actions = (
        pd.DataFrame() if corporate_actions is None else corporate_actions.copy()
    )
    identity_action_types = {
        "TICKER_CHANGE", "RENAME", "SPLIT", "STOCK_DIVIDEND", "STOCK_MERGER",
        "REORGANIZATION",
    }
    identity_actions: list[dict[str, Any]] = []
    if not prepared_actions.empty:
        for raw in prepared_actions.to_dict(orient="records"):
            kind = str(raw.get("action_type", "")).strip().upper()
            predecessor = str(raw.get("security_id", "")).strip()
            successor = str(raw.get("successor_security_id", "")).strip()
            if (
                kind not in identity_action_types
                or not successor
                or successor == predecessor
            ):
                continue
            action_id = str(raw.get("action_id", "")).strip()
            matching = [
                source
                for source in source_by_hash.get(
                    str(raw.get("evidence_sha256", "")).strip().lower(), []
                )
                if source.dataset == "corporate_actions"
                and source.role == SourceRole.SIGNAL_INPUT
                and source.source_id == str(raw.get("source_id", "")).strip()
            ]
            if len(matching) != 1:
                gaps.append(
                    {
                        "code": "UNPROVEN_MEMBERSHIP_IDENTITY_ACTION",
                        "action_id": action_id,
                    }
                )
                continue
            source = matching[0]
            source_metadata = dict(source.metadata)
            published_at = pd.to_datetime(
                source.published_at, errors="coerce", utc=True
            )
            accepted_at = pd.to_datetime(
                source_metadata.get("accepted_at"), errors="coerce", utc=True
            )
            publication_verified = bool(
                source_metadata.get("publication_time_from_payload") is True
                and source_metadata.get("accepted_at_verified_in_payload") is True
                and not pd.isna(published_at)
                and not pd.isna(accepted_at)
                and published_at == accepted_at
            )
            available = _source_available_at(source)
            announced = pd.to_datetime(
                raw.get("announced_at"), errors="coerce", utc=True
            )
            effective = pd.to_datetime(
                raw.get("effective_at"), errors="coerce", utc=True
            )
            if (
                pd.isna(available)
                or not publication_verified
                or pd.isna(announced)
                or pd.isna(effective)
                or announced > effective
                or str(raw.get("terms_verified", "")).strip().casefold()
                not in {"true", "1"}
            ):
                gaps.append(
                    {
                        "code": "MEMBERSHIP_IDENTITY_ACTION_INVALID",
                        "action_id": action_id,
                        "publication_time_verified": publication_verified,
                    }
                )
                continue
            effective_day = pd.Timestamp(effective).tz_convert(
                "America/New_York"
            ).tz_localize(None).normalize()
            if effective_day not in close_by_day:
                gaps.append(
                    {
                        "code": "ACTION_EFFECTIVE_DATE_NOT_XNYS_SESSION",
                        "action_id": action_id,
                        "effective_date": effective_day.date().isoformat(),
                    }
                )
                continue
            identity_actions.append(
                {
                    "mutation_type": "IDENTITY_SUCCESSION",
                    "mutation_id": action_id,
                    "security_id": predecessor,
                    "successor_security_id": successor,
                    "announced_utc": announced,
                    "effective_utc": effective,
                    "available_utc": available,
                }
            )

    snapshots: list[dict[str, Any]] = []
    values = holdings.copy()
    values["as_of"] = pd.to_datetime(values.get("as_of_date"), errors="coerce").dt.normalize()
    for digest, group in values.groupby("content_sha256", dropna=False):
        matching = [
            source
            for source in source_by_hash.get(str(digest).strip().lower(), [])
            if source.dataset == "fund_holdings_observed"
        ]
        if len(matching) != 1:
            gaps.append({"code": "UNPROVEN_MEMBERSHIP_BASELINE", "evidence_sha256": str(digest)})
            continue
        source = matching[0]
        role = str(group["evidence_role"].iloc[0])
        is_live_signal = source.role == SourceRole.SIGNAL_INPUT and role == SourceRole.SIGNAL_INPUT.value
        is_sec_anchor = _is_causal_sec_anchor(source) and role == SourceRole.VALIDATION_ANCHOR.value
        if not is_live_signal and not is_sec_anchor:
            continue
        as_of_values = group["as_of"].dropna().unique()
        security_ids = group["security_id"].astype(str).str.strip()
        if len(as_of_values) != 1 or security_ids.eq("").any() or security_ids.duplicated().any():
            gaps.append({"code": "MEMBERSHIP_BASELINE_INVALID", "evidence_sha256": str(digest)})
            continue
        available = _source_available_at(source)
        if pd.isna(available):
            gaps.append({"code": "MEMBERSHIP_BASELINE_AVAILABILITY_INVALID", "evidence_sha256": str(digest)})
            continue
        snapshots.append(
            {
                "digest": str(digest),
                "as_of": pd.Timestamp(as_of_values[0]).normalize(),
                "available": available,
                "members": frozenset(security_ids),
                "kind": "SEC_ANCHOR" if is_sec_anchor else "OBSERVED_SIGNAL",
                "validated": is_live_signal,
            }
        )

    def apply_events(
        initial: frozenset[str], start_time: pd.Timestamp, cutoff: pd.Timestamp
    ) -> tuple[frozenset[str], list[str]]:
        state = set(initial)
        conflicts: list[str] = []
        event_mutations = prepared_events.loc[
            prepared_events["effective_utc"].notna()
            & prepared_events["announced_utc"].notna()
            & prepared_events["available_utc"].notna()
            & prepared_events["effective_utc"].gt(start_time)
            & prepared_events["effective_utc"].le(cutoff)
            & prepared_events["announced_utc"].le(cutoff)
            & prepared_events["available_utc"].le(cutoff)
        ].copy()
        event_mutations["mutation_type"] = event_mutations["event_type"].astype(
            str
        ).str.strip().str.upper()
        event_mutations["mutation_id"] = event_mutations["event_id"].astype(str)
        event_mutations["successor_security_id"] = ""
        mutations = event_mutations[
            [
                "mutation_type", "mutation_id", "security_id",
                "successor_security_id", "announced_utc", "effective_utc",
                "available_utc",
            ]
        ].to_dict(orient="records")
        mutations.extend(
            action
            for action in identity_actions
            if action["effective_utc"] > start_time
            and action["effective_utc"] <= cutoff
            and action["announced_utc"] <= cutoff
            and action["available_utc"] <= cutoff
        )
        mutations.sort(
            key=lambda row: (
                row["effective_utc"], row["announced_utc"], row["mutation_id"]
            )
        )
        for row in mutations:
            kind = str(row["mutation_type"]).strip().upper()
            security_id = str(row["security_id"]).strip()
            event_id = str(row["mutation_id"])
            if kind == "ADD":
                if security_id in state:
                    conflicts.append(event_id)
                state.add(security_id)
            elif kind == "REMOVE":
                if security_id not in state:
                    conflicts.append(event_id)
                state.discard(security_id)
            elif kind == "IDENTITY_SUCCESSION":
                if security_id in state:
                    state.discard(security_id)
                    state.add(str(row["successor_security_id"]).strip())
            else:
                conflicts.append(event_id)
        return frozenset(state), conflicts

    for snapshot in snapshots:
        origin_cutoff = close_at_or_before(snapshot["as_of"])
        if origin_cutoff is None or snapshot["available"] < origin_cutoff:
            gaps.append(
                {
                    "code": "MEMBERSHIP_BASELINE_TIME_INVALID",
                    "evidence_sha256": snapshot["digest"],
                }
            )
            snapshot["validated"] = False
            continue
        if snapshot["kind"] == "SEC_ANCHOR":
            available_members, conflicts = apply_events(
                snapshot["members"], origin_cutoff, snapshot["available"]
            )
            snapshot["available_members"] = available_members
            if conflicts:
                gaps.append(
                    {
                        "code": "ANCHOR_ACCEPTANCE_WINDOW_CONFLICT",
                        "evidence_sha256": snapshot["digest"],
                        "event_ids": conflicts[:20],
                    }
                )
                snapshot["validated"] = False
        else:
            snapshot["available_members"] = snapshot["members"]

    sec_anchors = sorted(
        (item for item in snapshots if item["kind"] == "SEC_ANCHOR"),
        key=lambda item: (item["as_of"], item["available"], item["digest"]),
    )
    reconciled = 0
    for prior, following in zip(sec_anchors, sec_anchors[1:], strict=False):
        if not prior.get("available_members") or prior["validated"] is False and any(
            gap.get("evidence_sha256") == prior["digest"]
            and gap.get("code") in {
                "MEMBERSHIP_BASELINE_TIME_INVALID",
                "ANCHOR_ACCEPTANCE_WINDOW_CONFLICT",
            }
            for gap in gaps
        ):
            continue
        report_cutoff = close_at_or_before(following["as_of"])
        if report_cutoff is None or prior["available"] > report_cutoff:
            gaps.append(
                {
                    "code": "ANCHOR_RECONCILIATION_WINDOW_INVALID",
                    "anchor_date": following["as_of"].date().isoformat(),
                }
            )
            continue
        replayed, conflicts = apply_events(
            prior["available_members"], prior["available"], report_cutoff
        )
        if conflicts or replayed != following["members"]:
            gaps.append(
                {
                    "code": "QUARTERLY_ANCHOR_RECONCILIATION_FAILED",
                    "anchor_date": following["as_of"].date().isoformat(),
                    "missing": sorted(following["members"] - replayed)[:20],
                    "extra": sorted(replayed - following["members"])[:20],
                    "conflicting_event_ids": conflicts[:20],
                }
            )
            continue
        prior["validated"] = True
        reconciled += 1

    rows: list[dict[str, Any]] = []
    replayed_by_day: dict[pd.Timestamp, frozenset[str]] = {}
    for raw_decision in decisions:
        decision = pd.Timestamp(raw_decision).tz_localize(None).normalize()
        cutoff = close_by_day.get(decision)
        eligible = [
            item
            for item in snapshots
            if item["validated"] is True and item["available"] <= cutoff
        ] if cutoff is not None else []
        if not eligible:
            gaps.append(
                {"code": "MISSING_DECISION_TIME_BASELINE", "decision_date": decision.date().isoformat()}
            )
            continue
        baseline = max(
            eligible,
            key=lambda item: (item["available"], item["as_of"], item["digest"]),
        )
        state, conflicts = apply_events(
            baseline["available_members"], baseline["available"], cutoff
        )
        if conflicts:
            gaps.append(
                {
                    "code": "MEMBERSHIP_EVENT_STATE_CONFLICT",
                    "decision_date": decision.date().isoformat(),
                    "event_ids": conflicts[:20],
                }
            )
        replayed_by_day[decision] = state
        rows.extend(
            {
                "universe_id": UNIVERSE_ID,
                "decision_date": decision,
                "security_id": security_id,
            }
            for security_id in sorted(state)
        )
    return MembershipReplayResult(
        pd.DataFrame(rows, columns=columns),
        replayed_by_day,
        tuple(gaps),
        reconciled,
    )


__all__ = ["MembershipReplayResult", "replay_causal_membership"]
