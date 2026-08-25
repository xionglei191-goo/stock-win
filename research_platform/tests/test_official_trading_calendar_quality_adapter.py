from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from research_platform import official_trading_calendar as calendar
from research_platform.delisted_history_quality import _SourceEvidenceError, _load_dataset
from research_platform.official_trading_calendar_quality_adapter import (
    OFFICIAL_MANIFEST_ROLE,
    build_official_trading_calendar_quality_index,
    materialize_official_trading_calendar_quality_index,
)
from research_platform.tests.test_official_trading_calendar import (
    FIXED_NOW,
    _fixture_session,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonl(rows: list[dict[str, object]]) -> bytes:
    return b"\n".join(_canonical(row) for row in rows) + b"\n"


class OfficialTradingCalendarQualityAdapterTests(unittest.TestCase):
    def _build(self, root: Path):
        cas = calendar.OfficialTradingCalendarCAS(root)
        artifact = calendar.OfficialTradingCalendarClient(
            cas=cas,
            session=_fixture_session(),
            clock=lambda: FIXED_NOW,
        ).fetch()
        manifest = calendar.OfficialTradingCalendarManifestStore(cas).seal(artifact)
        reference = build_official_trading_calendar_quality_index(
            cas_root=root,
            manifest_sha256=manifest.manifest_sha256,
        )
        return cas, artifact, manifest, reference

    def test_builds_a_real_calendar_source_index_from_cold_replayed_v2_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, artifact, manifest, reference = self._build(root)

            loaded = _load_dataset(
                "trading_calendar",
                reference.to_source_identity(),
                root,
            )
            index_bytes, _ = cas.read_blob(reference.content_hash)
            index = json.loads(index_bytes)

            self.assertEqual(loaded.row_count, 4_382)
            self.assertEqual(len(loaded.partitions), 12)
            self.assertEqual(
                reference.official_manifest_sha256,
                manifest.manifest_sha256,
            )
            self.assertEqual(
                reference.official_logical_content_sha256,
                artifact.logical_content_sha256,
            )
            self.assertEqual(
                index["upstream_evidence"]["protocol_version"],
                calendar.PROTOCOL_VERSION,
            )
            self.assertEqual(
                index["upstream_evidence"]["manifest_sha256"],
                manifest.manifest_sha256,
            )
            self.assertEqual(
                index["upstream_evidence"]["logical_content_sha256"],
                artifact.logical_content_sha256,
            )
            for partition in index["partitions"]:
                manifest_sources = [
                    source
                    for source in partition["raw_sources"]
                    if source["role"] == OFFICIAL_MANIFEST_ROLE
                ]
                self.assertEqual(len(manifest_sources), 1)
                self.assertEqual(
                    manifest_sources[0]["content_hash"],
                    manifest.manifest_sha256,
                )
                self.assertEqual(
                    manifest_sources[0]["protocol_version"],
                    calendar.PROTOCOL_VERSION,
                )

    def test_tampered_upstream_logical_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _artifact, _manifest, reference = self._build(root)
            index_bytes, _ = cas.read_blob(reference.content_hash)
            index = json.loads(index_bytes)
            index["upstream_evidence"]["logical_content_sha256"] = "0" * 64
            forged_hash, forged_path = cas.put_blob(_canonical(index))

            with self.assertRaisesRegex(
                _SourceEvidenceError,
                "upstream evidence identity mismatch",
            ):
                _load_dataset(
                    "trading_calendar",
                    {
                        "content_hash": forged_hash,
                        "object_path": str(forged_path),
                    },
                    root,
                )

    def test_consistently_rewritten_partition_and_envelope_still_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _artifact, _manifest, reference = self._build(root)
            index_bytes, _ = cas.read_blob(reference.content_hash)
            index = json.loads(index_bytes)
            partition = index["partitions"][0]
            normalized_bytes, _ = cas.read_blob(partition["content_hash"])
            rows = [json.loads(line) for line in normalized_bytes.splitlines()]
            rows[0]["is_open"] = not rows[0]["is_open"]
            normalized_hash, normalized_path = cas.put_blob(_jsonl(rows))
            partition["content_hash"] = normalized_hash
            partition["object_path"] = str(normalized_path)

            envelope_source = next(
                source
                for source in partition["raw_sources"]
                if source["role"] == "ROWS_ENVELOPE"
            )
            envelope_bytes, _ = cas.read_blob(envelope_source["content_hash"])
            envelope = json.loads(envelope_bytes)
            envelope["rows"] = rows
            rewritten_envelope = _canonical(envelope)
            envelope_hash, envelope_path = cas.put_blob(rewritten_envelope)
            envelope_source.update(
                {
                    "content_hash": envelope_hash,
                    "object_path": str(envelope_path),
                    "byte_count": len(rewritten_envelope),
                }
            )
            forged_hash, forged_path = cas.put_blob(_canonical(index))

            with self.assertRaisesRegex(
                _SourceEvidenceError,
                "partition does not match official manifest",
            ):
                _load_dataset(
                    "trading_calendar",
                    {
                        "content_hash": forged_hash,
                        "object_path": str(forged_path),
                    },
                    root,
                )

    def test_tampered_official_manifest_is_rejected_before_index_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _artifact, manifest, _reference = self._build(root)
            manifest_path = Path(manifest.object_path)
            manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "CAS object hash mismatch",
            ):
                build_official_trading_calendar_quality_index(
                    cas_root=root,
                    manifest_sha256=manifest.manifest_sha256,
                )

    def test_materializes_complete_v2_closure_and_builds_independent_v3_index(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "official"
            target = root / "delisted_history_inputs"
            _cas, artifact, manifest, _reference = self._build(source)

            reference = materialize_official_trading_calendar_quality_index(
                source_cas_root=source,
                target_cas_root=target,
                manifest_sha256=manifest.manifest_sha256,
            )
            self.assertEqual(reference.copied_cas_object_count, 21)
            self.assertTrue(Path(reference.object_path).is_relative_to(target))
            target_cas = calendar.OfficialTradingCalendarCAS(target)
            target_manifest, target_manifest_path = target_cas.read_blob(
                manifest.manifest_sha256
            )
            self.assertTrue(target_manifest_path.is_relative_to(target))
            self.assertEqual(
                len(
                    calendar.OfficialTradingCalendarManifestStore(
                        target_cas
                    ).replay(manifest.manifest_sha256).rows
                ),
                len(artifact.rows),
            )

            index_bytes, _ = target_cas.read_blob(reference.content_hash)
            index = json.loads(index_bytes)
            source_prefix = str(source.resolve())
            self.assertNotIn(source_prefix, index_bytes.decode("utf-8"))
            self.assertEqual(index["row_count"], 4_382)
            self.assertEqual(len(index["partitions"]), 12)
            self.assertEqual(
                index["upstream_evidence"]["object_path"],
                str(target_manifest_path),
            )

            moved_source = root / "official-moved"
            shutil.move(str(source), str(moved_source))
            loaded = _load_dataset(
                "trading_calendar",
                reference.to_source_identity(),
                target,
            )
            self.assertEqual(loaded.row_count, 4_382)
            self.assertEqual(len(loaded.partitions), 12)
            self.assertEqual(
                hashlib.sha256(target_manifest).hexdigest(),
                manifest.manifest_sha256,
            )

    def test_materialization_fails_before_target_replay_when_source_closure_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "official"
            target = root / "delisted_history_inputs"
            cas, artifact, manifest, _reference = self._build(source)
            missing = artifact.raw_sources[0].content_sha256
            _content, missing_path = cas.read_blob(missing)
            missing_path.unlink()

            with self.assertRaisesRegex(
                calendar.OfficialTradingCalendarBlockedError,
                "missing",
            ):
                materialize_official_trading_calendar_quality_index(
                    source_cas_root=source,
                    target_cas_root=target,
                    manifest_sha256=manifest.manifest_sha256,
                )

    def test_materialization_rejects_same_source_and_target_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cas, _artifact, manifest, _reference = self._build(root)
            with self.assertRaisesRegex(ValueError, "must differ"):
                materialize_official_trading_calendar_quality_index(
                    source_cas_root=root,
                    target_cas_root=root,
                    manifest_sha256=manifest.manifest_sha256,
                )


if __name__ == "__main__":
    unittest.main()
