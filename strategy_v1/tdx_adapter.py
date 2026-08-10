from __future__ import annotations

import json
import sys
import threading
from contextlib import AbstractContextManager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .config import StrategyConfig
from .models import Signal


_TQ_CONNECTION_LOCK = threading.RLock()


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _code_name(item: Any) -> tuple[str, str]:
    if isinstance(item, str):
        return item, item
    if isinstance(item, dict):
        code = str(item.get("Code") or item.get("code") or "")
        name = str(item.get("Name") or item.get("name") or code)
        return code, name
    return "", ""


def _bound_frame_to_window(
    frame: pd.DataFrame,
    *,
    start_time: str | None,
    end_time: str | None,
    warmup_bars: int,
) -> pd.DataFrame:
    if frame.empty or (not start_time and not end_time):
        return frame
    bounded = frame
    if end_time:
        end = pd.Timestamp(end_time)
        bounded = bounded[bounded.index.normalize() <= end.normalize()]
    if bounded.empty or not start_time:
        return bounded
    start = pd.Timestamp(start_time).normalize()
    in_window = bounded.index.normalize() >= start
    if not in_window.any():
        return bounded.iloc[0:0]
    first = int(in_window.argmax())
    return bounded.iloc[max(0, first - max(0, int(warmup_bars))) :]


def _convert_bar_code(
    arguments: tuple[
        str,
        dict[str, Any],
        tuple[str, ...],
        str | None,
        str | None,
        int,
    ],
) -> tuple[str, pd.DataFrame | None]:
    code, available, fields, start_time, end_time, warmup_bars = arguments
    columns: dict[str, pd.Series] = {}
    for field in fields:
        frame = available.get(field.lower())
        if isinstance(frame, pd.DataFrame) and code in frame.columns:
            columns[field] = frame[code]
    if not columns:
        return code, None
    combined = pd.DataFrame(columns)
    combined.index = pd.to_datetime(combined.index)
    combined = _bound_frame_to_window(
        combined.sort_index(),
        start_time=start_time,
        end_time=end_time,
        warmup_bars=warmup_bars,
    )
    combined = combined[~combined.index.duplicated(keep="last")]
    required = [column for column in ("Close",) if column in combined.columns]
    if required:
        combined = combined.dropna(subset=required)
    return code, combined if not combined.empty else None


def _tq_time(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if len(text) in (8, 14) and text.isdigit():
        return text
    timestamp = pd.Timestamp(text)
    if timestamp == timestamp.normalize():
        return timestamp.strftime("%Y%m%d")
    return timestamp.strftime("%Y%m%d%H%M%S")


class TdxAdapter(AbstractContextManager["TdxAdapter"]):
    def __init__(
        self,
        config: StrategyConfig,
        strategy_path: str | Path,
        *,
        transform_workers: int = 1,
        minimum_batch_size: int = 100,
    ):
        self.config = config
        self.strategy_path = str(Path(strategy_path).resolve())
        self.transform_workers = max(1, int(transform_workers))
        self.minimum_batch_size = max(1, int(minimum_batch_size))
        self.successful_batch_sizes: list[int] = []
        self._tq = None
        self._connection_lock_acquired = False

    def __enter__(self) -> "TdxAdapter":
        if sys.platform != "win32":
            raise RuntimeError("The TDX adapter is only supported on Windows")
        tqcenter = self.config.tq_user_dir / "tqcenter.py"
        if not tqcenter.exists():
            raise FileNotFoundError(f"tqcenter.py not found: {tqcenter}")
        _TQ_CONNECTION_LOCK.acquire()
        self._connection_lock_acquired = True
        try:
            sys.path.insert(0, str(self.config.tq_user_dir))
            from tqcenter import tq

            tq.initialize(self.strategy_path)
            self._tq = tq
        except Exception:
            self._release_connection_lock()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if self._tq is not None:
                self._tq.close()
            self._tq = None
        finally:
            self._release_connection_lock()

    def _release_connection_lock(self) -> None:
        if self._connection_lock_acquired:
            self._connection_lock_acquired = False
            _TQ_CONNECTION_LOCK.release()

    @property
    def tq(self):
        if self._tq is None:
            raise RuntimeError("TDX connection is not initialized")
        return self._tq

    def list_a_shares(self) -> tuple[list[str], dict[str, str]]:
        items = self.tq.get_stock_list("5", list_type=1)
        names: dict[str, str] = {}
        for item in items:
            code, name = _code_name(item)
            if code.endswith((".SH", ".SZ", ".BJ")):
                names[code] = name
        return sorted(names), names

    def list_sectors(self) -> list[dict[str, str]]:
        sectors: dict[str, str] = {}
        for market in self.config.sector_markets:
            for item in self.tq.get_stock_list(market, list_type=1):
                code, name = _code_name(item)
                if code:
                    sectors[code] = name
        return [{"code": code, "name": name} for code, name in sorted(sectors.items())]

    def load_sector_members(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        self.config.ensure_runtime_dirs()
        cache_file = self.config.cache_dir / "sector_members.json"
        if cache_file.exists() and not refresh:
            return json.loads(cache_file.read_text(encoding="utf-8"))

        result: dict[str, dict[str, Any]] = {}
        for sector in self.list_sectors():
            raw_members = self.tq.get_stock_list_in_sector(sector["code"], list_type=1)
            members = []
            for item in raw_members:
                code, _ = _code_name(item)
                if code.endswith((".SH", ".SZ", ".BJ")):
                    members.append(code)
            if members:
                result[sector["code"]] = {
                    "name": sector["name"],
                    "members": sorted(set(members)),
                }
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def fetch_bars(
        self,
        codes: list[str],
        period: str,
        count: int,
        fields: tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume", "Amount"),
        dividend_type: str = "front",
        start_time: str | None = None,
        end_time: str | None = None,
        warmup_bars: int = 0,
    ) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        for requested in _chunks(codes, self.config.batch_size):
            for batch, raw in self._fetch_market_batches(
                requested,
                period=period,
                count=count,
                fields=fields,
                dividend_type=dividend_type,
                end_time=end_time,
            ):
                available = {str(field).lower(): frame for field, frame in raw.items()}
                arguments = [
                    (code, available, fields, start_time, end_time, warmup_bars)
                    for code in batch
                ]
                if self.transform_workers > 1 and len(arguments) > 1:
                    with ThreadPoolExecutor(
                        max_workers=min(self.transform_workers, len(arguments)),
                        thread_name_prefix="tdx-bars",
                    ) as executor:
                        converted = executor.map(_convert_bar_code, arguments)
                        for code, frame in converted:
                            if frame is not None:
                                result[code] = frame
                else:
                    for arguments_item in arguments:
                        code, frame = _convert_bar_code(arguments_item)
                        if frame is not None:
                            result[code] = frame
        return {code: result[code] for code in codes if code in result}

    def _fetch_market_batches(
        self,
        codes: list[str],
        *,
        period: str,
        count: int,
        fields: tuple[str, ...],
        dividend_type: str,
        end_time: str | None,
    ) -> Iterable[tuple[list[str], dict[str, Any]]]:
        pending = [codes]
        while pending:
            batch = pending.pop(0)
            raw = self.tq.get_market_data(
                field_list=list(fields),
                stock_list=batch,
                period=period,
                end_time=_tq_time(end_time),
                count=count,
                dividend_type=dividend_type,
                fill_data=False,
            )
            usable = isinstance(raw, dict) and any(
                isinstance(frame, pd.DataFrame) for frame in raw.values()
            )
            if usable:
                self.successful_batch_sizes.append(len(batch))
                yield batch, raw
                continue
            if len(batch) <= self.minimum_batch_size:
                continue
            midpoint = max(self.minimum_batch_size, len(batch) // 2)
            pending[0:0] = [batch[:midpoint], batch[midpoint:]]


    def refresh_intraday(self, codes: list[str]) -> None:
        for batch in _chunks(codes, min(self.config.batch_size, 50)):
            self.tq.refresh_kline(stock_list=batch, period="5m")

    def ensure_candidate_block(self) -> None:
        existing = self.tq.get_user_sector()
        codes = {_code_name(item)[0] for item in existing}
        if self.config.candidate_block not in codes and f"BKCODE.{self.config.candidate_block}" not in codes:
            self.tq.create_sector(self.config.candidate_block, "V1候选股")

    def push_candidates(self, codes: list[str]) -> dict[str, Any]:
        self.ensure_candidate_block()
        return self.tq.send_user_block(
            block_code=self.config.candidate_block,
            stock_list=sorted(set(codes)),
            show=True,
        )

    def push_warnings(self, signals: list[Signal]) -> dict[str, Any]:
        if not signals:
            return {}
        return self.tq.send_warn(
            stock_list=[signal.code for signal in signals],
            time_list=[signal.timestamp.strftime("%Y%m%d%H%M%S") for signal in signals],
            price_list=[f"{signal.price:.3f}" for signal in signals],
            close_list=[f"{signal.price:.3f}" for signal in signals],
            volum_list=["0" for _ in signals],
            bs_flag_list=["0" if signal.side == "BUY" else "1" for signal in signals],
            warn_type_list=["0" for _ in signals],
            reason_list=[signal.reason[:25] for signal in signals],
            count=len(signals),
        )

    def push_signal_data(self, signals: list[Signal]) -> None:
        for signal in signals:
            values = [
                "1" if signal.side == "BUY" else "-1",
                f"{signal.price:.3f}",
                f"{signal.center_lower or 0:.3f}",
                f"{signal.center_upper or 0:.3f}",
                str(signal.leader_rank),
            ]
            self.tq.send_bt_data(
                stock_code=signal.code,
                time_list=[signal.timestamp.strftime("%Y%m%d%H%M%S")],
                data_list=[values],
                count=1,
            )
