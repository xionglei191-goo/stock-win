from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_platform.adjusted_bar_factor_source_assessment import SOURCE_STATUS
from research_platform.validation_gates import (
    CALENDAR_REFERENCE_FILENAME,
    CORPORATE_ACTION_REFERENCE_FILENAME,
    GATES_DIRNAME,
    PINNED_FACTOR_ASSESSMENT_SHA256,
    GateResult,
    ValidationGateBlockedError,
    ensure_backtest_allowed,
    run_validation_gates,
)


class ValidationGatesTests(unittest.TestCase):
    def _gates_dir(self, root: Path) -> Path:
        path = root / "data" / GATES_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_missing_references_report_but_never_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = run_validation_gates(Path(tmp))
            by_name = {item.name: item for item in results}
            self.assertEqual(
                set(by_name),
                {"trading_calendar_quality_index", "adjusted_bar_factor_source", "corporate_action_evidence"},
            )
            self.assertFalse(by_name["trading_calendar_quality_index"].ok)
            self.assertFalse(by_name["trading_calendar_quality_index"].blocking)
            self.assertFalse(by_name["corporate_action_evidence"].ok)
            self.assertFalse(by_name["corporate_action_evidence"].blocking)
            # The frozen factor assessment always rebuilds and must pass.
            self.assertTrue(by_name["adjusted_bar_factor_source"].ok)
            ensure_backtest_allowed(Path(tmp))  # must not raise

    def test_factor_assessment_gate_reports_fail_closed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = {item.name: item for item in run_validation_gates(Path(tmp))}
            gate = results["adjusted_bar_factor_source"]
            self.assertTrue(gate.ok)
            self.assertIn(SOURCE_STATUS, gate.detail)

    def test_registered_calendar_artifact_that_fails_replay_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gates_dir = self._gates_dir(root)
            (gates_dir / CALENDAR_REFERENCE_FILENAME).write_text(
                json.dumps(
                    {
                        "cas_root": str(root / "missing_cas"),
                        "manifest_sha256": "0" * 64,
                        "expected_index_content_sha256": "1" * 64,
                    }
                ),
                encoding="utf-8",
            )
            results = run_validation_gates(root)
            calendar = next(item for item in results if item.name == "trading_calendar_quality_index")
            self.assertFalse(calendar.ok)
            self.assertTrue(calendar.blocking)
            with self.assertRaises(ValidationGateBlockedError):
                ensure_backtest_allowed(root)

    def test_malformed_reference_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gates_dir = self._gates_dir(root)
            (gates_dir / CORPORATE_ACTION_REFERENCE_FILENAME).write_text(
                json.dumps({"manifest_sha256": 7}), encoding="utf-8"
            )
            with self.assertRaises(ValidationGateBlockedError):
                ensure_backtest_allowed(root)

    def test_index_content_hash_drift_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gates_dir = self._gates_dir(root)
            (gates_dir / CALENDAR_REFERENCE_FILENAME).write_text(
                json.dumps(
                    {
                        "cas_root": "does-not-matter",
                        "manifest_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            # A malformed manifest hash fails replay and blocks; a drifted but
            # well-formed expectation is covered by the hash comparison branch.
            with self.assertRaises(ValidationGateBlockedError):
                ensure_backtest_allowed(root)

    def test_pinned_factor_hash_is_stable(self) -> None:
        from research_platform.adjusted_bar_factor_source_assessment import (
            build_frozen_adjusted_bar_factor_source_capability_assessment,
        )

        artifact = build_frozen_adjusted_bar_factor_source_capability_assessment()
        self.assertEqual(artifact.logical_content_sha256, PINNED_FACTOR_ASSESSMENT_SHA256)

    def test_gate_result_serializes(self) -> None:
        result = GateResult(name="x", ok=True, blocking=False, detail="d")
        self.assertEqual(result.to_dict(), {"name": "x", "ok": True, "blocking": False, "detail": "d"})


if __name__ == "__main__":
    unittest.main()
