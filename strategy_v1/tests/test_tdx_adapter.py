from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

import pandas as pd

from strategy_v1.tdx_adapter import _bound_frame_to_window, _read_us_equity_constituents


class TdxAdapterWindowTests(unittest.TestCase):
    def test_bounds_history_and_preserves_requested_warmup(self) -> None:
        index = pd.date_range("2024-01-01", periods=10, freq="B")
        frame = pd.DataFrame({"Close": range(10)}, index=index)

        bounded = _bound_frame_to_window(
            frame,
            start_time="2024-01-08",
            end_time="2024-01-11",
            warmup_bars=2,
        )

        self.assertEqual(bounded.index[0], pd.Timestamp("2024-01-04"))
        self.assertEqual(bounded.index[-1], pd.Timestamp("2024-01-11"))
        self.assertEqual(len(bounded), 6)

    def test_returns_empty_when_symbol_has_no_bar_in_window(self) -> None:
        frame = pd.DataFrame(
            {"Close": [1.0, 2.0]},
            index=pd.to_datetime(["2024-01-01", "2024-01-02"]),
        )

        bounded = _bound_frame_to_window(
            frame,
            start_time="2024-02-01",
            end_time="2024-02-29",
            warmup_bars=90,
        )

        self.assertTrue(bounded.empty)

    def test_us_equity_master_uses_only_broad_index_constituents(self) -> None:
        content = "\n".join(
            (
                "#标普成份股",
                "AAPL",
                "BRK.B",
                "#美股-热门ETF",
                "SPY",
                "QQQ",
                "#纳斯达克100",
                "MSFT",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mgblock.dat"
            path.write_text(content, encoding="gbk")

            values = _read_us_equity_constituents(path)

        self.assertEqual(values, {"AAPL", "BRK.B", "MSFT"})


if __name__ == "__main__":
    unittest.main()
