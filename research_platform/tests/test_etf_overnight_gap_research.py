from __future__ import annotations

import hashlib
import json
import unittest

import numpy as np
import pandas as pd

from research_platform.etf_overnight_gap_research import (
    FROZEN_PROTOCOL_SHA256,
    assess_development,
    build_overnight_gap_events,
    protocol_manifest,
)


class EtfOvernightGapResearchTests(unittest.TestCase):
    def test_protocol_manifest_matches_frozen_hash(self) -> None:
        payload = json.dumps(protocol_manifest(), ensure_ascii=False, indent=2).encode("utf-8")
        self.assertEqual(hashlib.sha256(payload).hexdigest(), FROZEN_PROTOCOL_SHA256)

    def test_opening_gap_uses_prior_close_and_exit_uses_next_open(self) -> None:
        bars, market, dates = synthetic_market()
        events = build_overnight_gap_events(bars, market)
        selected = events.loc[
            events["selected"] & events["entry_date"].eq(dates[170])
        ].iloc[0]
        self.assertEqual(selected["code"], "510001.SH")
        self.assertAlmostEqual(float(selected["entry_gap"]), -0.02, places=12)
        self.assertEqual(pd.Timestamp(selected["signal_date"]), dates[169])
        self.assertEqual(pd.Timestamp(selected["exit_date"]), dates[171])
        self.assertTrue(bool(selected["executable"]))
        self.assertGreater(int(selected["quantity"]), 0)

    def test_correlated_opening_candidates_are_deduplicated(self) -> None:
        bars, market, dates = synthetic_market(correlated_second=True)
        events = build_overnight_gap_events(bars, market)
        day = events.loc[
            events["entry_date"].eq(dates[170]) & events["gap_qualified"]
        ]
        self.assertEqual(int(day["selected"].sum()), 1)
        self.assertEqual(int(day["blocked_correlation"].sum()), 1)
        self.assertEqual(day.loc[day["selected"], "code"].iloc[0], "510002.SH")

    def test_gap_outside_frozen_band_is_not_selected(self) -> None:
        bars, market, dates = synthetic_market()
        changed = bars.copy()
        signal_close = float(
            changed.loc[
                changed["code"].eq("510001.SH") & changed["timestamp"].eq(dates[169]),
                "Close",
            ].iloc[0]
        )
        changed.loc[
            changed["code"].eq("510001.SH") & changed["timestamp"].eq(dates[170]),
            "Open",
        ] = signal_close * 0.995
        events = build_overnight_gap_events(changed, market)
        event = events.loc[
            events["code"].eq("510001.SH") & events["entry_date"].eq(dates[170])
        ].iloc[0]
        self.assertTrue(bool(event["blocked_entry_gap"]))
        self.assertFalse(bool(event["gap_qualified"]))
        self.assertFalse(bool(event["selected"]))

    def test_future_rows_do_not_change_historical_selection(self) -> None:
        bars, market, dates = synthetic_market()
        baseline = build_overnight_gap_events(bars, market)
        future_dates = pd.bdate_range(dates[-1] + pd.Timedelta(days=1), periods=5)
        additions = []
        for code, group in bars.groupby("code", sort=False):
            last = group.iloc[-1]
            additions.append(
                pd.DataFrame(
                    {
                        "code": code,
                        "name": last["name"],
                        "timestamp": future_dates,
                        "Open": [float(last["Close"]) * 0.5] * len(future_dates),
                        "High": [float(last["Close"]) * 0.6] * len(future_dates),
                        "Low": [float(last["Close"]) * 0.4] * len(future_dates),
                        "Close": [float(last["Close"]) * 0.5] * len(future_dates),
                        "Amount": [500_000_000.0] * len(future_dates),
                        "Volume": [50_000_000.0] * len(future_dates),
                    }
                )
            )
        future_market = pd.concat(
            [
                market,
                pd.DataFrame(
                    {
                        "timestamp": future_dates,
                        "Close": [float(market["Close"].iloc[-1]) * 0.8]
                        * len(future_dates),
                    }
                ),
            ],
            ignore_index=True,
        )
        changed = build_overnight_gap_events(
            pd.concat([bars, *additions], ignore_index=True), future_market
        )
        columns = [
            "code",
            "signal_date",
            "entry_date",
            "entry_gap",
            "selected",
            "daily_rank",
            "blocked_correlation",
            "blocked_daily_capacity",
        ]
        cutoff = dates[-1]
        pd.testing.assert_frame_equal(
            baseline.loc[baseline["entry_date"].le(cutoff), columns].reset_index(drop=True),
            changed.loc[changed["entry_date"].le(cutoff), columns].reset_index(drop=True),
        )

    def test_double_cost_reduces_trade_return_without_stamp_duty(self) -> None:
        bars, market, dates = synthetic_market()
        base = build_overnight_gap_events(bars, market, execution_cost_multiplier=1.0)
        stress = build_overnight_gap_events(bars, market, execution_cost_multiplier=2.0)
        base_event = base.loc[
            base["selected"] & base["entry_date"].eq(dates[170])
        ].iloc[0]
        stress_event = stress.loc[
            stress["selected"] & stress["entry_date"].eq(dates[170])
        ].iloc[0]
        self.assertGreater(float(base_event["net_return"]), float(stress_event["net_return"]))
        self.assertEqual(protocol_manifest()["execution"]["base_costs"]["stamp_duty"], 0.0)

    def test_passing_development_still_keeps_replication_sealed(self) -> None:
        base = []
        stress = []
        for label in ("dev_2021_2022", "dev_2022_2023", "dev_2023_2024"):
            common = {
                "window": {"label": label, "role": "DEVELOPMENT"},
                "portfolio_trades": 40,
                "portfolio_annualized_return": 0.10,
                "portfolio_total_return": 0.10,
                "median_trade_return": 0.01,
                "ex_top3_contribution": 0.02,
                "portfolio_max_drawdown": -0.05,
                "fill_rate": 0.90,
            }
            base.append(common)
            stress.append({**common, "portfolio_annualized_return": 0.08})
        decision = assess_development(base, stress)
        self.assertEqual(decision["decision"], "REQUIRE_SURVIVOR_AUDIT")
        self.assertTrue(decision["survivor_audit_required"])
        self.assertFalse(decision["replication_opened"])
        self.assertFalse(decision["holdout_opened"])


def synthetic_market(
    *, correlated_second: bool = False
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    sessions = 190
    dates = pd.bdate_range("2020-01-01", periods=sessions)
    frames = []
    for index in range(10):
        code = f"510{index + 1:03d}.SH"
        if index == 0 or (index == 1 and correlated_second):
            close = np.linspace(10.0, 20.0, sessions)
        else:
            close = np.linspace(10.0, 13.0 - index * 0.05, sessions)
        open_price = close.copy()
        if index == 0 or (index == 1 and correlated_second):
            open_price[170] = close[169] * 0.98
            open_price[171] = close[170] * 1.01
        high = np.maximum(open_price, close) * 1.01
        low = np.minimum(open_price, close) * 0.99
        frames.append(
            pd.DataFrame(
                {
                    "code": code,
                    "name": f"ETF {index}",
                    "timestamp": dates,
                    "Open": open_price,
                    "High": high,
                    "Low": low,
                    "Close": close,
                    "Amount": 100_000_000.0 + index * 10_000_000.0,
                    "Volume": 10_000_000.0,
                }
            )
        )
    market = pd.DataFrame(
        {"timestamp": dates, "Close": np.linspace(3000.0, 4000.0, sessions)}
    )
    return pd.concat(frames, ignore_index=True), market, dates


if __name__ == "__main__":
    unittest.main()
