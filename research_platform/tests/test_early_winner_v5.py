from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from research_platform.early_winner_research import ResearchDataBlockedError
from research_platform.early_winner_v5_research import (
    CLASSIFIER_RULE_HASH,
    EarlyWinnerV5ResearchService,
    FROZEN_VALIDATION_YEARS,
    FrozenValidationSealedError,
    PROTOCOL_HASH,
    PROTOCOL_VERSION,
    V5ProtocolChangeRequiresV6,
    assess_v5_frozen_validation,
    evaluate_v5_pair,
    frozen_validation_readiness,
    historical_universe_master_gate,
    load_frozen_validation_shards,
    prepare_v5_design_frame,
    replay_event_provenance,
    select_v5_candidates,
    validate_event_provenance,
)
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config


class EarlyWinnerV5Tests(unittest.TestCase):
    def test_project_and_strategy_are_permanently_research_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV5ResearchService(config, database)
            detail = service.detail()
            scan = service.strategy.scan(asof="2026-08-12")
            stored = database.query(
                "SELECT category, lifecycle FROM research_projects WHERE project_id=?",
                ("early_winner_v5",),
            )[0]
            database.execute(
                "UPDATE research_projects SET category='independent', lifecycle='LIVE' "
                "WHERE project_id='early_winner_v5'"
            )
            EarlyWinnerV5ResearchService(config, database)
            repaired = database.query(
                "SELECT category, lifecycle FROM research_projects WHERE project_id=?",
                ("early_winner_v5",),
            )[0]

        self.assertEqual(PROTOCOL_VERSION, "early-winner-v5-event-quiet-v1")
        self.assertEqual(detail["strategy"]["lifecycle"], "RESEARCH_ONLY")
        self.assertEqual(stored["category"], "research_project")
        self.assertEqual(stored["lifecycle"], "RESEARCH_ONLY")
        self.assertEqual(repaired["category"], "research_project")
        self.assertEqual(repaired["lifecycle"], "RESEARCH_ONLY")
        self.assertFalse(detail["frozen_validation_opened"])
        self.assertFalse(detail["promotion_allowed"])
        self.assertEqual(scan.signals, ())
        self.assertEqual(scan.candidates, ())
        self.assertFalse(scan.state["trade_signals_enabled"])

    def test_event_replay_is_deterministic_and_hard_negative_has_priority(self) -> None:
        asof = "2023-06-30"
        positive = self._event("positive", "ACQUISITION", 3.0, "2023-06-28T15:00:00")
        negative = self._event("negative", "REDUCTION", -2.0, "2023-06-20T15:00:00")

        first = replay_event_provenance([positive, negative], asof)
        second = replay_event_provenance([negative, positive], asof)

        self.assertEqual(first, second)
        self.assertEqual(first["selected_event_type"], "REDUCTION")
        self.assertEqual(first["selected_event_score"], -2.0)
        self.assertEqual(first["classifier_rule_hash"], CLASSIFIER_RULE_HASH)
        self.assertEqual(
            first["hard_negative_event_hashes"], [negative["event_hash"]]
        )

    def test_event_provenance_rejects_schema_tampering_and_duplicate_hashes(self) -> None:
        row = self._candidate_row(
            code="600000.SH",
            amount_ratio=0.8,
            event_type="BUYBACK",
            event_score=1.0,
            effective_at="2023-06-28T15:00:00",
        )
        valid = validate_event_provenance(pd.DataFrame([row]))
        tampered = dict(row)
        tampered["selected_event_score"] = 3.0
        invalid = validate_event_provenance(pd.DataFrame([tampered]))
        missing = validate_event_provenance(
            pd.DataFrame([{key: value for key, value in row.items() if key != "event_replay_hash"}])
        )
        event = self._event("same", "BUYBACK", 1.0, "2023-06-28T15:00:00")
        untrusted = dict(event)
        untrusted["source_url"] = "https://example.com/fake.pdf"

        self.assertTrue(valid["ready"])
        self.assertFalse(invalid["ready"])
        self.assertIn("selected_event_score", invalid["errors"][0])
        self.assertEqual(missing["status"], "SCHEMA_INCOMPLETE")
        with self.assertRaises(ValueError):
            replay_event_provenance([event, event], "2023-06-30")
        with self.assertRaises(ValueError):
            replay_event_provenance([untrusted], "2023-06-30")

    def test_selection_is_positive_only_ordered_capped_and_does_not_look_at_entry(self) -> None:
        rows: list[dict[str, object]] = []
        for index in range(26):
            rows.append(
                self._candidate_row(
                    code=f"{600000 + index:06d}.SH",
                    amount_ratio=0.50 + index / 100.0,
                    event_type="ACQUISITION" if index < 22 else "BUYBACK",
                    event_score=3.0 if index < 22 else 1.0,
                    effective_at=(
                        "2023-06-29T15:00:00"
                        if index % 2 == 0
                        else "2023-06-28T15:00:00"
                    ),
                    industry="A" if index < 10 else f"I{index}",
                    entry_executable=index != 0,
                )
            )
        rows.append(
            self._candidate_row(
                code="688888.SH",
                amount_ratio=0.01,
                event_type="REDUCTION",
                event_score=-2.0,
                effective_at="2023-06-29T15:00:00",
                industry="Z",
            )
        )
        no_event = self._base_row("688889.SH", amount_ratio=0.01, industry="Z")
        no_event.update(replay_event_provenance([], no_event["asof"]))
        rows.append(no_event)

        selected = select_v5_candidates(pd.DataFrame(rows))

        self.assertEqual(len(selected), 20)
        self.assertLessEqual(int((selected["industry"] == "A").sum()), 5)
        self.assertTrue((selected["selected_event_score"] > 0).all())
        self.assertNotIn("688888.SH", set(selected["code"]))
        self.assertNotIn("688889.SH", set(selected["code"]))
        self.assertEqual(selected.iloc[0]["code"], "600000.SH")
        self.assertFalse(bool(selected.iloc[0]["entry_executable"]))
        self.assertEqual(selected["v5_rank"].tolist(), list(range(1, 21)))

    def test_prepare_design_keeps_unexecutable_top_slot_for_cash_not_refill(self) -> None:
        rows = [
            self._candidate_row(
                code="600000.SH",
                amount_ratio=0.50,
                event_type="ACQUISITION",
                event_score=3.0,
                effective_at="2023-06-29T15:00:00",
                entry_executable=False,
            ),
            self._candidate_row(
                code="600001.SH",
                amount_ratio=0.60,
                event_type="ACQUISITION",
                event_score=3.0,
                effective_at="2023-06-29T15:00:00",
                entry_executable=True,
            ),
        ]

        prepared = prepare_v5_design_frame(
            pd.DataFrame(rows), require_all_design_years=False
        ).sort_values("v5_selection_score", ascending=False)

        self.assertEqual(prepared.iloc[0]["code"], "600000.SH")
        self.assertFalse(bool(prepared.iloc[0]["entry_executable"]))
        self.assertTrue(bool(prepared.iloc[0]["v5_candidate_eligible"]))

    def test_evaluation_uses_v4_paired_eight_phase_cash_and_cost_ledger(self) -> None:
        rows: list[dict[str, object]] = []
        for week, decision in enumerate(pd.date_range("2023-01-06", periods=16, freq="W-FRI")):
            asof = decision.date().isoformat()
            for slot in range(2):
                row = self._candidate_row(
                    code=f"{600000 + slot:06d}.SH",
                    amount_ratio=0.5 + slot,
                    event_type="ACQUISITION",
                    event_score=3.0,
                    effective_at=f"{asof}T15:00:00",
                    industry=f"I{slot}",
                    entry_executable=not (slot == 0 and week % 2 == 0),
                    asof=asof,
                )
                entry = decision + pd.offsets.BDay(1)
                exit_at = entry + pd.offsets.BDay(40)
                row.update(
                    {
                        "target": int((week + slot) % 2 == 0),
                        "forward_return_40": 0.05 if slot == 0 else -0.01,
                        "relative_return_60": 0.2 - slot / 10.0,
                        "planned_entry_time": entry.isoformat(),
                        "entry_time": (
                            entry.isoformat() if row["entry_executable"] else None
                        ),
                        "planned_exit_time": exit_at.isoformat(),
                        "exit_time": (
                            exit_at.isoformat() if row["entry_executable"] else None
                        ),
                    }
                )
                rows.append(row)

        candidate, baseline = evaluate_v5_pair(pd.DataFrame(rows))

        self.assertEqual(candidate["phase_count"], 8)
        self.assertEqual(baseline["phase_count"], 8)
        self.assertEqual(
            candidate["return_policy"],
            "EIGHT_PHASE_NON_OVERLAPPING_FULL_EXIT_REBUILD",
        )
        self.assertEqual(
            candidate["paired_cycle_policy"],
            "JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY",
        )
        self.assertEqual(candidate["unfilled_slot_policy"], "CASH_NO_REFILL")
        self.assertEqual(
            candidate["cost_policy"],
            "20BPS_ROUND_TRIP_PER_FILLED_SLOT; DOUBLE=40BPS",
        )
        candidate_cycles = candidate["phase_metrics"][0]["cycles"]
        baseline_cycles = baseline["phase_metrics"][0]["cycles"]
        self.assertEqual(
            [
                (item["asof"], item["joint_capital_available_at"])
                for item in candidate_cycles
            ],
            [
                (item["asof"], item["joint_capital_available_at"])
                for item in baseline_cycles
            ],
        )
        self.assertGreater(candidate["phase_metrics"][0]["cash_slots"], 0)

    def test_design_rejects_every_non_design_year_including_observation(self) -> None:
        row = self._candidate_row(
            code="600000.SH",
            amount_ratio=0.8,
            event_type="BUYBACK",
            event_score=1.0,
            effective_at="2023-06-28T15:00:00",
        )
        row["asof"] = "2024-01-05"

        with self.assertRaises(ResearchDataBlockedError):
            prepare_v5_design_frame(
                pd.DataFrame([row]), require_all_design_years=False
            )
        row["asof"] = "2026-01-09"
        with self.assertRaises(ResearchDataBlockedError):
            prepare_v5_design_frame(
                pd.DataFrame([row]), require_all_design_years=False
            )

    def test_frozen_loader_never_calls_reader_until_every_gate_is_ready(self) -> None:
        calls: list[object] = []

        def reader(value: object) -> pd.DataFrame:
            calls.append(value)
            year = int(str(value))
            return pd.DataFrame([{"asof": f"{year}-01-05", "code": "600000.SH"}])

        gates = self._frozen_gates()
        gates["event_provenance"] = {"ready": False}
        with self.assertRaises(FrozenValidationSealedError):
            load_frozen_validation_shards(
                {2024: "2024", 2025: "2025"}, gates=gates, reader=reader
            )
        self.assertEqual(calls, [])

        gates = self._frozen_gates()
        loaded = load_frozen_validation_shards(
            {2024: "2024", 2025: "2025"}, gates=gates, reader=reader
        )
        self.assertEqual(calls, ["2024", "2025"])
        self.assertEqual(loaded["asof"].tolist(), ["2024-01-05", "2025-01-05"])

    def test_protocol_change_is_v6_and_happens_before_frozen_reader(self) -> None:
        calls: list[object] = []
        with self.assertRaises(V5ProtocolChangeRequiresV6):
            load_frozen_validation_shards(
                {2024: "sealed-2024", 2025: "sealed-2025"},
                gates=self._frozen_gates(),
                protocol_hash="0" * 64,
                reader=lambda value: calls.append(value) or pd.DataFrame(),
            )

        self.assertEqual(calls, [])

    def test_frozen_gate_distinguishes_inconclusive_rejected_and_observation(self) -> None:
        candidate = {year: self._metrics(candidate=True) for year in FROZEN_VALIDATION_YEARS}
        baseline = {year: self._metrics(candidate=False) for year in FROZEN_VALIDATION_YEARS}

        passed = assess_v5_frozen_validation(candidate, baseline)
        candidate[2024] = self._metrics(candidate=True, invested_periods=1)
        inconclusive = assess_v5_frozen_validation(candidate, baseline)
        candidate[2024] = self._metrics(candidate=True, precision=0.05)
        rejected = assess_v5_frozen_validation(candidate, baseline)

        self.assertEqual(passed["status"], "OBSERVATION_ONLY")
        self.assertFalse(passed["promotion_allowed"])
        self.assertEqual(inconclusive["status"], "INCONCLUSIVE_SAMPLE")
        self.assertEqual(rejected["status"], "VALIDATION_REJECTED")

    def test_historical_master_must_cover_design_and_frozen_years(self) -> None:
        gate = self._master_gate(coverage_end="2023-12-31")

        design = historical_universe_master_gate(gate, through_year=2023)
        frozen = historical_universe_master_gate(gate, through_year=2025)
        readiness = frozen_validation_readiness(
            {**self._frozen_gates(), "historical_universe_master": gate}
        )

        self.assertTrue(design["ready"])
        self.assertFalse(frozen["ready"])
        self.assertFalse(readiness["ready"])

    def test_master_gate_uses_verified_store_not_raw_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "security_master").mkdir()
            (root / "security_master" / "current.json").write_text(
                '{"ready":true,"coverage_end":"2099-12-31"}', encoding="utf-8"
            )
            with patch(
                "research_platform.early_winner_v5_research.historical_universe_master_gate",
                wraps=historical_universe_master_gate,
            ):
                from research_platform.early_winner_v5_research import (
                    read_historical_universe_master_gate,
                )

                gate = read_historical_universe_master_gate(root)

        self.assertFalse(gate["ready"])
        self.assertIn(
            gate["status"],
            {
                "ARTIFACT_INVALID",
                "MASTER_VERIFIER_UNAVAILABLE",
                "MASTER_AUDIT_FAILED",
            },
        )

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _event(
        cls,
        identity: str,
        event_type: str,
        event_score: float,
        effective_at: str,
    ) -> dict[str, object]:
        return {
            "event_hash": cls._hash(identity),
            "source_url": f"https://www.cninfo.com.cn/{cls._hash(identity)}",
            "event_type": event_type,
            "event_score": event_score,
            "published_at": effective_at,
            "effective_at": effective_at,
        }

    @classmethod
    def _base_row(
        cls,
        code: str,
        *,
        amount_ratio: float,
        industry: str = "I",
        entry_executable: bool = True,
        asof: str = "2023-06-30",
    ) -> dict[str, object]:
        return {
            "code": code,
            "asof": asof,
            "industry": industry,
            "amount_ratio": amount_ratio,
            "relative_return_60": 0.10,
            "v4_eligible": True,
            "target": 1,
            "evaluation_period": True,
            "label_window_matured": True,
            "entry_executable": entry_executable,
            "forward_return_40": 0.10,
            "planned_entry_time": "2023-07-03T09:30:00",
            "entry_time": "2023-07-03T09:30:00" if entry_executable else None,
            "planned_exit_time": "2023-08-28T09:30:00",
            "exit_time": "2023-08-28T09:30:00" if entry_executable else None,
        }

    @classmethod
    def _candidate_row(
        cls,
        *,
        code: str,
        amount_ratio: float,
        event_type: str,
        event_score: float,
        effective_at: str,
        industry: str = "I",
        entry_executable: bool = True,
        asof: str = "2023-06-30",
    ) -> dict[str, object]:
        row = cls._base_row(
            code,
            amount_ratio=amount_ratio,
            industry=industry,
            entry_executable=entry_executable,
            asof=asof,
        )
        row.update(
            replay_event_provenance(
                [
                    cls._event(
                        f"{code}-{event_type}-{effective_at}",
                        event_type,
                        event_score,
                        effective_at,
                    )
                ],
                row["asof"],
            )
        )
        return row

    @classmethod
    def _master_gate(cls, *, coverage_end: str = "2025-12-31") -> dict[str, object]:
        return {
            "ready": True,
            "status": "READY",
            "detail": "fixture",
            "snapshot_id": "master-fixture",
            "manifest_hash": cls._hash("master"),
            "protocol_version": "historical-security-master-v1",
            "coverage_start": "2018-01-01",
            "coverage_end": coverage_end,
            "source_counts": {},
            "reconciliation": {},
            "promotion_blocked": False,
        }

    @classmethod
    def _frozen_gates(cls) -> dict[str, object]:
        content_hash = cls._hash("fixture")
        return {
            "preregistration": {
                "ready": True,
                "protocol_version": PROTOCOL_VERSION,
                "protocol_hash": PROTOCOL_HASH,
            },
            "historical_universe_master": cls._master_gate(),
            "event_provenance": {
                "ready": True,
                "schema_version": "early-winner-v5-event-replay-v1",
                "classifier_rule_hash": CLASSIFIER_RULE_HASH,
                "snapshot_hash": content_hash,
            },
            "trading_calendar": {"ready": True, "content_hash": content_hash},
            "execution_status": {"ready": True, "content_hash": content_hash},
            "label_snapshot": {
                "ready": True,
                "snapshot_hash": content_hash,
                "return_column": "forward_return_40",
            },
            "frozen_snapshot": {
                "ready": True,
                "sealed": True,
                "years": [2024, 2025],
                "protocol_hash": PROTOCOL_HASH,
                "manifest_hash": content_hash,
            },
        }

    @staticmethod
    def _metrics(
        *,
        candidate: bool,
        invested_periods: int = 2,
        precision: float | None = None,
    ) -> dict[str, object]:
        total_return = 0.06 if candidate else 0.01
        double_cost = 0.04 if candidate else 0.005
        drawdown = -0.08 if candidate else -0.10
        phase_metrics = []
        for phase in range(8):
            cycles = [
                {
                    "asof": f"2024-{phase + 1:02d}-{1 + cycle:02d}",
                    "planned_entry_at": f"2024-{phase + 1:02d}-{2 + cycle:02d}T09:30:00",
                    "joint_capital_available_at": f"2024-{phase + 2:02d}-{5 + cycle:02d}T00:00:00",
                    "filled_slots": 20 if cycle < invested_periods else 0,
                }
                for cycle in range(3)
            ]
            phase_metrics.append(
                {
                    "phase": phase,
                    "periods": 3,
                    "invested_periods": invested_periods,
                    "total_return": total_return,
                    "double_cost_return": double_cost,
                    "max_drawdown": drawdown,
                    "cycles": cycles,
                }
            )
        return {
            "protocol_hash": PROTOCOL_HASH,
            "return_policy": "EIGHT_PHASE_NON_OVERLAPPING_FULL_EXIT_REBUILD",
            "paired_cycle_policy": "JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY",
            "unfilled_slot_policy": "CASH_NO_REFILL",
            "cost_policy": "20BPS_ROUND_TRIP_PER_FILLED_SLOT; DOUBLE=40BPS",
            "drawdown_policy": "CYCLE_ENDPOINT_NAV_INCLUDING_INITIAL_1.0",
            "phase_count": 8,
            "min_phase_periods": 3,
            "min_phase_invested_periods": invested_periods,
            "precision_at_20": (
                precision if precision is not None else (0.15 if candidate else 0.10)
            ),
            "pr_auc": 0.15 if candidate else 0.10,
            "worst_phase_total_return": total_return,
            "worst_phase_double_cost_return": double_cost,
            "worst_phase_max_drawdown": drawdown,
            "phase_metrics": phase_metrics,
        }


if __name__ == "__main__":
    unittest.main()
