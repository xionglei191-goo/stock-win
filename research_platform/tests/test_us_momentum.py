from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research_platform.strategies.us_momentum import (
    USMomentumParameters,
    USMomentumStrategy,
    _score_bars,
)


def _make_bars(n: int = 220, trend: str = "up") -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(42)
    if trend == "up":
        prices = 100.0 * np.cumprod(1 + rng.normal(0.001, 0.012, n))
        # force MA50 > MA200 structure by biasing upward enough
        prices = np.sort(prices)  # monotonically rising guarantees MA50>MA200
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
    return {code: _make_bars(220, trend) for code in codes}


class TestScoreBars:
    def test_uptrend_passes(self):
        bars = _make_bars(220, "up")
        params = USMomentumParameters()
        result = _score_bars(bars, params)
        assert result is not None
        assert "rs_score" in result
        assert result["ma_fast"] > result["ma_slow"]

    def test_downtrend_rejected(self):
        bars = _make_bars(220, "down")
        params = USMomentumParameters()
        assert _score_bars(bars, params) is None

    def test_too_few_bars_rejected(self):
        bars = _make_bars(50, "up")
        params = USMomentumParameters()
        assert _score_bars(bars, params) is None

    def test_depth_filter_rejects_far_from_high(self):
        # monotonically sorted prices mean last bar IS the 50-day high → depth=0
        # use a price series that has a recent pullback instead
        n = 220
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        prices = np.linspace(100, 130, n)
        prices[-1] = prices[-2] * 0.85  # last bar pulls back 15% — well beyond 0.1%
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
        bars = _make_bars(220, "up")
        result = _score_bars(bars, USMomentumParameters())
        assert result is not None
        # rs_score is rounded to 6 decimal places; verify against unrounded components
        raw_expected = result["ret_short"] * 0.4 + result["ret_mid"] * 0.35 + result["ret_long"] * 0.25
        assert abs(result["rs_score"] - raw_expected) < 1e-4


class TestUSMomentumStrategy:
    def test_empty_result_no_us_codes(self):
        strategy = USMomentumStrategy()
        bars = {"600001.SH": _make_bars(220, "up"), "000001.SZ": _make_bars(220, "up")}
        result = strategy.scan(run_id="r1", front_bars=bars)
        assert result.candidates == ()
        assert result.signals == ()

    def test_candidates_only_us_codes(self):
        strategy = USMomentumStrategy()
        bars = {
            "AAPL.US": _make_bars(220, "up"),
            "MSFT.US": _make_bars(220, "up"),
            "600001.SH": _make_bars(220, "up"),
        }
        result = strategy.scan(run_id="r1", front_bars=bars)
        codes = {c["code"] for c in result.candidates}
        assert "600001.SH" not in codes
        for code in codes:
            assert code.endswith(".US")

    def test_candidates_sorted_by_rs_score(self):
        strategy = USMomentumStrategy()
        codes = [f"S{i:03d}.US" for i in range(10)]
        bars = _make_us_bars(codes, "up")
        result = strategy.scan(run_id="r1", front_bars=bars)
        scores = [c["rs_score"] for c in result.candidates]
        assert scores == sorted(scores, reverse=True)

    def test_no_signals_when_emit_disabled(self):
        strategy = USMomentumStrategy(USMomentumParameters(emit_live_entry_signals=False))
        bars = _make_us_bars(["AAPL.US", "MSFT.US", "GOOG.US"], "up")
        result = strategy.scan(run_id="r1", front_bars=bars, backtest_mode=False)
        assert result.signals == ()

    def test_signals_emitted_in_backtest_mode(self):
        strategy = USMomentumStrategy()
        bars = _make_us_bars(["AAPL.US", "MSFT.US", "GOOG.US"], "up")
        result = strategy.scan(run_id="r1", front_bars=bars, backtest_mode=True)
        assert len(result.signals) > 0
        for sig in result.signals:
            assert sig.side == "BUY"
            assert sig.stop_price is not None
            assert sig.stop_price < sig.evidence["close"]

    def test_stop_price_eight_pct_below_close(self):
        strategy = USMomentumStrategy(USMomentumParameters(stop_ratio=0.08))
        bars = _make_us_bars(["AAPL.US"], "up")
        result = strategy.scan(run_id="r1", front_bars=bars, backtest_mode=True)
        if result.signals:
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
        )
        signal_codes = {s.code for s in result.signals}
        assert "AAPL.US" not in signal_codes

    def test_metadata(self):
        m = USMomentumStrategy.metadata
        assert m.strategy_id == "us_momentum_v1"
        assert m.asset_classes == ("US_STOCK",)
        assert m.lifecycle == "RESEARCH_ONLY"
        assert m.requires_approval is True
