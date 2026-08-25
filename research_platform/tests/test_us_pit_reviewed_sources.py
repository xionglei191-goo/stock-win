from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from research_platform.us_pit import (
    LicenseClass,
    SourceRole,
    SyncRequest,
    USPITService,
)
from research_platform.us_pit.sources_reviewed import (
    ReviewedEvidenceSpec,
    ReviewedLocalEvidenceAdapter,
)


class ReviewedEvidenceSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "announcement.pdf"
        self.source.write_bytes(b"official announcement fixture")
        self.request = SyncRequest(
            start_date=date(2024, 1, 1),
            end_date=date(2026, 12, 31),
            observed_at=datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc),
        )

    def _spec(self, **updates: object) -> ReviewedEvidenceSpec:
        values: dict[str, object] = {
            "path": self.source,
            "dataset": "membership_events",
            "source_id": "sp-official-announcement",
            "source_version": "published-2024-01-24",
            "public_url": "https://www.spglobal.com/spdji/en/documents/indexnews/fixture.pdf",
            "role": SourceRole.SIGNAL_INPUT,
            "license_class": LicenseClass.OFFICIAL_PUBLIC,
            "published_at": datetime(2024, 1, 24, 22, 0, tzinfo=timezone.utc),
        }
        values.update(updates)
        return ReviewedEvidenceSpec(**values)  # type: ignore[arg-type]

    def test_reviewed_file_is_frozen_with_causal_lineage(self) -> None:
        service = USPITService(self.root / "store")
        batch = service.sync(
            ReviewedLocalEvidenceAdapter(self._spec()),
            self.request,
        )

        self.assertEqual(len(batch.dependencies), 1)
        dependency = batch.dependencies[0]
        self.assertEqual(dependency.dataset, "membership_events")
        self.assertEqual(dependency.role, SourceRole.SIGNAL_INPUT)
        self.assertEqual(
            service.store.object_path(dependency.object_sha256).read_bytes(),
            b"official announcement fixture",
        )
        self.assertNotIn(str(self.root), str(dependency.metadata))
        self.assertFalse(dependency.metadata["normalization_performed"])

    def test_signal_input_requires_publication_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "publication timestamp"):
            self._spec(published_at=None)

    def test_local_placeholder_and_unlicensed_sources_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            self._spec(public_url="file:///private/announcement.pdf")
        with self.assertRaisesRegex(ValueError, "placeholder"):
            self._spec(public_url="https://example.invalid/announcement.pdf")
        with self.assertRaisesRegex(ValueError, "unlicensed"):
            self._spec(license_class=LicenseClass.UNLICENSED_REFERENCE)

    def test_future_publication_and_out_of_window_asof_fail_closed(self) -> None:
        future = self._spec(
            published_at=datetime(2026, 8, 14, tzinfo=timezone.utc)
        )
        with self.assertRaisesRegex(ValueError, "after observed_at"):
            tuple(ReviewedLocalEvidenceAdapter(future).fetch(self.request))

        holdings = self._spec(
            dataset="fund_holdings_observed",
            role=SourceRole.VALIDATION_ANCHOR,
            as_of_date=date(2020, 12, 31),
        )
        with self.assertRaisesRegex(ValueError, "outside the sync window"):
            tuple(ReviewedLocalEvidenceAdapter(holdings).fetch(self.request))


if __name__ == "__main__":
    unittest.main()
