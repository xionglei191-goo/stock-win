from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import pandas as pd
import exchange_calendars as xcals

from .hashing import canonical_json_bytes, sha256_file, sha256_json
from .membership_replay import replay_causal_membership
from .models import SourceDependency, SourceRole, UNIVERSE_ID
from .official_normalize import OfficialNormalizationResult
from .quality import REQUIRED_ARTIFACT_COLUMNS
from .store import USPITStore


WORKSPACE_FORMAT_VERSION = "us-pit-reviewed-workspace-v1"
_IDENTITY_REVIEW_FILE = "identity_review.parquet"
_EVENT_REVIEW_FILE = "membership_events.parquet"
_ACTION_REVIEW_FILE = "corporate_actions.parquet"
_EXCEPTION_REVIEW_FILE = "session_exceptions.parquet"


class ReviewWorkspaceError(ValueError):
    """A reviewed input is ambiguous, mutable, or causally unsupported."""


@dataclass(frozen=True)
class ReviewWorkspaceResult:
    workspace_id: str
    path: Path
    manifest: Mapping[str, Any]

    @property
    def status(self) -> str:
        return str(self.manifest["status"])


def stable_security_id(*, isin: Any = None, cusip: Any = None) -> str:
    isin_value = _clean_identifier(isin)
    cusip_value = _clean_identifier(cusip)
    if isin_value:
        return f"us_isin_{isin_value.lower()}"
    if cusip_value:
        return f"us_cusip_{cusip_value.lower()}"
    raise ReviewWorkspaceError("stable security identity requires ISIN or CUSIP")


def _candidate_identity_components(holdings: pd.DataFrame) -> dict[str, str]:
    """Collapse observed ISIN/CUSIP pairs before assigning canonical IDs."""

    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    identifiers_by_candidate: dict[str, tuple[str, ...]] = {}
    for row in holdings.to_dict(orient="records"):
        candidate_id = str(row["holding_candidate_id"])
        values = tuple(
            value
            for value in (
                None
                if _clean_identifier(row.get("isin")) is None
                else f"isin:{_clean_identifier(row.get('isin'))}",
                None
                if _clean_identifier(row.get("cusip")) is None
                else f"cusip:{_clean_identifier(row.get('cusip'))}",
            )
            if value is not None
        )
        identifiers_by_candidate[candidate_id] = values
        for value in values:
            find(value)
        if len(values) == 2:
            union(values[0], values[1])

    members: dict[str, set[str]] = {}
    for value in parent:
        members.setdefault(find(value), set()).add(value)
    canonical_by_root: dict[str, str] = {}
    for root, values in members.items():
        isins = sorted(item.split(":", 1)[1] for item in values if item.startswith("isin:"))
        cusips = sorted(item.split(":", 1)[1] for item in values if item.startswith("cusip:"))
        if len(isins) > 1:
            raise ReviewWorkspaceError(
                "one identifier component contains multiple ISIN values: "
                + ", ".join(isins)
            )
        canonical_by_root[root] = stable_security_id(
            isin=isins[0] if isins else None,
            cusip=cusips[0] if cusips else None,
        )
    result: dict[str, str] = {}
    for candidate_id, values in identifiers_by_candidate.items():
        if values:
            result[candidate_id] = canonical_by_root[find(values[0])]
    return result


def _clean_identifier(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    normalized = "".join(character for character in str(value).upper() if character.isalnum())
    return normalized or None


def _clean_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    normalized = str(value).strip()
    return normalized or None


def _issuer_id(row: pd.Series) -> str:
    for prefix, column in (("lei", "lei"), ("cik", "cik")):
        value = _clean_identifier(row.get(column))
        if value:
            return f"us_issuer_{prefix}_{value.lower()}"
    candidate = _clean_text(row.get("issuer_id"))
    if candidate and candidate.startswith("us_issuer_"):
        return candidate
    raise ReviewWorkspaceError(
        "issuer identity requires reviewed LEI/CIK or an explicit us_issuer_ identifier"
    )


def _empty_frame(dataset: str) -> pd.DataFrame:
    return pd.DataFrame(columns=sorted(REQUIRED_ARTIFACT_COLUMNS[dataset]))


def _date_string(value: Any, *, field: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ReviewWorkspaceError(f"invalid {field}")
    return pd.Timestamp(parsed).date().isoformat()


def _normalize_ticker(value: Any) -> str:
    ticker = _clean_text(value)
    if not ticker:
        raise ReviewWorkspaceError("reviewed identity requires ticker")
    ticker = ticker.upper()
    if ticker.endswith(".US"):
        ticker = ticker[:-3]
    if not ticker or any(character.isspace() for character in ticker):
        raise ReviewWorkspaceError(f"invalid US ticker: {value!r}")
    return ticker


def _canonical_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    def normalize(value: Any) -> Any:
        if value is None or pd.isna(value):
            return None
        if isinstance(value, (pd.Timestamp, datetime, date)):
            return value.isoformat()
        item = getattr(value, "item", None)
        if callable(item):
            return item()
        return value

    value = frame.copy()
    value = value.where(pd.notna(value), None)
    return [
        {str(key): normalize(item) for key, item in row.items()}
        for row in value.to_dict(orient="records")
    ]


class USPITReviewWorkspaceAssembler:
    """Create a deterministic, fail-closed workspace from reviewed candidates.

    This stage resolves only stable identity and mechanically replays evidence.
    It never manufactures membership announcements, corporate actions, listing
    validity, trading exceptions, bars, or benchmark returns.
    """

    def __init__(self, store: USPITStore | Path | str) -> None:
        self.store = store if isinstance(store, USPITStore) else USPITStore(store)

    def assemble(
        self,
        normalization: OfficialNormalizationResult | Path | str,
        review_dir: Path | str,
        output_dir: Path | str,
        *,
        decision_start: date,
        decision_end: date,
        source_batch_ids: Iterable[str],
        reviewer: str = "local-user",
        approved_at: datetime | None = None,
    ) -> ReviewWorkspaceResult:
        if decision_start > decision_end:
            raise ReviewWorkspaceError("decision_start must not be after decision_end")
        candidate = self._load_normalization(normalization)
        review_root = Path(review_dir).resolve()
        if not review_root.is_dir():
            raise ReviewWorkspaceError(f"review directory not found: {review_root}")
        output = Path(output_dir).resolve()
        if (
            output == review_root
            or output in review_root.parents
            or review_root in output.parents
        ):
            raise ReviewWorkspaceError("output directory cannot contain or replace review inputs")

        batch_ids = tuple(sorted(set(str(item).strip() for item in source_batch_ids)))
        if not batch_ids or any(not value for value in batch_ids):
            raise ReviewWorkspaceError("at least one source batch is required")
        sources = self._load_sources(batch_ids)
        source_keys = {
            (item.source_id, item.dataset, item.object_sha256) for item in sources
        }

        holdings_candidate = candidate.load_frame("fund_holdings_observed_candidate")
        identity_candidate = candidate.load_frame("security_identity_candidates")
        normalization_issues = candidate.load_frame("normalization_issues")
        review = self._read_identity_review(review_root / _IDENTITY_REVIEW_FILE)
        resolved, unresolved = self._resolve_identities(
            holdings_candidate,
            identity_candidate,
            normalization_issues,
            review,
        )
        resolved_issue_ids = self._resolved_issue_ids(
            review, holdings_candidate, normalization_issues
        )

        artifacts: dict[str, pd.DataFrame] = {}
        artifacts["fund_holdings_observed"] = self._holdings(resolved, source_keys)
        artifacts["security_master"] = self._security_master(resolved)
        artifacts["identifiers"] = self._identifiers(resolved)
        artifacts["listing_aliases"] = self._listing_aliases(resolved)
        artifacts["membership_events"] = self._reviewed_evidence_table(
            review_root / _EVENT_REVIEW_FILE,
            "membership_events",
            source_keys,
        )
        artifacts["corporate_actions"] = self._reviewed_evidence_table(
            review_root / _ACTION_REVIEW_FILE,
            "corporate_actions",
            source_keys,
        )
        artifacts["session_exceptions"] = self._reviewed_evidence_table(
            review_root / _EXCEPTION_REVIEW_FILE,
            "session_exceptions",
            source_keys,
        )

        calendar_path = review_root / "xnys_calendar.parquet"
        if calendar_path.is_file():
            frozen_calendar = pd.read_parquet(calendar_path)
        else:
            frozen_calendar = self._frozen_xnys_calendar(
                decision_start,
                decision_end,
            )
        artifacts["xnys_calendar"] = frozen_calendar
        decisions = self._decision_month_ends(
            frozen_calendar, decision_start, decision_end
        )
        memberships, replay_gaps = self._replay_membership(
            artifacts["fund_holdings_observed"],
            artifacts["membership_events"],
            artifacts["corporate_actions"],
            decisions,
            sources,
            frozen_calendar,
        )
        artifacts["membership_monthly"] = memberships

        for dataset in REQUIRED_ARTIFACT_COLUMNS:
            if dataset in artifacts:
                continue
            path = review_root / f"{dataset}.parquet"
            artifacts[dataset] = pd.read_parquet(path) if path.is_file() else _empty_frame(dataset)

        artifacts["anchor_reconciliations"] = self._anchor_reconciliations(
            artifacts["fund_holdings_observed"], memberships
        )
        lifecycle_path = review_root / "lifecycle_reconciliations.parquet"
        if lifecycle_path.is_file():
            lifecycle = pd.read_parquet(lifecycle_path)
            missing_lifecycle = (
                REQUIRED_ARTIFACT_COLUMNS["lifecycle_reconciliations"]
                - set(lifecycle.columns)
            )
            if missing_lifecycle:
                raise ReviewWorkspaceError(
                    "lifecycle_reconciliations.parquet is missing columns: "
                    + ", ".join(sorted(missing_lifecycle))
                )
            self._validate_lifecycle_evidence(
                lifecycle,
                artifacts["fund_holdings_observed"],
                artifacts["membership_events"],
                artifacts["corporate_actions"],
                source_keys,
            )
            artifacts["lifecycle_reconciliations"] = lifecycle
        else:
            artifacts["lifecycle_reconciliations"] = _empty_frame(
                "lifecycle_reconciliations"
            )

        gaps = self._gap_report(
            artifacts,
            decisions,
            unresolved,
            replay_gaps,
            normalization_issues,
            resolved_issue_ids,
        )
        approval_time = approved_at or datetime.now().astimezone()
        if approval_time.tzinfo is None:
            raise ReviewWorkspaceError("review approved_at must be timezone-aware")
        reviewer_name = str(reviewer).strip()
        if not reviewer_name:
            raise ReviewWorkspaceError("reviewer is required")
        review_receipts = self._freeze_review_inputs(
            review_root, reviewer=reviewer_name, approved_at=approval_time
        )
        manifest_identity = {
            "format_version": WORKSPACE_FORMAT_VERSION,
            "normalization_id": candidate.normalization_id,
            "normalization_manifest_sha256": sha256_file(candidate.path / "manifest.json"),
            "source_batch_ids": list(batch_ids),
            "decision_start": decision_start.isoformat(),
            "decision_end": decision_end.isoformat(),
            "review_inputs": {
                name: value["object_sha256"] for name, value in review_receipts.items()
            },
            "review_receipts": review_receipts,
            "artifacts": {
                name: sha256_json(_canonical_rows(frame))
                for name, frame in sorted(artifacts.items())
            },
            "gap_report_sha256": sha256_json(gaps),
        }
        workspace_id = sha256_json(manifest_identity)
        manifest = {
            **manifest_identity,
            "workspace_id": workspace_id,
            "status": "REVIEW_READY" if not gaps["blocking_gaps"] else "DATA_BLOCKED",
            "direct_build_allowed": not gaps["blocking_gaps"],
            "universe_id": UNIVERSE_ID,
            "gap_counts": gaps["counts"],
        }
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_object = self.store.put_bytes(
            manifest_bytes,
            media_type="application/vnd.us-pit.reviewed-workspace-manifest+json",
        )
        manifest_receipt = {
            "workspace_id": workspace_id,
            "manifest_sha256": manifest_object.sha256,
            "manifest_size_bytes": manifest_object.size_bytes,
            "cas_object_sha256": manifest_object.sha256,
        }
        return self._publish(
            output,
            workspace_id,
            artifacts,
            gaps,
            manifest,
            manifest_receipt,
        )

    def _load_sources(self, batch_ids: tuple[str, ...]) -> tuple[SourceDependency, ...]:
        values: dict[tuple[str, str, str, str], SourceDependency] = {}
        for batch_id in batch_ids:
            for item in self.store.load_source_batch(batch_id).dependencies:
                values[(item.source_id, item.dataset, item.as_of_date or "", item.object_sha256)] = item
        return tuple(values[key] for key in sorted(values))

    def _load_normalization(
        self, value: OfficialNormalizationResult | Path | str
    ) -> OfficialNormalizationResult:
        if isinstance(value, OfficialNormalizationResult):
            result = value
        else:
            path = Path(value).resolve()
            manifest_path = path / "manifest.json"
            if not manifest_path.is_file():
                raise ReviewWorkspaceError("normalization manifest not found")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result = OfficialNormalizationResult(str(manifest.get("normalization_id", "")), path, manifest)
        if result.manifest.get("candidate_only") is not True or result.manifest.get("direct_build_allowed") is not False:
            raise ReviewWorkspaceError("normalization package is not a review-only official candidate")
        if result.path.name != result.normalization_id:
            raise ReviewWorkspaceError("normalization directory identity mismatch")
        for name in (
            "fund_holdings_observed_candidate",
            "security_identity_candidates",
            "normalization_issues",
        ):
            result.load_frame(name)
        return result

    @staticmethod
    def _read_identity_review(path: Path) -> pd.DataFrame:
        columns = [
            "holding_candidate_id",
            "approved",
            "issuer_id",
            "exchange",
            "valid_from",
            "valid_to",
            "review_note",
            "resolved_issue_ids",
        ]
        if not path.is_file():
            return pd.DataFrame(columns=columns)
        frame = pd.read_parquet(path)
        required = {
            "holding_candidate_id",
            "approved",
            "issuer_id",
            "exchange",
            "valid_from",
            "valid_to",
            "review_note",
            "resolved_issue_ids",
        }
        missing = required - set(frame.columns)
        if missing:
            raise ReviewWorkspaceError(
                f"identity review is missing columns: {', '.join(sorted(missing))}"
            )
        if frame["holding_candidate_id"].isna().any() or frame["holding_candidate_id"].duplicated().any():
            raise ReviewWorkspaceError("identity review candidate key must be unique and non-null")
        return frame

    @staticmethod
    def _resolve_identities(
        holdings: pd.DataFrame,
        identities: pd.DataFrame,
        issues: pd.DataFrame,
        review: pd.DataFrame,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        if set(review["holding_candidate_id"].astype(str)) - set(holdings["holding_candidate_id"].astype(str)):
            raise ReviewWorkspaceError("identity review references an unknown holding candidate")
        candidate = holdings.merge(
            review,
            on="holding_candidate_id",
            how="left",
            validate="one_to_one",
            suffixes=("", "_review"),
        )
        if len(identities) != len(holdings):
            raise ReviewWorkspaceError("identity candidates and holdings candidates disagree")
        if set(identities["holding_candidate_id"].astype(str)) != set(
            holdings["holding_candidate_id"].astype(str)
        ):
            raise ReviewWorkspaceError(
                "identity candidate keys and holding candidate keys disagree"
            )
        identity_columns = [
            "holding_candidate_id",
            "identity_candidate_key",
            "isin",
            "cusip",
            "content_sha256",
            "source_row_number",
        ]
        left = holdings[identity_columns].astype("string").sort_values(
            "holding_candidate_id"
        ).reset_index(drop=True)
        right = identities[identity_columns].astype("string").sort_values(
            "holding_candidate_id"
        ).reset_index(drop=True)
        if not left.equals(right):
            raise ReviewWorkspaceError(
                "identity candidates do not reproduce holding identifier lineage"
            )
        canonical_ids = _candidate_identity_components(holdings)
        unresolved: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        # First pass: issuer identities established by approved SIGNAL_INPUT
        # rows.  These may legitimately confirm issuer identity for securities
        # also seen in validation anchors (the anchor itself never backfills
        # anything - it merely co-observes an already-proven identity).
        signal_issuer_ids: dict[str, str] = {}
        for row in candidate.to_dict(orient="records"):
            if str(row.get("evidence_role")) != SourceRole.SIGNAL_INPUT.value:
                continue
            approved = str(row.get("approved", "")).strip().casefold() in {"1", "true", "yes"}
            identifier_key = _clean_text(row.get("identity_candidate_key"))
            note = _clean_text(row.get("review_note"))
            if not approved or not identifier_key or not note:
                continue
            try:
                security_id = canonical_ids[str(row["holding_candidate_id"])]
            except KeyError:
                continue
            issuer_id = _issuer_id(pd.Series(row))
            if not issuer_id:
                continue
            existing = signal_issuer_ids.get(security_id)
            if existing is not None and existing != issuer_id:
                raise ReviewWorkspaceError(
                    "conflicting issuer identities for " + security_id
                )
            signal_issuer_ids[security_id] = issuer_id

        for row in candidate.to_dict(orient="records"):
            approved = str(row.get("approved", "")).strip().casefold() in {"1", "true", "yes"}
            identifier_key = _clean_text(row.get("identity_candidate_key"))
            note = _clean_text(row.get("review_note"))
            validation_anchor = str(row.get("evidence_role")) == SourceRole.VALIDATION_ANCHOR.value
            if validation_anchor and identifier_key:
                approved = True
                note = note or "OFFICIAL_VALIDATION_ANCHOR_STABLE_IDENTIFIER"
            if not approved or not identifier_key or not note:
                unresolved.append(
                    {
                        "code": "IDENTITY_NOT_EXPLICITLY_APPROVED",
                        "holding_candidate_id": str(row["holding_candidate_id"]),
                        "detail": "candidate requires approval, stable identity, and review note",
                    }
                )
                continue
            try:
                row["security_id"] = canonical_ids[str(row["holding_candidate_id"])]
                if validation_anchor:
                    signal_issuer = signal_issuer_ids.get(str(row["security_id"]))
                    row_issuer = _clean_text(row.get("issuer_id"))
                    if signal_issuer is not None:
                        row["issuer_id_resolved"] = signal_issuer
                    elif row_issuer and row_issuer.startswith("us_issuer_"):
                        # Operator-reviewed issuer identity bound from an
                        # allow-listed non-anchor official source (e.g. the
                        # frozen SEC company index) during identity review.
                        row["issuer_id_resolved"] = row_issuer
                    else:
                        unresolved.append(
                            {
                                "code": "ISSUER_IDENTITY_NOT_EXPLICITLY_APPROVED",
                                "security_id": str(row["security_id"]),
                                "detail": (
                                    "validation anchors cannot backfill an issuer identity; "
                                    "freeze and approve issuer/share-class evidence"
                                ),
                            }
                        )
                        row["issuer_id_resolved"] = None
                else:
                    row["issuer_id_resolved"] = _issuer_id(pd.Series(row))
                # A historical validation response observed today can verify an
                # identity, but it cannot establish that ticker/exchange was
                # available at the historical decision time.
                row["ticker_resolved"] = (
                    None if validation_anchor else _normalize_ticker(row.get("ticker"))
                )
                row["exchange_resolved"] = (
                    None if validation_anchor else _clean_text(row.get("exchange"))
                )
                valid_from_value = row.get("valid_from")
                if validation_anchor and _clean_text(valid_from_value) is None:
                    valid_from_value = row.get("as_of_date")
                row["valid_from_resolved"] = _date_string(
                    valid_from_value, field="valid_from"
                )
                row["valid_to_resolved"] = (
                    None if _clean_text(row.get("valid_to")) is None else _date_string(row.get("valid_to"), field="valid_to")
                )
                if not validation_anchor and not row["exchange_resolved"]:
                    raise ReviewWorkspaceError("exchange is required")
            except ReviewWorkspaceError as exc:
                unresolved.append(
                    {
                        "code": "IDENTITY_REVIEW_INVALID",
                        "holding_candidate_id": str(row["holding_candidate_id"]),
                        "detail": str(exc),
                    }
                )
                continue
            rows.append(row)
        resolved = pd.DataFrame(rows)
        return resolved, unresolved

    @staticmethod
    def _resolved_issue_ids(
        review: pd.DataFrame,
        holdings: pd.DataFrame,
        issues: pd.DataFrame,
    ) -> set[str]:
        candidate_rows = {
            str(row.holding_candidate_id): (
                str(row.content_sha256),
                int(row.source_row_number),
            )
            for row in holdings.itertuples(index=False)
        }
        issue_rows = {
            str(row.issue_id): (
                str(row.content_sha256),
                int(row.source_row_number),
            )
            for row in issues.itertuples(index=False)
        }
        resolved: set[str] = set()
        for row in review.to_dict(orient="records"):
            approved = str(row.get("approved", "")).strip().casefold() in {
                "1",
                "true",
                "yes",
            }
            note = _clean_text(row.get("review_note"))
            raw_ids = _clean_text(row.get("resolved_issue_ids"))
            if not approved or not note or not raw_ids:
                continue
            candidate_key = str(row.get("holding_candidate_id"))
            expected = candidate_rows.get(candidate_key)
            if expected is None:
                raise ReviewWorkspaceError(
                    f"identity review references an unknown holding candidate: {candidate_key}"
                )
            for issue_id in (item.strip() for item in raw_ids.split(",")):
                if issue_id and issue_id not in issue_rows:
                    raise ReviewWorkspaceError(
                        f"identity review resolves an unknown issue_id: {issue_id}"
                    )
                if issue_id:
                    if issue_rows[issue_id] != expected:
                        raise ReviewWorkspaceError(
                            "identity review may resolve only issues from the same source row"
                        )
                    resolved.add(issue_id)
        return resolved

    @staticmethod
    def _holdings(
        resolved: pd.DataFrame,
        source_keys: set[tuple[str, str, str]],
    ) -> pd.DataFrame:
        columns = sorted(REQUIRED_ARTIFACT_COLUMNS["fund_holdings_observed"])
        if resolved.empty:
            return pd.DataFrame(columns=columns)
        rows = []
        for row in resolved.to_dict(orient="records"):
            key = (str(row["source_id"]), "fund_holdings_observed", str(row["content_sha256"]))
            if key not in source_keys:
                raise ReviewWorkspaceError("normalized holding is not bound to a supplied source batch")
            rows.append(
                {
                    "as_of_date": row["as_of_date"],
                    "published_at": row["published_at"],
                    "observed_at": row["observed_at"],
                    "url": row["url"],
                    "source_version": row["source_version"],
                    "content_sha256": row["content_sha256"],
                    "evidence_role": row["evidence_role"],
                    "security_id": row["security_id"],
                    "source_id": row["source_id"],
                }
            )
        return pd.DataFrame(rows).drop_duplicates().sort_values(
            ["as_of_date", "content_sha256", "security_id"], kind="stable"
        ).reset_index(drop=True)

    @staticmethod
    def _security_master(resolved: pd.DataFrame) -> pd.DataFrame:
        columns = sorted(REQUIRED_ARTIFACT_COLUMNS["security_master"])
        if resolved.empty:
            return pd.DataFrame(columns=columns)
        rows = []
        for row in resolved.to_dict(orient="records"):
            isin, cusip = _clean_identifier(row.get("isin")), _clean_identifier(row.get("cusip"))
            primary_type, primary = ("ISIN", isin) if isin else ("CUSIP", cusip)
            rows.append(
                {
                    "security_id": row["security_id"],
                    "issuer_id": row["issuer_id_resolved"],
                    "primary_identifier_type": primary_type,
                    "primary_identifier": primary,
                    "asset_class": "COMMON_EQUITY",
                }
            )
        frame = pd.DataFrame(rows).drop_duplicates()
        if frame["security_id"].duplicated().any():
            raise ReviewWorkspaceError("one security_id has conflicting master records")
        return frame.sort_values("security_id").reset_index(drop=True)

    @staticmethod
    def _identifiers(resolved: pd.DataFrame) -> pd.DataFrame:
        columns = sorted(REQUIRED_ARTIFACT_COLUMNS["identifiers"])
        rows: list[dict[str, Any]] = []
        for row in resolved.to_dict(orient="records"):
            for identifier_type, field in (("ISIN", "isin"), ("CUSIP", "cusip")):
                value = _clean_identifier(row.get(field))
                if value:
                    rows.append(
                        {
                            "security_id": row["security_id"],
                            "identifier_type": identifier_type,
                            "identifier_value": value,
                            "valid_from": row["valid_from_resolved"],
                            "valid_to": row["valid_to_resolved"],
                        }
                    )
        return pd.DataFrame(rows, columns=columns).drop_duplicates().sort_values(
            ["security_id", "identifier_type", "valid_from"], kind="stable"
        ).reset_index(drop=True)

    @staticmethod
    def _listing_aliases(resolved: pd.DataFrame) -> pd.DataFrame:
        columns = sorted(REQUIRED_ARTIFACT_COLUMNS["listing_aliases"])
        rows = [
            {
                "security_id": row["security_id"],
                "ticker": row["ticker_resolved"],
                "vendor_code": f"{row['ticker_resolved']}.US",
                "exchange": row["exchange_resolved"],
                "valid_from": row["valid_from_resolved"],
                "valid_to": row["valid_to_resolved"],
            }
            for row in resolved.to_dict(orient="records")
            if _clean_text(row.get("ticker_resolved")) is not None
            and _clean_text(row.get("exchange_resolved")) is not None
        ]
        frame = pd.DataFrame(rows, columns=columns).drop_duplicates()
        if frame.empty:
            return frame
        return frame.sort_values(
            ["security_id", "valid_from", "ticker"], kind="stable"
        ).reset_index(drop=True)

    @staticmethod
    def _reviewed_evidence_table(
        path: Path,
        dataset: str,
        source_keys: set[tuple[str, str, str]],
    ) -> pd.DataFrame:
        if not path.is_file():
            return _empty_frame(dataset)
        frame = pd.read_parquet(path)
        missing = REQUIRED_ARTIFACT_COLUMNS[dataset] - set(frame.columns)
        if missing:
            raise ReviewWorkspaceError(
                f"{path.name} is missing columns: {', '.join(sorted(missing))}"
            )
        if not frame.empty:
            if "approved" not in frame.columns or "review_note" not in frame.columns:
                raise ReviewWorkspaceError(
                    f"{dataset} rows require explicit approved and review_note fields"
                )
            approved = frame["approved"].fillna(False).astype(bool)
            notes = frame["review_note"].fillna("").astype(str).str.strip()
            if not approved.all() or notes.eq("").any():
                raise ReviewWorkspaceError(
                    f"{dataset} contains rows without explicit evidence review approval"
                )
        hash_column = "evidence_sha256"
        for row in frame.to_dict(orient="records"):
            key = (str(row["source_id"]), dataset, str(row[hash_column]).lower())
            if key not in source_keys:
                raise ReviewWorkspaceError(
                    f"{dataset} row lacks an exact captured source dependency"
                )
        return frame.drop(columns=["approved", "review_note"], errors="ignore").copy()

    @staticmethod
    def _validate_lifecycle_evidence(
        lifecycle: pd.DataFrame,
        holdings: pd.DataFrame,
        events: pd.DataFrame,
        actions: pd.DataFrame,
        source_keys: set[tuple[str, str, str]],
    ) -> None:
        for row in lifecycle.to_dict(orient="records"):
            if str(row.get("scope")) != "SECURITY" or str(
                row.get("status")
            ) != "RECONCILED":
                raise ReviewWorkspaceError(
                    "lifecycle rows must be per-security RECONCILED evidence"
                )
            digest = str(row.get("evidence_sha256", "")).lower()
            source_id = str(row.get("source_id", ""))
            security_id = str(row.get("security_id", ""))
            action_id = _clean_text(row.get("action_id"))
            if action_id:
                dataset = "corporate_actions"
                frame = actions
                matched = (
                    frame["security_id"].astype(str).eq(security_id)
                    & frame["action_id"].astype(str).eq(action_id)
                    & frame["evidence_sha256"].astype(str).str.lower().eq(digest)
                ) if not frame.empty else pd.Series(dtype=bool)
            else:
                candidates: list[tuple[str, pd.DataFrame, str]] = [
                    ("fund_holdings_observed", holdings, "content_sha256"),
                    ("membership_events", events, "evidence_sha256"),
                ]
                matches = []
                for candidate_dataset, frame, hash_column in candidates:
                    if frame.empty:
                        continue
                    mask = (
                        frame["security_id"].astype(str).eq(security_id)
                        & frame[hash_column].astype(str).str.lower().eq(digest)
                    )
                    if bool(mask.any()):
                        matches.append(candidate_dataset)
                if len(matches) != 1:
                    raise ReviewWorkspaceError(
                        "lifecycle evidence must resolve to one holding, event, or action row"
                    )
                dataset = matches[0]
                matched = pd.Series([True])
            if not bool(matched.any()) or (
                source_id,
                dataset,
                digest,
            ) not in source_keys:
                raise ReviewWorkspaceError(
                    "lifecycle reconciliation lacks exact captured evidence"
                )

    @staticmethod
    def _frozen_xnys_calendar(start: date, end: date) -> pd.DataFrame:
        calendar = xcals.get_calendar("XNYS")
        first_decision_month = pd.Timestamp(start).to_period("M").start_time
        warmup_sessions = calendar.sessions_window(
            calendar.date_to_session(first_decision_month, direction="next"),
            -282,
        )
        schedule_start = pd.Timestamp(warmup_sessions[0]).date()
        end_label = pd.Timestamp(end)
        if calendar.is_session(end_label):
            next_label = calendar.next_session(end_label)
        else:
            next_label = calendar.date_to_session(end_label, direction="next")
        schedule = calendar.schedule.loc[str(schedule_start) : str(next_label)]
        return pd.DataFrame(
            {
                "session_date": pd.DatetimeIndex(schedule.index)
                .tz_localize(None)
                .normalize(),
                "market_open": [
                    pd.Timestamp(value)
                    .tz_convert("America/New_York")
                    .isoformat()
                    for value in schedule["open"]
                ],
                "market_close": [
                    pd.Timestamp(value)
                    .tz_convert("America/New_York")
                    .isoformat()
                    for value in schedule["close"]
                ],
            }
        ).reset_index(drop=True)

    @staticmethod
    def _decision_month_ends(
        calendar: pd.DataFrame, start: date, end: date
    ) -> pd.DatetimeIndex:
        if "session_date" not in calendar:
            raise ReviewWorkspaceError("frozen XNYS calendar requires session_date")
        sessions = pd.DatetimeIndex(pd.to_datetime(calendar["session_date"], errors="coerce"))
        if sessions.isna().any() or sessions.duplicated().any():
            raise ReviewWorkspaceError("review calendar has invalid or duplicate sessions")
        sessions = sessions.normalize().sort_values()
        window = sessions[(sessions.date >= start) & (sessions.date <= end)]
        values = [group.max() for _, group in pd.Series(window, index=window).groupby(window.to_period("M"))]
        return pd.DatetimeIndex(values)

    @staticmethod
    def _replay_membership(
        holdings: pd.DataFrame,
        events: pd.DataFrame,
        corporate_actions: pd.DataFrame,
        decisions: pd.DatetimeIndex,
        sources: tuple[SourceDependency, ...],
        calendar: pd.DataFrame,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        columns = sorted(REQUIRED_ARTIFACT_COLUMNS["membership_monthly"])
        if decisions.empty or holdings.empty or calendar.empty:
            return pd.DataFrame(columns=columns), [
                {"code": "NO_REPLAYABLE_DECISIONS", "detail": "calendar or holdings baseline is missing"}
            ]
        if any(
            item.source_id == "sec_nport_ivv"
            and item.dataset == "fund_holdings_observed"
            and dict(item.metadata).get("artifact_kind")
            == "raw_complete_edgar_submission"
            for item in sources
        ):
            result = replay_causal_membership(
                holdings,
                events,
                decisions,
                sources,
                calendar,
                corporate_actions,
            )
            return result.memberships.reindex(columns=columns), list(result.gaps)
        calendar = calendar.copy()
        calendar["session_date"] = pd.to_datetime(calendar["session_date"]).dt.normalize()
        calendar["close"] = pd.to_datetime(calendar["market_close"], utc=True, errors="coerce")
        close_by_day = dict(zip(calendar["session_date"], calendar["close"], strict=True))
        dependency_by_hash: dict[str, list[SourceDependency]] = {}
        for item in sources:
            dependency_by_hash.setdefault(item.object_sha256, []).append(item)
        values = holdings.copy()
        values["as_of"] = pd.to_datetime(values["as_of_date"], errors="coerce").dt.normalize()
        values["published"] = pd.to_datetime(values["published_at"], errors="coerce", utc=True)
        values["observed"] = pd.to_datetime(values["observed_at"], errors="coerce", utc=True)
        values = values.loc[values["evidence_role"].astype(str).eq(SourceRole.SIGNAL_INPUT.value)]
        snapshots = []
        for digest, group in values.groupby("content_sha256"):
            snapshots.append(
                {
                    "digest": digest,
                    "as_of": group["as_of"].iloc[0],
                    "published": group["published"].iloc[0],
                    "observed": group["observed"].iloc[0],
                    "members": set(group["security_id"].astype(str)),
                }
            )
        event_values = events.copy()
        if not event_values.empty:
            event_values["announced"] = pd.to_datetime(event_values["announced_at"], utc=True, errors="coerce")
            event_values["effective"] = pd.to_datetime(event_values["effective_at"], utc=True, errors="coerce")
        rows: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for decision in decisions:
            cutoff = close_by_day.get(decision)
            eligible = [
                item for item in snapshots
                if not pd.isna(item["published"])
                and not pd.isna(item["observed"])
                and item["as_of"] <= decision
                and item["published"] <= cutoff
                and item["observed"] <= cutoff
            ]
            if not eligible:
                gaps.append({"code": "MISSING_DECISION_TIME_BASELINE", "decision_date": decision.date().isoformat()})
                continue
            baseline = max(eligible, key=lambda item: (item["as_of"], item["observed"], item["digest"]))
            members = set(baseline["members"])
            if not event_values.empty:
                applicable = event_values.loc[
                    event_values["announced"].le(cutoff)
                    & event_values["effective"].le(cutoff)
                    & event_values["effective"].dt.tz_convert(None).dt.normalize().gt(baseline["as_of"])
                ].sort_values(["effective", "announced", "event_id"], kind="stable")
                for event in applicable.to_dict(orient="records"):
                    dependencies = [
                        item
                        for item in dependency_by_hash.get(
                            str(event["evidence_sha256"]).lower(), []
                        )
                        if item.dataset == "membership_events"
                        and item.role == SourceRole.SIGNAL_INPUT
                        and item.source_id == str(event["source_id"])
                    ]
                    if len(dependencies) != 1:
                        gaps.append({"code": "UNPROVEN_MEMBERSHIP_EVENT", "event_id": str(event["event_id"])})
                        continue
                    dependency = dependencies[0]
                    source_published = pd.to_datetime(
                        dependency.published_at, errors="coerce", utc=True
                    )
                    source_observed = pd.to_datetime(
                        dependency.observed_at, errors="coerce", utc=True
                    )
                    if (
                        pd.isna(source_published)
                        or pd.isna(source_observed)
                        or source_published > cutoff
                        or source_observed > cutoff
                    ):
                        gaps.append(
                            {
                                "code": "MEMBERSHIP_EVENT_NOT_AVAILABLE",
                                "event_id": str(event["event_id"]),
                                "decision_date": decision.date().isoformat(),
                            }
                        )
                        continue
                    kind = str(event["event_type"]).upper()
                    security_id = str(event["security_id"])
                    if kind == "ADD":
                        if security_id in members:
                            gaps.append(
                                {
                                    "code": "MEMBERSHIP_EVENT_STATE_CONFLICT",
                                    "event_id": str(event["event_id"]),
                                    "detail": "ADD targets an existing member",
                                }
                            )
                        members.add(security_id)
                    elif kind == "REMOVE":
                        if security_id not in members:
                            gaps.append(
                                {
                                    "code": "MEMBERSHIP_EVENT_STATE_CONFLICT",
                                    "event_id": str(event["event_id"]),
                                    "detail": "REMOVE targets a non-member",
                                }
                            )
                        members.discard(security_id)
                    else:
                        gaps.append({"code": "UNSUPPORTED_MEMBERSHIP_EVENT", "event_id": str(event["event_id"])})
            rows.extend(
                {"universe_id": UNIVERSE_ID, "decision_date": decision, "security_id": security_id}
                for security_id in sorted(members)
            )
        return pd.DataFrame(rows, columns=columns), gaps

    @staticmethod
    def _anchor_reconciliations(holdings: pd.DataFrame, memberships: pd.DataFrame) -> pd.DataFrame:
        columns = sorted(REQUIRED_ARTIFACT_COLUMNS["anchor_reconciliations"])
        rows: list[dict[str, Any]] = []
        if holdings.empty or memberships.empty:
            return pd.DataFrame(columns=columns)
        decision_values = pd.to_datetime(memberships["decision_date"]).dt.normalize()
        for digest, group in holdings.loc[
            holdings["evidence_role"].astype(str).eq(SourceRole.VALIDATION_ANCHOR.value)
        ].groupby("content_sha256"):
            anchor_date = pd.to_datetime(group["as_of_date"].iloc[0]).normalize()
            same_month = sorted(set(decision_values[decision_values.dt.to_period("M").eq(anchor_date.to_period("M"))]))
            if not same_month:
                continue
            decision = same_month[-1]
            replay = set(memberships.loc[decision_values.eq(decision), "security_id"].astype(str))
            anchor = set(group["security_id"].astype(str))
            rows.append(
                {
                    "anchor_date": anchor_date,
                    "status": "RECONCILED" if anchor == replay else "UNRESOLVED",
                    "unexplained_additions": len(anchor - replay),
                    "unexplained_removals": len(replay - anchor),
                    "source_id": group["source_id"].iloc[0],
                    "evidence_sha256": digest,
                }
            )
        return pd.DataFrame(rows, columns=columns)

    @staticmethod
    def _gap_report(
        artifacts: Mapping[str, pd.DataFrame],
        decisions: pd.DatetimeIndex,
        unresolved: list[dict[str, Any]],
        replay_gaps: list[dict[str, Any]],
        normalization_issues: pd.DataFrame,
        resolved_issue_ids: set[str],
    ) -> dict[str, Any]:
        gaps: list[dict[str, Any]] = list(unresolved) + list(replay_gaps)
        high_issues = normalization_issues.loc[
            normalization_issues["severity"].astype(str).isin({"CRITICAL", "HIGH"})
        ]
        gaps.extend(
            {
                "code": str(row.code),
                "source_id": str(row.source_id),
                "source_row_number": int(row.source_row_number),
                "issue_id": str(row.issue_id),
            }
            for row in high_issues.itertuples(index=False)
            if str(row.issue_id) not in resolved_issue_ids
        )
        if len(decisions) < 60:
            gaps.append({"code": "INSUFFICIENT_DECISION_MONTHS", "actual": len(decisions), "required": 60})
        # EMPTY-PREPARE-EV2 (approved): the market-bar, benchmark, fee,
        # listing-alias, coverage and lifecycle artifacts are filled by a later
        # prepare-market pass, never by assemble-reviewed itself.  An empty
        # frame for these is a legitimate intermediate state (recorded as
        # deferred) and does not make this assembler pass fail-open at the
        # final gate; prepare-market enforces non-empty on its own output.
        allow_empty = {"membership_events", "corporate_actions", "session_exceptions"}
        prepare_deferred = {
            "listing_aliases", "bars_raw", "bars_vendor_front", "bars_pit_signal",
            "benchmarks", "execution_fee_schedule", "bar_coverage",
            "lifecycle_reconciliations",
        }
        for dataset, frame in artifacts.items():
            missing = sorted(REQUIRED_ARTIFACT_COLUMNS[dataset] - set(frame.columns))
            if missing:
                gaps.append({"code": "SCHEMA_MISMATCH", "dataset": dataset, "missing_columns": missing})
            elif frame.empty and dataset not in allow_empty:
                if dataset in prepare_deferred:
                    gaps.append({
                        "code": "EMPTY_REQUIRED_ARTIFACT",
                        "dataset": dataset,
                        "detail": "deferred to prepare-market (EMPTY-PREPARE-EV2)",
                    })
                else:
                    gaps.append({"code": "EMPTY_REQUIRED_ARTIFACT", "dataset": dataset})
        deduplicated = []
        deferred: list[dict[str, Any]] = []
        seen: set[str] = set()
        for gap in gaps:
            key = sha256_json(gap)
            if key in seen:
                continue
            seen.add(key)
            if (
                str(gap.get("code") or "") == "EMPTY_REQUIRED_ARTIFACT"
                and str(gap.get("detail") or "").startswith("deferred to prepare-market")
            ):
                deferred.append(gap)
            else:
                deduplicated.append(gap)
        counts: dict[str, int] = {}
        for gap in deduplicated + deferred:
            code = str(gap.get("code", "UNKNOWN"))
            counts[code] = counts.get(code, 0) + 1
        return {
            "status": "DATA_BLOCKED" if deduplicated else "REVIEW_READY",
            "counts": counts,
            "blocking_gaps": deduplicated,
            "deferred_gaps": deferred,
        }

    def _freeze_review_inputs(
        self,
        root: Path,
        *,
        reviewer: str,
        approved_at: datetime,
    ) -> dict[str, dict[str, Any]]:
        receipts: dict[str, dict[str, Any]] = {}
        for path in sorted(root.glob("*.parquet")):
            if not path.is_file():
                continue
            reference = self.store.put_bytes(
                path.read_bytes(), media_type="application/vnd.apache.parquet"
            )
            receipts[path.name] = {
                "object_sha256": reference.sha256,
                "size_bytes": reference.size_bytes,
                "reviewer": reviewer,
                "approved_at": approved_at.isoformat(),
                "source_filename": path.name,
            }
        return receipts

    def _publish(
        self,
        output: Path,
        workspace_id: str,
        artifacts: Mapping[str, pd.DataFrame],
        gaps: Mapping[str, Any],
        manifest: Mapping[str, Any],
        manifest_receipt: Mapping[str, Any],
    ) -> ReviewWorkspaceResult:
        target = output / workspace_id
        manifest_bytes = canonical_json_bytes(manifest)
        receipt_bytes = canonical_json_bytes(manifest_receipt)
        if target.exists():
            existing = target / "manifest.json"
            if not existing.is_file() or existing.read_bytes() != manifest_bytes:
                raise ReviewWorkspaceError("review workspace identity collision")
            receipt_path = target / "manifest.cas.json"
            object_path = self.store.object_path(
                str(manifest_receipt["cas_object_sha256"])
            )
            if (
                not receipt_path.is_file()
                or receipt_path.read_bytes() != receipt_bytes
                or not object_path.is_file()
                or object_path.read_bytes() != manifest_bytes
            ):
                raise ReviewWorkspaceError(
                    "review workspace manifest CAS receipt is missing or corrupt"
                )
            for dataset, expected in dict(manifest["artifacts"]).items():
                path = target / f"{dataset}.parquet"
                if not path.is_file() or sha256_json(
                    _canonical_rows(pd.read_parquet(path))
                ) != expected:
                    raise ReviewWorkspaceError(
                        f"review workspace artifact is missing or corrupt: {dataset}"
                    )
            gap_path = target / "gap_report.json"
            if not gap_path.is_file() or sha256_json(
                json.loads(gap_path.read_text(encoding="utf-8"))
            ) != manifest["gap_report_sha256"]:
                raise ReviewWorkspaceError("review workspace gap report is missing or corrupt")
            return ReviewWorkspaceResult(workspace_id, target, manifest)
        output.mkdir(parents=True, exist_ok=True)
        stage = output / f".{workspace_id}.{uuid4().hex}.staging"
        stage.mkdir()
        try:
            for dataset, frame in artifacts.items():
                frame.to_parquet(stage / f"{dataset}.parquet", index=False)
            (stage / "gap_report.json").write_bytes(canonical_json_bytes(gaps))
            (stage / "manifest.json").write_bytes(manifest_bytes)
            (stage / "manifest.cas.json").write_bytes(receipt_bytes)
            for path in stage.iterdir():
                path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            os.rename(stage, target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return ReviewWorkspaceResult(workspace_id, target, manifest)


__all__ = [
    "ReviewWorkspaceError",
    "ReviewWorkspaceResult",
    "USPITReviewWorkspaceAssembler",
    "WORKSPACE_FORMAT_VERSION",
    "stable_security_id",
]
