from __future__ import annotations

import struct
import unittest

import pandas as pd

from research_platform.intraday_pullback_research import (
    build_daily_watchlist,
    build_intraday_reversal_events,
    build_v9_intraday_entry_events,
    build_v9_staged_entry_events,
    build_v9_trade_pairs,
    decode_lc5_bytes,
    select_chronological_entries,
)


CODE = "000001.SZ"


def daily_bars(*, append_future: bool = False) -> pd.DataFrame:
    index = pd.date_range("2025-09-01", periods=86, freq="B")
    close = [10.0 + offset * 0.035 for offset in range(70)]
    close += [12.35, 12.30, 12.25, 12.20, 12.15, 12.18]
    close += [12.20, 12.25, 12.30, 12.35, 12.40, 12.45, 12.50, 12.55, 12.60, 12.65]
    volume = [8_000_000.0] * len(index)
    open_price = [value * 0.998 for value in close]
    frame = pd.DataFrame(
        {
            "code": CODE,
            "timestamp": index,
            "Open": open_price,
            "High": [value * 1.005 for value in close],
            "Low": [value * 0.995 for value in close],
            "Close": close,
            "Volume": volume,
            "Amount": [value * quantity for value, quantity in zip(close, volume)],
        }
    )
    if append_future:
        extra_index = pd.date_range(index[-1] + pd.Timedelta(days=1), periods=5, freq="B")
        extra = pd.DataFrame(
            {
                "code": CODE,
                "timestamp": extra_index,
                "Open": 20.0,
                "High": 21.0,
                "Low": 19.0,
                "Close": 20.0,
                "Volume": 20_000_000.0,
                "Amount": 400_000_000.0,
            }
        )
        frame = pd.concat([frame, extra], ignore_index=True)
    return frame


def intraday_bars() -> pd.DataFrame:
    morning = pd.date_range("2026-01-06 09:35", "2026-01-06 11:30", freq="5min")
    afternoon = pd.date_range("2026-01-06 13:05", "2026-01-06 15:00", freq="5min")
    timestamps = morning.append(afternoon)
    close = [9.82, 9.76, 9.70, 9.72, 9.75, 9.80, 9.85, 9.86]
    close += [9.86] * (len(timestamps) - len(close))
    open_price = close.copy()
    open_price[0] = 9.80
    open_price[7] = 9.86
    low = [min(value, open_value) - 0.01 for value, open_value in zip(close, open_price)]
    low[2] = 9.69
    volume = [100_000.0] * len(timestamps)
    return pd.DataFrame(
        {
            "code": CODE,
            "timestamp": timestamps,
            "Open": open_price,
            "High": [max(value, open_value) + 0.01 for value, open_value in zip(close, open_price)],
            "Low": low,
            "Close": close,
            "Volume": volume,
            "Amount": [value * quantity for value, quantity in zip(close, volume)],
        }
    )


def watchlist_row() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": CODE,
                "name": "sample",
                "signal_date": pd.Timestamp("2026-01-05"),
                "entry_date": pd.Timestamp("2026-01-06"),
                "raw_close": 10.0,
                "limit_ratio": 0.10,
                "turnover_20d": 100_000_000.0,
                "return_60d": 0.20,
                "return_5d": -0.05,
                "market_gate": True,
                "market_phase": "FERMENT",
                "market_score": 0.70,
                "market_regime": "NORMAL",
                "exit_open_1d": 10.20,
                "exit_date_1d": pd.Timestamp("2026-01-07"),
                "exit_open_3d": 10.30,
                "exit_date_3d": pd.Timestamp("2026-01-09"),
                "exit_open_5d": 10.40,
                "exit_date_5d": pd.Timestamp("2026-01-13"),
            }
        ]
    )


class IntradayPullbackResearchTests(unittest.TestCase):
    def test_lc5_decoder_reads_timestamp_and_ohlcv(self) -> None:
        encoded_date = (2026 - 2004) * 2048 + 1 * 100 + 5
        payload = struct.pack(
            "<HHfffffII",
            encoded_date,
            9 * 60 + 35,
            10.0,
            10.2,
            9.9,
            10.1,
            1_010_000.0,
            100_000,
            0,
        )

        bars = decode_lc5_bytes(payload, CODE)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars.loc[0, "timestamp"], pd.Timestamp("2026-01-05 09:35"))
        self.assertAlmostEqual(bars.loc[0, "Close"], 10.1, places=5)
        self.assertEqual(bars.loc[0, "Volume"], 100_000.0)

    def test_daily_watchlist_features_do_not_use_appended_future(self) -> None:
        baseline_bars = daily_bars()
        future_bars = daily_bars(append_future=True)
        baseline = build_daily_watchlist(
            baseline_bars, baseline_bars, {CODE: "sample"}
        )
        future = build_daily_watchlist(
            future_bars, future_bars, {CODE: "sample"}
        )
        cutoff = baseline_bars["timestamp"].iloc[75]
        columns = ["code", "signal_date", "return_60d", "return_5d", "ma20", "ma60"]
        pd.testing.assert_frame_equal(
            baseline.loc[baseline["signal_date"].le(cutoff), columns].reset_index(drop=True),
            future.loc[future["signal_date"].le(cutoff), columns].reset_index(drop=True),
        )

    def test_intraday_confirmation_enters_at_next_bar_open(self) -> None:
        bars = intraday_bars()
        events = build_intraday_reversal_events(watchlist_row(), bars)

        self.assertEqual(len(events), 1)
        event = events.iloc[0]
        self.assertEqual(event["confirmation_timestamp"], pd.Timestamp("2026-01-06 10:00"))
        self.assertEqual(event["entry_timestamp"], pd.Timestamp("2026-01-06 10:05"))
        self.assertAlmostEqual(event["entry_price"], 9.85)
        self.assertGreaterEqual(event["rebound_from_low"], 0.01)
        self.assertGreater(event["net_return_1d"], 0.0)

        changed_future = bars.copy()
        changed_future.loc[
            changed_future["timestamp"].gt(pd.Timestamp("2026-01-06 10:05")),
            ["Open", "High", "Low", "Close"],
        ] = 20.0
        future_events = build_intraday_reversal_events(watchlist_row(), changed_future)
        columns = [
            "code",
            "confirmation_timestamp",
            "entry_timestamp",
            "entry_price",
            "session_low_return",
            "rebound_from_low",
            "vwap_spread",
        ]
        pd.testing.assert_frame_equal(events[columns], future_events[columns])

    def test_capacity_is_allocated_in_observable_time_order(self) -> None:
        base = watchlist_row().iloc[0].to_dict()
        rows = []
        for index, (code, timestamp, score) in enumerate(
            [
                ("000001.SZ", "2026-01-06 10:05", 0.40),
                ("000002.SZ", "2026-01-06 10:10", 0.30),
                ("000003.SZ", "2026-01-06 10:15", 0.20),
                ("000004.SZ", "2026-01-06 14:00", 0.99),
            ]
        ):
            row = dict(base)
            row.update(
                {
                    "code": code,
                    "entry_timestamp": pd.Timestamp(timestamp),
                    "score": score,
                    "executable": True,
                    "net_return_1d": 0.01 + index * 0.001,
                }
            )
            rows.append(row)

        accepted = select_chronological_entries(pd.DataFrame(rows))

        self.assertEqual(
            accepted["code"].tolist(),
            ["000001.SZ", "000002.SZ", "000003.SZ"],
        )

    def test_v9_trades_are_paired_and_raw_prices_are_recovered(self) -> None:
        trades = pd.DataFrame(
            [
                {
                    "rowid": 1,
                    "timestamp": "2026-01-06 09:30:00",
                    "code": CODE,
                    "side": "BUY",
                    "quantity": 1000,
                    "price": 10.01,
                    "fees": 5.0,
                    "pnl": None,
                    "reason": "ENTRY",
                    "evidence": '{"entry_price": 9.80}',
                },
                {
                    "rowid": 2,
                    "timestamp": "2026-01-09 09:30:00",
                    "code": CODE,
                    "side": "SELL",
                    "quantity": 1000,
                    "price": 10.989,
                    "fees": 10.0,
                    "pnl": 969.0,
                    "reason": "TIME_EXIT",
                    "evidence": "{}",
                },
            ]
        )

        pairs = build_v9_trade_pairs(trades)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs.loc[0, "pair_id"], "2026-01-06:000001.SZ")
        self.assertAlmostEqual(pairs.loc[0, "raw_entry_open"], 10.0)
        self.assertAlmostEqual(pairs.loc[0, "raw_exit_open"], 11.0)
        self.assertAlmostEqual(pairs.loc[0, "signal_close"], 9.8)

    def test_v9_pullback_entry_uses_only_confirmation_and_next_bar(self) -> None:
        bars = intraday_bars()
        bars.loc[:, "Open"] = bars["Open"] + 0.20
        bars.loc[:, "High"] = bars["High"] + 0.20
        bars.loc[:, "Low"] = bars["Low"] + 0.20
        bars.loc[:, "Close"] = bars["Close"] + 0.20
        bars.loc[2, "Low"] = 9.80
        bars.loc[:, "Amount"] = bars["Close"] * bars["Volume"]
        pairs = pd.DataFrame(
            [
                {
                    "pair_id": "2026-01-06:000001.SZ",
                    "code": CODE,
                    "entry_date": pd.Timestamp("2026-01-06"),
                    "raw_entry_open": 10.0,
                    "raw_exit_open": 10.8,
                    "exit_date": pd.Timestamp("2026-01-09"),
                    "realized_net_return": 0.075,
                    "exit_reason": "TIME_EXIT",
                }
            ]
        )

        events = build_v9_intraday_entry_events(pairs, bars)

        self.assertEqual(len(events), 1)
        event = events.iloc[0]
        self.assertEqual(event["confirmation_timestamp"], pd.Timestamp("2026-01-06 10:00"))
        self.assertEqual(
            event["alternative_entry_timestamp"], pd.Timestamp("2026-01-06 10:05")
        )
        self.assertAlmostEqual(event["alternative_raw_entry"], 10.05)

        changed_future = bars.copy()
        changed_future.loc[
            changed_future["timestamp"].gt(pd.Timestamp("2026-01-06 10:05")),
            ["Open", "High", "Low", "Close"],
        ] = 20.0
        future_events = build_v9_intraday_entry_events(pairs, changed_future)
        columns = [
            "pair_id",
            "confirmation_timestamp",
            "alternative_entry_timestamp",
            "alternative_raw_entry",
            "pullback_from_open",
            "rebound_from_low",
            "vwap_spread",
        ]
        pd.testing.assert_frame_equal(events[columns], future_events[columns])

    def test_v9_staged_entry_falls_back_at_1435(self) -> None:
        bars = intraday_bars()
        pairs = pd.DataFrame(
            [
                {
                    "pair_id": "2026-01-06:000001.SZ",
                    "code": CODE,
                    "entry_date": pd.Timestamp("2026-01-06"),
                    "raw_entry_open": 9.80,
                    "quantity": 1000,
                    "raw_exit_open": 10.20,
                    "exit_date": pd.Timestamp("2026-01-07"),
                    "realized_net_return": 0.03,
                    "exit_reason": "TIME_EXIT",
                }
            ]
        )

        staged = build_v9_staged_entry_events(pairs, bars, pd.DataFrame())

        self.assertEqual(len(staged), 1)
        self.assertFalse(staged.loc[0, "pullback_triggered"])
        self.assertTrue(staged.loc[0, "completed"])
        self.assertEqual(
            staged.loc[0, "second_entry_timestamp"],
            pd.Timestamp("2026-01-06 14:35"),
        )
        self.assertAlmostEqual(staged.loc[0, "second_raw_entry"], 9.86)


if __name__ == "__main__":
    unittest.main()
