from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .models import DataRequirement, RuntimeAdapter, StrategyMetadata


DEFAULT_BAR_FIELDS = ("Open", "High", "Low", "Close", "Volume", "Amount")


@dataclass(frozen=True)
class DataPlan:
    front_fields: tuple[str, ...]
    raw_fields: tuple[str, ...]
    require_market_index: bool
    require_sectors: bool
    require_style_benchmarks: bool
    require_course49_events: bool
    require_market_activity: bool
    event_minimum_streak: int

    @property
    def datasets(self) -> tuple[str, ...]:
        values = ["security_master", "market_index"]
        if self.front_fields:
            values.append("daily_front")
        if self.raw_fields:
            values.append("daily_raw")
        if self.require_sectors:
            values.append("sector_membership")
        if self.require_style_benchmarks:
            values.append("style_benchmarks")
        if self.require_course49_events:
            values.extend(("dragon_tiger", "limit_behavior"))
        if self.require_market_activity:
            values.append("market_activity")
        return tuple(values)

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "datasets": list(self.datasets)}


def build_data_plan(
    metadata: Iterable[StrategyMetadata],
    *,
    event_minimum_streak: int = 1,
) -> DataPlan:
    strategies = tuple(metadata)
    requirements: tuple[DataRequirement, ...] = tuple(
        requirement
        for strategy in strategies
        for requirement in strategy.data_requirements
    )
    adapters = {RuntimeAdapter(strategy.runtime_adapter) for strategy in strategies}
    front_fields = _bar_fields(requirements, "front")
    raw_fields = _bar_fields(requirements, "none")

    # The execution engine always needs an unadjusted open/close and a stable
    # adjusted series even when an older plugin omitted one side of the contract.
    if not front_fields:
        front_fields = DEFAULT_BAR_FIELDS
    if not raw_fields:
        raw_fields = DEFAULT_BAR_FIELDS

    course49 = RuntimeAdapter.COURSE49_DAILY in adapters
    chan = RuntimeAdapter.CHAN_DAILY in adapters
    datasets = {requirement.dataset for requirement in requirements}
    adaptive_course49 = any(
        strategy.runtime_adapter == RuntimeAdapter.COURSE49_DAILY
        and strategy.strategy_family != "course49_v1"
        for strategy in strategies
    )
    return DataPlan(
        front_fields=front_fields,
        raw_fields=raw_fields,
        require_market_index=True,
        require_sectors=chan or course49 or "sectors" in datasets,
        require_style_benchmarks=adaptive_course49,
        require_course49_events=course49,
        require_market_activity=adaptive_course49 or "market_activity" in datasets,
        event_minimum_streak=max(1, int(event_minimum_streak)),
    )


def required_bar_lookback(
    metadata: Iterable[StrategyMetadata],
    *,
    minimum: int = 0,
) -> int:
    return max(
        [minimum]
        + [
            int(requirement.lookback)
            for strategy in metadata
            for requirement in strategy.data_requirements
            if requirement.dataset == "bars"
        ]
    )


def _bar_fields(
    requirements: tuple[DataRequirement, ...],
    adjustment: str,
) -> tuple[str, ...]:
    fields = {
        field
        for requirement in requirements
        if requirement.dataset == "bars" and requirement.adjustment == adjustment
        for field in (requirement.fields or DEFAULT_BAR_FIELDS)
    }
    order = {field: index for index, field in enumerate(DEFAULT_BAR_FIELDS)}
    return tuple(sorted(fields, key=lambda field: (order.get(field, 999), field)))
