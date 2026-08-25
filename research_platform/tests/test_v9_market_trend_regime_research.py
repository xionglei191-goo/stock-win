from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.v9_market_trend_regime_research import (
    MARKET_TREND_SESSIONS,
    filter_v9_trades_by_market_trend,
    protocol_manifest,
)


class V9MarketTrendRegimeResearchTests(unittest.TestCase):
    def test_entry_uses_last_market_close_strictly_before_buy_day(self) -> None:
        market_dates = pd.bdate_range("2024-01-02", periods=MARKET_TREND_SESSIONS)
        closes = np.full(len(market_dates), 100.0)
        closes[-1] = 110.0
        market = pd.DataFrame({"timestamp": market_dates, "Close": closes})
        buy_day = market_dates[-1] + pd.offsets.BDay(1)
        sell_day = buy_day + pd.offsets.BDay(1)
        trades = pd.DataFrame(
            [
                {"timestamp": buy_day, "side": "BUY", "code": "000001.SZ"},
                {"timestamp": sell_day, "side": "SELL", "code": "000001.SZ"},
            ]
        )
        filtered, decisions = filter_v9_trades_by_market_trend(trades, market)
        self.assertTrue(bool(decisions.loc[0, "accepted"]))
        self.assertEqual(decisions.loc[0, "market_date"], market_dates[-1])
        self.assertEqual(len(filtered), 2)

    def test_same_day_and_future_market_rows_do_not_change_entry(self) -> None:
        market_dates = pd.bdate_range("2024-01-02", periods=MARKET_TREND_SESSIONS)
        market = pd.DataFrame({"timestamp": market_dates, "Close": 100.0})
        market.loc[market.index[-1], "Close"] = 110.0
        buy_day = market_dates[-1] + pd.offsets.BDay(1)
        sell_day = buy_day + pd.offsets.BDay(1)
        trades = pd.DataFrame(
            [
                {"timestamp": buy_day, "side": "BUY", "code": "000001.SZ"},
                {"timestamp": sell_day, "side": "SELL", "code": "000001.SZ"},
            ]
        )
        _, first = filter_v9_trades_by_market_trend(trades, market)
        appended = pd.concat(
            [
                market,
                pd.DataFrame(
                    [
                        {"timestamp": buy_day, "Close": 1.0},
                        {"timestamp": sell_day, "Close": 1.0},
                    ]
                ),
            ],
            ignore_index=True,
        )
        _, second = filter_v9_trades_by_market_trend(trades, appended)
        self.assertEqual(bool(first.loc[0, "accepted"]), bool(second.loc[0, "accepted"]))
        self.assertEqual(first.loc[0, "market_date"], second.loc[0, "market_date"])

    def test_insufficient_history_blocks_entry(self) -> None:
        dates = pd.bdate_range("2025-01-02", periods=10)
        trades = pd.DataFrame(
            [
                {"timestamp": dates[-1], "side": "BUY", "code": "000001.SZ"},
                {
                    "timestamp": dates[-1] + pd.offsets.BDay(1),
                    "side": "SELL",
                    "code": "000001.SZ",
                },
            ]
        )
        filtered, decisions = filter_v9_trades_by_market_trend(
            trades, pd.DataFrame({"timestamp": dates, "Close": 100.0})
        )
        self.assertFalse(bool(decisions.loc[0, "accepted"]))
        self.assertTrue(filtered.empty)

    def test_protocol_freezes_one_external_gate(self) -> None:
        protocol = protocol_manifest()
        self.assertEqual(protocol["market_gate"]["lookback_sessions"], 120)
        self.assertFalse(protocol["market_gate"]["additional_trend_or_breadth_filters"])
        self.assertFalse(protocol["market_gate"]["parameter_search"])
        self.assertTrue(protocol["invariants"]["no_production_promotion"])


if __name__ == "__main__":
    unittest.main()
