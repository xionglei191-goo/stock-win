from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

import exchange_calendars as xcals
import pandas as pd

from research_platform.us_market_time import ny_session_date, ny_session_dates

from .hashing import sha256_json
from .membership_replay import replay_causal_membership
from .models import (
    QUALITY_CONTRACT_REVISION,
    LicenseClass,
    QUALITY_POLICY_VERSION,
    QualityIssue,
    QualityReport,
    QualitySeverity,
    ReleaseStatus,
    SourceDependency,
    SourceRole,
    UNIVERSE_ID,
)
from .sources_fees import FEE_EVIDENCE_CONTRACT_VERSION, fee_rate_entries


REQUIRED_ARTIFACT_COLUMNS: dict[str, frozenset[str]] = {
    "fund_holdings_observed": frozenset(
        {
            "as_of_date",
            "published_at",
            "observed_at",
            "url",
            "source_version",
            "content_sha256",
            "evidence_role",
            "security_id",
        }
    ),
    "membership_events": frozenset(
        {
            "event_id",
            "security_id",
            "event_type",
            "announced_at",
            "effective_at",
            "source_id",
            "evidence_sha256",
        }
    ),
    "membership_monthly": frozenset({"universe_id", "decision_date", "security_id"}),
    "security_master": frozenset(
        {
            "security_id",
            "issuer_id",
            "primary_identifier_type",
            "primary_identifier",
            "asset_class",
        }
    ),
    "identifiers": frozenset(
        {
            "security_id",
            "identifier_type",
            "identifier_value",
            "valid_from",
            "valid_to",
        }
    ),
    "listing_aliases": frozenset(
        {
            "security_id",
            "ticker",
            "vendor_code",
            "exchange",
            "valid_from",
            "valid_to",
        }
    ),
    "corporate_actions": frozenset(
        {
            "action_id",
            "security_id",
            "action_type",
            "announced_at",
            "effective_at",
            "pay_date",
            "terms_verified",
            "source_id",
            "evidence_sha256",
        }
    ),
    "session_exceptions": frozenset(
        {
            "security_id",
            "session_date",
            "exception_type",
            "verified",
            "source_id",
            "evidence_sha256",
        }
    ),
    "bars_raw": frozenset(
        {"security_id", "date", "Open", "High", "Low", "Close", "Volume"}
    ),
    "bars_vendor_front": frozenset(
        {"security_id", "date", "Open", "High", "Low", "Close", "Volume"}
    ),
    "bars_pit_signal": frozenset(
        {
            "decision_date",
            "security_id",
            "date",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
        }
    ),
    "benchmarks": frozenset(
        {
            "symbol",
            "date",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "adjustment",
            "TotalReturnClose",
            "total_return_source_id",
            "total_return_evidence_sha256",
        }
    ),
    "xnys_calendar": frozenset({"session_date", "market_open", "market_close"}),
    "execution_fee_schedule": frozenset(
        {
            "effective_from",
            "effective_to",
            "commission_rate",
            "min_commission",
            "slippage_rate",
            "sec_sell_fee_rate",
            "finra_taf_per_share",
            "finra_taf_cap",
            "fee_model_id",
            "sec_evidence_url",
            "finra_evidence_url",
            "sec_evidence_sha256",
            "finra_evidence_sha256",
            "fee_derivation_sha256",
        }
    ),
    "bar_coverage": frozenset(
        {
            "decision_date",
            "security_id",
            "expected_sessions",
            "raw_sessions",
            "signal_sessions",
            "explained_missing_sessions",
            "passed",
        }
    ),
    "anchor_reconciliations": frozenset(
        {
            "anchor_date",
            "status",
            "unexplained_additions",
            "unexplained_removals",
            "source_id",
            "evidence_sha256",
        }
    ),
    "lifecycle_reconciliations": frozenset(
        {
            "scope",
            "coverage_kind",
            "current_through",
            "action_id",
            "security_id",
            "status",
            "includes_delisted",
            "source_id",
            "evidence_sha256",
        }
    ),
}


def frame_derivation_sha256(frame: pd.DataFrame) -> str:
    """Hash normalized rows independently of Parquet encoder details."""

    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        normalized: dict[str, Any] = {}
        for key, item in row.items():
            if isinstance(item, pd.Timestamp):
                normalized[str(key)] = item.isoformat()
            elif isinstance(item, float) and math.isnan(item):
                normalized[str(key)] = None
            elif pd.isna(item):
                normalized[str(key)] = None
            elif hasattr(item, "item"):
                normalized[str(key)] = item.item()
            else:
                normalized[str(key)] = item
        records.append(normalized)
    return sha256_json(records)


@dataclass(frozen=True)
class QualityPolicy:
    min_decision_months: int = 60
    min_warmup_sessions: int = 282
    min_anchor_reconciliations: int = 1
    universe_id: str = UNIVERSE_ID
    version: str = QUALITY_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.min_decision_months < 1:
            raise ValueError("min_decision_months must be positive")
        if self.min_warmup_sessions < 1:
            raise ValueError("min_warmup_sessions must be positive")
        if self.min_anchor_reconciliations < 1:
            raise ValueError("min_anchor_reconciliations must be positive")


class USPITQualityValidator:
    def __init__(self, policy: QualityPolicy | None = None) -> None:
        self.policy = policy or QualityPolicy()

    def validate(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        sources: Iterable[SourceDependency],
    ) -> QualityReport:
        issues: list[QualityIssue] = []
        metrics: dict[str, Any] = {}
        source_items = tuple(sources)

        for dataset, required in REQUIRED_ARTIFACT_COLUMNS.items():
            frame = artifacts.get(dataset)
            if frame is None:
                self._issue(
                    issues,
                    "MISSING_ARTIFACT",
                    QualitySeverity.CRITICAL,
                    dataset,
                    "required release artifact is missing",
                )
                continue
            missing = sorted(required - set(frame.columns))
            if missing:
                self._issue(
                    issues,
                    "SCHEMA_MISMATCH",
                    QualitySeverity.CRITICAL,
                    dataset,
                    "required columns are missing",
                    {"missing_columns": missing},
                )

        self._validate_sources(source_items, issues)
        calendar = self._validate_calendar(
            artifacts.get("xnys_calendar"), source_items, issues, metrics
        )
        memberships = self._validate_membership(
            artifacts.get("membership_monthly"), calendar, issues, metrics
        )
        self._validate_evidence_tables(artifacts, source_items, issues)
        replayed_memberships = self._validate_membership_replay(
            artifacts,
            memberships,
            source_items,
            issues,
            metrics,
        )
        self._validate_identity(artifacts, memberships, issues, metrics)
        self._validate_bars(
            artifacts, memberships, calendar, source_items, issues, metrics
        )
        lifecycle_covered = self._validate_reconciliations(
            artifacts,
            memberships,
            replayed_memberships,
            source_items,
            issues,
            metrics,
        )
        self._validate_actions(artifacts, issues, metrics)
        self._validate_session_exceptions(artifacts.get("session_exceptions"), issues)
        self._validate_fees(
            artifacts.get("execution_fee_schedule"), calendar, source_items, issues
        )

        hard_failure = any(
            issue.severity in {QualitySeverity.CRITICAL, QualitySeverity.HIGH}
            for issue in issues
        )
        # This field is derived exclusively from complete per-security lineage.
        # A caller-supplied includes_delisted=True value is never an unlock.
        includes_delisted = bool(lifecycle_covered and not hard_failure)
        status = ReleaseStatus.DATA_READY if not hard_failure else ReleaseStatus.DATA_BLOCKED
        metrics["critical_issues"] = sum(
            issue.severity == QualitySeverity.CRITICAL for issue in issues
        )
        metrics["high_issues"] = sum(issue.severity == QualitySeverity.HIGH for issue in issues)
        metrics["includes_delisted"] = includes_delisted
        metrics["quality_contract_revision"] = QUALITY_CONTRACT_REVISION
        return QualityReport(
            policy_version=self.policy.version,
            status=status,
            includes_delisted=includes_delisted,
            issues=tuple(issues),
            metrics=metrics,
        )

    def _validate_sources(
        self,
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
    ) -> None:
        if not sources:
            self._issue(
                issues,
                "NO_SOURCE_PROVENANCE",
                QualitySeverity.CRITICAL,
                "manifest",
                "release has no source dependencies",
            )
            return
        invalid_source_rows = 0
        signal_policy_violations = 0
        for item in sources:
            try:
                observed = pd.Timestamp(item.observed_at)
            except (TypeError, ValueError):
                observed = pd.NaT
            try:
                published = (
                    pd.NaT
                    if item.published_at is None
                    else pd.Timestamp(item.published_at)
                )
            except (TypeError, ValueError):
                published = pd.NaT
            try:
                as_of = (
                    pd.NaT if item.as_of_date is None else pd.Timestamp(item.as_of_date)
                )
            except (TypeError, ValueError):
                as_of = pd.NaT
            digest = str(item.object_sha256)
            invalid = (
                not item.source_id.strip()
                or not item.source_version.strip()
                or not item.dataset.strip()
                or pd.isna(observed)
                or observed.tzinfo is None
                or (item.published_at is not None and pd.isna(published))
                or (
                    not pd.isna(published)
                    and published.tzinfo is None
                )
                or (
                    not pd.isna(published)
                    and published.tzinfo is not None
                    and published.tz_convert("UTC") > observed.tz_convert("UTC")
                )
                or (item.as_of_date is not None and pd.isna(as_of))
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            )
            invalid_source_rows += int(invalid)
            if item.role == SourceRole.SIGNAL_INPUT and item.metadata.get(
                "eligible_for_historical_signal"
            ) is False:
                signal_policy_violations += 1
        if invalid_source_rows:
            self._issue(
                issues,
                "SOURCE_PROVENANCE_INVALID",
                QualitySeverity.CRITICAL,
                "manifest",
                "source dependencies require valid identities, hashes, and causal timestamps",
                {"affected_rows": invalid_source_rows},
            )
        if signal_policy_violations:
            self._issue(
                issues,
                "SOURCE_SIGNAL_POLICY_VIOLATION",
                QualitySeverity.CRITICAL,
                "manifest",
                "a source explicitly marked ineligible for historical signals was assigned SIGNAL_INPUT",
                {"affected_rows": signal_policy_violations},
            )
        unlicensed = [
            item.source_id
            for item in sources
            if item.license_class == LicenseClass.UNLICENSED_REFERENCE
        ]
        if unlicensed:
            self._issue(
                issues,
                "UNLICENSED_DEPENDENCY",
                QualitySeverity.CRITICAL,
                "manifest",
                "unlicensed reference data cannot be a READY release dependency",
                {"source_ids": sorted(set(unlicensed))},
            )
        if not any(item.role == SourceRole.SIGNAL_INPUT for item in sources):
            self._issue(
                issues,
                "NO_SIGNAL_TIME_SOURCE",
                QualitySeverity.CRITICAL,
                "manifest",
                "release has no source that was available to the signal process",
            )
        membership_datasets = {
            "fund_holdings_observed",
            "membership_events",
            "membership_monthly",
            "membership_baseline",
        }
        if not any(
            item.role == SourceRole.SIGNAL_INPUT and item.dataset in membership_datasets
            for item in sources
        ):
            self._issue(
                issues,
                "NO_PIT_MEMBERSHIP_SOURCE",
                QualitySeverity.CRITICAL,
                "manifest",
                "release has no decision-time membership evidence source",
            )
        if not any(
            item.role == SourceRole.SIGNAL_INPUT
            and item.dataset in {"bars_raw", "tdx_us_daily"}
            for item in sources
        ):
            self._issue(
                issues,
                "NO_RAW_BAR_SOURCE",
                QualitySeverity.CRITICAL,
                "manifest",
                "release has no captured raw US bar source",
            )
        if not any(item.role == SourceRole.VALIDATION_ANCHOR for item in sources):
            self._issue(
                issues,
                "NO_OFFICIAL_ANCHOR_SOURCE",
                QualitySeverity.CRITICAL,
                "manifest",
                "release has no captured quarterly validation anchor",
            )
        duplicate_keys = pd.Series(
            [
                (
                    item.source_id,
                    item.dataset,
                    item.as_of_date,
                    item.object_sha256,
                )
                for item in sources
            ]
        ).duplicated()
        if bool(duplicate_keys.any()):
            self._issue(
                issues,
                "DUPLICATE_SOURCE_DEPENDENCY",
                QualitySeverity.HIGH,
                "manifest",
                "source dependency grain is not unique",
                {"duplicate_count": int(duplicate_keys.sum())},
            )

    def _validate_calendar(
        self,
        frame: pd.DataFrame | None,
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> pd.DatetimeIndex:
        if not self._usable(frame, "xnys_calendar"):
            return pd.DatetimeIndex([])
        assert frame is not None
        sessions = pd.to_datetime(frame["session_date"], errors="coerce").dt.normalize()
        if sessions.isna().any():
            self._issue(
                issues,
                "INVALID_SESSION_DATE",
                QualitySeverity.CRITICAL,
                "xnys_calendar",
                "session_date contains invalid values",
                {"invalid_count": int(sessions.isna().sum())},
            )
        if sessions.duplicated().any():
            self._issue(
                issues,
                "DUPLICATE_SESSION",
                QualitySeverity.CRITICAL,
                "xnys_calendar",
                "calendar session grain is not unique",
                {"duplicate_count": int(sessions.duplicated().sum())},
            )
        timezone_invalid = 0
        open_values: list[pd.Timestamp] = []
        close_values: list[pd.Timestamp] = []
        for raw_open, raw_close in zip(frame["market_open"], frame["market_close"], strict=True):
            try:
                opened = pd.Timestamp(raw_open)
                closed = pd.Timestamp(raw_close)
                if opened.tzinfo is None or closed.tzinfo is None:
                    timezone_invalid += 1
                open_values.append(opened)
                close_values.append(closed)
            except (TypeError, ValueError):
                timezone_invalid += 1
        if timezone_invalid:
            self._issue(
                issues,
                "CALENDAR_TIMEZONE_INVALID",
                QualitySeverity.CRITICAL,
                "xnys_calendar",
                "market open/close must be timezone-aware timestamps",
                {"invalid_count": timezone_invalid},
            )
        invalid_order = sum(
            opened.tzinfo is not None
            and closed.tzinfo is not None
            and opened.tz_convert("UTC") >= closed.tz_convert("UTC")
            for opened, closed in zip(open_values, close_values, strict=True)
        )
        if invalid_order:
            self._issue(
                issues,
                "CALENDAR_SESSION_ORDER_INVALID",
                QualitySeverity.CRITICAL,
                "xnys_calendar",
                "market_close must follow market_open",
                {"invalid_count": invalid_order},
            )
        clean = pd.DatetimeIndex(sessions.dropna().drop_duplicates().sort_values())
        metrics["calendar_sessions"] = len(clean)
        if not clean.empty and timezone_invalid == 0:
            self._validate_calendar_reference(
                frame, clean, open_values, close_values, sources, issues, metrics
            )
        return clean

    def _validate_calendar_reference(
        self,
        frame: pd.DataFrame,
        sessions: pd.DatetimeIndex,
        open_values: list[pd.Timestamp],
        close_values: list[pd.Timestamp],
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> None:
        calendar_sources = [
            source for source in sources if source.dataset == "xnys_calendar"
        ]
        artifact_hash = frame_derivation_sha256(frame)
        lineage_ok = len(calendar_sources) == 1 and (
            calendar_sources[0].object_sha256 == artifact_hash
            or str(calendar_sources[0].metadata.get("normalized_artifact_sha256", ""))
            == artifact_hash
        )
        metrics["calendar_lineage_exact"] = lineage_ok
        if not lineage_ok:
            self._issue(
                issues,
                "CALENDAR_LINEAGE_INVALID",
                QualitySeverity.CRITICAL,
                "xnys_calendar",
                "frozen calendar must be bound to one exact captured schedule",
                {"artifact_derivation_sha256": artifact_hash},
            )

        try:
            reference = xcals.get_calendar("XNYS").schedule.loc[
                str(sessions.min().date()) : str(sessions.max().date())
            ]
        except Exception as exc:
            self._issue(
                issues,
                "XNYS_REFERENCE_UNAVAILABLE",
                QualitySeverity.CRITICAL,
                "xnys_calendar",
                "installed exchange_calendars XNYS schedule cannot be loaded",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            return

        reference_sessions = pd.DatetimeIndex(reference.index).tz_localize(None).normalize()
        supplied_sessions = set(sessions)
        expected_sessions = set(reference_sessions)
        missing = sorted(expected_sessions - supplied_sessions)
        extra = sorted(supplied_sessions - expected_sessions)
        supplied_times = {
            pd.Timestamp(session).normalize(): (
                opened.tz_convert("UTC"),
                closed.tz_convert("UTC"),
            )
            for session, opened, closed in zip(
                pd.to_datetime(frame["session_date"], errors="coerce").dt.normalize(),
                open_values,
                close_values,
                strict=True,
            )
            if not pd.isna(session)
            and opened.tzinfo is not None
            and closed.tzinfo is not None
        }
        time_mismatches: list[str] = []
        for session in reference_sessions:
            actual = supplied_times.get(pd.Timestamp(session))
            if actual is None:
                continue
            expected_open = pd.Timestamp(reference.loc[session, "open"]).tz_convert("UTC")
            expected_close = pd.Timestamp(reference.loc[session, "close"]).tz_convert("UTC")
            if actual != (expected_open, expected_close):
                time_mismatches.append(pd.Timestamp(session).date().isoformat())
        metrics["calendar_reference_exact"] = not missing and not extra and not time_mismatches
        if missing or extra or time_mismatches:
            self._issue(
                issues,
                "XNYS_CALENDAR_MISMATCH",
                QualitySeverity.CRITICAL,
                "xnys_calendar",
                "frozen sessions and market times must exactly match exchange_calendars XNYS",
                {
                    "missing_sessions": [item.date().isoformat() for item in missing[:20]],
                    "extra_sessions": [item.date().isoformat() for item in extra[:20]],
                    "time_mismatches": time_mismatches[:20],
                },
            )

    def _validate_membership(
        self,
        frame: pd.DataFrame | None,
        calendar: pd.DatetimeIndex,
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> pd.DataFrame:
        if not self._usable(frame, "membership_monthly"):
            return pd.DataFrame(columns=["universe_id", "decision_date", "security_id"])
        assert frame is not None
        value = frame.copy()
        value["decision_date"] = pd.to_datetime(value["decision_date"], errors="coerce").dt.normalize()
        null_key = value[["universe_id", "decision_date", "security_id"]].isna().any(axis=1)
        blank_security = value["security_id"].astype(str).str.strip().eq("")
        if null_key.any() or blank_security.any():
            self._issue(
                issues,
                "NULL_MEMBERSHIP_KEY",
                QualitySeverity.CRITICAL,
                "membership_monthly",
                "membership primary key contains null or blank values",
                {"affected_rows": int((null_key | blank_security).sum())},
            )
        duplicates = value.duplicated(["universe_id", "decision_date", "security_id"])
        if duplicates.any():
            self._issue(
                issues,
                "DUPLICATE_MEMBERSHIP",
                QualitySeverity.CRITICAL,
                "membership_monthly",
                "membership grain is not unique",
                {"duplicate_rows": int(duplicates.sum())},
            )
        wrong_universe = value["universe_id"].astype(str) != self.policy.universe_id
        if wrong_universe.any():
            self._issue(
                issues,
                "WRONG_UNIVERSE",
                QualitySeverity.CRITICAL,
                "membership_monthly",
                "release contains a different universe",
                {"unexpected": sorted(value.loc[wrong_universe, "universe_id"].astype(str).unique())},
            )
        decisions = pd.DatetimeIndex(value["decision_date"].dropna().drop_duplicates().sort_values())
        metrics["decision_months"] = len(decisions)
        metrics["membership_rows"] = len(value)
        metrics["unique_securities"] = int(value["security_id"].nunique())
        metrics["certified_start"] = (
            decisions.min().date().isoformat() if not decisions.empty else None
        )
        metrics["certified_end"] = (
            decisions.max().date().isoformat() if not decisions.empty else None
        )
        if len(decisions) < self.policy.min_decision_months:
            self._issue(
                issues,
                "INSUFFICIENT_DECISION_MONTHS",
                QualitySeverity.CRITICAL,
                "membership_monthly",
                "certified history is shorter than the policy minimum",
                {"actual": len(decisions), "required": self.policy.min_decision_months},
            )
        if decisions.empty or calendar.empty:
            return value

        first, last = decisions.min(), decisions.max()
        calendar_window = calendar[(calendar >= first.to_period("M").start_time) & (calendar <= last)]
        expected: list[pd.Timestamp] = []
        for _, sessions in pd.Series(calendar_window, index=calendar_window).groupby(
            calendar_window.to_period("M")
        ):
            expected.append(pd.Timestamp(sessions.max()).normalize())
        expected_index = pd.DatetimeIndex(expected)
        missing_monthends = expected_index.difference(decisions)
        off_monthends = decisions.difference(expected_index)
        if len(missing_monthends) or len(off_monthends):
            self._issue(
                issues,
                "NONCONTIGUOUS_MONTH_ENDS",
                QualitySeverity.CRITICAL,
                "membership_monthly",
                "decision dates are not every actual XNYS month end",
                {
                    "missing": [item.date().isoformat() for item in missing_monthends],
                    "not_month_end": [item.date().isoformat() for item in off_monthends],
                },
            )
        warmup = int((calendar < first).sum())
        metrics["warmup_sessions"] = warmup
        if warmup < self.policy.min_warmup_sessions:
            self._issue(
                issues,
                "INSUFFICIENT_WARMUP",
                QualitySeverity.CRITICAL,
                "xnys_calendar",
                "calendar does not contain enough sessions before the first decision",
                {"actual": warmup, "required": self.policy.min_warmup_sessions},
            )
        return value

    def _validate_evidence_tables(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
    ) -> None:
        self._validate_evidence_lineage(artifacts, sources, issues)
        holdings = artifacts.get("fund_holdings_observed")
        if self._usable(holdings, "fund_holdings_observed") and holdings is not None:
            for column in ("as_of_date", "published_at", "observed_at"):
                parsed = pd.to_datetime(holdings[column], errors="coerce", utc=True)
                if column != "published_at" and parsed.isna().any():
                    self._issue(
                        issues,
                        "INVALID_EVIDENCE_TIMESTAMP",
                        QualitySeverity.HIGH,
                        "fund_holdings_observed",
                        f"{column} contains invalid timestamps",
                        {"invalid_count": int(parsed.isna().sum())},
                    )
            as_of = pd.to_datetime(holdings["as_of_date"], errors="coerce", utc=True)
            observed = pd.to_datetime(holdings["observed_at"], errors="coerce", utc=True)
            future_observation = as_of.notna() & observed.notna() & (observed < as_of)
            if future_observation.any():
                self._issue(
                    issues,
                    "EVIDENCE_TIME_TRAVEL",
                    QualitySeverity.CRITICAL,
                    "fund_holdings_observed",
                    "evidence was observed before its stated as-of date",
                    {"affected_rows": int(future_observation.sum())},
                )
            published = pd.to_datetime(holdings["published_at"], errors="coerce", utc=True)
            signal_input = holdings["evidence_role"].astype(str).eq(SourceRole.SIGNAL_INPUT.value)
            unavailable = signal_input & (published.isna() | (published > observed))
            if unavailable.any():
                self._issue(
                    issues,
                    "UNPROVEN_SIGNAL_AVAILABILITY",
                    QualitySeverity.CRITICAL,
                    "fund_holdings_observed",
                    "signal-input holdings lack a publication time available when observed",
                    {"affected_rows": int(unavailable.sum())},
                )

        events = artifacts.get("membership_events")
        if self._usable(events, "membership_events") and events is not None and not events.empty:
            if events["event_id"].isna().any() or events["event_id"].duplicated().any():
                self._issue(
                    issues,
                    "DUPLICATE_EVENT_ID",
                    QualitySeverity.CRITICAL,
                    "membership_events",
                    "membership event IDs must be non-null and unique",
                )
            announced = pd.to_datetime(events["announced_at"], errors="coerce", utc=True)
            effective = pd.to_datetime(events["effective_at"], errors="coerce", utc=True)
            invalid = announced.isna() | effective.isna() | (announced > effective)
            if invalid.any():
                self._issue(
                    issues,
                    "EVENT_TIME_INVALID",
                    QualitySeverity.CRITICAL,
                    "membership_events",
                    "membership event must be announced no later than its effective time",
                    {"affected_rows": int(invalid.sum())},
                )

    def _validate_evidence_lineage(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
    ) -> None:
        """Enforce row-level foreign keys into captured manifest evidence.

        A SHA-shaped value is not provenance.  Every normalized evidence row
        must point to the exact captured object and agree with its source,
        dataset and role.  This deliberately prevents an arbitrary collection
        of official source batches from blessing unrelated normalized tables.
        """

        by_hash: dict[str, list[SourceDependency]] = {}
        for source in sources:
            by_hash.setdefault(str(source.object_sha256), []).append(source)

        specifications: tuple[
            tuple[str, str, frozenset[str], SourceRole | None], ...
        ] = (
            (
                "fund_holdings_observed",
                "content_sha256",
                frozenset({"fund_holdings_observed"}),
                None,
            ),
            (
                "membership_events",
                "evidence_sha256",
                frozenset({"membership_events"}),
                SourceRole.SIGNAL_INPUT,
            ),
            (
                "corporate_actions",
                "evidence_sha256",
                frozenset({"corporate_actions"}),
                SourceRole.SIGNAL_INPUT,
            ),
            (
                "session_exceptions",
                "evidence_sha256",
                frozenset({"session_exceptions"}),
                SourceRole.SIGNAL_INPUT,
            ),
            (
                "anchor_reconciliations",
                "evidence_sha256",
                frozenset({"fund_holdings_observed"}),
                SourceRole.VALIDATION_ANCHOR,
            ),
        )
        for dataset, hash_column, allowed_datasets, required_role in specifications:
            frame = artifacts.get(dataset)
            if not self._usable(frame, dataset) or frame is None or frame.empty:
                continue
            invalid_rows: list[int] = []
            ambiguous_rows: list[int] = []
            for ordinal, (_, row) in enumerate(frame.iterrows()):
                digest = str(row.get(hash_column, "")).strip().lower()
                candidates = [
                    item
                    for item in by_hash.get(digest, [])
                    if item.dataset in allowed_datasets
                ]
                source_id = str(row.get("source_id", "")).strip()
                if source_id:
                    candidates = [item for item in candidates if item.source_id == source_id]
                role = required_role
                if dataset == "fund_holdings_observed":
                    try:
                        role = SourceRole(str(row.get("evidence_role", "")))
                    except ValueError:
                        candidates = []
                        role = None
                if role is not None:
                    candidates = [item for item in candidates if item.role == role]
                if dataset == "fund_holdings_observed":
                    candidates = [
                        item
                        for item in candidates
                        if self._holding_metadata_matches_source(row, item)
                    ]
                if not candidates:
                    invalid_rows.append(ordinal)
                elif len(candidates) != 1:
                    ambiguous_rows.append(ordinal)
            if invalid_rows:
                self._issue(
                    issues,
                    "EVIDENCE_FOREIGN_KEY_BROKEN",
                    QualitySeverity.CRITICAL,
                    dataset,
                    "normalized evidence rows do not reference matching captured manifest objects",
                    {"affected_rows": len(invalid_rows), "sample_row_ordinals": invalid_rows[:20]},
                )
            if ambiguous_rows:
                self._issue(
                    issues,
                    "EVIDENCE_FOREIGN_KEY_AMBIGUOUS",
                    QualitySeverity.CRITICAL,
                    dataset,
                    "normalized evidence rows map ambiguously to captured manifest objects",
                    {
                        "affected_rows": len(ambiguous_rows),
                        "sample_row_ordinals": ambiguous_rows[:20],
                    },
                )

        benchmarks = artifacts.get("benchmarks")
        if self._usable(benchmarks, "benchmarks") and benchmarks is not None:
            invalid_rows: list[int] = []
            ambiguous_rows: list[int] = []
            for ordinal, (_, row) in enumerate(benchmarks.iterrows()):
                digest = str(
                    row.get("total_return_evidence_sha256", "")
                ).strip().lower()
                source_id = str(row.get("total_return_source_id", "")).strip()
                candidates = [
                    item
                    for item in by_hash.get(digest, [])
                    if item.source_id == source_id
                    and item.dataset in {"benchmark_total_return", "benchmarks"}
                    and item.role
                    in {SourceRole.SIGNAL_INPUT, SourceRole.VALIDATION_ANCHOR}
                ]
                if not candidates:
                    invalid_rows.append(ordinal)
                elif len(candidates) != 1:
                    ambiguous_rows.append(ordinal)
            if invalid_rows:
                self._issue(
                    issues,
                    "EVIDENCE_FOREIGN_KEY_BROKEN",
                    QualitySeverity.CRITICAL,
                    "benchmarks",
                    "benchmark total-return rows lack matching captured evidence",
                    {"affected_rows": len(invalid_rows), "sample_row_ordinals": invalid_rows[:20]},
                )
            if ambiguous_rows:
                self._issue(
                    issues,
                    "EVIDENCE_FOREIGN_KEY_AMBIGUOUS",
                    QualitySeverity.CRITICAL,
                    "benchmarks",
                    "benchmark total-return evidence references are ambiguous",
                    {
                        "affected_rows": len(ambiguous_rows),
                        "sample_row_ordinals": ambiguous_rows[:20],
                    },
                )

        lifecycle = artifacts.get("lifecycle_reconciliations")
        if (
            self._usable(lifecycle, "lifecycle_reconciliations")
            and lifecycle is not None
            and not lifecycle.empty
        ):
            invalid_rows: list[int] = []
            ambiguous_rows: list[int] = []
            for ordinal, (_, row) in enumerate(lifecycle.iterrows()):
                digest = str(row.get("evidence_sha256", "")).strip().lower()
                action_id = str(row.get("action_id", "")).strip()
                allowed = (
                    frozenset({"corporate_actions"})
                    if action_id and action_id.lower() not in {"nan", "none"}
                    else frozenset({"lifecycle_status"})
                )
                candidates = [
                    item
                    for item in by_hash.get(digest, [])
                    if item.dataset in allowed
                    and item.source_id == str(row.get("source_id", "")).strip()
                    and item.role
                    in {SourceRole.SIGNAL_INPUT, SourceRole.VALIDATION_ANCHOR}
                    and self._lifecycle_evidence_supports_row(artifacts, row, item)
                ]
                if not candidates:
                    invalid_rows.append(ordinal)
                elif len(candidates) != 1:
                    ambiguous_rows.append(ordinal)
            if invalid_rows:
                self._issue(
                    issues,
                    "EVIDENCE_FOREIGN_KEY_BROKEN",
                    QualitySeverity.CRITICAL,
                    "lifecycle_reconciliations",
                    "lifecycle rows lack a matching captured evidence object",
                    {"affected_rows": len(invalid_rows), "sample_row_ordinals": invalid_rows[:20]},
                )
            if ambiguous_rows:
                self._issue(
                    issues,
                    "EVIDENCE_FOREIGN_KEY_AMBIGUOUS",
                    QualitySeverity.CRITICAL,
                    "lifecycle_reconciliations",
                    "lifecycle evidence references are ambiguous",
                    {
                        "affected_rows": len(ambiguous_rows),
                        "sample_row_ordinals": ambiguous_rows[:20],
                    },
                )

    @staticmethod
    def _holding_metadata_matches_source(
        row: pd.Series,
        source: SourceDependency,
    ) -> bool:
        if str(row.get("source_version", "")) != source.source_version:
            return False
        if str(row.get("url", "")) != source.url:
            return False

        def same_timestamp(left: Any, right: str | None) -> bool:
            parsed_left = pd.to_datetime(left, errors="coerce", utc=True)
            parsed_right = pd.to_datetime(right, errors="coerce", utc=True)
            if pd.isna(parsed_left) or pd.isna(parsed_right):
                return bool(pd.isna(parsed_left) and pd.isna(parsed_right))
            return bool(parsed_left == parsed_right)

        def same_date(left: Any, right: str | None) -> bool:
            parsed_left = pd.to_datetime(left, errors="coerce")
            parsed_right = pd.to_datetime(right, errors="coerce")
            if pd.isna(parsed_left) or pd.isna(parsed_right):
                return bool(pd.isna(parsed_left) and pd.isna(parsed_right))
            return bool(parsed_left.date() == parsed_right.date())

        return bool(
            same_date(row.get("as_of_date"), source.as_of_date)
            and same_timestamp(row.get("observed_at"), source.observed_at)
            and same_timestamp(row.get("published_at"), source.published_at)
        )

    @staticmethod
    def _lifecycle_evidence_supports_row(
        artifacts: Mapping[str, pd.DataFrame],
        row: pd.Series,
        source: SourceDependency,
    ) -> bool:
        digest = str(row.get("evidence_sha256", "")).strip().lower()
        security_id = str(row.get("security_id", "")).strip()
        action_id = str(row.get("action_id", "")).strip()
        has_action = bool(action_id and action_id.lower() not in {"nan", "none"})
        if source.dataset == "fund_holdings_observed" and not has_action:
            frame = artifacts.get("fund_holdings_observed")
            return bool(
                frame is not None
                and not frame.empty
                and (
                    frame["content_sha256"].astype(str).str.lower().eq(digest)
                    & frame["security_id"].astype(str).str.strip().eq(security_id)
                ).any()
            )
        if source.dataset == "membership_events" and not has_action:
            frame = artifacts.get("membership_events")
            return bool(
                frame is not None
                and not frame.empty
                and (
                    frame["evidence_sha256"].astype(str).str.lower().eq(digest)
                    & frame["security_id"].astype(str).str.strip().eq(security_id)
                ).any()
            )
        if source.dataset == "corporate_actions" and has_action:
            frame = artifacts.get("corporate_actions")
            return bool(
                frame is not None
                and not frame.empty
                and (
                    frame["evidence_sha256"].astype(str).str.lower().eq(digest)
                    & frame["security_id"].astype(str).str.strip().eq(security_id)
                    & frame["action_id"].astype(str).str.strip().eq(action_id)
                ).any()
            )
        if source.dataset == "lifecycle_status" and not has_action:
            source_through = pd.to_datetime(
                source.metadata.get("current_through"), errors="coerce"
            )
            row_through = pd.to_datetime(row.get("current_through"), errors="coerce")
            covered = {
                str(item).strip().lower()
                for item in source.metadata.get("covered_security_ids", [])
            }
            return bool(
                not pd.isna(source_through)
                and not pd.isna(row_through)
                and source_through.normalize() >= row_through.normalize()
                and str(source.metadata.get("coverage_kind", ""))
                == "TERMINATION_SURVEILLANCE"
                and security_id.lower() in covered
            )
        return False

    @staticmethod
    def _lifecycle_status_contract_valid(
        source: SourceDependency,
        sources: tuple[SourceDependency, ...],
    ) -> bool:
        metadata = dict(source.metadata)
        try:
            if int(metadata.get("coverage_contract_version", 0)) != 3:
                return False
            if metadata.get("coverage_kind") != "TERMINATION_SURVEILLANCE":
                return False
            if metadata.get("coverage_derived_from_payload") is not True:
                return False
            if metadata.get("source_records_bound_to_cas") is not True:
                return False
            if metadata.get("observation_identifiers_verified_in_payload") is not True:
                return False
            covered = tuple(
                sorted(str(item).strip().lower() for item in metadata["covered_security_ids"])
            )
            if (
                not covered
                or len(covered) != len(set(covered))
                or any(not item.startswith("us_") for item in covered)
                or metadata.get("covered_security_ids_sha256")
                != sha256_json(list(covered))
                or int(metadata.get("covered_security_count", -1)) != len(covered)
            ):
                return False
            records = metadata["source_records"]
            if not isinstance(records, list) or not records:
                return False
            normalized_records: list[dict[str, Any]] = []
            derived: set[str] = set()
            bound_hashes: list[str] = []
            seen: set[tuple[str, str, str]] = set()
            for raw in records:
                if not isinstance(raw, dict):
                    return False
                source_id = str(raw.get("source_id", "")).strip()
                dataset = str(raw.get("dataset", "")).strip()
                digest = str(raw.get("evidence_sha256", "")).strip().lower()
                url = str(raw.get("url", "")).strip()
                published_at = str(raw.get("published_at", "")).strip()
                observations = raw.get("observations", [])
                if not isinstance(observations, list) or not observations:
                    return False
                normalized_observations: list[dict[str, str]] = []
                ids_set: set[str] = set()
                observation_keys: set[tuple[str, str, str]] = set()
                for observation in observations:
                    if not isinstance(observation, dict):
                        return False
                    security_id = str(observation.get("security_id", "")).strip().lower()
                    identifier_type = str(
                        observation.get("identifier_type", "")
                    ).strip().upper()
                    identifier_value = re.sub(
                        r"[^A-Z0-9]", "",
                        str(observation.get("identifier_value", "")).upper(),
                    )
                    observed_status = str(
                        observation.get("observed_status", "")
                    ).strip().upper()
                    locator = str(observation.get("evidence_locator", "")).strip()
                    observed_through = str(
                        observation.get("observed_through", "")
                    ).strip()
                    status_effective_at = str(
                        observation.get("status_effective_at", "")
                    ).strip()
                    evidence_excerpt = str(
                        observation.get("evidence_excerpt", "")
                    ).strip()
                    try:
                        pd.Timestamp(observed_through)
                        if status_effective_at:
                            pd.Timestamp(status_effective_at)
                    except (TypeError, ValueError):
                        return False
                    key = (security_id, identifier_type, identifier_value)
                    if (
                        not security_id.startswith("us_")
                        or identifier_type not in {"ISIN", "CUSIP"}
                        or not identifier_value
                        or observed_status not in {
                            "LISTED", "ACTIVE_HOLDING", "TERMINATED", "DELISTED",
                            "MERGED", "BANKRUPT", "HALTED",
                        }
                        or not locator
                        or not observed_through
                        or not evidence_excerpt
                        or len(evidence_excerpt) > 500
                        or observed_status
                        in {"TERMINATED", "DELISTED", "MERGED", "BANKRUPT"}
                        and not status_effective_at
                        or key in observation_keys
                    ):
                        return False
                    observation_keys.add(key)
                    ids_set.add(security_id)
                    normalized_observations.append(
                        {
                            "security_id": security_id,
                            "identifier_type": identifier_type,
                            "identifier_value": identifier_value,
                            "observed_status": observed_status,
                            "evidence_locator": locator,
                            "observed_through": observed_through,
                            "status_effective_at": status_effective_at,
                            "evidence_excerpt": evidence_excerpt,
                        }
                    )
                normalized_observations.sort(
                    key=lambda item: (
                        item["security_id"], item["identifier_type"],
                        item["identifier_value"],
                    )
                )
                ids = tuple(sorted(ids_set))
                identity = (source_id, dataset, digest)
                if (
                    not source_id
                    or not ids
                    or len(ids) != len(set(ids))
                    or any(not item.startswith("us_") for item in ids)
                    or identity in seen
                ):
                    return False
                seen.add(identity)
                candidates = [
                    item
                    for item in sources
                    if item is not source
                    and item.source_id == source_id
                    and item.dataset == dataset
                    and item.object_sha256 == digest
                    and item.url == url
                    and item.published_at == published_at
                    and item.license_class != LicenseClass.UNLICENSED_REFERENCE
                    and item.role in {SourceRole.SIGNAL_INPUT, SourceRole.VALIDATION_ANCHOR}
                ]
                if len(candidates) != 1:
                    return False
                derived.update(ids)
                bound_hashes.append(digest)
                normalized_records.append(
                    {
                        "source_id": source_id,
                        "dataset": dataset,
                        "evidence_sha256": digest,
                        "published_at": published_at,
                        "url": url,
                        "observations": normalized_observations,
                    }
                )
            normalized_records.sort(
                key=lambda item: (
                    item["source_id"], item["dataset"], item["evidence_sha256"]
                )
            )
            return bool(
                tuple(sorted(derived)) == covered
                and metadata.get("source_records_sha256")
                == sha256_json(normalized_records)
                and int(metadata.get("source_record_count", -1)) == len(records)
                and sorted(metadata.get("source_dependency_object_sha256s", []))
                == sorted(bound_hashes)
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _validate_membership_replay(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        memberships: pd.DataFrame,
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> dict[pd.Timestamp, frozenset[str]]:
        """Rebuild every published month from immutable decision-time evidence."""

        holdings = artifacts.get("fund_holdings_observed")
        events = artifacts.get("membership_events")
        calendar = artifacts.get("xnys_calendar")
        if (
            memberships.empty
            or not self._usable(holdings, "fund_holdings_observed")
            or holdings is None
            or not self._usable(events, "membership_events")
            or events is None
            or not self._usable(calendar, "xnys_calendar")
            or calendar is None
        ):
            return {}

        if any(
            item.source_id == "sec_nport_ivv"
            and item.dataset == "fund_holdings_observed"
            and dict(item.metadata).get("artifact_kind")
            == "raw_complete_edgar_submission"
            for item in sources
        ):
            decisions = pd.DatetimeIndex(
                pd.to_datetime(memberships["decision_date"], errors="coerce")
                .dropna()
                .dt.normalize()
                .drop_duplicates()
                .sort_values()
            )
            result = replay_causal_membership(
                holdings,
                events,
                decisions,
                sources,
                calendar,
                artifacts.get("corporate_actions"),
            )
            grouped: dict[str, list[dict[str, Any]]] = {}
            for gap in result.gaps:
                grouped.setdefault(
                    str(gap.get("code", "MEMBERSHIP_REPLAY_FAILED")), []
                ).append(gap)
            for code, values in grouped.items():
                self._issue(
                    issues,
                    code,
                    QualitySeverity.CRITICAL,
                    "membership_monthly",
                    "causal SEC/S&P membership replay failed",
                    {"count": len(values), "sample": values[:10]},
                )
            reported = {
                day: frozenset(
                    memberships.loc[
                        pd.to_datetime(
                            memberships["decision_date"], errors="coerce"
                        )
                        .dt.normalize()
                        .eq(day),
                        "security_id",
                    ].astype(str)
                )
                for day in decisions
            }
            mismatches = [
                day.date().isoformat()
                for day in decisions
                if result.replayed.get(day, frozenset())
                != reported.get(day, frozenset())
            ]
            if mismatches:
                self._issue(
                    issues,
                    "MEMBERSHIP_REPLAY_MISMATCH",
                    QualitySeverity.CRITICAL,
                    "membership_monthly",
                    "reported membership differs from causal SEC/S&P replay",
                    {
                        "affected_months": len(mismatches),
                        "sample": mismatches[:20],
                    },
                )
            metrics["membership_replay_months"] = len(result.replayed)
            metrics["membership_replay_exact"] = bool(
                len(result.replayed) == len(decisions)
                and not result.gaps
                and not mismatches
            )
            metrics["reconciled_sec_anchor_intervals"] = (
                result.reconciled_anchor_count
            )
            return dict(result.replayed)

        calendar_value = calendar.copy()
        calendar_value["session_date"] = pd.to_datetime(
            calendar_value["session_date"], errors="coerce"
        ).dt.normalize()
        calendar_value["market_close_utc"] = pd.to_datetime(
            calendar_value["market_close"], errors="coerce", utc=True
        )
        close_by_session = {
            pd.Timestamp(row.session_date).normalize(): row.market_close_utc
            for row in calendar_value.itertuples(index=False)
            if not pd.isna(row.session_date) and not pd.isna(row.market_close_utc)
        }

        signal_holdings = holdings.loc[
            holdings["evidence_role"].astype(str).eq(SourceRole.SIGNAL_INPUT.value)
        ].copy()
        signal_holdings["as_of_normalized"] = pd.to_datetime(
            signal_holdings["as_of_date"], errors="coerce"
        ).dt.normalize()
        signal_holdings["published_utc"] = pd.to_datetime(
            signal_holdings["published_at"], errors="coerce", utc=True
        )
        signal_holdings["observed_utc"] = pd.to_datetime(
            signal_holdings["observed_at"], errors="coerce", utc=True
        )
        snapshots: list[dict[str, Any]] = []
        duplicate_snapshot_members = 0
        inconsistent_snapshot_metadata = 0
        for digest, group in signal_holdings.groupby("content_sha256", dropna=False):
            metadata_columns = [
                "as_of_normalized",
                "published_utc",
                "observed_utc",
                "url",
                "source_version",
                "evidence_role",
            ]
            if any(group[column].nunique(dropna=False) != 1 for column in metadata_columns):
                inconsistent_snapshot_metadata += 1
                continue
            security_values = group["security_id"].astype(str).str.strip()
            duplicate_snapshot_members += int(security_values.duplicated().sum())
            first = group.iloc[0]
            snapshots.append(
                {
                    "digest": str(digest),
                    "as_of": first["as_of_normalized"],
                    "published": first["published_utc"],
                    "observed": first["observed_utc"],
                    "members": frozenset(security_values),
                }
            )
        if duplicate_snapshot_members or inconsistent_snapshot_metadata:
            self._issue(
                issues,
                "MEMBERSHIP_BASELINE_INVALID",
                QualitySeverity.CRITICAL,
                "fund_holdings_observed",
                "signal-input holding snapshots must have one metadata record and unique securities",
                {
                    "duplicate_security_rows": duplicate_snapshot_members,
                    "inconsistent_snapshots": inconsistent_snapshot_metadata,
                },
            )
        conflicting_snapshot_keys = 0
        snapshot_versions: dict[tuple[Any, Any], frozenset[str]] = {}
        for snapshot in snapshots:
            key = (snapshot["as_of"], snapshot["observed"])
            prior = snapshot_versions.get(key)
            if prior is not None and prior != snapshot["members"]:
                conflicting_snapshot_keys += 1
            snapshot_versions[key] = snapshot["members"]
        if conflicting_snapshot_keys:
            self._issue(
                issues,
                "MEMBERSHIP_BASELINE_CONFLICT",
                QualitySeverity.CRITICAL,
                "fund_holdings_observed",
                "multiple signal baselines at the same as-of/observation time disagree",
                {"conflicting_snapshot_keys": conflicting_snapshot_keys},
            )

        source_by_hash: dict[str, list[SourceDependency]] = {}
        for source in sources:
            source_by_hash.setdefault(source.object_sha256, []).append(source)
        event_values = events.copy()
        event_values["announced_utc"] = pd.to_datetime(
            event_values["announced_at"], errors="coerce", utc=True
        )
        event_values["effective_utc"] = pd.to_datetime(
            event_values["effective_at"], errors="coerce", utc=True
        )
        event_values["source_published_utc"] = pd.Series(
            pd.NaT, index=event_values.index, dtype="datetime64[ns, UTC]"
        )
        event_values["source_observed_utc"] = pd.Series(
            pd.NaT, index=event_values.index, dtype="datetime64[ns, UTC]"
        )
        unproven_event_rows = 0
        for index, row in event_values.iterrows():
            candidates = [
                item
                for item in source_by_hash.get(
                    str(row.get("evidence_sha256", "")).strip().lower(), []
                )
                if item.dataset == "membership_events"
                and item.role == SourceRole.SIGNAL_INPUT
                and item.source_id == str(row.get("source_id", "")).strip()
            ]
            if len(candidates) != 1:
                unproven_event_rows += 1
                continue
            source = candidates[0]
            published = pd.to_datetime(source.published_at, errors="coerce", utc=True)
            observed = pd.to_datetime(source.observed_at, errors="coerce", utc=True)
            if pd.isna(published) or pd.isna(observed):
                unproven_event_rows += 1
                continue
            event_values.at[index, "source_published_utc"] = published
            event_values.at[index, "source_observed_utc"] = observed
        if unproven_event_rows:
            self._issue(
                issues,
                "UNPROVEN_EVENT_AVAILABILITY",
                QualitySeverity.CRITICAL,
                "membership_events",
                "membership events require one captured signal source with a publication timestamp",
                {"affected_rows": unproven_event_rows},
            )

        event_types = event_values["event_type"].astype(str).str.strip().str.upper()
        unsupported_event = ~event_types.isin({"ADD", "REMOVE"})
        if unsupported_event.any():
            self._issue(
                issues,
                "UNSUPPORTED_MEMBERSHIP_EVENT",
                QualitySeverity.CRITICAL,
                "membership_events",
                "membership replay supports only explicit ADD and REMOVE events",
                {"affected_rows": int(unsupported_event.sum())},
            )

        decisions = pd.DatetimeIndex(
            pd.to_datetime(memberships["decision_date"], errors="coerce")
            .dropna()
            .dt.normalize()
            .drop_duplicates()
            .sort_values()
        )
        replayed: dict[pd.Timestamp, frozenset[str]] = {}
        missing_baselines: list[str] = []
        inconsistent_events: list[str] = []
        mismatches: list[dict[str, Any]] = []
        for decision in decisions:
            cutoff = close_by_session.get(decision)
            if cutoff is None or pd.isna(cutoff):
                missing_baselines.append(decision.date().isoformat())
                continue
            eligible = [
                snapshot
                for snapshot in snapshots
                if not pd.isna(snapshot["as_of"])
                and not pd.isna(snapshot["published"])
                and not pd.isna(snapshot["observed"])
                and snapshot["as_of"] <= decision
                and snapshot["published"] <= cutoff
                and snapshot["observed"] <= cutoff
            ]
            if not eligible:
                missing_baselines.append(decision.date().isoformat())
                continue
            baseline = max(
                eligible,
                key=lambda item: (
                    item["as_of"],
                    item["observed"],
                    item["digest"],
                ),
            )
            state = set(baseline["members"])
            applicable = event_values.loc[
                event_values["effective_utc"].notna()
                & event_values["announced_utc"].notna()
                & event_values["source_published_utc"].notna()
                & event_values["effective_utc"].le(cutoff)
                & event_values["announced_utc"].le(cutoff)
                & event_values["source_published_utc"].le(cutoff)
                & pd.to_datetime(event_values["effective_utc"], utc=True)
                .dt.tz_convert(None)
                .dt.normalize()
                .gt(baseline["as_of"])
            ].copy()
            applicable["normalized_type"] = (
                applicable["event_type"].astype(str).str.strip().str.upper()
            )
            applicable = applicable.sort_values(
                ["effective_utc", "announced_utc", "event_id"], kind="mergesort"
            )
            for row in applicable.itertuples(index=False):
                security_id = str(row.security_id).strip()
                event_id = str(row.event_id)
                if row.normalized_type == "ADD":
                    if security_id in state:
                        inconsistent_events.append(event_id)
                    state.add(security_id)
                elif row.normalized_type == "REMOVE":
                    if security_id not in state:
                        inconsistent_events.append(event_id)
                    state.discard(security_id)
            reconstructed = frozenset(state)
            replayed[decision] = reconstructed
            reported = frozenset(
                memberships.loc[
                    pd.to_datetime(memberships["decision_date"], errors="coerce")
                    .dt.normalize()
                    .eq(decision),
                    "security_id",
                ]
                .astype(str)
                .str.strip()
            )
            if reconstructed != reported:
                mismatches.append(
                    {
                        "decision_date": decision.date().isoformat(),
                        "missing_from_reported": sorted(reconstructed - reported)[:20],
                        "fabricated_or_extra": sorted(reported - reconstructed)[:20],
                    }
                )
        if missing_baselines:
            self._issue(
                issues,
                "MISSING_DECISION_TIME_BASELINE",
                QualitySeverity.CRITICAL,
                "fund_holdings_observed",
                "one or more decisions have no holdings baseline actually available by market close",
                {"count": len(missing_baselines), "sample": missing_baselines[:20]},
            )
        if inconsistent_events:
            self._issue(
                issues,
                "MEMBERSHIP_EVENT_STATE_CONFLICT",
                QualitySeverity.CRITICAL,
                "membership_events",
                "ADD/REMOVE events conflict with the reconstructed prior state",
                {
                    "count": len(set(inconsistent_events)),
                    "sample_event_ids": sorted(set(inconsistent_events))[:20],
                },
            )
        if mismatches:
            self._issue(
                issues,
                "MEMBERSHIP_REPLAY_MISMATCH",
                QualitySeverity.CRITICAL,
                "membership_monthly",
                "reported monthly membership is not the exact deterministic evidence replay",
                {"affected_months": len(mismatches), "sample": mismatches[:10]},
            )
        metrics["membership_replay_months"] = len(replayed)
        metrics["membership_replay_exact"] = bool(
            len(replayed) == len(decisions) and not mismatches and not inconsistent_events
        )
        return replayed

    def _validate_identity(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        memberships: pd.DataFrame,
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> None:
        master = artifacts.get("security_master")
        aliases = artifacts.get("listing_aliases")
        identifiers = artifacts.get("identifiers")
        if not self._usable(master, "security_master") or master is None:
            return
        if master["security_id"].isna().any() or master["security_id"].duplicated().any():
            self._issue(
                issues,
                "SECURITY_MASTER_KEY_INVALID",
                QualitySeverity.CRITICAL,
                "security_master",
                "security_id must be non-null and unique",
            )
        invalid_security_id = ~master["security_id"].astype(str).str.lower().map(
            lambda value: value.startswith("us_") and not value.endswith(".us")
        )
        if invalid_security_id.any():
            self._issue(
                issues,
                "SECURITY_ID_NOT_NAMESPACED",
                QualitySeverity.CRITICAL,
                "security_master",
                "stable US security IDs must use the us_ namespace and cannot be tickers",
                {"affected_rows": int(invalid_security_id.sum())},
            )
        invalid_primary = ~master["primary_identifier_type"].astype(str).isin({"ISIN", "CUSIP"})
        if invalid_primary.any():
            self._issue(
                issues,
                "UNSTABLE_PRIMARY_IDENTIFIER",
                QualitySeverity.CRITICAL,
                "security_master",
                "formal securities require an ISIN or CUSIP primary identifier",
                {"affected_rows": int(invalid_primary.sum())},
            )
        primary_duplicate = master.duplicated(["primary_identifier_type", "primary_identifier"])
        if primary_duplicate.any():
            self._issue(
                issues,
                "PRIMARY_IDENTIFIER_COLLISION",
                QualitySeverity.CRITICAL,
                "security_master",
                "one primary identifier maps to multiple securities",
                {"affected_rows": int(primary_duplicate.sum())},
            )
        members = set(memberships.get("security_id", pd.Series(dtype=str)).dropna().astype(str))
        master_ids = set(master["security_id"].dropna().astype(str))
        missing_master = sorted(members - master_ids)
        if missing_master:
            self._issue(
                issues,
                "ORPHAN_MEMBERSHIP_SECURITY",
                QualitySeverity.CRITICAL,
                "security_master",
                "membership contains securities absent from the master",
                {"count": len(missing_master), "sample": missing_master[:20]},
            )
        if self._usable(identifiers, "identifiers") and identifiers is not None:
            identifier_ids = set(identifiers["security_id"].dropna().astype(str))
            missing_identifiers = sorted(members - identifier_ids)
            if missing_identifiers:
                self._issue(
                    issues,
                    "MISSING_IDENTIFIER_HISTORY",
                    QualitySeverity.CRITICAL,
                    "identifiers",
                    "member security has no identifier history",
                    {"count": len(missing_identifiers), "sample": missing_identifiers[:20]},
                )
            collisions = identifiers.dropna(subset=["identifier_type", "identifier_value"]).duplicated(
                ["identifier_type", "identifier_value", "valid_from"], keep=False
            )
            if collisions.any():
                grouped = identifiers.loc[collisions].groupby(
                    ["identifier_type", "identifier_value", "valid_from"]
                )["security_id"].nunique()
                if (grouped > 1).any():
                    self._issue(
                        issues,
                        "IDENTIFIER_HISTORY_COLLISION",
                        QualitySeverity.CRITICAL,
                        "identifiers",
                        "identifier history maps the same identifier to different securities",
                    )
        if not self._usable(aliases, "listing_aliases") or aliases is None:
            return
        alias_value = aliases.copy()
        alias_value["valid_from"] = pd.to_datetime(alias_value["valid_from"], errors="coerce").dt.normalize()
        alias_value["valid_to"] = pd.to_datetime(alias_value["valid_to"], errors="coerce").dt.normalize()
        invalid_range = alias_value["valid_from"].isna() | (
            alias_value["valid_to"].notna()
            & (alias_value["valid_to"] < alias_value["valid_from"])
        )
        if invalid_range.any():
            self._issue(
                issues,
                "ALIAS_VALIDITY_INVALID",
                QualitySeverity.CRITICAL,
                "listing_aliases",
                "alias validity interval is invalid",
                {"affected_rows": int(invalid_range.sum())},
            )
        missing_alias = 0
        ambiguous_alias = 0
        if {"decision_date", "security_id"}.issubset(memberships.columns):
            for row in memberships[["decision_date", "security_id"]].itertuples(index=False):
                active = alias_value[
                    alias_value["security_id"].astype(str).eq(str(row.security_id))
                    & (alias_value["valid_from"] <= row.decision_date)
                    & (
                        alias_value["valid_to"].isna()
                        | (alias_value["valid_to"] >= row.decision_date)
                    )
                ]
                if active.empty:
                    missing_alias += 1
                elif len(active) != 1:
                    ambiguous_alias += 1
        metrics["missing_alias_membership_rows"] = missing_alias
        metrics["ambiguous_alias_membership_rows"] = ambiguous_alias
        if missing_alias:
            self._issue(
                issues,
                "MISSING_ACTIVE_ALIAS",
                QualitySeverity.CRITICAL,
                "listing_aliases",
                "membership cannot be mapped to a ticker/vendor code on its decision date",
                {"affected_rows": missing_alias},
            )
        if ambiguous_alias:
            self._issue(
                issues,
                "AMBIGUOUS_ACTIVE_ALIAS",
                QualitySeverity.CRITICAL,
                "listing_aliases",
                "membership maps to multiple active aliases on its decision date",
                {"affected_rows": ambiguous_alias},
            )

    def _validate_bars(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        memberships: pd.DataFrame,
        calendar: pd.DatetimeIndex,
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> None:
        for dataset, key in (
            ("bars_raw", ["security_id", "date"]),
            ("bars_vendor_front", ["security_id", "date"]),
            ("bars_pit_signal", ["decision_date", "security_id", "date"]),
            ("benchmarks", ["symbol", "date"]),
        ):
            frame = artifacts.get(dataset)
            if not self._usable(frame, dataset) or frame is None:
                continue
            if frame.duplicated(key).any():
                self._issue(
                    issues,
                    "DUPLICATE_BAR_GRAIN",
                    QualitySeverity.CRITICAL,
                    dataset,
                    "bar primary key is not unique",
                    {"duplicate_rows": int(frame.duplicated(key).sum())},
                )
            price_columns = ["Open", "High", "Low", "Close"]
            numeric = frame[price_columns + ["Volume"]].apply(pd.to_numeric, errors="coerce")
            invalid = numeric[price_columns].isna().any(axis=1) | (numeric[price_columns] <= 0).any(axis=1)
            invalid |= numeric["Volume"].isna() | (numeric["Volume"] < 0)
            invalid |= numeric["High"] < numeric[["Open", "Low", "Close"]].max(axis=1)
            invalid |= numeric["Low"] > numeric[["Open", "High", "Close"]].min(axis=1)
            if invalid.any():
                self._issue(
                    issues,
                    "INVALID_OHLCV",
                    QualitySeverity.CRITICAL,
                    dataset,
                    "OHLCV contains null, non-positive, negative-volume, or inconsistent rows",
                    {"affected_rows": int(invalid.sum())},
                )
        self._validate_market_artifact_lineage(artifacts, sources, issues, metrics)
        signal = artifacts.get("bars_pit_signal")
        if self._usable(signal, "bars_pit_signal") and signal is not None:
            decisions = pd.to_datetime(signal["decision_date"], errors="coerce").dt.normalize()
            dates = pd.to_datetime(signal["date"], errors="coerce").dt.normalize()
            future = decisions.isna() | dates.isna() | (dates > decisions)
            if future.any():
                self._issue(
                    issues,
                    "SIGNAL_BAR_TIME_TRAVEL",
                    QualitySeverity.CRITICAL,
                    "bars_pit_signal",
                    "signal adjustment contains a bar after its decision date",
                    {"affected_rows": int(future.sum())},
                )
        benchmark = artifacts.get("benchmarks")
        if self._usable(benchmark, "benchmarks") and benchmark is not None:
            symbols = set(benchmark["symbol"].dropna().astype(str).str.upper())
            missing = sorted({"SPY", "BIL"} - symbols)
            if missing:
                self._issue(
                    issues,
                    "MISSING_BENCHMARK",
                    QualitySeverity.CRITICAL,
                    "benchmarks",
                    "both SPY and BIL are required",
                    {"missing": missing},
                )
            adjustments = set(
                benchmark["adjustment"].dropna().astype(str).str.lower().str.strip()
            )
            if adjustments != {"none"}:
                self._issue(
                    issues,
                    "BENCHMARK_NOT_RAW",
                    QualitySeverity.CRITICAL,
                    "benchmarks",
                    "SPY/BIL benchmark artifact must be unadjusted raw OHLCV",
                    {"observed_adjustments": sorted(adjustments)},
                )
            total_return = pd.to_numeric(
                benchmark["TotalReturnClose"], errors="coerce"
            )
            invalid_total_return = (
                total_return.isna()
                | ~total_return.map(lambda value: math.isfinite(float(value)))
                | total_return.le(0)
            )
            if invalid_total_return.any():
                self._issue(
                    issues,
                    "BENCHMARK_TOTAL_RETURN_INVALID",
                    QualitySeverity.CRITICAL,
                    "benchmarks",
                    "SPY/BIL require a positive evidence-backed total-return close on every row",
                    {"affected_rows": int(invalid_total_return.sum())},
                )
            metrics["benchmark_total_return_rows"] = int(
                (~invalid_total_return).sum()
            )

        coverage = artifacts.get("bar_coverage")
        if not self._usable(coverage, "bar_coverage") or coverage is None:
            return
        coverage_value = coverage.copy()
        coverage_value["decision_date"] = pd.to_datetime(
            coverage_value["decision_date"], errors="coerce"
        ).dt.normalize()
        duplicates = coverage_value.duplicated(["decision_date", "security_id"])
        if duplicates.any():
            self._issue(
                issues,
                "DUPLICATE_COVERAGE_GRAIN",
                QualitySeverity.CRITICAL,
                "bar_coverage",
                "coverage grain is not unique",
                {"duplicate_rows": int(duplicates.sum())},
            )
        recomputed = self._recompute_bar_coverage(artifacts, memberships, calendar)
        membership_keys = set(
            zip(
                pd.to_datetime(memberships.get("decision_date"), errors="coerce").dt.normalize(),
                memberships.get("security_id", pd.Series(dtype=str)).astype(str),
                strict=True,
            )
        ) if not memberships.empty else set()
        coverage_keys = set(
            zip(
                coverage_value["decision_date"],
                coverage_value["security_id"].astype(str),
                strict=True,
            )
        )
        missing_coverage = membership_keys - coverage_keys
        extra_coverage = coverage_keys - membership_keys
        supplied_by_key = coverage_value.set_index(["decision_date", "security_id"])
        mismatch_count = 0
        failed_count = 0
        for row in recomputed.itertuples(index=False):
            key = (pd.Timestamp(row.decision_date).normalize(), str(row.security_id))
            if key not in supplied_by_key.index:
                continue
            supplied = supplied_by_key.loc[key]
            if isinstance(supplied, pd.DataFrame):
                mismatch_count += 1
                continue
            claimed = (
                pd.to_numeric(pd.Series([supplied.get("expected_sessions")]), errors="coerce").iloc[0],
                pd.to_numeric(pd.Series([supplied.get("raw_sessions")]), errors="coerce").iloc[0],
                pd.to_numeric(pd.Series([supplied.get("signal_sessions")]), errors="coerce").iloc[0],
                pd.to_numeric(
                    pd.Series([supplied.get("explained_missing_sessions")]), errors="coerce"
                ).iloc[0],
                bool(self._bool_series(pd.Series([supplied.get("passed")])).iloc[0]),
            )
            actual = (
                row.expected_sessions,
                row.raw_sessions,
                row.signal_sessions,
                row.explained_missing_sessions,
                row.passed,
            )
            mismatch_count += int(claimed != actual)
            failed_count += int(not row.passed)
        metrics["bar_coverage_rows"] = len(coverage_value)
        metrics["bar_coverage_recomputed_rows"] = len(recomputed)
        metrics["bar_coverage_attestation_mismatches"] = mismatch_count
        metrics["bar_coverage_failed_rows"] = (
            failed_count + mismatch_count + len(missing_coverage) + len(extra_coverage)
        )
        if failed_count or mismatch_count or missing_coverage or extra_coverage:
            self._issue(
                issues,
                "INCOMPLETE_BAR_COVERAGE",
                QualitySeverity.CRITICAL,
                "bar_coverage",
                "member history is incomplete or unexplained",
                {
                    "failed_rows": failed_count,
                    "attestation_mismatches": mismatch_count,
                    "missing_membership_rows": len(missing_coverage),
                    "extra_coverage_rows": len(extra_coverage),
                },
            )
        self._validate_decision_and_next_open(
            artifacts, memberships, calendar, issues
        )

    def _validate_market_artifact_lineage(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> None:
        for dataset in ("bars_raw", "bars_vendor_front"):
            frame = artifacts.get(dataset)
            if not self._usable(frame, dataset) or frame is None:
                continue
            candidates = [source for source in sources if source.dataset == dataset]
            artifact_hash = frame_derivation_sha256(frame)
            exact = len(candidates) == 1 and (
                candidates[0].object_sha256 == artifact_hash
                or str(candidates[0].metadata.get("normalized_artifact_sha256", ""))
                == artifact_hash
            )
            metrics[f"{dataset}_lineage_exact"] = exact
            if not exact:
                self._issue(
                    issues,
                    "MARKET_ARTIFACT_LINEAGE_INVALID",
                    QualitySeverity.CRITICAL,
                    dataset,
                    "normalized market bars must bind to one exact captured source or derivation hash",
                    {"artifact_derivation_sha256": artifact_hash},
                )

    def _recompute_bar_coverage(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        memberships: pd.DataFrame,
        calendar: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        columns = [
            "decision_date",
            "security_id",
            "expected_sessions",
            "raw_sessions",
            "signal_sessions",
            "explained_missing_sessions",
            "passed",
        ]
        raw = artifacts.get("bars_raw")
        signal = artifacts.get("bars_pit_signal")
        aliases = artifacts.get("listing_aliases")
        if (
            memberships.empty
            or calendar.empty
            or raw is None
            or signal is None
            or aliases is None
        ):
            return pd.DataFrame(columns=columns)
        raw_keys = set(
            zip(
                raw["security_id"].astype(str),
                pd.to_datetime(raw["date"], errors="coerce").dt.normalize(),
                strict=True,
            )
        )
        signal_keys = set(
            zip(
                pd.to_datetime(signal["decision_date"], errors="coerce").dt.normalize(),
                signal["security_id"].astype(str),
                pd.to_datetime(signal["date"], errors="coerce").dt.normalize(),
                strict=True,
            )
        )
        alias_values = aliases.copy()
        alias_values["valid_from"] = pd.to_datetime(
            alias_values["valid_from"], errors="coerce"
        ).dt.normalize()
        exceptions = artifacts.get("session_exceptions")
        exception_keys: set[tuple[str, pd.Timestamp]] = set()
        if exceptions is not None and not exceptions.empty:
            valid = self._bool_series(exceptions["verified"]) & exceptions[
                "exception_type"
            ].astype(str).str.upper().isin({"HALTED", "NO_TRADE"})
            exception_keys = set(
                zip(
                    exceptions.loc[valid, "security_id"].astype(str),
                    pd.to_datetime(
                        exceptions.loc[valid, "session_date"], errors="coerce"
                    ).dt.normalize(),
                    strict=True,
                )
            )
        rows: list[dict[str, Any]] = []
        calendar_start = calendar.min()
        for member in memberships[["decision_date", "security_id"]].itertuples(index=False):
            decision = pd.Timestamp(member.decision_date).normalize()
            security_id = str(member.security_id)
            listed = alias_values.loc[
                alias_values["security_id"].astype(str).eq(security_id), "valid_from"
            ].dropna()
            start = max(calendar_start, listed.min()) if not listed.empty else calendar_start
            expected_days = calendar[(calendar >= start) & (calendar <= decision)]
            raw_count = sum((security_id, day) in raw_keys for day in expected_days)
            signal_count = sum(
                (decision, security_id, day) in signal_keys for day in expected_days
            )
            explained = sum(
                (security_id, day) in exception_keys
                and (security_id, day) not in raw_keys
                and (decision, security_id, day) not in signal_keys
                for day in expected_days
            )
            expected_count = len(expected_days)
            rows.append(
                {
                    "decision_date": decision,
                    "security_id": security_id,
                    "expected_sessions": expected_count,
                    "raw_sessions": raw_count,
                    "signal_sessions": signal_count,
                    "explained_missing_sessions": explained,
                    "passed": bool(
                        expected_count > 0
                        and raw_count + explained == expected_count
                        and signal_count + explained == expected_count
                    ),
                }
            )
        return pd.DataFrame(rows, columns=columns)

    def _validate_decision_and_next_open(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        memberships: pd.DataFrame,
        calendar: pd.DatetimeIndex,
        issues: list[QualityIssue],
    ) -> None:
        raw = artifacts.get("bars_raw")
        signal = artifacts.get("bars_pit_signal")
        if raw is None or signal is None or memberships.empty or calendar.empty:
            return
        raw_keys = set(
            zip(
                raw["security_id"].astype(str),
                pd.to_datetime(raw["date"], errors="coerce").dt.normalize(),
                strict=True,
            )
        )
        signal_keys = set(
            zip(
                pd.to_datetime(signal["decision_date"], errors="coerce").dt.normalize(),
                signal["security_id"].astype(str),
                pd.to_datetime(signal["date"], errors="coerce").dt.normalize(),
                strict=True,
            )
        )
        exceptions = artifacts.get("session_exceptions")
        exception_keys: set[tuple[str, pd.Timestamp]] = set()
        if exceptions is not None and not exceptions.empty:
            verified = self._bool_series(exceptions["verified"])
            exception_keys = set(
                zip(
                    exceptions.loc[verified, "security_id"].astype(str),
                    pd.to_datetime(
                        exceptions.loc[verified, "session_date"], errors="coerce"
                    ).dt.normalize(),
                    strict=True,
                )
            )
        missing_signal = 0
        missing_raw = 0
        missing_next_open = 0
        calendar_values = set(calendar)
        for row in memberships[["decision_date", "security_id"]].itertuples(index=False):
            decision = pd.Timestamp(row.decision_date).normalize()
            security_id = str(row.security_id)
            if (decision, security_id, decision) not in signal_keys and (
                security_id,
                decision,
            ) not in exception_keys:
                missing_signal += 1
            if (security_id, decision) not in raw_keys and (
                security_id,
                decision,
            ) not in exception_keys:
                missing_raw += 1
            later = calendar[calendar > decision]
            if len(later):
                next_session = later[0]
                if (security_id, next_session) not in raw_keys and (
                    security_id,
                    next_session,
                ) not in exception_keys:
                    missing_next_open += 1
            elif decision in calendar_values:
                self._issue(
                    issues,
                    "CALENDAR_MISSING_NEXT_SESSION",
                    QualitySeverity.CRITICAL,
                    "xnys_calendar",
                    "calendar ends at a decision date and cannot identify the execution session",
                )
                break
        if missing_signal or missing_raw or missing_next_open:
            self._issue(
                issues,
                "MISSING_DECISION_EXECUTION_BAR",
                QualitySeverity.CRITICAL,
                "bars_raw",
                "decision close or next-session execution data is missing without a verified exception",
                {
                    "missing_signal_close": missing_signal,
                    "missing_raw_close": missing_raw,
                    "missing_next_open": missing_next_open,
                },
            )

    def _validate_reconciliations(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        memberships: pd.DataFrame,
        replayed_memberships: Mapping[pd.Timestamp, frozenset[str]],
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> bool:
        source_by_hash: dict[str, list[SourceDependency]] = {}
        for source in sources:
            source_by_hash.setdefault(source.object_sha256, []).append(source)

        anchors = artifacts.get("anchor_reconciliations")
        if self._usable(anchors, "anchor_reconciliations") and anchors is not None:
            metrics["anchor_reconciliations"] = len(anchors)
            holdings = artifacts.get("fund_holdings_observed")
            actual_failures = 0
            attestation_mismatches = 0
            valid_anchor_count = 0
            anchor_samples: list[dict[str, Any]] = []
            duplicate_anchors = anchors.duplicated(["anchor_date", "evidence_sha256"])
            for _, anchor in anchors.iterrows():
                anchor_date = pd.to_datetime(
                    anchor.get("anchor_date"), errors="coerce"
                )
                if pd.isna(anchor_date):
                    actual_failures += 1
                    continue
                anchor_date = pd.Timestamp(anchor_date).normalize()
                digest = str(anchor.get("evidence_sha256", "")).strip().lower()
                candidates = [
                    item
                    for item in source_by_hash.get(digest, [])
                    if item.dataset == "fund_holdings_observed"
                    and item.role == SourceRole.VALIDATION_ANCHOR
                    and item.source_id == str(anchor.get("source_id", "")).strip()
                ]
                anchor_rows = pd.DataFrame()
                if holdings is not None and self._usable(
                    holdings, "fund_holdings_observed"
                ):
                    holding_dates = pd.to_datetime(
                        holdings["as_of_date"], errors="coerce"
                    ).dt.normalize()
                    anchor_rows = holdings.loc[
                        holdings["content_sha256"].astype(str).str.lower().eq(digest)
                        & holdings["evidence_role"]
                        .astype(str)
                        .eq(SourceRole.VALIDATION_ANCHOR.value)
                        & holding_dates.eq(anchor_date)
                    ]
                eligible_decisions = [
                    decision
                    for decision in replayed_memberships
                    if decision <= anchor_date
                    and decision.to_period("M") == anchor_date.to_period("M")
                ]
                if len(candidates) != 1 or anchor_rows.empty or not eligible_decisions:
                    actual_failures += 1
                    continue
                if anchor_rows["security_id"].astype(str).str.strip().duplicated().any():
                    actual_failures += 1
                    continue
                decision = max(eligible_decisions)
                anchor_set = frozenset(
                    anchor_rows["security_id"].astype(str).str.strip()
                )
                replay_set = replayed_memberships[decision]
                additions = anchor_set - replay_set
                removals = replay_set - anchor_set
                actual_reconciled = not additions and not removals
                actual_failures += int(not actual_reconciled)
                valid_anchor_count += 1
                claimed_additions = pd.to_numeric(
                    pd.Series([anchor.get("unexplained_additions")]), errors="coerce"
                ).iloc[0]
                claimed_removals = pd.to_numeric(
                    pd.Series([anchor.get("unexplained_removals")]), errors="coerce"
                ).iloc[0]
                claimed_reconciled = str(anchor.get("status", "")) == "RECONCILED"
                if (
                    pd.isna(claimed_additions)
                    or pd.isna(claimed_removals)
                    or float(claimed_additions) != float(len(additions))
                    or float(claimed_removals) != float(len(removals))
                    or claimed_reconciled != actual_reconciled
                ):
                    attestation_mismatches += 1
                if not actual_reconciled:
                    anchor_samples.append(
                        {
                            "anchor_date": anchor_date.date().isoformat(),
                            "actual_additions": sorted(additions)[:20],
                            "actual_removals": sorted(removals)[:20],
                        }
                    )
            metrics["anchor_reconciliations_recomputed"] = valid_anchor_count
            metrics["anchor_actual_failures"] = actual_failures
            if (
                len(anchors) < self.policy.min_anchor_reconciliations
                or valid_anchor_count < self.policy.min_anchor_reconciliations
                or actual_failures
                or bool(duplicate_anchors.any())
            ):
                self._issue(
                    issues,
                    "ANCHOR_RECONCILIATION_FAILED",
                    QualitySeverity.CRITICAL,
                    "anchor_reconciliations",
                    "validation anchors do not exactly match the independently replayed membership",
                    {
                        "actual": len(anchors),
                        "required": self.policy.min_anchor_reconciliations,
                        "recomputed": valid_anchor_count,
                        "failed": actual_failures,
                        "duplicates": int(duplicate_anchors.sum()),
                        "sample": anchor_samples[:10],
                    },
                )
            if attestation_mismatches:
                self._issue(
                    issues,
                    "ANCHOR_ATTESTATION_MISMATCH",
                    QualitySeverity.CRITICAL,
                    "anchor_reconciliations",
                    "supplied reconciliation status/counts disagree with recomputed set differences",
                    {"affected_rows": attestation_mismatches},
                )

        lifecycle_covered = False
        lifecycle = artifacts.get("lifecycle_reconciliations")
        if self._usable(lifecycle, "lifecycle_reconciliations") and lifecycle is not None:
            metrics["lifecycle_reconciliations"] = len(lifecycle)
            members = set(
                memberships.get("security_id", pd.Series(dtype=str))
                .dropna()
                .astype(str)
                .str.strip()
            )
            valid_rows = lifecycle.loc[
                lifecycle["scope"].astype(str).eq("SECURITY")
                & lifecycle["status"].astype(str).eq("RECONCILED")
                & lifecycle["security_id"].notna()
                & lifecycle["security_id"].astype(str).str.strip().ne("")
            ].copy()
            duplicate_security = valid_rows["security_id"].astype(str).duplicated()
            evidenced_ids: set[str] = set()
            latest_decision = pd.to_datetime(
                memberships.get("decision_date"), errors="coerce"
            ).max()
            coverage_contract_failures = 0
            for _, row in valid_rows.iterrows():
                digest = str(row.get("evidence_sha256", "")).strip().lower()
                source_id = str(row.get("source_id", "")).strip()
                action_id = str(row.get("action_id", "")).strip()
                has_action = bool(action_id and action_id.lower() not in {"nan", "none"})
                allowed = (
                    {"corporate_actions"}
                    if has_action
                    else {"lifecycle_status"}
                )
                candidates = [
                    item
                    for item in source_by_hash.get(digest, [])
                    if item.source_id == source_id
                    and item.dataset in allowed
                    and item.role
                    in {SourceRole.SIGNAL_INPUT, SourceRole.VALIDATION_ANCHOR}
                    and self._lifecycle_evidence_supports_row(artifacts, row, item)
                ]
                coverage_kind = str(row.get("coverage_kind", "")).strip().upper()
                current_through = pd.to_datetime(
                    row.get("current_through"), errors="coerce"
                )
                expected_kind = "TERMINAL_ACTION" if has_action else "STATUS_SURVEILLANCE"
                contract_valid = coverage_kind == expected_kind and not pd.isna(current_through)
                if not has_action:
                    contract_valid = bool(
                        contract_valid
                        and not pd.isna(latest_decision)
                        and current_through.normalize() >= latest_decision.normalize()
                        and len(candidates) == 1
                        and self._lifecycle_status_contract_valid(
                            candidates[0], sources
                        )
                        and str(
                            candidates[0].metadata.get("covered_security_ids_sha256", "")
                        )
                        == sha256_json(sorted(members))
                    )
                elif len(candidates) == 1:
                    actions_for_row = artifacts.get("corporate_actions")
                    matching_action = (
                        pd.DataFrame()
                        if actions_for_row is None
                        else actions_for_row.loc[
                            actions_for_row["action_id"].astype(str).eq(action_id)
                            & actions_for_row["security_id"].astype(str).eq(
                                str(row["security_id"]).strip()
                            )
                        ]
                    )
                    effective = (
                        ny_session_dates(matching_action["effective_at"]).min()
                        if not matching_action.empty
                        else pd.NaT
                    )
                    contract_valid = bool(
                        contract_valid
                        and not pd.isna(effective)
                        and current_through.normalize()
                        >= effective.normalize()
                    )
                else:
                    contract_valid = False
                coverage_contract_failures += int(not contract_valid)
                if len(candidates) == 1 and contract_valid:
                    evidenced_ids.add(str(row["security_id"]).strip())

            actions = artifacts.get("corporate_actions")
            terminal_missing: list[str] = []
            if actions is not None and self._usable(actions, "corporate_actions"):
                terminal_types = {
                    "CASH_MERGER",
                    "STOCK_MERGER",
                    "DELISTING",
                    "BANKRUPTCY",
                    "SPINOFF",
                }
                terminal = actions.loc[
                    actions["action_type"]
                    .astype(str)
                    .str.strip()
                    .str.upper()
                    .isin(terminal_types)
                ]
                reconciled_pairs = {
                    (str(row.security_id).strip(), str(row.action_id).strip())
                    for row in valid_rows.itertuples(index=False)
                    if str(row.action_id).strip().lower() not in {"", "nan", "none"}
                }
                terminal_missing = [
                    str(row.action_id)
                    for row in terminal.itertuples(index=False)
                    if (str(row.security_id).strip(), str(row.action_id).strip())
                    not in reconciled_pairs
                ]
            missing_members = sorted(members - evidenced_ids)
            extra_members = sorted(evidenced_ids - members)
            invalid_scope_or_status = len(lifecycle) - len(valid_rows)
            lifecycle_covered = bool(
                members
                and not missing_members
                and not extra_members
                and not duplicate_security.any()
                and not terminal_missing
                and not invalid_scope_or_status
                and not coverage_contract_failures
            )
            metrics["lifecycle_member_count"] = len(members)
            metrics["lifecycle_evidence_covered"] = len(evidenced_ids & members)
            metrics["lifecycle_coverage_complete"] = lifecycle_covered
            metrics["lifecycle_coverage_contract_failures"] = coverage_contract_failures
            if not lifecycle_covered:
                self._issue(
                    issues,
                    "LIFECYCLE_RECONCILIATION_FAILED",
                    QualitySeverity.CRITICAL,
                    "lifecycle_reconciliations",
                    "every historical member requires one evidence-backed per-security lifecycle reconciliation",
                    {
                        "missing_members": missing_members[:20],
                        "extra_members": extra_members[:20],
                        "duplicate_securities": int(duplicate_security.sum()),
                        "terminal_actions_missing": terminal_missing[:20],
                        "invalid_scope_or_status": invalid_scope_or_status,
                        "coverage_contract_failures": coverage_contract_failures,
                    },
                )
        if memberships.empty:
            self._issue(
                issues,
                "EMPTY_MEMBERSHIP",
                QualitySeverity.CRITICAL,
                "membership_monthly",
                "membership dataset has no rows",
            )
        return lifecycle_covered

    def _validate_actions(
        self,
        artifacts: Mapping[str, pd.DataFrame],
        issues: list[QualityIssue],
        metrics: dict[str, Any],
    ) -> None:
        actions = artifacts.get("corporate_actions")
        if not self._usable(actions, "corporate_actions") or actions is None:
            return
        metrics["corporate_actions"] = len(actions)
        if actions.empty:
            return
        duplicate = actions["action_id"].isna() | actions["action_id"].duplicated()
        announced = pd.to_datetime(actions["announced_at"], errors="coerce", utc=True)
        effective = pd.to_datetime(actions["effective_at"], errors="coerce", utc=True)
        invalid_time = announced.isna() | effective.isna() | (announced > effective)
        unverified = ~self._bool_series(actions["terms_verified"])
        if duplicate.any() or invalid_time.any() or unverified.any():
            self._issue(
                issues,
                "CORPORATE_ACTION_INVALID",
                QualitySeverity.CRITICAL,
                "corporate_actions",
                "corporate actions require unique IDs, valid timing, and verified terms",
                {
                    "duplicate": int(duplicate.sum()),
                    "invalid_time": int(invalid_time.sum()),
                    "unverified": int(unverified.sum()),
                },
            )

        def safe_ny_session_date(value: Any) -> pd.Timestamp:
            try:
                return ny_session_date(value)
            except (TypeError, ValueError):
                return pd.NaT

        calendar = artifacts.get("xnys_calendar")
        if self._usable(calendar, "xnys_calendar") and calendar is not None:
            frozen_sessions = set(
                pd.to_datetime(
                    calendar["session_date"], errors="coerce"
                ).dt.normalize()
            )
            effective_days = actions["effective_at"].map(safe_ny_session_date)
            non_session = effective_days.isna() | ~effective_days.isin(frozen_sessions)
            if non_session.any():
                self._issue(
                    issues,
                    "CORPORATE_ACTION_NON_XNYS_SESSION",
                    QualitySeverity.CRITICAL,
                    "corporate_actions",
                    "corporate-action effective dates must be explicit frozen XNYS sessions; automatic roll-forward is forbidden",
                    {"affected_rows": int(non_session.sum())},
                )
        term_failures = 0
        successor_identity_failures = 0
        security_master = artifacts.get("security_master")
        aliases = artifacts.get("listing_aliases")
        known_security_ids = (
            set(security_master["security_id"].dropna().astype(str))
            if self._usable(security_master, "security_master")
            and security_master is not None
            else set()
        )
        for _, action in actions.iterrows():
            action_type = str(action.get("action_type", "")).strip().upper()
            if action_type in {"TICKER_CHANGE", "RENAME"}:
                continue
            if action_type in {"SPLIT", "STOCK_DIVIDEND"}:
                term_failures += not self._has_positive_term(
                    action, ("split_ratio", "share_ratio", "ratio")
                )
                predecessor = str(action.get("security_id") or "").strip()
                successor = str(
                    action.get("successor_security_id") or ""
                ).strip()
                if successor and successor != predecessor:
                    effective_day = safe_ny_session_date(
                        action.get("effective_at")
                    )
                    active_aliases = (
                        aliases.loc[
                            aliases["security_id"].astype(str).eq(successor)
                            & (
                                pd.to_datetime(
                                    aliases["valid_from"], errors="coerce"
                                ).dt.normalize()
                                <= effective_day
                            )
                            & (
                                pd.to_datetime(
                                    aliases["valid_to"], errors="coerce"
                                ).dt.normalize().isna()
                                | (
                                    pd.to_datetime(
                                        aliases["valid_to"], errors="coerce"
                                    ).dt.normalize()
                                    >= effective_day
                                )
                            )
                        ]
                        if aliases is not None
                        and self._usable(aliases, "listing_aliases")
                        and effective_day is not None
                        else pd.DataFrame()
                    )
                    successor_identity_failures += int(
                        successor not in known_security_ids
                        or len(active_aliases) != 1
                    )
            elif action_type == "CASH_DIVIDEND":
                term_failures += not self._has_nonnegative_term(
                    action, ("cash_amount", "cash_per_share")
                )
                pay_date = pd.to_datetime(action.get("pay_date"), errors="coerce", utc=True)
                effective_date = pd.to_datetime(
                    action.get("effective_at"), errors="coerce", utc=True
                )
                term_failures += bool(
                    pd.isna(pay_date)
                    or pd.isna(effective_date)
                    or pay_date < effective_date
                )
            elif action_type in {"CASH_MERGER", "DELISTING", "BANKRUPTCY"}:
                term_failures += not self._has_nonnegative_term(
                    action,
                    ("cash_amount", "cash_per_share", "settlement_cash_per_share"),
                )
            elif action_type == "STOCK_MERGER":
                term_failures += not self._has_positive_term(
                    action, ("share_ratio", "exchange_ratio", "ratio")
                )
                term_failures += not self._valid_successor(action.get("successor_security_id"))
            elif action_type == "SPINOFF":
                term_failures += not self._has_positive_term(
                    action, ("share_ratio", "ratio")
                )
                term_failures += not self._valid_successor(action.get("successor_security_id"))
                fraction = self._first_number(action, ("cost_basis_fraction",))
                term_failures += fraction is None or not 0 <= fraction <= 1
            else:
                term_failures += 1
        if term_failures:
            self._issue(
                issues,
                "CORPORATE_ACTION_TERMS_INCOMPLETE",
                QualitySeverity.CRITICAL,
                "corporate_actions",
                "action-specific settlement/share terms are incomplete or unsupported",
                {"failed_checks": int(term_failures)},
            )
        if successor_identity_failures:
            self._issue(
                issues,
                "CORPORATE_ACTION_SUCCESSOR_IDENTITY_INVALID",
                QualitySeverity.CRITICAL,
                "corporate_actions",
                "share-ratio successor IDs require a security-master row and one active alias on the effective XNYS session",
                {"failed_actions": int(successor_identity_failures)},
            )
        terminal_types = {"CASH_MERGER", "STOCK_MERGER", "DELISTING", "BANKRUPTCY", "SPINOFF"}
        terminal_ids = set(
            actions.loc[
                actions["action_type"].astype(str).str.upper().isin(terminal_types), "action_id"
            ].dropna().astype(str)
        )
        lifecycle = artifacts.get("lifecycle_reconciliations")
        reconciled_ids: set[str] = set()
        if lifecycle is not None and not lifecycle.empty:
            reconciled_ids = set(
                lifecycle.loc[
                    lifecycle["status"].astype(str).eq("RECONCILED"), "action_id"
                ].dropna().astype(str)
            )
        missing = sorted(terminal_ids - reconciled_ids)
        if missing:
            self._issue(
                issues,
                "UNRECONCILED_TERMINAL_ACTION",
                QualitySeverity.CRITICAL,
                "lifecycle_reconciliations",
                "terminal corporate actions lack verified settlement reconciliation",
                {"count": len(missing), "sample": missing[:20]},
            )

    def _validate_fees(
        self,
        frame: pd.DataFrame | None,
        calendar: pd.DatetimeIndex,
        sources: tuple[SourceDependency, ...],
        issues: list[QualityIssue],
    ) -> None:
        if not self._usable(frame, "execution_fee_schedule") or frame is None:
            return
        starts = pd.to_datetime(frame["effective_from"], errors="coerce").dt.normalize()
        ends = pd.to_datetime(frame["effective_to"], errors="coerce").dt.normalize()
        rate_columns = [
            "commission_rate",
            "min_commission",
            "slippage_rate",
            "sec_sell_fee_rate",
            "finra_taf_per_share",
            "finra_taf_cap",
        ]
        rates = frame[rate_columns].apply(pd.to_numeric, errors="coerce")
        invalid = starts.isna() | (ends.notna() & (ends < starts))
        invalid |= rates.isna().any(axis=1) | (rates < 0).any(axis=1)
        if invalid.any() or starts.duplicated().any():
            self._issue(
                issues,
                "FEE_SCHEDULE_INVALID",
                QualitySeverity.CRITICAL,
                "execution_fee_schedule",
                "effective-dated fees are invalid, negative, or ambiguous",
                {"affected_rows": int(invalid.sum())},
            )
        model_invalid = frame["fee_model_id"].astype(str).str.strip().ne(
            "us_equity_effective_fees_v1"
        )
        evidence_url_invalid = (
            ~frame["sec_evidence_url"].astype(str).str.startswith("https://www.sec.gov/")
            | ~frame["finra_evidence_url"].astype(str).str.startswith(
                "https://www.finra.org/"
            )
        )
        if model_invalid.any() or evidence_url_invalid.any():
            self._issue(
                issues,
                "FEE_EVIDENCE_INVALID",
                QualitySeverity.CRITICAL,
                "execution_fee_schedule",
                "fee rows require the frozen US fee model and official SEC/FINRA evidence URLs",
                {
                    "invalid_models": int(model_invalid.sum()),
                    "invalid_evidence_urls": int(evidence_url_invalid.sum()),
                },
            )

        fee_sources = [
            source for source in sources if source.dataset == "execution_fee_schedule"
        ]
        sec_sources = [source for source in sources if source.dataset == "regulatory_fee_sec"]
        finra_sources = [
            source for source in sources if source.dataset == "regulatory_fee_finra"
        ]
        artifact_hash = frame_derivation_sha256(frame)
        exact_lineage = len(fee_sources) == 1 and (
            fee_sources[0].object_sha256 == artifact_hash
            or str(fee_sources[0].metadata.get("normalized_artifact_sha256", ""))
            == artifact_hash
        )
        derivation_hashes = set(frame["fee_derivation_sha256"].astype(str).str.lower())
        official_inputs_valid = bool(
            sec_sources
            and finra_sources
            and self._fee_rows_bind_to_official_sources(
                frame, sec_sources, authority="SEC"
            )
            and self._fee_rows_bind_to_official_sources(
                frame, finra_sources, authority="FINRA"
            )
            and len(derivation_hashes) == 1
            and all(len(item) == 64 for item in derivation_hashes)
        )
        metadata_valid = bool(
            exact_lineage
            and official_inputs_valid
            and int(
                fee_sources[0].metadata.get("fee_evidence_contract_version", 0)
            )
            == FEE_EVIDENCE_CONTRACT_VERSION
            and str(fee_sources[0].metadata.get("sec_url", "")).startswith(
                "https://www.sec.gov/"
            )
            and str(fee_sources[0].metadata.get("finra_url", "")).startswith(
                "https://www.finra.org/"
            )
        )
        if not metadata_valid:
            self._issue(
                issues,
                "FEE_LINEAGE_INVALID",
                QualitySeverity.CRITICAL,
                "execution_fee_schedule",
                "effective fees must bind to exact raw SEC and FINRA evidence plus one derivation",
                {"artifact_derivation_sha256": artifact_hash},
            )
        if not calendar.empty and not frame.empty:
            active_counts = pd.Series(0, index=calendar, dtype=int)
            for start, end in zip(starts, ends, strict=True):
                if pd.isna(start):
                    continue
                active_counts += ((calendar >= start) & (pd.isna(end) | (calendar <= end))).astype(int)
            invalid_sessions = active_counts.ne(1)
            if invalid_sessions.any():
                self._issue(
                    issues,
                    "FEE_SCHEDULE_GAP",
                    QualitySeverity.CRITICAL,
                    "execution_fee_schedule",
                    "every frozen XNYS session must map to exactly one effective fee row",
                    {
                        "affected_sessions": int(invalid_sessions.sum()),
                        "sample": [
                            pd.Timestamp(item).date().isoformat()
                            for item in active_counts.index[invalid_sessions][:20]
                        ],
                    },
                )

    @staticmethod
    def _fee_rows_bind_to_official_sources(
        frame: pd.DataFrame,
        sources: list[SourceDependency],
        *,
        authority: str,
    ) -> bool:
        expected_dataset = (
            "regulatory_fee_sec" if authority == "SEC" else "regulatory_fee_finra"
        )
        source_by_hash = {item.object_sha256.lower(): item for item in sources}
        for row in frame.to_dict(orient="records"):
            hash_column = (
                "sec_evidence_sha256"
                if authority == "SEC"
                else "finra_evidence_sha256"
            )
            evidence_hash = str(row.get(hash_column, "")).lower()
            source = source_by_hash.get(evidence_hash)
            if source is None:
                return False
            if (
                source.dataset != expected_dataset
                or source.role != SourceRole.VALIDATION_ANCHOR
                or source.license_class != LicenseClass.OFFICIAL_PUBLIC
                or int(source.metadata.get("fee_evidence_contract_version", 0))
                != FEE_EVIDENCE_CONTRACT_VERSION
            ):
                return False
            if authority == "SEC":
                if not source.url.startswith("https://www.sec.gov/"):
                    return False
            elif not source.url.startswith("https://www.finra.org/"):
                return False
            try:
                row_date = pd.Timestamp(row["effective_from"]).date()
            except (KeyError, TypeError, ValueError):
                return False
            matches: list[date] = []
            for entry in fee_rate_entries(dict(source.metadata)):
                try:
                    effective = date.fromisoformat(str(entry["effective_from"]))
                    if effective > row_date:
                        continue
                    if authority == "SEC":
                        matched = math.isclose(
                            float(entry["sec_sell_fee_rate"]),
                            float(row["sec_sell_fee_rate"]),
                            rel_tol=0.0,
                            abs_tol=1e-15,
                        )
                    else:
                        matched = math.isclose(
                            float(entry["finra_taf_per_share"]),
                            float(row["finra_taf_per_share"]),
                            rel_tol=0.0,
                            abs_tol=1e-15,
                        ) and math.isclose(
                            float(entry["finra_taf_cap"]),
                            float(row["finra_taf_cap"]),
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                    if matched:
                        matches.append(effective)
                except (KeyError, TypeError, ValueError):
                    continue
            if not matches:
                return False
            latest_all: list[date] = []
            for candidate in sources:
                for entry in fee_rate_entries(dict(candidate.metadata)):
                    try:
                        effective = date.fromisoformat(str(entry["effective_from"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if effective <= row_date:
                        latest_all.append(effective)
            if not latest_all or max(matches) != max(latest_all):
                return False
        return True

    def _validate_session_exceptions(
        self,
        frame: pd.DataFrame | None,
        issues: list[QualityIssue],
    ) -> None:
        if not self._usable(frame, "session_exceptions") or frame is None or frame.empty:
            return
        dates = pd.to_datetime(frame["session_date"], errors="coerce").dt.normalize()
        duplicate = pd.DataFrame(
            {"security_id": frame["security_id"].astype(str), "session_date": dates}
        ).duplicated()
        verified = self._bool_series(frame["verified"])
        supported = frame["exception_type"].astype(str).str.upper().isin({"HALTED", "NO_TRADE"})
        evidence_valid = frame["evidence_sha256"].astype(str).map(
            lambda value: len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )
        invalid = dates.isna() | duplicate | ~verified | ~supported | ~evidence_valid
        if invalid.any():
            self._issue(
                issues,
                "SESSION_EXCEPTION_INVALID",
                QualitySeverity.CRITICAL,
                "session_exceptions",
                "session exceptions must be unique, verified, supported, and evidence-backed",
                {"affected_rows": int(invalid.sum())},
            )

    @staticmethod
    def _bool_series(values: pd.Series) -> pd.Series:
        def convert(value: Any) -> bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes"}
            if pd.isna(value):
                return False
            return bool(value)

        return values.map(convert).astype(bool)

    @staticmethod
    def _first_number(row: pd.Series, names: tuple[str, ...]) -> float | None:
        for name in names:
            if name not in row or pd.isna(row.get(name)):
                continue
            try:
                value = float(row.get(name))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return value
        return None

    @classmethod
    def _has_positive_term(cls, row: pd.Series, names: tuple[str, ...]) -> bool:
        value = cls._first_number(row, names)
        return value is not None and value > 0

    @classmethod
    def _has_nonnegative_term(cls, row: pd.Series, names: tuple[str, ...]) -> bool:
        value = cls._first_number(row, names)
        return value is not None and value >= 0

    @staticmethod
    def _valid_successor(value: Any) -> bool:
        normalized = str(value).strip().lower()
        return normalized.startswith("us_") and not normalized.endswith(".us")

    @staticmethod
    def _usable(frame: pd.DataFrame | None, dataset: str) -> bool:
        return frame is not None and REQUIRED_ARTIFACT_COLUMNS[dataset].issubset(frame.columns)

    @staticmethod
    def _issue(
        issues: list[QualityIssue],
        code: str,
        severity: QualitySeverity,
        dataset: str,
        message: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        issues.append(
            QualityIssue(
                code=code,
                severity=severity,
                dataset=dataset,
                message=message,
                evidence=dict(evidence or {}),
            )
        )


__all__ = [
    "QualityPolicy",
    "REQUIRED_ARTIFACT_COLUMNS",
    "USPITQualityValidator",
    "frame_derivation_sha256",
]
