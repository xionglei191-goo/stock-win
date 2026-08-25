from __future__ import annotations

import base64
import copy
import concurrent.futures
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import research_platform.early_winner_v6_research as v6_module

from research_platform.early_winner_v5_research import (
    EarlyWinnerV5ResearchService,
    replay_event_provenance,
)
from research_platform.early_winner_v6_research import (
    CLASSIFIER_RULE_HASH,
    DEPENDENCY_LOCK_HASH,
    EVALUATOR_BUNDLE_HASH,
    EVALUATOR_COMPONENT_HASHES,
    EVENT_EFFECTIVE_RULE_VERSION,
    EVENT_RAW_REPLAY_SCHEMA_VERSION,
    LABEL_SCHEMA_HASH,
    MANIFEST_VERSION,
    PROJECT_ID,
    PROTOCOL_HASH,
    PROTOCOL_VERSION,
    PROTOCOL_SPEC,
    LOCKED_V6_CRITICAL_AST_HASH,
    V5_REJECTED_STATUS,
    EarlyWinnerV6ResearchService,
    FrozenManifestError,
    FrozenValidationAlreadyOpened,
    FrozenValidationAuditError,
    V6FrozenValidationLedger,
    V6ProtocolChangeRequiresV7,
    _logical_frame_hash,
    _manifest_payload_hash,
    _sorted_row_key_hash,
    assess_v6_frozen_result,
    assert_locked_dependencies,
    current_v6_critical_ast_hash,
    evaluate_v6_frozen_frame,
    frame_schema_hash,
    frozen_validation_readiness,
    run_v6_frozen_validation_once,
    v6_frozen_root,
    v6_raw_event_replay_hash,
)
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config


class EarlyWinnerV6RetirementTests(unittest.TestCase):
    def test_v6_fails_closed_as_protocol_changed_and_requires_v7(self) -> None:
        with self.assertRaises(V6ProtocolChangeRequiresV7):
            assert_locked_dependencies()
        with patch.object(v6_module, "_read_file_once") as reader:
            readiness = frozen_validation_readiness({})
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "V7_REQUIRED")
        self.assertIn("create V7", readiness["detail"])
        reader.assert_not_called()


@unittest.skip(
    "V6 mechanics are immutable historical evidence; the changed V4 dependency "
    "must not be patched to make V6 executable again"
)
class EarlyWinnerV6Tests(unittest.TestCase):
    def test_v6_is_research_only_without_mutating_v5_or_existing_v6_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            EarlyWinnerV5ResearchService(config, database)
            before_v5 = database.query(
                "SELECT status, lifecycle, data_gates_json FROM research_projects "
                "WHERE project_id='early_winner_v5'"
            )[0]
            service = EarlyWinnerV6ResearchService(config, database)
            after_v5 = database.query(
                "SELECT status, lifecycle, data_gates_json FROM research_projects "
                "WHERE project_id='early_winner_v5'"
            )[0]
            preserved_gates = {"custom_ready_gate": {"ready": True}}
            database.update_research_project(
                PROJECT_ID, status="SEALED_CUSTOM", data_gates=preserved_gates
            )
            EarlyWinnerV6ResearchService(config, database)
            preserved_v6 = database.query(
                "SELECT status, data_gates_json FROM research_projects WHERE project_id=?",
                (PROJECT_ID,),
            )[0]
            detail = service.detail()
            scan = service.strategy.scan(asof="2026-08-12")

        self.assertEqual(after_v5, before_v5)
        self.assertEqual(preserved_v6["status"], "SEALED_CUSTOM")
        self.assertEqual(json.loads(preserved_v6["data_gates_json"]), preserved_gates)
        self.assertEqual(detail["lifecycle"], "RESEARCH_ONLY")
        self.assertEqual(detail["status"], "SEALED_CUSTOM")
        self.assertEqual(detail["v5_disposition"]["status"], V5_REJECTED_STATUS)
        self.assertFalse(detail["promotion_allowed"])
        self.assertEqual(scan.signals, ())
        self.assertEqual(scan.candidates, ())
        self.assertFalse(service.strategy.metadata.scan_enabled)
        self.assertFalse(service.strategy.metadata.backtest_enabled)

    def test_readiness_requires_raw_rehash_security_binding_and_calendar_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gates = self._gates(Path(directory) / "manifest.json", "0" * 64)
            self.assertTrue(frozen_validation_readiness(gates)["ready"])
            for field in (
                "raw_content_rehash_passed",
                "announcement_security_binding_passed",
                "effective_at_calendar_derived_passed",
            ):
                broken = copy.deepcopy(gates)
                broken["event_provenance"][field] = False
                result = frozen_validation_readiness(broken)
                self.assertFalse(result["ready"], field)
                self.assertIn(field, result["detail"])

    def test_frozen_evaluator_accepts_synthetic_2024_and_recomputes_v4_labels(self) -> None:
        components = self._components()
        frame = self._frame(2024, components)
        frame["v4_eligible"] = False
        frame["target"] = 0

        candidate, baseline = evaluate_v6_frozen_frame(
            frame, expected_year=2024, component_hashes=components
        )

        self.assertEqual(candidate["year"], 2024)
        self.assertEqual(baseline["year"], 2024)
        self.assertEqual(candidate["phase_count"], 8)
        self.assertEqual(candidate["return_policy"], "EIGHT_PHASE_NON_OVERLAPPING_FULL_EXIT_REBUILD")
        self.assertGreater(candidate["precision_at_20"], baseline["precision_at_20"])
        self.assertEqual(candidate["evaluator_bundle_hash"], EVALUATOR_BUNDLE_HASH)
        self.assertEqual(candidate["label_schema_hash"], LABEL_SCHEMA_HASH)

    def test_manifest_bound_runner_consumes_once_and_assessment_is_audit_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root / "runtime")
            database = Database(config)
            database.initialize()
            gates = self._write_snapshot(v6_frozen_root(config), tamper_after_manifest=False)

            result = run_v6_frozen_validation_once(
                database=database, gates=gates, runner_id="unit-test"
            )
            assessment = assess_v6_frozen_result(database=database)
            ledger = V6FrozenValidationLedger(database).get()

            self.assertEqual(ledger["state"], "RESULT_COMMITTED")
            self.assertEqual(ledger["artifact_hash"], result["artifact_hash"])
            self.assertEqual(ledger["result_path"], result["result_path"])
            self.assertEqual(ledger["result_byte_size"], result["result_byte_size"])
            self.assertTrue(Path(result["result_path"]).is_relative_to(config.runtime_dir))
            self.assertEqual(assessment["status"], "OBSERVATION_ONLY")
            self.assertEqual(assessment["audit_id"], result["audit_id"])
            with self.assertRaises(FrozenValidationAlreadyOpened):
                run_v6_frozen_validation_once(
                    database=database, gates=gates, runner_id="second-attempt"
                )
            with self.assertRaises(TypeError):
                assess_v6_frozen_result(result, database=database)

    def test_shard_tamper_fails_closed_and_cannot_be_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root / "runtime")
            database = Database(config)
            database.initialize()
            gates = self._write_snapshot(v6_frozen_root(config), tamper_after_manifest=True)

            with self.assertRaises(FrozenManifestError):
                run_v6_frozen_validation_once(
                    database=database, gates=gates, runner_id="tamper-test"
                )
            self.assertEqual(V6FrozenValidationLedger(database).get()["state"], "FAILED_CLOSED")
            with self.assertRaises(FrozenValidationAlreadyOpened):
                run_v6_frozen_validation_once(
                    database=database, gates=gates, runner_id="tamper-retry"
                )

    def test_database_claim_is_atomic_under_competing_openers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root / "runtime")
            database = Database(config)
            database.initialize()
            frozen_root = v6_frozen_root(config)
            frozen_root.mkdir(parents=True)
            manifest_path = frozen_root / "never-read.json"
            manifest_path.write_text("{}", encoding="utf-8")
            gates = self._gates(manifest_path, self._hash("sealed-manifest"))
            readiness = frozen_validation_readiness(gates)
            ledger = V6FrozenValidationLedger(database)
            ledger.seal(readiness)

            def claim(runner: str) -> str:
                try:
                    return ledger.claim_once(runner_id=runner)["audit_id"]
                except FrozenValidationAlreadyOpened:
                    return "REJECTED"

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(claim, ("runner-a", "runner-b")))

        self.assertEqual(sum(item.startswith("ewv6_audit_") for item in outcomes), 1)
        self.assertEqual(outcomes.count("REJECTED"), 1)

    def test_assessment_rejects_cycle_from_wrong_year_even_with_rehashed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root / "runtime")
            database = Database(config)
            database.initialize()
            gates = self._write_snapshot(v6_frozen_root(config), tamper_after_manifest=False)
            result = run_v6_frozen_validation_once(
                database=database, gates=gates, runner_id="year-test"
            )
            payload = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
            forged = copy.deepcopy(payload)
            methods = forged["yearly"]["2025"]["evidence"]["methods"]
            for method in ("EVENT_QUIET", "RS60"):
                methods[method]["phases"][0]["cycles"][0]["asof"] = "2024-01-05"
            forged["yearly"]["2025"]["evidence"]["cycle_evidence_hash"] = self._hash(
                json.dumps(methods, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            ledger_row = V6FrozenValidationLedger(database).get()
            with patch.object(
                v6_module,
                "_load_committed_result_artifact",
                return_value=(forged, ledger_row),
            ):
                with self.assertRaisesRegex(FrozenValidationAuditError, "does not belong to 2025"):
                    assess_v6_frozen_result(database=database)

    def test_result_artifact_tamper_is_detected_before_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory) / "runtime")
            database = Database(config)
            database.initialize()
            gates = self._write_snapshot(v6_frozen_root(config), tamper_after_manifest=False)
            result = run_v6_frozen_validation_once(
                database=database, gates=gates, runner_id="artifact-tamper"
            )
            path = Path(result["result_path"])
            path.write_bytes(path.read_bytes() + b" ")

            with self.assertRaisesRegex(
                FrozenValidationAuditError, "size/hash does not reproduce"
            ):
                assess_v6_frozen_result(database=database)

    def test_assessment_ignores_reported_summaries_and_recomputes_from_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory) / "runtime")
            database = Database(config)
            database.initialize()
            gates = self._write_snapshot(v6_frozen_root(config), tamper_after_manifest=False)
            result = run_v6_frozen_validation_once(
                database=database, gates=gates, runner_id="evidence-only"
            )
            payload = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
            payload["yearly"]["2024"]["reported_metrics"]["candidate"][
                "precision_at_20"
            ] = -999.0
            ledger_row = V6FrozenValidationLedger(database).get()

            with patch.object(
                v6_module,
                "_load_committed_result_artifact",
                return_value=(payload, ledger_row),
            ):
                assessment = assess_v6_frozen_result(database=database)

        self.assertEqual(assessment["status"], "OBSERVATION_ONLY")
        self.assertGreater(
            assessment["recomputed_yearly"]["2024"]["candidate"]["precision_at_20"],
            0.0,
        )

    def test_evaluator_ast_bundle_is_stable_and_detects_critical_or_dependency_changes(self) -> None:
        source_path = Path(v6_module.__file__)
        source = source_path.read_text(encoding="utf-8")
        self.assertEqual(current_v6_critical_ast_hash(), LOCKED_V6_CRITICAL_AST_HASH)
        self.assertEqual(
            PROTOCOL_SPEC["frozen_open"]["database_state_machine"],
            ["SEALED", "CONSUMING", "RESULT_COMMITTED", "FAILED_CLOSED"],
        )
        self.assertIn("early_winner_v5_research.py", EVALUATOR_COMPONENT_HASHES)
        self.assertIn("strategies/early_winner_v6.py", EVALUATOR_COMPONENT_HASHES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formatting_only = root / "formatting.py"
            formatting_only.write_text(source + "\n# harmless formatting comment\n", encoding="utf-8")
            self.assertEqual(
                current_v6_critical_ast_hash(formatting_only),
                LOCKED_V6_CRITICAL_AST_HASH,
            )
            changed = root / "changed.py"
            changed.write_text(
                source.replace(
                    "gross_return = float(sum(filled_returns)) / PORTFOLIO_SIZE",
                    "gross_return = float(sum(filled_returns)) / (PORTFOLIO_SIZE - 1)",
                    1,
                ),
                encoding="utf-8",
            )
            self.assertNotEqual(
                current_v6_critical_ast_hash(changed), LOCKED_V6_CRITICAL_AST_HASH
            )
            changed_v5 = root / "changed_v5.py"
            changed_v5.write_bytes(Path(v6_module.v5_module.__file__).read_bytes() + b"\n# drift\n")
            with patch.object(v6_module.v5_module, "__file__", str(changed_v5)):
                with self.assertRaises(V6ProtocolChangeRequiresV7):
                    assert_locked_dependencies()

    def test_manifest_must_be_in_fixed_root_and_reparse_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root / "runtime")
            database = Database(config)
            database.initialize()
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            gates = self._gates(outside, self._hash(outside.read_bytes()))
            ledger = V6FrozenValidationLedger(database)
            with self.assertRaisesRegex(FrozenManifestError, "outside fixed V6 root"):
                ledger.seal(frozen_validation_readiness(gates))

            frozen_root = v6_frozen_root(config)
            frozen_root.mkdir(parents=True)
            inside = frozen_root / "manifest.json"
            inside.write_text("{}", encoding="utf-8")
            gates = self._gates(inside, self._hash(inside.read_bytes()))
            with patch.object(
                v6_module,
                "_is_link_or_reparse",
                side_effect=lambda path: Path(path) == inside,
            ):
                with self.assertRaisesRegex(FrozenManifestError, "symlink/reparse"):
                    ledger.seal(frozen_validation_readiness(gates))

            runtime_ancestor = Path(config.runtime_dir) / "research"
            with patch.object(
                v6_module,
                "_is_link_or_reparse",
                side_effect=lambda path: Path(path) == runtime_ancestor,
            ):
                with self.assertRaisesRegex(FrozenManifestError, "symlink/reparse"):
                    ledger.seal(frozen_validation_readiness(gates))

    def test_frozen_manifest_and_each_shard_are_read_once_into_verified_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory) / "runtime")
            database = Database(config)
            database.initialize()
            gates = self._write_snapshot(v6_frozen_root(config), tamper_after_manifest=False)
            original = v6_module._read_file_once
            with patch.object(v6_module, "_read_file_once", wraps=original) as reader:
                run_v6_frozen_validation_once(
                    database=database, gates=gates, runner_id="single-read"
                )
            frozen_calls = [
                Path(call.args[0]).name
                for call in reader.call_args_list
                if Path(call.args[0]).suffix in {".json", ".parquet"}
                and Path(call.args[0]).is_relative_to(v6_frozen_root(config))
            ]

        self.assertEqual(sorted(frozen_calls), ["2024.parquet", "2025.parquet", "manifest.json"])

    @staticmethod
    def _hash(value: str | bytes) -> str:
        raw = value if isinstance(value, bytes) else value.encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _components(cls) -> dict[str, str]:
        return {
            "historical_universe_master_manifest_hash": cls._hash("master"),
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
            "historical_universe_master": {
                "ready": True,
                "status": "READY",
                "detail": "synthetic fixture",
                "snapshot_id": "synthetic-master",
                "manifest_hash": components["historical_universe_master_manifest_hash"],
                "protocol_version": "historical-security-master-v1",
                "coverage_start": "2018-01-01",
                "coverage_end": "2025-12-31",
                "source_counts": {},
                "reconciliation": {},
                "promotion_blocked": False,
            },
            "event_provenance": {
                "ready": True,
                "schema_version": EVENT_RAW_REPLAY_SCHEMA_VERSION,
                "legacy_selection_schema_version": "early-winner-v5-event-replay-v1",
                "classifier_rule_hash": CLASSIFIER_RULE_HASH,
                "source": "CNINFO_OFFICIAL",
                "content_hash_algorithm": "SHA256_RAW_BYTES",
                "raw_content_rehash_passed": True,
                "announcement_security_binding_passed": True,
                "effective_at_calendar_derived_passed": True,
                "effective_rule_version": EVENT_EFFECTIVE_RULE_VERSION,
                "trading_calendar_hash": components["trading_calendar_content_hash"],
                "snapshot_hash": components["event_provenance_snapshot_hash"],
                "raw_content_manifest_hash": components["event_raw_content_manifest_hash"],
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
                "snapshot_id": "synthetic-v6-snapshot",
                "manifest_path": str(manifest_path),
                "manifest_hash": manifest_hash,
            },
        }

    @classmethod
    def _event_payload(cls, code: str, asof: pd.Timestamp) -> dict[str, object]:
        content = f"cninfo:{code}:{asof.date()}".encode("utf-8")
        content_hash = cls._hash(content)
        published = asof.normalize() + pd.Timedelta(hours=14)
        close_at = asof.normalize() + pd.Timedelta(hours=15)
        next_session = asof + pd.offsets.BDay(1)
        record: dict[str, object] = {
            "event_hash": content_hash,
            "source_url": f"https://www.cninfo.com.cn/{content_hash}.pdf",
            "event_type": "ACQUISITION",
            "event_score": 3.0,
            "published_at": published.isoformat(),
            "effective_at": published.isoformat(),
            "announcement_id": f"ANN-{code}-{asof.date()}",
            "security_code": code,
            "raw_content_base64": base64.b64encode(content).decode("ascii"),
            "raw_content_sha256": content_hash,
            "published_after_close": False,
            "session_close_at": close_at.isoformat(),
            "next_trading_session_at": next_session.isoformat(),
            "effective_rule_version": EVENT_EFFECTIVE_RULE_VERSION,
            "effective_at_calendar_hash": cls._components()[
                "trading_calendar_content_hash"
            ],
        }
        payload = replay_event_provenance([record], asof)
        payload["event_replay_records"] = json.dumps([record], ensure_ascii=False)
        payload["all_event_hashes"] = json.dumps(payload["all_event_hashes"])
        payload["hard_negative_event_hashes"] = json.dumps(
            payload["hard_negative_event_hashes"]
        )
        payload["v6_event_raw_replay_hash"] = v6_raw_event_replay_hash(
            code=code, asof=asof, records=[record]
        )
        return payload

    @classmethod
    def _empty_event_payload(cls, code: str, asof: pd.Timestamp) -> dict[str, object]:
        payload = replay_event_provenance([], asof)
        payload["event_replay_records"] = "[]"
        payload["all_event_hashes"] = "[]"
        payload["hard_negative_event_hashes"] = "[]"
        payload["v6_event_raw_replay_hash"] = v6_raw_event_replay_hash(
            code=code, asof=asof, records=[]
        )
        return payload

    @classmethod
    def _frame(cls, year: int, components: dict[str, str]) -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        for asof in pd.date_range(f"{year}-01-05", periods=32, freq="W-FRI"):
            entry = asof + pd.offsets.BDay(1)
            exit_at = entry + pd.offsets.BDay(40)
            for slot in range(25):
                code = f"{600000 + slot:06d}.SH"
                positive = slot < 5
                row: dict[str, object] = {
                    "asof": asof.date().isoformat(),
                    "code": code,
                    "industry": f"I{slot // 5}",
                    "amount_ratio": 0.5 + slot / 100.0,
                    "listed_days": 500,
                    "valid_days_20": 20,
                    "adv20": 500_000_000.0,
                    "suspended": False,
                    "is_st": False,
                    "is_quit": False,
                    "return_60": 0.05,
                    "turnover_20": 0.02,
                    "price_to_ma60": 1.10,
                    "relative_return_60": float(slot),
                    "execution_status_complete": True,
                    "close": 11.0,
                    "ma60": 10.0,
                    "entry_executable": True,
                    "forward_return_40": 0.20 if positive else -0.01,
                    "label_window_matured": True,
                    "planned_entry_time": entry.isoformat(),
                    "entry_time": entry.isoformat(),
                    "planned_exit_time": exit_at.isoformat(),
                    "exit_time": exit_at.isoformat(),
                }
                row.update(
                    cls._event_payload(code, asof)
                    if positive
                    else cls._empty_event_payload(code, asof)
                )
                for column in (
                    "historical_universe_master_manifest_hash",
                    "event_provenance_snapshot_hash",
                    "event_raw_content_manifest_hash",
                    "trading_calendar_content_hash",
                    "execution_status_content_hash",
                    "label_snapshot_hash",
                ):
                    row[column] = components[column]
                rows.append(row)
        return pd.DataFrame(rows)

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
    def _write_snapshot(cls, root: Path, *, tamper_after_manifest: bool) -> dict[str, object]:
        root.mkdir(parents=True)
        components = cls._components()
        frames = {year: cls._frame(year, components) for year in (2024, 2025)}
        paths = {year: root / f"{year}.parquet" for year in frames}
        for year, frame in frames.items():
            frame.to_parquet(paths[year], index=False)
        descriptors = [cls._descriptor(paths[year], year, frames[year]) for year in (2024, 2025)]
        manifest: dict[str, object] = {
            "manifest_version": MANIFEST_VERSION,
            "project_id": PROJECT_ID,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_hash": PROTOCOL_HASH,
            "snapshot_id": "synthetic-v6-snapshot",
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
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        gates = cls._gates(manifest_path, cls._hash(manifest_path.read_bytes()))
        if tamper_after_manifest:
            with paths[2024].open("ab") as stream:
                stream.write(b"tamper")
        return gates

if __name__ == "__main__":
    unittest.main()
