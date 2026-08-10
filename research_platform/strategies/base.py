from __future__ import annotations

from typing import Any, Protocol

from research_platform.models import StrategyMetadata, StrategyScanResult


class Strategy(Protocol):
    metadata: StrategyMetadata

    def scan(self, **context: Any) -> StrategyScanResult: ...
