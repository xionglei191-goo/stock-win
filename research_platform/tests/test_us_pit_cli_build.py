from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.us_pit import (
    LicenseClass,
    SourceDependency,
    SourceRole,
    USPITService,
)


class USPITReviewedBuildTests(unittest.TestCase):
    def test_build_from_directory_requires_every_normalized_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = USPITService(root / "store")
            reference = service.store.put_bytes(b"official")
            batch = service.store.write_source_batch(
                [
                    SourceDependency(
                        source_id="official",
                        source_version="1",
                        role=SourceRole.SIGNAL_INPUT,
                        license_class=LicenseClass.OFFICIAL_PUBLIC,
                        object_sha256=reference.sha256,
                        observed_at="2026-01-01T00:00:00+00:00",
                        url="https://example.invalid/evidence",
                        dataset="membership_events",
                    )
                ]
            )
            reviewed = root / "reviewed"
            reviewed.mkdir()
            pd.DataFrame({"unexpected": [1]}).to_parquet(
                reviewed / "membership_monthly.parquet", index=False
            )

            with self.assertRaisesRegex(ValueError, "prepare-market"):
                service.build_from_directory(
                    reviewed,
                    source_batch_ids=[batch.batch_id],
                )

    def test_source_batch_loading_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = USPITService(Path(directory) / "store")
            reference = service.store.put_bytes(b"official")
            batch = service.store.write_source_batch(
                [
                    SourceDependency(
                        source_id="official",
                        source_version="1",
                        role=SourceRole.VALIDATION_ANCHOR,
                        license_class=LicenseClass.OFFICIAL_PUBLIC,
                        object_sha256=reference.sha256,
                        observed_at="2026-01-01T00:00:00+00:00",
                        url="https://example.invalid/evidence",
                        dataset="fund_holdings_observed",
                    )
                ]
            )
            self.assertEqual(
                service.store.load_source_batch(batch.batch_id).batch_id,
                batch.batch_id,
            )
            batch.path.chmod(0o666)
            batch.path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                service.store.load_source_batch(batch.batch_id)


if __name__ == "__main__":
    unittest.main()
