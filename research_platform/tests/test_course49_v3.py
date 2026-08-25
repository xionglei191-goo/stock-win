from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd

from research_platform.strategies.course49 import Course49Market
from research_platform.strategies.course49_v2 import MarketStyle
from research_platform.strategies.course49_v2 import build_course49_eligibility_matrix
from research_platform.strategies.course49_v3 import (
    Course49V3Strategy,
    build_course49_v3_candidate_matrix,
    select_trade_mode_v3,
)
from research_platform.strategies.course49_v4 import Course49V4Strategy
from research_platform.strategies.course49_v5 import Course49V5Strategy
from research_platform.strategies.course49_v6 import Course49V6Strategy
from research_platform.strategies.course49_v7 import Course49V7Strategy
from research_platform.strategies.course49_v8 import Course49V8Strategy
from research_platform.strategies.course49_v9 import Course49V9Strategy
from research_platform.strategies.course49_v10 import (
    MIN_MARKET_SCORE_CHANGE_3D,
    Course49V10Strategy,
)
from research_platform.strategies.course49_v11 import (
    MIN_OPEN_BOARD_COUNT,
    Course49V11Strategy,
)


def market(phase: str = "ACCELERATION") -> Course49Market:
    return Course49Market(
        asof=pd.Timestamp("2026-07-29"),
        score=0.72,
        regime="STRONG",
        phase=phase,
        score_change_3d=0.08,
        entry_allowed=True,
        advance_percentile=0.60,
        limit_strength_percentile=0.70,
        premium_percentile=0.65,
        streak_percentile=0.80,
    )


def style(code: str = "DEFENSIVE", suitability: float = 0.0) -> MarketStyle:
    return MarketStyle(
        code=code,
        suitability=suitability,
        entry_allowed=code != "UNKNOWN",
        benchmark_codes={"large": "000300.SH", "small": "000852.SH", "growth": None},
        large_return_5d=-0.01,
        large_return_20d=-0.03,
        small_return_5d=-0.01,
        small_return_20d=-0.02,
        growth_return_20d=None,
        large_above_ma20=False,
        small_above_ma20=False,
        growth_above_ma20=None,
    )


def leader(
    streak: int,
    quality: float,
    *,
    rank: int = 1,
    open_board_count: int = 0,
) -> dict[str, object]:
    return {
        "streak": streak,
        "leader_rank": rank,
        "role": "SPACE_LEADER",
        "board_quality_score": quality,
        "limit_behavior": {
            "confirmations": ["EARLY_SEAL", "STRONG_SEAL"],
            "open_board_count": open_board_count,
        },
    }


class Course49V3Tests(unittest.TestCase):
    def test_candidate_matrix_only_keeps_eligible_ten_percent_second_boards(self) -> None:
        index = pd.date_range("2025-10-01", periods=65, freq="B")

        def frame(limit_return: float) -> pd.DataFrame:
            close = [10.0] * 63
            close.extend([10.0 * (1.0 + limit_return), 10.0 * (1.0 + limit_return) ** 2])
            item = pd.DataFrame(
                {
                    "Close": close,
                    "Volume": [1_000_000.0] * 65,
                    "Amount": [25_000_000.0] * 65,
                },
                index=index,
            )
            item.attrs["amount_unit"] = "CNY"
            return item

        bars_by_code = {
            "000001.SZ": frame(0.10),
            "300001.SZ": frame(0.20),
            "000002.SZ": frame(0.10),
        }
        names = {"000002.SZ": "ST样本"}
        eligibility = build_course49_eligibility_matrix(bars_by_code, names)
        candidates = build_course49_v3_candidate_matrix(
            bars_by_code, names, eligibility
        )

        self.assertTrue(bool(candidates.loc[index[-1], "000001.SZ"]))
        self.assertFalse(bool(candidates.loc[index[-1], "300001.SZ"]))
        self.assertFalse(bool(candidates.loc[index[-1], "000002.SZ"]))

        first_board_candidates = build_course49_v3_candidate_matrix(
            bars_by_code, names, eligibility, minimum_streak=1
        )
        self.assertTrue(bool(first_board_candidates.loc[index[-2], "000001.SZ"]))

    def test_global_core_holding_does_not_fake_sector_fade(self) -> None:
        strategy = Course49V3Strategy()

        self.assertFalse(strategy.holding_sector_weak("GLOBAL_CORE", None))
        self.assertTrue(strategy.holding_sector_weak("880001.SH", None))

    def test_global_rank_accepts_base_point_in_time_arguments(self) -> None:
        strategy = Course49V3Strategy()

        self.assertEqual(
            strategy.rank_sectors(
                {},
                {},
                {},
                {},
                asof=pd.Timestamp("2026-01-05"),
                eligible_codes={"000001.SZ"},
            ),
            [],
        )

    def test_defensive_index_does_not_veto_local_acceleration(self) -> None:
        strategy = Course49V3Strategy()
        self.assertTrue(strategy.entry_allowed(market(), style()))
        self.assertEqual(strategy.effective_suitability(market(), style()), 0.50)
        self.assertEqual(strategy.entry_sector_count(), 3)
        self.assertTrue(strategy.leader_in_entry_scope({}, set()))

    def test_missing_critical_benchmark_still_blocks_entry(self) -> None:
        self.assertFalse(Course49V3Strategy().entry_allowed(market(), style("UNKNOWN")))

    def test_local_acceleration_modes_require_core_role_and_quality(self) -> None:
        self.assertEqual(
            select_trade_mode_v3(market(), style(), leader(2, 0.75)),
            ("LOCAL_ACCELERATION_CORE", 0.15),
        )
        self.assertEqual(
            select_trade_mode_v3(market(), style(), leader(4, 0.65)),
            ("LOCAL_ACCELERATION_HIGH_BOARD", 0.20),
        )
        self.assertIsNone(select_trade_mode_v3(market(), style(), leader(2, 0.74)))
        self.assertIsNone(select_trade_mode_v3(market(), style(), leader(4, 0.64)))
        self.assertIsNone(select_trade_mode_v3(market(), style(), leader(4, 0.90, rank=2)))
        self.assertIsNone(select_trade_mode_v3(market("RECOVERY"), style(), leader(4, 0.90)))

    def test_v4_requires_net_buy_blocks_growth_and_caps_board_height(self) -> None:
        strategy = Course49V4Strategy()
        confirmed = SimpleNamespace(
            listed=True, risk="", net_buy_ratio=0.05
        )
        weak = SimpleNamespace(
            listed=True, risk="", net_buy_ratio=0.0499
        )

        self.assertTrue(strategy.capital_allowed(confirmed))  # type: ignore[arg-type]
        self.assertFalse(strategy.capital_allowed(weak))  # type: ignore[arg-type]
        self.assertFalse(strategy.entry_allowed(market(), style("GROWTH_TREND", 0.4)))
        self.assertTrue(strategy.entry_allowed(market(), style("DEFENSIVE", 0.0)))
        self.assertEqual(
            strategy.select_mode(market(), style(), leader(3, 0.80)),
            ("CAPITAL_CONFIRMED_CORE", 0.22),
        )
        self.assertEqual(
            strategy.select_mode(market(), style(), leader(4, 0.70)),
            ("CAPITAL_CONFIRMED_FOURTH_BOARD", 0.22),
        )
        self.assertIsNone(strategy.select_mode(market(), style(), leader(5, 0.90)))

    def test_v5_only_changes_the_confirmed_risk_budget(self) -> None:
        v4_mode = Course49V4Strategy().select_mode(market(), style(), leader(3, 0.80))
        v5_mode = Course49V5Strategy().select_mode(market(), style(), leader(3, 0.80))

        self.assertEqual(v4_mode, ("CAPITAL_CONFIRMED_CORE", 0.22))
        self.assertEqual(v5_mode, ("CAPITAL_CONFIRMED_CORE", 0.25))
        self.assertFalse(Course49V5Strategy.metadata.scan_enabled)

    def test_v6_only_accepts_small_cap_acceleration_first_board_quality_band(self) -> None:
        strategy = Course49V6Strategy()
        small_cap = style("SMALL_CAP_SPECULATION", 1.0)

        self.assertEqual(
            strategy.select_mode(market(), small_cap, leader(1, 0.55)),
            ("SMALL_CAP_ACCELERATION_FIRST_BOARD", 0.30),
        )
        self.assertIsNone(strategy.select_mode(market(), small_cap, leader(1, 0.70)))
        self.assertIsNone(strategy.select_mode(market(), small_cap, leader(2, 0.67)))
        self.assertIsNone(strategy.select_mode(market(), style(), leader(1, 0.67)))
        self.assertEqual(strategy.candidate_minimum_streak(), 1)
        self.assertEqual(strategy.candidate_limit(), 10)
        self.assertEqual(strategy.stop_loss_ratio(), 0.03)
        self.assertFalse(strategy.metadata.scan_enabled)

    def test_v6_exits_after_five_observed_holding_days(self) -> None:
        strategy = Course49V6Strategy()
        state: dict[str, object] = {}
        reason = ""
        for _ in range(5):
            state, reason = strategy.evaluate_exit_state(
                state,
                price=10.0,
                entry_price=10.0,
                below_ma5=False,
                market_weak=False,
                sector_weak=False,
                leader_weak=False,
                immediate_reason="",
            )

        self.assertEqual(reason, "FIRST_BOARD_TIME_EXIT")
        self.assertEqual(state["holding_days"], 5)

    def test_v7_requires_broad_risk_on_low_quality_reseal(self) -> None:
        strategy = Course49V7Strategy()
        broad = style("BROAD_RISK_ON", 0.8)

        self.assertEqual(
            strategy.select_mode(
                market("CLIMAX"), broad, leader(1, 0.54, open_board_count=2)
            ),
            ("BROAD_RISK_ON_FIRST_BOARD_RESEAL", 0.20),
        )
        self.assertIsNone(
            strategy.select_mode(
                market(), broad, leader(1, 0.55, open_board_count=2)
            )
        )
        self.assertIsNone(
            strategy.select_mode(
                market(), broad, leader(1, 0.54, open_board_count=1)
            )
        )
        self.assertIsNone(
            strategy.select_mode(
                market(), style("SMALL_CAP_SPECULATION", 1.0),
                leader(1, 0.54, open_board_count=3),
            )
        )
        self.assertEqual(strategy.candidate_limit(), 5)
        self.assertFalse(strategy.metadata.scan_enabled)

    def test_v7_exits_after_three_observed_holding_days(self) -> None:
        strategy = Course49V7Strategy()
        state: dict[str, object] = {}
        reason = ""
        for _ in range(3):
            state, reason = strategy.evaluate_exit_state(
                state,
                price=10.0,
                entry_price=10.0,
                below_ma5=False,
                market_weak=False,
                sector_weak=False,
                leader_weak=False,
                immediate_reason="",
            )

        self.assertEqual(reason, "BROAD_FIRST_BOARD_TIME_EXIT")
        self.assertEqual(state["holding_days"], 3)

    def test_v8_blocks_recent_lhb_crowding(self) -> None:
        strategy = Course49V8Strategy()

        self.assertTrue(strategy.capital_allowed(None))
        self.assertFalse(strategy.capital_allowed(SimpleNamespace(listed=True)))  # type: ignore[arg-type]
        self.assertEqual(strategy.metadata.version, "8.0.0")
        self.assertFalse(strategy.metadata.scan_enabled)

    def test_v9_freezes_quality_band_and_bounded_risk_budget(self) -> None:
        strategy = Course49V9Strategy()
        broad = style("BROAD_RISK_ON", 0.8)

        self.assertEqual(
            strategy.select_mode(
                market(), broad, leader(1, 0.50, open_board_count=2)
            ),
            ("BROAD_RISK_ON_LOW_CROWDING_RESEAL", 0.30),
        )
        self.assertIsNone(
            strategy.select_mode(
                market(), broad, leader(1, 0.499, open_board_count=2)
            )
        )
        self.assertIsNone(
            strategy.select_mode(
                market(), broad, leader(1, 0.55, open_board_count=2)
            )
        )
        self.assertAlmostEqual(strategy.target_weight(0.30, 0.8, 0.52), 0.24)
        self.assertEqual(strategy.metadata.lifecycle, "HISTORICAL_REJECTED")

    def test_v10_requires_rising_market_reward_score(self) -> None:
        strategy = Course49V10Strategy()
        broad = style("BROAD_RISK_ON", 0.8)
        accepted = market()
        accepted = replace(accepted, score_change_3d=MIN_MARKET_SCORE_CHANGE_3D)
        rejected = replace(accepted, score_change_3d=MIN_MARKET_SCORE_CHANGE_3D - 0.001)

        self.assertTrue(strategy.entry_allowed(accepted, broad))
        self.assertFalse(strategy.entry_allowed(rejected, broad))
        self.assertEqual(
            strategy.entry_block_reason(rejected, broad),
            "market_reward_momentum_below_threshold",
        )
        self.assertEqual(strategy.metadata.lifecycle, "HOLDOUT_TARGET_REJECTED")
        self.assertFalse(strategy.metadata.scan_enabled)

    def test_v11_requires_three_intraday_open_board_events(self) -> None:
        strategy = Course49V11Strategy()
        accepted = SimpleNamespace(open_board_count=MIN_OPEN_BOARD_COUNT)
        rejected = SimpleNamespace(open_board_count=MIN_OPEN_BOARD_COUNT - 1)

        self.assertTrue(strategy.candidate_behavior_allowed(accepted))  # type: ignore[arg-type]
        self.assertFalse(strategy.candidate_behavior_allowed(rejected))  # type: ignore[arg-type]
        self.assertEqual(
            strategy.metadata.lifecycle, "HISTORICAL_ROBUSTNESS_REJECTED"
        )
        self.assertFalse(strategy.metadata.scan_enabled)


if __name__ == "__main__":
    unittest.main()
