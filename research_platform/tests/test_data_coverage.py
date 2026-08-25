from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from research_platform.data_coverage import sector_membership_coverage


class DataCoverageTests(unittest.TestCase):
    def _prepare_database(self, path: Path, rows: list[tuple[str, str, str]]) -> None:
        connection = sqlite3.connect(str(path))
        connection.execute(
            "CREATE TABLE data_snapshots (snapshot_id TEXT, created_at TEXT, "
            "content_hash TEXT, dataset TEXT, query_json TEXT)"
        )
        for snapshot_id, asof, quality in rows:
            connection.execute(
                "INSERT INTO data_snapshots VALUES (?,?,?,?,?)",
                (
                    snapshot_id,
                    "2026-08-25T10:00:00+08:00",
                    "hash-" + snapshot_id,
                    "sector_membership",
                    json.dumps({"asof": asof, "quality": quality}),
                ),
            )
        connection.commit()
        connection.close()

    def test_empty_database_reports_zeroes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "research.db"
            self._prepare_database(database, [])
            report = sector_membership_coverage(database)
            self.assertEqual(report["snapshot_count"], 0)
            self.assertEqual(report["distinct_effective_dates"], 0)
            self.assertIsNone(report["latest_effective"])
            self.assertEqual(report["gaps"], [])

    def test_counts_qualities_and_detects_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "research.db"
            self._prepare_database(
                database,
                [
                    ("s1", "2026-01-05", "LIMITED"),
                    ("s2", "2026-01-06", "LIMITED"),
                    ("s3", "2026-03-01", "HISTORICAL_SNAPSHOT"),
                ],
            )
            report = sector_membership_coverage(database)
            self.assertEqual(report["snapshot_count"], 3)
            self.assertEqual(report["distinct_effective_dates"], 3)
            self.assertEqual(report["earliest_effective"], "2026-01-05")
            self.assertEqual(report["latest_effective"], "2026-03-01")
            self.assertEqual(report["quality_counts"], {"LIMITED": 2, "HISTORICAL_SNAPSHOT": 1})
            self.assertEqual(len(report["gaps"]), 1)
            self.assertEqual(report["gaps"][0]["from"], "2026-01-06")
            self.assertEqual(report["gaps"][0]["to"], "2026-03-01")

    def test_ignores_other_datasets_and_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "research.db"
            self._prepare_database(database, [("s1", "2026-01-05", "LIMITED")])
            connection = sqlite3.connect(str(database))
            connection.execute(
                "INSERT INTO data_snapshots VALUES (?,?,?,?,?)",
                ("other", "t", "h", "daily_front", "{}"),
            )
            connection.execute(
                "INSERT INTO data_snapshots VALUES (?,?,?,?,?)",
                ("broken", "t", "h", "sector_membership", "{not-json"),
            )
            connection.commit()
            connection.close()
            report = sector_membership_coverage(database)
            self.assertEqual(report["snapshot_count"], 2)  # s1 + broken row
            self.assertEqual(report["distinct_effective_dates"], 1)


if __name__ == "__main__":
    unittest.main()
