from __future__ import annotations

import unittest
from types import SimpleNamespace

import pandas as pd

from research_platform.api import _position_changes
from research_platform.backtest_engine import (
    _course49_attribution,
    _historical_limit_codes,
    _execution_cost_config,
    _performance_metrics,
    _roll_course49_pending,
    _resolve_sampling_mode,
    _select_universe,
    _snapshot_event_minimum_streak,
    _stratified_sample,
    _slice_schedule,
    _trading_day,
    _trading_days,
    _validation_summary,
)
from research_platform.config import PortfolioConfig


class BacktestTimeTests(unittest.TestCase):
    def test_snapshot_event_scope_defaults_are_backward_compatible(self) -> None:
        self.assertEqual(
            _snapshot_event_minimum_streak({"course49_event_scope": "strategy_candidates"}),
            2,
        )
        self.assertEqual(
            _snapshot_event_minimum_streak({"course49_event_scope": "all_limit_symbols"}),
            1,
        )
        self.assertEqual(
            _snapshot_event_minimum_streak({"course49_event_minimum_streak": 1}),
            1,
        )

    def test_aware_signal_and_naive_bar_share_trading_day(self) -> None:
        signal_time = pd.Timestamp("2026-08-07 15:00", tz="Asia/Shanghai")
        bar_days = _trading_days(pd.DatetimeIndex(["2026-08-07", "2026-08-08"]))

        self.assertEqual(_trading_day(signal_time), pd.Timestamp("2026-08-07"))
        self.assertTrue((bar_days == _trading_day(signal_time)).any())

    def test_universe_selection_and_custom_code_normalization(self) -> None:
        codes = ["600000.SH", "000001.SZ", "300001.SZ", "688001.SH", "830001.BJ"]

        self.assertEqual(_select_universe(codes, "growth", []), ["300001.SZ"])
        self.assertEqual(_select_universe(codes, "star", []), ["688001.SH"])
        self.assertEqual(
            _select_universe(codes, "custom", ["600000", "300001.SZ"]),
            ["600000.SH", "300001.SZ"],
        )
        self.assertEqual(_select_universe(codes, "all_a", []), codes)

    def test_stratified_sampling_is_reproducible_and_covers_markets(self) -> None:
        codes = [
            "600001.SH", "600002.SH", "600003.SH",
            "000001.SZ", "000002.SZ", "000003.SZ",
            "300001.SZ", "300002.SZ", "300003.SZ",
            "688001.SH", "688002.SH", "688003.SH",
            "830001.BJ", "830002.BJ", "830003.BJ",
        ]
        dates = pd.date_range("2026-01-01", periods=21, freq="B")
        bars = {}
        for index, code in enumerate(codes, start=1):
            frame = pd.DataFrame(
                {
                    "Close": [10.0] * len(dates),
                    "Volume": [1_000_000.0] * len(dates),
                    "Amount": [20_000_000.0 * index] * len(dates),
                },
                index=dates,
            )
            frame.attrs["amount_unit"] = "CNY"
            bars[code] = frame
        first = _stratified_sample(codes, bars, 10, 49)
        second = _stratified_sample(list(reversed(codes)), bars, 10, 49)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)
        self.assertEqual(
            {"SH", "SZ", "BJ"},
            {code.rsplit(".", 1)[-1] for code in first},
        )
        self.assertTrue(any(code.startswith("300") for code in first))
        self.assertTrue(any(code.startswith("688") for code in first))

    def test_legacy_stock_limit_switches_to_stratified(self) -> None:
        self.assertEqual(_resolve_sampling_mode("full", 500), "stratified")
        self.assertEqual(_resolve_sampling_mode("full", None), "full")

    def test_schedule_is_limited_to_requested_dates(self) -> None:
        schedule = {"2026-01-05": {}, "2026-01-06": {}, "2026-01-07": {}}

        sliced = _slice_schedule(schedule, "2026-01-06", "2026-01-06")

        self.assertEqual(list(sliced), ["2026-01-06"])

    def test_lhb_candidates_only_include_historical_limit_ups(self) -> None:
        dates = pd.date_range("2026-07-01", periods=4, freq="B")
        bars = {
            "000001.SZ": pd.DataFrame({"Close": [10.0, 11.0, 11.2, 11.3]}, index=dates),
            "000002.SZ": pd.DataFrame({"Close": [10.0, 10.5, 10.6, 10.7]}, index=dates),
            "300001.SZ": pd.DataFrame({"Close": [10.0, 11.0, 12.0, 12.1]}, index=dates),
        }

        result = _historical_limit_codes(
            bars,
            {"000001.SZ": "样本", "000002.SZ": "样本", "300001.SZ": "创业板样本"},
            pd.Timestamp("2026-07-01"),
            pd.Timestamp("2026-07-06"),
        )

        self.assertEqual(result, ["000001.SZ"])

    def test_unfilled_buy_expires_but_blocked_sell_is_carried(self) -> None:
        stale_buy = SimpleNamespace(
            signal_id="buy", code="000001.SZ", side="BUY", generated_at=pd.Timestamp("2026-07-01 18:00")
        )
        blocked_sell = SimpleNamespace(
            signal_id="sell", code="000002.SZ", side="SELL", generated_at=pd.Timestamp("2026-07-01 18:00")
        )

        pending = _roll_course49_pending(
            [stale_buy, blocked_sell],  # type: ignore[list-item]
            pd.Timestamp("2026-07-02"),
            {"000002.SZ": SimpleNamespace()},  # type: ignore[dict-item]
            set(),
        )

        self.assertEqual([item.signal_id for item in pending], ["sell"])

    def test_performance_metrics_and_position_changes(self) -> None:
        equity = pd.DataFrame(
            {
                "timestamp": ["2026-01-05", "2026-01-06", "2026-01-07"],
                "equity": [50_000.0, 51_000.0, 50_500.0],
            }
        )
        equity.index.name = "timestamp"
        trades = pd.DataFrame(
            [
                {"timestamp": "2026-01-05", "strategy_id": "chan_v1", "code": "600000.SH", "side": "BUY", "quantity": 100, "price": 10.0, "pnl": None},
                {"timestamp": "2026-01-07", "strategy_id": "chan_v1", "code": "600000.SH", "side": "SELL", "quantity": 100, "price": 10.5, "pnl": 50.0},
            ]
        )

        metrics = _performance_metrics(equity, trades, 50_000.0)
        changes = _position_changes(trades.to_dict("records"))

        self.assertAlmostEqual(metrics["total_return"], 0.01)
        self.assertEqual(metrics["trades"], 2)
        self.assertEqual(metrics["closed_trades"], 1)
        self.assertFalse(metrics["validation"]["target_verified"])
        self.assertEqual(metrics["win_rate"], 1.0)
        self.assertEqual(changes[0]["quantity_after"], 100)
        self.assertEqual(changes[1]["quantity_after"], 0)

    def test_validation_requires_return_duration_and_closed_trades(self) -> None:
        verified = _validation_summary(
            {"trading_days": 250, "closed_trades": 30, "annualized_return": 0.20}
        )
        short = _validation_summary(
            {"trading_days": 249, "closed_trades": 29, "annualized_return": 0.80}
        )

        self.assertTrue(verified["historical_threshold_met"])
        self.assertFalse(verified["target_verified"])
        self.assertFalse(short["target_verified"])
        self.assertIn("INSUFFICIENT_TRADING_DAYS", short["reasons"])

    def test_execution_cost_multiplier_scales_all_costs(self) -> None:
        base = PortfolioConfig()
        stressed = _execution_cost_config(base, 2.0)

        self.assertEqual(stressed.commission_rate, base.commission_rate * 2)
        self.assertEqual(stressed.min_commission, base.min_commission * 2)
        self.assertEqual(stressed.stamp_duty_rate, base.stamp_duty_rate * 2)
        self.assertEqual(stressed.slippage_rate, base.slippage_rate * 2)

    def test_course49_attribution_separates_capital_and_board_confirmation(self) -> None:
        trades = pd.DataFrame(
            [
                {"timestamp": "2026-01-05", "code": "000001.SZ", "side": "BUY", "reason": "SECOND_BOARD_CAPITAL_CONFIRMED,EARLY_SEAL", "pnl": None},
                {"timestamp": "2026-01-06", "code": "000002.SZ", "side": "BUY", "reason": "SECOND_BOARD_QUALITY_CONFIRMED,STRONG_SEAL", "pnl": None},
                {"timestamp": "2026-01-07", "code": "000001.SZ", "side": "SELL", "reason": "BELOW_MA5", "pnl": 120.0},
                {"timestamp": "2026-01-08", "code": "000002.SZ", "side": "SELL", "reason": "FIXED_STOP", "pnl": -40.0},
            ]
        )

        result = {item["cohort"]: item for item in _course49_attribution(trades)}

        self.assertEqual(result["CAPITAL_AND_BOARD"]["entries"], 1)
        self.assertEqual(result["CAPITAL_AND_BOARD"]["win_rate"], 1.0)
        self.assertEqual(result["BOARD_ONLY"]["entries"], 1)
        self.assertEqual(result["BOARD_ONLY"]["total_pnl"], -40.0)


if __name__ == "__main__":
    unittest.main()
