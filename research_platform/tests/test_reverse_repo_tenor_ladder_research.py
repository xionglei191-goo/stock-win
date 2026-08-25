from __future__ import annotations

import struct
import unittest

import pandas as pd

from research_platform.reverse_repo_tenor_ladder_research import (
    RepoTenor,
    decode_tenor_day_bytes,
    protocol_manifest,
    simulate_tenor_ladder,
)


TEST_TENORS = (
    RepoTenor("131810.SZ", "R-001", "sz131810", 1),
    RepoTenor("131811.SZ", "R-002", "sz131811", 2),
    RepoTenor("131800.SZ", "R-003", "sz131800", 3),
)


def _rates(dates: pd.DatetimeIndex, values: dict[str, list[float]]) -> pd.DataFrame:
    rows = []
    tenor_by_code = {tenor.code: tenor.tenor_days for tenor in TEST_TENORS}
    for code, code_values in values.items():
        for date_value, rate in zip(dates, code_values, strict=True):
            rows.append(
                {
                    "timestamp": date_value,
                    "code": code,
                    "tenor_days": tenor_by_code[code],
                    "Close": rate,
                    "Low": rate,
                    "Volume": 1_000.0,
                }
            )
    return pd.DataFrame(rows)


class ReverseRepoTenorLadderResearchTests(unittest.TestCase):
    def test_decoder_uses_repo_scale_and_keeps_volume(self) -> None:
        payload = struct.pack(
            "<IIIIIfII", 20240102, 15_000, 18_000, 12_000, 14_900, 100.0, 500, 0
        )
        rates = decode_tenor_day_bytes(payload, TEST_TENORS[0])
        self.assertAlmostEqual(rates.loc[0, "Close"], 1.49)
        self.assertEqual(rates.loc[0, "Volume"], 500.0)
        self.assertEqual(rates.loc[0, "tenor_days"], 1)

    def test_regular_day_allows_only_one_day_tenor(self) -> None:
        dates = pd.to_datetime(
            ["2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 4, "cash": [50_000.0] * 4}
        )
        rates = _rates(
            dates,
            {
                "131810.SZ": [1.0] * 4,
                "131811.SZ": [9.0] * 4,
                "131800.SZ": [8.0] * 4,
            },
        )
        result = simulate_tenor_ladder(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
            tenors=TEST_TENORS,
        )
        frame = result["equity"]
        self.assertEqual(frame.loc[0, "repo_code"], "131810.SZ")
        self.assertEqual(frame.loc[0, "repo_tenor_days"], 1)
        self.assertEqual(frame.loc[0, "actual_occupied_days"], 1)

    def test_friday_selects_best_tenor_maturing_by_monday(self) -> None:
        dates = pd.to_datetime(
            ["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 4, "cash": [50_000.0] * 4}
        )
        rates = _rates(
            dates,
            {
                "131810.SZ": [1.0] * 4,
                "131811.SZ": [2.0] * 4,
                "131800.SZ": [3.0] * 4,
            },
        )
        result = simulate_tenor_ladder(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
            tenors=TEST_TENORS,
        )
        frame = result["equity"]
        self.assertEqual(frame.loc[0, "repo_code"], "131800.SZ")
        self.assertEqual(frame.loc[0, "repo_tenor_days"], 3)
        self.assertEqual(frame.loc[0, "actual_occupied_days"], 1)

    def test_thursday_weekend_interest_uses_three_actual_days(self) -> None:
        dates = pd.to_datetime(
            ["2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"]
        )
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 4, "cash": [50_000.0] * 4}
        )
        rates = _rates(
            dates,
            {
                "131810.SZ": [3.65] * 4,
                "131811.SZ": [9.0] * 4,
                "131800.SZ": [9.0] * 4,
            },
        )
        result = simulate_tenor_ladder(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
            tenors=TEST_TENORS,
        )
        frame = result["equity"]
        expected = 50_000.0 * 0.0365 * 3.0 / 365.0
        self.assertEqual(frame.loc[0, "repo_code"], "131810.SZ")
        self.assertEqual(frame.loc[0, "actual_occupied_days"], 3)
        self.assertAlmostEqual(frame.loc[2, "settled_repo_pnl"], expected)

    def test_future_quotes_do_not_change_historical_selection(self) -> None:
        dates = pd.to_datetime(["2024-01-05", "2024-01-08", "2024-01-09"])
        equity = pd.DataFrame(
            {"timestamp": dates, "equity": [50_000.0] * 3, "cash": [50_000.0] * 3}
        )
        rates = _rates(
            dates,
            {
                "131810.SZ": [1.0] * 3,
                "131811.SZ": [2.0] * 3,
                "131800.SZ": [3.0] * 3,
            },
        )
        first = simulate_tenor_ladder(
            equity,
            rates,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
            tenors=TEST_TENORS,
        )
        future = rates.copy()
        future.loc[future["timestamp"].eq(pd.Timestamp("2024-01-09")), "Close"] = 99.0
        second = simulate_tenor_ladder(
            equity,
            future,
            initial_cash=50_000.0,
            rate_field="Close",
            commission_rate=0.0,
            tenors=TEST_TENORS,
        )
        self.assertEqual(first["net_interest"], second["net_interest"])
        self.assertEqual(
            first["equity"].loc[0, "repo_code"], second["equity"].loc[0, "repo_code"]
        )

    def test_protocol_discloses_post_hoc_and_research_only(self) -> None:
        protocol = protocol_manifest()
        self.assertEqual(protocol["research_status"], "post_result_retrospective_extension")
        self.assertTrue(protocol["post_hoc_disclosure"]["known_target_shortfall_percentage_points"])
        self.assertTrue(protocol["invariants"]["principal_available_every_next_open"])
        self.assertTrue(protocol["invariants"]["no_production_registration"])
        self.assertTrue(
            protocol["decision_rule"]["passing_is_not_production_authorization"]
        )


if __name__ == "__main__":
    unittest.main()
