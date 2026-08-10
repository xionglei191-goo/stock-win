from __future__ import annotations

import unittest

import pandas as pd

from research_platform.strategies.course49 import Course49Market
from research_platform.strategies.course49 import build_course49_market_matrix
from research_platform.strategies.course49_v2 import (
    Course49V2Strategy,
    adaptive_target_weight,
    build_course49_eligibility_matrix,
    build_course49_feature_matrix,
    infer_market_style,
    select_trade_mode,
    update_exit_state,
)


def market(*, phase: str = "FERMENT", advance: float = 0.60, limits: float = 0.60) -> Course49Market:
    return Course49Market(
        asof=pd.Timestamp("2026-01-30"),
        score=0.65,
        regime="STRONG",
        phase=phase,
        score_change_3d=0.03,
        entry_allowed=True,
        advance_percentile=advance,
        limit_strength_percentile=limits,
        premium_percentile=0.60,
        streak_percentile=0.60,
    )


def bars(values: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-01-02", periods=len(values), freq="B")
    return pd.DataFrame({"Close": values}, index=index)


def leader(
    *,
    streak: int,
    score: float,
    role: str = "SPACE_LEADER",
    confirmations: tuple[str, ...] = ("EARLY_SEAL",),
    lhb: tuple[str, ...] = (),
    theme: str = "FERMENT",
) -> dict[str, object]:
    return {
        "streak": streak,
        "leader_rank": 1,
        "role": role,
        "theme_phase": theme,
        "board_quality_score": score,
        "limit_behavior": {"confirmations": list(confirmations)},
        "lhb": {"confirmations": list(lhb)},
    }


class Course49V2Tests(unittest.TestCase):
    def test_matrix_sector_ranking_matches_bar_path(self) -> None:
        index = pd.date_range("2026-01-01", periods=30, freq="B")
        codes = [f"60000{offset}.SH" for offset in range(6)]
        front_bars: dict[str, pd.DataFrame] = {}
        raw_bars: dict[str, pd.DataFrame] = {}
        for offset, code in enumerate(codes):
            close = [10.0 + day * 0.01 + offset * 0.02 for day in range(30)]
            if offset < 4:
                close[-1] = close[-2] * 1.10
            frame = pd.DataFrame(
                {
                    "Close": close,
                    "Volume": [1_000_000.0 + offset * 10_000.0] * 29
                    + [1_500_000.0 + offset * 10_000.0],
                },
                index=index,
            )
            front_bars[code] = frame
            raw_bars[code] = frame.copy()
        names = {code: f"sample-{code}" for code in codes}
        sectors = {
            "T001": {"name": "sample-sector", "members": codes},
        }
        strategy = Course49V2Strategy()
        expected = strategy.rank_sectors(
            front_bars,
            raw_bars,
            names,
            sectors,
        )
        actual = strategy.rank_sectors(
            front_bars,
            raw_bars,
            names,
            sectors,
            feature_matrix=build_course49_feature_matrix(
                front_bars,
                raw_bars,
                names,
            ),
            asof=index[-1],
            eligible_codes=set(codes),
        )

        self.assertEqual(len(actual), len(expected))
        self.assertEqual(actual[0]["sector_code"], expected[0]["sector_code"])
        self.assertEqual(actual[0]["limit_count"], expected[0]["limit_count"])
        self.assertEqual(actual[0]["theme_phase"], expected[0]["theme_phase"])
        self.assertAlmostEqual(float(actual[0]["score"]), float(expected[0]["score"]))

    def test_eligibility_matrix_is_point_in_time_and_handles_suspension(self) -> None:
        index = pd.date_range("2025-10-01", periods=65, freq="B")
        frame = pd.DataFrame(
            {
                "Close": [10.0] * 65,
                "Volume": [1_000_000.0] * 64 + [0.0],
                "Amount": [25_000_000.0] * 65,
            },
            index=index,
        )
        frame.attrs["amount_unit"] = "CNY"
        first = build_course49_eligibility_matrix({"000001.SZ": frame}, {})
        changed = frame.copy()
        changed.attrs["amount_unit"] = "CNY"
        changed.loc[index[-1], "Amount"] = 0.0
        second = build_course49_eligibility_matrix({"000001.SZ": changed}, {})

        self.assertFalse(bool(first.loc[index[58], "000001.SZ"]))
        self.assertTrue(bool(first.loc[index[59], "000001.SZ"]))
        self.assertFalse(bool(first.loc[index[-1], "000001.SZ"]))
        pd.testing.assert_series_equal(first.iloc[:-1, 0], second.iloc[:-1, 0])

    def test_missing_critical_benchmark_blocks_entries(self) -> None:
        style = infer_market_style(
            {"000300.CSI": bars([100.0] * 21)},
            market(),
        )
        self.assertEqual(style.code, "UNKNOWN")
        self.assertFalse(style.entry_allowed)
        self.assertEqual(style.suitability, 0.0)

    def test_short_preferred_benchmark_uses_complete_fallback(self) -> None:
        upward = [100.0 + index for index in range(21)]
        style = infer_market_style(
            {
                "000300.CSI": bars([100.0]),
                "000300.SH": bars(upward),
                "000852.CSI": bars([100.0]),
                "000852.SH": bars(upward),
            },
            market(advance=0.55),
        )
        self.assertEqual(style.code, "BROAD_RISK_ON")
        self.assertEqual(style.benchmark_codes["large"], "000300.SH")
        self.assertEqual(style.benchmark_codes["small"], "000852.SH")

    def test_small_cap_and_broad_style_boundaries(self) -> None:
        small = [100.0] * 15 + [101.0, 101.5, 102.0, 102.5, 103.0, 103.5]
        style = infer_market_style(
            {
                "000300.CSI": bars([100.0] * 21),
                "000852.CSI": bars(small),
            },
            market(limits=0.55),
        )
        self.assertEqual(style.code, "SMALL_CAP_SPECULATION")
        self.assertEqual(style.suitability, 1.0)

        upward = [100.0 + index for index in range(21)]
        broad = infer_market_style(
            {
                "000300.CSI": bars(upward),
                "000852.CSI": bars(upward),
            },
            market(advance=0.55),
        )
        self.assertEqual(broad.code, "BROAD_RISK_ON")
        self.assertEqual(broad.suitability, 0.80)

    def test_three_trade_modes(self) -> None:
        self.assertEqual(
            select_trade_mode(
                "RECOVERY",
                "SMALL_CAP_SPECULATION",
                leader(streak=1, score=0.65, role="THEME_LEADER", theme="START"),
            ),
            ("RECOVERY_IGNITION", 0.15),
        )
        self.assertEqual(
            select_trade_mode(
                "FERMENT",
                "MIXED",
                leader(streak=2, score=0.55, lhb=("LHB_NET_BUY",)),
            ),
            ("FERMENT_SECOND_BOARD", 0.25),
        )
        self.assertEqual(
            select_trade_mode(
                "ACCELERATION",
                "BROAD_RISK_ON",
                leader(streak=3, score=0.70, confirmations=("PREMIUM_MEMORY",), theme="ACCELERATION"),
            ),
            ("ACCELERATION_CORE_RELAY", 0.20),
        )
        self.assertIsNone(select_trade_mode("DIVERGENCE", "SMALL_CAP_SPECULATION", leader(streak=2, score=0.9)))

    def test_dynamic_weight_is_versioned_and_capped(self) -> None:
        self.assertAlmostEqual(adaptive_target_weight(0.25, 0.55, 0.70), 0.1375)
        self.assertAlmostEqual(adaptive_target_weight(0.15, 0.25, 0.85), 0.10)
        self.assertAlmostEqual(adaptive_target_weight(0.30, 1.0, 0.90), 0.30)

    def test_confirmed_exit_counts_reset(self) -> None:
        state, reason = update_exit_state(
            {}, price=10.0, entry_price=10.0, below_ma5=True,
            market_weak=True, sector_weak=False, leader_weak=False,
        )
        self.assertEqual(reason, "")
        self.assertEqual(state["market_weak_days"], 1)
        state, reason = update_exit_state(
            state, price=10.0, entry_price=10.0, below_ma5=False,
            market_weak=False, sector_weak=False, leader_weak=False,
        )
        self.assertEqual(reason, "")
        self.assertEqual(state["market_weak_days"], 0)
        state, reason = update_exit_state(
            state, price=9.8, entry_price=10.0, below_ma5=True,
            market_weak=True, sector_weak=False, leader_weak=False,
        )
        state, reason = update_exit_state(
            state, price=9.7, entry_price=10.0, below_ma5=True,
            market_weak=True, sector_weak=False, leader_weak=False,
        )
        self.assertEqual(reason, "MARKET_WEAK_CONFIRMED")

    def test_trailing_profit_and_immediate_exit_priority(self) -> None:
        state, reason = update_exit_state(
            {"max_close": 11.0}, price=10.50, entry_price=10.0, below_ma5=False,
            market_weak=False, sector_weak=False, leader_weak=False,
        )
        self.assertEqual(reason, "TRAILING_PROFIT")
        _, reason = update_exit_state(
            state, price=9.0, entry_price=10.0, below_ma5=True,
            market_weak=True, sector_weak=True, leader_weak=True,
            immediate_reason="CAPITAL_DISTRIBUTION",
        )
        self.assertEqual(reason, "CAPITAL_DISTRIBUTION")

    def test_feature_matrix_does_not_leak_future_prices(self) -> None:
        index = pd.date_range("2026-01-01", periods=30, freq="B")
        frame = pd.DataFrame(
            {
                "Close": [10.0 + offset * 0.1 for offset in range(30)],
                "Volume": [1_000_000.0] * 30,
            },
            index=index,
        )
        first = build_course49_feature_matrix(
            {"000001.SZ": frame}, {"000001.SZ": frame}, {"000001.SZ": "样本"}
        )
        changed = frame.copy()
        changed.iloc[-1, changed.columns.get_loc("Close")] = 1000.0
        second = build_course49_feature_matrix(
            {"000001.SZ": changed}, {"000001.SZ": changed}, {"000001.SZ": "样本"}
        )
        prior = index[-2]
        self.assertAlmostEqual(
            float(first["return_5d"].loc[prior, "000001.SZ"]),
            float(second["return_5d"].loc[prior, "000001.SZ"]),
        )

    def test_market_matrix_does_not_leak_future_prices(self) -> None:
        index = pd.date_range("2026-01-01", periods=30, freq="B")
        frame = pd.DataFrame({"Close": [10.0 + offset * 0.1 for offset in range(30)]}, index=index)
        first = build_course49_market_matrix(
            {"000001.SZ": frame}, {"000001.SZ": "样本"}
        )
        changed = frame.copy()
        changed.iloc[-1, changed.columns.get_loc("Close")] = 1000.0
        second = build_course49_market_matrix(
            {"000001.SZ": changed}, {"000001.SZ": "样本"}
        )
        prior = index[-2]
        self.assertAlmostEqual(float(first.loc[prior, "score"]), float(second.loc[prior, "score"]))
        self.assertEqual(first.loc[prior, "phase"], second.loc[prior, "phase"])


if __name__ == "__main__":
    unittest.main()
