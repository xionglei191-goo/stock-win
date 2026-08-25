from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import requests

from research_platform.sse_delisted_suspension_source import (
    PAGE_SIZE,
    SOURCE_SCOPE,
    SOURCE_STATUS,
    SSE_JSONP_CALLBACK,
    SSE_SQL_ID,
    SSEDelistedSuspensionBlockedError,
    SSEDelistedSuspensionCAS,
    SSEDelistedSuspensionManifestStore,
    SSEDelistedSuspensionSourceClient,
    build_sse_delisted_suspension_request_url,
    parse_sse_delisted_suspension_page,
    plan_sse_query_windows,
)


RETRIEVED_AT = "2026-08-13T14:00:00+08:00"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _row(
    *,
    code: str = "600432",
    name: str = "退市吉恩",
    start: str = "20180712",
    end: str = "20180716",
    control_type: str = "TR",
    event_type: str = "LXTP",
    stop_time: str = "",
    stop_reason: str = "重要公告",
    end_reason: str = "终止上市",
) -> dict[str, str]:
    return {
        "startStopDate": start,
        "stopReason": stop_reason,
        "productCode": code,
        "endStopReason": end_reason,
        "controlType": control_type,
        "endStopDate": end,
        "stopTime": stop_time,
        "type": event_type,
        "productName": name,
    }


def _payload(
    rows: list[dict[str, str]],
    *,
    page_no: int = 1,
    total: int | None = None,
    page_count: int | None = None,
) -> dict[str, Any]:
    total_value = len(rows) if total is None else total
    page_count_value = (
        ((total_value + PAGE_SIZE - 1) // PAGE_SIZE)
        if page_count is None
        else page_count
    )
    return {
        "actionErrors": [],
        "actionMessages": [],
        "fieldErrors": {},
        "isPagination": "true",
        "jsonCallBack": SSE_JSONP_CALLBACK,
        "locale": "en",
        "pageHelp": {
            "beginPage": 1 if total_value else 0,
            "cacheSize": 1,
            "data": rows,
            "endDate": None,
            "endPage": None,
            "objectResult": None,
            "pageCount": page_count_value,
            "pageNo": page_no,
            "pageSize": PAGE_SIZE,
            "pageSizeWithOutLimit": PAGE_SIZE,
            "searchDate": None,
            "sort": None,
            "startDate": None,
            "total": total_value,
        },
        "pageNo": None,
        "pageSize": None,
        "queryDate": "",
        "result": rows,
        "securityCode": "",
        "sqlId": SSE_SQL_ID,
        "texts": None,
        "type": "",
        "validateCode": "",
    }


def _raw(
    rows: list[dict[str, str]],
    *,
    page_no: int = 1,
    total: int | None = None,
    page_count: int | None = None,
) -> bytes:
    body = json.dumps(
        _payload(rows, page_no=page_no, total=total, page_count=page_count),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{SSE_JSONP_CALLBACK}({body})".encode("utf-8")


class _Response:
    def __init__(
        self,
        *,
        url: str,
        content: bytes,
        status_code: int = 200,
        content_type: str = "application/json;charset=UTF-8",
    ) -> None:
        self.url = url
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class _FakeSession:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return self.responses[url]

    def post(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("suspension source must never issue POST")


class _TimeoutSession:
    def get(self, _url: str, **_kwargs: Any) -> _Response:
        raise requests.Timeout("simulated timeout")


class _RedirectedResponseSession:
    def __init__(self, expected_url: str, response_url: str, content: bytes) -> None:
        self.expected_url = expected_url
        self.response_url = response_url
        self.content = content

    def get(self, url: str, **_kwargs: Any) -> _Response:
        if url != self.expected_url:
            raise AssertionError("unexpected request URL")
        return _Response(url=self.response_url, content=self.content)


def _master_record(
    *,
    code: str = "600432.SH",
    exchange: str = "SSE",
    listed_at: str = "2003-09-05",
    delisted_at: str = "2018-07-13",
) -> dict[str, Any]:
    suffix = "SSE" if exchange == "SSE" else "SZSE"
    return {
        "canonical_entity_id": f"CN:{suffix}:{code[:6]}",
        "exchange": exchange,
        "code_alias": code,
        "board": "SSE_MAIN" if exchange == "SSE" else "SZSE_MAIN",
        "listed_at": listed_at,
        "delisted_at": delisted_at,
        "valid_from": listed_at,
        "valid_to": delisted_at,
        "event_type": "TERMINATED_LISTING",
        "source_url": "https://official.example/security-master",
        "source_hash": "1" * 64,
        "retrieved_at": RETRIEVED_AT,
        "name": "Fixture",
        "attributes": {},
    }


def _build_master(
    root: Path,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = records or [_master_record()]
    content = b"\n".join(_canonical(item) for item in rows) + b"\n"
    content_hash = hashlib.sha256(content).hexdigest()
    object_path = root / "objects" / content_hash[:2] / content_hash
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(content)
    manifest = {
        "protocol_version": "synthetic-security-master-v1",
        "quality_policy_version": "synthetic-v1",
        "sources": [],
        "artifacts": {
            "security_master_jsonl": {
                "content_hash": content_hash,
                "object_path": str(object_path.resolve()),
                "row_count": len(rows),
            }
        },
    }
    manifest_bytes = _canonical(manifest)
    snapshot_id = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_path = root / "manifests" / f"{snapshot_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_bytes)
    return {
        "snapshot_id": snapshot_id,
        "manifest_hash": snapshot_id,
        "manifest_path": str(manifest_path.resolve()),
        "protocol_version": "synthetic-security-master-v1",
    }


def _fixture_session() -> _FakeSession:
    url = build_sse_delisted_suspension_request_url(
        code="600432.SH",
        query_start="2018-01-01",
        query_end="2018-07-13",
        page_no=1,
    )
    rows = [
        _row(),
        _row(
            name="*ST吉恩",
            start="20170502",
            end="20180529",
            end_reason="重要公告",
        ),
    ]
    return _FakeSession({url: _Response(url=url, content=_raw(rows))})


class SSEDelistedSuspensionSourceTests(unittest.TestCase):
    def test_real_shape_fixture_parses_lxtp_as_full_day_candidate(self) -> None:
        page = parse_sse_delisted_suspension_page(
            _raw([_row()]),
            code="600432.SH",
            query_start="2017-05-01",
            query_end="2018-07-17",
            page_no=1,
        )
        self.assertEqual(page.total, 1)
        self.assertEqual(page.events[0].event_type, "LXTP")
        self.assertTrue(page.events[0].full_day_candidate)
        self.assertEqual(page.events[0].start_stop_date, "2018-07-12")
        self.assertEqual(page.events[0].end_stop_date, "2018-07-16")

    def test_wh_is_candidate_but_partial_day_codes_are_raw_events_only(self) -> None:
        rows = [
            _row(code="688555", start="20221121", end="20221121", event_type="LSTP", stop_time="WH"),
            _row(code="688555", start="20221122", end="20221122", event_type="LSTP", stop_time="930"),
            _row(code="688555", start="20221123", end="20221123", event_type="LSTP", stop_time="AM"),
            _row(code="688555", start="20221124", end="20221124", event_type="LSTP", stop_time="PM"),
        ]
        page = parse_sse_delisted_suspension_page(
            _raw(rows),
            code="688555.SH",
            query_start="2022-01-01",
            query_end="2022-12-31",
            page_no=1,
        )
        self.assertEqual(
            [item.full_day_candidate for item in page.events],
            [True, False, False, False],
        )

    def test_non_tr_event_is_never_full_day_candidate(self) -> None:
        page = parse_sse_delisted_suspension_page(
            _raw([_row(control_type="CB", stop_time="WH")]),
            code="600432.SH",
            query_start="2017-05-01",
            query_end="2018-07-17",
            page_no=1,
        )
        self.assertFalse(page.events[0].full_day_candidate)

    def test_query_windows_are_contiguous_and_each_is_shorter_than_three_years(self) -> None:
        windows = plan_sse_query_windows("2018-01-01", "2025-12-31")
        self.assertEqual(
            windows,
            (
                ("2018-01-01", "2020-12-31"),
                ("2021-01-01", "2023-12-31"),
                ("2024-01-01", "2025-12-31"),
            ),
        )
        for index, (start, end) in enumerate(windows):
            self.assertLess((date.fromisoformat(end) - date.fromisoformat(start)).days, 1096)
            if index:
                self.assertEqual(
                    date.fromisoformat(start),
                    date.fromisoformat(windows[index - 1][1]).fromordinal(
                        date.fromisoformat(windows[index - 1][1]).toordinal() + 1
                    ),
                )

    def test_leap_day_windows_are_contiguous_and_stay_within_three_years(self) -> None:
        windows = plan_sse_query_windows("2020-02-29", "2026-03-01")
        self.assertEqual(
            windows,
            (
                ("2020-02-29", "2023-02-27"),
                ("2023-02-28", "2026-02-27"),
                ("2026-02-28", "2026-03-01"),
            ),
        )

    def test_request_is_fixed_get_jsonp_contract(self) -> None:
        url = build_sse_delisted_suspension_request_url(
            code="600432.SH",
            query_start="2017-05-01",
            query_end="2018-07-17",
            page_no=1,
        )
        self.assertIn("sqlId=GW_PL_JYTS_TFPXX", url)
        self.assertIn("productCode=600432", url)
        self.assertIn("pageHelp.pageNo=1", url)
        with self.assertRaises(SSEDelistedSuspensionBlockedError):
            build_sse_delisted_suspension_request_url(
                code="600432.SH",
                query_start="2017-01-01",
                query_end="2020-01-02",
                page_no=1,
            )

    def test_fetch_uses_only_get_and_persists_exact_raw_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(root / "master")
            session = _fixture_session()
            artifact = SSEDelistedSuspensionSourceClient(
                cas=SSEDelistedSuspensionCAS(root / "cas"),
                session=session,
            ).fetch(
                master_identity=master,
                coverage_start="2018-01-01",
                coverage_end="2023-12-31",
                retrieved_at=RETRIEVED_AT,
            )

            self.assertEqual(len(session.calls), 1)
            _url, kwargs = session.calls[0]
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], 30.0)
            self.assertEqual(kwargs["headers"]["Referer"], "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/")
            self.assertEqual(len(artifact.events), 2)
            raw = artifact.raw_responses[0]
            self.assertEqual(raw.method, "GET")
            self.assertEqual(raw.retrieved_at, RETRIEVED_AT)
            self.assertEqual(Path(raw.object_path).read_bytes(), _raw([
                _row(),
                _row(name="*ST吉恩", start="20170502", end="20180529", end_reason="重要公告"),
            ]))
            self.assertFalse(artifact.source_contract["ready"])
            self.assertEqual(artifact.source_contract["status"], SOURCE_STATUS)
            self.assertEqual(artifact.source_contract["source_scope"], SOURCE_SCOPE)
            self.assertFalse(artifact.source_contract["training_allowed"])
            self.assertFalse(artifact.source_contract["trading_allowed"])
            self.assertFalse(artifact.source_contract["retrieved_at_is_publication_time"])
            self.assertNotIn("published_at", artifact.events[0].to_dict())
            self.assertNotIn("suspension_status", artifact.events[0].to_dict())

    def test_manifest_cold_replays_raw_pages_and_frozen_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(root / "master")
            cas = SSEDelistedSuspensionCAS(root / "cas")
            artifact = SSEDelistedSuspensionSourceClient(
                cas=cas,
                session=_fixture_session(),
            ).fetch(
                master_identity=master,
                coverage_start="2018-01-01",
                coverage_end="2023-12-31",
                retrieved_at=RETRIEVED_AT,
            )
            reference = SSEDelistedSuspensionManifestStore(cas).seal(artifact)
            replayed = SSEDelistedSuspensionManifestStore(cas).replay(
                reference.manifest_sha256
            )

            self.assertEqual(replayed.to_dict(), artifact.to_dict())
            self.assertFalse(reference.ready)
            self.assertEqual(reference.status, SOURCE_STATUS)
            self.assertFalse(reference.training_allowed)
            self.assertFalse(reference.trading_allowed)
            self.assertEqual(reference.master_snapshot_id, master["snapshot_id"])
            self.assertEqual(
                reference.target_set_sha256,
                artifact.master_binding["target_set_sha256"],
            )

    def test_master_scope_is_derived_and_szse_rows_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(
                root / "master",
                [
                    _master_record(),
                    _master_record(code="000511.SZ", exchange="SZSE"),
                    {
                        **_master_record(code="600001.SH"),
                        "delisted_at": None,
                        "valid_to": None,
                        "event_type": "ACTIVE_LISTING",
                    },
                ],
            )
            artifact = SSEDelistedSuspensionSourceClient(
                cas=SSEDelistedSuspensionCAS(root / "cas"),
                session=_fixture_session(),
            ).fetch(
                master_identity=master,
                coverage_start="2018-01-01",
                coverage_end="2023-12-31",
                retrieved_at=RETRIEVED_AT,
            )
            self.assertEqual([item.code for item in artifact.targets], ["600432.SH"])
            self.assertTrue(artifact.master_binding["targets_derived_from_frozen_master"])
            self.assertFalse(artifact.master_binding["caller_target_list_allowed"])

    def test_wrong_master_snapshot_and_master_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(root / "master")
            client = SSEDelistedSuspensionSourceClient(
                cas=SSEDelistedSuspensionCAS(root / "cas"),
                session=_fixture_session(),
            )
            with self.assertRaises(SSEDelistedSuspensionBlockedError):
                client.fetch(
                    master_identity={**master, "snapshot_id": "f" * 64},
                    coverage_start="2018-01-01",
                    coverage_end="2023-12-31",
                    retrieved_at=RETRIEVED_AT,
                )
            manifest = Path(master["manifest_path"])
            manifest.write_bytes(manifest.read_bytes() + b" ")
            with self.assertRaises(SSEDelistedSuspensionBlockedError):
                client.fetch(
                    master_identity=master,
                    coverage_start="2018-01-01",
                    coverage_end="2023-12-31",
                    retrieved_at=RETRIEVED_AT,
                )

    def test_raw_cas_tamper_breaks_cold_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(root / "master")
            cas = SSEDelistedSuspensionCAS(root / "cas")
            artifact = SSEDelistedSuspensionSourceClient(
                cas=cas,
                session=_fixture_session(),
            ).fetch(
                master_identity=master,
                coverage_start="2018-01-01",
                coverage_end="2023-12-31",
                retrieved_at=RETRIEVED_AT,
            )
            reference = SSEDelistedSuspensionManifestStore(cas).seal(artifact)
            Path(artifact.raw_responses[0].object_path).write_bytes(b"tampered")
            with self.assertRaises(SSEDelistedSuspensionBlockedError):
                SSEDelistedSuspensionManifestStore(cas).replay(
                    reference.manifest_sha256
                )

    def test_manifest_cannot_forge_ready_or_publication_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(root / "master")
            cas = SSEDelistedSuspensionCAS(root / "cas")
            artifact = SSEDelistedSuspensionSourceClient(
                cas=cas,
                session=_fixture_session(),
            ).fetch(
                master_identity=master,
                coverage_start="2018-01-01",
                coverage_end="2023-12-31",
                retrieved_at=RETRIEVED_AT,
            )
            reference = SSEDelistedSuspensionManifestStore(cas).seal(artifact)
            manifest_bytes, _ = cas.read_blob(reference.manifest_sha256)
            manifest = json.loads(manifest_bytes)
            manifest["source_contract"]["ready"] = True
            forged_hash, _ = cas.put_blob(_canonical(manifest))
            with self.assertRaises(SSEDelistedSuspensionBlockedError):
                SSEDelistedSuspensionManifestStore(cas).replay(forged_hash)

    def test_duplicate_keys_nonfinite_wrong_callback_sql_and_schema_fail_closed(self) -> None:
        valid = _raw([_row()])
        cases = [
            valid.replace(b'"locale":"en"', b'"locale":"en","locale":"en"'),
            valid.replace(b'"pageSize":25', b'"pageSize":NaN'),
            valid.replace(SSE_JSONP_CALLBACK.encode(), b"wrongCallback", 1),
            valid.replace(SSE_SQL_ID.encode(), b"WRONG_SQL", 1),
            valid.replace(b'"productName":"', b'"unexpected":"x","productName":"', 1),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                with self.assertRaises(SSEDelistedSuspensionBlockedError):
                    parse_sse_delisted_suspension_page(
                        raw,
                        code="600432.SH",
                        query_start="2017-05-01",
                        query_end="2018-07-17",
                        page_no=1,
                    )

    def test_interval_nonoverlap_inverted_event_and_wrong_target_fail_closed(self) -> None:
        cases = [
            _row(start="20160101", end="20160102"),
            _row(start="20180716", end="20180712"),
            _row(code="600433"),
        ]
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(SSEDelistedSuspensionBlockedError):
                    parse_sse_delisted_suspension_page(
                        _raw([row]),
                        code="600432.SH",
                        query_start="2017-05-01",
                        query_end="2018-07-17",
                        page_no=1,
                    )

    def test_pagination_requires_every_contiguous_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(root / "master")
            first_rows = [
                _row(
                    start=f"201801{index + 1:02d}",
                    end=f"201801{index + 1:02d}",
                    event_type="LSTP",
                    stop_time="WH",
                    stop_reason=f"reason-{index}",
                )
                for index in range(PAGE_SIZE)
            ]
            second_row = _row(
                start="20180201",
                end="20180201",
                event_type="LSTP",
                stop_time="WH",
            )
            responses: dict[str, _Response] = {}
            for page_no, raw in (
                (1, _raw(first_rows, page_no=1, total=26, page_count=2)),
                (2, _raw([second_row], page_no=2, total=26, page_count=2)),
            ):
                url = build_sse_delisted_suspension_request_url(
                    code="600432.SH",
                    query_start="2018-01-01",
                    query_end="2018-07-13",
                    page_no=page_no,
                )
                responses[url] = _Response(url=url, content=raw)
            artifact = SSEDelistedSuspensionSourceClient(
                cas=SSEDelistedSuspensionCAS(root / "cas"),
                session=_FakeSession(responses),
            ).fetch(
                master_identity=master,
                coverage_start="2018-01-01",
                coverage_end="2023-12-31",
                retrieved_at=RETRIEVED_AT,
            )
            self.assertEqual(len(artifact.raw_responses), 2)
            self.assertEqual(len(artifact.events), 26)

    def test_identical_cross_window_event_is_deduplicated_but_raw_pages_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(
                root / "master",
                [
                    _master_record(
                        code="600500.SH",
                        listed_at="2018-01-01",
                        delisted_at="2024-01-02",
                    )
                ],
            )
            repeated = _row(
                code="600500",
                name="Cross Window",
                start="20201231",
                end="20210102",
            )
            responses: dict[str, _Response] = {}
            for query_start, query_end in (
                ("2018-01-01", "2020-12-31"),
                ("2021-01-01", "2023-12-31"),
            ):
                url = build_sse_delisted_suspension_request_url(
                    code="600500.SH",
                    query_start=query_start,
                    query_end=query_end,
                    page_no=1,
                )
                responses[url] = _Response(url=url, content=_raw([repeated]))
            artifact = SSEDelistedSuspensionSourceClient(
                cas=SSEDelistedSuspensionCAS(root / "cas"),
                session=_FakeSession(responses),
            ).fetch(
                master_identity=master,
                coverage_start="2018-01-01",
                coverage_end="2023-12-31",
                retrieved_at=RETRIEVED_AT,
            )
            self.assertEqual(len(artifact.raw_responses), 2)
            self.assertEqual(len(artifact.events), 1)

    def test_conflicting_cross_window_event_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(
                root / "master",
                [
                    _master_record(
                        code="600500.SH",
                        listed_at="2018-01-01",
                        delisted_at="2024-01-02",
                    )
                ],
            )
            first = _row(
                code="600500",
                name="Cross Window",
                start="20201231",
                end="20210102",
            )
            changed = {**first, "endStopReason": "changed"}
            responses: dict[str, _Response] = {}
            for query_start, query_end, row in (
                ("2018-01-01", "2020-12-31", first),
                ("2021-01-01", "2023-12-31", changed),
            ):
                url = build_sse_delisted_suspension_request_url(
                    code="600500.SH",
                    query_start=query_start,
                    query_end=query_end,
                    page_no=1,
                )
                responses[url] = _Response(url=url, content=_raw([row]))
            with self.assertRaises(SSEDelistedSuspensionBlockedError):
                SSEDelistedSuspensionSourceClient(
                    cas=SSEDelistedSuspensionCAS(root / "cas"),
                    session=_FakeSession(responses),
                ).fetch(
                    master_identity=master,
                    coverage_start="2018-01-01",
                    coverage_end="2023-12-31",
                    retrieved_at=RETRIEVED_AT,
                )

    def test_http_timeout_redirect_content_type_and_hash_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = _build_master(root / "master")
            with self.assertRaises(SSEDelistedSuspensionBlockedError):
                SSEDelistedSuspensionSourceClient(
                    cas=SSEDelistedSuspensionCAS(root / "timeout-cas"),
                    session=_TimeoutSession(),
                ).fetch(
                    master_identity=master,
                    coverage_start="2018-01-01",
                    coverage_end="2023-12-31",
                    retrieved_at=RETRIEVED_AT,
                )

            for options in (
                {"content_type": "text/html"},
                {"status_code": 500},
            ):
                url = build_sse_delisted_suspension_request_url(
                    code="600432.SH",
                    query_start="2018-01-01",
                    query_end="2018-07-13",
                    page_no=1,
                )
                session = _FakeSession(
                    {url: _Response(url=url, content=_raw([_row()]), **options)}
                )
                with self.assertRaises(SSEDelistedSuspensionBlockedError):
                    SSEDelistedSuspensionSourceClient(
                        cas=SSEDelistedSuspensionCAS(root / f"cas-{len(str(options))}"),
                        session=session,
                    ).fetch(
                        master_identity=master,
                        coverage_start="2018-01-01",
                        coverage_end="2023-12-31",
                        retrieved_at=RETRIEVED_AT,
                    )

            expected_url = build_sse_delisted_suspension_request_url(
                code="600432.SH",
                query_start="2018-01-01",
                query_end="2018-07-13",
                page_no=1,
            )
            redirected_url = expected_url.replace(
                "pageHelp.pageNo=1", "pageHelp.pageNo=2"
            )
            with self.assertRaises(SSEDelistedSuspensionBlockedError):
                SSEDelistedSuspensionSourceClient(
                    cas=SSEDelistedSuspensionCAS(root / "redirect-cas"),
                    session=_RedirectedResponseSession(
                        expected_url,
                        redirected_url,
                        _raw([_row()]),
                    ),
                ).fetch(
                    master_identity=master,
                    coverage_start="2018-01-01",
                    coverage_end="2023-12-31",
                    retrieved_at=RETRIEVED_AT,
                )

            with self.assertRaises(SSEDelistedSuspensionBlockedError):
                SSEDelistedSuspensionSourceClient(
                    cas=SSEDelistedSuspensionCAS(root / "hash-cas"),
                    session=_fixture_session(),
                ).fetch(
                    master_identity=master,
                    coverage_start="2018-01-01",
                    coverage_end="2023-12-31",
                    retrieved_at=RETRIEVED_AT,
                    expected_hashes={
                        "600432.SH:2018-01-01:2018-07-13:page=1": "f" * 64
                    },
                )

    def test_retrieved_at_is_audit_metadata_not_published_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = SSEDelistedSuspensionSourceClient(
                cas=SSEDelistedSuspensionCAS(root / "cas"),
                session=_fixture_session(),
            ).fetch(
                master_identity=_build_master(root / "master"),
                coverage_start="2018-01-01",
                coverage_end="2023-12-31",
                retrieved_at=RETRIEVED_AT,
            )
            serialized = json.dumps(artifact.to_dict(), ensure_ascii=False)
            self.assertIn(RETRIEVED_AT, serialized)
            self.assertNotIn("published_at", serialized)
            self.assertNotIn("effective_at", serialized)
            self.assertFalse(artifact.source_contract["publication_time_resolved"])


if __name__ == "__main__":
    unittest.main()
