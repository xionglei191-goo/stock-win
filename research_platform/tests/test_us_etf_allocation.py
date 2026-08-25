from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.models import StrategyCategory
from research_platform.strategies import create_strategy_registry
from research_platform.strategies.qqq_treasury_rotation import (
    QQQTreasuryRotationStrategy,
)
from research_platform.strategies.qqq_vol_dca import QQQVolDCAStrategy


def _daily_bars(values: np.ndarray, end: str) -> pd.DataFrame:
    index = pd.date_range(end=end, periods=len(values), freq="B")
    return pd.DataFrame({"Close": values.astype(float)}, index=index)


def _monthly_bars(values: np.ndarray, start: str = "2024-01-31") -> pd.DataFrame:
    index = pd.date_range(start=start, periods=len(values), freq="ME")
    return pd.DataFrame({"Close": values.astype(float)}, index=index)


class QQQVolDCAValidationTests(unittest.TestCase):
    def test_monthly_base_contribution_is_not_duplicated(self) -> None:
        strategy = QQQVolDCAStrategy()
        qqq = _daily_bars(np.linspace(100.0, 150.0, 1300), "2026-08-03")
        vxn = _daily_bars(np.full(1300, 20.0), "2026-08-03")
        bars = {"QQQ.US": qqq, "VXN.US": vxn}

        first = strategy.scan(run_id="r1", front_bars=bars, asof=qqq.index[-1])
        self.assertEqual(len(first.candidates), 1)
        self.assertAlmostEqual(first.candidates[0]["base_multiple"], 0.8)
        self.assertAlmostEqual(
            first.state["runtime_state"]["reserve_multiple"], 0.2
        )

        second = strategy.scan(
            run_id="r2",
            front_bars=bars,
            runtime_state=first.state["runtime_state"],
            asof=qqq.index[-1],
        )
        self.assertEqual(second.candidates, ())
        self.assertEqual(second.state["contribution_multiple"], 0.0)

    def test_extreme_drawdown_queues_all_tiers_but_spends_one_tranche(self) -> None:
        strategy = QQQVolDCAStrategy()
        qqq_values = np.linspace(100.0, 200.0, 1300)
        qqq_values[-1] = 130.0
        vol_values = np.full(1300, 18.0)
        vol_values[-1] = 60.0
        qqq = _daily_bars(qqq_values, "2026-08-07")
        vxn = _daily_bars(vol_values, "2026-08-07")

        result = strategy.scan(
            run_id="panic",
            front_bars={"QQQ.US": qqq, "VXN.US": vxn},
            runtime_state={
                "last_base_month": "2026-08",
                "reserve_multiple": 4.0,
            },
            asof=qqq.index[-1],
        )

        self.assertEqual(result.state["runtime_state"]["triggered_tiers"], [1, 2, 3])
        self.assertEqual(result.state["runtime_state"]["pending_panic_tranches"], 6)
        self.assertAlmostEqual(result.candidates[0]["panic_multiple"], 0.5)
        self.assertAlmostEqual(
            result.state["runtime_state"]["reserve_multiple"], 3.5
        )

        duplicate = strategy.scan(
            run_id="same-week",
            front_bars={"QQQ.US": qqq, "VXN.US": vxn},
            runtime_state=result.state["runtime_state"],
            asof=qqq.index[-1],
        )
        self.assertEqual(duplicate.candidates, ())
        self.assertEqual(
            duplicate.state["runtime_state"]["pending_panic_tranches"], 6
        )

    def test_vix_is_used_when_vxn_is_unavailable(self) -> None:
        strategy = QQQVolDCAStrategy()
        qqq = _daily_bars(np.linspace(100.0, 140.0, 1300), "2026-08-07")
        vix = _daily_bars(np.linspace(15.0, 25.0, 1300), "2026-08-07")
        result = strategy.scan(
            run_id="fallback",
            front_bars={"QQQ.US": qqq, "VIX.US": vix},
            asof=qqq.index[-1],
        )
        self.assertEqual(result.state["market"]["volatility_code"], "VIX.US")

    def test_asof_boundary_blocks_future_rows(self) -> None:
        strategy = QQQVolDCAStrategy()
        qqq = _daily_bars(np.linspace(100.0, 160.0, 1300), "2026-08-07")
        vxn = _daily_bars(np.linspace(15.0, 25.0, 1300), "2026-08-07")
        asof = qqq.index[-1]
        baseline = strategy.scan(
            run_id="baseline",
            front_bars={"QQQ.US": qqq, "VXN.US": vxn},
            asof=asof,
        )
        future_index = pd.date_range("2026-08-10", periods=5, freq="B")
        qqq_future = pd.concat(
            [qqq, pd.DataFrame({"Close": [90.0] * 5}, index=future_index)]
        )
        vxn_future = pd.concat(
            [vxn, pd.DataFrame({"Close": [80.0] * 5}, index=future_index)]
        )
        extended = strategy.scan(
            run_id="extended",
            front_bars={"QQQ.US": qqq_future, "VXN.US": vxn_future},
            asof=asof,
        )
        self.assertEqual(baseline.state["market"], extended.state["market"])
        self.assertEqual(baseline.candidates, extended.candidates)

    def test_four_calm_weeks_reset_the_panic_cycle(self) -> None:
        strategy = QQQVolDCAStrategy()
        qqq = _daily_bars(np.linspace(100.0, 180.0, 1300), "2026-09-25")
        vol_values = np.full(1300, 20.0)
        vol_values[-40:] = 5.0
        vxn = _daily_bars(vol_values, "2026-09-25")
        fridays = [item for item in qqq.index if item.weekday() == 4][-4:]
        state = {
            "last_base_month": fridays[0].strftime("%Y-%m"),
            "triggered_tiers": [1, 2, 3],
            "cycle": 1,
        }
        for index, friday in enumerate(fridays):
            result = strategy.scan(
                run_id=f"calm-{index}",
                front_bars={"QQQ.US": qqq, "VXN.US": vxn},
                runtime_state=state,
                asof=friday,
            )
            state = result.state["runtime_state"]
        self.assertEqual(state["triggered_tiers"], [])
        self.assertEqual(state["cycle"], 2)
        self.assertEqual(state["reset_streak"], 0)


class QQQTreasuryRotationValidationTests(unittest.TestCase):
    def _scan(
        self,
        qqq: np.ndarray,
        tlt: np.ndarray,
        sgov: np.ndarray,
        runtime_state: dict[str, object] | None = None,
    ):
        strategy = QQQTreasuryRotationStrategy()
        bars = {
            "QQQ.US": _monthly_bars(qqq),
            "TLT.US": _monthly_bars(tlt),
            "SGOV.US": _monthly_bars(sgov),
        }
        return strategy.scan(
            run_id="rotation",
            front_bars=bars,
            runtime_state=runtime_state,
            asof=pd.Timestamp("2025-09-02"),
        )

    def test_both_risk_assets_eligible(self) -> None:
        result = self._scan(
            np.linspace(100.0, 200.0, 20),
            np.linspace(100.0, 150.0, 20),
            np.linspace(100.0, 104.0, 20),
        )
        self.assertEqual(result.state["regime"], "QQQ_AND_TLT")
        self.assertEqual(
            result.state["target_weights"],
            {"QQQ.US": 0.6, "TLT.US": 0.3, "SGOV.US": 0.1},
        )
        self.assertEqual(len(result.candidates), 3)

    def test_qqq_only_and_sgov_only_regimes(self) -> None:
        qqq_only = self._scan(
            np.linspace(100.0, 200.0, 20),
            np.linspace(150.0, 80.0, 20),
            np.linspace(100.0, 104.0, 20),
        )
        self.assertEqual(qqq_only.state["regime"], "QQQ_ONLY")
        self.assertEqual(qqq_only.state["target_weights"]["QQQ.US"], 0.7)

        sgov_only = self._scan(
            np.linspace(180.0, 90.0, 20),
            np.linspace(150.0, 80.0, 20),
            np.linspace(100.0, 110.0, 20),
        )
        self.assertEqual(sgov_only.state["regime"], "SGOV_ONLY")
        self.assertEqual(sgov_only.state["target_weights"]["SGOV.US"], 1.0)

    def test_same_completed_month_is_not_rebalanced_twice(self) -> None:
        first = self._scan(
            np.linspace(100.0, 200.0, 20),
            np.linspace(100.0, 150.0, 20),
            np.linspace(100.0, 104.0, 20),
        )
        second = self._scan(
            np.linspace(100.0, 200.0, 20),
            np.linspace(100.0, 150.0, 20),
            np.linspace(100.0, 104.0, 20),
            first.state["runtime_state"],
        )
        self.assertFalse(second.state["rebalance_due"])
        self.assertEqual(second.candidates, ())

    def test_incomplete_current_month_is_excluded(self) -> None:
        strategy = QQQTreasuryRotationStrategy()
        base = {
            "QQQ.US": _monthly_bars(np.linspace(100.0, 200.0, 20)),
            "TLT.US": _monthly_bars(np.linspace(100.0, 150.0, 20)),
            "SGOV.US": _monthly_bars(np.linspace(100.0, 104.0, 20)),
        }
        baseline = strategy.scan(
            run_id="base",
            front_bars=base,
            asof=pd.Timestamp("2025-09-15"),
        )
        extended = {
            code: pd.concat(
                [frame, pd.DataFrame({"Close": [value]}, index=[pd.Timestamp("2025-09-12")])]
            )
            for (code, frame), value in zip(base.items(), (20.0, 20.0, 200.0))
        }
        with_incomplete = strategy.scan(
            run_id="incomplete",
            front_bars=extended,
            asof=pd.Timestamp("2025-09-15"),
        )
        self.assertEqual(
            baseline.state["target_weights"],
            with_incomplete.state["target_weights"],
        )
        self.assertEqual(baseline.state["decision_period"], "2025-08")


class USAllocationRegistrationTests(unittest.TestCase):
    def test_both_strategies_are_registered_in_one_research_family(self) -> None:
        registry = create_strategy_registry()
        self.assertEqual(len(registry), 26)
        for strategy_id in ("qqq_vol_dca_v1", "qqq_treasury_rotation_v1"):
            metadata = registry[strategy_id].metadata
            self.assertEqual(metadata.strategy_family, "us_etf_allocation")
            self.assertEqual(metadata.category, StrategyCategory.RESEARCH_PROJECT)
            self.assertFalse(metadata.scan_enabled)
            self.assertFalse(metadata.backtest_enabled)
            self.assertEqual(metadata.lifecycle, "RESEARCH_ONLY")


if __name__ == "__main__":
    unittest.main()
