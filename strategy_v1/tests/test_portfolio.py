from __future__ import annotations

import unittest

import pandas as pd

from strategy_v1.config import StrategyConfig
from strategy_v1.models import PortfolioState, Signal
from strategy_v1.portfolio import PaperBroker


class PortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()
        self.broker = PaperBroker(self.config, PortfolioState(cash=100_000.0))

    def signal(self, side: str, timestamp: str) -> Signal:
        return Signal(
            timestamp=pd.Timestamp(timestamp).to_pydatetime(),
            code="600000.SH",
            side=side,  # type: ignore[arg-type]
            price=10.0,
            reason="test",
            market_regime="NORMAL",
        )

    def test_position_limit_and_t_plus_one(self) -> None:
        self.broker.queue([self.signal("BUY", "2026-01-05 10:00")])
        buy_bars = {
            "600000.SH": pd.DataFrame(
                {"Open": [10.0]}, index=[pd.Timestamp("2026-01-05 10:30")]
            )
        }
        self.broker.process_pending(buy_bars, {"600000.SH": 9.9}, {"600000.SH": "浦发银行"})
        self.assertIn("600000.SH", self.broker.state.positions)
        self.assertLessEqual(
            self.broker.state.positions["600000.SH"].quantity * 10.0,
            self.config.risk.initial_cash * self.config.risk.max_position_weight,
        )

        self.broker.queue([self.signal("SELL", "2026-01-05 11:00")])
        same_day = {
            "600000.SH": pd.DataFrame(
                {"Open": [10.2]}, index=[pd.Timestamp("2026-01-05 13:00")]
            )
        }
        self.broker.process_pending(same_day, {"600000.SH": 9.9}, {"600000.SH": "浦发银行"})
        self.assertIn("600000.SH", self.broker.state.positions)

        next_day = {
            "600000.SH": pd.DataFrame(
                {"Open": [10.2, 10.3]},
                index=[pd.Timestamp("2026-01-05 13:00"), pd.Timestamp("2026-01-06 09:30")],
            )
        }
        self.broker.process_pending(next_day, {"600000.SH": 10.1}, {"600000.SH": "浦发银行"})
        self.assertNotIn("600000.SH", self.broker.state.positions)

    def test_limit_up_buy_remains_pending(self) -> None:
        self.broker.queue([self.signal("BUY", "2026-01-05 10:00")])
        bars = {
            "600000.SH": pd.DataFrame(
                {"Open": [11.0]}, index=[pd.Timestamp("2026-01-05 10:30")]
            )
        }
        self.broker.process_pending(bars, {"600000.SH": 10.0}, {"600000.SH": "浦发银行"})
        self.assertNotIn("600000.SH", self.broker.state.positions)
        self.assertEqual(len(self.broker.state.pending_orders), 1)


if __name__ == "__main__":
    unittest.main()
