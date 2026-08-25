from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from unittest.mock import patch

import research_platform.bse_termination_events as bse_events

from research_platform.bse_termination_events import (
    BSEEventLedgerStore,
    BSETerminationEventBlockedError,
    BSETerminationEventClient,
    BSETerminationNoticeEvidenceClient,
    BSE_REQUIRED_FIELDS,
    ManifestEvidence,
    SOURCE_CONTRACT_ADMITTED,
    SOURCE_CONTRACT_UNADMITTED,
    TERMINATION_CLASSIFICATION_INCOMPLETE,
    TargetListingEvidenceClient,
    build_bse_termination_request_url,
    classify_bse_terminations,
    parse_bse_termination_page,
    parse_bse_termination_notice_evidence,
    parse_target_listing_evidence,
)


RETRIEVED_AT = "2026-08-13T10:30:00+08:00"
START_DATE = "2021-11-15"
END_DATE = "2023-12-31"


def _row(
    code: str,
    short_name: str,
    legal_name: str,
    notice_date: str,
    path: str,
) -> dict[str, Any]:
    return {
        "companyCd": code,
        "xxfcbj": "2",
        "companyName": short_name,
        "disclosureTitle": f"\u5173\u4e8e{legal_name}\u80a1\u7968\u7ec8\u6b62\u4e0a\u5e02\u7684\u516c\u544a",
        "disclosurePostTitle": "",
        "destFilePath": path,
        "publishDate": notice_date,
        "fileExt": "pdf",
        "isNewThree": 3,
        "xxzrlx": "B",
        "infoId": 0,
    }


def _three_rows() -> list[dict[str, Any]]:
    return [
        _row(
            "833994",
            "\u7ff0\u535a\u9ad8\u65b0",
            "\u7ff0\u535a\u9ad8\u65b0\u6750\u6599\uff08\u5408\u80a5\uff09\u80a1\u4efd\u6709\u9650\u516c\u53f8",
            "2022-07-22",
            "/disclosure/2022/2022-07-22/1658490830_367062.pdf",
        ),
        _row(
            "833874",
            "\u6cf0\u7965\u80a1\u4efd",
            "\u5341\u5830\u5e02\u6cf0\u7965\u5b9e\u4e1a\u80a1\u4efd\u6709\u9650\u516c\u53f8",
            "2022-07-15",
            "/disclosure/2022/2022-07-15/1657873668_989344.pdf",
        ),
        _row(
            "832317",
            "\u89c2\u5178\u9632\u52a1",
            "\u89c2\u5178\u9632\u52a1\u6280\u672f\u80a1\u4efd\u6709\u9650\u516c\u53f8",
            "2022-04-25",
            "/uploads/6/file/public/202204/20220425154042_5k3rv6mpla.pdf",
        ),
    ]


def _raw_page(
    rows: list[dict[str, Any]],
    *,
    number: int,
    total_elements: int,
    total_pages: int,
    size: int = 20,
    first_page: bool | None = None,
    last_page: bool | None = None,
    status: Any = 0,
    sort: Any = None,
) -> bytes:
    if first_page is None:
        first_page = number == 0
    if last_page is None:
        last_page = total_pages == 0 or number == total_pages - 1
    payload = [
        {
            "listInfo": {
                "content": rows,
                "firstPage": first_page,
                "lastPage": last_page,
                "number": number,
                "numberOfElements": len(rows),
                "size": size,
                "sort": sort,
                "totalElements": total_elements,
                "totalPages": total_pages,
            },
            "status": status,
        }
    ]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"null({body})".encode("utf-8")


class _Response:
    def __init__(
        self,
        content: bytes,
        *,
        url: str,
        status_code: int = 200,
        content_type: str = "text/html;charset=utf-8",
    ) -> None:
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected network call")
        return self.responses.pop(0)


def _target_html(
    legal_name: str,
    code: str,
    listing_date: str,
    *,
    spaced_chinese_date: bool = False,
) -> bytes:
    visible_date = listing_date
    if spaced_chinese_date:
        year, month, day = (int(item) for item in listing_date.split("-"))
        visible_date = f"{year} \u5e74 {month} \u6708 {day} \u65e5"
    return (
        "<!doctype html><html><head><title>Official switch-board listing</title>"
        f"</head><body>{legal_name} (stock code {code}) switch-board listing date "
        f"{visible_date}. The shares will be listed on the exchange.</body></html>"
    ).encode("utf-8")


def _notice_pdf(seed: bytes = b"fixture") -> bytes:
    return b"%PDF-1.7\n" + seed + b"\n%%EOF\n"


def _notice_text(legal_name: str, code: str, effective_date: str) -> str:
    year, month, day = (int(item) for item in effective_date.split("-"))
    return (
        f"\u5173\u4e8e{legal_name}\u80a1\u7968\u7ec8\u6b62\u4e0a\u5e02\u7684\u516c\u544a\n"
        f"\u8bc1\u5238\u4ee3\u7801\uff1a{code}\u3002\u672c\u6240\u73b0\u51b3\u5b9a\u81ea {year} \u5e74 {month} \u6708 {day} \u65e5\u8d77"
        "\u7ec8\u6b62\u5176\u80a1\u7968\u4e0a\u5e02\u3002"
    )


def _write_manifest_variant(root: Path, value: dict[str, Any]) -> ManifestEvidence:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    path = root / "manifests" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return ManifestEvidence(
        manifest_sha256=digest,
        cas_uri=f"sha256:{digest}",
        object_path=str(path.resolve()),
        byte_count=len(content),
    )


def _manifest_value(artifact: Any) -> dict[str, Any]:
    assert artifact.manifest is not None
    return json.loads(Path(artifact.manifest.object_path).read_text(encoding="utf-8"))


class BSETerminationPageContractTests(unittest.TestCase):
    def test_real_observed_projection_shape_parses_three_termination_notices(self) -> None:
        raw = _raw_page(
            _three_rows(), number=0, total_elements=3, total_pages=1
        )
        url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        page = parse_bse_termination_page(
            raw,
            start_date=START_DATE,
            end_date=END_DATE,
            request_page=0,
            source_url=url,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )

        self.assertEqual(page.total_elements, 3)
        self.assertEqual(
            [item.code_alias for item in page.records],
            ["833994.BJ", "833874.BJ", "832317.BJ"],
        )
        self.assertEqual(
            page.records[0].legal_name,
            "\u7ff0\u535a\u9ad8\u65b0\u6750\u6599(\u5408\u80a5)\u80a1\u4efd\u6709\u9650\u516c\u53f8",
        )
        self.assertEqual(
            page.records[-1].notice_url,
            "https://www.bse.cn/uploads/6/file/public/202204/20220425154042_5k3rv6mpla.pdf",
        )

    def test_request_url_freezes_get_query_and_repeated_projection_fields(self) -> None:
        url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=7
        )
        parsed = urlsplit(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "www.bse.cn")
        self.assertEqual(dict(pairs)["page"], "7")
        self.assertEqual(dict(pairs)["keyword"], "\u7ec8\u6b62\u4e0a\u5e02")
        self.assertEqual(
            [value for key, value in pairs if key == "needFields[]"],
            list(BSE_REQUIRED_FIELDS),
        )

    def test_schema_page_metadata_total_duplicate_code_date_host_and_hash_fail_closed(self) -> None:
        url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        cases: list[tuple[bytes, str, str, str | None]] = []

        extra = {**_three_rows()[0], "unexpected": "drift"}
        cases.append(
            (
                _raw_page([extra], number=0, total_elements=1, total_pages=1),
                url,
                "row schema drift",
                None,
            )
        )
        cases.append(
            (
                _raw_page(
                    _three_rows(), number=0, total_elements=3, total_pages=2
                ),
                url,
                "total-pages metadata mismatch",
                None,
            )
        )
        duplicate = [_three_rows()[0], _three_rows()[0]]
        cases.append(
            (
                _raw_page(duplicate, number=0, total_elements=2, total_pages=1),
                url,
                "duplicate",
                None,
            )
        )
        bad_code = {**_three_rows()[0], "companyCd": "83399X"}
        cases.append(
            (
                _raw_page([bad_code], number=0, total_elements=1, total_pages=1),
                url,
                "company code",
                None,
            )
        )
        bad_date = {**_three_rows()[0], "publishDate": "2024-01-01"}
        cases.append(
            (
                _raw_page([bad_date], number=0, total_elements=1, total_pages=1),
                url,
                "outside query range",
                None,
            )
        )
        valid = _raw_page(
            _three_rows(), number=0, total_elements=3, total_pages=1
        )
        cases.append(
            (
                valid,
                url.replace("www.bse.cn", "evil.example"),
                "request URL contract drift",
                None,
            )
        )
        cases.append((valid, url, "hash mismatch", "0" * 64))

        for raw, source_url, message, expected_hash in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(BSETerminationEventBlockedError, message):
                    parse_bse_termination_page(
                        raw,
                        start_date=START_DATE,
                        end_date=END_DATE,
                        request_page=0,
                        source_url=source_url,
                        expected_sha256=expected_hash,
                    )

    def test_jsonp_status_and_full_field_fallback_fail_closed(self) -> None:
        url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        valid = _raw_page(
            _three_rows(), number=0, total_elements=3, total_pages=1
        )
        with self.assertRaisesRegex(BSETerminationEventBlockedError, "wrapper drift"):
            parse_bse_termination_page(
                valid[5:-1],
                start_date=START_DATE,
                end_date=END_DATE,
                request_page=0,
                source_url=url,
            )
        status = _raw_page(
            _three_rows(), number=0, total_elements=3, total_pages=1, status="0"
        )
        with self.assertRaisesRegex(BSETerminationEventBlockedError, "status mismatch"):
            parse_bse_termination_page(
                status,
                start_date=START_DATE,
                end_date=END_DATE,
                request_page=0,
                source_url=url,
            )
        full = {**_three_rows()[0], "disclosureCode": "not-projected"}
        with self.assertRaisesRegex(BSETerminationEventBlockedError, "row schema drift"):
            parse_bse_termination_page(
                _raw_page([full], number=0, total_elements=1, total_pages=1),
                start_date=START_DATE,
                end_date=END_DATE,
                request_page=0,
                source_url=url,
            )


class BSETerminationClientTests(unittest.TestCase):
    def test_default_source_contract_blocks_before_network(self) -> None:
        session = _Session([])
        with tempfile.TemporaryDirectory() as directory:
            client = BSETerminationEventClient(
                session=session,  # type: ignore[arg-type]
                store=BSEEventLedgerStore(Path(directory)),
            )
            with self.assertRaises(BSETerminationEventBlockedError) as caught:
                client.fetch(start_date=START_DATE, end_date=END_DATE)

        self.assertEqual(caught.exception.status, SOURCE_CONTRACT_UNADMITTED)
        self.assertEqual(session.calls, [])

    def test_two_page_get_only_loop_saves_exact_raw_bytes_and_manifest(self) -> None:
        first_day = date(2023, 12, 31)
        rows = [
            _row(
                f"{830000 + index:06d}",
                f"\u6d4b\u8bd5{index:02d}",
                f"\u6d4b\u8bd5\u4f01\u4e1a{index:02d}\u80a1\u4efd\u6709\u9650\u516c\u53f8",
                (first_day - timedelta(days=index)).isoformat(),
                f"/disclosure/2023/fixture-{index:02d}.pdf",
            )
            for index in range(21)
        ]
        first = _raw_page(
            rows[:20], number=0, total_elements=21, total_pages=2
        )
        second = _raw_page(
            rows[20:], number=1, total_elements=21, total_pages=2
        )
        urls = [
            build_bse_termination_request_url(
                start_date=START_DATE, end_date=END_DATE, page=page
            )
            for page in (0, 1)
        ]
        session = _Session(
            [
                _Response(first, url=urls[0]),
                _Response(second, url=urls[1]),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BSEEventLedgerStore(Path(directory))
            artifact = BSETerminationEventClient(
                session=session,  # type: ignore[arg-type]
                store=store,
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            ).fetch(
                start_date=START_DATE,
                end_date=END_DATE,
                retrieved_at=RETRIEVED_AT,
                expected_page_hashes={
                    0: hashlib.sha256(first).hexdigest(),
                    1: hashlib.sha256(second).hexdigest(),
                },
            )

            self.assertIsNotNone(artifact.manifest)
            assert artifact.manifest is not None
            manifest = store.verify_manifest(artifact.manifest)
            self.assertEqual(manifest["logical_content_sha256"], artifact.logical_content_sha256)
            self.assertEqual(len(artifact.raw_pages), 2)
            for raw, evidence in zip((first, second), artifact.raw_pages, strict=True):
                self.assertEqual(Path(evidence.object_path).read_bytes(), raw)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), evidence.content_sha256)
                self.assertEqual(evidence.method, "GET")

        self.assertEqual([item["url"] for item in session.calls], urls)
        self.assertTrue(all(item["allow_redirects"] is False for item in session.calls))

    def test_content_type_and_cross_page_total_drift_fail_closed(self) -> None:
        one = _raw_page(
            [_three_rows()[0]], number=0, total_elements=1, total_pages=1
        )
        url0 = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        with tempfile.TemporaryDirectory() as directory:
            bad_type = BSETerminationEventClient(
                session=_Session(
                    [_Response(one, url=url0, content_type="application/json")]
                ),  # type: ignore[arg-type]
                store=BSEEventLedgerStore(Path(directory) / "type"),
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            )
            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "content type"
            ):
                bad_type.fetch(start_date=START_DATE, end_date=END_DATE)

        rows = [
            _row(
                f"{830000 + index:06d}",
                f"\u6d4b\u8bd5{index:02d}",
                f"\u6d4b\u8bd5\u4f01\u4e1a{index:02d}\u80a1\u4efd\u6709\u9650\u516c\u53f8",
                "2022-01-01",
                f"/disclosure/2022/fixture-{index:02d}.pdf",
            )
            for index in range(22)
        ]
        first = _raw_page(rows[:20], number=0, total_elements=21, total_pages=2)
        second = _raw_page(rows[20:], number=1, total_elements=22, total_pages=2)
        url1 = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=1
        )
        with tempfile.TemporaryDirectory() as directory:
            client = BSETerminationEventClient(
                session=_Session(
                    [_Response(first, url=url0), _Response(second, url=url1)]
                ),  # type: ignore[arg-type]
                store=BSEEventLedgerStore(Path(directory)),
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            )
            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "totals changed"
            ):
                client.fetch(start_date=START_DATE, end_date=END_DATE)

    def test_manifest_recomputes_and_rejects_forged_summary_and_caller_aggregate(
        self,
    ) -> None:
        raw = _raw_page(
            _three_rows(), number=0, total_elements=3, total_pages=1
        )
        url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = BSEEventLedgerStore(root)
            artifact = BSETerminationEventClient(
                session=_Session([_Response(raw, url=url)]),  # type: ignore[arg-type]
                store=store,
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            ).fetch(
                start_date=START_DATE,
                end_date=END_DATE,
                retrieved_at=RETRIEVED_AT,
                expected_page_hashes={0: hashlib.sha256(raw).hexdigest()},
            )
            forged = _manifest_value(artifact)
            forged["completeness"]["ready"] = True
            forged["completeness"]["promotion_blocked"] = False
            forged["completeness"]["status"] = "SOURCE_COMPLETE"
            forged_evidence = _write_manifest_variant(root, forged)

            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "aggregate"
            ):
                store.verify_manifest(forged_evidence)

            caller_completeness = {
                **artifact.completeness,
                "ready": True,
                "promotion_blocked": False,
                "status": "SOURCE_COMPLETE",
            }
            caller_artifact = replace(
                artifact,
                completeness=caller_completeness,
                manifest=None,
            )
            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "caller-supplied"
            ):
                store.publish(caller_artifact)

    def test_manifest_reparse_rejects_missing_page(self) -> None:
        first_day = date(2023, 12, 31)
        rows = [
            _row(
                f"{831000 + index:06d}",
                f"\u5206\u9875{index:02d}",
                f"\u5206\u9875\u4f01\u4e1a{index:02d}\u80a1\u4efd\u6709\u9650\u516c\u53f8",
                (first_day - timedelta(days=index)).isoformat(),
                f"/disclosure/2023/page-{index:02d}.pdf",
            )
            for index in range(21)
        ]
        pages = (
            _raw_page(rows[:20], number=0, total_elements=21, total_pages=2),
            _raw_page(rows[20:], number=1, total_elements=21, total_pages=2),
        )
        urls = [
            build_bse_termination_request_url(
                start_date=START_DATE, end_date=END_DATE, page=page
            )
            for page in (0, 1)
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = BSEEventLedgerStore(root)
            artifact = BSETerminationEventClient(
                session=_Session(
                    [
                        _Response(pages[0], url=urls[0]),
                        _Response(pages[1], url=urls[1]),
                    ]
                ),  # type: ignore[arg-type]
                store=store,
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            ).fetch(
                start_date=START_DATE,
                end_date=END_DATE,
                retrieved_at=RETRIEVED_AT,
                expected_page_hashes={
                    index: hashlib.sha256(raw).hexdigest()
                    for index, raw in enumerate(pages)
                },
            )
            missing = _manifest_value(artifact)
            missing["raw_pages"] = missing["raw_pages"][:1]
            missing_evidence = _write_manifest_variant(root, missing)

            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "close pagination"
            ):
                store.verify_manifest(missing_evidence)

    def test_manifest_reparse_rejects_target_evidence_replacement(self) -> None:
        raw = _raw_page(
            _three_rows(), number=0, total_elements=3, total_pages=1
        )
        url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = BSEEventLedgerStore(root)
            sse_name = "\u89c2\u5178\u9632\u52a1\u6280\u672f\u80a1\u4efd\u6709\u9650\u516c\u53f8"
            sse_raw = _target_html(sse_name, "688287", "2022-05-25")
            sse = parse_target_listing_evidence(
                sse_raw,
                exchange="SSE",
                target_code="688287",
                legal_name=sse_name,
                listing_date="2022-05-25",
                source_url=(
                    "https://www.sse.com.cn/disclosure/announcement/listing/c/"
                    "c_20220523_5702466.shtml"
                ),
                retrieved_at=RETRIEVED_AT,
                content_type="text/html",
                store=store,
            )
            szse_name = (
                "\u7ff0\u535a\u9ad8\u65b0\u6750\u6599\uff08\u5408\u80a5\uff09\u80a1\u4efd\u6709\u9650\u516c\u53f8"
            )
            szse_raw = _target_html(szse_name, "301321", "2022-08-18")
            szse = parse_target_listing_evidence(
                szse_raw,
                exchange="SZSE",
                target_code="301321",
                legal_name=szse_name,
                listing_date="2022-08-18",
                source_url=(
                    "https://www.szse.cn/disclosure/notice/company/"
                    "t20220817_595389.html"
                ),
                retrieved_at=RETRIEVED_AT,
                content_type="text/html",
                store=store,
            )
            artifact = BSETerminationEventClient(
                session=_Session([_Response(raw, url=url)]),  # type: ignore[arg-type]
                store=store,
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            ).fetch(
                start_date=START_DATE,
                end_date=END_DATE,
                retrieved_at=RETRIEVED_AT,
                target_evidence=[sse, szse],
                expected_page_hashes={0: hashlib.sha256(raw).hexdigest()},
            )
            replaced = _manifest_value(artifact)
            self.assertEqual(len(replaced["target_evidence"]), 2)
            replaced["target_evidence"][0]["object_path"] = replaced[
                "target_evidence"
            ][1]["object_path"]
            replaced_evidence = _write_manifest_variant(root, replaced)

            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "outside the declared ledger store"
            ):
                store.verify_manifest(replaced_evidence)

    def test_manifest_reparse_rejects_tampered_raw_page_cas(self) -> None:
        raw = _raw_page(
            [_three_rows()[0]], number=0, total_elements=1, total_pages=1
        )
        url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BSEEventLedgerStore(Path(directory))
            artifact = BSETerminationEventClient(
                session=_Session([_Response(raw, url=url)]),  # type: ignore[arg-type]
                store=store,
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            ).fetch(
                start_date=START_DATE,
                end_date=END_DATE,
                retrieved_at=RETRIEVED_AT,
                expected_page_hashes={0: hashlib.sha256(raw).hexdigest()},
            )
            assert artifact.manifest is not None
            Path(artifact.raw_pages[0].object_path).write_bytes(b"tampered raw page")

            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "tampered"
            ):
                store.verify_manifest(artifact.manifest)


class BSETerminationClassificationTests(unittest.TestCase):
    def test_two_official_target_bindings_classify_transfer_third_stays_unclassified(self) -> None:
        raw = _raw_page(
            _three_rows(), number=0, total_elements=3, total_pages=1
        )
        list_url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BSEEventLedgerStore(Path(directory))
            sse_name = "\u89c2\u5178\u9632\u52a1\u6280\u672f\u80a1\u4efd\u6709\u9650\u516c\u53f8"
            sse_raw = _target_html(sse_name, "688287", "2022-05-25")
            sse = parse_target_listing_evidence(
                sse_raw,
                exchange="SSE",
                target_code="688287.SH",
                legal_name=sse_name,
                listing_date="2022-05-25",
                source_url=(
                    "https://www.sse.com.cn/disclosure/announcement/listing/c/"
                    "c_20220523_5702466.shtml"
                ),
                retrieved_at=RETRIEVED_AT,
                content_type="text/html",
                store=store,
                expected_sha256=hashlib.sha256(sse_raw).hexdigest(),
            )
            szse_name = (
                "\u7ff0\u535a\u9ad8\u65b0\u6750\u6599\uff08\u5408\u80a5\uff09\u80a1\u4efd\u6709\u9650\u516c\u53f8"
            )
            szse_raw = _target_html(
                szse_name,
                "301321",
                "2022-08-18",
                spaced_chinese_date=True,
            )
            szse = parse_target_listing_evidence(
                szse_raw,
                exchange="SZSE",
                target_code="301321.SZ",
                legal_name=szse_name,
                listing_date="2022-08-18",
                source_url=(
                    "https://www.szse.cn/English/about/news/listings/main/"
                    "t20220818_595422.html"
                ),
                retrieved_at=RETRIEVED_AT,
                content_type="text/html;charset=utf-8",
                store=store,
            )
            artifact = BSETerminationEventClient(
                session=_Session([_Response(raw, url=list_url)]),  # type: ignore[arg-type]
                store=store,
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            ).fetch(
                start_date=START_DATE,
                end_date=END_DATE,
                retrieved_at=RETRIEVED_AT,
                target_evidence=[sse, szse],
                expected_page_hashes={0: hashlib.sha256(raw).hexdigest()},
            )

            self.assertTrue(Path(sse.object_path).is_file())
            self.assertTrue(Path(szse.object_path).is_file())

        events = {item.source_code_alias: item for item in artifact.events}
        self.assertEqual(events["832317.BJ"].classification, "TRANSFER")
        self.assertEqual(events["832317.BJ"].target_code_alias, "688287.SH")
        self.assertEqual(events["833994.BJ"].classification, "TRANSFER")
        self.assertEqual(events["833994.BJ"].target_code_alias, "301321.SZ")
        self.assertEqual(
            events["833874.BJ"].classification, "TERMINATION_UNCLASSIFIED"
        )
        self.assertIsNone(events["833874.BJ"].target_code_alias)
        self.assertNotIn("DELIST", {item.classification for item in artifact.events})
        self.assertFalse(artifact.completeness["ready"])
        self.assertEqual(
            artifact.completeness["status"],
            TERMINATION_CLASSIFICATION_INCOMPLETE,
        )
        self.assertEqual(artifact.completeness["transfer_count"], 2)
        self.assertEqual(artifact.completeness["unclassified_codes"], ["833874.BJ"])

    def test_termination_notice_alone_never_infers_delist_or_transfer(self) -> None:
        raw = _raw_page(
            _three_rows(), number=0, total_elements=3, total_pages=1
        )
        page = parse_bse_termination_page(
            raw,
            start_date=START_DATE,
            end_date=END_DATE,
            request_page=0,
            source_url=build_bse_termination_request_url(
                start_date=START_DATE, end_date=END_DATE, page=0
            ),
        )
        events = classify_bse_terminations(page.records, [], [])

        self.assertEqual(
            {item.classification for item in events},
            {"TERMINATION_UNCLASSIFIED"},
        )

    def test_target_evidence_identity_code_date_origin_and_binding_fail_closed(self) -> None:
        name = "\u89c2\u5178\u9632\u52a1\u6280\u672f\u80a1\u4efd\u6709\u9650\u516c\u53f8"
        valid_raw = _target_html(name, "688287", "2022-05-25")
        valid_url = (
            "https://www.sse.com.cn/disclosure/announcement/listing/c/"
            "c_20220523_5702466.shtml"
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BSEEventLedgerStore(Path(directory))
            cases = (
                (
                    _target_html("\u5176\u4ed6\u516c\u53f8\u80a1\u4efd\u6709\u9650\u516c\u53f8", "688287", "2022-05-25"),
                    valid_url,
                    "same legal company identity",
                ),
                (
                    _target_html(name, "688999", "2022-05-25"),
                    valid_url,
                    "target code",
                ),
                (
                    _target_html(name, "688287", "2022-05-26"),
                    valid_url,
                    "listing date",
                ),
                (
                    valid_raw,
                    valid_url.replace("www.sse.com.cn", "evil.example"),
                    "origin changed",
                ),
            )
            for raw, url, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(
                        BSETerminationEventBlockedError, message
                    ):
                        parse_target_listing_evidence(
                            raw,
                            exchange="SSE",
                            target_code="688287",
                            legal_name=name,
                            listing_date="2022-05-25",
                            source_url=url,
                            retrieved_at=RETRIEVED_AT,
                            content_type="text/html;charset=utf-8",
                            store=store,
                        )

            evidence = parse_target_listing_evidence(
                valid_raw,
                exchange="SSE",
                target_code="688287",
                legal_name=name,
                listing_date="2022-05-25",
                source_url=valid_url,
                retrieved_at=RETRIEVED_AT,
                content_type="text/html;charset=utf-8",
                store=store,
            )
            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "not bound"
            ):
                classify_bse_terminations([], [], [evidence])

            post_evidence = replace(evidence, method="POST")
            raw = _raw_page(
                [_three_rows()[-1]], number=0, total_elements=1, total_pages=1
            )
            record = parse_bse_termination_page(
                raw,
                start_date=START_DATE,
                end_date=END_DATE,
                request_page=0,
                source_url=build_bse_termination_request_url(
                    start_date=START_DATE, end_date=END_DATE, page=0
                ),
            ).records[0]
            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "GET-only"
            ):
                classify_bse_terminations([record], [], [post_evidence])

            Path(evidence.object_path).write_bytes(b"tampered target evidence")
            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "tampered"
            ):
                classify_bse_terminations([record], [], [evidence])


class TargetListingEvidenceClientTests(unittest.TestCase):
    def test_default_contract_blocks_before_network(self) -> None:
        session = _Session([])
        with tempfile.TemporaryDirectory() as directory:
            client = TargetListingEvidenceClient(
                session=session,  # type: ignore[arg-type]
                store=BSEEventLedgerStore(Path(directory)),
            )
            with self.assertRaises(BSETerminationEventBlockedError) as caught:
                client.fetch(
                    exchange="SZSE",
                    target_code="301321",
                    legal_name=(
                        "\u7ff0\u535a\u9ad8\u65b0\u6750\u6599\uff08\u5408\u80a5\uff09\u80a1\u4efd\u6709\u9650\u516c\u53f8"
                    ),
                    listing_date="2022-08-18",
                    source_url=(
                        "https://www.szse.cn/disclosure/notice/company/"
                        "t20220817_595389.html"
                    ),
                )

        self.assertEqual(caught.exception.status, SOURCE_CONTRACT_UNADMITTED)
        self.assertEqual(session.calls, [])

    def test_get_only_capture_and_changed_response_url_fail_closed(self) -> None:
        name = (
            "\u7ff0\u535a\u9ad8\u65b0\u6750\u6599\uff08\u5408\u80a5\uff09\u80a1\u4efd\u6709\u9650\u516c\u53f8"
        )
        source_url = (
            "https://www.szse.cn/disclosure/notice/company/"
            "t20220817_595389.html"
        )
        raw = _target_html(
            name,
            "301321",
            "2022-08-18",
            spaced_chinese_date=True,
        )
        session = _Session([_Response(raw, url=source_url, content_type="text/html")])
        with tempfile.TemporaryDirectory() as directory:
            evidence = TargetListingEvidenceClient(
                session=session,  # type: ignore[arg-type]
                store=BSEEventLedgerStore(Path(directory)),
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            ).fetch(
                exchange="SZSE",
                target_code="301321",
                legal_name=name,
                listing_date="2022-08-18",
                source_url=source_url,
                retrieved_at=RETRIEVED_AT,
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
            self.assertEqual(Path(evidence.object_path).read_bytes(), raw)

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0]["allow_redirects"] is False)

        changed = _Session(
            [
                _Response(
                    raw,
                    url=source_url.replace("www.szse.cn", "evil.example"),
                    content_type="text/html",
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = TargetListingEvidenceClient(
                session=changed,  # type: ignore[arg-type]
                store=BSEEventLedgerStore(Path(directory)),
                source_contract_status=SOURCE_CONTRACT_ADMITTED,
            )
            with self.assertRaisesRegex(
                BSETerminationEventBlockedError, "response URL changed"
            ):
                client.fetch(
                    exchange="SZSE",
                    target_code="301321",
                    legal_name=name,
                    listing_date="2022-08-18",
                    source_url=source_url,
                    retrieved_at=RETRIEVED_AT,
                )


class BSETerminationNoticeEvidenceTests(unittest.TestCase):
    def _record(self) -> Any:
        raw = _raw_page(
            [_three_rows()[-1]], number=0, total_elements=1, total_pages=1
        )
        return parse_bse_termination_page(
            raw,
            start_date=START_DATE,
            end_date=END_DATE,
            request_page=0,
            source_url=build_bse_termination_request_url(
                start_date=START_DATE, end_date=END_DATE, page=0
            ),
        ).records[0]

    def test_recomputed_notice_extracts_effective_date_and_tampering_fails(self) -> None:
        record = self._record()
        raw = _notice_pdf()
        extracted = (
            _notice_text(record.legal_name, "832317", "2022-04-26"),
            "pypdf",
            "TEST",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BSEEventLedgerStore(Path(directory))
            with patch.object(bse_events, "_extract_pdf_text", return_value=extracted):
                evidence = parse_bse_termination_notice_evidence(
                    raw,
                    record=record,
                    retrieved_at=RETRIEVED_AT,
                    content_type="application/pdf",
                    store=store,
                    expected_sha256=hashlib.sha256(raw).hexdigest(),
                )
                self.assertEqual(
                    evidence.termination_effective_date, "2022-04-26"
                )
                classify_bse_terminations([record], [evidence], [])

                Path(evidence.object_path).write_bytes(_notice_pdf(b"tampered"))
                with self.assertRaisesRegex(
                    BSETerminationEventBlockedError, "tampered"
                ):
                    classify_bse_terminations([record], [evidence], [])

    def test_notice_effective_date_must_be_unique_after_notice_and_match_identity(self) -> None:
        record = self._record()
        raw = _notice_pdf()
        invalid = (
            (
                _notice_text(record.legal_name, "832317", "2022-04-25"),
                "must follow",
            ),
            (
                _notice_text(
                    record.legal_name, "832317", "2022-04-26"
                )
                + _notice_text(
                    record.legal_name, "832317", "2022-04-27"
                ),
                "ambiguous",
            ),
            (
                _notice_text(
                    "\u5176\u4ed6\u516c\u53f8\u80a1\u4efd\u6709\u9650\u516c\u53f8",
                    "832317",
                    "2022-04-26",
                ),
                "same legal company identity",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BSEEventLedgerStore(Path(directory))
            for text_value, message in invalid:
                with self.subTest(message=message):
                    with patch.object(
                        bse_events,
                        "_extract_pdf_text",
                        return_value=(text_value, "pypdf", "TEST", 1),
                    ):
                        with self.assertRaisesRegex(
                            BSETerminationEventBlockedError, message
                        ):
                            parse_bse_termination_notice_evidence(
                                raw,
                                record=record,
                                retrieved_at=RETRIEVED_AT,
                                content_type="application/pdf",
                                store=store,
                            )

    def test_notice_client_default_blocks_and_get_capture_is_strict(self) -> None:
        record = self._record()
        session = _Session([])
        with tempfile.TemporaryDirectory() as directory:
            client = BSETerminationNoticeEvidenceClient(
                session=session,  # type: ignore[arg-type]
                store=BSEEventLedgerStore(Path(directory)),
            )
            with self.assertRaises(BSETerminationEventBlockedError) as caught:
                client.fetch(record=record)
        self.assertEqual(caught.exception.status, SOURCE_CONTRACT_UNADMITTED)
        self.assertEqual(session.calls, [])

        raw = _notice_pdf()
        session = _Session(
            [
                _Response(
                    raw,
                    url=record.notice_url,
                    content_type="application/pdf",
                )
            ]
        )
        extracted = (
            _notice_text(record.legal_name, "832317", "2022-04-26"),
            "pypdf",
            "TEST",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bse_events, "_extract_pdf_text", return_value=extracted):
                evidence = BSETerminationNoticeEvidenceClient(
                    session=session,  # type: ignore[arg-type]
                    store=BSEEventLedgerStore(Path(directory)),
                    source_contract_status=SOURCE_CONTRACT_ADMITTED,
                ).fetch(
                    record=record,
                    retrieved_at=RETRIEVED_AT,
                    expected_sha256=hashlib.sha256(raw).hexdigest(),
                )
                self.assertEqual(
                    evidence.termination_effective_date, "2022-04-26"
                )
        self.assertTrue(session.calls[0]["allow_redirects"] is False)

    def test_v2_manifest_recomputes_notice_target_and_complete_summary(self) -> None:
        list_raw = _raw_page(
            [_three_rows()[-1]], number=0, total_elements=1, total_pages=1
        )
        list_url = build_bse_termination_request_url(
            start_date=START_DATE, end_date=END_DATE, page=0
        )
        target_name = "\u89c2\u5178\u9632\u52a1\u6280\u672f\u80a1\u4efd\u6709\u9650\u516c\u53f8"
        target_raw = _target_html(target_name, "688287", "2022-05-25")
        notice_raw = _notice_pdf()
        extracted = (
            _notice_text(target_name, "832317", "2022-04-26"),
            "pypdf",
            "TEST",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = BSEEventLedgerStore(Path(directory))
            record = parse_bse_termination_page(
                list_raw,
                start_date=START_DATE,
                end_date=END_DATE,
                request_page=0,
                source_url=list_url,
            ).records[0]
            with patch.object(bse_events, "_extract_pdf_text", return_value=extracted):
                notice = parse_bse_termination_notice_evidence(
                    notice_raw,
                    record=record,
                    retrieved_at=RETRIEVED_AT,
                    content_type="application/pdf",
                    store=store,
                )
                target = parse_target_listing_evidence(
                    target_raw,
                    exchange="SSE",
                    target_code="688287",
                    legal_name=target_name,
                    listing_date="2022-05-25",
                    source_url=(
                        "https://www.sse.com.cn/disclosure/announcement/listing/c/"
                        "c_20220523_5702466.shtml"
                    ),
                    retrieved_at=RETRIEVED_AT,
                    content_type="text/html",
                    store=store,
                )
                artifact = BSETerminationEventClient(
                    session=_Session([_Response(list_raw, url=list_url)]),  # type: ignore[arg-type]
                    store=store,
                    source_contract_status=SOURCE_CONTRACT_ADMITTED,
                ).fetch(
                    start_date=START_DATE,
                    end_date=END_DATE,
                    retrieved_at=RETRIEVED_AT,
                    termination_notice_evidence=[notice],
                    target_evidence=[target],
                    expected_page_hashes={
                        0: hashlib.sha256(list_raw).hexdigest()
                    },
                )
                assert artifact.manifest is not None
                verified = store.verify_manifest(artifact.manifest)

            self.assertTrue(artifact.completeness["ready"])
            self.assertEqual(artifact.completeness["status"], "SOURCE_COMPLETE")
            self.assertEqual(
                verified["events"][0]["termination_effective_date"],
                "2022-04-26",
            )


if __name__ == "__main__":
    unittest.main()
