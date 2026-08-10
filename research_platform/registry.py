from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import DataRequirement


@dataclass(frozen=True)
class SourceDefinition:
    dataset: str
    provider: str
    cacheable: bool
    description: str
    available: bool = True


class SourceRegistry:
    """Explicit authoritative-source mapping; field-level silent fallback is forbidden."""

    def __init__(self) -> None:
        self._sources = {
            "symbols": SourceDefinition("symbols", "tdx", True, "Security master and names"),
            "bars": SourceDefinition("bars", "tdx", True, "Daily and intraday OHLCV"),
            "sectors": SourceDefinition("sectors", "tdx", True, "Industry and concept membership"),
            "limit_events": SourceDefinition("limit_events", "tdx", True, "Limit-up and limit-down events"),
            "dragon_tiger": SourceDefinition(
                "dragon_tiger", "tdx", True, "Point-in-time Dragon-Tiger List capital flows"
            ),
            "limit_behavior": SourceDefinition(
                "limit_behavior", "tdx", True, "Point-in-time limit-up, seal and auction behavior"
            ),
            "market_activity": SourceDefinition(
                "market_activity", "tdx", True, "Point-in-time whole-market limit and breadth ecology"
            ),
            "financials": SourceDefinition("financials", "tdx", True, "Point-in-time financial statements"),
            "macro": SourceDefinition(
                "macro", "external", True, "Reserved external macro provider", False
            ),
            "news": SourceDefinition(
                "news", "external", True, "Reserved external news provider", False
            ),
        }

    def register(self, definition: SourceDefinition, *, replace: bool = False) -> None:
        if definition.dataset in self._sources and not replace:
            raise ValueError(f"Dataset source is already registered: {definition.dataset}")
        self._sources[definition.dataset] = definition

    def resolve(self, dataset: str) -> SourceDefinition:
        if dataset not in self._sources:
            raise KeyError(f"No authoritative source registered for dataset: {dataset}")
        return self._sources[dataset]

    def as_records(self) -> list[dict[str, object]]:
        return [definition.__dict__.copy() for definition in self._sources.values()]

    def validate_requirements(self, requirements: Iterable["DataRequirement"]) -> None:
        missing: list[str] = []
        unavailable: list[str] = []
        for requirement in requirements:
            if not requirement.required:
                continue
            try:
                source = self.resolve(requirement.dataset)
            except KeyError:
                missing.append(requirement.dataset)
                continue
            if not source.available:
                unavailable.append(requirement.dataset)
        if missing:
            raise ValueError(
                f"No authoritative source registered for required datasets: {', '.join(sorted(set(missing)))}"
            )
        if unavailable:
            raise ValueError(
                f"Required data providers are not available: {', '.join(sorted(set(unavailable)))}"
            )
