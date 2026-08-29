from __future__ import annotations

import base64
import json
import math
import re
from collections import Counter
import os
import shutil
import stat
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from .evidence_time import source_available_at
from .hashing import canonical_json_bytes, sha256_bytes, sha256_file, sha256_json
from .models import LicenseClass, SourceDependency, SourceRole, UNIVERSE_ID
from .quality import REQUIRED_ARTIFACT_COLUMNS
from .sources_fees import (
    FEE_EVIDENCE_CONTRACT_VERSION,
    FINRA_TAF_URL,
    SEC_FEE_URL,
    fee_rate_entries,
)
from .store import SourceBatch, USPITStore


MARKET_ARTIFACTS = (
    "bars_raw",
    "bars_vendor_front",
    "bars_pit_signal",
    "benchmarks",
    "xnys_calendar",
    "execution_fee_schedule",
    "bar_coverage",
)
REQUIRED_ARTIFACTS = tuple(REQUIRED_ARTIFACT_COLUMNS)
PASSTHROUGH_ARTIFACTS = tuple(
    name for name in REQUIRED_ARTIFACTS if name not in MARKET_ARTIFACTS
)

BENCHMARK_CODES = {"SPY": "SPY.US", "BIL": "BIL.US"}
BAR_FIELDS = ("Open", "High", "Low", "Close", "Volume", "Amount")
PRICE_COLUMNS = ("Open", "High", "Low", "Close")
NEW_YORK = ZoneInfo("America/New_York")

# Section 31 rates are dollars per million converted to a decimal rate.  Each
# effective date is backed by the SEC advisory index captured in fee evidence.
_SEC_RATES = (
    (date(2018, 5, 22), 13.00 / 1_000_000),
    (date(2019, 4, 16), 20.70 / 1_000_000),
    (date(2020, 2, 18), 22.10 / 1_000_000),
    (date(2021, 2, 25), 5.10 / 1_000_000),
    (date(2022, 5, 14), 22.90 / 1_000_000),
    (date(2023, 2, 27), 8.00 / 1_000_000),
    (date(2024, 5, 22), 27.80 / 1_000_000),
    (date(2025, 5, 14), 0.0),
    (date(2026, 4, 4), 20.60 / 1_000_000),
)

# FINRA's 2020 filing and current adjustment schedule preserve the complete
# equity TAF progression needed by this data product.
_FINRA_TAF = (
    (date(2012, 7, 1), 0.000119, 5.95),
    (date(2022, 1, 1), 0.000130, 6.49),
    (date(2023, 1, 1), 0.000145, 7.27),
    (date(2024, 1, 1), 0.000166, 8.30),
    (date(2026, 1, 1), 0.000195, 9.79),
)


class HistoricalBarProvider(Protocol):
    """The read-only subset shared by TdxProvider and deterministic tests."""

    def fetch_bars(
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
    ) -> dict[str, pd.DataFrame]: ...

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
    ) -> tuple[dict[str, pd.DataFrame], tuple[Any, ...]]: ...


@dataclass(frozen=True)
class MarketPreparationGap:
    code: str
    dataset: str
    detail: str
    severity: str = "CRITICAL"
    security_id: str | None = None
    vendor_code: str | None = None
    session_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketPreparationResult:
    status: str
    output_dir: Path
    source_batch: SourceBatch
    report_path: Path
    manifest_sha256: str
    gaps: tuple[MarketPreparationGap, ...]
    row_counts: Mapping[str, int]

    @property
    def ready(self) -> bool:
        return self.status == "MARKET_READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output_dir": str(self.output_dir),
            "source_batch_id": self.source_batch.batch_id,
            "report_path": str(self.report_path),
            "manifest_sha256": self.manifest_sha256,
            "gaps": [item.to_dict() for item in self.gaps],
            "row_counts": dict(self.row_counts),
        }


def _raw_rpc_capture_records(envelopes: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Serialize exact TQ request/response bytes with independently checkable hashes."""

    records: list[dict[str, Any]] = []
    for item in envelopes:
        request_bytes = bytes(item.request_bytes)
        response_bytes = bytes(item.response_bytes)
        received_at = item.fetched_at.isoformat()
        records.append(
            {
                "method": str(item.method),
                "request_base64": base64.b64encode(request_bytes).decode("ascii"),
                "request_sha256": sha256_bytes(request_bytes),
                "request_utf8": request_bytes.decode("utf-8"),
                "response_base64": base64.b64encode(response_bytes).decode("ascii"),
                "response_sha256": sha256_bytes(response_bytes),
                "response_utf8": response_bytes.decode("utf-8"),
                "received_at": received_at,
                # Compatibility with captures produced before the explicit
                # receive-time contract was named.
                "fetched_at": received_at,
            }
        )
    return records


class USPITMarketPreparer:
    """Complete a reviewed US PIT workspace with immutable market artifacts.

    This class never infers membership and never promotes a release.  It
    exports provider observations, validates the input frozen XNYS schedule,
    derives effective-dated fees, and publishes an auditable full workspace.
    """

    def __init__(
        self,
        store: USPITStore | Path | str,
        provider: HistoricalBarProvider | None,
        *,
        tdx_source_version: str,
        clock: Callable[[], datetime] | None = None,
        commission_rate: float = 0.0005,
        slippage_rate: float = 0.0005,
        allow_test_fee_evidence: bool = False,
        allow_test_provider_capture: bool = False,
    ) -> None:
        version = str(tdx_source_version).strip()
        if not version:
            raise ValueError("tdx_source_version is required")
        if commission_rate < 0 or slippage_rate < 0:
            raise ValueError("commission and slippage rates must be non-negative")
        self.store = store if isinstance(store, USPITStore) else USPITStore(store)
        self.provider = provider
        self.tdx_source_version = version
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.commission_rate = float(commission_rate)
        self.slippage_rate = float(slippage_rate)
        self.allow_test_fee_evidence = bool(allow_test_fee_evidence)
        self.allow_test_provider_capture = bool(allow_test_provider_capture)

    def inspect_reviewed_workspace(
        self, input_dir: Path | str
    ) -> tuple[MarketPreparationGap, ...]:
        """Verify the immutable reviewed-input gate without touching TDX."""

        source_root = Path(input_dir).resolve()
        if not source_root.is_dir():
            raise ValueError(f"reviewed input directory not found: {source_root}")
        before = self._input_hashes(source_root)
        gaps: list[MarketPreparationGap] = []
        self._load_inputs(source_root, gaps)
        self._inherit_workspace_gate(source_root, gaps)
        if before != self._input_hashes(source_root):
            raise ValueError("reviewed input changed while it was being inspected")
        return tuple(gaps)

    def prepare(
        self,
        input_dir: Path | str,
        output_dir: Path | str,
        *,
        start_date: date,
        end_date: date,
        universe_id: str = UNIVERSE_ID,
    ) -> MarketPreparationResult:
        if universe_id != UNIVERSE_ID:
            raise ValueError(f"market preparation only supports {UNIVERSE_ID}")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        started_at = self.clock()
        if started_at.tzinfo is None:
            raise ValueError("market preparation clock must be timezone-aware")

        source_root = Path(input_dir).resolve()
        target = Path(output_dir).resolve()
        if not source_root.is_dir():
            raise ValueError(f"reviewed input directory not found: {source_root}")
        if (
            target == source_root
            or source_root in target.parents
            or target in source_root.parents
        ):
            raise ValueError("market output and reviewed input directories must be disjoint")
        if target.exists():
            raise ValueError(f"reviewed market output already exists and is immutable: {target}")

        input_hashes = self._input_hashes(source_root)
        gaps: list[MarketPreparationGap] = []
        inputs = self._load_inputs(source_root, gaps)
        if input_hashes != self._input_hashes(source_root):
            raise ValueError("reviewed input changed while it was being loaded")
        inherited_sources = self._inherit_workspace_gate(source_root, gaps)
        # A reviewed workspace that has not passed its own immutable gate must
        # never reach the market-data boundary.  Besides being wasteful, doing
        # so could make a structurally invalid input appear to have usable TDX
        # lineage.  Preserve the reviewed artifacts and publish a blocked
        # market workspace without invoking the provider.
        if gaps and (source_root / "manifest.json").is_file() and (
            source_root / "gap_report.json"
        ).is_file():
            observed_at = self.clock()
            if observed_at.tzinfo is None or observed_at < started_at:
                raise ValueError("market preparation completion clock is invalid")
            source_batch = self.store.write_source_batch(inherited_sources)
            return self._publish(
                source_root,
                target,
                inputs,
                source_batch,
                gaps,
                status="DATA_BLOCKED",
                start_date=start_date,
                end_date=end_date,
                fetch_start=start_date,
                fetch_end=end_date,
                observed_at=observed_at,
                universe_id=universe_id,
                input_hashes=input_hashes,
            )
        calendar, fetch_start, fetch_end = self._calendar(
            inputs["xnys_calendar"], start_date, end_date, gaps
        )
        aliases, alias_collisions = self._normalize_aliases(
            inputs["listing_aliases"], gaps
        )
        memberships = self._normalize_memberships(
            inputs["membership_monthly"], start_date, end_date, gaps
        )
        self._validate_membership_calendar(
            memberships, calendar, start_date, end_date, gaps
        )
        security_ids = sorted(set(memberships["security_id"].astype(str)))
        self._validate_security_master(inputs["security_master"], security_ids, gaps)

        corporate_actions = inputs["corporate_actions"]
        spinoff_successor_ids: list[str] = []
        if "successor_security_id" in corporate_actions.columns:
            spinoff_successor_ids = sorted(
                {
                    str(item).strip()
                    for item in corporate_actions.loc[
                        corporate_actions["action_type"]
                        .astype(str)
                        .str.strip()
                        .str.upper()
                        .eq("SPINOFF")
                        & corporate_actions["security_id"]
                        .astype(str)
                        .isin(security_ids),
                        "successor_security_id",
                    ]
                    if str(item).strip().startswith("us_")
                }
            )
        market_input_security_ids = sorted(
            set(security_ids) | set(spinoff_successor_ids)
        )
        self._validate_security_master(
            inputs["security_master"], market_input_security_ids, gaps
        )

        vendor_codes = self._required_vendor_codes(
            aliases, market_input_security_ids, fetch_start, fetch_end, gaps
        )
        spinoff_vendor_codes = {
            str(item).strip().upper()
            for item in aliases.loc[
                aliases["security_id"].astype(str).isin(spinoff_successor_ids),
                "vendor_code",
            ]
            if str(item).strip()
        }
        requested_codes = sorted(set(vendor_codes) | set(BENCHMARK_CODES.values()))
        # SCOPE-C (approved): the TDX pool only carries currently-listed US
        # equities.  Historical members that are absent from the pool have no
        # historical bars anywhere in TDX; requesting them stalls the RPC and
        # must be avoided.  They are recorded as excluded market-data gaps and
        # their in-window tenures are excluded from coverage expectations.
        pool_codes: set[str] = set()
        pool_mapping: dict[str, str] = {}
        pool_capture_ref = None
        if self.provider is not None:
            try:
                pool_list, raw_pool_mapping = self.provider.list_us_stocks()
                pool_mapping = {
                    str(key).upper(): str(value)
                    for key, value in raw_pool_mapping.items()
                }
                pool_codes = {str(item).upper() for item in pool_list}
                pool_capture_ref = self.store.put_bytes(
                    canonical_json_bytes(
                        {
                            "format_version": "tdx-us-stock-pool-capture-v1",
                            "codes": sorted(pool_codes),
                            "names": {
                                str(key).upper(): str(value)
                                for key, value in sorted(pool_mapping.items())
                            },
                        }
                    ),
                    media_type="application/json",
                )
                pool_codes |= set(BENCHMARK_CODES.values())
            except Exception:
                pool_codes = set()
                pool_mapping = {}
                pool_capture_ref = None
        if pool_codes:
            # Reviewed spinoff successors are required causal valuation inputs.
            # TDX can serve their bars even when its current-stock discovery
            # pool omits the child listing, so fetch those exact reviewed codes
            # without treating them as Scope-C membership securities.
            fetch_codes = [
                c
                for c in requested_codes
                if c in pool_codes or c in spinoff_vendor_codes
            ]
            no_data_codes = [
                c
                for c in requested_codes
                if c not in pool_codes and c not in spinoff_vendor_codes
            ]
        else:
            # No pool available (e.g. provider-less test mode): keep the full
            # request and let the fetch path surface per-code unavailability.
            fetch_codes = requested_codes
            no_data_codes = []
        excluded_market_data: list[dict[str, Any]] = []
        excluded_security_ids: set[str] = set()
        if no_data_codes:
            no_data_set = {c.upper() for c in no_data_codes}
            alias_by_code: dict[str, list[str]] = {}
            vendor_list = aliases[["vendor_code", "security_id"]].copy()
            vendor_list["vendor_code"] = vendor_list["vendor_code"].astype(str).str.upper()
            for code in no_data_codes:
                matched = vendor_list.loc[
                    vendor_list["vendor_code"].eq(code.upper()),
                    "security_id",
                ].astype(str)
                alias_by_code[code] = sorted(set(matched))
                excluded_security_ids.update(matched)
            excluded_market_data = [
                {
                    "vendor_code": code,
                    "security_ids": alias_by_code[code],
                    "detail": (
                        "TDX pool carries no US history for this code; "
                        "in-window tenures excluded per SCOPE-C"
                    ),
                    "rule_version": "SCOPE-C-QUALITY-v1",
                    "reason_counts": {"TDX_POOL_ABSENT": 1},
                }
                for code in no_data_codes
            ]
        else:
            excluded_market_data = []
            excluded_security_ids = set()
        # Alias collisions are recorded (not blocking): identity variants share
        # one vendor code and consume the same TDX bars.
        excluded_market_data.extend(alias_collisions)
        count = max(1, len(calendar) + 10)
        vendor_gap_start = len(gaps)
        raw_response, raw_envelopes = self._fetch_vendor_bars(
            fetch_codes,
            fetch_start,
            fetch_end,
            count,
            adjustment="none",
            dataset="bars_raw",
            gaps=gaps,
        )
        front_response, front_envelopes = self._fetch_vendor_bars(
            fetch_codes,
            fetch_start,
            fetch_end,
            count,
            adjustment="front",
            dataset="bars_vendor_front",
            gaps=gaps,
        )
        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at < started_at:
            raise ValueError("market preparation completion clock is invalid")

        if raw_envelopes and front_envelopes:
            raw_capture_ref = self.store.put_bytes(
                canonical_json_bytes(_raw_rpc_capture_records(raw_envelopes)),
                media_type="application/json",
            )
            front_capture_ref = self.store.put_bytes(
                canonical_json_bytes(_raw_rpc_capture_records(front_envelopes)),
                media_type="application/json",
            )
            capture_boundary = "TQReadOnlyClient.call_raw_http_bytes"
        else:
            raw_capture_ref = self.store.put_dataframe(
                self._vendor_capture_frame(raw_response)
            )
            front_capture_ref = self.store.put_dataframe(
                self._vendor_capture_frame(front_response)
            )
            capture_boundary = "TEST_PROVIDER_FETCH_BARS_RETURN"

        if excluded_market_data:
            sessions = pd.DatetimeIndex(
                pd.to_datetime(calendar["session_date"], errors="raise")
            ).normalize()
            sessions = sessions[
                (sessions >= pd.Timestamp(fetch_start))
                & (sessions <= pd.Timestamp(fetch_end))
            ]
            alias_dates = aliases.copy()
            alias_dates["vendor_code"] = (
                alias_dates["vendor_code"].astype(str).str.upper()
            )
            alias_dates["valid_from"] = pd.to_datetime(
                alias_dates["valid_from"], errors="raise"
            ).dt.normalize()
            alias_dates["valid_to"] = pd.to_datetime(
                alias_dates["valid_to"], errors="coerce"
            ).dt.normalize()
            for record in excluded_market_data:
                if record.get("rule_version") != "SCOPE-C-QUALITY-v1":
                    continue
                code = str(record["vendor_code"]).upper()
                tenures = alias_dates.loc[alias_dates["vendor_code"].eq(code)]
                required: set[pd.Timestamp] = set()
                for tenure in tenures.itertuples(index=False):
                    lower = max(pd.Timestamp(fetch_start), pd.Timestamp(tenure.valid_from))
                    upper = (
                        pd.Timestamp(fetch_end)
                        if pd.isna(tenure.valid_to)
                        else min(pd.Timestamp(fetch_end), pd.Timestamp(tenure.valid_to))
                    )
                    if lower <= upper:
                        required.update(
                            sessions[(sessions >= lower) & (sessions <= upper)]
                        )
                record.update(
                    {
                        "first_problem_session": (
                            min(required).date().isoformat() if required else None
                        ),
                        "last_problem_session": (
                            max(required).date().isoformat() if required else None
                        ),
                        "reason_counts": {
                            "TDX_POOL_ABSENT": len(required)
                        },
                        "raw_capture_sha256": raw_capture_ref.sha256,
                        "front_capture_sha256": front_capture_ref.sha256,
                        "pool_capture_sha256": (
                            None
                            if pool_capture_ref is None
                            else pool_capture_ref.sha256
                        ),
                    }
                )

        raw_vendor = self._normalize_vendor_frames(raw_response, "bars_raw", gaps)
        front_vendor = self._normalize_vendor_frames(
            front_response, "bars_vendor_front", gaps
        )
        raw_vendor = self._restrict_to_calendar(
            raw_vendor, calendar, "bars_raw", gaps
        )
        front_vendor = self._restrict_to_calendar(
            front_vendor, calendar, "bars_vendor_front", gaps
        )

        quality_codes, quality_security_ids, quality_records = (
            self._scope_c_quality_exclusions(
                raw_vendor,
                front_vendor,
                aliases,
                security_ids,
                calendar,
                inputs["session_exceptions"],
                fetch_start,
                fetch_end,
                raw_capture_ref.sha256,
                front_capture_ref.sha256,
                gaps[vendor_gap_start:],
            )
        )
        excluded_security_ids.update(quality_security_ids)
        if pool_capture_ref is not None:
            for record in quality_records:
                record["pool_capture_sha256"] = pool_capture_ref.sha256
        excluded_market_data.extend(quality_records)
        if quality_codes:
            gaps = [
                gap
                for gap in gaps
                if not (
                    gap.vendor_code is not None
                    and gap.vendor_code.upper() in quality_codes
                    and gap.dataset in {"bars_raw", "bars_vendor_front"}
                )
            ]
        # R4.2: TDX validity gaps outside alias-tenure intersection are not
        # required history and must not block MARKET_READY for the required set.
        required_by_code: dict[str, set[str]] = {}
        try:
            _sessions_idx = pd.DatetimeIndex(
                pd.to_datetime(calendar["session_date"], errors="coerce")
            ).normalize()
            _fetch_lo = pd.Timestamp(fetch_start)
            _fetch_hi = pd.Timestamp(fetch_end)
            _sessions_req = _sessions_idx[
                (_sessions_idx >= _fetch_lo) & (_sessions_idx <= _fetch_hi)
            ]
            for _code, _g in aliases.groupby("vendor_code", sort=False):
                _code_u = str(_code).upper()
                if _code_u in set(BENCHMARK_CODES.values()):
                    continue
                _required: set[pd.Timestamp] = set()
                for _alias in _g.itertuples(index=False):
                    _lo = max(_fetch_lo, pd.Timestamp(_alias.valid_from))
                    _hi = (
                        _fetch_hi
                        if pd.isna(_alias.valid_to)
                        else min(_fetch_hi, pd.Timestamp(_alias.valid_to))
                    )
                    if _lo <= _hi:
                        _required.update(_sessions_req[(_sessions_req >= _lo) & (_sessions_req <= _hi)])
                required_by_code[_code_u] = {d.date().isoformat() for d in _required}
        except Exception:
            required_by_code = {}
        if required_by_code:
            _drop_codes = {"TDX_INVALID_OHLCV", "TDX_DUPLICATE_DAILY_BAR", "TDX_NON_XNYS_SESSION", "TDX_BAR_COLUMNS_MISSING"}
            gaps = [
                gap
                for gap in gaps
                if not (
                    gap.code in _drop_codes
                    and gap.vendor_code is not None
                    and gap.session_date is not None
                    and gap.vendor_code.upper() not in set(BENCHMARK_CODES.values())
                    and gap.session_date not in required_by_code.get(gap.vendor_code.upper(), {gap.session_date})
                )
            ]
        included_security_ids = sorted(
            set(security_ids) - excluded_security_ids
        )
        included_memberships = memberships.loc[
            ~memberships["security_id"].astype(str).isin(excluded_security_ids)
        ].copy()
        lineage_security_ids: set[str] = set(included_security_ids)
        lineage_security_ids.update(spinoff_successor_ids)
        try:
            _ca_for_lineage = inputs["corporate_actions"]
            if not _ca_for_lineage.empty and "action_type" in _ca_for_lineage.columns:
                _succ_col = _ca_for_lineage.get("successor_security_id", pd.Series(dtype="object")).astype(str)
                _mask_lineage = _ca_for_lineage["action_type"].astype(str).str.upper().isin({"SPLIT", "STOCK_DIVIDEND"})
                for _sid, _succ in zip(_ca_for_lineage.loc[_mask_lineage, "security_id"].astype(str), _succ_col[_mask_lineage], strict=False):
                    if str(_succ).startswith("us_") and str(_succ) in set(included_security_ids):
                        lineage_security_ids.add(str(_sid))
        except Exception:
            pass
        mapping_security_ids = sorted(lineage_security_ids)
        raw = self._map_to_stable_ids(
            raw_vendor,
            aliases,
            mapping_security_ids,
            fetch_start,
            fetch_end,
            "bars_raw",
            gaps,
        )
        front = self._map_to_stable_ids(
            front_vendor,
            aliases,
            mapping_security_ids,
            fetch_start,
            fetch_end,
            "bars_vendor_front",
            gaps,
        )
        prepared_actions, spinoff_basis_dependency = self._derive_spinoff_basis(
            inputs["corporate_actions"],
            raw,
            aliases,
            calendar,
            included_security_ids,
            raw_capture_ref.sha256,
            observed_at,
            fetch_end,
            gaps,
        )
        signal_sources = (
            inherited_sources
            if spinoff_basis_dependency is None
            else (*inherited_sources, spinoff_basis_dependency)
        )
        signal = self._build_pit_signal_bars(
            raw,
            included_memberships,
            prepared_actions,
            calendar,
            signal_sources,
            gaps,
        )
        benchmark_base, benchmark_evidence = self._build_benchmarks(
            raw_vendor,
            front_vendor,
            calendar,
            fetch_start,
            fetch_end,
            raw_capture_ref.sha256,
            front_capture_ref.sha256,
            gaps,
        )
        benchmarks = benchmark_base.copy()
        benchmarks["total_return_source_id"] = "tdx_benchmark_total_return_v1"
        benchmark_evidence["normalized_without_evidence_foreign_key_sha256"] = (
            sha256_json(_json_records(benchmarks))
        )
        benchmark_evidence_ref = self.store.put_bytes(
            canonical_json_bytes(benchmark_evidence), media_type="application/json"
        )
        benchmarks["total_return_evidence_sha256"] = benchmark_evidence_ref.sha256

        sec_fee_sources = [
            item for item in inherited_sources if item.dataset == "regulatory_fee_sec"
        ]
        finra_fee_sources = [
            item for item in inherited_sources if item.dataset == "regulatory_fee_finra"
        ]
        if self.allow_test_fee_evidence and not sec_fee_sources and not finra_fee_sources:
            test_sec = self.store.put_bytes(b"test-only SEC fee evidence")
            test_finra = self.store.put_bytes(b"test-only FINRA fee evidence")
            sec_fee_sources = [
                self._dependency(
                    source_id="test_sec_fee_evidence",
                    dataset="regulatory_fee_sec",
                    object_sha256=test_sec.sha256,
                    observed_at=observed_at,
                    as_of_date=fetch_end,
                    url=SEC_FEE_URL,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    source_version="test-fixture",
                    metadata={
                        "test_fixture": True,
                        "fee_evidence_contract_version": FEE_EVIDENCE_CONTRACT_VERSION,
                        "rate_entries": [
                            {
                                "effective_from": effective.isoformat(),
                                "sec_sell_fee_rate": rate,
                            }
                            for effective, rate in _SEC_RATES
                        ],
                    },
                    role=SourceRole.VALIDATION_ANCHOR,
                )
            ]
            finra_fee_sources = [
                self._dependency(
                    source_id="test_finra_fee_evidence",
                    dataset="regulatory_fee_finra",
                    object_sha256=test_finra.sha256,
                    observed_at=observed_at,
                    as_of_date=fetch_end,
                    url=FINRA_TAF_URL,
                    license_class=LicenseClass.OFFICIAL_PUBLIC,
                    source_version="test-fixture",
                    metadata={
                        "test_fixture": True,
                        "fee_evidence_contract_version": FEE_EVIDENCE_CONTRACT_VERSION,
                        "rate_entries": [
                            {
                                "effective_from": effective.isoformat(),
                                "finra_taf_per_share": per_share,
                                "finra_taf_cap": cap,
                            }
                            for effective, per_share, cap in _FINRA_TAF
                        ],
                    },
                    role=SourceRole.VALIDATION_ANCHOR,
                )
            ]
        fee_derivation_sha256 = sha256_file(Path(__file__))
        fees = self._fee_schedule(fetch_start, fetch_end, gaps)
        sec_bindings = self._fee_evidence_bindings(
            fees,
            sec_fee_sources,
            authority="SEC",
            gaps=gaps,
        )
        finra_bindings = self._fee_evidence_bindings(
            fees,
            finra_fee_sources,
            authority="FINRA",
            gaps=gaps,
        )
        fees["sec_evidence_sha256"] = sec_bindings
        fees["finra_evidence_sha256"] = finra_bindings
        fees["fee_derivation_sha256"] = fee_derivation_sha256
        fee_evidence = {
            "format_version": "us-equity-effective-fee-evidence-v1",
            "sec_fee_advisory_index": SEC_FEE_URL,
            "finra_taf_adjustment_schedule": FINRA_TAF_URL,
            "section_31_rates": [
                {
                    "effective_from": effective.isoformat(),
                    "rate_per_dollar": rate,
                }
                for effective, rate in _SEC_RATES
            ],
            "finra_taf_rates": [
                {
                    "effective_from": effective.isoformat(),
                    "per_share": per_share,
                    "trade_cap": cap,
                }
                for effective, per_share, cap in _FINRA_TAF
            ],
            "model_assumptions": {
                "commission_rate": self.commission_rate,
                "slippage_rate": self.slippage_rate,
                "min_commission": 0.0,
                "stamp_duty_rate": 0.0,
                "board_lot": 1,
            },
            "sec_raw_evidence_sha256": sorted(
                item.object_sha256 for item in sec_fee_sources
            ),
            "finra_raw_evidence_sha256": sorted(
                item.object_sha256 for item in finra_fee_sources
            ),
            "fee_derivation_sha256": fee_derivation_sha256,
        }
        fee_evidence_ref = self.store.put_bytes(
            canonical_json_bytes(fee_evidence), media_type="application/json"
        )
        calendar_ref = self.store.put_dataframe(calendar)
        dependencies = (
            self._dependency(
                source_id="tdx_us_market_raw_v1",
                dataset="bars_raw",
                object_sha256=raw_capture_ref.sha256,
                observed_at=observed_at,
                as_of_date=fetch_end,
                url=(
                    "tdx-adapter://fetch_bars?period=1d&dividend_type=none"
                    "&upstream_fill_data=false"
                ),
                license_class=LicenseClass.LOCAL_VENDOR,
                metadata={
                    "read_only": True,
                    "fill_data": False,
                    "capture_boundary": capture_boundary,
                    "http_response_bytes_frozen": bool(raw_envelopes),
                    "requested_codes": requested_codes,
                    "scope_c_rule_version": "SCOPE-C-QUALITY-v1",
                    "scope_c_exclusions": [
                        dict(item) for item in excluded_market_data
                    ],
                    "normalized_artifact_sha256": sha256_json(_json_records(raw)),
                },
            ),
            self._dependency(
                source_id="tdx_us_market_front_v1",
                dataset="bars_vendor_front",
                object_sha256=front_capture_ref.sha256,
                observed_at=observed_at,
                as_of_date=fetch_end,
                url=(
                    "tdx-adapter://fetch_bars?period=1d&dividend_type=front"
                    "&upstream_fill_data=false"
                ),
                license_class=LicenseClass.LOCAL_VENDOR,
                metadata={
                    "read_only": True,
                    "fill_data": False,
                    "capture_boundary": capture_boundary,
                    "http_response_bytes_frozen": bool(front_envelopes),
                    "requested_codes": requested_codes,
                    "not_used_directly_as_pit_signal": True,
                    "normalized_artifact_sha256": sha256_json(_json_records(front)),
                },
            ),
            self._dependency(
                source_id="tdx_benchmark_total_return_v1",
                dataset="benchmark_total_return",
                object_sha256=benchmark_evidence_ref.sha256,
                observed_at=observed_at,
                as_of_date=fetch_end,
                url="tdx://derived/benchmark-total-return-v1",
                license_class=LicenseClass.LOCAL_VENDOR,
                metadata={
                    "read_only": True,
                    "causal_scale_invariant_chain": True,
                    "raw_capture_sha256": raw_capture_ref.sha256,
                    "front_capture_sha256": front_capture_ref.sha256,
                    "normalized_artifact_sha256": sha256_json(
                        _json_records(benchmarks)
                    ),
                },
            ),
            self._dependency(
                source_id="exchange_calendars_xnys",
                dataset="xnys_calendar",
                object_sha256=calendar_ref.sha256,
                observed_at=observed_at,
                as_of_date=fetch_end,
                url="python-package://exchange-calendars/XNYS",
                license_class=LicenseClass.PERMISSIVE,
                source_version=getattr(xcals, "__version__", "unknown"),
                metadata={
                    "calendar": "XNYS",
                    "frozen": True,
                    "normalized_artifact_sha256": sha256_json(
                        _json_records(calendar)
                    ),
                },
            ),
            self._dependency(
                source_id="sec_finra_effective_fee_schedule_v1",
                dataset="execution_fee_schedule",
                object_sha256=fee_evidence_ref.sha256,
                observed_at=observed_at,
                as_of_date=fetch_end,
                url=SEC_FEE_URL,
                license_class=LicenseClass.OFFICIAL_PUBLIC,
                source_version="sec-finra-through-2026-04-04",
                metadata={
                    "sec_url": SEC_FEE_URL,
                    "finra_url": FINRA_TAF_URL,
                    "commission_and_slippage_are_model_assumptions": True,
                    "normalized_artifact_sha256": sha256_json(_json_records(fees)),
                    "sec_raw_evidence_sha256": sorted(
                        item.object_sha256 for item in sec_fee_sources
                    ),
                    "finra_raw_evidence_sha256": sorted(
                        item.object_sha256 for item in finra_fee_sources
                    ),
                    "fee_derivation_sha256": fee_derivation_sha256,
                    "fee_evidence_contract_version": FEE_EVIDENCE_CONTRACT_VERSION,
                },
            ),
        )
        pool_dependency: SourceDependency | None = None
        if pool_capture_ref is not None:
            pool_dependency = self._dependency(
                source_id="tdx_us_stock_pool_v1",
                dataset="tdx_us_stock_pool",
                object_sha256=pool_capture_ref.sha256,
                observed_at=observed_at,
                published_at=observed_at,
                as_of_date=fetch_end,
                url="tdx-adapter://list_us_stocks",
                license_class=LicenseClass.LOCAL_VENDOR,
                metadata={
                    "read_only": True,
                    "capture_boundary": "TdxProvider.list_us_stocks",
                    "listed_code_count": len(
                        pool_codes - set(BENCHMARK_CODES.values())
                    ),
                },
                role=SourceRole.VALIDATION_ANCHOR,
            )
            dependencies = (
                *dependencies,
                pool_dependency,
            )
        dependencies = (*dependencies, *sec_fee_sources, *finra_fee_sources)
        if spinoff_basis_dependency is not None:
            dependencies = (*dependencies, spinoff_basis_dependency)
        lifecycle_reconciliations: pd.DataFrame | None = None
        if pool_dependency is not None:
            lifecycle_reconciliations, lifecycle_dependency = (
                self._build_lifecycle_reconciliations(
                    included_memberships,
                    prepared_actions,
                    aliases,
                    pool_codes,
                    pool_mapping,
                    pool_dependency,
                    observed_at,
                    fetch_end,
                    gaps,
                )
            )
            if lifecycle_dependency is not None:
                dependencies = (*dependencies, lifecycle_dependency)
        combined_dependencies: dict[tuple[str, str, str | None, str], SourceDependency] = {
            (item.source_id, item.dataset, item.as_of_date, item.object_sha256): item
            for item in (*inherited_sources, *dependencies)
        }
        source_batch = self.store.write_source_batch(combined_dependencies.values())

        coverage = self._bar_coverage(
            included_memberships,
            aliases,
            raw,
            signal,
            calendar,
            inputs["session_exceptions"],
            fetch_start,
        )
        for row in coverage.loc[~coverage["passed"]].itertuples(index=False):
            gaps.append(
                MarketPreparationGap(
                    code="INCOMPLETE_BAR_COVERAGE",
                    dataset="bar_coverage",
                    security_id=str(row.security_id),
                    session_date=pd.Timestamp(row.decision_date).date().isoformat(),
                    detail=(
                        f"expected={row.expected_sessions}, raw={row.raw_sessions}, "
                        f"signal={row.signal_sessions}, explained={row.explained_missing_sessions}"
                    ),
                )
            )
        self._validate_next_session_opens(
            raw, included_memberships, calendar, prepared_actions, gaps,
        )

        prepared_inputs = {**inputs, "corporate_actions": prepared_actions}
        passthrough = self._scope_c_filtered_passthrough(
            prepared_inputs,
            included_memberships,
            excluded_security_ids,
        )
        if lifecycle_reconciliations is not None:
            passthrough["lifecycle_reconciliations"] = lifecycle_reconciliations
        frames = {
            **{name: passthrough[name] for name in PASSTHROUGH_ARTIFACTS},
            "bars_raw": raw,
            "bars_vendor_front": front,
            "bars_pit_signal": signal,
            "benchmarks": benchmarks,
            "xnys_calendar": calendar,
            "execution_fee_schedule": fees,
            "bar_coverage": coverage,
        }
        status = "DATA_BLOCKED" if gaps else "MARKET_READY"
        return self._publish(
            source_root,
            target,
            frames,
            source_batch,
            gaps,
            status=status,
            start_date=start_date,
            end_date=end_date,
            fetch_start=fetch_start,
            fetch_end=fetch_end,
            observed_at=observed_at,
            universe_id=universe_id,
            input_hashes=input_hashes,
            excluded_market_data=excluded_market_data,
        )

    @staticmethod
    def _input_hashes(root: Path) -> dict[str, str]:
        return {
            path.name: sha256_file(path)
            for path in sorted(root.iterdir())
            if path.is_file()
        }

    def _load_inputs(
        self, root: Path, gaps: list[MarketPreparationGap]
    ) -> tuple[dict[str, pd.DataFrame], tuple[Any, ...]]:
        result: dict[str, pd.DataFrame] = {}
        for name, required_columns in REQUIRED_ARTIFACT_COLUMNS.items():
            path = root / f"{name}.parquet"
            if not path.is_file():
                gaps.append(
                    MarketPreparationGap(
                        code="MISSING_INPUT_ARTIFACT",
                        dataset=name,
                        detail=f"required reviewed input is absent: {path.name}",
                    )
                )
                result[name] = pd.DataFrame(columns=sorted(required_columns))
                continue
            frame = pd.read_parquet(path)
            missing = sorted(set(required_columns) - set(frame.columns))
            if missing:
                gaps.append(
                    MarketPreparationGap(
                        code="MISSING_INPUT_COLUMNS",
                        dataset=name,
                        detail="missing columns: " + ", ".join(missing),
                    )
                )
                for column in missing:
                    frame[column] = None
            result[name] = frame
        return result

    def _inherit_workspace_gate(
        self, root: Path, gaps: list[MarketPreparationGap]
    ) -> tuple[SourceDependency, ...]:
        manifest_path = root / "manifest.json"
        manifest_receipt_path = root / "manifest.cas.json"
        gap_path = root / "gap_report.json"
        if (
            not manifest_path.is_file()
            or not manifest_receipt_path.is_file()
            or not gap_path.is_file()
        ):
            gaps.append(
                MarketPreparationGap(
                    code="REVIEW_WORKSPACE_GATE_MISSING",
                    dataset="review_workspace",
                    detail=(
                        "input requires manifest.json, manifest.cas.json, and "
                        "gap_report.json"
                    ),
                )
            )
            return ()
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_receipt = json.loads(
                manifest_receipt_path.read_text(encoding="utf-8")
            )
            gap_report = json.loads(gap_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            gaps.append(
                MarketPreparationGap(
                    code="REVIEW_WORKSPACE_GATE_INVALID",
                    dataset="review_workspace",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            return ()
        manifest_digest = sha256_file(manifest_path)
        receipt_digest = str(manifest_receipt.get("cas_object_sha256") or "")
        receipt_valid = (
            manifest_receipt.get("workspace_id") == manifest.get("workspace_id")
            and manifest_receipt.get("manifest_sha256") == manifest_digest
            and manifest_receipt.get("manifest_size_bytes") == len(manifest_bytes)
            and receipt_digest == manifest_digest
            and _is_sha256(receipt_digest)
        )
        if receipt_valid:
            try:
                receipt_object = self.store.object_path(receipt_digest)
                receipt_valid = (
                    receipt_object.is_file()
                    and receipt_object.read_bytes() == manifest_bytes
                )
            except (OSError, ValueError):
                receipt_valid = False
        if not receipt_valid:
            gaps.append(
                MarketPreparationGap(
                    code="REVIEW_WORKSPACE_MANIFEST_CAS_INVALID",
                    dataset="review_workspace",
                    detail="reviewed workspace manifest lacks an exact CAS receipt",
                )
            )
        expected_gap_hash = str(manifest.get("gap_report_sha256") or "")
        artifact_hashes = manifest.get("artifacts")
        review_inputs = manifest.get("review_inputs")
        batch_ids = manifest.get("source_batch_ids")
        identity_keys = (
            "format_version",
            "normalization_id",
            "normalization_manifest_sha256",
            "source_batch_ids",
            "decision_start",
            "decision_end",
            "review_inputs",
        )
        manifest_identity = {key: manifest.get(key) for key in identity_keys}
        # review_receipts was added to the assembler identity when review
        # inputs began being frozen into CAS.  Keep compatibility with older
        # fixture workspaces that legitimately predate that field while
        # verifying it whenever the assembler emitted it.
        if "review_receipts" in manifest:
            manifest_identity["review_receipts"] = manifest.get("review_receipts")
        manifest_identity.update(
            {
                "artifacts": manifest.get("artifacts"),
                "gap_report_sha256": manifest.get("gap_report_sha256"),
            }
        )
        normalization_manifest_hash = str(
            manifest.get("normalization_manifest_sha256") or ""
        )
        normalization_id = str(manifest.get("normalization_id") or "")
        normalization_manifest_path = (
            self.store.root
            / "normalized"
            / "official"
            / normalization_id
            / "manifest.json"
        )
        normalization_receipt_valid = False
        if normalization_manifest_path.is_file():
            try:
                normalization_manifest = json.loads(
                    normalization_manifest_path.read_text(encoding="utf-8")
                )
                normalization_identity = {
                    key: normalization_manifest.get(key)
                    for key in (
                        "format_version",
                        "source_batch_ids",
                        "sources",
                        "artifacts",
                        "policy",
                    )
                }
                normalization_receipt_valid = (
                    normalization_manifest_path.parent.name == normalization_id
                    and normalization_manifest.get("normalization_id")
                    == normalization_id
                    and normalization_id == sha256_json(normalization_identity)
                    and normalization_manifest.get("candidate_only") is True
                    and normalization_manifest.get("direct_build_allowed") is False
                    and sha256_file(normalization_manifest_path)
                    == normalization_manifest_hash
                )
            except (OSError, json.JSONDecodeError):
                normalization_receipt_valid = False
        manifest_invalid = (
            manifest.get("format_version") != "us-pit-reviewed-workspace-v1"
            or manifest.get("universe_id") != UNIVERSE_ID
            or not _is_sha256(manifest.get("workspace_id"))
            or root.name != manifest.get("workspace_id")
            or manifest.get("workspace_id") != sha256_json(manifest_identity)
            or not _is_sha256(manifest.get("normalization_id"))
            or not _is_sha256(manifest.get("normalization_manifest_sha256"))
            or not normalization_receipt_valid
            or not isinstance(review_inputs, Mapping)
            or any(
                not isinstance(name, str) or not _is_sha256(digest)
                for name, digest in (
                    review_inputs.items() if isinstance(review_inputs, Mapping) else ()
                )
            )
            or not isinstance(batch_ids, list)
            or not batch_ids
            or any(not _is_sha256(batch_id) for batch_id in (batch_ids or ()))
            or not _valid_date_window(
                manifest.get("decision_start"), manifest.get("decision_end")
            )
            or not isinstance(artifact_hashes, Mapping)
            or set(artifact_hashes or ()) != set(REQUIRED_ARTIFACTS)
            or any(
                not _is_sha256(digest)
                for digest in (
                    artifact_hashes.values()
                    if isinstance(artifact_hashes, Mapping)
                    else ()
                )
            )
            or not _is_sha256(expected_gap_hash)
        )
        if manifest_invalid:
            gaps.append(
                MarketPreparationGap(
                    code="REVIEW_WORKSPACE_MANIFEST_INVALID",
                    dataset="review_workspace",
                    detail="input is not an assembler-produced reviewed workspace manifest",
                )
            )
        if expected_gap_hash != sha256_json(gap_report):
            gaps.append(
                MarketPreparationGap(
                    code="REVIEW_WORKSPACE_GAP_HASH_MISMATCH",
                    dataset="review_workspace",
                    detail="gap_report.json does not match the input manifest",
                )
            )
        if not isinstance(artifact_hashes, Mapping):
            gaps.append(
                MarketPreparationGap(
                    code="REVIEW_WORKSPACE_MANIFEST_INVALID",
                    dataset="review_workspace",
                    detail="manifest artifacts mapping is missing",
                )
            )
        else:
            for name in REQUIRED_ARTIFACTS:
                path = root / f"{name}.parquet"
                expected = str(artifact_hashes.get(name) or "")
                if not path.is_file() or expected != sha256_json(
                    _json_records(pd.read_parquet(path))
                ):
                    gaps.append(
                        MarketPreparationGap(
                            code="REVIEW_WORKSPACE_ARTIFACT_HASH_MISMATCH",
                            dataset=name,
                            detail=f"{name}.parquet does not match the input manifest",
                        )
                    )
        blocking = gap_report.get("blocking_gaps")
        input_blocked = (
            manifest.get("status") != "REVIEW_READY"
            or manifest.get("direct_build_allowed") is not True
            or not isinstance(blocking, list)
            or bool(blocking)
        )
        if input_blocked:
            gaps.append(
                MarketPreparationGap(
                    code="INPUT_REVIEW_WORKSPACE_BLOCKED",
                    dataset="review_workspace",
                    detail=(
                        "assembler gate is not REVIEW_READY or retains blocking gaps; "
                        f"status={manifest.get('status')}, gaps="
                        f"{len(blocking) if isinstance(blocking, list) else 'INVALID'}"
                    ),
                )
            )
        if not isinstance(batch_ids, list) or not batch_ids:
            gaps.append(
                MarketPreparationGap(
                    code="REVIEW_WORKSPACE_SOURCE_BATCHES_MISSING",
                    dataset="review_workspace",
                    detail="input manifest has no source_batch_ids lineage",
                )
            )
            return ()
        inherited: dict[tuple[str, str, str | None, str], SourceDependency] = {}
        for batch_id in (str(item).strip() for item in batch_ids):
            if not batch_id:
                continue
            try:
                batch = self.store.load_source_batch(batch_id)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                gaps.append(
                    MarketPreparationGap(
                        code="REVIEW_WORKSPACE_SOURCE_BATCH_INVALID",
                        dataset="review_workspace",
                        detail=f"{batch_id}: {exc}",
                    )
                )
                continue
            for item in batch.dependencies:
                try:
                    object_path = self.store.object_path(item.object_sha256)
                    object_valid = (
                        object_path.is_file()
                        and sha256_file(object_path) == item.object_sha256
                    )
                except (OSError, ValueError):
                    object_valid = False
                if not object_valid:
                    gaps.append(
                        MarketPreparationGap(
                            code="REVIEW_WORKSPACE_SOURCE_OBJECT_INVALID",
                            dataset=item.dataset,
                            detail=(
                                f"captured object is missing or corrupt: "
                                f"{item.source_id}/{item.object_sha256}"
                            ),
                        )
                    )
                    continue
                inherited[
                    (item.source_id, item.dataset, item.as_of_date, item.object_sha256)
                ] = item
        return tuple(
            inherited[key]
            for key in sorted(
                inherited,
                key=lambda values: tuple(
                    "" if value is None else str(value) for value in values
                ),
            )
        )

    @staticmethod
    def _calendar(
        frozen: pd.DataFrame,
        start_date: date,
        end_date: date,
        gaps: list[MarketPreparationGap],
    ) -> tuple[pd.DataFrame, date, date]:
        required = {"session_date", "market_open", "market_close"}
        if not required.issubset(frozen.columns):
            gaps.append(
                MarketPreparationGap(
                    code="FROZEN_XNYS_CALENDAR_SCHEMA_INVALID",
                    dataset="xnys_calendar",
                    detail="input frozen calendar lacks required columns",
                )
            )
            return frozen.copy(), start_date, end_date
        value = frozen.copy()
        sessions = pd.to_datetime(value["session_date"], errors="coerce").dt.normalize()
        invalid_sessions = sessions.isna() | sessions.duplicated(keep=False)
        if invalid_sessions.any():
            gaps.append(
                MarketPreparationGap(
                    code="FROZEN_XNYS_CALENDAR_INVALID",
                    dataset="xnys_calendar",
                    detail=f"invalid or duplicate sessions: {int(invalid_sessions.sum())}",
                )
            )
        value = value.loc[~invalid_sessions].copy()
        value["session_date"] = sessions.loc[~invalid_sessions]
        value = value.sort_values("session_date", kind="stable").reset_index(drop=True)
        if value.empty:
            gaps.append(
                MarketPreparationGap(
                    code="FROZEN_XNYS_CALENDAR_EMPTY",
                    dataset="xnys_calendar",
                    detail="input frozen calendar has no usable sessions",
                )
            )
            return value, start_date, end_date

        try:
            reference = xcals.get_calendar("XNYS")
            end_label = pd.Timestamp(end_date).normalize()
            if reference.is_session(end_label):
                next_label = reference.next_session(end_label)
            else:
                next_label = reference.date_to_session(end_label, direction="next")
            required_next = pd.Timestamp(next_label).tz_localize(None).normalize()
            fetch_start = pd.Timestamp(value["session_date"].min()).date()
            fetch_end = pd.Timestamp(value["session_date"].max()).date()
            expected_schedule = reference.schedule.loc[
                str(fetch_start) : str(fetch_end)
            ].copy()
        except Exception as exc:
            gaps.append(
                MarketPreparationGap(
                    code="XNYS_CALENDAR_ERROR",
                    dataset="xnys_calendar",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            return value, start_date, end_date

        expected_sessions = pd.DatetimeIndex(expected_schedule.index).tz_localize(None).normalize()
        observed_sessions = pd.DatetimeIndex(value["session_date"])
        if not observed_sessions.equals(expected_sessions):
            gaps.append(
                MarketPreparationGap(
                    code="FROZEN_XNYS_CALENDAR_MISMATCH",
                    dataset="xnys_calendar",
                    detail="input sessions do not exactly match exchange-calendars XNYS",
                )
            )
        expected_open = [
            pd.Timestamp(item).tz_convert(NEW_YORK)
            for item in expected_schedule["open"]
        ]
        expected_close = [
            pd.Timestamp(item).tz_convert(NEW_YORK)
            for item in expected_schedule["close"]
        ]
        observed_open = pd.to_datetime(value["market_open"], errors="coerce", utc=True)
        observed_close = pd.to_datetime(value["market_close"], errors="coerce", utc=True)
        if (
            len(value) != len(expected_schedule)
            or observed_open.isna().any()
            or observed_close.isna().any()
            or any(
                actual != expected.tz_convert(timezone.utc)
                for actual, expected in zip(observed_open, expected_open, strict=True)
            )
            or any(
                actual != expected.tz_convert(timezone.utc)
                for actual, expected in zip(observed_close, expected_close, strict=True)
            )
        ):
            gaps.append(
                MarketPreparationGap(
                    code="FROZEN_XNYS_SESSION_TIME_MISMATCH",
                    dataset="xnys_calendar",
                    detail="input open/close timestamps do not match frozen XNYS sessions",
                )
            )
        if required_next not in observed_sessions:
            gaps.append(
                MarketPreparationGap(
                    code="FROZEN_XNYS_NEXT_SESSION_MISSING",
                    dataset="xnys_calendar",
                    session_date=required_next.date().isoformat(),
                    detail="calendar must retain the execution session after requested end",
                )
            )
        if pd.Timestamp(start_date) < observed_sessions.min():
            gaps.append(
                MarketPreparationGap(
                    code="FROZEN_XNYS_WARMUP_START_MISSING",
                    dataset="xnys_calendar",
                    session_date=start_date.isoformat(),
                    detail="requested range starts before the frozen calendar",
                )
            )
        return value, fetch_start, fetch_end

    @staticmethod
    def _normalize_aliases(
        frame: pd.DataFrame, gaps: list[MarketPreparationGap]
    ) -> tuple[pd.DataFrame, list[dict[str, object]]]:
        value = frame.copy()
        value["security_id"] = value["security_id"].astype(str).str.strip()
        value["vendor_code"] = value["vendor_code"].astype(str).str.upper().str.strip()
        value["exchange"] = value["exchange"].astype(str).str.upper().str.strip()
        value["valid_from"] = pd.to_datetime(value["valid_from"], errors="coerce").dt.normalize()
        value["valid_to"] = pd.to_datetime(value["valid_to"], errors="coerce").dt.normalize()
        invalid = (
            value["security_id"].eq("")
            | value["vendor_code"].eq("")
            | ~value["exchange"].isin({"XNYS", "XNAS", "ARCX", "BATS"})
            | value["valid_from"].isna()
        ) | (
            value["valid_to"].notna() & (value["valid_to"] < value["valid_from"])
        )
        for row in value.loc[invalid].itertuples(index=False):
            gaps.append(
                MarketPreparationGap(
                    code="INVALID_ALIAS_RANGE",
                    dataset="listing_aliases",
                    security_id=str(row.security_id),
                    vendor_code=str(row.vendor_code),
                    detail="alias validity is missing or reversed",
                )
            )
        value = value.loc[~invalid].copy()
        aliases = list(value.itertuples(index=False))
        collisions: list[dict[str, object]] = []
        for index, left in enumerate(aliases):
            left_end = pd.Timestamp.max.normalize() if pd.isna(left.valid_to) else left.valid_to
            for right in aliases[index + 1 :]:
                if (
                    str(left.vendor_code) != str(right.vendor_code)
                    or str(left.security_id) == str(right.security_id)
                ):
                    continue
                right_end = (
                    pd.Timestamp.max.normalize()
                    if pd.isna(right.valid_to)
                    else right.valid_to
                )
                if max(left.valid_from, right.valid_from) <= min(left_end, right_end):
                    # SCOPE-C (approved): identity variants of the same listed
                    # instrument legitimately share one TDX vendor code (same
                    # ticker).  Both securities keep their alias and consume
                    # the same vendor bars; this is recorded (not blocking) so
                    # the collision surface stays auditable.
                    collisions.append(
                        {
                            "vendor_code": str(left.vendor_code),
                            "security_ids": sorted(
                                {str(left.security_id), str(right.security_id)}
                            ),
                            "detail": (
                                "identity variants share one TDX vendor code; "
                                "vendor bars are shared by both securities"
                            ),
                        }
                    )
        return value, collisions

    @staticmethod
    def _normalize_memberships(
        frame: pd.DataFrame,
        start_date: date,
        end_date: date,
        gaps: list[MarketPreparationGap],
    ) -> pd.DataFrame:
        value = frame.copy()
        value["decision_date"] = pd.to_datetime(
            value["decision_date"], errors="coerce"
        ).dt.normalize()
        value["security_id"] = value["security_id"].astype(str).str.strip()
        invalid = value["decision_date"].isna() | value["security_id"].eq("")
        if invalid.any():
            gaps.append(
                MarketPreparationGap(
                    code="INVALID_MEMBERSHIP_KEY",
                    dataset="membership_monthly",
                    detail=f"invalid decision/security keys: {int(invalid.sum())}",
                )
            )
        duplicate = value.duplicated(
            ["universe_id", "decision_date", "security_id"], keep=False
        )
        if duplicate.any():
            gaps.append(
                MarketPreparationGap(
                    code="DUPLICATE_MEMBERSHIP",
                    dataset="membership_monthly",
                    detail=f"duplicate membership rows: {int(duplicate.sum())}",
                )
            )
        wrong_universe = value["universe_id"].astype(str).ne(UNIVERSE_ID)
        if wrong_universe.any():
            gaps.append(
                MarketPreparationGap(
                    code="WRONG_MEMBERSHIP_UNIVERSE",
                    dataset="membership_monthly",
                    detail="membership rows do not belong to sp500_ivv_proxy_v1",
                )
            )
        window = value["decision_date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
        value = value.loc[window & ~invalid & ~duplicate & ~wrong_universe].copy()
        if value.empty:
            gaps.append(
                MarketPreparationGap(
                    code="NO_MEMBERSHIP_DECISIONS",
                    dataset="membership_monthly",
                    detail="no decision rows fall inside the requested market window",
                )
            )
        return value.sort_values(["decision_date", "security_id"], kind="stable")

    @staticmethod
    def _validate_security_master(
        frame: pd.DataFrame,
        required_ids: list[str],
        gaps: list[MarketPreparationGap],
    ) -> None:
        master_ids = set(frame.get("security_id", pd.Series(dtype=str)).astype(str))
        for security_id in sorted(set(required_ids) - master_ids):
            gaps.append(
                MarketPreparationGap(
                    code="MISSING_SECURITY_MASTER",
                    dataset="security_master",
                    security_id=security_id,
                    detail="membership security has no stable security master row",
                )
            )

    @staticmethod
    def _validate_membership_calendar(
        memberships: pd.DataFrame,
        calendar: pd.DataFrame,
        start_date: date,
        end_date: date,
        gaps: list[MarketPreparationGap],
    ) -> None:
        if memberships.empty or calendar.empty:
            return
        sessions = pd.DatetimeIndex(
            pd.to_datetime(calendar["session_date"], errors="coerce")
        ).normalize()
        decisions = pd.DatetimeIndex(
            memberships["decision_date"].dropna().drop_duplicates().sort_values()
        )
        window = sessions[
            (sessions >= pd.Timestamp(start_date))
            & (sessions <= pd.Timestamp(end_date))
        ]
        expected = pd.DatetimeIndex(
            [
                pd.Timestamp(group.max()).normalize()
                for _, group in pd.Series(window, index=window).groupby(
                    window.to_period("M")
                )
            ]
        )
        if len(decisions) < 60:
            gaps.append(
                MarketPreparationGap(
                    code="DECISION_MONTH_COUNT_INVALID",
                    dataset="membership_monthly",
                    detail=f"expected at least 60 decision months, observed {len(decisions)}",
                )
            )
        if not decisions.equals(expected):
            gaps.append(
                MarketPreparationGap(
                    code="DECISION_MONTH_ENDS_INVALID",
                    dataset="membership_monthly",
                    detail="decisions are not every actual XNYS month end in the window",
                )
            )
        warmup = int((sessions < decisions.min()).sum()) if len(decisions) else 0
        if warmup < 282:
            gaps.append(
                MarketPreparationGap(
                    code="INSUFFICIENT_SIGNAL_WARMUP",
                    dataset="xnys_calendar",
                    detail=f"expected at least 282 sessions, observed {warmup}",
                )
            )

    @staticmethod
    def _required_vendor_codes(
        aliases: pd.DataFrame,
        security_ids: list[str],
        start_date: date,
        end_date: date,
        gaps: list[MarketPreparationGap],
    ) -> list[str]:
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        codes: set[str] = set()
        for security_id in security_ids:
            rows = aliases.loc[
                aliases["security_id"].eq(security_id)
                & (aliases["valid_from"] <= end)
                & (aliases["valid_to"].isna() | (aliases["valid_to"] >= start))
            ]
            if rows.empty:
                gaps.append(
                    MarketPreparationGap(
                        code="MISSING_VENDOR_ALIAS",
                        dataset="listing_aliases",
                        security_id=security_id,
                        detail="no reviewed TDX alias overlaps the requested window",
                    )
                )
                continue
            for code in rows["vendor_code"].astype(str):
                if not code.endswith(".US"):
                    gaps.append(
                        MarketPreparationGap(
                            code="INVALID_US_VENDOR_CODE",
                            dataset="listing_aliases",
                            security_id=security_id,
                            vendor_code=code,
                            detail="TDX US aliases must use the .US suffix",
                        )
                    )
                    continue
                codes.add(code)
        return sorted(codes)

    def _fetch_vendor_bars(
        self,
        codes: list[str],
        start_date: date,
        end_date: date,
        count: int,
        *,
        adjustment: str,
        dataset: str,
        gaps: list[MarketPreparationGap],
    ) -> dict[str, pd.DataFrame]:
        if not codes:
            return {}, ()
        try:
            if self.provider is None:
                raise RuntimeError(
                    "TDX provider is unavailable after the reviewed workspace passed its gate"
                )
            evidence_fetch = getattr(self.provider, "fetch_bars_evidence", None)
            if callable(evidence_fetch):
                values, envelopes = evidence_fetch(
                    codes,
                    "1d",
                    count,
                    fields=BAR_FIELDS,
                    dividend_type=adjustment,
                    start_time=start_date.isoformat(),
                    end_time=end_date.isoformat(),
                    warmup_bars=0,
                )
            elif self.allow_test_provider_capture:
                values = self.provider.fetch_bars(
                    codes,
                    "1d",
                    count,
                    fields=BAR_FIELDS,
                    dividend_type=adjustment,
                    start_time=start_date.isoformat(),
                    end_time=end_date.isoformat(),
                    warmup_bars=0,
                )
                envelopes = ()
            else:
                raise ValueError(
                    "production market preparation requires fetch_bars_evidence raw envelopes"
                )
        except Exception as exc:
            gaps.append(
                MarketPreparationGap(
                    code="TDX_FETCH_FAILED",
                    dataset=dataset,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            return {}, ()
        result: dict[str, pd.DataFrame] = {}
        for code in codes:
            frame = values.get(code)
            if frame is None or frame.empty:
                gaps.append(
                    MarketPreparationGap(
                        code="TDX_CODE_UNAVAILABLE",
                        dataset=dataset,
                        vendor_code=code,
                        detail="TDX returned no daily rows; no substitute was used",
                    )
                )
                continue
            result[code] = frame.copy()
        return result, tuple(envelopes)

    @classmethod
    def _normalize_vendor_frames(
        cls,
        values: Mapping[str, pd.DataFrame],
        dataset: str,
        gaps: list[MarketPreparationGap],
    ) -> dict[str, pd.DataFrame]:
        return {
            code: cls._normalize_bar_frame(frame, code, dataset, gaps)
            for code, frame in values.items()
        }

    @staticmethod
    def _restrict_to_calendar(
        values: Mapping[str, pd.DataFrame],
        calendar: pd.DataFrame,
        dataset: str,
        gaps: list[MarketPreparationGap],
    ) -> dict[str, pd.DataFrame]:
        valid_sessions = set(
            pd.to_datetime(calendar["session_date"], errors="coerce").dt.normalize()
        )
        result: dict[str, pd.DataFrame] = {}
        for code, frame in values.items():
            extra = ~frame["date"].isin(valid_sessions)
            for day in frame.loc[extra, "date"]:
                gaps.append(
                    MarketPreparationGap(
                        code="TDX_NON_XNYS_SESSION",
                        dataset=dataset,
                        vendor_code=code,
                        session_date=pd.Timestamp(day).date().isoformat(),
                        detail="provider row is outside the frozen XNYS calendar and was rejected",
                    )
                )
            result[code] = frame.loc[~extra].copy()
        return result

    @staticmethod
    def _scope_c_quality_exclusions(
        raw_values: Mapping[str, pd.DataFrame],
        front_values: Mapping[str, pd.DataFrame],
        aliases: pd.DataFrame,
        security_ids: list[str],
        calendar: pd.DataFrame,
        session_exceptions: pd.DataFrame,
        fetch_start: date,
        fetch_end: date,
        raw_capture_sha256: str,
        front_capture_sha256: str,
        vendor_gaps: list[MarketPreparationGap],
    ) -> tuple[set[str], set[str], list[dict[str, Any]]]:
        """Exclude incomplete TDX instruments as an auditable SCOPE-C subset.

        The exclusion is code-wide and never repairs data.  Benchmark codes are
        intentionally outside this mechanism and remain fail-closed.
        """

        member_ids = set(str(item) for item in security_ids)
        alias_value = aliases.loc[
            aliases["security_id"].astype(str).isin(member_ids)
        ].copy()
        if alias_value.empty:
            return set(), set(), []
        alias_value["vendor_code"] = (
            alias_value["vendor_code"].astype(str).str.upper()
        )
        alias_value["valid_from"] = pd.to_datetime(
            alias_value["valid_from"], errors="coerce"
        ).dt.normalize()
        alias_value["valid_to"] = pd.to_datetime(
            alias_value["valid_to"], errors="coerce"
        ).dt.normalize()
        sessions = pd.DatetimeIndex(
            pd.to_datetime(calendar["session_date"], errors="coerce")
        ).normalize()
        sessions = sessions[
            (sessions >= pd.Timestamp(fetch_start))
            & (sessions <= pd.Timestamp(fetch_end))
        ]
        exception_keys: set[tuple[str, pd.Timestamp]] = set()
        if not session_exceptions.empty:
            exception_days = pd.to_datetime(
                session_exceptions.get("session_date"), errors="coerce"
            ).dt.normalize()
            verified = session_exceptions.get(
                "verified", pd.Series(False, index=session_exceptions.index)
            ).map(_truthy)
            supported = session_exceptions.get(
                "exception_type", pd.Series("", index=session_exceptions.index)
            ).astype(str).str.upper().isin({"HALTED", "NO_TRADE"})
            for sid, day in zip(
                session_exceptions.loc[verified & supported, "security_id"].astype(str),
                exception_days.loc[verified & supported],
                strict=True,
            ):
                if not pd.isna(day):
                    exception_keys.add((sid, pd.Timestamp(day)))
        benchmark_codes = set(BENCHMARK_CODES.values())
        excluded_codes: set[str] = set()
        excluded_ids: set[str] = set()
        records: list[dict[str, Any]] = []
        for code, group in alias_value.groupby("vendor_code", sort=True):
            code = str(code).upper()
            if code in benchmark_codes:
                continue
            required: set[pd.Timestamp] = set()
            for alias in group.itertuples(index=False):
                lower = max(pd.Timestamp(fetch_start), pd.Timestamp(alias.valid_from))
                upper = (
                    pd.Timestamp(fetch_end)
                    if pd.isna(alias.valid_to)
                    else min(pd.Timestamp(fetch_end), pd.Timestamp(alias.valid_to))
                )
                if lower <= upper:
                    required.update(sessions[(sessions >= lower) & (sessions <= upper)])
            if not required:
                continue
            raw_dates = set(
                pd.to_datetime(
                    raw_values.get(code, pd.DataFrame()).get(
                        "date", pd.Series(dtype="datetime64[ns]")
                    ),
                    errors="coerce",
                ).dropna().dt.normalize()
            )
            front_dates = set(
                pd.to_datetime(
                    front_values.get(code, pd.DataFrame()).get(
                        "date", pd.Series(dtype="datetime64[ns]")
                    ),
                    errors="coerce",
                ).dropna().dt.normalize()
            )
            def _explained(day: pd.Timestamp) -> bool:
                active = group.loc[
                    (group["valid_from"] <= day)
                    & (group["valid_to"].isna() | (group["valid_to"] >= day)),
                    "security_id",
                ].astype(str)
                return bool(len(active)) and all(
                    (sid, day) in exception_keys for sid in set(active)
                )

            missing_raw = {
                day for day in required - raw_dates if not _explained(day)
            }
            missing_front = {
                day for day in required - front_dates if not _explained(day)
            }
            issues: list[MarketPreparationGap] = []
            for gap in vendor_gaps:
                if (
                    gap.vendor_code is None
                    or gap.vendor_code.upper() != code
                    or gap.dataset not in {"bars_raw", "bars_vendor_front"}
                ):
                    continue
                if gap.session_date is None:
                    issues.append(gap)
                    continue
                day = pd.to_datetime(gap.session_date, errors="coerce")
                normalized_day = (
                    None if pd.isna(day) else pd.Timestamp(day).normalize()
                )
                if (
                    normalized_day is not None
                    and normalized_day in required
                    and not _explained(normalized_day)
                ):
                    issues.append(gap)
            if not missing_raw and not missing_front and not issues:
                continue
            reason_counts = Counter(item.code for item in issues)
            if missing_raw:
                reason_counts["RAW_REQUIRED_SESSION_MISSING"] += len(missing_raw)
            if missing_front:
                reason_counts["FRONT_REQUIRED_SESSION_MISSING"] += len(missing_front)
            problem_days = set(missing_raw) | set(missing_front)
            for item in issues:
                if item.session_date is not None:
                    day = pd.to_datetime(item.session_date, errors="coerce")
                    if not pd.isna(day):
                        problem_days.add(pd.Timestamp(day).normalize())
            security_values = sorted(set(group["security_id"].astype(str)))
            excluded_codes.add(code)
            excluded_ids.update(security_values)
            records.append(
                {
                    "vendor_code": code,
                    "security_ids": security_values,
                    "detail": (
                        "TDX required history is incomplete or invalid; all mapped "
                        "securities excluded without filling per SCOPE-C-QUALITY-v1"
                    ),
                    "rule_version": "SCOPE-C-QUALITY-v1",
                    "reason_counts": dict(sorted(reason_counts.items())),
                    "first_problem_session": (
                        min(problem_days).date().isoformat() if problem_days else None
                    ),
                    "last_problem_session": (
                        max(problem_days).date().isoformat() if problem_days else None
                    ),
                    "raw_capture_sha256": raw_capture_sha256,
                    "front_capture_sha256": front_capture_sha256,
                }
            )
        return excluded_codes, excluded_ids, records

    @staticmethod
    def _scope_c_filtered_passthrough(
        inputs: Mapping[str, pd.DataFrame],
        memberships: pd.DataFrame,
        excluded_security_ids: set[str],
    ) -> dict[str, pd.DataFrame]:
        """Build the final tradable subset without weakening downstream gates."""

        excluded = {str(item) for item in excluded_security_ids}
        result = {name: frame.copy() for name, frame in inputs.items()}
        result["membership_monthly"] = memberships.copy()
        # Keep the complete evidence graph for causal replay.  Removing one
        # side of an historical ADD/REMOVE or identity-succession pair changes
        # the replay itself.  Scope-C is a projection of the replayed state,
        # not a mutation of source evidence.  Only per-security exceptions and
        # the replaced lifecycle output are safe to filter here.
        for dataset in ("session_exceptions", "lifecycle_reconciliations"):
            frame = result.get(dataset)
            if frame is None or frame.empty or "security_id" not in frame.columns:
                continue
            result[dataset] = frame.loc[
                ~frame["security_id"].astype(str).isin(excluded)
            ].copy()

        holdings = result.get("fund_holdings_observed", pd.DataFrame())
        anchors = result.get("anchor_reconciliations", pd.DataFrame())
        if not holdings.empty and not anchors.empty and not memberships.empty:
            scope_security_ids = set(memberships["security_id"].astype(str))
            events = result.get("membership_events", pd.DataFrame()).copy()
            if not events.empty:
                events["effective_day"] = pd.to_datetime(
                    events["effective_at"], errors="coerce", utc=True
                ).map(
                    lambda value: (
                        pd.NaT
                        if pd.isna(value)
                        else pd.Timestamp(value)
                        .tz_convert(NEW_YORK)
                        .tz_localize(None)
                        .normalize()
                    )
                )
            actions = result.get("corporate_actions", pd.DataFrame())
            spinoff_successors = (
                set(
                    actions.loc[
                        actions["action_type"]
                        .astype(str)
                        .str.strip()
                        .str.upper()
                        .eq("SPINOFF"),
                        "successor_security_id",
                    ].dropna().astype(str)
                )
                if not actions.empty and "successor_security_id" in actions.columns
                else set()
            )

            def explained_residual(security_id: str, anchor_day: pd.Timestamp) -> bool:
                if events.empty:
                    return False
                matching = events.loc[
                    events["security_id"].astype(str).eq(security_id)
                    & events["effective_day"].notna()
                    & events["effective_day"].le(anchor_day)
                ].sort_values(["effective_day", "event_id"], kind="stable")
                latest_kind = (
                    ""
                    if matching.empty
                    else str(matching.iloc[-1]["event_type"]).strip().upper()
                )
                if latest_kind == "REMOVE":
                    return True
                if security_id in spinoff_successors:
                    return not matching["event_type"].astype(str).str.upper().eq(
                        "ADD"
                    ).any()
                return False
            decisions = pd.to_datetime(
                memberships["decision_date"], errors="raise"
            ).dt.normalize()
            rebuilt: list[dict[str, Any]] = []
            validation = holdings.loc[
                holdings["evidence_role"].astype(str).eq(
                    SourceRole.VALIDATION_ANCHOR.value
                )
                & holdings["source_id"].astype(str).eq("sec_nport_ivv")
            ]
            for digest, group in validation.groupby(
                "content_sha256", sort=True
            ):
                anchor_date = pd.to_datetime(
                    group["as_of_date"].iloc[0], errors="raise"
                ).normalize()
                same_month = sorted(
                    set(
                        decisions[
                            decisions.dt.to_period("M").eq(
                                anchor_date.to_period("M")
                            )
                        ]
                    )
                )
                if not same_month:
                    continue
                replay = set(
                    memberships.loc[
                        decisions.eq(same_month[-1]), "security_id"
                    ].astype(str)
                )
                observed = set(group["security_id"].astype(str)) & scope_security_ids
                unexplained_additions = {
                    security_id
                    for security_id in observed - replay
                    if not explained_residual(security_id, anchor_date)
                }
                unexplained_removals = replay - observed
                rebuilt.append(
                    {
                        "anchor_date": anchor_date,
                        "evidence_sha256": str(digest),
                        "source_id": str(group["source_id"].iloc[0]),
                        "status": (
                            "RECONCILED"
                            if not unexplained_additions and not unexplained_removals
                            else "UNRESOLVED"
                        ),
                        "unexplained_additions": len(unexplained_additions),
                        "unexplained_removals": len(unexplained_removals),
                    }
                )
            result["anchor_reconciliations"] = pd.DataFrame(
                rebuilt,
                columns=list(anchors.columns),
            ).drop_duplicates(["anchor_date", "evidence_sha256"])
        return result

    @staticmethod
    def _normalize_bar_frame(
        frame: pd.DataFrame,
        code: str,
        dataset: str,
        gaps: list[MarketPreparationGap],
    ) -> pd.DataFrame:
        value = frame.copy()
        index = pd.DatetimeIndex(pd.to_datetime(value.index, errors="coerce"))
        if index.tz is not None:
            index = index.tz_convert(NEW_YORK).tz_localize(None)
        value["date"] = index.normalize()
        missing_columns = sorted(set(PRICE_COLUMNS + ("Volume",)) - set(value.columns))
        if missing_columns:
            gaps.append(
                MarketPreparationGap(
                    code="TDX_BAR_COLUMNS_MISSING",
                    dataset=dataset,
                    vendor_code=code,
                    detail="missing columns: " + ", ".join(missing_columns),
                )
            )
            for column in missing_columns:
                value[column] = math.nan
        duplicates = value["date"].duplicated(keep=False)
        if duplicates.any():
            gaps.append(
                MarketPreparationGap(
                    code="TDX_DUPLICATE_DAILY_BAR",
                    dataset=dataset,
                    vendor_code=code,
                    detail=f"duplicate sessions: {int(duplicates.sum())}",
                )
            )
            value = value.loc[~duplicates].copy()
        numeric = value[list(PRICE_COLUMNS) + ["Volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        invalid = value["date"].isna() | numeric[list(PRICE_COLUMNS)].isna().any(axis=1)
        invalid |= (numeric[list(PRICE_COLUMNS)] <= 0).any(axis=1)
        invalid |= numeric["Volume"].isna() | (numeric["Volume"] < 0)
        invalid |= numeric["High"] < numeric[list(PRICE_COLUMNS)].max(axis=1)
        invalid |= numeric["Low"] > numeric[list(PRICE_COLUMNS)].min(axis=1)
        for row in value.loc[invalid].itertuples(index=False):
            gaps.append(
                MarketPreparationGap(
                    code="TDX_INVALID_OHLCV",
                    dataset=dataset,
                    vendor_code=code,
                    session_date=(
                        None if pd.isna(row.date) else pd.Timestamp(row.date).date().isoformat()
                    ),
                    detail="invalid daily OHLCV row was rejected as a blocked gap, not filled",
                )
            )
        value = value.loc[~invalid].copy()
        for column in PRICE_COLUMNS + ("Volume",):
            value[column] = pd.to_numeric(value[column], errors="coerce")
        if "Amount" in value.columns:
            value["Amount"] = pd.to_numeric(value["Amount"], errors="coerce")
        columns = ["date", *PRICE_COLUMNS, "Volume"]
        if "Amount" in value.columns:
            columns.append("Amount")
        return value[columns].sort_values("date", kind="stable").reset_index(drop=True)

    @staticmethod
    def _vendor_capture_frame(values: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
        parts: list[pd.DataFrame] = []
        for code in sorted(values):
            part = values[code].copy()
            part.insert(0, "source_index", list(part.index))
            part = part.reset_index(drop=True)
            part.insert(0, "vendor_code", code)
            parts.append(part)
        if not parts:
            return pd.DataFrame(columns=["vendor_code", "source_index", *BAR_FIELDS])
        all_columns = sorted(
            set().union(*(set(part.columns) for part in parts))
            - {"vendor_code", "source_index"}
        )
        return pd.concat(parts, ignore_index=True, sort=False)[
            ["vendor_code", "source_index", *all_columns]
        ]

    @staticmethod
    def _map_to_stable_ids(
        vendor_bars: Mapping[str, pd.DataFrame],
        aliases: pd.DataFrame,
        security_ids: list[str],
        start_date: date,
        end_date: date,
        dataset: str,
        gaps: list[MarketPreparationGap],
    ) -> pd.DataFrame:
        parts: list[pd.DataFrame] = []
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        for alias in aliases.loc[aliases["security_id"].isin(security_ids)].itertuples(
            index=False
        ):
            code = str(alias.vendor_code)
            frame = vendor_bars.get(code)
            if frame is None:
                continue
            valid_to = end if pd.isna(alias.valid_to) else min(end, pd.Timestamp(alias.valid_to))
            valid_from = max(start, pd.Timestamp(alias.valid_from))
            part = frame.loc[frame["date"].between(valid_from, valid_to)].copy()
            if part.empty:
                continue
            part.insert(0, "security_id", str(alias.security_id))
            parts.append(part)
        columns = ["security_id", "date", *PRICE_COLUMNS, "Volume", "Amount"]
        if not parts:
            return pd.DataFrame(columns=columns)
        value = pd.concat(parts, ignore_index=True)
        if "Amount" not in value.columns:
            value["Amount"] = math.nan
        duplicate = value.duplicated(["security_id", "date"], keep=False)
        for row in value.loc[duplicate, ["security_id", "date"]].drop_duplicates().itertuples(
            index=False
        ):
            gaps.append(
                MarketPreparationGap(
                    code="AMBIGUOUS_VENDOR_ALIAS",
                    dataset=dataset,
                    security_id=str(row.security_id),
                    session_date=pd.Timestamp(row.date).date().isoformat(),
                    detail="multiple active aliases map to the same stable security session",
                )
            )
        value = value.loc[~duplicate, columns]
        return value.sort_values(["security_id", "date"], kind="stable").reset_index(drop=True)

    def _build_lifecycle_reconciliations(
        self,
        memberships: pd.DataFrame,
        corporate_actions: pd.DataFrame,
        aliases: pd.DataFrame,
        pool_codes: set[str],
        pool_mapping: Mapping[str, str],
        pool_dependency: SourceDependency,
        observed_at: datetime,
        as_of_date: date,
        gaps: list[MarketPreparationGap],
    ) -> tuple[pd.DataFrame, SourceDependency | None]:
        """Bind final-scope lifecycle surveillance to the frozen TDX pool.

        The pool proves only that a vendor listing is present at capture time.
        Historical terminal states continue to require their reviewed corporate
        action; a spinoff is deliberately not treated as termination of the
        parent security.
        """

        columns = sorted(REQUIRED_ARTIFACT_COLUMNS["lifecycle_reconciliations"])
        members = sorted(
            set(
                memberships.get("security_id", pd.Series(dtype="object"))
                .dropna()
                .astype(str)
                .str.strip()
            )
        )
        if not members:
            return pd.DataFrame(columns=columns), None

        terminal_types = {
            "CASH_MERGER",
            "STOCK_MERGER",
            "DELISTING",
            "BANKRUPTCY",
        }
        actions = corporate_actions.copy()
        if actions.empty:
            terminal = actions
        else:
            terminal = actions.loc[
                actions["security_id"].astype(str).isin(members)
                & actions["action_type"]
                .astype(str)
                .str.strip()
                .str.upper()
                .isin(terminal_types)
            ].copy()
        terminal_by_security = {
            str(security_id): group.copy()
            for security_id, group in terminal.groupby("security_id", sort=False)
        }
        identity_types = {
            "TICKER_CHANGE",
            "RENAME",
            "SPLIT",
            "STOCK_DIVIDEND",
            "REORGANIZATION",
        }
        identity = (
            actions.loc[
                actions["security_id"].astype(str).isin(members)
                & actions["action_type"]
                .astype(str)
                .str.strip()
                .str.upper()
                .isin(identity_types)
            ].copy()
            if not actions.empty
            else actions
        )
        identity_by_security = {
            str(security_id): group.copy()
            for security_id, group in identity.groupby("security_id", sort=False)
        }

        cutoff = pd.Timestamp(as_of_date).normalize()
        alias_value = aliases.copy()
        alias_value["valid_from"] = pd.to_datetime(
            alias_value["valid_from"], errors="coerce"
        ).dt.normalize()
        alias_value["valid_to"] = pd.to_datetime(
            alias_value["valid_to"], errors="coerce"
        ).dt.normalize()
        alias_value["vendor_code"] = (
            alias_value["vendor_code"].astype(str).str.strip().str.upper()
        )

        observations: list[dict[str, str]] = []
        terminal_rows: list[dict[str, Any]] = []
        nonterminal_members: list[str] = []
        for security_id in members:
            terminal_rows_for_security = terminal_by_security.get(security_id)
            if terminal_rows_for_security is not None:
                if len(terminal_rows_for_security) != 1:
                    gaps.append(
                        MarketPreparationGap(
                            code="LIFECYCLE_TERMINAL_ACTION_AMBIGUOUS",
                            dataset="lifecycle_reconciliations",
                            security_id=security_id,
                            detail=(
                                "one final-scope security has multiple terminal "
                                "actions and cannot be represented by one reconciliation"
                            ),
                        )
                    )
                    continue
                action = terminal_rows_for_security.iloc[0]
                effective = pd.to_datetime(action.get("effective_at"), errors="coerce")
                if pd.isna(effective):
                    gaps.append(
                        MarketPreparationGap(
                            code="LIFECYCLE_TERMINAL_ACTION_TIME_INVALID",
                            dataset="lifecycle_reconciliations",
                            security_id=security_id,
                            detail="terminal action has no parseable effective timestamp",
                        )
                    )
                    continue
                if effective.tzinfo is not None:
                    effective = effective.tz_convert(NEW_YORK).tz_localize(None)
                terminal_rows.append(
                    {
                        "scope": "SECURITY",
                        "coverage_kind": "TERMINAL_ACTION",
                        "current_through": effective.normalize(),
                        "action_id": str(action.get("action_id", "")).strip(),
                        "security_id": security_id,
                        "status": "RECONCILED",
                        "includes_delisted": True,
                        "source_id": str(action.get("source_id", "")).strip(),
                        "evidence_sha256": str(
                            action.get("evidence_sha256", "")
                        ).strip(),
                    }
                )
                continue

            active = alias_value.loc[
                alias_value["security_id"].astype(str).eq(security_id)
                & alias_value["valid_from"].notna()
                & alias_value["valid_from"].le(cutoff)
                & (alias_value["valid_to"].isna() | alias_value["valid_to"].ge(cutoff))
                & alias_value["vendor_code"].isin(pool_codes)
            ]
            if len(active) != 1:
                lineage = identity_by_security.get(security_id)
                if lineage is not None and len(lineage) == 1:
                    action = lineage.iloc[0]
                    successor = str(
                        action.get("successor_security_id", "")
                    ).strip()
                    successor_active = alias_value.loc[
                        alias_value["security_id"].astype(str).eq(successor)
                        & alias_value["valid_from"].notna()
                        & alias_value["valid_from"].le(cutoff)
                        & (
                            alias_value["valid_to"].isna()
                            | alias_value["valid_to"].ge(cutoff)
                        )
                        & alias_value["vendor_code"].isin(pool_codes)
                    ]
                    if successor.startswith("us_") and len(successor_active) == 1:
                        terminal_rows.append(
                            {
                                "scope": "SECURITY",
                                "coverage_kind": "IDENTITY_SUCCESSION",
                                "current_through": cutoff,
                                "action_id": str(
                                    action.get("action_id", "")
                                ).strip(),
                                "security_id": security_id,
                                "status": "RECONCILED",
                                "includes_delisted": True,
                                "source_id": str(
                                    action.get("source_id", "")
                                ).strip(),
                                "evidence_sha256": str(
                                    action.get("evidence_sha256", "")
                                ).strip(),
                            }
                        )
                        continue
                gaps.append(
                    MarketPreparationGap(
                        code="LIFECYCLE_CURRENT_LISTING_UNPROVEN",
                        dataset="lifecycle_reconciliations",
                        security_id=security_id,
                        detail=(
                            "TDX pool capture requires exactly one final-date active "
                            f"reviewed alias; found {len(active)}"
                        ),
                    )
                )
                continue
            alias = active.iloc[0]
            vendor_code = str(alias["vendor_code"]).strip().upper()
            excerpt = f"{vendor_code}|{pool_mapping.get(vendor_code, '')}"[:500]
            observations.append(
                {
                    "security_id": security_id,
                    "identifier_type": "VENDOR_CODE",
                    "identifier_value": re.sub(r"[^A-Z0-9]", "", vendor_code),
                    "observed_status": "LISTED",
                    "evidence_locator": f"codes:{vendor_code}",
                    "observed_through": as_of_date.isoformat(),
                    "status_effective_at": "",
                    "evidence_excerpt": excerpt,
                }
            )
            nonterminal_members.append(security_id)

        lifecycle_dependency: SourceDependency | None = None
        status_rows: list[dict[str, Any]] = []
        if observations:
            observations.sort(
                key=lambda item: (
                    item["security_id"],
                    item["identifier_type"],
                    item["identifier_value"],
                )
            )
            published_at = str(pool_dependency.published_at or "")
            source_records = [
                {
                    "source_id": pool_dependency.source_id,
                    "dataset": pool_dependency.dataset,
                    "evidence_sha256": pool_dependency.object_sha256,
                    "published_at": published_at,
                    "url": pool_dependency.url,
                    "observations": observations,
                }
            ]
            evidence_payload = {
                "format_version": "tdx-lifecycle-status-v2",
                "current_through": as_of_date.isoformat(),
                "source_records": source_records,
            }
            evidence_ref = self.store.put_bytes(
                canonical_json_bytes(evidence_payload), media_type="application/json"
            )
            covered = sorted(nonterminal_members)
            lifecycle_dependency = self._dependency(
                source_id="tdx_lifecycle_status_v2",
                dataset="lifecycle_status",
                object_sha256=evidence_ref.sha256,
                observed_at=observed_at,
                published_at=observed_at,
                as_of_date=as_of_date,
                url="tdx://derived/lifecycle-status-v2",
                license_class=LicenseClass.LOCAL_VENDOR,
                source_version="TDX-LIFECYCLE-v2",
                metadata={
                    "coverage_contract_version": 4,
                    "coverage_kind": "TERMINATION_SURVEILLANCE",
                    "current_through": as_of_date.isoformat(),
                    "covered_security_ids": covered,
                    "covered_security_ids_sha256": sha256_json(covered),
                    "covered_security_count": len(covered),
                    "source_records": source_records,
                    "source_records_sha256": sha256_json(source_records),
                    "source_record_count": len(source_records),
                    "source_dependency_object_sha256s": [
                        pool_dependency.object_sha256
                    ],
                    "coverage_derived_from_payload": True,
                    "source_records_bound_to_cas": True,
                    "observation_identifiers_verified_in_payload": True,
                    "spinoff_is_not_terminal": True,
                },
                role=SourceRole.VALIDATION_ANCHOR,
            )
            status_rows = [
                {
                    "scope": "SECURITY",
                    "coverage_kind": "STATUS_SURVEILLANCE",
                    "current_through": cutoff,
                    "action_id": "",
                    "security_id": security_id,
                    "status": "RECONCILED",
                    "includes_delisted": True,
                    "source_id": lifecycle_dependency.source_id,
                    "evidence_sha256": lifecycle_dependency.object_sha256,
                }
                for security_id in covered
            ]

        result = pd.DataFrame([*terminal_rows, *status_rows], columns=columns)
        return result, lifecycle_dependency

    def _derive_spinoff_basis(
        self,
        corporate_actions: pd.DataFrame,
        raw: pd.DataFrame,
        aliases: pd.DataFrame,
        calendar: pd.DataFrame,
        included_security_ids: list[str],
        raw_capture_sha256: str,
        observed_at: datetime,
        as_of_date: date,
        gaps: list[MarketPreparationGap],
    ) -> tuple[pd.DataFrame, SourceDependency | None]:
        """Derive causal spinoff basis from the first joint raw trading session."""

        actions = corporate_actions.copy()
        for column in (
            "cost_basis_derivation_method",
            "cost_basis_available_at",
            "cost_basis_evidence_source_id",
            "cost_basis_evidence_sha256",
            "cost_basis_parent_vendor_code",
            "cost_basis_successor_vendor_code",
            "cost_basis_parent_vwap",
            "cost_basis_successor_vwap",
            "cost_basis_raw_capture_sha256",
        ):
            if column not in actions.columns:
                actions[column] = None
        if actions.empty or raw.empty:
            return actions, None

        included = set(str(item) for item in included_security_ids)
        raw_value = raw.copy()
        raw_value["date"] = pd.to_datetime(
            raw_value["date"], errors="coerce"
        ).dt.normalize()
        alias_value = aliases.copy()
        alias_value["valid_from"] = pd.to_datetime(
            alias_value["valid_from"], errors="coerce"
        ).dt.normalize()
        alias_value["valid_to"] = pd.to_datetime(
            alias_value["valid_to"], errors="coerce"
        ).dt.normalize()
        close_by_day = {
            pd.Timestamp(row.session_date).normalize(): pd.Timestamp(
                row.market_close
            ).tz_convert(timezone.utc)
            for row in calendar.itertuples(index=False)
        }

        def active_vendor_code(security_id: str, day: pd.Timestamp) -> str | None:
            matched = alias_value.loc[
                alias_value["security_id"].astype(str).eq(security_id)
                & alias_value["valid_from"].le(day)
                & (
                    alias_value["valid_to"].isna()
                    | alias_value["valid_to"].ge(day)
                ),
                "vendor_code",
            ].astype(str)
            values = sorted(set(matched))
            return values[0] if len(values) == 1 else None

        records: list[dict[str, Any]] = []
        row_indexes: list[int] = []
        spinoff_mask = actions["action_type"].astype(str).str.strip().str.upper().eq(
            "SPINOFF"
        )
        for index, action in actions.loc[spinoff_mask].iterrows():
            parent = str(action.get("security_id", "")).strip()
            if parent not in included:
                continue
            existing = _nonnegative_term(action, ("cost_basis_fraction",))
            if existing is not None:
                continue
            successor = str(action.get("successor_security_id", "")).strip()
            ratio = _positive_term(action, ("share_ratio", "ratio"))
            effective = pd.to_datetime(
                action.get("effective_at"), errors="coerce", utc=True
            )
            if pd.isna(effective):
                continue
            effective_day = (
                pd.Timestamp(effective)
                .tz_convert(NEW_YORK)
                .tz_localize(None)
                .normalize()
            )
            parent_rows = raw_value.loc[
                raw_value["security_id"].astype(str).eq(parent)
                & raw_value["date"].ge(effective_day)
            ]
            successor_rows = raw_value.loc[
                raw_value["security_id"].astype(str).eq(successor)
                & raw_value["date"].ge(effective_day)
            ]
            common = sorted(
                set(parent_rows["date"].dropna())
                & set(successor_rows["date"].dropna())
            )
            if ratio is None or not successor.startswith("us_") or not common:
                continue
            joint_day = pd.Timestamp(common[0]).normalize()
            parent_row = parent_rows.loc[parent_rows["date"].eq(joint_day)]
            successor_row = successor_rows.loc[successor_rows["date"].eq(joint_day)]
            parent_code = active_vendor_code(parent, joint_day)
            successor_code = active_vendor_code(successor, joint_day)
            market_close = close_by_day.get(joint_day)
            if (
                len(parent_row) != 1
                or len(successor_row) != 1
                or parent_code is None
                or successor_code is None
                or market_close is None
            ):
                continue
            parent_record = parent_row.iloc[0]
            successor_record = successor_row.iloc[0]
            parent_amount = pd.to_numeric(
                parent_record.get("Amount"), errors="coerce"
            )
            parent_volume = pd.to_numeric(
                parent_record.get("Volume"), errors="coerce"
            )
            successor_amount = pd.to_numeric(
                successor_record.get("Amount"), errors="coerce"
            )
            successor_volume = pd.to_numeric(
                successor_record.get("Volume"), errors="coerce"
            )
            values = (
                parent_amount,
                parent_volume,
                successor_amount,
                successor_volume,
            )
            if any(not math.isfinite(float(value)) or float(value) <= 0 for value in values):
                continue
            parent_vwap = float(parent_amount) * 10_000.0 / float(parent_volume)
            successor_vwap = (
                float(successor_amount) * 10_000.0 / float(successor_volume)
            )
            denominator = parent_vwap + ratio * successor_vwap
            fraction = ratio * successor_vwap / denominator
            if (
                not math.isfinite(parent_vwap)
                or not math.isfinite(successor_vwap)
                or not math.isfinite(fraction)
                or not 0 < fraction < 1
            ):
                continue

            def row_identity(row: pd.Series) -> dict[str, Any]:
                return {
                    "security_id": str(row.get("security_id", "")),
                    "date": pd.Timestamp(row["date"]).date().isoformat(),
                    **{
                        column: float(row[column])
                        for column in (*PRICE_COLUMNS, "Volume", "Amount")
                    },
                }

            parent_identity = row_identity(parent_record)
            successor_identity = row_identity(successor_record)
            records.append(
                {
                    "action_id": str(action.get("action_id", "")),
                    "security_id": parent,
                    "successor_security_id": successor,
                    "share_ratio": ratio,
                    "joint_session": joint_day.date().isoformat(),
                    "available_at": market_close.isoformat(),
                    "parent_vendor_code": parent_code,
                    "successor_vendor_code": successor_code,
                    "parent_vwap": parent_vwap,
                    "successor_vwap": successor_vwap,
                    "cost_basis_fraction": fraction,
                    "parent_raw_row_sha256": sha256_json(parent_identity),
                    "successor_raw_row_sha256": sha256_json(successor_identity),
                    "raw_capture_sha256": raw_capture_sha256,
                    "method": "TDX_JOINT_SESSION_VWAP_RELATIVE_FMV-v2",
                }
            )
            row_indexes.append(int(index))

        if not records:
            return actions, None
        payload = {
            "format_version": "us-pit-causal-spinoff-basis-v2",
            "preregistration": "docs/us-pit-market-readiness-preregistration-v2.md",
            "amount_unit_multiplier": 10_000,
            "records": sorted(records, key=lambda item: item["action_id"]),
        }
        reference = self.store.put_bytes(
            canonical_json_bytes(payload), media_type="application/json"
        )
        by_action = {item["action_id"]: item for item in records}
        for index in row_indexes:
            action_id = str(actions.at[index, "action_id"])
            record = by_action[action_id]
            actions.at[index, "cost_basis_fraction"] = record[
                "cost_basis_fraction"
            ]
            actions.at[index, "cost_basis_derivation_method"] = record["method"]
            actions.at[index, "cost_basis_available_at"] = record["available_at"]
            actions.at[index, "cost_basis_evidence_source_id"] = (
                "tdx_spinoff_basis_v2"
            )
            actions.at[index, "cost_basis_evidence_sha256"] = reference.sha256
            actions.at[index, "cost_basis_parent_vendor_code"] = record[
                "parent_vendor_code"
            ]
            actions.at[index, "cost_basis_successor_vendor_code"] = record[
                "successor_vendor_code"
            ]
            actions.at[index, "cost_basis_parent_vwap"] = record["parent_vwap"]
            actions.at[index, "cost_basis_successor_vwap"] = record[
                "successor_vwap"
            ]
            actions.at[index, "cost_basis_raw_capture_sha256"] = raw_capture_sha256
        dependency = self._dependency(
            source_id="tdx_spinoff_basis_v2",
            dataset="spinoff_basis",
            object_sha256=reference.sha256,
            observed_at=observed_at,
            as_of_date=as_of_date,
            url="tdx://derived/causal-spinoff-basis-v2",
            license_class=LicenseClass.LOCAL_VENDOR,
            source_version="CAUSAL-SPINOFF-BASIS-v2",
            metadata={
                "read_only": True,
                "fill_data": False,
                "raw_capture_sha256": raw_capture_sha256,
                "record_count": len(records),
                "action_ids": sorted(by_action),
                "method": "TDX_JOINT_SESSION_VWAP_RELATIVE_FMV-v2",
                "normalized_payload_sha256": sha256_json(payload),
            },
            role=SourceRole.SIGNAL_INPUT,
        )
        return actions, dependency

    @staticmethod
    def _build_pit_signal_bars(
        raw: pd.DataFrame,
        memberships: pd.DataFrame,
        corporate_actions: pd.DataFrame,
        calendar: pd.DataFrame,
        sources: tuple[SourceDependency, ...],
        gaps: list[MarketPreparationGap],
    ) -> pd.DataFrame:
        output_columns = [
            "decision_date",
            "security_id",
            "date",
            *PRICE_COLUMNS,
            "Volume",
            "Amount",
        ]
        if raw.empty or memberships.empty:
            return pd.DataFrame(columns=output_columns)
        actions = corporate_actions.copy()
        actions["effective_utc"] = pd.to_datetime(
            actions.get("effective_at"), errors="coerce", utc=True
        )
        actions["effective_day"] = (
            actions["effective_utc"]
            .dt.tz_convert(NEW_YORK)
            .dt.tz_localize(None)
            .dt.normalize()
        )
        actions["announced_utc"] = pd.to_datetime(
            actions.get("announced_at"), errors="coerce", utc=True
        )
        successor_ids = actions.get(
            "successor_security_id",
            pd.Series("", index=actions.index, dtype="object"),
        ).fillna("").astype(str)
        dependency_by_key: dict[tuple[str, str], list[SourceDependency]] = {}
        basis_dependency_by_key: dict[
            tuple[str, str], list[SourceDependency]
        ] = {}
        for source in sources:
            if source.dataset == "corporate_actions":
                dependency_by_key.setdefault(
                    (source.source_id, source.object_sha256), []
                ).append(source)
            elif source.dataset == "spinoff_basis":
                basis_dependency_by_key.setdefault(
                    (source.source_id, source.object_sha256), []
                ).append(source)
        close_by_day = {
            pd.Timestamp(row.session_date).normalize(): pd.Timestamp(row.market_close).tz_convert(
                timezone.utc
            )
            for row in calendar.itertuples(index=False)
        }
        import numpy as _np

        # Pre-group raw by security so per-(decision, security) windowing is an
        # O(log n) slice instead of a full-table boolean scan per member.
        raw_by_sid: dict[str, pd.DataFrame] = {}
        if not raw.empty:
            _raw_group = raw.copy()
            _raw_group["_date_n"] = pd.to_datetime(
                _raw_group["date"], errors="coerce"
            ).dt.normalize()
            for sid, group in _raw_group.groupby("security_id", sort=False):
                sorted_group = group.sort_values("_date_n", kind="stable").reset_index(drop=True)
                raw_by_sid[str(sid)] = sorted_group
        parts: list[pd.DataFrame] = []
        for decision, members in memberships.groupby("decision_date", sort=True):
            decision_day = pd.Timestamp(decision).normalize()
            decision_close = close_by_day.get(decision_day)
            if decision_close is None:
                gaps.append(
                    MarketPreparationGap(
                        code="DECISION_NOT_IN_XNYS_CALENDAR",
                        dataset="bars_pit_signal",
                        session_date=decision_day.date().isoformat(),
                        detail="cannot establish the decision-time information cutoff",
                    )
                )
                continue
            for security_id in sorted(set(members["security_id"].astype(str))):
                group = raw_by_sid.get(security_id)
                if group is None or group.empty:
                    series = pd.DataFrame()
                else:
                    cutoff = int(
                        _np.searchsorted(
                            group["_date_n"].to_numpy(),
                            _np.datetime64(decision_day, "ns"),
                            side="right",
                        )
                    )
                    series = group.iloc[:cutoff].drop(columns=["_date_n"]).copy()
                lineage_ids = {security_id}
                lineage_cursor = security_id
                lineage_blocked = False
                while True:
                    predecessor = actions.loc[
                        actions["action_type"].astype(str).str.upper().isin(
                            {"SPLIT", "STOCK_DIVIDEND"}
                        )
                        & successor_ids.eq(lineage_cursor)
                        & actions["effective_day"].notna()
                        & (actions["effective_day"] <= decision_day)
                    ]
                    if predecessor.empty:
                        break
                    if len(predecessor) != 1:
                        gaps.append(
                            MarketPreparationGap(
                                code="AMBIGUOUS_SHARE_RATIO_IDENTITY_CHAIN",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail="successor has multiple eligible share-ratio predecessors",
                            )
                        )
                        lineage_blocked = True
                        break
                    predecessor_action = predecessor.iloc[0]
                    old_id = str(predecessor_action.get("security_id") or "").strip()
                    if not old_id.startswith("us_") or old_id in lineage_ids:
                        gaps.append(
                            MarketPreparationGap(
                                code="INVALID_SHARE_RATIO_IDENTITY_CHAIN",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail="share-ratio predecessor is invalid or cyclic",
                            )
                        )
                        lineage_blocked = True
                        break
                    effective = pd.Timestamp(predecessor_action["effective_day"])
                    predecessor_rows = pd.DataFrame()
                    pred_group = raw_by_sid.get(old_id)
                    if pred_group is not None and not pred_group.empty:
                        pred_cutoff = int(
                            _np.searchsorted(
                                pred_group["_date_n"].to_numpy(),
                                _np.datetime64(effective, "ns"),
                                side="left",
                            )
                        )
                        predecessor_rows = pred_group.iloc[:pred_cutoff].drop(
                            columns=["_date_n"]
                        ).copy()
                    if predecessor_rows.empty:
                        gaps.append(
                            MarketPreparationGap(
                                code="SHARE_RATIO_PREDECESSOR_BARS_MISSING",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail=f"no raw history for predecessor {old_id}",
                            )
                        )
                        lineage_blocked = True
                        break
                    series = pd.concat([predecessor_rows, series], ignore_index=True)
                    lineage_ids.add(old_id)
                    lineage_cursor = old_id
                if lineage_blocked or series.empty:
                    continue
                security_actions = actions.loc[
                    actions["security_id"].astype(str).isin(lineage_ids)
                ].copy()
                invalid_action_time = (
                    security_actions["effective_utc"].isna()
                    | security_actions["effective_day"].isna()
                    | security_actions["announced_utc"].isna()
                )
                for action in security_actions.loc[invalid_action_time].itertuples(
                    index=False
                ):
                    gaps.append(
                        MarketPreparationGap(
                            code="CORPORATE_ACTION_TIME_INVALID",
                            dataset="bars_pit_signal",
                            security_id=security_id,
                            session_date=decision_day.date().isoformat(),
                            detail=f"action_id={getattr(action, 'action_id', '')}",
                        )
                    )
                if invalid_action_time.any():
                    continue
                relevant = security_actions.loc[
                    security_actions["effective_day"].notna()
                    & (security_actions["effective_day"] <= decision_day)
                ].sort_values(["effective_day", "action_id"], kind="stable")
                blocked = False
                for _, action in relevant.iterrows():
                    kind = str(action.get("action_type", "")).strip().upper()
                    evidence_key = (
                        str(action.get("source_id", "")),
                        str(action.get("evidence_sha256", "")).lower(),
                    )
                    evidence = dependency_by_key.get(evidence_key, [])
                    if len(evidence) != 1:
                        gaps.append(
                            MarketPreparationGap(
                                code="CORPORATE_ACTION_EVIDENCE_MISSING",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail="action lacks one exact captured source dependency",
                            )
                        )
                        blocked = True
                        break
                    evidence_item = evidence[0]
                    available_at = source_available_at(evidence_item)
                    announced = action.get("announced_utc")
                    effective_at = pd.Timestamp(action["effective_utc"])
                    if pd.isna(announced) or pd.Timestamp(announced) > effective_at:
                        gaps.append(
                            MarketPreparationGap(
                                code="CORPORATE_ACTION_TIMING_INVALID",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail="announcement is missing or occurs after effective date",
                            )
                        )
                        blocked = True
                        break
                    if not _truthy(action.get("terms_verified")):
                        gaps.append(
                            MarketPreparationGap(
                                code="UNVERIFIED_SIGNAL_CORPORATE_ACTION",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail=f"{kind} terms are not verified",
                            )
                        )
                        blocked = True
                        break
                    if pd.Timestamp(announced) > decision_close:
                        gaps.append(
                            MarketPreparationGap(
                                code="CORPORATE_ACTION_NOT_KNOWN_AT_DECISION",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail="action announcement was not public by decision close",
                            )
                        )
                        blocked = True
                        break
                    if pd.isna(available_at) or available_at > decision_close:
                        gaps.append(
                            MarketPreparationGap(
                                code="CORPORATE_ACTION_EVIDENCE_LATE",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail="action evidence was unavailable at decision close",
                            )
                        )
                        blocked = True
                        break
                    if kind in {"TICKER_CHANGE", "RENAME", "REORGANIZATION", "STOCK_MERGER"}:
                        continue
                    if kind not in {
                        "SPLIT",
                        "STOCK_DIVIDEND",
                        "CASH_DIVIDEND",
                        "SPINOFF",
                    }:
                        gaps.append(
                            MarketPreparationGap(
                                code="UNSUPPORTED_SIGNAL_CORPORATE_ACTION",
                                dataset="bars_pit_signal",
                                security_id=security_id,
                                session_date=decision_day.date().isoformat(),
                                detail=f"unsupported action_type={kind or 'BLANK'}",
                            )
                        )
                        blocked = True
                        break
                    effective = pd.Timestamp(action["effective_day"])
                    earlier = series["date"] < effective
                    if not earlier.any():
                        continue
                    if kind in {"SPLIT", "STOCK_DIVIDEND"}:
                        ratio = _positive_term(action, ("split_ratio", "share_ratio", "ratio"))
                        if ratio is None:
                            gaps.append(
                                MarketPreparationGap(
                                    code="CORPORATE_ACTION_TERMS_MISSING",
                                    dataset="bars_pit_signal",
                                    security_id=security_id,
                                    session_date=decision_day.date().isoformat(),
                                    detail=f"{kind} has no positive split/share ratio",
                                )
                            )
                            blocked = True
                            break
                        series.loc[earlier, list(PRICE_COLUMNS)] /= ratio
                        series.loc[earlier, "Volume"] *= ratio
                    elif kind == "SPINOFF":
                        allocation = _nonnegative_term(
                            action, ("cost_basis_fraction",)
                        )
                        successor = str(
                            action.get("successor_security_id") or ""
                        ).strip()
                        if not successor.startswith("us_"):
                            gaps.append(
                                MarketPreparationGap(
                                    code="CORPORATE_ACTION_TERMS_MISSING",
                                    dataset="bars_pit_signal",
                                    security_id=security_id,
                                    session_date=decision_day.date().isoformat(),
                                    detail="SPINOFF requires a stable successor and cost_basis_fraction in [0,1)",
                                )
                            )
                            blocked = True
                            break
                        if allocation is None or not 0 <= allocation < 1:
                            gaps.append(
                                MarketPreparationGap(
                                    code="CORPORATE_ACTION_TERMS_MISSING",
                                    dataset="bars_pit_signal",
                                    security_id=security_id,
                                    session_date=decision_day.date().isoformat(),
                                    detail="SPINOFF requires a stable successor and cost_basis_fraction in [0,1)",
                                )
                            )
                            blocked = True
                            break
                        method = str(
                            action.get("cost_basis_derivation_method") or ""
                        ).strip()
                        if method:
                            basis_key = (
                                str(
                                    action.get("cost_basis_evidence_source_id")
                                    or ""
                                ),
                                str(
                                    action.get("cost_basis_evidence_sha256") or ""
                                ).lower(),
                            )
                            basis_candidates = basis_dependency_by_key.get(
                                basis_key, []
                            )
                            basis_available = pd.to_datetime(
                                action.get("cost_basis_available_at"),
                                errors="coerce",
                                utc=True,
                            )
                            if (
                                method
                                != "TDX_JOINT_SESSION_VWAP_RELATIVE_FMV-v2"
                                or len(basis_candidates) != 1
                                or pd.isna(basis_available)
                                or basis_available > decision_close
                                or str(
                                    action.get("cost_basis_raw_capture_sha256")
                                    or ""
                                )
                                != str(
                                    basis_candidates[0].metadata.get(
                                        "raw_capture_sha256", ""
                                    )
                                )
                            ):
                                gaps.append(
                                    MarketPreparationGap(
                                        code="SPINOFF_BASIS_EVIDENCE_INVALID",
                                        dataset="bars_pit_signal",
                                        security_id=security_id,
                                        session_date=decision_day.date().isoformat(),
                                        detail=(
                                            "derived spinoff basis is missing exact "
                                            "causal TDX evidence"
                                        ),
                                    )
                                )
                                blocked = True
                                break
                        retained = 1.0 - allocation
                        if retained <= 0:
                            blocked = True
                            break
                        series.loc[earlier, list(PRICE_COLUMNS)] *= retained
                    else:
                        pay_date = pd.to_datetime(
                            action.get("pay_date"), errors="coerce", utc=True
                        )
                        if pd.isna(pay_date) or pd.Timestamp(pay_date) < effective_at:
                            gaps.append(
                                MarketPreparationGap(
                                    code="CORPORATE_ACTION_TERMS_MISSING",
                                    dataset="bars_pit_signal",
                                    security_id=security_id,
                                    session_date=decision_day.date().isoformat(),
                                    detail="CASH_DIVIDEND requires pay_date on or after effective date",
                                )
                            )
                            blocked = True
                            break
                        amount = _nonnegative_term(
                            action, ("cash_amount", "cash_per_share", "amount_per_share")
                        )
                        prior = series.loc[earlier].sort_values("date").tail(1)
                        if amount is None or prior.empty:
                            factor = None
                        else:
                            prior_close = float(prior.iloc[0]["Close"])
                            factor = (prior_close - amount) / prior_close
                        if factor is None or not math.isfinite(factor) or factor <= 0:
                            gaps.append(
                                MarketPreparationGap(
                                    code="CORPORATE_ACTION_TERMS_MISSING",
                                    dataset="bars_pit_signal",
                                    security_id=security_id,
                                    session_date=decision_day.date().isoformat(),
                                    detail="CASH_DIVIDEND cannot produce a positive causal adjustment",
                                )
                            )
                            blocked = True
                            break
                        series.loc[earlier, list(PRICE_COLUMNS)] *= factor
                if blocked:
                    continue
                series["security_id"] = security_id
                series = series.sort_values("date", kind="stable")
                if series["date"].duplicated().any():
                    gaps.append(
                        MarketPreparationGap(
                            code="SHARE_RATIO_IDENTITY_CHAIN_DATE_CONFLICT",
                            dataset="bars_pit_signal",
                            security_id=security_id,
                            session_date=decision_day.date().isoformat(),
                            detail="predecessor and successor raw histories overlap",
                        )
                    )
                    continue
                series.insert(0, "decision_date", decision_day)
                parts.append(series[output_columns])
        if not parts:
            return pd.DataFrame(columns=output_columns)
        return pd.concat(parts, ignore_index=True).sort_values(
            ["decision_date", "security_id", "date"], kind="stable"
        ).reset_index(drop=True)

    @staticmethod
    def _build_benchmarks(
        raw_vendor: Mapping[str, pd.DataFrame],
        front_vendor: Mapping[str, pd.DataFrame],
        calendar: pd.DataFrame,
        start_date: date,
        end_date: date,
        raw_capture_sha256: str,
        front_capture_sha256: str,
        gaps: list[MarketPreparationGap],
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        expected_sessions = pd.DatetimeIndex(
            pd.to_datetime(calendar["session_date"], errors="coerce")
        ).normalize()
        expected = set(expected_sessions)
        parts: list[pd.DataFrame] = []
        derivations: dict[str, Any] = {}
        for symbol, code in BENCHMARK_CODES.items():
            raw = raw_vendor.get(code)
            front = front_vendor.get(code)
            if raw is None or front is None:
                continue
            raw_value = raw.loc[
                raw["date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
            ].copy()
            front_value = front[["date", "Close"]].rename(columns={"Close": "front_close"})
            value = raw_value.merge(front_value, on="date", how="left", validate="one_to_one")
            missing_front = value["front_close"].isna()
            for day in value.loc[missing_front, "date"]:
                gaps.append(
                    MarketPreparationGap(
                        code="BENCHMARK_FRONT_ROW_MISSING",
                        dataset="benchmarks",
                        vendor_code=code,
                        session_date=pd.Timestamp(day).date().isoformat(),
                        detail="total-return derivation has no matching TDX front-adjusted close",
                    )
                )
            value = value.loc[~missing_front].copy()
            observed = set(value["date"])
            for day in sorted(expected - observed):
                gaps.append(
                    MarketPreparationGap(
                        code="BENCHMARK_SESSION_MISSING",
                        dataset="benchmarks",
                        vendor_code=code,
                        session_date=pd.Timestamp(day).date().isoformat(),
                        detail="benchmark session is absent; no value was filled",
                    )
                )
            if value.empty:
                continue
            value = value.sort_values("date", kind="stable").reset_index(drop=True)
            front_close = pd.to_numeric(value["front_close"], errors="coerce")
            daily_return = front_close.pct_change(fill_method=None).fillna(0.0)
            invalid_return = ~daily_return.map(lambda item: math.isfinite(float(item)))
            invalid_return |= daily_return <= -1
            if invalid_return.any():
                gaps.append(
                    MarketPreparationGap(
                        code="BENCHMARK_TOTAL_RETURN_INVALID",
                        dataset="benchmarks",
                        vendor_code=code,
                        detail=f"invalid derived daily returns: {int(invalid_return.sum())}",
                    )
                )
                continue
            value["TotalReturnClose"] = 100.0 * (1.0 + daily_return).cumprod()
            value.insert(0, "symbol", symbol)
            value["adjustment"] = "none"
            columns = [
                "symbol",
                "date",
                *PRICE_COLUMNS,
                "Volume",
                "adjustment",
                "TotalReturnClose",
            ]
            parts.append(value[columns])
            derivations[symbol] = {
                "vendor_code": code,
                "first_date": value["date"].min().date().isoformat(),
                "last_date": value["date"].max().date().isoformat(),
                "row_count": len(value),
                "output_sha256": sha256_json(
                    _json_records(value[["date", "front_close", "TotalReturnClose"]])
                ),
            }
        columns = [
            "symbol",
            "date",
            *PRICE_COLUMNS,
            "Volume",
            "adjustment",
            "TotalReturnClose",
        ]
        benchmark = (
            pd.concat(parts, ignore_index=True)[columns]
            if parts
            else pd.DataFrame(columns=columns)
        )
        evidence = {
            "format_version": "tdx-benchmark-total-return-v1",
            "algorithm": (
                "normalize each benchmark to 100 and compound same-date TDX front-close "
                "returns; ratios are invariant to later global front-adjustment rescaling"
            ),
            "raw_capture_sha256": raw_capture_sha256,
            "front_capture_sha256": front_capture_sha256,
            "calendar_first_session": (
                expected_sessions.min().date().isoformat()
                if len(expected_sessions)
                else None
            ),
            "calendar_last_session": (
                expected_sessions.max().date().isoformat()
                if len(expected_sessions)
                else None
            ),
            "derivations": derivations,
        }
        return benchmark, evidence

    def _fee_schedule(
        self,
        start_date: date,
        end_date: date,
        gaps: list[MarketPreparationGap],
    ) -> pd.DataFrame:
        if start_date < _SEC_RATES[0][0]:
            gaps.append(
                MarketPreparationGap(
                    code="FEE_HISTORY_UNSUPPORTED",
                    dataset="execution_fee_schedule",
                    session_date=start_date.isoformat(),
                    detail=(
                        "the built-in official Section 31 evidence begins on "
                        f"{_SEC_RATES[0][0].isoformat()}"
                    ),
                )
            )
        boundaries = {start_date, end_date + timedelta(days=1)}
        boundaries.update(day for day, _ in _SEC_RATES if start_date < day <= end_date)
        boundaries.update(day for day, _, _ in _FINRA_TAF if start_date < day <= end_date)
        ordered = sorted(boundaries)
        rows: list[dict[str, Any]] = []
        for index, effective in enumerate(ordered[:-1]):
            sec = _effective_value(_SEC_RATES, effective)
            taf = _effective_value(_FINRA_TAF, effective)
            if sec is None or taf is None:
                continue
            finra_rate, finra_cap = taf
            rows.append(
                {
                    "effective_from": effective.isoformat(),
                    "effective_to": (ordered[index + 1] - timedelta(days=1)).isoformat(),
                    "commission_rate": self.commission_rate,
                    "min_commission": 0.0,
                    "slippage_rate": self.slippage_rate,
                    "sec_sell_fee_rate": sec,
                    "finra_taf_per_share": finra_rate,
                    "finra_taf_cap": finra_cap,
                    "fee_model_id": "us_equity_effective_fees_v1",
                    "sec_evidence_url": SEC_FEE_URL,
                    "finra_evidence_url": FINRA_TAF_URL,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _fee_evidence_bindings(
        fees: pd.DataFrame,
        sources: list[SourceDependency],
        *,
        authority: str,
        gaps: list[MarketPreparationGap],
    ) -> list[str]:
        bindings: list[str] = []
        for row in fees.to_dict(orient="records"):
            row_date = pd.Timestamp(row["effective_from"]).date()
            candidates: list[tuple[date, SourceDependency]] = []
            for source in sources:
                if (
                    source.role != SourceRole.VALIDATION_ANCHOR
                    or source.license_class != LicenseClass.OFFICIAL_PUBLIC
                    or int(source.metadata.get("fee_evidence_contract_version", 0))
                    != FEE_EVIDENCE_CONTRACT_VERSION
                ):
                    continue
                for entry in fee_rate_entries(dict(source.metadata)):
                    try:
                        effective = date.fromisoformat(str(entry["effective_from"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if effective > row_date:
                        continue
                    if authority == "SEC":
                        try:
                            matches = math.isclose(
                                float(entry["sec_sell_fee_rate"]),
                                float(row["sec_sell_fee_rate"]),
                                rel_tol=0.0,
                                abs_tol=1e-15,
                            )
                        except (KeyError, TypeError, ValueError):
                            matches = False
                    else:
                        try:
                            matches = math.isclose(
                                float(entry["finra_taf_per_share"]),
                                float(row["finra_taf_per_share"]),
                                rel_tol=0.0,
                                abs_tol=1e-15,
                            ) and math.isclose(
                                float(entry["finra_taf_cap"]),
                                float(row["finra_taf_cap"]),
                                rel_tol=0.0,
                                abs_tol=1e-12,
                            )
                        except (KeyError, TypeError, ValueError):
                            matches = False
                    if matches:
                        candidates.append((effective, source))
            if not candidates:
                gaps.append(
                    MarketPreparationGap(
                        code="REGULATORY_FEE_RAW_EVIDENCE_MISSING",
                        dataset="execution_fee_schedule",
                        session_date=row_date.isoformat(),
                        detail=(
                            f"no frozen {authority} object proves the active fee row"
                        ),
                    )
                )
                bindings.append("")
                continue
            latest = max(item[0] for item in candidates)
            exact_candidates = [
                item for effective, item in candidates if effective == latest
            ]
            exact_shas = {item.object_sha256 for item in exact_candidates}
            if len(exact_shas) != 1:
                gaps.append(
                    MarketPreparationGap(
                        code="REGULATORY_FEE_RAW_EVIDENCE_AMBIGUOUS",
                        dataset="execution_fee_schedule",
                        session_date=row_date.isoformat(),
                        detail=(
                            f"multiple frozen {authority} objects claim the active fee row"
                        ),
                    )
                )
                bindings.append("")
                continue
            bindings.append(next(iter(exact_shas)))
        return bindings

    @staticmethod
    def _bar_coverage(
        memberships: pd.DataFrame,
        aliases: pd.DataFrame,
        raw: pd.DataFrame,
        signal: pd.DataFrame,
        calendar: pd.DataFrame,
        session_exceptions: pd.DataFrame,
        start_date: date,
        excluded_security_ids: set[str] = frozenset(),
    ) -> pd.DataFrame:
        columns = [
            "decision_date",
            "security_id",
            "expected_sessions",
            "raw_sessions",
            "signal_sessions",
            "explained_missing_sessions",
            "passed",
        ]
        if memberships.empty or calendar.empty:
            return pd.DataFrame(columns=columns)
        sessions = pd.DatetimeIndex(pd.to_datetime(calendar["session_date"])).normalize()
        exception_keys: set[tuple[str, pd.Timestamp]] = set()
        if not session_exceptions.empty:
            exception_days = pd.to_datetime(
                session_exceptions.get("session_date"), errors="coerce"
            ).dt.normalize()
            verified = session_exceptions.get("verified", pd.Series(False, index=session_exceptions.index)).map(
                _truthy
            )
            supported = session_exceptions.get(
                "exception_type", pd.Series("", index=session_exceptions.index)
            ).astype(str).str.upper().isin({"HALTED", "NO_TRADE"})
            for security_id, day in zip(
                session_exceptions.loc[verified & supported, "security_id"].astype(str),
                exception_days.loc[verified & supported],
                strict=True,
            ):
                if not pd.isna(day):
                    exception_keys.add((security_id, pd.Timestamp(day)))
        raw_keys = set(
            zip(raw["security_id"].astype(str), pd.to_datetime(raw["date"]).dt.normalize(), strict=True)
        )
        import numpy as _np

        rows: list[dict[str, Any]] = []
        sessions_array = _np.asarray(sessions.values, dtype="datetime64[ns]")
        session_position = {ts: i for i, ts in enumerate(sessions)}
        # Pre-index raw/exception dates per security so window counting is
        # O(log n) via cumsum instead of O(expected_days) per member.
        raw_entry = raw.copy()
        raw_entry["date_n"] = pd.to_datetime(
            raw_entry["date"], errors="coerce"
        ).dt.normalize()
        raw_dates_by_sid: dict[str, set[pd.Timestamp]] = {}
        for sid, day in zip(
            raw_entry["security_id"].astype(str),
            raw_entry["date_n"],
            strict=True,
        ):
            if not pd.isna(day):
                raw_dates_by_sid.setdefault(sid, set()).add(pd.Timestamp(day))
        except_dates_by_sid: dict[str, set[pd.Timestamp]] = {}
        for sid, day in exception_keys:
            except_dates_by_sid.setdefault(sid, set()).add(day)
        signal_dates_by_member: dict[tuple[str, str], set[pd.Timestamp]] = {}
        if not signal.empty:
            signal_view = signal.copy()
            signal_view["_d"] = pd.to_datetime(
                signal_view["date"], errors="coerce"
            ).dt.normalize()
            signal_view["_k"] = pd.to_datetime(
                signal_view["decision_date"], errors="coerce"
            ).dt.normalize().dt.strftime("%Y-%m-%d")
            signal_view["_key"] = (
                signal_view["security_id"].astype(str)
                + "|"
                + signal_view["_k"]
            )
            grouped = signal_view.groupby("_key", sort=False)["_d"].agg(set)
            for key, dates in grouped.items():
                sid, day = str(key).split("|", 1)
                signal_dates_by_member[(sid, day)] = set(dates)

        def _cumcounts(dates: set[pd.Timestamp]) -> _np.ndarray:
            mask = _np.zeros(len(sessions), dtype=_np.int64)
            for ts in dates:
                position = session_position.get(ts)
                if position is not None:
                    mask[position] = 1
            return _np.cumsum(mask)

        raw_cs: dict[str, _np.ndarray] = {}
        except_cs: dict[str, _np.ndarray] = {}
        for member in memberships[["decision_date", "security_id"]].itertuples(index=False):
            decision = pd.Timestamp(member.decision_date).normalize()
            security_id = str(member.security_id)
            alias_rows = aliases.loc[aliases["security_id"].eq(security_id)]
            first_alias = alias_rows["valid_from"].min() if not alias_rows.empty else pd.NaT
            first_expected = pd.Timestamp(start_date)
            if not pd.isna(first_alias):
                first_expected = max(first_expected, pd.Timestamp(first_alias))
            start_idx = int(
                _np.searchsorted(
                    sessions_array,
                    _np.datetime64(first_expected, "ns"),
                    side="left",
                )
            )
            end_idx = int(
                _np.searchsorted(
                    sessions_array,
                    _np.datetime64(decision, "ns"),
                    side="right",
                )
            )
            expected_count = max(0, end_idx - start_idx)
            if security_id in excluded_security_ids:
                rows.append(
                    {
                        "decision_date": decision,
                        "security_id": security_id,
                        "expected_sessions": expected_count,
                        "raw_sessions": 0,
                        "signal_sessions": 0,
                        "explained_missing_sessions": expected_count,
                        "passed": True,
                    }
                )
                continue
            if security_id not in raw_cs:
                raw_cs[security_id] = _cumcounts(
                    raw_dates_by_sid.get(security_id, set())
                )
                except_cs[security_id] = _cumcounts(
                    except_dates_by_sid.get(security_id, set())
                )
            rcs = raw_cs[security_id]
            ecs = except_cs[security_id]
            raw_count = 0
            if expected_count:
                raw_count = int(rcs[end_idx - 1]) - (
                    int(rcs[start_idx - 1]) if start_idx > 0 else 0
                )
            signal_dates = signal_dates_by_member.get(
                (security_id, decision.strftime("%Y-%m-%d")), set()
            )
            signal_count = sum(
                1
                for day in signal_dates
                if start_idx <= session_position.get(day, -1) < end_idx
            )
            except_dates = except_dates_by_sid.get(security_id, set())
            raw_dates = raw_dates_by_sid.get(security_id, set())
            signal_bucket = signal_dates_by_member.get(
                (security_id, decision.strftime("%Y-%m-%d")), set()
            )
            explained = sum(
                1
                for day in except_dates
                if start_idx <= session_position.get(day, -1) < end_idx
                and day not in raw_dates
                and day not in signal_bucket
            )
            passed = (
                expected_count > 0
                and raw_count + explained == expected_count
                and signal_count + explained == expected_count
            )
            rows.append(
                {
                    "decision_date": decision,
                    "security_id": security_id,
                    "expected_sessions": expected_count,
                    "raw_sessions": raw_count,
                    "signal_sessions": signal_count,
                    "explained_missing_sessions": explained,
                    "passed": passed,
                }
            )
        return pd.DataFrame(rows, columns=columns)

    @staticmethod
    def _validate_next_session_opens(
        raw: pd.DataFrame,
        memberships: pd.DataFrame,
        calendar: pd.DataFrame,
        corporate_actions: pd.DataFrame,
        gaps: list[MarketPreparationGap],
        excluded_security_ids: set[str] = frozenset(),
    ) -> None:
        if raw.empty or memberships.empty or calendar.empty:
            return
        sessions = pd.DatetimeIndex(
            pd.to_datetime(calendar["session_date"], errors="coerce")
        ).normalize()
        raw_value = raw.copy()
        raw_value["date"] = pd.to_datetime(raw_value["date"], errors="coerce").dt.normalize()
        # Pre-group by security for fast next-session lookup; SCOPE-C excluded
        # securities have no obtainable TDX bars, so they must not be required
        # to supply a next execution open.
        raw_by: dict[str, pd.DataFrame] = {}
        if not raw_value.empty:
            for sid, group in raw_value.groupby("security_id", sort=False):
                raw_by[str(sid)] = group
        identity_actions = corporate_actions.copy()
        if not identity_actions.empty:
            identity_actions = identity_actions.loc[
                identity_actions["action_type"]
                .astype(str)
                .str.strip()
                .str.upper()
                .isin(
                    {
                        "TICKER_CHANGE",
                        "RENAME",
                        "SPLIT",
                        "STOCK_DIVIDEND",
                        "REORGANIZATION",
                    }
                )
                & identity_actions["terms_verified"]
                .astype(str)
                .str.strip()
                .str.casefold()
                .isin({"true", "1"})
            ].copy()
            identity_actions["effective_day"] = pd.to_datetime(
                identity_actions["effective_at"], errors="coerce", utc=True
            ).map(
                lambda value: (
                    pd.NaT
                    if pd.isna(value)
                    else pd.Timestamp(value)
                    .tz_convert(NEW_YORK)
                    .tz_localize(None)
                    .normalize()
                )
            )
        excluded = set(str(x) for x in excluded_security_ids)
        for member in memberships[["decision_date", "security_id"]].itertuples(
            index=False
        ):
            decision = pd.Timestamp(member.decision_date).normalize()
            security_id = str(member.security_id)
            if security_id in excluded:
                continue
            later = sessions[sessions > decision]
            if not len(later):
                gaps.append(
                    MarketPreparationGap(
                        code="NEXT_EXECUTION_SESSION_UNKNOWN",
                        dataset="xnys_calendar",
                        security_id=security_id,
                        session_date=decision.date().isoformat(),
                        detail="frozen calendar cannot identify the next execution session",
                    )
                )
                continue
            next_session = later[0]
            group = raw_by.get(security_id)
            match = pd.DataFrame()
            if group is not None and not group.empty:
                match = group.loc[group["date"].eq(next_session)]
            opens = pd.to_numeric(match.get("Open"), errors="coerce")
            if len(match) != 1 or opens.isna().any() or (opens <= 0).any():
                lineage = identity_actions.loc[
                    identity_actions["security_id"].astype(str).eq(security_id)
                    & identity_actions["effective_day"].eq(next_session)
                ]
                if len(lineage) == 1:
                    successor = str(
                        lineage.iloc[0].get("successor_security_id", "")
                    ).strip()
                    successor_group = raw_by.get(successor)
                    if successor_group is not None and not successor_group.empty:
                        match = successor_group.loc[
                            successor_group["date"].eq(next_session)
                        ]
                        opens = pd.to_numeric(match.get("Open"), errors="coerce")
            if len(match) != 1 or opens.isna().any() or (opens <= 0).any():
                gaps.append(
                    MarketPreparationGap(
                        code="NEXT_EXECUTION_OPEN_MISSING",
                        dataset="bars_raw",
                        security_id=security_id,
                        session_date=next_session.date().isoformat(),
                        detail="next actual XNYS Open is absent; no value was filled",
                    )
                )

    def _dependency(
        self,
        *,
        source_id: str,
        dataset: str,
        object_sha256: str,
        observed_at: datetime,
        published_at: datetime | str | None = None,
        as_of_date: date,
        url: str,
        license_class: LicenseClass,
        metadata: Mapping[str, Any],
        source_version: str | None = None,
        role: SourceRole = SourceRole.SIGNAL_INPUT,
    ) -> SourceDependency:
        published_value: str | None
        if isinstance(published_at, datetime):
            if published_at.tzinfo is None:
                raise ValueError("published_at must be timezone-aware")
            published_value = published_at.astimezone(timezone.utc).isoformat()
        elif published_at is None:
            published_value = None
        else:
            published_value = str(published_at)
        return SourceDependency(
            source_id=source_id,
            source_version=source_version or self.tdx_source_version,
            role=role,
            license_class=license_class,
            object_sha256=object_sha256,
            observed_at=observed_at.astimezone(timezone.utc).isoformat(),
            url=url,
            dataset=dataset,
            as_of_date=as_of_date.isoformat(),
            published_at=published_value,
            metadata=dict(metadata),
        )

    def _publish(
        self,
        input_dir: Path,
        target: Path,
        frames: Mapping[str, pd.DataFrame],
        source_batch: SourceBatch,
        gaps: list[MarketPreparationGap],
        *,
        status: str,
        start_date: date,
        end_date: date,
        fetch_start: date,
        fetch_end: date,
        observed_at: datetime,
        universe_id: str,
        input_hashes: Mapping[str, str],
        excluded_market_data: list[Mapping[str, Any]] = (),  # SCOPE-C record,
    ) -> MarketPreparationResult:
        if target.exists():
            raise ValueError(f"reviewed market output already exists and is immutable: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.{uuid4().hex}.staging"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            files: dict[str, dict[str, Any]] = {}
            for name in REQUIRED_ARTIFACTS:
                path = staging / f"{name}.parquet"
                frames[name].to_parquet(path, index=False)
                files[path.name] = {
                    "sha256": sha256_file(path),
                    "row_count": len(frames[name]),
                }
            if dict(input_hashes) != self._input_hashes(input_dir):
                raise ValueError("reviewed input changed during market preparation")
            input_gap_report = json.loads(
                (input_dir / "gap_report.json").read_text(encoding="utf-8")
            )
            input_blockers = input_gap_report.get("blocking_gaps")
            if not isinstance(input_blockers, list):
                input_blockers = []
            upstream_counts = input_gap_report.get("counts")
            if not isinstance(upstream_counts, Mapping):
                upstream_counts = {}
            upstream_samples: dict[str, list[Mapping[str, Any]]] = {}
            for item in input_blockers:
                if not isinstance(item, Mapping):
                    continue
                code = str(item.get("code") or "UNKNOWN")
                bucket = upstream_samples.setdefault(code, [])
                if len(bucket) < 3:
                    bucket.append(dict(item))
            report = {
                "format_version": "us-pit-market-prepare-v1",
                "status": status,
                "universe_id": universe_id,
                "requested_start": start_date.isoformat(),
                "requested_end": end_date.isoformat(),
                "fetch_start_including_warmup": fetch_start.isoformat(),
                "fetch_end_including_next_session": fetch_end.isoformat(),
                "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
                "source_batch_id": source_batch.batch_id,
                "input_source_dependency_ids": [
                    item.source_id for item in source_batch.dependencies
                ],
                "input_manifest_sha256": sha256_file(input_dir / "manifest.json"),
                "input_gap_report_sha256": sha256_file(input_dir / "gap_report.json"),
                "upstream_review_gaps": {
                    "status": str(input_gap_report.get("status") or "UNKNOWN"),
                    "total": len(input_blockers),
                    "counts": {
                        str(code): int(count)
                        for code, count in sorted(upstream_counts.items())
                    },
                    "samples": upstream_samples,
                },
                "gaps": [item.to_dict() for item in gaps],
                "excluded_market_data": [
                    dict(item) for item in excluded_market_data
                ],
                "row_counts": {name: len(frame) for name, frame in frames.items()},
                "broker_writes_enabled": False,
            }
            report_path = staging / "market_prepare_report.json"
            report_path.write_bytes(canonical_json_bytes(report))
            files[report_path.name] = {"sha256": sha256_file(report_path)}
            lineage_path = staging / "market_source_batch.json"
            lineage_path.write_bytes(
                canonical_json_bytes(
                    {
                        "batch_id": source_batch.batch_id,
                        "dependencies": [
                            item.to_dict() for item in source_batch.dependencies
                        ],
                    }
                )
            )
            files[lineage_path.name] = {"sha256": sha256_file(lineage_path)}
            market_manifest = {
                "format_version": "us-pit-reviewed-market-v1",
                "source_batch_id": source_batch.batch_id,
                "required_artifacts": list(REQUIRED_ARTIFACTS),
                "input_sha256": input_hashes,
                "files": files,
            }
            # The service verifier expects the report and lineage files to be
            # included in this immutable manifest as well as all 17 tables.
            market_manifest_path = staging / "reviewed_manifest.json"
            market_manifest_path.write_bytes(canonical_json_bytes(market_manifest))
            gap_counts: dict[str, int] = {}
            for gap in gaps:
                gap_counts[gap.code] = gap_counts.get(gap.code, 0) + 1
            gap_report = {
                "status": "DATA_BLOCKED" if gaps else "REVIEW_READY",
                "counts": gap_counts,
                "blocking_gaps": [item.to_dict() for item in gaps],
            }
            gap_path = staging / "gap_report.json"
            gap_path.write_bytes(canonical_json_bytes(gap_report))
            workspace_manifest = {
                "format_version": "us-pit-complete-reviewed-v1",
                "status": "DATA_BLOCKED" if gaps else "REVIEW_READY",
                "direct_build_allowed": not gaps,
                "universe_id": universe_id,
                "source_batch_ids": [source_batch.batch_id],
                "artifacts": {
                    name: sha256_json(_json_records(frames[name]))
                    for name in REQUIRED_ARTIFACTS
                },
                "gap_report_sha256": sha256_json(gap_report),
                "market_manifest_sha256": sha256_file(market_manifest_path),
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_bytes(canonical_json_bytes(workspace_manifest))
            manifest_sha256 = sha256_file(manifest_path)
            for path in staging.iterdir():
                path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            os.rename(staging, target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return MarketPreparationResult(
            status=status,
            output_dir=target,
            source_batch=source_batch,
            report_path=target / "market_prepare_report.json",
            manifest_sha256=manifest_sha256,
            gaps=tuple(gaps),
            row_counts={name: len(frame) for name, frame in frames.items()},
        )


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return bool(value)


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _valid_date_window(start: Any, end: Any) -> bool:
    start_day = pd.to_datetime(start, errors="coerce")
    end_day = pd.to_datetime(end, errors="coerce")
    return not pd.isna(start_day) and not pd.isna(end_day) and start_day <= end_day


def _terms_mapping(row: pd.Series) -> dict[str, Any]:
    value = row.get("terms")
    if isinstance(value, Mapping):
        return dict(value)
    for column in ("terms_json", "terms"):
        raw = row.get(column)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return {}


def _number_term(row: pd.Series, names: tuple[str, ...]) -> float | None:
    terms = _terms_mapping(row)
    for name in names:
        raw = row.get(name)
        if raw is None or (not isinstance(raw, (dict, list)) and pd.isna(raw)):
            raw = terms.get(name)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _positive_term(row: pd.Series, names: tuple[str, ...]) -> float | None:
    value = _number_term(row, names)
    return value if value is not None and value > 0 else None


def _nonnegative_term(row: pd.Series, names: tuple[str, ...]) -> float | None:
    value = _number_term(row, names)
    return value if value is not None and value >= 0 else None


def _effective_value(schedule: tuple[Any, ...], day: date) -> Any:
    values = [row[1:] for row in schedule if row[0] <= day]
    if not values:
        return None
    latest = values[-1]
    return latest[0] if len(latest) == 1 else latest


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        value: dict[str, Any] = {}
        for key, item in row.items():
            try:
                missing = bool(pd.isna(item))
            except (TypeError, ValueError):
                missing = False
            if item is None or missing:
                value[str(key)] = None
            elif isinstance(item, (pd.Timestamp, datetime, date)):
                value[str(key)] = item.isoformat()
            elif hasattr(item, "item"):
                value[str(key)] = item.item()
            else:
                value[str(key)] = item
        records.append(value)
    return records


__all__ = [
    "BENCHMARK_CODES",
    "HistoricalBarProvider",
    "MARKET_ARTIFACTS",
    "PASSTHROUGH_ARTIFACTS",
    "REQUIRED_ARTIFACTS",
    "MarketPreparationGap",
    "MarketPreparationResult",
    "USPITMarketPreparer",
]
