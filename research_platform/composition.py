from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping

from .models import (
    ExecutionModel,
    PlatformSignal,
    RuntimeAdapter,
    SignalStatus,
    StrategyCategory,
    StrategyMetadata,
    StrategyScanResult,
)


class CompositionMode(StrEnum):
    CAPITAL_SLEEVES = "capital_sleeves"
    SCORE_FUSION = "score_fusion"
    INTERSECTION = "intersection"
    RISK_OVERLAY = "risk_overlay"
    COMPARISON = "comparison"


class ConflictPolicy(StrEnum):
    RISK_FIRST = "risk_first"
    NET_SCORE = "net_score"
    PRIORITY = "priority"


@dataclass(frozen=True)
class StrategyGroupMember:
    strategy_id: str
    weight: float
    role: str = "alpha"
    priority: int = 100


@dataclass(frozen=True)
class StrategyGroupDefinition:
    group_id: str
    version: str
    name: str
    description: str
    composition_mode: CompositionMode
    conflict_policy: ConflictPolicy
    members: tuple[StrategyGroupMember, ...]
    enabled: bool = True
    built_in: bool = False

    def validate(self, strategies: Iterable[str] | Mapping[str, object]) -> None:
        available = set(strategies)
        if not self.group_id or not self.members:
            raise ValueError("Strategy group requires an id and at least one member")
        if self.group_id in available:
            raise ValueError("Strategy group id must not conflict with a strategy id")
        member_ids = [member.strategy_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("Strategy group members must be unique")
        unknown = sorted(set(member_ids) - available)
        if unknown:
            raise ValueError(f"Unknown strategy group members: {', '.join(unknown)}")
        if any(member.weight <= 0 for member in self.members):
            raise ValueError("Strategy group weights must be positive")
        if self.composition_mode == CompositionMode.CAPITAL_SLEEVES:
            total = sum(member.weight for member in self.members)
            if abs(total - 1.0) > 1e-6:
                raise ValueError("Capital-sleeve weights must sum to 1")
        alpha_count = sum(member.role == "alpha" for member in self.members)
        risk_count = sum(member.role == "risk" for member in self.members)
        if any(member.role not in {"alpha", "risk"} for member in self.members):
            raise ValueError("Strategy group roles must be 'alpha' or 'risk'")
        if self.composition_mode in {CompositionMode.SCORE_FUSION, CompositionMode.INTERSECTION}:
            if alpha_count < 2 or risk_count:
                raise ValueError("Signal fusion and intersection require at least two alpha members")
        if self.composition_mode == CompositionMode.RISK_OVERLAY:
            if alpha_count < 1 or risk_count < 1:
                raise ValueError("Risk overlay requires at least one alpha and one risk member")
        if isinstance(strategies, Mapping) and self.composition_mode not in {
            CompositionMode.CAPITAL_SLEEVES,
            CompositionMode.COMPARISON,
        }:
            metadata = [strategies[item].metadata for item in member_ids]  # type: ignore[attr-defined]
            if any(item.execution_model == ExecutionModel.MULTI_LEG for item in metadata):
                raise ValueError("Multi-leg strategies can only use capital_sleeves or comparison")
            shared_assets = set(metadata[0].asset_classes)
            for item in metadata[1:]:
                shared_assets &= set(item.asset_classes)
            if not shared_assets:
                raise ValueError("Signal-level composition requires a shared asset class")


def built_in_groups() -> tuple[StrategyGroupDefinition, ...]:
    return (
        StrategyGroupDefinition(
            group_id="early_winner_v1_compare",
            version="1.0.0",
            name="早期强势股规则 / ML 对照",
            description="在同一不可变研究快照上并列验证规则榜和模型榜，不做信号融合。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("early_winner_rule_v1", 0.50, priority=10),
                StrategyGroupMember("early_winner_ml_v1", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="combined",
            version="3.1.0",
            name="缠论 / 49课历史比较组合",
            description="保留缠论与49课体系各50%资金分舱的历史复现；因缠论被审计否决，不支持扫描。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("chan_v1", 0.50, priority=10),
                StrategyGroupMember("course49_system", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_system_compare",
            version="1.0.0",
            name="49课 V2 / 体系影子对比",
            description="在同一不可变数据快照上比较归档V2与正式体系，验证信号和执行等价性。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v2", 0.50, priority=10),
                StrategyGroupMember("course49_system", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_compare",
            version="2.0.0",
            name="49课 V1 / V2 对比",
            description="同一数据快照下并列运行V1和V2，不进行信号融合。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v1", 0.50, priority=10),
                StrategyGroupMember("course49_v2", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_v3_compare",
            version="1.0.0",
            name="49课 V2 / V3 对比",
            description="同一数据快照并列运行V2和局部加速V3，用于验证风格硬闸门的改造效果。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v2", 0.50, priority=10),
                StrategyGroupMember("course49_v3", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_v4_compare",
            version="1.0.0",
            name="49课 V3 / V4 对比",
            description="同一不可变快照并列运行局部加速V3与资金确认V4。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v3", 0.50, priority=10),
                StrategyGroupMember("course49_v4", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_v5_compare",
            version="1.0.0",
            name="49课 V4 / V5 风险预算对比",
            description="同一信号与快照下对比22%和25%基础风险预算。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v4", 0.50, priority=10),
                StrategyGroupMember("course49_v5", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_v6_compare",
            version="1.0.0",
            name="49课 V5 / V6 奖励切换对比",
            description="同一快照比较高标资金确认与小盘加速首板启动。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v5", 0.50, priority=10),
                StrategyGroupMember("course49_v6", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_v9_compare",
            version="1.0.0",
            name="49课 V6 / V9 失效边界对比",
            description="同一快照比较两个已被稳健性门禁否决的阶段性奖励模式。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v6", 0.50, priority=10),
                StrategyGroupMember("course49_v9", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_v10_compare",
            version="1.0.0",
            name="49课 V9 / V10 市场奖励过滤对比",
            description="同一不可变快照比较V9原始形态与V10三日市场奖励动量过滤。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v9", 0.50, priority=10),
                StrategyGroupMember("course49_v10", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="course49_v11_compare",
            version="1.0.0",
            name="49课 V9 / V11 回封强度对比",
            description="同一不可变快照比较V9两次开板门槛与V11三次开板门槛。",
            composition_mode=CompositionMode.COMPARISON,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_v9", 0.50, priority=10),
                StrategyGroupMember("course49_v11", 0.50, priority=20),
            ),
            built_in=True,
        ),
        StrategyGroupDefinition(
            group_id="adaptive_multi_strategy",
            version="2.1.0",
            name="多策略历史比较组合",
            description="保留49课、缠论与已否决配对套利的资金分舱历史回测；不支持扫描。",
            composition_mode=CompositionMode.CAPITAL_SLEEVES,
            conflict_policy=ConflictPolicy.RISK_FIRST,
            members=(
                StrategyGroupMember("course49_system", 0.35, priority=10),
                StrategyGroupMember("chan_v1", 0.25, priority=20),
                StrategyGroupMember("pairs_arbitrage_v1", 0.40, priority=30),
            ),
            built_in=True,
        ),
    )


class StrategyCatalog:
    def __init__(
        self,
        strategies: dict[str, object],
        groups: Iterable[StrategyGroupDefinition] = (),
    ) -> None:
        self.strategies = dict(strategies)
        self.groups: dict[str, StrategyGroupDefinition] = {}
        self.group_issues: list[dict[str, str]] = []
        for group in built_in_groups():
            try:
                group.validate(self.strategies)
            except ValueError as exc:
                if "Unknown strategy group members" in str(exc):
                    continue
                raise
            self.groups[group.group_id] = group
        for group in groups:
            try:
                group.validate(self.strategies)
            except ValueError as exc:
                self.group_issues.append(
                    {
                        "plugin_id": group.group_id,
                        "origin": "strategy_groups",
                        "code": "INVALID_GROUP",
                        "message": str(exc),
                    }
                )
                continue
            self.groups[group.group_id] = group

    def resolve(self, item_id: str) -> tuple[StrategyGroupDefinition | None, tuple[str, ...]]:
        if item_id in self.strategies:
            return None, (item_id,)
        group = self.groups.get(item_id)
        if group is None or not group.enabled:
            raise KeyError(item_id)
        return group, tuple(member.strategy_id for member in group.members)

    def metadata(self, item_id: str) -> StrategyMetadata:
        if item_id in self.strategies:
            return self.strategies[item_id].metadata  # type: ignore[attr-defined]
        group = self.groups[item_id]
        members = [self.strategies[item.strategy_id].metadata for item in group.members]  # type: ignore[attr-defined]
        requirements = tuple(dict.fromkeys(req for item in members for req in item.data_requirements))
        return StrategyMetadata(
            strategy_id=group.group_id,
            version=group.version,
            name=group.name,
            description=group.description,
            frequency="multi",
            requires_approval=any(item.requires_approval for item in members),
            enabled=group.enabled,
            asset_classes=tuple(sorted({asset for item in members for asset in item.asset_classes})),
            execution_model=(
                ExecutionModel.MULTI_LEG
                if any(item.execution_model == ExecutionModel.MULTI_LEG for item in members)
                else ExecutionModel.SINGLE_LEG
            ),
            supports_short=any(item.supports_short for item in members),
            data_requirements=requirements,
        )

    def capital_weights(self, item_id: str) -> dict[str, float]:
        group, members = self.resolve(item_id)
        if group is None:
            return {members[0]: 0.50}
        if group.composition_mode in {CompositionMode.CAPITAL_SLEEVES, CompositionMode.COMPARISON}:
            total = sum(member.weight for member in group.members)
            return {member.strategy_id: member.weight / total for member in group.members}
        total = sum(member.weight for member in group.members)
        return {member.strategy_id: member.weight / total for member in group.members}

    def as_records(self) -> dict[str, list[dict[str, object]]]:
        strategies = []
        archived_strategies = []
        for strategy_id in sorted(self.strategies):
            metadata = self.metadata(strategy_id)
            record = {
                    "strategy_id": strategy_id,
                    "version": metadata.version,
                    "name": metadata.name,
                    "description": metadata.description,
                    "frequency": metadata.frequency,
                    "requires_approval": metadata.requires_approval,
                    "enabled": metadata.enabled,
                    "asset_classes": list(metadata.asset_classes),
                    "execution_model": metadata.execution_model.value,
                    "supports_short": metadata.supports_short,
                    "strategy_family": metadata.strategy_family or metadata.strategy_id,
                    "lifecycle": metadata.lifecycle,
                    "scan_enabled": metadata.scan_enabled,
                    "backtest_enabled": metadata.backtest_enabled,
                    "runtime_adapter": RuntimeAdapter(metadata.runtime_adapter).value,
                    "plugin_api_version": metadata.plugin_api_version,
                    "plugin_origin": getattr(self.strategies[strategy_id], "__plugin_origin__", "builtin"),
                    "framework_id": metadata.framework_id,
                    "policy_version": metadata.policy_version,
                    "archived": metadata.archived,
                    "category": StrategyCategory(metadata.category).value,
                    "data_requirements": [requirement.__dict__ for requirement in metadata.data_requirements],
                }
            (archived_strategies if metadata.archived else strategies).append(record)
        groups = []
        for group_id in sorted(self.groups):
            group = self.groups[group_id]
            member_metadata = [
                self.strategies[member.strategy_id].metadata  # type: ignore[attr-defined]
                for member in group.members
            ]
            groups.append(
                {
                    "group_id": group.group_id,
                    "version": group.version,
                    "name": group.name,
                    "description": group.description,
                    "composition_mode": group.composition_mode.value,
                    "conflict_policy": group.conflict_policy.value,
                    "enabled": group.enabled,
                    "built_in": group.built_in,
                    "scan_supported": all(item.scan_enabled for item in member_metadata),
                    "backtest_supported": all(item.backtest_enabled for item in member_metadata)
                    and group.composition_mode in {
                        CompositionMode.CAPITAL_SLEEVES,
                        CompositionMode.COMPARISON,
                    },
                    "members": [member.__dict__ for member in group.members],
                    "category": (
                        "framework"
                        if group.group_id == "course49_system_compare"
                        else "research_archive"
                        if group.group_id in {"combined", "adaptive_multi_strategy"}
                        or any(item.archived for item in member_metadata)
                        else "research_project"
                        if all(
                            StrategyCategory(item.category)
                            == StrategyCategory.RESEARCH_PROJECT
                            for item in member_metadata
                        )
                        else "independent"
                    ),
                    "scan_block_reason": (
                        ""
                        if all(item.scan_enabled for item in member_metadata)
                        else "包含不可扫描的策略"
                    ),
                    "backtest_block_reason": (
                        ""
                        if all(item.backtest_enabled for item in member_metadata)
                        and group.composition_mode in {
                            CompositionMode.CAPITAL_SLEEVES,
                            CompositionMode.COMPARISON,
                        }
                        else (
                            "该信号级组合暂只支持扫描"
                            if group.composition_mode not in {
                                CompositionMode.CAPITAL_SLEEVES,
                                CompositionMode.COMPARISON,
                            }
                            else "包含不可回测的策略"
                        )
                    ),
                }
            )
        return {
            "strategies": strategies,
            "archived_strategies": archived_strategies,
            "groups": groups,
        }


class CompositionEngine:
    def compose(
        self,
        group: StrategyGroupDefinition,
        results: list[StrategyScanResult],
        run_id: str,
    ) -> list[StrategyScanResult]:
        if group.composition_mode in {CompositionMode.CAPITAL_SLEEVES, CompositionMode.COMPARISON}:
            return results
        if any(result.order_groups for result in results):
            raise ValueError("Multi-leg strategies can only be combined with capital_sleeves")

        result_by_id = {result.strategy.strategy_id: result for result in results}
        members = [member for member in group.members if member.strategy_id in result_by_id]
        signals = [signal for result in results for signal in result.signals]
        if group.composition_mode == CompositionMode.SCORE_FUSION:
            output = self._score_fusion(group, members, signals, run_id)
        elif group.composition_mode == CompositionMode.INTERSECTION:
            output = self._intersection(group, members, signals, run_id)
        else:
            output = self._risk_overlay(group, members, signals, run_id)
        metadata = StrategyMetadata(
            group.group_id,
            group.version,
            group.name,
            group.description,
            "composite",
            True,
        )
        return [
            StrategyScanResult(
                strategy=metadata,
                signals=tuple(output),
                candidates=tuple(item for result in results for item in result.candidates),
                state={
                    "composition_mode": group.composition_mode.value,
                    "conflict_policy": group.conflict_policy.value,
                    "components": {result.strategy.strategy_id: result.state for result in results},
                },
            )
        ]

    def _score_fusion(
        self,
        group: StrategyGroupDefinition,
        members: list[StrategyGroupMember],
        signals: list[PlatformSignal],
        run_id: str,
    ) -> list[PlatformSignal]:
        weights = {member.strategy_id: member.weight for member in members}
        grouped: dict[str, list[PlatformSignal]] = {}
        for signal in signals:
            grouped.setdefault(signal.code, []).append(signal)
        output = []
        for code, items in grouped.items():
            sell_items = [item for item in items if item.side == "SELL"]
            if sell_items and group.conflict_policy == ConflictPolicy.RISK_FIRST:
                output.append(_composite_signal(group, run_id, sell_items, "SELL", 1.0, 0.0))
                continue
            if group.conflict_policy == ConflictPolicy.PRIORITY:
                priorities = {member.strategy_id: member.priority for member in members}
                best_priority = min(priorities.get(item.strategy_id, 10_000) for item in items)
                selected = [
                    item
                    for item in items
                    if priorities.get(item.strategy_id, 10_000) == best_priority
                ]
                anchor = max(selected, key=lambda item: (item.strength, item.generated_at))
                same_side = [item for item in selected if item.side == anchor.side]
                output.append(
                    _composite_signal(
                        group,
                        run_id,
                        same_side,
                        anchor.side,
                        max(item.strength for item in same_side),
                        sum(
                            weights.get(item.strategy_id, 0.0) * item.target_weight
                            for item in same_side
                        ),
                    )
                )
                continue
            signed = sum(weights.get(item.strategy_id, 0.0) * item.strength * (1 if item.side == "BUY" else -1) for item in items)
            if abs(signed) < 1e-9:
                continue
            side = "BUY" if signed > 0 else "SELL"
            selected = [item for item in items if item.side == side]
            target = sum(weights.get(item.strategy_id, 0.0) * item.target_weight for item in selected)
            output.append(_composite_signal(group, run_id, selected, side, min(1.0, abs(signed)), target))
        return output

    def _intersection(
        self,
        group: StrategyGroupDefinition,
        members: list[StrategyGroupMember],
        signals: list[PlatformSignal],
        run_id: str,
    ) -> list[PlatformSignal]:
        alpha_ids = {member.strategy_id for member in members if member.role == "alpha"}
        grouped: dict[tuple[str, str], list[PlatformSignal]] = {}
        for signal in signals:
            grouped.setdefault((signal.code, signal.side), []).append(signal)
        output = []
        for (_, side), items in grouped.items():
            if {item.strategy_id for item in items} >= alpha_ids:
                output.append(
                    _composite_signal(
                        group,
                        run_id,
                        items,
                        side,
                        min(item.strength for item in items),
                        min(item.target_weight for item in items),
                    )
                )
        return output

    def _risk_overlay(
        self,
        group: StrategyGroupDefinition,
        members: list[StrategyGroupMember],
        signals: list[PlatformSignal],
        run_id: str,
    ) -> list[PlatformSignal]:
        overlay_ids = {member.strategy_id for member in members if member.role == "risk"}
        blocked = {signal.code for signal in signals if signal.strategy_id in overlay_ids and signal.side == "SELL"}
        output = []
        for signal in signals:
            if signal.strategy_id in overlay_ids or (signal.side == "BUY" and signal.code in blocked):
                continue
            output.append(_composite_signal(group, run_id, [signal], signal.side, signal.strength, signal.target_weight))
        return output


def _composite_signal(
    group: StrategyGroupDefinition,
    run_id: str,
    signals: list[PlatformSignal],
    side: str,
    strength: float,
    target_weight: float,
) -> PlatformSignal:
    anchor = max(signals, key=lambda item: item.generated_at)
    return PlatformSignal(
        run_id=run_id,
        strategy_id=group.group_id,
        strategy_version=group.version,
        generated_at=anchor.generated_at,
        available_at=max(item.available_at for item in signals),
        code=anchor.code,
        side=side,  # type: ignore[arg-type]
        strength=strength,
        target_weight=target_weight,
        horizon=anchor.horizon,
        valid_until=min(item.valid_until for item in signals),
        stop_price=anchor.stop_price if side == "BUY" else None,
        status=(
            SignalStatus.PROPOSED
            if any(item.status == SignalStatus.PROPOSED for item in signals)
            else SignalStatus.APPROVED
        ),
        reason_codes=("COMPOSITE", group.composition_mode.value, *tuple(dict.fromkeys(code for item in signals for code in item.reason_codes))),
        evidence={
            "composition_mode": group.composition_mode.value,
            "conflict_policy": group.conflict_policy.value,
            "components": [
                {"strategy_id": item.strategy_id, "signal_id": item.signal_id, "strength": item.strength}
                for item in signals
            ],
        },
    )
