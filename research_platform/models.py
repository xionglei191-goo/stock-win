from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4


class DataStatus(StrEnum):
    READY = "READY"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED_DATA = "BLOCKED_DATA"


class SignalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"


class ExecutionModel(StrEnum):
    SINGLE_LEG = "SINGLE_LEG"
    MULTI_LEG = "MULTI_LEG"


class RuntimeAdapter(StrEnum):
    CHAN_DAILY = "chan_daily"
    COURSE49_DAILY = "course49_daily"
    GENERIC_DAILY = "generic_daily"


class OrderGroupAction(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    REBALANCE = "REBALANCE"


@dataclass(frozen=True)
class DataHealth:
    dataset: str
    status: DataStatus
    latest_at: str | None
    expected_at: str | None
    row_count: int
    message: str = ""
    source: str = "tdx"


@dataclass(frozen=True)
class DataRequirement:
    dataset: str
    frequency: str = "1d"
    adjustment: str = "none"
    lookback: int = 0
    required: bool = True
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyMetadata:
    strategy_id: str
    version: str
    name: str
    description: str
    frequency: str
    requires_approval: bool
    enabled: bool = True
    asset_classes: tuple[str, ...] = ("A_STOCK",)
    execution_model: ExecutionModel = ExecutionModel.SINGLE_LEG
    supports_short: bool = False
    data_requirements: tuple[DataRequirement, ...] = ()
    strategy_family: str = ""
    lifecycle: str = "BUILT_IN"
    scan_enabled: bool = True
    backtest_enabled: bool = True
    runtime_adapter: RuntimeAdapter = RuntimeAdapter.GENERIC_DAILY
    plugin_api_version: str = "1"


@dataclass(frozen=True)
class PlatformSignal:
    run_id: str
    strategy_id: str
    strategy_version: str
    generated_at: datetime
    available_at: datetime
    code: str
    side: Literal["BUY", "SELL"]
    strength: float
    target_weight: float
    horizon: str
    valid_until: datetime
    stop_price: float | None
    status: SignalStatus
    reason_codes: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
    signal_id: str = field(default_factory=lambda: uuid4().hex)

    def as_record(self) -> dict[str, Any]:
        record = asdict(self)
        for key in ("generated_at", "available_at", "valid_until"):
            record[key] = record[key].isoformat()
        record["status"] = self.status.value
        record["reason_codes"] = json.dumps(self.reason_codes, ensure_ascii=False)
        record["evidence"] = json.dumps(self.evidence, ensure_ascii=False)
        return record


@dataclass(frozen=True)
class OrderLegIntent:
    code: str
    side: Literal["BUY", "SELL", "SHORT", "COVER"]
    ratio: float
    target_weight: float
    leg_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class OrderGroupIntent:
    run_id: str
    strategy_id: str
    strategy_version: str
    generated_at: datetime
    available_at: datetime
    valid_until: datetime
    group_key: str
    action: OrderGroupAction
    strength: float
    gross_target_weight: float
    status: SignalStatus
    reason_codes: tuple[str, ...]
    legs: tuple[OrderLegIntent, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
    intent_id: str = field(default_factory=lambda: uuid4().hex)

    def as_record(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "generated_at": self.generated_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "group_key": self.group_key,
            "action": self.action.value,
            "strength": self.strength,
            "gross_target_weight": self.gross_target_weight,
            "status": self.status.value,
            "reason_codes": json.dumps(self.reason_codes, ensure_ascii=False),
            "evidence": json.dumps(self.evidence, ensure_ascii=False),
        }

    def leg_records(self) -> list[dict[str, Any]]:
        return [
            {
                "leg_id": leg.leg_id,
                "intent_id": self.intent_id,
                "code": leg.code,
                "side": leg.side,
                "ratio": leg.ratio,
                "target_weight": leg.target_weight,
            }
            for leg in self.legs
        ]


@dataclass(frozen=True)
class StrategyScanResult:
    strategy: StrategyMetadata
    signals: tuple[PlatformSignal, ...]
    candidates: tuple[dict[str, Any], ...]
    state: dict[str, Any]
    order_groups: tuple[OrderGroupIntent, ...] = ()


@dataclass(frozen=True)
class ScanReport:
    run_id: str
    status: RunStatus
    started_at: str
    finished_at: str
    data_health: tuple[DataHealth, ...]
    strategy_results: tuple[StrategyScanResult, ...]
    error: str = ""
