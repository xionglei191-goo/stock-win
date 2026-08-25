from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.chan_pullback_research import (
    ChanAnchor,
    assess_development,
    build_chan_pullback_events,
    evaluate_pullback_setup,
    protocol_manifest,
)


class ChanPullbackResearchTests(unittest.TestCase):
    def test_protocol_keeps_later_windows_sealed(self) -> None:
        protocol = protocol_manifest()
        self.assertEqual(protocol["research_status"], "development_only")
        self.assertEqual(protocol["execution"]["target_weight"], 0.10)
        self.assertEqual(protocol["execution"]["maximum_positions"], 3)
        self.assertTrue(
            protocol["data"]["replication_and_holdout_snapshot_ids_intentionally_absent"]
        )
        self.assertNotIn("bt_1f2378fe2c984617911770ccb742a05e", str(protocol))

    def test_pullback_setup_requires_center_touch_and_reclaim(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=124)
        history = pd.DataFrame(
            {
                "code": "600001.SH",
                "name": "Sample",
                "timestamp": dates,
                "Open": np.full(124, 10.0),
                "High": np.full(124, 10.2),
                "Low": np.full(124, 9.9),
                "Close": np.full(124, 10.0),
                "raw_Low": np.full(124, 9.9),
                "raw_Close": np.full(124, 10.0),
                "volume_ratio": np.full(124, 0.5),
                "return_60d": np.full(124, 0.20),
                "amount_20d": np.full(124, 100_000_000.0),
            }
        )
        history.loc[120:123, "Close"] = [10.5, 10.8, 10.0, 10.2]
        history.loc[120:123, "Low"] = [10.2, 10.5, 9.95, 9.98]
        history.loc[120:123, "raw_Low"] = [10.2, 10.5, 9.95, 9.98]
        history.loc[123, "raw_Close"] = 10.2
        anchor = ChanAnchor(120, dates[120], 9.0, 9.8)
        setup = evaluate_pullback_setup(history, 123, anchor)
        self.assertIsNotNone(setup)
        self.assertGreaterEqual(float(setup["stop_price"]), 10.2 * 0.95)

        too_deep = history.copy()
        too_deep.loc[122, "Low"] = 8.5
        self.assertIsNone(evaluate_pullback_setup(too_deep, 123, anchor))

    def test_next_open_execution_and_gap_cancellation(self) -> None:
        front, raw, market, dates = synthetic_pullback_market()

        def detector(_: pd.DataFrame, signal_position: int) -> ChanAnchor:
            return ChanAnchor(
                signal_position - 3,
                dates[signal_position - 3],
                10.5,
                12.0,
            )

        events = build_chan_pullback_events(
            front,
            raw,
            market,
            {"600001.SH": "Sample"},
            start_date=str(dates[150].date()),
            end_date=str(dates[150].date()),
            anchor_detector=detector,
        )
        self.assertEqual(len(events), 1)
        event = events.iloc[0]
        self.assertTrue(bool(event["selected"]))
        self.assertTrue(bool(event["executable"]))
        self.assertEqual(pd.Timestamp(event["entry_date"]), dates[151])
        self.assertEqual(str(event["exit_reason"]), "MAX_HOLDING")

        changed = raw.copy()
        changed.loc[changed["timestamp"].eq(dates[151]), "Open"] = (
            float(event["raw_signal_close"]) * 1.06
        )
        blocked = build_chan_pullback_events(
            front,
            changed,
            market,
            {"600001.SH": "Sample"},
            start_date=str(dates[150].date()),
            end_date=str(dates[150].date()),
            anchor_detector=detector,
        ).iloc[0]
        self.assertTrue(bool(blocked["blocked_entry_gap"]))
        self.assertFalse(bool(blocked["executable"]))

    def test_appended_future_does_not_change_historical_signal(self) -> None:
        front, raw, market, dates = synthetic_pullback_market()

        def detector(_: pd.DataFrame, signal_position: int) -> ChanAnchor:
            return ChanAnchor(signal_position - 3, dates[signal_position - 3], 10.5, 12.0)

        kwargs = {
            "names": {"600001.SH": "Sample"},
            "start_date": str(dates[150].date()),
            "end_date": str(dates[150].date()),
            "anchor_detector": detector,
        }
        baseline = build_chan_pullback_events(front, raw, market, **kwargs)
        future_dates = pd.bdate_range(dates[-1] + pd.Timedelta(days=1), periods=5)
        last_front = front.iloc[-1]
        last_raw = raw.iloc[-1]
        future_front = pd.DataFrame(
            {
                "code": "600001.SH",
                "timestamp": future_dates,
                "Open": float(last_front["Close"]) * 0.5,
                "High": float(last_front["Close"]) * 0.6,
                "Low": float(last_front["Close"]) * 0.4,
                "Close": float(last_front["Close"]) * 0.5,
                "Volume": 1_000_000.0,
                "Amount": 20_000_000.0,
            }
        )
        future_raw = future_front.copy()
        future_raw.loc[:, ["Open", "High", "Low", "Close"]] = (
            future_raw.loc[:, ["Open", "High", "Low", "Close"]] * 1.1
        )
        future_market = pd.DataFrame(
            {"code": "999999.SH", "timestamp": future_dates, "Close": 1000.0}
        )
        changed = build_chan_pullback_events(
            pd.concat([front, future_front], ignore_index=True),
            pd.concat([raw, future_raw], ignore_index=True),
            pd.concat([market, future_market], ignore_index=True),
            **kwargs,
        )
        columns = ["code", "signal_date", "anchor_date", "score", "daily_rank", "selected"]
        pd.testing.assert_frame_equal(baseline[columns], changed[columns])

    def test_passing_development_still_does_not_open_later_windows(self) -> None:
        reports = []
        for label in ("dev_2021_2022", "dev_2022_2023", "dev_2023_2024"):
            reports.append(
                {
                    "window": {"label": label, "role": "DEVELOPMENT"},
                    "portfolio_trades": 40,
                    "portfolio_annualized_return": 0.10,
                    "portfolio_total_return": 0.10,
                    "median_trade_return": 0.01,
                    "ex_top3_contribution": 0.02,
                    "portfolio_max_drawdown": -0.05,
                    "fill_rate": 0.90,
                }
            )
        decision = assess_development(reports)
        self.assertEqual(decision["decision"], "REQUIRE_SURVIVOR_AUDIT")
        self.assertFalse(decision["replication_opened"])
        self.assertFalse(decision["holdout_opened"])

    def test_failed_first_window_stops_without_opening_other_development_windows(self) -> None:
        report = {
            "window": {"label": "dev_2021_2022", "role": "DEVELOPMENT"},
            "portfolio_trades": 66,
            "portfolio_annualized_return": -0.07,
            "portfolio_total_return": -0.073,
            "median_trade_return": -0.034,
            "ex_top3_contribution": -0.13,
            "portfolio_max_drawdown": -0.112,
            "fill_rate": 0.92,
        }
        decision = assess_development([report])
        self.assertEqual(decision["decision"], "REJECT")
        self.assertTrue(decision["early_stopped"])
        self.assertEqual(
            decision["unopened_development_windows"],
            ["dev_2022_2023", "dev_2023_2024"],
        )
        self.assertFalse(decision["replication_opened"])
        self.assertFalse(decision["holdout_opened"])


def synthetic_pullback_market() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    dates = pd.bdate_range("2023-01-02", periods=170)
    close = np.linspace(8.0, 12.0, len(dates))
    close[148] = 13.0
    close[149] = 12.0
    close[150] = 12.2
    close[151:] = 12.25
    open_price = close.copy()
    open_price[150] = 12.0
    open_price[151] = close[150] * 1.01
    volume = np.full(len(dates), 10_000_000.0)
    volume[150] = 5_000_000.0
    front = pd.DataFrame(
        {
            "code": "600001.SH",
            "timestamp": dates,
            "Open": open_price,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": volume,
            "Amount": 150_000_000.0,
        }
    )
    raw = front.copy()
    market = pd.DataFrame(
        {
            "code": "999999.SH",
            "timestamp": dates,
            "Close": np.linspace(3000.0, 4000.0, len(dates)),
        }
    )
    return front, raw, market, dates


if __name__ == "__main__":
    unittest.main()
