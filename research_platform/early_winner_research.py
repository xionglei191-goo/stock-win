from __future__ import annotations

import base64
import hashlib
import http.client
import inspect
import json
import os
import pickle
import platform
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from pydantic import BaseModel, Field

from .config import PlatformConfig
from .storage import Database, _file_sha256
from .strategies.early_winner import (
    EarlyWinnerParameters,
    FEATURE_COLUMNS,
    ML_STRATEGY_ID,
    PROJECT_ID,
    RULE_STRATEGY_ID,
    EarlyWinnerMLStrategy,
    EarlyWinnerRuleStrategy,
    attach_execution_outcomes,
    build_technical_feature_rows,
    classify_announcement,
    early_winner_exit_reason,
    mark_research_universe_eligibility,
    score_rule_candidates,
    select_ml_candidates,
    technical_feature_row,
)


PROJECT_VERSION = "1.0.0"
PROJECT_NAME = "早期强势股识别"
PROJECT_DESCRIPTION = "规则多因子与机器学习并行的研究项目；只输出候选，不产生交易信号。"
MODEL_RANDOM_SEED = 49
MODEL_FEATURE_COLUMNS = tuple(FEATURE_COLUMNS)
MODEL_FEATURE_PIPELINE_VERSION = "early-winner-features-v2"
MODEL_MISSING_VALUE_FILL = 0.0
VALIDATION_HOLDING_TRADING_DAYS = 60
VALIDATION_PERIODS_PER_YEAR = 252.0 / VALIDATION_HOLDING_TRADING_DAYS
MODEL_PARAMETERS = {
    "learning_rate": 0.05,
    "max_iter": 200,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 50,
    "l2_regularization": 1.0,
    "random_state": MODEL_RANDOM_SEED,
}
TDX_FINANCIAL_FIELDS = (
    "FN183",
    "FN184",
    "FN197",
    "FN202",
    "FN228",
    "FN230",
    "FN232",
    "FN234",
    "FN242",
    "FN246",
    "FN247",
    "FN285",
    "FN286",
    "FN313",
    "FN314",
    "FN315",
    "FN319",
    "FN325",
    "FN326",
)
TDX_FLOW_FIELDS = (
    "GP01",
    "GP05",
    "GP06",
    "GP08",
    "GP09",
    "GP23",
    "GP26",
    "GP29",
    "GP35",
    "GP42",
)
TDX_CONSENSUS_FIELDS = tuple(f"GO{index}" for index in range(3, 26))
EVENT_REVIEW_PROMPT_VERSION = "early-winner-event-v1"
HISTORY_BUILDER_VERSION = "early-winner-history-v3"
LEGACY_HISTORY_TRUST_POLICY_VERSION = "early-winner-legacy-history-quarantine-v1"
LEGACY_HISTORY_REJECTION_STATUS = "SUPERSEDED_DATA_QUALITY_REJECTED"
ANNOUNCEMENT_CACHE_VERSION = "cninfo-announcement-v2"
CNINFO_ANNOUNCEMENT_RESULT_CAP = 3_000
CNINFO_INDUSTRY_STANDARD_CODES = ("008013", "008002")


class EventReviewOutput(BaseModel):
    event_type: Literal[
        "EARNINGS_FORECAST",
        "ACQUISITION",
        "PRICE_INCREASE",
        "MAJOR_ORDER",
        "CONTROL_CHANGE",
        "EXPANSION",
        "BUYBACK",
        "NEUTRAL",
    ]
    score: float = Field(ge=-4, le=3)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=300)


class ResearchDataBlockedError(RuntimeError):
    pass


class CninfoContractError(ResearchDataBlockedError):
    pass


class CninfoTransportError(ResearchDataBlockedError):
    pass


@dataclass(frozen=True)
class ProviderGate:
    ready: bool
    status: str
    detail: str
    checked_at: str
    metadata: dict[str, Any]

    def as_record(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "status": self.status,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "metadata": self.metadata,
        }


def _same_windows_path(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _normalize_evidence_refs(value: Any) -> list[str]:
    if value is None or value is pd.NA:
        return []
    if isinstance(value, np.ndarray):
        values = value.tolist()
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        try:
            if bool(pd.isna(value)):
                return []
        except (TypeError, ValueError):
            pass
        values = [value]
    return [str(item) for item in values if item is not None and str(item)]


class TdxResearchHttpClient:
    endpoint = "http://127.0.0.1:17709/"
    registry_paths = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端64",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信专业版",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端(量化模拟)",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信iTendx研究终端",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端(测试)",
    )

    def __init__(
        self,
        timeout_seconds: float = 120.0,
        *,
        tdx_root: Path | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.tdx_root = Path(tdx_root).resolve() if tdx_root is not None else None
        self.max_attempts = max(1, int(max_attempts))

    def call(self, method: str, params: Mapping[str, Any]) -> Any:
        payload = json.dumps(
            {"id": uuid4().hex, "method": method, "params": dict(params)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        last_error: Exception | None = None
        result: Any = None
        for attempt in range(self.max_attempts):
            request = urllib.request.Request(
                self.endpoint,
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            completed = threading.Event()
            outcome: dict[str, Any] = {}

            def read_response() -> None:
                try:
                    with urllib.request.urlopen(
                        request,
                        timeout=self.timeout_seconds,
                    ) as response:
                        if response.status != 200:
                            raise ResearchDataBlockedError(
                                f"TDX HTTP status {response.status}"
                            )
                        outcome["result"] = json.loads(
                            response.read().decode("utf-8")
                        )
                except Exception as exc:  # transferred to the owning build thread
                    outcome["error"] = exc
                finally:
                    completed.set()

            worker = threading.Thread(
                target=read_response,
                name=f"tdx-rpc-{method}",
                daemon=True,
            )
            worker.start()
            if not completed.wait(self.timeout_seconds):
                # urllib's socket timeout is an inactivity timeout and can be reset by
                # a dribbling local response.  The research pipeline needs a true wall
                # clock deadline so a wedged TDX call cannot hold a shard indefinitely.
                raise ResearchDataBlockedError(
                    f"TDX method {method} exceeded the {self.timeout_seconds:g}s hard deadline"
                )
            try:
                if "error" in outcome:
                    raise outcome["error"]
                result = outcome.get("result")
                break
            except ResearchDataBlockedError:
                raise
            except (
                OSError,
                http.client.HTTPException,
                urllib.error.URLError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(0.5 * (2**attempt))
        else:
            raise ResearchDataBlockedError(f"TDX HTTP unavailable: {last_error}") from last_error
        if not isinstance(result, dict):
            raise ResearchDataBlockedError("TDX JSON-RPC response is not an object")
        if result.get("error"):
            raise ResearchDataBlockedError(f"TDX JSON-RPC error: {result['error']}")
        value = result.get("result")
        if isinstance(value, dict) and "ErrorId" in value:
            if str(value.get("ErrorId")) != "0":
                raise ResearchDataBlockedError(
                    f"TDX method {method} failed: {value.get('ErrorInfo') or value.get('Msg') or value}"
                )
            if "Value" in value:
                return value.get("Value")
            return {
                key: item
                for key, item in value.items()
                if key not in {"ErrorId", "ErrorInfo", "Error", "Msg"}
            }
        return value

    def admission_probe(self) -> ProviderGate:
        checked_at = datetime.now().astimezone().isoformat()
        if platform.system() != "Windows":
            return ProviderGate(False, "UNSUPPORTED_OS", platform.system(), checked_at, {})
        registered_installs = self._registered_installs()
        configured_install = self._configured_install()
        installs = list(registered_installs)
        if configured_install is not None and not any(
            _same_windows_path(item.get("location"), configured_install["location"])
            for item in installs
        ):
            installs.append(configured_install)
        if not installs:
            return ProviderGate(
                False,
                "NOT_INSTALLED",
                "注册表和显式 TDX_ROOT 均未发现可用通达信；研究数据读取已关闭",
                checked_at,
                {},
            )
        try:
            import psutil

            processes = [
                process.info
                for process in psutil.process_iter(["name", "exe"])
                if str(process.info.get("name") or "").lower() == "tdxw.exe"
            ]
        except Exception as exc:
            return ProviderGate(False, "PROCESS_CHECK_FAILED", str(exc), checked_at, {})
        if not processes:
            return ProviderGate(
                False,
                "CLIENT_NOT_RUNNING",
                "TdxW.exe 未运行或未登录",
                checked_at,
                {"installs": installs},
            )
        if configured_install is not None and not registered_installs:
            expected_executable = str(Path(configured_install["location"]) / "TdxW.exe")
            matching_processes = [
                process
                for process in processes
                if _same_windows_path(process.get("exe"), expected_executable)
            ]
            if not matching_processes:
                return ProviderGate(
                    False,
                    "CLIENT_PATH_MISMATCH",
                    "TdxW.exe 已运行，但不是显式 TDX_ROOT 中的客户端",
                    checked_at,
                    {
                        "installs": installs,
                        "processes": processes,
                        "expected_executable": expected_executable,
                    },
                )
        try:
            with socket.create_connection(("127.0.0.1", 17709), timeout=3):
                pass
            matches = self.call("get_match_stkinfo", {"key_word": "茅台"})
            stocks = self.call("get_stock_list", {"market": "5", "list_type": 0})
            codes = _extract_stock_codes(stocks)
            if not codes:
                raise ResearchDataBlockedError("get_stock_list returned no A-share codes")
            representative_codes = [
                code
                for code in ("000001.SZ", "600519.SH", "300750.SZ", "688981.SH")
                if code in codes
            ]
            probe_codes = representative_codes or codes[:4]
            financial = self.call(
                "get_financial_data",
                {
                    "stock_list": probe_codes,
                    "table_list": list(TDX_FINANCIAL_FIELDS),
                    "start_time": "20240101",
                    "end_time": datetime.now().strftime("%Y%m%d"),
                    "report_type": "announce_time",
                },
            )
            flows = self.call(
                "get_gpjy_value",
                {
                    "stock_list": probe_codes,
                    "table_list": list(TDX_FLOW_FIELDS),
                    "start_time": "20240101",
                    "end_time": datetime.now().strftime("%Y%m%d"),
                },
            )
            consensus = self.call(
                "get_gp_one_data",
                {"stock_list": probe_codes, "table_list": list(TDX_CONSENSUS_FIELDS)},
            )
            factor_probe_code = "600519.SH" if "600519.SH" in codes else probe_codes[0]
            market_front = self.fetch_market_range(
                [factor_probe_code],
                start_time="20230620",
                end_time="20230705",
                dividend_type="front",
                batch_size=1,
            )
            market_raw = self.fetch_market_range(
                [factor_probe_code],
                start_time="20230620",
                end_time="20230705",
                dividend_type="none",
                batch_size=1,
                include_forward_factor=True,
            )
            stock_info = self.fetch_stock_info(probe_codes[:1])
            _assert_rpc_field_contract(financial, TDX_FINANCIAL_FIELDS, "financial")
            _assert_rpc_field_contract(flows, TDX_FLOW_FIELDS, "institutional_flows")
            _assert_rpc_field_contract(consensus, TDX_CONSENSUS_FIELDS, "consensus")
            required_bar_fields = {"Open", "High", "Low", "Close", "Volume", "Amount"}
            for adjustment, frames in (("front", market_front), ("none", market_raw)):
                probe_frame = frames.get(factor_probe_code)
                if (
                    probe_frame is None
                    or probe_frame.empty
                    or not required_bar_fields.issubset(probe_frame.columns)
                ):
                    raise ResearchDataBlockedError(
                        f"TDX market {adjustment} contract missing OHLCVA fields"
                    )
            factor_audit = _audit_forward_factor_semantics(
                market_raw[factor_probe_code], market_front[factor_probe_code]
            )
            required_info = {"Name", "IsSTGP", "IsQuitGP", "J_start", "rs_hyname"}
            if not required_info.issubset(stock_info.get(probe_codes[0], {})):
                raise ResearchDataBlockedError("TDX stock-info contract drift")
        except ResearchDataBlockedError as exc:
            return ProviderGate(
                False,
                "CONTRACT_FAILED",
                str(exc),
                checked_at,
                {"installs": installs, "processes": processes},
            )
        return ProviderGate(
            True,
            "READY",
            "TDX HTTP、财务、资金和一致预期字段准入通过",
            checked_at,
            {
                "installs": installs,
                "processes": processes,
                "probe_codes": probe_codes,
                "match_shape": _shape(matches),
                "financial_shape": _shape(financial),
                "flow_shape": _shape(flows),
                "consensus_shape": _shape(consensus),
                "market_front_rows": len(market_front[factor_probe_code]),
                "market_raw_rows": len(market_raw[factor_probe_code]),
                "forward_factor_semantics": factor_audit,
                "stock_info_fields": sorted(stock_info[probe_codes[0]]),
            },
        )

    def list_a_shares(self) -> tuple[list[str], dict[str, str]]:
        value = self.call("get_stock_list", {"market": "5", "list_type": 1})
        codes = _extract_stock_codes(value)
        names: dict[str, str] = {}
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("Code") or item.get("code") or "").upper()
                if code in codes:
                    names[code] = str(item.get("Name") or item.get("name") or "")
        return codes, names

    def fetch_market_frames(
        self,
        codes: Iterable[str],
        *,
        count: int,
        dividend_type: str,
        batch_size: int = 50,
    ) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        items = list(codes)
        for offset in range(0, len(items), batch_size):
            batch = items[offset : offset + batch_size]
            value = self.call(
                "get_market_data",
                {
                    "field_list": ["Open", "High", "Low", "Close", "Volume", "Amount"],
                    "stock_list": batch,
                    "period": "1d",
                    "count": count,
                    "dividend_type": dividend_type,
                    "fill_data": False,
                },
            )
            result.update(_market_frames_from_rpc(value, batch))
        return result

    def fetch_market_range(
        self,
        codes: Iterable[str],
        *,
        start_time: str,
        end_time: str,
        dividend_type: str,
        batch_size: int = 50,
        include_forward_factor: bool = False,
    ) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        items = list(codes)
        for offset in range(0, len(items), batch_size):
            batch = items[offset : offset + batch_size]
            value = self.call(
                "get_market_data",
                {
                    "field_list": [
                        "Open",
                        "High",
                        "Low",
                        "Close",
                        "Volume",
                        "Amount",
                        *(["ForwardFactor"] if include_forward_factor else []),
                    ],
                    "stock_list": batch,
                    "period": "1d",
                    "start_time": start_time,
                    "end_time": end_time,
                    "count": 0,
                    "dividend_type": dividend_type,
                    "fill_data": False,
                },
            )
            result.update(_market_frames_from_rpc(value, batch))
        return result

    def fetch_trading_calendar(self, start_time: str, end_time: str) -> list[str]:
        calendars: dict[str, list[str]] = {}
        for market in ("SH", "SZ"):
            value = self.call(
                "get_trading_calendar",
                {"market": market, "start_time": start_time, "end_time": end_time},
            )
            raw = value.get("Value") if isinstance(value, dict) and "Value" in value else value
            if isinstance(raw, dict) and isinstance(raw.get("Date"), list):
                raw = raw["Date"]
            if not isinstance(raw, list):
                raise ResearchDataBlockedError(f"TDX {market} trading calendar contract drift")
            dates = sorted({_normalize_calendar_date(item) for item in raw if item})
            if not dates:
                raise ResearchDataBlockedError(f"TDX {market} trading calendar is empty")
            calendars[market] = dates
        if calendars["SH"] != calendars["SZ"]:
            raise ResearchDataBlockedError("沪深交易日历不一致，历史构建失败关闭")
        return calendars["SH"]

    def fetch_financial_history(
        self,
        codes: Iterable[str],
        *,
        start_time: str,
        end_time: str,
        batch_size: int = 25,
    ) -> dict[str, Any]:
        return self._batched_payload(
            "get_financial_data",
            list(codes),
            {
                "table_list": list(TDX_FINANCIAL_FIELDS),
                "start_time": start_time,
                "end_time": end_time,
                "report_type": "announce_time",
            },
            batch_size,
        )

    def fetch_flow_history(
        self,
        codes: Iterable[str],
        *,
        start_time: str,
        end_time: str,
        batch_size: int = 50,
    ) -> dict[str, Any]:
        return self._batched_payload(
            "get_gpjy_value",
            list(codes),
            {
                "table_list": list(TDX_FLOW_FIELDS),
                "start_time": start_time,
                "end_time": end_time,
            },
            batch_size,
        )

    def fetch_security_status_history(
        self,
        codes: Iterable[str],
        *,
        end_time: str,
        batch_size: int = 50,
    ) -> dict[str, Any]:
        return self._batched_payload(
            "get_gpjy_value",
            list(codes),
            {
                "table_list": ["GP29"],
                "start_time": "19900101",
                "end_time": end_time,
            },
            batch_size,
        )

    def fetch_consensus_snapshot(
        self,
        codes: Iterable[str],
        *,
        batch_size: int = 100,
    ) -> dict[str, Any]:
        return self._batched_payload(
            "get_gp_one_data",
            list(codes),
            {"table_list": list(TDX_CONSENSUS_FIELDS)},
            batch_size,
        )

    def fetch_stock_info(self, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        fields = ["Name", "IsSTGP", "IsQuitGP", "J_start", "rs_hyname", "ActiveCapital"]
        for code in codes:
            value = self.call("get_stock_info", {"stock_code": code, "field_list": fields})
            if isinstance(value, dict):
                result[str(code)] = value
        return result

    def _batched_payload(
        self,
        method: str,
        codes: list[str],
        params: Mapping[str, Any],
        batch_size: int,
    ) -> dict[str, Any]:
        batches: list[Any] = []
        for offset in range(0, len(codes), batch_size):
            batch = codes[offset : offset + batch_size]
            batches.append(self.call(method, {"stock_list": batch, **dict(params)}))
        return {"method": method, "batches": batches}

    def _registered_installs(self) -> list[dict[str, str]]:
        import winreg

        installs: list[dict[str, str]] = []
        for registry_path in self.registry_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, registry_path) as key:
                    name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                    try:
                        location = str(winreg.QueryValueEx(key, "InstallLocation")[0])
                    except OSError:
                        location = ""
                    installs.append(
                        {"name": name, "location": location, "source": "registry"}
                    )
            except OSError:
                continue
        return installs

    def _configured_install(self) -> dict[str, str] | None:
        if self.tdx_root is None:
            return None
        executable = self.tdx_root / "TdxW.exe"
        tq_user_dir = self.tdx_root / "PYPlugins" / "user"
        if not executable.is_file() or not tq_user_dir.is_dir():
            return None
        return {
            "name": "通达信（TDX_ROOT）",
            "location": str(self.tdx_root),
            "source": "configured_root",
        }


class CninfoDirectProvider:
    """Direct, fail-closed adapter for CNINFO's public disclosure endpoints."""

    stock_master_url = "https://www.cninfo.com.cn/new/data/szse_stock.json"
    announcement_url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    industry_url = "https://webapi.cninfo.com.cn/api/stock/p_stock2110"
    page_size = 30

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        announcement_batch_size: int = 50,
        max_announcement_pages: int = 2_000,
        industry_workers: int = 4,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.announcement_batch_size = max(1, announcement_batch_size)
        self.max_announcement_pages = max(1, max_announcement_pages)
        self.industry_workers = max(1, industry_workers)
        self._stock_org_ids: dict[str, str] | None = None
        self._http_local = threading.local()

    def probe(self) -> ProviderGate:
        checked_at = datetime.now().astimezone().isoformat()
        try:
            org_ids = self._load_stock_org_ids(refresh=True)
            sample_code = "000001" if "000001" in org_ids else next(iter(org_ids))
            end = datetime.now().date()
            start = end - timedelta(days=400)
            announcement_payload = self._fetch_announcement_page(
                [sample_code],
                start.strftime("%Y%m%d"),
                end.strftime("%Y%m%d"),
                page=1,
            )
            announcements = self._validate_announcement_payload(
                announcement_payload,
                require_record=True,
            )
            industries = self._fetch_industry_for_code(
                sample_code,
                "19900101",
                end.strftime("%Y%m%d"),
            )
            if not industries:
                raise CninfoContractError(
                    f"CNINFO industry probe returned no records for {sample_code}"
                )
        except ModuleNotFoundError as exc:
            return ProviderGate(
                False,
                "DEPENDENCY_MISSING",
                "缺少 cryptography；无法生成巨潮 WebAPI 的公开请求校验值",
                checked_at,
                {"dependency": str(exc.name or "cryptography")},
            )
        except CninfoContractError as exc:
            return ProviderGate(
                False,
                "CONTRACT_FAILED",
                str(exc),
                checked_at,
                self._gate_metadata(),
            )
        except (CninfoTransportError, OSError, ValueError) as exc:
            return ProviderGate(
                False,
                "UNAVAILABLE",
                str(exc),
                checked_at,
                self._gate_metadata(),
            )
        return ProviderGate(
            True,
            "READY",
            "巨潮公告、原文链接和历史行业变更接口实时准入通过",
            checked_at,
            {
                **self._gate_metadata(),
                "stock_master_symbols": len(org_ids),
                "announcement_sample_rows": len(announcements),
                "industry_sample_rows": len(industries),
            },
        )

    def fetch_announcements(
        self,
        start_date: str,
        end_date: str,
        *,
        codes: Iterable[str] | None = None,
        _pagination_attempt: int = 0,
    ) -> list[dict[str, Any]]:
        _validate_yyyymmdd_range(start_date, end_date)
        normalized_codes = _unique_cninfo_codes(codes or ())
        batches = [
            normalized_codes[offset : offset + self.announcement_batch_size]
            for offset in range(0, len(normalized_codes), self.announcement_batch_size)
        ] or [[]]
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for batch in batches:
            first = self._fetch_announcement_page(batch, start_date, end_date, page=1)
            first_records = self._validate_announcement_payload(first)
            total = _required_non_negative_int(first, "totalAnnouncement")
            if total >= CNINFO_ANNOUNCEMENT_RESULT_CAP:
                if len(batch) <= 1:
                    raise CninfoContractError(
                        f"CNINFO announcement result for {batch or ['ALL']} is saturated "
                        f"at {total}; query cannot be split further"
                    )
                midpoint = len(batch) // 2
                for sub_batch in (batch[:midpoint], batch[midpoint:]):
                    for item in self.fetch_announcements(
                        start_date,
                        end_date,
                        codes=sub_batch,
                    ):
                        identity = (item["code"], item["announcement_id"])
                        if identity not in seen:
                            seen.add(identity)
                            output.append(item)
                continue
            reported_pages = int(first.get("totalpages") or 0)
            calculated_pages = (
                (total + self.page_size - 1) // self.page_size if total else 0
            )
            total_pages = max(reported_pages, calculated_pages)
            if total_pages > self.max_announcement_pages:
                raise CninfoContractError(
                    f"CNINFO announcement page count {total_pages} exceeds safety limit "
                    f"{self.max_announcement_pages}"
                )
            raw_count = len(first_records)
            pages = [first_records]
            for page in range(2, total_pages + 1):
                payload = self._fetch_announcement_page(batch, start_date, end_date, page=page)
                records = self._validate_announcement_payload(payload)
                raw_count += len(records)
                pages.append(records)
            if raw_count < total:
                if _pagination_attempt + 1 < self.max_attempts:
                    self._reset_http_session()
                    time.sleep(1.0 * (2**_pagination_attempt))
                    for item in self.fetch_announcements(
                        start_date,
                        end_date,
                        codes=batch,
                        _pagination_attempt=_pagination_attempt + 1,
                    ):
                        identity = (item["code"], item["announcement_id"])
                        if identity not in seen:
                            seen.add(identity)
                            output.append(item)
                    continue
                if len(batch) > 1:
                    # CNINFO occasionally reports a stable total that is one row
                    # larger than the paginated payload for a multi-code query.  A
                    # smaller query gives each code an independent total and avoids
                    # accepting an incomplete snapshot.  Single-code mismatches still
                    # fail closed because there is no narrower auditable query.
                    midpoint = len(batch) // 2
                    for sub_batch in (batch[:midpoint], batch[midpoint:]):
                        for item in self.fetch_announcements(
                            start_date,
                            end_date,
                            codes=sub_batch,
                        ):
                            identity = (item["code"], item["announcement_id"])
                            if identity not in seen:
                                seen.add(identity)
                                output.append(item)
                    continue
                raise CninfoContractError(
                    f"CNINFO announcement pagination incomplete: expected {total}, got {raw_count}"
                )
            for records in pages:
                for raw in records:
                    item = self._normalize_announcement(raw)
                    identity = (item["code"], item["announcement_id"])
                    if identity not in seen:
                        seen.add(identity)
                        output.append(item)
        return sorted(output, key=lambda item: (item["published_at"], item["code"]))

    def fetch_industry_changes(
        self,
        codes: Iterable[str],
        *,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        _validate_yyyymmdd_range(start_date, end_date)
        normalized_codes = _unique_cninfo_codes(codes)
        if not normalized_codes:
            return []
        workers = min(self.industry_workers, len(normalized_codes))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cninfo-industry") as pool:
            chunks = pool.map(
                lambda code: self._fetch_industry_for_code(code, start_date, end_date),
                normalized_codes,
            )
            records = [item for chunk in chunks for item in chunk]
        return sorted(records, key=lambda item: (item["effective_at"], item["code"]))

    def _load_stock_org_ids(self, *, refresh: bool = False) -> dict[str, str]:
        if self._stock_org_ids is not None and not refresh:
            return self._stock_org_ids
        payload = self._request_json(
            self.stock_master_url,
            headers=self._www_headers(),
            method="GET",
        )
        values = payload.get("stockList")
        if not isinstance(values, list) or not values:
            raise CninfoContractError("CNINFO stock master is missing non-empty stockList")
        result: dict[str, str] = {}
        for item in values:
            if not isinstance(item, dict):
                raise CninfoContractError("CNINFO stock master contains a non-object row")
            code = str(item.get("code") or "").strip()
            org_id = str(item.get("orgId") or "").strip()
            if len(code) == 6 and org_id:
                result[code] = org_id
        if "000001" not in result or len(result) < 1_000:
            raise CninfoContractError(
                f"CNINFO stock master coverage is invalid: {len(result)} usable symbols"
            )
        self._stock_org_ids = result
        return result

    def _fetch_announcement_page(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        page: int,
    ) -> dict[str, Any]:
        org_ids = self._load_stock_org_ids()
        unknown = [code for code in codes if code not in org_ids]
        if unknown:
            raise CninfoContractError(
                f"CNINFO stock master has no orgId for: {', '.join(unknown[:5])}"
            )
        stock = ";".join(f"{code},{org_ids[code]}" for code in codes)
        payload = {
            "pageNum": str(page),
            "pageSize": str(self.page_size),
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": stock,
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{_cninfo_date(start_date)}~{_cninfo_date(end_date)}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        return self._request_json(
            self.announcement_url,
            data=payload,
            headers=self._www_headers(),
            method="POST",
        )

    def _fetch_industry_for_code(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "scode": code,
                "sdate": _cninfo_date(start_date),
                "edate": _cninfo_date(end_date),
            }
        )
        source_url = f"{self.industry_url}?{query}"
        payload = self._request_json(
            source_url,
            data=b"",
            headers=self._webapi_headers(),
            method="POST",
        )
        if str(payload.get("resultcode")) != "200":
            raise CninfoContractError(
                f"CNINFO industry request failed for {code}: "
                f"{payload.get('resultcode')} {payload.get('resultmsg')}"
            )
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise CninfoContractError("CNINFO industry response is missing records")
        output: list[dict[str, Any]] = []
        for raw in raw_records:
            if not isinstance(raw, dict):
                raise CninfoContractError("CNINFO industry response contains a non-object row")
            required = ("SECCODE", "VARYDATE", "F001V", "F002V", "F003V")
            missing = [field for field in required if not str(raw.get(field) or "").strip()]
            if missing:
                raise CninfoContractError(
                    f"CNINFO industry fields drifted for {code}: missing {missing}"
                )
            returned_code = str(raw["SECCODE"]).strip()
            if returned_code != code:
                raise CninfoContractError(
                    f"CNINFO industry response code mismatch: requested {code}, got {returned_code}"
                )
            standard_code = str(raw["F001V"]).strip()
            if standard_code not in CNINFO_INDUSTRY_STANDARD_CODES:
                continue
            industry = next(
                (str(raw.get(field) or "").strip() for field in ("F007V", "F006V", "F005V", "F004V") if str(raw.get(field) or "").strip()),
                "",
            )
            if not industry:
                raise CninfoContractError(f"CNINFO industry name is missing for {code}")
            raw_payload = _canonical_json(raw)
            output.append(
                {
                    "code": _standard_stock_code(code),
                    "industry": industry,
                    "industry_code": str(raw["F003V"]),
                    "industry_standard_code": standard_code,
                    "industry_standard": str(raw["F002V"]),
                    "industry_hierarchy": {
                        "section": str(raw.get("F004V") or ""),
                        "subclass": str(raw.get("F005V") or ""),
                        "major": str(raw.get("F006V") or ""),
                        "middle": str(raw.get("F007V") or ""),
                    },
                    "effective_at": pd.Timestamp(raw["VARYDATE"]).isoformat(),
                    "source": "cninfo",
                    "source_url": source_url,
                    "raw_hash": hashlib.sha256(raw_payload.encode("utf-8")).hexdigest(),
                }
            )
        return output

    def _validate_announcement_payload(
        self,
        payload: Mapping[str, Any],
        *,
        require_record: bool = False,
    ) -> list[dict[str, Any]]:
        _required_non_negative_int(payload, "totalAnnouncement")
        records = payload.get("announcements")
        if not isinstance(records, list):
            raise CninfoContractError("CNINFO announcement response is missing announcements")
        if require_record and not records:
            raise CninfoContractError("CNINFO announcement probe returned no records")
        for raw in records:
            if not isinstance(raw, dict):
                raise CninfoContractError("CNINFO announcement response contains a non-object row")
            required = (
                "secCode",
                "orgId",
                "announcementId",
                "announcementTitle",
                "announcementTime",
                "adjunctUrl",
            )
            missing = [field for field in required if raw.get(field) in (None, "")]
            if missing:
                raise CninfoContractError(
                    f"CNINFO announcement fields drifted: missing {missing}"
                )
        return records

    def _normalize_announcement(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        code = _standard_stock_code(raw["secCode"])
        if not code:
            raise CninfoContractError(f"CNINFO returned invalid stock code: {raw['secCode']}")
        published = _cninfo_announcement_time(raw["announcementTime"])
        published_timestamp = pd.Timestamp(published)
        date_only = (
            published_timestamp.hour == 0
            and published_timestamp.minute == 0
            and published_timestamp.second == 0
        )
        effective_timestamp = (
            published_timestamp.normalize() + pd.Timedelta(hours=15, seconds=1)
            if date_only
            else published_timestamp
        )
        adjunct_url = str(raw["adjunctUrl"]).strip()
        if adjunct_url.startswith(("http://", "https://")):
            source_url = adjunct_url
        else:
            source_url = f"https://static.cninfo.com.cn/{adjunct_url.lstrip('/')}"
        raw_payload = _canonical_json(dict(raw))
        return {
            "code": code,
            "name": str(raw.get("secName") or ""),
            "title": str(raw["announcementTitle"]),
            "published_at": published,
            "effective_at": effective_timestamp.isoformat(),
            "publication_precision": (
                "DATE_ONLY_CONSERVATIVE_AFTER_CLOSE" if date_only else "TIMESTAMP"
            ),
            "source": "cninfo",
            "source_url": source_url,
            "announcement_id": str(raw["announcementId"]),
            "org_id": str(raw["orgId"]),
            "document_type": str(raw.get("adjunctType") or ""),
            "document_size_kb": _optional_int(raw.get("adjunctSize")),
            "raw_hash": hashlib.sha256(raw_payload.encode("utf-8")).hexdigest(),
        }

    def _request_json(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        method: str,
    ) -> dict[str, Any]:
        body: bytes | None
        request_headers = dict(headers or {})
        if isinstance(data, Mapping):
            body = urllib.parse.urlencode(data).encode("utf-8")
            request_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"
            )
        else:
            body = data
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self._http_session().request(
                    method,
                    url,
                    data=body,
                    headers=request_headers,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                raw = response.content
                payload = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(payload, dict):
                    raise CninfoContractError(
                        f"CNINFO returned non-object JSON from {url}"
                    )
                return payload
            except CninfoContractError:
                raise
            except (
                OSError,
                http.client.HTTPException,
                requests.RequestException,
                urllib.error.URLError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                self._reset_http_session()
                if attempt + 1 < self.max_attempts:
                    time.sleep(1.0 * (2**attempt))
        raise CninfoTransportError(f"CNINFO request failed: {last_error}") from last_error

    def _http_session(self) -> requests.Session:
        session = getattr(self._http_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Connection": "keep-alive"})
            self._http_local.session = session
        return session

    def _reset_http_session(self) -> None:
        session = getattr(self._http_local, "session", None)
        if session is not None:
            session.close()
            delattr(self._http_local, "session")

    @staticmethod
    def _www_headers() -> dict[str, str]:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://www.cninfo.com.cn",
            "Referer": "https://www.cninfo.com.cn/",
            "User-Agent": "tdx-research-platform/1.0 (+research-only)",
            "X-Requested-With": "XMLHttpRequest",
        }

    @staticmethod
    def _webapi_headers() -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Enckey": _cninfo_accept_enckey(),
            "Origin": "https://webapi.cninfo.com.cn",
            "Referer": "https://webapi.cninfo.com.cn/",
            "User-Agent": "tdx-research-platform/1.0 (+research-only)",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _gate_metadata(self) -> dict[str, Any]:
        return {
            "provider": "cninfo_direct",
            "announcement_endpoint": self.announcement_url,
            "industry_endpoint": self.industry_url,
            "industry_standard_codes": list(CNINFO_INDUSTRY_STANDARD_CODES),
            "account_required": False,
            "akshare_required": False,
        }


class EarlyWinnerResearchService:
    def __init__(
        self,
        config: PlatformConfig,
        database: Database,
        *,
        tdx_client: TdxResearchHttpClient | None = None,
        cninfo_provider: CninfoDirectProvider | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.tdx_client = tdx_client or TdxResearchHttpClient(tdx_root=config.tdx_root)
        self.cninfo_provider = cninfo_provider or CninfoDirectProvider()
        self.rule_strategy = EarlyWinnerRuleStrategy()
        self.ml_strategy = EarlyWinnerMLStrategy()
        current = self.database.query(
            "SELECT status, data_asof, data_gates_json FROM research_projects WHERE project_id=?",
            (PROJECT_ID,),
        )
        status = str(current[0]["status"]) if current else "DATA_BUILDING"
        data_asof = str(current[0].get("data_asof") or "") or None if current else None
        gates = _decode_json(current[0].get("data_gates_json"), {}) if current else {}
        self.database.upsert_research_project(
            project_id=PROJECT_ID,
            version=PROJECT_VERSION,
            name=PROJECT_NAME,
            description=PROJECT_DESCRIPTION,
            status=status,
            data_asof=data_asof,
            data_gates=gates,
        )

    def detail(self) -> dict[str, Any]:
        project_rows = self.database.query(
            "SELECT * FROM research_projects WHERE project_id=?",
            (PROJECT_ID,),
        )
        if not project_rows:
            raise KeyError(PROJECT_ID)
        project = dict(project_rows[0])
        project["data_gates"] = _decode_json(project.pop("data_gates_json", "{}"), {})
        project["strategies"] = [
            self._strategy_record(self.rule_strategy),
            self._strategy_record(self.ml_strategy),
        ]
        project["latest_model"] = self._latest_decoded("research_models")
        project["latest_validation"] = self._latest_decoded("research_validations")
        project["latest_batches"] = self._decoded_rows(
            self.database.query(
                """SELECT * FROM research_data_batches WHERE project_id=?
                ORDER BY fetched_at DESC LIMIT 20""",
                (PROJECT_ID,),
            )
        )
        history = self.history_status()
        project["history"] = history
        if history.get("status") == "BLOCKED_DATA":
            stored_status = str(project.get("status") or "")
            stored_feature_gate = dict(project["data_gates"].get("feature_history") or {})
            project["stored_status"] = stored_status
            project["status"] = "BLOCKED_DATA"
            project["data_gates"]["feature_history"] = {
                "ready": False,
                "status": LEGACY_HISTORY_REJECTION_STATUS,
                "detail": (
                    "Legacy history artifacts are retained for audit only; they are not "
                    "eligible for training or validation."
                ),
                "trust_policy_version": LEGACY_HISTORY_TRUST_POLICY_VERSION,
                "legacy_build_id": str(history.get("build_id") or ""),
                "legacy_artifact_status": str(history.get("artifact_status") or ""),
                "stored_gate": stored_feature_gate,
            }
            for key in ("latest_model", "latest_validation"):
                artifact = project.get(key)
                if isinstance(artifact, dict):
                    artifact["audit_only"] = True
                    artifact["qualification_status"] = LEGACY_HISTORY_REJECTION_STATUS
        rule = self.candidates(method="rule")
        ml = self.candidates(method="ml")
        project["candidates"] = {"rule": rule, "ml": ml}
        project["overlap"] = sorted(
            {str(item["code"]) for item in rule} & {str(item["code"]) for item in ml}
        )
        project["trade_signals_enabled"] = False
        project["tdx_push_enabled"] = False
        project["promotion_allowed"] = False
        project["write_actions_enabled"] = False
        project["candidate_generation_enabled"] = False
        project["artifacts_audit_only"] = True
        return project

    def candidates(self, *, method: str | None = None, asof: str | None = None) -> list[dict[str, Any]]:
        if method is not None and method not in {"rule", "ml"}:
            raise ValueError("method must be rule or ml")
        filters = ["project_id=?"]
        values: list[Any] = [PROJECT_ID]
        if method:
            filters.append("method=?")
            values.append(method)
        if asof:
            filters.append("asof=?")
            values.append(asof)
        elif method:
            filters.append(
                "asof=(SELECT MAX(asof) FROM research_candidates WHERE project_id=? AND method=?)"
            )
            values.extend((PROJECT_ID, method))
        rows = self.database.query(
            f"""SELECT * FROM research_candidates WHERE {' AND '.join(filters)}
            ORDER BY asof DESC, method, rank""",
            values,
        )
        return self._decoded_rows(rows)

    def history_status(self) -> dict[str, Any]:
        builds = self.database.query(
            """SELECT * FROM early_winner_history_builds
            WHERE project_id=? ORDER BY updated_at DESC LIMIT 1""",
            (PROJECT_ID,),
        )
        if not builds:
            return {
                "project_id": PROJECT_ID,
                "status": "BLOCKED_DATA",
                "artifact_status": "NOT_STARTED",
                "start_year": 2018,
                "end_year": 2025,
                "expected_shards": 8,
                "completed_shards": 0,
                "shards": [],
                "evidence_retained": False,
                "trust_policy": {
                    "ready": False,
                    "status": LEGACY_HISTORY_REJECTION_STATUS,
                    "version": LEGACY_HISTORY_TRUST_POLICY_VERSION,
                    "reasons": [
                        "The legacy V1 history builder is retired and cannot create a new admissible artifact."
                    ],
                },
            }
        build = dict(builds[0])
        build["shards"] = self.database.query(
            """SELECT * FROM early_winner_history_shards
            WHERE build_id=? ORDER BY shard_year""",
            (build["build_id"],),
        )
        artifact_status = str(build.get("status") or "")
        build["artifact_status"] = artifact_status
        build["evidence_retained"] = True
        build["trust_policy"] = {
            "ready": False,
            "status": LEGACY_HISTORY_REJECTION_STATUS,
            "version": LEGACY_HISTORY_TRUST_POLICY_VERSION,
            "reasons": [
                "The legacy builder selected securities from a current stock list instead of an interval-valid historical security master.",
                "The legacy artifacts are not bound to the authoritative historical-security-master and delisted-history quality manifests.",
                "The legacy 2018-2025 snapshot crosses the sealed development/frozen-validation boundary.",
            ],
        }
        if artifact_status == "SUCCEEDED":
            build["status"] = "BLOCKED_DATA"
        return build

    def build_history(
        self,
        *,
        start_year: int = 2018,
        end_year: int = 2025,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        raise ResearchDataBlockedError(
            "Legacy history builder is retired: it used a current-list universe and cannot "
            "produce admissible historical evidence. Use the versioned historical-security "
            "master and delisted-history pipeline instead."
        )

        # The implementation below is intentionally retained as immutable audit evidence for
        # previously materialized artifacts. It must not be made reachable again; any successor
        # requires a new versioned project and protocol.
        if start_year < 2000 or end_year < start_year or end_year >= datetime.now().year:
            raise ValueError("history range must be complete calendar years before the current year")
        self._progress(progress_callback, "PROVIDER_GATES", 0.01, "检查历史数据接口")
        tdx_gate = self.tdx_client.admission_probe()
        cninfo_gate = self.cninfo_provider.probe()
        if not tdx_gate.ready or not cninfo_gate.ready:
            self.database.update_research_project(
                PROJECT_ID,
                status="BLOCKED_DATA",
                data_gates={
                    "tdx_http": tdx_gate.as_record(),
                    "cninfo_direct": cninfo_gate.as_record(),
                },
            )
            raise ResearchDataBlockedError("历史构建的数据接口准入未通过")

        calendar_start = f"{start_year - 1}0601"
        calendar_end = f"{end_year + 1}0630"
        calendar = self.tdx_client.fetch_trading_calendar(calendar_start, calendar_end)
        weekly_dates = _weekly_decision_dates(calendar, start_year, end_year)
        expected_weeks = sum(1 for item in weekly_dates if start_year <= item.year <= end_year)
        if expected_weeks < (end_year - start_year + 1) * 45:
            raise ResearchDataBlockedError("交易日历覆盖不足，无法形成完整周度历史")
        calendar_hash = hashlib.sha256(
            json.dumps(calendar, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        calendar_batch = self._persist_trading_calendar(
            calendar,
            calendar_hash=calendar_hash,
            start_time=calendar_start,
            end_time=calendar_end,
        )
        codes, names = self.tdx_client.list_a_shares()
        maximum = max(0, int(os.getenv("EARLY_WINNER_HISTORY_MAX_SYMBOLS", "0")))
        if maximum:
            codes = codes[:maximum]
        if not codes:
            raise ResearchDataBlockedError("TDX 未返回历史构建股票池")
        universe_hash = hashlib.sha256(
            json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        build_id = _history_build_id(start_year, end_year, calendar_hash, universe_hash)
        now = datetime.now().astimezone().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO early_winner_history_builds
                (build_id, project_id, start_year, end_year, status, expected_shards,
                 completed_shards, calendar_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'RUNNING', ?, 0, ?, ?, ?)
                ON CONFLICT(build_id) DO UPDATE SET status='RUNNING', updated_at=excluded.updated_at,
                    error=''""",
                (
                    build_id,
                    PROJECT_ID,
                    start_year,
                    end_year,
                    end_year - start_year + 1,
                    calendar_hash,
                    now,
                    now,
                ),
            )
        try:
            industry_history = self._load_or_fetch_history_industries(
                codes,
                end_year=end_year,
                universe_hash=universe_hash,
            )
            for index, year in enumerate(range(start_year, end_year + 1)):
                progress = 0.05 + 0.88 * index / max(1, end_year - start_year + 1)
                self._progress(
                    progress_callback,
                    "HISTORY_SHARD",
                    progress,
                    f"构建 {year} 年周度点时特征",
                )
                existing = self.database.query(
                    """SELECT * FROM early_winner_history_shards
                    WHERE build_id=? AND shard_year=? AND status='SUCCEEDED'""",
                    (build_id, year),
                )
                if existing:
                    path = Path(str(existing[0]["path"]))
                    if path.exists() and _file_sha256(path) == str(existing[0]["content_hash"]):
                        continue
                    raise ResearchDataBlockedError(f"{year} 年冻结分片缺失或哈希不一致")
                try:
                    self._build_history_year(
                        build_id=build_id,
                        year=year,
                        calendar=calendar,
                        codes=codes,
                        names=names,
                        industry_history=industry_history,
                    )
                except Exception as exc:
                    with self.database.connect() as connection:
                        connection.execute(
                            """UPDATE early_winner_history_shards
                            SET status='FAILED', finished_at=?, error=?
                            WHERE build_id=? AND shard_year=?""",
                            (
                                datetime.now().astimezone().isoformat(),
                                str(exc),
                                build_id,
                                year,
                            ),
                        )
                    raise
                self._refresh_history_build_progress(build_id)

            manifest = self._write_history_manifest(
                build_id,
                start_year=start_year,
                end_year=end_year,
                calendar_hash=calendar_hash,
                universe_count=len(codes),
                universe_hash=universe_hash,
            )
            completed = self._refresh_history_build_progress(
                build_id,
                status="SUCCEEDED",
                manifest=manifest,
            )
            gates = {
                "tdx_http": tdx_gate.as_record(),
                "cninfo_direct": cninfo_gate.as_record(),
                "feature_history": {
                    "ready": True,
                    "status": "READY",
                    "detail": f"{start_year}-{end_year} 年冻结历史已完成",
                    "calendar_hash": calendar_hash,
                    "calendar_batch_id": calendar_batch["batch_id"],
                },
                "point_in_time_policy": {
                    "ready": True,
                    "status": "READY",
                    "detail": "历史财务、公告和行业按发布/生效时点连接",
                },
                "consensus_policy": {
                    "ready": True,
                    "status": "FORWARD_ONLY",
                    "detail": "一致预期不参与历史回填",
                },
            }
            self.database.update_research_project(
                PROJECT_ID,
                status="DATA_BUILDING",
                data_asof=f"{end_year}-12-31",
                data_gates=gates,
            )
            self._progress(progress_callback, "COMPLETED", 1.0, "历史特征已冻结")
            return completed
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE early_winner_history_builds
                    SET status='FAILED', error=?, updated_at=? WHERE build_id=?""",
                    (str(exc), datetime.now().astimezone().isoformat(), build_id),
                )
            self.database.update_research_project(PROJECT_ID, status="BLOCKED_DATA")
            raise

    def _build_history_year(
        self,
        *,
        build_id: str,
        year: int,
        calendar: list[str],
        codes: list[str],
        names: Mapping[str, str],
        industry_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        started_at = datetime.now().astimezone().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO early_winner_history_shards
                (build_id, shard_year, status, started_at)
                VALUES (?, ?, 'RUNNING', ?)
                ON CONFLICT(build_id, shard_year) DO UPDATE SET status='RUNNING',
                    started_at=excluded.started_at, finished_at=NULL, error=''""",
                (build_id, year, started_at),
            )
        start = f"{year - 1}0601"
        end = f"{year + 1}0630"
        benchmark_map = self.tdx_client.fetch_market_range(
            ["000300.CSI", "000300.SH"],
            start_time=start,
            end_time=end,
            dividend_type="front",
            batch_size=2,
        )
        benchmark = next((item for item in benchmark_map.values() if not item.empty), None)
        front = self.tdx_client.fetch_market_range(
            codes,
            start_time=start,
            end_time=end,
            dividend_type="front",
        )
        year_weeks = [item for item in _weekly_decision_dates(calendar, year, year)]
        technical_by_date: dict[str, list[dict[str, Any]]] = {}
        supplemental_codes: set[str] = set()
        for asof in year_weeks:
            sliced = {
                code: frame.loc[pd.to_datetime(frame.index) <= asof]
                for code, frame in front.items()
                if not frame.empty
            }
            benchmark_slice = (
                benchmark.loc[pd.to_datetime(benchmark.index) <= asof]
                if benchmark is not None
                else None
            )
            technical = build_technical_feature_rows(
                sliced,
                benchmark=benchmark_slice,
                names=names,
            )
            technical = _align_weekly_decision_rows(technical, asof)
            selected = sorted(
                (
                    item
                    for item in technical
                    if float(item.get("adv20") or 0.0) >= 100_000_000
                    and int(item.get("valid_days_20") or 0) >= 18
                ),
                key=lambda item: str(item["code"]),
            )
            technical_by_date[asof.date().isoformat()] = selected
            supplemental_codes.update(str(item["code"]) for item in selected)
        selected_codes = sorted(supplemental_codes)
        if not selected_codes:
            raise ResearchDataBlockedError(f"{year} 年没有形成合格技术样本")
        # Resolve and cache the externally paginated announcement snapshot before
        # the expensive supplementary TDX queries. A CNINFO contract failure can
        # then be retried without repeating the financial and flow collection.
        announcements = self._load_or_fetch_history_announcements(
            year=year,
            codes=selected_codes,
        )
        raw = self.tdx_client.fetch_market_range(
            selected_codes,
            start_time=start,
            end_time=end,
            dividend_type="none",
        )
        financial = self.tdx_client.fetch_financial_history(
            selected_codes,
            start_time=f"{year - 2}0101",
            end_time=f"{year}1231",
        )
        flows = self.tdx_client.fetch_flow_history(
            selected_codes,
            start_time=f"{year - 1}0101",
            end_time=f"{year}1231",
        )
        security_status = self.tdx_client.fetch_security_status_history(
            selected_codes,
            end_time=f"{year}1231",
        )
        selected_code_set = set(selected_codes)
        industries = [
            dict(item)
            for item in industry_history
            if str(item.get("code")) in selected_code_set
            and pd.Timestamp(item.get("effective_at")) <= pd.Timestamp(f"{year}-12-31T15:00:00")
        ]
        announcements_by_code: dict[str, list[dict[str, Any]]] = {}
        for item in announcements:
            announcements_by_code.setdefault(str(item["code"]), []).append(dict(item))
        industries_by_code: dict[str, list[dict[str, Any]]] = {}
        for item in industries:
            industries_by_code.setdefault(str(item["code"]), []).append(dict(item))
        all_rows: list[pd.DataFrame] = []
        for asof_text, technical in technical_by_date.items():
            asof = pd.Timestamp(asof_text)
            combined: list[dict[str, Any]] = []
            for item in technical:
                code = str(item["code"])
                event_items = [
                    event
                    for event in announcements_by_code.get(code, [])
                    if asof - pd.Timedelta(days=30)
                    <= pd.Timestamp(event.get("effective_at") or event["published_at"])
                    <= asof + pd.Timedelta(hours=15)
                ]
                event = _aggregate_events(event_items)
                available_industries = [
                    record
                    for record in industries_by_code.get(code, [])
                    if pd.Timestamp(record["effective_at"]) <= asof + pd.Timedelta(hours=15)
                ]
                latest_industry = max(
                    available_industries,
                    key=lambda record: pd.Timestamp(record["effective_at"]),
                    default={},
                )
                status = _historical_security_status(security_status, code, asof)
                combined.append(
                    {
                        **item,
                        **_financial_features(financial, code, asof),
                        **_flow_features(flows, code, asof),
                        "industry": str(latest_industry.get("industry") or "未分类"),
                        "is_st": status["is_st"],
                        "is_quit": _historical_quit_status(
                            announcements_by_code.get(code, []), asof
                        ),
                        "event_score": event["score"],
                        "event_type": event["event_type"],
                        "turnover_20": np.nan,
                        "evidence_refs": [
                            *event["evidence_refs"],
                            f"tdx:financial:{code}:{asof_text}",
                            f"tdx:flow:{code}:{asof_text}",
                        ],
                        "published_at": event.get("latest_published_at") or asof.isoformat(),
                        "effective_at": latest_industry.get("effective_at") or asof.isoformat(),
                    }
                )
            _attach_industry_cross_section(combined)
            eligible = mark_research_universe_eligibility(combined)
            all_rows.append(attach_execution_outcomes(pd.DataFrame(eligible), raw))
        frame = pd.concat(all_rows, ignore_index=True)
        financial_coverage = float(frame["revenue_yoy"].notna().mean())
        industry_coverage = float((frame["industry"] != "未分类").mean())
        if financial_coverage < 0.85:
            raise ResearchDataBlockedError(
                f"{year} 年财务时点覆盖率 {financial_coverage:.1%} 低于 85%"
            )
        if industry_coverage < 0.90:
            raise ResearchDataBlockedError(
                f"{year} 年行业时点覆盖率 {industry_coverage:.1%} 低于 90%"
            )
        raw_batches = {
            "market_front": self._persist_raw_frames("market_front", "tdx", front, pd.Timestamp(f"{year}-12-31")),
            "market_none": self._persist_raw_frames("market_none", "tdx", raw, pd.Timestamp(f"{year}-12-31")),
            "financial_history": self._persist_raw_dataset("financial_history", "tdx", financial, pd.Timestamp(f"{year}-12-31")),
            "institutional_flows": self._persist_raw_dataset("institutional_flows", "tdx", flows, pd.Timestamp(f"{year}-12-31")),
            "security_status_history": self._persist_raw_dataset("security_status_history", "tdx", security_status, pd.Timestamp(f"{year}-12-31")),
            "announcements": self._persist_raw_dataset("announcements", "cninfo/direct", announcements, pd.Timestamp(f"{year}-12-31")),
            "industry_history": self._persist_raw_dataset("industry_history", "cninfo/direct", industries, pd.Timestamp(f"{year}-12-31")),
        }
        batch = self._persist_history_shard(
            build_id,
            year,
            frame,
            metadata={
                "builder_version": HISTORY_BUILDER_VERSION,
                "weekly_sessions": len(year_weeks),
                "financial_coverage": financial_coverage,
                "industry_coverage": industry_coverage,
                "universe_symbols": len(codes),
                "prefilter_symbols": len(selected_codes),
                "consensus_history_policy": "EXCLUDED_FORWARD_ONLY",
                "raw_batch_ids": {
                    name: record["batch_id"] for name, record in raw_batches.items()
                },
            },
        )
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE early_winner_history_shards SET status='SUCCEEDED', batch_id=?,
                path=?, content_hash=?, row_count=?, finished_at=?, error=''
                WHERE build_id=? AND shard_year=?""",
                (
                    batch["batch_id"],
                    batch["path"],
                    batch["content_hash"],
                    batch["row_count"],
                    datetime.now().astimezone().isoformat(),
                    build_id,
                    year,
                ),
            )
        return batch

    def _load_or_fetch_history_industries(
        self,
        codes: list[str],
        *,
        end_year: int,
        universe_hash: str,
    ) -> list[dict[str, Any]]:
        rows = self.database.query(
            """SELECT * FROM research_data_batches WHERE project_id=?
            AND dataset='industry_history_full' AND status='SUCCEEDED'
            AND published_end=? ORDER BY fetched_at DESC""",
            (PROJECT_ID, f"{end_year}-12-31"),
        )
        for row in rows:
            metadata = _decode_json(row.get("metadata_json"), {})
            path = Path(str(row.get("path") or ""))
            if metadata.get("universe_hash") != universe_hash:
                continue
            if path.exists() and _file_sha256(path) == str(row.get("content_hash") or ""):
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    return [dict(item) for item in payload if isinstance(item, Mapping)]
                raise ResearchDataBlockedError("历史行业缓存格式漂移")
        industries = self.cninfo_provider.fetch_industry_changes(
            codes,
            start_date="19900101",
            end_date=f"{end_year}1231",
        )
        self._persist_raw_dataset(
            "industry_history_full",
            "cninfo/direct",
            industries,
            pd.Timestamp(f"{end_year}-12-31"),
            metadata={
                "universe_hash": universe_hash,
                "universe_count": len(codes),
                "frozen": True,
            },
        )
        return industries

    def _load_or_fetch_history_announcements(
        self,
        *,
        year: int,
        codes: list[str],
    ) -> list[dict[str, Any]]:
        start_date = f"{year - 1}1201"
        end_date = f"{year}1231"
        normalized_codes = sorted(set(codes))
        chunks = [
            normalized_codes[offset : offset + self.cninfo_provider.announcement_batch_size]
            for offset in range(
                0,
                len(normalized_codes),
                self.cninfo_provider.announcement_batch_size,
            )
        ]
        output: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            chunk_hash = hashlib.sha256(
                json.dumps(
                    {
                        "cache_version": ANNOUNCEMENT_CACHE_VERSION,
                        "year": year,
                        "start_date": start_date,
                        "end_date": end_date,
                        "codes": chunk,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            batch_id = f"ewann_{year}_{chunk_hash[:24]}"
            rows = self.database.query(
                "SELECT * FROM research_data_batches WHERE batch_id=?",
                (batch_id,),
            )
            if rows:
                row = rows[0]
                metadata = _decode_json(row.get("metadata_json"), {})
                path = Path(str(row.get("path") or ""))
                if (
                    row.get("status") != "SUCCEEDED"
                    or metadata.get("chunk_hash") != chunk_hash
                    or not path.exists()
                    or _file_sha256(path) != str(row.get("content_hash") or "")
                ):
                    raise ResearchDataBlockedError(
                        f"{year} announcement chunk {index + 1} cache hash mismatch"
                    )
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, list):
                    raise ResearchDataBlockedError(
                        f"{year} announcement chunk {index + 1} cache schema drift"
                    )
                output.extend(
                    dict(item) for item in payload if isinstance(item, Mapping)
                )
                continue
            payload = self.cninfo_provider.fetch_announcements(
                start_date,
                end_date,
                codes=chunk,
            )
            self._persist_raw_dataset(
                "announcements_history_chunk",
                "cninfo/direct",
                payload,
                pd.Timestamp(f"{year}-12-31"),
                batch_id=batch_id,
                published_start=f"{year - 1}-12-01",
                metadata={
                    "year": year,
                    "cache_version": ANNOUNCEMENT_CACHE_VERSION,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                    "chunk_hash": chunk_hash,
                    "codes": chunk,
                    "frozen": True,
                },
            )
            output.extend(payload)
        deduplicated = {
            (str(item.get("code") or ""), str(item.get("announcement_id") or "")): item
            for item in output
        }
        return sorted(
            deduplicated.values(),
            key=lambda item: (str(item.get("published_at") or ""), str(item.get("code") or "")),
        )

    def _persist_trading_calendar(
        self,
        calendar: list[str],
        *,
        calendar_hash: str,
        start_time: str,
        end_time: str,
    ) -> dict[str, Any]:
        self.config.ensure_runtime_dirs()
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "calendar"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{calendar_hash}.json"
        serialized = json.dumps(calendar, ensure_ascii=False, separators=(",", ":"))
        if path.exists():
            if _file_sha256(path) != calendar_hash:
                raise ResearchDataBlockedError("冻结交易日历文件哈希不一致")
        else:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(path)
        record = {
            "batch_id": f"ewcal_{calendar_hash[:24]}",
            "project_id": PROJECT_ID,
            "dataset": "trading_calendar",
            "source": "tdx/sh+sz",
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": _normalize_calendar_date(start_time),
            "published_end": _normalize_calendar_date(end_time),
            "row_count": len(calendar),
            "path": str(path),
            "content_hash": calendar_hash,
            "schema_hash": hashlib.sha256(b"trading-calendar-v1:list[str]").hexdigest(),
            "metadata": {
                "builder_version": HISTORY_BUILDER_VERSION,
                "markets": ["SH", "SZ"],
                "cross_market_match": True,
                "frozen": True,
            },
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def _persist_history_shard(
        self,
        build_id: str,
        year: int,
        frame: pd.DataFrame,
        *,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._validate_feature_frame(frame)
        self.config.ensure_runtime_dirs()
        batch_id = f"ewh_{build_id}_{year}"
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "features"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch_id}.parquet"
        if path.exists():
            raise ResearchDataBlockedError(f"冻结历史分片已存在且未登记: {path}")
        temporary = path.with_suffix(".tmp.parquet")
        ordered = frame.sort_values(["asof", "code"]).reset_index(drop=True)
        ordered.to_parquet(temporary, index=False)
        temporary.replace(path)
        schema_payload = json.dumps(
            [(column, str(dtype)) for column, dtype in ordered.dtypes.items()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        record = {
            "batch_id": batch_id,
            "project_id": PROJECT_ID,
            "dataset": "early_winner_features",
            "source": "tdx+cninfo/direct",
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": f"{year}-01-01",
            "published_end": f"{year}-12-31",
            "row_count": len(ordered),
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": hashlib.sha256(schema_payload.encode("utf-8")).hexdigest(),
            "metadata": {
                **dict(metadata),
                "build_id": build_id,
                "shard_year": year,
                "point_in_time_validated": True,
                "frozen": True,
            },
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def _refresh_history_build_progress(
        self,
        build_id: str,
        *,
        status: str = "RUNNING",
        manifest: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        shards = self.database.query(
            "SELECT * FROM early_winner_history_shards WHERE build_id=? ORDER BY shard_year",
            (build_id,),
        )
        completed = [item for item in shards if item["status"] == "SUCCEEDED"]
        now = datetime.now().astimezone().isoformat()
        values = {
            "completed": len(completed),
            "last_year": max((int(item["shard_year"]) for item in completed), default=None),
            "path": str((manifest or {}).get("path") or ""),
            "hash": str((manifest or {}).get("hash") or ""),
        }
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE early_winner_history_builds SET status=?, completed_shards=?,
                last_completed_year=?, manifest_path=CASE WHEN ?='' THEN manifest_path ELSE ? END,
                manifest_hash=CASE WHEN ?='' THEN manifest_hash ELSE ? END, updated_at=?, error=''
                WHERE build_id=?""",
                (
                    status,
                    values["completed"],
                    values["last_year"],
                    values["path"],
                    values["path"],
                    values["hash"],
                    values["hash"],
                    now,
                    build_id,
                ),
            )
        return self.history_status()

    def _write_history_manifest(
        self,
        build_id: str,
        *,
        start_year: int,
        end_year: int,
        calendar_hash: str,
        universe_count: int,
        universe_hash: str,
    ) -> dict[str, str]:
        shards = self.database.query(
            """SELECT shard_year, batch_id, path, content_hash, row_count
            FROM early_winner_history_shards WHERE build_id=? ORDER BY shard_year""",
            (build_id,),
        )
        payload = {
            "builder_version": HISTORY_BUILDER_VERSION,
            "build_id": build_id,
            "project_id": PROJECT_ID,
            "start_year": start_year,
            "end_year": end_year,
            "calendar_hash": calendar_hash,
            "universe_count": universe_count,
            "universe_hash": universe_hash,
            "consensus_history_policy": "EXCLUDED_FORWARD_ONLY",
            "shards": shards,
        }
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "history"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{build_id}.manifest.json"
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest():
            raise ResearchDataBlockedError("冻结历史清单已存在但内容不一致")
        path.write_text(serialized, encoding="utf-8")
        return {"path": str(path), "hash": _file_sha256(path)}

    def review_announcements(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        client: Any | None = None,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        openai_client = client
        for raw in records:
            item = dict(raw)
            deterministic = classify_announcement(
                str(item.get("title") or ""),
                str(item.get("text") or ""),
            )
            item.update(deterministic)
            item["classifier"] = "rule"
            item["prompt_version"] = EVENT_REVIEW_PROMPT_VERSION
            if deterministic["hard_negative"] or not deterministic["requires_ai_review"]:
                item["ai_review_status"] = "NOT_REQUIRED"
                output.append(item)
                continue
            if openai_client is None and self.config.openai_api_key:
                from openai import OpenAI

                openai_client = OpenAI(
                    api_key=self.config.openai_api_key,
                    timeout=self.config.openai_timeout_seconds,
                    max_retries=self.config.openai_max_retries,
                )
            if openai_client is None:
                item["ai_review_status"] = "AI_REVIEW_UNAVAILABLE"
                output.append(item)
                continue
            payload = {
                "title": item.get("title"),
                "text": item.get("text", ""),
                "published_at": item.get("published_at"),
                "source_url": item.get("source_url"),
                "raw_hash": item.get("raw_hash"),
            }
            try:
                response = openai_client.responses.parse(
                    model=self.config.openai_model,
                    store=False,
                    reasoning={"effort": "low"},
                    input=[
                        {
                            "role": "system",
                            "content": (
                                "你只复核低置信度上市公司公告事件。不得使用公告发布时间后的市场数据，"
                                "不得输出减持、澄清或风险提示等硬负面类别；这些类别由本地规则锁定。"
                            ),
                        },
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    text_format=EventReviewOutput,
                )
                parsed = response.output_parsed
                if parsed is None:
                    raise ValueError("Model returned no structured event review")
                item.update(parsed.model_dump(mode="json"))
                item["classifier"] = "ai_review"
                item["ai_review_status"] = "SUCCEEDED"
                item["ai_model"] = str(getattr(response, "model", self.config.openai_model))
                item["ai_response_id"] = str(getattr(response, "id", ""))
            except Exception as exc:
                item["ai_review_status"] = "FAILED"
                item["ai_review_error"] = str(exc)
            output.append(item)
        return output

    def refresh(self, progress_callback: Any | None = None) -> dict[str, Any]:
        self._reject_legacy_write_path("refresh")
        self._progress(progress_callback, "PROVIDER_GATES", 0.05, "检查 TDX 与巨潮数据接口")
        self.database.update_research_project(PROJECT_ID, status="DATA_BUILDING")
        tdx_gate = self.tdx_client.admission_probe()
        cninfo_gate = self.cninfo_provider.probe()
        gates = {
            "tdx_http": tdx_gate.as_record(),
            "cninfo_direct": cninfo_gate.as_record(),
            "point_in_time_policy": {
                "ready": True,
                "status": "READY",
                "detail": "published_at/effective_at 不晚于决策时点；收盘后公告次日生效",
            },
            "consensus_policy": {
                "ready": True,
                "status": "FORWARD_ONLY",
                "detail": "一致预期仅逐日留存，不回填历史",
            },
        }
        if not tdx_gate.ready or not cninfo_gate.ready:
            self.database.update_research_project(
                PROJECT_ID,
                status="BLOCKED_DATA",
                data_gates=gates,
            )
            return {
                "project_id": PROJECT_ID,
                "status": "BLOCKED_DATA",
                "data_gates": gates,
                "trade_signals": 0,
            }
        self._progress(
            progress_callback,
            "DATA_ADMITTED",
            0.20,
            "数据接口准入通过；等待点时特征批次",
        )
        feature_batch = self._latest_feature_batch()
        if feature_batch is None:
            try:
                self._progress(
                    progress_callback,
                    "LIVE_COLLECTION",
                    0.30,
                    "采集行情、财务、资金、公告和行业时点数据",
                )
                live_frame, live_metadata = self._collect_live_feature_frame()
                self.ingest_feature_frame(
                    live_frame,
                    source="tdx+cninfo/direct",
                    metadata=live_metadata,
                )
                feature_batch = self._latest_feature_batch()
            except Exception as exc:
                gates["feature_history"] = {
                    "ready": False,
                    "status": "COLLECTION_FAILED",
                    "detail": str(exc),
                }
                self.database.update_research_project(
                    PROJECT_ID,
                    status="BLOCKED_DATA",
                    data_gates=gates,
                )
                return {
                    "project_id": PROJECT_ID,
                    "status": "BLOCKED_DATA",
                    "data_gates": gates,
                    "trade_signals": 0,
                }
        if feature_batch is None:
            raise ResearchDataBlockedError("点时特征批次写入后不可读")
        frame = pd.read_parquet(str(feature_batch["path"]))
        self._validate_feature_frame(frame)
        latest_asof = str(pd.to_datetime(frame["asof"]).max().date())
        latest = frame.loc[pd.to_datetime(frame["asof"]).dt.date == pd.Timestamp(latest_asof).date()]
        rule_candidates = score_rule_candidates(latest.to_dict("records"))
        run_id = uuid4().hex
        snapshot_id = str(feature_batch.get("batch_id") or "")
        self._persist_candidates(
            method="rule",
            strategy_id=RULE_STRATEGY_ID,
            asof=latest_asof,
            run_id=run_id,
            snapshot_id=snapshot_id,
            candidates=rule_candidates,
        )
        gates["feature_history"] = {
            "ready": True,
            "status": "READY",
            "detail": f"{len(frame)} 条点时特征，最新 {latest_asof}",
            "row_count": len(frame),
        }
        self.database.update_research_project(
            PROJECT_ID,
            status="DATA_BUILDING",
            data_asof=latest_asof,
            data_gates=gates,
        )
        self._progress(progress_callback, "COMPLETED", 1.0, "规则候选已刷新")
        return {
            "project_id": PROJECT_ID,
            "status": "DATA_BUILDING",
            "asof": latest_asof,
            "rule_candidates": len(rule_candidates),
            "data_gates": gates,
            "trade_signals": 0,
        }

    def ingest_feature_frame(
        self,
        frame: pd.DataFrame,
        *,
        source: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_feature_frame(frame)
        self.config.ensure_runtime_dirs()
        batch_id = f"ewf_{uuid4().hex}"
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "features"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch_id}.parquet"
        ordered = frame.sort_values(["asof", "code"]).reset_index(drop=True)
        ordered.to_parquet(path, index=False)
        columns_payload = json.dumps(
            [(column, str(dtype)) for column, dtype in ordered.dtypes.items()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        published = pd.to_datetime(ordered["asof"])
        record = {
            "batch_id": batch_id,
            "project_id": PROJECT_ID,
            "dataset": "early_winner_features",
            "source": source,
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": str(published.min().date()),
            "published_end": str(published.max().date()),
            "row_count": len(ordered),
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": hashlib.sha256(columns_payload.encode("utf-8")).hexdigest(),
            "metadata": {
                **dict(metadata or {}),
                "point_in_time_validated": True,
                "feature_columns": list(MODEL_FEATURE_COLUMNS),
            },
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def _collect_live_feature_frame(self) -> tuple[pd.DataFrame, dict[str, Any]]:
        codes, names = self.tdx_client.list_a_shares()
        maximum = max(0, int(os.getenv("EARLY_WINNER_MAX_SYMBOLS", "0")))
        if maximum:
            codes = codes[:maximum]
        front = self.tdx_client.fetch_market_frames(
            codes,
            count=180,
            dividend_type="front",
        )
        benchmark_map = self.tdx_client.fetch_market_frames(
            ["000300.CSI", "000300.SH"],
            count=180,
            dividend_type="front",
            batch_size=2,
        )
        benchmark = next((frame for frame in benchmark_map.values() if not frame.empty), None)
        technical = build_technical_feature_rows(front, benchmark=benchmark, names=names)
        if not technical:
            raise ResearchDataBlockedError("TDX 行情未形成任何 120 日合格样本")
        preselected = sorted(
            (
                row
                for row in technical
                if float(row.get("adv20") or 0.0) >= 100_000_000
                and int(row.get("valid_days_20") or 0) >= 18
            ),
            key=lambda row: (-float(row.get("relative_return_60") or -999), str(row["code"])),
        )[:800]
        selected_codes = [str(row["code"]) for row in preselected]
        if not selected_codes:
            raise ResearchDataBlockedError("成交额和数据完整性门禁后无样本")
        asof = max(pd.Timestamp(row["asof"]) for row in preselected)
        raw_execution_bars = self.tdx_client.fetch_market_frames(
            selected_codes,
            count=180,
            dividend_type="none",
        )
        start = (asof - pd.Timedelta(days=550)).strftime("%Y%m%d")
        end = asof.strftime("%Y%m%d")
        financial = self.tdx_client.fetch_financial_history(
            selected_codes,
            start_time=start,
            end_time=end,
        )
        flows = self.tdx_client.fetch_flow_history(
            selected_codes,
            start_time=(asof - pd.Timedelta(days=120)).strftime("%Y%m%d"),
            end_time=end,
        )
        consensus = self.tdx_client.fetch_consensus_snapshot(selected_codes)
        stock_info = self.tdx_client.fetch_stock_info(selected_codes)
        announcements = self.review_announcements(
            self.cninfo_provider.fetch_announcements(
                (asof - pd.Timedelta(days=45)).strftime("%Y%m%d"),
                end,
                codes=selected_codes,
            )
        )
        industry_changes = self.cninfo_provider.fetch_industry_changes(
            selected_codes,
            start_date="19900101",
            end_date=end,
        )
        raw_batches = {
            "market_front": self._persist_raw_frames(
                "market_front", "tdx", {code: front[code] for code in selected_codes if code in front}, asof
            ),
            "market_none": self._persist_raw_frames(
                "market_none", "tdx", raw_execution_bars, asof
            ),
            "financial_history": self._persist_raw_dataset(
                "financial_history", "tdx", financial, asof
            ),
            "institutional_flows": self._persist_raw_dataset(
                "institutional_flows", "tdx", flows, asof
            ),
            "consensus_snapshots": self._persist_raw_dataset(
                "consensus_snapshots",
                "tdx",
                consensus,
                asof,
                metadata={"history_policy": "FORWARD_ONLY", "backfill_allowed": False},
            ),
            "announcements": self._persist_raw_dataset(
                "announcements", "cninfo/direct", announcements, asof
            ),
            "industry_history": self._persist_raw_dataset(
                "industry_history", "cninfo/direct", industry_changes, asof
            ),
        }
        announcements_by_code: dict[str, list[dict[str, Any]]] = {}
        for item in announcements:
            available_at = pd.Timestamp(item.get("effective_at") or item["published_at"])
            if available_at <= asof + pd.Timedelta(hours=15):
                announcements_by_code.setdefault(str(item["code"]), []).append(item)
        industries_by_code: dict[str, list[dict[str, Any]]] = {}
        for item in industry_changes:
            if pd.Timestamp(item["effective_at"]) <= asof + pd.Timedelta(hours=15):
                industries_by_code.setdefault(str(item["code"]), []).append(item)
        supplemental: dict[str, dict[str, Any]] = {}
        technical_by_code = {str(row["code"]): row for row in preselected}
        for code in selected_codes:
            info = stock_info.get(code, {})
            active_capital = _first_number(info.get("ActiveCapital")) or 0.0
            average_volume = float(
                technical_by_code[code].get("avg_volume_20") or 0.0
            )
            industry_records = industries_by_code.get(code, [])
            latest_industry = max(
                industry_records,
                key=lambda item: pd.Timestamp(item["effective_at"]),
                default={},
            )
            event_items = announcements_by_code.get(code, [])
            event = _aggregate_events(event_items)
            values = {
                **_financial_features(financial, code, asof),
                **_flow_features(flows, code, asof),
                **_consensus_features(consensus, code),
                "industry": str(latest_industry.get("industry") or "未分类"),
                "is_st": _truthy(info.get("IsSTGP")),
                "is_quit": _truthy(info.get("IsQuitGP")),
                "event_score": event["score"],
                "event_type": event["event_type"],
                "turnover_20": (
                    average_volume / (active_capital * 100.0)
                    if active_capital > 0
                    else np.nan
                ),
                "evidence_refs": [
                    *event["evidence_refs"],
                    f"tdx:financial:{code}:{end}",
                    f"tdx:flow:{code}:{end}",
                ],
                "published_at": event.get("latest_published_at") or asof.isoformat(),
                "effective_at": latest_industry.get("effective_at") or asof.isoformat(),
            }
            supplemental[code] = values
        combined = []
        for row in preselected:
            code = str(row["code"])
            combined.append({**row, **supplemental[code]})
        _attach_industry_cross_section(combined)
        frame = pd.DataFrame(combined)
        financial_coverage = float(frame["revenue_yoy"].notna().mean())
        industry_coverage = float((frame["industry"] != "未分类").mean())
        if financial_coverage < 0.85:
            raise ResearchDataBlockedError(
                f"财务时点覆盖率 {financial_coverage:.1%} 低于 85%"
            )
        if industry_coverage < 0.90:
            raise ResearchDataBlockedError(
                f"行业时点覆盖率 {industry_coverage:.1%} 低于 90%"
            )
        return frame, {
            "universe_symbols": len(codes),
            "technical_rows": len(technical),
            "prefilter_rows": len(frame),
            "financial_coverage": financial_coverage,
            "industry_coverage": industry_coverage,
            "announcement_rows": len(announcements),
            "industry_change_rows": len(industry_changes),
            "consensus_snapshot_policy": "FORWARD_ONLY",
            "raw_batch_ids": {
                dataset: record["batch_id"] for dataset, record in raw_batches.items()
            },
        }

    def _persist_raw_frames(
        self,
        dataset: str,
        source: str,
        frames: Mapping[str, pd.DataFrame],
        asof: pd.Timestamp,
    ) -> dict[str, Any]:
        rows: list[pd.DataFrame] = []
        for code, raw in frames.items():
            if raw.empty:
                continue
            frame = raw.copy().reset_index()
            frame.rename(columns={frame.columns[0]: "bar_time"}, inplace=True)
            frame.insert(0, "code", str(code))
            rows.append(frame)
        payload = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["code", "bar_time"])
        self.config.ensure_runtime_dirs()
        batch_id = f"ewr_{uuid4().hex}"
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "raw" / dataset
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch_id}.parquet"
        payload.to_parquet(path, index=False)
        schema = json.dumps(
            [(column, str(dtype)) for column, dtype in payload.dtypes.items()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        record = {
            "batch_id": batch_id,
            "project_id": PROJECT_ID,
            "dataset": dataset,
            "source": source,
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": None,
            "published_end": str(asof.date()),
            "row_count": len(payload),
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": hashlib.sha256(schema.encode("utf-8")).hexdigest(),
            "metadata": {"adjustment": "none" if dataset == "market_none" else "front"},
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def _persist_raw_dataset(
        self,
        dataset: str,
        source: str,
        payload: Any,
        asof: pd.Timestamp,
        *,
        batch_id: str | None = None,
        published_start: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.config.ensure_runtime_dirs()
        batch_id = batch_id or f"ewr_{uuid4().hex}"
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "raw" / dataset
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch_id}.json"
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        serialized_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if path.exists():
            if _file_sha256(path) != serialized_hash:
                raise ResearchDataBlockedError(f"raw batch path collision: {path}")
        else:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(path)
        if isinstance(payload, Mapping):
            schema_shape: Any = sorted(map(str, payload.keys()))
            row_count = len(payload)
        elif isinstance(payload, list):
            keys = sorted({str(key) for item in payload if isinstance(item, Mapping) for key in item})
            schema_shape = keys
            row_count = len(payload)
        else:
            schema_shape = type(payload).__name__
            row_count = 1
        record = {
            "batch_id": batch_id,
            "project_id": PROJECT_ID,
            "dataset": dataset,
            "source": source,
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": published_start,
            "published_end": str(asof.date()),
            "row_count": row_count,
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": hashlib.sha256(
                json.dumps(schema_shape, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "metadata": dict(metadata or {}),
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def train(self, progress_callback: Any | None = None) -> dict[str, Any]:
        self._reject_legacy_write_path("train")
        self.database.update_research_project(PROJECT_ID, status="VALIDATING")
        self._progress(progress_callback, "LOAD_FEATURES", 0.05, "读取点时特征历史")
        feature_snapshot = self._feature_snapshot()
        frame = self._load_feature_history(feature_snapshot["batches"])
        model_frame, feature_names = self._model_frame(frame)
        years = set(pd.to_datetime(model_frame["asof"]).dt.year)
        required_years = set(range(2018, 2026))
        if not required_years.issubset(years):
            missing = sorted(required_years - years)
            self.database.update_research_project(PROJECT_ID, status="BLOCKED_DATA")
            raise ResearchDataBlockedError(f"模型冻结窗口缺少年份: {missing}")
        self._progress(progress_callback, "WALK_FORWARD", 0.20, "运行 2024/2025 滚动样本外验证")
        split_specs = (
            (2018, 2022, 2023, 2024),
            (2019, 2023, 2024, 2025),
        )
        split_metrics: dict[str, Any] = {}
        walk_forward_models: dict[str, Any] = {}
        for index, (train_start, train_end, validation_year, test_year) in enumerate(split_specs):
            model, metrics = self._fit_split(
                model_frame,
                feature_names,
                train_start=train_start,
                train_end=train_end,
                validation_year=validation_year,
                test_year=test_year,
            )
            split_metrics[str(test_year)] = metrics
            walk_forward_models[str(test_year)] = model
            self._progress(
                progress_callback,
                "WALK_FORWARD",
                0.30 + 0.25 * (index + 1),
                f"{test_year} 冻结测试完成",
            )
        final_training = model_frame.loc[
            pd.to_datetime(model_frame["asof"]).dt.year <= 2025
        ].copy()
        final_training = _purge_tail_dates(final_training, 60)
        if final_training.empty or final_training["target"].nunique() < 2:
            raise ResearchDataBlockedError("最终训练窗口在 60 日清洗后缺少两类样本")
        model = self._new_model()
        model.fit(final_training[list(feature_names)], final_training["target"].astype(int))
        self.config.ensure_runtime_dirs()
        model_id = f"ewm_{uuid4().hex}"
        model_dir = self.config.runtime_dir / "research" / PROJECT_ID / "models"
        model_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = model_dir / f"{model_id}.pkl"
        artifact = {
            "model": model,
            "walk_forward_models": walk_forward_models,
            "feature_names": feature_names,
            "feature_columns": MODEL_FEATURE_COLUMNS,
            "feature_pipeline_version": MODEL_FEATURE_PIPELINE_VERSION,
            "missing_value_fill": MODEL_MISSING_VALUE_FILL,
            "parameters": MODEL_PARAMETERS,
            "random_seed": MODEL_RANDOM_SEED,
        }
        artifact_path.write_bytes(pickle.dumps(artifact, protocol=pickle.HIGHEST_PROTOCOL))
        schema_hash = hashlib.sha256(
            json.dumps(
                {
                    "feature_names": feature_names,
                    "pipeline_version": MODEL_FEATURE_PIPELINE_VERSION,
                    "missing_value_fill": MODEL_MISSING_VALUE_FILL,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        import sklearn

        record = {
            "model_id": model_id,
            "project_id": PROJECT_ID,
            "strategy_id": ML_STRATEGY_ID,
            "status": "SUCCEEDED",
            "created_at": datetime.now().astimezone().isoformat(),
            "artifact_path": str(artifact_path),
            "artifact_hash": _file_sha256(artifact_path),
            "feature_schema_hash": schema_hash,
            "training_start": "2018-01-01",
            "training_end": str(pd.to_datetime(final_training["asof"]).max().date()),
            "validation_start": "2023-01-01",
            "validation_end": "2024-12-31",
            "test_start": "2024-01-01",
            "test_end": "2025-12-31",
            "random_seed": MODEL_RANDOM_SEED,
            "library_version": str(sklearn.__version__),
            "metrics": {"splits": split_metrics},
            "metadata": {
                "parameters": MODEL_PARAMETERS,
                "feature_names": feature_names,
                "feature_pipeline_version": MODEL_FEATURE_PIPELINE_VERSION,
                "missing_value_fill": MODEL_MISSING_VALUE_FILL,
                "purge_trading_days": 60,
                "embargo_trading_days": 20,
                "excluded_year": 2026,
                "dependencies": {
                    "python": platform.python_version(),
                    "numpy": np.__version__,
                    "pandas": pd.__version__,
                    "scikit_learn": sklearn.__version__,
                },
                "python_implementation": sys.implementation.name,
                "snapshot_id": feature_snapshot["snapshot_id"],
                "snapshot_hash": feature_snapshot["snapshot_hash"],
                "snapshot_batches": feature_snapshot["batches"],
            },
            "error": "",
        }
        if self._feature_snapshot()["snapshot_id"] != feature_snapshot["snapshot_id"]:
            artifact_path.unlink(missing_ok=True)
            raise ResearchDataBlockedError("训练期间特征批次发生变化；模型文件未登记")
        try:
            self._refresh_ml_candidates(
                model,
                feature_names,
                frame,
                model_id,
                snapshot_id=feature_snapshot["snapshot_id"],
            )
        except Exception:
            artifact_path.unlink(missing_ok=True)
            raise
        self.database.save_research_model(record)
        self.database.update_research_project(PROJECT_ID, status="VALIDATING")
        self._progress(progress_callback, "COMPLETED", 1.0, "ML 模型和候选榜已生成")
        return {"project_id": PROJECT_ID, "status": "VALIDATING", "model": record}

    def validate(self, progress_callback: Any | None = None) -> dict[str, Any]:
        self._reject_legacy_write_path("validate")
        self.database.update_research_project(PROJECT_ID, status="VALIDATING")
        model_row = self._latest_decoded("research_models")
        if not model_row or model_row.get("status") != "SUCCEEDED":
            raise ResearchDataBlockedError("尚无可验证的 ML 模型")
        model_metadata = dict(model_row.get("metadata") or {})
        frozen_snapshot_id = str(model_metadata.get("snapshot_id") or "")
        current_snapshot = self._feature_snapshot()
        if not frozen_snapshot_id or current_snapshot["snapshot_id"] != frozen_snapshot_id:
            raise ResearchDataBlockedError("训练与验证不是同一不可变特征快照")
        frame = self._load_feature_history(current_snapshot["batches"])
        self._validate_execution_outcomes(frame)
        artifact_path = Path(str(model_row["artifact_path"]))
        if not artifact_path.exists() or _file_sha256(artifact_path) != model_row["artifact_hash"]:
            raise ResearchDataBlockedError("ML 模型文件缺失或哈希不一致")
        artifact = pickle.loads(artifact_path.read_bytes())
        feature_names = tuple(artifact["feature_names"])
        model_frame, _ = self._model_frame(frame, expected_feature_names=feature_names)
        walk_forward_models = artifact.get("walk_forward_models")
        if not isinstance(walk_forward_models, dict) or set(walk_forward_models) != {
            "2024",
            "2025",
        }:
            raise ResearchDataBlockedError(
                "ML model artifact is missing frozen 2024/2025 walk-forward models"
            )
        model_frame["probability"] = np.nan
        model_years = pd.to_datetime(model_frame["asof"]).dt.year
        for year in (2024, 2025):
            positions = model_years == year
            if not bool(positions.any()):
                raise ResearchDataBlockedError(
                    f"validation snapshot has no rows for frozen test year {year}"
                )
            split_model = walk_forward_models[str(year)]
            model_frame.loc[positions, "probability"] = split_model.predict_proba(
                model_frame.loc[positions, list(feature_names)]
            )[:, 1]
        model_frame = self._attach_validation_eligibility(model_frame)
        self._progress(progress_callback, "VALIDATION", 0.25, "计算规则、ML 与纯 RS60 基准")
        methods = {
            "rule": self._evaluate_method(model_frame, "rule_score", "rule_eligible"),
            "ml": self._evaluate_method(model_frame, "probability", "common_eligible"),
            "baseline": self._evaluate_method(
                model_frame, "relative_return_60", "common_eligible"
            ),
        }
        gates: dict[str, Any] = {}
        for method in ("rule", "ml"):
            yearly: dict[str, bool] = {}
            for year in (2024, 2025):
                candidate = methods[method]["yearly"].get(str(year), {})
                baseline = methods["baseline"]["yearly"].get(str(year), {})
                yearly[str(year)] = bool(
                    candidate
                    and baseline
                    and candidate["precision_at_20"] > baseline["precision_at_20"]
                    and candidate["total_return"] > baseline["total_return"]
                    and candidate["double_cost_return"] > baseline["double_cost_return"]
                    and candidate["max_drawdown"] >= baseline["max_drawdown"] - 0.03
                )
            gates[method] = {"yearly": yearly, "passed": all(yearly.values())}
        passed = bool(gates["rule"]["passed"] or gates["ml"]["passed"])
        status = "OBSERVATION_ONLY" if passed else "VALIDATION_REJECTED"
        now = datetime.now().astimezone().isoformat()
        validation_id = f"ewv_{uuid4().hex}"
        snapshot_id = frozen_snapshot_id
        champion = _select_validation_champion(
            gates=gates,
            model_row=model_row,
            snapshot_id=snapshot_id,
            validation_id=validation_id,
            selected_at=now,
            rule_artifact_hash=self._rule_artifact_hash(),
        )
        validation = {
            "validation_id": validation_id,
            "project_id": PROJECT_ID,
            "status": status,
            "created_at": now,
            "finished_at": now,
            "snapshot_id": snapshot_id,
            "rule_metrics": methods["rule"],
            "ml_metrics": methods["ml"],
            "baseline_metrics": methods["baseline"],
            "stress_metrics": {
                "rule_double_cost": methods["rule"].get("double_cost_return"),
                "ml_double_cost": methods["ml"].get("double_cost_return"),
                "baseline_double_cost": methods["baseline"].get("double_cost_return"),
                "return_policy": {
                    "type": "NON_OVERLAPPING_CAPITAL_CYCLES",
                    "holding_trading_days": VALIDATION_HOLDING_TRADING_DAYS,
                    "periods_per_year": VALIDATION_PERIODS_PER_YEAR,
                    "weekly_metrics": ["precision_at_20", "pr_auc", "ic"],
                },
            },
            "gates": gates,
            "champion": champion,
            "error": "",
        }
        self.database.save_research_validation(validation)
        self.database.update_research_project(PROJECT_ID, status=status)
        self._progress(progress_callback, "COMPLETED", 1.0, f"验证完成：{status}")
        return {"project_id": PROJECT_ID, **validation}

    @staticmethod
    def _rule_artifact_hash() -> str:
        parameters = {
            key: value
            for key, value in vars(EarlyWinnerParameters()).items()
            if not key.startswith("_")
        }
        payload = {
            "feature_columns": list(FEATURE_COLUMNS),
            "parameters": parameters,
            "score_rule_candidates": inspect.getsource(score_rule_candidates),
            "early_winner_exit_reason": inspect.getsource(early_winner_exit_reason),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _fit_split(
        self,
        frame: pd.DataFrame,
        feature_names: tuple[str, ...],
        *,
        train_start: int,
        train_end: int,
        validation_year: int,
        test_year: int,
    ) -> tuple[Any, dict[str, Any]]:
        years = pd.to_datetime(frame["asof"]).dt.year
        train = frame.loc[(years >= train_start) & (years <= train_end)].copy()
        validation = frame.loc[years == validation_year].copy()
        test = frame.loc[years == test_year].copy()
        train = _purge_tail_dates(train, 60)
        validation = _embargo_head_dates(validation, 20)
        test = _embargo_head_dates(test, 20)
        if train["target"].nunique() < 2 or validation.empty or test.empty:
            raise ResearchDataBlockedError(
                f"{test_year} split lacks two-class training, validation, or test rows"
            )
        model = self._new_model()
        model.fit(train[list(feature_names)], train["target"].astype(int))
        validation_probability = model.predict_proba(validation[list(feature_names)])[:, 1]
        test_probability = model.predict_proba(test[list(feature_names)])[:, 1]
        return model, {
            "train_rows": len(train),
            "validation_rows": len(validation),
            "test_rows": len(test),
            "validation": _classification_metrics(validation["target"], validation_probability),
            "test": _classification_metrics(test["target"], test_probability),
        }

    def _new_model(self) -> Any:
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(**MODEL_PARAMETERS)

    def _model_frame(
        self,
        frame: pd.DataFrame,
        *,
        expected_feature_names: Iterable[str] | None = None,
    ) -> tuple[pd.DataFrame, tuple[str, ...]]:
        data = frame.copy()
        if "forward_return_60" in data:
            data["target"] = 0
            for _, group in data.groupby("asof", sort=False):
                marked = mark_research_universe_eligibility(group.to_dict("records"))
                eligible_by_code = {
                    str(item["code"]): bool(item.get("eligible")) for item in marked
                }
                universe = group["code"].astype(str).map(eligible_by_code).fillna(False)
                if "entry_executable" in group:
                    universe &= group["entry_executable"].fillna(False).astype(bool)
                qualified = group.loc[universe]
                returns = pd.to_numeric(
                    qualified["forward_return_60"], errors="coerce"
                ).dropna()
                if returns.empty:
                    continue
                threshold = float(returns.quantile(0.95))
                winners = pd.to_numeric(
                    qualified["forward_return_60"], errors="coerce"
                ) >= threshold
                data.loc[qualified.index, "target"] = winners.astype(int)
        elif "target" not in data:
            raise ResearchDataBlockedError("特征历史缺少 forward_return_60")
        feature_names: list[str] = []
        for column in MODEL_FEATURE_COLUMNS:
            if column not in data:
                data[column] = np.nan
            data[column] = pd.to_numeric(data[column], errors="coerce")
            missing = f"{column}__missing"
            data[missing] = data[column].isna().astype(float)
            # Keep the missingness signal explicit while giving sklearn a finite,
            # deterministic value.  Some supported sklearn/numpy combinations
            # cannot bin a feature that is entirely NaN in a frozen training split.
            data[column] = data[column].fillna(MODEL_MISSING_VALUE_FILL)
            feature_names.extend((column, missing))
        if expected_feature_names is not None:
            feature_names = list(expected_feature_names)
            missing = [column for column in feature_names if column not in data]
            if missing:
                raise ResearchDataBlockedError(f"模型特征版本不兼容: {missing}")
        return data, tuple(feature_names)

    def _refresh_ml_candidates(
        self,
        model: Any,
        feature_names: tuple[str, ...],
        frame: pd.DataFrame,
        model_id: str,
        *,
        snapshot_id: str,
    ) -> None:
        latest_asof = pd.to_datetime(frame["asof"]).max()
        latest = frame.loc[pd.to_datetime(frame["asof"]) == latest_asof].copy()
        model_frame, _ = self._model_frame(
            latest.assign(target=0), expected_feature_names=feature_names
        )
        latest["probability"] = model.predict_proba(model_frame[list(feature_names)])[:, 1]
        latest = pd.DataFrame(
            mark_research_universe_eligibility(latest.to_dict("records")),
            index=latest.index,
        )
        latest["factors"] = latest.apply(
            lambda row: {
                column: float(row[column])
                for column in MODEL_FEATURE_COLUMNS
                if column in row and pd.notna(row[column])
            },
            axis=1,
        )
        latest["gates"] = latest.apply(
            lambda row: {
                "universe": bool(row.get("universe_gate")),
                "extreme_heat": bool(row.get("extreme_heat")),
            },
            axis=1,
        )
        latest["evidence_refs"] = latest.apply(
            lambda row: [
                f"model:{model_id}",
                *_normalize_evidence_refs(row.get("evidence_refs")),
            ],
            axis=1,
        )
        candidates = select_ml_candidates(latest.to_dict("records"))
        self._persist_candidates(
            method="ml",
            strategy_id=ML_STRATEGY_ID,
            asof=str(latest_asof.date()),
            run_id=uuid4().hex,
            snapshot_id=snapshot_id,
            candidates=candidates,
        )

    def _attach_validation_eligibility(self, frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        data["rule_score"] = -np.inf
        data["rule_eligible"] = False
        data["common_eligible"] = False
        for _, group in data.groupby("asof", sort=False):
            common = mark_research_universe_eligibility(group.to_dict("records"))
            common_by_code = {
                str(item["code"]): bool(item.get("eligible")) for item in common
            }
            data.loc[group.index, "common_eligible"] = group["code"].astype(str).map(
                common_by_code
            ).fillna(False)
            selected = score_rule_candidates(group.to_dict("records"))
            score_by_code = {
                str(item["code"]): float(item["score"]) for item in selected
            }
            matched = group["code"].astype(str).isin(score_by_code)
            matched_index = group.index[matched]
            data.loc[matched_index, "rule_eligible"] = True
            data.loc[matched_index, "rule_score"] = group.loc[
                matched_index, "code"
            ].astype(str).map(score_by_code)
        return data

    def _evaluate_method(
        self,
        frame: pd.DataFrame,
        score_column: str,
        eligibility_column: str,
    ) -> dict[str, Any]:
        tests = frame.loc[pd.to_datetime(frame["asof"]).dt.year.isin((2024, 2025))].copy()
        yearly: dict[str, Any] = {}
        for year in (2024, 2025):
            subset = tests.loc[pd.to_datetime(tests["asof"]).dt.year == year]
            metrics, _, yearly_ic = _evaluate_non_overlapping_portfolio(
                subset,
                score_column=score_column,
                eligibility_column=eligibility_column,
            )
            metrics["ic"] = float(np.mean(yearly_ic)) if yearly_ic else 0.0
            metrics.update(_ranking_metrics(subset, score_column, eligibility_column))
            yearly[str(year)] = metrics
        combined, selected_returns, ic_values = _evaluate_non_overlapping_portfolio(
            tests,
            score_column=score_column,
            eligibility_column=eligibility_column,
        )
        combined["ic"] = float(np.mean(ic_values)) if ic_values else 0.0
        combined.update(_ranking_metrics(tests, score_column, eligibility_column))
        combined.update(_top_five_attribution(selected_returns))
        return {"yearly": yearly, **combined}

    def _load_feature_history(
        self,
        batches: Iterable[Mapping[str, Any]] | None = None,
    ) -> pd.DataFrame:
        batch_rows = list(batches) if batches is not None else self.database.query(
            """SELECT * FROM research_data_batches
            WHERE project_id=? AND dataset='early_winner_features' AND status='SUCCEEDED'
            ORDER BY batch_id""",
            (PROJECT_ID,),
        )
        frames: list[pd.DataFrame] = []
        for batch in batch_rows:
            path = Path(str(batch.get("path") or ""))
            if not path.exists() or _file_sha256(path) != str(batch.get("content_hash") or ""):
                continue
            frames.append(pd.read_parquet(path))
        if not frames:
            raise ResearchDataBlockedError("无可用的点时特征批次")
        frame = pd.concat(frames, ignore_index=True)
        frame = frame.sort_values(["asof", "code"]).drop_duplicates(["asof", "code"], keep="last")
        self._validate_feature_frame(frame)
        return frame

    def _feature_snapshot(self) -> dict[str, Any]:
        batches = self.database.query(
            """SELECT batch_id, path, content_hash, schema_hash, published_start,
            published_end, row_count FROM research_data_batches
            WHERE project_id=? AND dataset='early_winner_features' AND status='SUCCEEDED'
            ORDER BY batch_id""",
            (PROJECT_ID,),
        )
        if not batches:
            raise ResearchDataBlockedError("无可用的点时特征批次")
        components = []
        for batch in batches:
            path = Path(str(batch.get("path") or ""))
            if not path.exists() or _file_sha256(path) != str(batch.get("content_hash") or ""):
                raise ResearchDataBlockedError(
                    f"特征批次文件缺失或哈希不一致: {batch.get('batch_id')}"
                )
            components.append(dict(batch))
        payload = json.dumps(
            [
                {
                    "batch_id": item["batch_id"],
                    "content_hash": item["content_hash"],
                    "schema_hash": item["schema_hash"],
                    "row_count": item["row_count"],
                }
                for item in components
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return {
            "snapshot_id": f"ewfs_{digest[:32]}",
            "snapshot_hash": digest,
            "batches": components,
        }

    def _validate_feature_frame(self, frame: pd.DataFrame) -> None:
        required = {"code", "asof", *MODEL_FEATURE_COLUMNS}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise ResearchDataBlockedError(f"特征批次缺少字段: {missing}")
        asof = _decision_timestamps(frame["asof"])
        if asof.isna().any():
            raise ResearchDataBlockedError("特征批次包含无效 asof")
        if "published_at" in frame:
            published = pd.to_datetime(frame["published_at"], errors="coerce")
            if published.isna().any() or bool((published > asof).any()):
                raise ResearchDataBlockedError("发现 published_at 晚于决策时点")
        if "effective_at" in frame:
            effective = pd.to_datetime(frame["effective_at"], errors="coerce")
            if effective.isna().any() or bool((effective > asof).any()):
                raise ResearchDataBlockedError("发现 effective_at 晚于决策时点")

    def _validate_execution_outcomes(self, frame: pd.DataFrame) -> None:
        required = {"code", "industry", "forward_return_60", "entry_executable"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise ResearchDataBlockedError(f"验证批次缺少执行/标签字段: {missing}")
        outcomes = pd.to_numeric(frame["forward_return_60"], errors="coerce")
        if outcomes.isna().all():
            raise ResearchDataBlockedError("验证批次没有可用的未复权下一开盘执行收益")
        test_years = set(pd.to_datetime(frame["asof"]).dt.year)
        if not {2024, 2025}.issubset(test_years):
            raise ResearchDataBlockedError("验证批次必须完整覆盖 2024 和 2025")

    def _latest_feature_batch(self) -> dict[str, Any] | None:
        rows = self.database.query(
            """SELECT * FROM research_data_batches
            WHERE project_id=? AND dataset='early_winner_features' AND status='SUCCEEDED'
            ORDER BY fetched_at DESC LIMIT 1""",
            (PROJECT_ID,),
        )
        if not rows:
            return None
        path = Path(str(rows[0].get("path") or ""))
        if not path.exists() or _file_sha256(path) != str(rows[0].get("content_hash") or ""):
            return None
        return rows[0]

    def _persist_candidates(
        self,
        *,
        method: str,
        strategy_id: str,
        asof: str,
        run_id: str,
        snapshot_id: str,
        candidates: Iterable[Mapping[str, Any]],
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        records = []
        for candidate in candidates:
            records.append(
                {
                    **dict(candidate),
                    "candidate_id": uuid4().hex,
                    "run_id": run_id,
                    "strategy_id": strategy_id,
                    "snapshot_id": snapshot_id,
                    "created_at": now,
                }
            )
        self.database.replace_research_candidates(PROJECT_ID, method, asof, records)

    def _latest_decoded(self, table: str) -> dict[str, Any] | None:
        if table not in {"research_models", "research_validations"}:
            raise ValueError(table)
        rows = self.database.query(
            f"SELECT * FROM {table} WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
            (PROJECT_ID,),
        )
        return self._decoded_rows(rows)[0] if rows else None

    @staticmethod
    def _reject_legacy_write_path(action: str) -> None:
        raise ResearchDataBlockedError(
            f"Legacy early_winner_v1 {action} is retired because its historical universe "
            "failed the survivorship-bias audit. Training and frozen validation must run in "
            "a newly sealed versioned research project."
        )

    def _decoded_rows(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        json_fields = {
            "metadata_json",
            "factor_json",
            "gate_json",
            "evidence_refs_json",
            "metrics_json",
            "rule_metrics_json",
            "ml_metrics_json",
            "baseline_metrics_json",
            "stress_metrics_json",
            "gates_json",
            "champion_json",
        }
        output: list[dict[str, Any]] = []
        for raw in rows:
            row: dict[str, Any] = {}
            for key, value in raw.items():
                if key in json_fields:
                    row[key.removesuffix("_json")] = _decode_json(value, {} if key != "evidence_refs_json" else [])
                else:
                    row[key] = value
            output.append(row)
        return output

    @staticmethod
    def _strategy_record(strategy: Any) -> dict[str, Any]:
        metadata = strategy.metadata
        return {
            "strategy_id": metadata.strategy_id,
            "version": metadata.version,
            "name": metadata.name,
            "lifecycle": metadata.lifecycle,
            "category": str(metadata.category),
            "scan_enabled": metadata.scan_enabled,
            "backtest_enabled": metadata.backtest_enabled,
        }

    @staticmethod
    def _progress(callback: Any | None, phase: str, progress: float, detail: str) -> None:
        if callback is not None:
            callback(
                phase=phase,
                progress=progress,
                detail=detail,
                cache_status="",
                waiting_reason="",
            )


def _extract_stock_codes(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = value.get("Code") or value.get("code") or value.get("codes") or value.get("Value")
        return _extract_stock_codes(values)
    if not isinstance(value, list):
        return []
    codes: list[str] = []
    for item in value:
        code = item.get("Code") if isinstance(item, dict) else item
        text = str(code or "").upper()
        if text.endswith((".SH", ".SZ", ".BJ")):
            codes.append(text)
    return sorted(set(codes))


def _market_frames_from_rpc(value: Any, codes: Iterable[str]) -> dict[str, pd.DataFrame]:
    requested = [str(code) for code in codes]
    result: dict[str, pd.DataFrame] = {}
    if not isinstance(value, dict):
        return result
    for code in requested:
        direct = value.get(code)
        frame = _rpc_frame(direct)
        if frame is None:
            columns: dict[str, pd.Series] = {}
            for field in (
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
                "Amount",
                "ForwardFactor",
            ):
                series = _rpc_series(value.get(field) or value.get(field.lower()), code)
                if series is not None:
                    columns[field] = series
            if columns:
                frame = pd.DataFrame(columns)
        if frame is None or frame.empty:
            continue
        rename = {str(column).lower(): column for column in frame.columns}
        normalized = pd.DataFrame(index=frame.index)
        for field in (
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "Amount",
            "ForwardFactor",
        ):
            source = rename.get(field.lower())
            if source is not None:
                normalized[field] = pd.to_numeric(frame[source], errors="coerce")
        normalized.index = pd.to_datetime(normalized.index, errors="coerce")
        normalized = normalized.loc[~normalized.index.isna()].sort_index()
        if "Amount" in normalized:
            normalized["Amount"] = normalized["Amount"] * 10_000.0
        if {"Open", "High", "Low", "Close", "Volume", "Amount"}.issubset(normalized.columns):
            result[code] = normalized.dropna(subset=["Close"])
    return result


def _audit_forward_factor_semantics(
    raw: pd.DataFrame,
    front: pd.DataFrame,
    *,
    maximum_relative_error: float = 0.001,
) -> dict[str, Any]:
    """Verify TDX's forward factor is a multiplicative total-return factor.

    A constant normalization scalar may separate ``raw * factor`` and the
    displayed front-adjusted series, so the invariant is checked on adjacent
    returns.  The probe window deliberately spans Kweichow Moutai's 2023
    ex-dividend session; no factor change means the semantic contract was not
    actually exercised and admission fails closed.
    """
    if "ForwardFactor" not in raw:
        raise ResearchDataBlockedError("TDX raw market contract missing ForwardFactor")
    common = raw.index.intersection(front.index).sort_values()
    if len(common) < 2:
        raise ResearchDataBlockedError("TDX ForwardFactor audit has insufficient overlap")
    raw_close = pd.to_numeric(raw.loc[common, "Close"], errors="coerce")
    factor = pd.to_numeric(raw.loc[common, "ForwardFactor"], errors="coerce")
    front_close = pd.to_numeric(front.loc[common, "Close"], errors="coerce")
    valid = raw_close.gt(0) & factor.gt(0) & front_close.gt(0)
    raw_close = raw_close.loc[valid]
    factor = factor.loc[valid]
    front_close = front_close.loc[valid]
    if len(raw_close) < 2 or int(factor.nunique()) < 2:
        raise ResearchDataBlockedError(
            "TDX ForwardFactor audit did not span a positive factor change"
        )
    implied_return = (raw_close * factor).pct_change()
    front_return = front_close.pct_change()
    relative_error = (implied_return - front_return).abs().dropna()
    if relative_error.empty:
        raise ResearchDataBlockedError("TDX ForwardFactor audit produced no comparisons")
    max_error = float(relative_error.max())
    if not np.isfinite(max_error) or max_error > maximum_relative_error:
        raise ResearchDataBlockedError(
            "TDX ForwardFactor multiplication semantics failed: "
            f"max return error {max_error:.8f}"
        )
    return {
        "ready": True,
        "probe_start": pd.Timestamp(common.min()).date().isoformat(),
        "probe_end": pd.Timestamp(common.max()).date().isoformat(),
        "rows": int(len(raw_close)),
        "factor_values": int(factor.nunique()),
        "max_adjacent_return_error": max_error,
        "maximum_allowed_error": maximum_relative_error,
        "formula": "front_return ~= pct_change(raw_close * ForwardFactor)",
    }


def _rpc_frame(value: Any) -> pd.DataFrame | None:
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        frame = pd.DataFrame(value)
        date_column = next(
            (column for column in ("Date", "date", "Time", "time") if column in frame),
            None,
        )
        if date_column:
            return frame.set_index(date_column)
    if isinstance(value, dict):
        if {"columns", "index", "data"}.issubset(value):
            return pd.DataFrame(value["data"], columns=value["columns"], index=value["index"])
        list_columns = {
            str(key): item
            for key, item in value.items()
            if isinstance(item, list)
        }
        lengths = {len(item) for item in list_columns.values()}
        if list_columns and len(lengths) == 1:
            frame = pd.DataFrame(list_columns)
            date_column = next(
                (column for column in ("Date", "date", "datetime") if column in frame),
                None,
            )
            if date_column:
                return frame.set_index(date_column)
        if value and all(isinstance(item, dict) for item in value.values()):
            frame = pd.DataFrame.from_dict(value, orient="index")
            if any(str(column).lower() == "close" for column in frame.columns):
                return frame
    return None


def _rpc_series(value: Any, code: str) -> pd.Series | None:
    if isinstance(value, dict) and code in value:
        value = value[code]
    if isinstance(value, dict):
        if {"index", "data"}.issubset(value):
            data = value["data"]
            if isinstance(data, list) and data and isinstance(data[0], list):
                data = [item[0] if item else None for item in data]
            return pd.Series(data, index=value["index"])
        if value and not any(isinstance(item, (dict, list)) for item in value.values()):
            return pd.Series(value)
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        dates: list[Any] = []
        values: list[Any] = []
        for item in value:
            item_code = str(item.get("Code") or item.get("code") or code)
            if item_code != code:
                continue
            dates.append(item.get("Date") or item.get("date") or item.get("Time"))
            values.append(item.get("Value") or item.get("value"))
        if values:
            return pd.Series(values, index=dates)
    return None


def _cninfo_accept_enckey(timestamp: int | None = None) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = b"1234567887654321"
    plaintext = str(int(time.time()) if timestamp is None else timestamp).encode("ascii")
    padding_size = 16 - len(plaintext) % 16
    padded = plaintext + bytes([padding_size]) * padding_size
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _validate_yyyymmdd_range(start_date: str, end_date: str) -> None:
    try:
        start = datetime.strptime(start_date, "%Y%m%d").date()
        end = datetime.strptime(end_date, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("CNINFO dates must use YYYYMMDD") from exc
    if start > end:
        raise ValueError("CNINFO start_date must not be after end_date")


def _cninfo_date(value: str) -> str:
    _validate_yyyymmdd_range(value, value)
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def _unique_cninfo_codes(codes: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in codes:
        code = str(value).split(".", 1)[0].strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"Invalid CNINFO stock code: {value}")
        if code not in seen:
            seen.add(code)
            output.append(code)
    return output


def _required_non_negative_int(payload: Mapping[str, Any], field: str) -> int:
    try:
        value = int(payload[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise CninfoContractError(f"CNINFO response is missing integer {field}") from exc
    if value < 0:
        raise CninfoContractError(f"CNINFO response has negative {field}")
    return value


def _cninfo_announcement_time(value: Any) -> str:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            raise CninfoContractError(f"Invalid CNINFO announcementTime: {value}")
        return timestamp.isoformat()
    local = datetime.fromtimestamp(numeric / 1_000, tz=ZoneInfo("Asia/Shanghai"))
    return local.replace(tzinfo=None).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _standard_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.endswith((".SH", ".SZ", ".BJ")):
        return text
    digits = "".join(character for character in text if character.isdigit())[-6:]
    if len(digits) != 6:
        return ""
    if digits.startswith(("4", "8", "92")):
        return f"{digits}.BJ"
    if digits.startswith(("0", "2", "3")):
        return f"{digits}.SZ"
    return f"{digits}.SH"


def _field_values(
    payload: Any,
    field: str,
    code: str,
    *,
    component: int = 0,
) -> list[tuple[pd.Timestamp, float]]:
    values: list[tuple[pd.Timestamp, float]] = []

    def append(date_value: Any, raw_value: Any) -> None:
        timestamp = _professional_timestamp(date_value)
        numeric = _component_number(raw_value, component)
        if timestamp is not None and numeric is not None:
            values.append((timestamp, numeric))

    def visit(node: Any, field_scope: bool = False) -> None:
        if isinstance(node, dict):
            node_code = str(node.get("Code") or node.get("code") or "")
            if not field_scope and code in node:
                visit(node[code], False)
                return
            if field in node and (not node_code or node_code == code):
                date_values = (
                    node.get("announce_time")
                    or node.get("Date")
                    or node.get("date")
                    or node.get("tag_time")
                )
                raw_values = node[field]
                if isinstance(date_values, list) and isinstance(raw_values, list):
                    for date_value, raw_value in zip(date_values, raw_values):
                        append(date_value, raw_value)
                else:
                    append(date_values, raw_values)
            if field_scope:
                if node_code and node_code != code:
                    return
                if code in node:
                    child = node[code]
                    if isinstance(child, (dict, list)):
                        visit(child, True)
                    else:
                        append(None, child)
                    return
                if {"index", "data"}.issubset(node):
                    data = node["data"]
                    for date_value, raw_value in zip(node["index"], data):
                        append(date_value, raw_value)
                    return
                date_value = (
                    node.get("announce_time")
                    or node.get("Date")
                    or node.get("date")
                    or node.get("tag_time")
                )
                if date_value is not None and ("Value" in node or "value" in node):
                    append(date_value, node.get("Value", node.get("value")))
                    return
                if node and all(
                    not isinstance(item, (dict, list)) for item in node.values()
                ):
                    for possible_date, raw_value in node.items():
                        if _professional_timestamp(possible_date) is not None:
                            append(possible_date, raw_value)
                    return
            for key, child in node.items():
                if key == field:
                    visit(child, True)
                elif key in {"batches", "Value", "value"} or field_scope:
                    visit(child, field_scope)
        elif isinstance(node, list):
            for child in node:
                visit(child, field_scope)

    visit(payload)
    return sorted(set(values), key=lambda item: item[0])


def _professional_timestamp(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    text = str(value).strip().split(".", 1)[0]
    try:
        if text.isdigit() and len(text) == 6:
            text = "20" + text
        return pd.Timestamp(text)
    except (TypeError, ValueError):
        return None


def _first_number(value: Any) -> float | None:
    if isinstance(value, (list, tuple)):
        for item in value:
            number = _first_number(item)
            if number is not None:
                return number
        return None
    if isinstance(value, dict):
        for key in ("Value", "value", "0", 0):
            if key in value:
                return _first_number(value[key])
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _component_number(value: Any, component: int) -> float | None:
    if isinstance(value, dict):
        for key in ("Value", "value"):
            if key in value:
                return _component_number(value[key], component)
    if isinstance(value, (list, tuple)) and value:
        if component < len(value):
            return _first_number(value[component])
    return _first_number(value)


def _latest_field(
    payload: Any,
    field: str,
    code: str,
    asof: pd.Timestamp,
    *,
    component: int = 0,
) -> float | None:
    eligible = [
        item
        for item in _field_values(payload, field, code, component=component)
        if item[0] <= asof
    ]
    return eligible[-1][1] if eligible else None


def _field_change(payload: Any, field: str, code: str, asof: pd.Timestamp) -> float | None:
    eligible = [item for item in _field_values(payload, field, code) if item[0] <= asof]
    if len(eligible) < 2:
        return None
    return eligible[-1][1] - eligible[-2][1]


def _field_ratio_change(
    payload: Any,
    field: str,
    code: str,
    asof: pd.Timestamp,
) -> float | None:
    eligible = [item for item in _field_values(payload, field, code) if item[0] <= asof]
    if len(eligible) < 2 or eligible[-2][1] == 0:
        return None
    return eligible[-1][1] / eligible[-2][1] - 1.0


def _financial_features(payload: Any, code: str, asof: pd.Timestamp) -> dict[str, Any]:
    forecast_low = _latest_field(payload, "FN285", code, asof)
    forecast_high = _latest_field(payload, "FN286", code, asof)
    forecast_mid = (
        (forecast_low + forecast_high) / 2
        if forecast_low is not None and forecast_high is not None
        else forecast_low if forecast_low is not None else forecast_high
    )
    prior_mid_change = _field_change(payload, "FN285", code, asof)
    return {
        "revenue_yoy": _latest_field(payload, "FN183", code, asof),
        "profit_yoy": _latest_field(payload, "FN184", code, asof),
        "gross_margin_change": _field_change(payload, "FN202", code, asof),
        "roe": _latest_field(payload, "FN197", code, asof),
        "ocf_profit_ratio": _latest_field(payload, "FN228", code, asof),
        "forecast_revision": prior_mid_change if prior_mid_change is not None else forecast_mid,
        "institution_holding_change_ratio": _field_ratio_change(
            payload, "FN247", code, asof
        ),
    }


def _flow_features(payload: Any, code: str, asof: pd.Timestamp) -> dict[str, Any]:
    shareholder = _field_values(payload, "GP01", code)
    shareholder = [item for item in shareholder if item[0] <= asof]
    northbound = _field_values(payload, "GP06", code)
    northbound = [item for item in northbound if item[0] <= asof]
    institution_buy = _latest_field(
        payload, "GP09", code, asof, component=1
    ) or 0.0
    institution_sell = _latest_field(
        payload, "GP08", code, asof, component=1
    ) or 0.0
    return {
        "northbound_change_ratio": (
            northbound[-1][1] / northbound[max(0, len(northbound) - 21)][1] - 1.0
            if len(northbound) >= 2 and northbound[max(0, len(northbound) - 21)][1] != 0
            else np.nan
        ),
        "institution_lhb_ratio": institution_buy - institution_sell,
        "shareholder_count_change": (
            shareholder[-1][1] / shareholder[-2][1] - 1.0
            if len(shareholder) >= 2 and shareholder[-2][1] != 0
            else np.nan
        ),
        "turnover_20": np.nan,
    }


def _historical_security_status(
    payload: Any, code: str, asof: pd.Timestamp
) -> dict[str, bool]:
    changes = [
        item
        for item in _field_values(payload, "GP29", code, component=1)
        if item[0] <= asof
    ]
    is_st = False
    for _, raw_status in changes:
        status = int(raw_status)
        if status in {2, 3}:
            is_st = True
        elif status == 4:
            is_st = False
    return {"is_st": is_st}


def _historical_quit_status(
    announcements: Iterable[Mapping[str, Any]], asof: pd.Timestamp
) -> bool:
    for item in announcements:
        available_at = pd.Timestamp(item.get("effective_at") or item.get("published_at"))
        if available_at <= asof + pd.Timedelta(hours=15):
            title = str(item.get("title") or "")
            if "退市整理" in title or "终止上市" in title:
                return True
    return False


def _consensus_features(payload: Any, code: str) -> dict[str, Any]:
    pe = _latest_field(payload, "GO23", code, pd.Timestamp.max)
    return {"valuation_raw": pe, "valuation_percentile": np.nan}


def _aggregate_events(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    events = []
    for item in items:
        classification = classify_announcement(str(item.get("title") or ""))
        events.append((classification, item))
    if not events:
        return {"score": 0.0, "event_type": "NONE", "evidence_refs": [], "latest_published_at": None}
    hard_negative = [item for item in events if item[0]["hard_negative"]]
    selected = min(hard_negative, key=lambda item: float(item[0]["score"])) if hard_negative else max(
        events, key=lambda item: float(item[0]["score"])
    )
    return {
        "score": float(selected[0]["score"]),
        "event_type": str(selected[0]["event_type"]),
        "evidence_refs": [
            f"cninfo:{item.get('raw_hash') or item.get('source_url') or ''}"
            for _, item in events
        ],
        "latest_published_at": max(str(item.get("published_at") or "") for _, item in events),
    }


def _attach_industry_cross_section(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    frame = pd.DataFrame(rows)
    valuation_raw = (
        frame["valuation_raw"]
        if "valuation_raw" in frame.columns
        else pd.Series(np.nan, index=frame.index, dtype=float)
    )
    frame["valuation_percentile"] = pd.to_numeric(
        valuation_raw, errors="coerce"
    ).rank(pct=True, ascending=True).fillna(0.50)
    grouped = frame.groupby("industry", dropna=False)
    industry_momentum = grouped.apply(
        lambda group: float(
            0.50 * pd.to_numeric(group["relative_return_20"], errors="coerce").mean()
            + 0.30 * pd.to_numeric(group["relative_return_60"], errors="coerce").mean()
            + 0.20 * pd.to_numeric(group["relative_return_120"], errors="coerce").mean()
        ),
        include_groups=False,
    )
    breadth = grouped["return_20"].apply(
        lambda values: float((pd.to_numeric(values, errors="coerce") > 0).mean())
    )
    amount_trend = grouped["amount_ratio"].apply(
        lambda values: float(pd.to_numeric(values, errors="coerce").mean() - 1.0)
    )
    for position, row in frame.iterrows():
        industry = row["industry"]
        rows[position]["industry_momentum"] = float(industry_momentum.loc[industry])
        rows[position]["industry_breadth"] = float(breadth.loc[industry])
        rows[position]["industry_amount_trend"] = float(amount_trend.loc[industry])
        rows[position]["valuation_percentile"] = float(row["valuation_percentile"])


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _shape(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(map(str, value))[:30], "length": len(value)}
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    return {"type": type(value).__name__}


def _assert_rpc_field_contract(
    payload: Any,
    expected_fields: Iterable[str],
    dataset: str,
) -> None:
    discovered: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                discovered.add(str(key))
                if str(key).lower() in {"columns", "field_list", "fields"} and isinstance(
                    nested, (list, tuple)
                ):
                    discovered.update(map(str, nested))
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value[:20]:
                visit(nested)

    visit(payload)
    missing = sorted(set(expected_fields) - discovered)
    if missing:
        raise ResearchDataBlockedError(
            f"TDX {dataset} field contract drift; missing fields: {missing}"
        )


def _decode_json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _decision_timestamps(values: Iterable[Any]) -> pd.Series:
    timestamps = pd.Series(pd.to_datetime(values, errors="coerce"))
    at_midnight = timestamps.notna() & timestamps.dt.time.eq(datetime.min.time())
    timestamps.loc[at_midnight] = timestamps.loc[at_midnight] + pd.Timedelta(hours=15)
    return timestamps


def _purge_tail_dates(frame: pd.DataFrame, count: int) -> pd.DataFrame:
    dates = pd.to_datetime(frame["asof"], errors="coerce")
    if dates.isna().all():
        return frame.iloc[0:0]
    boundary = dates.max() - pd.offsets.BDay(count)
    return frame.loc[dates <= boundary]


def _embargo_head_dates(frame: pd.DataFrame, count: int) -> pd.DataFrame:
    dates = pd.to_datetime(frame["asof"], errors="coerce")
    if dates.isna().all():
        return frame.iloc[0:0]
    boundary = dates.min() + pd.offsets.BDay(count)
    return frame.loc[dates > boundary]


def _select_with_industry_cap(
    frame: pd.DataFrame,
    score_column: str,
    *,
    maximum_candidates: int = 20,
    maximum_per_industry: int = 5,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    ranked = frame.sort_values([score_column, "code"], ascending=[False, True])
    positions: list[Any] = []
    counts: dict[str, int] = {}
    for index, row in ranked.iterrows():
        industry = str(row.get("industry") or "未分类")
        if counts.get(industry, 0) >= maximum_per_industry:
            continue
        counts[industry] = counts.get(industry, 0) + 1
        positions.append(index)
        if len(positions) >= maximum_candidates:
            break
    return ranked.loc[positions]


def _ranking_metrics(
    frame: pd.DataFrame,
    score_column: str,
    eligibility_column: str,
) -> dict[str, float]:
    from sklearn.metrics import average_precision_score

    eligible = frame.loc[
        frame["entry_executable"].astype(bool)
        & frame[eligibility_column].astype(bool)
    ]
    if eligible.empty or score_column not in eligible:
        return {"pr_auc": 0.0}
    target = pd.to_numeric(eligible["target"], errors="coerce").fillna(0).astype(int)
    score = pd.to_numeric(eligible[score_column], errors="coerce")
    valid = score.notna() & np.isfinite(score)
    if not valid.any() or target.loc[valid].nunique() < 2:
        return {"pr_auc": 0.0}
    return {
        "pr_auc": float(average_precision_score(target.loc[valid], score.loc[valid]))
    }


def _evaluate_non_overlapping_portfolio(
    frame: pd.DataFrame,
    *,
    score_column: str,
    eligibility_column: str,
    return_column: str = "forward_return_60",
    holding_trading_days: int = VALIDATION_HOLDING_TRADING_DAYS,
) -> tuple[dict[str, float], list[tuple[str, str, float]], list[float]]:
    cycle_returns: list[float] = []
    weekly_precision: list[float] = []
    cycle_turnover: list[float] = []
    selected_returns: list[tuple[str, str, float]] = []
    ic_values: list[float] = []
    previous: set[str] = set()
    next_available_at: pd.Timestamp | None = None
    for asof, group in frame.groupby("asof", sort=True):
        pool = group.loc[
            group["entry_executable"].fillna(False).astype(bool)
            & group[eligibility_column].fillna(False).astype(bool)
        ].copy()
        eligible = _select_with_industry_cap(pool, score_column)
        if eligible.empty:
            continue
        weekly_precision.append(float(eligible["target"].mean()))
        if (
            len(pool) >= 3
            and pd.to_numeric(pool[score_column], errors="coerce").nunique() > 1
            and pd.to_numeric(pool[return_column], errors="coerce").nunique() > 1
        ):
            correlation = pool[score_column].corr(
                pool[return_column], method="spearman"
            )
            if pd.notna(correlation):
                ic_values.append(float(correlation))
        decision_at = pd.Timestamp(asof)
        if next_available_at is not None and decision_at < next_available_at:
            continue
        returns = pd.to_numeric(eligible[return_column], errors="coerce")
        valid = eligible.loc[returns.notna()].copy()
        if valid.empty:
            continue
        returns = pd.to_numeric(valid[return_column], errors="coerce")
        current = set(valid["code"].astype(str))
        cycle_turnover.append(1.0 - len(previous & current) / max(1, len(current)))
        previous = current
        cycle_returns.append(float(returns.mean()))
        selected_returns.extend(
            (str(asof), str(row["code"]), float(row[return_column]))
            for _, row in valid.iterrows()
        )
        exits = (
            pd.to_datetime(valid["exit_time"], errors="coerce")
            if "exit_time" in valid
            else pd.Series(pd.NaT, index=valid.index, dtype="datetime64[ns]")
        )
        if bool(exits.notna().any()):
            next_available_at = pd.Timestamp(exits.max()).normalize()
        else:
            next_available_at = decision_at + pd.offsets.BDay(
                holding_trading_days
            )
    metrics = _portfolio_metrics(
        cycle_returns,
        weekly_precision,
        cycle_turnover,
        periods_per_year=252.0 / max(1, holding_trading_days),
    )
    metrics["weekly_rank_periods"] = int(len(weekly_precision))
    metrics["return_policy"] = "NON_OVERLAPPING_CAPITAL_CYCLES"
    return metrics, selected_returns, ic_values


def _top_five_attribution(
    selected: Iterable[tuple[str, str, float]],
) -> dict[str, Any]:
    rows = list(selected)
    if not rows:
        return {
            "top_five_contributors": [],
            "without_top_five_total_return": 0.0,
            "top_five_return_impact": 0.0,
        }
    ranked_positions = sorted(
        range(len(rows)), key=lambda position: rows[position][2], reverse=True
    )[:5]
    excluded = set(ranked_positions)
    by_date: dict[str, list[float]] = {}
    for position, (asof, _, value) in enumerate(rows):
        if position in excluded:
            continue
        by_date.setdefault(asof, []).append(value)
    without_periods = [float(np.mean(values)) for _, values in sorted(by_date.items()) if values]
    full_by_date: dict[str, list[float]] = {}
    for asof, _, value in rows:
        full_by_date.setdefault(asof, []).append(value)
    full_periods = [float(np.mean(values)) for _, values in sorted(full_by_date.items()) if values]
    full_return = _portfolio_metrics(
        full_periods,
        [],
        [0.0] * len(full_periods),
        periods_per_year=VALIDATION_PERIODS_PER_YEAR,
    )["total_return"]
    without_return = _portfolio_metrics(
        without_periods,
        [],
        [0.0] * len(without_periods),
        periods_per_year=VALIDATION_PERIODS_PER_YEAR,
    )["total_return"]
    return {
        "top_five_contributors": [
            {"asof": rows[position][0], "code": rows[position][1], "return": rows[position][2]}
            for position in ranked_positions
        ],
        "without_top_five_total_return": without_return,
        "top_five_return_impact": full_return - without_return,
    }


def _classification_metrics(target: Iterable[Any], probability: Iterable[float]) -> dict[str, float]:
    from sklearn.metrics import average_precision_score

    y = np.asarray(list(target), dtype=int)
    p = np.asarray(list(probability), dtype=float)
    if len(y) == 0:
        return {"pr_auc": 0.0, "precision_at_20": 0.0}
    order = np.argsort(-p)[:20]
    return {
        "pr_auc": float(average_precision_score(y, p)) if len(np.unique(y)) > 1 else 0.0,
        "precision_at_20": float(y[order].mean()) if len(order) else 0.0,
    }


def _portfolio_metrics(
    period_returns: Iterable[float],
    precision: Iterable[float],
    turnover: Iterable[float],
    *,
    periods_per_year: float = 52.0,
) -> dict[str, float]:
    returns = np.asarray(list(period_returns), dtype=float)
    precisions = np.asarray(list(precision), dtype=float)
    turnovers = np.asarray(list(turnover), dtype=float)
    if not len(returns):
        return {
            "periods": 0,
            "precision_at_20": 0.0,
            "total_return": 0.0,
            "double_cost_return": 0.0,
            "sharpe": 0.0,
            "calmar": 0.0,
            "annualized_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0,
        }
    cost = np.pad(turnovers, (0, max(0, len(returns) - len(turnovers))), constant_values=0)[: len(returns)] * 0.002
    net = returns - cost
    double_cost = returns - 2 * cost
    # Include the initial NAV so a loss in the first invested period is not
    # silently promoted to the equity curve's first high-water mark.
    curve = np.concatenate(([1.0], np.cumprod(1.0 + net)))
    peak = np.maximum.accumulate(curve)
    drawdown = curve / peak - 1.0
    total_return = float(np.prod(1.0 + net) - 1.0)
    annualized_return = (
        float((1.0 + total_return) ** (periods_per_year / len(net)) - 1.0)
        if 1.0 + total_return > 0
        else -1.0
    )
    maximum_drawdown = float(drawdown.min())
    return {
        "periods": int(len(returns)),
        "precision_at_20": float(precisions.mean()) if len(precisions) else 0.0,
        "total_return": total_return,
        "double_cost_return": float(np.prod(1.0 + double_cost) - 1.0),
        "sharpe": float(net.mean() / net.std(ddof=1) * np.sqrt(periods_per_year)) if len(net) > 1 and net.std(ddof=1) > 0 else 0.0,
        "calmar": float(annualized_return / abs(maximum_drawdown)) if maximum_drawdown < 0 else 0.0,
        "annualized_return": annualized_return,
        "max_drawdown": maximum_drawdown,
        "turnover": float(turnovers.mean()) if len(turnovers) else 0.0,
    }


__all__ = [
    "CninfoDirectProvider",
    "EarlyWinnerResearchService",
    "MODEL_FEATURE_COLUMNS",
    "MODEL_PARAMETERS",
    "ProviderGate",
    "ResearchDataBlockedError",
    "TdxResearchHttpClient",
]


def _normalize_calendar_date(value: Any) -> str:
    """Normalize TDX calendar values without guessing an ambiguous timezone."""
    if value is None:
        return ""
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    text = str(value).strip()
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) >= 8:
        try:
            return pd.Timestamp(digits[:8]).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return ""
    try:
        return pd.Timestamp(text).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _weekly_decision_dates(
    trading_calendar: Iterable[Any],
    start_year: int,
    end_year: int,
) -> list[pd.Timestamp]:
    dates = pd.DatetimeIndex(
        sorted(
            {
                normalized
                for item in trading_calendar
                if (normalized := _normalize_calendar_date(item))
            }
        )
    )
    dates = dates[(dates.year >= start_year) & (dates.year <= end_year)]
    if dates.empty:
        return []
    frame = pd.DataFrame({"date": dates})
    iso = frame["date"].dt.isocalendar()
    frame["iso_year"] = iso["year"]
    frame["iso_week"] = iso["week"]
    return [
        pd.Timestamp(value)
        for value in frame.groupby(["iso_year", "iso_week"], sort=True)["date"].max().tolist()
    ]


def _align_weekly_decision_rows(
    rows: Iterable[Mapping[str, Any]],
    decision_at: Any,
) -> list[dict[str, Any]]:
    decision = pd.Timestamp(decision_at).normalize()
    output: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        last_bar = pd.to_datetime(item.get("asof"), errors="coerce")
        if pd.isna(last_bar):
            item["suspended"] = True
            item["last_bar_at"] = ""
        else:
            item["last_bar_at"] = last_bar.date().isoformat()
            item["suspended"] = bool(item.get("suspended")) or (
                last_bar.normalize() < decision
            )
        item["asof"] = decision.date().isoformat()
        output.append(item)
    return output


def _history_build_id(
    start_year: int,
    end_year: int,
    calendar_hash: str,
    universe_hash: str,
) -> str:
    payload = {
        "project_id": PROJECT_ID,
        "builder_version": HISTORY_BUILDER_VERSION,
        "start_year": int(start_year),
        "end_year": int(end_year),
        "calendar_hash": calendar_hash,
        "universe_hash": universe_hash,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"history-{digest[:24]}"


def _select_validation_champion(
    *,
    gates: Mapping[str, Mapping[str, Any]],
    model_row: Mapping[str, Any],
    snapshot_id: str,
    validation_id: str,
    selected_at: str,
    rule_artifact_hash: str,
) -> dict[str, Any]:
    if bool(gates.get("rule", {}).get("passed")):
        return {
            "method": "rule",
            "strategy_id": RULE_STRATEGY_ID,
            "artifact_hash": rule_artifact_hash,
            "feature_schema_hash": str(model_row.get("feature_schema_hash") or ""),
            "model_id": "",
            "model_artifact_hash": "",
            "snapshot_id": snapshot_id,
            "validation_id": validation_id,
            "selected_at": selected_at,
            "selection_policy": "rule_preferred_when_both_pass",
        }
    if bool(gates.get("ml", {}).get("passed")):
        artifact_hash = str(model_row.get("artifact_hash") or "")
        return {
            "method": "ml",
            "strategy_id": ML_STRATEGY_ID,
            "artifact_hash": artifact_hash,
            "feature_schema_hash": str(model_row.get("feature_schema_hash") or ""),
            "model_id": str(model_row.get("model_id") or ""),
            "model_artifact_hash": artifact_hash,
            "snapshot_id": snapshot_id,
            "validation_id": validation_id,
            "selected_at": selected_at,
            "selection_policy": "only_passing_method",
        }
    return {}
