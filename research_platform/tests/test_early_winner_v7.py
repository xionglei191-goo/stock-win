from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import research_platform.early_winner_v7_research as v7_module
from research_platform.early_winner_v7_research import (
    CLASSIFIER_RULE_HASH,
    DEPENDENCY_LOCK_HASH,
    EVALUATOR_BUNDLE_HASH,
    EVALUATOR_COMPONENT_HASHES,
    EVENT_EFFECTIVE_RULE_VERSION,
    EVENT_RAW_REPLAY_SCHEMA_VERSION,
    LABEL_SCHEMA_HASH,
    LOCKED_V7_CRITICAL_AST_HASH,
    MANIFEST_VERSION,
    PROJECT_ID,
    PROTOCOL_HASH,
    PROTOCOL_VERSION,
    EarlyWinnerV7Strategy,
    FrozenValidationAlreadyOpened,
    V7FrozenValidationLedger,
    V7ProtocolChangeRequiresV8,
    _logical_frame_hash,
    _manifest_payload_hash,
    _sorted_row_key_hash,
    assess_v7_frozen_result,
    assert_locked_dependencies,
    current_v7_critical_ast_hash,
    evaluate_v7_frozen_frame,
    frame_schema_hash,
    frozen_validation_readiness,
    register_v7_project,
    run_v7_frozen_validation_once,
    v7_frozen_root,
)
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config
from research_platform.tests import test_early_winner_v6 as v6_test_module


class EarlyWinnerV7Tests(unittest.TestCase):
    def test_v7_is_research_only_and_registration_never_reads_frozen_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            with patch.object(v7_module, "_read_file_once") as reader:
                register_v7_project(database)
            database.update_research_project(
                PROJECT_ID,
                status="CUSTOM_BLOCKED",
                data_gates={"preserved": {"ready": False}},
            )
            register_v7_project(database)
            row = database.query(
                "SELECT status, lifecycle, category, data_gates_json "
                "FROM research_projects WHERE project_id=?",
                (PROJECT_ID,),
            )[0]
            scan = EarlyWinnerV7Strategy().scan(asof="2026-08-13")

        reader.assert_not_called()
        self.assertEqual(row["status"], "CUSTOM_BLOCKED")
        self.assertEqual(row["lifecycle"], "RESEARCH_ONLY")
        self.assertEqual(row["category"], "research_project")
        self.assertEqual(json.loads(row["data_gates_json"]), {"preserved": {"ready": False}})
        self.assertEqual(scan.signals, ())
        self.assertEqual(scan.candidates, ())
        self.assertFalse(scan.strategy.scan_enabled)
        self.assertFalse(scan.strategy.backtest_enabled)
        self.assertFalse(scan.state["trade_signals_enabled"])

    def test_protocol_change_preempts_caller_ready_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            gates = self._gates(Path(directory) / "missing.json", self._hash("missing"))
            gates["historical_universe_master"] = {"ready": True, "status": "READY"}
            gates["delisted_history_quality"] = {"ready": True, "status": "READY"}
            readiness = frozen_validation_readiness(config, gates)

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "V8_REQUIRED")
        self.assertIn("create V8", readiness["detail"])

    def test_protocol_change_preempts_authoritative_ready_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            gates = self._gates(Path(directory) / "manifest.json", self._hash("manifest"))
            with self._authoritative_gate_patch():
                readiness = frozen_validation_readiness(config, gates)

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "V8_REQUIRED")
        self.assertIn("create V8", readiness["detail"])

    def test_v7_evaluation_refuses_changed_dependencies(self) -> None:
        components = self._components()
        frame = self._frame(2024, components)
        frame["v4_eligible"] = False
        frame["target"] = 0

        with self.assertRaises(V7ProtocolChangeRequiresV8):
            evaluate_v7_frozen_frame(
                frame, expected_year=2024, component_hashes=components
            )

    def test_v7_runner_refuses_changed_dependencies_before_opening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory) / "runtime")
            database = Database(config)
            database.initialize()
            gates = self._write_snapshot(v7_frozen_root(config))
            with self._authoritative_gate_patch():
                with self.assertRaises(V7ProtocolChangeRequiresV8):
                    run_v7_frozen_validation_once(
                        database=database, gates=gates, runner_id="synthetic-v7"
                    )
            self.assertEqual(
                database.query(
                    "SELECT COUNT(*) AS n FROM sqlite_master "
                    "WHERE type='table' AND name='early_winner_v7_frozen_runs'"
                )[0]["n"],
                0,
            )

    def test_database_claim_is_atomic_under_competing_openers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory) / "runtime")
            database = Database(config)
            database.initialize()
            root = v7_frozen_root(config)
            root.mkdir(parents=True)
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            readiness = {
                "ready": True,
                "snapshot_id": "synthetic",
                "manifest_path": str(manifest),
                "manifest_hash": self._hash(manifest.read_bytes()),
                "component_hashes": self._components(),
            }
            ledger = V7FrozenValidationLedger(database)
            ledger.seal(readiness)

            def claim(runner: str) -> str:
                try:
                    return ledger.claim_once(runner_id=runner)["audit_id"]
                except FrozenValidationAlreadyOpened:
                    return "REJECTED"

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(claim, ("runner-a", "runner-b")))

        self.assertEqual(sum(value.startswith("ewv7_audit_") for value in outcomes), 1)
        self.assertEqual(outcomes.count("REJECTED"), 1)

    def test_v7_detects_post_preregistration_data_audit_changes(self) -> None:
        with self.assertRaises(V7ProtocolChangeRequiresV8):
            assert_locked_dependencies()
        self.assertEqual(current_v7_critical_ast_hash(), LOCKED_V7_CRITICAL_AST_HASH)
        for name in (
            "early_winner_v4_research.py",
            "early_winner_research.py",
            "strategies/early_winner.py",
            "early_winner_v5_research.py",
            "early_winner_v6_research.py",
            "historical_security_master.py",
            "delisted_history_quality.py",
            "strategies/early_winner_v7.py",
        ):
            self.assertIn(name, EVALUATOR_COMPONENT_HASHES)
        source = Path(v7_module.__file__).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "changed.py"
            changed.write_text(
                source.replace(
                    "claim_before_first_manifest_read\": True",
                    "claim_before_first_manifest_read\": False",
                    1,
                ),
                encoding="utf-8",
            )
            self.assertNotEqual(
                current_v7_critical_ast_hash(changed), LOCKED_V7_CRITICAL_AST_HASH
            )
            changed_master = Path(directory) / "master.py"
            changed_master.write_bytes(
                Path(v7_module.master_module.__file__).read_bytes() + b"\n# drift\n"
            )
            with patch.object(v7_module.master_module, "__file__", str(changed_master)):
                with self.assertRaises(V7ProtocolChangeRequiresV8):
                    assert_locked_dependencies()

    @staticmethod
    def _hash(value: str | bytes) -> str:
        raw = value if isinstance(value, bytes) else value.encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _components(cls) -> dict[str, str]:
        return {
            "historical_universe_master_manifest_hash": cls._hash("master"),
            "delisted_history_manifest_hash": cls._hash("delisted-manifest"),
            "delisted_history_report_hash": cls._hash("delisted-report"),
            "event_provenance_snapshot_hash": cls._hash("event-snapshot"),
            "event_raw_content_manifest_hash": cls._hash("event-raw"),
            "trading_calendar_content_hash": cls._hash("calendar"),
            "execution_status_content_hash": cls._hash("execution"),
            "label_snapshot_hash": cls._hash("labels"),
            "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
            "label_schema_hash": LABEL_SCHEMA_HASH,
            "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
        }

    @classmethod
    def _master_gate(cls) -> dict[str, object]:
        return {
            "ready": True,
            "status": "READY",
            "detail": "synthetic fixture",
            "snapshot_id": "synthetic-master",
            "manifest_hash": cls._components()[
                "historical_universe_master_manifest_hash"
            ],
            "protocol_version": "synthetic-master-v1",
            "coverage_start": "2018-01-01",
            "coverage_end": "2025-12-31",
            "promotion_blocked": False,
        }

    @classmethod
    def _delisted_gate(cls) -> dict[str, object]:
        return {
            "ready": True,
            "status": "READY",
            "detail": "synthetic full replay",
            "promotion_blocked": False,
            "historical_security_master_snapshot": "synthetic-master",
            "manifest_hash": cls._components()["delisted_history_manifest_hash"],
            "report_hash": cls._components()["delisted_history_report_hash"],
        }

    @classmethod
    def _authoritative_gate_patch(cls):
        return _TwoPatch(
            patch.object(
                v7_module,
                "load_historical_universe_master_gate",
                return_value=cls._master_gate(),
            ),
            patch.object(
                v7_module,
                "load_verified_delisted_history_gate",
                return_value=cls._delisted_gate(),
            ),
        )

    @classmethod
    def _gates(cls, manifest_path: Path, manifest_hash: str) -> dict[str, object]:
        components = cls._components()
        return {
            "preregistration": {
                "ready": True,
                "protocol_version": PROTOCOL_VERSION,
                "protocol_hash": PROTOCOL_HASH,
                "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
                "label_schema_hash": LABEL_SCHEMA_HASH,
                "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
            },
            "event_provenance": {
                "ready": True,
                "schema_version": EVENT_RAW_REPLAY_SCHEMA_VERSION,
                "legacy_selection_schema_version": v7_module.EVENT_REPLAY_SCHEMA_VERSION,
                "classifier_rule_hash": CLASSIFIER_RULE_HASH,
                "source": "CNINFO_OFFICIAL",
                "content_hash_algorithm": "SHA256_RAW_BYTES",
                "raw_content_rehash_passed": True,
                "announcement_security_binding_passed": True,
                "effective_at_calendar_derived_passed": True,
                "effective_rule_version": EVENT_EFFECTIVE_RULE_VERSION,
                "trading_calendar_hash": components["trading_calendar_content_hash"],
                "snapshot_hash": components["event_provenance_snapshot_hash"],
                "raw_content_manifest_hash": components[
                    "event_raw_content_manifest_hash"
                ],
            },
            "trading_calendar": {
                "ready": True,
                "content_hash": components["trading_calendar_content_hash"],
            },
            "execution_status": {
                "ready": True,
                "content_hash": components["execution_status_content_hash"],
            },
            "label_snapshot": {
                "ready": True,
                "snapshot_hash": components["label_snapshot_hash"],
                "return_column": "forward_return_40",
                "label_schema_hash": LABEL_SCHEMA_HASH,
                "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
            },
            "frozen_snapshot": {
                "ready": True,
                "sealed": True,
                "years": [2024, 2025],
                "protocol_hash": PROTOCOL_HASH,
                "snapshot_id": "synthetic-v7-snapshot",
                "manifest_path": str(manifest_path),
                "manifest_hash": manifest_hash,
            },
        }

    @classmethod
    def _event_payload(cls, code: str, asof: pd.Timestamp) -> dict[str, object]:
        return v6_test_module.EarlyWinnerV6Tests._event_payload.__func__(cls, code, asof)

    @classmethod
    def _empty_event_payload(cls, code: str, asof: pd.Timestamp) -> dict[str, object]:
        return v6_test_module.EarlyWinnerV6Tests._empty_event_payload.__func__(
            cls, code, asof
        )

    @classmethod
    def _frame(cls, year: int, components: dict[str, str]) -> pd.DataFrame:
        frame = v6_test_module.EarlyWinnerV6Tests._frame.__func__(
            cls, year, components
        )
        frame["delisted_history_manifest_hash"] = components[
            "delisted_history_manifest_hash"
        ]
        frame["delisted_history_report_hash"] = components[
            "delisted_history_report_hash"
        ]
        return frame

    @classmethod
    def _descriptor(cls, path: Path, year: int, frame: pd.DataFrame) -> dict[str, object]:
        raw = path.read_bytes()
        asof = pd.to_datetime(frame["asof"])
        return {
            "year": year,
            "relative_path": path.name,
            "format": "parquet",
            "byte_size": len(raw),
            "content_hash": cls._hash(raw),
            "schema_hash": frame_schema_hash(frame),
            "logical_content_hash": _logical_frame_hash(frame),
            "row_count": len(frame),
            "decision_date_count": int(asof.dt.normalize().nunique()),
            "code_count": int(frame["code"].nunique()),
            "min_asof": asof.min().date().isoformat(),
            "max_asof": asof.max().date().isoformat(),
            "sorted_row_key_hash": _sorted_row_key_hash(frame),
            "duplicate_grain_count": int(frame.duplicated(["asof", "code"]).sum()),
        }

    @classmethod
    def _write_snapshot(cls, root: Path) -> dict[str, object]:
        root.mkdir(parents=True)
        components = cls._components()
        frames = {year: cls._frame(year, components) for year in (2024, 2025)}
        paths = {year: root / f"{year}.parquet" for year in frames}
        for year, frame in frames.items():
            frame.to_parquet(paths[year], index=False)
        descriptors = [
            cls._descriptor(paths[year], year, frames[year]) for year in (2024, 2025)
        ]
        manifest: dict[str, object] = {
            "manifest_version": MANIFEST_VERSION,
            "project_id": PROJECT_ID,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_hash": PROTOCOL_HASH,
            "snapshot_id": "synthetic-v7-snapshot",
            "sealed": True,
            "frozen_years": [2024, 2025],
            "timezone": "Asia/Shanghai",
            "decision_boundary": "WEEK_LAST_TRADING_SESSION_CLOSE",
            "row_grain": ["asof", "code"],
            "return_column": "forward_return_40",
            "evaluator_bundle_hash": EVALUATOR_BUNDLE_HASH,
            "label_schema_hash": LABEL_SCHEMA_HASH,
            "dependency_lock_hash": DEPENDENCY_LOCK_HASH,
            "components": components,
            "shards": descriptors,
            "total_rows": sum(len(frame) for frame in frames.values()),
            "schema_hash": frame_schema_hash(frames[2024]),
        }
        manifest["manifest_payload_hash"] = _manifest_payload_hash(manifest)
        manifest_path = root / "manifest.json"
        manifest_path.write_text(v7_module._canonical_json(manifest), encoding="utf-8")
        return cls._gates(manifest_path, cls._hash(manifest_path.read_bytes()))


class _TwoPatch:
    def __init__(self, first, second) -> None:
        self.first = first
        self.second = second

    def __enter__(self):
        self.first.__enter__()
        self.second.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        suppress_second = self.second.__exit__(exc_type, exc, traceback)
        suppress_first = self.first.__exit__(exc_type, exc, traceback)
        return suppress_first or suppress_second


if __name__ == "__main__":
    unittest.main()
