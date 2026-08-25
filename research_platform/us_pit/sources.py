from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from .models import LicenseClass, SourceRole, UNIVERSE_ID


@dataclass(frozen=True)
class SyncRequest:
    start_date: date
    end_date: date
    observed_at: datetime
    universe_id: str = UNIVERSE_ID

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True)
class SourceArtifact:
    dataset: str
    payload: bytes
    media_type: str
    url: str
    observed_at: datetime
    role: SourceRole
    license_class: LicenseClass
    as_of_date: date | None = None
    published_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dataset or not self.url:
            raise ValueError("source artifact dataset and url are required")
        if self.observed_at.tzinfo is None:
            raise ValueError("source artifact observed_at must be timezone-aware")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("source artifact published_at must be timezone-aware")


class SourceAdapter(ABC):
    """Pure ingestion boundary. Adapters return evidence; they never publish releases."""

    source_id: str
    source_version: str

    @abstractmethod
    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        raise NotImplementedError


class StaticSourceAdapter(SourceAdapter):
    """Deterministic adapter useful for local imports and tests."""

    def __init__(
        self,
        source_id: str,
        source_version: str,
        artifacts: Iterable[SourceArtifact],
    ) -> None:
        self.source_id = source_id
        self.source_version = source_version
        self._artifacts = tuple(artifacts)

    def fetch(self, request: SyncRequest) -> Iterable[SourceArtifact]:
        del request
        return self._artifacts


__all__ = ["SourceAdapter", "SourceArtifact", "StaticSourceAdapter", "SyncRequest"]
