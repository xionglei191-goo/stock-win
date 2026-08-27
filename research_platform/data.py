from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import pandas as pd

from strategy_v1.config import StrategyConfig
from strategy_v1.tdx_adapter import TdxAdapter

from .config import PlatformConfig
from .data_cache import canonical_hash, shared_memory_cache
from .course49_market import MARKET_ACTIVITY_FIELDS
from .lhb import COURSE49_FIELDS, LHB_FIELDS
from .models import DataHealth, DataStatus
from .registry import SourceRegistry
from .us_tdx import TQRawRPCEnvelope, TQReadOnlyClient


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


class TdxProvider(AbstractContextManager["TdxProvider"]):
    def __init__(
        self,
        config: PlatformConfig,
        caller_path: str | Path,
        *,
        cache_reads: bool = True,
    ):
        legacy = StrategyConfig(
            tdx_root=config.tdx_root,
            batch_size=config.performance.bar_batch_size,
        )
        self.config = config
        self.adapter = TdxAdapter(
            legacy,
            caller_path,
            transform_workers=config.performance.worker_threads,
            minimum_batch_size=config.performance.minimum_batch_size,
        )
        self.successful_event_batch_sizes: list[int] = []
        self.memory = shared_memory_cache(config)
        self.cache_reads = cache_reads
        self.memory_hits = 0

    def __enter__(self) -> "TdxProvider":
        self.adapter.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.adapter.__exit__(exc_type, exc_value, traceback)

    def list_a_shares(self) -> tuple[list[str], dict[str, str]]:
        key = self._memory_key("a_share_master", {"asof": date.today().isoformat()})
        cached = self._memory_get(key)
        if isinstance(cached, tuple):
            return cached
        result = self.adapter.list_a_shares()
        self.memory.put(key, result)
        return result

    def list_us_stocks(self) -> tuple[list[str], dict[str, str]]:
        key = self._memory_key("us_stock_master", {"asof": date.today().isoformat()})
        cached = self._memory_get(key)
        if isinstance(cached, tuple):
            return cached
        result = self.adapter.list_us_stocks()
        self.memory.put(key, result)
        return result

    def fetch_bars(
        self,
        codes: list[str],
        period: str,
        count: int,
        *,
        fields: tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume", "Amount"),
        dividend_type: str = "front",
        start_time: str | None = None,
        end_time: str | None = None,
        warmup_bars: int = 0,
        batch_callback: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, pd.DataFrame]:
        cache_key = self._memory_key(
            "bars",
            {
                "codes": codes,
                "period": period,
                "count": count,
                "fields": fields,
                "dividend_type": dividend_type,
                "start_time": start_time,
                "end_time": end_time,
                "warmup_bars": warmup_bars,
                "latest_asof": "" if end_time else date.today().isoformat(),
            },
        )
        cached = self._memory_get(cache_key)
        if isinstance(cached, dict):
            if batch_callback is not None:
                batch_callback(len(codes), len(codes), len(codes))
            return cached
        bars = self.adapter.fetch_bars(
            codes,
            period,
            count,
            fields=fields,
            dividend_type=dividend_type,
            start_time=start_time,
            end_time=end_time,
            warmup_bars=warmup_bars,
            batch_callback=batch_callback,
        )
        for code, frame in bars.items():
            is_us = str(code).upper().endswith(".US")
            if "Amount" in frame.columns:
                if is_us:
                    # TDX's US Amount scale is not documented by the local
                    # adapter. Preserve the raw value and make the uncertainty
                    # explicit; strategy liquidity uses Close * Volume instead.
                    frame["Amount"] = pd.to_numeric(frame["Amount"], errors="coerce")
                    frame.attrs["amount_unit"] = "TDX_RAW_UNKNOWN"
                else:
                    frame["Amount"] = (
                        pd.to_numeric(frame["Amount"], errors="coerce") * 10_000.0
                    )
                    frame.attrs["amount_unit"] = "CNY"
            frame.attrs["volume_unit"] = "share"
            frame.attrs["timezone"] = (
                "America/New_York" if is_us else self.config.timezone
            )
            frame.attrs["source"] = "tdx"
            frame.attrs["adjustment"] = dividend_type
        self.memory.put(cache_key, bars)
        return bars

    def fetch_bars_evidence(
        self,
        codes: list[str],
        period: str,
        count: int,
        *,
        fields: tuple[str, ...],
        dividend_type: str,
        start_time: str | None = None,
        end_time: str | None = None,
        warmup_bars: int = 0,
    ) -> tuple[dict[str, pd.DataFrame], tuple[TQRawRPCEnvelope, ...]]:
        """Fetch US bars directly from read-only JSON-RPC and preserve bytes.

        This production evidence path deliberately bypasses TdxAdapter's
        duplicate removal and Close-null filtering. Normalization belongs to
        the PIT market preparer after the response has entered CAS.
        """

        if warmup_bars != 0:
            raise ValueError("raw TQ evidence fetch does not accept warmup_bars")
        normalized = [str(code).strip().upper() for code in codes]
        if not normalized or any(not code.endswith(".US") for code in normalized):
            raise ValueError("raw TQ evidence fetch only accepts non-empty .US codes")
        client = TQReadOnlyClient(timeout_seconds=120.0)
        envelopes: list[TQRawRPCEnvelope] = []
        result: dict[str, pd.DataFrame] = {}
        # TDX batch RPC empirically returns usable rows for only the first
        # ~12 codes per request; cap the batch at 10 (verified all-ok) so no
        # valid vendor series is silently dropped.
        configured = int(self.config.performance.bar_batch_size or 10)
        batch_size = max(1, min(10, configured))
        for batch in _chunks(normalized, batch_size):
            envelope = client.call_raw(
                "get_market_data",
                {
                    "field_list": list(fields),
                    "stock_list": batch,
                    "period": period,
                    "end_time": "" if end_time is None else str(end_time).replace("-", ""),
                    "count": int(count),
                    "dividend_type": dividend_type,
                    "fill_data": False,
                },
            )
            envelopes.append(envelope)
            payload = envelope.value
            if not isinstance(payload, dict):
                continue
            for code in batch:
                raw = payload.get(code)
                if not isinstance(raw, dict):
                    continue
                lengths = [len(value) for value in raw.values() if isinstance(value, list)]
                if not lengths:
                    continue
                rows = max(lengths)
                columns: dict[str, list[Any]] = {}
                for key, value in raw.items():
                    if isinstance(value, list):
                        columns[str(key)] = list(value) + [None] * (rows - len(value))
                frame = pd.DataFrame(columns)
                date_values = frame.get("Date")
                if date_values is None:
                    frame.index = pd.RangeIndex(len(frame))
                else:
                    frame.index = pd.to_datetime(date_values.astype(str), errors="coerce")
                if start_time:
                    lower = pd.Timestamp(start_time).normalize()
                    frame = frame.loc[pd.DatetimeIndex(frame.index) >= lower]
                result[code] = frame
        return result, tuple(envelopes)

    def load_sectors(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        key = self._memory_key(
            "sectors",
            {"asof": date.today().isoformat()},
        )
        if not refresh:
            cached = self._memory_get(key)
            if isinstance(cached, dict):
                return cached
        result = self.adapter.load_sector_members(refresh=refresh)
        self.memory.put(key, result)
        return result

    def fetch_limit_snapshot(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        key = self._memory_key(
            "limit_snapshot",
            {"codes": codes, "asof": date.today().isoformat()},
        )
        cached = self._memory_get(key)
        if isinstance(cached, dict):
            return cached
        result: dict[str, dict[str, Any]] = {}
        for batch in _chunks(codes, self.config.performance.event_batch_size):
            raw = self.adapter.tq.get_zdt_data(stock_list=batch)
            if isinstance(raw, dict):
                result.update({str(code): value for code, value in raw.items() if isinstance(value, dict)})
        self.memory.put(key, result)
        return result

    def fetch_lhb_history(
        self,
        codes: list[str],
        start_time: str,
        end_time: str,
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        result: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for raw in self._iter_professional_history(codes, LHB_FIELDS, start_time, end_time):
            result.update(raw)
        return result

    def fetch_course49_history(
        self,
        codes: list[str],
        start_time: str,
        end_time: str,
        batch_callback: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        key = self._memory_key(
            "course49_events",
            {"codes": codes, "start_time": start_time, "end_time": end_time},
        )
        cached = self._memory_get(key)
        if isinstance(cached, dict):
            if batch_callback is not None:
                batch_callback(len(codes), len(codes), len(codes))
            return cached
        result: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for raw in self._iter_professional_history(
            codes,
            COURSE49_FIELDS,
            start_time,
            end_time,
            batch_callback=batch_callback,
        ):
            result.update(raw)
        self.memory.put(key, result)
        return result

    def iter_course49_history(
        self,
        codes: list[str],
        start_time: str,
        end_time: str,
        batch_callback: Callable[[int, int, int], None] | None = None,
    ) -> Iterator[dict[str, dict[str, list[dict[str, Any]]]]]:
        yield from self._iter_professional_history(
            codes,
            COURSE49_FIELDS,
            start_time,
            end_time,
            batch_callback=batch_callback,
        )

    def _iter_professional_history(
        self,
        codes: list[str],
        fields: Iterable[str],
        start_time: str,
        end_time: str,
        batch_callback: Callable[[int, int, int], None] | None = None,
    ) -> Iterator[dict[str, dict[str, list[dict[str, Any]]]]]:
        pending = list(_chunks(codes, self.config.performance.event_batch_size))
        completed = 0
        while pending:
            batch = pending.pop(0)
            raw = self.adapter.tq.get_gpjy_value(
                stock_list=batch,
                field_list=list(fields),
                start_time=start_time,
                end_time=end_time,
            )
            usable = isinstance(raw, dict) and any(
                isinstance(value, dict) for value in raw.values()
            )
            if usable:
                self.successful_event_batch_sizes.append(len(batch))
                completed += len(batch)
                if batch_callback is not None:
                    batch_callback(completed, len(codes), len(batch))
                yield {
                    str(code): value
                    for code, value in raw.items()
                    if isinstance(value, dict)
                }
                continue
            if len(batch) <= self.config.performance.minimum_batch_size:
                completed += len(batch)
                if batch_callback is not None:
                    batch_callback(completed, len(codes), len(batch))
                continue
            midpoint = max(
                self.config.performance.minimum_batch_size,
                len(batch) // 2,
            )
            pending[0:0] = [batch[:midpoint], batch[midpoint:]]

    def effective_batch_sizes(self) -> dict[str, int]:
        bar_sizes = self.adapter.successful_batch_sizes
        event_sizes = self.successful_event_batch_sizes
        return {
            "bar_configured": self.config.performance.bar_batch_size,
            "bar_min_success": min(bar_sizes) if bar_sizes else 0,
            "bar_max_success": max(bar_sizes) if bar_sizes else 0,
            "bar_batches": len(bar_sizes),
            "event_configured": self.config.performance.event_batch_size,
            "event_min_success": min(event_sizes) if event_sizes else 0,
            "event_max_success": max(event_sizes) if event_sizes else 0,
            "event_batches": len(event_sizes),
            "memory_hits": self.memory_hits,
        }

    def fetch_market_activity(
        self,
        start_time: str,
        end_time: str,
    ) -> dict[str, list[dict[str, Any]]]:
        key = self._memory_key(
            "market_activity",
            {"start_time": start_time, "end_time": end_time},
        )
        cached = self._memory_get(key)
        if isinstance(cached, dict):
            return cached
        raw = self.adapter.tq.get_scjy_value(
            field_list=list(MARKET_ACTIVITY_FIELDS),
            start_time=start_time,
            end_time=end_time,
        )
        if not isinstance(raw, dict):
            return {}
        result = {
            str(field): rows
            for field, rows in raw.items()
            if isinstance(rows, list)
        }
        self.memory.put(key, result)
        return result

    def _memory_key(self, dataset: str, query: dict[str, Any]) -> str:
        return "tdx:" + canonical_hash(
            {
                "dataset": dataset,
                "provider": "tdx",
                "query": query,
            }
        )

    def _memory_get(self, key: str) -> Any | None:
        if not self.cache_reads:
            return None
        value = self.memory.get(key)
        if value is not None:
            self.memory_hits += 1
        return value

    def push_candidates(self, block_code: str, block_name: str, codes: list[str], show: bool = False) -> Any:
        existing = self.adapter.tq.get_user_sector()
        existing_codes = {
            str(item.get("Code", "")) if isinstance(item, dict) else str(item) for item in existing
        }
        if block_code not in existing_codes and f"BKCODE.{block_code}" not in existing_codes:
            self.adapter.tq.create_sector(block_code, block_name)
        return self.adapter.tq.send_user_block(
            block_code=block_code,
            stock_list=sorted(set(codes)),
            show=show,
        )

    def push_warning(self, signal: dict[str, Any], approved: bool = False) -> Any:
        side = signal["side"]
        flag = "0" if side == "BUY" and approved else "1" if side == "SELL" else "2"
        evidence = signal.get("evidence") or {}
        if isinstance(evidence, str):
            import json
            evidence = json.loads(evidence)
        price = float(evidence.get("price", 0.0) or 0.0)
        return self.adapter.tq.send_warn(
            stock_list=[signal["code"]],
            time_list=[pd.Timestamp(signal["generated_at"]).strftime("%Y%m%d%H%M%S")],
            price_list=[f"{price:.3f}"],
            close_list=[f"{price:.3f}"],
            volum_list=["0"],
            bs_flag_list=[flag],
            warn_type_list=["0"],
            reason_list=[str(signal.get("reason_codes", "strategy signal"))[:25]],
            count=1,
        )

    def push_signal_values(self, signal: dict[str, Any]) -> Any:
        import json

        evidence = signal.get("evidence") or {}
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        strategy_number = "1" if signal["strategy_id"] == "chan_v1" else "2"
        values = [
            strategy_number,
            "1" if signal["side"] == "BUY" else "-1",
            f"{float(signal['strength']):.4f}",
            f"{float(evidence.get('center_lower', 0.0) or 0.0):.4f}",
            f"{float(evidence.get('center_upper', 0.0) or 0.0):.4f}",
            f"{float(signal.get('stop_price') or 0.0):.4f}",
        ]
        return self.adapter.tq.send_bt_data(
            stock_code=signal["code"],
            time_list=[pd.Timestamp(signal["generated_at"]).strftime("%Y%m%d%H%M%S")],
            data_list=[values],
            count=1,
        )


class ResearchDataHub:
    def __init__(self, config: PlatformConfig):
        self.config = config
        self.sources = SourceRegistry()

    def assess_daily(
        self,
        bars: dict[str, pd.DataFrame],
        expected_index: pd.DataFrame | None,
        dataset: str = "daily_bars",
        expected_symbol_count: int | None = None,
    ) -> DataHealth:
        if not bars or expected_index is None or expected_index.empty:
            return DataHealth(dataset, DataStatus.FAILED, None, None, 0, "No data returned")
        expected = pd.Timestamp(expected_index.index[-1]).normalize()
        latest_values = [pd.Timestamp(frame.index[-1]).normalize() for frame in bars.values() if not frame.empty]
        if not latest_values:
            return DataHealth(dataset, DataStatus.FAILED, None, expected.isoformat(), 0, "No valid rows")
        latest = max(latest_values)
        denominator = max(len(latest_values), int(expected_symbol_count or 0))
        coverage = sum(value == expected for value in latest_values) / denominator
        status = DataStatus.READY if latest >= expected and coverage >= 0.90 else DataStatus.PARTIAL
        message = "" if status == DataStatus.READY else f"Only {coverage:.1%} of symbols reach expected date"
        return DataHealth(dataset, status, latest.isoformat(), expected.isoformat(), len(latest_values), message)

    def assess_intraday(
        self,
        bars: dict[str, pd.DataFrame],
        expected_day: pd.Timestamp,
        dataset: str = "intraday_30m",
    ) -> DataHealth:
        latest_values = [pd.Timestamp(frame.index[-1]) for frame in bars.values() if not frame.empty]
        if not latest_values:
            return DataHealth(dataset, DataStatus.FAILED, None, expected_day.isoformat(), 0, "No 30-minute data")
        latest = max(latest_values)
        status = DataStatus.READY if latest.normalize() >= expected_day.normalize() else DataStatus.STALE
        message = "" if status == DataStatus.READY else "Latest 30-minute bar predates the latest completed trading day"
        return DataHealth(dataset, status, latest.isoformat(), expected_day.isoformat(), len(latest_values), message)

    @staticmethod
    def now() -> datetime:
        return datetime.now().astimezone()
