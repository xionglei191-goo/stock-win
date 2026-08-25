from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from research_platform.us_pit.hashing import canonical_json_bytes, sha256_file
from research_platform.us_pit.models import (
    QUALITY_POLICY_VERSION,
    LicenseClass,
    QualityReport,
    ReleaseManifest,
    ReleaseStatus,
    SourceDependency,
    SourceRole,
    UNIVERSE_ID,
)
from research_platform.us_pit.store import JSON_MEDIA_TYPE, USPITStore


class USPITCatalogTests(unittest.TestCase):
    def _dependency(self, store: USPITStore) -> SourceDependency:
        reference = store.put_bytes(b"official fixture")
        return SourceDependency(
            source_id="official-fixture",
            source_version="2024-01",
            role=SourceRole.SIGNAL_INPUT,
            license_class=LicenseClass.OFFICIAL_PUBLIC,
            object_sha256=reference.sha256,
            observed_at="2024-02-01T00:00:00+00:00",
            url="https://official.example/fixture",
            dataset="membership_events",
            as_of_date="2024-01-31",
            published_at="2024-01-31T22:00:00+00:00",
        )

    def _release(self, store: USPITStore) -> tuple[ReleaseManifest, dict]:
        report = QualityReport(
            policy_version=QUALITY_POLICY_VERSION,
            status=ReleaseStatus.DATA_READY,
            includes_delisted=True,
            issues=(),
            metrics={"decision_months": 60},
        )
        report_ref = store.put_bytes(
            canonical_json_bytes(report.to_dict()),
            media_type=JSON_MEDIA_TYPE,
        )
        descriptor = store.descriptor("quality_report", report_ref)
        manifest = ReleaseManifest(
            universe_id=UNIVERSE_ID,
            created_at="2024-02-02T00:00:00+00:00",
            status=ReleaseStatus.DATA_READY,
            artifacts={"quality_report": descriptor},
            sources=(self._dependency(store),),
        )
        return manifest, {"quality_report": report_ref}

    @staticmethod
    def _rows(path: Path, query: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            return list(connection.execute(query, parameters))

    def test_source_batch_catalog_is_idempotent_and_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "us_pit")
            dependency = self._dependency(store)
            first = store.write_source_batch([dependency])
            second = store.write_source_batch([dependency])

            self.assertEqual(first.batch_id, second.batch_id)
            self.assertTrue(store.catalog_path.is_file())
            rows = self._rows(
                store.catalog_path,
                "SELECT * FROM us_pit_source_batches",
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["batch_sha256"], sha256_file(first.path))
            self.assertEqual(rows[0]["path"], f"raw/batches/{first.batch_id}.json")
            self.assertEqual(
                json.loads(rows[0]["source_lineage_json"]),
                [dependency.to_dict()],
            )

            with closing(sqlite3.connect(store.catalog_path)) as connection, connection:
                connection.execute(
                    """
                    UPDATE us_pit_source_batches
                    SET source_lineage_json = '[]'
                    WHERE batch_id = ?
                    """,
                    (first.batch_id,),
                )
            with self.assertRaisesRegex(ValueError, "source batch conflict"):
                store.write_source_batch([dependency])

    def test_release_catalog_tracks_artifacts_and_load_backfills_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = USPITStore(Path(temporary) / "us_pit")
            manifest, objects = self._release(store)
            release = store.publish_release(manifest, objects)
            repeated = store.publish_release(manifest, objects)
            self.assertEqual(repeated.release_id, release.release_id)

            release_rows = self._rows(
                store.catalog_path,
                "SELECT * FROM us_pit_releases WHERE release_id = ?",
                (release.release_id,),
            )
            artifact_rows = self._rows(
                store.catalog_path,
                "SELECT * FROM us_pit_release_artifacts WHERE release_id = ?",
                (release.release_id,),
            )
            self.assertEqual(len(release_rows), 1)
            self.assertEqual(len(artifact_rows), 1)
            self.assertEqual(release_rows[0]["status"], "DATA_READY")
            self.assertEqual(release_rows[0]["universe_id"], UNIVERSE_ID)
            self.assertEqual(
                release_rows[0]["manifest_path"],
                f"releases/{release.release_id}/manifest.json",
            )
            gate = json.loads(release_rows[0]["gate_report_artifact"])
            self.assertEqual(gate["name"], "quality_report")
            self.assertEqual(
                artifact_rows[0]["path"],
                f"releases/{release.release_id}/quality_report.json",
            )
            self.assertEqual(
                artifact_rows[0]["object_sha256"],
                manifest.artifacts["quality_report"].object_sha256,
            )

            # Simulate a catalog lost between the atomic directory rename and
            # the SQLite commit. Loading an immutable release reconstructs the
            # metadata without copying any artifact contents into SQLite.
            with closing(sqlite3.connect(store.catalog_path)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    "DELETE FROM us_pit_releases WHERE release_id = ?",
                    (release.release_id,),
                )
            store.load_release(release.release_id)
            self.assertEqual(
                len(
                    self._rows(
                        store.catalog_path,
                        "SELECT 1 FROM us_pit_releases WHERE release_id = ?",
                        (release.release_id,),
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    self._rows(
                        store.catalog_path,
                        "SELECT 1 FROM us_pit_release_artifacts WHERE release_id = ?",
                        (release.release_id,),
                    )
                ),
                1,
            )

    def test_release_catalog_conflict_rolls_back_and_temp_root_is_deletable(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        store = USPITStore(root / "us_pit")
        manifest, objects = self._release(store)
        release = store.publish_release(manifest, objects)
        with closing(sqlite3.connect(store.catalog_path)) as connection, connection:
            connection.execute(
                """
                UPDATE us_pit_release_artifacts
                SET object_sha256 = ?
                WHERE release_id = ? AND artifact_name = 'quality_report'
                """,
                ("0" * 64, release.release_id),
            )
        with self.assertRaisesRegex(ValueError, "release artifact conflict"):
            store.load_release(release.release_id)

        # USPITStore keeps no persistent SQLite handle. This assertion is
        # specifically important on Windows, where an unclosed connection
        # prevents TemporaryDirectory cleanup.
        temporary.cleanup()
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
