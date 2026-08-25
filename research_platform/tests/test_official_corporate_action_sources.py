from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from research_platform import cninfo_delisted_disclosures as cninfo
from research_platform.official_corporate_action_sources import (
    CNINFO_SOURCE_AUTHORITY,
    SSE_SOURCE_AUTHORITY,
    CorporateActionEvidenceBlockedError,
    FrozenCorporateActionTarget,
    _assemble_manifest,
    _canonical_json_bytes,
    _parse_sse_page,
    _reconciliation_title,
    _sse_document_url,
    _sse_request,
    replay_official_corporate_action_evidence,
)


NOW = "2026-08-13T22:00:00+08:00"
SSE_ROW_FIELDS = {
    "ADDDATE",
    "BULLETIN_HEADING",
    "BULLETIN_TYPE",
    "BULLETIN_YEAR",
    "INDEXCLASS",
    "OPERATION_SEQ",
    "PLAN_Date",
    "PLAN_Year",
    "ROWNUM",
    "ROWNUM_",
    "SECURITY_CODE",
    "SECURITY_NAME",
    "SSEDATE",
    "SSEDate",
    "SSETime",
    "SSETimeStr",
    "TITLE",
    "URL",
    "author",
    "book_Name",
    "bulletinHeading",
    "bulletinType",
    "bulletin_No",
    "bulletin_Type",
    "bulletin_Year",
    "category_A",
    "category_B",
    "category_C",
    "category_D",
    "chapter_No",
    "companyAbbr",
    "dispatch_Organ",
    "file_Serial",
    "finish_Time",
    "initial_Date",
    "isChangeFlag",
    "journal_Issue",
    "journal_Name",
    "journal_Section",
    "journal_Year",
    "keyWord",
    "key_Word",
    "language",
    "lemma_CN",
    "lemma_EN",
    "publishing_Comp",
    "question",
    "question_Class",
    "read_Status",
    "save_Time",
    "section",
    "security_Code",
    "source",
    "spareVolEnd",
    "title",
    "title_ETC",
    "title_PY",
    "unit_Code",
    "unit_Type",
}


def _target() -> FrozenCorporateActionTarget:
    return FrozenCorporateActionTarget(
        canonical_entity_id="CN:SSE:600432",
        exchange="SSE",
        code="600432.SH",
        query_start="2018-01-01",
        query_end="2018-07-12",
    )


def _sse_row(*, title: str = "退市吉恩关于整理期结束的公告") -> dict[str, Any]:
    row = {field: None for field in SSE_ROW_FIELDS}
    row.update(
        {
            "ADDDATE": "2018-07-11 17:58:39",
            "BULLETIN_HEADING": "临时公告",
            "BULLETIN_TYPE": "其它",
            "BULLETIN_YEAR": "2018",
            "SECURITY_CODE": "600432",
            "SSEDATE": "2018-07-12",
            "TITLE": title,
            "URL": "/disclosure/listedinfo/announcement/c/2018-07-12/600432_20180712_1.pdf",
        }
    )
    return row


def _sse_page(
    *,
    row: Mapping[str, Any] | None = None,
    begin_page: int = 1,
    page_no: int = 1,
) -> bytes:
    page = {
        "beginPage": begin_page,
        "cacheSize": 1,
        "data": [_sse_row() if row is None else dict(row)],
        "endDate": None,
        "endPage": begin_page,
        "objectResult": None,
        "pageCount": 1,
        "pageNo": page_no,
        "pageSize": 100,
        "pageSizeWithOutLimit": None,
        "searchDate": None,
        "sort": None,
        "startDate": None,
        "total": 1,
    }
    value = {
        "BULLETIN_TYPE": None,
        "END_DATE": None,
        "SECURITY_CODE": None,
        "START_DATE": None,
        "TITLE": None,
        "beginDate": "2018-01-01",
        "endDate": "2018-07-12",
        "isNew": None,
        "isPagination": "true",
        "jsonCallBack": "jsonpCallback",
        "keyWord": "",
        "pageHelp": page,
        "productId": "600432",
        "reportType": "ALL",
        "reportType2": "",
        "result": None,
        "secCodes": None,
        "securityType": "0101,120100,020100,020200,120200",
        "stockType": None,
    }
    return b"jsonpCallback(" + json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8") + b")"


def _cninfo_stock_master() -> bytes:
    rows = [
        {
            "category": "A股",
            "code": f"{index:06d}",
            "orgId": f"org{index:06d}",
            "pinyin": "x",
            "zwjc": "样本",
        }
        for index in range(1, 1001)
    ]
    rows[431] = {
        "category": "A股",
        "code": "600432",
        "orgId": "gssh0600432",
        "pinyin": "tsje",
        "zwjc": "退市吉恩",
    }
    return json.dumps(
        {"stockList": rows}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _cninfo_row(*, title: str = "关于整理期结束的公告") -> dict[str, Any]:
    row = {field: None for field in cninfo.ANNOUNCEMENT_ROW_FIELDS}
    row.update(
        {
            "adjunctSize": 10,
            "adjunctType": "PDF",
            "adjunctUrl": "finalpage/2018-07-12/1200000001.PDF",
            "announcementId": "1200000001",
            "announcementTime": 1531324800000,
            "announcementTitle": title,
            "announcementType": "01010501",
            "announcementTypeName": "临时公告",
            "orgId": "gssh0600432",
            "secCode": "600432",
        }
    )
    return row


def _cninfo_page(*, row: Mapping[str, Any] | None = None) -> bytes:
    value = {
        "announcements": [_cninfo_row() if row is None else dict(row)],
        "categoryList": None,
        "classifiedAnnouncements": None,
        "hasMore": False,
        "totalAnnouncement": 1,
        "totalRecordNum": 1,
        "totalSecurities": 1,
        "totalpages": 0,
    }
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


class OfficialCorporateActionSourcesTests(unittest.TestCase):
    def test_sse_begin_page_is_frozen_as_the_real_offset(self) -> None:
        request = _sse_request(_target(), 2)

        self.assertEqual(request["pageHelp.pageNo"], "2")
        self.assertEqual(request["pageHelp.beginPage"], "2")
        self.assertEqual(request["pageHelp.endPage"], "2")

    def test_sse_replay_rejects_page_no_without_matching_begin_page(self) -> None:
        with self.assertRaisesRegex(
            CorporateActionEvidenceBlockedError, "pagination semantics drift"
        ):
            _parse_sse_page(
                _sse_page(begin_page=1, page_no=2),
                target=_target(),
                page=2,
                page_size=100,
            )

    def test_sse_replay_rejects_rows_outside_target_date(self) -> None:
        row = _sse_row()
        row["SSEDATE"] = "2018-07-13"

        with self.assertRaisesRegex(
            CorporateActionEvidenceBlockedError, "escaped target scope"
        ):
            _parse_sse_page(
                _sse_page(row=row),
                target=_target(),
                page=1,
                page_size=100,
            )

    def test_sse_star_legacy_pdf_path_is_admitted_but_other_paths_are_not(self) -> None:
        self.assertEqual(
            _sse_document_url(
                "/disclosure/listedinfo/bulletin/star/c/688086_20210313_1.pdf",
                "688086.SH",
            ),
            "https://static.sse.com.cn/disclosure/listedinfo/bulletin/star/c/688086_20210313_1.pdf",
        )
        with self.assertRaises(CorporateActionEvidenceBlockedError):
            _sse_document_url("/other/688086_20210313_1.pdf", "688086.SH")

    def test_reconciliation_removes_only_frozen_short_name_prefix(self) -> None:
        self.assertEqual(
            _reconciliation_title("退市吉恩关于整理期结束的公告", "退市吉恩"),
            "关于整理期结束的公告",
        )
        self.assertEqual(
            _reconciliation_title("关于退市吉恩摘牌的公告", "退市吉恩"),
            "关于退市吉恩摘牌的公告",
        )

    def test_assembled_evidence_never_emits_gp_rows_or_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = cninfo.CninfoDisclosureCAS(Path(directory))
            stock = cas.capture(
                _cninfo_stock_master(),
                source_id="CNINFO_STOCK_MASTER",
                role="STOCK_MASTER",
                source_url=cninfo.CNINFO_STOCK_MASTER_URL,
                method="GET",
                retrieved_at=NOW,
                content_type="application/json",
            )
            sse = cas.capture(
                _sse_page(),
                source_id="SSE_ANNOUNCEMENTS_600432.SH_1",
                role="ANNOUNCEMENT_PAGE",
                source_url="https://query.sse.com.cn/security/stock/queryCompanyBulletin.do",
                method="GET",
                retrieved_at=NOW,
                content_type="application/json;charset=UTF-8",
            )
            cn_page = cas.capture(
                _cninfo_page(),
                source_id="CNINFO_ANNOUNCEMENTS_600432.SH_1",
                role="ANNOUNCEMENT_PAGE",
                source_url=cninfo.CNINFO_ANNOUNCEMENT_URL,
                method="POST",
                retrieved_at=NOW,
                content_type="application/json",
            )
            target = _target()
            cn_target = cninfo.FrozenDisclosureTarget(**target.to_dict())
            manifest = _assemble_manifest(
                cas=cas,
                targets=[target],
                stock_master=stock.to_dict(),
                sse_pages=[
                    {
                        "exchange": "SSE",
                        "code": target.code,
                        "query_start": target.query_start,
                        "query_end": target.query_end,
                        "page_num": 1,
                        "page_size": 100,
                        "request": _sse_request(target, 1),
                        "raw": sse.to_dict(),
                    }
                ],
                cninfo_pages=[
                    {
                        "exchange": "SSE",
                        "code": target.code,
                        "org_id": "gssh0600432",
                        "query_start": target.query_start,
                        "query_end": target.query_end,
                        "page_num": 1,
                        "page_size": 30,
                        "request": cninfo._announcement_request(
                            cn_target, "gssh0600432", 1
                        ),
                        "raw": cn_page.to_dict(),
                    }
                ],
                candidate_documents=[],
                candidate_document_gaps=[],
            )

            self.assertFalse(manifest["ready"])
            self.assertFalse(manifest["source_contract"]["gp30_eligible"])
            self.assertFalse(manifest["source_contract"]["gp43_eligible"])
            self.assertEqual(manifest["normalized"]["structured_events"], [])
            self.assertEqual(manifest["statistics"]["gp30_row_count"], 0)
            self.assertEqual(manifest["statistics"]["gp43_row_count"], 0)
            self.assertTrue(
                manifest["normalized"]["reconciliation"][0][
                    "zero_event_candidate"
                ]
            )
            self.assertFalse(
                manifest["normalized"]["reconciliation"][0][
                    "zero_event_proven"
                ]
            )

    def test_candidate_requires_pdf_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = cninfo.CninfoDisclosureCAS(Path(directory))
            stock = cas.capture(
                _cninfo_stock_master(),
                source_id="CNINFO_STOCK_MASTER",
                role="STOCK_MASTER",
                source_url=cninfo.CNINFO_STOCK_MASTER_URL,
                method="GET",
                retrieved_at=NOW,
                content_type="application/json",
            )
            sse = cas.capture(
                _sse_page(row=_sse_row(title="退市吉恩权益分派实施公告")),
                source_id="SSE_PAGE",
                role="ANNOUNCEMENT_PAGE",
                source_url="https://query.sse.com.cn/security/stock/queryCompanyBulletin.do",
                method="GET",
                retrieved_at=NOW,
                content_type="application/json",
            )
            cn_page = cas.capture(
                _cninfo_page(row=_cninfo_row(title="权益分派实施公告")),
                source_id="CNINFO_PAGE",
                role="ANNOUNCEMENT_PAGE",
                source_url=cninfo.CNINFO_ANNOUNCEMENT_URL,
                method="POST",
                retrieved_at=NOW,
                content_type="application/json",
            )
            target = _target()
            cn_target = cninfo.FrozenDisclosureTarget(**target.to_dict())

            with self.assertRaisesRegex(
                CorporateActionEvidenceBlockedError, "coverage does not match"
            ):
                _assemble_manifest(
                    cas=cas,
                    targets=[target],
                    stock_master=stock.to_dict(),
                    sse_pages=[
                        {
                            "exchange": "SSE",
                            "code": target.code,
                            "query_start": target.query_start,
                            "query_end": target.query_end,
                            "page_num": 1,
                            "page_size": 100,
                            "request": _sse_request(target, 1),
                            "raw": sse.to_dict(),
                        }
                    ],
                    cninfo_pages=[
                        {
                            "exchange": "SSE",
                            "code": target.code,
                            "org_id": "gssh0600432",
                            "query_start": target.query_start,
                            "query_end": target.query_end,
                            "page_num": 1,
                            "page_size": 30,
                            "request": cninfo._announcement_request(
                                cn_target, "gssh0600432", 1
                            ),
                            "raw": cn_page.to_dict(),
                        }
                    ],
                    candidate_documents=[],
                    candidate_document_gaps=[],
                )

    def test_manifest_cold_replay_rejects_caller_ready_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = cninfo.CninfoDisclosureCAS(Path(directory))
            payload = {
                "manifest_schema_version": "official-corporate-action-evidence-manifest-v1",
                "protocol_version": "official-corporate-action-evidence-v1",
                "targets": [],
                "stock_master": {},
                "sse_pages": [],
                "cninfo_pages": [],
                "candidate_documents": [],
                "candidate_document_gaps": [],
                "normalized": {},
                "logical_content_sha256": "0" * 64,
                "source_contract": {},
                "statistics": {},
                "ready": True,
            }
            digest, _path = cas.put_blob(_canonical_json_bytes(payload))

            with self.assertRaisesRegex(
                CorporateActionEvidenceBlockedError, "blocked contract"
            ):
                replay_official_corporate_action_evidence(
                    cas_root=Path(directory), manifest_sha256=digest
                )


if __name__ == "__main__":
    unittest.main()
