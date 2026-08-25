from __future__ import annotations

import struct
import unittest

import pandas as pd

from research_platform.etf_pullback_research import (
    ASSETS,
    build_etf_market_state_pullback_events,
    build_etf_pullback_events,
    build_etf_relative_pullback_events,
    build_market_state_context,
    decode_day_bytes,
)


class EtfPullbackResearchTests(unittest.TestCase):
    def test_day_decoder_uses_etf_price_precision(self) -> None:
        payload = struct.pack(
            "<IIIIIfII",
            20260105,
            3210,
            3250,
            3180,
            3230,
            500_000_000.0,
            150_000_000,
            0,
        )

        bars = decode_day_bytes(payload, ASSETS[0])

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars.loc[0, "timestamp"], pd.Timestamp("2026-01-05"))
        self.assertAlmostEqual(bars.loc[0, "Close"], 3.23)

    def test_future_rows_do_not_change_historical_events(self) -> None:
        bars = self._synthetic_bars()
        baseline = build_etf_pullback_events(bars)
        self.assertFalse(baseline.empty)
        future = pd.DataFrame(
            {
                "code": ASSETS[0].code,
                "name": ASSETS[0].name,
                "timestamp": pd.date_range(
                    bars["timestamp"].max() + pd.Timedelta(days=1), periods=10, freq="B"
                ),
                "Open": 20.0,
                "High": 21.0,
                "Low": 19.0,
                "Close": 20.0,
                "Amount": 1_000_000_000.0,
                "Volume": 100_000_000.0,
            }
        )
        changed = build_etf_pullback_events(pd.concat([bars, future], ignore_index=True))
        cutoff = bars["timestamp"].max()
        columns = [
            "code",
            "signal_date",
            "entry_date",
            "entry_open",
            "rsi2",
            "return_3d",
            "return_60d",
        ]
        pd.testing.assert_frame_equal(
            baseline.loc[baseline["signal_date"].le(cutoff), columns].reset_index(drop=True),
            changed.loc[changed["signal_date"].le(cutoff), columns].reset_index(drop=True),
        )

    def test_relative_pullback_uses_prior_quantile_and_leader_rank(self) -> None:
        bars = self._relative_bars()

        events = build_etf_relative_pullback_events(bars)

        self.assertFalse(events.empty)
        event = events.iloc[0]
        self.assertEqual(event["code"], ASSETS[0].code)
        self.assertLessEqual(event["return_3d"], event["return_3d_q10"])
        self.assertLessEqual(event["momentum_rank"], 3)
        self.assertGreater(event["entry_date"], event["signal_date"])

        cutoff = bars["timestamp"].max()
        future = bars.copy()
        extra = future.loc[future["code"].eq(ASSETS[0].code)].tail(10).copy()
        extra["timestamp"] = pd.date_range(
            cutoff + pd.Timedelta(days=1), periods=len(extra), freq="B"
        )
        extra[["Open", "High", "Low", "Close"]] = 50.0
        changed = build_etf_relative_pullback_events(
            pd.concat([future, extra], ignore_index=True)
        )
        columns = [
            "code",
            "signal_date",
            "entry_date",
            "entry_open",
            "return_3d",
            "return_3d_q10",
            "return_120d",
            "momentum_rank",
        ]
        pd.testing.assert_frame_equal(
            events.loc[events["signal_date"].le(cutoff), columns].reset_index(drop=True),
            changed.loc[changed["signal_date"].le(cutoff), columns].reset_index(drop=True),
        )

    def test_market_state_context_excludes_current_and_future_rows(self) -> None:
        dates = pd.date_range("2024-01-01", periods=180, freq="B")
        close = [3000.0 + position * 5.0 for position in range(len(dates))]
        target_position = 150
        close[target_position] = close[target_position - 1] * 0.97
        market_index = pd.DataFrame(
            {
                "code": "999999.SH",
                "timestamp": dates,
                "Close": close,
            }
        )
        market_activity = pd.DataFrame(
            {
                "timestamp": dates.astype(str),
                "advance_count": 2000.0,
                "decline_count": 1800.0,
                "limit_down_total": 2.0,
            }
        )

        context = build_market_state_context(market_index, market_activity)

        target = context.loc[context["timestamp"].eq(dates[target_position])].iloc[0]
        returns = pd.Series(close) / pd.Series(close).shift(3) - 1.0
        expected_q20 = returns.iloc[target_position - 126 : target_position].quantile(0.20)
        self.assertAlmostEqual(target["index_return_3d_q20"], expected_q20)
        self.assertTrue(target["market_state_allowed"])

        future_dates = pd.date_range(dates[-1] + pd.Timedelta(days=1), periods=10, freq="B")
        future_index = pd.DataFrame(
            {
                "code": "999999.SH",
                "timestamp": future_dates,
                "Close": 1000.0,
            }
        )
        future_activity = pd.DataFrame(
            {
                "timestamp": future_dates.astype(str),
                "advance_count": 1.0,
                "decline_count": 4000.0,
                "limit_down_total": 500.0,
            }
        )
        changed = build_market_state_context(
            pd.concat([market_index, future_index], ignore_index=True),
            pd.concat([market_activity, future_activity], ignore_index=True),
        )
        pd.testing.assert_frame_equal(
            context,
            changed.loc[changed["timestamp"].le(dates[-1])].reset_index(drop=True),
        )

    def test_market_state_pullback_requires_allowed_state_and_is_future_safe(self) -> None:
        bars = self._relative_bars()
        dates = pd.DatetimeIndex(sorted(pd.to_datetime(bars["timestamp"]).unique()))
        market_state = self._allowed_market_state(dates)

        events = build_etf_market_state_pullback_events(bars, market_state)

        self.assertFalse(events.empty)
        event = events.iloc[0]
        self.assertEqual(event["hypothesis_id"], "broad_etf_market_state_relative_pullback")
        self.assertLessEqual(event["return_3d"], event["return_3d_q20"])
        self.assertLessEqual(event["momentum_rank"], 3)
        self.assertGreaterEqual(event["advance_ratio"], 0.25)

        blocked_state = market_state.copy()
        blocked_state.loc[
            blocked_state["timestamp"].eq(event["signal_date"]), "market_state_allowed"
        ] = False
        blocked = build_etf_market_state_pullback_events(bars, blocked_state)
        blocked_keys = set(zip(blocked["code"], blocked["signal_date"]))
        self.assertNotIn((event["code"], event["signal_date"]), blocked_keys)

        cutoff = dates[-12]
        extra = bars.loc[bars["code"].eq(ASSETS[0].code)].tail(10).copy()
        extra_dates = pd.date_range(dates[-1] + pd.Timedelta(days=1), periods=10, freq="B")
        extra["timestamp"] = extra_dates
        extra[["Open", "High", "Low", "Close"]] = 50.0
        changed = build_etf_market_state_pullback_events(
            pd.concat([bars, extra], ignore_index=True),
            pd.concat(
                [market_state, self._allowed_market_state(extra_dates)],
                ignore_index=True,
            ),
        )
        columns = [
            "code",
            "signal_date",
            "entry_date",
            "entry_open",
            "return_3d",
            "return_3d_q20",
            "return_120d",
            "momentum_rank",
            "index_return_3d_q20",
        ]
        pd.testing.assert_frame_equal(
            events.loc[events["signal_date"].le(cutoff), columns].reset_index(drop=True),
            changed.loc[changed["signal_date"].le(cutoff), columns].reset_index(drop=True),
        )

    @staticmethod
    def _synthetic_bars() -> pd.DataFrame:
        dates = pd.date_range("2025-01-01", periods=235, freq="B")
        close = [3.0 + index * 0.004 for index in range(225)]
        close += [3.90, 3.86, 3.80, 3.75, 3.72, 3.76, 3.80, 3.84, 3.88, 3.92]
        return pd.DataFrame(
            {
                "code": ASSETS[0].code,
                "name": ASSETS[0].name,
                "timestamp": dates,
                "Open": [value * 0.999 for value in close],
                "High": [value * 1.005 for value in close],
                "Low": [value * 0.995 for value in close],
                "Close": close,
                "Amount": 1_000_000_000.0,
                "Volume": 100_000_000.0,
            }
        )

    @staticmethod
    def _relative_bars() -> pd.DataFrame:
        dates = pd.date_range("2024-01-01", periods=330, freq="B")
        rows = []
        for asset_index, asset in enumerate(ASSETS[:4]):
            slope = 0.006 - asset_index * 0.001
            close = [3.0 + index * slope for index in range(len(dates))]
            if asset_index == 0:
                for position in range(300, 303):
                    close[position] = close[position - 1] * 0.975
                for position in range(303, len(close)):
                    close[position] = close[position - 1] * 1.01
            rows.append(
                pd.DataFrame(
                    {
                        "code": asset.code,
                        "name": asset.name,
                        "timestamp": dates,
                        "Open": [value * 0.999 for value in close],
                        "High": [value * 1.005 for value in close],
                        "Low": [value * 0.995 for value in close],
                        "Close": close,
                        "Amount": 1_000_000_000.0,
                        "Volume": 100_000_000.0,
                    }
                )
            )
        return pd.concat(rows, ignore_index=True)

    @staticmethod
    def _allowed_market_state(dates: pd.DatetimeIndex) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp": dates,
                "index_close": 3500.0,
                "index_ma120": 3400.0,
                "index_return_3d": -0.02,
                "index_return_3d_q20": -0.01,
                "advance_ratio": 0.50,
                "limit_down_total": 2.0,
                "limit_down_q80": 5.0,
                "market_state_allowed": True,
            }
        )


if __name__ == "__main__":
    unittest.main()
