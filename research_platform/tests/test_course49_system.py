from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from research_platform.config import PlatformConfig
from research_platform.storage import Database
from research_platform.strategies.course49 import Course49Market
from research_platform.strategies.course49_system import (
    FRAMEWORK_ID,
    LEADER_PULLBACK_PLAYBOOK_ID,
    POLICY_VERSION,
    Course49Router,
    Course49PlaybookRegistry,
    Course49Context,
    Course49SystemStrategy,
    PlaybookCandidate,
    PlaybookResult,
    PlaybookValidationEvidence,
    PlaybookLifecycle,
    PlaybookMetadata,
    RecoveryIgnitionPlaybook,
    _pullback_candidate_features,
    build_leader_pullback_candidate_matrix,
    production_playbooks,
    research_playbooks,
    update_pullback_exit_state,
)
from research_platform.strategies.course49_v2 import Course49V2Strategy, MarketStyle
from research_platform.tests.helpers import temporary_config


def market(phase: str = "FERMENT") -> Course49Market:
    return Course49Market(
        asof=pd.Timestamp("2026-01-30"),
        score=0.65,
        regime="STRONG",
        phase=phase,
        score_change_3d=0.03,
        entry_allowed=True,
        advance_percentile=0.60,
        limit_strength_percentile=0.60,
        premium_percentile=0.60,
        streak_percentile=0.60,
    )


def style(code: str = "MIXED", suitability: float = 0.55) -> MarketStyle:
    return MarketStyle(
        code,
        suitability,
        True,
        {"large": "000300.CSI", "small": "000852.CSI", "growth": "399006.SZ"},
        0.01,
        0.02,
        0.02,
        0.03,
        0.04,
        True,
        True,
        True,
    )


def leader(code: str = "000001.SZ") -> dict[str, object]:
    return {
        "code": code,
        "name": "sample",
        "price": 10.0,
        "sector_code": "T001",
        "sector_name": "sample-sector",
        "sector_rank": 1,
        "sector_score": 0.80,
        "theme_phase": "FERMENT",
        "streak": 2,
        "leader_rank": 1,
        "role": "SPACE_LEADER",
        "leader_score": 0.90,
        "board_quality_score": 0.70,
        "limit_behavior": {"confirmations": ["EARLY_SEAL"]},
        "lhb": {"confirmations": []},
        "capital_risk": "",
    }


def pullback_frame(*, future: bool = False) -> pd.DataFrame:
    index = pd.date_range("2025-12-01", periods=30, freq="B")
    close = [8.5 + index_value * 0.04 for index_value in range(20)] + [
        9.8, 10.0, 11.0, 11.5, 12.0, 11.7, 12.2, 11.7, 11.4, 11.8,
    ]
    volume = [1_000_000.0] * 27 + [500_000.0, 500_000.0, 900_000.0]
    frame = pd.DataFrame(
        {
            "Open": [value * 0.995 for value in close[:-1]] + [11.5],
            "High": [value * 1.01 for value in close],
            "Low": [value * 0.99 for value in close[:-3]] + [11.55, 11.20, 11.35],
            "Close": close,
            "Volume": volume,
            "Amount": [value * amount for value, amount in zip(close, volume)],
        },
        index=index,
    )
    if future:
        frame.loc[index[-1] + pd.offsets.BDay(1)] = {
            "Open": 13.0,
            "High": 13.5,
            "Low": 12.8,
            "Close": 13.4,
            "Volume": 2_000_000.0,
            "Amount": 26_800_000.0,
        }
    return frame


def pullback_context(*, phase: str = "DIVERGENCE", future: bool = False) -> Course49Context:
    frame = pullback_frame(future=future)
    asof = pd.Timestamp("2026-01-09")
    current_market = market(phase)
    if phase == "DIVERGENCE":
        current_market = Course49Market(
            **{
                **current_market.__dict__,
                "score": 0.60,
                "regime": "NORMAL",
                "entry_allowed": False,
            }
        )
    sector = {
        "sector_code": "T001",
        "sector_name": "sample-sector",
        "rank": 1,
        "score": 0.80,
        "theme_phase": "DIVERGENCE",
    }
    return Course49Context(
        asof=asof,
        context_version="1.0.0",
        stock_pool_hash="pool",
        sector_membership_hash="sector",
        data_completeness={"critical_benchmarks": True},
        market=current_market,
        style=style("MIXED", 0.55),
        suitability=0.55,
        entry_allowed=current_market.entry_allowed,
        entry_block_reason="",
        sectors=(sector,),
        entry_sector_codes=frozenset({"T001"}),
        holding_sectors=(sector,),
        leaders=(),
        positions={},
        runtime_state={},
        front_bars={"000001.SZ": frame},
        raw_bars={"000001.SZ": frame},
        sector_members={"T001": {"name": "sample-sector", "members": ["000001.SZ"]}},
        lhb_history={},
    )


class Course49SystemTests(unittest.TestCase):
    def test_all_production_playbooks_use_shared_context_boundaries(self) -> None:
        scenarios = {
            "recovery_ignition": (
                market("RECOVERY"),
                style("SMALL_CAP_SPECULATION", 1.0),
                {**leader(), "streak": 1, "theme_phase": "START", "board_quality_score": 0.65},
                0.15,
            ),
            "ferment_second_board": (
                market("FERMENT"), style("MIXED", 0.55), leader(), 0.1375,
            ),
            "acceleration_core_relay": (
                market("ACCELERATION"),
                style("BROAD_RISK_ON", 0.80),
                {**leader(), "theme_phase": "ACCELERATION", "board_quality_score": 0.70},
                0.16,
            ),
        }
        playbooks = {
            item.metadata.playbook_id: item
            for item in production_playbooks(Course49SystemStrategy.metadata.data_requirements)
        }
        for playbook_id, (current_market, current_style, current_leader, expected_weight) in scenarios.items():
            with self.subTest(playbook_id=playbook_id):
                context = Course49Context(
                    asof=pd.Timestamp("2026-01-30"),
                    context_version="1.0.0",
                    stock_pool_hash="pool",
                    sector_membership_hash="sector",
                    data_completeness={"critical_benchmarks": True},
                    market=current_market,
                    style=current_style,
                    suitability=current_style.suitability,
                    entry_allowed=True,
                    entry_block_reason="",
                    sectors=({"sector_code": "T001", "rank": 1},),
                    entry_sector_codes=frozenset({"T001"}),
                    holding_sectors=(),
                    leaders=(current_leader,),
                    positions={},
                    runtime_state={},
                    front_bars={},
                    raw_bars={},
                    sector_members={},
                    lhb_history={},
                )
                result = playbooks[playbook_id].evaluate(context)
                self.assertTrue(result.admitted)
                self.assertEqual(len(result.candidates), 1)
                self.assertAlmostEqual(result.candidates[0].target_weight, expected_weight)

    def test_new_playbook_requires_research_lifecycle_and_promotion_gates(self) -> None:
        registry = Course49PlaybookRegistry()
        playbook = RecoveryIgnitionPlaybook(
            PlaybookMetadata(
                "research_candidate",
                FRAMEWORK_ID,
                "0.1.0",
                "candidate",
                "candidate",
                PlaybookLifecycle.RESEARCH,
                Course49SystemStrategy.metadata.data_requirements,
                0.10,
                "RECOVERY",
            )
        )
        registry.register(playbook)
        weak = PlaybookValidationEvidence(249, 29, True, True, True, True)
        with self.assertRaises(ValueError):
            registry.promote("research_candidate", weak)
        strong = PlaybookValidationEvidence(250, 30, True, True, True, True)
        promoted = registry.promote("research_candidate", strong)
        self.assertEqual(promoted.lifecycle, PlaybookLifecycle.PRODUCTION)

    def test_pullback_playbook_accepts_healthy_divergence_and_rejects_acceleration(self) -> None:
        playbook = research_playbooks(Course49SystemStrategy.metadata.data_requirements)[0]
        accepted = playbook.evaluate(pullback_context())
        self.assertTrue(accepted.admitted)
        self.assertEqual(len(accepted.candidates), 1)
        candidate = accepted.candidates[0]
        self.assertEqual(candidate.playbook_id, LEADER_PULLBACK_PLAYBOOK_ID)
        self.assertAlmostEqual(candidate.target_weight, 0.10)
        self.assertGreater(candidate.stop_price or 0.0, candidate.evidence["price"] * 0.94)

        rejected = playbook.evaluate(pullback_context(phase="ACCELERATION"))
        self.assertFalse(rejected.admitted)
        self.assertFalse(rejected.candidates)

    def test_pullback_uses_raw_prices_for_stop_levels(self) -> None:
        playbook = research_playbooks(Course49SystemStrategy.metadata.data_requirements)[0]
        context = pullback_context()
        raw = context.raw_bars["000001.SZ"].copy()
        for column in ("Open", "High", "Low", "Close"):
            raw[column] *= 2.0
        result = playbook.evaluate(
            replace(context, raw_bars={"000001.SZ": raw})
        )

        candidate = result.candidates[0]
        self.assertAlmostEqual(candidate.evidence["price"], 23.6)
        self.assertGreater(candidate.stop_price or 0.0, 22.0)

    def test_pullback_exact_rules_cover_limit_depth_volume_and_reclaim(self) -> None:
        front = pullback_frame()
        raw = front.copy()
        asof = front.index[-1]
        self.assertIsNotNone(
            _pullback_candidate_features(front, raw, "000001.SZ", asof)
        )

        without_limit = raw.copy()
        without_limit.iloc[22, without_limit.columns.get_loc("Close")] = 10.8
        self.assertIsNone(
            _pullback_candidate_features(front, without_limit, "000001.SZ", asof)
        )

        shallow = front.copy()
        shallow.iloc[-1, shallow.columns.get_loc("Close")] = 12.05
        self.assertIsNone(
            _pullback_candidate_features(shallow, raw, "000001.SZ", asof)
        )

        high_volume = front.copy()
        high_volume.iloc[-3:-1, high_volume.columns.get_loc("Volume")] = 2_000_000.0
        self.assertIsNone(
            _pullback_candidate_features(high_volume, raw, "000001.SZ", asof)
        )

        below_ma5 = front.copy()
        below_ma5.iloc[-1, below_ma5.columns.get_loc("Close")] = 11.5
        below_ma5.iloc[-1, below_ma5.columns.get_loc("Open")] = 11.45
        self.assertIsNone(
            _pullback_candidate_features(below_ma5, raw, "000001.SZ", asof)
        )

        limit_down = raw.copy()
        limit_down.iloc[-3, limit_down.columns.get_loc("Close")] = 10.9
        self.assertIsNone(
            _pullback_candidate_features(front, limit_down, "000001.SZ", asof)
        )

    def test_pullback_only_uses_top_three_themes_and_tie_breaks_by_code(self) -> None:
        playbook = research_playbooks(Course49SystemStrategy.metadata.data_requirements)[0]
        context = pullback_context()
        sectors = tuple(
            {
                "sector_code": f"T00{index}",
                "sector_name": f"sector-{index}",
                "rank": index,
                "score": 0.80,
                "theme_phase": "DIVERGENCE",
            }
            for index in range(1, 5)
        )
        fourth_only = replace(
            context,
            sectors=sectors,
            sector_members={
                "T001": {"members": []},
                "T002": {"members": []},
                "T003": {"members": []},
                "T004": {"members": ["000001.SZ"]},
            },
        )
        self.assertFalse(playbook.evaluate(fourth_only).candidates)

        frame = pullback_frame()
        tied = replace(
            context,
            front_bars={"000002.SZ": frame, "000001.SZ": frame},
            raw_bars={"000002.SZ": frame, "000001.SZ": frame},
            sector_members={
                "T001": {"members": ["000002.SZ", "000001.SZ"]}
            },
        )
        candidates = playbook.evaluate(tied).candidates
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].code, "000001.SZ")

    def test_pullback_playbook_rejects_capital_risk_and_does_not_look_ahead(self) -> None:
        playbook = research_playbooks(Course49SystemStrategy.metadata.data_requirements)[0]
        baseline = playbook.evaluate(pullback_context())
        future = playbook.evaluate(pullback_context(future=True))
        self.assertEqual(
            [(item.code, item.score, item.stop_price) for item in baseline.candidates],
            [(item.code, item.score, item.stop_price) for item in future.candidates],
        )
        with patch(
            "research_platform.strategies.course49_system.latest_lhb_features",
            return_value=SimpleNamespace(risk="CAPITAL_DISTRIBUTION"),
        ):
            blocked = playbook.evaluate(pullback_context())
        self.assertTrue(blocked.admitted)
        self.assertFalse(blocked.candidates)

    def test_pullback_candidate_matrix_is_point_in_time(self) -> None:
        frame = pullback_frame()
        eligibility = pd.DataFrame(True, index=frame.index, columns=["000001.SZ"])
        matrix = build_leader_pullback_candidate_matrix(
            {"000001.SZ": frame},
            {"000001.SZ": frame},
            {"000001.SZ": "sample"},
            eligibility,
        )
        self.assertTrue(bool(matrix.iloc[-1, 0]))

    def test_pullback_exit_has_structure_and_time_limits(self) -> None:
        state: dict[str, object] = {}
        state, reason = update_pullback_exit_state(
            state,
            price=10.0,
            entry_price=10.0,
            below_ma5=True,
            below_ma10=True,
            market_weak=False,
            sector_weak=False,
        )
        self.assertEqual(reason, "")
        _, reason = update_pullback_exit_state(
            state,
            price=9.9,
            entry_price=10.0,
            below_ma5=True,
            below_ma10=True,
            market_weak=False,
            sector_weak=False,
        )
        self.assertEqual(reason, "PULLBACK_STRUCTURE_BROKEN")

        state = {}
        for _ in range(5):
            state, reason = update_pullback_exit_state(
                state,
                price=10.1,
                entry_price=10.0,
                below_ma5=False,
                below_ma10=False,
                market_weak=False,
                sector_weak=False,
            )
        self.assertEqual(reason, "PULLBACK_TIME_EXIT")

        state = {}
        for _ in range(2):
            state, reason = update_pullback_exit_state(
                state,
                price=10.1,
                entry_price=10.0,
                below_ma5=False,
                below_ma10=False,
                market_weak=True,
                sector_weak=False,
            )
        self.assertEqual(reason, "PULLBACK_MARKET_WEAK_CONFIRMED")

        _, reason = update_pullback_exit_state(
            {"max_close": 10.8},
            price=10.36,
            entry_price=10.0,
            below_ma5=False,
            below_ma10=False,
            market_weak=False,
            sector_weak=False,
        )
        self.assertEqual(reason, "")
        _, reason = update_pullback_exit_state(
            {"max_close": 10.81},
            price=10.37,
            entry_price=10.0,
            below_ma5=False,
            below_ma10=False,
            market_weak=False,
            sector_weak=False,
        )
        self.assertEqual(reason, "PULLBACK_TRAILING_PROFIT")

    def test_research_playbook_is_not_in_default_production_set(self) -> None:
        strategy = Course49SystemStrategy()
        production_ids = {
            item.metadata.playbook_id for item in strategy.playbook_registry.production()
        }
        all_ids = {item.metadata.playbook_id for item in strategy.playbooks}
        self.assertNotIn(LEADER_PULLBACK_PLAYBOOK_ID, production_ids)
        self.assertIn(LEADER_PULLBACK_PLAYBOOK_ID, all_ids)

    def test_research_playbook_requires_explicit_scan_selection(self) -> None:
        strategy = Course49SystemStrategy()
        context = pullback_context()
        arguments = {
            "run_id": "pullback-research",
            "front_bars": context.front_bars,
            "raw_bars": context.raw_bars,
            "names": {"000001.SZ": "sample"},
            "sector_members": context.sector_members,
            "positions": [],
            "benchmark_bars": {},
            "asof": context.asof,
            "eligible_codes": {"000001.SZ"},
        }
        with (
            patch.object(strategy, "analyze_market", return_value=context.market),
            patch.object(strategy, "infer_style", return_value=context.style),
            patch.object(strategy, "rank_sectors", return_value=list(context.sectors)),
            patch.object(strategy, "rank_leaders", return_value=[]),
        ):
            default_result = strategy.scan(**arguments)
            research_result = strategy.scan(
                **arguments,
                playbook_ids=[LEADER_PULLBACK_PLAYBOOK_ID],
            )
        self.assertFalse([item for item in default_result.signals if item.side == "BUY"])
        buys = [item for item in research_result.signals if item.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].playbook_id, LEADER_PULLBACK_PLAYBOOK_ID)

    def test_system_signal_matches_v2_policy_on_same_context(self) -> None:
        index = pd.date_range("2025-12-01", periods=45, freq="B")
        frame = pd.DataFrame(
            {"Close": [10.0] * 45, "Volume": [1_000_000.0] * 45},
            index=index,
        )
        sector = {
            "sector_code": "T001",
            "sector_name": "sample-sector",
            "rank": 1,
            "score": 0.80,
            "theme_phase": "FERMENT",
        }
        arguments = {
            "run_id": "same-snapshot",
            "front_bars": {"000001.SZ": frame},
            "raw_bars": {"000001.SZ": frame},
            "names": {"000001.SZ": "sample"},
            "sector_members": {"T001": {"name": "sample-sector", "members": ["000001.SZ"]}},
            "positions": [],
            "benchmark_bars": {},
            "asof": index[-1],
            "eligible_codes": {"000001.SZ"},
        }
        old = Course49V2Strategy()
        system = Course49SystemStrategy()
        with (
            patch.object(old, "analyze_market", return_value=market()),
            patch.object(old, "infer_style", return_value=style()),
            patch.object(old, "rank_sectors", return_value=[sector]),
            patch.object(old, "rank_leaders", return_value=[leader()]),
            patch.object(system, "analyze_market", return_value=market()),
            patch.object(system, "infer_style", return_value=style()),
            patch.object(system, "rank_sectors", return_value=[sector]),
            patch.object(system, "rank_leaders", return_value=[leader()]),
        ):
            old_result = old.scan(**arguments)
            system_result = system.scan(
                **arguments,
                context_metadata={"market_count": 5000, "eligible_count": 4500},
            )

        self.assertEqual(len(old_result.signals), 1)
        self.assertEqual(len(system_result.signals), 1)
        old_signal = old_result.signals[0]
        new_signal = system_result.signals[0]
        self.assertEqual((new_signal.code, new_signal.side), (old_signal.code, old_signal.side))
        self.assertAlmostEqual(new_signal.strength, old_signal.strength)
        self.assertAlmostEqual(new_signal.target_weight, old_signal.target_weight)
        self.assertEqual(new_signal.reason_codes, old_signal.reason_codes)
        self.assertEqual(new_signal.framework_id, FRAMEWORK_ID)
        self.assertEqual(new_signal.playbook_id, "ferment_second_board")
        self.assertEqual(new_signal.policy_version, POLICY_VERSION)
        self.assertEqual(system_result.state["funnel"]["market"], 5000)
        self.assertEqual(system_result.state["funnel"]["eligible"], 4500)
        self.assertEqual(system_result.state["funnel"]["routed"], 1)

    def test_router_is_deterministic_and_deduplicates_by_code(self) -> None:
        first = PlaybookCandidate(
            "recovery_ignition", "000001.SZ", 0.80, 0.15, (), {}, leader()
        )
        second = PlaybookCandidate(
            "ferment_second_board", "000001.SZ", 0.90, 0.25, (), {}, leader()
        )
        routed, audit = Course49Router().route(
            (
                PlaybookResult("recovery_ignition", True, (first,), ()),
                PlaybookResult("ferment_second_board", True, (second,), ()),
            )
        )
        self.assertEqual([item.playbook_id for item in routed], ["ferment_second_board"])
        self.assertEqual([item["status"] for item in audit], ["ROUTED", "DEDUPED"])

    def test_schema_v9_persists_playbook_state_and_freezes_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config: PlatformConfig = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            database.save_backtest_state(
                "bt-system",
                "course49_system",
                "2026-01-30",
                {
                    "market_phase": "FERMENT",
                    "market_style": "MIXED",
                    "style_suitability": 0.55,
                    "entry_allowed": True,
                    "funnel": {"routed": 1},
                    "playbook_states": [
                        {
                            "playbook_id": "ferment_second_board",
                            "lifecycle": "PRODUCTION",
                            "admitted": True,
                            "candidate_count": 1,
                            "routed_count": 1,
                            "budget": 0.1375,
                            "blocked_reasons": [],
                        }
                    ],
                },
            )
            rows = database.query(
                "SELECT * FROM backtest_playbook_states WHERE backtest_id='bt-system'"
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["routed_count"], 1)
        self.assertAlmostEqual(rows[0]["budget"], 0.1375)


if __name__ == "__main__":
    unittest.main()
