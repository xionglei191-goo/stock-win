from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .hashing import sha256_json


UNIVERSE_ID = "sp500_ivv_proxy_v1"
MANIFEST_FORMAT_VERSION = "us-pit-release-v1"
QUALITY_POLICY_VERSION = "us-pit-quality-v1"
QUALITY_CONTRACT_REVISION = 3


class ReleaseStatus(StrEnum):
    DATA_BLOCKED = "DATA_BLOCKED"
    DATA_READY = "DATA_READY"


class QualitySeverity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SourceRole(StrEnum):
    SIGNAL_INPUT = "SIGNAL_INPUT"
    VALIDATION_ANCHOR = "VALIDATION_ANCHOR"
    CROSS_CHECK = "CROSS_CHECK"
    REFERENCE_ONLY = "REFERENCE_ONLY"


class LicenseClass(StrEnum):
    OFFICIAL_PUBLIC = "OFFICIAL_PUBLIC"
    PERMISSIVE = "PERMISSIVE"
    LOCAL_VENDOR = "LOCAL_VENDOR"
    UNLICENSED_REFERENCE = "UNLICENSED_REFERENCE"


class EvidenceAuthority(StrEnum):
    AUTHORITATIVE_PRIMARY = "AUTHORITATIVE_PRIMARY"
    INDEPENDENT_SECONDARY = "INDEPENDENT_SECONDARY"


def _iso(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class ObjectRef:
    sha256: str
    size_bytes: int
    media_type: str
    path: Path
    row_count: int | None = None
    schema_sha256: str | None = None


@dataclass(frozen=True)
class ArtifactDescriptor:
    name: str
    filename: str
    object_sha256: str
    size_bytes: int
    media_type: str
    row_count: int | None = None
    schema_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactDescriptor":
        return cls(
            name=str(value["name"]),
            filename=str(value["filename"]),
            object_sha256=str(value["object_sha256"]),
            size_bytes=int(value["size_bytes"]),
            media_type=str(value["media_type"]),
            row_count=None if value.get("row_count") is None else int(value["row_count"]),
            schema_sha256=value.get("schema_sha256"),
        )


@dataclass(frozen=True)
class SourceDependency:
    source_id: str
    source_version: str
    role: SourceRole
    license_class: LicenseClass
    object_sha256: str
    observed_at: str
    url: str
    dataset: str
    as_of_date: str | None = None
    published_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        observed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if not self.url:
            raise ValueError("source dependency url is required")
        if len(self.object_sha256) != 64:
            raise ValueError("source dependency object_sha256 must be SHA-256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "role": self.role.value,
            "license_class": self.license_class.value,
            "object_sha256": self.object_sha256,
            "observed_at": self.observed_at,
            "url": self.url,
            "dataset": self.dataset,
            "as_of_date": self.as_of_date,
            "published_at": self.published_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceDependency":
        return cls(
            source_id=str(value["source_id"]),
            source_version=str(value["source_version"]),
            role=SourceRole(str(value["role"])),
            license_class=LicenseClass(str(value["license_class"])),
            object_sha256=str(value["object_sha256"]),
            observed_at=str(value["observed_at"]),
            url=str(value["url"]),
            dataset=str(value["dataset"]),
            as_of_date=value.get("as_of_date"),
            published_at=value.get("published_at"),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: QualitySeverity
    dataset: str
    message: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "dataset": self.dataset,
            "message": self.message,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QualityIssue":
        return cls(
            code=str(value["code"]),
            severity=QualitySeverity(str(value["severity"])),
            dataset=str(value["dataset"]),
            message=str(value["message"]),
            evidence=dict(value.get("evidence") or {}),
        )


@dataclass(frozen=True)
class QualityReport:
    policy_version: str
    status: ReleaseStatus
    includes_delisted: bool
    issues: tuple[QualityIssue, ...]
    metrics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def hard_failures(self) -> tuple[QualityIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity in {QualitySeverity.CRITICAL, QualitySeverity.HIGH}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "status": self.status.value,
            "includes_delisted": self.includes_delisted,
            "issues": [issue.to_dict() for issue in self.issues],
            "metrics": dict(self.metrics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QualityReport":
        return cls(
            policy_version=str(value["policy_version"]),
            status=ReleaseStatus(str(value["status"])),
            includes_delisted=bool(value.get("includes_delisted", False)),
            issues=tuple(QualityIssue.from_dict(item) for item in value.get("issues", [])),
            metrics=dict(value.get("metrics") or {}),
        )


@dataclass(frozen=True)
class ReleaseManifest:
    universe_id: str
    created_at: str
    status: ReleaseStatus
    artifacts: Mapping[str, ArtifactDescriptor]
    sources: tuple[SourceDependency, ...]
    quality_policy_version: str = QUALITY_POLICY_VERSION
    format_version: str = MANIFEST_FORMAT_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def identity_payload(self) -> dict[str, Any]:
        """Identity excludes wall-clock creation time but includes every dependency."""

        return {
            "format_version": self.format_version,
            "universe_id": self.universe_id,
            "status": self.status.value,
            "quality_policy_version": self.quality_policy_version,
            "artifacts": {
                key: self.artifacts[key].to_dict() for key in sorted(self.artifacts)
            },
            "sources": [
                source.to_dict()
                for source in sorted(
                    self.sources,
                    key=lambda item: (
                        item.source_id,
                        item.dataset,
                        item.as_of_date or "",
                        item.object_sha256,
                    ),
                )
            ],
            "metadata": dict(self.metadata),
        }

    @property
    def release_id(self) -> str:
        return sha256_json(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "release_id": self.release_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReleaseManifest":
        manifest = cls(
            universe_id=str(value["universe_id"]),
            created_at=str(value["created_at"]),
            status=ReleaseStatus(str(value["status"])),
            artifacts={
                str(key): ArtifactDescriptor.from_dict(item)
                for key, item in dict(value.get("artifacts") or {}).items()
            },
            sources=tuple(SourceDependency.from_dict(item) for item in value.get("sources", [])),
            quality_policy_version=str(value.get("quality_policy_version", QUALITY_POLICY_VERSION)),
            format_version=str(value.get("format_version", MANIFEST_FORMAT_VERSION)),
            metadata=dict(value.get("metadata") or {}),
        )
        supplied_id = value.get("release_id")
        if supplied_id and str(supplied_id) != manifest.release_id:
            raise ValueError("release manifest identity mismatch")
        return manifest


@dataclass(frozen=True)
class EvidenceReference:
    url: str
    authority: EvidenceAuthority
    content_sha256: str
    source_id: str

    def __post_init__(self) -> None:
        if not self.url or not self.source_id:
            raise ValueError("evidence URL and source_id are required")
        if len(self.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_sha256
        ):
            raise ValueError("evidence content_sha256 must be a lowercase SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "authority": self.authority.value,
            "content_sha256": self.content_sha256,
            "source_id": self.source_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceReference":
        return cls(
            url=str(value["url"]),
            authority=EvidenceAuthority(str(value["authority"])),
            content_sha256=str(value["content_sha256"]),
            source_id=str(value["source_id"]),
        )


@dataclass(frozen=True)
class OverrideProposal:
    override_id: str
    dataset: str
    record_key: Mapping[str, Any]
    before: Mapping[str, Any] | None
    after: Mapping[str, Any]
    reason: str
    evidence: tuple[EvidenceReference, ...]
    proposed_at: str
    proposed_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "override_id": self.override_id,
            "dataset": self.dataset,
            "record_key": dict(self.record_key),
            "before": None if self.before is None else dict(self.before),
            "after": dict(self.after),
            "reason": self.reason,
            "evidence": [item.to_dict() for item in self.evidence],
            "proposed_at": self.proposed_at,
            "proposed_by": self.proposed_by,
        }

    @property
    def draft_sha256(self) -> str:
        return sha256_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OverrideProposal":
        return cls(
            override_id=str(value["override_id"]),
            dataset=str(value["dataset"]),
            record_key=dict(value["record_key"]),
            before=None if value.get("before") is None else dict(value["before"]),
            after=dict(value["after"]),
            reason=str(value["reason"]),
            evidence=tuple(EvidenceReference.from_dict(item) for item in value["evidence"]),
            proposed_at=str(value["proposed_at"]),
            proposed_by=str(value["proposed_by"]),
        )


@dataclass(frozen=True)
class OverrideApproval:
    override_id: str
    draft_sha256: str
    approved_at: str
    approved_by: str
    acknowledgement: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "ArtifactDescriptor",
    "EvidenceAuthority",
    "EvidenceReference",
    "LicenseClass",
    "MANIFEST_FORMAT_VERSION",
    "ObjectRef",
    "OverrideApproval",
    "OverrideProposal",
    "QUALITY_POLICY_VERSION",
    "QUALITY_CONTRACT_REVISION",
    "QualityIssue",
    "QualityReport",
    "QualitySeverity",
    "ReleaseManifest",
    "ReleaseStatus",
    "SourceDependency",
    "SourceRole",
    "UNIVERSE_ID",
]
