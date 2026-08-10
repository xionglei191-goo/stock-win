from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from research_platform.config import PlatformConfig
from research_platform.storage import Database
from research_platform.strategies.course49 import Course49Market
from research_platform.strategies.course49_system import (
    FRAMEWORK_ID,
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
    production_playbooks,
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
