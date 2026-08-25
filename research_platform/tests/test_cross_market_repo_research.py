from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.cross_market_repo_research import (
    GC001_ELIGIBLE_FROM,
    _slice_day_payload,
    protocol_manifest,
    simulate_cross_market_sweep,
)
from research_platform.etf_pullback_research import DAY_DTYPE
from research_platform.reverse_repo_tenor_ladder_research import (
    RepoTenor,
    decode_tenor_day_bytes,
)


def _rates(dates: pd.DatetimeIndex, r001: float, gc001: float) -> pd.DataFrame:
    rows = []
    for code, rate in (("131810.SZ", r001), ("204001.SH", gc001)):
        for date_value in dates:
            rows.append(
                {
                    "timestamp": date_value,
                    "code": code,
                    "tenor_days": 1,
                    "Close": rate,
                    "Low": rate,
                    "Volume": 1_000.0,
                }
            )
    return pd.DataFrame(rows)


class CrossMarketRepoResearchTests(unittest.TestCase):
    def test_snapshot_slice_excludes_invalid_rows_before_research_start(self) -> None:
        records = np.zeros(2, dtype=DAY_DTYPE)
        records["date"] = [20070925, 20210401]
        records["open"] = [150_000, 10_000]
        records["high"] = [1_002_600, 11_000]
        records["low"] = [150_000, 9_000]
        records["close"] = [300_000, 10_500]
        records["amount"] = [1_000_000.0, 1_000_000.0]
        records["volume"] = [1_000, 1_000]
        payload = _slice_day_payload(
            records.tobytes(), pd.Timestamp("2021-04-01"), pd.Timestamp("2026-08-10")
        )
        frame = decode_tenor_day_bytes(
            payload, RepoTenor("204001.SH", "GC001", "sh204001", 1)
        )
        self.assertEqual(len(frame), 1)
        self.assertEqual(str(frame.loc[0, "timestamp"].date()), "2021-04-01")

    def test_gc001_is_blocked_before_historical_eligibility(self) -> None:
        dates = pd.to_datetime(
            ["2022-05-12", "2022-05-13", "2022-05-16", "2022-05-17"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 4, "cash": [50_000.0] * 4}
        )
        result = simulate_cross_market_sweep(
            equity,
            _rates(dates, r001=1.0, gc001=9.0),
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        frame = result["equity"]
        self.assertEqual(frame.loc[0, "repo_code"], "131810.SZ")
        self.assertEqual(frame.loc[1, "repo_code"], "131810.SZ")

    def test_gc001_can_be_selected_from_effective_date(self) -> None:
        dates = pd.to_datetime(
            ["2022-05-16", "2022-05-17", "2022-05-18", "2022-05-19"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 4, "cash": [50_000.0] * 4}
        )
        result = simulate_cross_market_sweep(
            equity,
            _rates(dates, r001=1.0, gc001=2.0),
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertEqual(result["equity"].loc[0, "repo_code"], "204001.SH")
        self.assertGreater(result["gc001_selections"], 0)

    def test_daily_low_stress_selects_higher_lower_bound(self) -> None:
        dates = pd.to_datetime(
            ["2022-05-16", "2022-05-17", "2022-05-18"]
        )
        rates = _rates(dates, r001=1.0, gc001=2.0)
        rates.loc[rates["code"].eq("131810.SZ"), "Low"] = 1.5
        rates.loc[rates["code"].eq("204001.SH"), "Low"] = 1.2
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 3, "cash": [50_000.0] * 3}
        )
        result = simulate_cross_market_sweep(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Low",
            commission_rate=0.0,
        )
        self.assertEqual(result["equity"].loc[0, "repo_code"], "131810.SZ")

    def test_appended_future_quote_does_not_change_history(self) -> None:
        dates = pd.to_datetime(
            ["2022-05-16", "2022-05-17", "2022-05-18"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 3, "cash": [50_000.0] * 3}
        )
        rates = _rates(dates, r001=1.0, gc001=2.0)
        first = simulate_cross_market_sweep(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        future = rates.copy()
        future.loc[future["timestamp"].eq(dates[-1]), "Close"] = 99.0
        second = simulate_cross_market_sweep(
            equity,
            future,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
        )
        self.assertEqual(first["net_interest"], second["net_interest"])

    def test_protocol_freezes_effective_date_and_never_promotes(self) -> None:
        protocol = protocol_manifest()
        gc001 = next(
            item for item in protocol["instruments"] if item["code"] == "204001.SH"
        )
        self.assertEqual(gc001["eligible_from"], GC001_ELIGIBLE_FROM)
        self.assertTrue(protocol["historical_eligibility"]["no_backfill"])
        self.assertTrue(protocol["invariants"]["no_production_registration"])
        self.assertTrue(
            protocol["decision_rule"]["passing_is_not_production_authorization"]
        )


if __name__ == "__main__":
    unittest.main()
