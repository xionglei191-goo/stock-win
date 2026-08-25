"""Fail-closed qualification gates for the US momentum programme.

This module deliberately separates *qualification* from execution.  It does
not mutate a strategy, a PIT release, or a paper account.  Instead it consumes
frozen evidence, opens the sealed historical window once, and returns an
auditable decision.  The three public entry points cover the three independent
gates required before paper collection can be trusted:

* :class:`HistoricalQualificationService` coordinates the locked historical
  protocol and delegates the final thresholds to :mod:`us_promotion`;
* :class:`PaperQualificationTracker` evaluates a deterministic paper replay;
* :func:`evaluate_tdx_quote_qualification` evaluates the 20-session TDX quote
  shadow test.

All ambiguous, missing, duplicated, non-finite, or temporally inconsistent
evidence fails closed.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .us_pit.dataset import USBacktestDataset
from .us_pit.models import QUALITY_CONTRACT_REVISION, ReleaseStatus, UNIVERSE_ID
from .us_promotion import (
    EXPECTED_NEIGHBORHOODS,
    HistoricalPromotionEvidence,
    NeighborhoodResult,
    PromotionDecision,
    PromotionMetrics,
    evaluate_historical_promotion,
)


NY_TZ = ZoneInfo("America/New_York")
SHA256_LENGTH = 64
REGULAR_SESSION_POLL_SLOTS = 390
TDX_REQUIRED_SESSIONS = 20
PAPER_REQUIRED_SESSIONS = 252
PAPER_REQUIRED_MONTH_END_CYCLES = 12
PAPER_REQUIRED_CLOSED_TRADES = 20


class QualificationError(ValueError):
    """Raised when evidence is unsafe to evaluate."""


class SealedHoldoutError(QualificationError):
    """Raised when a frozen sealed window would be opened more than once."""


@dataclass(frozen=True)
class HistoricalWindowSplit:
    development_start: date
    development_end: date
    validation_start: date
    validation_end: date
    sealed_start: date
    sealed_end: date
    oos_start: date
    oos_end: date
    development_months: int
    validation_months: int
    sealed_months: int


@dataclass(frozen=True)
class HistoricalRunRequest:
    """One pre-declared strict runner invocation.

    A production adapter should translate ``parameters`` to
    ``USMomentumParameters`` and apply the two cost multipliers independently.
    ``excluded_security_ids`` is used only for the top-issuer removal check.
    """

    run_id: str
    start_date: date
    end_date: date
    parameters: Mapping[str, Any]
    commission_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    excluded_security_ids: frozenset[str] = frozenset()
    touches_sealed: bool = False


class StrictQualificationRunner(Protocol):
    def __call__(
        self,
        dataset: USBacktestDataset,
        request: HistoricalRunRequest,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class HistoricalQualificationResult:
    freeze_sha256: str
    split: HistoricalWindowSplit
    evidence: HistoricalPromotionEvidence
    decision: PromotionDecision
    run_sha256: Mapping[str, str]


def _is_sha256(value: object) -> bool:
    text = str(value)
    return len(text) == SHA256_LENGTH and all(character in "0123456789abcdef" for character in text)


def _day(value: object) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(NY_TZ).tz_localize(None)
    return stamp.normalize()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QualificationError("non-finite value cannot be frozen")
        return value
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (set, frozenset, tuple, list)):
        items = [_json_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True)) if isinstance(value, (set, frozenset)) else items
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if pd.isna(value):
        return None
    return str(value)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _frame_sha256(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty:
        return _sha256_json([])
    normalized = frame.copy()
    normalized = normalized.reindex(sorted(normalized.columns), axis=1)
    records = normalized.astype(object).where(pd.notna(normalized), None).to_dict("records")
    return _sha256_json(records)


class _SealedRegistry:
    """Small transactional registry preventing a second sealed opening."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._memory: dict[str, int] = {}

    def open_once(self, freeze_sha256: str) -> int:
        # Consume the opening before invoking any sealed runner.  A crash or a
        # runner error therefore cannot be retried into the holdout unnoticed.
        if self.path is None:
            if self._memory.get(freeze_sha256, 0) != 0:
                raise SealedHoldoutError(
                    "sealed holdout has already been opened for this frozen protocol"
                )
            self._memory[freeze_sha256] = 1
            return 1

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30.0)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sealed_openings (
                    freeze_sha256 TEXT PRIMARY KEY,
                    opened_at TEXT NOT NULL,
                    CHECK(length(freeze_sha256) = 64)
                )
                """
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO sealed_openings(freeze_sha256, opened_at) VALUES (?, ?)",
                (freeze_sha256, datetime.now(tz=ZoneInfo("UTC")).isoformat()),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            raise SealedHoldoutError(
                "sealed holdout has already been opened for this frozen protocol"
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise SealedHoldoutError("sealed registry is unreadable") from exc
        finally:
            if connection is not None:
                connection.close()
        return 1


def _parameter_mapping(parameters: Mapping[str, Any] | object) -> dict[str, Any]:
    if isinstance(parameters, Mapping):
        result = dict(parameters)
    elif is_dataclass(parameters):
        result = asdict(parameters)
    else:
        raise QualificationError("strategy parameters must be a mapping or dataclass")
    required = {"rs_top_pct", "exit_top_pct", "stop_ratio"}
    if not required.issubset(result):
        missing = ", ".join(sorted(required - set(result)))
        raise QualificationError(f"strategy parameters are missing: {missing}")
    return result


def _quality_is_ready(dataset: USBacktestDataset) -> bool:
    report = getattr(dataset, "quality_report", None)
    status = getattr(report, "status", None)
    status_value = getattr(status, "value", status)
    hard_failures = tuple(getattr(report, "hard_failures", ()))
    return (
        status_value == ReleaseStatus.DATA_READY.value
        and bool(getattr(report, "includes_delisted", False))
        and not hard_failures
        and getattr(report, "metrics", {}).get("quality_contract_revision")
        == QUALITY_CONTRACT_REVISION
        and getattr(dataset, "universe_id", None) == UNIVERSE_ID
        and _is_sha256(getattr(dataset, "release_id", ""))
    )


def _split_windows(dataset: USBacktestDataset) -> HistoricalWindowSplit:
    decisions = sorted({_day(item) for item in dataset.membership_by_date})
    if len(decisions) < 60:
        raise QualificationError("at least 60 monthly PIT decision points are required")
    decisions = decisions[-len(decisions) :]
    periods = [item.to_period("M") for item in decisions]
    if len(periods) != len(set(periods)):
        raise QualificationError("membership contains duplicate decision months")
    for previous, current in zip(periods, periods[1:]):
        if current.ordinal != previous.ordinal + 1:
            raise QualificationError("PIT decision months are not continuous")

    sealed_count = 12
    validation_count = 12
    development_count = len(decisions) - validation_count - sealed_count
    if development_count < 36:
        raise QualificationError("development window must contain at least 36 months")

    if not isinstance(dataset.calendar, pd.DataFrame):
        raise QualificationError("frozen XNYS calendar must be a data frame")
    calendar_column = next(
        (
            column
            for column in ("session_date", "session")
            if column in dataset.calendar.columns
        ),
        None,
    )
    if calendar_column is None:
        raise QualificationError("frozen XNYS calendar lacks session_date")
    try:
        calendar = pd.DatetimeIndex(
            [_day(value) for value in dataset.calendar[calendar_column]]
        )
    except (TypeError, ValueError) as exc:
        raise QualificationError("frozen XNYS calendar contains an invalid session") from exc
    if len(calendar) == 0:
        raise QualificationError("frozen XNYS calendar is empty")
    if calendar.isna().any():
        raise QualificationError("frozen XNYS calendar contains an invalid session")
    if calendar.has_duplicates:
        raise QualificationError("frozen XNYS calendar contains duplicate sessions")
    if not calendar.is_monotonic_increasing:
        raise QualificationError("frozen XNYS calendar is not ordered")
    last_session = pd.Timestamp(calendar[-1])
    if last_session < decisions[-1]:
        raise QualificationError("frozen XNYS calendar ends before the final PIT decision")

    dev = decisions[:development_count]
    validation = decisions[development_count : development_count + validation_count]
    sealed = decisions[-sealed_count:]
    return HistoricalWindowSplit(
        development_start=dev[0].date(),
        development_end=dev[-1].date(),
        validation_start=validation[0].date(),
        validation_end=validation[-1].date(),
        sealed_start=sealed[0].date(),
        sealed_end=last_session.date(),
        oos_start=validation[0].date(),
        oos_end=last_session.date(),
        development_months=development_count,
        validation_months=validation_count,
        sealed_months=sealed_count,
    )


def _positive_multiplier(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise QualificationError(f"{name} must be positive") from exc
    if not math.isfinite(result) or result <= 0:
        raise QualificationError(f"{name} must be positive")
    return result


def _qualification_dataset_view(
    dataset: USBacktestDataset,
    excluded_security_ids: Iterable[object],
) -> tuple[USBacktestDataset, frozenset[str]]:
    """Return an ephemeral filtered view without mutating release-backed data."""

    excluded = frozenset(str(value).strip().lower() for value in excluded_security_ids)
    if any(
        not value or not value.startswith("us_") or value.endswith(".us")
        for value in excluded
    ):
        raise QualificationError("excluded_security_ids must contain stable us_ security IDs")
    if not excluded:
        return dataset, excluded

    known = {
        str(value).strip().lower()
        for members in dataset.membership_by_date.values()
        for value in members
    }
    if "security_id" in dataset.security_master.columns:
        known.update(
            dataset.security_master["security_id"].dropna().astype(str).str.strip().str.lower()
        )
    unknown = sorted(excluded - known)
    if unknown:
        raise QualificationError(
            "excluded_security_ids are absent from the frozen release: "
            + ", ".join(unknown[:10])
        )

    memberships = {
        decision: frozenset(
            security_id
            for security_id in members
            if str(security_id).strip().lower() not in excluded
        )
        for decision, members in dataset.membership_by_date.items()
    }
    if any(not members for members in memberships.values()):
        raise QualificationError("issuer exclusion leaves an empty PIT membership set")

    signal_bars = {
        decision: {
            security_id: frame
            for security_id, frame in values.items()
            if str(security_id).strip().lower() not in excluded
        }
        for decision, values in dataset.signal_bars_by_decision.items()
    }
    raw_bars = {
        security_id: frame
        for security_id, frame in dataset.raw_bars.items()
        if str(security_id).strip().lower() not in excluded
    }
    vendor_front_bars = {
        security_id: frame
        for security_id, frame in dataset.vendor_front_bars.items()
        if str(security_id).strip().lower() not in excluded
    }
    return (
        replace(
            dataset,
            membership_by_date=memberships,
            raw_bars=raw_bars,
            vendor_front_bars=vendor_front_bars,
            signal_bars_by_decision=signal_bars,
        ),
        excluded,
    )


def run_strict_qualification_backtest(
    dataset: USBacktestDataset,
    request: HistoricalRunRequest,
) -> Mapping[str, Any]:
    """Adapt one frozen qualification request to the strict US engine.

    The adapter creates only an in-memory view for issuer exclusion.  Release
    frames and their effective-dated fee table remain unchanged; the strict
    engine applies commission and slippage multipliers after selecting the
    effective fee row for each execution session.
    """

    if not isinstance(dataset, USBacktestDataset):
        raise QualificationError("strict qualification requires USBacktestDataset")
    if not isinstance(request, HistoricalRunRequest):
        raise QualificationError("strict qualification requires HistoricalRunRequest")
    if not _quality_is_ready(dataset):
        raise QualificationError("strict qualification requires a verified DATA_READY release")
    if not str(request.run_id).strip():
        raise QualificationError("qualification run_id is required")
    if request.start_date > request.end_date:
        raise QualificationError("qualification start_date cannot be after end_date")

    commission_multiplier = _positive_multiplier(
        request.commission_multiplier, "commission_multiplier"
    )
    slippage_multiplier = _positive_multiplier(
        request.slippage_multiplier, "slippage_multiplier"
    )
    parameter_values = _parameter_mapping(request.parameters)
    if "excluded_codes" in parameter_values:
        parameter_values["excluded_codes"] = tuple(parameter_values["excluded_codes"])
    try:
        from .strategies.us_momentum import USMomentumParameters
        from .strategies.us_momentum_backtest import run_backtest

        parameters = USMomentumParameters(**parameter_values)
    except (TypeError, ValueError) as exc:
        raise QualificationError(f"invalid US momentum parameters: {exc}") from exc

    view, excluded = _qualification_dataset_view(
        dataset, request.excluded_security_ids
    )
    names: dict[str, str] = {}
    if "security_id" in dataset.security_master.columns:
        label_column = next(
            (
                column
                for column in ("security_name", "company_name", "name")
                if column in dataset.security_master.columns
            ),
            None,
        )
        for row in dataset.security_master.itertuples(index=False):
            security_id = str(getattr(row, "security_id")).strip().lower()
            label = getattr(row, label_column) if label_column is not None else security_id
            names[security_id] = security_id if pd.isna(label) else str(label)

    try:
        raw_result = run_backtest(
            dataset=view,
            names=names,
            initial_capital=100_000.0,
            params=parameters,
            start_date=request.start_date,
            end_date=request.end_date,
            commission_multiplier=commission_multiplier,
            slippage_multiplier=slippage_multiplier,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationError(f"strict US engine rejected {request.run_id}: {exc}") from exc
    if not isinstance(raw_result, Mapping):
        raise QualificationError("strict US engine returned an invalid result")
    result = dict(raw_result)
    data_contract = dict(result.get("data_contract") or {})
    if data_contract.get("release_id") != dataset.release_id:
        raise QualificationError("strict US engine result release_id does not match request")
    data_contract.update(
        {
            "qualification_run_id": request.run_id,
            "excluded_security_ids": sorted(excluded),
            "fee_multipliers": {
                "commission": commission_multiplier,
                "slippage": slippage_multiplier,
            },
        }
    )
    result["data_contract"] = data_contract
    return result


def _neighbor_parameters(base: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    changes = {
        "entry_20": ("rs_top_pct", 0.20),
        "entry_30": ("rs_top_pct", 0.30),
        "exit_35": ("exit_top_pct", 0.35),
        "exit_45": ("exit_top_pct", 0.45),
        "stop_06": ("stop_ratio", 0.06),
        "stop_10": ("stop_ratio", 0.10),
    }
    if set(changes) != EXPECTED_NEIGHBORHOODS:
        raise AssertionError("neighborhood protocol drift")
    result: dict[str, dict[str, Any]] = {}
    for variation, (name, value) in changes.items():
        values = dict(base)
        values[name] = value
        result[variation] = values
    return result


def _run_fingerprint(result: Mapping[str, Any]) -> str:
    permitted = {
        "period": result.get("period"),
        "equity_curve": result.get("equity_curve"),
        "trades": result.get("trades"),
        "metrics": result.get("metrics"),
        "data_contract": result.get("data_contract"),
    }
    return _sha256_json(permitted)


def _equity_series(result: Mapping[str, Any]) -> pd.Series:
    value = result.get("equity_curve")
    if isinstance(value, Mapping):
        series = pd.Series(dict(value), dtype=float)
        series.index = pd.to_datetime(series.index, errors="coerce")
    elif isinstance(value, pd.DataFrame) and {"timestamp", "equity"}.issubset(value.columns):
        series = value.set_index("timestamp")["equity"].astype(float)
        series.index = pd.to_datetime(series.index, errors="coerce")
    elif isinstance(value, pd.Series):
        series = value.astype(float).copy()
        series.index = pd.to_datetime(series.index, errors="coerce")
    else:
        raise QualificationError("strict runner did not return a usable equity_curve")
    series = series[~series.index.isna()].sort_index()
    if series.index.has_duplicates or len(series) < 2:
        raise QualificationError("equity_curve must contain unique ordered sessions")
    values = pd.to_numeric(series, errors="coerce")
    if not np.isfinite(values.to_numpy()).all() or (values <= 0).any():
        raise QualificationError("equity_curve contains invalid values")
    return values


def _benchmark_series(
    dataset: USBacktestDataset,
    symbol: str,
    start: date,
    end: date,
) -> pd.Series:
    frame = dataset.benchmark_bars.get(symbol)
    if frame is None:
        frame = dataset.benchmark_bars.get(symbol.replace(".US", ""))
    if frame is None or frame.empty:
        raise QualificationError(f"benchmark {symbol} is missing")
    # Promotion comparisons must use a causally frozen total-return level.
    # Raw closes materially understate BIL and SPY because they omit cash
    # distributions, so accepting Close here could falsely qualify a strategy.
    # The release quality gate proves this column and its evidence lineage; the
    # qualification layer deliberately has no fallback to raw prices.
    total_return_name = next(
        (
            item
            for item in (
                "TotalReturnClose",
                "total_return_close",
                "TOTAL_RETURN_CLOSE",
            )
            if item in frame.columns
        ),
        None,
    )
    if total_return_name is None:
        raise QualificationError(
            f"benchmark {symbol} lacks a PIT total-return level; raw Close is not "
            "eligible for promotion"
        )
    series = pd.to_numeric(frame[total_return_name], errors="coerce")
    series.index = pd.to_datetime(frame.index, errors="coerce")
    series = series.loc[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]
    series = series[~series.index.isna()].sort_index()
    if series.index.has_duplicates or len(series) < 2 or not np.isfinite(series.to_numpy()).all() or (series <= 0).any():
        raise QualificationError(f"benchmark {symbol} is invalid in the requested window")
    return series


def _performance(
    equity: pd.Series,
    bil: pd.Series,
    *,
    closed_trades: int = 0,
    issuers: int = 0,
    cagr_without_top_issuer: float | None = None,
) -> PromotionMetrics:
    elapsed_sessions = len(equity) - 1
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (252 / elapsed_sessions) - 1.0)
    drawdown = equity / equity.cummax() - 1.0
    strategy_returns = equity.pct_change(fill_method=None).dropna()
    bil_returns = bil.pct_change(fill_method=None).dropna()
    aligned = pd.concat([strategy_returns, bil_returns], axis=1, join="inner").dropna()
    if len(aligned) < 2:
        raise QualificationError("insufficient aligned BIL observations for excess Sharpe")
    excess = aligned.iloc[:, 0] - aligned.iloc[:, 1]
    volatility = float(excess.std(ddof=1))
    if not math.isfinite(volatility):
        raise QualificationError("excess Sharpe volatility is invalid")
    if volatility == 0:
        excess_sharpe = float("inf") if float(excess.mean()) > 0 else 0.0
    else:
        excess_sharpe = float(excess.mean() / volatility * math.sqrt(252))
    return PromotionMetrics(
        cagr=cagr,
        total_return=total_return,
        excess_sharpe=excess_sharpe,
        max_drawdown=abs(float(drawdown.min())),
        closed_trades=closed_trades,
        issuers=issuers,
        cagr_without_top_issuer=cagr_without_top_issuer,
    )


def _benchmark_metrics(series: pd.Series, bil: pd.Series) -> PromotionMetrics:
    return _performance(series / float(series.iloc[0]), bil / float(bil.iloc[0]))


def _closed_trade_evidence(
    result: Mapping[str, Any],
    dataset: USBacktestDataset,
) -> tuple[int, int, str, frozenset[str]]:
    raw_trades = result.get("trades")
    if not isinstance(raw_trades, Sequence) or isinstance(raw_trades, (str, bytes)):
        raise QualificationError("strict runner trades must be a sequence")
    closed = [row for row in raw_trades if isinstance(row, Mapping) and str(row.get("side", "")).upper() == "SELL"]
    if not closed:
        return 0, 0, "", frozenset()

    master = dataset.security_master.copy()
    required = {"security_id", "issuer_id"}
    if not required.issubset(master.columns):
        raise QualificationError("security_master lacks issuer identity")
    issuer_by_security = {
        str(row.security_id): str(row.issuer_id)
        for row in master.loc[:, ["security_id", "issuer_id"]].itertuples(index=False)
        if str(row.security_id) and str(row.issuer_id)
    }
    contribution: dict[str, float] = {}
    observed_issuers: set[str] = set()
    for row in closed:
        security_id = str(row.get("security_id", ""))
        issuer = issuer_by_security.get(security_id)
        if not issuer:
            raise QualificationError(f"closed trade has no issuer mapping: {security_id}")
        pnl = float(row.get("pnl", 0.0))
        if not math.isfinite(pnl):
            raise QualificationError("closed trade has non-finite pnl")
        observed_issuers.add(issuer)
        contribution[issuer] = contribution.get(issuer, 0.0) + pnl
    top_issuer = max(contribution, key=lambda item: (contribution[item], item))
    excluded = frozenset(
        security_id for security_id, issuer in issuer_by_security.items() if issuer == top_issuer
    )
    if not excluded:
        raise QualificationError("top issuer has no securities to exclude")
    return len(closed), len(observed_issuers), top_issuer, excluded


def _monthly_bearish_count(spy: pd.Series) -> int:
    monthly = spy.groupby(spy.index.to_period("M")).last().pct_change(fill_method=None).dropna()
    return int((monthly < 0).sum())


class HistoricalQualificationService:
    """Coordinate the one-shot historical promotion protocol.

    The callable is intentionally explicit: ``runner(dataset, request)``.  This
    lets the strict engine remain the only implementation of fills while this
    service owns windowing, freezing, benchmark calculations, and promotion.
    """

    def __init__(self, sealed_registry_path: str | Path | None = None) -> None:
        self._registry = _SealedRegistry(
            None if sealed_registry_path is None else Path(sealed_registry_path)
        )

    def qualify(
        self,
        dataset: USBacktestDataset,
        strict_runner: StrictQualificationRunner,
        *,
        parameters: Mapping[str, Any] | object,
        strategy_code_sha256: str,
        strategy_id: str = "us_momentum_v1",
    ) -> HistoricalQualificationResult:
        if not isinstance(dataset, USBacktestDataset):
            raise QualificationError("historical qualification requires USBacktestDataset")
        if not _quality_is_ready(dataset):
            raise QualificationError("historical qualification requires a verified DATA_READY release")
        if not _is_sha256(strategy_code_sha256):
            raise QualificationError("strategy_code_sha256 must be a lowercase SHA-256")
        base_parameters = _parameter_mapping(parameters)
        split = _split_windows(dataset)

        # Development and validation remain outside the sealed opening.  Their
        # successful deterministic completion is part of the frozen evidence.
        development = self._invoke(
            strict_runner,
            dataset,
            HistoricalRunRequest(
                "development",
                split.development_start,
                split.development_end,
                base_parameters,
            ),
        )
        validation = self._invoke(
            strict_runner,
            dataset,
            HistoricalRunRequest(
                "validation",
                split.validation_start,
                split.validation_end,
                base_parameters,
            ),
        )
        freeze_payload = {
            "protocol": "us-momentum-historical-qualification-v1",
            "strategy_id": strategy_id,
            "strategy_code_sha256": strategy_code_sha256,
            "parameters": base_parameters,
            "release_id": dataset.release_id,
            "universe_id": dataset.universe_id,
            "fee_schedule_sha256": _frame_sha256(dataset.fee_schedule),
            "split": asdict(split),
            "neighborhoods": _neighbor_parameters(base_parameters),
            "stress": {"commission_multiplier": 2.0, "slippage_multiplier": 2.0},
        }
        freeze_sha256 = _sha256_json(freeze_payload)
        sealed_run_count = self._registry.open_once(freeze_sha256)

        run_results: dict[str, Mapping[str, Any]] = {
            "development": development,
            "validation": validation,
        }
        # One declared sealed batch.  The registry opening is consumed before
        # the first call, so partial failure cannot be retried against holdout.
        run_results["sealed"] = self._invoke(
            strict_runner,
            dataset,
            HistoricalRunRequest(
                "sealed",
                split.sealed_start,
                split.sealed_end,
                base_parameters,
                touches_sealed=True,
            ),
        )
        run_results["oos_24m"] = self._invoke(
            strict_runner,
            dataset,
            HistoricalRunRequest(
                "oos_24m",
                split.oos_start,
                split.oos_end,
                base_parameters,
                touches_sealed=True,
            ),
        )

        closed_trades, issuer_count, _top_issuer, excluded = _closed_trade_evidence(
            run_results["oos_24m"], dataset
        )
        run_results["top_issuer_removed"] = self._invoke(
            strict_runner,
            dataset,
            HistoricalRunRequest(
                "top_issuer_removed",
                split.oos_start,
                split.oos_end,
                base_parameters,
                excluded_security_ids=excluded,
                touches_sealed=True,
            ),
        )
        run_results["double_cost"] = self._invoke(
            strict_runner,
            dataset,
            HistoricalRunRequest(
                "double_cost",
                split.oos_start,
                split.oos_end,
                base_parameters,
                commission_multiplier=2.0,
                slippage_multiplier=2.0,
                touches_sealed=True,
            ),
        )
        for variation, values in _neighbor_parameters(base_parameters).items():
            run_results[variation] = self._invoke(
                strict_runner,
                dataset,
                HistoricalRunRequest(
                    variation,
                    split.oos_start,
                    split.oos_end,
                    values,
                    touches_sealed=True,
                ),
            )

        oos_bil_series = _benchmark_series(dataset, "BIL.US", split.oos_start, split.oos_end)
        oos_spy_series = _benchmark_series(dataset, "SPY.US", split.oos_start, split.oos_end)
        sealed_bil_series = _benchmark_series(dataset, "BIL.US", split.sealed_start, split.sealed_end)
        sealed_spy_series = _benchmark_series(dataset, "SPY.US", split.sealed_start, split.sealed_end)

        removed_metrics = _performance(
            _equity_series(run_results["top_issuer_removed"]),
            oos_bil_series,
        )
        oos_strategy = _performance(
            _equity_series(run_results["oos_24m"]),
            oos_bil_series,
            closed_trades=closed_trades,
            issuers=issuer_count,
            cagr_without_top_issuer=removed_metrics.cagr,
        )
        sealed_strategy = _performance(
            _equity_series(run_results["sealed"]),
            sealed_bil_series,
        )
        stress = _performance(
            _equity_series(run_results["double_cost"]),
            oos_bil_series,
        )
        neighborhood_metrics = {
            variation: _performance(_equity_series(run_results[variation]), oos_bil_series)
            for variation in sorted(EXPECTED_NEIGHBORHOODS)
        }
        oos_bil = _benchmark_metrics(oos_bil_series, oos_bil_series)
        oos_spy = _benchmark_metrics(oos_spy_series, oos_bil_series)
        sealed_bil = _benchmark_metrics(sealed_bil_series, sealed_bil_series)
        sealed_spy = _benchmark_metrics(sealed_spy_series, sealed_bil_series)

        evidence = HistoricalPromotionEvidence(
            oos_strategy=oos_strategy,
            oos_spy=oos_spy,
            oos_bil=oos_bil,
            sealed_strategy=sealed_strategy,
            sealed_spy=sealed_spy,
            sealed_bil=sealed_bil,
            double_cost_strategy=stress,
            double_cost_bil=oos_bil,
            neighborhoods=tuple(
                NeighborhoodResult(
                    variation_id=variation,
                    cagr=neighborhood_metrics[variation].cagr,
                    max_drawdown=neighborhood_metrics[variation].max_drawdown,
                )
                for variation in sorted(EXPECTED_NEIGHBORHOODS)
            ),
            neighborhood_base_cagr=oos_strategy.cagr,
            neighborhood_bil_cagr=oos_bil.cagr,
            development_months=split.development_months,
            validation_months=split.validation_months,
            sealed_months=split.sealed_months,
            bearish_regime_months=_monthly_bearish_count(oos_spy_series),
            data_ready=True,
            release_hash_verified=True,
            frozen_before_sealed=True,
            sealed_run_count=sealed_run_count,
        )
        decision = evaluate_historical_promotion(evidence)
        return HistoricalQualificationResult(
            freeze_sha256=freeze_sha256,
            split=split,
            evidence=evidence,
            decision=decision,
            run_sha256={name: _run_fingerprint(value) for name, value in run_results.items()},
        )

    @staticmethod
    def _invoke(
        runner: StrictQualificationRunner,
        dataset: USBacktestDataset,
        request: HistoricalRunRequest,
    ) -> Mapping[str, Any]:
        try:
            result = runner(dataset, request)
        except Exception as exc:
            raise QualificationError(f"strict runner failed for {request.run_id}: {exc}") from exc
        if not isinstance(result, Mapping):
            raise QualificationError(f"strict runner returned invalid result for {request.run_id}")
        # Validate immediately, before any later protocol step can use it.
        _equity_series(result)
        return result


@dataclass(frozen=True)
class PaperSessionEvidence:
    session: date
    equity: float
    bil_equity: float
    input_sha256: str
    output_sha256: str
    replay_output_sha256: str


@dataclass(frozen=True)
class PaperCycleEvidence:
    cycle_id: str
    decision_session: date
    execution_session: date
    complete: bool
    replay_verified: bool


@dataclass(frozen=True)
class PaperTradeEvidence:
    trade_id: str
    opened_session: date
    closed_session: date | None


@dataclass(frozen=True)
class PaperQualificationDecision:
    qualified: bool
    status: str
    gates: Mapping[str, bool]
    failures: tuple[str, ...]
    metrics: Mapping[str, float | int]


class PaperQualificationTracker:
    """Pure deterministic evaluator for the one-year paper collection gate."""

    def __init__(self, frozen_xnys_sessions: Iterable[object]) -> None:
        sessions = [_day(item).date() for item in frozen_xnys_sessions]
        if not sessions or len(sessions) != len(set(sessions)):
            raise QualificationError("frozen XNYS sessions must be unique and non-empty")
        self._sessions = tuple(sorted(sessions))
        self._session_set = frozenset(self._sessions)

    def evaluate(
        self,
        session_evidence: Sequence[PaperSessionEvidence],
        cycle_evidence: Sequence[PaperCycleEvidence],
        trade_evidence: Sequence[PaperTradeEvidence],
    ) -> PaperQualificationDecision:
        observed_sessions = [item.session for item in session_evidence]
        valid_hashes = all(
            _is_sha256(item.input_sha256)
            and _is_sha256(item.output_sha256)
            and _is_sha256(item.replay_output_sha256)
            for item in session_evidence
        )
        replayable = valid_hashes and all(
            item.output_sha256 == item.replay_output_sha256 for item in session_evidence
        ) and all(item.replay_verified for item in cycle_evidence)
        session_integrity = (
            bool(session_evidence)
            and len(observed_sessions) == len(set(observed_sessions))
            and observed_sessions == sorted(observed_sessions)
            and set(observed_sessions).issubset(self._session_set)
            and all(
                math.isfinite(item.equity)
                and item.equity > 0
                and math.isfinite(item.bil_equity)
                and item.bil_equity > 0
                for item in session_evidence
            )
        )
        unique_cycles = {item.cycle_id for item in cycle_evidence if item.complete}
        cycle_months = {
            (item.decision_session.year, item.decision_session.month)
            for item in cycle_evidence
            if item.complete
        }
        cycle_integrity = (
            len([item.cycle_id for item in cycle_evidence])
            == len({item.cycle_id for item in cycle_evidence})
            and all(
                bool(item.cycle_id)
                and item.decision_session in self._session_set
                and item.execution_session in self._session_set
                and item.execution_session > item.decision_session
                for item in cycle_evidence
            )
        )
        closed = [item for item in trade_evidence if item.closed_session is not None]
        trade_integrity = (
            len([item.trade_id for item in trade_evidence])
            == len({item.trade_id for item in trade_evidence})
            and all(
                bool(item.trade_id)
                and item.opened_session in self._session_set
                and (
                    item.closed_session is None
                    or (
                        item.closed_session in self._session_set
                        and item.closed_session >= item.opened_session
                    )
                )
                for item in trade_evidence
            )
        )
        if session_integrity:
            equity = pd.Series(
                [item.equity for item in session_evidence],
                index=pd.to_datetime(observed_sessions),
                dtype=float,
            )
            paper_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
            drawdown = abs(float((equity / equity.cummax() - 1.0).min()))
            bil_return = float(
                session_evidence[-1].bil_equity / session_evidence[0].bil_equity - 1.0
            )
        else:
            paper_return = float("nan")
            bil_return = float("nan")
            drawdown = float("inf")

        gates = {
            "session_integrity": session_integrity,
            "cycle_integrity": cycle_integrity,
            "trade_integrity": trade_integrity,
            "replayable": replayable,
            "sessions": session_integrity and len(set(observed_sessions)) >= PAPER_REQUIRED_SESSIONS,
            "month_end_cycles": cycle_integrity
            and len(unique_cycles) >= PAPER_REQUIRED_MONTH_END_CYCLES
            and len(cycle_months) >= PAPER_REQUIRED_MONTH_END_CYCLES,
            "closed_trades": trade_integrity and len(closed) >= PAPER_REQUIRED_CLOSED_TRADES,
            "return_vs_bil": session_integrity and paper_return > bil_return,
            "max_drawdown": session_integrity and drawdown <= 0.20,
        }
        failures = tuple(name for name, passed in gates.items() if not passed)
        integrity = all(gates[name] for name in ("session_integrity", "cycle_integrity", "trade_integrity", "replayable"))
        qualified = not failures
        status = "PAPER_QUALIFIED" if qualified else ("PAPER_COLLECTING" if integrity else "PAPER_BLOCKED")
        return PaperQualificationDecision(
            qualified=qualified,
            status=status,
            gates=gates,
            failures=failures,
            metrics={
                "unique_sessions": len(set(observed_sessions)),
                "complete_month_end_cycles": len(unique_cycles),
                "closed_trades": len(closed),
                "paper_return": paper_return,
                "bil_return": bil_return,
                "max_drawdown": drawdown,
            },
        )


@dataclass(frozen=True)
class TDXSampleInstrument:
    symbol: str
    exchange: str


# Stable, declared before any qualification observations.  It deliberately
# spans both listing venues and avoids symbols with punctuation aliases.
TDX_QUALIFICATION_SAMPLE: tuple[TDXSampleInstrument, ...] = (
    TDXSampleInstrument("SPY.US", "NYSE"),
    TDXSampleInstrument("IBM.US", "NYSE"),
    TDXSampleInstrument("JNJ.US", "NYSE"),
    TDXSampleInstrument("JPM.US", "NYSE"),
    TDXSampleInstrument("KO.US", "NYSE"),
    TDXSampleInstrument("DIS.US", "NYSE"),
    TDXSampleInstrument("WMT.US", "NYSE"),
    TDXSampleInstrument("XOM.US", "NYSE"),
    TDXSampleInstrument("CVX.US", "NYSE"),
    TDXSampleInstrument("CAT.US", "NYSE"),
    TDXSampleInstrument("BA.US", "NYSE"),
    TDXSampleInstrument("GE.US", "NYSE"),
    TDXSampleInstrument("GS.US", "NYSE"),
    TDXSampleInstrument("HD.US", "NYSE"),
    TDXSampleInstrument("MCD.US", "NYSE"),
    TDXSampleInstrument("NKE.US", "NYSE"),
    TDXSampleInstrument("AAPL.US", "NASDAQ"),
    TDXSampleInstrument("MSFT.US", "NASDAQ"),
    TDXSampleInstrument("NVDA.US", "NASDAQ"),
    TDXSampleInstrument("AMZN.US", "NASDAQ"),
    TDXSampleInstrument("META.US", "NASDAQ"),
    TDXSampleInstrument("GOOGL.US", "NASDAQ"),
    TDXSampleInstrument("TSLA.US", "NASDAQ"),
    TDXSampleInstrument("AVGO.US", "NASDAQ"),
    TDXSampleInstrument("COST.US", "NASDAQ"),
    TDXSampleInstrument("PEP.US", "NASDAQ"),
    TDXSampleInstrument("CSCO.US", "NASDAQ"),
    TDXSampleInstrument("ADBE.US", "NASDAQ"),
    TDXSampleInstrument("AMD.US", "NASDAQ"),
    TDXSampleInstrument("QCOM.US", "NASDAQ"),
    TDXSampleInstrument("INTU.US", "NASDAQ"),
)


@dataclass(frozen=True)
class TDXDailySymbolEvidence:
    session: date
    symbol: str
    exchange: str
    expected_poll_slots: int
    captured_poll_slots: int
    fresh_poll_slots: int
    poll_interval_seconds: int
    maximum_source_latency_seconds: float
    opening_observed_at: datetime
    opening_source_at: datetime
    snapshot_open: float
    final_raw_open: float
    timezone_errors: int = 0
    future_timestamp_errors: int = 0
    market_state_errors: int = 0


@dataclass(frozen=True)
class TDXQuoteQualificationDecision:
    qualified: bool
    status: str
    gates: Mapping[str, bool]
    failures: tuple[str, ...]
    metrics: Mapping[str, float | int]


def _aware_and_session(value: datetime, session: date) -> bool:
    if value.tzinfo is None or value.utcoffset() is None:
        return False
    return value.astimezone(NY_TZ).date() == session


def evaluate_tdx_quote_qualification(
    evidence: Sequence[TDXDailySymbolEvidence],
    frozen_xnys_sessions: Iterable[object],
) -> TDXQuoteQualificationDecision:
    """Evaluate the fixed 20-session, SPY+30-symbol TDX shadow test."""

    calendar = tuple(sorted({_day(item).date() for item in frozen_xnys_sessions}))
    sample = {(item.symbol, item.exchange) for item in TDX_QUALIFICATION_SAMPLE}
    observed_sessions = tuple(sorted({item.session for item in evidence}))
    positions = [calendar.index(item) for item in observed_sessions if item in calendar]
    consecutive = (
        len(observed_sessions) == TDX_REQUIRED_SESSIONS
        and len(positions) == TDX_REQUIRED_SESSIONS
        and positions == list(range(positions[0], positions[0] + TDX_REQUIRED_SESSIONS))
        if positions
        else False
    )
    keys = [(item.session, item.symbol, item.exchange) for item in evidence]
    expected_keys = {
        (session, symbol, exchange)
        for session in observed_sessions
        for symbol, exchange in sample
    }
    exact_sample = (
        len(keys) == len(set(keys))
        and set(keys) == expected_keys
        and len(evidence) == TDX_REQUIRED_SESSIONS * len(sample)
    )

    schedule_ok = exact_sample and all(
        item.expected_poll_slots == REGULAR_SESSION_POLL_SLOTS
        and item.poll_interval_seconds == 60
        and 0 <= item.fresh_poll_slots <= item.captured_poll_slots <= item.expected_poll_slots
        for item in evidence
    )
    total_expected = sum(item.expected_poll_slots for item in evidence)
    total_fresh = sum(item.fresh_poll_slots for item in evidence)
    fresh_ratio = total_fresh / total_expected if total_expected else 0.0
    freshness = schedule_ok and fresh_ratio >= 0.995 and all(
        math.isfinite(item.maximum_source_latency_seconds)
        and 0 <= item.maximum_source_latency_seconds <= 90
        for item in evidence
    )
    timestamp_integrity = exact_sample and all(
        _aware_and_session(item.opening_observed_at, item.session)
        and _aware_and_session(item.opening_source_at, item.session)
        and item.opening_source_at <= item.opening_observed_at
        and item.timezone_errors == 0
        and item.future_timestamp_errors == 0
        and item.market_state_errors == 0
        for item in evidence
    )
    opening_capture = exact_sample and all(
        time(9, 30)
        <= item.opening_observed_at.astimezone(NY_TZ).time().replace(tzinfo=None)
        <= time(9, 35)
        and math.isfinite(item.snapshot_open)
        and item.snapshot_open > 0
        and math.isfinite(item.final_raw_open)
        and item.final_raw_open > 0
        for item in evidence
    )
    opening_accuracy = opening_capture and all(
        abs(item.snapshot_open - item.final_raw_open)
        <= max(0.02, item.final_raw_open * 0.0005) + 1e-12
        for item in evidence
    )
    gates = {
        "twenty_consecutive_xnys_sessions": consecutive,
        "fixed_cross_exchange_sample": exact_sample,
        "sixty_second_schedule": schedule_ok,
        "fresh_quote_ratio": freshness,
        "opening_capture": opening_capture,
        "opening_accuracy": opening_accuracy,
        "timestamp_and_market_state_integrity": timestamp_integrity,
    }
    failures = tuple(name for name, passed in gates.items() if not passed)
    qualified = not failures
    return TDXQuoteQualificationDecision(
        qualified=qualified,
        status="TDX_QUALIFIED" if qualified else "PAPER_BLOCKED",
        gates=gates,
        failures=failures,
        metrics={
            "sessions": len(observed_sessions),
            "symbols": len(sample),
            "opening_observations": len(evidence) if opening_capture else 0,
            "expected_poll_slots": total_expected,
            "fresh_poll_slots": total_fresh,
            "fresh_quote_ratio": fresh_ratio,
        },
    )


__all__ = [
    "HistoricalQualificationResult",
    "HistoricalQualificationService",
    "HistoricalRunRequest",
    "HistoricalWindowSplit",
    "PAPER_REQUIRED_CLOSED_TRADES",
    "PAPER_REQUIRED_MONTH_END_CYCLES",
    "PAPER_REQUIRED_SESSIONS",
    "PaperCycleEvidence",
    "PaperQualificationDecision",
    "PaperQualificationTracker",
    "PaperSessionEvidence",
    "PaperTradeEvidence",
    "QualificationError",
    "REGULAR_SESSION_POLL_SLOTS",
    "SealedHoldoutError",
    "StrictQualificationRunner",
    "run_strict_qualification_backtest",
    "TDXDailySymbolEvidence",
    "TDXQuoteQualificationDecision",
    "TDXSampleInstrument",
    "TDX_QUALIFICATION_SAMPLE",
    "evaluate_tdx_quote_qualification",
]
