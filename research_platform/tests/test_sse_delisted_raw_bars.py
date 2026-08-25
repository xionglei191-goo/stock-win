from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from unittest.mock import patch

from research_platform.delisted_history_quality import (
    _SourceEvidenceError,
    _load_dataset,
)
from research_platform.official_historical_bars import (
    OfficialHistoricalBarsBlockedError,
    OfficialHistoricalBarsClient,
)
from research_platform.sse_delisted_raw_bars import (
    CUTOFF_CAPTURE_CONTRACT_UNADMITTED,
    EXPECTED_CURRENT_SSE_TARGET_COUNT,
    PARTIAL_SOURCE_STATUS,
    SSEDelistedRawBarsCAS,
    SSEDelistedRawBarsManifestStore,
    _load_current_sse_delisted_targets,
    assess_sse_dayk_cutoff_capture,
    build_sse_delisted_raw_bars_quality_index,
    capture_current_sse_delisted_raw_bars,
    materialize_sse_delisted_raw_bars_quality_index,
    require_sse_dayk_cutoff_capture_contract,
)


RETRIEVED_AT = "2026-08-13T12:00:00+08:00"
SOURCE_URL = "https://yunhq.sse.com.cn:32042/v1/sh1/dayk/600432"


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.url = SOURCE_URL
        self.status_code = 200
        self.headers = {"Content-Type": "application/javascript"}


class _Session:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = [_Response(item) for item in responses]

    def get(self, url: str, **kwargs: Any) -> _Response:
        if not self.responses:
            raise AssertionError("unexpected network call")
        response = self.responses.pop(0)
        response.url = f"{url}?{urlencode(kwargs['params'])}"
        return response


class _FailingSession:
    def get(self, url: str, **kwargs: Any) -> _Response:
        raise OSError("fixture connection failed")


class _NoNetworkSession:
    def get(self, url: str, **kwargs: Any) -> _Response:
        raise AssertionError("resumed capture unexpectedly used the network")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    return b"\n".join(_canonical(item) for item in rows) + b"\n"


def _row(day: str, close: str) -> list[str]:
    return [day, "10", "11", "9", close, "1000", "10000"]


def _raw(rows: list[list[str]], *, begin: int, end: int, total: int = 4) -> bytes:
    payload = {
        "code": "600432",
        "total": total,
        "begin": begin,
        "end": end,
        "kline": rows,
    }
    return (
        "jsonpCallback("
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ");"
    ).encode("utf-8")


def _fixture_pages() -> tuple[bytes, bytes]:
    return (
        _raw(
            [_row("20180102", "10.1"), _row("20200102", "10.2")],
            begin=0,
            end=2,
        ),
        _raw(
            [_row("20220104", "10.3"), _row("20231229", "10.4")],
            begin=2,
            end=4,
        ),
    )


def _bulk_master_identity() -> dict[str, Any]:
    target_codes = ("600432.SH",) + tuple(
        f"{code:06d}.SH" for code in range(610000, 610098)
    )
    eligible_codes = ("600432.SH",)
    deferred_codes = target_codes[1:]
    return {
        "snapshot_id": "a" * 64,
        "security_master_content_hash": "b" * 64,
        "target_codes": target_codes,
        "target_codes_sha256": hashlib.sha256(_canonical(list(target_codes))).hexdigest(),
        "eligible_codes": eligible_codes,
        "eligible_codes_sha256": hashlib.sha256(
            _canonical(list(eligible_codes))
        ).hexdigest(),
        "deferred_codes": deferred_codes,
        "deferred_codes_sha256": hashlib.sha256(
            _canonical(list(deferred_codes))
        ).hexdigest(),
    }


class SSEDelistedRawBarsTests(unittest.TestCase):
    def _build(self, root: Path):
        pages = _fixture_pages()
        cas = SSEDelistedRawBarsCAS(root)
        artifact = OfficialHistoricalBarsClient(
            session=_Session(list(pages)),  # type: ignore[arg-type]
            cas=cas,  # type: ignore[arg-type]
        ).fetch_sse(
            "600432.SH",
            page_size=2,
            retrieved_at=RETRIEVED_AT,
            expected_page_hashes={
                "0:2": hashlib.sha256(pages[0]).hexdigest(),
                "2:4": hashlib.sha256(pages[1]).hexdigest(),
            },
        )
        manifest = SSEDelistedRawBarsManifestStore(cas).seal(artifact)
        reference = build_sse_delisted_raw_bars_quality_index(
            cas_root=root,
            manifest_sha256s=[manifest.manifest_sha256],
        )
        return cas, artifact, manifest, reference

    def test_post_cutoff_dayk_capture_is_unadmitted_before_network(self) -> None:
        assessment = assess_sse_dayk_cutoff_capture(cutoff_date="2023-12-31")

        self.assertFalse(assessment.safe)
        self.assertTrue(assessment.promotion_blocked)
        self.assertFalse(assessment.server_side_date_bound_admitted)
        self.assertFalse(assessment.metadata_only_boundary_probe_admitted)
        self.assertFalse(assessment.zero_post_cutoff_response_rows_guaranteed)
        self.assertEqual(assessment.pagination_basis, "ROW_POSITION")
        self.assertEqual(
            assessment.admitted_query_parameters,
            ("callback", "select", "begin", "end"),
        )
        self.assertEqual(assessment.status, CUTOFF_CAPTURE_CONTRACT_UNADMITTED)
        with patch(
            "research_platform.sse_delisted_raw_bars.requests.Session.get"
        ) as network_get:
            with self.assertRaisesRegex(
                OfficialHistoricalBarsBlockedError,
                "no admitted server-side date bound",
            ) as raised:
                require_sse_dayk_cutoff_capture_contract(
                    cutoff_date="2023-12-31"
                )
        network_get.assert_not_called()
        self.assertEqual(
            raised.exception.status,
            CUTOFF_CAPTURE_CONTRACT_UNADMITTED,
        )

    def test_cold_replays_official_jsonp_into_sse_year_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, artifact, manifest, reference = self._build(root)

            replayed = SSEDelistedRawBarsManifestStore(cas).replay(
                manifest.manifest_sha256
            )
            loaded = _load_dataset(
                "raw_execution_bars",
                reference.to_source_identity(),
                root,
            )
            index_bytes, _ = cas.read_blob(reference.content_hash)
            index = json.loads(index_bytes)

            self.assertEqual(replayed.logical_content_sha256, artifact.logical_content_sha256)
            self.assertEqual(len(replayed.raw_responses), 2)
            self.assertEqual(loaded.row_count, 4)
            self.assertEqual(len(loaded.partitions), 6)
            self.assertEqual(reference.partition_count, 6)
            self.assertFalse(reference.ready)
            self.assertTrue(reference.promotion_blocked)
            self.assertEqual(reference.status, PARTIAL_SOURCE_STATUS)
            self.assertFalse(index["ready"])
            self.assertFalse(index["complete"])
            self.assertEqual(
                sorted((key[1], item.row_count) for key, item in loaded.partitions.items()),
                [(2018, 1), (2019, 0), (2020, 1), (2021, 0), (2022, 1), (2023, 1)],
            )
            manifest_bytes, _ = cas.read_blob(manifest.manifest_sha256)
            sealed = json.loads(manifest_bytes)
            self.assertEqual(
                sealed["scope"]["allowed_use"],
                "UNADJUSTED_RAW_EXECUTION_BAR_EVIDENCE_ONLY",
            )
            self.assertFalse(sealed["scope"]["szse_coverage"])
            self.assertFalse(sealed["scope"]["label_generation_allowed"])

    def test_raw_jsonp_tamper_breaks_manifest_cold_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _artifact, manifest, _reference = self._build(root)
            raw_path = cas.object_path(manifest.raw_page_hashes[0])
            raw_path.write_bytes(raw_path.read_bytes() + b" ")

            with self.assertRaisesRegex(
                OfficialHistoricalBarsBlockedError, "hash mismatch"
            ):
                SSEDelistedRawBarsManifestStore(cas).replay(
                    manifest.manifest_sha256
                )

    def test_windows_reparse_raw_object_fails_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _artifact, manifest, _reference = self._build(root)
            raw_path = cas.object_path(manifest.raw_page_hashes[0])
            real_lstat = os.lstat

            def marked_reparse(path: Any, *args: Any, **kwargs: Any) -> Any:
                metadata = real_lstat(path, *args, **kwargs)
                if Path(path) == raw_path:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_file_attributes=(
                            int(getattr(metadata, "st_file_attributes", 0)) | 0x400
                        ),
                    )
                return metadata

            with patch(
                "research_platform.sse_delisted_raw_bars.os.lstat",
                side_effect=marked_reparse,
            ):
                with self.assertRaisesRegex(
                    OfficialHistoricalBarsBlockedError,
                    "symlink, junction, or reparse point",
                ):
                    SSEDelistedRawBarsManifestStore(cas).replay(
                        manifest.manifest_sha256
                    )

    def test_parser_contract_and_incomplete_pagination_fail_closed(self) -> None:
        for label, mutate, message in (
            (
                "parser",
                lambda value: value.__setitem__("parser_contract_sha256", "0" * 64),
                "identity mismatch",
            ),
            (
                "pagination",
                lambda value: value.__setitem__("raw_pages", value["raw_pages"][:1]),
                "coverage is incomplete",
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cas, _artifact, manifest, _reference = self._build(root)
                manifest_bytes, _ = cas.read_blob(manifest.manifest_sha256)
                value = json.loads(manifest_bytes)
                mutate(value)
                forged_hash, _ = cas.put_blob(_canonical(value))

                with self.assertRaisesRegex(
                    OfficialHistoricalBarsBlockedError, message
                ):
                    SSEDelistedRawBarsManifestStore(cas).replay(forged_hash)

    def test_consistently_rewritten_partition_and_envelope_still_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _artifact, _manifest, reference = self._build(root)
            index_bytes, _ = cas.read_blob(reference.content_hash)
            index = json.loads(index_bytes)
            partition = next(item for item in index["partitions"] if item["year"] == 2018)
            normalized_bytes, _ = cas.read_blob(partition["content_hash"])
            rows = [json.loads(line) for line in normalized_bytes.splitlines()]
            rows[0]["close"] = 10.9
            normalized_hash, normalized_path = cas.put_blob(_jsonl(rows))
            partition["content_hash"] = normalized_hash
            partition["object_path"] = str(normalized_path)

            envelope_source = next(
                item
                for item in partition["raw_sources"]
                if item["role"] == "ROWS_ENVELOPE"
            )
            envelope_bytes, _ = cas.read_blob(envelope_source["content_hash"])
            envelope = json.loads(envelope_bytes)
            envelope["rows"] = rows
            rewritten = _canonical(envelope)
            envelope_hash, envelope_path = cas.put_blob(rewritten)
            envelope_source.update(
                {
                    "content_hash": envelope_hash,
                    "object_path": str(envelope_path),
                    "byte_count": len(rewritten),
                }
            )
            forged_hash, forged_path = cas.put_blob(_canonical(index))

            with self.assertRaisesRegex(
                _SourceEvidenceError,
                "does not match SSE official manifest",
            ):
                _load_dataset(
                    "raw_execution_bars",
                    {"content_hash": forged_hash, "object_path": str(forged_path)},
                    root,
                )

    def test_special_index_authority_requires_manifest_on_every_partition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _artifact, _manifest, reference = self._build(root)
            index_bytes, _ = cas.read_blob(reference.content_hash)
            index = json.loads(index_bytes)
            index["partitions"][0]["raw_sources"] = [
                item
                for item in index["partitions"][0]["raw_sources"]
                if item["role"] != "SSE_OFFICIAL_DAILY_BARS_MANIFEST"
            ]
            forged_hash, forged_path = cas.put_blob(_canonical(index))

            with self.assertRaisesRegex(
                _SourceEvidenceError, "has no SSE official manifest"
            ):
                _load_dataset(
                    "raw_execution_bars",
                    {"content_hash": forged_hash, "object_path": str(forged_path)},
                    root,
                )

    def test_materializes_complete_closure_and_cold_replays_from_target_alone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "delisted_history_inputs"
            _cas, _artifact, manifest, source_reference = self._build(source)

            reference = materialize_sse_delisted_raw_bars_quality_index(
                source_cas_root=source,
                target_cas_root=target,
                quality_index_sha256=source_reference.content_hash,
            )

            self.assertEqual(
                reference.source_quality_index_sha256,
                source_reference.content_hash,
            )
            self.assertEqual(reference.copied_cas_object_count, 15)
            self.assertTrue(Path(reference.object_path).is_relative_to(target))
            self.assertEqual(reference.codes, ("600432.SH",))
            self.assertEqual(reference.row_count, 4)
            self.assertEqual(reference.partition_count, 6)
            self.assertNotEqual(
                reference.manifest_sha256s,
                (manifest.manifest_sha256,),
            )

            target_cas = SSEDelistedRawBarsCAS(target)
            target_index_bytes, _ = target_cas.read_blob(reference.content_hash)
            self.assertNotIn(str(source.resolve()), target_index_bytes.decode("utf-8"))

            moved_source = root / "source-moved"
            shutil.move(str(source), str(moved_source))
            loaded = _load_dataset(
                "raw_execution_bars",
                reference.to_source_identity(),
                target,
            )
            self.assertEqual(loaded.row_count, 4)
            self.assertEqual(len(loaded.partitions), 6)
            replayed = SSEDelistedRawBarsManifestStore(target_cas).replay(
                reference.manifest_sha256s[0]
            )
            self.assertEqual(replayed.code, "600432.SH")

    def test_materialization_ignores_forged_source_object_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            cas, _artifact, _manifest, source_reference = self._build(source)
            index_bytes, _ = cas.read_blob(source_reference.content_hash)
            index = json.loads(index_bytes)
            for partition in index["partitions"]:
                partition["object_path"] = "Z:\\untrusted\\partition"
                for raw_source in partition["raw_sources"]:
                    raw_source["object_path"] = "Z:\\untrusted\\raw"
            forged_hash, _ = cas.put_blob(_canonical(index))

            reference = materialize_sse_delisted_raw_bars_quality_index(
                source_cas_root=source,
                target_cas_root=target,
                quality_index_sha256=forged_hash,
            )

            loaded = _load_dataset(
                "raw_execution_bars",
                reference.to_source_identity(),
                target,
            )
            self.assertEqual(loaded.row_count, 4)
            target_index, _ = SSEDelistedRawBarsCAS(target).read_blob(
                reference.content_hash
            )
            self.assertNotIn("untrusted", target_index.decode("utf-8"))

    def test_materialization_fails_when_source_raw_page_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            cas, _artifact, manifest, source_reference = self._build(source)
            cas.object_path(manifest.raw_page_hashes[0]).unlink()

            with self.assertRaisesRegex(
                OfficialHistoricalBarsBlockedError,
                "cannot be opened as a stable file",
            ):
                materialize_sse_delisted_raw_bars_quality_index(
                    source_cas_root=source,
                    target_cas_root=target,
                    quality_index_sha256=source_reference.content_hash,
                )

    def test_materialization_rejects_same_source_and_target_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cas, _artifact, _manifest, source_reference = self._build(root)

            with self.assertRaisesRegex(ValueError, "must differ"):
                materialize_sse_delisted_raw_bars_quality_index(
                    source_cas_root=root,
                    target_cas_root=root,
                    quality_index_sha256=source_reference.content_hash,
                )

    def test_bulk_capture_checkpoints_each_code_and_resumes_without_network(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _bulk_master_identity()
            pages = _fixture_pages()
            with patch(
                "research_platform.sse_delisted_raw_bars._load_current_sse_delisted_targets",
                return_value=master,
            ):
                first = capture_current_sse_delisted_raw_bars(
                    security_master_root=root / "master",
                    cas_root=root / "cas",
                    session=_Session(list(pages)),  # type: ignore[arg-type]
                    page_size=2,
                    request_delay_seconds=0,
                    max_attempts_per_code=1,
                    max_new_captures=1,
                )
                resumed = capture_current_sse_delisted_raw_bars(
                    security_master_root=root / "master",
                    cas_root=root / "cas",
                    session=_NoNetworkSession(),  # type: ignore[arg-type]
                    page_size=2,
                    request_delay_seconds=0,
                    max_attempts_per_code=1,
                    max_new_captures=0,
                )

            self.assertEqual(len(first.target_codes), EXPECTED_CURRENT_SSE_TARGET_COUNT)
            self.assertEqual(first.eligible_codes, ("600432.SH",))
            self.assertEqual(first.deferred_codes, master["deferred_codes"])
            self.assertEqual(first.captured_codes, ("600432.SH",))
            self.assertTrue(first.eligible_capture_complete)
            self.assertFalse(first.complete)
            self.assertEqual(
                first.deferred_capture_assessment.status,
                CUTOFF_CAPTURE_CONTRACT_UNADMITTED,
            )
            self.assertTrue(first.quality_index_sha256)
            self.assertEqual(resumed.resumed_codes, ("600432.SH",))
            self.assertEqual(resumed.captured_codes, ())
            self.assertEqual(resumed.manifest_sha256s, first.manifest_sha256s)
            self.assertTrue(Path(resumed.checkpoint_pointer_path).is_file())

    def test_bulk_capture_records_failure_and_retries_it_on_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _bulk_master_identity()
            with patch(
                "research_platform.sse_delisted_raw_bars._load_current_sse_delisted_targets",
                return_value=master,
            ):
                failed = capture_current_sse_delisted_raw_bars(
                    security_master_root=root / "master",
                    cas_root=root / "cas",
                    session=_FailingSession(),  # type: ignore[arg-type]
                    page_size=2,
                    request_delay_seconds=0,
                    max_attempts_per_code=1,
                    max_new_captures=1,
                )
                recovered = capture_current_sse_delisted_raw_bars(
                    security_master_root=root / "master",
                    cas_root=root / "cas",
                    session=_Session(list(_fixture_pages())),  # type: ignore[arg-type]
                    page_size=2,
                    request_delay_seconds=0,
                    max_attempts_per_code=1,
                    max_new_captures=1,
                )

            self.assertEqual(failed.failed_codes, ("600432.SH",))
            self.assertEqual(recovered.captured_codes, ("600432.SH",))
            self.assertEqual(recovered.failed_codes, ())

    def test_bulk_capture_rejects_master_target_count_drift_before_network(self) -> None:
        master = _bulk_master_identity()
        master["target_codes"] = master["target_codes"][:-1]
        with tempfile.TemporaryDirectory() as directory, patch(
            "research_platform.sse_delisted_raw_bars._load_current_sse_delisted_targets",
            return_value=master,
        ):
            with self.assertRaisesRegex(
                OfficialHistoricalBarsBlockedError, "target count changed"
            ):
                capture_current_sse_delisted_raw_bars(
                    security_master_root=Path(directory) / "master",
                    cas_root=Path(directory) / "cas",
                    session=_NoNetworkSession(),  # type: ignore[arg-type]
                    request_delay_seconds=0,
                )

    def test_bulk_capture_never_requests_post_audit_deferred_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _bulk_master_identity()
            with patch(
                "research_platform.sse_delisted_raw_bars._load_current_sse_delisted_targets",
                return_value=master,
            ):
                result = capture_current_sse_delisted_raw_bars(
                    security_master_root=root / "master",
                    cas_root=root / "cas",
                    session=_Session(list(_fixture_pages())),  # type: ignore[arg-type]
                    page_size=2,
                    request_delay_seconds=0,
                    max_attempts_per_code=1,
                )

            self.assertTrue(result.eligible_capture_complete)
            self.assertFalse(result.complete)
            self.assertEqual(result.captured_codes, ("600432.SH",))
            self.assertEqual(result.deferred_codes, master["deferred_codes"])

    def test_current_published_master_resolves_frozen_99_sse_targets(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        current_pointer = repository_root / "data" / "security_master" / "current.json"
        if not current_pointer.exists():
            self.skipTest("repository has no published security master")
        identity = _load_current_sse_delisted_targets(
            repository_root / "data" / "security_master"
        )

        self.assertEqual(len(identity["target_codes"]), EXPECTED_CURRENT_SSE_TARGET_COUNT)
        self.assertEqual(len(identity["eligible_codes"]), 56)
        self.assertEqual(len(identity["deferred_codes"]), 43)
        self.assertEqual(identity["snapshot_id"], "1ce6cc99a95e88b243bee74fd5e30638d33577aeb07b700be32891887591fe37")
        self.assertIn("600432.SH", identity["target_codes"])
        self.assertIn("688086.SH", identity["target_codes"])
        self.assertEqual(
            set(identity["eligible_codes"]) | set(identity["deferred_codes"]),
            set(identity["target_codes"]),
        )
        self.assertFalse(
            set(identity["eligible_codes"]) & set(identity["deferred_codes"])
        )

    def test_adapter_rejects_non_sse_artifact_without_claiming_szse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas, _artifact, manifest, _reference = self._build(root)
            manifest_bytes, _ = cas.read_blob(manifest.manifest_sha256)
            value = json.loads(manifest_bytes)
            value["exchange"] = "SZSE"
            forged_hash, _ = cas.put_blob(_canonical(value))

            with self.assertRaisesRegex(
                OfficialHistoricalBarsBlockedError, "identity mismatch"
            ):
                build_sse_delisted_raw_bars_quality_index(
                    cas_root=root,
                    manifest_sha256s=[forged_hash],
                )


if __name__ == "__main__":
    unittest.main()
