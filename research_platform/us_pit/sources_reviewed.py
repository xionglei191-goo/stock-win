from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from .models import LicenseClass, SourceRole
from .sources import SourceAdapter, SourceArtifact, SyncRequest


_SIGNAL_DATASETS = frozenset(
    {
        "membership_events",
        "corporate_actions",
        "session_exceptions",
        "benchmark_total_return",
        "lifecycle_status",
        "lifecycle_observation",
    }
)
_SUPPORTED_DATASETS = _SIGNAL_DATASETS | frozenset({"fund_holdings_observed"})


@dataclass(frozen=True)
class ReviewedEvidenceSpec:
    """One locally reviewed copy of a public primary-source object.

    The local file is only a transport into the content-addressed store.  Its
    asserted publication time and public URL remain subject to row-level
    reconciliation and the release quality gates.
    """

    path: Path
    dataset: str
    source_id: str
    source_version: str
    public_url: str
    role: SourceRole
    license_class: LicenseClass
    published_at: datetime | None
    as_of_date: date | None = None
    media_type: str | None = None
    authority: str = "OFFICIAL_PRIMARY"
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        dataset = self.dataset.strip()
        if dataset not in _SUPPORTED_DATASETS:
            raise ValueError(f"unsupported reviewed evidence dataset: {dataset}")
        if not self.source_id.strip() or not self.source_version.strip():
            raise ValueError("reviewed evidence requires source_id and source_version")
        if self.license_class == LicenseClass.UNLICENSED_REFERENCE:
            raise ValueError("unlicensed reference material cannot be imported as reviewed evidence")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        if self.role == SourceRole.SIGNAL_INPUT and self.published_at is None:
            raise ValueError("signal evidence requires a verified publication timestamp")
        if dataset in _SIGNAL_DATASETS and self.role != SourceRole.SIGNAL_INPUT:
            raise ValueError(f"{dataset} evidence must use SIGNAL_INPUT")
        if dataset == "fund_holdings_observed" and self.as_of_date is None:
            raise ValueError("fund holdings evidence requires as_of_date")
        if dataset == "lifecycle_status":
            required = {
                "coverage_kind",
                "current_through",
                "covered_security_ids_sha256",
            }
            missing = required - set(self.metadata)
            if missing:
                raise ValueError(
                    "lifecycle evidence metadata is incomplete: "
                    + ", ".join(sorted(missing))
                )
        parsed = urlparse(self.public_url)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not hostname:
            raise ValueError("reviewed evidence requires a public HTTPS source URL")
        if hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".invalid"):
            raise ValueError("reviewed evidence URL cannot point to a local or placeholder host")
        if not self.authority.strip():
            raise ValueError("reviewed evidence authority is required")


class ReviewedLocalEvidenceAdapter(SourceAdapter):
    """Freeze one explicitly reviewed source file without interpreting it."""

    def __init__(self, spec: ReviewedEvidenceSpec) -> None:
        self.spec = spec
        self.source_id = spec.source_id.strip()
        self.source_version = spec.source_version.strip()

    def fetch(self, request: SyncRequest) -> tuple[SourceArtifact, ...]:
        path = self.spec.path.expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"reviewed evidence is not a regular file: {path}")
        payload = path.read_bytes()
        if not payload:
            raise ValueError("reviewed evidence file is empty")
        if self.spec.published_at is not None:
            observed_utc = request.observed_at.astimezone().astimezone(
                self.spec.published_at.tzinfo
            )
            if self.spec.published_at > observed_utc:
                raise ValueError("reviewed evidence publication time is after observed_at")
        if self.spec.as_of_date is not None and not (
            request.start_date <= self.spec.as_of_date <= request.end_date
        ):
            raise ValueError("reviewed evidence as_of_date is outside the sync window")
        media_type = self.spec.media_type or mimetypes.guess_type(path.name)[0]
        if not media_type:
            media_type = "application/octet-stream"
        return (
            SourceArtifact(
                dataset=self.spec.dataset.strip(),
                payload=payload,
                media_type=media_type,
                url=self.spec.public_url,
                observed_at=request.observed_at,
                published_at=self.spec.published_at,
                as_of_date=self.spec.as_of_date,
                role=self.spec.role,
                license_class=self.spec.license_class,
                metadata={
                    "artifact_kind": "locally_reviewed_primary_source_copy",
                    "authority": self.spec.authority.strip(),
                    "original_filename": path.name,
                    "raw_frozen": True,
                    "normalization_performed": False,
                    "human_review_assertion_only": True,
                    "eligible_for_historical_signal": (
                        self.spec.role == SourceRole.SIGNAL_INPUT
                    ),
                    **dict(self.spec.metadata),
                },
            ),
        )


__all__ = ["ReviewedEvidenceSpec", "ReviewedLocalEvidenceAdapter"]
