from __future__ import annotations

import unittest

import pandas as pd

from research_platform.course49_diagnostics import (
    build_execution_event_table,
    summarize_events,
)


class Course49DiagnosticsTests(unittest.TestCase):
    def test_next_open_limit_up_is_excluded_and_costs_are_applied(self) -> None:
        dates = pd.date_range("2026-01-05", periods=7, freq="B")
        raw = pd.DataFrame(
            {
                "code": ["000001.SZ"] * 7 + ["000002.SZ"] * 7,
                "timestamp": list(dates) * 2,
                "Open": [10, 10, 12.10, 12.2, 12.3, 12.5, 12.7]
                + [10, 10, 11, 11.2, 11.4, 11.6, 11.8],
                "Close": [10, 11, 12.10, 12.2, 12.4, 12.6, 12.8]
                + [10, 11, 11.3, 11.5, 11.7, 11.9, 12.1],
            }
        )
        events = pd.DataFrame(
            {
                "code": ["000001.SZ", "000002.SZ"],
                "event_date": [dates[1], dates[1]],
                "limit_event": [True, True],
                "board_quality_score": [0.8, 0.8],
                "risk": ["", ""],
                "board_confirmations": [["EARLY_SEAL"], ["EARLY_SEAL"]],
            }
        )
        states = pd.DataFrame(
            {
                "timestamp": [dates[1]],
                "market_phase": ["ACCELERATION"],
                "market_style": ["DEFENSIVE"],
            }
        )

        result = build_execution_event_table(raw, events, states)

        blocked = result.loc[result["code"] == "000001.SZ"].iloc[0]
        tradable = result.loc[result["code"] == "000002.SZ"].iloc[0]
        self.assertFalse(bool(blocked["next_open_tradable"]))
        self.assertTrue(pd.isna(blocked["net_return_1d"]))
        self.assertTrue(bool(tradable["next_open_tradable"]))
        gross_return = 11.3 / 11.0 - 1.0
        self.assertLess(float(tradable["net_return_1d"]), gross_return)
        execution_gross_return = 11.2 / 11.0 - 1.0
        self.assertLess(
            float(tradable["execution_net_return_1d"]), execution_gross_return
        )
        self.assertGreater(float(tradable["execution_net_return_1d"]), 0.0)

    def test_v3_cohort_requires_acceleration_and_two_boards(self) -> None:
        dates = pd.date_range("2026-01-05", periods=8, freq="B")
        raw = pd.DataFrame(
            {
                "code": ["000001.SZ"] * 8,
                "timestamp": dates,
                "Open": [10, 10, 11, 12, 12.3, 12.5, 12.7, 12.9],
                "Close": [10, 11, 12.1, 12.3, 12.5, 12.7, 12.9, 13.1],
            }
        )
        events = pd.DataFrame(
            {
                "code": ["000001.SZ"],
                "event_date": [dates[2]],
                "limit_event": [True],
                "board_quality_score": [0.8],
                "risk": [""],
                "board_confirmations": [["STRONG_SEAL"]],
            }
        )
        states = pd.DataFrame(
            {
                "timestamp": [dates[2]],
                "market_phase": ["ACCELERATION"],
                "market_style": ["DEFENSIVE"],
            }
        )

        result = build_execution_event_table(raw, events, states)

        self.assertEqual(int(result.iloc[0]["streak"]), 2)
        self.assertTrue(bool(result.iloc[0]["v3_rule_eligible"]))
        summary = summarize_events(result, ["market_phase"])
        self.assertEqual(summary[0]["market_phase"], "ACCELERATION")
        self.assertAlmostEqual(
            summary[0]["net_1d_pct"],
            round(float(result.iloc[0]["execution_net_return_1d"]) * 100.0, 4),
        )


if __name__ == "__main__":
    unittest.main()
