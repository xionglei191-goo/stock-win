from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from research_platform.official_historical_bars import (
    BSE_STATUS_CONTRACT_UNADMITTED,
    PRICE_ADJUSTMENT_CONTRACT_UNADMITTED,
    REQUIRES_AUTHORIZED_SZSI_HISTORY,
    SSE_DAYK_SELECT,
    OfficialHistoricalBarsBlockedError,
    OfficialHistoricalBarsClient,
    RawResponseCAS,
    RawResponseEvidence,
    observe_bse_status_from_bytes,
    parse_bse_kline_response,
    parse_sse_dayk_page,
    parse_szse_public_history_probe,
)


RETRIEVED_AT = "2026-08-12T14:30:00+08:00"


class _Response:
    def __init__(
        self,
        content: bytes,
        *,
        url: str,
        content_type: str = "application/json",
        status_code: int = 200,
    ) -> None:
        self.content = content
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected network call")
        return self.responses.pop(0)


def _sse_raw(
    rows: list[list[Any]],
    *,
    total: int,
    begin: int,
    end: int,
    code: str = "600432",
    callback: str = "jsonpCallback",
) -> bytes:
    payload = {
        "code": code,
        "total": total,
        "begin": begin,
        "end": end,
        "kline": rows,
    }
    return (
        f"{callback}({json.dumps(payload, ensure_ascii=False, separators=(',', ':'))});"
    ).encode("utf-8")


def _sse_row(day: str, close: str = "10.1") -> list[str]:
    return [day, "10", "10.5", "9.8", close, "1234", "12456.70"]


def _bse_row(day: str, *, close: str = "12.1") -> dict[str, str]:
    return {
        "jsrq": day,
        "jrkp": "12.0",
        "jrsp": close,
        "drzd": "12.5",
        "drzx": "11.8",
        "zrsp": "11.9",
        "cjl": "1000",
        "cjje": "12050.0",
    }


def _bse_raw(
    rows: list[dict[str, Any]], *, status: Any = "0", msg: str = "success"
) -> bytes:
    return json.dumps(
        {"status": status, "msg": msg, "data": rows},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _evidence(raw: bytes) -> RawResponseEvidence:
    digest = hashlib.sha256(raw).hexdigest()
    return RawResponseEvidence(
        source_url="https://www.szse.cn/api/market/ssjjhq/getHistoryData",
        method="GET",
        retrieved_at=RETRIEVED_AT,
        content_sha256=digest,
        byte_count=len(raw),
        content_type="application/json",
        cas_uri=f"sha256:{digest}",
        object_path=None,
        persisted=False,
        request={"code": "000511"},
        response={},
    )


class SSEOfficialHistoricalBarsTests(unittest.TestCase):
    def test_full_history_uses_explicit_half_open_pages_and_raw_cas(self) -> None:
        first = _sse_raw(
            [_sse_row("20180706"), _sse_row("20180709")],
            total=4,
            begin=0,
            end=2,
        )
        second = _sse_raw(
            [_sse_row("20180710"), _sse_row("20180711")],
            total=4,
            begin=2,
            end=4,
        )
        session = _Session(
            [
                _Response(
                    first,
                    url="https://yunhq.sse.com.cn:32042/v1/sh1/dayk/600432",
                    content_type="application/javascript",
                ),
                _Response(
                    second,
                    url="https://yunhq.sse.com.cn:32042/v1/sh1/dayk/600432",
                    content_type="application/javascript",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = OfficialHistoricalBarsClient(
                session=session,  # type: ignore[arg-type]
                cas=RawResponseCAS(Path(directory) / "raw"),
            )
            artifact = client.fetch_sse(
                "600432.SH",
                page_size=2,
                retrieved_at=RETRIEVED_AT,
                expected_page_hashes={
                    "0:2": hashlib.sha256(first).hexdigest(),
                    "2:4": hashlib.sha256(second).hexdigest(),
                },
            )

            for response in artifact.raw_responses:
                self.assertTrue(response.persisted)
                self.assertTrue(Path(str(response.object_path)).is_file())
                self.assertEqual(
                    hashlib.sha256(Path(str(response.object_path)).read_bytes()).hexdigest(),
                    response.content_sha256,
                )

        self.assertEqual(artifact.code, "600432.SH")
        self.assertEqual(len(artifact.bars), 4)
        self.assertEqual(artifact.pagination["interval"], "HALF_OPEN")
        self.assertEqual(artifact.pagination["page_count"], 2)
        self.assertEqual(len(artifact.logical_content_sha256), 64)
        self.assertEqual(
            artifact.usage_gate["status"],
            PRICE_ADJUSTMENT_CONTRACT_UNADMITTED,
        )
        self.assertFalse(artifact.usage_gate["feature_generation_allowed"])
        self.assertFalse(artifact.usage_gate["label_generation_allowed"])
        self.assertFalse(artifact.usage_gate["execution_backtest_allowed"])
        self.assertEqual([call["params"]["begin"] for call in session.calls], [0, 2])
        self.assertEqual([call["params"]["end"] for call in session.calls], [2, 4])
        self.assertTrue(all(call["params"]["select"] == SSE_DAYK_SELECT for call in session.calls))
        self.assertTrue(all(call["params"]["callback"] == "jsonpCallback" for call in session.calls))
        self.assertTrue(all(call["allow_redirects"] is False for call in session.calls))

    def test_negative_probe_accepts_only_echo_or_documented_normalization(self) -> None:
        rows = [_sse_row("20180709"), _sse_row("20180710")]
        echo = parse_sse_dayk_page(
            _sse_raw(rows, total=4, begin=-3, end=-1),
            code="600432",
            request_begin=-3,
            request_end=-1,
        )
        normalized = parse_sse_dayk_page(
            _sse_raw(rows, total=4, begin=1, end=3),
            code="600432",
            request_begin=-3,
            request_end=-1,
        )

        self.assertEqual(echo.response_interval_semantics, "REQUEST_ECHO_HALF_OPEN")
        self.assertEqual(
            normalized.response_interval_semantics,
            "DATA_BOUND_NORMALIZED_HALF_OPEN",
        )
        self.assertEqual((normalized.normalized_begin, normalized.normalized_end), (1, 3))

    def test_pagination_total_and_schema_drift_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            OfficialHistoricalBarsBlockedError, "pagination begin/end drift"
        ):
            parse_sse_dayk_page(
                _sse_raw([_sse_row("20180709")], total=2, begin=1, end=2),
                code="600432",
                request_begin=0,
                request_end=1,
            )
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "schema drift"):
            parse_sse_dayk_page(
                _sse_raw([["20180709", "10"]], total=1, begin=0, end=1),
                code="600432",
                request_begin=0,
                request_end=1,
            )
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "outside admitted"):
            parse_sse_dayk_page(
                _sse_raw([_sse_row("20180709")], total=100_001, begin=0, end=1),
                code="600432",
                request_begin=0,
                request_end=1,
            )

    def test_duplicate_date_invalid_numeric_ohlc_and_hash_fail_closed(self) -> None:
        cases = (
            (
                _sse_raw(
                    [_sse_row("20180709"), _sse_row("20180709")],
                    total=2,
                    begin=0,
                    end=2,
                ),
                "duplicate",
            ),
            (
                _sse_raw(
                    [["20180709", "NaN", "10.5", "9.8", "10", "1", "1"]],
                    total=1,
                    begin=0,
                    end=1,
                ),
                "non-finite",
            ),
            (
                _sse_raw(
                    [["20180709", "10", "9", "8", "10", "1", "1"]],
                    total=1,
                    begin=0,
                    end=1,
                ),
                "OHLC range",
            ),
        )
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, message):
                    parse_sse_dayk_page(
                        raw,
                        code="600432",
                        request_begin=0,
                        request_end=2 if "duplicate" in message else 1,
                    )
        raw = _sse_raw([_sse_row("20180709")], total=1, begin=0, end=1)
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "hash mismatch"):
            parse_sse_dayk_page(
                raw,
                code="600432",
                request_begin=0,
                request_end=1,
                expected_sha256="0" * 64,
            )

    def test_jsonp_callback_and_strict_origin_fail_closed(self) -> None:
        raw = _sse_raw([_sse_row("20180709")], total=1, begin=0, end=1, callback="evil")
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "callback wrapper"):
            parse_sse_dayk_page(
                raw,
                code="600432",
                request_begin=0,
                request_end=1,
            )
        client = OfficialHistoricalBarsClient(
            session=_Session([]),  # type: ignore[arg-type]
            sse_endpoint="https://example.com:32042/v1/sh1/dayk/{code}",
        )
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "origin changed"):
            client.fetch_sse("600432", page_size=1, retrieved_at=RETRIEVED_AT)


class BSEOfficialHistoricalBarsTests(unittest.TestCase):
    def test_status_must_be_manually_admitted_before_fetch(self) -> None:
        session = _Session([])
        client = OfficialHistoricalBarsClient(session=session)  # type: ignore[arg-type]
        with self.assertRaises(OfficialHistoricalBarsBlockedError) as caught:
            client.fetch_bse("832317", expected_status=None, retrieved_at=RETRIEVED_AT)

        self.assertEqual(caught.exception.status, BSE_STATUS_CONTRACT_UNADMITTED)
        self.assertEqual(session.calls, [])

    def test_read_only_status_observation_does_not_admit_payload(self) -> None:
        raw = _bse_raw([_bse_row("2020-07-27")], status=0)
        observed = observe_bse_status_from_bytes(raw)

        self.assertFalse(observed["ready"])
        self.assertEqual(observed["status"], BSE_STATUS_CONTRACT_UNADMITTED)
        self.assertEqual(observed["observed_status"], 0)
        self.assertTrue(observed["promotion_blocked"])

    def test_full_response_is_validated_and_pagination_is_recorded_unsupported(self) -> None:
        raw = _bse_raw(
            [_bse_row("2020-07-27"), _bse_row("2022-04-25", close="12.2")],
            status="0",
        )
        session = _Session(
            [
                _Response(
                    raw,
                    url=(
                        "https://www.bse.cn/companyEchartsController/getKLine/"
                        "list/832317.do"
                    ),
                )
            ]
        )
        client = OfficialHistoricalBarsClient(session=session)  # type: ignore[arg-type]
        artifact = client.fetch_bse(
            "832317.BJ",
            expected_status="0",
            retrieved_at=RETRIEVED_AT,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )

        self.assertEqual(artifact.code, "832317.BJ")
        self.assertEqual([item.date for item in artifact.bars], ["2020-07-27", "2022-04-25"])
        self.assertEqual(artifact.bars[0].previous_close, 11.9)
        self.assertFalse(artifact.pagination["supported"])
        self.assertEqual(
            artifact.pagination["server_behavior"],
            "FULL_RESPONSE_IGNORES_BEGIN_END",
        )
        self.assertEqual(session.calls[0]["params"]["type"], "dayKline")
        self.assertEqual(session.calls[0]["params"]["xxfcbj"], 2)

    def test_status_schema_range_unique_numeric_date_and_hash_fail_closed(self) -> None:
        valid = _bse_row("2020-07-27")
        invalid_cases: list[tuple[bytes, str, Any]] = []
        invalid_cases.append((_bse_raw([valid], status="1"), "status mismatch", "0"))
        invalid_cases.append((_bse_raw([valid], status=0), "status mismatch", "0"))
        extra = {**valid, "unexpected": "x"}
        invalid_cases.append((_bse_raw([extra]), "schema drift", "0"))
        invalid_cases.append(
            (_bse_raw([valid, valid]), "duplicate dates", "0")
        )
        bad_number = {**valid, "cjl": "Infinity"}
        invalid_cases.append((_bse_raw([bad_number]), "non-finite", "0"))
        bad_date = {**valid, "jsrq": "2020-99-99"}
        invalid_cases.append((_bse_raw([bad_date]), "invalid BSE row 0 date", "0"))
        bad_range = {**valid, "drzd": "11.0"}
        invalid_cases.append((_bse_raw([bad_range]), "OHLC range", "0"))

        for raw, message, expected_status in invalid_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, message):
                    parse_bse_kline_response(
                        raw,
                        code="832317",
                        expected_status=expected_status,
                    )
        raw = _bse_raw([valid])
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "hash mismatch"):
            parse_bse_kline_response(
                raw,
                code="832317",
                expected_status="0",
                expected_sha256="f" * 64,
            )

    def test_redirect_or_changed_response_host_is_rejected(self) -> None:
        raw = _bse_raw([_bse_row("2020-07-27")])
        session = _Session(
            [_Response(raw, url="https://example.com/kline", status_code=200)]
        )
        client = OfficialHistoricalBarsClient(session=session)  # type: ignore[arg-type]
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "origin changed"):
            client.fetch_bse(
                "832317",
                expected_status="0",
                retrieved_at=RETRIEVED_AT,
            )


class SZSEOfficialHistoricalBarsTests(unittest.TestCase):
    def test_public_failure_exposes_authorized_history_blocker(self) -> None:
        raw = json.dumps(
            {
                "code": -1,
                "message": "历史K线数据获取失败",
                "data": {"picupdata": [], "name": "", "code": ""},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        session = _Session(
            [
                _Response(
                    raw,
                    url="https://www.szse.cn/api/market/ssjjhq/getHistoryData",
                )
            ]
        )
        client = OfficialHistoricalBarsClient(session=session)  # type: ignore[arg-type]
        probe = client.probe_szse("000511.SZ", retrieved_at=RETRIEVED_AT)

        self.assertFalse(probe.ready)
        self.assertEqual(probe.status, REQUIRES_AUTHORIZED_SZSI_HISTORY)
        self.assertEqual(probe.observed_code, -1)
        self.assertFalse(probe.data_present)
        self.assertTrue(probe.to_dict()["promotion_blocked"])
        self.assertEqual(session.calls[0]["params"]["cycleType"], 32)
        self.assertEqual(session.calls[0]["params"]["marketId"], 1)

    def test_code_zero_with_missing_data_still_fails_closed(self) -> None:
        raw = json.dumps({"code": 0, "message": "ok", "data": {}}).encode("utf-8")
        probe = parse_szse_public_history_probe(
            raw,
            code="000511",
            raw_response=_evidence(raw),
        )

        self.assertFalse(probe.ready)
        self.assertEqual(probe.status, REQUIRES_AUTHORIZED_SZSI_HISTORY)
        self.assertIn("returned no data", probe.detail)

    def test_missing_response_code_and_hash_mismatch_fail_closed(self) -> None:
        missing = json.dumps({"message": "bad", "data": {}}).encode("utf-8")
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "schema drift"):
            parse_szse_public_history_probe(
                missing,
                code="000511",
                raw_response=_evidence(missing),
            )
        raw = json.dumps({"code": -1, "data": {}}).encode("utf-8")
        with self.assertRaisesRegex(OfficialHistoricalBarsBlockedError, "hash mismatch"):
            parse_szse_public_history_probe(
                raw,
                code="000511",
                raw_response=_evidence(raw),
                expected_sha256="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
