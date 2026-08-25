from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .etf_pullback_research import DAY_DTYPE, EtfAsset, decode_day_bytes


PROTOCOL_VERSION = "1.0.0"
DATA_CONTRACT_VERSION = "1.0.0"
FROZEN_PROTOCOL_SHA256 = (
    "cb67bd416368c1aba7874e08088f0a0d5e3e27a80bf54fe4ec989489a41e6293"
)
FROZEN_DIVIDENDS_SHA256 = (
    "e4d12eaf9e785c1ef03c7e9ccfc8e249e4926e5f35222d38e3d163ea317f6462"
)
SNAPSHOT_START = "2021-04-01"
SNAPSHOT_END = "2026-08-07"
MINIMUM_COVERAGE = 0.995
MAX_DIVIDEND_PRICE_ERROR_CNY = 0.10
MAX_CUMULATIVE_NAV_DECLINE_CNY = 0.01


@dataclass(frozen=True)
class FrozenSource:
    source_id: str
    relative_path: str
    sha256: str
    authority: str
    url: str
    role: str


CASH_ETF_ASSETS = (
    EtfAsset("511880.SH", "Yinhua Money Market ETF", "sh", "sh511880"),
    EtfAsset("511990.SH", "Huabao Cash Tianyi ETF", "sh", "sh511990"),
)

FROZEN_RAW_SOURCES = (
    FrozenSource(
        "yinhua_511880_nav",
        "raw/yinhua_511880_nav_20210401_20260807_retrieved_20260811.json",
        "fd306e0f66552dca10c9109283e4e332aa8367432e7863250146a40cccb809a5",
        "Yinhua Fund Management",
        "https://www.yhfund.com.cn/servlet/json",
        "official NAV and cumulative NAV",
    ),
    FrozenSource(
        "huabao_511990_income",
        "raw/huabao_511990_nav_full_retrieved_20260811.json",
        "025de165ea19eebd064616b8f0f3c79401882111b196bc106832bcb3f744e64c",
        "Huabao WP Fund Management",
        "https://api.fsfund.com/v2/webzk/queryController/queryNavPage",
        "official daily fund income, yield, and publication timestamp",
    ),
    FrozenSource(
        "yinhua_511880_corporate_action_attempt",
        "raw/yinhua_511880_corporate_actions_retrieved_20260811.json",
        "f12a989e33ca5de1012cc055157831e23640134f525a2442309ff9fba47499b1",
        "Yinhua Fund Management",
        "https://www.yhfund.com.cn/servlet/json",
        "failed official corporate-action endpoint response retained for audit",
    ),
    FrozenSource(
        "sse_511880_dividend_index",
        "raw/sse_511880_dividend_announcements_20210101_20260811_retrieved_20260811.json",
        "d154de2fa5935eb3e4edc3b29516c88f4c9b31bce74912ada82d07704e4ced87",
        "Shanghai Stock Exchange",
        "https://query.sse.com.cn/commonQuery.do",
        "official dividend announcement index",
    ),
    FrozenSource(
        "yinhua_511880_publication_rule_2021",
        "raw/publication_rules/511880_20210507_prospectus.pdf",
        "6b8323b64d85ee536c870015a89549344d4aa6bd1597731e3109b8db857c6bd6",
        "Shanghai Stock Exchange fund disclosure",
        "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2021-05-07/511880_20210507_1.pdf",
        "2021 prospectus page 86: NAV is disclosed no later than the next day",
    ),
    FrozenSource(
        "yinhua_511880_publication_rule",
        "raw/publication_rules/511880_20260430_prospectus.pdf",
        "12da6b08a9c55695327a1dde5f21258cf52938f0c097e22e7f2663d569b91b25",
        "Shanghai Stock Exchange fund disclosure",
        "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-04-30/511880_20260430_FUO0.pdf",
        "prospectus page 111: A/B NAV is disclosed no later than the next day",
    ),
    FrozenSource(
        "huabao_511990_publication_rule",
        "raw/publication_rules/511990_fund_contract.pdf",
        "6642e1813104bf65fa27219cb3d9ed325390cbb86ba1e39a815718d87929042f",
        "Huabao WP Fund Management",
        "https://www.fsfund.com/data/20191129/%E5%8D%8E%E5%AE%9D%E7%8E%B0%E9%87%91%E6%B7%BB%E7%9B%8A%E4%BA%A4%E6%98%93%E5%9E%8B%E8%B4%A7%E5%B8%81%E5%B8%82%E5%9C%BA%E5%9F%BA%E9%87%91%E5%9F%BA%E9%87%91%E5%90%88%E5%90%8C.pdf",
        "fund contract page 60: A-class income is disclosed no later than the next day",
    ),
)


def verify_file_hash(path: Path, expected_sha256: str) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = _file_sha256(path)
    expected = expected_sha256.lower()
    if actual != expected:
        raise ValueError(
            f"Frozen source hash mismatch: {path}; expected={expected}, actual={actual}"
        )
    return {
        "path": str(path.resolve()),
        "bytes": int(path.stat().st_size),
        "sha256": actual,
    }


def verify_frozen_protocol(validation_dir: Path) -> dict[str, Any]:
    validation_dir = Path(validation_dir)
    result = verify_file_hash(
        validation_dir / "protocol.json", FROZEN_PROTOCOL_SHA256
    )
    sidecar = validation_dir / "protocol.sha256"
    if not sidecar.is_file():
        raise FileNotFoundError(sidecar)
    sidecar_hash = sidecar.read_text(encoding="utf-8").strip().split()[0].lower()
    if sidecar_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen protocol sidecar mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={sidecar_hash}"
        )
    protocol = json.loads((validation_dir / "protocol.json").read_text(encoding="utf-8"))
    if protocol.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Unsupported cash-instrument protocol version")
    return {**result, "protocol_version": PROTOCOL_VERSION}


def verify_frozen_raw_sources(validation_dir: Path) -> list[dict[str, Any]]:
    validation_dir = Path(validation_dir)
    verified: list[dict[str, Any]] = []
    for source in FROZEN_RAW_SOURCES:
        result = verify_file_hash(validation_dir / source.relative_path, source.sha256)
        verified.append({**asdict(source), **result})
    return verified


def parse_yinhua_nav(
    path: Path,
    *,
    start_date: str = SNAPSHOT_START,
    end_date: str = SNAPSHOT_END,
) -> pd.DataFrame:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if str(payload.get("error_no")) != "0":
        raise ValueError(f"Yinhua NAV endpoint failed: {payload.get('error_info')}")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError("Yinhua NAV payload has an unexpected result envelope")
    rows = results[0].get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Yinhua NAV payload is empty")
    frame = pd.DataFrame(rows).rename(
        columns={
            "nav_date": "timestamp",
            "relate_price": "unit_nav",
            "cumulative_net": "cumulative_nav",
            "profit_per_million": "profit_per_million",
            "seven_days_annual_profit": "seven_day_annual_yield_percent",
        }
    )
    required = {
        "timestamp",
        "unit_nav",
        "cumulative_nav",
        "profit_per_million",
        "seven_day_annual_yield_percent",
    }
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Yinhua NAV payload is missing fields: {sorted(missing)}")
    frame = frame[list(required)].copy()
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"], format="%Y%m%d", errors="raise"
    ).dt.normalize()
    for column in required.difference({"timestamp"}):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = _validate_official_frame(
        frame,
        start_date=start_date,
        end_date=end_date,
        positive_columns=("unit_nav", "cumulative_nav"),
        nonnegative_columns=("profit_per_million",),
        dataset="Yinhua NAV",
    )
    frame["publication_time"] = pd.NaT
    return frame[
        [
            "timestamp",
            "unit_nav",
            "cumulative_nav",
            "profit_per_million",
            "seven_day_annual_yield_percent",
            "publication_time",
        ]
    ]


def parse_huabao_income(
    path: Path,
    *,
    start_date: str = SNAPSHOT_START,
    end_date: str = SNAPSHOT_END,
) -> pd.DataFrame:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if str(payload.get("code")) != "0000":
        raise ValueError(f"Huabao income endpoint failed: {payload.get('message')}")
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Huabao income payload is empty")
    frame = pd.DataFrame(rows).rename(
        columns={
            "navDate": "timestamp",
            "fundIncome": "income_per_100_units",
            "yield": "seven_day_annual_yield",
            "publishSign": "publish_sign",
            "modifyTime": "publication_time",
        }
    )
    required = {
        "timestamp",
        "income_per_100_units",
        "seven_day_annual_yield",
        "publish_sign",
        "publication_time",
    }
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Huabao income payload is missing fields: {sorted(missing)}")
    frame = frame[list(required)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise").dt.normalize()
    frame["income_per_100_units"] = pd.to_numeric(
        frame["income_per_100_units"], errors="coerce"
    )
    frame["seven_day_annual_yield"] = pd.to_numeric(
        frame["seven_day_annual_yield"], errors="coerce"
    )
    frame["publication_time"] = pd.to_datetime(
        frame["publication_time"], format="%Y%m%d%H%M%S", errors="coerce"
    )
    frame = _validate_official_frame(
        frame,
        start_date=start_date,
        end_date=end_date,
        positive_columns=("seven_day_annual_yield",),
        nonnegative_columns=("income_per_100_units",),
        dataset="Huabao income",
    )
    if frame["publish_sign"].astype(str).ne("1").any():
        raise ValueError("Huabao income payload contains unpublished rows")
    return frame[
        [
            "timestamp",
            "income_per_100_units",
            "seven_day_annual_yield",
            "publish_sign",
            "publication_time",
        ]
    ]


def apply_conservative_publication_rule(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    timestamps = pd.to_datetime(result["timestamp"], errors="raise").dt.normalize()
    result["publication_available_at"] = (
        timestamps + pd.Timedelta(days=2) - pd.Timedelta(seconds=1)
    )
    result["publication_time_basis"] = "NEXT_CALENDAR_DAY_23_59_59_RULE_CEILING"
    return result


def load_yinhua_dividends(validation_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validation_dir = Path(validation_dir)
    path = validation_dir / "yinhua_dividends.json"
    structured = verify_file_hash(path, FROZEN_DIVIDENDS_SHA256)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != DATA_CONTRACT_VERSION:
        raise ValueError("Unsupported Yinhua dividend schema version")
    if payload.get("instrument_code") != "511880.SH":
        raise ValueError("Yinhua dividend instrument code mismatch")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("Yinhua dividend records are empty")
    verified_pdfs: list[dict[str, Any]] = []
    previous_ex_date: pd.Timestamp | None = None
    for record in records:
        for field in (
            "announcement_date",
            "base_date",
            "record_date",
            "ex_date",
            "cash_payment_date",
        ):
            pd.Timestamp(record[field])
        per_ten = float(record["dividend_per_10_units"])
        per_unit = float(record["dividend_per_unit"])
        if per_ten <= 0.0 or abs(per_ten / 10.0 - per_unit) > 1e-9:
            raise ValueError("Yinhua dividend unit conversion mismatch")
        ex_date = pd.Timestamp(record["ex_date"]).normalize()
        if previous_ex_date is not None and ex_date <= previous_ex_date:
            raise ValueError("Yinhua dividend records are not strictly ordered")
        if pd.Timestamp(record["announcement_date"]) > ex_date:
            raise ValueError("Yinhua dividend was announced after its ex-date")
        previous_ex_date = ex_date
        pdf = verify_file_hash(
            validation_dir / record["source_file"], record["source_sha256"]
        )
        verified_pdfs.append(
            {
                "ex_date": record["ex_date"],
                "url": record["source_url"],
                **pdf,
            }
        )
    return records, {**structured, "pdfs": verified_pdfs}


def create_cash_etf_snapshot(
    *,
    tdx_root: Path,
    output_root: Path,
    assets: tuple[EtfAsset, ...] = CASH_ETF_ASSETS,
    start_date: str = SNAPSHOT_START,
    end_date: str = SNAPSHOT_END,
) -> dict[str, Any]:
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Cash-ETF snapshot cannot extend beyond the frozen end")
    frames: list[pd.DataFrame] = []
    prefixes: list[tuple[EtfAsset, bytes, dict[str, Any]]] = []
    for asset in assets:
        path = (
            Path(tdx_root)
            / "vipdoc"
            / asset.market
            / "lday"
            / f"{asset.local_code}.day"
        )
        prefix, source = _read_day_prefix(path, end)
        frame = decode_day_bytes(prefix, asset)
        frame = frame.loc[frame["timestamp"].between(start, end)].reset_index(drop=True)
        _validate_price_frame(frame, asset.code)
        frames.append(frame)
        prefixes.append((asset, prefix, source))
    bars = pd.concat(frames, ignore_index=True).sort_values(
        ["code", "timestamp"]
    ).reset_index(drop=True)

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cash_etf_", dir=str(output_root)))
    try:
        raw_dir = staging / "raw_prefix"
        raw_dir.mkdir()
        sources: list[dict[str, Any]] = []
        for asset, prefix, source in prefixes:
            prefix_name = f"{asset.local_code}_through_{end.strftime('%Y%m%d')}.day"
            prefix_path = raw_dir / prefix_name
            prefix_path.write_bytes(prefix)
            sources.append(
                {
                    "code": asset.code,
                    "name": asset.name,
                    "source_path": source["path"],
                    "source_size_at_read": source["source_size_at_read"],
                    "source_mtime_ns_at_read": source["source_mtime_ns_at_read"],
                    "prefix_file": f"raw_prefix/{prefix_name}",
                    "prefix_bytes": source["prefix_bytes"],
                    "prefix_sha256": source["prefix_sha256"],
                }
            )
        bars_path = staging / "bars.parquet"
        bars.to_parquet(bars_path, index=False)
        bars_hash = _file_sha256(bars_path)
        identity = {
            "data_contract_version": DATA_CONTRACT_VERSION,
            "start_date": str(start.date()),
            "end_date": str(end.date()),
            "sources": [
                {
                    "code": source["code"],
                    "prefix_bytes": source["prefix_bytes"],
                    "prefix_sha256": source["prefix_sha256"],
                }
                for source in sources
            ],
            "bars_sha256": bars_hash,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()
        manifest = {
            **identity,
            "snapshot_id": snapshot_id,
            "sources": sources,
            "bars_file": "bars.parquet",
            "rows": int(len(bars)),
            "rows_by_code": {
                str(code): int(count)
                for code, count in bars.groupby("code").size().sort_index().items()
            },
            "minimum_date": str(bars["timestamp"].min().date()),
            "maximum_date": str(bars["timestamp"].max().date()),
            "duplicate_keys": int(bars.duplicated(["code", "timestamp"]).sum()),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        target = output_root / snapshot_id
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("bars_sha256") != bars_hash:
                raise ValueError(f"Immutable cash-ETF snapshot collision: {snapshot_id}")
            load_cash_etf_snapshot(target)
            return existing
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_cash_etf_snapshot(snapshot_dir: Path) -> pd.DataFrame:
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    bars_path = snapshot_dir / manifest["bars_file"]
    verify_file_hash(bars_path, manifest["bars_sha256"])
    for source in manifest["sources"]:
        verify_file_hash(snapshot_dir / source["prefix_file"], source["prefix_sha256"])
    bars = pd.read_parquet(bars_path)
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="raise").dt.normalize()
    if bars["timestamp"].max() > pd.Timestamp(SNAPSHOT_END):
        raise ValueError("Cash-ETF snapshot contains post-protocol data")
    for code, frame in bars.groupby("code", sort=False):
        _validate_price_frame(frame.reset_index(drop=True), str(code))
    return bars.sort_values(["code", "timestamp"]).reset_index(drop=True)


def reconcile_yinhua_dividends(
    nav: pd.DataFrame,
    bars: pd.DataFrame,
    records: Iterable[dict[str, Any]],
    *,
    maximum_price_error: float = MAX_DIVIDEND_PRICE_ERROR_CNY,
    maximum_cumulative_nav_decline: float = MAX_CUMULATIVE_NAV_DECLINE_CNY,
) -> dict[str, Any]:
    nav = nav.sort_values("timestamp").reset_index(drop=True)
    bars = bars.loc[bars["code"].eq("511880.SH")].sort_values("timestamp").reset_index(
        drop=True
    )
    reconciliations: list[dict[str, Any]] = []
    for record in records:
        ex_date = pd.Timestamp(record["ex_date"]).normalize()
        nav_position = nav.index[nav["timestamp"].eq(ex_date)].tolist()
        bar_position = bars.index[bars["timestamp"].eq(ex_date)].tolist()
        if len(nav_position) != 1 or nav_position[0] == 0:
            raise ValueError(f"Missing Yinhua NAV around ex-date {ex_date.date()}")
        if len(bar_position) != 1 or bar_position[0] == 0:
            raise ValueError(f"Missing Yinhua TDX price around ex-date {ex_date.date()}")
        nav_current = nav.iloc[nav_position[0]]
        nav_previous = nav.iloc[nav_position[0] - 1]
        bar_current = bars.iloc[bar_position[0]]
        bar_previous = bars.iloc[bar_position[0] - 1]
        expected = float(record["dividend_per_unit"])
        unit_nav_delta = float(nav_current["unit_nav"] - nav_previous["unit_nav"])
        cumulative_delta = float(
            nav_current["cumulative_nav"] - nav_previous["cumulative_nav"]
        )
        cumulative_continuity_error = abs(unit_nav_delta - cumulative_delta)
        theoretical_ex_reference = float(bar_previous["Close"] - expected)
        ex_low = float(bar_current["Low"])
        ex_high = float(bar_current["High"])
        if theoretical_ex_reference < ex_low:
            price_range_error = ex_low - theoretical_ex_reference
        elif theoretical_ex_reference > ex_high:
            price_range_error = theoretical_ex_reference - ex_high
        else:
            price_range_error = 0.0
        passed = (
            price_range_error <= maximum_price_error
            and unit_nav_delta >= -maximum_cumulative_nav_decline
            and cumulative_delta >= -maximum_cumulative_nav_decline
            and cumulative_continuity_error <= maximum_cumulative_nav_decline
        )
        reconciliations.append(
            {
                "ex_date": str(ex_date.date()),
                "expected_dividend_per_unit": expected,
                "official_unit_nav_delta": unit_nav_delta,
                "official_cumulative_nav_delta": cumulative_delta,
                "official_nav_continuity_error": cumulative_continuity_error,
                "tdx_previous_close": float(bar_previous["Close"]),
                "tdx_theoretical_ex_reference": theoretical_ex_reference,
                "tdx_ex_day_low": ex_low,
                "tdx_ex_day_high": ex_high,
                "tdx_reference_range_error": price_range_error,
                "passed": bool(passed),
            }
        )
    return {
        "maximum_price_error_cny": maximum_price_error,
        "maximum_cumulative_nav_decline_cny": maximum_cumulative_nav_decline,
        "records": reconciliations,
        "passed": bool(reconciliations and all(item["passed"] for item in reconciliations)),
    }


def assess_readiness(checks: dict[str, bool]) -> dict[str, Any]:
    blockers = sorted(name for name, passed in checks.items() if not bool(passed))
    ready = not blockers
    return {
        "decision": "READY_FOR_DEVELOPMENT" if ready else "DATA_BLOCKED",
        "checks": {name: bool(value) for name, value in checks.items()},
        "blockers": blockers,
        "development_results_unsealed": ready,
        "production_authorized": False,
    }


def run_cash_instrument_readiness(
    *,
    validation_dir: Path,
    tdx_root: Path,
) -> dict[str, Any]:
    validation_dir = Path(validation_dir)
    protocol = verify_frozen_protocol(validation_dir)
    raw_sources = verify_frozen_raw_sources(validation_dir)
    records, dividend_sources = load_yinhua_dividends(validation_dir)
    snapshot_manifest = create_cash_etf_snapshot(
        tdx_root=Path(tdx_root),
        output_root=validation_dir / "snapshots",
    )
    snapshot_dir = validation_dir / "snapshots" / snapshot_manifest["snapshot_id"]
    bars = load_cash_etf_snapshot(snapshot_dir)
    yinhua = parse_yinhua_nav(
        validation_dir / FROZEN_RAW_SOURCES[0].relative_path
    )
    huabao = parse_huabao_income(
        validation_dir / FROZEN_RAW_SOURCES[1].relative_path
    )
    yinhua = apply_conservative_publication_rule(yinhua)
    huabao = apply_conservative_publication_rule(huabao)
    reconciliation = reconcile_yinhua_dividends(yinhua, bars, records)

    yinhua_coverage = _trading_session_coverage(bars, "511880.SH", yinhua)
    huabao_coverage = _trading_session_coverage(bars, "511990.SH", huabao)
    yinhua_observed_publication_coverage = _publication_coverage_for_trading_sessions(
        bars, "511880.SH", yinhua, column="publication_time"
    )
    huabao_observed_publication_coverage = _publication_coverage_for_trading_sessions(
        bars, "511990.SH", huabao, column="publication_time"
    )
    yinhua_rule_publication_coverage = _publication_coverage_for_trading_sessions(
        bars, "511880.SH", yinhua, column="publication_available_at"
    )
    huabao_rule_publication_coverage = _publication_coverage_for_trading_sessions(
        bars, "511990.SH", huabao, column="publication_available_at"
    )
    broker = _assess_broker_rules(validation_dir)
    checks = {
        "frozen_protocol_hash": True,
        "frozen_official_source_hashes": True,
        "immutable_tdx_prefix_snapshot": True,
        "yinhua_official_record_coverage": yinhua_coverage >= MINIMUM_COVERAGE,
        "huabao_official_record_coverage": huabao_coverage >= MINIMUM_COVERAGE,
        "yinhua_point_in_time_publication_coverage": (
            yinhua_rule_publication_coverage >= MINIMUM_COVERAGE
        ),
        "huabao_point_in_time_publication_coverage": (
            huabao_rule_publication_coverage >= MINIMUM_COVERAGE
        ),
        "yinhua_dividend_reconciliation": reconciliation["passed"],
        "exchange_board_lot_100_units": True,
        "exchange_intraday_t_plus_zero": True,
        "account_broker_fee_schedule": broker["passed"],
        "account_broker_settlement_and_t_plus_zero": broker["passed"],
    }
    readiness = assess_readiness(checks)
    source_manifest = {
        "data_contract_version": DATA_CONTRACT_VERSION,
        "protocol": protocol,
        "raw_sources": raw_sources,
        "dividend_sources": dividend_sources,
        "price_snapshot": {
            **snapshot_manifest,
            "path": str(snapshot_dir.resolve()),
        },
        "normalized_datasets": {
            "511880.SH": _dataset_summary(yinhua),
            "511990.SH": _dataset_summary(huabao),
        },
    }
    source_manifest_path = validation_dir / "source_manifest.json"
    source_manifest_path.write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    source_manifest_hash = _file_sha256(source_manifest_path)
    (validation_dir / "source_manifest.sha256").write_text(
        source_manifest_hash + "\n", encoding="utf-8"
    )
    report = {
        "protocol_version": PROTOCOL_VERSION,
        "data_contract_version": DATA_CONTRACT_VERSION,
        "research_status": readiness["decision"],
        "result_window_status": (
            "UNSEALED" if readiness["development_results_unsealed"] else "SEALED"
        ),
        "source_manifest_sha256": source_manifest_hash,
        "candidate_data": {
            "511880.SH": {
                **_dataset_summary(yinhua),
                "trading_session_coverage": yinhua_coverage,
                "observed_publication_timestamp_coverage": (
                    yinhua_observed_publication_coverage
                ),
                "conservative_rule_publication_coverage": (
                    yinhua_rule_publication_coverage
                ),
                "publication_time_basis": (
                    "official next-day deadline; modeled as the next calendar day "
                    "at 23:59:59"
                ),
                "dividend_reconciliation": reconciliation,
            },
            "511990.SH": {
                **_dataset_summary(huabao),
                "trading_session_coverage": huabao_coverage,
                "observed_publication_timestamp_coverage": (
                    huabao_observed_publication_coverage
                ),
                "conservative_rule_publication_coverage": (
                    huabao_rule_publication_coverage
                ),
                "publication_time_basis": (
                    "official next-day deadline; modeled as the next calendar day "
                    "at 23:59:59"
                ),
            },
        },
        "broker_evidence": broker,
        **readiness,
    }
    (validation_dir / "readiness.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return report


def _validate_official_frame(
    frame: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    positive_columns: tuple[str, ...],
    nonnegative_columns: tuple[str, ...],
    dataset: str,
) -> pd.DataFrame:
    frame = frame.loc[
        frame["timestamp"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    ].copy()
    frame.sort_values("timestamp", inplace=True)
    frame.reset_index(drop=True, inplace=True)
    if frame.empty:
        raise ValueError(f"{dataset} has no rows in the frozen window")
    if frame["timestamp"].duplicated().any():
        raise ValueError(f"{dataset} contains duplicate dates")
    invalid = frame[list(positive_columns)].isna().any(axis=1)
    invalid |= frame[list(positive_columns)].le(0.0).any(axis=1)
    if nonnegative_columns:
        invalid |= frame[list(nonnegative_columns)].isna().any(axis=1)
        invalid |= frame[list(nonnegative_columns)].lt(0.0).any(axis=1)
    if invalid.any():
        raise ValueError(f"{dataset} contains {int(invalid.sum())} invalid rows")
    return frame


def _validate_price_frame(frame: pd.DataFrame, code: str) -> None:
    if frame.empty:
        raise ValueError(f"Cash-ETF snapshot is empty for {code}")
    if frame["timestamp"].duplicated().any():
        raise ValueError(f"Cash-ETF DAY data contains duplicate sessions for {code}")
    if not frame["timestamp"].is_monotonic_increasing:
        raise ValueError(f"Cash-ETF DAY data is not sorted for {code}")
    invalid = (
        frame[["Open", "High", "Low", "Close"]].le(0.0).any(axis=1)
        | frame["Low"].gt(frame[["Open", "Close"]].min(axis=1))
        | frame["High"].lt(frame[["Open", "Close"]].max(axis=1))
        | frame["Low"].gt(frame["High"])
        | frame["Volume"].le(0.0)
        | frame["Amount"].le(0.0)
    )
    if invalid.any():
        raise ValueError(f"Cash-ETF DAY data contains {int(invalid.sum())} invalid rows for {code}")


def _read_day_prefix(path: Path, end: pd.Timestamp) -> tuple[bytes, dict[str, Any]]:
    path = Path(path)
    before = path.stat()
    chunks: list[bytes] = []
    previous_date = 0
    cutoff = int(end.strftime("%Y%m%d"))
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(DAY_DTYPE.itemsize)
            if not chunk:
                break
            if len(chunk) != DAY_DTYPE.itemsize:
                raise ValueError(f"Truncated DAY record: {path}")
            date_value = int.from_bytes(chunk[:4], "little", signed=False)
            if date_value < previous_date:
                raise ValueError(f"Unsorted DAY records: {path}")
            previous_date = date_value
            if date_value > cutoff:
                break
            chunks.append(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"DAY source changed while reading: {path}")
    payload = b"".join(chunks)
    return payload, {
        "path": str(path.resolve()),
        "source_size_at_read": int(before.st_size),
        "source_mtime_ns_at_read": int(before.st_mtime_ns),
        "prefix_bytes": len(payload),
        "prefix_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _trading_session_coverage(
    bars: pd.DataFrame, code: str, official: pd.DataFrame
) -> float:
    trading_dates = set(bars.loc[bars["code"].eq(code), "timestamp"])
    official_dates = set(official["timestamp"])
    if not trading_dates:
        return 0.0
    return len(trading_dates.intersection(official_dates)) / len(trading_dates)


def _publication_coverage_for_trading_sessions(
    bars: pd.DataFrame,
    code: str,
    official: pd.DataFrame,
    *,
    column: str,
) -> float:
    trading_dates = set(bars.loc[bars["code"].eq(code), "timestamp"])
    published_dates = set(
        official.loc[official[column].notna(), "timestamp"]
    )
    if not trading_dates:
        return 0.0
    return len(trading_dates.intersection(published_dates)) / len(trading_dates)


def _dataset_summary(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(frame)),
        "start_date": str(frame["timestamp"].min().date()),
        "end_date": str(frame["timestamp"].max().date()),
        "duplicate_dates": int(frame["timestamp"].duplicated().sum()),
    }


def _assess_broker_rules(validation_dir: Path) -> dict[str, Any]:
    path = Path(validation_dir) / "broker_rules.json"
    if not path.is_file():
        return {
            "passed": False,
            "status": "MISSING",
            "required_path": str(path.resolve()),
            "reason": (
                "Account-specific commission, minimum commission, board-lot, and "
                "same-day sell-proceeds reuse evidence has not been frozen."
            ),
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != DATA_CONTRACT_VERSION:
        raise ValueError("Unsupported broker-rules schema version")
    instruments = payload.get("instruments")
    if not isinstance(instruments, list):
        raise ValueError("Broker rules must contain an instruments list")
    by_code = {item.get("code"): item for item in instruments}
    checks: dict[str, bool] = {}
    evidence: list[dict[str, Any]] = []
    for code in ("511880.SH", "511990.SH"):
        item = by_code.get(code, {})
        source_file = item.get("source_file")
        source_hash = item.get("source_sha256")
        source_ok = bool(source_file and source_hash)
        if source_ok:
            verified = verify_file_hash(Path(validation_dir) / source_file, source_hash)
            evidence.append({"code": code, **verified})
        coverage_start = pd.to_datetime(item.get("coverage_start"), errors="coerce")
        coverage_end = pd.to_datetime(item.get("coverage_end"), errors="coerce")
        checks[code] = bool(
            source_ok
            and int(item.get("board_lot", 0)) == 100
            and item.get("t_plus_zero_sell_proceeds_reusable") is True
            and float(item.get("commission_rate", -1.0)) >= 0.0
            and float(item.get("minimum_commission_cny", -1.0)) >= 0.0
            and pd.notna(coverage_start)
            and pd.notna(coverage_end)
            and coverage_start <= pd.Timestamp(SNAPSHOT_START)
            and coverage_end >= pd.Timestamp(SNAPSHOT_END)
        )
    passed = bool(checks and all(checks.values()))
    return {
        "passed": passed,
        "status": "VERIFIED" if passed else "INCOMPLETE",
        "checks": checks,
        "evidence": evidence,
    }


def _file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
