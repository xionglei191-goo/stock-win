from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import research_platform.szse_code_change_events as szse_events
import research_platform.bse_current_delisting_events as bse_current
import research_platform.historical_security_master as master_module
import research_platform.pending_listing_source as pending_listing
import research_platform.security_master_observation as master_observation
import research_platform.tests.test_sse_risk_warning_active_intervals as status7_fixtures
from research_platform.__main__ import build_parser
from research_platform.early_winner_research import ResearchDataBlockedError
from research_platform.early_winner_v4_research import EarlyWinnerV4ResearchService
from research_platform.historical_security_master import (
    BSE_CODE_MAPPING_URL,
    BSE_GENERAL_SWITCH_DATE,
    BSE_TERMINATION_EVENT_LOGICAL_SHA256,
    BSE_TERMINATION_EVENT_MANIFEST_SHA256,
    BSE_TERMINATION_EVENT_PROTOCOL_VERSION,
    QUALITY_POLICY_VERSION,
    SSE_ACTIVE_API_URL,
    SZSE_CODE_CHANGE_SOURCE_NAME,
    HistoricalSecurityMasterBlockedError,
    HistoricalSecurityMasterBuilder,
    HistoricalSecurityMasterStore,
    OfficialSecurityMasterClient,
    ParsedOfficialSource,
    SecurityMasterRecord,
    build_quality_report,
    build_sse_active_page_bundle,
    integrate_bse_current_delisting_manifest,
    integrate_bse_termination_event_manifest,
    integrate_szse_code_change_artifacts,
    make_transfer_records,
    parse_bse_code_mapping_html,
    parse_sse_active_json,
    parse_sse_delist_json,
    parse_szse_active_xlsx,
    parse_szse_delist_xlsx,
    publish_historical_security_master,
    records_active_on,
    validate_security_master_records,
)
from research_platform.sse_risk_warning_source import (
    PROTOCOL_VERSION as SSE_RISK_WARNING_PROTOCOL_VERSION,
    SOURCE_SPECS as SSE_RISK_WARNING_SOURCE_SPECS,
    SSERiskWarningManifestStore,
    SSERiskWarningRawCAS,
    SSERiskWarningSourceClient,
    build_source_request_url as build_sse_risk_warning_request_url,
)
from research_platform.sse_risk_warning_active_intervals import (
    SSERiskWarningActiveIntervalsCAS,
    SSERiskWarningActiveIntervalsClient,
    SSERiskWarningActiveIntervalsManifestStore,
)
from research_platform.szse_code_change_events import (
    SOURCE_CONTRACT_UNADMITTED,
    SZSEDisclosureCAS,
    parse_szse_code_change_pdf,
)
from research_platform.storage import Database
from research_platform.tests.helpers import temporary_config
from research_platform.tests import test_pending_listing_source as pending_fixtures


RETRIEVED_AT = "2026-08-12T12:00:00+08:00"
PENDING_FIXTURE_NOW = datetime(
    2026, 8, 13, 1, 29, 17, tzinfo=timezone(timedelta(hours=8))
)
BSE_CURRENT_FIXTURE_NOW = datetime(
    2026, 8, 13, 1, 48, 34, tzinfo=timezone(timedelta(hours=8))
)


def _fixture_tdx_names(
    active_codes: list[str] | tuple[str, ...],
) -> dict[str, str]:
    return {
        code: ("逸飞激光" if code == "688646.SH" else f"Fixture {code}")
        for code in sorted(active_codes)
    }


@contextmanager
def _admitted_current_observation(active_codes: list[str] | tuple[str, ...]):
    tdx_observation = master_observation.TDXAShareObservation.capture(
        _fixture_tdx_names(active_codes),
        observed_at=RETRIEVED_AT,
    )
    canonical_codes = list(tdx_observation.codes)
    manifest_sha256 = "0" * 64
    metadata = {
        "protocol_version": master_observation.PROTOCOL_VERSION,
        "manifest_sha256": manifest_sha256,
        "logical_content_sha256": "1" * 64,
        "validated_at": RETRIEVED_AT,
        "as_of": RETRIEVED_AT,
        "tdx_observed_at": RETRIEVED_AT,
        "tdx_names": dict(tdx_observation.names),
        "tdx_code_count": tdx_observation.code_count,
        "tdx_code_set_sha256": tdx_observation.code_set_sha256,
        "tdx_identity_sha256": tdx_observation.identity_sha256,
        "pending_listing_manifest_sha256": (
            master_module.PENDING_LISTING_MANIFEST_SHA256
        ),
        "pending_listing_logical_content_sha256": (
            master_module.PENDING_LISTING_LOGICAL_SHA256
        ),
        "bse_current_delisting_manifest_sha256": (
            master_module.BSE_CURRENT_DELISTING_MANIFEST_SHA256
        ),
        "bse_current_delisting_logical_content_sha256": (
            master_module.BSE_CURRENT_DELISTING_LOGICAL_SHA256
        ),
        "freshness_required_at_publish": True,
        "immutable_replay_after_publish": True,
    }
    with patch.object(
            master_module,
            "_normalize_current_observation_reference",
            return_value=(
                type(
                    "SyntheticObservationBatch",
                    (),
                    {
                        "pending_listing": type(
                            "PendingEvidence",
                            (),
                            {
                                "manifest_sha256": metadata[
                                    "pending_listing_manifest_sha256"
                                ],
                                "logical_content_sha256": metadata[
                                    "pending_listing_logical_content_sha256"
                                ],
                            },
                        )(),
                        "bse_current_delisting": type(
                            "BSEEvidence",
                            (),
                            {
                                "manifest_sha256": metadata[
                                    "bse_current_delisting_manifest_sha256"
                                ],
                                "logical_content_sha256": metadata[
                                    "bse_current_delisting_logical_content_sha256"
                                ],
                            },
                        )(),
                        "validated_at": metadata["validated_at"],
                        "as_of": metadata["as_of"],
                        "tdx_a_share": tdx_observation,
                    },
                )(),
                metadata,
            ),
    ) as replay:
        yield manifest_sha256, replay


@contextmanager
def _real_shape_current_observation(
    root: Path,
    *,
    now: datetime,
    active_codes: tuple[str, ...] | None = None,
):
    """Seal a fresh observation with dynamic child digests in temporary CAS roots."""

    pending_source_root = master_observation.DEFAULT_PENDING_CAS_ROOT
    bse_source_root = master_observation.DEFAULT_BSE_CURRENT_DELISTING_CAS_ROOT
    for required in (
        pending_source_root,
        bse_source_root,
    ):
        if not required.is_dir():
            raise unittest.SkipTest("ignored official current-evidence CAS is absent")
    pending_root = root / "pending"
    bse_root = root / "bse"
    observation_root = root / "observations"
    shutil.copytree(pending_source_root, pending_root)
    shutil.copytree(bse_source_root, bse_root)

    pending_store = pending_listing.PendingListingManifestStore(
        pending_listing.PendingListingRawCAS(pending_root)
    )
    pending_artifact = pending_listing.PendingListingManifestStore(
        pending_listing.PendingListingRawCAS(pending_source_root)
    ).replay(master_module.PENDING_LISTING_MANIFEST_SHA256)
    pending_base = now - timedelta(minutes=2)
    pending_sources = tuple(
        replace(
            item,
            retrieved_at=(pending_base + timedelta(seconds=index)).isoformat(),
        )
        for index, item in enumerate(pending_artifact.raw_sources)
    )
    pending_artifact = replace(
        pending_artifact,
        retrieved_at=pending_sources[-1].retrieved_at,
        raw_sources=pending_sources,
    )
    pending_manifest_content = pending_listing._canonical_json_bytes(
        pending_listing._manifest_payload(pending_artifact)
    )
    pending_manifest_sha256 = hashlib.sha256(pending_manifest_content).hexdigest()
    pending_manifest_path = (
        pending_root
        / "sha256"
        / pending_manifest_sha256[:2]
        / pending_manifest_sha256
    )
    pending_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    pending_manifest_path.write_bytes(pending_manifest_content)
    pending_reference = pending_listing.PendingListingManifestReference(
        manifest_sha256=pending_manifest_sha256,
        byte_count=len(pending_manifest_content),
        cas_uri=f"sha256:{pending_manifest_sha256}",
        object_path=str(pending_manifest_path),
    )

    bse_store = bse_current.BSECurrentDelistingManifestStore(
        bse_current.BSECurrentDelistingCAS(bse_root)
    )
    bse_artifact = bse_current.BSECurrentDelistingManifestStore(
        bse_current.BSECurrentDelistingCAS(bse_source_root)
    ).replay(master_module.BSE_CURRENT_DELISTING_MANIFEST_SHA256)
    bse_base = now - timedelta(minutes=1)
    capture_index = 0
    notices = []
    for notice in bse_artifact.notices:
        attempts = []
        for attempt in notice.transport_attempts:
            attempts.append(
                replace(
                    attempt,
                    retrieved_at=(
                        bse_base + timedelta(seconds=capture_index)
                    ).isoformat(),
                )
            )
            capture_index += 1
        notices.append(replace(notice, transport_attempts=tuple(attempts)))
    pages = []
    for page in bse_artifact.catalogue_pages:
        pages.append(
            replace(
                page,
                retrieved_at=(
                    bse_base + timedelta(seconds=capture_index)
                ).isoformat(),
            )
        )
        capture_index += 1
    closure = replace(
        bse_artifact.catalogue_closure_probe,
        retrieved_at=(bse_base + timedelta(seconds=capture_index)).isoformat(),
    )
    parsed_pages = tuple(
        bse_current._catalogue_evidence_from_dict(page.to_dict(), cas=bse_store.cas)
        for page in pages
    )
    parsed_closure = bse_current._catalogue_evidence_from_dict(
        closure.to_dict(),
        cas=bse_store.cas,
    )
    bse_artifact = bse_current._build_artifact(
        notices=tuple(notices),
        pages=parsed_pages,
        closure=parsed_closure,
    )
    bse_manifest_content = bse_current._canonical_json_bytes(
        bse_artifact.to_manifest_dict()
    )
    bse_manifest_sha256 = hashlib.sha256(bse_manifest_content).hexdigest()
    bse_manifest_path = bse_root / "manifests" / f"{bse_manifest_sha256}.json"
    bse_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    bse_manifest_path.write_bytes(bse_manifest_content)
    bse_reference = bse_current.ManifestReference(
        manifest_sha256=bse_manifest_sha256,
        byte_count=len(bse_manifest_content),
        cas_uri=f"sha256:{bse_manifest_sha256}",
    )

    codes = active_codes or tuple(
        sorted(
            {
                *master_observation.PENDING_REQUIRED_CODES,
                "000001.SZ",
                "600000.SH",
                "920001.BJ",
            }
        )
    )
    policy = master_observation.SecurityMasterObservationPolicy(
        pending_cas_root=pending_root,
        bse_current_delisting_cas_root=bse_root,
        minimum_tdx_code_count=6,
    )
    with (
        patch.object(
            pending_listing.PendingListingManifestStore,
            "replay",
            return_value=pending_artifact,
        ),
        patch.object(
            bse_current.BSECurrentDelistingManifestStore,
            "replay",
            return_value=bse_artifact,
        ),
    ):
        batch = master_observation._assemble_security_master_observation(
            policy=policy,
            pending_manifest=master_observation.UnderlyingManifestReference(
                cas_root=pending_root,
                manifest_sha256=pending_reference.manifest_sha256,
                logical_content_sha256=pending_artifact.logical_content_sha256,
            ),
            bse_current_delisting_manifest=master_observation.UnderlyingManifestReference(
                cas_root=bse_root,
                manifest_sha256=bse_reference.manifest_sha256,
                logical_content_sha256=bse_artifact.logical_content_sha256,
            ),
            tdx_observation=master_observation.TDXAShareObservation.capture(
                _fixture_tdx_names(codes),
                observed_at=now - timedelta(seconds=5),
            ),
            as_of=now,
            validation_now=now,
        )
    observation_store = master_observation.SecurityMasterObservationStore(
        observation_root,
        policy=policy,
    )
    manifest_content = master_observation._canonical_json_bytes(
        batch.to_manifest_dict()
    )
    manifest_sha256 = hashlib.sha256(manifest_content).hexdigest()
    manifest_path = observation_root / "manifests" / f"{manifest_sha256}.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(manifest_content)
    reference = master_observation.ObservationManifestReference(
        manifest_sha256=manifest_sha256,
        byte_count=len(manifest_content),
        cas_uri=f"sha256:{manifest_sha256}",
        object_path=str(manifest_path),
    )
    with (
        patch.object(master_module, "PENDING_LISTING_STORE_ROOT", pending_root),
        patch.object(master_module, "BSE_CURRENT_DELISTING_STORE_ROOT", bse_root),
        patch.object(master_module, "CURRENT_OBSERVATION_STORE_ROOT", observation_root),
        patch.object(master_module, "CURRENT_OBSERVATION_MINIMUM_TDX_CODE_COUNT", 6),
        patch.object(master_module, "_current_wall_clock", return_value=now + timedelta(seconds=70)),
    ):
        yield reference, observation_store, batch, pending_artifact, bse_artifact


def _sse_active_row(code: str, *, company_abbr: str | None = None) -> dict[str, str]:
    return {
        "A_STOCK_CODE": code,
        "COMPANY_CODE": code,
        "COMPANY_ABBR": company_abbr or f"测试{code}",
        "LIST_DATE": "19991110",
        "DELIST_DATE": "-",
    }


class _SseActivePageResponse:
    def __init__(self, url: str, content: bytes) -> None:
        self.status_code = 200
        self.url = url
        self.headers = {"Content-Type": "application/json; charset=UTF-8"}
        self.content = content


class _SseActivePageSession:
    def __init__(self, pages: dict[int, bytes]) -> None:
        self.pages = dict(pages)
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> _SseActivePageResponse:
        self.urls.append(url)
        query = parse_qs(urlparse(url).query)
        page_no = int(query["pageHelp.pageNo"][0])
        return _SseActivePageResponse(url, self.pages[page_no])


def _sse_bytes(
    rows: list[dict[str, str]] | None = None,
    *,
    page_count: int = 1,
    total: int | None = None,
) -> bytes:
    values = rows or [
        {
            "A_STOCK_CODE": "600432",
            "COMPANY_CODE": "600432",
            "COMPANY_ABBR": "吉恩退",
            "LIST_DATE": "20030905",
            "DELIST_DATE": "20180713",
        }
    ]
    payload = {
        "actionErrors": [],
        "fieldErrors": {},
        "pageHelp": {
            "pageNo": 1,
            "pageCount": page_count,
            "total": len(values) if total is None else total,
        },
        "result": values,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _sse_active_bytes(
    rows: list[dict[str, str]] | None = None,
    *,
    page_no: int = 1,
    page_count: int = 1,
    total: int | None = None,
) -> bytes:
    values = (
        [
            {
                "A_STOCK_CODE": "600000",
                "COMPANY_CODE": "600000",
                "COMPANY_ABBR": "浦发银行",
                "LIST_DATE": "19991110",
                "DELIST_DATE": "-",
            }
        ]
        if rows is None
        else rows
    )
    payload = {
        "actionErrors": [],
        "fieldErrors": {},
        "pageHelp": {
            "pageNo": page_no,
            "pageCount": page_count,
            "total": len(values) if total is None else total,
        },
        "result": values,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


SZSE_ACTIVE_TEST_HEADER = [
    "板块",
    "公司全称",
    "英文名称",
    "注册地址",
    "A股代码",
    "A股简称",
    "A股上市日期",
    "A股总股本",
    "A股流通股本",
    "B股代码",
    "B股 简 称",
    "B股上市日期",
    "B股总股本",
    "B股流通股本",
    "地      区",
    "省    份",
    "城     市",
    "所属行业",
    "公司网址",
    "目前尚未盈利",
    "具有表决权差异安排",
    "具有协议控制架构",
]


def _szse_active_row(
    code: str = "000001",
    *,
    name: str = "平安银行",
    company_name: str = "平安银行股份有限公司",
    listed_at: str = "1991-04-03",
    board: str = "主板",
) -> list[str]:
    return [
        board,
        company_name,
        "Test Company",
        "深圳市",
        code,
        name,
        listed_at,
        "100",
        "90",
        "",
        "",
        "",
        "",
        "",
        "华南",
        "广东",
        "深圳",
        "测试行业",
        "https://example.invalid",
        "否",
        "否",
        "否",
    ]


def _szse_active_xlsx_bytes(
    rows: list[list[str]] | None = None,
    *,
    header: list[str] | None = None,
) -> bytes:
    return _xlsx_bytes(
        rows or [_szse_active_row()],
        header=header or SZSE_ACTIVE_TEST_HEADER,
    )


def _xlsx_bytes(
    rows: list[list[str]] | None = None,
    *,
    header: list[str] | None = None,
) -> bytes:
    values = [
        header or ["证券代码", "证券简称", "上市日期", "终止上市日期"],
        *(rows or [["000511", "烯碳退", "1993-05-18", "2018-07-18"]]),
    ]
    xml_rows: list[str] = []
    for row_number, row in enumerate(values, start=1):
        cells: list[str] = []
        for column, value in enumerate(row):
            reference = f"{chr(ord('A') + column)}{row_number}"
            escaped = (
                value.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            cells.append(
                f'<c r="{reference}" t="inlineStr"><is><t>{escaped}</t></is></c>'
            )
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("xl/worksheets/sheet1.xml", (2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, sheet)
    return output.getvalue()


def _bse_bytes(
    rows: list[list[str]] | None = None,
    *,
    header: list[str] | None = None,
) -> bytes:
    values = rows or [["1", "*ST云创", "2021/8/26", "835305", "920305"]]
    headings = header or ["序号", "证券简称", "上市日期", "旧代码", "新代码"]
    html = ["<html><body><table><tr>"]
    html.extend(f"<th>{value}</th>" for value in headings)
    html.append("</tr>")
    for row in values:
        html.append("<tr>")
        html.extend(f"<td>{value}</td>" for value in row)
        html.append("</tr>")
    html.append("</table></body></html>")
    return "".join(html).encode("utf-8")


class _RiskWarningResponse:
    def __init__(self, url: str, content: bytes) -> None:
        self.status_code = 200
        self.url = url
        self.headers = {"Content-Type": "application/json;charset=UTF-8"}
        self.content = content


class _RiskWarningSession:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = dict(responses)

    def get(self, url: str, **_kwargs: object) -> _RiskWarningResponse:
        return _RiskWarningResponse(url, self.responses[url])


def _risk_warning_raw(
    source_index: int,
    rows: list[tuple[str, str]],
) -> bytes:
    spec = SSE_RISK_WARNING_SOURCE_SPECS[source_index]
    values = [
        {spec.code_field: code, spec.name_field: name}
        for code, name in sorted(rows)
    ]
    payload = {
        "actionErrors": [],
        "actionMessages": [],
        "fieldErrors": {},
        "isPagination": "false",
        "jsonCallBack": spec.callback,
        "locale": "en",
        "pageHelp": {
            "beginPage": 0,
            "cacheSize": 1,
            "data": values,
            "endDate": None,
            "endPage": None,
            "objectResult": None,
            "pageCount": 1,
            "pageNo": 1,
            "pageSize": len(values),
            "pageSizeWithOutLimit": len(values),
            "searchDate": None,
            "sort": None,
            "startDate": None,
            "total": len(values),
        },
        "pageNo": None,
        "pageSize": None,
        "queryDate": "",
        "result": values,
        "securityCode": "",
        "sqlId": spec.sql_id,
        "texts": None,
        "type": spec.response_type,
        "validateCode": "",
    }
    return (
        f"{spec.callback}("
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ")"
    ).encode("utf-8")


def _sealed_risk_warning_manifest(
    root: Path,
    *,
    main_rows: list[tuple[str, str]] | None = None,
    star_rows: list[tuple[str, str]] | None = None,
    retrieved_at: str = RETRIEVED_AT,
):
    rows = (
        main_rows or [("600053", "*ST测试")],
        star_rows or [("688022", "*ST科创")],
    )
    responses = {
        build_sse_risk_warning_request_url(spec): _risk_warning_raw(index, rows[index])
        for index, spec in enumerate(SSE_RISK_WARNING_SOURCE_SPECS)
    }
    cas = SSERiskWarningRawCAS(root / "risk-warning-cas")
    artifact = SSERiskWarningSourceClient(
        cas=cas,
        session=_RiskWarningSession(responses),  # type: ignore[arg-type]
    ).fetch_current(retrieved_at=retrieved_at)
    store = SSERiskWarningManifestStore(cas)
    reference = store.seal(artifact)
    return store, reference, artifact


def _sealed_status7_active_intervals(
    root: Path,
    *,
    main_rows: list[tuple[str, str]] | None = None,
    star_rows: list[tuple[str, str]] | None = None,
):
    risk_store, risk_reference, risk_artifact = _sealed_risk_warning_manifest(
        root,
        main_rows=main_rows,
        star_rows=star_rows,
        retrieved_at=status7_fixtures.RISK_RETRIEVED_AT,
    )
    risk_a_names = {
        item.code: item.name
        for item in risk_artifact.securities
        if item.share_class == "A"
    }
    risk_b_codes = sorted(
        item.code
        for item in risk_artifact.securities
        if item.share_class == "B"
    )
    status_rows = []
    for index, (code_alias, name) in enumerate(
        sorted(risk_a_names.items()), start=1
    ):
        code = code_alias.removesuffix(".SH")
        status_rows.append(
            status7_fixtures._status_row(
                code,
                name,
                index,
                listed_at=("20190722" if code.startswith("688") else "19970418"),
                b_stock_code=(risk_b_codes[0].removesuffix(".SH") if index == 1 and risk_b_codes else "-"),
            )
        )
    transition_store = status7_fixtures._transition_store(root)
    transition_store.replay = Mock(
        return_value=status7_fixtures._transition_artifact()
    )
    cas = SSERiskWarningActiveIntervalsCAS(root / "status7-active")
    artifact = SSERiskWarningActiveIntervalsClient(
        cas=cas,
        session=status7_fixtures._status_session([status_rows]),  # type: ignore[arg-type]
    ).fetch_current(
        risk_warning_manifest_sha256=risk_reference.manifest_sha256,
        risk_warning_store=risk_store,
        transition_manifest_sha256=(
            status7_fixtures.TRANSITION_MANIFEST_SHA256
        ),
        transition_store=transition_store,
        retrieved_at=status7_fixtures.STATUS_RETRIEVED_AT,
    )
    store = SSERiskWarningActiveIntervalsManifestStore(
        cas,
        risk_warning_store=risk_store,
        transition_store=transition_store,
    )
    reference = store.seal(artifact)
    return (
        risk_store,
        risk_reference,
        risk_artifact,
        store,
        reference,
        artifact,
    )


@contextmanager
def _pending_fixture_contract(
    specs: tuple[pending_listing.SZSEPendingDocumentSpec, ...],
    source_order: tuple[str, ...],
):
    with (
        patch.object(pending_listing, "SZSE_DOCUMENT_SPECS", specs),
        patch.object(pending_listing, "SOURCE_ORDER", source_order),
        patch.object(pending_listing, "MIN_CNINFO_MASTER_ROWS", 1),
        patch.object(pending_listing, "MIN_SZSE_ACTIVE_ROWS", 1),
        patch.object(
            pending_listing,
            "_extract_pdf_text",
            side_effect=pending_fixtures._fake_pdf_extract,
        ),
    ):
        yield


def _sealed_pending_listing_manifest(root: Path):
    specs, responses = pending_fixtures.PendingListingEndToEndTests()._fixture()
    source_order = tuple(
        [item.source_id for item in pending_listing.SSE_PENDING_SPECS]
        + [
            pending_listing.CNINFO_CURRENT_IPO_SOURCE_ID,
            pending_listing.CNINFO_MASTER_SOURCE_ID,
        ]
        + [
            pending_listing._cninfo_announcement_source_id(item.code)
            for item in specs
        ]
        + [item.source_id for item in specs]
        + [pending_listing.SZSE_ACTIVE_SOURCE_ID]
    )
    cas = pending_listing.PendingListingRawCAS(root)
    with _pending_fixture_contract(specs, source_order):
        artifact = pending_listing.PendingListingSourceClient(
            cas=cas,
            session=pending_fixtures._Session(responses),  # type: ignore[arg-type]
            clock=lambda: PENDING_FIXTURE_NOW,
        ).fetch_current()
        store = pending_listing.PendingListingManifestStore(cas)
        reference = store.seal(artifact)
    return store, reference, artifact, specs, source_order


def _current_reconciliation_sources() -> tuple[ParsedOfficialSource, ...]:
    return (
        parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
        parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
        parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
        parse_sse_active_json(
            _sse_active_bytes(
                rows=[
                    _sse_active_row("600000"),
                    {**_sse_active_row("688287"), "LIST_DATE": "20220525"},
                ]
            ),
            retrieved_at=RETRIEVED_AT,
        ),
        parse_szse_active_xlsx(
            _szse_active_xlsx_bytes(
                [
                    _szse_active_row("000001"),
                    _szse_active_row("301192", listed_at="2022-08-11"),
                    _szse_active_row("301321", listed_at="2022-08-18"),
                ]
            ),
            retrieved_at=RETRIEVED_AT,
        ),
    )


def _current_reconciliation_sources_with_bse_delist_targets(
) -> tuple[ParsedOfficialSource, ...]:
    sources = list(_current_reconciliation_sources())
    sources[2] = parse_bse_code_mapping_html(
        _bse_bytes(
            [
                ["1", "*ST浜戝垱", "2021/8/26", "835305", "920305"],
                ["2", "娴嬭瘯璇佸埜", "2021/11/15", "838680", "920680"],
            ]
        ),
        retrieved_at=RETRIEVED_AT,
    )
    return tuple(sources)


SZSE_CODE_CHANGE_TEXT = (
    "本公司证券简称由中航电测变更为中航成飞，"
    "证券代码由300114变更为302132。"
    "上述证券简称和证券代码变更自2025年2月17日起生效，"
    "属于同一上市公司证券身份的连续变更。"
)


def _szse_code_change_pdf(seed: bytes = b"security-master-fixture") -> bytes:
    return b"%PDF-1.7\n" + seed + b"\n%%EOF\n"


def _szse_extracted_text() -> szse_events._ExtractedText:
    return szse_events._ExtractedText(
        text=SZSE_CODE_CHANGE_TEXT,
        engine="pypdf",
        engine_version="TEST",
        page_count=3,
    )


def _admitted_szse_code_change_artifact(
    directory: str,
    *,
    raw_pdf: bytes | None = None,
) -> szse_events.SZSECodeChangeArtifact:
    content = raw_pdf or _szse_code_change_pdf()
    evidence = SZSEDisclosureCAS(Path(directory) / "szse-event-cas").capture(
        content,
        source_url=szse_events.PRIMARY_DISCLOSURE_URL,
        retrieved_at=RETRIEVED_AT,
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    return parse_szse_code_change_pdf(content, raw_evidence=evidence)


def _sources_with_szse_302_alias(
    *,
    include_extra_unresolved: bool = False,
) -> tuple[ParsedOfficialSource, ...]:
    active_rows = [
        _szse_active_row(
            "302132",
            name="中航成飞",
            company_name="中航成飞股份有限公司",
            listed_at="2010-08-27",
            board="创业板",
        )
    ]
    if include_extra_unresolved:
        active_rows.append(
            _szse_active_row(
                "302999",
                name="未解决别名",
                company_name="未解决别名股份有限公司",
                listed_at="2012-01-03",
                board="创业板",
            )
        )
    return (
        parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
        parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
        parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
        parse_sse_active_json(_sse_active_bytes(), retrieved_at=RETRIEVED_AT),
        parse_szse_active_xlsx(
            _szse_active_xlsx_bytes(active_rows), retrieved_at=RETRIEVED_AT
        ),
    )


class HistoricalSecurityMasterTests(unittest.TestCase):
    def test_publish_cli_accepts_only_one_observation_digest(self) -> None:
        args = build_parser().parse_args(
            [
                "security-master-publish",
                "--current-observation-manifest",
                "a" * 64,
            ]
        )
        self.assertEqual(args.command, "security-master-publish")
        self.assertEqual(args.current_observation_manifest, "a" * 64)
        self.assertFalse(hasattr(args, "runtime_dir"))
        self.assertFalse(hasattr(args, "tdx_active_codes"))
        self.assertFalse(hasattr(args, "pending_listing_manifest"))
        self.assertFalse(hasattr(args, "bse_current_delisting_manifest"))

    def test_production_publish_rejects_non_digest_before_any_source_access(
        self,
    ) -> None:
        with (
            patch.object(master_module, "_normalize_current_observation_reference") as replay,
            patch.object(master_module, "SSERiskWarningSourceClient") as risk,
            patch.object(master_module, "SZSECodeChangeClient") as alias,
            patch.object(master_module, "OfficialSecurityMasterClient") as official,
            self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "only one observation manifest SHA-256",
            ),
        ):
            publish_historical_security_master("not-a-digest")
        replay.assert_not_called()
        risk.assert_not_called()
        alias.assert_not_called()
        official.assert_not_called()

    def test_production_publish_derives_codes_and_fixed_evidence_internally(
        self,
    ) -> None:
        digest = "a" * 64
        observed_codes = ("000001.SZ", "600000.SH")
        tdx_observation = master_observation.TDXAShareObservation.capture(
            _fixture_tdx_names(observed_codes),
            observed_at=RETRIEVED_AT,
        )
        observation = type(
            "Observation",
            (),
            {"tdx_a_share": tdx_observation},
        )()
        risk_artifact = object()
        risk_reference = type(
            "RiskReference", (), {"manifest_sha256": "b" * 64}
        )()
        active_interval_artifact = object()
        active_interval_reference = type(
            "ActiveIntervalReference", (), {"manifest_sha256": "d" * 64}
        )()
        alias_artifact = type(
            "AliasArtifact",
            (),
            {"ready": True, "status": master_module.SZSE_CODE_CHANGE_ADMITTED},
        )()
        builder = Mock()
        builder.fetch_and_build.return_value = {
            "snapshot_id": "c" * 64,
            "published": True,
        }

        with (
            patch.object(
                master_module,
                "_normalize_current_observation_reference",
                return_value=(observation, {}),
            ) as replay,
            patch.object(
                master_module,
                "_current_wall_clock",
                return_value=datetime.fromisoformat(RETRIEVED_AT),
            ),
            patch.object(master_module, "SSERiskWarningRawCAS") as risk_cas_type,
            patch.object(master_module, "SSERiskWarningManifestStore") as risk_store_type,
            patch.object(master_module, "SSERiskWarningSourceClient") as risk_client_type,
            patch.object(master_module, "SSERiskWarningTransitionCAS") as transition_cas_type,
            patch.object(
                master_module,
                "SSERiskWarningTransitionManifestStore",
            ) as transition_store_type,
            patch.object(
                master_module,
                "SSERiskWarningActiveIntervalsCAS",
            ) as active_interval_cas_type,
            patch.object(
                master_module,
                "SSERiskWarningActiveIntervalsManifestStore",
            ) as active_interval_store_type,
            patch.object(
                master_module,
                "SSERiskWarningActiveIntervalsClient",
            ) as active_interval_client_type,
            patch.object(master_module, "SZSEDisclosureCAS") as alias_cas_type,
            patch.object(master_module, "SZSECodeChangeClient") as alias_client_type,
            patch.object(master_module, "HistoricalSecurityMasterStore") as master_store_type,
            patch.object(
                master_module,
                "HistoricalSecurityMasterBuilder",
                return_value=builder,
            ),
        ):
            risk_client_type.return_value.fetch_current.return_value = risk_artifact
            risk_store_type.return_value.seal.return_value = risk_reference
            active_interval_client_type.return_value.fetch_current.return_value = (
                active_interval_artifact
            )
            active_interval_store_type.return_value.seal.return_value = (
                active_interval_reference
            )
            alias_client_type.return_value.fetch_primary.return_value = alias_artifact
            result = publish_historical_security_master(digest)

        self.assertEqual(result["snapshot_id"], "c" * 64)
        replay.assert_called_once_with(digest, store=None, require_current=True)
        risk_cas_type.assert_called_once_with(master_module.SSE_RISK_WARNING_STORE_ROOT)
        transition_cas_type.assert_called_once_with(
            master_module.SSE_RISK_WARNING_TRANSITION_STORE_ROOT
        )
        transition_store_type.assert_called_once_with(
            transition_cas_type.return_value
        )
        transition_store_type.return_value.replay.assert_called_once_with(
            master_module.SSE_RISK_WARNING_TRANSITION_MANIFEST_SHA256
        )
        active_interval_cas_type.assert_called_once_with(
            master_module.SSE_RISK_WARNING_ACTIVE_INTERVALS_STORE_ROOT
        )
        active_interval_store_type.assert_called_once_with(
            active_interval_cas_type.return_value,
            risk_warning_store=risk_store_type.return_value,
            transition_store=transition_store_type.return_value,
        )
        active_interval_client_type.assert_called_once_with(
            cas=active_interval_cas_type.return_value
        )
        active_interval_client_type.return_value.fetch_current.assert_called_once_with(
            risk_warning_manifest_sha256=risk_reference.manifest_sha256,
            risk_warning_store=risk_store_type.return_value,
            transition_manifest_sha256=(
                master_module.SSE_RISK_WARNING_TRANSITION_MANIFEST_SHA256
            ),
            transition_store=transition_store_type.return_value,
            retrieved_at=RETRIEVED_AT,
        )
        active_interval_store_type.return_value.seal.assert_called_once_with(
            active_interval_artifact
        )
        alias_cas_type.assert_called_once_with(master_module.SZSE_CODE_CHANGE_STORE_ROOT)
        master_store_type.assert_called_once_with(
            master_module.HISTORICAL_SECURITY_MASTER_STORE_ROOT
        )
        alias_client_type.return_value.fetch_primary.assert_called_once_with(
            retrieved_at=RETRIEVED_AT,
            expected_sha256=master_module.SZSE_CODE_CHANGE_RAW_PDF_SHA256,
        )
        kwargs = builder.fetch_and_build.call_args.kwargs
        self.assertEqual(kwargs["tdx_active_codes"], observed_codes)
        self.assertEqual(kwargs["current_observation_manifest"], digest)
        self.assertIsNone(kwargs["current_observation_store"])
        self.assertEqual(
            kwargs["bse_termination_event_manifest_sha256"],
            BSE_TERMINATION_EVENT_MANIFEST_SHA256,
        )
        self.assertEqual(kwargs["sse_risk_warning_manifest"], "b" * 64)
        self.assertEqual(
            kwargs["sse_risk_warning_active_intervals_manifest"], "d" * 64
        )
        self.assertIs(
            kwargs["sse_risk_warning_active_intervals_store"],
            active_interval_store_type.return_value,
        )
        self.assertNotIn("pending_listing_manifest", kwargs)
        self.assertNotIn("bse_current_delisting_manifest", kwargs)

    def test_production_publish_rejects_a_quality_failed_attempt(self) -> None:
        digest = "a" * 64
        tdx_observation = master_observation.TDXAShareObservation.capture(
            _fixture_tdx_names(("600000.SH",)),
            observed_at=RETRIEVED_AT,
        )
        observation = type(
            "Observation",
            (),
            {"tdx_a_share": tdx_observation},
        )()
        alias_artifact = type(
            "AliasArtifact",
            (),
            {"ready": True, "status": master_module.SZSE_CODE_CHANGE_ADMITTED},
        )()
        builder = Mock()
        builder.fetch_and_build.return_value = {
            "manifest_hash": "c" * 64,
            "published": False,
            "gate": {
                "status": "ACTIVE_RECONCILIATION_FAILED",
                "detail": "one official current-state transition is unresolved",
            },
        }

        with (
            patch.object(
                master_module,
                "_normalize_current_observation_reference",
                return_value=(observation, {}),
            ),
            patch.object(
                master_module,
                "_current_wall_clock",
                return_value=datetime.fromisoformat(RETRIEVED_AT),
            ),
            patch.object(master_module, "SSERiskWarningRawCAS"),
            patch.object(master_module, "SSERiskWarningManifestStore") as risk_store,
            patch.object(master_module, "SSERiskWarningSourceClient") as risk_client,
            patch.object(master_module, "SSERiskWarningTransitionCAS"),
            patch.object(
                master_module,
                "SSERiskWarningTransitionManifestStore",
            ) as transition_store,
            patch.object(master_module, "SSERiskWarningActiveIntervalsCAS"),
            patch.object(
                master_module,
                "SSERiskWarningActiveIntervalsManifestStore",
            ) as active_interval_store,
            patch.object(
                master_module,
                "SSERiskWarningActiveIntervalsClient",
            ) as active_interval_client,
            patch.object(master_module, "SZSEDisclosureCAS"),
            patch.object(master_module, "SZSECodeChangeClient") as alias_client,
            patch.object(master_module, "HistoricalSecurityMasterStore"),
            patch.object(
                master_module,
                "HistoricalSecurityMasterBuilder",
                return_value=builder,
            ),
            self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "ACTIVE_RECONCILIATION_FAILED.*audit_manifest=" + "c" * 64,
            ),
        ):
            risk_client.return_value.fetch_current.return_value = object()
            risk_store.return_value.seal.return_value = type(
                "RiskReference", (), {"manifest_sha256": "b" * 64}
            )()
            active_interval_client.return_value.fetch_current.return_value = object()
            active_interval_store.return_value.seal.return_value = type(
                "ActiveIntervalReference", (), {"manifest_sha256": "d" * 64}
            )()
            alias_client.return_value.fetch_primary.return_value = alias_artifact
            publish_historical_security_master(digest)
        transition_store.return_value.replay.assert_called_once_with(
            master_module.SSE_RISK_WARNING_TRANSITION_MANIFEST_SHA256
        )
        kwargs = builder.fetch_and_build.call_args.kwargs
        self.assertEqual(
            kwargs["sse_risk_warning_active_intervals_manifest"], "d" * 64
        )
        self.assertIs(
            kwargs["sse_risk_warning_active_intervals_store"],
            active_interval_store.return_value,
        )

    def test_production_publish_wraps_builder_io_failure_as_blocked_data(self) -> None:
        digest = "a" * 64
        tdx_observation = master_observation.TDXAShareObservation.capture(
            _fixture_tdx_names(("600000.SH",)),
            observed_at=RETRIEVED_AT,
        )
        observation = type(
            "Observation",
            (),
            {"tdx_a_share": tdx_observation},
        )()
        alias_artifact = type(
            "AliasArtifact",
            (),
            {"ready": True, "status": master_module.SZSE_CODE_CHANGE_ADMITTED},
        )()
        builder = Mock()
        builder.fetch_and_build.side_effect = OSError("simulated pointer I/O")

        with (
            patch.object(
                master_module,
                "_normalize_current_observation_reference",
                return_value=(observation, {}),
            ),
            patch.object(
                master_module,
                "_current_wall_clock",
                return_value=datetime.fromisoformat(RETRIEVED_AT),
            ),
            patch.object(master_module, "SSERiskWarningRawCAS"),
            patch.object(master_module, "SSERiskWarningManifestStore") as risk_store,
            patch.object(master_module, "SSERiskWarningSourceClient") as risk_client,
            patch.object(master_module, "SSERiskWarningTransitionCAS"),
            patch.object(
                master_module,
                "SSERiskWarningTransitionManifestStore",
            ) as transition_store,
            patch.object(master_module, "SSERiskWarningActiveIntervalsCAS"),
            patch.object(
                master_module,
                "SSERiskWarningActiveIntervalsManifestStore",
            ) as active_interval_store,
            patch.object(
                master_module,
                "SSERiskWarningActiveIntervalsClient",
            ) as active_interval_client,
            patch.object(master_module, "SZSEDisclosureCAS"),
            patch.object(master_module, "SZSECodeChangeClient") as alias_client,
            patch.object(master_module, "HistoricalSecurityMasterStore"),
            patch.object(
                master_module,
                "HistoricalSecurityMasterBuilder",
                return_value=builder,
            ),
            self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "build or pointer commit failed closed.*simulated pointer I/O",
            ),
        ):
            risk_client.return_value.fetch_current.return_value = object()
            risk_store.return_value.seal.return_value = type(
                "RiskReference", (), {"manifest_sha256": "b" * 64}
            )()
            active_interval_client.return_value.fetch_current.return_value = object()
            active_interval_store.return_value.seal.return_value = type(
                "ActiveIntervalReference", (), {"manifest_sha256": "d" * 64}
            )()
            alias_client.return_value.fetch_primary.return_value = alias_artifact
            publish_historical_security_master(digest)
        transition_store.return_value.replay.assert_called_once_with(
            master_module.SSE_RISK_WARNING_TRANSITION_MANIFEST_SHA256
        )
        kwargs = builder.fetch_and_build.call_args.kwargs
        self.assertEqual(
            kwargs["sse_risk_warning_active_intervals_manifest"], "d" * 64
        )
        self.assertIs(
            kwargs["sse_risk_warning_active_intervals_store"],
            active_interval_store.return_value,
        )

    def test_official_active_sources_create_open_listing_intervals(self) -> None:
        sse = parse_sse_active_json(
            _sse_active_bytes(), retrieved_at=RETRIEVED_AT
        )
        szse = parse_szse_active_xlsx(
            _szse_active_xlsx_bytes(), retrieved_at=RETRIEVED_AT
        )

        self.assertEqual([record.code_alias for record in sse.records], ["600000.SH"])
        self.assertEqual([record.code_alias for record in szse.records], ["000001.SZ"])
        for record in (*sse.records, *szse.records):
            self.assertEqual(record.event_type, "ACTIVE_LISTING")
            self.assertIsNone(record.delisted_at)
            self.assertIsNone(record.valid_to)
            self.assertEqual(record.valid_from, record.listed_at)

    def test_szse_302_alias_is_observed_only_and_blocks_historical_completion(self) -> None:
        szse_active = parse_szse_active_xlsx(
            _szse_active_xlsx_bytes(
                [
                    _szse_active_row(
                        "302132",
                        name="中航电测",
                        company_name="中航电测仪器股份有限公司",
                        listed_at="2010-08-27",
                        board="创业板",
                    )
                ]
            ),
            retrieved_at=RETRIEVED_AT,
        )
        observation = szse_active.records[0]

        self.assertEqual(observation.code_alias, "302132.SZ")
        self.assertEqual(observation.board, "CHINEXT")
        self.assertEqual(observation.listed_at, "2010-08-27")
        self.assertEqual(observation.valid_from, "2026-08-12")
        self.assertEqual(observation.event_type, "ACTIVE_ALIAS_OBSERVATION")
        self.assertEqual(observation.attributes["previous_code_candidate"], "300114")
        self.assertTrue(observation.attributes["entity_chain_evidence_required"])
        self.assertNotEqual(observation.canonical_entity_id, "CN:SZSE:300114")
        self.assertEqual(records_active_on(szse_active.records, "2023-12-31"), ())
        self.assertEqual(
            [item.code_alias for item in records_active_on(szse_active.records, "2026-08-12")],
            ["302132.SZ"],
        )
        self.assertFalse(szse_active.statistics["code_alias_history_complete"])

        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_sse_active_json(
                _sse_active_bytes(
                    rows=[
                        _sse_active_row("600000"),
                        {
                            **_sse_active_row("688646"),
                            "COMPANY_ABBR": "逸飞激光",
                            "LIST_DATE": "20230728",
                        },
                    ]
                ),
                retrieved_at=RETRIEVED_AT,
            ),
            szse_active,
        )
        records = tuple(record for source in sources for record in source.records)
        gate = build_quality_report(
            records,
            sources,
            ["600000.SH", "302132.SZ", "920305.BJ"],
            expected_sse_szse_overlap=2,
        )["gate"]

        self.assertEqual(gate["status"], "SZSE_CODE_ALIAS_HISTORY_INCOMPLETE")
        self.assertTrue(
            gate["source_completeness"]["szse_active_listing_source_verified"]
        )
        self.assertFalse(
            gate["source_completeness"]["szse_active_listing_intervals"]
        )
        self.assertFalse(
            gate["source_completeness"]["szse_code_alias_history_complete"]
        )
        self.assertEqual(
            gate["reconciliation"]["szse_unresolved_alias_sample"],
            ["300114.SZ->302132.SZ"],
        )
        self.assertFalse(gate["ready"])

    def test_admitted_szse_event_replaces_observation_with_atomic_aliases(self) -> None:
        sources = _sources_with_szse_302_alias()
        records = tuple(record for source in sources for record in source.records)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            szse_events,
            "_extract_text_from_pdf",
            return_value=_szse_extracted_text(),
        ):
            artifact = _admitted_szse_code_change_artifact(directory)
            normalized_records, normalized_sources = (
                integrate_szse_code_change_artifacts(
                    records,
                    sources,
                    (artifact,),
                )
            )
            gate = build_quality_report(
                normalized_records,
                normalized_sources,
                ["600000.SH", "302132.SZ", "920305.BJ"],
                expected_sse_szse_overlap=2,
                szse_code_change_artifacts=(artifact,),
            )["gate"]

        alias_records = tuple(
            record
            for record in normalized_records
            if record.code_alias in {"300114.SZ", "302132.SZ"}
        )
        self.assertEqual(len(alias_records), 2)
        self.assertFalse(
            any(record.event_type == "ACTIVE_ALIAS_OBSERVATION" for record in normalized_records)
        )
        old = next(record for record in alias_records if record.code_alias == "300114.SZ")
        new = next(record for record in alias_records if record.code_alias == "302132.SZ")
        self.assertEqual(old.canonical_entity_id, "CN:SZSE:300114")
        self.assertEqual(new.canonical_entity_id, old.canonical_entity_id)
        self.assertEqual(old.listed_at, "2010-08-27")
        self.assertEqual(old.valid_from, "2010-08-27")
        self.assertEqual(old.valid_to, "2025-02-17")
        self.assertEqual(new.valid_from, "2025-02-17")
        self.assertIsNone(new.valid_to)
        self.assertEqual(
            [item.code_alias for item in records_active_on(alias_records, "2024-12-31")],
            ["300114.SZ"],
        )
        self.assertEqual(
            [item.code_alias for item in records_active_on(alias_records, "2025-02-17")],
            ["302132.SZ"],
        )
        self.assertEqual(gate["status"], "SOURCE_INCOMPLETE")
        self.assertTrue(
            gate["source_completeness"]["szse_code_alias_history_complete"]
        )
        self.assertTrue(
            gate["source_completeness"][
                "szse_code_change_event_source_verified"
            ]
        )
        self.assertEqual(
            gate["source_completeness"][
                "szse_code_change_event_protocol_version"
            ],
            szse_events.PROTOCOL_VERSION,
        )
        self.assertEqual(
            gate["source_completeness"][
                "szse_code_change_event_raw_pdf_sha256"
            ],
            artifact.raw_evidence.content_sha256,
        )
        self.assertEqual(
            gate["source_completeness"]["szse_code_change_event_text_sha256"],
            artifact.text_evidence.text_sha256,  # type: ignore[union-attr]
        )
        self.assertEqual(
            gate["source_completeness"]["szse_code_change_event_interval_count"],
            2,
        )
        self.assertEqual(gate["reconciliation"]["szse_unresolved_alias_count"], 0)
        self.assertEqual(
            gate["reconciliation"]["szse_unresolved_alias_resolved_count"], 1
        )
        event_source = next(
            source
            for source in normalized_sources
            if source.name == SZSE_CODE_CHANGE_SOURCE_NAME
        )
        self.assertEqual(event_source.statistics["interval_count"], 2)
        self.assertEqual(
            event_source.statistics["protocol_version"],
            szse_events.PROTOCOL_VERSION,
        )

    def test_builder_manifest_binds_szse_event_raw_text_and_intervals(self) -> None:
        active_xlsx = _szse_active_xlsx_bytes(
            [
                _szse_active_row(
                    "302132",
                    name="中航成飞",
                    company_name="中航成飞股份有限公司",
                    listed_at="2010-08-27",
                    board="创业板",
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            szse_events,
            "_extract_text_from_pdf",
            return_value=_szse_extracted_text(),
        ):
            artifact = _admitted_szse_code_change_artifact(directory)
            store = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            builder = HistoricalSecurityMasterBuilder(store)
            active_codes = sorted(
                {
                    "600000.SH",
                    "302132.SZ",
                    *master_module.PENDING_LISTING_RECONCILIATION_CODES,
                }
            )
            with _admitted_current_observation(active_codes) as (manifest, _):
                release = builder.build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "*ST娴滄垵鍨?", "2021/8/26", "835305", "920305"],
                            ["2", "濞村鐦拠浣稿煖", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    sse_active_json=_sse_active_bytes(),
                    szse_active_xlsx=active_xlsx,
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    szse_code_change_artifacts=(artifact,),
                    current_observation_manifest=manifest,
                )
            loaded = store.load_latest_attempt()
            event_manifest = next(
                source
                for source in loaded["manifest"]["sources"]
                if source["name"] == SZSE_CODE_CHANGE_SOURCE_NAME
            )
            master_metadata = loaded["manifest"]["artifacts"][
                "security_master_jsonl"
            ]
            master_rows = [
                json.loads(line)
                for line in Path(master_metadata["object_path"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        statistics = event_manifest["statistics"]
        self.assertEqual(release["gate"]["status"], "SOURCE_INCOMPLETE")
        self.assertEqual(loaded["manifest"]["quality_policy_version"], QUALITY_POLICY_VERSION)
        self.assertEqual(statistics["protocol_version"], szse_events.PROTOCOL_VERSION)
        self.assertEqual(statistics["raw_pdf_sha256"], artifact.raw_evidence.content_sha256)
        self.assertEqual(statistics["text_sha256"], artifact.text_evidence.text_sha256)  # type: ignore[union-attr]
        self.assertTrue(statistics["text_recomputed_from_raw"])
        self.assertEqual(statistics["interval_count"], 2)
        self.assertEqual(
            [item["code_alias"] for item in statistics["intervals"]],
            ["300114.SZ", "302132.SZ"],
        )
        self.assertEqual(event_manifest["content_hash"], statistics["raw_pdf_sha256"])
        event_rows = [
            row
            for row in master_rows
            if row["code_alias"] in {"300114.SZ", "302132.SZ"}
        ]
        self.assertEqual(len(event_rows), 2)
        self.assertFalse(
            any(row["event_type"] == "ACTIVE_ALIAS_OBSERVATION" for row in master_rows)
        )

    def test_unadmitted_mutated_duplicate_or_tampered_szse_event_fails_closed(self) -> None:
        sources = _sources_with_szse_302_alias()
        records = tuple(record for source in sources for record in source.records)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            szse_events,
            "_extract_text_from_pdf",
            return_value=_szse_extracted_text(),
        ):
            artifact = _admitted_szse_code_change_artifact(directory)
            unadmitted = replace(
                artifact,
                ready=False,
                status=SOURCE_CONTRACT_UNADMITTED,
            )
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError, "not admitted"
            ):
                integrate_szse_code_change_artifacts(
                    records, sources, (unadmitted,)
                )

            wrong_boundary = replace(
                artifact,
                intervals=(
                    artifact.intervals[0],
                    replace(artifact.intervals[1], valid_from="2025-02-18"),
                ),
            )
            with self.assertRaises(HistoricalSecurityMasterBlockedError):
                integrate_szse_code_change_artifacts(
                    records, sources, (wrong_boundary,)
                )

            wrong_entity = replace(
                artifact,
                intervals=(
                    replace(
                        artifact.intervals[0],
                        canonical_entity_id="CN:SZSE:WRONG",
                    ),
                    artifact.intervals[1],
                ),
            )
            with self.assertRaises(HistoricalSecurityMasterBlockedError):
                integrate_szse_code_change_artifacts(
                    records, sources, (wrong_entity,)
                )

            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError, "exactly once"
            ):
                integrate_szse_code_change_artifacts(
                    records, sources, (artifact, artifact)
                )

            Path(artifact.raw_evidence.object_path).write_bytes(
                _szse_code_change_pdf(b"tampered-after-admission")
            )
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError, "replay failed"
            ):
                integrate_szse_code_change_artifacts(
                    records, sources, (artifact,)
                )

    def test_extra_unresolved_szse_alias_remains_blocked_after_one_resolution(self) -> None:
        sources = _sources_with_szse_302_alias(include_extra_unresolved=True)
        records = tuple(record for source in sources for record in source.records)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            szse_events,
            "_extract_text_from_pdf",
            return_value=_szse_extracted_text(),
        ):
            artifact = _admitted_szse_code_change_artifact(directory)
            gate = build_quality_report(
                records,
                sources,
                ["600000.SH", "302132.SZ", "302999.SZ", "920305.BJ"],
                expected_sse_szse_overlap=2,
                szse_code_change_artifacts=(artifact,),
            )["gate"]

        self.assertEqual(gate["status"], "SZSE_CODE_ALIAS_HISTORY_INCOMPLETE")
        self.assertFalse(
            gate["source_completeness"]["szse_code_alias_history_complete"]
        )
        self.assertTrue(
            gate["source_completeness"][
                "szse_code_change_event_source_verified"
            ]
        )
        self.assertEqual(
            gate["reconciliation"]["szse_unresolved_alias_discovered_count"], 2
        )
        self.assertEqual(
            gate["reconciliation"]["szse_unresolved_alias_resolved_count"], 1
        )
        self.assertEqual(gate["reconciliation"]["szse_unresolved_alias_count"], 1)
        self.assertEqual(
            gate["reconciliation"]["szse_unresolved_alias_sample"],
            ["?->302999.SZ"],
        )

    def test_sse_active_client_fetches_all_pages_and_preserves_exact_evidence(self) -> None:
        page_one = _sse_active_bytes(
            [_sse_active_row("600000"), _sse_active_row("600001")],
            page_no=1,
            page_count=2,
            total=3,
        )
        page_two = _sse_active_bytes(
            [_sse_active_row("600002")],
            page_no=2,
            page_count=2,
            total=3,
        )
        session = _SseActivePageSession({1: page_one, 2: page_two})
        client = OfficialSecurityMasterClient(session=session)  # type: ignore[arg-type]

        parsed = client._fetch_sse_active(retrieved_at=RETRIEVED_AT)

        self.assertEqual(
            [record.code_alias for record in parsed.records],
            ["600000.SH", "600001.SH", "600002.SH"],
        )
        self.assertEqual(parsed.statistics["page_count"], 2)
        self.assertEqual(parsed.statistics["page_row_counts"], [2, 1])
        self.assertEqual(parsed.statistics["raw_evidence"], "sse-active-page-bundle-v1")
        self.assertEqual(parsed.source_hash, hashlib.sha256(parsed.raw_bytes).hexdigest())
        envelope = json.loads(parsed.raw_bytes.decode("utf-8"))
        self.assertEqual(
            [
                base64.b64decode(item["content_base64"], validate=True)
                for item in envelope["pages"]
            ],
            [page_one, page_two],
        )
        self.assertEqual(
            [item["content_sha256"] for item in envelope["pages"]],
            [hashlib.sha256(page_one).hexdigest(), hashlib.sha256(page_two).hexdigest()],
        )
        self.assertEqual(
            [parse_qs(urlparse(url).query)["pageHelp.pageNo"][0] for url in session.urls],
            ["1", "2"],
        )
        self.assertEqual(
            [
                parse_qs(urlparse(url).query)["pageHelp.beginPage"][0]
                for url in session.urls
            ],
            ["1", "2"],
        )
        replayed = parse_sse_active_json(
            parsed.raw_bytes,
            retrieved_at=RETRIEVED_AT,
            expected_hash=parsed.source_hash,
        )
        self.assertEqual(replayed.records, parsed.records)
        self.assertEqual(replayed.statistics, parsed.statistics)

    def test_sse_active_multipage_anomalies_fail_closed(self) -> None:
        page_one = _sse_active_bytes(
            [_sse_active_row("600000"), _sse_active_row("600001")],
            page_no=1,
            page_count=2,
            total=3,
        )

        cases = (
            (
                "metadata drifted",
                _sse_active_bytes(
                    [_sse_active_row("600002")],
                    page_no=2,
                    page_count=2,
                    total=4,
                ),
            ),
            (
                "sequence is discontinuous",
                _sse_active_bytes(
                    [_sse_active_row("600002")],
                    page_no=1,
                    page_count=2,
                    total=3,
                ),
            ),
            (
                "duplicate",
                _sse_active_bytes(
                    [_sse_active_row("600001")],
                    page_no=2,
                    page_count=2,
                    total=3,
                ),
            ),
        )
        for expected_message, page_two in cases:
            with self.subTest(expected_message=expected_message):
                client = OfficialSecurityMasterClient(
                    session=_SseActivePageSession({1: page_one, 2: page_two})
                )  # type: ignore[arg-type]
                with self.assertRaisesRegex(
                    HistoricalSecurityMasterBlockedError, expected_message
                ):
                    client._fetch_sse_active(retrieved_at=RETRIEVED_AT)

        invalid_last_page_client = OfficialSecurityMasterClient(
            session=_SseActivePageSession(
                {
                    1: _sse_active_bytes(
                        [_sse_active_row("600000"), _sse_active_row("600001")],
                        page_no=1,
                        page_count=2,
                        total=4,
                    ),
                    2: _sse_active_bytes(
                        [_sse_active_row("600002")],
                        page_no=2,
                        page_count=2,
                        total=4,
                    ),
                }
            )
        )  # type: ignore[arg-type]
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "row count"):
            invalid_last_page_client._fetch_sse_active(retrieved_at=RETRIEVED_AT)

    def test_sse_active_page_bundle_hash_tampering_fails_closed(self) -> None:
        first_url = SSE_ACTIVE_API_URL
        second_url = (
            SSE_ACTIVE_API_URL.replace("pageHelp.beginPage=1", "pageHelp.beginPage=2")
            .replace("pageHelp.pageNo=1", "pageHelp.pageNo=2")
        )
        bundle = build_sse_active_page_bundle(
            [
                (
                    first_url,
                    _sse_active_bytes(
                        [_sse_active_row("600000"), _sse_active_row("600001")],
                        page_no=1,
                        page_count=2,
                        total=3,
                    ),
                ),
                (
                    second_url,
                    _sse_active_bytes(
                        [_sse_active_row("600002")],
                        page_no=2,
                        page_count=2,
                        total=3,
                    ),
                ),
            ]
        )
        envelope = json.loads(bundle.decode("utf-8"))
        envelope["pages"][1]["content_base64"] = base64.b64encode(b"{}").decode(
            "ascii"
        )
        tampered = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "hash mismatch"):
            parse_sse_active_json(tampered, retrieved_at=RETRIEVED_AT)

    def test_delisted_security_exists_before_but_not_on_or_after_delisting(self) -> None:
        sse = parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT)
        szse = parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT)

        self.assertEqual(
            [record.code_alias for record in records_active_on(sse.records, "2018-07-12")],
            ["600432.SH"],
        )
        self.assertEqual(records_active_on(sse.records, "2018-07-13"), ())
        self.assertEqual(
            [record.code_alias for record in records_active_on(szse.records, "2018-07-17")],
            ["000511.SZ"],
        )
        self.assertEqual(records_active_on(szse.records, "2018-07-18"), ())

    def test_bse_old_and_new_aliases_have_non_overlapping_effective_intervals(self) -> None:
        parsed = parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT)

        before = records_active_on(parsed.records, "2023-06-30")
        after = records_active_on(parsed.records, BSE_GENERAL_SWITCH_DATE)

        self.assertEqual([record.code_alias for record in before], ["835305.BJ"])
        self.assertEqual([record.code_alias for record in after], ["920305.BJ"])
        self.assertEqual(before[0].canonical_entity_id, after[0].canonical_entity_id)
        self.assertEqual(before[0].listed_at, "2021-11-15")
        self.assertEqual(
            before[0].attributes["official_page_listed_at"], "2021-08-26"
        )

    def test_transfer_uses_one_entity_and_two_listing_intervals(self) -> None:
        records = make_transfer_records(
            canonical_entity_id="CN:ENTITY:GUANDIAN",
            from_exchange="BSE",
            from_code="832317",
            from_listed_at="2021-11-15",
            from_delisted_at="2022-05-24",
            to_exchange="SSE",
            to_code="688287",
            to_listed_at="2022-05-25",
            source_url="https://www.bse.cn/example",
            source_hash="a" * 64,
            retrieved_at=RETRIEVED_AT,
            name="观典防务",
        )

        self.assertEqual(
            [record.code_alias for record in records_active_on(records, "2022-05-23")],
            ["832317.BJ"],
        )
        self.assertEqual(records_active_on(records, "2022-05-24"), ())
        self.assertEqual(
            [record.code_alias for record in records_active_on(records, "2022-05-25")],
            ["688287.SH"],
        )
        self.assertEqual(len({record.canonical_entity_id for record in records}), 1)

    def test_sse_pagination_and_all_official_schema_drift_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            HistoricalSecurityMasterBlockedError, "pagination incomplete"
        ):
            parse_sse_delist_json(
                _sse_bytes(page_count=2, total=2), retrieved_at=RETRIEVED_AT
            )
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "schema drift"):
            parse_szse_delist_xlsx(
                _xlsx_bytes(header=["代码", "简称", "上市", "退市"]),
                retrieved_at=RETRIEVED_AT,
            )
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "schema drift"):
            parse_bse_code_mapping_html(
                _bse_bytes(header=["编号", "简称", "日期", "旧", "新"]),
                retrieved_at=RETRIEVED_AT,
            )

    def test_active_schema_pagination_duplicates_and_hash_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            HistoricalSecurityMasterBlockedError, "pagination incomplete"
        ):
            parse_sse_active_json(
                _sse_active_bytes(page_count=2, total=2),
                retrieved_at=RETRIEVED_AT,
            )
        drifted_header = list(SZSE_ACTIVE_TEST_HEADER)
        drifted_header[4] = "证券代码"
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "schema drift"):
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(header=drifted_header),
                retrieved_at=RETRIEVED_AT,
            )
        duplicate_sse = [
            {
                "A_STOCK_CODE": "600000",
                "COMPANY_CODE": "600000",
                "COMPANY_ABBR": "浦发银行",
                "LIST_DATE": "19991110",
                "DELIST_DATE": "-",
            },
            {
                "A_STOCK_CODE": "600000",
                "COMPANY_CODE": "600000",
                "COMPANY_ABBR": "浦发银行",
                "LIST_DATE": "19991110",
                "DELIST_DATE": "-",
            },
        ]
        duplicate_szse = [
            "主板",
            "平安银行股份有限公司",
            "Ping An Bank Co., Ltd.",
            "深圳市",
            "000001",
            "平安银行",
            "1991-04-03",
            "100",
            "90",
            "",
            "",
            "",
            "",
            "",
            "华南",
            "广东",
            "深圳",
            "银行",
            "https://example.invalid",
            "否",
            "否",
            "否",
        ]
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "duplicate"):
            parse_sse_active_json(
                _sse_active_bytes(duplicate_sse), retrieved_at=RETRIEVED_AT
            )
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "duplicate"):
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(
                    [duplicate_szse, list(duplicate_szse)]
                ),
                retrieved_at=RETRIEVED_AT,
            )
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "hash mismatch"):
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(),
                retrieved_at=RETRIEVED_AT,
                expected_hash="0" * 64,
            )

    def test_overlapping_alias_or_entity_interval_fails_closed(self) -> None:
        parsed = parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT)
        original = parsed.records[0]
        overlapping = replace(
            original,
            code_alias="600999.SH",
            event_type="DUPLICATE_ENTITY_EVIDENCE",
        )

        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "overlapping entity"):
            validate_security_master_records([original, overlapping])

    def test_content_addressed_release_is_deterministic_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            builder = HistoricalSecurityMasterBuilder(store)
            kwargs = {
                "sse_json": _sse_bytes(),
                "szse_xlsx": _xlsx_bytes(),
                "bse_mapping_html": _bse_bytes(
                    [
                        ["1", "*ST娴滄垵鍨?", "2021/8/26", "835305", "920305"],
                        ["2", "濞村鐦拠浣稿煖", "2021/11/15", "838680", "920680"],
                    ]
                ),
                "tdx_active_codes": ["600000.SH"],
                "retrieved_at": RETRIEVED_AT,
                "expected_sse_szse_overlap": 2,
            }
            with _admitted_current_observation(kwargs["tdx_active_codes"]) as (
                manifest,
                _,
            ):
                kwargs["current_observation_manifest"] = manifest
                first = builder.build_from_bytes(**kwargs)
                second = builder.build_from_bytes(**kwargs)
            release = store.load_latest_attempt()
            master_path = Path(
                release["manifest"]["artifacts"]["security_master_jsonl"][
                    "object_path"
                ]
            )
            master_path.write_bytes(b"tampered")
            corrupted_gate = store.load_gate()
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "content hash mismatch",
            ):
                store.load_latest_attempt()

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(
            first["gate"]["status"], "ACTIVE_INTERVAL_SOURCE_INCOMPLETE"
        )
        self.assertEqual(corrupted_gate["status"], "NOT_BUILT")
        self.assertFalse(corrupted_gate["ready"])

    def test_legacy_failed_current_pointer_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            builder = HistoricalSecurityMasterBuilder(store)
            active_codes = ["600000.SH"]
            with _admitted_current_observation(active_codes) as (manifest, _):
                result = builder.build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "*ST测试一", "2021/8/26", "835305", "920305"],
                            ["2", "测试二", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    current_observation_manifest=manifest,
                )
            self.assertFalse(result["published"])
            store.current.write_bytes(store.latest_attempt.read_bytes())

            gate = store.load_gate()
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "non-ready release",
            ):
                store.load_current_release()

        self.assertEqual(gate["status"], "ARTIFACT_INVALID")
        self.assertIn("non-ready release", gate["detail"])

    def test_ready_attempt_without_current_commit_stays_not_built(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            builder = HistoricalSecurityMasterBuilder(store)
            active_codes = ["600000.SH"]
            original_replace = store._atomic_replace
            original_quality_builder = master_module.build_quality_report

            def fail_current_commit(path: Path, content: bytes) -> None:
                if path == store.current:
                    raise OSError("simulated current-pointer crash")
                original_replace(path, content)

            def ready_quality(*args: object, **kwargs: object) -> dict[str, object]:
                report = original_quality_builder(*args, **kwargs)
                report["gate"]["ready"] = True
                report["gate"]["status"] = "READY"
                report["gate"]["detail"] = "synthetic promoted gate"
                report["gate"]["promotion_blocked"] = False
                report["promotion_blocked"] = False
                return report

            with (
                _admitted_current_observation(active_codes) as (manifest, _),
                patch.object(store, "_atomic_replace", side_effect=fail_current_commit),
                patch.object(
                    master_module,
                    "build_quality_report",
                    side_effect=ready_quality,
                ),
                # This test isolates the current-pointer commit point.  The
                # dedicated V15 admission regression covers forged READY
                # reports, so do not let that earlier layer mask the simulated
                # pointer failure here.
                patch.object(
                    store,
                    "_validate_v15_ready_admission",
                    return_value=None,
                ),
            ):
                with self.assertRaisesRegex(OSError, "current-pointer crash"):
                    builder.build_from_bytes(
                        sse_json=_sse_bytes(),
                        szse_xlsx=_xlsx_bytes(),
                        bse_mapping_html=_bse_bytes(
                            [
                                ["1", "*ST测试一", "2021/8/26", "835305", "920305"],
                                ["2", "测试二", "2021/11/15", "838680", "920680"],
                            ]
                        ),
                        tdx_active_codes=active_codes,
                        retrieved_at=RETRIEVED_AT,
                        expected_sse_szse_overlap=2,
                        current_observation_manifest=manifest,
                    )

            self.assertTrue(store.latest_attempt.is_file())
            self.assertFalse(store.current.exists())
            self.assertEqual(store.load_gate()["status"], "NOT_BUILT")
            self.assertEqual(
                store.load_latest_attempt()["quality_report"]["gate"]["status"],
                "READY",
            )

    def test_v15_ready_admission_rejects_forged_ready_without_required_sources(
        self,
    ) -> None:
        active_codes = ["600000.SH"]
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
        )
        records = tuple(record for source in sources for record in source.records)
        quality = build_quality_report(
            records,
            sources,
            active_codes,
            expected_sse_szse_overlap=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            store.current.parent.mkdir(parents=True, exist_ok=True)
            sentinel_current = b'{"sentinel":"existing-current-pointer"}'
            store.current.write_bytes(sentinel_current)
            with _admitted_current_observation(active_codes) as (
                manifest,
                replay,
            ):
                metadata = replay.return_value[1]
                completeness = quality["gate"]["source_completeness"]
                completeness.update(
                    {
                        "current_observation_source_verified": True,
                        "current_observation_protocol_version": metadata[
                            "protocol_version"
                        ],
                        "current_observation_manifest_sha256": metadata[
                            "manifest_sha256"
                        ],
                        "current_observation_logical_content_sha256": metadata[
                            "logical_content_sha256"
                        ],
                        "current_observation_validated_at": metadata[
                            "validated_at"
                        ],
                        "current_observation_as_of": metadata["as_of"],
                        "current_observation_tdx_observed_at": metadata[
                            "tdx_observed_at"
                        ],
                        "current_observation_tdx_code_count": metadata[
                            "tdx_code_count"
                        ],
                        "current_observation_tdx_code_set_sha256": metadata[
                            "tdx_code_set_sha256"
                        ],
                        "current_observation_tdx_identity_sha256": metadata[
                            "tdx_identity_sha256"
                        ],
                        "current_observation_pending_listing_manifest_sha256": metadata[
                            "pending_listing_manifest_sha256"
                        ],
                        "current_observation_pending_listing_logical_content_sha256": metadata[
                            "pending_listing_logical_content_sha256"
                        ],
                        "current_observation_bse_current_delisting_manifest_sha256": metadata[
                            "bse_current_delisting_manifest_sha256"
                        ],
                        "current_observation_bse_current_delisting_logical_content_sha256": metadata[
                            "bse_current_delisting_logical_content_sha256"
                        ],
                        "pending_listing_manifest_sha256": metadata[
                            "pending_listing_manifest_sha256"
                        ],
                        "pending_listing_logical_content_sha256": metadata[
                            "pending_listing_logical_content_sha256"
                        ],
                        "bse_current_delisting_manifest_sha256": metadata[
                            "bse_current_delisting_manifest_sha256"
                        ],
                        "bse_current_delisting_logical_content_sha256": metadata[
                            "bse_current_delisting_logical_content_sha256"
                        ],
                    }
                )
                quality["gate"].update(
                    {
                        "ready": True,
                        "status": "READY",
                        "detail": "caller-forged READY",
                        "promotion_blocked": False,
                    }
                )
                quality["promotion_blocked"] = False

                with self.assertRaisesRegex(
                    HistoricalSecurityMasterBlockedError,
                    "V15 READY admission",
                ):
                    store.publish(
                        sources=sources,
                        records=records,
                        quality_report=quality,
                        tdx_active_codes=active_codes,
                        current_observation_manifest=manifest,
                    )

            self.assertEqual(store.current.read_bytes(), sentinel_current)
            self.assertFalse(store.latest_attempt.exists())

    def test_v14_current_release_requires_observation_binding_and_identity_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            builder = HistoricalSecurityMasterBuilder(store)
            active_codes = ["600000.SH"]
            original_quality_builder = master_module.build_quality_report

            def ready_quality(*args: object, **kwargs: object) -> dict[str, object]:
                report = original_quality_builder(*args, **kwargs)
                report["gate"]["ready"] = True
                report["gate"]["status"] = "READY"
                report["gate"]["detail"] = "synthetic V14 ready gate"
                report["gate"]["promotion_blocked"] = False
                report["promotion_blocked"] = False
                return report

            def install_manifest(manifest: dict[str, object]) -> str:
                content = master_module._canonical_json_bytes(manifest)
                digest = hashlib.sha256(content).hexdigest()
                manifest_path = store.manifests / f"{digest}.json"
                store._atomic_write_exact(manifest_path, content)
                pointer = {
                    "snapshot_id": digest,
                    "manifest_hash": digest,
                    "manifest_path": str(manifest_path),
                    "protocol_version": master_module.PROTOCOL_VERSION,
                }
                store._atomic_replace(
                    store.current,
                    master_module._canonical_json_bytes(pointer),
                )
                return digest

            with (
                _admitted_current_observation(active_codes) as (manifest, _),
                patch.object(
                    master_module,
                    "build_quality_report",
                    side_effect=ready_quality,
                ),
                # Keep this test focused on observation/identity artifacts;
                # V15 source admission is exercised separately.
                patch.object(
                    store,
                    "_validate_v15_ready_admission",
                    return_value=None,
                ),
            ):
                published = builder.build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "BSE sample one", "2021/8/26", "835305", "920305"],
                            ["2", "BSE sample two", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    current_observation_manifest=manifest,
                )
                self.assertTrue(published["published"])
                valid_release = store.load_current_release()
                self.assertEqual(
                    valid_release["snapshot_id"], published["snapshot_id"]
                )

                variants = {
                    "missing_current_observation": lambda value: value.pop(
                        "current_observation"
                    ),
                    "missing_tdx_identity_snapshot": lambda value: value[
                        "artifacts"
                    ].pop("tdx_identity_snapshot"),
                }
                for label, mutate in variants.items():
                    with self.subTest(label=label):
                        altered = copy.deepcopy(valid_release["manifest"])
                        mutate(altered)
                        digest = install_manifest(altered)
                        self.assertEqual(
                            json.loads(store.current.read_text("utf-8"))[
                                "snapshot_id"
                            ],
                            digest,
                        )
                        gate = store.load_gate()
                        self.assertEqual(gate["status"], "ARTIFACT_INVALID")
                        with self.assertRaises(
                            HistoricalSecurityMasterBlockedError
                        ):
                            store.load_current_release()

    def test_store_publish_lock_rejects_a_competing_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            second = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            with first._exclusive_publish_lock():
                with self.assertRaisesRegex(
                    HistoricalSecurityMasterBlockedError,
                    "publication is already in progress",
                ):
                    with second._exclusive_publish_lock():
                        self.fail("competing publisher unexpectedly acquired the lock")

    @unittest.skipUnless(os.name == "nt", "Windows lock cleanup regression")
    def test_store_publish_lock_closes_handle_when_unlock_raises(self) -> None:
        import msvcrt

        real_locking = msvcrt.locking

        def flaky_unlock(file_descriptor: int, mode: int, size: int) -> None:
            if mode == msvcrt.LK_UNLCK:
                raise OSError("synthetic unlock failure")
            real_locking(file_descriptor, mode, size)

        with tempfile.TemporaryDirectory() as directory:
            first = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            second = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            with patch.object(msvcrt, "locking", side_effect=flaky_unlock):
                with self.assertRaisesRegex(OSError, "synthetic unlock failure"):
                    with first._exclusive_publish_lock():
                        pass
            with second._exclusive_publish_lock():
                pass

    def test_v7_quality_snapshot_is_invalid_after_current_policy_bump(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "security_master")
            builder = HistoricalSecurityMasterBuilder(store)
            with patch.object(
                master_module,
                "QUALITY_POLICY_VERSION",
                "cn-historical-security-master-quality-v7",
            ):
                active_codes = ["600000.SH"]
                with _admitted_current_observation(active_codes) as (manifest, _):
                    builder.build_from_bytes(
                        sse_json=_sse_bytes(),
                        szse_xlsx=_xlsx_bytes(),
                        bse_mapping_html=_bse_bytes(
                            [
                                ["1", "*ST娴滄垵鍨?", "2021/8/26", "835305", "920305"],
                                ["2", "濞村鐦拠浣稿煖", "2021/11/15", "838680", "920680"],
                            ]
                        ),
                        tdx_active_codes=active_codes,
                        retrieved_at=RETRIEVED_AT,
                        expected_sse_szse_overlap=2,
                        current_observation_manifest=manifest,
                    )
            gate = store.load_gate()
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "quality policy mismatch",
            ):
                store.load_latest_attempt()

        self.assertEqual(QUALITY_POLICY_VERSION, "cn-historical-security-master-quality-v15")
        self.assertEqual(gate["status"], "NOT_BUILT")

    def test_fixed_bse_v2_manifest_materializes_transfers_without_forging_active_codes(
        self,
    ) -> None:
        sse_active = parse_sse_active_json(
            _sse_active_bytes(
                [
                    _sse_active_row("600000"),
                    {
                        **_sse_active_row("688287"),
                        "LIST_DATE": "20220525",
                    },
                ]
            ),
            retrieved_at=RETRIEVED_AT,
        )
        szse_active = parse_szse_active_xlsx(
            _szse_active_xlsx_bytes(
                [
                    _szse_active_row(),
                    _szse_active_row(
                        "301192",
                        name="泰祥股份",
                        company_name="十堰市泰祥实业股份有限公司",
                        listed_at="2022-08-11",
                        board="创业板",
                    ),
                    _szse_active_row(
                        "301321",
                        name="翰博高新",
                        company_name="翰博高新材料(合肥)股份有限公司",
                        listed_at="2022-08-18",
                        board="创业板",
                    ),
                ]
            ),
            retrieved_at=RETRIEVED_AT,
        )
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(
                _bse_bytes(
                    [
                        ["1", "*ST云创", "2021/8/26", "835305", "920305"],
                        ["2", "测试证券", "2021/11/15", "838680", "920680"],
                    ]
                ),
                retrieved_at=RETRIEVED_AT,
            ),
            sse_active,
            szse_active,
        )
        records = tuple(record for source in sources for record in source.records)
        normalized_records, normalized_sources = (
            integrate_bse_termination_event_manifest(records, sources)
        )
        complete_tdx = [
            "600000.SH",
            "688287.SH",
            "000001.SZ",
            "301192.SZ",
            "301321.SZ",
            "920305.BJ",
            "920680.BJ",
        ]
        gate = build_quality_report(
            normalized_records,
            normalized_sources,
            complete_tdx,
            expected_sse_szse_overlap=2,
        )["gate"]
        missing_current = build_quality_report(
            normalized_records,
            normalized_sources,
            [code for code in complete_tdx if code not in {"920305.BJ", "920680.BJ"}],
            expected_sse_szse_overlap=2,
        )["gate"]

        transfers = {
            record.code_alias: record
            for record in normalized_records
            if record.event_type in {"TRANSFER_OUT", "TRANSFER_IN"}
        }
        expected_boundaries = {
            "832317.BJ": "2022-04-26",
            "688287.SH": "2022-05-25",
            "833874.BJ": "2022-07-18",
            "301192.SZ": "2022-08-11",
            "833994.BJ": "2022-07-25",
            "301321.SZ": "2022-08-18",
        }
        self.assertEqual(set(transfers), set(expected_boundaries))
        for code, boundary in expected_boundaries.items():
            record = transfers[code]
            if code.endswith(".BJ"):
                self.assertEqual(record.valid_from, "2021-11-15")
                self.assertEqual(record.valid_to, boundary)
                self.assertEqual(record.delisted_at, boundary)
                self.assertNotEqual(
                    record.valid_to,
                    record.attributes["termination_notice_date"],
                )
            else:
                self.assertEqual(record.valid_from, boundary)
                self.assertEqual(record.listed_at, boundary)
                self.assertIsNone(record.valid_to)
        for source_code, target_code in (
            ("832317.BJ", "688287.SH"),
            ("833874.BJ", "301192.SZ"),
            ("833994.BJ", "301321.SZ"),
        ):
            self.assertEqual(
                transfers[source_code].canonical_entity_id,
                transfers[target_code].canonical_entity_id,
            )
        self.assertFalse(
            any(
                record.event_type == "ACTIVE_LISTING"
                and record.code_alias in {"688287.SH", "301192.SZ", "301321.SZ"}
                for record in normalized_records
            )
        )
        completeness = gate["source_completeness"]
        self.assertTrue(completeness["bse_termination_and_transfer_events"])
        self.assertEqual(
            completeness["bse_termination_event_protocol_version"],
            BSE_TERMINATION_EVENT_PROTOCOL_VERSION,
        )
        self.assertEqual(
            completeness["bse_termination_event_manifest_sha256"],
            BSE_TERMINATION_EVENT_MANIFEST_SHA256,
        )
        self.assertEqual(
            completeness["bse_termination_event_logical_content_sha256"],
            BSE_TERMINATION_EVENT_LOGICAL_SHA256,
        )
        self.assertEqual(
            gate["status"], "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
        )
        self.assertFalse(
            completeness["bse_current_delisting_source_verified"]
        )
        self.assertEqual(
            missing_current["status"],
            "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE",
        )
        self.assertEqual(
            missing_current["reconciliation"]["bse_current_alias_missing_sample"],
            ["920305.BJ", "920680.BJ"],
        )
        self.assertEqual(
            missing_current["reconciliation"]["active_reconciliation_status"],
            "ACTIVE_RECONCILIATION_FAILED",
        )

        with self.assertRaisesRegex(
            HistoricalSecurityMasterBlockedError,
            "policy-bound V2 release",
        ):
            integrate_bse_termination_event_manifest(
                records,
                sources,
                "0" * 64,
            )

    def test_bse_transfer_target_can_later_terminate_on_target_exchange(self) -> None:
        sse_terminated = parse_sse_delist_json(
            _sse_bytes(
                [
                    {
                        "A_STOCK_CODE": "600432",
                        "COMPANY_CODE": "600432",
                        "COMPANY_ABBR": "吉恩退",
                        "LIST_DATE": "20030905",
                        "DELIST_DATE": "20180713",
                    },
                    {
                        "A_STOCK_CODE": "688287",
                        "COMPANY_CODE": "688287",
                        "COMPANY_ABBR": "退市观典",
                        "LIST_DATE": "20220525",
                        "DELIST_DATE": "20260610",
                    },
                ]
            ),
            retrieved_at=RETRIEVED_AT,
        )
        sources = (
            sse_terminated,
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(
                _bse_bytes(
                    [
                        ["1", "*ST云创", "2021/8/26", "835305", "920305"],
                        ["2", "测试证券", "2021/11/15", "838680", "920680"],
                    ]
                ),
                retrieved_at=RETRIEVED_AT,
            ),
            parse_sse_active_json(
                _sse_active_bytes([_sse_active_row("600000")]),
                retrieved_at=RETRIEVED_AT,
            ),
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(
                    [
                        _szse_active_row(),
                        _szse_active_row("301192", listed_at="2022-08-11"),
                        _szse_active_row("301321", listed_at="2022-08-18"),
                    ]
                ),
                retrieved_at=RETRIEVED_AT,
            ),
        )
        records = tuple(record for source in sources for record in source.records)

        normalized_records, normalized_sources = (
            integrate_bse_termination_event_manifest(records, sources)
        )

        target = next(
            record
            for record in normalized_records
            if record.code_alias == "688287.SH"
            and record.event_type == "TRANSFER_IN"
        )
        self.assertEqual(target.listed_at, "2022-05-25")
        self.assertEqual(target.valid_from, "2022-05-25")
        self.assertEqual(target.valid_to, "2026-06-10")
        self.assertEqual(target.delisted_at, "2026-06-10")
        self.assertEqual(
            target.attributes["target_catalog_event_type"],
            "TERMINATED_LISTING",
        )
        self.assertEqual(
            target.attributes["target_catalog_delisted_at"],
            "2026-06-10",
        )
        self.assertFalse(
            any(
                record.code_alias == "688287.SH"
                and record.event_type == "TERMINATED_LISTING"
                for record in normalized_records
            )
        )
        self.assertTrue(
            master_module._verified_bse_termination_event_metadata(
                records=normalized_records,
                sources=normalized_sources,
                source_by_name={source.name: source for source in normalized_sources},
            )["verified"]
        )

    def test_all_interval_sources_are_independent_fail_closed_gates(self) -> None:
        terminated_sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
        )
        terminated_records = tuple(
            record for source in terminated_sources for record in source.records
        )
        coverage_failed = build_quality_report(
            terminated_records,
            terminated_sources,
            ["920305.BJ"],
        )["gate"]
        # Caller booleans are not evidence and cannot bypass a missing source.
        active_intervals_incomplete = build_quality_report(
            terminated_records,
            terminated_sources,
            ["920305.BJ"],
            expected_sse_szse_overlap=2,
            sse_active_interval_history_complete=True,
            szse_active_interval_history_complete=True,
            bse_event_history_complete=True,
        )["gate"]
        sources = (
            *terminated_sources,
            parse_sse_active_json(
                _sse_active_bytes(
                    [
                        _sse_active_row("600000"),
                        _sse_active_row("688646", company_abbr="逸飞激光"),
                    ]
                ),
                retrieved_at=RETRIEVED_AT,
            ),
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(), retrieved_at=RETRIEVED_AT
            ),
        )
        records = tuple(record for source in sources for record in source.records)
        with tempfile.TemporaryDirectory() as directory:
            (
                risk_store,
                risk_reference,
                _risk_artifact,
                status7_store,
                status7_reference,
                _status7_artifact,
            ) = _sealed_status7_active_intervals(Path(directory))
            active_codes = [
                "600000.SH",
                "600053.SH",
                "688022.SH",
                "688646.SH",
                "000001.SZ",
                "920305.BJ",
            ]
            bse_incomplete = build_quality_report(
                records,
                sources,
                active_codes,
                expected_sse_szse_overlap=2,
                sse_risk_warning_manifest=risk_reference,
                sse_risk_warning_store=risk_store,
                sse_risk_warning_active_intervals_manifest=status7_reference,
                sse_risk_warning_active_intervals_store=status7_store,
            )["gate"]
            self_reported_bse_complete = build_quality_report(
                records,
                sources,
                active_codes,
                expected_sse_szse_overlap=2,
                bse_event_history_complete=True,
                sse_risk_warning_manifest=risk_reference,
                sse_risk_warning_store=risk_store,
                sse_risk_warning_active_intervals_manifest=status7_reference,
                sse_risk_warning_active_intervals_store=status7_store,
            )["gate"]

        self.assertEqual(coverage_failed["status"], "SOURCE_COVERAGE_FAILED")
        self.assertEqual(
            coverage_failed["reconciliation"]["required_sse_szse_overlap"], 239
        )
        self.assertEqual(
            active_intervals_incomplete["status"],
            "ACTIVE_INTERVAL_SOURCE_INCOMPLETE",
        )
        self.assertFalse(active_intervals_incomplete["ready"])
        self.assertFalse(
            active_intervals_incomplete["source_completeness"][
                "sse_active_listing_intervals"
            ]
        )
        self.assertFalse(
            active_intervals_incomplete["source_completeness"][
                "szse_active_listing_intervals"
            ]
        )
        self.assertEqual(bse_incomplete["status"], "SOURCE_INCOMPLETE")
        self.assertFalse(bse_incomplete["ready"])
        self.assertTrue(bse_incomplete["promotion_blocked"])
        self.assertTrue(
            bse_incomplete["source_completeness"]["sse_active_listing_intervals"]
        )
        self.assertTrue(
            bse_incomplete["source_completeness"]["szse_active_listing_intervals"]
        )
        self.assertEqual(self_reported_bse_complete["status"], "SOURCE_INCOMPLETE")
        self.assertFalse(self_reported_bse_complete["ready"])

    def test_active_tdx_difference_fails_reconciliation(self) -> None:
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_sse_active_json(_sse_active_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(), retrieved_at=RETRIEVED_AT
            ),
        )
        records = tuple(record for source in sources for record in source.records)

        gate = build_quality_report(
            records,
            sources,
            ["600000.SH", "000002.SZ", "920305.BJ"],
            expected_sse_szse_overlap=2,
        )["gate"]

        self.assertEqual(gate["status"], "ACTIVE_RECONCILIATION_FAILED")
        self.assertEqual(gate["reconciliation"]["szse_missing_from_tdx_count"], 1)
        self.assertEqual(gate["reconciliation"]["szse_extra_in_tdx_count"], 1)
        self.assertFalse(gate["ready"])

    def test_replayed_risk_warning_manifest_explains_current_only_differences(self) -> None:
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_sse_active_json(_sse_active_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(), retrieved_at=RETRIEVED_AT
            ),
        )
        records = tuple(record for source in sources for record in source.records)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_reference, risk_artifact = (
                _sealed_risk_warning_manifest(
                    root,
                    main_rows=[("600053", "*ST测试"), ("900915", "ST测试B")],
                    star_rows=[("688022", "*ST科创")],
                )
            )
            active_codes = [
                "600000.SH",
                "600053.SH",
                "688022.SH",
                "000001.SZ",
            ]
            gate = build_quality_report(
                records,
                sources,
                active_codes,
                expected_sse_szse_overlap=2,
                sse_risk_warning_manifest=risk_reference,
                sse_risk_warning_store=risk_store,
            )["gate"]

            master_store = HistoricalSecurityMasterStore(root / "security-master")
            with _admitted_current_observation(active_codes) as (manifest, _):
                release = HistoricalSecurityMasterBuilder(
                    master_store
                ).build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "*ST娴滄垵鍨?", "2021/8/26", "835305", "920305"],
                            ["2", "濞村鐦拠浣稿煖", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    sse_active_json=_sse_active_bytes(),
                    szse_active_xlsx=_szse_active_xlsx_bytes(),
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    sse_risk_warning_manifest=risk_reference,
                    sse_risk_warning_store=risk_store,
                    current_observation_manifest=manifest,
                )
            loaded = master_store.load_latest_attempt()
            master_path = Path(
                loaded["manifest"]["artifacts"]["security_master_jsonl"][
                    "object_path"
                ]
            )
            master_bytes = master_path.read_bytes()

        counts = gate["source_counts"]["sse_current_risk_warning"]
        reconciliation = gate["reconciliation"]
        completeness = gate["source_completeness"]
        self.assertEqual(gate["status"], "SOURCE_INCOMPLETE")
        self.assertEqual(counts["protocol_version"], SSE_RISK_WARNING_PROTOCOL_VERSION)
        self.assertEqual(counts["manifest_sha256"], risk_reference.manifest_sha256)
        self.assertEqual(
            counts["logical_content_sha256"],
            risk_artifact.logical_content_sha256,
        )
        self.assertEqual(counts["raw_hashes"], {
            item.source_id: item.content_sha256
            for item in risk_artifact.raw_responses
        })
        self.assertEqual(counts["main_board_rows"], 2)
        self.assertEqual(counts["main_board_a_share_rows"], 1)
        self.assertEqual(counts["main_board_b_share_rows"], 1)
        self.assertEqual(counts["star_market_rows"], 1)
        self.assertEqual(counts["star_market_a_share_rows"], 1)
        self.assertEqual(counts["star_market_b_share_rows"], 0)
        self.assertEqual(counts["a_share_rows"], 2)
        self.assertEqual(counts["b_share_rows_excluded"], 1)
        self.assertEqual(reconciliation["sse_extra_in_tdx_count"], 2)
        self.assertEqual(
            reconciliation["sse_current_risk_warning_explained_extra_count"], 2
        )
        self.assertEqual(
            reconciliation["sse_unexplained_after_risk_warning_count"], 0
        )
        self.assertTrue(reconciliation["sse_current_set_equality_holds"])
        self.assertEqual(
            reconciliation[
                "sse_current_risk_warning_explained_extra_code_set_sha256"
            ],
            risk_artifact.statistics["a_share_code_set_sha256"],
        )
        self.assertTrue(
            completeness["sse_current_risk_warning_source_verified"]
        )
        self.assertEqual(
            completeness["sse_current_risk_warning_main_board_rows"], 2
        )
        self.assertEqual(
            completeness["sse_current_risk_warning_star_market_rows"], 1
        )
        self.assertEqual(
            completeness["sse_current_risk_warning_a_share_count"], 2
        )
        self.assertEqual(
            completeness["sse_current_risk_warning_b_share_excluded_count"], 1
        )
        self.assertFalse(completeness["sse_active_listing_intervals"])
        self.assertEqual(
            completeness[
                "sse_current_risk_warning_listing_interval_covered_count"
            ],
            0,
        )
        self.assertEqual(
            completeness[
                "sse_current_risk_warning_listing_interval_missing_count"
            ],
            2,
        )
        self.assertEqual(
            completeness[
                "sse_current_risk_warning_listing_interval_missing_sample"
            ],
            ["600053.SH", "688022.SH"],
        )
        self.assertTrue(
            completeness[
                "sse_current_risk_warning_is_current_reconciliation_only"
            ]
        )
        self.assertFalse(
            completeness[
                "sse_current_risk_warning_contributes_historical_intervals"
            ]
        )
        self.assertEqual(
            loaded["manifest"]["quality_policy_version"],
            QUALITY_POLICY_VERSION,
        )
        self.assertNotIn(b"600053.SH", master_bytes)
        self.assertNotIn(b"688022.SH", master_bytes)

    def test_builder_materializes_status7_records_into_published_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                risk_store,
                risk_reference,
                _risk_artifact,
                status7_store,
                status7_reference,
                status7_artifact,
            ) = _sealed_status7_active_intervals(
                root,
                main_rows=[("600053", "*ST测试")],
                star_rows=[("688022", "*ST科创")],
            )
            active_codes = [
                "000001.SZ",
                "600000.SH",
                "600053.SH",
                "688022.SH",
            ]
            expected_tdx = master_observation.TDXAShareObservation.capture(
                _fixture_tdx_names(active_codes),
                observed_at=RETRIEVED_AT,
            )
            master_store = HistoricalSecurityMasterStore(root / "security-master")
            with _admitted_current_observation(active_codes) as (manifest, _):
                result = HistoricalSecurityMasterBuilder(
                    master_store
                ).build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "*ST测试", "2021/8/26", "835305", "920305"],
                            ["2", "测试证券", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    sse_active_json=_sse_active_bytes(),
                    szse_active_xlsx=_szse_active_xlsx_bytes(),
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    sse_risk_warning_manifest=risk_reference,
                    sse_risk_warning_store=risk_store,
                    sse_risk_warning_active_intervals_manifest=status7_reference,
                    sse_risk_warning_active_intervals_store=status7_store,
                    current_observation_manifest=manifest,
                )

            attempt = master_store.load_latest_attempt()
            master_path = Path(
                attempt["manifest"]["artifacts"]["security_master_jsonl"][
                    "object_path"
                ]
            )
            rows = [
                json.loads(line)
                for line in master_path.read_text("utf-8").splitlines()
                if line
            ]
            identity_metadata = attempt["manifest"]["artifacts"][
                "tdx_identity_snapshot"
            ]
            identity_snapshot = json.loads(
                Path(identity_metadata["object_path"]).read_text("utf-8")
            )

        status7_rows = {
            row["code_alias"]: row
            for row in rows
            if row["code_alias"] in {"600053.SH", "688022.SH"}
        }
        self.assertEqual(set(status7_rows), {"600053.SH", "688022.SH"})
        self.assertEqual(
            {row["source_hash"] for row in status7_rows.values()},
            {status7_reference.manifest_sha256},
        )
        completeness = result["gate"]["source_completeness"]
        self.assertEqual(
            completeness[
                "sse_risk_warning_active_intervals_manifest_sha256"
            ],
            status7_reference.manifest_sha256,
        )
        self.assertEqual(
            completeness["sse_risk_warning_active_intervals_interval_count"],
            len(status7_artifact.intervals),
        )
        self.assertEqual(
            completeness["current_observation_tdx_identity_sha256"],
            expected_tdx.identity_sha256,
        )
        self.assertEqual(
            attempt["manifest"]["current_observation"]["tdx_identity_sha256"],
            expected_tdx.identity_sha256,
        )
        self.assertNotIn("tdx_names", attempt["manifest"]["current_observation"])
        self.assertEqual(
            identity_metadata["content_hash"], expected_tdx.identity_sha256
        )
        self.assertEqual(identity_metadata["row_count"], expected_tdx.code_count)
        self.assertEqual(identity_snapshot, dict(expected_tdx.names))

    def test_converged_transition_requires_status2_official_name_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                risk_store,
                risk_reference,
                _risk_artifact,
                status7_store,
                status7_reference,
                status7_artifact,
            ) = _sealed_status7_active_intervals(root)
            self.assertEqual(
                status7_artifact.transition_binding_state,
                master_module.SSE_TRANSITION_BINDING_CONVERGED,
            )
            transition_code = status7_artifact.transition_code_alias.removesuffix(
                ".SH"
            )
            active_codes = [
                "000001.SZ",
                "600000.SH",
                "600053.SH",
                "688022.SH",
                status7_artifact.transition_code_alias,
                "920305.BJ",
            ]

            def report_for_status2_name(name: str) -> dict[str, object]:
                transition_row = {
                    **_sse_active_row(transition_code),
                    "COMPANY_ABBR": name,
                    "LIST_DATE": "20230728",
                }
                sources = (
                    parse_sse_delist_json(
                        _sse_bytes(), retrieved_at=RETRIEVED_AT
                    ),
                    parse_szse_delist_xlsx(
                        _xlsx_bytes(), retrieved_at=RETRIEVED_AT
                    ),
                    parse_bse_code_mapping_html(
                        _bse_bytes(), retrieved_at=RETRIEVED_AT
                    ),
                    parse_sse_active_json(
                        _sse_active_bytes(
                            [_sse_active_row("600000"), transition_row]
                        ),
                        retrieved_at=RETRIEVED_AT,
                    ),
                    parse_szse_active_xlsx(
                        _szse_active_xlsx_bytes(),
                        retrieved_at=RETRIEVED_AT,
                    ),
                )
                records = tuple(
                    record for source in sources for record in source.records
                )
                return build_quality_report(
                    records,
                    sources,
                    active_codes,
                    expected_sse_szse_overlap=2,
                    sse_risk_warning_manifest=risk_reference,
                    sse_risk_warning_store=risk_store,
                    sse_risk_warning_active_intervals_manifest=status7_reference,
                    sse_risk_warning_active_intervals_store=status7_store,
                )["gate"]

            mismatch = report_for_status2_name("not-the-transition-name")
            matched = report_for_status2_name(
                status7_artifact.transition_new_name
            )

        self.assertEqual(mismatch["status"], "ACTIVE_RECONCILIATION_FAILED")
        mismatch_completeness = mismatch["source_completeness"]
        self.assertEqual(
            mismatch_completeness[
                "sse_transition_official_name_mismatch_count"
            ],
            1,
        )
        self.assertEqual(
            mismatch_completeness[
                "sse_transition_official_name_mismatch_sample"
            ],
            [
                {
                    "code": status7_artifact.transition_code_alias,
                    "expected_name": status7_artifact.transition_new_name,
                    "observed_name": "not-the-transition-name",
                }
            ],
        )
        matched_completeness = matched["source_completeness"]
        self.assertEqual(
            matched_completeness[
                "sse_transition_official_name_mismatch_count"
            ],
            0,
        )
        self.assertEqual(
            matched_completeness[
                "sse_transition_official_name_mismatch_sample"
            ],
            [],
        )
        self.assertNotEqual(matched["status"], "ACTIVE_RECONCILIATION_FAILED")

    def test_additional_record_cannot_forge_status7_dedup_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                risk_store,
                risk_reference,
                _risk_artifact,
                status7_store,
                status7_reference,
                status7_artifact,
            ) = _sealed_status7_active_intervals(
                root,
                main_rows=[("600053", "*ST测试")],
                star_rows=[("688022", "*ST科创")],
            )
            interval = status7_artifact.intervals[0]
            forged = SecurityMasterRecord(
                canonical_entity_id=interval.canonical_entity_id,
                exchange=interval.exchange,
                code_alias=interval.code_alias,
                board=interval.board,
                listed_at=interval.listed_at,
                delisted_at=None,
                valid_from=interval.valid_from,
                valid_to=None,
                event_type="ACTIVE_LISTING",
                source_url="https://caller.invalid/forged-status7",
                source_hash="f" * 64,
                retrieved_at=RETRIEVED_AT,
                name=interval.name,
                attributes={"company_code": interval.code_alias[:6]},
            )
            active_codes = [
                "000001.SZ",
                "600000.SH",
                "600053.SH",
                "688022.SH",
            ]
            with (
                _admitted_current_observation(active_codes) as (manifest, _),
                self.assertRaisesRegex(
                    HistoricalSecurityMasterBlockedError,
                    "overlapping alias intervals|status-7.*provenance",
                ),
            ):
                HistoricalSecurityMasterBuilder(
                    HistoricalSecurityMasterStore(root / "security-master")
                ).build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "*ST测试", "2021/8/26", "835305", "920305"],
                            ["2", "测试证券", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    sse_active_json=_sse_active_bytes(),
                    szse_active_xlsx=_szse_active_xlsx_bytes(),
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    additional_records=(forged,),
                    sse_risk_warning_manifest=risk_reference,
                    sse_risk_warning_store=risk_store,
                    sse_risk_warning_active_intervals_manifest=status7_reference,
                    sse_risk_warning_active_intervals_store=status7_store,
                    current_observation_manifest=manifest,
                )

    def test_unexplained_current_extra_requires_pending_listing_evidence(self) -> None:
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_sse_active_json(
                _sse_active_bytes(
                    rows=[
                        _sse_active_row("600000"),
                        {
                            **_sse_active_row("688646"),
                            "COMPANY_ABBR": "逸飞激光",
                            "LIST_DATE": "20230728",
                        },
                    ]
                ),
                retrieved_at=RETRIEVED_AT,
            ),
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(), retrieved_at=RETRIEVED_AT
            ),
        )
        records = tuple(record for source in sources for record in source.records)
        with tempfile.TemporaryDirectory() as directory:
            (
                risk_store,
                risk_reference,
                _risk_artifact,
                status7_store,
                status7_reference,
                _status7_artifact,
            ) = _sealed_status7_active_intervals(Path(directory))
            gate = build_quality_report(
                records,
                sources,
                [
                    "600000.SH",
                    "600053.SH",
                    "688022.SH",
                    "688646.SH",
                    "688826.SH",
                    "000001.SZ",
                    "920305.BJ",
                ],
                expected_sse_szse_overlap=2,
                sse_risk_warning_manifest=risk_reference,
                sse_risk_warning_store=risk_store,
                sse_risk_warning_active_intervals_manifest=status7_reference,
                sse_risk_warning_active_intervals_store=status7_store,
            )["gate"]

        reconciliation = gate["reconciliation"]
        self.assertEqual(gate["status"], "PENDING_LISTING_STATUS_INCOMPLETE")
        self.assertFalse(gate["ready"])
        self.assertEqual(
            reconciliation["active_reconciliation_status"],
            "PENDING_LISTING_STATUS_INCOMPLETE",
        )
        self.assertEqual(
            reconciliation["sse_current_risk_warning_explained_extra_count"], 2
        )
        self.assertEqual(
            reconciliation["sse_unexplained_after_risk_warning_count"], 1
        )
        self.assertEqual(
            reconciliation["sse_unexplained_after_risk_warning_sample"],
            ["688826.SH"],
        )
        self.assertFalse(reconciliation["sse_current_set_equality_holds"])
        self.assertFalse(
            reconciliation["pending_listing_status_source_verified"]
        )

    def test_pending_listing_manifest_is_current_only_explain_set(self) -> None:
        sources = _current_reconciliation_sources()
        records = tuple(record for source in sources for record in source.records)
        records, sources = integrate_bse_termination_event_manifest(records, sources)
        active_codes = [
            "600000.SH",
            "688287.SH",
            "600053.SH",
            "688022.SH",
            "688826.SH",
            "688835.SH",
            "688836.SH",
            "000001.SZ",
            "301192.SZ",
            "301321.SZ",
            "301655.SZ",
            "301688.SZ",
            "301697.SZ",
            "920305.BJ",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_reference, _risk_artifact = (
                _sealed_risk_warning_manifest(root)
            )
            (
                pending_store,
                pending_reference,
                pending_artifact,
                specs,
                source_order,
            ) = _sealed_pending_listing_manifest(root / "pending-cas")
            with (
                _pending_fixture_contract(specs, source_order),
                patch.object(
                    master_module,
                    "PENDING_LISTING_STORE_ROOT",
                    root / "pending-cas",
                ),
                patch.object(
                    master_module,
                    "PENDING_LISTING_MANIFEST_SHA256",
                    pending_reference.manifest_sha256,
                ),
                patch.object(
                    master_module,
                    "PENDING_LISTING_LOGICAL_SHA256",
                    pending_artifact.logical_content_sha256,
                ),
                patch.object(
                    master_module,
                    "_current_wall_clock",
                    return_value=PENDING_FIXTURE_NOW,
                ),
            ):
                quality = build_quality_report(
                    records,
                    sources,
                    active_codes,
                    expected_sse_szse_overlap=2,
                    sse_risk_warning_manifest=risk_reference,
                    sse_risk_warning_store=risk_store,
                    pending_listing_manifest=pending_reference,
                    pending_listing_store=pending_store,
                    pending_listing_validation_now=PENDING_FIXTURE_NOW,
                    pending_listing_as_of=PENDING_FIXTURE_NOW,
                )

        gate = quality["gate"]
        completeness = gate["source_completeness"]
        reconciliation = gate["reconciliation"]
        counts = gate["source_counts"]["pending_listing_current_official"]
        self.assertEqual(
            quality["quality_policy_version"],
            QUALITY_POLICY_VERSION,
        )
        self.assertEqual(
            gate["status"], "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
        )
        self.assertFalse(
            completeness["bse_current_delisting_source_verified"]
        )
        self.assertTrue(completeness["pending_listing_status_source_verified"])
        self.assertEqual(
            completeness["pending_listing_protocol_version"],
            pending_listing.PROTOCOL_VERSION,
        )
        self.assertEqual(
            completeness["pending_listing_manifest_sha256"],
            pending_reference.manifest_sha256,
        )
        self.assertEqual(
            completeness["pending_listing_logical_content_sha256"],
            pending_artifact.logical_content_sha256,
        )
        self.assertEqual(
            completeness["pending_listing_raw_hashes"],
            {
                item.source_id: item.content_sha256
                for item in pending_artifact.raw_sources
            },
        )
        self.assertEqual(counts["raw_source_count"], 12)
        self.assertEqual(counts["official_code_count"], 6)
        self.assertEqual(reconciliation["pending_listing_explained_sse_count"], 3)
        self.assertEqual(reconciliation["pending_listing_explained_szse_count"], 3)
        self.assertEqual(
            reconciliation["pending_listing_manifest_sha256"],
            pending_reference.manifest_sha256,
        )
        self.assertEqual(
            reconciliation["pending_listing_logical_content_sha256"],
            pending_artifact.logical_content_sha256,
        )
        self.assertEqual(
            reconciliation["pending_listing_raw_hashes"],
            completeness["pending_listing_raw_hashes"],
        )
        self.assertTrue(reconciliation["sse_current_set_equality_holds"])
        self.assertTrue(reconciliation["szse_current_set_equality_holds"])
        self.assertTrue(
            reconciliation["pending_listing_is_current_reconciliation_only"]
        )
        self.assertFalse(
            reconciliation["pending_listing_contributes_historical_intervals"]
        )
        self.assertFalse(
            reconciliation["pending_listing_contributes_trading_eligibility"]
        )
        self.assertFalse(
            reconciliation["tdx_active_snapshot_caller_retrieved_at_accepted"]
        )
        self.assertEqual(
            reconciliation["tdx_active_snapshot_observed_at"],
            PENDING_FIXTURE_NOW.isoformat(),
        )
        self.assertEqual(
            reconciliation["pending_listing_as_of"],
            PENDING_FIXTURE_NOW.isoformat(),
        )
        self.assertFalse(
            any(
                record.code_alias
                in master_module.PENDING_LISTING_RECONCILIATION_CODES
                for record in records
            )
        )

    def test_pending_listing_stale_redated_or_tampered_stays_incomplete(self) -> None:
        sources = _current_reconciliation_sources()
        records = tuple(record for source in sources for record in source.records)
        records, sources = integrate_bse_termination_event_manifest(records, sources)
        active_codes = [
            "600000.SH",
            "688287.SH",
            "600053.SH",
            "688022.SH",
            *sorted(master_module.PENDING_LISTING_RECONCILIATION_CODES),
            "000001.SZ",
            "301192.SZ",
            "301321.SZ",
            "920305.BJ",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            risk_store, risk_reference, _artifact = _sealed_risk_warning_manifest(root)
            (
                pending_store,
                pending_reference,
                pending_artifact,
                specs,
                source_order,
            ) = _sealed_pending_listing_manifest(root / "pending-cas")

            def build_at(
                wall_clock: datetime,
                *,
                validation_now: datetime | None = None,
                as_of: datetime | None = None,
                manifest: object = pending_reference,
                store: object = pending_store,
            ) -> dict[str, object]:
                with (
                    _pending_fixture_contract(specs, source_order),
                    patch.object(
                        master_module,
                        "PENDING_LISTING_STORE_ROOT",
                        root / "pending-cas",
                    ),
                    patch.object(
                        master_module,
                        "PENDING_LISTING_MANIFEST_SHA256",
                        pending_reference.manifest_sha256,
                    ),
                    patch.object(
                        master_module,
                        "PENDING_LISTING_LOGICAL_SHA256",
                        pending_artifact.logical_content_sha256,
                    ),
                    patch.object(
                        master_module,
                        "_current_wall_clock",
                        return_value=wall_clock,
                    ),
                ):
                    return build_quality_report(
                        records,
                        sources,
                        active_codes,
                        expected_sse_szse_overlap=2,
                        sse_risk_warning_manifest=risk_reference,
                        sse_risk_warning_store=risk_store,
                        pending_listing_manifest=manifest,  # type: ignore[arg-type]
                        pending_listing_store=store,  # type: ignore[arg-type]
                        pending_listing_validation_now=validation_now,
                        pending_listing_as_of=as_of,
                    )["gate"]

            stale = build_at(PENDING_FIXTURE_NOW + timedelta(minutes=16))
            redated = build_at(
                PENDING_FIXTURE_NOW + timedelta(minutes=16),
                validation_now=PENDING_FIXTURE_NOW,
                as_of=PENDING_FIXTURE_NOW,
            )
            wrong_digest = build_at(PENDING_FIXTURE_NOW, manifest="0" * 64)
            wrong_root_store = pending_listing.PendingListingManifestStore(
                pending_listing.PendingListingRawCAS(root / "wrong-pending-cas")
            )
            wrong_root = build_at(PENDING_FIXTURE_NOW, store=wrong_root_store)
            Path(pending_artifact.raw_sources[0].object_path).write_bytes(b"tampered")
            tampered = build_at(PENDING_FIXTURE_NOW)

        for label, gate in (
            ("stale", stale),
            ("redated", redated),
            ("wrong_digest", wrong_digest),
            ("wrong_root", wrong_root),
            ("tampered", tampered),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    gate["status"], "PENDING_LISTING_STATUS_INCOMPLETE"
                )
                self.assertFalse(
                    gate["source_completeness"][
                        "pending_listing_status_source_verified"
                    ]
                )
                self.assertEqual(
                    gate["reconciliation"]["active_reconciliation_status"],
                    "PENDING_LISTING_STATUS_INCOMPLETE",
                )
        self.assertIn(
            "stale", stale["source_completeness"]["pending_listing_error"]
        )
        self.assertIn(
            "caller re-dating",
            redated["source_completeness"]["pending_listing_error"],
        )
        self.assertIn(
            "policy-bound release",
            wrong_digest["source_completeness"]["pending_listing_error"],
        )
        self.assertIn(
            "root is not policy-bound",
            wrong_root["source_completeness"]["pending_listing_error"],
        )
        self.assertTrue(
            "hash mismatch"
            in tampered["source_completeness"]["pending_listing_error"]
            or "changed during read"
            in tampered["source_completeness"]["pending_listing_error"]
        )

    def test_fixed_bse_current_manifest_closes_same_entity_alias_intervals(self) -> None:
        root = master_module.BSE_CURRENT_DELISTING_STORE_ROOT
        digest = master_module.BSE_CURRENT_DELISTING_MANIFEST_SHA256
        manifest_path = root / "manifests" / f"{digest}.json"
        if not manifest_path.is_file():
            self.skipTest("ignored local real BSE current-delisting CAS is not present")
        sources = _current_reconciliation_sources_with_bse_delist_targets()
        records = tuple(record for source in sources for record in source.records)
        with patch.object(
            master_module,
            "_current_wall_clock",
            return_value=BSE_CURRENT_FIXTURE_NOW,
        ):
            normalized_records, normalized_sources = (
                integrate_bse_current_delisting_manifest(
                    records,
                    sources,
                    digest,
                    validation_now=BSE_CURRENT_FIXTURE_NOW,
                    as_of=BSE_CURRENT_FIXTURE_NOW,
                )
            )

        event_source = next(
            source
            for source in normalized_sources
            if source.name == master_module.BSE_CURRENT_DELISTING_SOURCE_NAME
        )
        self.assertEqual(
            [record.code_alias for record in event_source.records],
            ["920305.BJ", "920680.BJ"],
        )
        expected_dates = {"920305.BJ": "2026-07-30", "920680.BJ": "2026-01-05"}
        for record in event_source.records:
            mapping_record = next(
                item
                for source in sources
                if source.name == "bse_code_mapping"
                for item in source.records
                if item.code_alias == record.code_alias and item.valid_to is None
            )
            self.assertEqual(record.canonical_entity_id, mapping_record.canonical_entity_id)
            self.assertEqual(record.valid_from, mapping_record.valid_from)
            self.assertEqual(record.valid_to, expected_dates[record.code_alias])
            self.assertEqual(record.delisted_at, expected_dates[record.code_alias])
            self.assertEqual(record.event_type, "TERMINATED_LISTING")
            self.assertEqual(
                record.attributes["effective_date_source"], "NOTICE_PDF_ONLY"
            )
        self.assertFalse(
            any(
                record.code_alias in expected_dates and record.valid_to is None
                for record in normalized_records
            )
        )

    def test_builder_publishes_bse_closed_intervals_and_rejects_stale_active_codes(
        self,
    ) -> None:
        root = master_module.BSE_CURRENT_DELISTING_STORE_ROOT
        digest = master_module.BSE_CURRENT_DELISTING_MANIFEST_SHA256
        manifest_path = root / "manifests" / f"{digest}.json"
        if not manifest_path.is_file():
            self.skipTest("ignored local real BSE current-delisting CAS is not present")
        active_codes = [
            "600000.SH",
            "000001.SZ",
        ]
        with tempfile.TemporaryDirectory() as directory:
            builder = HistoricalSecurityMasterBuilder(
                HistoricalSecurityMasterStore(Path(directory) / "master")
            )
            with patch.object(
                master_module,
                "_current_wall_clock",
                return_value=BSE_CURRENT_FIXTURE_NOW,
            ):
                manifest = "0" * 64
                tdx_observation = master_observation.TDXAShareObservation.capture(
                    _fixture_tdx_names(active_codes),
                    observed_at=RETRIEVED_AT,
                )
                metadata = {
                    "protocol_version": master_observation.PROTOCOL_VERSION,
                    "manifest_sha256": manifest,
                    "logical_content_sha256": "1" * 64,
                    "validated_at": RETRIEVED_AT,
                    "as_of": RETRIEVED_AT,
                    "tdx_observed_at": RETRIEVED_AT,
                    "tdx_names": dict(tdx_observation.names),
                    "tdx_code_count": tdx_observation.code_count,
                    "tdx_code_set_sha256": tdx_observation.code_set_sha256,
                    "tdx_identity_sha256": tdx_observation.identity_sha256,
                    "pending_listing_manifest_sha256": (
                        master_module.PENDING_LISTING_MANIFEST_SHA256
                    ),
                    "pending_listing_logical_content_sha256": (
                        master_module.PENDING_LISTING_LOGICAL_SHA256
                    ),
                    "bse_current_delisting_manifest_sha256": digest,
                    "bse_current_delisting_logical_content_sha256": (
                        master_module.BSE_CURRENT_DELISTING_LOGICAL_SHA256
                    ),
                    "freshness_required_at_publish": True,
                    "immutable_replay_after_publish": True,
                }
                synthetic_batch = type(
                    "SyntheticObservationBatch",
                    (),
                    {
                        "pending_listing": type(
                            "PendingEvidence",
                            (),
                            {
                                "manifest_sha256": metadata[
                                    "pending_listing_manifest_sha256"
                                ],
                                "logical_content_sha256": metadata[
                                    "pending_listing_logical_content_sha256"
                                ],
                            },
                        )(),
                        "bse_current_delisting": type(
                            "BSEEvidence",
                            (),
                            {
                                "manifest_sha256": metadata[
                                    "bse_current_delisting_manifest_sha256"
                                ],
                                "logical_content_sha256": metadata[
                                    "bse_current_delisting_logical_content_sha256"
                                ],
                            },
                        )(),
                        "validated_at": metadata["validated_at"],
                        "as_of": metadata["as_of"],
                        "tdx_a_share": tdx_observation,
                    },
                )()
                observation_patch = patch.object(
                    master_module,
                    "_normalize_current_observation_reference",
                    return_value=(synthetic_batch, metadata),
                )
                observation_patch.start()
                release = builder.build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "*ST浜戝垱", "2021/8/26", "835305", "920305"],
                            ["2", "娴嬭瘯璇佸埜", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    sse_active_json=_sse_active_bytes(
                        [
                            _sse_active_row("600000"),
                            {
                                **_sse_active_row("688646"),
                                "COMPANY_ABBR": "逸飞激光",
                            },
                        ]
                    ),
                    szse_active_xlsx=_szse_active_xlsx_bytes(),
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    bse_current_delisting_manifest=digest,
                    bse_current_delisting_validation_now=BSE_CURRENT_FIXTURE_NOW,
                    bse_current_delisting_as_of=BSE_CURRENT_FIXTURE_NOW,
                    current_observation_manifest=manifest,
                    )
                observation_patch.stop()
                stale_active_gate = build_quality_report(
                    tuple(
                        record
                        for source in _current_reconciliation_sources_with_bse_delist_targets()
                        for record in source.records
                    ),
                    _current_reconciliation_sources_with_bse_delist_targets(),
                    [*active_codes, "920305.BJ", "920680.BJ"],
                    expected_sse_szse_overlap=2,
                    bse_current_delisting_manifest=digest,
                    bse_current_delisting_validation_now=BSE_CURRENT_FIXTURE_NOW,
                    bse_current_delisting_as_of=BSE_CURRENT_FIXTURE_NOW,
                )["gate"]

            manifest = json.loads(Path(release["manifest_path"]).read_text("utf-8"))
            master_path = Path(
                manifest["artifacts"]["security_master_jsonl"]["object_path"]
            )
            published_rows = [
                json.loads(line)
                for line in master_path.read_text("utf-8").splitlines()
                if line
            ]
        published = {
            row["code_alias"]: row
            for row in published_rows
            if row["code_alias"] in {"920305.BJ", "920680.BJ"}
        }
        self.assertEqual(
            {code: row["valid_to"] for code, row in published.items()},
            {"920305.BJ": "2026-07-30", "920680.BJ": "2026-01-05"},
        )
        self.assertTrue(
            release["gate"]["source_completeness"][
                "bse_current_delisting_source_verified"
            ]
        )
        self.assertEqual(stale_active_gate["status"], "ACTIVE_RECONCILIATION_FAILED")
        self.assertEqual(
            stale_active_gate["reconciliation"]["bse_delisted_still_active_sample"],
            ["920305.BJ", "920680.BJ"],
        )

    def test_bse_current_manifest_stale_redated_missing_or_tampered_fails_closed(
        self,
    ) -> None:
        root = master_module.BSE_CURRENT_DELISTING_STORE_ROOT
        digest = master_module.BSE_CURRENT_DELISTING_MANIFEST_SHA256
        manifest_path = root / "manifests" / f"{digest}.json"
        if not manifest_path.is_file():
            self.skipTest("ignored local real BSE current-delisting CAS is not present")
        sources = _current_reconciliation_sources_with_bse_delist_targets()
        records = tuple(record for source in sources for record in source.records)
        records, sources = integrate_bse_termination_event_manifest(records, sources)

        def gate_at(
            wall_clock: datetime,
            *,
            manifest: object = digest,
            store: object = None,
            validation_now: datetime | None = None,
            as_of: datetime | None = None,
        ) -> dict[str, object]:
            with patch.object(
                master_module,
                "_current_wall_clock",
                return_value=wall_clock,
            ):
                return build_quality_report(
                    records,
                    sources,
                    ["600000.SH", "688287.SH", "000001.SZ", "301192.SZ", "301321.SZ"],
                    expected_sse_szse_overlap=2,
                    bse_current_delisting_manifest=manifest,  # type: ignore[arg-type]
                    bse_current_delisting_store=store,  # type: ignore[arg-type]
                    bse_current_delisting_validation_now=validation_now,
                    bse_current_delisting_as_of=as_of,
                )["gate"]

        stale = gate_at(BSE_CURRENT_FIXTURE_NOW + timedelta(minutes=16))
        redated = gate_at(
            BSE_CURRENT_FIXTURE_NOW + timedelta(minutes=16),
            validation_now=BSE_CURRENT_FIXTURE_NOW,
            as_of=BSE_CURRENT_FIXTURE_NOW,
        )
        wrong_digest = gate_at(BSE_CURRENT_FIXTURE_NOW, manifest="0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            wrong_store = bse_current.BSECurrentDelistingManifestStore(
                bse_current.BSECurrentDelistingCAS(Path(directory))
            )
            wrong_root = gate_at(BSE_CURRENT_FIXTURE_NOW, store=wrong_store)

        for label, gate in (
            ("stale", stale),
            ("redated", redated),
            ("wrong_digest", wrong_digest),
            ("wrong_root", wrong_root),
        ):
            with self.subTest(label=label):
                completeness = gate["source_completeness"]
                self.assertEqual(
                    gate["status"], "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
                )
                self.assertFalse(
                    completeness["bse_current_delisting_source_verified"]
                )
        self.assertIn(
            "stale",
            stale["source_completeness"]["bse_current_delisting_error"],
        )
        self.assertIn(
            "caller re-dating",
            redated["source_completeness"]["bse_current_delisting_error"],
        )
        self.assertIn(
            "policy-bound release",
            wrong_digest["source_completeness"]["bse_current_delisting_error"],
        )
        self.assertIn(
            "root is not policy-bound",
            wrong_root["source_completeness"]["bse_current_delisting_error"],
        )

        with tempfile.TemporaryDirectory() as directory:
            copied_root = Path(directory) / "cas"
            copied_root.mkdir(parents=True)
            shutil.copytree(root / "manifests", copied_root / "manifests")
            shutil.copytree(root / "objects", copied_root / "objects")
            copied_manifest = copied_root / "manifests" / f"{digest}.json"
            copied_manifest.write_bytes(copied_manifest.read_bytes() + b"tampered")
            with (
                patch.object(
                    master_module,
                    "BSE_CURRENT_DELISTING_STORE_ROOT",
                    copied_root,
                ),
                patch.object(
                    master_module,
                    "_current_wall_clock",
                    return_value=BSE_CURRENT_FIXTURE_NOW,
                ),
            ):
                tampered = build_quality_report(
                    records,
                    sources,
                    ["600000.SH", "688287.SH", "000001.SZ", "301192.SZ", "301321.SZ"],
                    expected_sse_szse_overlap=2,
                    bse_current_delisting_manifest=digest,
                    bse_current_delisting_validation_now=BSE_CURRENT_FIXTURE_NOW,
                    bse_current_delisting_as_of=BSE_CURRENT_FIXTURE_NOW,
                )["gate"]
        self.assertEqual(
            tampered["status"], "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
        )
        self.assertIn(
            "hash mismatch",
            tampered["source_completeness"]["bse_current_delisting_error"],
        )

    def test_bse_current_manifest_missing_fixed_cas_fails_closed(self) -> None:
        sources = _current_reconciliation_sources_with_bse_delist_targets()
        records = tuple(record for source in sources for record in source.records)
        records, sources = integrate_bse_termination_event_manifest(records, sources)
        with tempfile.TemporaryDirectory() as directory:
            missing_root = Path(directory) / "missing-cas"
            with (
                patch.object(
                    master_module,
                    "BSE_CURRENT_DELISTING_STORE_ROOT",
                    missing_root,
                ),
                patch.object(
                    master_module,
                    "_current_wall_clock",
                    return_value=BSE_CURRENT_FIXTURE_NOW,
                ),
            ):
                gate = build_quality_report(
                    records,
                    sources,
                    [
                        "600000.SH",
                        "688287.SH",
                        "000001.SZ",
                        "301192.SZ",
                        "301321.SZ",
                        "920305.BJ",
                        "920680.BJ",
                    ],
                    expected_sse_szse_overlap=2,
                    bse_current_delisting_manifest=(
                        master_module.BSE_CURRENT_DELISTING_MANIFEST_SHA256
                    ),
                    bse_current_delisting_validation_now=BSE_CURRENT_FIXTURE_NOW,
                    bse_current_delisting_as_of=BSE_CURRENT_FIXTURE_NOW,
                )["gate"]
        self.assertEqual(
            gate["status"], "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
        )
        self.assertFalse(
            gate["source_completeness"][
                "bse_current_delisting_source_verified"
            ]
        )

    def test_legacy_unbound_release_does_not_recheck_bse_freshness(self) -> None:
        root = master_module.BSE_CURRENT_DELISTING_STORE_ROOT
        digest = master_module.BSE_CURRENT_DELISTING_MANIFEST_SHA256
        if not (root / "manifests" / f"{digest}.json").is_file():
            self.skipTest("ignored local real BSE current-delisting CAS is not present")
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "master")
            builder = HistoricalSecurityMasterBuilder(store)
            with patch.object(
                master_module,
                "_current_wall_clock",
                return_value=BSE_CURRENT_FIXTURE_NOW,
            ):
                active_codes = ["600000.SH", "000001.SZ"]
                with _admitted_current_observation(active_codes) as (manifest, _):
                    builder.build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "*ST浜戝垱", "2021/8/26", "835305", "920305"],
                            ["2", "娴嬭瘯璇佸埜", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    sse_active_json=_sse_active_bytes(),
                    szse_active_xlsx=_szse_active_xlsx_bytes(),
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    bse_current_delisting_manifest=digest,
                    bse_current_delisting_validation_now=BSE_CURRENT_FIXTURE_NOW,
                    bse_current_delisting_as_of=BSE_CURRENT_FIXTURE_NOW,
                    current_observation_manifest=manifest,
                    )
            with patch.object(
                master_module,
                "_current_wall_clock",
                return_value=BSE_CURRENT_FIXTURE_NOW + timedelta(minutes=16),
            ):
                gate = store.load_gate()
        self.assertNotEqual(
            gate["status"], "BSE_CURRENT_DELISTING_SOURCE_INCOMPLETE"
        )

    def test_bound_current_observation_is_fresh_at_publish_and_audit_only_on_load(
        self,
    ) -> None:
        active_codes = ["600000.SH", "000001.SZ"]
        tdx_observation = master_observation.TDXAShareObservation.capture(
            _fixture_tdx_names(active_codes),
            observed_at=RETRIEVED_AT,
        )
        active_hash = tdx_observation.code_set_sha256
        metadata = {
            "protocol_version": master_observation.PROTOCOL_VERSION,
            "manifest_sha256": "0" * 64,
            "logical_content_sha256": "1" * 64,
            "validated_at": RETRIEVED_AT,
            "as_of": RETRIEVED_AT,
            "tdx_observed_at": RETRIEVED_AT,
            "tdx_names": dict(tdx_observation.names),
            "tdx_code_count": tdx_observation.code_count,
            "tdx_code_set_sha256": active_hash,
            "tdx_identity_sha256": tdx_observation.identity_sha256,
            "pending_listing_manifest_sha256": "2" * 64,
            "pending_listing_logical_content_sha256": "3" * 64,
            "bse_current_delisting_manifest_sha256": "4" * 64,
            "bse_current_delisting_logical_content_sha256": "5" * 64,
            "freshness_required_at_publish": True,
            "immutable_replay_after_publish": True,
        }
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
        )
        records = tuple(record for source in sources for record in source.records)
        quality = build_quality_report(
            records,
            sources,
            active_codes,
            expected_sse_szse_overlap=2,
        )
        completeness = quality["gate"]["source_completeness"]
        completeness.update(
            {
                "current_observation_source_verified": True,
                "current_observation_protocol_version": metadata[
                    "protocol_version"
                ],
                "current_observation_manifest_sha256": metadata[
                    "manifest_sha256"
                ],
                "current_observation_logical_content_sha256": metadata[
                    "logical_content_sha256"
                ],
                "current_observation_validated_at": metadata["validated_at"],
                "current_observation_as_of": metadata["as_of"],
                "current_observation_tdx_observed_at": metadata[
                    "tdx_observed_at"
                ],
                "current_observation_tdx_code_count": metadata["tdx_code_count"],
                "current_observation_tdx_code_set_sha256": active_hash,
                "current_observation_tdx_identity_sha256": metadata[
                    "tdx_identity_sha256"
                ],
                "current_observation_pending_listing_manifest_sha256": metadata[
                    "pending_listing_manifest_sha256"
                ],
                "current_observation_pending_listing_logical_content_sha256": metadata[
                    "pending_listing_logical_content_sha256"
                ],
                "current_observation_bse_current_delisting_manifest_sha256": metadata[
                    "bse_current_delisting_manifest_sha256"
                ],
                "current_observation_bse_current_delisting_logical_content_sha256": metadata[
                    "bse_current_delisting_logical_content_sha256"
                ],
                "pending_listing_manifest_sha256": metadata[
                    "pending_listing_manifest_sha256"
                ],
                "pending_listing_logical_content_sha256": metadata[
                    "pending_listing_logical_content_sha256"
                ],
                "bse_current_delisting_manifest_sha256": metadata[
                    "bse_current_delisting_manifest_sha256"
                ],
                "bse_current_delisting_logical_content_sha256": metadata[
                    "bse_current_delisting_logical_content_sha256"
                ],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "master")
            with patch.object(
                master_module,
                "_normalize_current_observation_reference",
                return_value=(object(), metadata),
            ) as replay:
                store.publish(
                    sources=sources,
                    records=records,
                    quality_report=quality,
                    tdx_active_codes=active_codes,
                    current_observation_manifest=metadata["manifest_sha256"],
                )
                gate = store.load_gate()
                attempt = store.load_latest_attempt()
                attempt_gate = attempt["quality_report"]["gate"]
        self.assertEqual(
            [call.kwargs["require_current"] for call in replay.call_args_list],
            [True],
        )
        self.assertEqual(gate["status"], "NOT_BUILT")
        self.assertEqual(
            attempt_gate["status"], "ACTIVE_INTERVAL_SOURCE_INCOMPLETE"
        )
        observation_binding = attempt["manifest"]["current_observation"]
        self.assertNotIn("tdx_names", observation_binding)
        self.assertEqual(
            observation_binding["tdx_identity_sha256"],
            tdx_observation.identity_sha256,
        )
        self.assertEqual(
            attempt["manifest"]["artifacts"]["tdx_identity_snapshot"][
                "content_hash"
            ],
            tdx_observation.identity_sha256,
        )

    def test_dynamic_current_observation_drives_child_replay_and_publish_window(
        self,
    ) -> None:
        fixture_now = datetime(
            2026,
            8,
            13,
            4,
            45,
            tzinfo=timezone(timedelta(hours=8)),
        )
        bse_mapping = _bse_bytes(
            [
                ["1", "*ST娴滄垵鍨?", "2021/8/26", "835305", "920305"],
                ["2", "濞村鐦拠浣稿煖", "2021/11/15", "838680", "920680"],
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _real_shape_current_observation(root, now=fixture_now) as (
                reference,
                observation_store,
                batch,
                pending_artifact,
                bse_artifact,
            ):
                sources = (
                    parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
                    parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
                    parse_bse_code_mapping_html(bse_mapping, retrieved_at=RETRIEVED_AT),
                )
                records = tuple(
                    record for source in sources for record in source.records
                )
                original_observation_replay = (
                    master_observation.SecurityMasterObservationStore.replay
                )
                replay_calls = 0

                def replay_once_then_use_verified_batch(
                    store_self: object,
                    manifest_sha256: str,
                ):
                    nonlocal replay_calls
                    replay_calls += 1
                    if replay_calls == 1:
                        return original_observation_replay(
                            store_self,
                            manifest_sha256,
                        )
                    return batch

                with (
                    patch.object(
                        master_observation.SecurityMasterObservationStore,
                        "replay",
                        new=replay_once_then_use_verified_batch,
                    ),
                    patch.object(
                        pending_listing.PendingListingManifestStore,
                        "replay",
                        return_value=pending_artifact,
                    ),
                    patch.object(
                        bse_current.BSECurrentDelistingManifestStore,
                        "replay",
                        return_value=bse_artifact,
                    ),
                ):
                    report = build_quality_report(
                        records,
                        sources,
                        batch.tdx_a_share.codes,
                        expected_sse_szse_overlap=2,
                        current_observation_manifest=reference,
                        current_observation_store=observation_store,
                    )
                completeness = report["gate"]["source_completeness"]
                self.assertEqual(replay_calls, 1)

                self.assertTrue(
                    completeness["current_observation_source_verified"]
                )
                self.assertEqual(
                    dict(batch.tdx_a_share.names),
                    _fixture_tdx_names(batch.tdx_a_share.codes),
                )
                self.assertEqual(
                    completeness["current_observation_tdx_identity_sha256"],
                    batch.tdx_a_share.identity_sha256,
                )
                self.assertEqual(
                    completeness["pending_listing_manifest_sha256"],
                    batch.pending_listing.manifest_sha256,
                )
                self.assertEqual(
                    completeness["bse_current_delisting_manifest_sha256"],
                    batch.bse_current_delisting.manifest_sha256,
                )
                self.assertNotEqual(
                    batch.pending_listing.manifest_sha256,
                    master_module.PENDING_LISTING_MANIFEST_SHA256,
                )
                self.assertNotEqual(
                    batch.bse_current_delisting.manifest_sha256,
                    master_module.BSE_CURRENT_DELISTING_MANIFEST_SHA256,
                )

                with self.assertRaisesRegex(
                    HistoricalSecurityMasterBlockedError,
                    "TDX code set does not match",
                ), patch.object(
                    master_module,
                    "_normalize_current_observation_reference",
                    return_value=(
                        batch,
                        {
                            "tdx_code_set_sha256": batch.tdx_a_share.code_set_sha256,
                        },
                    ),
                ):
                    build_quality_report(
                        records,
                        sources,
                        batch.tdx_a_share.codes[:-1],
                        expected_sse_szse_overlap=2,
                        current_observation_manifest=reference,
                        current_observation_store=observation_store,
                    )

                wrong_root = root / "wrong-observation-root"
                wrong_root.mkdir()
                wrong_store = master_observation.SecurityMasterObservationStore(
                    wrong_root,
                    policy=master_observation.SecurityMasterObservationPolicy(
                        pending_cas_root=master_module.PENDING_LISTING_STORE_ROOT,
                        bse_current_delisting_cas_root=(
                            master_module.BSE_CURRENT_DELISTING_STORE_ROOT
                        ),
                        minimum_tdx_code_count=6,
                    ),
                )
                with self.assertRaisesRegex(
                    HistoricalSecurityMasterBlockedError,
                    "root is not policy-bound",
                ):
                    build_quality_report(
                        records,
                        sources,
                        batch.tdx_a_share.codes,
                        expected_sse_szse_overlap=2,
                        current_observation_manifest=reference,
                        current_observation_store=wrong_store,
                    )

                observed_at = datetime.fromisoformat(batch.tdx_a_share.observed_at)
                wrong_digest_artifact, wrong_digest_metadata = (
                    master_module._replay_pending_listing_artifact(
                        manifest=master_module.PENDING_LISTING_MANIFEST_SHA256,
                        store=pending_listing.PendingListingManifestStore(
                            pending_listing.PendingListingRawCAS(
                                master_module.PENDING_LISTING_STORE_ROOT
                            )
                        ),
                        tdx_snapshot_observed_at=observed_at,
                        validation_now=batch.validated_at,
                        as_of=batch.as_of,
                        expected_manifest_sha256=(
                            batch.pending_listing.manifest_sha256
                        ),
                        expected_logical_content_sha256=(
                            batch.pending_listing.logical_content_sha256
                        ),
                        prevalidated_observation=True,
                    )
                )
                self.assertIsNone(wrong_digest_artifact)
                self.assertIn(
                    "policy-bound release",
                    wrong_digest_metadata["error"],
                )

                with patch.object(
                    pending_listing.PendingListingManifestStore,
                    "replay",
                    return_value=pending_artifact,
                ):
                    wrong_logical_artifact, wrong_logical_metadata = (
                        master_module._replay_pending_listing_artifact(
                            manifest=batch.pending_listing.manifest_sha256,
                            store=pending_listing.PendingListingManifestStore(
                                pending_listing.PendingListingRawCAS(
                                    master_module.PENDING_LISTING_STORE_ROOT
                                )
                            ),
                            tdx_snapshot_observed_at=observed_at,
                            validation_now=batch.validated_at,
                            as_of=batch.as_of,
                            expected_manifest_sha256=(
                                batch.pending_listing.manifest_sha256
                            ),
                            expected_logical_content_sha256="f" * 64,
                            prevalidated_observation=True,
                        )
                    )
                self.assertIsNone(wrong_logical_artifact)
                self.assertIn(
                    "not admitted",
                    wrong_logical_metadata["error"],
                )

                with patch.object(
                    bse_current.BSECurrentDelistingManifestStore,
                    "replay",
                    return_value=bse_artifact,
                ):
                    wrong_bse_logical, wrong_bse_metadata = (
                        master_module._replay_bse_current_delisting_artifact(
                            manifest=batch.bse_current_delisting.manifest_sha256,
                            store=bse_current.BSECurrentDelistingManifestStore(
                                bse_current.BSECurrentDelistingCAS(
                                    master_module.BSE_CURRENT_DELISTING_STORE_ROOT
                                )
                            ),
                            tdx_snapshot_observed_at=observed_at,
                            validation_now=batch.validated_at,
                            as_of=batch.as_of,
                            expected_manifest_sha256=(
                                batch.bse_current_delisting.manifest_sha256
                            ),
                            expected_logical_content_sha256="e" * 64,
                            prevalidated_observation=True,
                        )
                    )
                self.assertIsNone(wrong_bse_logical)
                self.assertIn("not admitted", wrong_bse_metadata["error"])

                with patch.object(
                    master_module,
                    "_current_wall_clock",
                    return_value=fixture_now + timedelta(minutes=5, seconds=1),
                ), patch.object(
                    master_observation.SecurityMasterObservationStore,
                    "replay",
                    return_value=batch,
                ), self.assertRaisesRegex(
                    HistoricalSecurityMasterBlockedError,
                    "five-minute historical publication window",
                ):
                    master_module._normalize_current_observation_reference(
                        reference,
                        store=observation_store,
                        require_current=True,
                    )

                with patch.object(
                    master_module,
                    "_current_wall_clock",
                    return_value=fixture_now + timedelta(days=30),
                ), patch.object(
                    master_observation.SecurityMasterObservationStore,
                    "replay",
                    return_value=batch,
                ):
                    replayed, metadata = (
                        master_module._normalize_current_observation_reference(
                            reference,
                            store=observation_store,
                            require_current=False,
                        )
                    )
                self.assertEqual(
                    replayed.logical_content_sha256,
                    batch.logical_content_sha256,
                )
                self.assertEqual(
                    metadata["manifest_sha256"],
                    reference.manifest_sha256,
                )
                self.assertEqual(
                    metadata["tdx_names"],
                    dict(batch.tdx_a_share.names),
                )
                self.assertEqual(
                    metadata["tdx_identity_sha256"],
                    batch.tdx_a_share.identity_sha256,
                )

    def test_publish_rejects_caller_observation_summary(self) -> None:
        active_codes = ["600000.SH", "000001.SZ"]
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
        )
        records = tuple(record for source in sources for record in source.records)
        quality = build_quality_report(
            records,
            sources,
            active_codes,
            expected_sse_szse_overlap=2,
        )
        completeness = quality["gate"]["source_completeness"]
        completeness.update(
            {
                "current_observation_source_verified": True,
                "current_observation_protocol_version": (
                    master_observation.PROTOCOL_VERSION
                ),
                "current_observation_manifest_sha256": "0" * 64,
                "current_observation_logical_content_sha256": "1" * 64,
                "current_observation_validated_at": RETRIEVED_AT,
                "current_observation_tdx_code_set_sha256": "2" * 64,
                "current_observation_tdx_identity_sha256": "3" * 64,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            store = HistoricalSecurityMasterStore(Path(directory) / "master")
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "cold-replayed|cold replay",
            ):
                store.publish(
                    sources=sources,
                    records=records,
                    quality_report=quality,
                    tdx_active_codes=active_codes,
                    current_observation_manifest="0" * 64,
                )

    def test_fixed_real_pending_manifest_cold_replays_through_master(self) -> None:
        cas_root = master_module.PENDING_LISTING_STORE_ROOT
        digest = master_module.PENDING_LISTING_MANIFEST_SHA256
        manifest_path = cas_root / "sha256" / digest[:2] / digest
        if not manifest_path.is_file():
            self.skipTest("ignored local real pending-listing CAS is not present")
        sources = _current_reconciliation_sources()
        records = tuple(record for source in sources for record in source.records)
        with patch.object(
            master_module,
            "_current_wall_clock",
            return_value=PENDING_FIXTURE_NOW,
        ):
            gate = build_quality_report(
                records,
                sources,
                [
                    "600000.SH",
                    "688287.SH",
                    *sorted(master_module.PENDING_LISTING_RECONCILIATION_CODES),
                    "000001.SZ",
                    "301192.SZ",
                    "301321.SZ",
                    "920305.BJ",
                ],
                expected_sse_szse_overlap=2,
                pending_listing_manifest=digest,
            )["gate"]

        completeness = gate["source_completeness"]
        self.assertTrue(completeness["pending_listing_status_source_verified"])
        self.assertEqual(
            completeness["pending_listing_protocol_version"],
            "cn-pending-listing-official-evidence-v2",
        )
        self.assertEqual(completeness["pending_listing_manifest_sha256"], digest)
        self.assertEqual(
            completeness["pending_listing_logical_content_sha256"],
            master_module.PENDING_LISTING_LOGICAL_SHA256,
        )
        self.assertEqual(completeness["pending_listing_raw_source_count"], 12)
        self.assertEqual(len(completeness["pending_listing_raw_hashes"]), 12)

    def test_risk_warning_conflicts_or_missing_tdx_codes_fail_reconciliation(self) -> None:
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_sse_active_json(_sse_active_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(), retrieved_at=RETRIEVED_AT
            ),
        )
        records = tuple(record for source in sources for record in source.records)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_store, missing_reference, _artifact = (
                _sealed_risk_warning_manifest(root / "missing")
            )
            missing_gate = build_quality_report(
                records,
                sources,
                ["600000.SH", "600053.SH", "000001.SZ", "920305.BJ"],
                expected_sse_szse_overlap=2,
                sse_risk_warning_manifest=missing_reference,
                sse_risk_warning_store=missing_store,
            )["gate"]

            duplicate_store, duplicate_reference, _artifact = (
                _sealed_risk_warning_manifest(
                    root / "duplicate",
                    main_rows=[("600000", "ST重复")],
                )
            )
            duplicate_gate = build_quality_report(
                records,
                sources,
                ["600000.SH", "688022.SH", "000001.SZ", "920305.BJ"],
                expected_sse_szse_overlap=2,
                sse_risk_warning_manifest=duplicate_reference,
                sse_risk_warning_store=duplicate_store,
            )["gate"]

        self.assertEqual(missing_gate["status"], "ACTIVE_RECONCILIATION_FAILED")
        self.assertEqual(
            missing_gate["reconciliation"][
                "sse_current_risk_warning_missing_from_tdx_count"
            ],
            1,
        )
        self.assertEqual(duplicate_gate["status"], "ACTIVE_RECONCILIATION_FAILED")
        self.assertEqual(
            duplicate_gate["reconciliation"][
                "sse_current_risk_warning_duplicate_normal_active_count"
            ],
            1,
        )

    def test_risk_warning_manifest_pair_reference_and_raw_cas_are_reverified(self) -> None:
        sources = (
            parse_sse_delist_json(_sse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_delist_xlsx(_xlsx_bytes(), retrieved_at=RETRIEVED_AT),
            parse_bse_code_mapping_html(_bse_bytes(), retrieved_at=RETRIEVED_AT),
            parse_sse_active_json(_sse_active_bytes(), retrieved_at=RETRIEVED_AT),
            parse_szse_active_xlsx(
                _szse_active_xlsx_bytes(), retrieved_at=RETRIEVED_AT
            ),
        )
        records = tuple(record for source in sources for record in source.records)
        with tempfile.TemporaryDirectory() as directory:
            risk_store, risk_reference, artifact = _sealed_risk_warning_manifest(
                Path(directory)
            )
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "requires both a manifest and CAS store",
            ):
                build_quality_report(
                    records,
                    sources,
                    ["600000.SH", "000001.SZ", "920305.BJ"],
                    expected_sse_szse_overlap=2,
                    sse_risk_warning_manifest=risk_reference,
                )
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "reference metadata is inconsistent",
            ):
                build_quality_report(
                    records,
                    sources,
                    ["600000.SH", "000001.SZ", "920305.BJ"],
                    expected_sse_szse_overlap=2,
                    sse_risk_warning_manifest=replace(
                        risk_reference,
                        byte_count=risk_reference.byte_count + 1,
                    ),
                    sse_risk_warning_store=risk_store,
                )
            Path(artifact.raw_responses[0].object_path).write_bytes(b"tampered")
            with self.assertRaisesRegex(
                HistoricalSecurityMasterBlockedError,
                "canonical manifest replay failed",
            ):
                build_quality_report(
                    records,
                    sources,
                    ["600000.SH", "000001.SZ", "920305.BJ"],
                    expected_sse_szse_overlap=2,
                    sse_risk_warning_manifest=risk_reference.manifest_sha256,
                    sse_risk_warning_store=risk_store,
                )

    def test_builder_with_active_sources_remains_blocked_by_bse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder = HistoricalSecurityMasterBuilder(
                HistoricalSecurityMasterStore(root / "security_master")
            )
            (
                risk_store,
                risk_reference,
                _risk_artifact,
                status7_store,
                status7_reference,
                _status7_artifact,
            ) = _sealed_status7_active_intervals(root)
            active_codes = sorted(
                {
                    "600000.SH",
                    "600053.SH",
                    "688022.SH",
                    "688646.SH",
                    "688826.SH",
                    "688835.SH",
                    "688836.SH",
                    "000001.SZ",
                    "301655.SZ",
                    "301688.SZ",
                    "301697.SZ",
                }
            )
            with _admitted_current_observation(active_codes) as (manifest, _):
                release = builder.build_from_bytes(
                    sse_json=_sse_bytes(),
                    szse_xlsx=_xlsx_bytes(),
                    bse_mapping_html=_bse_bytes(
                        [
                            ["1", "*ST娴滄垵鍨?", "2021/8/26", "835305", "920305"],
                            ["2", "濞村鐦拠浣稿煖", "2021/11/15", "838680", "920680"],
                        ]
                    ),
                    sse_active_json=_sse_active_bytes(
                        [
                            _sse_active_row("600000"),
                            {
                                **_sse_active_row("688646"),
                                "COMPANY_ABBR": "逸飞激光",
                            },
                        ]
                    ),
                    szse_active_xlsx=_szse_active_xlsx_bytes(),
                    tdx_active_codes=active_codes,
                    retrieved_at=RETRIEVED_AT,
                    expected_sse_szse_overlap=2,
                    sse_risk_warning_manifest=risk_reference,
                    sse_risk_warning_store=risk_store,
                    sse_risk_warning_active_intervals_manifest=status7_reference,
                    sse_risk_warning_active_intervals_store=status7_store,
                    current_observation_manifest=manifest,
                )
            self.assertFalse(
                (root / "security_master" / "current.json").exists()
            )
            self.assertTrue(
                (root / "security_master" / "latest_attempt.json").is_file()
            )

        self.assertEqual(release["gate"]["status"], "SOURCE_INCOMPLETE")
        self.assertFalse(release["gate"]["ready"])
        self.assertFalse(release["published"])
        self.assertTrue(
            release["gate"]["source_completeness"]["sse_active_listing_intervals"]
        )
        self.assertTrue(
            release["gate"]["source_completeness"]["szse_active_listing_intervals"]
        )
        self.assertFalse(
            release["gate"]["source_completeness"][
                "bse_termination_and_transfer_events"
            ]
        )

    def test_expected_source_hash_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "hash mismatch"):
            parse_sse_delist_json(
                _sse_bytes(),
                retrieved_at=RETRIEVED_AT,
                expected_hash="0" * 64,
            )

    def test_official_client_is_get_only_and_rejects_redirect(self) -> None:
        class Response:
            status_code = 302
            url = BSE_CODE_MAPPING_URL
            headers = {"Location": "https://example.com/not-official"}
            content = b""

        class Session:
            def get(self, url: str, **kwargs: object) -> Response:
                self.url = url
                self.kwargs = kwargs
                return Response()

        session = Session()
        client = OfficialSecurityMasterClient(session=session)  # type: ignore[arg-type]
        with self.assertRaisesRegex(HistoricalSecurityMasterBlockedError, "HTTP 302"):
            client._get(
                BSE_CODE_MAPPING_URL,
                expected_host="www.bse.cn",
                expected_content=("text/html",),
            )
        self.assertFalse(session.kwargs["allow_redirects"])

    def test_v4_detail_and_audit_read_real_missing_gate_before_any_feature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerV4ResearchService(config, database)
            detail = service.detail()

            with patch.object(service, "_current_v4_batches") as batches:
                with self.assertRaises(ResearchDataBlockedError):
                    service.run_development_audit()
                batches.assert_not_called()

        gate = detail["data_gates"]["historical_universe_master"]
        self.assertEqual(detail["status"], "BLOCKED_DATA")
        self.assertEqual(gate["status"], "NOT_BUILT")
        self.assertFalse(gate["ready"])


if __name__ == "__main__":
    unittest.main()
