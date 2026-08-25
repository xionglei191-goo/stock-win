from __future__ import annotations

import numpy as np
import pandas as pd
import unittest
import tempfile
from pathlib import Path

from research_platform.storage import Database
from research_platform.strategies.us_momentum import (
    USMomentumParameters,
    USMomentumStrategy,
    _score_bars,
    _signal_times,
)
from research_platform.tests.helpers import temporary_config


def _make_bars(n: int = 300, trend: str = "up") -> pd.DataFrame:
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(42)
    if trend == "up":
        prices = 100.0 * np.cumprod(1 + rng.normal(0.001, 0.012, n))
        prices = np.sort(prices)  # monotonically rising guarantees MA200<close
    elif trend == "down":
        prices = 100.0 * np.cumprod(1 + rng.normal(-0.002, 0.012, n))
    else:
        prices = np.full(n, 100.0)
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {"Open": prices, "High": prices * 1.01, "Low": prices * 0.99, "Close": prices, "Volume": volume},
        index=dates,
    )


def _make_us_bars(codes: list[str], trend: str = "up") -> dict[str, pd.DataFrame]:
    return {code: _make_bars(300, trend) for code in codes}


class TestScoreBars(unittest.TestCase):
    def test_uptrend_passes(self):
        bars = _make_bars(300, "up")
        params = USMomentumParameters()
        result = _score_bars(bars, params)
        assert result is not None
        assert "rs_score" in result
        assert result["close"] > result["ma_slow"]

    def test_downtrend_rejected(self):
        bars = _make_bars(300, "down")
        params = USMomentumParameters()
        assert _score_bars(bars, params) is None

    def test_too_few_bars_rejected(self):
        bars = _make_bars(50, "up")
        params = USMomentumParameters()
        assert _score_bars(bars, params) is None

    def test_depth_filter_rejects_far_from_high(self):
        n = 300
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        prices = np.linspace(100, 130, n)
        prices[-1] = prices[-2] * 0.85  # last bar pulls back 15%
        volume = np.full(n, 2_000_000.0)
        bars = pd.DataFrame(
            {"Open": prices, "High": prices * 1.01, "Low": prices * 0.99, "Close": prices, "Volume": volume},
            index=dates,
        )
        params = USMomentumParameters(max_depth_from_high=0.001)
        assert _score_bars(bars, params) is None

    def test_vol_ratio_filter(self):
        bars = _make_bars(220, "up")
        # spike recent volume 5x
        bars = bars.copy()
        bars.iloc[-5:, bars.columns.get_loc("Volume")] *= 20
        params = USMomentumParameters(vol_ratio_cap=1.5)
        assert _score_bars(bars, params) is None

    def test_rs_score_components(self):
        bars = _make_bars(300, "up")
        result = _score_bars(bars, USMomentumParameters())
        assert result is not None
        # rs_score = max(ret_long,0)脳0.50 + ret_mid脳0.30 + ret_short脳0.20
        # + acceleration_bonus = max(ret_short - 0.15, 0) * 0.5
        ret_long_adj = max(result["ret_long"], 0.0)
        acceleration = max(result["ret_short"] - 0.15, 0.0) * 0.5
        raw_expected = ret_long_adj * 0.50 + result["ret_mid"] * 0.30 + result["ret_short"] * 0.20 + acceleration
        assert abs(result["rs_score"] - raw_expected) < 1e-4


class TestUSMomentumStrategy(unittest.TestCase):
    def test_empty_result_no_us_codes(self):
        strategy = USMomentumStrategy()
        bars = {"600001.SH": _make_bars(300, "up"), "000001.SZ": _make_bars(300, "up")}
        result = strategy.scan(run_id="r1", front_bars=bars, is_rebalance_day=True)
        assert result.candidates == ()
        assert result.signals == ()

    def test_candidates_only_us_codes(self):
        strategy = USMomentumStrategy()
        bars = {
            "AAPL.US": _make_bars(300, "up"),
            "MSFT.US": _make_bars(300, "up"),
            "600001.SH": _make_bars(300, "up"),
        }
        result = strategy.scan(
            run_id="r1",
            front_bars=bars,
            is_rebalance_day=True,
        )
        codes = {c["code"] for c in result.candidates}
        assert "600001.SH" not in codes
        for code in codes:
            assert code.endswith(".US")

    def test_candidates_sorted_by_rs_score(self):
        strategy = USMomentumStrategy()
        codes = [f"S{i:03d}.US" for i in range(10)]
        bars = _make_us_bars(codes, "up")
        result = strategy.scan(
            run_id="r1",
            front_bars=bars,
            is_rebalance_day=True,
        )
        scores = [c["rs_score"] for c in result.candidates]
        assert scores == sorted(scores, reverse=True)

    def test_no_signals_when_emit_disabled(self):
        strategy = USMomentumStrategy(USMomentumParameters(emit_live_entry_signals=False))
        bars = _make_us_bars(["AAPL.US", "MSFT.US", "GOOG.US"], "up")
        result = strategy.scan(
            run_id="r1",
            front_bars=bars,
            backtest_mode=False,
            is_rebalance_day=True,
        )
        assert result.signals == ()

    def test_signals_emitted_in_backtest_mode(self):
        strategy = USMomentumStrategy(USMomentumParameters(use_market_regime=False))
        bars = _make_us_bars(["AAPL.US", "MSFT.US", "GOOG.US"], "up")
        result = strategy.scan(
            run_id="r1",
            front_bars=bars,
            backtest_mode=True,
            is_rebalance_day=True,
        )
        assert len(result.signals) > 0
        for sig in result.signals:
            assert sig.side == "BUY"
            assert sig.stop_price is not None
            assert sig.stop_price < sig.evidence["close"]

    def test_stop_price_eight_pct_below_close(self):
        strategy = USMomentumStrategy(
            USMomentumParameters(stop_ratio=0.08, use_market_regime=False)
        )
        bars = _make_us_bars(["AAPL.US"], "up")
        result = strategy.scan(
            run_id="r1",
            front_bars=bars,
            backtest_mode=True,
            is_rebalance_day=True,
        )
        self.assertTrue(result.signals)
        sig = result.signals[0]
        close = sig.evidence["close"]
        assert abs(sig.stop_price - close * 0.92) < 0.01

    def test_existing_position_excluded_from_signals(self):
        strategy = USMomentumStrategy()
        bars = _make_us_bars(["AAPL.US", "MSFT.US"], "up")
        positions = [{"code": "AAPL.US", "weight": 0.10}]
        result = strategy.scan(
            run_id="r1",
            front_bars=bars,
            positions=positions,
            backtest_mode=True,
            is_rebalance_day=True,
        )
        signal_codes = {s.code for s in result.signals}
        assert "AAPL.US" not in signal_codes

    def test_metadata(self):
        m = USMomentumStrategy.metadata
        assert m.strategy_id == "us_momentum_v1"
        assert m.asset_classes == ("US_STOCK",)
        assert m.lifecycle == "RESEARCH_ONLY"
        assert m.requires_approval is True
        assert m.scan_enabled is False
        assert m.backtest_enabled is True
        assert m.runtime_adapter.value == "us_strict"

    def test_missing_market_data_fails_closed(self):
        strategy = USMomentumStrategy()
        result = strategy.scan(
            run_id="r1",
            front_bars={"AAPL.US": _make_bars()},
            backtest_mode=True,
            is_rebalance_day=True,
        )
        self.assertEqual(result.state["status"], "MARKET_DATA_UNAVAILABLE")
        self.assertFalse(result.signals)

    def test_non_rebalance_day_and_same_month_repeat_are_blocked(self):
        strategy = USMomentumStrategy(USMomentumParameters(use_market_regime=False))
        bars = _make_us_bars(["AAPL.US", "MSFT.US"])
        first = strategy.scan(
            run_id="r1",
            front_bars=bars,
            backtest_mode=True,
            is_rebalance_day=False,
        )
        self.assertEqual(first.state["status"], "NOT_REBALANCE_DAY")
        self.assertEqual(first.state["runtime_state"], {})

        month_end = strategy.scan(
            run_id="r2",
            front_bars=bars,
            backtest_mode=True,
            is_rebalance_day=True,
        )
        repeat = strategy.scan(
            run_id="r3",
            front_bars=bars,
            backtest_mode=True,
            is_rebalance_day=True,
            runtime_state=month_end.state["runtime_state"],
        )
        self.assertEqual(repeat.state["status"], "ALREADY_REBALANCED")
        self.assertFalse(repeat.signals)

    def test_stale_position_data_does_not_force_sell(self):
        strategy = USMomentumStrategy(USMomentumParameters(use_market_regime=False))
        fresh = _make_bars()
        stale = fresh.iloc[:-1]
        result = strategy.scan(
            run_id="r1",
            front_bars={"AAPL.US": stale, "MSFT.US": fresh},
            positions=[{"code": "AAPL.US", "weight": 0.10}],
            asof=fresh.index[-1],
            is_rebalance_day=True,
        )
        self.assertNotIn("AAPL.US", {signal.code for signal in result.signals})
        self.assertIn("AAPL.US", result.state["stale_rejected"])

    def test_qqq_and_non_tradable_us_analysis_inputs_are_excluded(self):
        strategy = USMomentumStrategy(USMomentumParameters(use_market_regime=False))
        codes = ["AAPL.US", "QQQ.US", "TLT.US"]
        result = strategy.scan(
            run_id="r1",
            front_bars=_make_us_bars(codes),
            backtest_mode=True,
            is_rebalance_day=True,
            tradable_codes={"AAPL.US"},
        )
        candidate_codes = {item["code"] for item in result.candidates}
        signal_codes = {item.code for item in result.signals}
        self.assertEqual(candidate_codes, {"AAPL.US"})
        self.assertEqual(signal_codes, {"AAPL.US"})

    def test_strength_and_absolute_weights_stay_bounded(self):
        params = USMomentumParameters(use_market_regime=False, max_total_weight=0.15)
        result = USMomentumStrategy(params).scan(
            run_id="r1",
            front_bars=_make_us_bars([f"S{i}.US" for i in range(20)]),
            backtest_mode=True,
            is_rebalance_day=True,
        )
        buys = [signal for signal in result.signals if signal.side == "BUY"]
        self.assertTrue(buys)
        self.assertTrue(all(0.0 <= signal.strength <= 1.0 for signal in buys))
        self.assertTrue(all(signal.target_weight <= params.target_weight for signal in buys))
        self.assertLessEqual(sum(signal.target_weight for signal in buys), 0.150001)

    def test_signal_window_uses_next_nyse_session_after_holiday(self):
        generated, available, valid_until = _signal_times("2025-12-31")

        self.assertEqual(generated.date().isoformat(), "2025-12-31")
        self.assertEqual(available.date().isoformat(), "2026-01-02")
        self.assertEqual(valid_until.date().isoformat(), "2026-01-02")

    def test_rebalance_period_state_survives_database_roundtrip(self):
        strategy = USMomentumStrategy(USMomentumParameters(use_market_regime=False))
        bars = _make_us_bars(["AAPL.US", "MSFT.US"])
        month_end = strategy.scan(
            run_id="r1",
            front_bars=bars,
            backtest_mode=True,
            is_rebalance_day=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Database(temporary_config(Path(directory)))
            database.initialize()
            database.replace_runtime_states(
                strategy.metadata.strategy_id,
                month_end.state["runtime_state"],
                str(month_end.state["asof"]),
            )
            restored = database.load_runtime_states(strategy.metadata.strategy_id)

        repeat = strategy.scan(
            run_id="r2",
            front_bars=bars,
            backtest_mode=True,
            is_rebalance_day=True,
            runtime_state=restored,
        )
        self.assertEqual(repeat.state["status"], "ALREADY_REBALANCED")
        self.assertFalse(repeat.signals)


if __name__ == "__main__":
    unittest.main()
