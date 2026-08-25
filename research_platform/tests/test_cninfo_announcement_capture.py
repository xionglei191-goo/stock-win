from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from research_platform import cninfo_delisted_disclosures as cninfo
from research_platform import official_trading_calendar as calendar
from research_platform.cninfo_announcement_capture import (
    AuthoritativeAnnouncementScope,
    CninfoAnnouncementCaptureBlockedError,
    CninfoAnnouncementCaptureCoordinator,
    load_authoritative_szse_announcement_scope,
)
from research_platform.historical_security_master import SecurityMasterRecord
from research_platform.tests.test_cninfo_announcement_quality_adapter import (
    _master_bytes,
)
from research_platform.tests.test_cninfo_delisted_disclosures import (
    _PagedSession,
    _Session,
    _announcement_row,
    _pdf,
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


def _scope(*targets: cninfo.FrozenDisclosureTarget) -> AuthoritativeAnnouncementScope:
    payload = {
        "master_snapshot_id": "a" * 64,
        "master_content_sha256": "b" * 64,
        "targets": [item.to_dict() for item in targets],
    }
    return AuthoritativeAnnouncementScope(
        master_snapshot_id="a" * 64,
        master_content_sha256="b" * 64,
        master_row_count=6_041,
        targets=tuple(targets),
        scope_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
    )


class CninfoAnnouncementCaptureTests(unittest.TestCase):
    target = cninfo.FrozenDisclosureTarget(
        canonical_entity_id="CN:SZSE:000511",
        exchange="SZSE",
        code="000511.SZ",
        query_start="2018-01-01",
        query_end="2018-07-17",
    )
    org_id = "gssz0000511"

    def _coordinator(self, directory: str):
        root = Path(directory)
        session = _Session(
            rows=[
                _announcement_row(
                    "1200000001",
                    code="000511",
                    org_id=self.org_id,
                    announcement_time=int(
                        datetime(
                            2018,
                            3,
                            30,
                            tzinfo=timezone.utc,
                        ).timestamp()
                        * 1000
                    ),
                )
            ],
            pdfs={"1200000001": _pdf("Official disclosure")},
            master=_master_bytes("000511", self.org_id),
        )
        mocked_scope = _scope(self.target)
        scope_patch = patch(
            "research_platform.cninfo_announcement_capture."
            "load_authoritative_szse_announcement_scope",
            return_value=mocked_scope,
        )
        scope_patch.start()
        self.addCleanup(scope_patch.stop)
        coordinator = CninfoAnnouncementCaptureCoordinator(
            cas_root=root / "cas",
            checkpoint_root=root / "checkpoints",
            session=session,  # type: ignore[arg-type]
        )
        return coordinator, session

    def test_capture_resumes_without_network_and_checkpoint_cannot_promote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator, session = self._coordinator(directory)
            first = coordinator.capture(codes=[self.target.code])
            self.assertTrue(first.complete)
            self.assertTrue(first.selected_complete)
            self.assertTrue(first.full_authoritative_scope)
            self.assertFalse(first.ready)
            self.assertEqual(first.captured_codes, (self.target.code,))
            first_call_count = len(session.calls)

            second = coordinator.capture(codes=[self.target.code])
            self.assertEqual(second, first)
            self.assertEqual(len(session.calls), first_call_count)

            checkpoint = next(
                (
                    Path(directory)
                    / "checkpoints"
                    / coordinator.scope.master_snapshot_id[:16]
                    / self.target.code
                ).iterdir()
            )
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            value["caller_ready_accepted"] = True
            checkpoint.write_bytes(_canonical(value))
            with self.assertRaisesRegex(
                CninfoAnnouncementCaptureBlockedError,
                "checkpoint identity mismatch",
            ):
                coordinator.progress(codes=[self.target.code])

    def test_capture_resumes_after_page_checkpoint_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [
                _announcement_row(
                    f"{1200001000 + index}",
                    code="000511",
                    org_id=self.org_id,
                )
                for index in range(31)
            ]
            session = _PagedSession(
                rows=rows,
                pdfs={
                    str(row["announcementId"]): _pdf("Official disclosure")
                    for row in rows
                },
                master=_master_bytes("000511", self.org_id),
            )
            with patch(
                "research_platform.cninfo_announcement_capture."
                "load_authoritative_szse_announcement_scope",
                return_value=_scope(self.target),
            ):
                coordinator = CninfoAnnouncementCaptureCoordinator(
                    cas_root=root / "cas",
                    checkpoint_root=root / "checkpoints",
                    session=session,  # type: ignore[arg-type]
                )
            real_write = coordinator._write_page_checkpoint
            page_writes = 0

            def fail_once(*args: object, **kwargs: object) -> None:
                real_write(*args, **kwargs)  # type: ignore[arg-type]
                nonlocal page_writes
                page_writes += 1
                if page_writes == 2:
                    raise RuntimeError("simulated page checkpoint interruption")

            with patch.object(
                coordinator,
                "_write_page_checkpoint",
                side_effect=fail_once,
            ):
                with self.assertRaisesRegex(RuntimeError, "page checkpoint"):
                    coordinator.capture(codes=[self.target.code])
            calls_after_interrupt = len(session.calls)
            self.assertEqual(
                sum(method == "GET" and "szse_stock" in url for method, url, _ in session.calls),
                1,
            )
            result = coordinator.capture(codes=[self.target.code])
            self.assertTrue(result.complete)
            new_calls = session.calls[calls_after_interrupt:]
            self.assertFalse(
                any(method == "GET" and "szse_stock" in url for method, url, _ in new_calls)
            )
            self.assertFalse(any(method == "POST" for method, _url, _ in new_calls))
            self.assertEqual(sum(method == "GET" for method, _url, _ in new_calls), 31)

    def test_capture_resumes_each_pdf_without_redownload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [
                _announcement_row(
                    f"{1200000001 + index}",
                    code="000511",
                    org_id=self.org_id,
                )
                for index in range(2)
            ]
            session = _Session(
                rows=rows,
                pdfs={str(row["announcementId"]): _pdf("Official disclosure") for row in rows},
                master=_master_bytes("000511", self.org_id),
            )
            with patch(
                "research_platform.cninfo_announcement_capture."
                "load_authoritative_szse_announcement_scope",
                return_value=_scope(self.target),
            ):
                coordinator = CninfoAnnouncementCaptureCoordinator(
                    cas_root=root / "cas",
                    checkpoint_root=root / "checkpoints",
                    session=session,  # type: ignore[arg-type]
                )
            real_write = coordinator._write_document_checkpoint
            write_count = 0

            def interrupt_second(*args: object, **kwargs: object) -> None:
                nonlocal write_count
                write_count += 1
                if write_count == 2:
                    raise RuntimeError("simulated PDF checkpoint interruption")
                real_write(*args, **kwargs)  # type: ignore[arg-type]

            with patch.object(
                coordinator,
                "_write_document_checkpoint",
                side_effect=interrupt_second,
            ):
                with self.assertRaisesRegex(RuntimeError, "PDF checkpoint"):
                    coordinator.capture(codes=[self.target.code])
            calls_after_interrupt = len(session.calls)
            result = coordinator.capture(codes=[self.target.code])
            self.assertTrue(result.complete)
            new_calls = session.calls[calls_after_interrupt:]
            self.assertEqual(sum(method == "GET" for method, _url, _ in new_calls), 1)
            self.assertFalse(any(method == "POST" for method, _url, _ in new_calls))

    def test_tampered_resumable_pdf_fails_closed_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator, session = self._coordinator(directory)
            real_write = coordinator._write_document_checkpoint

            def interrupt_after_write(*args: object, **kwargs: object) -> None:
                real_write(*args, **kwargs)  # type: ignore[arg-type]
                raise RuntimeError("interrupt after PDF checkpoint")

            with patch.object(
                coordinator,
                "_write_document_checkpoint",
                side_effect=interrupt_after_write,
            ):
                with self.assertRaisesRegex(RuntimeError, "after PDF"):
                    coordinator.capture(codes=[self.target.code])
            work_root = (
                Path(directory)
                / "checkpoints"
                / coordinator.scope.master_snapshot_id[:16]
                / self.target.code
                / "_work"
            )
            document_checkpoint = next((work_root / "documents").glob("*.json"))
            checkpoint_value = json.loads(document_checkpoint.read_text(encoding="utf-8"))
            object_path = Path(checkpoint_value["document"]["raw"]["object_path"])
            object_path.write_bytes(b"%PDF-tampered")
            call_count = len(session.calls)
            with self.assertRaisesRegex(
                CninfoAnnouncementCaptureBlockedError,
                "document failed cold replay",
            ):
                coordinator.capture(codes=[self.target.code])
            self.assertEqual(len(session.calls), call_count)

    def test_parse_checkpoint_resume_skips_completed_pdf_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [
                _announcement_row(
                    f"{1200000001 + index}",
                    code="000511",
                    org_id=self.org_id,
                )
                for index in range(3)
            ]
            session = _Session(
                rows=rows,
                pdfs={
                    str(row["announcementId"]): _pdf("Official disclosure")
                    for row in rows
                },
                master=_master_bytes("000511", self.org_id),
            )
            with patch(
                "research_platform.cninfo_announcement_capture."
                "load_authoritative_szse_announcement_scope",
                return_value=_scope(self.target),
            ):
                coordinator = CninfoAnnouncementCaptureCoordinator(
                    cas_root=root / "cas",
                    checkpoint_root=root / "checkpoints",
                    session=session,  # type: ignore[arg-type]
                )
            real_write = coordinator._write_parse_checkpoint
            parse_writes = 0

            def interrupt_second(*args: object, **kwargs: object) -> None:
                nonlocal parse_writes
                real_write(*args, **kwargs)  # type: ignore[arg-type]
                parse_writes += 1
                if parse_writes == 2:
                    raise RuntimeError("simulated parse checkpoint interruption")

            extract_calls = 0
            real_extract = cninfo._extract_pdf_text

            def counted_extract(raw: bytes):
                nonlocal extract_calls
                extract_calls += 1
                return real_extract(raw)

            with patch.object(
                coordinator,
                "_write_parse_checkpoint",
                side_effect=interrupt_second,
            ), patch.object(
                cninfo, "_extract_pdf_text", side_effect=counted_extract
            ):
                with self.assertRaisesRegex(RuntimeError, "parse checkpoint"):
                    coordinator.capture(codes=[self.target.code])
            self.assertEqual(extract_calls, 2)
            progress = coordinator.progress(codes=[self.target.code])
            self.assertEqual(progress.in_progress_codes, (self.target.code,))
            self.assertEqual(progress.planned_page_count, 1)
            self.assertEqual(progress.checkpointed_page_count, 1)
            self.assertEqual(progress.planned_document_count, 3)
            self.assertEqual(progress.checkpointed_document_count, 2)
            self.assertEqual(progress.checkpointed_parse_count, 2)
            serialized = progress.to_dict()
            self.assertEqual(serialized["in_progress_codes"], [self.target.code])
            self.assertNotIn("caller_ready_accepted", serialized)
            extract_calls = 0
            with patch.object(
                cninfo, "_extract_pdf_text", side_effect=counted_extract
            ):
                result = coordinator.capture(codes=[self.target.code])
            self.assertTrue(result.complete)
            self.assertEqual(result.in_progress_codes, ())
            self.assertEqual(result.planned_document_count, 0)
            # One new parse plus one full raw-PDF admission replay.
            self.assertEqual(extract_calls, 4)

    def test_tampered_parse_checkpoint_blocks_current_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _session = self._coordinator(directory)
            real_write = coordinator._write_parse_checkpoint

            def tamper_after_write(*args: object, **kwargs: object) -> None:
                real_write(*args, **kwargs)  # type: ignore[arg-type]
                target = args[0]
                announcement_id = str(kwargs["announcement_id"])
                path = (
                    coordinator._work_directory(target)  # type: ignore[arg-type]
                    / "parses"
                    / f"{announcement_id}.json"
                )
                value = json.loads(path.read_text(encoding="utf-8"))
                value["parse_evidence"]["normalized_text_sha256"] = "0" * 64
                path.write_bytes(_canonical(value))

            with patch.object(
                coordinator,
                "_write_parse_checkpoint",
                side_effect=tamper_after_write,
            ):
                with self.assertRaisesRegex(
                    CninfoAnnouncementCaptureBlockedError,
                    "does not replay from raw bytes",
                ):
                    coordinator.capture(codes=[self.target.code])
            self.assertFalse(
                (coordinator._checkpoint_directory(self.target) / "current.json").exists()
            )

    def test_tampered_work_plan_cannot_reinterpret_child_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator, session = self._coordinator(directory)
            real_write = coordinator._write_document_checkpoint

            def interrupt_after_write(*args: object, **kwargs: object) -> None:
                real_write(*args, **kwargs)  # type: ignore[arg-type]
                raise RuntimeError("interrupt after child checkpoint")

            with patch.object(
                coordinator,
                "_write_document_checkpoint",
                side_effect=interrupt_after_write,
            ):
                with self.assertRaisesRegex(RuntimeError, "child checkpoint"):
                    coordinator.capture(codes=[self.target.code])
            plan_path = (
                Path(directory)
                / "checkpoints"
                / coordinator.scope.master_snapshot_id[:16]
                / self.target.code
                / "_work"
                / "plan.json"
            )
            value = json.loads(plan_path.read_text(encoding="utf-8"))
            value["stock_master"]["retrieved_at"] = "2026-08-12T00:00:00+00:00"
            plan_path.write_bytes(_canonical(value))
            call_count = len(session.calls)
            with self.assertRaisesRegex(
                CninfoAnnouncementCaptureBlockedError,
                "page-checkpoint identity mismatch",
            ):
                coordinator.capture(codes=[self.target.code])
            self.assertEqual(len(session.calls), call_count)

    def test_uncheckpointed_orphan_pdf_is_never_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator, session = self._coordinator(directory)
            orphan_hash, _ = coordinator.cas.put_blob(_pdf("Orphan object"))
            coordinator.capture(codes=[self.target.code])
            self.assertEqual(
                sum(method == "GET" and url.endswith("1200000001.PDF") for method, url, _ in session.calls),
                1,
            )
            checkpoint = next(
                (
                    Path(directory)
                    / "checkpoints"
                    / coordinator.scope.master_snapshot_id[:16]
                    / self.target.code
                    / "_work"
                    / "documents"
                ).glob("*.json")
            )
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertNotEqual(value["document"]["raw"]["content_hash"], orphan_hash)

    def test_materialization_merges_raw_evidence_and_cold_replays_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _session = self._coordinator(directory)
            coordinator.capture(codes=[self.target.code])
            target_root = Path(directory) / "input-cas"
            calendar_cas = calendar.OfficialTradingCalendarCAS(
                target_root
            )
            calendar_artifact = calendar.OfficialTradingCalendarClient(
                cas=calendar_cas,
                session=_fixture_session(),
                clock=lambda: FIXED_NOW,
            ).fetch()
            calendar_manifest = calendar.OfficialTradingCalendarManifestStore(
                calendar_cas
            ).seal(calendar_artifact)

            result = coordinator.materialize_quality_index(
                calendar_manifest_sha256=calendar_manifest.manifest_sha256,
                target_cas_root=target_root,
            )
            self.assertTrue(result.full_authoritative_scope)
            self.assertFalse(result.ready)
            self.assertEqual(result.selected_target_count, 1)
            self.assertEqual(result.quality_index.row_count, 1)
            index_bytes, index_path = cninfo.CninfoDisclosureCAS(
                target_root
            ).read_blob(
                result.quality_index.content_hash
            )
            self.assertEqual(index_path, Path(result.quality_index.object_path))
            self.assertEqual(result.copied_raw_object_count, 3)
            serialized = index_bytes.decode("utf-8")
            self.assertNotIn('"financial_reports"', serialized)
            self.assertNotIn('"earnings_guidance_express"', serialized)
            self.assertIn('"caller_ready_accepted":false', serialized)

    def test_partial_selection_cannot_claim_full_scope(self) -> None:
        second = cninfo.FrozenDisclosureTarget(
            canonical_entity_id="CN:SZSE:000979",
            exchange="SZSE",
            code="000979.SZ",
            query_start="2018-01-01",
            query_end="2018-12-27",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mocked_scope = _scope(self.target, second)
            with patch(
                "research_platform.cninfo_announcement_capture."
                "load_authoritative_szse_announcement_scope",
                return_value=mocked_scope,
            ):
                coordinator = CninfoAnnouncementCaptureCoordinator(
                    cas_root=root / "cas",
                    checkpoint_root=root / "checkpoints",
                    session=_Session(
                        rows=[],
                        pdfs={},
                        master=_master_bytes("000511", self.org_id),
                    ),  # type: ignore[arg-type]
                )
            with self.assertRaisesRegex(
                CninfoAnnouncementCaptureBlockedError,
                "partial target selection",
            ):
                coordinator.materialize_quality_index(
                    calendar_manifest_sha256="c" * 64,
                    codes=[self.target.code],
                )

    def test_sealed_manifest_can_be_cold_admitted_after_checkpoint_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coordinator, _session = self._coordinator(directory)
            artifact = coordinator.client.fetch(
                master_snapshot_id=coordinator.scope.master_snapshot_id,
                targets=[self.target],
            )
            reference = coordinator.manifest_store.seal(artifact)
            result = coordinator.admit_existing_target_manifest(
                code=self.target.code,
                manifest_sha256=reference.manifest_sha256,
            )
            self.assertTrue(result.complete)
            self.assertFalse(result.ready)
            checkpoint = (
                Path(directory)
                / "checkpoints"
                / coordinator.scope.master_snapshot_id[:16]
                / self.target.code
                / "current.json"
            )
            self.assertTrue(checkpoint.exists())
            self.assertLess(len(str(checkpoint.absolute())), 220)

    def test_selected_progress_cannot_impersonate_authoritative_completion(self) -> None:
        second = cninfo.FrozenDisclosureTarget(
            canonical_entity_id="CN:SZSE:000979",
            exchange="SZSE",
            code="000979.SZ",
            query_start="2018-01-01",
            query_end="2018-12-27",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mocked_scope = _scope(self.target, second)
            with patch(
                "research_platform.cninfo_announcement_capture."
                "load_authoritative_szse_announcement_scope",
                return_value=mocked_scope,
            ):
                coordinator = CninfoAnnouncementCaptureCoordinator(
                    cas_root=root / "cas",
                    checkpoint_root=root / "checkpoints",
                    session=_Session(
                        rows=[
                            _announcement_row(
                                "1200000001",
                                code="000511",
                                org_id=self.org_id,
                            )
                        ],
                        pdfs={"1200000001": _pdf("Official disclosure")},
                        master=_master_bytes("000511", self.org_id),
                    ),  # type: ignore[arg-type]
                )
            progress = coordinator.capture(codes=[self.target.code])
            self.assertTrue(progress.selected_complete)
            self.assertFalse(progress.full_authoritative_scope)
            self.assertFalse(progress.complete)
            self.assertFalse(progress.ready)

    def test_authoritative_loader_derives_exact_frozen_target_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                SecurityMasterRecord(
                    canonical_entity_id=f"CN:SZSE:{index:06d}",
                    exchange="SZSE",
                    code_alias=f"{index:06d}.SZ",
                    board="MAIN",
                    listed_at="2010-01-01",
                    delisted_at="2021-01-05",
                    valid_from="2010-01-01",
                    valid_to="2021-01-05",
                    event_type="TERMINATED_LISTING",
                    source_url="https://www.szse.cn/",
                    source_hash="d" * 64,
                    retrieved_at="2026-08-13T12:00:00+08:00",
                    name=f"Fixture {index}",
                    attributes={},
                )
                for index in range(1, 141)
            ]
            raw = b"\n".join(_canonical(record.to_dict()) for record in records) + b"\n"
            digest = hashlib.sha256(raw).hexdigest()
            object_path = root / "objects" / digest[:2] / digest
            object_path.parent.mkdir(parents=True)
            object_path.write_bytes(raw)
            release = {
                "snapshot_id": "a" * 64,
                "manifest": {
                    "artifacts": {
                        "security_master_jsonl": {
                            "content_hash": digest,
                            "object_path": str(object_path),
                            "row_count": 140,
                        }
                    }
                },
            }
            with patch(
                "research_platform.cninfo_announcement_capture."
                "HistoricalSecurityMasterStore.load_current_release",
                return_value=release,
            ):
                scope = load_authoritative_szse_announcement_scope(
                    master_store_root=root,
                    expected_snapshot_id="a" * 64,
                )
            self.assertEqual(len(scope.targets), 140)
            self.assertEqual(scope.targets[0].query_start, "2018-01-01")
            self.assertEqual(scope.targets[0].query_end, "2021-01-04")
            self.assertEqual(scope.master_content_sha256, digest)


if __name__ == "__main__":
    unittest.main()
