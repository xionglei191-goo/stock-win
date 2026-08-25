from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .config import PlatformConfig, PortfolioConfig
from .etf_pullback_research import DAY_DTYPE, decode_day_bytes
from .storage import Database, ParquetSnapshotStore


PROTOCOL_VERSION = "1.0.0"
HYPOTHESIS_ID = "domestic_equity_etf_cross_sectional_reversal"
FROZEN_PROTOCOL_SHA256 = "dce16d38f57fd38a9fbe8b5b0c3f2619046e6aa29e5e577fe23e579a88c473ab"
DEVELOPMENT_END = "2024-06-28"
SNAPSHOT_START = "2019-01-01"
TNF_HEADER_BYTES = 50
TNF_RECORD_BYTES = 360
TNF_NAME_START = 31
TNF_NAME_END = 71
EXCLUDED_SH_PREFIXES = ("511", "513", "518")
EXCLUDED_CODES = frozenset(
    {"159003.SZ", "159005.SZ", "159980.SZ", "159981.SZ", "159985.SZ"}
)
EXCLUDED_NAME_TOKENS = tuple(
    value.encode("ascii").decode("unicode_escape")
    for value in (
        r"\u503a",
        r"\u8d27\u5e01",
        r"\u73b0\u91d1",
        r"\u540c\u4e1a\u5b58\u5355",
        r"\u5b58\u5355",
        r"\u77ed\u878d",
        r"\u65e5\u65e5\u946b",
        r"\u5929\u5929\u91d1",
        r"\u6dfb\u5229",
        r"\u8d22\u5bcc\u5b9d",
        r"\u65e5\u5229",
        r"\u6dfb\u76ca",
        r"\u9ec4\u91d1",
        r"\u767d\u94f6",
        r"\u91d1ETF",
        r"\u8c46\u7c95",
        r"\u80fd\u6e90\u5316\u5de5",
        r"\u539f\u6cb9",
        r"\u6cb9\u6c14",
        r"\u6052\u751f",
        r"\u7eb3\u6307",
        r"\u7eb3\u65af\u8fbe\u514b",
        r"\u6807\u666e",
        r"\u9053\u743c\u65af",
        r"\u65e5\u7ecf",
        r"\u65e5\u672c",
        r"\u7f8e\u56fd",
        r"\u5fb7\u56fd",
        r"\u6cd5\u56fd",
        r"\u6e2f\u80a1",
        r"\u9999\u6e2f",
        r"\u4e2d\u6982",
        r"\u6d77\u5916",
        r"\u6c99\u7279",
        r"\u5370\u5ea6",
        r"\u4e1c\u5357\u4e9a",
        r"\u4e9a\u592a",
        r"\u7f8e\u80a1",
        r"\u97e9\u56fd",
        r"\u4e2d\u97e9",
        r"\u65b0\u52a0\u5761",
    )
)


@dataclass(frozen=True)
class EquityEtfAsset:
    code: str
    name: str
    market: str
    local_code: str


@dataclass(frozen=True)
class ResearchWindow:
    label: str
    role: str
    start_date: str
    end_date: str
    evaluation_weight: float


WINDOWS = (
    ResearchWindow("dev_2021_2022", "DEVELOPMENT", "2021-04-01", "2022-04-29", 0.05),
    ResearchWindow("dev_2022_2023", "DEVELOPMENT", "2022-05-01", "2023-05-31", 0.05),
    ResearchWindow("dev_2023_2024", "DEVELOPMENT", "2023-06-01", "2024-06-28", 0.05),
    ResearchWindow("replication_2024_2025", "REPLICATION", "2024-07-01", "2025-07-24", 0.25),
    ResearchWindow("holdout_2025_2026", "HOLDOUT", "2025-07-25", "2026-08-07", 0.60),
)
DEVELOPMENT_MARKET_SNAPSHOT_IDS = (
    "bt_89d697919ea74826abe4a7702bd0a3e9",
    "bt_4bec5474e50b44bdb53aff39bb4075ca",
    "bt_e40fe0fd8a2546729bbfe591b768c27a",
)


def decode_tnf_security_master(path: Path, market: str) -> pd.DataFrame:
    """Decode the current TDX security-name cache without invoking the client."""

    data = Path(path).read_bytes()
    if len(data) < TNF_HEADER_BYTES or (len(data) - TNF_HEADER_BYTES) % TNF_RECORD_BYTES:
        raise ValueError(f"Invalid TNF payload size: {path}")
    suffix = ".SH" if market.lower() == "sh" else ".SZ"
    rows: list[dict[str, str]] = []
    for offset in range(TNF_HEADER_BYTES, len(data), TNF_RECORD_BYTES):
        record = data[offset : offset + TNF_RECORD_BYTES]
        code = record[:6].decode("ascii", errors="ignore")
        if len(code) != 6 or not code.isdigit():
            continue
        name = (
            record[TNF_NAME_START:TNF_NAME_END]
            .split(b"\0", 1)[0]
            .decode("gbk", errors="ignore")
            .strip()
        )
        rows.append({"code": f"{code}{suffix}", "name": name})
    return pd.DataFrame(rows).drop_duplicates("code", keep="last").reset_index(drop=True)


def is_domestic_equity_etf(code: str, name: str) -> bool:
    normalized_code = str(code).upper()
    normalized_name = str(name).upper()
    if "ETF" not in normalized_name:
        return False
    local_code = normalized_code.split(".", 1)[0]
    if normalized_code.endswith(".SZ") and not local_code.startswith("159"):
        return False
    if normalized_code.endswith(".SH") and local_code.startswith(EXCLUDED_SH_PREFIXES):
        return False
    if normalized_code in EXCLUDED_CODES:
        return False
    return not any(token.upper() in normalized_name for token in EXCLUDED_NAME_TOKENS)


def discover_current_domestic_equity_etfs(tdx_root: Path) -> tuple[EquityEtfAsset, ...]:
    """Discover the current survivor roster; callers must retain that caveat."""

    root = Path(tdx_root)
    assets: list[EquityEtfAsset] = []
    for market, cache_name, pattern in (
        ("sh", "shs.tnf", "sh5*.day"),
        ("sz", "szs.tnf", "sz1*.day"),
    ):
        master = decode_tnf_security_master(root / "T0002" / "hq_cache" / cache_name, market)
        names = dict(master.loc[:, ["code", "name"]].itertuples(index=False, name=None))
        suffix = ".SH" if market == "sh" else ".SZ"
        for path in sorted((root / "vipdoc" / market / "lday").glob(pattern)):
            local_code = path.stem.lower()
            code = f"{local_code[2:]}{suffix}"
            name = str(names.get(code, ""))
            if is_domestic_equity_etf(code, name):
                assets.append(EquityEtfAsset(code, name, market, local_code))
    return tuple(sorted(assets, key=lambda item: item.code))


def protocol_manifest() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "research_status": "development_only",
        "population": {
            "definition": "current TDX-listed domestic equity ETFs with local DAY history",
            "classification": {
                "requires_name_token": "ETF",
                "excluded_sh_prefixes": list(EXCLUDED_SH_PREFIXES),
                "excluded_codes": sorted(EXCLUDED_CODES),
                "excluded_name_tokens": list(EXCLUDED_NAME_TOKENS),
            },
            "known_bias": "current-survivor roster omits ETFs liquidated before 2026-08-10",
            "promotion_block": "historical listed-and-liquidated ETF roster must be audited first",
        },
        "data": {
            "source": "local TDX TNF security cache and unadjusted DAY files",
            "snapshot_start": SNAPSHOT_START,
            "development_end": DEVELOPMENT_END,
            "future_windows_excluded_from_development_snapshot": True,
        },
        "signal": {
            "signal_time": "daily close",
            "minimum_history_sessions": 200,
            "minimum_amount_20d": 50_000_000,
            "close_above_ma200": True,
            "positive_return_120d": True,
            "top_return_120d_cross_section": 0.30,
            "return_3d_maximum": -0.03,
            "return_3d_at_or_below_prior_q10": True,
            "prior_quantile_window": 252,
            "prior_quantile_minimum_observations": 126,
            "bottom_return_3d_cross_section": 0.20,
            "close_below_ma10": True,
            "maximum_volume_ratio": 2.0,
            "benchmark_close_above_ma120": True,
            "repeat_signal": "false-to-true transition only",
        },
        "ranking": {
            "score": {
                "oversold_depth": 0.45,
                "return_120d": 0.30,
                "liquidity": 0.25,
            },
            "maximum_daily_candidates": 3,
            "correlation_lookback": 60,
            "minimum_correlation_observations": 40,
            "maximum_pair_correlation": 0.95,
            "tie_break": "code ascending",
        },
        "execution": {
            "entry": "next trading-day raw open",
            "entry_gap_bounds": [-0.03, 0.03],
            "target_weight": 0.10,
            "maximum_positions": 3,
            "exit": "next open after close reclaims MA10 or closes 8% below entry",
            "maximum_holding_sessions": 10,
            "t_plus_one": True,
            "costs": "stock commission, minimum commission, stamp duty, and fixed slippage",
        },
        "development_gate": {
            "minimum_trades_per_window": 30,
            "minimum_annualized_return_per_window": 0.05,
            "positive_total_return": True,
            "positive_median_trade": True,
            "positive_ex_top3_contribution": True,
            "maximum_mark_to_market_drawdown": -0.10,
            "minimum_fill_rate": 0.60,
            "all_development_windows_must_pass": True,
            "passing_action": "freeze candidate and acquire survivor-complete historical roster",
        },
        "windows": [asdict(window) for window in WINDOWS],
        "opening_rule": "development, survivor audit, replication, then holdout",
    }


def save_protocol(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = protocol_manifest()
    serialized = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path = output_dir / "protocol.json"
    if path.exists() and path.read_bytes() != serialized:
        raise ValueError(f"Frozen protocol already exists with different content: {path}")
    path.write_bytes(serialized)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(serialized).hexdigest()}


def create_development_snapshot(
    *,
    tdx_root: Path,
    output_root: Path,
    assets: Sequence[EquityEtfAsset] | None = None,
    start_date: str = SNAPSHOT_START,
    end_date: str = DEVELOPMENT_END,
) -> dict[str, Any]:
    """Publish a content-addressed snapshot truncated before replication."""

    selected_assets = tuple(assets or discover_current_domestic_equity_etfs(tdx_root))
    if not selected_assets:
        raise ValueError("No domestic equity ETF assets were discovered")
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end > pd.Timestamp(DEVELOPMENT_END):
        raise ValueError("Development snapshot cannot include replication or holdout dates")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".equity_etf_", dir=str(output_root)))
    try:
        frames: list[pd.DataFrame] = []
        sources: list[dict[str, Any]] = []
        for asset in selected_assets:
            path = Path(tdx_root) / "vipdoc" / asset.market / "lday" / f"{asset.local_code}.day"
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError(f"DAY source changed while reading: {path}")
            decoded = decode_day_bytes(data, asset)
            decoded = decoded.loc[pd.to_datetime(decoded["timestamp"]).between(start, end)]
            if not decoded.empty:
                frames.append(decoded)
            sources.append(
                {
                    "code": asset.code,
                    "name": asset.name,
                    "path": str(path.resolve()),
                    "bytes": int(before.st_size),
                    "mtime_ns": int(before.st_mtime_ns),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "snapshot_rows": int(len(decoded)),
                }
            )
        bars = pd.concat(frames, ignore_index=True).sort_values(
            ["code", "timestamp"]
        ).reset_index(drop=True)
        if bars.duplicated(["code", "timestamp"]).any():
            raise ValueError("ETF snapshot contains duplicate code-session keys")
        invalid_ohlc = (
            bars["Low"].gt(bars[["Open", "Close"]].min(axis=1))
            | bars["High"].lt(bars[["Open", "Close"]].max(axis=1))
            | bars["Low"].gt(bars["High"])
            | bars[["Open", "High", "Low", "Close"]].le(0.0).any(axis=1)
        )
        if invalid_ohlc.any():
            raise ValueError(f"ETF snapshot contains {int(invalid_ohlc.sum())} invalid OHLC rows")
        bars_path = staging / "bars.parquet"
        bars.to_parquet(bars_path, index=False)
        bars_hash = _file_sha256(bars_path)
        identity = {
            "protocol_version": PROTOCOL_VERSION,
            "start_date": str(start.date()),
            "end_date": str(end.date()),
            "survivor_roster_asof": "2026-08-10",
            "assets": [asdict(asset) for asset in selected_assets],
            "source_files": sources,
            "bars_sha256": bars_hash,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        manifest = {
            **identity,
            "snapshot_id": snapshot_id,
            "bars_rows": int(len(bars)),
            "bars_codes": int(bars["code"].nunique()),
            "duplicate_keys": 0,
            "invalid_ohlc": 0,
            "known_bias": "current-survivor roster",
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        target = output_root / snapshot_id
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("bars_sha256") != bars_hash:
                raise ValueError(f"Immutable ETF snapshot collision: {snapshot_id}")
            return existing
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_development_snapshot(snapshot_dir: Path) -> pd.DataFrame:
    snapshot_dir = Path(snapshot_dir)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    path = snapshot_dir / "bars.parquet"
    actual_hash = _file_sha256(path)
    if actual_hash != manifest.get("bars_sha256"):
        raise ValueError(
            f"ETF snapshot hash mismatch: expected={manifest.get('bars_sha256')}, actual={actual_hash}"
        )
    bars = pd.read_parquet(path)
    if pd.to_datetime(bars["timestamp"]).max() > pd.Timestamp(DEVELOPMENT_END):
        raise ValueError("Development snapshot contains a future-window row")
    return bars


def load_development_market_index(
    config: PlatformConfig,
    database: Database,
    snapshot_ids: Sequence[str] = DEVELOPMENT_MARKET_SNAPSHOT_IDS,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    store = ParquetSnapshotStore(config, database)
    frames: list[pd.DataFrame] = []
    sources: list[dict[str, Any]] = []
    for snapshot_id in snapshot_ids:
        frame = store.load_records(snapshot_id, "market_index")
        query = store.dataset_query(snapshot_id, "market_index")
        frame = frame.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.normalize()
        frame.dropna(subset=["timestamp"], inplace=True)
        frames.append(frame)
        sources.append(
            {
                "snapshot_id": snapshot_id,
                "rows": int(len(frame)),
                "start_date": str(frame["timestamp"].min().date()),
                "end_date": str(frame["timestamp"].max().date()),
                "query": query,
            }
        )
    combined = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    value_columns = ["Open", "High", "Low", "Close", "Volume", "Amount"]
    conflicts = combined.groupby("timestamp", sort=False)[value_columns].agg(
        lambda values: pd.to_numeric(values, errors="coerce").max()
        - pd.to_numeric(values, errors="coerce").min()
    )
    if conflicts.fillna(0.0).gt(1e-8).any(axis=None):
        raise ValueError("Development market snapshots disagree on overlapping sessions")
    combined = combined.drop_duplicates("timestamp", keep="first").reset_index(drop=True)
    return combined, sources


def build_cross_sectional_reversal_events(
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> pd.DataFrame:
    if execution_cost_multiplier <= 0:
        raise ValueError("execution_cost_multiplier must be positive")
    if bars.empty or market_index.empty:
        return pd.DataFrame()
    frame = _prepare_features(bars, market_index)
    base_eligible = (
        frame["ma200"].notna()
        & frame["return_120d"].notna()
        & frame["amount_20d"].ge(50_000_000.0)
    )
    frame["momentum_percentile"] = np.nan
    frame.loc[base_eligible, "momentum_percentile"] = frame.loc[base_eligible].groupby(
        "timestamp", sort=False
    )["return_120d"].rank(method="average", pct=True, ascending=False)
    frame["pullback_percentile"] = np.nan
    frame.loc[base_eligible, "pullback_percentile"] = frame.loc[base_eligible].groupby(
        "timestamp", sort=False
    )["return_3d"].rank(method="average", pct=True, ascending=True)
    eligible = (
        base_eligible
        & frame["Close"].gt(frame["ma200"])
        & frame["return_120d"].gt(0.0)
        & frame["momentum_percentile"].le(0.30)
        & frame["return_3d"].le(-0.03)
        & frame["return_3d"].le(frame["return_3d_q10"])
        & frame["pullback_percentile"].le(0.20)
        & frame["Close"].lt(frame["ma10"])
        & frame["volume_ratio"].le(2.0)
        & frame["market_allowed"].fillna(False)
    ).fillna(False)
    previous = eligible.groupby(frame["code"], sort=False).shift(1).fillna(False)
    trigger = eligible & ~previous.astype(bool)
    candidates = frame.loc[trigger].copy()
    if candidates.empty:
        return pd.DataFrame()
    candidates["oversold_quality"] = (-candidates["return_3d"] / 0.12).clip(0.0, 1.0)
    candidates["trend_quality"] = (candidates["return_120d"] / 0.50).clip(0.0, 1.0)
    candidates["liquidity_quality"] = candidates.groupby("timestamp", sort=False)[
        "amount_20d"
    ].rank(method="average", pct=True)
    candidates["score"] = (
        0.45 * candidates["oversold_quality"]
        + 0.30 * candidates["trend_quality"]
        + 0.25 * candidates["liquidity_quality"]
    )
    candidates.sort_values(
        ["timestamp", "score", "code"], ascending=[True, False, True], inplace=True
    )
    return_pivot = frame.pivot(index="timestamp", columns="code", values="daily_return")
    selected_rows: list[dict[str, Any]] = []
    for signal_date, day in candidates.groupby("timestamp", sort=True):
        chosen: list[str] = []
        for row in day.itertuples(index=False):
            record = row._asdict()
            record["signal_date"] = pd.Timestamp(signal_date).normalize()
            record["blocked_correlation"] = False
            record["blocked_daily_capacity"] = False
            if len(chosen) >= 3:
                record["blocked_daily_capacity"] = True
            elif any(
                _trailing_correlation(
                    return_pivot,
                    pd.Timestamp(signal_date),
                    str(row.code),
                    chosen_code,
                )
                >= 0.95
                for chosen_code in chosen
            ):
                record["blocked_correlation"] = True
            else:
                chosen.append(str(row.code))
            record["selected"] = not (
                record["blocked_correlation"] or record["blocked_daily_capacity"]
            )
            record["daily_rank"] = len(chosen) if record["selected"] else None
            selected_rows.append(record)
    events = pd.DataFrame(selected_rows)
    events = _annotate_event_execution(
        events,
        frame,
        execution_config or PortfolioConfig(),
        execution_cost_multiplier,
    )
    keep = [
        "code",
        "name",
        "signal_date",
        "return_3d",
        "return_3d_q10",
        "return_3d_z",
        "return_120d",
        "momentum_percentile",
        "pullback_percentile",
        "amount_20d",
        "volume_ratio",
        "ma10",
        "ma200",
        "market_close",
        "market_ma120",
        "score",
        "daily_rank",
        "selected",
        "blocked_correlation",
        "blocked_daily_capacity",
        "blocked_missing_entry",
        "blocked_entry_gap",
        "blocked_missing_exit",
        "executable",
        "entry_date",
        "entry_open",
        "entry_gap",
        "exit_date",
        "exit_open",
        "exit_reason",
        "holding_sessions",
        "quantity",
        "net_return",
    ]
    return events.loc[:, keep].sort_values(
        ["signal_date", "score", "code"], ascending=[True, False, True]
    ).reset_index(drop=True)


def _prepare_features(bars: pd.DataFrame, market_index: pd.DataFrame) -> pd.DataFrame:
    required = {"code", "name", "timestamp", "Open", "High", "Low", "Close", "Amount", "Volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars is missing columns: {sorted(missing)}")
    frame = bars.loc[:, sorted(required)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce").dt.normalize()
    frame.dropna(subset=["timestamp"], inplace=True)
    frame.sort_values(["code", "timestamp"], inplace=True)
    if frame.duplicated(["code", "timestamp"]).any():
        raise ValueError("bars contains duplicate code-session keys")
    grouped = frame.groupby("code", sort=False)
    frame["daily_return"] = grouped["Close"].pct_change(fill_method=None)
    frame["return_3d"] = frame["Close"] / grouped["Close"].shift(3) - 1.0
    frame["return_120d"] = frame["Close"] / grouped["Close"].shift(120) - 1.0
    frame["ma10"] = grouped["Close"].transform(
        lambda values: values.rolling(10, min_periods=10).mean()
    )
    frame["ma200"] = grouped["Close"].transform(
        lambda values: values.rolling(200, min_periods=200).mean()
    )
    frame["amount_20d"] = grouped["Amount"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    previous_volume = grouped["Volume"].transform(
        lambda values: values.shift(1).rolling(20, min_periods=20).mean()
    )
    frame["volume_ratio"] = frame["Volume"] / previous_volume.replace(0.0, np.nan)
    frame["return_3d_q10"] = grouped["return_3d"].transform(
        lambda values: values.shift(1).rolling(252, min_periods=126).quantile(0.10)
    )
    return_volatility = grouped["return_3d"].transform(
        lambda values: values.shift(1).rolling(252, min_periods=126).std(ddof=0)
    )
    frame["return_3d_z"] = frame["return_3d"] / return_volatility.replace(0.0, np.nan)

    market = market_index.copy()
    market["timestamp"] = pd.to_datetime(market["timestamp"], errors="coerce").dt.normalize()
    market.dropna(subset=["timestamp"], inplace=True)
    market.sort_values("timestamp", inplace=True)
    if market["timestamp"].duplicated().any():
        raise ValueError("market_index must contain one row per session")
    market["market_close"] = pd.to_numeric(market["Close"], errors="coerce")
    market["market_ma120"] = market["market_close"].rolling(120, min_periods=120).mean()
    market["market_allowed"] = market["market_close"].gt(market["market_ma120"])
    return frame.merge(
        market.loc[:, ["timestamp", "market_close", "market_ma120", "market_allowed"]],
        on="timestamp",
        how="left",
        validate="many_to_one",
    )


def _trailing_correlation(
    returns: pd.DataFrame,
    signal_date: pd.Timestamp,
    code: str,
    other_code: str,
    *,
    lookback: int = 60,
    minimum_observations: int = 40,
) -> float:
    if code not in returns or other_code not in returns:
        return -1.0
    pair = returns.loc[:signal_date, [code, other_code]].tail(lookback).dropna()
    if len(pair) < minimum_observations:
        return -1.0
    correlation = pair[code].corr(pair[other_code])
    return float(correlation) if np.isfinite(correlation) else -1.0


def _annotate_event_execution(
    events: pd.DataFrame,
    feature_frame: pd.DataFrame,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> pd.DataFrame:
    result = events.copy()
    result["blocked_missing_entry"] = False
    result["blocked_entry_gap"] = False
    result["blocked_missing_exit"] = False
    result["executable"] = False
    for column in (
        "entry_open",
        "entry_gap",
        "exit_open",
        "net_return",
    ):
        result[column] = np.nan
    result["entry_date"] = pd.NaT
    result["exit_date"] = pd.NaT
    result["exit_reason"] = ""
    result["holding_sessions"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    result["quantity"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    for code, indexes in result.loc[result["selected"]].groupby("code", sort=False).groups.items():
        history = feature_frame.loc[feature_frame["code"].eq(code)].reset_index(drop=True)
        positions = {pd.Timestamp(value).normalize(): index for index, value in history["timestamp"].items()}
        for event_index in indexes:
            signal_date = pd.Timestamp(result.at[event_index, "signal_date"]).normalize()
            signal_position = positions.get(signal_date)
            if signal_position is None or signal_position + 1 >= len(history):
                result.at[event_index, "blocked_missing_entry"] = True
                continue
            signal = history.iloc[signal_position]
            entry_position = signal_position + 1
            entry = history.iloc[entry_position]
            entry_open = float(entry["Open"])
            entry_gap = entry_open / float(signal["Close"]) - 1.0
            result.at[event_index, "entry_date"] = pd.Timestamp(entry["timestamp"])
            result.at[event_index, "entry_open"] = entry_open
            result.at[event_index, "entry_gap"] = entry_gap
            if not -0.03 <= entry_gap <= 0.03:
                result.at[event_index, "blocked_entry_gap"] = True
                continue
            maximum_exit_position = entry_position + 10
            exit_position: int | None = None
            exit_reason = "MAX_HOLDING"
            for position in range(entry_position, min(maximum_exit_position, len(history))):
                observed = history.iloc[position]
                if float(observed["Close"]) <= entry_open * 0.92:
                    exit_position = position + 1
                    exit_reason = "FIXED_STOP_CLOSE"
                    break
                if float(observed["Close"]) >= float(observed["ma10"]):
                    exit_position = position + 1
                    exit_reason = "MA10_RECLAIM"
                    break
            if exit_position is None:
                exit_position = maximum_exit_position
            if exit_position >= len(history):
                result.at[event_index, "blocked_missing_exit"] = True
                continue
            exit_bar = history.iloc[exit_position]
            quantity, net_return = _trade_quantity_and_return(
                entry_open,
                float(exit_bar["Open"]),
                config,
                cost_multiplier,
            )
            if quantity <= 0 or not np.isfinite(net_return):
                result.at[event_index, "blocked_missing_entry"] = True
                continue
            result.at[event_index, "exit_date"] = pd.Timestamp(exit_bar["timestamp"])
            result.at[event_index, "exit_open"] = float(exit_bar["Open"])
            result.at[event_index, "exit_reason"] = exit_reason
            result.at[event_index, "holding_sessions"] = int(exit_position - entry_position)
            result.at[event_index, "quantity"] = int(quantity)
            result.at[event_index, "net_return"] = float(net_return)
            result.at[event_index, "executable"] = True
    return result


def _trade_quantity_and_return(
    entry_open: float,
    exit_open: float,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> tuple[int, float]:
    if not np.isfinite(entry_open) or not np.isfinite(exit_open) or entry_open <= 0.0:
        return 0, np.nan
    buy_price = entry_open * (1.0 + config.slippage_rate * cost_multiplier)
    target_cash = config.initial_cash * 0.10
    quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
    if quantity <= 0:
        return 0, np.nan
    buy_value = buy_price * quantity
    buy_fee = max(
        config.min_commission * cost_multiplier,
        buy_value * config.commission_rate * cost_multiplier,
    )
    sell_price = exit_open * (1.0 - config.slippage_rate * cost_multiplier)
    sell_value = sell_price * quantity
    sell_fee = max(
        config.min_commission * cost_multiplier,
        sell_value * config.commission_rate * cost_multiplier,
    ) + sell_value * config.stamp_duty_rate * cost_multiplier
    net_return = (sell_value - sell_fee - buy_value - buy_fee) / (buy_value + buy_fee)
    return quantity, float(net_return)


def evaluate_development_window(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    market_index: pd.DataFrame,
    window: ResearchWindow,
    *,
    execution_config: PortfolioConfig | None = None,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    start = pd.Timestamp(window.start_date).normalize()
    end = pd.Timestamp(window.end_date).normalize()
    scoped = events.loc[pd.to_datetime(events["signal_date"]).between(start, end)].copy()
    selected = scoped.loc[scoped["selected"]]
    complete = selected.loc[
        selected["executable"] & pd.to_datetime(selected["exit_date"]).le(end)
    ].copy()
    calendar = market_index.copy()
    calendar["timestamp"] = pd.to_datetime(calendar["timestamp"]).dt.normalize()
    calendar = calendar.loc[calendar["timestamp"].between(start, end)]
    portfolio = simulate_mark_to_market_portfolio(
        complete,
        bars,
        calendar["timestamp"].tolist(),
        config=execution_config or PortfolioConfig(),
        cost_multiplier=execution_cost_multiplier,
    )
    accepted_returns = pd.Series(portfolio.pop("accepted_trade_returns"), dtype=float)
    ex_top3 = accepted_returns.sort_values(ascending=False).iloc[3:]
    attempted = int(len(selected))
    return {
        "window": asdict(window),
        "raw_signals": int(len(scoped)),
        "selected_signals": attempted,
        "executable_signals": int(len(complete)),
        "blocked_correlation": int(scoped["blocked_correlation"].sum()),
        "blocked_daily_capacity": int(scoped["blocked_daily_capacity"].sum()),
        "blocked_entry_gap": int(selected["blocked_entry_gap"].sum()),
        "blocked_missing_bars": int(
            selected[["blocked_missing_entry", "blocked_missing_exit"]].any(axis=1).sum()
        ),
        "fill_rate": float(len(complete) / attempted) if attempted else 0.0,
        "median_trade_return": (
            float(accepted_returns.median()) if not accepted_returns.empty else None
        ),
        "mean_trade_return": (
            float(accepted_returns.mean()) if not accepted_returns.empty else None
        ),
        "win_rate": (
            float((accepted_returns > 0.0).mean()) if not accepted_returns.empty else None
        ),
        "ex_top3_contribution": float(ex_top3.sum() * 0.10),
        **portfolio,
    }


def simulate_mark_to_market_portfolio(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: Sequence[Any],
    *,
    config: PortfolioConfig,
    cost_multiplier: float,
) -> dict[str, Any]:
    initial_cash = float(config.initial_cash)
    cash = initial_cash
    positions: dict[str, dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    close_lookup = bars.copy()
    close_lookup["timestamp"] = pd.to_datetime(close_lookup["timestamp"]).dt.normalize()
    closes = close_lookup.pivot(index="timestamp", columns="code", values="Close")
    entries = {
        pd.Timestamp(day).normalize(): group.sort_values(
            ["score", "code"], ascending=[False, True]
        )
        for day, group in events.groupby("entry_date", sort=True)
    }
    for value in sorted({pd.Timestamp(day).normalize() for day in calendar}):
        for code in sorted(
            [code for code, position in positions.items() if position["exit_date"] <= value]
        ):
            position = positions.pop(code)
            sell_price = float(position["exit_open"]) * (
                1.0 - config.slippage_rate * cost_multiplier
            )
            sell_value = sell_price * position["quantity"]
            sell_fee = max(
                config.min_commission * cost_multiplier,
                sell_value * config.commission_rate * cost_multiplier,
            ) + sell_value * config.stamp_duty_rate * cost_multiplier
            cash += sell_value - sell_fee
            accepted[position["accepted_index"]]["realized_exit_value"] = sell_value - sell_fee
        for _, event in entries.get(value, pd.DataFrame()).iterrows():
            code = str(event["code"])
            if code in positions or len(positions) >= 3:
                continue
            buy_price = float(event["entry_open"]) * (
                1.0 + config.slippage_rate * cost_multiplier
            )
            target_cash = min(initial_cash * 0.10, cash)
            quantity = int(target_cash // (buy_price * config.board_lot)) * config.board_lot
            if quantity <= 0:
                continue
            buy_value = buy_price * quantity
            buy_fee = max(
                config.min_commission * cost_multiplier,
                buy_value * config.commission_rate * cost_multiplier,
            )
            if buy_value + buy_fee > cash:
                continue
            cash -= buy_value + buy_fee
            accepted_index = len(accepted)
            accepted.append(
                {
                    "code": code,
                    "net_return": float(event["net_return"]),
                    "entry_cost": buy_value + buy_fee,
                    "realized_exit_value": np.nan,
                }
            )
            positions[code] = {
                "quantity": quantity,
                "exit_date": pd.Timestamp(event["exit_date"]).normalize(),
                "exit_open": float(event["exit_open"]),
                "accepted_index": accepted_index,
                "last_close": float(event["entry_open"]),
            }
        market_value = 0.0
        for code, position in positions.items():
            if value in closes.index and code in closes and pd.notna(closes.at[value, code]):
                position["last_close"] = float(closes.at[value, code])
            market_value += position["quantity"] * position["last_close"]
        equity_rows.append({"timestamp": value, "equity": cash + market_value})
    equity = pd.DataFrame(equity_rows)
    if equity.empty:
        total_return = 0.0
        max_drawdown = 0.0
    else:
        total_return = float(equity["equity"].iloc[-1] / initial_cash - 1.0)
        max_drawdown = float((equity["equity"] / equity["equity"].cummax() - 1.0).min())
    years = max(len(equity) / 252.0, 1.0 / 252.0)
    annualized = float((1.0 + total_return) ** (1.0 / years) - 1.0)
    return {
        "portfolio_trades": int(len(accepted)),
        "portfolio_total_return": total_return,
        "portfolio_annualized_return": annualized,
        "portfolio_max_drawdown": max_drawdown,
        "portfolio_final_equity": float(initial_cash * (1.0 + total_return)),
        "accepted_trade_returns": [float(item["net_return"]) for item in accepted],
    }


def assess_development(reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    development = [
        report
        for report in reports
        if str(report.get("window", {}).get("role", "")).upper() == "DEVELOPMENT"
    ]
    if not development:
        raise ValueError("At least one development report is required")
    checks: list[dict[str, Any]] = []
    for report in development:
        window_checks = {
            "minimum_trades": int(report["portfolio_trades"]) >= 30,
            "minimum_annualized_return": float(report["portfolio_annualized_return"]) >= 0.05,
            "positive_total_return": float(report["portfolio_total_return"]) > 0.0,
            "positive_median_trade": (
                report["median_trade_return"] is not None
                and float(report["median_trade_return"]) > 0.0
            ),
            "positive_ex_top3_contribution": float(report["ex_top3_contribution"]) > 0.0,
            "maximum_drawdown": float(report["portfolio_max_drawdown"]) >= -0.10,
            "minimum_fill_rate": float(report["fill_rate"]) >= 0.60,
        }
        checks.append(
            {
                "window": str(report["window"]["label"]),
                "checks": window_checks,
                "passed": all(window_checks.values()),
            }
        )
    passed = len(checks) == 3 and all(item["passed"] for item in checks)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "hypothesis_id": HYPOTHESIS_ID,
        "decision": "REQUIRE_SURVIVOR_AUDIT" if passed else "REJECT",
        "development_qualified": passed,
        "checks": checks,
        "survivor_audit_required": passed,
        "replication_opened": False,
        "holdout_opened": False,
    }


def write_development_artifacts(
    output_dir: Path,
    reports: Sequence[Mapping[str, Any]],
    events: pd.DataFrame,
    decision: Mapping[str, Any],
    *,
    protocol_sha256: str,
    snapshot_id: str,
) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    events_path = output_dir / "development_events.parquet"
    events.to_parquet(events_path, index=False)
    payload = {
        "protocol_sha256": protocol_sha256,
        "snapshot_id": snapshot_id,
        "reports": [dict(report) for report in reports],
        "decision": dict(decision),
        "events_sha256": _file_sha256(events_path),
    }
    result_path = output_dir / "development_result.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return {"result": str(result_path.resolve()), "events": str(events_path.resolve())}


def run_frozen_development(
    config: PlatformConfig,
    database: Database,
    *,
    snapshot_dir: Path,
    output_dir: Path,
    execution_cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    protocol_path = Path(output_dir) / "protocol.json"
    protocol_hash = _file_sha256(protocol_path)
    if protocol_hash != FROZEN_PROTOCOL_SHA256:
        raise ValueError(
            "Frozen ETF protocol hash mismatch: "
            f"expected={FROZEN_PROTOCOL_SHA256}, actual={protocol_hash}"
        )
    bars = load_development_snapshot(snapshot_dir)
    manifest = json.loads((Path(snapshot_dir) / "manifest.json").read_text(encoding="utf-8"))
    market_index, market_sources = load_development_market_index(config, database)
    events = build_cross_sectional_reversal_events(
        bars,
        market_index,
        execution_config=config.portfolio,
        execution_cost_multiplier=execution_cost_multiplier,
    )
    reports = [
        evaluate_development_window(
            events,
            bars,
            market_index,
            window,
            execution_config=config.portfolio,
            execution_cost_multiplier=execution_cost_multiplier,
        )
        for window in WINDOWS
        if window.role == "DEVELOPMENT"
    ]
    decision = assess_development(reports)
    paths = write_development_artifacts(
        output_dir,
        reports,
        events,
        decision,
        protocol_sha256=protocol_hash,
        snapshot_id=str(manifest["snapshot_id"]),
    )
    return {
        "protocol_sha256": protocol_hash,
        "snapshot_id": str(manifest["snapshot_id"]),
        "snapshot_rows": int(len(bars)),
        "snapshot_codes": int(bars["code"].nunique()),
        "market_sources": market_sources,
        "events": int(len(events)),
        "reports": reports,
        "decision": decision,
        "artifacts": paths,
    }


def _file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
