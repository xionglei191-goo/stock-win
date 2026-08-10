from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.models import OrderGroupAction
from research_platform.strategies.pairs_arbitrage import PairSpec, PairsArbitrageStrategy


def pair_bars(spike: float = 1.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(49)
    right = np.exp(np.cumsum(rng.normal(0.0005, 0.008, 90))) * 100
    left = right * 1.1 * np.exp(rng.normal(0, 0.002, 90))
    left[-1] *= spike
    index = pd.date_range("2025-01-01", periods=90, freq="B")
    make = lambda values: pd.DataFrame({"Open": values, "Close": values}, index=index)
    return make(left), make(right)


class PairsArbitrageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pair = PairSpec("LEFT.SH", "RIGHT.SH", "test pair")
        self.strategy = PairsArbitrageStrategy((self.pair,))

    def test_entry_is_an_atomic_long_short_group(self) -> None:
        left, right = pair_bars(1.005)
        result = self.strategy.scan(
            run_id="run",
            front_bars={self.pair.left: left, self.pair.right: right},
            raw_bars={self.pair.left: left, self.pair.right: right},
        )
        self.assertEqual(len(result.order_groups), 1)
        intent = result.order_groups[0]
        self.assertEqual(intent.action, OrderGroupAction.OPEN)
        self.assertEqual({leg.side for leg in intent.legs}, {"BUY", "SHORT"})
        self.assertEqual(sum(leg.target_weight for leg in intent.legs), 1.0)

    def test_active_pair_closes_after_mean_reversion(self) -> None:
        left, right = pair_bars()
        self.strategy.exit_zscore = 0.65
        position = {
            "group_key": self.pair.key,
            "legs": [
                {"code": self.pair.left, "side": "SHORT", "ratio": 1.0, "target_weight": 0.5},
                {"code": self.pair.right, "side": "LONG", "ratio": 1.0, "target_weight": 0.5},
            ],
        }
        result = self.strategy.scan(
            run_id="run",
            front_bars={self.pair.left: left, self.pair.right: right},
            raw_bars={self.pair.left: left, self.pair.right: right},
            positions=[position],
        )
        self.assertEqual(len(result.order_groups), 1)
        intent = result.order_groups[0]
        self.assertEqual(intent.action, OrderGroupAction.CLOSE)
        self.assertEqual({leg.side for leg in intent.legs}, {"SELL", "COVER"})

    def test_future_rows_do_not_change_prior_statistics(self) -> None:
        left, right = pair_bars(1.005)
        before = self.strategy.pair_statistics(left, right)
        future_date = left.index[-1] + pd.offsets.BDay(1)
        left_with_future = pd.concat([left, pd.DataFrame({"Open": [999.0], "Close": [999.0]}, index=[future_date])])
        right_with_future = pd.concat([right, pd.DataFrame({"Open": [1.0], "Close": [1.0]}, index=[future_date])])
        after = self.strategy.pair_statistics(
            left_with_future.loc[: left.index[-1]], right_with_future.loc[: right.index[-1]]
        )
        self.assertAlmostEqual(before["zscore"], after["zscore"], places=12)  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
