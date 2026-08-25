from __future__ import annotations

import unittest

import pandas as pd

from research_platform.v9_shadow_regime_research import (
    SHADOW_LOOKBACK_TRADES,
    assess_shadow_regime,
    filter_v9_trades_by_shadow_pnl,
    protocol_manifest,
)


def _trade(timestamp: str, side: str, code: str, pnl: float | None = None) -> dict:
    return {"timestamp": timestamp, "side": side, "code": code, "pnl": pnl}


class V9ShadowRegimeResearchTests(unittest.TestCase):
    def test_gate_uses_only_closes_before_the_entry_day(self) -> None:
        prior = [-1.0] * SHADOW_LOOKBACK_TRADES
        trades = pd.DataFrame(
            [
                _trade("2025-01-02", "SELL", "000001.SZ", 100.0),
                _trade("2025-01-02", "BUY", "000002.SZ"),
                _trade("2025-01-03", "BUY", "000003.SZ"),
                _trade("2025-01-06", "SELL", "000003.SZ", 1.0),
            ]
        )
        filtered, decisions, _ = filter_v9_trades_by_shadow_pnl(trades, prior)
        self.assertFalse(bool(decisions.loc[0, "accepted"]))
        self.assertTrue(bool(decisions.loc[1, "accepted"]))
        self.assertNotIn("000002.SZ", set(filtered["code"]))
        self.assertIn("000003.SZ", set(filtered["code"]))

    def test_appended_future_trade_does_not_change_prior_decisions(self) -> None:
        trades = pd.DataFrame(
            [
                _trade("2025-01-02", "BUY", "000001.SZ"),
                _trade("2025-01-03", "SELL", "000001.SZ", 1.0),
            ]
        )
        _, first, _ = filter_v9_trades_by_shadow_pnl(trades)
        future = pd.concat(
            [trades, pd.DataFrame([_trade("2025-02-03", "BUY", "000002.SZ")])],
            ignore_index=True,
        )
        with self.assertRaisesRegex(ValueError, "open positions"):
            filter_v9_trades_by_shadow_pnl(future)
        closed_future = pd.concat(
            [
                future,
                pd.DataFrame([_trade("2025-02-04", "SELL", "000002.SZ", -1.0)]),
            ],
            ignore_index=True,
        )
        _, second, _ = filter_v9_trades_by_shadow_pnl(closed_future)
        pd.testing.assert_frame_equal(first, second.iloc[: len(first)].reset_index(drop=True))

    def test_protocol_freezes_binary_ten_trade_gate(self) -> None:
        protocol = protocol_manifest()
        self.assertEqual(protocol["shadow_gate"]["lookback_closed_trades"], 10)
        self.assertFalse(protocol["shadow_gate"]["parameter_search"])
        self.assertTrue(protocol["underlying"]["never_increase_risk"])
        self.assertTrue(protocol["decision"]["passing_is_not_production_authorization"])

    def test_assessment_requires_target_and_robust_trade_pnl(self) -> None:
        windows = [
            {
                "label": label,
                "annualized_return": 0.10,
                "max_drawdown": -0.05,
            }
            for label in (
                "2021-04_2022-04",
                "2022-05_2023-05",
                "2023-06_2024-06",
                "2024-07_2025-07",
                "2025-07_2026-08",
            )
        ]
        base = {
            "weighted_annualized_return": 0.41,
            "windows": windows,
            "exact_v9_controls": True,
            "exact_repo_controls": True,
            "cash_blocks": 0,
        }
        stress = {
            **base,
            "weighted_annualized_return": 0.40,
        }
        decision = assess_shadow_regime(base, stress, [10.0] * 5 + [-1.0] * 5)
        self.assertTrue(decision["retrospective_qualified"])
        rejected = assess_shadow_regime(
            {**base, "weighted_annualized_return": 0.399},
            stress,
            [10.0] * 5 + [-1.0] * 5,
        )
        self.assertFalse(rejected["retrospective_qualified"])


if __name__ == "__main__":
    unittest.main()
