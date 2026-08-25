from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

import research_platform.pending_listing_source as pending


FIXED_NOW = datetime(2026, 8, 13, 9, 30, tzinfo=timezone(timedelta(hours=8)))
REAL_RELEASE_MANIFEST_SHA256 = (
    "8878c2be2e26ca534364311a3c86717d15c176bfcf8a3deeabf9771e3b2e9765"
)
REAL_RELEASE_LOGICAL_SHA256 = (
    "81c2f4252c0d49591309b0a6b03cb8036a92de72b2a075ef284222b18212ed90"
)


class _Response:
    def __init__(
        self,
        url: str,
        content: bytes,
        content_type: str,
        *,
        status_code: int = 200,
        response_url: str | None = None,
    ) -> None:
        self.url = response_url or url
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class _Session:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        if url not in self.responses:
            raise AssertionError(f"unexpected URL: {url}")
        return self.responses[url]


def _sse_payload(
    spec: pending.SSEPendingSpec,
    *,
    listed_date: str = "-",
    status: str = "0",
    total: int = 1,
) -> bytes:
    row = {field: "-" for field in pending.SSE_ROW_FIELDS}
    row.update(
        {
            "COMPANY_FULL_NAME": spec.company_full_name,
            "SECURITY_CODE": spec.code,
            "SECURITY_EXPAND_NAME": spec.name,
            "SECURITY_NAME": spec.name,
            "IPO_OVERALL_STATUS": status,
            "LISTED_DATE": listed_date,
            "STOCK_TYPE": "2",
            "NUM": "1",
            "ONLINE_ISSUANCE_DATE": "2026-08-10",
        }
    )
    page = {
        "beginPage": 1,
        "cacheSize": 5,
        "data": [row],
        "endDate": None,
        "endPage": 1,
        "objectResult": None,
        "pageCount": 1,
        "pageNo": 1,
        "pageSize": 25,
        "pageSizeWithOutLimit": 25,
        "searchDate": None,
        "sort": None,
        "startDate": None,
        "total": total,
    }
    value = {
        "actionErrors": [],
        "actionMessages": [],
        "fieldErrors": {},
        "isPagination": "true",
        "jsonCallBack": pending.SSE_JSONP_CALLBACK,
        "locale": "en",
        "pageHelp": page,
        "pageNo": None,
        "pageSize": None,
        "queryDate": "",
        "result": [row],
        "securityCode": "",
        "sqlId": pending.SSE_IPO_SQL_ID,
        "texts": None,
        "type": "",
        "validateCode": "",
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return f"{pending.SSE_JSONP_CALLBACK}({encoded})".encode("utf-8")


def _current_ipo_payload(
    specs: tuple[pending.SZSEPendingDocumentSpec, ...],
    *,
    omit: str | None = None,
    listed: str | None = None,
) -> bytes:
    rows: list[dict[str, object]] = []
    for index, spec in enumerate(specs, start=1):
        if spec.code == omit:
            continue
        row = {field: None for field in pending.CNINFO_CURRENT_IPO_ROW_FIELDS}
        row.update(
            {
                "obSecCode0007": spec.code,
                "obSecName0007": spec.name,
                "f035d0089Date": spec.subscription_date,
                "f035d0089Time": f"{spec.subscription_date} 00:00:00",
                "f001v0116": "013006",
                "f007d0007": listed if spec.code == specs[0].code else None,
                "obSeqId": str(1000 + index),
                "f003n0089": 1.0,
                "f004n0089": 1.0,
                "f043n0089": 1.0,
                "f050n0089": 0.0,
                "f117n0089": 0.0,
            }
        )
        rows.append(row)
    return json.dumps(
        {"code": 200, "message": "执行成功", "data": rows},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _stock_master_payload(
    specs: tuple[pending.SZSEPendingDocumentSpec, ...],
) -> bytes:
    rows = [
        {
            "code": spec.code,
            "pinyin": f"py{index}",
            "category": "A股",
            "orgId": spec.org_id,
            "zwjc": spec.name,
        }
        for index, spec in enumerate(specs, start=1)
    ]
    return json.dumps(
        {"stockList": rows}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _announcement_payload(
    spec: pending.SZSEPendingDocumentSpec,
    *,
    hard_negative: bool = False,
) -> bytes:
    rows = [
        {
            "announcementId": None,
            "stockId": None,
            "title": spec.title_marker,
            "uri": f"finalpage/{spec.publication_date}/{spec.announcement_id}.PDF",
            "announcementDate": spec.publication_date,
            "announcementTime": None,
        }
    ]
    if hard_negative:
        rows.append(
            {
                "announcementId": None,
                "stockId": None,
                "title": "关于中止发行的公告",
                "uri": f"finalpage/{spec.publication_date}/9999999999.PDF",
                "announcementDate": spec.publication_date,
                "announcementTime": None,
            }
        )
    row_count = len(rows)
    page = {
        "endRow": "0",
        "hasNextPage": False,
        "hasPreviousPage": False,
        "isFirstPage": False,
        "isLastPage": False,
        "list": rows,
        "navigateFirstPage": 0,
        "navigateLastPage": 0,
        "navigatePages": 0,
        "navigatepageNums": None,
        "nextPage": 0,
        "pageNum": 0,
        "pageSize": 0,
        "pages": 0,
        "prePage": 0,
        "size": 0,
        "startRow": "0",
        "total": str(row_count),
    }
    return json.dumps(
        {"code": 200, "message": "执行成功", "data": page},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    worksheet_rows: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column, value in enumerate(row):
            number = column + 1
            letters = ""
            while number:
                number, remainder = divmod(number - 1, 26)
                letters = chr(ord("A") + remainder) + letters
            cells.append(
                f'<c r="{letters}{row_number}" t="inlineStr"><is><t>'
                f"{escape(str(value))}</t></is></c>"
            )
        worksheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(worksheet_rows)}</sheetData></worksheet>'
    ).encode("utf-8")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def _active_xlsx(*, target_code: str | None = None) -> bytes:
    row = [""] * len(pending.SZSE_ACTIVE_HEADER)
    row[0] = "创业板"
    row[1] = "示例股份有限公司"
    row[4] = target_code or "300001"
    row[5] = "示例股份"
    row[6] = "40000"
    return _xlsx_bytes([list(pending.SZSE_ACTIVE_HEADER), row])


def _custom_szse_specs() -> tuple[tuple[pending.SZSEPendingDocumentSpec, ...], dict[str, bytes]]:
    raw_documents: dict[str, bytes] = {}
    specs: list[pending.SZSEPendingDocumentSpec] = []
    for original in pending.SZSE_DOCUMENT_SPECS:
        raw = f"%PDF-FAKE-{original.code}".encode("ascii")
        spec = replace(
            original,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
        specs.append(spec)
        raw_documents[spec.source_id] = raw
    return tuple(specs), raw_documents


def _fake_pdf_extract(raw: bytes) -> tuple[str, int, str, str, str]:
    code = raw.decode("ascii").rsplit("-", 1)[-1]
    spec = next(item for item in pending.SZSE_DOCUMENT_SPECS if item.code == code)
    subscription = (
        f"{spec.subscription_date[:4]}年{int(spec.subscription_date[5:7])}月"
        f"{int(spec.subscription_date[8:10])}日（T日）"
    )
    text = (
        f"{spec.company_full_name}{spec.title_marker}"
        f"发行人股票简称为“{spec.name}”股票代码为“{spec.code}”{subscription}"
    )
    return text, 1, "test-parser", "1", hashlib.sha256(text.encode()).hexdigest()


class PendingListingParserTests(unittest.TestCase):
    def test_sse_requires_unique_issue_in_progress_unlisted_row(self) -> None:
        spec = pending.SSE_PENDING_SPECS[0]
        parsed = pending.parse_sse_pending_response(_sse_payload(spec), spec=spec)
        self.assertEqual(parsed.security.code, "688826.SH")
        self.assertEqual(parsed.summary["listed_date"], "-")

        for raw in (
            _sse_payload(spec, listed_date="2026-08-20"),
            _sse_payload(spec, status="99"),
            _sse_payload(spec, total=2),
        ):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(pending.PendingListingSourceBlockedError):
                    pending.parse_sse_pending_response(raw, spec=spec)

    def test_sse_rejects_duplicate_json_keys(self) -> None:
        spec = pending.SSE_PENDING_SPECS[0]
        raw = _sse_payload(spec).replace(
            b'"locale":"en"', b'"locale":"en","locale":"en"'
        )
        with self.assertRaisesRegex(
            pending.PendingListingSourceBlockedError, "duplicate JSON key"
        ):
            pending.parse_sse_pending_response(raw, spec=spec)

    def test_current_ipo_list_closes_withdrawal_and_listing(self) -> None:
        specs = pending.SZSE_DOCUMENT_SPECS
        parsed, summary = pending.parse_cninfo_current_ipo_list(
            _current_ipo_payload(specs)
        )
        self.assertEqual(set(parsed), {"301655", "301688", "301697"})
        self.assertTrue(summary["target_codes_have_null_listing_date"])

        with self.assertRaisesRegex(
            pending.PendingListingSourceBlockedError, "withdrawn, suspended, or listed"
        ):
            pending.parse_cninfo_current_ipo_list(
                _current_ipo_payload(specs, omit="301688")
            )
        with self.assertRaisesRegex(
            pending.PendingListingSourceBlockedError, "not pending/unlisted"
        ):
            pending.parse_cninfo_current_ipo_list(
                _current_ipo_payload(specs, listed="2026-08-20")
            )
        changed = json.loads(_current_ipo_payload(specs))
        changed["data"][0]["f001v0116"] = "SUSPENDED"
        with self.assertRaisesRegex(
            pending.PendingListingSourceBlockedError, "not pending/unlisted"
        ):
            pending.parse_cninfo_current_ipo_list(
                json.dumps(changed, ensure_ascii=False).encode()
            )

    def test_stock_master_binds_code_org_and_name(self) -> None:
        with patch.object(pending, "MIN_CNINFO_MASTER_ROWS", 1):
            rows, _summary = pending.parse_cninfo_stock_master(
                _stock_master_payload(pending.SZSE_DOCUMENT_SPECS)
            )
        self.assertEqual(rows["301655"]["orgId"], "9900057453")

        value = json.loads(_stock_master_payload(pending.SZSE_DOCUMENT_SPECS))
        value["stockList"][0]["orgId"] = "wrong"
        with patch.object(pending, "MIN_CNINFO_MASTER_ROWS", 1):
            with self.assertRaises(pending.PendingListingSourceBlockedError):
                pending.parse_cninfo_stock_master(
                    json.dumps(value, ensure_ascii=False).encode()
                )

    def test_complete_announcement_page_rejects_hard_negative(self) -> None:
        spec = pending.SZSE_DOCUMENT_SPECS[0]
        rows, summary = pending.parse_cninfo_ipo_announcements(
            _announcement_payload(spec), spec=spec
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(summary["hard_negative_count"], 0)
        with self.assertRaisesRegex(
            pending.PendingListingSourceBlockedError, "hard negative"
        ):
            pending.parse_cninfo_ipo_announcements(
                _announcement_payload(spec, hard_negative=True), spec=spec
            )

    def test_pdf_requires_company_title_code_name_and_subscription_date(self) -> None:
        original = pending.SZSE_DOCUMENT_SPECS[0]
        raw = b"%PDF-FAKE-301655"
        spec = replace(original, expected_sha256=hashlib.sha256(raw).hexdigest())
        with patch.object(pending, "_extract_pdf_text", side_effect=_fake_pdf_extract):
            with patch.object(pending, "SZSE_DOCUMENT_SPECS", (spec,)):
                parsed = pending.parse_cninfo_issuance_pdf(raw, spec=spec)
        self.assertEqual(parsed.security.code, "301655.SZ")

        with patch.object(
            pending,
            "_extract_pdf_text",
            return_value=("unrelated", 1, "test", "1", "a" * 64),
        ):
            with self.assertRaisesRegex(
                pending.PendingListingSourceBlockedError, "lacks assigned-code"
            ):
                pending.parse_cninfo_issuance_pdf(raw, spec=spec)

    def test_active_catalogue_must_be_complete_and_target_absent(self) -> None:
        with patch.object(pending, "MIN_SZSE_ACTIVE_ROWS", 1):
            codes, summary = pending.parse_szse_active_catalogue(_active_xlsx())
            self.assertEqual(codes, {"300001"})
            self.assertEqual(summary["target_codes_absent"], ["301655", "301688", "301697"])
            with self.assertRaisesRegex(
                pending.PendingListingSourceBlockedError, "already active"
            ):
                pending.parse_szse_active_catalogue(
                    _active_xlsx(target_code="301655")
                )


class PendingListingEndToEndTests(unittest.TestCase):
    def test_local_real_release_manifest_cold_replays_when_cas_is_available(self) -> None:
        cas_root = (
            Path(__file__).resolve().parents[2]
            / "data"
            / "security_master"
            / "pending_listing"
            / "cas"
        )
        manifest_path = (
            cas_root
            / "sha256"
            / REAL_RELEASE_MANIFEST_SHA256[:2]
            / REAL_RELEASE_MANIFEST_SHA256
        )
        if not manifest_path.is_file():
            self.skipTest("ignored local real-evidence CAS is not present")
        replayed = pending.PendingListingManifestStore(
            pending.PendingListingRawCAS(cas_root)
        ).replay(REAL_RELEASE_MANIFEST_SHA256)
        self.assertEqual(
            replayed.logical_content_sha256, REAL_RELEASE_LOGICAL_SHA256
        )
        self.assertEqual(replayed.statistics["raw_source_count"], 12)
        self.assertEqual(replayed.statistics["target_count"], 6)
        self.assertEqual(
            replayed.to_dict()["protocol_version"],
            "cn-pending-listing-official-evidence-v2",
        )

    def test_cas_replay_rejects_reparse_leaf_and_parent_components(self) -> None:
        class _ReparseStat:
            def __init__(self, original: object) -> None:
                self._original = original
                self.st_file_attributes = int(
                    getattr(original, "st_file_attributes", 0)
                ) | 0x00000400

            def __getattr__(self, name: str) -> object:
                return getattr(self._original, name)

        with tempfile.TemporaryDirectory() as directory:
            cas = pending.PendingListingRawCAS(Path(directory))
            digest, object_path = cas.put_blob(b"immutable evidence")
            real_lstat = pending.os.lstat
            root = Path(directory).absolute()
            parent = root / "sha256"

            for unsafe_path in (object_path, parent):
                def fake_lstat(path: object, *, _unsafe: Path = unsafe_path) -> object:
                    value = real_lstat(path)
                    if Path(path).absolute() == _unsafe.absolute():
                        return _ReparseStat(value)
                    return value

                with self.subTest(unsafe_path=str(unsafe_path)):
                    with patch.object(pending.os, "lstat", side_effect=fake_lstat):
                        with self.assertRaisesRegex(
                            pending.PendingListingSourceBlockedError,
                            "link or reparse point",
                        ):
                            cas.read_blob(digest)

    def _fixture(self) -> tuple[
        tuple[pending.SZSEPendingDocumentSpec, ...],
        dict[str, _Response],
    ]:
        specs, raw_documents = _custom_szse_specs()
        responses: dict[str, _Response] = {}
        for spec in pending.SSE_PENDING_SPECS:
            responses[spec.request_url] = _Response(
                spec.request_url, _sse_payload(spec), "application/json;charset=UTF-8"
            )
        responses[pending.CNINFO_CURRENT_IPO_URL] = _Response(
            pending.CNINFO_CURRENT_IPO_URL,
            _current_ipo_payload(specs),
            "application/json;charset=UTF-8",
        )
        responses[pending.CNINFO_STOCK_MASTER_URL] = _Response(
            pending.CNINFO_STOCK_MASTER_URL,
            _stock_master_payload(specs),
            "application/json",
        )
        for spec in specs:
            url = pending.build_cninfo_announcement_request_url(spec.code)
            responses[url] = _Response(
                url, _announcement_payload(spec), "application/json;charset=UTF-8"
            )
        for spec in specs:
            responses[spec.request_url] = _Response(
                spec.request_url,
                raw_documents[spec.source_id],
                "application/pdf",
            )
        responses[pending.SZSE_ACTIVE_XLSX_URL] = _Response(
            pending.SZSE_ACTIVE_XLSX_URL,
            _active_xlsx(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;charset=GBK",
        )
        return specs, responses

    def test_get_only_cas_manifest_replay_and_freshness(self) -> None:
        specs, responses = self._fixture()
        source_order = tuple(
            [item.source_id for item in pending.SSE_PENDING_SPECS]
            + [pending.CNINFO_CURRENT_IPO_SOURCE_ID, pending.CNINFO_MASTER_SOURCE_ID]
            + [pending._cninfo_announcement_source_id(item.code) for item in specs]
            + [item.source_id for item in specs]
            + [pending.SZSE_ACTIVE_SOURCE_ID]
        )
        session = _Session(responses)
        with tempfile.TemporaryDirectory() as directory:
            cas = pending.PendingListingRawCAS(Path(directory))
            with (
                patch.object(pending, "SZSE_DOCUMENT_SPECS", specs),
                patch.object(pending, "SOURCE_ORDER", source_order),
                patch.object(pending, "MIN_CNINFO_MASTER_ROWS", 1),
                patch.object(pending, "MIN_SZSE_ACTIVE_ROWS", 1),
                patch.object(pending, "_extract_pdf_text", side_effect=_fake_pdf_extract),
            ):
                artifact = pending.PendingListingSourceClient(
                    cas=cas,
                    session=session,
                    clock=lambda: FIXED_NOW,
                ).fetch_current()
                self.assertEqual(artifact.statistics["target_count"], 6)
                self.assertEqual(artifact.statistics["raw_source_count"], 12)
                self.assertEqual(
                    {item.code for item in artifact.securities},
                    {
                        "688826.SH",
                        "688835.SH",
                        "688836.SH",
                        "301655.SZ",
                        "301688.SZ",
                        "301697.SZ",
                    },
                )
                self.assertTrue(
                    all(call["allow_redirects"] is False for call in session.calls)
                )
                self.assertTrue(
                    all(call["url"].startswith("https://") for call in session.calls)
                )

                store = pending.PendingListingManifestStore(cas)
                reference = store.seal(artifact)
                replayed = store.replay(reference.manifest_sha256)
                self.assertEqual(
                    replayed.logical_content_sha256, artifact.logical_content_sha256
                )
                self.assertTrue(
                    all(
                        item.retrieved_at == FIXED_NOW.isoformat()
                        for item in replayed.raw_sources
                    )
                )
                pending.validate_pending_listing_freshness(
                    replayed, now=FIXED_NOW + timedelta(minutes=14)
                )
                with self.assertRaisesRegex(
                    pending.PendingListingSourceBlockedError, "stale"
                ):
                    pending.validate_pending_listing_freshness(
                        replayed, now=FIXED_NOW + timedelta(minutes=16)
                    )
                pending.validate_pending_listing_freshness(
                    replayed,
                    now=FIXED_NOW + timedelta(minutes=1),
                    as_of=FIXED_NOW + timedelta(minutes=1),
                )
                with self.assertRaisesRegex(
                    pending.PendingListingSourceBlockedError, "after the requested as_of"
                ):
                    pending.validate_pending_listing_freshness(
                        replayed,
                        now=FIXED_NOW + timedelta(minutes=1),
                        as_of=FIXED_NOW - timedelta(minutes=1),
                    )

                oldest = FIXED_NOW - timedelta(minutes=16)
                newer = FIXED_NOW - timedelta(minutes=6)
                sources = [
                    replace(item, retrieved_at=newer.isoformat())
                    for item in replayed.raw_sources
                ]
                sources[0] = replace(sources[0], retrieved_at=oldest.isoformat())
                oldest_source_artifact = replace(
                    replayed, raw_sources=tuple(sources)
                )
                with self.assertRaisesRegex(
                    pending.PendingListingSourceBlockedError, "stale"
                ):
                    pending.validate_pending_listing_freshness(
                        oldest_source_artifact, now=FIXED_NOW
                    )

                redated = replace(
                    artifact,
                    retrieved_at=(FIXED_NOW + timedelta(hours=1)).isoformat(),
                )
                with self.assertRaisesRegex(
                    pending.PendingListingSourceBlockedError, "retrieved_at"
                ):
                    store.seal(redated)

    def test_redirect_content_type_and_current_list_removal_fail_closed(self) -> None:
        specs, responses = self._fixture()
        source_order = tuple(
            [item.source_id for item in pending.SSE_PENDING_SPECS]
            + [pending.CNINFO_CURRENT_IPO_SOURCE_ID, pending.CNINFO_MASTER_SOURCE_ID]
            + [pending._cninfo_announcement_source_id(item.code) for item in specs]
            + [item.source_id for item in specs]
            + [pending.SZSE_ACTIVE_SOURCE_ID]
        )

        def run(modifier: str) -> None:
            local = dict(responses)
            if modifier == "redirect":
                spec = pending.SSE_PENDING_SPECS[0]
                original = local[spec.request_url]
                local[spec.request_url] = _Response(
                    spec.request_url,
                    original.content,
                    original.headers["Content-Type"],
                    response_url="https://query.sse.com.cn/redirected",
                )
            elif modifier == "content_type":
                current = local[pending.CNINFO_CURRENT_IPO_URL]
                local[pending.CNINFO_CURRENT_IPO_URL] = _Response(
                    current.url, current.content, "text/html"
                )
            else:
                current = local[pending.CNINFO_CURRENT_IPO_URL]
                local[pending.CNINFO_CURRENT_IPO_URL] = _Response(
                    current.url,
                    _current_ipo_payload(specs, omit="301688"),
                    "application/json",
                )
            with tempfile.TemporaryDirectory() as directory:
                client = pending.PendingListingSourceClient(
                    cas=pending.PendingListingRawCAS(Path(directory)),
                    session=_Session(local),
                    clock=lambda: FIXED_NOW,
                )
                with self.assertRaises(pending.PendingListingSourceBlockedError):
                    client.fetch_current()

        with (
            patch.object(pending, "SZSE_DOCUMENT_SPECS", specs),
            patch.object(pending, "SOURCE_ORDER", source_order),
            patch.object(pending, "MIN_CNINFO_MASTER_ROWS", 1),
            patch.object(pending, "MIN_SZSE_ACTIVE_ROWS", 1),
            patch.object(pending, "_extract_pdf_text", side_effect=_fake_pdf_extract),
        ):
            for modifier in ("redirect", "content_type", "withdrawn"):
                with self.subTest(modifier=modifier):
                    run(modifier)


if __name__ == "__main__":
    unittest.main()
