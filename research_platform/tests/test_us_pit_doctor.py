from __future__ import annotations

import unittest

from research_platform.__main__ import _latest_release_summary


class USPITDoctorTests(unittest.TestCase):
    def test_latest_release_is_selected_by_created_at_not_hash(self) -> None:
        releases = [
            {
                "release_id": "f" * 64,
                "created_at": "2026-08-01T00:00:00+00:00",
                "status": "DATA_BLOCKED",
            },
            {
                "release_id": "0" * 64,
                "created_at": "2026-08-02T00:00:00+00:00",
                "status": "DATA_READY",
            },
        ]

        latest = _latest_release_summary(releases)

        self.assertIsNotNone(latest)
        self.assertEqual("0" * 64, latest["release_id"])

    def test_empty_catalog_has_no_latest_release(self) -> None:
        self.assertIsNone(_latest_release_summary([]))


if __name__ == "__main__":
    unittest.main()
