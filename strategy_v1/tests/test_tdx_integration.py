from __future__ import annotations

import os
import unittest

from strategy_v1.config import StrategyConfig
from strategy_v1.tdx_adapter import TdxAdapter


@unittest.skipUnless(os.environ.get("TDX_INTEGRATION") == "1", "set TDX_INTEGRATION=1 to use the live client")
class TdxIntegrationTests(unittest.TestCase):
    def test_reads_daily_ohlcv(self) -> None:
        config = StrategyConfig()
        with TdxAdapter(config, __file__) as adapter:
            bars = adapter.fetch_bars(["600519.SH"], "1d", 3, dividend_type="none")
        self.assertIn("600519.SH", bars)
        self.assertEqual(len(bars["600519.SH"]), 3)
        self.assertTrue({"Open", "High", "Low", "Close", "Volume"}.issubset(bars["600519.SH"].columns))


if __name__ == "__main__":
    unittest.main()
