from __future__ import annotations

import http.client
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research_platform.early_winner_research import (
    CninfoContractError,
    CninfoDirectProvider,
    EarlyWinnerResearchService,
    MODEL_PARAMETERS,
    ProviderGate,
    ResearchDataBlockedError,
    TdxResearchHttpClient,
    _attach_industry_cross_section,
    _align_weekly_decision_rows,
    _audit_forward_factor_semantics,
    _cninfo_accept_enckey,
    _assert_rpc_field_contract,
    _embargo_head_dates,
    _financial_features,
    _flow_features,
    _evaluate_non_overlapping_portfolio,
    _market_frames_from_rpc,
    _normalize_evidence_refs,
    _purge_tail_dates,
)
from research_platform.storage import Database, _file_sha256
from research_platform.strategies.early_winner import (
    EarlyWinnerRuleStrategy,
    _as_string_list,
    attach_execution_outcomes,
    classify_announcement,
    early_winner_exit_reason,
    effective_publication_time,
    historical_price_limit_ratio,
    is_one_price_limit,
    point_in_time_latest,
    score_rule_candidates,
    technical_feature_row,
)
from research_platform.tests.helpers import temporary_config


def feature_rows(asof: str = "2025-12-26", count: int = 30) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        strength = float(index + 1)
        rows.append(
            {
                "code": f"{600000 + index:06d}.SH",
                "name": f"样本{index}",
                "industry": "制造业",
                "asof": asof,
                "listed_days": 500,
                "valid_days_20": 20,
                "adv20": 200_000_000.0,
                "suspended": False,
                "is_st": False,
                "is_quit": False,
                "industry_momentum": strength,
                "industry_breadth": 0.70,
                "industry_amount_trend": 0.10,
                "revenue_yoy": 35.0 + strength,
                "profit_yoy": 45.0 + strength,
                "gross_margin_change": 1.0,
                "roe": 12.0 + strength / 10,
                "ocf_profit_ratio": 1.10,
                "forecast_revision": 10.0,
                "return_20": strength / 100,
                "return_60": strength / 80,
                "return_120": strength / 70,
                "relative_return_20": strength / 100,
                "relative_return_60": strength / 80,
                "relative_return_120": strength / 70,
                "volume_ratio": 1.6 + strength / 100,
                "amount_ratio": 1.4 + strength / 100,
                "breakout_distance": 0.01 + strength / 1000,
                "ma20_slope": 0.02 + strength / 1000,
                "close": 12.0,
                "ma20": 11.0,
                "ma60": 10.0,
                "event_score": 2.0,
                "northbound_change_ratio": strength,
                "institution_lhb_ratio": strength,
                "institution_holding_change_ratio": strength,
                "shareholder_count_change": -strength,
                "turnover_20": 0.05 + strength / 1000,
                "price_to_ma60": 1.20,
                "valuation_percentile": 0.60,
                "published_at": "2025-12-25T15:00:00",
                "effective_at": "2025-12-25T15:00:00",
                "evidence_refs": [f"fixture:{index}"],
            }
        )
    return rows


class _ReadyTdx:
    def admission_probe(self) -> ProviderGate:
        return ProviderGate(True, "READY", "fixture", "2025-12-26T15:00:00+08:00", {})


class _ReadyCninfo:
    def probe(self) -> ProviderGate:
        return ProviderGate(True, "READY", "fixture", "2025-12-26T15:00:00+08:00", {})


class _FixtureCninfo(CninfoDirectProvider):
    def __init__(self, *, drift: bool = False) -> None:
        super().__init__(
            max_attempts=1,
            announcement_batch_size=2,
            industry_workers=1,
        )
        self._stock_org_ids = {
            "000001": "gssz0000001",
            "000002": "gssz0000002",
        }
        self.drift = drift
        self.announcement_pages: list[int] = []

    def _request_json(self, url: str, *, data=None, headers=None, method: str):  # type: ignore[no-untyped-def]
        if url == self.announcement_url:
            page = int(data["pageNum"])
            self.announcement_pages.append(page)
            raw = {
                "secCode": "000001" if page == 1 else "000002",
                "secName": "平安银行" if page == 1 else "万科A",
                "orgId": f"gssz000000{page}",
                "announcementId": f"122500000{page}",
                "announcementTitle": "业绩预告" if page == 1 else "重大合同公告",
                "announcementTime": 1_786_118_400_000 + (page - 1) * 86_400_000,
                "adjunctUrl": f"finalpage/2026-08-08/122500000{page}.PDF",
                "adjunctType": "PDF",
                "adjunctSize": 100 + page,
            }
            if self.drift:
                raw.pop("adjunctUrl")
            return {
                "totalAnnouncement": 2,
                "totalpages": 2,
                "announcements": [raw],
            }
        if url.startswith(self.industry_url):
            return {
                "resultcode": 200,
                "resultmsg": "success",
                "records": [
                    {
                        "SECCODE": "000001",
                        "VARYDATE": "2019-05-08",
                        "F001V": "008002",
                        "F002V": "巨潮行业分类标准",
                        "F003V": "Z07010101",
                        "F004V": "金融",
                        "F005V": "银行",
                        "F006V": "银行",
                        "F007V": "综合性银行",
                    }
                ],
            }
        raise AssertionError(url)


class EarlyWinnerTests(unittest.TestCase):
    def test_weekly_decision_alignment_marks_stale_last_bar_suspended(self) -> None:
        rows = _align_weekly_decision_rows(
            [
                {"code": "A", "asof": "2024-01-04", "suspended": False},
                {"code": "B", "asof": "2024-01-05", "suspended": False},
            ],
            "2024-01-05",
        )
        self.assertEqual([item["asof"] for item in rows], ["2024-01-05", "2024-01-05"])
        self.assertEqual([item["last_bar_at"] for item in rows], ["2024-01-04", "2024-01-05"])
        self.assertEqual([item["suspended"] for item in rows], [True, False])

    def test_missing_raw_valuation_uses_neutral_cross_section_percentile(self) -> None:
        rows = [
            {
                "industry": "fixture",
                "relative_return_20": 0.1,
                "relative_return_60": 0.2,
                "relative_return_120": 0.3,
                "return_20": 0.1,
                "amount_ratio": 1.2,
            },
            {
                "industry": "fixture",
                "relative_return_20": 0.2,
                "relative_return_60": 0.3,
                "relative_return_120": 0.4,
                "return_20": -0.1,
                "amount_ratio": 0.8,
            },
        ]
        _attach_industry_cross_section(rows)
        self.assertEqual([item["valuation_percentile"] for item in rows], [0.5, 0.5])

    def test_history_announcement_chunks_resume_from_frozen_cache(self) -> None:
        class _ChunkProvider:
            announcement_batch_size = 2

            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def fetch_announcements(self, start_date, end_date, *, codes):  # type: ignore[no-untyped-def]
                chunk = tuple(codes)
                self.calls.append(chunk)
                return [
                    {
                        "code": code,
                        "announcement_id": f"{code}-{start_date}-{end_date}",
                        "published_at": f"{end_date[:4]}-12-31T15:00:00",
                    }
                    for code in chunk
                ]

        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            provider = _ChunkProvider()
            service = EarlyWinnerResearchService(
                config,
                database,
                cninfo_provider=provider,  # type: ignore[arg-type]
            )
            codes = ["600000.SH", "600001.SH", "600002.SH"]
            first = service._load_or_fetch_history_announcements(year=2024, codes=codes)
            second = service._load_or_fetch_history_announcements(year=2024, codes=codes)
            batches = database.query(
                "SELECT * FROM research_data_batches WHERE dataset='announcements_history_chunk'"
            )
        self.assertEqual(first, second)
        self.assertEqual(provider.calls, [("600000.SH", "600001.SH"), ("600002.SH",)])
        self.assertEqual(len(batches), 2)

    def test_tdx_timeout_is_retried_for_read_rpc(self) -> None:
        class _Response:
            status = 200

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return False

            @staticmethod
            def read() -> bytes:
                return b'{"result": {"ErrorId": "0", "Value": ["ok"]}}'

        client = TdxResearchHttpClient(max_attempts=2)
        with patch(
            "research_platform.early_winner_research.urllib.request.urlopen",
            side_effect=[TimeoutError("fixture timeout"), _Response()],
        ) as mocked:
            result = client.call("get_fixture", {})
        self.assertEqual(result, ["ok"])
        self.assertEqual(mocked.call_count, 2)

    def test_tdx_rpc_has_a_wall_clock_deadline(self) -> None:
        release = threading.Event()

        def blocking_open(*args, **kwargs):  # type: ignore[no-untyped-def]
            release.wait(timeout=2)
            raise TimeoutError("released fixture")

        client = TdxResearchHttpClient(timeout_seconds=0.05, max_attempts=3)
        started = time.monotonic()
        try:
            with patch(
                "research_platform.early_winner_research.urllib.request.urlopen",
                side_effect=blocking_open,
            ) as mocked:
                with self.assertRaisesRegex(
                    ResearchDataBlockedError,
                    "hard deadline",
                ):
                    client.call("get_fixture", {})
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(mocked.call_count, 1)

    def test_cninfo_incomplete_chunk_is_retried(self) -> None:
        class _Response:
            def __init__(self, payload: bytes | Exception) -> None:
                self.payload = payload

            @staticmethod
            def raise_for_status() -> None:
                return None

            @property
            def content(self) -> bytes:
                if isinstance(self.payload, Exception):
                    raise self.payload
                return self.payload

        provider = CninfoDirectProvider(max_attempts=2)
        responses = [
            _Response(http.client.IncompleteRead(b"{", 10)),
            _Response(b'{"ok": true}'),
        ]
        with patch(
            "research_platform.early_winner_research.requests.Session.request",
            side_effect=responses,
        ) as mocked:
            payload = provider._request_json(
                "https://example.invalid",
                method="GET",
            )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(mocked.call_count, 2)

    def test_configured_tdx_root_is_a_supported_portable_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tdx-mock"
            (root / "PYPlugins" / "user").mkdir(parents=True)
            (root / "TdxW.exe").write_bytes(b"fixture")
            client = TdxResearchHttpClient(tdx_root=root)
            install = client._configured_install()
        self.assertIsNotNone(install)
        self.assertEqual(install["source"], "configured_root")
        self.assertEqual(Path(install["location"]), root.resolve())

    def test_cninfo_direct_announcement_pagination_and_original_url(self) -> None:
        provider = _FixtureCninfo()
        records = provider.fetch_announcements(
            "20260801",
            "20260811",
            codes=["000001.SZ", "000002.SZ"],
        )
        self.assertEqual(provider.announcement_pages, [1, 2])
        self.assertEqual([item["code"] for item in records], ["000001.SZ", "000002.SZ"])
        self.assertTrue(records[0]["source_url"].startswith("https://static.cninfo.com.cn/"))
        self.assertEqual(len(records[0]["raw_hash"]), 64)
        self.assertEqual(records[0]["announcement_id"], "1225000001")
        self.assertEqual(records[0]["publication_precision"], "DATE_ONLY_CONSERVATIVE_AFTER_CLOSE")
        self.assertTrue(records[0]["effective_at"].endswith("15:00:01"))

    def test_cninfo_uses_calculated_page_count_when_reported_pages_are_short(self) -> None:
        class _ShortPageMetadataProvider(CninfoDirectProvider):
            def __init__(self) -> None:
                super().__init__(max_attempts=1, announcement_batch_size=50)
                self._stock_org_ids = {"000001": "gssz0000001"}
                self.pages: list[int] = []

            def _request_json(self, url: str, *, data=None, headers=None, method: str):  # type: ignore[no-untyped-def]
                page = int(data["pageNum"])
                self.pages.append(page)
                count = 30 if page == 1 else 1
                offset = 0 if page == 1 else 30
                return {
                    "totalAnnouncement": 31,
                    "totalpages": 1,
                    "announcements": [
                        {
                            "secCode": "000001",
                            "secName": "fixture",
                            "orgId": "gssz0000001",
                            "announcementId": f"fixture-{offset + index}",
                            "announcementTitle": "fixture",
                            "announcementTime": 1_700_000_000_000 + index,
                            "adjunctUrl": f"fixture-{offset + index}.PDF",
                        }
                        for index in range(count)
                    ],
                }

        provider = _ShortPageMetadataProvider()
        records = provider.fetch_announcements(
            "20240101",
            "20240131",
            codes=["000001.SZ"],
        )
        self.assertEqual(provider.pages, [1, 2])
        self.assertEqual(len(records), 31)

    def test_cninfo_saturated_result_is_split_by_code(self) -> None:
        class _SaturatedProvider(CninfoDirectProvider):
            def __init__(self) -> None:
                super().__init__(max_attempts=1, announcement_batch_size=50)
                self.batches: list[tuple[str, ...]] = []

            def _fetch_announcement_page(self, codes, start_date, end_date, *, page):  # type: ignore[no-untyped-def]
                batch = tuple(codes)
                self.batches.append(batch)
                code = codes[0]
                return {
                    "totalAnnouncement": 3_000 if len(codes) > 1 else 1,
                    "totalpages": 100 if len(codes) > 1 else 1,
                    "announcements": [
                        {
                            "secCode": code,
                            "secName": "fixture",
                            "orgId": f"org-{code}",
                            "announcementId": f"announcement-{code}",
                            "announcementTitle": "fixture",
                            "announcementTime": 1_700_000_000_000,
                            "adjunctUrl": f"{code}.PDF",
                        }
                    ],
                }

        provider = _SaturatedProvider()
        records = provider.fetch_announcements(
            "20240101",
            "20240131",
            codes=["000001.SZ", "000002.SZ"],
        )
        self.assertEqual(
            provider.batches,
            [("000001", "000002"), ("000001",), ("000002",)],
        )
        self.assertEqual([item["code"] for item in records], ["000001.SZ", "000002.SZ"])

    def test_cninfo_incomplete_pagination_retries_whole_code_group(self) -> None:
        class _ChangingSnapshotProvider(CninfoDirectProvider):
            def __init__(self) -> None:
                super().__init__(max_attempts=2, announcement_batch_size=50)
                self.first_pages = 0

            def _fetch_announcement_page(self, codes, start_date, end_date, *, page):  # type: ignore[no-untyped-def]
                self.first_pages += 1
                count = 1 if self.first_pages == 1 else 2
                return {
                    "totalAnnouncement": 2,
                    "totalpages": 1,
                    "announcements": [
                        {
                            "secCode": "000001",
                            "secName": "fixture",
                            "orgId": "org-000001",
                            "announcementId": f"announcement-{index}",
                            "announcementTitle": "fixture",
                            "announcementTime": 1_700_000_000_000 + index,
                            "adjunctUrl": f"fixture-{index}.PDF",
                        }
                        for index in range(count)
                    ],
                }

        provider = _ChangingSnapshotProvider()
        records = provider.fetch_announcements(
            "20240101",
            "20240131",
            codes=["000001.SZ"],
        )
        self.assertEqual(provider.first_pages, 2)
        self.assertEqual(len(records), 2)

    def test_cninfo_incomplete_multi_code_group_is_split_after_retries(self) -> None:
        class _IncompleteGroupProvider(CninfoDirectProvider):
            def __init__(self) -> None:
                super().__init__(max_attempts=1, announcement_batch_size=50)
                self.batches: list[tuple[str, ...]] = []

            def _fetch_announcement_page(self, codes, start_date, end_date, *, page):  # type: ignore[no-untyped-def]
                batch = tuple(codes)
                self.batches.append(batch)
                returned_codes = list(codes[:1]) if len(codes) > 1 else list(codes)
                return {
                    "totalAnnouncement": 2 if len(codes) > 1 else 1,
                    "totalpages": 1,
                    "announcements": [
                        {
                            "secCode": code,
                            "secName": "fixture",
                            "orgId": f"org-{code}",
                            "announcementId": f"announcement-{code}",
                            "announcementTitle": "fixture",
                            "announcementTime": 1_700_000_000_000,
                            "adjunctUrl": f"{code}.PDF",
                        }
                        for code in returned_codes
                    ],
                }

        provider = _IncompleteGroupProvider()
        records = provider.fetch_announcements(
            "20240101",
            "20240131",
            codes=["000001.SZ", "000002.SZ"],
        )
        self.assertEqual(
            provider.batches,
            [("000001", "000002"), ("000001",), ("000002",)],
        )
        self.assertEqual([item["code"] for item in records], ["000001.SZ", "000002.SZ"])

    def test_cninfo_direct_industry_contract_and_enckey(self) -> None:
        provider = _FixtureCninfo()
        records = provider.fetch_industry_changes(
            ["000001.SZ"],
            start_date="19900101",
            end_date="20260811",
        )
        self.assertEqual(records[0]["industry"], "综合性银行")
        self.assertEqual(records[0]["industry_standard_code"], "008002")
        self.assertEqual(records[0]["effective_at"], "2019-05-08T00:00:00")
        self.assertEqual(_cninfo_accept_enckey(1_700_000_000), "WltfJaS1dRCcbgz+YSPgFg==")

    def test_cninfo_direct_field_drift_fails_closed(self) -> None:
        with self.assertRaises(CninfoContractError):
            _FixtureCninfo(drift=True).fetch_announcements(
                "20260801",
                "20260811",
                codes=["000001.SZ"],
            )

    def test_prior_high_excludes_current_bar(self) -> None:
        index = pd.bdate_range("2025-01-01", periods=121)
        close = np.linspace(10, 15, len(index))
        frame = pd.DataFrame(
            {
                "Open": close,
                "High": close + 0.10,
                "Low": close - 0.10,
                "Close": close,
                "Volume": np.full(len(index), 1_000_000.0),
                "Amount": np.full(len(index), 120_000_000.0),
            },
            index=index,
        )
        frame.iloc[-1, frame.columns.get_loc("Open")] = 20.0
        frame.iloc[-1, frame.columns.get_loc("Close")] = 20.0
        frame.iloc[-1, frame.columns.get_loc("Low")] = 19.9
        frame.iloc[-1, frame.columns.get_loc("High")] = 99.0
        row = technical_feature_row("600000.SH", frame)
        self.assertIsNotNone(row)
        self.assertLess(float(row["prior_high_60"]), 20.0)
        self.assertGreater(float(row["breakout_distance"]), 0.0)

    def test_event_hard_negative_has_precedence(self) -> None:
        result = classify_announcement("重大订单暨股东拟减持风险提示")
        self.assertEqual(result["event_type"], "REDUCTION")
        self.assertTrue(result["hard_negative"])
        self.assertLess(float(result["score"]), 0)

    def test_publication_after_close_moves_to_next_trading_day(self) -> None:
        effective = effective_publication_time(
            "2025-03-07T18:00:00",
            ["2025-03-07", "2025-03-10"],
        )
        self.assertEqual(effective, pd.Timestamp("2025-03-10T15:00:00"))

    def test_point_in_time_join_ignores_future_publication(self) -> None:
        selected = point_in_time_latest(
            [
                {"published_at": "2025-04-01", "period_end": "2024-12-31", "value": 1},
                {"published_at": "2025-05-01", "period_end": "2025-03-31", "value": 2},
            ],
            "2025-04-15",
        )
        self.assertEqual(selected["value"], 1)

    def test_hard_negative_never_calls_ai_reviewer(self) -> None:
        class _Responses:
            def parse(self, **_: object) -> object:
                raise AssertionError("hard-negative announcement reached AI")

        class _Client:
            responses = _Responses()

        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerResearchService(config, database)
            reviewed = service.review_announcements(
                [{"title": "股东拟减持并提示风险", "published_at": "2025-01-01"}],
                client=_Client(),
            )
        self.assertEqual(reviewed[0]["event_type"], "REDUCTION")
        self.assertEqual(reviewed[0]["ai_review_status"], "NOT_REQUIRED")

    def test_execution_outcomes_use_next_open_and_delay_limit_down_exit(self) -> None:
        index = pd.bdate_range("2025-01-02", periods=70)
        bars = pd.DataFrame(
            {
                "Open": np.full(70, 10.0),
                "High": np.full(70, 10.2),
                "Low": np.full(70, 9.8),
                "Close": np.full(70, 10.0),
                "Volume": np.full(70, 1_000_000.0),
                "Amount": np.full(70, 10_000_000.0),
            },
            index=index,
        )
        decision_position = 2
        entry_position = decision_position + 1
        planned_exit = entry_position + 60
        bars.iloc[planned_exit - 1, bars.columns.get_loc("Close")] = 10.0
        for column in ("Open", "High", "Low", "Close"):
            bars.iloc[planned_exit, bars.columns.get_loc(column)] = 9.0
        bars.iloc[planned_exit + 1, bars.columns.get_loc("Open")] = 9.2
        result = attach_execution_outcomes(
            pd.DataFrame(
                [{
                    "code": "600000.SH",
                    "name": "浦发银行",
                    "asof": index[decision_position],
                    "adv20": 200_000_000.0,
                }]
            ),
            {"600000.SH": bars},
        ).iloc[0]
        self.assertTrue(bool(result["entry_executable"]))
        self.assertEqual(pd.Timestamp(result["entry_time"]), index[entry_position])
        self.assertEqual(int(result["exit_delay_days"]), 1)
        self.assertEqual(pd.Timestamp(result["exit_time"]), index[planned_exit + 1])
        self.assertLessEqual(float(result["order_value"]), 4_000_000.0)

    def test_one_price_limit_up_blocks_next_open_entry(self) -> None:
        index = pd.bdate_range("2025-01-02", periods=70)
        bars = pd.DataFrame(
            {
                "Open": np.full(70, 10.0),
                "High": np.full(70, 10.0),
                "Low": np.full(70, 10.0),
                "Close": np.full(70, 10.0),
                "Volume": np.full(70, 1_000_000.0),
                "Amount": np.full(70, 10_000_000.0),
            },
            index=index,
        )
        for column in ("Open", "High", "Low", "Close"):
            bars.iloc[3, bars.columns.get_loc(column)] = 11.0
        result = attach_execution_outcomes(
            pd.DataFrame([{"code": "600000.SH", "asof": index[2], "adv20": 200_000_000.0}]),
            {"600000.SH": bars},
        ).iloc[0]
        self.assertFalse(bool(result["entry_executable"]))

    def test_one_price_limit_rounds_half_up_to_one_cent(self) -> None:
        def flat(price: float) -> dict[str, float]:
            return {column: price for column in ("Open", "High", "Low", "Close")}

        self.assertFalse(
            is_one_price_limit(flat(11.05), 10.05, ratio=0.10, side="buy")
        )
        self.assertTrue(
            is_one_price_limit(flat(11.06), 10.05, ratio=0.10, side="buy")
        )
        self.assertTrue(
            is_one_price_limit(flat(9.05), 10.05, ratio=0.10, side="sell")
        )
        self.assertFalse(
            is_one_price_limit(flat(9.04), 10.05, ratio=0.10, side="sell")
        )

    def test_historical_price_limit_regimes_follow_effective_dates(self) -> None:
        self.assertEqual(
            historical_price_limit_ratio(
                "300001.SZ", "", "2020-08-21", listed_days=500
            ),
            0.10,
        )
        self.assertEqual(
            historical_price_limit_ratio(
                "300001.SZ", "ST测试", "2020-08-21", listed_days=500
            ),
            0.05,
        )
        self.assertEqual(
            historical_price_limit_ratio(
                "300001.SZ", "", "2020-08-24", listed_days=500
            ),
            0.20,
        )
        self.assertEqual(
            historical_price_limit_ratio(
                "300001.SZ", "ST测试", "2020-08-24", listed_days=500
            ),
            0.20,
        )
        self.assertEqual(
            historical_price_limit_ratio(
                "688001.SH", "*ST测试", "2020-08-24", listed_days=500
            ),
            0.20,
        )
        self.assertIsNone(
            historical_price_limit_ratio(
                "430001.BJ", "", "2021-11-15", listed_days=1
            )
        )
        self.assertEqual(
            historical_price_limit_ratio(
                "430001.BJ", "", "2021-11-16", listed_days=2
            ),
            0.30,
        )

    def test_corporate_action_factor_adjusts_limit_reference_price(self) -> None:
        index = pd.bdate_range("2023-01-02", periods=3)
        bars = pd.DataFrame(
            {
                "Open": [20.0, 11.0, 11.2],
                "High": [20.2, 11.0, 11.3],
                "Low": [19.8, 11.0, 11.1],
                "Close": [20.0, 11.0, 11.2],
                "Volume": 1_000_000.0,
                "Amount": 20_000_000.0,
                "ForwardFactor": [0.5, 1.0, 1.0],
            },
            index=index,
        )

        result = attach_execution_outcomes(
            pd.DataFrame(
                [{"code": "600000.SH", "asof": index[0], "adv20": 200_000_000.0}]
            ),
            {"600000.SH": bars},
            holding_days=1,
            require_forward_factor=True,
        ).iloc[0]

        # The ex-right reference is 20 * 0.5 / 1.0 = 10, making 11.00 the
        # rounded one-price limit-up.  Using the raw prior close would miss it.
        self.assertFalse(bool(result["entry_executable"]))

    def test_corporate_action_factor_produces_total_return_label(self) -> None:
        index = pd.bdate_range("2023-01-02", periods=3)
        bars = pd.DataFrame(
            {
                "Open": [20.0, 20.0, 10.0],
                "High": [20.2, 20.2, 10.2],
                "Low": [19.8, 19.8, 9.8],
                "Close": [20.0, 20.0, 10.0],
                "Volume": 1_000_000.0,
                "Amount": 20_000_000.0,
                "ForwardFactor": [0.5, 0.5, 1.0],
            },
            index=index,
        )

        result = attach_execution_outcomes(
            pd.DataFrame(
                [{"code": "600000.SH", "asof": index[0], "adv20": 200_000_000.0}]
            ),
            {"600000.SH": bars},
            holding_days=1,
            require_forward_factor=True,
        ).iloc[0]

        self.assertTrue(bool(result["entry_executable"]))
        self.assertAlmostEqual(float(result["forward_return_1"]), 0.0)
        self.assertEqual(float(result["entry_forward_factor"]), 0.5)
        self.assertEqual(float(result["exit_forward_factor"]), 1.0)

    def test_forward_factor_gate_is_explicit_and_backward_compatible(self) -> None:
        index = pd.bdate_range("2023-01-02", periods=3)
        bars = pd.DataFrame(
            {
                "Open": [10.0, 10.0, 10.2],
                "High": [10.2, 10.2, 10.3],
                "Low": [9.8, 9.8, 10.1],
                "Close": [10.0, 10.0, 10.2],
                "Volume": 1_000_000.0,
                "Amount": 10_000_000.0,
            },
            index=index,
        )
        features = pd.DataFrame(
            [{"code": "600000.SH", "asof": index[0], "adv20": 200_000_000.0}]
        )

        compatible = attach_execution_outcomes(
            features, {"600000.SH": bars}, holding_days=1
        ).iloc[0]
        strict = attach_execution_outcomes(
            features,
            {"600000.SH": bars},
            holding_days=1,
            require_forward_factor=True,
        ).iloc[0]

        self.assertTrue(bool(compatible["entry_executable"]))
        self.assertEqual(float(compatible["entry_forward_factor"]), 1.0)
        self.assertEqual(float(compatible["exit_forward_factor"]), 1.0)
        self.assertFalse(bool(strict["entry_executable"]))
        self.assertTrue(pd.isna(strict["forward_return_1"]))

    def test_security_status_change_uses_five_percent_limit_on_exit(self) -> None:
        index = pd.bdate_range("2023-01-02", periods=9)
        bars = pd.DataFrame(
            {
                "Open": np.full(9, 10.0),
                "High": np.full(9, 10.2),
                "Low": np.full(9, 9.8),
                "Close": np.full(9, 10.0),
                "Volume": np.full(9, 1_000_000.0),
                "Amount": np.full(9, 10_000_000.0),
            },
            index=index,
        )
        # Entry is index[1], and a five-session holding plans to exit at
        # index[6].  GP29 adds ST before that exit, so the flat 9.50 bar is a
        # 5% one-price limit-down and execution must wait until index[7].
        for column in ("Open", "High", "Low", "Close"):
            bars.iloc[6, bars.columns.get_loc(column)] = 9.5
        bars.iloc[7, bars.columns.get_loc("Open")] = 9.6
        status_changes = pd.Series([2], index=[index[5]])

        result = attach_execution_outcomes(
            pd.DataFrame(
                [
                    {
                        "code": "600000.SH",
                        "name": "浦发银行",
                        "is_st": False,
                        "asof": index[0],
                        "adv20": 200_000_000.0,
                    }
                ]
            ),
            {"600000.SH": bars},
            holding_days=5,
            security_status_history={"600000.SH": status_changes},
        ).iloc[0]

        self.assertTrue(bool(result["entry_executable"]))
        self.assertEqual(int(result["exit_delay_days"]), 1)
        self.assertEqual(pd.Timestamp(result["exit_time"]), index[7])

    def test_gp15_status_takes_priority_over_calculated_price_limit(self) -> None:
        index = pd.bdate_range("2023-01-02", periods=3)

        def bars_with_flat_entry(price: float) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "Open": [10.0, price, price + 0.2],
                    "High": [10.2, price, price + 0.3],
                    "Low": [9.8, price, price + 0.1],
                    "Close": [10.0, price, price + 0.2],
                    "Volume": 1_000_000.0,
                    "Amount": 10_000_000.0,
                },
                index=index,
            )

        features = pd.DataFrame(
            [{"code": "600000.SH", "asof": index[0], "adv20": 200_000_000.0}]
        )
        gp15_locked = attach_execution_outcomes(
            features,
            {"600000.SH": bars_with_flat_entry(10.5)},
            holding_days=1,
            limit_status_history={
                "600000.SH": pd.Series([2], index=[index[1]])
            },
        ).iloc[0]
        gp15_open = attach_execution_outcomes(
            features,
            {"600000.SH": bars_with_flat_entry(11.0)},
            holding_days=1,
            limit_status_history={
                "600000.SH": pd.Series([0], index=[index[1]])
            },
        ).iloc[0]
        exit_index = pd.bdate_range("2023-02-01", periods=4)
        exit_bars = pd.DataFrame(
            {
                "Open": [10.0, 10.0, 9.5, 9.6],
                "High": [10.2, 10.2, 9.5, 9.8],
                "Low": [9.8, 9.8, 9.5, 9.4],
                "Close": [10.0, 10.0, 9.5, 9.6],
                "Volume": 1_000_000.0,
                "Amount": 10_000_000.0,
            },
            index=exit_index,
        )
        gp15_exit_locked = attach_execution_outcomes(
            pd.DataFrame(
                [
                    {
                        "code": "600000.SH",
                        "asof": exit_index[0],
                        "adv20": 200_000_000.0,
                    }
                ]
            ),
            {"600000.SH": exit_bars},
            holding_days=1,
            limit_status_history={
                "600000.SH": pd.Series([-2], index=[exit_index[2]])
            },
        ).iloc[0]

        # GP15=2 identifies the lock even when 10.50 is not the calculated
        # 10% price; an explicit non-limit GP15 status suppresses fallback even
        # though 11.00 is the calculated limit-up.  GP15=-2 likewise delays a
        # flat 9.50 exit that the calculated 10% rule would otherwise fill.
        self.assertFalse(bool(gp15_locked["entry_executable"]))
        self.assertTrue(bool(gp15_open["entry_executable"]))
        self.assertEqual(int(gp15_exit_locked["exit_delay_days"]), 1)
        self.assertEqual(
            pd.Timestamp(gp15_exit_locked["exit_time"]), exit_index[3]
        )

    def test_weekly_rank_exit_executes_at_following_open(self) -> None:
        index = pd.bdate_range("2025-01-02", periods=80)
        bars = pd.DataFrame(
            {
                "Open": np.full(80, 10.0),
                "High": np.full(80, 10.2),
                "Low": np.full(80, 9.8),
                "Close": np.full(80, 10.0),
                "Volume": np.full(80, 1_000_000.0),
                "Amount": np.full(80, 10_000_000.0),
            },
            index=index,
        )
        state_date = index[10]
        states = pd.DataFrame(
            [{"rank": 41, "close": 10.0, "ma60": 9.0, "event_type": ""}],
            index=[state_date],
        )
        result = attach_execution_outcomes(
            pd.DataFrame(
                [{"code": "600000.SH", "asof": index[2], "adv20": 200_000_000.0}]
            ),
            {"600000.SH": bars},
            weekly_states={"600000.SH": states},
        ).iloc[0]
        self.assertEqual(result["exit_reason"], "RANK_OUTSIDE_40")
        self.assertEqual(pd.Timestamp(result["exit_time"]), index[11])

    def test_exit_policy_uses_rank_buffer_and_hard_negative_priority(self) -> None:
        self.assertIsNone(
            early_winner_exit_reason(current_rank=40, close=10.0, ma60=9.0)
        )
        self.assertEqual(
            early_winner_exit_reason(current_rank=41, close=10.0, ma60=9.0),
            "RANK_OUTSIDE_40",
        )
        self.assertEqual(
            early_winner_exit_reason(
                current_rank=1,
                close=10.0,
                ma60=9.0,
                event_type="REDUCTION",
            ),
            "MAJOR_NEGATIVE_EVENT",
        )

    def test_purge_and_embargo_are_business_day_windows(self) -> None:
        frame = pd.DataFrame({"asof": pd.bdate_range("2025-01-02", periods=100)})
        purged = _purge_tail_dates(frame, 60)
        embargoed = _embargo_head_dates(frame, 20)
        self.assertEqual(len(purged), 40)
        self.assertEqual(len(embargoed), 79)
        self.assertGreater(
            pd.Timestamp(embargoed["asof"].min()),
            pd.Timestamp(frame["asof"].min()) + pd.offsets.BDay(20),
        )

    def test_upstream_field_drift_fails_closed(self) -> None:
        with self.assertRaises(ResearchDataBlockedError):
            _assert_rpc_field_contract(
                {"columns": ["FN183"], "data": []},
                ("FN183", "FN184"),
                "financial",
            )

    def test_professional_rpc_uses_native_table_list_contract(self) -> None:
        class _CaptureClient(TdxResearchHttpClient):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[tuple[str, dict[str, object]]] = []

            def call(self, method: str, params: object) -> object:
                payload = dict(params)  # type: ignore[arg-type]
                self.calls.append((method, payload))
                return {}

        client = _CaptureClient()
        client.fetch_financial_history(["600519.SH"], start_time="20240101", end_time="20241231")
        client.fetch_flow_history(["600519.SH"], start_time="20240101", end_time="20241231")
        client.fetch_consensus_snapshot(["600519.SH"])
        self.assertEqual([name for name, _ in client.calls], [
            "get_financial_data", "get_gpjy_value", "get_gp_one_data",
        ])
        for _, params in client.calls:
            self.assertIn("table_list", params)
            self.assertNotIn("field_list", params)

    def test_historical_professional_requests_use_bounded_batches(self) -> None:
        class _CaptureClient(TdxResearchHttpClient):
            def __init__(self) -> None:
                super().__init__()
                self.batch_lengths: list[int] = []

            def call(self, method: str, params: object) -> object:
                payload = dict(params)  # type: ignore[arg-type]
                self.batch_lengths.append(len(payload["stock_list"]))
                return {}

        client = _CaptureClient()
        codes = [f"{index:06d}.SZ" for index in range(120)]
        client.fetch_financial_history(codes, start_time="20200101", end_time="20241231")
        self.assertEqual(client.batch_lengths, [25, 25, 25, 25, 20])

    def test_market_rpc_column_lists_normalize_to_ohlcva(self) -> None:
        payload = {
            "600519.SH": {
                "Date": ["20250102", "20250103"],
                "Open": [10.0, 10.1],
                "High": [10.2, 10.3],
                "Low": [9.9, 10.0],
                "Close": [10.1, 10.2],
                "Volume": [1000, 1100],
                "Amount": [2.0, 3.0],
                "ErrorId": "0",
            }
        }
        frame = _market_frames_from_rpc(payload, ["600519.SH"])["600519.SH"]
        self.assertEqual(list(frame.columns), ["Open", "High", "Low", "Close", "Volume", "Amount"])
        self.assertEqual(float(frame.iloc[0]["Amount"]), 20_000.0)

    def test_market_rpc_preserves_forward_factor_and_audits_multiplication(self) -> None:
        dates = pd.to_datetime(["2023-06-29", "2023-06-30", "2023-07-03"])
        raw = pd.DataFrame(
            {
                "Close": [1713.71, 1691.00, 1724.10],
                "ForwardFactor": [0.968790, 0.983663, 0.983663],
            },
            index=dates,
        )
        normalized = raw["Close"] * raw["ForwardFactor"]
        front = pd.DataFrame({"Close": normalized / normalized.iloc[-1] * 1724.10})

        audit = _audit_forward_factor_semantics(raw, front)

        self.assertTrue(audit["ready"])
        self.assertEqual(audit["factor_values"], 2)
        self.assertLess(audit["max_adjacent_return_error"], 1e-12)

    def test_forward_factor_semantics_fail_without_observed_change(self) -> None:
        dates = pd.to_datetime(["2023-01-02", "2023-01-03"])
        raw = pd.DataFrame(
            {"Close": [10.0, 10.1], "ForwardFactor": [1.0, 1.0]}, index=dates
        )
        front = pd.DataFrame({"Close": [10.0, 10.1]}, index=dates)

        with self.assertRaises(ResearchDataBlockedError):
            _audit_forward_factor_semantics(raw, front)

    def test_professional_series_use_dates_and_amount_components(self) -> None:
        financial = {
            "batches": [
                {
                    "600519.SH": {
                        "announce_time": ["20240101", "20250101", "20260101"],
                        "FN183": [10.0, 25.0, 99.0],
                        "FN184": [20.0, 35.0, 99.0],
                        "FN197": [8.0, 9.0, 10.0],
                        "FN202": [40.0, 42.0, 30.0],
                        "FN228": [90.0, 100.0, 110.0],
                        "FN285": [0.0, 8.0, 50.0],
                        "FN286": [0.0, 12.0, 60.0],
                        "FN247": [100.0, 110.0, 200.0],
                    }
                }
            ]
        }
        values = _financial_features(
            financial, "600519.SH", pd.Timestamp("2025-06-01")
        )
        self.assertEqual(values["revenue_yoy"], 25.0)
        self.assertEqual(values["profit_yoy"], 35.0)
        self.assertAlmostEqual(values["institution_holding_change_ratio"], 0.10)
        flows = {
            "batches": [
                {
                    "600519.SH": {
                        "GP01": [
                            {"Date": "20240101", "Value": [100.0, 0]},
                            {"Date": "20250101", "Value": [90.0, 0]},
                        ],
                        "GP06": [
                            {"Date": "20250101", "Value": [1000.0, 0]},
                            {"Date": "20250102", "Value": [1100.0, 0]},
                        ],
                        "GP08": [{"Date": "20250101", "Value": [2, 30.0]}],
                        "GP09": [{"Date": "20250101", "Value": [1, 80.0]}],
                    }
                }
            ]
        }
        flow_values = _flow_features(
            flows, "600519.SH", pd.Timestamp("2025-06-01")
        )
        self.assertEqual(flow_values["institution_lhb_ratio"], 50.0)

    def test_fixed_model_seed_is_reproducible(self) -> None:
        from sklearn.ensemble import HistGradientBoostingClassifier

        rng = np.random.default_rng(49)
        features = rng.normal(size=(200, 4))
        target = (features[:, 0] + features[:, 1] > 0).astype(int)
        first = HistGradientBoostingClassifier(**MODEL_PARAMETERS).fit(features, target)
        second = HistGradientBoostingClassifier(**MODEL_PARAMETERS).fit(features, target)
        np.testing.assert_allclose(first.predict_proba(features), second.predict_proba(features))

    def test_legacy_training_and_validation_fail_closed_after_universe_audit(self) -> None:
        rows: list[dict[str, object]] = []
        for asof in pd.date_range("2018-01-31", "2025-12-31", freq="ME"):
            period = feature_rows(str(asof.date()), count=20)
            for index, row in enumerate(period):
                row["published_at"] = f"{asof.date()}T14:00:00"
                row["effective_at"] = f"{asof.date()}T14:00:00"
                row["entry_executable"] = True
                row["forward_return_60"] = index / 100.0
                rows.append(row)
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerResearchService(config, database)
            batch = service.ingest_feature_frame(pd.DataFrame(rows), source="fixture-history")
            self.assertEqual(batch["content_hash"], _file_sha256(Path(batch["path"])))
            with self.assertRaisesRegex(ResearchDataBlockedError, "survivorship-bias"):
                service.train()
            with self.assertRaisesRegex(ResearchDataBlockedError, "survivorship-bias"):
                service.validate()

    def test_legacy_succeeded_history_is_reported_as_retained_but_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerResearchService(config, database)
            now = "2026-08-13T01:00:00+08:00"
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO early_winner_history_builds
                    (build_id, project_id, start_year, end_year, status, expected_shards,
                     completed_shards, calendar_hash, manifest_path, manifest_hash,
                     created_at, updated_at, error)
                    VALUES (?, ?, 2018, 2025, 'SUCCEEDED', 8, 8, 'calendar',
                            'retained.manifest.json', 'legacy-hash', ?, ?, '')""",
                    ("legacy-build", "early_winner_v1", now, now),
                )
            status = service.history_status()
            self.assertEqual(status["status"], "BLOCKED_DATA")
            self.assertEqual(status["artifact_status"], "SUCCEEDED")
            self.assertTrue(status["evidence_retained"])
            self.assertFalse(status["trust_policy"]["ready"])
            self.assertEqual(
                status["trust_policy"]["status"],
                "SUPERSEDED_DATA_QUALITY_REJECTED",
            )
            detail = service.detail()
            self.assertEqual(detail["status"], "BLOCKED_DATA")
            self.assertEqual(
                detail["data_gates"]["feature_history"]["status"],
                "SUPERSEDED_DATA_QUALITY_REJECTED",
            )

    def test_legacy_history_builder_is_retired_before_provider_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerResearchService(config, database)
            with self.assertRaisesRegex(ResearchDataBlockedError, "current-list universe"):
                service.build_history()

    def test_rule_selection_respects_industry_cap_and_emits_no_signals(self) -> None:
        candidates = score_rule_candidates(feature_rows())
        self.assertEqual(len(candidates), 5)
        result = EarlyWinnerRuleStrategy().scan(feature_rows=feature_rows())
        self.assertEqual(result.signals, ())
        self.assertEqual(len(result.candidates), 5)
        self.assertFalse(result.state["trade_signals_enabled"])

    def test_missing_fundamental_and_extreme_heat_block_new_candidates(self) -> None:
        rows = feature_rows()
        rows[0]["revenue_yoy"] = np.nan
        rows[0]["profit_yoy"] = np.nan
        rows[0]["forecast_revision"] = np.nan
        rows[-1]["forecast_revision"] = 0.0
        rows[-1]["price_to_ma60"] = 1.90
        rows[-1]["return_60"] = 9.0
        rows[-1]["turnover_20"] = 9.0
        codes = {item["code"] for item in score_rule_candidates(rows)}
        self.assertNotIn(rows[0]["code"], codes)
        self.assertNotIn(rows[-1]["code"], codes)

    def test_model_target_is_rebuilt_from_forward_returns(self) -> None:
        rows = feature_rows(count=30)
        for index, row in enumerate(rows):
            row["target"] = 1
            row["entry_executable"] = True
            row["forward_return_60"] = index / 100.0
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerResearchService(config, database)
            model_frame, _ = service._model_frame(pd.DataFrame(rows))
        self.assertGreater(int(model_frame["target"].sum()), 0)
        self.assertLess(int(model_frame["target"].sum()), len(model_frame))

    def test_model_features_impute_all_missing_column_with_indicator(self) -> None:
        rows = feature_rows(count=30)
        for index, row in enumerate(rows):
            row["turnover_20"] = np.nan
            row["entry_executable"] = True
            row["forward_return_60"] = index / 100.0
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerResearchService(config, database)
            model_frame, _ = service._model_frame(pd.DataFrame(rows))
        self.assertTrue(np.isfinite(model_frame["turnover_20"]).all())
        self.assertTrue((model_frame["turnover_20"] == 0.0).all())
        self.assertTrue((model_frame["turnover_20__missing"] == 1.0).all())

    def test_numpy_evidence_refs_are_normalized_for_candidate_storage(self) -> None:
        refs = _normalize_evidence_refs(np.array(["cninfo:a", "tdx:b"]))
        self.assertEqual(refs, ["cninfo:a", "tdx:b"])
        self.assertEqual(_as_string_list(np.array(refs)), refs)

    def test_validation_does_not_compound_overlapping_60_day_labels(self) -> None:
        rows: list[dict[str, object]] = []
        dates = pd.date_range("2024-01-05", periods=26, freq="W-FRI")
        for asof in dates:
            for index in range(20):
                rows.append(
                    {
                        "asof": asof,
                        "code": f"{600000 + index:06d}.SH",
                        "industry": f"industry-{index % 5}",
                        "score": float(20 - index),
                        "eligible": True,
                        "entry_executable": True,
                        "target": index == 0,
                        "forward_return_60": 1.0,
                        "exit_time": asof + pd.offsets.BDay(60),
                    }
                )
        metrics, _, _ = _evaluate_non_overlapping_portfolio(
            pd.DataFrame(rows),
            score_column="score",
            eligibility_column="eligible",
        )
        self.assertEqual(metrics["weekly_rank_periods"], 26)
        self.assertLess(metrics["periods"], metrics["weekly_rank_periods"])
        self.assertLessEqual(metrics["periods"], 3)
        self.assertLess(metrics["total_return"], 10.0)

    def test_feature_batch_refresh_persists_candidates_without_paper_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = EarlyWinnerResearchService(
                config,
                database,
                tdx_client=_ReadyTdx(),
                cninfo_provider=_ReadyCninfo(),
            )
            frame = pd.DataFrame(feature_rows())
            batch = service.ingest_feature_frame(frame, source="fixture")
            with self.assertRaisesRegex(ResearchDataBlockedError, "survivorship-bias"):
                service.refresh()
            candidates = service.candidates(method="rule")
            accounts = database.query(
                "SELECT * FROM paper_accounts WHERE strategy_id LIKE 'early_winner_%'"
            )
            signals = database.query(
                "SELECT * FROM signals WHERE strategy_id LIKE 'early_winner_%'"
            )
        self.assertEqual(batch["status"], "SUCCEEDED")
        self.assertEqual(candidates, [])
        self.assertEqual(accounts, [])
        self.assertEqual(signals, [])


if __name__ == "__main__":
    unittest.main()
