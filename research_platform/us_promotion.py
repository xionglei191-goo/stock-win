"""Deterministic promotion gates for the US momentum strategy.

The evaluator deliberately consumes frozen metrics instead of reaching into a
backtest or data store.  That makes the promotion decision reproducible and
keeps opening a sealed holdout separate from calculating its result.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Mapping


@dataclass(frozen=True)
class PromotionMetrics:
    """Metrics for one frozen evaluation window.

    ``max_drawdown`` is the positive loss magnitude (``0.20`` means 20%).
    ``excess_sharpe`` is calculated against the frozen BIL return series.
    Fields that are not needed by a benchmark may retain their defaults.
    """

    cagr: float
    total_return: float
    excess_sharpe: float
    max_drawdown: float
    closed_trades: int = 0
    issuers: int = 0
    cagr_without_top_issuer: float | None = None

    def __post_init__(self) -> None:
        if self.max_drawdown < 0:
            raise ValueError("max_drawdown must be a positive loss magnitude")
        if self.closed_trades < 0 or self.issuers < 0:
            raise ValueError("trade and issuer counts cannot be negative")


@dataclass(frozen=True)
class NeighborhoodResult:
    """One pre-declared, one-parameter robustness variation."""

    variation_id: str
    cagr: float
    max_drawdown: float

    def __post_init__(self) -> None:
        if not self.variation_id.strip():
            raise ValueError("variation_id is required")
        if self.max_drawdown < 0:
            raise ValueError("max_drawdown must be a positive loss magnitude")


@dataclass(frozen=True)
class HistoricalPromotionEvidence:
    """Frozen evidence required to promote into paper collection."""

    oos_strategy: PromotionMetrics
    oos_spy: PromotionMetrics
    oos_bil: PromotionMetrics
    sealed_strategy: PromotionMetrics
    sealed_spy: PromotionMetrics
    sealed_bil: PromotionMetrics
    double_cost_strategy: PromotionMetrics
    double_cost_bil: PromotionMetrics
    neighborhoods: tuple[NeighborhoodResult, ...]
    neighborhood_base_cagr: float
    neighborhood_bil_cagr: float
    development_months: int
    validation_months: int
    sealed_months: int
    bearish_regime_months: int
    data_ready: bool
    release_hash_verified: bool
    frozen_before_sealed: bool
    sealed_run_count: int


@dataclass(frozen=True)
class PromotionDecision:
    qualified: bool
    status: str
    gates: Mapping[str, bool]
    failures: tuple[str, ...]


EXPECTED_NEIGHBORHOODS = frozenset(
    {
        "entry_20",
        "entry_30",
        "exit_35",
        "exit_45",
        "stop_06",
        "stop_10",
    }
)


def evaluate_historical_promotion(
    evidence: HistoricalPromotionEvidence,
) -> PromotionDecision:
    """Apply the locked historical gates without hidden discretion."""

    oos = evidence.oos_strategy
    spy = evidence.oos_spy
    bil = evidence.oos_bil
    sealed = evidence.sealed_strategy
    stress = evidence.double_cost_strategy
    neighborhood_ids = [item.variation_id for item in evidence.neighborhoods]
    neighborhood_shape_ok = (
        len(neighborhood_ids) == len(EXPECTED_NEIGHBORHOODS)
        and len(set(neighborhood_ids)) == len(neighborhood_ids)
        and set(neighborhood_ids) == EXPECTED_NEIGHBORHOODS
    )
    neighborhood_passes = sum(
        item.cagr > evidence.neighborhood_bil_cagr and item.max_drawdown <= 0.25
        for item in evidence.neighborhoods
    )
    neighborhood_median = (
        median(item.cagr for item in evidence.neighborhoods)
        if evidence.neighborhoods
        else float("-inf")
    )

    gates = {
        "data_ready": evidence.data_ready and evidence.release_hash_verified,
        "window_split": (
            evidence.development_months >= 36
            and evidence.validation_months >= 12
            and evidence.sealed_months >= 12
        ),
        "bearish_regime_coverage": evidence.bearish_regime_months >= 3,
        "sealed_protocol": (
            evidence.frozen_before_sealed and evidence.sealed_run_count == 1
        ),
        "oos_cagr_vs_bil": oos.cagr >= bil.cagr + 0.03,
        "oos_cagr_vs_spy": oos.cagr >= spy.cagr - 0.03,
        "oos_sharpe_absolute": oos.excess_sharpe >= 0.60,
        "oos_sharpe_vs_spy": oos.excess_sharpe >= spy.excess_sharpe - 0.10,
        "oos_drawdown_absolute": oos.max_drawdown <= 0.20,
        "oos_drawdown_vs_spy": oos.max_drawdown <= spy.max_drawdown,
        "oos_trade_breadth": oos.closed_trades >= 30 and oos.issuers >= 12,
        "top_issuer_removed": (
            oos.cagr_without_top_issuer is not None
            and oos.cagr_without_top_issuer > bil.cagr
        ),
        "sealed_return_vs_bil": sealed.total_return > evidence.sealed_bil.total_return,
        "sealed_return_vs_spy": (
            sealed.total_return >= evidence.sealed_spy.total_return - 0.05
        ),
        "sealed_drawdown": sealed.max_drawdown <= 0.20,
        "double_cost_cagr": stress.cagr > evidence.double_cost_bil.cagr,
        "double_cost_sharpe": stress.excess_sharpe >= 0.40,
        "double_cost_drawdown": stress.max_drawdown <= 0.25,
        "neighborhood_set": neighborhood_shape_ok,
        "neighborhood_pass_count": neighborhood_shape_ok and neighborhood_passes >= 4,
        "neighborhood_median": (
            neighborhood_shape_ok
            and neighborhood_median >= evidence.neighborhood_base_cagr - 0.05
        ),
    }
    failures = tuple(name for name, passed in gates.items() if not passed)
    qualified = not failures
    return PromotionDecision(
        qualified=qualified,
        status="BACKTEST_QUALIFIED" if qualified else "HISTORICAL_FAILED",
        gates=gates,
        failures=failures,
    )


__all__ = [
    "EXPECTED_NEIGHBORHOODS",
    "HistoricalPromotionEvidence",
    "NeighborhoodResult",
    "PromotionDecision",
    "PromotionMetrics",
    "evaluate_historical_promotion",
]
