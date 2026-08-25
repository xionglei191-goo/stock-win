from __future__ import annotations

import unittest

from research_platform.us_promotion import (
    EXPECTED_NEIGHBORHOODS,
    HistoricalPromotionEvidence,
    NeighborhoodResult,
    PromotionMetrics,
    evaluate_historical_promotion,
)


def _metrics(
    cagr: float,
    *,
    total_return: float | None = None,
    sharpe: float = 0.0,
    drawdown: float = 0.10,
    trades: int = 0,
    issuers: int = 0,
    without_top: float | None = None,
) -> PromotionMetrics:
    return PromotionMetrics(
        cagr=cagr,
        total_return=cagr if total_return is None else total_return,
        excess_sharpe=sharpe,
        max_drawdown=drawdown,
        closed_trades=trades,
        issuers=issuers,
        cagr_without_top_issuer=without_top,
    )


def _passing_evidence() -> HistoricalPromotionEvidence:
    neighborhoods = tuple(
        NeighborhoodResult(item, cagr=0.10, max_drawdown=0.18)
        for item in sorted(EXPECTED_NEIGHBORHOODS)
    )
    return HistoricalPromotionEvidence(
        oos_strategy=_metrics(
            0.12,
            sharpe=0.80,
            drawdown=0.15,
            trades=35,
            issuers=14,
            without_top=0.07,
        ),
        oos_spy=_metrics(0.13, sharpe=0.85, drawdown=0.17),
        oos_bil=_metrics(0.04),
        sealed_strategy=_metrics(0.10, total_return=0.10, drawdown=0.12),
        sealed_spy=_metrics(0.12, total_return=0.12, drawdown=0.14),
        sealed_bil=_metrics(0.04, total_return=0.04),
        double_cost_strategy=_metrics(0.08, sharpe=0.50, drawdown=0.20),
        double_cost_bil=_metrics(0.04),
        neighborhoods=neighborhoods,
        neighborhood_base_cagr=0.12,
        neighborhood_bil_cagr=0.04,
        development_months=36,
        validation_months=12,
        sealed_months=12,
        bearish_regime_months=3,
        data_ready=True,
        release_hash_verified=True,
        frozen_before_sealed=True,
        sealed_run_count=1,
    )


class USHistoricalPromotionTests(unittest.TestCase):
    def test_all_locked_gates_promote_only_to_backtest_qualified(self) -> None:
        decision = evaluate_historical_promotion(_passing_evidence())

        self.assertTrue(decision.qualified)
        self.assertEqual(decision.status, "BACKTEST_QUALIFIED")
        self.assertFalse(decision.failures)

    def test_failed_release_and_reopened_holdout_fail_closed(self) -> None:
        source = _passing_evidence()
        evidence = HistoricalPromotionEvidence(
            **{
                **source.__dict__,
                "data_ready": False,
                "sealed_run_count": 2,
            }
        )

        decision = evaluate_historical_promotion(evidence)

        self.assertFalse(decision.qualified)
        self.assertEqual(decision.status, "HISTORICAL_FAILED")
        self.assertIn("data_ready", decision.failures)
        self.assertIn("sealed_protocol", decision.failures)

    def test_neighborhood_set_is_exact_and_not_a_parameter_search(self) -> None:
        source = _passing_evidence()
        evidence = HistoricalPromotionEvidence(
            **{
                **source.__dict__,
                "neighborhoods": source.neighborhoods[:-1],
            }
        )

        decision = evaluate_historical_promotion(evidence)

        self.assertFalse(decision.gates["neighborhood_set"])
        self.assertFalse(decision.qualified)


if __name__ == "__main__":
    unittest.main()
