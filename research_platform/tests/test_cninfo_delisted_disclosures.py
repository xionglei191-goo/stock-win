from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from research_platform.cninfo_delisted_disclosures import (
    CNINFO_ANNOUNCEMENT_URL,
    CNINFO_STOCK_MASTER_URL,
    EFFECTIVE_AT_UNRESOLVED,
    MASTER_BINDING_UNVERIFIED,
    STRUCTURED_VALUES_UNRESOLVED,
    CninfoDelistedDisclosureBlockedError,
    CninfoDelistedDisclosureClient,
    CninfoDelistedDisclosureManifestStore,
    CninfoDisclosureCAS,
    FrozenDisclosureTarget,
)


def _pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font_reference}
            )
        }
    )
    stream = DecodedStreamObject()
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _master_bytes(*, org_id: str = "gssh0600432") -> bytes:
    rows = [
        {
            "category": "A股",
            "code": f"{index:06d}",
            "orgId": f"fixture-{index:06d}",
            "pinyin": "fixture",
            "zwjc": f"Fixture {index}",
        }
        for index in range(999)
    ]
    rows.append(
        {
            "category": "A股",
            "code": "600432",
            "orgId": org_id,
            "pinyin": "fixture",
            "zwjc": "Fixture Delisted",
        }
    )
    return json.dumps({"stockList": rows}, ensure_ascii=False).encode("utf-8")


def _announcement_row(
    announcement_id: str,
    *,
    title: str = "2022 Annual Report",
    org_id: str = "gssh0600432",
    code: str = "600432",
    announcement_time: int | None = None,
) -> dict[str, object]:
    if announcement_time is None:
        announcement_time = int(
            datetime(
                2022, 3, 31, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
            * 1000
        )
    return {
        "adjunctSize": 1,
        "adjunctType": "PDF",
        "adjunctUrl": f"finalpage/2022-03-31/{announcement_id}.PDF",
        "announcementContent": "",
        "announcementId": announcement_id,
        "announcementTime": announcement_time,
        "announcementTitle": title,
        "announcementType": "01030101",
        "announcementTypeName": "Annual Report",
        "associateAnnouncement": None,
        "batchNum": None,
        "columnId": "09020202",
        "id": None,
        "important": None,
        "orgId": org_id,
        "orgName": "Fixture Company",
        "pageColumn": "SZSE",
        "secCode": code,
        "secName": "Fixture",
        "secNameList": None,
        "shortTitle": "Fixture",
        "storageTime": None,
        "tileSecName": "Fixture",
    }


def _page(rows: list[dict[str, object]], *, has_more: bool = False) -> bytes:
    total = len(rows)
    return json.dumps(
        {
            "announcements": rows,
            "categoryList": None,
            "classifiedAnnouncements": None,
            "hasMore": has_more,
            "totalAnnouncement": total,
            "totalRecordNum": total,
            "totalSecurities": 0,
            "totalpages": 0,
        },
        ensure_ascii=False,
    ).encode("utf-8")


class _Response:
    def __init__(
        self,
        content: bytes,
        url: str,
        content_type: str,
        *,
        status_code: int = 200,
    ) -> None:
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class _Session:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]],
        pdfs: dict[str, bytes],
        master: bytes | None = None,
        redirect_pdf: bool = False,
        pdf_type: str = "application/pdf",
        force_has_more: bool | None = None,
    ) -> None:
        self.rows = rows
        self.pdfs = pdfs
        self.master = master or _master_bytes()
        self.redirect_pdf = redirect_pdf
        self.pdf_type = pdf_type
        self.force_has_more = force_has_more
        self.calls: list[tuple[str, str, bool]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("GET", url, bool(kwargs.get("allow_redirects"))))
        if url == CNINFO_STOCK_MASTER_URL:
            return _Response(self.master, url, "application/json")
        announcement_id = Path(url).stem
        response_url = f"https://evil.example/{announcement_id}.PDF" if self.redirect_pdf else url
        return _Response(
            self.pdfs[announcement_id], response_url, self.pdf_type
        )

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("POST", url, bool(kwargs.get("allow_redirects"))))
        self.last_post_data = dict(kwargs["data"])  # type: ignore[arg-type]
        has_more = False if self.force_has_more is None else self.force_has_more
        return _Response(_page(self.rows, has_more=has_more), url, "application/json")


class _PagedSession(_Session):
    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("POST", url, bool(kwargs.get("allow_redirects"))))
        self.last_post_data = dict(kwargs["data"])  # type: ignore[arg-type]
        page_num = int(self.last_post_data["pageNum"])
        start = (page_num - 1) * 30
        page_rows = self.rows[start : start + 30]
        page_count = max(1, (len(self.rows) + 29) // 30)
        payload = {
            "announcements": page_rows,
            "categoryList": None,
            "classifiedAnnouncements": None,
            "hasMore": page_num < page_count,
            "totalAnnouncement": len(self.rows),
            "totalRecordNum": len(self.rows),
            "totalSecurities": 0,
            "totalpages": page_count - 1,
        }
        return _Response(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            url,
            "application/json",
        )


class CninfoDelistedDisclosureTests(unittest.TestCase):
    target = FrozenDisclosureTarget(
        canonical_entity_id="fixture-600432",
        exchange="SSE",
        code="600432.SH",
        query_start="2018-01-01",
        query_end="2023-12-31",
    )
    snapshot_id = "a" * 64
    observed_at = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    def _build(
        self,
        directory: str,
        *,
        rows: list[dict[str, object]] | None = None,
        pdfs: dict[str, bytes] | None = None,
        session: _Session | None = None,
    ):
        rows = rows or [_announcement_row("1200000001")]
        pdfs = pdfs or {"1200000001": _pdf("2022 Annual Report")}
        observed_session = session or _Session(rows=rows, pdfs=pdfs)
        cas = CninfoDisclosureCAS(Path(directory) / "cas")
        client = CninfoDelistedDisclosureClient(
            cas=cas,
            session=observed_session,  # type: ignore[arg-type]
            clock=lambda: self.observed_at,
        )
        artifact = client.fetch(
            master_snapshot_id=self.snapshot_id,
            targets=[self.target],
        )
        return artifact, cas, observed_session

    def test_fetch_seal_and_cold_replay_from_every_raw_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, session = self._build(directory)
            self.assertEqual(len(artifact.normalized_announcements), 1)
            announcement = artifact.normalized_announcements[0]
            self.assertEqual(announcement["publication_precision"], "DATE_ONLY")
            self.assertIsNone(announcement["effective_at"])
            self.assertEqual(
                announcement["effective_at_status"], EFFECTIVE_AT_UNRESOLVED
            )
            self.assertEqual(len(artifact.classification_candidates), 1)
            candidate = artifact.classification_candidates[0]
            self.assertEqual(candidate["dataset"], "financial_reports")
            self.assertEqual(candidate["candidate_type"], "ANNUAL")
            self.assertEqual(
                candidate["structured_values_status"], STRUCTURED_VALUES_UNRESOLVED
            )
            self.assertFalse(candidate["quality_row_emitted"])
            self.assertEqual(
                artifact.source_contract["status"], MASTER_BINDING_UNVERIFIED
            )
            self.assertFalse(artifact.dataset_gates["announcement_documents"]["ready"])
            self.assertEqual(
                artifact.statistics["financial_report_rows_emitted"], 0
            )
            self.assertTrue(all(not allow for _method, _url, allow in session.calls))
            self.assertEqual(session.last_post_data["pageSize"], "30")
            self.assertEqual(session.last_post_data["column"], "szse")

            store = CninfoDelistedDisclosureManifestStore(cas)
            reference = store.seal(artifact)
            replayed = store.replay(reference.manifest_sha256)
            self.assertEqual(replayed.to_dict(), artifact.to_dict())

            source_hashes = {
                artifact.stock_master.content_hash,
                *[item["raw"]["content_hash"] for item in artifact.query_pages],
                *[item["raw"]["content_hash"] for item in artifact.documents],
            }
            self.assertEqual(len(source_hashes), 3)
            for digest in source_hashes:
                raw, path = cas.read_blob(str(digest))
                self.assertTrue(raw)
                self.assertEqual(path.name, digest)

    def test_precise_timestamp_may_use_source_time_but_is_still_not_promoted(self) -> None:
        precise = int(
            datetime(2022, 3, 31, 2, 30, tzinfo=timezone.utc).timestamp() * 1000
        )
        rows = [_announcement_row("1200000002", announcement_time=precise)]
        pdfs = {"1200000002": _pdf("2022 Annual Report")}
        with tempfile.TemporaryDirectory() as directory:
            artifact, _cas, _session = self._build(
                directory, rows=rows, pdfs=pdfs
            )
        announcement = artifact.normalized_announcements[0]
        self.assertEqual(announcement["publication_precision"], "TIMESTAMP")
        self.assertEqual(announcement["published_at"], "2022-03-31T10:30:00+08:00")
        self.assertEqual(announcement["effective_at"], announcement["published_at"])
        self.assertFalse(artifact.dataset_gates["announcement_documents"]["ready"])

    def test_title_without_matching_pdf_text_is_not_a_financial_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, _cas, _session = self._build(
                directory,
                pdfs={"1200000001": _pdf("Unrelated official disclosure")},
            )
        self.assertEqual(artifact.classification_candidates, ())
        self.assertEqual(
            artifact.dataset_gates["financial_reports"]["structured_values_emitted"],
            0,
        )

    def test_guidance_is_only_a_candidate_without_forecast_values(self) -> None:
        rows = [
            _announcement_row(
                "1200000003", title="2022 Annual Earnings Forecast"
            )
        ]
        pdfs = {"1200000003": _pdf("2022 Annual Earnings Forecast")}
        with tempfile.TemporaryDirectory() as directory:
            artifact, _cas, _session = self._build(
                directory, rows=rows, pdfs=pdfs
            )
        self.assertEqual(len(artifact.classification_candidates), 1)
        candidate = artifact.classification_candidates[0]
        self.assertEqual(candidate["dataset"], "earnings_guidance_express")
        self.assertEqual(candidate["candidate_type"], "GUIDANCE")
        self.assertEqual(candidate["period_end_candidate"], "2022-12-31")
        self.assertEqual(
            artifact.statistics["earnings_guidance_express_rows_emitted"], 0
        )

    def test_code_org_id_mismatch_fails_closed(self) -> None:
        rows = [_announcement_row("1200000001", org_id="wrong-org")]
        session = _Session(
            rows=rows, pdfs={"1200000001": _pdf("2022 Annual Report")}
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                CninfoDelistedDisclosureBlockedError, "code/orgId"
            ):
                self._build(directory, rows=rows, session=session)

    def test_duplicate_announcement_id_fails_closed_before_pdf_fetch(self) -> None:
        rows = [
            _announcement_row("1200000001"),
            _announcement_row("1200000001"),
        ]
        session = _Session(
            rows=rows, pdfs={"1200000001": _pdf("2022 Annual Report")}
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                CninfoDelistedDisclosureBlockedError, "duplicate announcementId"
            ):
                self._build(directory, rows=rows, session=session)

    def test_pagination_has_more_drift_fails_closed(self) -> None:
        rows = [_announcement_row("1200000001")]
        session = _Session(
            rows=rows,
            pdfs={"1200000001": _pdf("2022 Annual Report")},
            force_has_more=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                CninfoDelistedDisclosureBlockedError, "hasMore"
            ):
                self._build(directory, rows=rows, session=session)

    def test_two_page_capture_is_complete_and_replays(self) -> None:
        rows = [
            _announcement_row(
                f"{1200001000 + index}",
                title=f"2022 General Disclosure {index}",
            )
            for index in range(31)
        ]
        pdfs = {
            str(row["announcementId"]): _pdf("Unrelated official disclosure")
            for row in rows
        }
        session = _PagedSession(rows=rows, pdfs=pdfs)
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session = self._build(
                directory, rows=rows, pdfs=pdfs, session=session
            )
            self.assertEqual(len(artifact.query_pages), 2)
            self.assertEqual(len(artifact.documents), 31)
            self.assertEqual(len(artifact.normalized_announcements), 31)
            self.assertEqual(artifact.classification_candidates, ())
            reference = CninfoDelistedDisclosureManifestStore(cas).seal(artifact)
            replayed = CninfoDelistedDisclosureManifestStore(cas).replay(
                reference.manifest_sha256
            )
            self.assertEqual(replayed.logical_content_sha256, artifact.logical_content_sha256)

    def test_pdf_redirect_content_type_and_magic_fail_closed(self) -> None:
        cases = (
            {
                "redirect_pdf": True,
                "pdf_type": "application/pdf",
                "pdf": _pdf("2022 Annual Report"),
                "message": "redirected",
            },
            {
                "redirect_pdf": False,
                "pdf_type": "application/octet-stream",
                "pdf": _pdf("2022 Annual Report"),
                "message": "content type",
            },
            {
                "redirect_pdf": False,
                "pdf_type": "application/pdf",
                "pdf": b"not-a-pdf",
                "message": "magic",
            },
        )
        for case in cases:
            with self.subTest(case=case["message"]), tempfile.TemporaryDirectory() as directory:
                session = _Session(
                    rows=[_announcement_row("1200000001")],
                    pdfs={"1200000001": case["pdf"]},  # type: ignore[dict-item]
                    redirect_pdf=bool(case["redirect_pdf"]),
                    pdf_type=str(case["pdf_type"]),
                )
                with self.assertRaisesRegex(
                    CninfoDelistedDisclosureBlockedError, str(case["message"])
                ):
                    self._build(directory, session=session)

    def test_caller_ready_field_cannot_self_certify_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session = self._build(directory)
            store = CninfoDelistedDisclosureManifestStore(cas)
            reference = store.seal(artifact)
            raw, _path = cas.read_blob(reference.manifest_sha256)
            payload = json.loads(raw.decode("utf-8"))
            payload["ready"] = True
            forged = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            forged_hash, _ = cas.put_blob(forged)
            with self.assertRaisesRegex(
                CninfoDelistedDisclosureBlockedError, "manifest schema drift"
            ):
                store.replay(forged_hash)

    def test_manifest_replay_detects_corrupted_raw_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session = self._build(directory)
            store = CninfoDelistedDisclosureManifestStore(cas)
            reference = store.seal(artifact)
            pdf_path = Path(artifact.documents[0]["raw"]["object_path"])
            pdf_path.write_bytes(b"%PDF-corrupted")
            with self.assertRaisesRegex(
                CninfoDelistedDisclosureBlockedError, "hash mismatch"
            ):
                store.replay(reference.manifest_sha256)

    def test_manifest_replay_reextracts_every_original_pdf(self) -> None:
        rows = [
            _announcement_row(f"{1200000100 + index}")
            for index in range(3)
        ]
        pdfs = {
            str(row["announcementId"]): _pdf("Official disclosure")
            for row in rows
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session = self._build(
                directory,
                rows=rows,
                pdfs=pdfs,
            )
            store = CninfoDelistedDisclosureManifestStore(cas)
            reference = store.seal(artifact)
            real_extract = (
                __import__(
                    "research_platform.cninfo_delisted_disclosures",
                    fromlist=["_extract_pdf_text"],
                )._extract_pdf_text
            )
            extract_calls = 0

            def counted_extract(raw: bytes):
                nonlocal extract_calls
                extract_calls += 1
                return real_extract(raw)

            with patch(
                "research_platform.cninfo_delisted_disclosures._extract_pdf_text",
                side_effect=counted_extract,
            ):
                replayed = store.replay(reference.manifest_sha256)
            self.assertEqual(extract_calls, len(rows))
            self.assertEqual(replayed.to_dict(), artifact.to_dict())

    def test_reparse_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cas"
            root.mkdir()
            real_lstat = os.lstat

            def marked(path: object) -> object:
                observed = real_lstat(path)
                if Path(path) == root:
                    return SimpleNamespace(
                        st_mode=observed.st_mode,
                        st_file_attributes=0x00000400,
                    )
                return observed

            with patch(
                "research_platform.cninfo_delisted_disclosures.os.lstat",
                side_effect=marked,
            ):
                with self.assertRaisesRegex(
                    CninfoDelistedDisclosureBlockedError, "reparse"
                ):
                    CninfoDisclosureCAS(root)


if __name__ == "__main__":
    unittest.main()
