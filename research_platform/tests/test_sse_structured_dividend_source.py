from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from research_platform.sse_structured_dividend_source import (
    QUERY_URL,
    FrozenSSEDividendTarget,
    SSEStructuredDividendBlockedError,
    _parse_page,
    capture_sse_structured_dividends,
    replay_sse_structured_dividends,
)


ROW = {
    "SECURITY_CODE_A": "688086",
    "RECORD_DATE_A": "2020-07-09",
    "EX_DIVIDEND_DATE_A": "2020-07-10",
    "DIVIDEND_PER_SHARE1_A": "    0.218",
    "DIVIDEND_PER_SHARE2_A": "    0.218",
    "EXCHANGE_RATE": "-",
    "NUM": "1",
    "COMPANY_CODE": "688086",
    "FULL_NAME": "广东紫晶信息存储技术股份有限公司",
    "DIVIDEND_DATE": "2020-07-09",
    "SECURITY_ABBR_A": "退市紫晶",
}


def _target() -> FrozenSSEDividendTarget:
    return FrozenSSEDividendTarget(
        canonical_entity_id="CN:SSE:688086",
        code="688086.SH",
        start_year=2020,
        end_year=2020,
    )


def _page_bytes(rows: list[dict[str, str]]) -> bytes:
    total = len(rows)
    page_help = {
        "beginPage": 1,
        "cacheSize": 1,
        "data": rows,
        "endDate": None,
        "endPage": 1,
        "objectResult": None,
        "pageCount": 1 if total else 0,
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
        "jsonCallBack": None,
        "locale": "en",
        "pageHelp": page_help,
        "pageNo": None,
        "pageSize": None,
        "queryDate": "",
        "result": rows,
        "securityCode": "",
        "sqlId": "COMMON_SSE_GP_SJTJ_FHSG_AGFH_L_NEW",
        "texts": None,
        "type": "",
        "validateCode": "",
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


class _Response:
    status_code = 200

    def __init__(self, *, params: dict[str, str], content: bytes) -> None:
        self.url = QUERY_URL + "?" + urlencode(params)
        self.headers = {"Content-Type": "application/json;charset=UTF-8"}
        self.content = content


class _Session:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls = 0

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls += 1
        self.asserted_url = url
        return _Response(
            params=dict(kwargs["params"]),  # type: ignore[arg-type]
            content=self.content,
        )


class SSEStructuredDividendSourceTests(unittest.TestCase):
    def test_official_row_normalizes_exact_cash_and_effective_date(self) -> None:
        parsed = _parse_page(_page_bytes([dict(ROW)]), target=_target(), year=2020, page=1)

        self.assertEqual(parsed["total"], 1)
        self.assertEqual(parsed["rows"][0]["gross_cash_per_share"], "0.218")
        self.assertEqual(parsed["rows"][0]["net_cash_per_share"], "0.218")
        self.assertEqual(parsed["rows"][0]["ex_dividend_date"], "2020-07-10")

    def test_schema_drift_is_rejected(self) -> None:
        row = dict(ROW)
        del row["NUM"]

        with self.assertRaisesRegex(
            SSEStructuredDividendBlockedError, "row schema drift"
        ):
            _parse_page(_page_bytes([row]), target=_target(), year=2020, page=1)

    def test_wrong_code_is_rejected(self) -> None:
        row = dict(ROW)
        row["SECURITY_CODE_A"] = "600432"

        with self.assertRaisesRegex(
            SSEStructuredDividendBlockedError, "escaped code scope"
        ):
            _parse_page(_page_bytes([row]), target=_target(), year=2020, page=1)

    def test_capture_and_cold_replay_remain_corroboration_only(self) -> None:
        session = _Session(_page_bytes([dict(ROW)]))
        with tempfile.TemporaryDirectory() as directory:
            reference = capture_sse_structured_dividends(
                cas_root=Path(directory),
                targets=[_target()],
                session=session,  # type: ignore[arg-type]
                clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
            )
            manifest = replay_sse_structured_dividends(
                cas_root=Path(directory),
                manifest_sha256=reference.manifest_sha256,
            )

        self.assertFalse(reference.ready)
        self.assertFalse(manifest["ready"])
        self.assertEqual(manifest["quality_rows"], [])
        self.assertEqual(manifest["statistics"]["gp30_row_count"], 0)
        self.assertEqual(manifest["statistics"]["gp43_row_count"], 0)
        self.assertIsNone(manifest["corroboration_rows"][0]["published_at"])
        self.assertIsNone(
            manifest["corroboration_rows"][0]["source_document_hash"]
        )

    def test_empty_response_does_not_prove_zero_events(self) -> None:
        session = _Session(_page_bytes([]))
        with tempfile.TemporaryDirectory() as directory:
            reference = capture_sse_structured_dividends(
                cas_root=Path(directory),
                targets=[_target()],
                session=session,  # type: ignore[arg-type]
                clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
            )
            manifest = replay_sse_structured_dividends(
                cas_root=Path(directory),
                manifest_sha256=reference.manifest_sha256,
            )

        self.assertEqual(manifest["corroboration_rows"], [])
        self.assertFalse(manifest["source_contract"]["zero_event_inference_allowed"])
        self.assertFalse(manifest["ready"])


if __name__ == "__main__":
    unittest.main()
