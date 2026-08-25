from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from research_platform.__main__ import build_parser, main
from research_platform.tests.helpers import temporary_config
from research_platform.us_pit import (
    LicenseClass,
    SourceDependency,
    SourceRole,
    USPITService,
)
from research_platform.us_pit.hashing import sha256_json


class USPITImportEvidenceCLITests(unittest.TestCase):
    def test_parser_exposes_required_and_optional_evidence_arguments(self) -> None:
        args = build_parser().parse_args(
            [
                "us-pit",
                "import-evidence",
                "--file",
                "announcement.pdf",
                "--dataset",
                "membership_events",
                "--source-id",
                "sp-official-announcement",
                "--source-version",
                "published-2024-01-24",
                "--url",
                "https://www.spglobal.com/announcement.pdf",
                "--published-at",
                "2024-01-24T22:00:00Z",
                "--as-of-date",
                "2024-01-24",
                "--role",
                "SIGNAL_INPUT",
                "--license-class",
                "OFFICIAL_PUBLIC",
                "--media-type",
                "application/pdf",
            ]
        )

        self.assertEqual(args.us_pit_command, "import-evidence")
        self.assertEqual(args.as_of_date, "2024-01-24")
        self.assertEqual(args.role, "SIGNAL_INPUT")
        self.assertEqual(args.license_class, "OFFICIAL_PUBLIC")
        self.assertEqual(args.media_type, "application/pdf")

    def test_import_freezes_local_file_and_prints_batch_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root)
            source = root / "announcement.pdf"
            source.write_bytes(b"reviewed official announcement")
            observed_at = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)

            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch("research_platform.__main__.datetime") as mocked_datetime,
                patch(
                    "research_platform.__main__.USMomentumProgram"
                ) as mocked_program,
                patch.object(
                    USPITService, "capture_official_evidence"
                ) as official_capture,
                patch("research_platform.__main__._print") as output,
                patch(
                    "sys.argv",
                    [
                        "research_platform",
                        "us-pit",
                        "import-evidence",
                        "--file",
                        str(source),
                        "--dataset",
                        "membership_events",
                        "--source-id",
                        "sp-official-announcement",
                        "--source-version",
                        "published-2024-01-24",
                        "--url",
                        "https://www.spglobal.com/announcement.pdf",
                        "--published-at",
                        "2024-01-24T22:00:00Z",
                    ],
                ),
            ):
                mocked_datetime.now.return_value = observed_at
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                self.assertEqual(main(), 0)

            payload = output.call_args.args[0]
            self.assertEqual(len(payload["batch_id"]), 64)
            self.assertEqual(len(payload["dependencies"]), 1)
            dependency = payload["dependencies"][0]
            self.assertEqual(dependency["dataset"], "membership_events")
            self.assertEqual(dependency["role"], "SIGNAL_INPUT")
            self.assertEqual(dependency["license_class"], "OFFICIAL_PUBLIC")
            self.assertEqual(
                dependency["published_at"], "2024-01-24T22:00:00+00:00"
            )
            self.assertNotIn(str(root), str(dependency))
            object_path = USPITService(config.us_pit_dir).store.object_path(
                dependency["object_sha256"]
            )
            self.assertEqual(
                object_path.read_bytes(), b"reviewed official announcement"
            )
            official_capture.assert_not_called()
            mocked_program.assert_called_once_with(config.us_program_database_path)

    def test_naive_publication_timestamp_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root)
            source = root / "announcement.pdf"
            source.write_bytes(b"reviewed official announcement")

            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch("research_platform.__main__.USMomentumProgram"),
                patch(
                    "sys.argv",
                    [
                        "research_platform",
                        "us-pit",
                        "import-evidence",
                        "--file",
                        str(source),
                        "--dataset",
                        "membership_events",
                        "--source-id",
                        "sp-official-announcement",
                        "--source-version",
                        "published-2024-01-24",
                        "--url",
                        "https://www.spglobal.com/announcement.pdf",
                        "--published-at",
                        "2024-01-24T22:00:00",
                    ],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    main()

    def test_lifecycle_v3_cli_requires_and_binds_captured_official_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = temporary_config(root)
            service = USPITService(config.us_pit_dir)
            raw = service.store.put_bytes(b"official lifecycle source CUSIP 037833100")
            raw_batch = service.store.write_source_batch(
                [
                    SourceDependency(
                        source_id="sec-lifecycle-source",
                        source_version="2026-08-01",
                        role=SourceRole.SIGNAL_INPUT,
                        license_class=LicenseClass.OFFICIAL_PUBLIC,
                        object_sha256=raw.sha256,
                        observed_at="2026-08-01T01:00:00+00:00",
                        published_at="2026-08-01T00:00:00+00:00",
                        url="https://www.sec.gov/Archives/lifecycle-source.txt",
                        dataset="lifecycle_observation",
                    )
                ]
            )
            records = [
                {
                    "source_id": "sec-lifecycle-source",
                    "dataset": "lifecycle_observation",
                    "evidence_sha256": raw.sha256,
                    "published_at": "2026-08-01T00:00:00+00:00",
                    "url": "https://www.sec.gov/Archives/lifecycle-source.txt",
                    "observations": [
                        {
                            "security_id": "us_cusip_037833100",
                            "identifier_type": "CUSIP",
                            "identifier_value": "037833100",
                            "observed_status": "LISTED",
                            "evidence_locator": "fixture:CUSIP",
                            "observed_through": "2026-07-31",
                            "status_effective_at": "",
                            "evidence_excerpt": "official lifecycle source",
                        }
                    ],
                }
            ]
            lifecycle = root / "lifecycle.json"
            lifecycle.write_text(
                json.dumps(
                    {
                        "format_version": "us-lifecycle-surveillance-v3",
                        "current_through": "2026-07-31",
                        "covered_security_ids": ["us_cusip_037833100"],
                        "covered_security_ids_sha256": sha256_json(
                            ["us_cusip_037833100"]
                        ),
                        "source_records": records,
                        "source_records_sha256": sha256_json(records),
                    }
                ),
                encoding="utf-8",
            )
            observed_at = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)
            with (
                patch("research_platform.__main__.PlatformConfig", return_value=config),
                patch("research_platform.__main__.datetime") as mocked_datetime,
                patch("research_platform.__main__.USMomentumProgram"),
                patch("research_platform.__main__._print") as output,
                patch(
                    "sys.argv",
                    [
                        "research_platform",
                        "us-pit",
                        "import-lifecycle",
                        "--file",
                        str(lifecycle),
                        "--source-id",
                        "verified-lifecycle-summary",
                        "--source-version",
                        "2026-07-31",
                        "--url",
                        "https://www.sec.gov/Archives/lifecycle-summary.json",
                        "--published-at",
                        "2026-08-01T00:00:00Z",
                        "--source-batch",
                        raw_batch.batch_id,
                    ],
                ),
            ):
                mocked_datetime.now.return_value = observed_at
                mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                self.assertEqual(main(), 0)
            payload = output.call_args.args[0]
            dependency = payload["dependencies"][0]
            self.assertEqual(
                dependency["metadata"]["source_dependency_object_sha256s"],
                [raw.sha256],
            )
            self.assertEqual(
                dependency["metadata"]["coverage_contract_version"], 3
            )

            args = build_parser().parse_args(
                [
                    "us-pit",
                    "import-lifecycle",
                    "--file",
                    str(lifecycle),
                    "--source-id",
                    "verified-lifecycle-summary",
                    "--source-version",
                    "2026-07-31",
                    "--url",
                    "https://www.sec.gov/Archives/lifecycle-summary.json",
                    "--published-at",
                    "2026-08-01T00:00:00Z",
                    "--source-batch",
                    raw_batch.batch_id,
                ]
            )
            self.assertEqual(args.source_batch, [raw_batch.batch_id])


if __name__ == "__main__":
    unittest.main()
