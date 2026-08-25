from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


PROTOCOL_VERSION = "1.0.0"
TQ_API_VERSION = "1.0.13"
TQ_MAX_RECORDS_PER_REQUEST = 24_000
DEFAULT_BARS_PER_SESSION = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "1h": 4}
REQUIRED_FIELDS = ("Open", "High", "Low", "Close", "Volume", "Amount")
REQUIRED_COLUMNS = ("code", "timestamp", *REQUIRED_FIELDS)
WATCHLIST_SIGNAL_COLUMNS = (
    "code",
    "name",
    "signal_date",
    "entry_date",
    "hypothesis_id",
    "raw_close",
    "limit_ratio",
    "return_20d",
    "pullback_depth",
    "pullback_volume_ratio",
    "turnover_20d",
    "score",
    "market_phase",
    "market_style",
    "market_score",
    "market_regime",
    "market_entry_allowed",
    "market_gate",
)


class TQDataError(ValueError):
    """Raised when a TQ response cannot be used as research input."""


@dataclass(frozen=True)
class TQRequestBatch:
    """A bounded, reproducible call to ``get_market_data``."""

    batch_id: str
    codes: tuple[str, ...]
    start_date: str
    end_date: str
    period: str
    estimated_records: int


def _as_date(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"Invalid date: {value!r}")
    return timestamp.normalize()


def plan_tq_intraday_batches(
    codes: Sequence[str],
    trading_dates: Sequence[Any],
    *,
    period: str = "5m",
    bars_per_session: int | None = None,
    max_records: int = TQ_MAX_RECORDS_PER_REQUEST,
    max_codes_per_batch: int = 100,
) -> tuple[TQRequestBatch, ...]:
    """Split requests so every TQ call stays below its 24,000-row limit.

    The planner is deterministic and does not assume that a calendar date is a
    trading day.  Callers pass the calendar returned by TQ or the local snapshot.
    """

    normalized_codes = tuple(sorted({str(code).strip() for code in codes if str(code).strip()}))
    dates = tuple(sorted({_as_date(value) for value in trading_dates}))
    if not normalized_codes or not dates:
        return ()
    if period not in DEFAULT_BARS_PER_SESSION and bars_per_session is None:
        raise ValueError(f"bars_per_session is required for period {period!r}")
    bars = int(bars_per_session or DEFAULT_BARS_PER_SESSION[period])
    if bars <= 0 or max_records <= 0 or max_codes_per_batch <= 0:
        raise ValueError("bars_per_session, max_records and max_codes_per_batch must be positive")
    batches: list[TQRequestBatch] = []
    batch_number = 0
    maximum_codes = min(max_codes_per_batch, max_records // bars)
    if maximum_codes <= 0:
        raise ValueError("max_records must accommodate at least one session")
    for date in dates:
        for code_start in range(0, len(normalized_codes), maximum_codes):
            code_batch = normalized_codes[code_start : code_start + maximum_codes]
            estimated = len(code_batch) * bars
            batch_number += 1
            identity = f"{period}|{','.join(code_batch)}|{date.date()}"
            batch_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            batches.append(
                TQRequestBatch(
                    batch_id=f"tq_{batch_number:05d}_{batch_id}",
                    codes=tuple(code_batch),
                    start_date=date.strftime("%Y%m%d"),
                    end_date=date.strftime("%Y%m%d"),
                    period=period,
                    estimated_records=int(estimated),
                )
            )
    return tuple(batches)


def plan_tq_watchlist_batches(
    watchlist: pd.DataFrame,
    *,
    code_column: str = "code",
    date_column: str = "session_date",
    period: str = "5m",
    bars_per_session: int | None = None,
    max_records: int = TQ_MAX_RECORDS_PER_REQUEST,
    max_codes_per_batch: int = 100,
) -> tuple[TQRequestBatch, ...]:
    """Plan only the code-session pairs explicitly present in a watchlist."""

    missing = {code_column, date_column} - set(watchlist.columns)
    if missing:
        raise ValueError(f"Missing watchlist columns: {sorted(missing)}")
    frame = watchlist.loc[:, [code_column, date_column]].copy()
    frame[code_column] = frame[code_column].astype(str).str.strip()
    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
    frame = frame.loc[frame[code_column].ne("") & frame[date_column].notna()].drop_duplicates()
    batches: list[TQRequestBatch] = []
    for session_date, day in frame.groupby(date_column, sort=True):
        batches.extend(
            plan_tq_intraday_batches(
                day[code_column].tolist(),
                [session_date],
                period=period,
                bars_per_session=bars_per_session,
                max_records=max_records,
                max_codes_per_batch=max_codes_per_batch,
            )
        )
    result: list[TQRequestBatch] = []
    for number, batch in enumerate(batches, start=1):
        suffix = batch.batch_id.rsplit("_", 1)[-1]
        result.append(TQRequestBatch(**{**asdict(batch), "batch_id": f"tq_{number:05d}_{suffix}"}))
    return tuple(result)


def build_event_acquisition_watchlist(
    event_tables: Mapping[str, pd.DataFrame],
    *,
    selected_only: bool = True,
) -> pd.DataFrame:
    """Strip future labels from daily events before requesting minute bars."""

    frames: list[pd.DataFrame] = []
    for window_label, events in sorted(event_tables.items()):
        if events.empty:
            continue
        missing = {"code", "signal_date", "entry_date", "hypothesis_id"} - set(events.columns)
        if missing:
            raise ValueError(f"Missing event columns for {window_label}: {sorted(missing)}")
        frame = events.copy()
        if selected_only:
            if "selected" not in frame:
                raise ValueError(f"Missing selected column for {window_label}")
            frame = frame.loc[frame["selected"].fillna(False).astype(bool)]
        available = [column for column in WATCHLIST_SIGNAL_COLUMNS if column in frame.columns]
        frame = frame.loc[:, available].copy()
        frame["window"] = str(window_label)
        frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce").dt.normalize()
        frame["session_date"] = pd.to_datetime(frame.pop("entry_date"), errors="coerce").dt.normalize()
        frame = frame.dropna(subset=["signal_date", "session_date"])
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["code", "session_date", "signal_date", "window", "hypothesis_ids"])
    combined = pd.concat(frames, ignore_index=True)
    identity = ["code", "session_date"]
    context_columns = [
        column
        for column in combined.columns
        if column not in {*identity, "hypothesis_id", "score"}
    ]
    aggregated = (
        combined.groupby(identity, sort=True, as_index=False)
        .agg(
            hypothesis_ids=("hypothesis_id", lambda values: ",".join(sorted(set(map(str, values))))),
            maximum_signal_score=("score", "max") if "score" in combined else ("hypothesis_id", "size"),
            **{column: (column, "first") for column in context_columns},
        )
    )
    columns = ["code", "session_date", "signal_date", "window", "hypothesis_ids"]
    columns.extend(column for column in aggregated.columns if column not in columns)
    return aggregated.loc[:, columns].sort_values(["session_date", "code"]).reset_index(drop=True)


def write_immutable_tq_watchlist(
    watchlist: pd.DataFrame,
    output_root: Path,
    *,
    source_windows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Publish the signal-time acquisition population before minute outcomes."""

    required = {"code", "session_date", "signal_date"}
    missing = required - set(watchlist.columns)
    if missing:
        raise ValueError(f"Missing acquisition columns: {sorted(missing)}")
    canonical = watchlist.copy().sort_values(["session_date", "code"]).reset_index(drop=True)
    if canonical.duplicated(["code", "session_date"]).any():
        raise ValueError("Acquisition watchlist contains duplicate code-session keys")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".tq_watchlist_", dir=str(output_root)))
    try:
        path = staging / "watchlist.parquet"
        canonical.to_parquet(path, index=False)
        content_hash = _file_sha256(path)
        identity = {
            "protocol_version": PROTOCOL_VERSION,
            "selection": "pre-registered daily events with selected=True; future label columns excluded",
            "source_windows": [dict(item) for item in source_windows],
            "watchlist_sha256": content_hash,
        }
        watchlist_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        target = output_root / watchlist_id
        manifest = {
            **identity,
            "watchlist_id": watchlist_id,
            "rows": int(len(canonical)),
            "codes": int(canonical["code"].nunique()),
            "sessions": int(canonical["session_date"].nunique()),
            "start_date": str(canonical["session_date"].min().date()) if not canonical.empty else None,
            "end_date": str(canonical["session_date"].max().date()) if not canonical.empty else None,
            "estimated_5m_rows": int(len(canonical) * DEFAULT_BARS_PER_SESSION["5m"]),
        }
        if target.exists():
            existing = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("watchlist_sha256") != content_hash:
                raise TQDataError(f"Immutable watchlist collision: {watchlist_id}")
            return existing
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _response_error(response: Mapping[str, Any]) -> str | None:
    error_id = response.get("ErrorId", response.get("error_id", "0"))
    if str(error_id) not in {"0", "0.0", "None", ""}:
        return f"TQ ErrorId={error_id}: {response.get('Msg', response.get('message', ''))}".strip()
    return None


def _field_frame(response: Mapping[str, Any], field: str) -> pd.DataFrame:
    value = response.get(field)
    if value is None:
        raise TQDataError(f"TQ response is missing field {field!r}")
    if isinstance(value, pd.DataFrame):
        frame = value.copy()
    elif isinstance(value, pd.Series):
        frame = value.to_frame()
    else:
        frame = pd.DataFrame(value)
    if frame.empty:
        return frame
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.loc[~frame.index.isna()]
    frame.columns = [str(column) for column in frame.columns]
    if frame.index.duplicated().any():
        raise TQDataError(f"TQ field {field!r} contains duplicate timestamps")
    return frame


def normalize_tq_market_data(
    response: Mapping[str, Any],
    *,
    requested_codes: Sequence[str] = (),
    period: str = "5m",
) -> pd.DataFrame:
    """Convert TQ's field-by-code data frames to an auditable long table."""

    if not isinstance(response, Mapping):
        raise TQDataError("TQ response must be a mapping")
    error = _response_error(response)
    if error:
        raise TQDataError(error)
    frames = {field: _field_frame(response, field) for field in REQUIRED_FIELDS}
    if not any(not frame.empty for frame in frames.values()):
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS) + ["period"])
    code_order = tuple(sorted({str(code) for code in requested_codes}))
    codes = code_order or tuple(sorted(set().union(*(set(frame.columns) for frame in frames.values()))))
    index = sorted(set().union(*(set(frame.index) for frame in frames.values())))
    rows: list[dict[str, Any]] = []
    for timestamp in index:
        for code in codes:
            values = {field: frames[field].at[timestamp, code] if code in frames[field].columns and timestamp in frames[field].index else np.nan for field in REQUIRED_FIELDS}
            if all(pd.isna(value) for value in values.values()):
                continue
            rows.append({"code": code, "timestamp": pd.Timestamp(timestamp), **values, "period": period})
    result = pd.DataFrame(rows, columns=list(REQUIRED_COLUMNS) + ["period"])
    if result.empty:
        return result
    for field in REQUIRED_FIELDS:
        result[field] = pd.to_numeric(result[field], errors="coerce")
    result["RawVolume"] = result["Volume"]
    result["RawAmount"] = result["Amount"]
    amount_scale, volume_scale = _infer_turnover_scales(result)
    result["Volume"] = result["Volume"] * volume_scale
    result["Amount"] = result["Amount"] * amount_scale
    result["amount_scale"] = amount_scale
    result["volume_scale"] = volume_scale
    result["code"] = result["code"].astype(str)
    result["timestamp"] = pd.to_datetime(result["timestamp"], errors="coerce")
    return result.sort_values(["code", "timestamp"]).reset_index(drop=True)


def _infer_turnover_scales(frame: pd.DataFrame) -> tuple[float, float]:
    valid = frame.loc[
        frame["Amount"].gt(0)
        & frame["Volume"].gt(0)
        & frame["Close"].gt(0),
        ["Amount", "Volume", "Close"],
    ]
    if valid.empty:
        return 1.0, 1.0
    raw_ratio = float((valid["Amount"] / valid["Volume"] / valid["Close"]).median())
    candidates = {
        (1.0, 1.0): 1.0,
        (10_000.0, 1.0): 0.0001,
        (1.0, 100.0): 100.0,
        (10_000.0, 100.0): 0.01,
    }
    distances = {
        scales: abs(np.log10(raw_ratio) - np.log10(expected))
        for scales, expected in candidates.items()
    }
    scales, distance = min(distances.items(), key=lambda item: item[1])
    if not np.isfinite(distance) or distance > 0.60:
        raise TQDataError(f"Could not infer TQ Amount/Volume units: median ratio={raw_ratio}")
    return scales


def validate_tq_intraday_bars(
    bars: pd.DataFrame,
    *,
    requested_codes: Sequence[str] = (),
    expected_sessions: Sequence[Any] = (),
    expected_bars_per_session: int | None = None,
    expected_code_sessions: Sequence[tuple[str, Any]] = (),
    minimum_coverage: float = 0.0,
    strict: bool = True,
) -> dict[str, Any]:
    """Validate price integrity and point-in-time coverage without filling rows."""

    missing = [column for column in REQUIRED_COLUMNS if column not in bars.columns]
    if missing:
        raise TQDataError(f"Missing bar columns: {missing}")
    frame = bars.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    for field in REQUIRED_FIELDS:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    invalid_timestamp = int(frame["timestamp"].isna().sum())
    duplicate_keys = int(frame.duplicated(["code", "timestamp"]).sum())
    invalid_ohlc = (
        frame["Open"].le(0)
        | frame["High"].le(0)
        | frame["Low"].le(0)
        | frame["Close"].le(0)
        | frame["High"].lt(frame[["Open", "Close"]].max(axis=1))
        | frame["Low"].gt(frame[["Open", "Close"]].min(axis=1))
        | frame["High"].lt(frame["Low"])
    )
    invalid_ohlc_count = int(invalid_ohlc.fillna(True).sum())
    invalid_volume = int((frame[["Volume", "Amount"]].lt(0).any(axis=1) | frame[["Volume", "Amount"]].isna().any(axis=1)).sum())
    requested_pairs = {
        (str(code), _as_date(session).date()) for code, session in expected_code_sessions
    }
    requested = tuple(sorted({str(code) for code in requested_codes if str(code)} | {code for code, _ in requested_pairs}))
    sessions = tuple(sorted({_as_date(value).date() for value in expected_sessions} | {session for _, session in requested_pairs}))
    observed = frame.dropna(subset=["timestamp"]).assign(session=lambda x: x["timestamp"].dt.date)
    observed_sessions = tuple(sorted(set(observed["session"]))) if not observed.empty else ()
    observed_pairs = set(zip(observed["code"].astype(str), observed["session"]))
    pair_counts = observed.groupby([observed["code"].astype(str), "session"]).size()
    if requested_pairs and expected_bars_per_session:
        expected_rows = len(requested_pairs) * int(expected_bars_per_session)
        coverage = float(len(frame) / expected_rows) if expected_rows else 0.0
    elif sessions and expected_bars_per_session:
        requested_pairs = {(code, session) for code in (requested or tuple(sorted(frame["code"].unique()))) for session in sessions}
        expected_rows = len(requested_pairs) * int(expected_bars_per_session)
        coverage = float(len(frame) / expected_rows) if expected_rows else 0.0
    else:
        expected_rows = None
        coverage = None
    missing_codes = sorted(set(requested) - set(frame["code"].astype(str)))
    missing_sessions = sorted(set(sessions) - set(observed_sessions))
    missing_pairs = sorted(requested_pairs - observed_pairs)
    minimum_pair_rows = int(expected_bars_per_session * 0.90) if expected_bars_per_session else 1
    underfilled_pairs = sorted(
        pair for pair in requested_pairs if 0 < int(pair_counts.get(pair, 0)) < minimum_pair_rows
    )
    report = {
        "rows": int(len(frame)),
        "codes": int(frame["code"].nunique()),
        "requested_codes": len(requested),
        "invalid_timestamp_rows": invalid_timestamp,
        "duplicate_keys": duplicate_keys,
        "invalid_ohlc_rows": invalid_ohlc_count,
        "invalid_volume_rows": invalid_volume,
        "expected_rows": expected_rows,
        "coverage": coverage,
        "missing_codes": missing_codes,
        "missing_sessions": [item.isoformat() for item in missing_sessions],
        "missing_code_sessions": [f"{code}:{session.isoformat()}" for code, session in missing_pairs],
        "underfilled_code_sessions": [
            f"{code}:{session.isoformat()}:{int(pair_counts.get((code, session), 0))}"
            for code, session in underfilled_pairs
        ],
        "min_timestamp": str(frame["timestamp"].min()) if not frame.empty else None,
        "max_timestamp": str(frame["timestamp"].max()) if not frame.empty else None,
    }
    hard_fail = invalid_timestamp or duplicate_keys or invalid_ohlc_count or invalid_volume
    if strict and (hard_fail or missing_pairs or underfilled_pairs or (coverage is not None and coverage < minimum_coverage)):
        raise TQDataError(f"Invalid TQ intraday bars: {report}")
    report["passed"] = not hard_fail and not missing_pairs and not underfilled_pairs and (coverage is None or coverage >= minimum_coverage)
    return report


def fetch_tq_intraday_batches(
    tq_client: Any,
    batches: Sequence[TQRequestBatch],
    *,
    field_list: Sequence[str] = REQUIRED_FIELDS,
    dividend_type: str = "none",
    fill_data: bool = False,
    checkpoint_dir: Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fetch planned batches through an injected, read-only TQ client."""

    if dividend_type != "none":
        raise ValueError("intraday research must use dividend_type='none'")
    if fill_data:
        raise ValueError("intraday research must use fill_data=False")
    frames: list[pd.DataFrame] = []
    batch_reports: list[dict[str, Any]] = []
    checkpoint_root = Path(checkpoint_dir) if checkpoint_dir is not None else None
    if checkpoint_root is not None:
        checkpoint_root.mkdir(parents=True, exist_ok=True)
    for batch in batches:
        frame = _load_tq_batch_checkpoint(checkpoint_root, batch) if checkpoint_root else None
        source = "checkpoint" if frame is not None else "tq"
        if frame is None:
            response = tq_client.get_market_data(
                field_list=list(field_list),
                stock_list=list(batch.codes),
                period=batch.period,
                start_time=batch.start_date,
                end_time=batch.end_date,
                count=0,
                dividend_type=dividend_type,
                fill_data=fill_data,
            )
            frame = normalize_tq_market_data(response, requested_codes=batch.codes, period=batch.period)
            if checkpoint_root is not None:
                _write_tq_batch_checkpoint(checkpoint_root, batch, frame)
        if len(frame) > TQ_MAX_RECORDS_PER_REQUEST:
            raise TQDataError(f"TQ batch {batch.batch_id} exceeded the row limit")
        if not frame.empty:
            frames.append(frame)
        batch_reports.append({**asdict(batch), "returned_rows": int(len(frame)), "source": source})
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(REQUIRED_COLUMNS) + ["period"])
    if not result.empty:
        result = result.sort_values(["code", "timestamp"]).reset_index(drop=True)
    return result, {"protocol_version": PROTOCOL_VERSION, "tq_api_version": TQ_API_VERSION, "batches": batch_reports, "returned_rows": int(len(result))}


def _checkpoint_identity(batch: TQRequestBatch) -> dict[str, Any]:
    request = asdict(batch)
    request["codes"] = list(batch.codes)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "tq_api_version": TQ_API_VERSION,
        "request": request,
        "fields": list(REQUIRED_FIELDS),
        "dividend_type": "none",
        "fill_data": False,
    }


def _load_tq_batch_checkpoint(
    checkpoint_root: Path,
    batch: TQRequestBatch,
) -> pd.DataFrame | None:
    parquet_path = checkpoint_root / f"{batch.batch_id}.parquet"
    manifest_path = checkpoint_root / f"{batch.batch_id}.json"
    if not parquet_path.exists() and not manifest_path.exists():
        return None
    if not parquet_path.exists() or not manifest_path.exists():
        raise TQDataError(f"Incomplete TQ checkpoint: {batch.batch_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("identity") != _checkpoint_identity(batch):
        raise TQDataError(f"TQ checkpoint request mismatch: {batch.batch_id}")
    digest = _file_sha256(parquet_path)
    if digest != manifest.get("bars_sha256"):
        raise TQDataError(f"TQ checkpoint hash mismatch: {batch.batch_id}")
    return pd.read_parquet(parquet_path)


def _write_tq_batch_checkpoint(
    checkpoint_root: Path,
    batch: TQRequestBatch,
    frame: pd.DataFrame,
) -> None:
    parquet_path = checkpoint_root / f"{batch.batch_id}.parquet"
    manifest_path = checkpoint_root / f"{batch.batch_id}.json"
    temporary_parquet = parquet_path.with_suffix(".parquet.tmp")
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    try:
        frame.to_parquet(temporary_parquet, index=False)
        payload = {
            "identity": _checkpoint_identity(batch),
            "rows": int(len(frame)),
            "bars_sha256": _file_sha256(temporary_parquet),
        }
        temporary_manifest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_parquet, parquet_path)
        os.replace(temporary_manifest, manifest_path)
    finally:
        temporary_parquet.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)


def write_immutable_tq_snapshot(
    bars: pd.DataFrame,
    output_root: Path,
    *,
    source_query: Mapping[str, Any],
    quality_report: Mapping[str, Any] | None = None,
    fetch_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a content-addressed Parquet snapshot and refuse mutation."""

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    canonical = bars.copy().sort_values(["code", "timestamp"]).reset_index(drop=True)
    validate_tq_intraday_bars(canonical, strict=True)
    staging = Path(tempfile.mkdtemp(prefix=".tq_snapshot_", dir=str(output_root)))
    try:
        bars_path = staging / "bars.parquet"
        canonical.to_parquet(bars_path, index=False)
        bars_hash = _file_sha256(bars_path)
        identity = {"protocol_version": PROTOCOL_VERSION, "source_query": dict(source_query), "bars_sha256": bars_hash}
        snapshot_id = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
        target = output_root / snapshot_id
        manifest = {
            **identity,
            "snapshot_id": snapshot_id,
            "bars_rows": int(len(canonical)),
            "codes": sorted(canonical["code"].astype(str).unique().tolist()),
            "min_timestamp": str(canonical["timestamp"].min()) if not canonical.empty else None,
            "max_timestamp": str(canonical["timestamp"].max()) if not canonical.empty else None,
            "quality_report": dict(quality_report or {}),
            "fetch_report": dict(fetch_report or {}),
        }
        if target.exists():
            manifest_path = target / "manifest.json"
            existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
            if existing.get("bars_sha256") != bars_hash:
                raise TQDataError(f"Immutable snapshot collision: {snapshot_id}")
            return existing
        manifest["bars_file_sha256"] = bars_hash
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(staging, target)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def capture_tq_intraday_snapshot(
    tq_client: Any,
    watchlist: pd.DataFrame,
    output_root: Path,
    *,
    period: str = "5m",
    max_codes_per_batch: int = 100,
    minimum_coverage: float = 0.95,
    checkpoint_dir: Path | None = None,
) -> dict[str, Any]:
    """Fetch a watchlist, validate it, and publish one immutable snapshot."""

    batches = plan_tq_watchlist_batches(
        watchlist,
        period=period,
        max_codes_per_batch=max_codes_per_batch,
    )
    bars, fetch_report = fetch_tq_intraday_batches(
        tq_client,
        batches,
        checkpoint_dir=checkpoint_dir,
    )
    requested_pairs = [
        (code, pd.Timestamp(batch.start_date))
        for batch in batches
        for code in batch.codes
    ]
    quality = validate_tq_intraday_bars(
        bars,
        expected_code_sessions=requested_pairs,
        expected_bars_per_session=DEFAULT_BARS_PER_SESSION.get(period),
        minimum_coverage=minimum_coverage,
        strict=True,
    )
    source_query = {
        "provider": "TdxQuant/tqcenter",
        "api_version": TQ_API_VERSION,
        "period": period,
        "dividend_type": "none",
        "fill_data": False,
        "fields": list(REQUIRED_FIELDS),
        "batches": [asdict(batch) for batch in batches],
    }
    return write_immutable_tq_snapshot(
        bars,
        output_root,
        source_query=source_query,
        quality_report=quality,
        fetch_report=fetch_report,
    )


@contextmanager
def initialized_tq_client(tdx_root: Path, *, identity_path: Path | None = None):
    """Open the read-only TQ data channel and always close it."""

    user_dir = Path(tdx_root).resolve() / "PYPlugins" / "user"
    module_path = user_dir / "tqcenter.py"
    if not module_path.exists():
        raise FileNotFoundError(f"TQ client module not found: {module_path}")
    original_path = list(sys.path)
    sys.path.insert(0, str(user_dir))
    try:
        module = importlib.import_module("tqcenter")
        loaded_path = Path(module.__file__).resolve()
        if loaded_path != module_path.resolve():
            raise TQDataError(f"Unexpected tqcenter module: {loaded_path}")
        client = module.tq
        client.initialize(str((identity_path or Path(__file__)).resolve()))
        try:
            yield client
        finally:
            client.close()
    finally:
        sys.path[:] = original_path


def capture_tq_watchlist_file(
    tdx_root: Path,
    watchlist_path: Path,
    output_root: Path,
    *,
    checkpoint_dir: Path | None = None,
    period: str = "5m",
) -> dict[str, Any]:
    """Capture an immutable snapshot from a frozen acquisition watchlist."""

    watchlist_path = Path(watchlist_path)
    manifest_path = watchlist_path.parent / "manifest.json"
    if not manifest_path.exists():
        raise TQDataError(f"Frozen watchlist manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise TQDataError(f"Invalid frozen watchlist manifest: {manifest_path}") from exc
    expected_hash = str(manifest.get("watchlist_sha256") or "")
    actual_hash = _file_sha256(watchlist_path)
    if not expected_hash or actual_hash != expected_hash:
        raise TQDataError(
            f"Frozen watchlist hash mismatch: expected={expected_hash or 'missing'}, actual={actual_hash}"
        )
    identity = {
        "protocol_version": manifest.get("protocol_version"),
        "selection": manifest.get("selection"),
        "source_windows": manifest.get("source_windows"),
        "watchlist_sha256": expected_hash,
    }
    computed_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    expected_id = str(manifest.get("watchlist_id") or "")
    if expected_id != computed_id or watchlist_path.parent.name != computed_id:
        raise TQDataError(
            "Frozen watchlist content address mismatch: "
            f"manifest={expected_id or 'missing'}, computed={computed_id}, "
            f"directory={watchlist_path.parent.name}"
        )
    if watchlist_path.suffix.lower() == ".parquet":
        watchlist = pd.read_parquet(watchlist_path)
    elif watchlist_path.suffix.lower() == ".csv":
        watchlist = pd.read_csv(watchlist_path)
    else:
        raise ValueError("watchlist_path must be a Parquet or CSV file")
    with initialized_tq_client(tdx_root) as client:
        return capture_tq_intraday_snapshot(
            client,
            watchlist,
            output_root,
            checkpoint_dir=checkpoint_dir,
            period=period,
        )


def _file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
