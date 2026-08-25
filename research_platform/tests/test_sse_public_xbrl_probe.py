from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research_platform.sse_public_xbrl_probe import (
    COVERAGE_MISSING,
    SEMANTICS_UNVERIFIED,
    SSE_DETAIL_SQL_ID,
    SSE_PAGE_SCRIPT_URL,
    SSE_QUERY_URL,
    SsePublicXbrlProbeBlockedError,
    SsePublicXbrlProbeCAS,
    SsePublicXbrlProbeClient,
    SsePublicXbrlProbeManifestStore,
)


SCRIPT = b"COMMON_SSE_PL_XBRL_YJGL_XQ commonSoaQuery.do reportYear stockId"


def _row(*, code: str = "600009", year: str = "2023") -> dict[str, str]:
    return {
        "assteration": "39.51",
        "growthRate": "101.57",
        "REPORT_PERIOD_ID": "5000",
        "REPORT_YEAR": year,
        "S2010_0380": "69480530979.72",
        "S2010_0690": "27453959185.37",
        "S2020_0010": "1104701.61",
        "S2090_0040": "93404.97",
        "S2090_0050": "82896.39",
        "S2090_0060": "402008.65",
        "S2090_0090": "0.38",
        "S2090_0130": "2.33",
        "STOCK_ID": code,
    }


def _payload(rows: list[dict[str, str]]) -> bytes:
    return json.dumps(
        {
            "actionErrors": [],
            "actionMessages": [],
            "fieldErrors": {},
            "isPagination": "false",
            "jsonCallBack": None,
            "locale": "en",
            "pageHelp": {
                "beginPage": 0,
                "cacheSize": 1,
                "data": [],
                "endDate": None,
                "endPage": 1,
                "objectResult": None,
                "pageCount": 0,
                "pageNo": 1,
                "pageSize": 20,
                "pageSizeWithOutLimit": 20,
                "searchDate": None,
                "sort": None,
                "startDate": None,
                "total": len(rows),
            },
            "pageNo": None,
            "pageSize": None,
            "queryDate": "",
            "result": rows,
            "securityCode": "",
            "sqlId": SSE_DETAIL_SQL_ID,
            "texts": None,
            "type": None,
            "validateCode": "",
        },
        separators=(",", ":"),
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
        self.history: list[object] = []


class _Session:
    def __init__(
        self,
        rows: list[dict[str, str]],
        *,
        script: bytes = SCRIPT,
        redirect: bool = False,
    ) -> None:
        self.rows = rows
        self.script = script
        self.redirect = redirect
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("GET", url, kwargs))
        response_url = "https://evil.example/script.js" if self.redirect else url
        return _Response(self.script, response_url, "application/javascript")

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("POST", url, kwargs))
        return _Response(_payload(self.rows), url, "application/json;charset=UTF-8")


class SsePublicXbrlProbeTests(unittest.TestCase):
    observed_at = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    def _fetch(
        self,
        directory: str,
        *,
        code: str,
        rows: list[dict[str, str]],
        session: _Session | None = None,
    ):
        cas = SsePublicXbrlProbeCAS(Path(directory) / "cas")
        observed_session = session or _Session(rows)
        client = SsePublicXbrlProbeClient(
            cas=cas,
            session=observed_session,  # type: ignore[arg-type]
            clock=lambda: self.observed_at,
        )
        artifact = client.fetch(code=code)
        return artifact, cas, observed_session

    def test_empty_delisted_sample_is_sealed_as_coverage_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, session = self._fetch(
                directory, code="600432.SH", rows=[]
            )
            self.assertEqual(artifact.observed_row_count, 0)
            self.assertEqual(artifact.source_contract["status"], COVERAGE_MISSING)
            self.assertFalse(artifact.source_contract["ready"])
            self.assertEqual(
                artifact.statistics["financial_report_rows_emitted"], 0
            )
            self.assertEqual(
                artifact.statistics["earnings_guidance_express_rows_emitted"], 0
            )
            post = session.calls[1]
            self.assertEqual(post[0:2], ("POST", SSE_QUERY_URL))
            self.assertFalse(post[2]["allow_redirects"])
            self.assertEqual(
                post[2]["data"],
                {
                    "isPagination": "false",
                    "sqlId": SSE_DETAIL_SQL_ID,
                    "stockId": "600432",
                    "reportYear": "2018,2019,2020,2021,2022,2023",
                },
            )

            store = SsePublicXbrlProbeManifestStore(cas)
            reference = store.seal(artifact)
            replayed = store.replay(reference.manifest_sha256)
            self.assertEqual(replayed.to_dict(), artifact.to_dict())

    def test_observed_values_remain_ineligible_for_quality_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, _cas, _session = self._fetch(
                directory, code="600009.SH", rows=[_row()]
            )
        self.assertEqual(artifact.observed_row_count, 1)
        self.assertEqual(artifact.observed_years, (2023,))
        self.assertEqual(artifact.observed_report_period_ids, ("5000",))
        self.assertEqual(artifact.source_contract["status"], SEMANTICS_UNVERIFIED)
        self.assertFalse(artifact.dataset_gates["financial_reports"]["ready"])
        self.assertEqual(
            artifact.dataset_gates["financial_reports"]["rows_emitted"], 0
        )
        self.assertIn(
            "SOURCE_DOCUMENT_HASH_UNAVAILABLE",
            artifact.dataset_gates["financial_reports"]["blocked_by"],
        )

    def test_frozen_scope_and_caller_ready_are_rejected_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = SsePublicXbrlProbeCAS(Path(directory) / "cas")
            session = _Session([])
            client = SsePublicXbrlProbeClient(
                cas=cas, session=session  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(
                SsePublicXbrlProbeBlockedError, "frozen at 2018-2023"
            ):
                client.fetch(code="600432.SH", start_year=2019)
            with self.assertRaisesRegex(
                SsePublicXbrlProbeBlockedError, "caller-ready"
            ):
                client.fetch(code="600432.SH", caller_ready=True)
            self.assertEqual(session.calls, [])

    def test_contract_redirect_and_schema_drift_fail_closed(self) -> None:
        cases = (
            ("redirect", _Session([], redirect=True), "redirected"),
            ("script", _Session([], script=b"unrelated"), "script contract"),
            ("future", _Session([_row(year="2024")]), "frozen report scope"),
        )
        for label, session, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                cas = SsePublicXbrlProbeCAS(Path(directory) / "cas")
                client = SsePublicXbrlProbeClient(
                    cas=cas, session=session  # type: ignore[arg-type]
                )
                with self.assertRaisesRegex(SsePublicXbrlProbeBlockedError, message):
                    client.fetch(code="600009.SH")

    def test_cold_replay_detects_raw_object_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact, cas, _session = self._fetch(
                directory, code="600009.SH", rows=[_row()]
            )
            store = SsePublicXbrlProbeManifestStore(cas)
            reference = store.seal(artifact)
            response_path = Path(artifact.query_response.object_path)
            response_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(
                SsePublicXbrlProbeBlockedError, "hash mismatch"
            ):
                store.replay(reference.manifest_sha256)

    def test_only_fixed_sse_code_shape_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = SsePublicXbrlProbeCAS(Path(directory) / "cas")
            session = _Session([])
            client = SsePublicXbrlProbeClient(
                cas=cas, session=session  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(
                SsePublicXbrlProbeBlockedError, "SSE A-share"
            ):
                client.fetch(code="000511.SZ")
            self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
