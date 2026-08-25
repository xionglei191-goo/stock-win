from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research_platform.__main__ import build_parser, main
from research_platform.tests.helpers import temporary_config


class EarlyWinnerCaptureCLITests(unittest.TestCase):
    def test_parsers_expose_only_bounded_runtime_controls(self) -> None:
        cases = (
            (
                "early-winner-capture-sse-delisted-bars",
                "max_new_captures",
                5,
            ),
            (
                "early-winner-capture-cninfo-announcements",
                "max_new_targets",
                1,
            ),
        )
        forbidden = {
            "start",
            "end",
            "codes",
            "ready",
            "caller_ready",
            "audit_start",
            "audit_end",
            "materialize",
        }
        for command, limit_name, expected_limit in cases:
            with self.subTest(command=command):
                args = build_parser().parse_args([command])
                self.assertEqual(args.command, command)
                self.assertEqual(getattr(args, limit_name), expected_limit)
                self.assertEqual(args.timeout_seconds, 30.0)
                self.assertTrue(forbidden.isdisjoint(vars(args)))
                for option in ("--start", "--end", "--codes", "--ready"):
                    with self.subTest(command=command, rejected_option=option):
                        with patch("sys.stderr"):
                            with self.assertRaises(SystemExit):
                                build_parser().parse_args(
                                    [command, option, "caller-value"]
                                )

    def test_sse_capture_is_early_audit_only_and_globally_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root)
            result = SimpleNamespace(
                deferred_codes=("600001.SH", "600002.SH"),
                to_dict=lambda: {
                    "eligible_capture_complete": True,
                    "complete": True,
                },
            )
            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.sse_delisted_raw_bars."
                    "capture_current_sse_delisted_raw_bars",
                    return_value=result,
                ) as capture,
                patch("research_platform.__main__.PlatformService") as platform,
                patch("research_platform.__main__._print") as output,
                patch(
                    "sys.argv",
                    [
                        "research_platform",
                        "early-winner-capture-sse-delisted-bars",
                    ],
                ),
            ):
                self.assertEqual(main(), 0)

            capture.assert_called_once_with(
                security_master_root=root / "data" / "security_master",
                cas_root=(
                    root
                    / "data"
                    / "research"
                    / "early_winner_v4"
                    / "sse_delisted_raw_bars"
                    / "cas"
                ),
                max_new_captures=5,
                timeout_seconds=30.0,
            )
            platform.assert_not_called()
            payload = output.call_args.args[0]
            self.assertEqual(payload["status"], "BLOCKED_DATA")
            self.assertTrue(payload["eligible_capture_complete"])
            self.assertFalse(payload["complete"])
            self.assertFalse(payload["global_complete"])
            self.assertFalse(payload["ready"])
            self.assertEqual(payload["deferred_count"], 2)
            self.assertTrue(payload["audit_only"])
            self.assertTrue(payload["no_training"])
            self.assertTrue(payload["no_trading"])

    def test_cninfo_capture_reports_progress_without_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root)
            progress = SimpleNamespace(
                complete=True,
                to_dict=lambda: {
                    "captured_count": 140,
                    "missing_count": 0,
                    "complete": True,
                },
            )
            coordinator = Mock()
            coordinator.capture.return_value = progress
            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.cninfo_announcement_capture."
                    "CninfoAnnouncementCaptureCoordinator",
                    return_value=coordinator,
                ) as constructor,
                patch("research_platform.__main__.PlatformService") as platform,
                patch("research_platform.__main__._print") as output,
                patch(
                    "sys.argv",
                    [
                        "research_platform",
                        "early-winner-capture-cninfo-announcements",
                    ],
                ),
            ):
                self.assertEqual(main(), 0)

            research_root = root / "data" / "research" / "early_winner_v4"
            constructor.assert_called_once_with(
                cas_root=(research_root / "cninfo_delisted_disclosures" / "cas"),
                checkpoint_root=(
                    research_root
                    / "cninfo_delisted_disclosures"
                    / "checkpoints_v1"
                ),
                master_store_root=root / "data" / "security_master",
                timeout_seconds=30.0,
            )
            coordinator.capture.assert_called_once_with(max_new_targets=1)
            coordinator.materialize_quality_index.assert_not_called()
            platform.assert_not_called()
            payload = output.call_args.args[0]
            self.assertEqual(payload["status"], "BLOCKED_DATA")
            self.assertTrue(payload["capture_complete"])
            self.assertFalse(payload["complete"])
            self.assertFalse(payload["global_complete"])
            self.assertFalse(payload["ready"])
            self.assertTrue(payload["audit_only"])

    def test_capture_exceptions_are_json_blocked_and_exit_two(self) -> None:
        cases = (
            (
                "early-winner-capture-sse-delisted-bars",
                "research_platform.sse_delisted_raw_bars."
                "capture_current_sse_delisted_raw_bars",
                "SSE_DELISTED_RAW_BARS_CAPTURE_FAILED_CLOSED",
            ),
            (
                "early-winner-capture-cninfo-announcements",
                "research_platform.cninfo_announcement_capture."
                "CninfoAnnouncementCaptureCoordinator",
                "CNINFO_ANNOUNCEMENTS_CAPTURE_FAILED_CLOSED",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            for command, target, reason in cases:
                with self.subTest(command=command):
                    with (
                        patch(
                            "research_platform.__main__.PlatformConfig",
                            return_value=config,
                        ),
                        patch(target, side_effect=RuntimeError("sealed failure")),
                        patch("research_platform.__main__.PlatformService") as platform,
                        patch("research_platform.__main__._print") as output,
                        patch("sys.argv", ["research_platform", command]),
                    ):
                        self.assertEqual(main(), 2)

                    platform.assert_not_called()
                    payload = output.call_args.args[0]
                    self.assertEqual(payload["status"], "BLOCKED_DATA")
                    self.assertEqual(payload["reason"], reason)
                    self.assertEqual(payload["error_type"], "RuntimeError")
                    self.assertEqual(payload["detail"], "sealed failure")
                    self.assertFalse(payload["global_complete"])
                    self.assertFalse(payload["ready"])
                    self.assertTrue(payload["audit_only"])
                    self.assertTrue(payload["no_training"])
                    self.assertTrue(payload["no_trading"])


class EarlyWinnerDelistedHistoryAuditCLITests(unittest.TestCase):
    command = "early-winner-audit-delisted-history"

    def test_parser_has_no_caller_readiness_date_or_path_controls(self) -> None:
        args = build_parser().parse_args([self.command])
        self.assertEqual(args.command, self.command)
        self.assertIsNone(args.source_index)
        forbidden = {
            "start",
            "end",
            "ready",
            "caller_ready",
            "runtime_dir",
            "input_cas_root",
            "output_root",
        }
        self.assertTrue(forbidden.isdisjoint(vars(args)))
        for option in (
            "--start",
            "--end",
            "--ready",
            "--runtime-dir",
            "--input-cas-root",
            "--output-root",
        ):
            with self.subTest(rejected_option=option):
                with patch("sys.stderr"):
                    with self.assertRaises(SystemExit):
                        build_parser().parse_args(
                            [self.command, option, "caller-value"]
                        )

    def test_default_runs_frozen_partial_audit_before_platform_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            result = {
                "status": "DELISTED_HISTORY_SOURCE_INCOMPLETE",
                "ready": False,
                "promotion_blocked": True,
                "source_dataset_count": 2,
            }
            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.delisted_history_audit_runner."
                    "run_current_partial_source_example",
                    return_value=result,
                ) as current_audit,
                patch(
                    "research_platform.delisted_history_audit_runner."
                    "run_delisted_history_audit"
                ) as explicit_audit,
                patch("research_platform.__main__.PlatformService") as platform,
                patch("research_platform.__main__._print") as output,
                patch("sys.argv", ["research_platform", self.command]),
            ):
                self.assertEqual(main(), 0)

            current_audit.assert_called_once_with(runtime_dir=config.runtime_dir)
            explicit_audit.assert_not_called()
            platform.assert_not_called()
            payload = output.call_args.args[0]
            self.assertEqual(
                payload["status"], "DELISTED_HISTORY_SOURCE_INCOMPLETE"
            )
            self.assertFalse(payload["ready"])
            self.assertTrue(payload["audit_only"])
            self.assertTrue(payload["no_training"])
            self.assertTrue(payload["no_trading"])
            self.assertFalse(payload["caller_ready_accepted"])

    def test_repeated_source_indexes_are_passed_as_strict_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            calendar = "a" * 64
            bars = "b" * 64
            result = {
                "status": "DELISTED_HISTORY_SOURCE_INCOMPLETE",
                "ready": False,
            }
            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.delisted_history_audit_runner."
                    "run_current_partial_source_example"
                ) as current_audit,
                patch(
                    "research_platform.delisted_history_audit_runner."
                    "run_delisted_history_audit",
                    return_value=result,
                ) as explicit_audit,
                patch("research_platform.__main__.PlatformService") as platform,
                patch("research_platform.__main__._print"),
                patch(
                    "sys.argv",
                    [
                        "research_platform",
                        self.command,
                        "--source-index",
                        f"trading_calendar={calendar}",
                        "--source-index",
                        f"raw_execution_bars={bars}",
                    ],
                ),
            ):
                self.assertEqual(main(), 0)

            current_audit.assert_not_called()
            explicit_audit.assert_called_once_with(
                runtime_dir=config.runtime_dir,
                source_index_digests={
                    "trading_calendar": calendar,
                    "raw_execution_bars": bars,
                },
            )
            platform.assert_not_called()

    def test_invalid_or_duplicate_source_index_fails_as_json(self) -> None:
        cases = (
            "trading_calendar=ABC",
            "Trading_calendar=" + "a" * 64,
            "trading_calendar=" + "a" * 64 + "=extra",
        )
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            for value in cases:
                with self.subTest(value=value):
                    with (
                        patch(
                            "research_platform.__main__.PlatformConfig",
                            return_value=config,
                        ),
                        patch(
                            "research_platform.delisted_history_audit_runner."
                            "run_delisted_history_audit"
                        ) as audit,
                        patch("research_platform.__main__.PlatformService") as platform,
                        patch("research_platform.__main__._print") as output,
                        patch(
                            "sys.argv",
                            [
                                "research_platform",
                                self.command,
                                "--source-index",
                                value,
                            ],
                        ),
                    ):
                        self.assertEqual(main(), 2)
                    audit.assert_not_called()
                    platform.assert_not_called()
                    payload = output.call_args.args[0]
                    self.assertEqual(payload["status"], "BLOCKED_DATA")
                    self.assertEqual(
                        payload["reason"],
                        "DELISTED_HISTORY_AUDIT_FAILED_CLOSED",
                    )
                    self.assertTrue(payload["audit_only"])

            duplicate = "trading_calendar=" + "a" * 64
            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.delisted_history_audit_runner."
                    "run_delisted_history_audit"
                ) as audit,
                patch("research_platform.__main__.PlatformService") as platform,
                patch("research_platform.__main__._print") as output,
                patch(
                    "sys.argv",
                    [
                        "research_platform",
                        self.command,
                        "--source-index",
                        duplicate,
                        "--source-index",
                        duplicate,
                    ],
                ),
            ):
                self.assertEqual(main(), 2)
            audit.assert_not_called()
            platform.assert_not_called()
            self.assertIn("duplicate", output.call_args.args[0]["detail"])

    def test_runner_exception_is_json_blocked_and_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch(
                    "research_platform.delisted_history_audit_runner."
                    "run_current_partial_source_example",
                    side_effect=RuntimeError("cold replay failed"),
                ),
                patch("research_platform.__main__.PlatformService") as platform,
                patch("research_platform.__main__._print") as output,
                patch("sys.argv", ["research_platform", self.command]),
            ):
                self.assertEqual(main(), 2)

            platform.assert_not_called()
            payload = output.call_args.args[0]
            self.assertEqual(payload["status"], "BLOCKED_DATA")
            self.assertFalse(payload["ready"])
            self.assertTrue(payload["promotion_blocked"])
            self.assertEqual(payload["error_type"], "RuntimeError")
            self.assertEqual(payload["detail"], "cold replay failed")
            self.assertTrue(payload["audit_only"])
            self.assertTrue(payload["no_training"])
            self.assertTrue(payload["no_trading"])


if __name__ == "__main__":
    unittest.main()
