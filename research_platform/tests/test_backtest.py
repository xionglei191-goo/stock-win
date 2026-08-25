from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

import pandas as pd

from research_platform.api import _position_changes
from research_platform.backtest_engine import (
    BacktestService,
    HistoricalPosition,
    _course49_attribution,
    _empty_execution_funnel,
    _finalize_execution_funnel,
    _historical_limit_codes,
    _execution_cost_config,
    _legacy_chan_cost_config,
    _performance_metrics,
    _roll_course49_pending,
    _resolve_sampling_mode,
    _select_universe,
    _snapshot_event_minimum_streak,
    _snapshot_replay_cache_status,
    _stratified_sample,
    _slice_chan_replay_history,
    _slice_daily,
    _slice_schedule,
    _trading_day,
    _trading_days,
    _us_point_in_time_visible,
    _us_sell_fees,
    _validation_summary,
)
from research_platform.config import PortfolioConfig, USPortfolioConfig
from research_platform.models import PlatformSignal, SignalStatus


class BacktestTimeTests(unittest.TestCase):
    def test_snapshot_replay_accepts_job_progress_callback(self) -> None:
        parameters = inspect.signature(BacktestService.replay_backtest).parameters

        self.assertIn("progress_callback", parameters)

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

    def test_snapshot_replay_defaults_missing_cache_status(self) -> None:
        self.assertEqual(_snapshot_replay_cache_status({}), "snapshot_replay")
        self.assertEqual(
            _snapshot_replay_cache_status({"cache_status": "exact_hit"}),
            "exact_hit",
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

    def test_us_universe_is_hard_separated_and_normalizes_tickers(self) -> None:
        codes = ["AAPL.US", "MSFT.US", "600000.SH"]

        self.assertEqual(
            _select_universe(codes, "all_us", [], market="US"),
            ["AAPL.US", "MSFT.US"],
        )
        self.assertEqual(
            _select_universe(codes, "custom", ["aapl", "MSFT.US"], market="US"),
            ["AAPL.US", "MSFT.US"],
        )
        with self.assertRaisesRegex(ValueError, "require.*all_us"):
            _select_universe(codes, "all_a", [], market="US")
        with self.assertRaisesRegex(ValueError, "cannot use.*all_us"):
            _select_universe(["600000.SH"], "all_us", [], market="CN")

    def test_us_point_in_time_visibility_rejects_stale_symbol(self) -> None:
        day = pd.Timestamp("2026-01-06")
        current = pd.DataFrame({"Close": [10.0]}, index=[day])
        stale = pd.DataFrame({"Close": [8.0]}, index=[day - pd.Timedelta(days=1)])

        visible = _us_point_in_time_visible(
            {"AAPL.US": current, "OLD.US": stale, "600000.SH": current},
            day,
        )

        self.assertEqual(set(visible), {"AAPL.US"})

    def test_us_sell_fees_include_commission_sec_and_finra(self) -> None:
        config = USPortfolioConfig(
            commission_rate=0.001,
            sec_sell_fee_rate=0.00002,
            finra_taf_per_share=0.0002,
            finra_taf_cap=10.0,
        )

        self.assertAlmostEqual(
            _us_sell_fees(10_000.0, 100, config),
            10.0 + 0.2 + 0.02,
        )

    def test_us_stop_uses_gap_open_and_checks_every_session(self) -> None:
        service = SimpleNamespace(
            strategies={
                "us_momentum_v1": SimpleNamespace(
                    metadata=SimpleNamespace(version="2.0.0")
                )
            }
        )
        position = HistoricalPosition(
            code="AAPL.US",
            quantity=10,
            average_price=100.0,
            entry_date="2026-01-02",
            stop_price=92.0,
            last_price=100.0,
            evidence="{}",
            entry_fees=1.0,
        )
        day = pd.Timestamp("2026-01-06")
        bars = {
            "AAPL.US": pd.DataFrame(
                {"Open": [90.0], "Low": [88.0], "Close": [89.0]},
                index=[day],
            )
        }
        config = USPortfolioConfig(
            commission_rate=0.0,
            slippage_rate=0.0,
            sec_sell_fee_rate=0.0,
            finra_taf_per_share=0.0,
        )
        positions = {"AAPL.US": position}

        cash, trades = BacktestService._fill_us_pending(
            service,
            "us_momentum_v1",
            [],
            day,
            bars,
            positions,
            0.0,
            config,
        )

        self.assertEqual(cash, 900.0)
        self.assertEqual(trades[0]["price"], 90.0)
        self.assertEqual(trades[0]["reason"], "US_FIXED_STOP")
        self.assertEqual(positions, {})

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

    def test_daily_slice_includes_the_full_cutoff_day_for_sorted_tz_bars(self) -> None:
        index = pd.DatetimeIndex(
            [
                "2026-01-05 15:00+08:00",
                "2026-01-06 09:30+08:00",
                "2026-01-06 15:00+08:00",
                "2026-01-07 09:30+08:00",
            ]
        )
        frame = pd.DataFrame({"Close": range(4)}, index=index)
        bars = {"000001.SZ": pd.concat([frame] * 7).sort_index()}

        sliced = _slice_daily(bars, pd.Timestamp("2026-01-06"))

        self.assertEqual(len(sliced["000001.SZ"]), 21)
        self.assertEqual(
            sliced["000001.SZ"].index[-1], pd.Timestamp("2026-01-06 15:00+08:00")
        )

    def test_daily_slice_preserves_unsorted_fallback_behavior(self) -> None:
        dates = pd.date_range("2025-12-01", periods=22, freq="B")
        frame = pd.DataFrame({"Close": range(22)}, index=dates[::-1])

        sliced = _slice_daily(
            {"000001.SZ": frame},
            pd.Timestamp("2025-12-29"),
        )

        self.assertEqual(len(sliced["000001.SZ"]), 21)
        self.assertNotIn(pd.Timestamp("2025-12-30"), sliced["000001.SZ"].index)

    def test_chan_replay_uses_only_frozen_warmup_and_window_rows(self) -> None:
        index = pd.date_range("2024-01-02", periods=300, freq="B")
        frame = pd.DataFrame(
            {
                "Open": range(300),
                "High": range(300),
                "Low": range(300),
                "Close": range(300),
                "Volume": range(300),
            },
            index=index,
        )
        frame.attrs["amount_unit"] = "CNY"

        front, raw, market = _slice_chan_replay_history(
            {"600000.SH": frame},
            {"600000.SH": frame},
            frame,
            index[200].date().isoformat(),
            index[250].date().isoformat(),
            120,
        )

        self.assertEqual(front["600000.SH"].index[0], index[80])
        self.assertEqual(front["600000.SH"].index[-1], index[250])
        self.assertEqual(raw["600000.SH"].index[-1], index[250])
        self.assertEqual(market.index[-1], index[250])
        self.assertEqual(front["600000.SH"].attrs["amount_unit"], "CNY")

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

    def test_pullback_open_gap_and_limit_blocks_are_counted(self) -> None:
        service = BacktestService.__new__(BacktestService)
        service.config = SimpleNamespace(portfolio=PortfolioConfig())
        funnel = _empty_execution_funnel()

        def signal(code: str) -> PlatformSignal:
            return PlatformSignal(
                run_id="run",
                strategy_id="course49_system",
                strategy_version="1.1.0",
                generated_at=pd.Timestamp("2026-07-01 18:00", tz="Asia/Shanghai"),
                available_at=pd.Timestamp("2026-07-01 18:00", tz="Asia/Shanghai"),
                code=code,
                side="BUY",
                strength=0.8,
                target_weight=0.1,
                horizon="daily-short",
                valid_until=pd.Timestamp("2026-07-02 09:25", tz="Asia/Shanghai"),
                stop_price=9.5,
                status=SignalStatus.PROPOSED,
                reason_codes=("LEADER_PULLBACK_RECLAIM",),
                evidence={"entry_gap_min": -0.03, "entry_gap_max": 0.08},
                playbook_id="leader_pullback_reclaim",
            )

        dates = pd.DatetimeIndex(["2026-07-01", "2026-07-02"])
        bars = {
            "000001.SZ": pd.DataFrame({"Open": [10.0, 10.9], "Close": [10.0, 10.9]}, index=dates),
            "000002.SZ": pd.DataFrame({"Open": [10.0, 9.6], "Close": [10.0, 9.6]}, index=dates),
            "000003.SZ": pd.DataFrame({"Open": [10.0, 11.0], "Close": [10.0, 11.0]}, index=dates),
        }
        cash, trades = service._fill_course49_pending(
            [signal(code) for code in bars],
            pd.Timestamp("2026-07-02"),
            bars,
            {code: "样本" for code in bars},
            {},
            50_000.0,
            set(),
            PortfolioConfig(),
            funnel,
        )
        result = _finalize_execution_funnel(funnel)
        self.assertEqual(cash, 50_000.0)
        self.assertFalse(trades)
        self.assertEqual(result["attempted_next_open"], 3)
        self.assertEqual(result["blocked_open_gap"], 2)
        self.assertEqual(result["blocked_limit_up_open"], 1)
        self.assertEqual(result["fill_rate"], 0.0)

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

        legacy = _legacy_chan_cost_config(stressed)
        self.assertEqual(legacy.commission_rate, stressed.commission_rate)
        self.assertEqual(legacy.min_commission, stressed.min_commission)
        self.assertEqual(legacy.stamp_duty_rate, stressed.stamp_duty_rate)
        self.assertEqual(legacy.slippage_rate, stressed.slippage_rate)

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
