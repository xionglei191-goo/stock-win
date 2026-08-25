from __future__ import annotations

import unittest

import pandas as pd

from research_platform.v9_limit_pullback_research import (
    build_anchor_limit_events,
    build_anchor_reclaim_events,
)


CODE = "000001.SZ"


def pair() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "pair_id": "2026-01-06:000001.SZ",
                "code": CODE,
                "entry_date": pd.Timestamp("2026-01-06"),
                "raw_entry_open": 10.40,
                "signal_close": 10.00,
                "quantity": 1000,
                "exit_date": pd.Timestamp("2026-01-12"),
                "raw_exit_open": 11.00,
                "realized_net_return": 0.05,
            }
        ]
    )


def bars(*, append_future: bool = False) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "code": CODE,
            "timestamp": pd.to_datetime(
                ["2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
            ),
            "Open": [10.40, 10.30, 10.20, 9.80],
            "High": [10.60, 10.50, 10.40, 10.00],
            "Low": [10.10, 9.95, 10.05, 9.70],
            "Close": [10.30, 10.20, 10.10, 9.90],
        }
    )
    if append_future:
        future = pd.DataFrame(
            {
                "code": CODE,
                "timestamp": [pd.Timestamp("2026-01-13")],
                "Open": [1.0],
                "High": [1.0],
                "Low": [1.0],
                "Close": [1.0],
            }
        )
        frame = pd.concat([frame, future], ignore_index=True)
    return frame


class V9LimitPullbackResearchTests(unittest.TestCase):
    def test_limit_fills_on_first_intraday_touch(self) -> None:
        events = build_anchor_limit_events(pair(), bars())

        self.assertEqual(len(events), 1)
        self.assertTrue(events.loc[0, "filled"])
        self.assertEqual(events.loc[0, "fill_type"], "INTRADAY_LIMIT_TOUCH")
        self.assertEqual(
            events.loc[0, "alternative_entry_date"], pd.Timestamp("2026-01-07")
        )
        self.assertAlmostEqual(events.loc[0, "alternative_raw_entry"], 10.0)

    def test_open_below_limit_gets_the_better_open(self) -> None:
        frame = bars()
        frame.loc[0, ["Open", "Low"]] = [9.90, 9.80]

        events = build_anchor_limit_events(pair(), frame)

        self.assertEqual(events.loc[0, "fill_type"], "OPEN_BELOW_LIMIT")
        self.assertAlmostEqual(events.loc[0, "alternative_raw_entry"], 9.90)

    def test_order_expires_after_three_sessions_and_before_exit(self) -> None:
        frame = bars()
        frame.loc[:2, "Low"] = 10.10

        events = build_anchor_limit_events(pair(), frame)

        self.assertFalse(events.loc[0, "filled"])
        self.assertEqual(events.loc[0, "fill_type"], "EXPIRED")
        self.assertEqual(events.loc[0, "observed_sessions"], 3)

    def test_appended_future_does_not_change_historical_fill(self) -> None:
        baseline = build_anchor_limit_events(pair(), bars())
        future = build_anchor_limit_events(pair(), bars(append_future=True))
        columns = [
            "pair_id",
            "filled",
            "alternative_entry_date",
            "alternative_raw_entry",
            "fill_type",
            "observed_sessions",
            "alternative_net_return",
        ]
        pd.testing.assert_frame_equal(baseline[columns], future[columns])

    def test_anchor_reclaim_enters_next_open_and_exits_after_three_sessions(self) -> None:
        frame = pd.DataFrame(
            {
                "code": CODE,
                "timestamp": pd.date_range("2026-01-06", periods=8, freq="B"),
                "Open": [10.2, 9.8, 9.9, 10.2, 10.3, 10.4, 10.5, 10.6],
                "High": [10.3, 10.0, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7],
                "Low": [10.1, 9.7, 9.8, 10.1, 10.2, 10.3, 10.4, 10.5],
                "Close": [10.15, 9.9, 10.1, 10.25, 10.35, 10.45, 10.55, 10.65],
            }
        )

        events = build_anchor_reclaim_events(pair(), frame)

        self.assertTrue(events.loc[0, "confirmed"])
        self.assertEqual(events.loc[0, "touch_date"], pd.Timestamp("2026-01-07"))
        self.assertEqual(
            events.loc[0, "confirmation_date"], pd.Timestamp("2026-01-08")
        )
        self.assertEqual(events.loc[0, "entry_date"], pd.Timestamp("2026-01-09"))
        self.assertEqual(events.loc[0, "exit_date_3d"], pd.Timestamp("2026-01-14"))
        self.assertTrue(events.loc[0, "executable"])

    def test_anchor_reclaim_does_not_use_appended_future(self) -> None:
        frame = pd.DataFrame(
            {
                "code": CODE,
                "timestamp": pd.date_range("2026-01-06", periods=9, freq="B"),
                "Open": [9.9, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8],
                "High": [10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0],
                "Low": [9.8, 10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7],
                "Close": [10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9],
            }
        )
        baseline = build_anchor_reclaim_events(pair(), frame)
        changed = frame.copy()
        changed.loc[
            changed["timestamp"].gt(pd.Timestamp("2026-01-12")),
            ["Open", "High", "Low", "Close"],
        ] = 50.0
        future = build_anchor_reclaim_events(pair(), changed)
        columns = [
            "pair_id",
            "touch_date",
            "confirmation_date",
            "entry_date",
            "entry_price",
        ]
        pd.testing.assert_frame_equal(baseline[columns], future[columns])


if __name__ == "__main__":
    unittest.main()
