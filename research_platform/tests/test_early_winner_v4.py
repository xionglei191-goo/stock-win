from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research_platform.early_winner_v4_research import (
    EarlyWinnerV4ResearchService,
    NON_OVERLAP_PHASES,
    ResearchDataBlockedError,
    EXECUTION_STATUS_FIELDS,
    EXECUTION_STATUS_PROTOCOL_VERSION,
    RAW_END,
    RAW_START,
    _evaluate_v4_pair,
    _evaluate_v4_year,
    passes_v4_development_gate,
    prepare_v4_labels,
    profile_v4_data,
    _embargo_by_trading_calendar,
    _purge_completed_outcomes,
)
from research_platform.early_winner_research import _field_values
from research_platform.delisted_history_quality import (
    audit_delisted_history,
)
from research_platform.storage import Database
from research_platform.strategies.early_winner import attach_execution_outcomes
from research_platform.tests.helpers import temporary_config
from research_platform.tests.test_delisted_history_quality import _SyntheticEvidence


class EarlyWinnerV4Tests(unittest.TestCase):
    @staticmethod
    def _ready_master_gate() -> dict[str, object]:
        return {
            "ready": True,
            "status": "READY",
            "detail": "synthetic ready master",
            "snapshot_id": "a" * 64,
            "manifest_hash": "a" * 64,
        }

    @staticmethod
    def _publish_ready_delisted_audit(
        config: object, master_gate: dict[str, object]
    ) -> dict[str, object]:
        input_root = (
            config.runtime_dir
            / "research"
            / "early_winner_v4"
            / "delisted_history_inputs"
        )
        fixture = _SyntheticEvidence(
            config.runtime_dir / "synthetic_delisted_fixture",
            master_root=config.runtime_dir / "security_master",
            input_cas_root=input_root,
        )
        master_gate.clear()
        master_gate.update(
            {
                "ready": True,
                "status": "READY",
                "detail": "synthetic ready master",
                "snapshot_id": fixture.master_identity["snapshot_id"],
                "manifest_hash": fixture.master_identity["manifest_hash"],
            }
        )
        return audit_delisted_history(
            master_records=fixture.master_records,
            master_identity=fixture.master_identity,
            source_indexes=fixture.source_indexes,
            input_cas_root=input_root,
            output_root=(
                config.runtime_dir
                / "research"
                / "early_winner_v4"
                / "delisted_history_quality"
            ),
        )

    def test_detail_reports_missing_delisted_history_gate_without_writing_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            master_gate = self._ready_master_gate()
            audit_root = (
                config.runtime_dir
                / "research"
                / "early_winner_v4"
                / "delisted_history_quality"
            )
            with patch.object(
                service, "_historical_universe_gate", return_value=master_gate
            ):
                detail = service.detail()

            self.assertEqual(detail["status"], "BLOCKED_DATA")
            self.assertEqual(
                detail["data_gates"]["delisted_history_quality"]["status"],
                "DELISTED_HISTORY_SOURCE_INCOMPLETE",
            )
            self.assertEqual(
                detail["data_gates"]["delisted_history_quality"][
                    "source_datasets"
                ],
                [],
            )
            self.assertEqual(
                len(
                    detail["data_gates"]["delisted_history_quality"][
                        "missing_source_datasets"
                    ]
                ),
                12,
            )
            self.assertEqual(
                detail["data_gates"]["delisted_history_quality"][
                    "required_source_dataset_count"
                ],
                12,
            )
            self.assertFalse(audit_root.exists())

    def test_build_blocks_on_delisted_history_before_tdx_or_source_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            master_gate = self._ready_master_gate()
            with (
                patch.object(
                    service, "_historical_universe_gate", return_value=master_gate
                ),
                patch(
                    "research_platform.early_winner_v4_research.TdxResearchHttpClient"
                ) as tdx_client,
                patch.object(service, "_v3_batches") as source_batches,
            ):
                with self.assertRaisesRegex(
                    ResearchDataBlockedError,
                    "DELISTED_HISTORY_SOURCE_INCOMPLETE",
                ):
                    service.build_label_snapshot()

            tdx_client.assert_not_called()
            source_batches.assert_not_called()
            project = database.query(
                "SELECT status, data_gates_json FROM research_projects WHERE project_id=?",
                ("early_winner_v4",),
            )[0]
            self.assertEqual(project["status"], "BLOCKED_DATA")
            self.assertEqual(
                json.loads(project["data_gates_json"])["delisted_history_quality"][
                    "status"
                ],
                "DELISTED_HISTORY_SOURCE_INCOMPLETE",
            )

    def test_development_audit_blocks_before_reading_label_shards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            with (
                patch.object(
                    service,
                    "_historical_universe_gate",
                    return_value=self._ready_master_gate(),
                ),
                patch.object(service, "_current_v4_batches") as current_batches,
                patch(
                    "research_platform.early_winner_v4_research.pd.read_parquet"
                ) as read_parquet,
            ):
                with self.assertRaisesRegex(
                    ResearchDataBlockedError,
                    "DELISTED_HISTORY_SOURCE_INCOMPLETE",
                ):
                    service.run_development_audit()

            current_batches.assert_not_called()
            read_parquet.assert_not_called()

    def test_ready_delisted_history_artifact_is_bound_to_current_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            master_gate = self._ready_master_gate()
            release = self._publish_ready_delisted_audit(config, master_gate)

            gate = service._delisted_history_gate(
                historical_master_gate=master_gate
            )

            self.assertTrue(gate["ready"])
            self.assertEqual(gate["status"], "READY")
            self.assertEqual(gate["manifest_hash"], release["manifest_hash"])
            self.assertEqual(gate["report_hash"], release["report_hash"])
            stale_master = {**master_gate, "snapshot_id": "c" * 64}
            stale = service._delisted_history_gate(
                historical_master_gate=stale_master
            )
            self.assertEqual(stale["status"], "DELISTED_HISTORY_ARTIFACT_INVALID")
            self.assertFalse(stale["ready"])

    def test_tampered_delisted_history_report_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            master_gate = self._ready_master_gate()
            release = self._publish_ready_delisted_audit(config, master_gate)
            Path(str(release["report_path"])).write_bytes(b"tampered")

            gate = service._delisted_history_gate(
                historical_master_gate=master_gate
            )

            self.assertEqual(gate["status"], "DELISTED_HISTORY_ARTIFACT_INVALID")
            self.assertFalse(gate["ready"])

    def test_tampered_delisted_source_index_fails_full_v4_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            master_gate = self._ready_master_gate()
            release = self._publish_ready_delisted_audit(config, master_gate)
            manifest = json.loads(
                Path(str(release["manifest_path"])).read_text(encoding="utf-8")
            )
            source_path = Path(
                manifest["source_indexes"]["raw_execution_bars"]["object_path"]
            )
            source_path.write_bytes(b"tampered-after-audit")

            gate = service._delisted_history_gate(
                historical_master_gate=master_gate
            )

            self.assertEqual(gate["status"], "DELISTED_HISTORY_ARTIFACT_INVALID")
            self.assertFalse(gate["ready"])
            self.assertIn("source index", gate["detail"])

    def test_tampered_delisted_raw_source_fails_full_v4_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            master_gate = self._ready_master_gate()
            release = self._publish_ready_delisted_audit(config, master_gate)
            manifest = json.loads(
                Path(str(release["manifest_path"])).read_text(encoding="utf-8")
            )
            index_path = Path(
                manifest["source_indexes"]["raw_execution_bars"]["object_path"]
            )
            source_index = json.loads(index_path.read_text(encoding="utf-8"))
            raw_path = Path(
                source_index["partitions"][0]["raw_sources"][0]["object_path"]
            )
            raw_path.write_bytes(b"tampered-raw-source-after-audit")

            gate = service._delisted_history_gate(
                historical_master_gate=master_gate
            )

            self.assertEqual(gate["status"], "DELISTED_HISTORY_ARTIFACT_INVALID")
            self.assertFalse(gate["ready"])
            self.assertIn("full source replay", gate["detail"])

    def test_status_builder_rejects_response_missing_requested_code(self) -> None:
        class Client:
            @staticmethod
            def call(method: str, params: object) -> dict[str, object]:
                return {}

        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)

            with self.assertRaises(ResearchDataBlockedError):
                service._build_execution_status_files(
                    Client(), ["600000.SH"], progress_callback=None
                )

    def test_status_builder_repairs_corrupt_cache_atomically(self) -> None:
        code = "600000.SH"

        class Client:
            calls = 0

            @classmethod
            def call(cls, method: str, params: object) -> dict[str, object]:
                cls.calls += 1
                return {code: self._status_rpc_node()}

        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            cache = (
                config.runtime_dir
                / "research"
                / "early_winner_v4"
                / "execution_status"
                / f"{EXECUTION_STATUS_PROTOCOL_VERSION}_{RAW_START}_{RAW_END}"
                / "600000_SH.json"
            )
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text("{", encoding="utf-8")

            files = service._build_execution_status_files(
                Client(), [code], progress_callback=None
            )
            repaired = json.loads(files[code].read_text(encoding="utf-8"))

        self.assertEqual(Client.calls, 1)
        self.assertEqual(repaired["values"]["GP15"][0]["value"], 2.0)

    def test_status_builder_rejects_gp15_conflict_and_preserves_gp30_multivalue(self) -> None:
        code = "600000.SH"

        class ConflictClient:
            @staticmethod
            def call(method: str, params: object) -> dict[str, object]:
                node = self._status_rpc_node()
                node["GP15"] = [
                    {"Date": "20230103", "Value": [2.0, 100.0]},
                    {"Date": "20230103", "Value": [-2.0, 100.0]},
                ]
                return {code: node}

        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            with self.assertRaises(ResearchDataBlockedError):
                service._build_execution_status_files(
                    ConflictClient(), [code], progress_callback=None
                )

        class MultiActionClient:
            @staticmethod
            def call(method: str, params: object) -> dict[str, object]:
                node = self._status_rpc_node()
                node["GP30"] = [
                    {"Date": "20230103", "Value": [2.0]},
                    {"Date": "20230103", "Value": [1.0]},
                ]
                return {code: node}

        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            files = service._build_execution_status_files(
                MultiActionClient(), [code], progress_callback=None
            )
            payload = json.loads(files[code].read_text(encoding="utf-8"))

        self.assertEqual(
            payload["values"]["GP30"],
            [
                {"date": "2023-01-03", "value": 1.0},
                {"date": "2023-01-03", "value": 2.0},
            ],
        )

    def test_status_profile_fails_below_ninety_nine_percent_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files: dict[str, Path] = {}
            for index in range(100):
                code = f"60{index:04d}.SH"
                values = {
                    field: [] for field in EXECUTION_STATUS_FIELDS
                }
                if index >= 2:
                    values["GP15"] = [{"date": "2023-01-03", "value": 0.0}]
                    values["GP30"] = [{"date": "2023-01-03", "value": 1.0}]
                path = root / f"{index}.json"
                path.write_text(json.dumps({"values": values}), encoding="utf-8")
                files[code] = path

            with self.assertRaises(ResearchDataBlockedError):
                EarlyWinnerV4ResearchService._execution_status_profile(files)

    @staticmethod
    def _status_rpc_node() -> dict[str, object]:
        return {
            "GP15": [{"Date": "20230103", "Value": [2.0, 100.0]}],
            "GP29": [{"Date": "20230103", "Value": [0.0, 2.0]}],
            "GP30": [{"Date": "20230103", "Value": [1.0]}],
            "GP43": [{"Date": "20230103", "Value": [5.0]}],
        }

    def test_gp15_parser_uses_state_component_not_seal_amount(self) -> None:
        payload = {
            "600000.SH": {
                "GP15": [{"Date": "20230103", "Value": [2.0, 123.4]}]
            }
        }

        parsed = _field_values(payload, "GP15", "600000.SH", component=0)

        self.assertEqual(parsed, [(pd.Timestamp("2023-01-03"), 2.0)])

    def test_40_day_outcome_uses_dynamic_return_column_and_raw_opens(self) -> None:
        dates = pd.bdate_range("2023-01-02", periods=50)
        bars = pd.DataFrame(
            {
                "Open": np.arange(10.0, 60.0),
                "High": np.arange(10.1, 60.1),
                "Low": np.arange(9.9, 59.9),
                "Close": np.arange(10.0, 60.0),
                "Volume": 1_000_000.0,
                "Amount": 20_000_000.0,
            },
            index=dates,
        )
        features = pd.DataFrame(
            [{"code": "600000.SH", "name": "测试", "asof": "2023-01-02", "adv20": 200_000_000.0}]
        )

        result = attach_execution_outcomes(
            features, {"600000.SH": bars}, holding_days=40
        ).iloc[0]

        self.assertTrue(result["entry_executable"])
        self.assertEqual(result["entry_time"], dates[1].isoformat())
        self.assertEqual(result["exit_time"], dates[41].isoformat())
        self.assertAlmostEqual(result["forward_return_40"], 51.0 / 11.0 - 1.0)
        self.assertNotIn("forward_return_60", result.index)

    def test_market_breadth_and_positive_top_decile_define_target(self) -> None:
        rows = []
        for index in range(20):
            rows.append(
                {
                    "code": f"600{index:03d}.SH",
                    "asof": "2023-06-30",
                    "listed_days": 300,
                    "valid_days_20": 20,
                    "adv20": 200_000_000.0,
                    "suspended": False,
                    "is_st": False,
                    "is_quit": False,
                    "execution_status_complete": True,
                    "return_60": index / 100.0,
                    "relative_return_60": index / 100.0,
                    "turnover_20": 0.01,
                    "price_to_ma60": 1.1,
                    "close": 11.0,
                    "ma60": 10.0,
                    "entry_executable": True,
                    "forward_return_40": (index - 10) / 100.0,
                }
            )

        result = prepare_v4_labels(pd.DataFrame(rows))

        self.assertTrue(result["v4_eligible"].all())
        self.assertGreater(result["market_breadth_ma60"].iloc[0], 0.50)
        self.assertTrue((result.loc[result["target"] == 1, "forward_return_40"] > 0).all())
        self.assertEqual(int(result["target"].sum()), 2)

    def test_bear_market_rows_are_not_training_or_evaluation_eligible(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "code": f"600{index:03d}.SH", "asof": "2023-06-30",
                    "listed_days": 300, "valid_days_20": 20, "adv20": 200_000_000.0,
                    "suspended": False, "is_st": False, "is_quit": False,
                    "execution_status_complete": True,
                    "return_60": 0.1, "relative_return_60": 0.1,
                    "turnover_20": 0.01, "price_to_ma60": 0.9,
                    "close": 9.0, "ma60": 10.0, "entry_executable": True,
                    "forward_return_40": 0.2,
                }
                for index in range(20)
            ]
        )

        result = prepare_v4_labels(frame)

        self.assertFalse(result["v4_eligible"].any())
        self.assertTrue(result["target"].isna().all())

    def test_decision_pool_does_not_filter_future_entry_failure(self) -> None:
        rows = []
        for index in range(20):
            rows.append(
                {
                    "code": f"600{index:03d}.SH",
                    "asof": "2023-06-30",
                    "listed_days": 300,
                    "valid_days_20": 20,
                    "adv20": 200_000_000.0,
                    "suspended": False,
                    "is_st": False,
                    "is_quit": False,
                    "execution_status_complete": True,
                    "return_60": index / 100.0,
                    "relative_return_60": index / 100.0,
                    "turnover_20": 0.01,
                    "price_to_ma60": 1.1,
                    "close": 11.0,
                    "ma60": 10.0,
                    "entry_executable": index != 0,
                    "forward_return_40": np.nan if index == 0 else index / 100.0,
                }
            )

        result = prepare_v4_labels(pd.DataFrame(rows))

        self.assertTrue(bool(result.loc[0, "v4_eligible"]))
        self.assertTrue(pd.isna(result.loc[0, "target"]))

    def test_incomplete_execution_status_never_reenters_decision_pool(self) -> None:
        rows = []
        for index in range(20):
            rows.append(
                {
                    "code": f"600{index:03d}.SH",
                    "asof": "2023-06-30",
                    "listed_days": 300,
                    "valid_days_20": 20,
                    "adv20": 200_000_000.0,
                    "suspended": False,
                    "is_st": False,
                    "is_quit": False,
                    "execution_status_complete": index != 0,
                    "return_60": 0.10,
                    "relative_return_60": 0.10,
                    "turnover_20": 0.01,
                    "price_to_ma60": 1.1,
                    "close": 11.0,
                    "ma60": 10.0,
                    "entry_executable": True,
                    "forward_return_40": 0.10,
                }
            )

        result = prepare_v4_labels(pd.DataFrame(rows))

        self.assertFalse(bool(result.loc[0, "v4_eligible"]))
        self.assertTrue(result.loc[1:, "v4_eligible"].all())
        self.assertTrue(pd.isna(result.loc[0, "target"]))

    def test_top20_is_ranked_before_fill_and_unfilled_slot_stays_cash(self) -> None:
        rows = []
        dates = pd.date_range("2023-02-03", periods=NON_OVERLAP_PHASES, freq="7D")
        for asof in dates:
            for index in range(21):
                executable = index != 0
                rows.append(
                    {
                        "code": f"600{index:03d}.SH",
                        "industry": f"industry-{index}",
                        "asof": asof.date().isoformat(),
                        "score": float(21 - index),
                        "evaluation_eligible": True,
                        "entry_executable": executable,
                        "planned_entry_time": (
                            asof + pd.Timedelta(days=3)
                        ).isoformat(),
                        "planned_exit_time": (
                            asof + pd.Timedelta(days=56)
                        ).isoformat(),
                        "exit_time": (asof + pd.Timedelta(days=56)).isoformat(),
                        "forward_return_40": (
                            np.nan if not executable else (10.0 if index == 20 else 0.10)
                        ),
                        "target": np.nan if not executable else 1.0,
                    }
                )

        metrics = _evaluate_v4_year(
            pd.DataFrame(rows), "score", "evaluation_eligible"
        )

        self.assertEqual(metrics["phase_count"], NON_OVERLAP_PHASES)
        for phase in metrics["phase_metrics"]:
            self.assertEqual(phase["periods"], 1)
            self.assertEqual(phase["selected_slots"], 20)
            self.assertEqual(phase["filled_slots"], 19)
            self.assertEqual(phase["cash_slots"], 1)
            self.assertAlmostEqual(phase["turnover"], 0.95)
            self.assertAlmostEqual(phase["total_return"], 0.095 - 0.95 * 0.002)

    def test_full_exit_rebuild_charges_every_cycle_despite_code_overlap(self) -> None:
        rows = []
        dates = pd.date_range("2023-02-03", periods=16, freq="7D")
        for asof in dates:
            for index in range(20):
                rows.append(
                    {
                        "code": f"600{index:03d}.SH",
                        "industry": f"industry-{index}",
                        "asof": asof.date().isoformat(),
                        "score": float(20 - index),
                        "evaluation_eligible": True,
                        "entry_executable": True,
                        "planned_entry_time": (
                            asof + pd.Timedelta(days=3)
                        ).isoformat(),
                        "planned_exit_time": (
                            asof + pd.Timedelta(days=56)
                        ).isoformat(),
                        "exit_time": (asof + pd.Timedelta(days=56)).isoformat(),
                        "forward_return_40": 0.01,
                        "target": float(index < 2),
                    }
                )

        metrics = _evaluate_v4_year(
            pd.DataFrame(rows), "score", "evaluation_eligible"
        )

        for phase in metrics["phase_metrics"]:
            self.assertEqual(phase["periods"], 2)
            self.assertAlmostEqual(phase["turnover"], 1.0)
            self.assertAlmostEqual(phase["total_return"], (1.0 + 0.008) ** 2 - 1.0)
            self.assertAlmostEqual(
                phase["double_cost_return"], (1.0 + 0.006) ** 2 - 1.0
            )

    def test_phase_drawdown_includes_initial_nav(self) -> None:
        asof = pd.Timestamp("2023-02-03")
        rows = [
            {
                "code": f"600{index:03d}.SH",
                "industry": f"industry-{index}",
                "asof": asof.date().isoformat(),
                "score": float(20 - index),
                "evaluation_eligible": True,
                "entry_executable": True,
                "planned_entry_time": (asof + pd.Timedelta(days=3)).isoformat(),
                "planned_exit_time": (asof + pd.Timedelta(days=56)).isoformat(),
                "exit_time": (asof + pd.Timedelta(days=56)).isoformat(),
                "forward_return_40": -0.10,
                "target": 0.0,
            }
            for index in range(20)
        ]

        metrics = _evaluate_v4_year(
            pd.DataFrame(rows), "score", "evaluation_eligible"
        )

        self.assertAlmostEqual(metrics["phase_metrics"][0]["max_drawdown"], -0.102)

    def test_phase_spacing_uses_planned_exit_not_eight_observed_weeks(self) -> None:
        rows = []
        dates = pd.date_range("2023-02-03", periods=16, freq="7D")
        for position, asof in enumerate(dates):
            holding_days = 70 if position == 0 else 56
            for index in range(20):
                rows.append(
                    {
                        "code": f"600{index:03d}.SH",
                        "industry": f"industry-{index}",
                        "asof": asof.date().isoformat(),
                        "score": float(20 - index),
                        "evaluation_eligible": True,
                        "entry_executable": True,
                        "planned_entry_time": (
                            asof + pd.Timedelta(days=3)
                        ).isoformat(),
                        "planned_exit_time": (
                            asof + pd.Timedelta(days=holding_days)
                        ).isoformat(),
                        "exit_time": (
                            asof + pd.Timedelta(days=holding_days)
                        ).isoformat(),
                        "forward_return_40": 0.01,
                        "target": float(index < 2),
                    }
                )

        metrics = _evaluate_v4_year(
            pd.DataFrame(rows), "score", "evaluation_eligible"
        )
        phase_zero = metrics["phase_metrics"][0]

        self.assertEqual(phase_zero["periods"], 2)
        self.assertEqual(
            [cycle["asof"] for cycle in phase_zero["cycles"]],
            [dates[0].date().isoformat(), dates[10].date().isoformat()],
        )

    def test_breadth_off_period_is_a_cash_cycle_without_phase_compression(self) -> None:
        rows = []
        dates = pd.date_range("2023-02-03", periods=9, freq="7D")
        for position, asof in enumerate(dates):
            for index in range(20):
                rows.append(
                    {
                        "code": f"600{index:03d}.SH",
                        "industry": f"industry-{index}",
                        "asof": asof.date().isoformat(),
                        "score": float(20 - index),
                        "evaluation_period": True,
                        "evaluation_eligible": position != 0,
                        "entry_executable": True,
                        "planned_entry_time": (
                            asof + pd.Timedelta(days=3)
                        ).isoformat(),
                        "planned_exit_time": (
                            asof + pd.Timedelta(days=56)
                        ).isoformat(),
                        "exit_time": (
                            asof + pd.Timedelta(days=56)
                        ).isoformat(),
                        "forward_return_40": 0.01,
                        "target": float(index < 2),
                    }
                )

        metrics = _evaluate_v4_year(
            pd.DataFrame(rows), "score", "evaluation_eligible"
        )
        phase_zero = metrics["phase_metrics"][0]

        self.assertEqual(metrics["breadth_cash_periods"], 1)
        self.assertEqual(phase_zero["periods"], 2)
        self.assertEqual(phase_zero["cycles"][0]["filled_slots"], 0)
        self.assertEqual(phase_zero["cycles"][0]["gross_return"], 0.0)
        self.assertEqual(phase_zero["cycles"][1]["asof"], dates[8].date().isoformat())

    def test_paired_evaluation_uses_joint_exit_boundary_and_identical_horizons(self) -> None:
        rows = []
        dates = pd.date_range("2023-02-03", periods=20, freq="7D")
        for position, asof in enumerate(dates):
            for index in range(40):
                # Candidate picks 0-19; RS60 picks 20-39. At phase-zero's
                # first cycle their actual exits differ, so the shared ledger
                # must wait for the later baseline exit before both rebuild.
                candidate_rank = 40 - index
                baseline_rank = index + 1
                delay_days = 0
                if position == 0 and index < 20:
                    delay_days = 7
                if position == 0 and index >= 20:
                    delay_days = 21
                rows.append(
                    {
                        "code": f"600{index:03d}.SH",
                        "industry": f"industry-{index}",
                        "asof": asof.date().isoformat(),
                        "score": float(candidate_rank),
                        "relative_return_60": float(baseline_rank),
                        "evaluation_period": True,
                        "evaluation_eligible": True,
                        "entry_executable": True,
                        "planned_entry_time": (
                            asof + pd.Timedelta(days=3)
                        ).isoformat(),
                        "planned_exit_time": (
                            asof + pd.Timedelta(days=56)
                        ).isoformat(),
                        "exit_time": (
                            asof + pd.Timedelta(days=56 + delay_days)
                        ).isoformat(),
                        "forward_return_40": 0.02 if index < 20 else 0.01,
                        "target": float(index < 4),
                    }
                )

        candidate, baseline = _evaluate_v4_pair(
            pd.DataFrame(rows),
            candidate_score_column="score",
            baseline_score_column="relative_return_60",
            eligibility_column="evaluation_eligible",
        )

        for candidate_phase, baseline_phase in zip(
            candidate["phase_metrics"], baseline["phase_metrics"], strict=True
        ):
            candidate_horizon = [
                (
                    cycle["asof"],
                    cycle["planned_entry_at"],
                    cycle["joint_capital_available_at"],
                )
                for cycle in candidate_phase["cycles"]
            ]
            baseline_horizon = [
                (
                    cycle["asof"],
                    cycle["planned_entry_at"],
                    cycle["joint_capital_available_at"],
                )
                for cycle in baseline_phase["cycles"]
            ]
            self.assertEqual(candidate_horizon, baseline_horizon)
        self.assertNotEqual(
            candidate["phase_metrics"][0]["cycles"][0]["capital_available_at"],
            baseline["phase_metrics"][0]["cycles"][0]["capital_available_at"],
        )
        self.assertEqual(
            candidate["phase_metrics"][0]["cycles"][0]["joint_capital_available_at"],
            baseline["phase_metrics"][0]["cycles"][0]["joint_capital_available_at"],
        )
        self.assertEqual(
            [cycle["asof"] for cycle in candidate["phase_metrics"][0]["cycles"]],
            [dates[0].date().isoformat(), dates[11].date().isoformat(), dates[19].date().isoformat()],
        )
        self.assertGreater(
            candidate["phase_metrics"][0]["total_return"],
            baseline["phase_metrics"][0]["total_return"],
        )

    def test_development_gate_requires_positive_double_cost_return(self) -> None:
        candidate = {"precision_at_20": 0.20, "total_return": -0.01, "double_cost_return": -0.02, "max_drawdown": -0.10}
        baseline = {"precision_at_20": 0.10, "total_return": -0.20, "double_cost_return": -0.21, "max_drawdown": -0.12}

        self.assertFalse(passes_v4_development_gate(candidate, baseline))

    def test_development_gate_uses_worst_phase_double_cost_return(self) -> None:
        candidate = {
            "phase_count": 8,
            "min_phase_periods": 3,
            "min_phase_invested_periods": 2,
            "precision_at_20": 0.20,
            "total_return": 0.20,
            "double_cost_return": 0.18,
            "max_drawdown": -0.08,
            "worst_phase_total_return": 0.05,
            "worst_phase_double_cost_return": -0.001,
            "worst_phase_max_drawdown": -0.12,
        }
        baseline = {
            "precision_at_20": 0.10,
            "worst_phase_total_return": -0.10,
            "worst_phase_double_cost_return": -0.20,
            "worst_phase_max_drawdown": -0.15,
        }

        self.assertFalse(passes_v4_development_gate(candidate, baseline))

    def test_development_gate_requires_same_phase_baseline_outperformance(self) -> None:
        candidate = {
            "phase_count": 8,
            "min_phase_periods": 3,
            "min_phase_invested_periods": 2,
            "precision_at_20": 0.20,
            "worst_phase_total_return": 0.01,
            "worst_phase_double_cost_return": 0.01,
            "worst_phase_max_drawdown": -0.10,
            "phase_metrics": [
                {
                    "phase": phase,
                    "total_return": 0.10 if phase == 0 else 0.01,
                    "double_cost_return": 0.08 if phase == 0 else 0.01,
                    "max_drawdown": -0.05 if phase == 0 else -0.10,
                }
                for phase in range(8)
            ],
        }
        baseline = {
            "phase_count": 8,
            "precision_at_20": 0.10,
            "worst_phase_total_return": 0.0,
            "worst_phase_double_cost_return": 0.0,
            "worst_phase_max_drawdown": -0.12,
            "phase_metrics": [
                {
                    "phase": phase,
                    "total_return": 0.11 if phase == 0 else 0.0,
                    "double_cost_return": 0.09 if phase == 0 else 0.0,
                    "max_drawdown": -0.04 if phase == 0 else -0.12,
                }
                for phase in range(8)
            ],
        }

        self.assertFalse(passes_v4_development_gate(candidate, baseline))

    def test_development_gate_requires_baseline_minimum_phase_sample(self) -> None:
        phase_metrics = [
            {
                "phase": phase,
                "total_return": 0.05,
                "double_cost_return": 0.04,
                "max_drawdown": -0.05,
            }
            for phase in range(8)
        ]
        candidate = {
            "phase_count": 8,
            "min_phase_periods": 3,
            "min_phase_invested_periods": 2,
            "precision_at_20": 0.20,
            "worst_phase_total_return": 0.05,
            "worst_phase_double_cost_return": 0.04,
            "worst_phase_max_drawdown": -0.05,
            "phase_metrics": phase_metrics,
        }
        baseline = {
            "phase_count": 8,
            "min_phase_periods": 3,
            "min_phase_invested_periods": 1,
            "precision_at_20": 0.10,
            "worst_phase_total_return": 0.0,
            "worst_phase_double_cost_return": 0.0,
            "worst_phase_max_drawdown": -0.10,
            "phase_metrics": [
                {
                    "phase": phase,
                    "total_return": 0.0,
                    "double_cost_return": 0.0,
                    "max_drawdown": -0.10,
                }
                for phase in range(8)
            ],
        }

        self.assertFalse(passes_v4_development_gate(candidate, baseline))

    def test_development_gate_can_pass_with_aligned_sufficient_phases(self) -> None:
        candidate = {
            "phase_count": 8,
            "min_phase_periods": 3,
            "min_phase_invested_periods": 2,
            "precision_at_20": 0.20,
            "worst_phase_total_return": 0.05,
            "worst_phase_double_cost_return": 0.04,
            "worst_phase_max_drawdown": -0.05,
            "phase_metrics": [
                {
                    "phase": phase,
                    "total_return": 0.05,
                    "double_cost_return": 0.04,
                    "max_drawdown": -0.05,
                }
                for phase in range(8)
            ],
        }
        baseline = {
            "phase_count": 8,
            "min_phase_periods": 3,
            "min_phase_invested_periods": 2,
            "precision_at_20": 0.10,
            "worst_phase_total_return": 0.01,
            "worst_phase_double_cost_return": 0.0,
            "worst_phase_max_drawdown": -0.07,
            "phase_metrics": [
                {
                    "phase": phase,
                    "total_return": 0.01,
                    "double_cost_return": 0.0,
                    "max_drawdown": -0.07,
                }
                for phase in range(8)
            ],
        }

        self.assertTrue(passes_v4_development_gate(candidate, baseline))

    def test_purge_uses_executable_exit_time_not_weekday_approximation(self) -> None:
        frame = pd.DataFrame(
            [
                {"asof": "2019-10-01", "exit_time": "2019-12-31T09:30:00"},
                {"asof": "2019-11-01", "exit_time": "2020-01-02T09:30:00"},
                {"asof": "2019-12-01", "exit_time": None},
            ]
        )

        result = _purge_completed_outcomes(frame, test_year=2020)

        self.assertEqual(result.index.tolist(), [0])

    def test_embargo_uses_frozen_exchange_sessions(self) -> None:
        calendar = pd.bdate_range("2020-01-02", periods=25).date.astype(str).tolist()
        frame = pd.DataFrame(
            [{"asof": day} for day in calendar]
        )

        result = _embargo_by_trading_calendar(
            frame, test_year=2020, trading_calendar=calendar
        )

        self.assertEqual(result["asof"].tolist(), calendar[20:])

    def test_profile_detects_duplicate_point_in_time_grain(self) -> None:
        feature_columns = (
            "industry_momentum", "industry_breadth", "industry_amount_trend",
            "return_20", "return_60", "return_120", "relative_return_20",
            "relative_return_60", "relative_return_120", "volume_ratio",
            "amount_ratio", "breakout_distance", "ma20_slope", "event_score",
            "price_to_ma60",
        )
        row = {
            "code": "600000.SH", "asof": "2023-06-30",
            "published_at": "2023-06-30T12:00:00", "effective_at": "2023-06-30T00:00:00",
            "universe_gate": True, "entry_executable": True,
            "execution_status_complete": True,
            "entry_time": "2023-07-03T00:00:00", "forward_return_40": 0.1,
            **{column: 0.1 for column in feature_columns},
        }

        profile = profile_v4_data(pd.DataFrame([row, row]))

        self.assertEqual(profile["duplicate_grain_rows"], 1)
        self.assertFalse(profile["timing_audit_passed"])

    def test_profile_missing_publication_timestamps_fails_closed(self) -> None:
        row = self._profile_row()
        row.pop("published_at")
        row.pop("effective_at")

        profile = profile_v4_data(pd.DataFrame([row]))

        self.assertFalse(profile["timing_audit_passed"])

    def test_profile_with_no_executable_entries_fails_timing_audit(self) -> None:
        row = self._profile_row()
        row.update(
            {
                "entry_executable": False,
                "entry_time": None,
                "exit_time": None,
            }
        )

        profile = profile_v4_data(pd.DataFrame([row]))

        self.assertFalse(profile["timing_audit_passed"])

    def test_development_audit_rejects_failed_timing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            with (
                patch.object(
                    service,
                    "_current_v4_batches",
                    return_value=[{"path": "unused.parquet"}],
                ),
                patch(
                    "research_platform.early_winner_v4_research.pd.read_parquet",
                    return_value=pd.DataFrame([{"asof": "2023-01-01", "code": "x"}]),
                ),
                patch(
                    "research_platform.early_winner_v4_research.profile_v4_data",
                    return_value={
                        "label_coverage": 1.0,
                        "duplicate_grain_rows": 0,
                        "timing_audit_passed": False,
                    },
                ),
            ):
                with self.assertRaises(ResearchDataBlockedError):
                    service.run_development_audit()

    def test_strategy_is_research_only_and_emits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            result = service.strategy.scan(asof="2026-08-12")

        self.assertEqual(result.signals, ())
        self.assertEqual(result.candidates, ())
        self.assertFalse(result.state["trade_signals_enabled"])
        self.assertFalse(result.state["frozen_validation_opened"])

    @staticmethod
    def _profile_row() -> dict[str, object]:
        row: dict[str, object] = {
            "code": "600000.SH",
            "asof": "2023-06-30",
            "published_at": "2023-06-30T12:00:00",
            "effective_at": "2023-06-30T12:00:00",
            "universe_gate": True,
            "execution_status_complete": True,
            "label_window_matured": True,
            "label_matured_in_development": True,
            "entry_executable": True,
            "planned_entry_time": "2023-07-03T00:00:00",
            "entry_time": "2023-07-03T00:00:00",
            "planned_exit_time": "2023-08-28T00:00:00",
            "exit_time": "2023-08-28T00:00:00",
            "forward_return_40": 0.10,
        }
        row.update({column: 0.1 for column in (
            "industry_momentum", "industry_breadth", "industry_amount_trend",
            "return_20", "return_60", "return_120", "relative_return_20",
            "relative_return_60", "relative_return_120", "volume_ratio",
            "amount_ratio", "breakout_distance", "ma20_slope", "event_score",
            "price_to_ma60",
        )})
        return row


if __name__ == "__main__":
    unittest.main()
