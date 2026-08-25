from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_platform import delisted_history_audit_runner as runner
from research_platform.delisted_history_quality import (
    DELISTED_HISTORY_SOURCE_INCOMPLETE,
    READY,
)
from research_platform.tests.test_delisted_history_quality import (
    _SyntheticEvidence,
)


class DelistedHistoryAuditRunnerTests(unittest.TestCase):
    def _fixture(
        self, directory: str
    ) -> tuple[Path, _SyntheticEvidence, dict[str, object]]:
        runtime_root = Path(directory) / "runtime"
        fixture = _SyntheticEvidence(
            Path(directory) / "fixture",
            master_root=runtime_root / "security_master",
            input_cas_root=(
                runtime_root
                / "research"
                / runner.PROJECT_ID
                / runner.INPUT_CAS_DIRECTORY
            ),
        )
        manifest_path = Path(fixture.master_identity["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        release: dict[str, object] = {
            "snapshot_id": fixture.master_identity["snapshot_id"],
            "manifest_hash": fixture.master_identity["manifest_hash"],
            "manifest": manifest,
            "quality_report": {
                "gate": {
                    "ready": True,
                    "status": READY,
                    "promotion_blocked": False,
                }
            },
        }
        return runtime_root, fixture, release

    def test_partial_mapping_publishes_replayable_audit_only_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_root, fixture, release = self._fixture(directory)
            calendar = fixture.source_indexes["trading_calendar"]["content_hash"]

            with patch.object(
                runner.HistoricalSecurityMasterStore,
                "load_current_release",
                return_value=release,
            ):
                result = runner.run_delisted_history_audit(
                    runtime_dir=runtime_root,
                    source_index_digests={"trading_calendar": calendar},
                )

            self.assertEqual(result["status"], DELISTED_HISTORY_SOURCE_INCOMPLETE)
            self.assertFalse(result["ready"])
            self.assertTrue(result["promotion_blocked"])
            self.assertTrue(result["partial_source_set"])
            self.assertTrue(result["audit_only"])
            self.assertTrue(result["no_training"])
            self.assertTrue(result["no_trading"])
            self.assertFalse(result["caller_ready_accepted"])
            self.assertEqual(result["source_dataset_count"], 1)
            self.assertEqual(
                result["gate"]["source_dataset_count"], 1
            )
            self.assertTrue(
                (
                    runtime_root
                    / "research"
                    / runner.PROJECT_ID
                    / runner.OUTPUT_DIRECTORY
                    / "current.json"
                ).is_file()
            )

    def test_complete_mapping_can_only_become_ready_from_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_root, fixture, release = self._fixture(directory)
            digests = {
                dataset: identity["content_hash"]
                for dataset, identity in fixture.source_indexes.items()
            }

            with patch.object(
                runner.HistoricalSecurityMasterStore,
                "load_current_release",
                return_value=release,
            ):
                result = runner.run_delisted_history_audit(
                    runtime_dir=runtime_root,
                    source_index_digests=digests,
                )

            self.assertEqual(result["status"], READY)
            self.assertTrue(result["ready"])
            self.assertFalse(result["promotion_blocked"])
            self.assertFalse(result["partial_source_set"])
            self.assertFalse(result["caller_ready_accepted"])
            self.assertTrue(result["no_training"])
            self.assertTrue(result["no_trading"])

    def test_unknown_dataset_and_non_digest_fail_before_publication(self) -> None:
        cases = (
            {},
            {"caller_ready": "0" * 64},
            {"trading_calendar": "ABC"},
            {"trading_calendar": {"digest": "0" * 64, "ready": True}},
        )
        for source_mapping in cases:
            with self.subTest(source_mapping=source_mapping):
                with tempfile.TemporaryDirectory() as directory:
                    runtime_root, _fixture, release = self._fixture(directory)
                    with patch.object(
                        runner.HistoricalSecurityMasterStore,
                        "load_current_release",
                        return_value=release,
                    ):
                        with self.assertRaises(
                            runner.DelistedHistoryAuditRunnerBlockedError
                        ):
                            runner.run_delisted_history_audit(
                                runtime_dir=runtime_root,
                                source_index_digests=source_mapping,  # type: ignore[arg-type]
                            )
                    self.assertFalse(
                        (
                            runtime_root
                            / "research"
                            / runner.PROJECT_ID
                            / runner.OUTPUT_DIRECTORY
                            / "current.json"
                        ).exists()
                    )

    def test_invalid_index_fails_cold_replay_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_root, fixture, release = self._fixture(directory)
            digest = fixture.source_indexes["trading_calendar"]["content_hash"]
            object_path = (
                fixture.input_cas / "sha256" / digest[:2] / digest
            )
            object_path.write_bytes(object_path.read_bytes() + b"\n")

            with patch.object(
                runner.HistoricalSecurityMasterStore,
                "load_current_release",
                return_value=release,
            ):
                with self.assertRaisesRegex(
                    runner.DelistedHistoryAuditRunnerBlockedError,
                    "failed cold replay",
                ):
                    runner.run_delisted_history_audit(
                        runtime_dir=runtime_root,
                        source_index_digests={"trading_calendar": digest},
                    )
            self.assertFalse(
                (
                    runtime_root
                    / "research"
                    / runner.PROJECT_ID
                    / runner.OUTPUT_DIRECTORY
                    / "current.json"
                ).exists()
            )

    def test_non_ready_current_master_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_root, fixture, release = self._fixture(directory)
            release["quality_report"] = {
                "gate": {
                    "ready": False,
                    "status": "BLOCKED_DATA",
                    "promotion_blocked": True,
                }
            }
            digest = fixture.source_indexes["trading_calendar"]["content_hash"]

            with patch.object(
                runner.HistoricalSecurityMasterStore,
                "load_current_release",
                return_value=release,
            ):
                with self.assertRaisesRegex(
                    runner.DelistedHistoryAuditRunnerBlockedError,
                    "not READY",
                ):
                    runner.run_delisted_history_audit(
                        runtime_dir=runtime_root,
                        source_index_digests={"trading_calendar": digest},
                    )

    def test_current_partial_example_is_inert_and_uses_frozen_digests(self) -> None:
        self.assertEqual(
            runner.CURRENT_PARTIAL_SOURCE_INDEX_DIGESTS,
            {
                "raw_execution_bars": (
                    "4444e219c7aa9f7db0fa238e1f1107d0c43a6b5e064430be0d55d3156d123dea"
                ),
                "trading_calendar": (
                    "f1cf94245e1e94ee90d8f447793b253dcce5d24afe77a2f18690037f967f2f11"
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
