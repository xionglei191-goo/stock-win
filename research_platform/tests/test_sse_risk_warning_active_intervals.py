from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from research_platform.sse_risk_warning_active_intervals import (
    PROTOCOL_VERSION,
    REQUEST_PAGE_SIZE,
    SOURCE_STATUS,
    SSERiskWarningActiveIntervalsBlockedError,
    SSERiskWarningActiveIntervalsCAS,
    SSERiskWarningActiveIntervalsClient,
    SSERiskWarningActiveIntervalsManifestStore,
    TRANSITION_BINDING_CONVERGED,
    TRANSITION_BINDING_LAG,
    build_page_request_url,
    parse_status_page,
)
from research_platform.sse_risk_warning_source import (
    SOURCE_SPECS,
    SSERiskWarningManifestStore,
    SSERiskWarningRawCAS,
    SSERiskWarningSourceClient,
    build_source_request_url,
)
from research_platform.sse_risk_warning_transition_source import (
    PROTOCOL_VERSION as TRANSITION_PROTOCOL_VERSION,
    SOURCE_STATUS as TRANSITION_SOURCE_STATUS,
    SSERiskWarningTransitionCAS,
    SSERiskWarningTransitionManifestStore,
)


RISK_RETRIEVED_AT = "2026-08-13T12:00:00+08:00"
STATUS_RETRIEVED_AT = "2026-08-13T13:00:00+08:00"
TRANSITION_MANIFEST_SHA256 = "a" * 64


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


class _Session:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        return self.responses[url]

    def post(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("official evidence adapters must never issue POST")


class _TimeoutSession:
    def get(self, _url: str, **_kwargs: object) -> _Response:
        raise requests.Timeout("simulated timeout")


def _risk_payload(
    source_index: int,
    rows: list[tuple[str, str]],
) -> dict[str, object]:
    spec = SOURCE_SPECS[source_index]
    if source_index == 0:
        result = [
            {"INSTRUMENT_SHORT": name, "INSTRUMENT_ID": code}
            for code, name in rows
        ]
    else:
        result = [
            {"secNameCn": name, "secCode": code}
            for code, name in rows
        ]
    return {
        "actionErrors": [],
        "actionMessages": [],
        "fieldErrors": {},
        "isPagination": "false",
        "jsonCallBack": spec.callback,
        "locale": "en",
        "pageHelp": {
            "beginPage": 0,
            "cacheSize": 1,
            "data": result,
            "endDate": None,
            "endPage": None,
            "objectResult": None,
            "pageCount": 1,
            "pageNo": 1,
            "pageSize": len(result),
            "pageSizeWithOutLimit": len(result),
            "searchDate": None,
            "sort": None,
            "startDate": None,
            "total": len(result),
        },
        "pageNo": None,
        "pageSize": None,
        "queryDate": "",
        "result": result,
        "securityCode": "",
        "sqlId": spec.sql_id,
        "texts": None,
        "type": spec.response_type,
        "validateCode": "",
    }


def _risk_raw(source_index: int, rows: list[tuple[str, str]]) -> bytes:
    spec = SOURCE_SPECS[source_index]
    payload = json.dumps(
        _risk_payload(source_index, rows),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{spec.callback}({payload})".encode("utf-8")


def _seal_risk_manifest(
    root: Path,
    *,
    main_rows: list[tuple[str, str]] | None = None,
    star_rows: list[tuple[str, str]] | None = None,
) -> tuple[SSERiskWarningManifestStore, str]:
    main = main_rows or [
        ("600053", "*ST九鼎"),
        ("600818", "ST中路"),
        ("900915", "ST中路B"),
    ]
    star = star_rows or [("688022", "*ST瀚川")]
    responses: dict[str, _Response] = {}
    for index, rows in enumerate((main, star)):
        url = build_source_request_url(SOURCE_SPECS[index])
        responses[url] = _Response(url=url, content=_risk_raw(index, rows))
    cas = SSERiskWarningRawCAS(root / "risk")
    artifact = SSERiskWarningSourceClient(
        cas=cas,
        session=_Session(responses),  # type: ignore[arg-type]
    ).fetch_current(retrieved_at=RISK_RETRIEVED_AT)
    store = SSERiskWarningManifestStore(cas)
    reference = store.seal(artifact)
    return store, reference.manifest_sha256


def _status_row(
    code: str,
    name: str,
    number: int,
    *,
    listed_at: str = "20200102",
    b_stock_code: str = "-",
    security_name: str | None = None,
    security_full_name: str | None = None,
    legal_name: str | None = None,
    state_code_stock: str = "8",
) -> dict[str, str]:
    star = code.startswith(("688", "689"))
    return {
        "AREA_NAME": "310000",
        "AREA_NAME_DESC": "上海市",
        "A_STOCK_CODE": code,
        "B_STOCK_CODE": b_stock_code,
        "COMPANY_ABBR": name,
        "COMPANY_ABBR_EN": f"COMPANY {code}",
        "COMPANY_CODE": code,
        "CSRC_CODE": "C",
        "CSRC_CODE_DESC": "制造业",
        "DELIST_DATE": "-",
        "FULL_NAME": legal_name or f"测试股份有限公司{code}",
        "FULL_NAME_IN_ENGLISH": f"TEST COMPANY {code}",
        "LIST_BOARD": "2" if star else "1",
        "LIST_DATE": listed_at,
        "NUM": str(number),
        "PRODUCT_STATUS": "   D  F  NY         " if star else "   S  F  N          ",
        "SEC_NAME_CN": security_name or name,
        "SEC_NAME_FULL": security_full_name or security_name or name,
        "STATE_CODE": "7",
        "STATE_CODE_STOCK": state_code_stock,
        "STOCK_TYPE": "8" if star else "1",
    }


def _status_payload(
    rows: list[dict[str, str]],
    *,
    page_no: int = 1,
    page_count: int = 1,
    total: int | None = None,
) -> dict[str, object]:
    total_rows = len(rows) if total is None else total
    return {
        "actionErrors": [],
        "actionMessages": [],
        "fieldErrors": {},
        "isPagination": "true",
        "jsonCallBack": None,
        "locale": "en",
        "pageHelp": {
            "beginPage": page_no,
            "cacheSize": 1,
            "data": rows,
            "endDate": None,
            "endPage": None,
            "objectResult": None,
            "pageCount": page_count,
            "pageNo": page_no,
            "pageSize": REQUEST_PAGE_SIZE,
            "pageSizeWithOutLimit": REQUEST_PAGE_SIZE,
            "searchDate": None,
            "sort": None,
            "startDate": None,
            "total": total_rows,
        },
        "pageNo": None,
        "pageSize": None,
        "queryDate": "",
        "result": rows,
        "securityCode": "",
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "texts": None,
        "type": "inParams",
        "validateCode": "",
    }


def _status_raw(
    rows: list[dict[str, str]],
    *,
    page_no: int = 1,
    page_count: int = 1,
    total: int | None = None,
    payload: dict[str, object] | None = None,
) -> bytes:
    value = payload or _status_payload(
        rows,
        page_no=page_no,
        page_count=page_count,
        total=total,
    )
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _base_status_rows(*, include_transition: bool = False) -> list[dict[str, str]]:
    rows = [
        _status_row("600053", "*ST九鼎", 1, listed_at="19970418"),
        _status_row(
            "600818",
            "ST中路",
            2,
            listed_at="19940128",
            b_stock_code="900915",
        ),
        _status_row(
            "688022",
            "*ST瀚川",
            3,
            listed_at="20190722",
            security_full_name="*ST瀚川智能",
        ),
    ]
    if include_transition:
        rows.append(
            _status_row(
                "688646",
                "ST逸飞",
                4,
                listed_at="20230728",
                security_name="逸飞激光",
                legal_name="武汉逸飞激光股份有限公司",
                state_code_stock="4",
            )
        )
    return rows


def _status_session(
    pages: list[list[dict[str, str]]],
    *,
    response_overrides: dict[int, dict[str, object]] | None = None,
) -> _Session:
    overrides = response_overrides or {}
    total = sum(len(page) for page in pages)
    responses: dict[str, _Response] = {}
    for page_no, rows in enumerate(pages, start=1):
        url = build_page_request_url(page_no)
        options = dict(overrides.get(page_no, {}))
        responses[url] = _Response(
            url=url,
            content=_status_raw(
                rows,
                page_no=page_no,
                page_count=len(pages),
                total=total,
            ),
            **options,
        )
    return _Session(responses)


def _transition_artifact(
    *,
    effective_date: str = "2026-08-13",
    code_alias: str = "688646.SH",
    new_name: str = "逸飞激光",
) -> SimpleNamespace:
    transition = SimpleNamespace(
        code_alias=code_alias,
        legal_name="武汉逸飞激光股份有限公司",
        old_name="ST逸飞",
        new_name=new_name,
        effective_date=effective_date,
    )
    return SimpleNamespace(
        source_contract={
            "ready": False,
            "status": TRANSITION_SOURCE_STATUS,
            "training_allowed": False,
            "trading_allowed": False,
        },
        transition=transition,
        to_dict=lambda: {"protocol_version": TRANSITION_PROTOCOL_VERSION},
    )


def _transition_store(root: Path) -> SSERiskWarningTransitionManifestStore:
    return SSERiskWarningTransitionManifestStore(
        SSERiskWarningTransitionCAS(root / "transition")
    )


def _fetch_with_transition(
    root: Path,
    *,
    risk_store: SSERiskWarningManifestStore,
    risk_manifest: str,
    rows: list[dict[str, str]],
    active_name: str = "active",
) -> tuple[
    object,
    SSERiskWarningTransitionManifestStore,
]:
    transition_store = _transition_store(root)
    with mock.patch.object(
        transition_store,
        "replay",
        return_value=_transition_artifact(),
    ):
        artifact = SSERiskWarningActiveIntervalsClient(
            cas=SSERiskWarningActiveIntervalsCAS(root / active_name),
            session=_status_session([rows]),  # type: ignore[arg-type]
        ).fetch_current(
            risk_warning_manifest_sha256=risk_manifest,
            risk_warning_store=risk_store,
            transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
            transition_store=transition_store,
            retrieved_at=STATUS_RETRIEVED_AT,
        )
    return artifact, transition_store


class SSERiskWarningActiveIntervalsTests(unittest.TestCase):
    def test_fetch_binds_exact_risk_set_and_transition_lag_to_sse_list_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            transition_store = _transition_store(root)
            session = _status_session([_base_status_rows(include_transition=True)])
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ):
                artifact = SSERiskWarningActiveIntervalsClient(
                    cas=SSERiskWarningActiveIntervalsCAS(root / "active"),
                    session=session,  # type: ignore[arg-type]
                ).fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                )

            self.assertEqual(len(session.calls), 1)
            _url, kwargs = session.calls[0]
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], 30.0)
            self.assertEqual(
                kwargs["headers"]["Referer"],
                "https://www.sse.com.cn/assortment/stock/list/share/",
            )
            self.assertEqual(
                [item.code_alias for item in artifact.intervals],
                ["600053.SH", "600818.SH", "688022.SH", "688646.SH"],
            )
            self.assertEqual(artifact.risk_warning_code_count, 3)
            self.assertEqual(artifact.transition_lag_codes, ("688646.SH",))
            self.assertEqual(
                artifact.transition_binding_state,
                TRANSITION_BINDING_LAG,
            )
            self.assertEqual(artifact.risk_warning_b_share_codes, ("900915.SH",))
            transition = artifact.intervals[-1]
            self.assertEqual(transition.listed_at, "2023-07-28")
            self.assertEqual(transition.valid_from, "2023-07-28")
            self.assertEqual(transition.event_type, "ACTIVE_LISTING")
            self.assertEqual(transition.name, "逸飞激光")
            self.assertEqual(
                transition.attributes["risk_binding_role"],
                "ADMITTED_TRANSITION_STATUS_LAG",
            )
            self.assertEqual(transition.attributes["state_code"], "7")
            self.assertEqual(transition.attributes["state_code_stock"], "4")
            self.assertEqual(
                artifact.statistics["state_marker_counts"],
                {"7|4": 1, "7|8": 3},
            )
            self.assertEqual(
                artifact.source_contract["risk_warning_state_marker"], "7|8"
            )
            self.assertEqual(
                artifact.source_contract["transition_lag_state_marker"], "7|4"
            )
            self.assertTrue(
                artifact.source_contract[
                    "state_marker_4_allowed_only_for_fixed_transition"
                ]
            )
            self.assertFalse(artifact.source_contract["ready"])
            self.assertEqual(artifact.source_contract["status"], SOURCE_STATUS)
            self.assertFalse(artifact.source_contract["training_allowed"])
            self.assertFalse(artifact.source_contract["trading_allowed"])
            self.assertTrue(
                artifact.source_contract["historical_master_integration_allowed"]
            )
            self.assertFalse(
                artifact.source_contract[
                    "transition_evidence_may_create_listing_intervals"
                ]
            )
            self.assertEqual(
                artifact.source_contract["transition_binding_state"],
                TRANSITION_BINDING_LAG,
            )
            self.assertEqual(
                artifact.statistics["transition_binding_state"],
                TRANSITION_BINDING_LAG,
            )

    def test_converged_transition_binds_state_without_creating_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            artifact, _transition_store_value = _fetch_with_transition(
                root,
                risk_store=risk_store,
                risk_manifest=risk_manifest,
                rows=_base_status_rows(),
                active_name="converged",
            )

            self.assertEqual(
                artifact.transition_binding_state,
                TRANSITION_BINDING_CONVERGED,
            )
            self.assertEqual(artifact.transition_lag_codes, ())
            self.assertNotIn(
                "688646.SH",
                [item.code_alias for item in artifact.intervals],
            )
            self.assertEqual(len(artifact.intervals), 3)
            self.assertEqual(
                artifact.source_contract["transition_binding_state"],
                TRANSITION_BINDING_CONVERGED,
            )
            self.assertEqual(
                artifact.statistics["transition_binding_state"],
                TRANSITION_BINDING_CONVERGED,
            )
            self.assertEqual(
                artifact.statistics["state_marker_counts"],
                {"7|8": 3},
            )

    def test_unexplained_extra_missing_risk_and_b_share_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            transition_store = _transition_store(root)
            cases = (
                (
                    [
                        *_base_status_rows(include_transition=True),
                        _status_row("688999", "ST未知", 5),
                    ],
                    "does not exactly explain status-7 extras",
                ),
                (
                    _base_status_rows()[:-1],
                    "missing admitted risk-warning codes",
                ),
                (
                    [
                        _status_row("600053", "*ST九鼎", 1),
                        _status_row("600818", "ST中路", 2),
                        _status_row("688022", "*ST瀚川", 3),
                    ],
                    "B-share identities do not match",
                ),
            )
            for rows, pattern in cases:
                with self.subTest(pattern=pattern):
                    client = SSERiskWarningActiveIntervalsClient(
                        cas=SSERiskWarningActiveIntervalsCAS(
                            root / f"active-{len(pattern)}"
                        ),
                        session=_status_session([rows]),  # type: ignore[arg-type]
                    )
                    with mock.patch.object(
                        transition_store,
                        "replay",
                        return_value=_transition_artifact(),
                    ), self.assertRaisesRegex(
                        SSERiskWarningActiveIntervalsBlockedError,
                        pattern,
                    ):
                        client.fetch_current(
                            risk_warning_manifest_sha256=risk_manifest,
                            risk_warning_store=risk_store,
                            transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                            transition_store=transition_store,
                            retrieved_at=STATUS_RETRIEVED_AT,
                        )

    def test_transition_must_exactly_explain_effective_status_lag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            transition_store = _transition_store(root)
            cases = (
                (
                    _transition_artifact(effective_date="2026-08-14"),
                    "fixed admitted transition",
                ),
                (
                    _transition_artifact(code_alias="688647.SH"),
                    "fixed admitted transition",
                ),
                (
                    _transition_artifact(new_name="伪造名称"),
                    "fixed admitted transition",
                ),
            )
            for index, (transition, pattern) in enumerate(cases):
                with self.subTest(pattern=pattern), mock.patch.object(
                    transition_store,
                    "replay",
                    return_value=transition,
                ):
                    client = SSERiskWarningActiveIntervalsClient(
                        cas=SSERiskWarningActiveIntervalsCAS(
                            root / f"transition-active-{index}"
                        ),
                        session=_status_session(
                            [_base_status_rows(include_transition=True)]
                        ),  # type: ignore[arg-type]
                    )
                    with self.assertRaisesRegex(
                        SSERiskWarningActiveIntervalsBlockedError,
                        pattern,
                    ):
                        client.fetch_current(
                            risk_warning_manifest_sha256=risk_manifest,
                            risk_warning_store=risk_store,
                            transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                            transition_store=transition_store,
                            retrieved_at=STATUS_RETRIEVED_AT,
                        )

            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ):
                converged = SSERiskWarningActiveIntervalsClient(
                    cas=SSERiskWarningActiveIntervalsCAS(root / "converged-transition"),
                    session=_status_session([_base_status_rows()]),  # type: ignore[arg-type]
                ).fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                )
            self.assertEqual(
                converged.transition_binding_state,
                TRANSITION_BINDING_CONVERGED,
            )

    def test_only_fixed_transition_may_use_normal_stock_state_inside_status_7(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            transition_store = _transition_store(root)

            risk_rows = _base_status_rows(include_transition=True)
            risk_rows[0]["STATE_CODE_STOCK"] = "4"
            with self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "state markers changed for 600053",
            ):
                parse_status_page(_status_raw(risk_rows), expected_page_no=1)

            wrong_state_code = _base_status_rows(include_transition=True)
            wrong_state_code[0]["STATE_CODE"] = "2"
            with self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "state markers changed for 600053",
            ):
                parse_status_page(_status_raw(wrong_state_code), expected_page_no=1)

            stale_transition_marker = _base_status_rows(include_transition=True)
            stale_transition_marker[-1]["STATE_CODE_STOCK"] = "8"
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ), self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "transition identity does not match",
            ):
                SSERiskWarningActiveIntervalsClient(
                    cas=SSERiskWarningActiveIntervalsCAS(root / "stale-marker"),
                    session=_status_session([stale_transition_marker]),  # type: ignore[arg-type]
                ).fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                )

    def test_two_page_client_closes_page_sequence_and_total(self) -> None:
        main_rows = [
            (f"600{number:03d}", f"ST测试{number:03d}")
            for number in range(1_000)
        ] + [
            (f"601{number:03d}", f"ST样本{number:03d}")
            for number in range(1_000)
        ]
        star_rows = [("688022", "*ST瀚川")]
        status_rows = [
            _status_row(code, name, index, listed_at="20200102")
            for index, (code, name) in enumerate(
                [*main_rows, *star_rows], start=1
            )
        ]
        self.assertEqual(len(status_rows), 2_001)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(
                root,
                main_rows=main_rows,
                star_rows=star_rows,
            )
            transition_store = _transition_store(root)
            session = _status_session(
                [status_rows[:REQUEST_PAGE_SIZE], status_rows[REQUEST_PAGE_SIZE:]]
            )
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ):
                artifact = SSERiskWarningActiveIntervalsClient(
                    cas=SSERiskWarningActiveIntervalsCAS(root / "active"),
                    session=session,  # type: ignore[arg-type]
                ).fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                )
            self.assertEqual(len(session.calls), 2)
            self.assertEqual(len(artifact.intervals), 2_001)
            self.assertEqual(artifact.statistics["page_count"], 2)
            self.assertEqual(artifact.raw_responses[1].response_summary["row_count"], 1)

    def test_parser_rejects_schema_order_pagination_and_duplicate_json_keys(self) -> None:
        rows = _base_status_rows()
        cases: list[tuple[bytes, str]] = []

        extra_top = _status_payload(rows)
        extra_top["unexpected"] = True
        cases.append((_status_raw(rows, payload=extra_top), "top-level schema drift"))

        diverged = _status_payload(rows)
        page = diverged["pageHelp"]
        assert isinstance(page, dict)
        page["data"] = rows[:-1]
        cases.append(
            (_status_raw(rows, payload=diverged), "result and pageHelp.data diverged")
        )

        reversed_rows = list(reversed(rows))
        for number, row in enumerate(reversed_rows, start=1):
            row["NUM"] = str(number)
        cases.append((_status_raw(reversed_rows), "not strictly ordered"))

        incomplete = _status_payload(rows, page_count=2, total=2_001)
        cases.append((_status_raw(rows, payload=incomplete), "row count is incomplete"))

        changed_row = [dict(row) for row in rows]
        changed_row[0]["UNEXPECTED"] = "x"
        cases.append((_status_raw(changed_row), "row schema drift"))

        duplicate_key = (
            b'{"actionErrors":[],"actionErrors":[],"actionMessages":[]}'
        )
        cases.append((duplicate_key, "duplicate JSON key"))

        for raw, pattern in cases:
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(
                    SSERiskWarningActiveIntervalsBlockedError,
                    pattern,
                ):
                    parse_status_page(raw, expected_page_no=1)

    def test_http_hash_and_official_origin_contracts_fail_closed(self) -> None:
        rows = _base_status_rows()
        first_url = build_page_request_url(1)
        cases = (
            ({1: {"status_code": 503}}, "HTTP 503"),
            ({1: {"content_type": "text/html"}}, "content type changed"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            transition_store = _transition_store(root)
            for overrides, pattern in cases:
                with self.subTest(pattern=pattern):
                    client = SSERiskWarningActiveIntervalsClient(
                        cas=SSERiskWarningActiveIntervalsCAS(
                            root / f"http-{len(pattern)}"
                        ),
                        session=_status_session(
                            [rows], response_overrides=overrides
                        ),  # type: ignore[arg-type]
                    )
                    with mock.patch.object(
                        transition_store,
                        "replay",
                        return_value=_transition_artifact(),
                    ), self.assertRaisesRegex(
                        SSERiskWarningActiveIntervalsBlockedError,
                        pattern,
                    ):
                        client.fetch_current(
                            risk_warning_manifest_sha256=risk_manifest,
                            risk_warning_store=risk_store,
                            transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                            transition_store=transition_store,
                            retrieved_at=STATUS_RETRIEVED_AT,
                        )

            redirected = _status_session([rows])
            redirected.responses[first_url].url = (
                "https://evil.invalid/commonQuery.do?COMPANY_STATUS=7"
            )
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ), self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "request origin changed",
            ):
                SSERiskWarningActiveIntervalsClient(
                    cas=SSERiskWarningActiveIntervalsCAS(root / "redirect"),
                    session=redirected,  # type: ignore[arg-type]
                ).fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                )

            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ), self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "GET failed closed",
            ):
                SSERiskWarningActiveIntervalsClient(
                    cas=SSERiskWarningActiveIntervalsCAS(root / "timeout"),
                    session=_TimeoutSession(),  # type: ignore[arg-type]
                ).fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                )

            session = _status_session([rows])
            client = SSERiskWarningActiveIntervalsClient(
                cas=SSERiskWarningActiveIntervalsCAS(root / "hash"),
                session=session,  # type: ignore[arg-type]
            )
            raw = session.responses[first_url].content
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ), self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "source hash mismatch",
            ):
                client.fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                    expected_page_hashes={1: "0" * 64},
                )
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ), self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "unused expected page hashes",
            ):
                client.fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                    expected_page_hashes={
                        1: hashlib.sha256(raw).hexdigest(),
                        2: "0" * 64,
                    },
                )

    def test_manifest_cold_replays_raw_pages_and_risk_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            transition_store = _transition_store(root)
            active_cas = SSERiskWarningActiveIntervalsCAS(root / "active")
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ):
                artifact = SSERiskWarningActiveIntervalsClient(
                    cas=active_cas,
                    session=_status_session([_base_status_rows()]),  # type: ignore[arg-type]
                ).fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                )
            store = SSERiskWarningActiveIntervalsManifestStore(
                active_cas,
                risk_warning_store=risk_store,
                transition_store=transition_store,
            )
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ):
                reference = store.seal(artifact)
                self.assertEqual(reference.protocol_version, PROTOCOL_VERSION)
                replayed = store.replay(reference.manifest_sha256)
            self.assertEqual(replayed.to_dict(), artifact.to_dict())

            manifest = json.loads(Path(reference.object_path).read_bytes())
            manifest["statistics"]["interval_count"] += 1
            tampered = json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            tampered_hash, _path = active_cas.put_blob(tampered)
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ), self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "statistics do not match",
            ):
                store.replay(tampered_hash)

            first_raw_path = Path(replayed.raw_responses[0].object_path)
            first_raw_path.write_bytes(b"tampered")
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ), self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "CAS object hash mismatch",
            ):
                store.replay(reference.manifest_sha256)

    def test_manifest_cold_replays_and_binds_both_transition_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            transition_store = _transition_store(root)
            artifacts = {}
            for suffix, rows, expected_state in (
                (
                    "lag",
                    _base_status_rows(include_transition=True),
                    TRANSITION_BINDING_LAG,
                ),
                (
                    "converged",
                    _base_status_rows(),
                    TRANSITION_BINDING_CONVERGED,
                ),
            ):
                with self.subTest(state=expected_state):
                    active_cas = SSERiskWarningActiveIntervalsCAS(
                        root / f"active-{suffix}"
                    )
                    with mock.patch.object(
                        transition_store,
                        "replay",
                        return_value=_transition_artifact(),
                    ):
                        artifact = SSERiskWarningActiveIntervalsClient(
                            cas=active_cas,
                            session=_status_session([rows]),  # type: ignore[arg-type]
                        ).fetch_current(
                            risk_warning_manifest_sha256=risk_manifest,
                            risk_warning_store=risk_store,
                            transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                            transition_store=transition_store,
                            retrieved_at=STATUS_RETRIEVED_AT,
                        )
                        store = SSERiskWarningActiveIntervalsManifestStore(
                            active_cas,
                            risk_warning_store=risk_store,
                            transition_store=transition_store,
                        )
                        reference = store.seal(artifact)
                        replayed = store.replay(reference.manifest_sha256)

                    self.assertEqual(replayed.to_dict(), artifact.to_dict())
                    self.assertEqual(replayed.transition_binding_state, expected_state)
                    self.assertEqual(
                        replayed.source_contract["transition_binding_state"],
                        expected_state,
                    )
                    self.assertEqual(
                        replayed.statistics["transition_binding_state"],
                        expected_state,
                    )
                    self.assertEqual(replayed.transition_code_alias, "688646.SH")
                    self.assertEqual(replayed.transition_new_name, "逸飞激光")
                    self.assertEqual(
                        replayed.transition_effective_date,
                        "2026-08-13",
                    )
                    artifacts[expected_state] = replayed

            self.assertNotEqual(
                artifacts[TRANSITION_BINDING_LAG].source_snapshot_sha256,
                artifacts[TRANSITION_BINDING_CONVERGED].source_snapshot_sha256,
            )
            self.assertNotEqual(
                artifacts[TRANSITION_BINDING_LAG].logical_content_sha256,
                artifacts[TRANSITION_BINDING_CONVERGED].logical_content_sha256,
            )

    def test_manifest_rejects_transition_state_identity_and_hash_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_manifest = _seal_risk_manifest(root)
            transition_store = _transition_store(root)
            active_cas = SSERiskWarningActiveIntervalsCAS(root / "active")
            with mock.patch.object(
                transition_store,
                "replay",
                return_value=_transition_artifact(),
            ):
                artifact = SSERiskWarningActiveIntervalsClient(
                    cas=active_cas,
                    session=_status_session([_base_status_rows()]),  # type: ignore[arg-type]
                ).fetch_current(
                    risk_warning_manifest_sha256=risk_manifest,
                    risk_warning_store=risk_store,
                    transition_manifest_sha256=TRANSITION_MANIFEST_SHA256,
                    transition_store=transition_store,
                    retrieved_at=STATUS_RETRIEVED_AT,
                )
                store = SSERiskWarningActiveIntervalsManifestStore(
                    active_cas,
                    risk_warning_store=risk_store,
                    transition_store=transition_store,
                )
                reference = store.seal(artifact)

            original = json.loads(Path(reference.object_path).read_bytes())
            cases = (
                (
                    lambda payload: payload.__setitem__(
                        "transition_binding_state", TRANSITION_BINDING_LAG
                    ),
                    "binding state",
                ),
                (
                    lambda payload: payload["source_contract"].__setitem__(
                        "transition_binding_state", TRANSITION_BINDING_LAG
                    ),
                    "source contract changed",
                ),
                (
                    lambda payload: payload["statistics"].__setitem__(
                        "transition_binding_state", TRANSITION_BINDING_LAG
                    ),
                    "statistics do not match",
                ),
                (
                    lambda payload: payload.__setitem__(
                        "transition_new_name", "伪造名称"
                    ),
                    "transition identity",
                ),
                (
                    lambda payload: payload.__setitem__(
                        "logical_content_sha256", "0" * 64
                    ),
                    "logical content hash mismatch",
                ),
                (
                    lambda payload: payload.__setitem__(
                        "source_snapshot_sha256", "0" * 64
                    ),
                    "source snapshot hash mismatch",
                ),
            )
            for mutate, pattern in cases:
                with self.subTest(pattern=pattern):
                    payload = json.loads(json.dumps(original, ensure_ascii=False))
                    mutate(payload)
                    tampered = json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    tampered_hash, _path = active_cas.put_blob(tampered)
                    with mock.patch.object(
                        transition_store,
                        "replay",
                        return_value=_transition_artifact(),
                    ), self.assertRaisesRegex(
                        SSERiskWarningActiveIntervalsBlockedError,
                        pattern,
                    ):
                        store.replay(tampered_hash)

    def test_cas_rejects_corrupt_existing_object_and_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"official status-7 bytes"
            digest = hashlib.sha256(content).hexdigest()
            object_path = root / "sha256" / digest[:2] / digest
            object_path.parent.mkdir(parents=True)
            object_path.write_bytes(b"corrupt")
            cas = SSERiskWarningActiveIntervalsCAS(root)
            with self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "collision or corruption",
            ):
                cas.put_blob(content)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            cas_root = root / "cas"
            try:
                cas_root.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")
            with self.assertRaisesRegex(
                SSERiskWarningActiveIntervalsBlockedError,
                "root is a link or reparse point",
            ):
                SSERiskWarningActiveIntervalsCAS(cas_root).put_blob(b"payload")


if __name__ == "__main__":
    unittest.main()
