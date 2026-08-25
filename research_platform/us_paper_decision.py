"""Fail-closed month-end decisions for the US momentum paper sleeve.

This module is the only bridge between a certified US PIT release, fresh TDX
daily bars, :class:`USMomentumStrategy`, and the isolated US paper executor.
It intentionally does not call the generic platform scanner (which also owns
A-share paths) and it never exposes a broker or live-order interface.

``USMomentumStrategy.scan(backtest_mode=True)`` is used here solely to enable
BUY *signal emission*.  Execution remains exclusively in
``USMomentumPaperService``.  Before the emitted signals are admitted, their
timestamps and identifiers are replaced with the causal paper contract:

* generated at the actual, post-close data-observation time;
* eligible for automatic policy approval at 09:20 New York time on the next
  session in the release-owned frozen XNYS calendar; and
* expired at 09:35, so no daily Open can be filled in later.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .strategies.us_momentum import USMomentumStrategy
from .us_paper import (
    NY_TZ,
    USMomentumPaperService,
    USPaperConflictError,
    USPaperState,
)
from .us_pit import ReleaseStatus, USBacktestDataset


UTC = ZoneInfo("UTC")
REQUIRED_BAR_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


class USPaperDecisionError(RuntimeError):
    """The dedicated US paper decision contract is invalid."""


class USPaperDecisionSource(Protocol):
    """Fresh daily-bar source used by the dedicated coordinator."""

    def fetch(
        self,
        codes: Sequence[str],
        *,
        asof: date,
        count: int,
    ) -> "USPaperBarBundle": ...


class _TDXProvider(Protocol):
    def fetch_bars(
        self,
        codes: list[str],
        period: str,
        count: int,
        *,
        fields: tuple[str, ...],
        dividend_type: str,
        end_time: str,
    ) -> Mapping[str, pd.DataFrame]: ...


@dataclass(frozen=True)
class USPaperBarBundle:
    """One causally observed pair of TDX front/raw daily datasets."""

    front_bars: Mapping[str, pd.DataFrame]
    raw_bars: Mapping[str, pd.DataFrame]
    observed_at: datetime
    source_id: str = "TDX"

    def __post_init__(self) -> None:
        observed = _aware(self.observed_at)
        object.__setattr__(self, "observed_at", observed)
        if self.source_id.strip().upper() != "TDX":
            raise ValueError("formal US paper decisions require TDX daily bars")


class TDXCurrentUSBarSource:
    """Read current US daily bars from a cache-disabled ``TdxProvider``.

    The provider is injected and must already be inside its context manager.
    This wrapper never enumerates A shares and never invokes a generic scan.
    """

    def __init__(
        self,
        provider: _TDXProvider,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if bool(getattr(provider, "cache_reads", False)):
            raise ValueError(
                "TDXCurrentUSBarSource requires TdxProvider(cache_reads=False)"
            )
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(NY_TZ))

    def fetch(
        self,
        codes: Sequence[str],
        *,
        asof: date,
        count: int,
    ) -> USPaperBarBundle:
        requested = sorted({_us_code(item) for item in codes})
        if not requested:
            raise ValueError("at least one US code is required")
        end_time = f"{asof.isoformat()} 23:59:59"
        front = self.provider.fetch_bars(
            requested,
            "1d",
            count,
            fields=REQUIRED_BAR_COLUMNS,
            dividend_type="front",
            end_time=end_time,
        )
        raw = self.provider.fetch_bars(
            requested,
            "1d",
            count,
            fields=REQUIRED_BAR_COLUMNS,
            dividend_type="none",
            end_time=end_time,
        )
        return USPaperBarBundle(
            front_bars=front,
            raw_bars=raw,
            observed_at=_aware(self.clock()),
            source_id="TDX",
        )


@dataclass(frozen=True)
class USPaperDecisionConfig:
    universe_id: str = "sp500_ivv_proxy_v1"
    benchmark_code: str = "SPY.US"
    history_bars: int = 1300
    required_sessions: int = 282
    close_delay_minutes: int = 15
    retry_minutes: int = 5
    approval_time: time = time(9, 20)
    expiry_time: time = time(9, 35)

    def __post_init__(self) -> None:
        if self.universe_id != "sp500_ivv_proxy_v1":
            raise ValueError("US paper universe is fixed to sp500_ivv_proxy_v1")
        object.__setattr__(self, "benchmark_code", _us_code(self.benchmark_code))
        if self.history_bars < self.required_sessions:
            raise ValueError("history_bars must cover required_sessions")
        if self.required_sessions < 282:
            raise ValueError("formal US paper decisions require at least 282 sessions")
        if self.close_delay_minutes != 15:
            raise ValueError("month-end decisions start exactly 15 minutes after close")
        if self.retry_minutes != 5:
            raise ValueError("month-end data retries are fixed at five minutes")
        if self.approval_time != time(9, 20) or self.expiry_time != time(9, 35):
            raise ValueError("paper approval/expiry times are fixed at 09:20/09:35")


class USPaperDecisionCoordinator:
    """Create one deterministic, paper-only rebalance period per real month-end."""

    def __init__(
        self,
        *,
        dataset: USBacktestDataset,
        paper: USMomentumPaperService,
        bar_source: USPaperDecisionSource,
        strategy: USMomentumStrategy | None = None,
        config: USPaperDecisionConfig | None = None,
        manifest_sha256: str | None = None,
        audit_store: "USPaperDecisionAuditStore | None" = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.dataset = dataset
        self.paper = paper
        self.bar_source = bar_source
        self.strategy = strategy or USMomentumStrategy()
        self.config = config or USPaperDecisionConfig()
        self.manifest_sha256 = _sha256_value(
            manifest_sha256, "manifest_sha256"
        )
        self.audit_store = audit_store
        self.clock = clock or (lambda: datetime.now(NY_TZ))

    def decide(
        self,
        decision_date: date | str | pd.Timestamp,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Attempt the frozen-calendar month-end decision.

        Routine timing/data failures return a structured fail-closed result and
        do not write a paper period.  Retryable data failures carry the next
        five-minute attempt time.  Passing ``now`` is intended for deterministic
        tests/replay; normal workers should rely on the injected clock.
        """

        explicit_now = now is not None
        current = _aware(now if now is not None else self.clock())
        day = _day(decision_date)
        try:
            gate = self._calendar_gate(day, current)
        except (KeyError, TypeError, ValueError, USPaperDecisionError) as exc:
            return self._result(
                "PAPER_BLOCKED",
                day,
                None,
                reason=f"FROZEN_CALENDAR_INVALID:{exc}",
                audit={"release_id": self.dataset.release_id},
            )
        if gate["status"] != "READY_TO_DECIDE":
            return gate

        execution_day = date.fromisoformat(str(gate["execution_session"]))
        try:
            existing = self._existing_period(day, execution_day)
        except USPaperDecisionError as exc:
            return self._blocked(
                day,
                execution_day,
                current,
                f"EXISTING_PERIOD_INVALID:{exc}",
                False,
            )
        if existing is not None:
            return self._result(
                "PERIOD_EXISTS",
                day,
                execution_day,
                reason="MONTH_END_PERIOD_ALREADY_RECORDED",
                period=existing,
                audit={"release_id": self.dataset.release_id},
            )

        try:
            identity = self._identity_context(day)
        except (KeyError, TypeError, ValueError, USPaperDecisionError) as exc:
            return self._blocked(day, execution_day, current, f"IDENTITY:{exc}", False)

        requested_codes = sorted(
            set(identity["code_by_security_id"].values())
            | {self.config.benchmark_code}
        )
        try:
            bundle = self.bar_source.fetch(
                requested_codes,
                asof=day,
                count=self.config.history_bars,
            )
        except Exception as exc:  # vendor failures are data blockers, never fallbacks
            return self._blocked(
                day,
                execution_day,
                current,
                f"TDX_FETCH_FAILED:{type(exc).__name__}:{exc}",
                True,
            )
        if not isinstance(bundle, USPaperBarBundle):
            return self._blocked(
                day,
                execution_day,
                current,
                "TDX_FETCH_RETURNED_AN_INVALID_BUNDLE",
                True,
            )

        processed_at = current if explicit_now else max(current, _aware(self.clock()))
        if bundle.observed_at > processed_at:
            return self._blocked(
                day,
                execution_day,
                current,
                "TDX_OBSERVATION_FROM_FUTURE",
                True,
            )
        # ``generated_at`` is the time the decision was actually computed, not
        # the market close nor the vendor's (possibly earlier) observation time.
        decision_at = max(
            processed_at, bundle.observed_at, gate["earliest_decision_at"]
        )
        if decision_at.date() != day:
            return self._blocked(
                day,
                execution_day,
                processed_at,
                "DECISION_MONTH_CLOSED_BEFORE_DATA_BECAME_AVAILABLE",
                False,
            )

        try:
            front, raw, coverage = self._validate_bundle(
                bundle,
                day,
                identity["security_id_by_code"],
                identity["alias_valid_from"],
            )
            signals, scan_audit = self._scan(
                day,
                execution_day,
                decision_at,
                front,
                raw,
                identity,
            )
        except (KeyError, TypeError, ValueError, USPaperDecisionError) as exc:
            return self._blocked(
                day,
                execution_day,
                processed_at,
                f"TDX_DATA_INVALID:{exc}",
                True,
            )

        if decision_at >= datetime.combine(
            execution_day, self.config.approval_time, NY_TZ
        ):
            return self._blocked(
                day,
                execution_day,
                processed_at,
                "AUTOMATIC_APPROVAL_CUTOFF_MISSED",
                False,
            )

        try:
            period = self.paper.create_period(
                signals,
                now=max(processed_at, decision_at),
                execution_session=execution_day,
                decision_at=decision_at,
                pit_release_id=self.dataset.release_id,
                manifest_sha256=self.manifest_sha256,
                position_aliases=identity["position_aliases"],
            )
            status = "PERIOD_CREATED"
        except USPaperConflictError:
            # A concurrent worker may have committed the unique month first.
            # Return that row only if its calendar contract is identical.
            period = self._existing_period(day, execution_day)
            if period is None:
                return self._blocked(
                    day,
                    execution_day,
                    processed_at,
                    "PAPER_PERIOD_IDEMPOTENCY_CONFLICT",
                    False,
                )
            status = "PERIOD_EXISTS"
        except Exception as exc:
            return self._blocked(
                day,
                execution_day,
                processed_at,
                f"PAPER_PERIOD_CREATE_FAILED:{type(exc).__name__}:{exc}",
                False,
            )

        result = self._result(
            status,
            day,
            execution_day,
            reason="US_MONTH_END_PAPER_SIGNALS_READY",
            period=period,
            signals=signals,
            audit={
                "release_id": self.dataset.release_id,
                "universe_id": self.dataset.universe_id,
                "decision_at": decision_at.isoformat(),
                "tdx_observed_at": bundle.observed_at.isoformat(),
                "tdx_source": bundle.source_id,
                "front_sha256": _bars_hash(front),
                "raw_sha256": _bars_hash(raw),
                "coverage": coverage,
                **scan_audit,
            },
        )
        if self.audit_store is not None:
            archive = self.audit_store.archive(
                decision_date=day,
                execution_date=execution_day,
                release_id=self.dataset.release_id,
                manifest_sha256=self.manifest_sha256,
                strategy_version=self.strategy.metadata.version,
                strategy_parameters=_json_safe(vars(self.strategy.parameters)),
                strategy_code_sha256=_source_code_sha256(type(self.strategy)),
                decision_engine_code_sha256=_source_code_sha256(
                    USPaperDecisionCoordinator
                ),
                front=front,
                raw=raw,
                names={
                    code: _security_name(self.dataset.security_master, security_id)
                    for security_id, code in identity["code_by_security_id"].items()
                }
                | {self.config.benchmark_code: "SPDR S&P 500 ETF Trust"},
                positions=self._strategy_positions(identity),
                position_aliases=identity["position_aliases"],
                security_id_by_code=identity["security_id_by_code"],
                tradable_codes=sorted(
                    identity["code_by_security_id"][security_id]
                    for security_id in identity["members"]
                ),
                result=result,
            )
            result = {**result, "decision_archive": archive}
        return result

    def _calendar_gate(self, day: date, current: datetime) -> dict[str, Any]:
        permanent = self._validate_release()
        if permanent is not None:
            return self._result(
                "PAPER_BLOCKED", day, None, reason=permanent, audit={}
            )
        calendar = _calendar(self.dataset.calendar)
        sessions = tuple(calendar.index.date)
        if day not in sessions:
            return self._result(
                "NOT_MONTH_END", day, None, reason="NOT_A_FROZEN_XNYS_SESSION"
            )
        same_month = [item for item in sessions if item.year == day.year and item.month == day.month]
        if not same_month or max(same_month) != day:
            return self._result(
                "NOT_MONTH_END", day, None, reason="NOT_THE_REAL_XNYS_MONTH_END"
            )
        later = [item for item in sessions if item > day]
        if not later:
            return self._result(
                "PAPER_BLOCKED",
                day,
                None,
                reason="FROZEN_CALENDAR_LACKS_NEXT_EXECUTION_SESSION",
            )
        execution_day = min(later)
        market_close = calendar.loc[pd.Timestamp(day), "market_close"]
        earliest = _aware(market_close) + timedelta(
            minutes=self.config.close_delay_minutes
        )
        if current < earliest:
            return self._result(
                "WAITING_CLOSE_DATA",
                day,
                execution_day,
                reason="MONTH_END_CLOSE_PLUS_15_NOT_REACHED",
                next_retry_at=earliest,
            )
        if current.date() != day:
            return self._result(
                "PAPER_BLOCKED",
                day,
                execution_day,
                reason="DECISION_MONTH_CLOSED_WITHOUT_A_CAUSAL_PERIOD",
            )
        if not _is_retry_slot(current, earliest, self.config.retry_minutes):
            return self._result(
                "WAITING_RETRY_SLOT",
                day,
                execution_day,
                reason="MONTH_END_DECISIONS_RETRY_EVERY_FIVE_MINUTES",
                next_retry_at=_next_retry(current, earliest, self.config.retry_minutes),
            )
        return {
            **self._result("READY_TO_DECIDE", day, execution_day),
            "earliest_decision_at": earliest,
        }

    def _validate_release(self) -> str | None:
        if self.dataset.universe_id != self.config.universe_id:
            return "US_PIT_UNIVERSE_MISMATCH"
        if self.dataset.quality_report.status != ReleaseStatus.DATA_READY:
            return "US_PIT_RELEASE_NOT_DATA_READY"
        if not self.dataset.includes_delisted:
            return "US_PIT_DELISTING_COVERAGE_NOT_DERIVED"
        if not str(self.dataset.release_id).strip():
            return "US_PIT_RELEASE_ID_MISSING"
        status = self.paper.status()
        if not status.get("paper_only") or status.get("mode") != "PAPER":
            return "US_PAPER_EXECUTOR_CONTRACT_INVALID"
        account = status.get("account") or {}
        if str(account.get("status")) == USPaperState.KILLED.value:
            return "US_PAPER_EXECUTOR_KILLED"
        return None

    def _identity_context(self, day: date) -> dict[str, Any]:
        decision = pd.Timestamp(day)
        if decision not in self.dataset.membership_by_date:
            raise USPaperDecisionError("exact month-end PIT membership is missing")
        members = set(self.dataset.membership_by_date[decision])
        pit_signal = self.dataset.signal_bars_by_decision.get(decision)
        if pit_signal is None:
            raise USPaperDecisionError("exact month-end PIT signal artifact is missing")
        missing_pit = sorted(members - set(pit_signal))
        if missing_pit:
            raise USPaperDecisionError(
                "PIT signal artifact lacks current members:" + ",".join(missing_pit)
            )
        benchmark = {
            _us_code(code): frame for code, frame in self.dataset.benchmark_bars.items()
        }
        if self.config.benchmark_code not in benchmark:
            raise USPaperDecisionError("release lacks the SPY benchmark")

        aliases = _aliases(self.dataset.listing_aliases)
        code_by_id: dict[str, str] = {}
        alias_valid_from: dict[str, date] = {}
        for security_id in sorted(members):
            active = _active_aliases(aliases, security_id, day)
            if len(active) != 1:
                raise USPaperDecisionError(
                    f"expected one active TDX alias for {security_id}, found {len(active)}"
                )
            code = _us_code(active.iloc[0]["vendor_code"])
            if code in code_by_id.values():
                raise USPaperDecisionError(f"TDX alias collision: {code}")
            code_by_id[security_id] = code
            alias_valid_from[security_id] = active.iloc[0]["valid_from"].date()

        paper_status = self.paper.status()
        positions = list(paper_status.get("positions") or [])
        position_security_ids: dict[str, str] = {}
        position_aliases: dict[str, str] = {}
        for position in positions:
            held_code = _us_code(position.get("code"))
            security_id = _stable_security_id(position.get("security_id"))
            if security_id in position_aliases:
                raise USPaperDecisionError(
                    f"duplicate persisted position identity: {security_id}"
                )
            current_alias = _active_aliases(aliases, security_id, day)
            if len(current_alias) != 1:
                raise USPaperDecisionError(
                    f"held security {security_id} lacks one current TDX alias"
                )
            current_code = _us_code(current_alias.iloc[0]["vendor_code"])
            # The persisted stable ID is authoritative.  A stale entry ticker
            # may legitimately be absent after FB->META.  It is only a conflict
            # when the release currently assigns that stale ticker to another
            # security, or the new ticker is already assigned to another held
            # stable identity.
            active_held_code = aliases[
                aliases["vendor_code"].eq(held_code)
                & (aliases["valid_from"] <= pd.Timestamp(day))
                & (
                    aliases["valid_to"].isna()
                    | (aliases["valid_to"] >= pd.Timestamp(day))
                )
            ]
            conflicting_ids = sorted(
                set(active_held_code["security_id"].astype(str)) - {security_id}
            )
            if conflicting_ids:
                raise USPaperDecisionError(
                    f"persisted alias {held_code} now belongs to {conflicting_ids}"
                )
            current_owner = next(
                (
                    stable_id
                    for stable_id, code in code_by_id.items()
                    if code == current_code and stable_id != security_id
                ),
                None,
            )
            if current_owner is not None:
                raise USPaperDecisionError(
                    f"current alias {current_code} conflicts with {current_owner}"
                )
            existing_code = code_by_id.get(security_id)
            if existing_code is not None and existing_code != current_code:
                raise USPaperDecisionError(
                    f"release has conflicting current aliases for {security_id}"
                )
            code_by_id[security_id] = current_code
            alias_valid_from.setdefault(
                security_id, current_alias.iloc[0]["valid_from"].date()
            )
            if current_code in position_security_ids:
                raise USPaperDecisionError(
                    f"multiple persisted positions resolve to {current_code}"
                )
            position_security_ids[current_code] = security_id
            position_aliases[security_id] = current_code

        security_id_by_code = {code: sid for sid, code in code_by_id.items()}
        if len(security_id_by_code) != len(code_by_id):
            raise USPaperDecisionError("stable IDs do not map to unique TDX aliases")
        return {
            "members": members,
            "code_by_security_id": code_by_id,
            "security_id_by_code": security_id_by_code,
            "alias_valid_from": alias_valid_from,
            "position_security_ids": position_security_ids,
            "position_aliases": position_aliases,
            "positions": positions,
            "paper_status": paper_status,
        }

    def _validate_bundle(
        self,
        bundle: USPaperBarBundle,
        day: date,
        security_id_by_code: Mapping[str, str],
        alias_valid_from: Mapping[str, date],
    ) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, Any]]:
        if bundle.source_id.upper() != "TDX":
            raise USPaperDecisionError("AKShare or another fallback cannot execute paper")
        front = _code_map(bundle.front_bars)
        raw = _code_map(bundle.raw_bars)
        required = set(security_id_by_code) | {self.config.benchmark_code}
        missing_front = sorted(required - set(front))
        missing_raw = sorted(required - set(raw))
        if missing_front or missing_raw:
            raise USPaperDecisionError(
                f"missing front={missing_front}, raw={missing_raw}"
            )

        calendar = _calendar(self.dataset.calendar)
        sessions = pd.DatetimeIndex(calendar.index[calendar.index <= pd.Timestamp(day)])
        if len(sessions) < self.config.required_sessions:
            raise USPaperDecisionError("frozen calendar lacks the 282-session warmup")
        exceptions = _verified_exceptions(self.dataset.session_exceptions)
        coverage: dict[str, Any] = {}
        for code in sorted(required):
            security_id = security_id_by_code.get(code)
            listed = alias_valid_from.get(security_id) if security_id else None
            expected_start = sessions[-self.config.required_sessions]
            if listed is not None:
                expected_start = max(expected_start, pd.Timestamp(listed))
            expected = sessions[sessions >= expected_start]
            explained = exceptions.get(security_id or "", set())
            front[code] = _validate_frame(
                front[code], code, "front", day, expected, explained
            )
            raw[code] = _validate_frame(
                raw[code], code, "raw", day, expected, explained
            )
            coverage[code] = {
                "security_id": security_id or "BENCHMARK:SPY",
                "expected_sessions": len(expected),
                "front_sessions": len(front[code].index.intersection(expected)),
                "raw_sessions": len(raw[code].index.intersection(expected)),
                "explained_exceptions": len(explained & set(expected.date)),
            }
        return front, raw, coverage

    def _scan(
        self,
        day: date,
        execution_day: date,
        decision_at: datetime,
        front: Mapping[str, pd.DataFrame],
        raw: Mapping[str, pd.DataFrame],
        identity: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        members: set[str] = set(identity["members"])
        code_by_id: Mapping[str, str] = identity["code_by_security_id"]
        member_codes = {code_by_id[item] for item in members}
        positions = self._strategy_positions(identity)
        names = {
            code: _security_name(self.dataset.security_master, security_id)
            for security_id, code in code_by_id.items()
        }
        names[self.config.benchmark_code] = "SPDR S&P 500 ETF Trust"
        run_id = "uspaper_" + _hash(
            {
                "release_id": self.dataset.release_id,
                "decision_date": day.isoformat(),
                "strategy_version": self.strategy.metadata.version,
            }
        )[:24]
        scan = self.strategy.scan(
            run_id=run_id,
            front_bars=dict(front),
            raw_bars=dict(raw),
            names=names,
            positions=positions,
            runtime_state={},
            # Signal emission only.  No backtest engine or broker is invoked.
            backtest_mode=True,
            asof=pd.Timestamp(day),
            is_rebalance_day=True,
            tradable_codes=member_codes,
        )
        if str(scan.state.get("status")) != "REBALANCE_READY":
            raise USPaperDecisionError(
                f"strategy did not produce a closed month-end decision: {scan.state.get('status')}"
            )
        stale = tuple(scan.state.get("stale_rejected") or ())
        if stale:
            raise USPaperDecisionError("strategy reported stale TDX bars:" + ",".join(stale))

        available_at = datetime.combine(
            execution_day, self.config.approval_time, NY_TZ
        )
        valid_until = datetime.combine(
            execution_day, self.config.expiry_time, NY_TZ
        )
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in scan.signals:
            code = _us_code(item.code)
            security_id = identity["security_id_by_code"].get(code)
            if security_id is None:
                raise USPaperDecisionError(f"signal code lacks a stable ID: {code}")
            if item.side == "BUY" and security_id not in members:
                raise USPaperDecisionError(f"BUY escaped current PIT membership: {code}")
            signal = _paper_signal(
                item,
                release_id=self.dataset.release_id,
                manifest_sha256=self.manifest_sha256,
                security_id=security_id,
                generated_at=decision_at,
                available_at=available_at,
                valid_until=valid_until,
            )
            pair = (code, signal["side"])
            if pair in seen:
                raise USPaperDecisionError(f"duplicate strategy signal: {pair}")
            seen.add(pair)
            normalized.append(signal)

        held_ids: Mapping[str, str] = identity["position_security_ids"]
        for held_code, security_id in sorted(held_ids.items()):
            if security_id in members or (held_code, "SELL") in seen:
                continue
            normalized.append(
                _forced_membership_exit(
                    code=held_code,
                    security_id=security_id,
                    release_id=self.dataset.release_id,
                    manifest_sha256=self.manifest_sha256,
                    generated_at=decision_at,
                    available_at=available_at,
                    valid_until=valid_until,
                )
            )
            seen.add((held_code, "SELL"))

        normalized.sort(key=lambda row: (row["side"] != "SELL", row["code"], row["signal_id"]))
        return normalized, {
            "run_id": run_id,
            "strategy_version": self.strategy.metadata.version,
            "signal_emission_adapter": "US_MOMENTUM_PAPER_ONLY",
            "backtest_mode_used_for_signal_emission_only": True,
            "member_count": len(members),
            "held_count": len(held_ids),
            "signal_count": len(normalized),
            "strategy_state": _json_safe(scan.state),
        }

    def _strategy_positions(self, identity: Mapping[str, Any]) -> list[dict[str, Any]]:
        status = identity["paper_status"]
        positions = list(identity["positions"])
        account = status.get("account") or {}
        cash = _finite_nonnegative(account.get("cash", 0.0), "paper cash")
        marked = 0.0
        for position in positions:
            quantity = _finite_nonnegative(position.get("quantity"), "position quantity")
            last = _positive(position.get("last_price"), "position last_price")
            marked += quantity * last
        equity = cash + marked
        if not math.isfinite(equity) or equity <= 0:
            raise USPaperDecisionError("paper equity must be positive")
        output: list[dict[str, Any]] = []
        for position in positions:
            quantity = _finite_nonnegative(position.get("quantity"), "position quantity")
            last = _positive(position.get("last_price"), "position last_price")
            security_id = _stable_security_id(position.get("security_id"))
            current_code = identity["position_aliases"].get(security_id)
            if current_code is None:
                raise USPaperDecisionError(
                    f"held security {security_id} lacks a verified current alias"
                )
            output.append(
                {
                    **dict(position),
                    "code": current_code,
                    "weight": quantity * last / equity,
                }
            )
        return output

    def _existing_period(self, day: date, execution_day: date) -> dict[str, Any] | None:
        status = self.paper.status()
        period_key = f"{day.year:04d}-{day.month:02d}"
        matches = [
            item for item in status.get("periods", [])
            if str(item.get("period_key")) == period_key
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise USPaperDecisionError("paper period key is not unique")
        period = dict(matches[0])
        if str(period.get("execution_session")) != execution_day.isoformat():
            raise USPaperDecisionError(
                "existing paper period targets a different frozen execution session"
            )
        decision_at = _aware(period.get("decision_at"))
        if decision_at.date() != day:
            raise USPaperDecisionError(
                "existing paper period does not carry a causal month-end timestamp"
            )
        period["orders"] = [
            dict(item) for item in status.get("orders", [])
            if str(item.get("period_id")) == str(period.get("period_id"))
        ]
        period["paper_only"] = True
        return period

    def _blocked(
        self,
        day: date,
        execution_day: date,
        current: datetime,
        reason: str,
        retryable: bool,
    ) -> dict[str, Any]:
        next_retry: datetime | None = None
        status = "PAPER_BLOCKED"
        if retryable and current.date() == day:
            calendar = _calendar(self.dataset.calendar)
            earliest = _aware(calendar.loc[pd.Timestamp(day), "market_close"]) + timedelta(
                minutes=self.config.close_delay_minutes
            )
            candidate = _next_retry(current, earliest, self.config.retry_minutes)
            if candidate.date() == day:
                status = "RETRY_SCHEDULED"
                next_retry = candidate
        return self._result(
            status,
            day,
            execution_day,
            reason=reason,
            next_retry_at=next_retry,
            audit={"release_id": self.dataset.release_id},
        )

    @staticmethod
    def _result(
        status: str,
        decision_day: date,
        execution_day: date | None,
        *,
        reason: str = "",
        next_retry_at: datetime | None = None,
        period: Mapping[str, Any] | None = None,
        signals: Sequence[Mapping[str, Any]] = (),
        audit: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "mode": "PAPER",
            "paper_only": True,
            "broker_writes_enabled": False,
            "status": status,
            "reason": reason,
            "decision_date": decision_day.isoformat(),
            "execution_session": (
                execution_day.isoformat() if execution_day is not None else None
            ),
            "next_retry_at": (
                next_retry_at.isoformat() if next_retry_at is not None else None
            ),
            "retry_seconds": 300 if next_retry_at is not None else None,
            "period": dict(period) if period is not None else None,
            "signals": [dict(item) for item in signals],
            "audit": dict(audit or {}),
        }


def _calendar(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"session_date", "market_open", "market_close"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise USPaperDecisionError("frozen XNYS calendar is incomplete")
    value = frame.copy()
    value["session_date"] = pd.to_datetime(value["session_date"], errors="raise").dt.normalize()
    if value["session_date"].duplicated().any():
        raise USPaperDecisionError("frozen XNYS calendar contains duplicate sessions")
    value = value.set_index("session_date").sort_index()
    for opened, closed in zip(value["market_open"], value["market_close"], strict=True):
        opening = _aware(opened)
        closing = _aware(closed)
        if opening >= closing or opening.date() != closing.date():
            raise USPaperDecisionError("frozen XNYS session hours are invalid")
    return value


def _aliases(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"security_id", "vendor_code", "valid_from", "valid_to"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise USPaperDecisionError("listing aliases are incomplete")
    value = frame.copy()
    value["security_id"] = value["security_id"].astype(str)
    value["vendor_code"] = value["vendor_code"].astype(str).str.strip().str.upper()
    value["valid_from"] = pd.to_datetime(value["valid_from"], errors="raise").dt.normalize()
    value["valid_to"] = pd.to_datetime(value["valid_to"], errors="coerce").dt.normalize()
    return value


def _active_aliases(aliases: pd.DataFrame, security_id: str, day: date) -> pd.DataFrame:
    boundary = pd.Timestamp(day)
    return aliases[
        aliases["security_id"].eq(str(security_id))
        & (aliases["valid_from"] <= boundary)
        & (aliases["valid_to"].isna() | (aliases["valid_to"] >= boundary))
    ]


def _verified_exceptions(frame: pd.DataFrame) -> dict[str, set[date]]:
    if frame is None or frame.empty:
        return {}
    required = {"security_id", "session_date", "verified"}
    if not required.issubset(frame.columns):
        raise USPaperDecisionError("session exceptions are malformed")
    value = frame.copy()
    verified = value["verified"].map(_truthy)
    dates = pd.to_datetime(value["session_date"], errors="raise").dt.date
    result: dict[str, set[date]] = {}
    for security_id, session_day, accepted in zip(
        value["security_id"].astype(str), dates, verified, strict=True
    ):
        if accepted:
            result.setdefault(security_id, set()).add(session_day)
    return result


def _code_map(source: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for key, frame in source.items():
        code = _us_code(key)
        if code in result:
            raise USPaperDecisionError(f"duplicate TDX code after normalization: {code}")
        result[code] = frame
    return result


def _validate_frame(
    frame: pd.DataFrame,
    code: str,
    adjustment: str,
    day: date,
    expected: pd.DatetimeIndex,
    explained: set[date],
) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise USPaperDecisionError(f"{code} {adjustment} bars are empty")
    if not set(REQUIRED_BAR_COLUMNS).issubset(frame.columns):
        raise USPaperDecisionError(f"{code} {adjustment} lacks OHLCV")
    value = frame.loc[:, list(REQUIRED_BAR_COLUMNS)].copy()
    index = pd.DatetimeIndex(pd.to_datetime(value.index, errors="raise"))
    if index.tz is not None:
        index = index.tz_convert(NY_TZ).tz_localize(None)
    index = index.normalize()
    if index.duplicated().any():
        raise USPaperDecisionError(f"{code} {adjustment} has duplicate sessions")
    value.index = index
    value = value.sort_index()
    if value.index[-1].date() != day:
        raise USPaperDecisionError(f"{code} {adjustment} is stale at month-end")
    if (value.index > pd.Timestamp(day)).any():
        raise USPaperDecisionError(f"{code} {adjustment} contains future sessions")
    missing = [
        item.date() for item in expected.difference(value.index)
        if item.date() not in explained
    ]
    if missing:
        sample = ",".join(item.isoformat() for item in missing[:5])
        raise USPaperDecisionError(
            f"{code} {adjustment} missing unexplained sessions:{sample}"
        )
    numeric = value.apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any() or not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise USPaperDecisionError(f"{code} {adjustment} contains non-finite OHLCV")
    if (numeric[["Open", "High", "Low", "Close"]] <= 0).any().any():
        raise USPaperDecisionError(f"{code} {adjustment} contains non-positive prices")
    if (numeric["Volume"] < 0).any():
        raise USPaperDecisionError(f"{code} {adjustment} contains negative volume")
    if (
        (numeric["High"] < numeric[["Open", "Close"]].max(axis=1)).any()
        or (numeric["Low"] > numeric[["Open", "Close"]].min(axis=1)).any()
        or (numeric["Low"] > numeric["High"]).any()
    ):
        raise USPaperDecisionError(f"{code} {adjustment} has invalid OHLC relationships")
    return numeric


def _paper_signal(
    value: Any,
    *,
    release_id: str,
    manifest_sha256: str,
    security_id: str,
    generated_at: datetime,
    available_at: datetime,
    valid_until: datetime,
) -> dict[str, Any]:
    evidence = dict(getattr(value, "evidence", {}) or {})
    evidence.update(
        {
            "security_id": security_id,
            "pit_release_id": release_id,
            "manifest_sha256": manifest_sha256,
            "paper_signal_contract": "GENERATED_POST_CLOSE_APPROVABLE_0920_EXPIRES_0935",
        }
    )
    side = str(getattr(value, "side", "")).upper()
    code = _us_code(getattr(value, "code", ""))
    canonical = {
        "pit_release_id": release_id,
        "security_id": security_id,
        "code": code,
        "side": side,
        "target_weight": float(getattr(value, "target_weight", 0.0)),
        "reason_codes": tuple(getattr(value, "reason_codes", ()) or ()),
    }
    return {
        "signal_id": "uspds_" + _hash(canonical)[:24],
        "code": code,
        "side": side,
        "target_weight": canonical["target_weight"],
        "generated_at": generated_at,
        "available_at": available_at,
        "valid_until": valid_until,
        "reason_codes": canonical["reason_codes"],
        "evidence": evidence,
    }


def _forced_membership_exit(
    *,
    code: str,
    security_id: str,
    release_id: str,
    manifest_sha256: str,
    generated_at: datetime,
    available_at: datetime,
    valid_until: datetime,
) -> dict[str, Any]:
    canonical = {
        "pit_release_id": release_id,
        "security_id": security_id,
        "code": code,
        "side": "SELL",
        "reason": "US_PIT_MEMBERSHIP_REMOVAL",
    }
    return {
        "signal_id": "uspds_" + _hash(canonical)[:24],
        "code": code,
        "side": "SELL",
        "target_weight": 0.0,
        "generated_at": generated_at,
        "available_at": available_at,
        "valid_until": valid_until,
        "reason_codes": ("US_PIT_MEMBERSHIP_REMOVAL",),
        "evidence": {
            "security_id": security_id,
            "pit_release_id": release_id,
            "manifest_sha256": manifest_sha256,
            "membership_verified": True,
            "paper_signal_contract": "GENERATED_POST_CLOSE_APPROVABLE_0920_EXPIRES_0935",
        },
    }


def _security_name(frame: pd.DataFrame, security_id: str) -> str:
    if frame is None or frame.empty or "security_id" not in frame.columns:
        return security_id
    rows = frame[frame["security_id"].astype(str).eq(str(security_id))]
    if rows.empty:
        return security_id
    for column in ("name", "security_name", "issuer_name", "display_name"):
        if column in rows.columns:
            value = str(rows.iloc[0][column]).strip()
            if value and value.lower() != "nan":
                return value
    return security_id


class USPaperDecisionAuditStore:
    """Content-addressed, immutable month-end paper-decision bundles.

    A period signal hash alone cannot prove that the ranking can be replayed.
    This store freezes the complete normalized front/raw inputs, release and
    strategy bindings, parameters, and decision output before the coordinator
    reports success.  Existing objects are byte-compared, never overwritten.
    """

    FORMAT = "us-paper-decision-bundle-v2"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.periods = self.root / "periods"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.periods.mkdir(parents=True, exist_ok=True)

    def archive(
        self,
        *,
        decision_date: date,
        execution_date: date,
        release_id: str,
        manifest_sha256: str,
        strategy_version: str,
        strategy_parameters: Mapping[str, Any],
        strategy_code_sha256: str,
        decision_engine_code_sha256: str,
        front: Mapping[str, pd.DataFrame],
        raw: Mapping[str, pd.DataFrame],
        names: Mapping[str, str],
        positions: Sequence[Mapping[str, Any]],
        position_aliases: Mapping[str, str],
        security_id_by_code: Mapping[str, str],
        tradable_codes: Sequence[str],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        front_object = self._put_bars(front)
        raw_object = self._put_bars(raw)
        result_value = _json_safe(result)
        manifest = {
            "format": self.FORMAT,
            "decision_date": decision_date.isoformat(),
            "execution_date": execution_date.isoformat(),
            "release_id": _sha256_value(release_id, "release_id"),
            "manifest_sha256": _sha256_value(
                manifest_sha256, "manifest_sha256"
            ),
            "strategy_id": "us_momentum_v1",
            "strategy_version": str(strategy_version),
            "strategy_parameters": _json_safe(strategy_parameters),
            "strategy_code_sha256": _sha256_value(
                strategy_code_sha256, "strategy_code_sha256"
            ),
            "decision_engine_code_sha256": _sha256_value(
                decision_engine_code_sha256, "decision_engine_code_sha256"
            ),
            "front_object_sha256": front_object,
            "raw_object_sha256": raw_object,
            "front_sha256": _bars_hash(front),
            "raw_sha256": _bars_hash(raw),
            "names": dict(sorted((str(key), str(value)) for key, value in names.items())),
            "positions": _json_safe(list(positions)),
            "position_aliases": dict(
                sorted(
                    (str(security_id), str(code))
                    for security_id, code in position_aliases.items()
                )
            ),
            "security_id_by_code": dict(
                sorted(
                    (str(code), str(security_id))
                    for code, security_id in security_id_by_code.items()
                )
            ),
            "tradable_codes": sorted(str(item) for item in tradable_codes),
            "decision_output": result_value,
            "decision_output_sha256": _hash(result_value),
        }
        payload = _canonical_bytes(manifest)
        bundle_sha256 = hashlib.sha256(payload).hexdigest()
        target = self.objects / f"{bundle_sha256}.json"
        self._write_once(target, payload)
        pointer = self.periods / f"{decision_date.isoformat()}.json"
        pointer_payload = _canonical_bytes(
            {
                "format": self.FORMAT,
                "decision_date": decision_date.isoformat(),
                "bundle_sha256": bundle_sha256,
                "release_id": release_id,
                "manifest_sha256": manifest_sha256,
            }
        )
        self._write_once(pointer, pointer_payload)
        return {
            "format": self.FORMAT,
            "bundle_sha256": bundle_sha256,
            "front_object_sha256": front_object,
            "raw_object_sha256": raw_object,
            "decision_output_sha256": manifest["decision_output_sha256"],
        }

    def load(self, decision_date: date | str) -> dict[str, Any]:
        day = _day(decision_date)
        pointer = self.periods / f"{day.isoformat()}.json"
        value = json.loads(pointer.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("format") != self.FORMAT:
            raise USPaperDecisionError("paper decision archive pointer format mismatch")
        if str(value.get("decision_date")) != day.isoformat():
            raise USPaperDecisionError("paper decision archive pointer date mismatch")
        digest = _sha256_value(value.get("bundle_sha256"), "bundle_sha256")
        target = self.objects / f"{digest}.json"
        payload = target.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise USPaperDecisionError("paper decision archive hash mismatch")
        manifest = json.loads(payload)
        if not isinstance(manifest, Mapping) or manifest.get("format") != self.FORMAT:
            raise USPaperDecisionError("paper decision archive format mismatch")
        if str(manifest.get("decision_date")) != day.isoformat():
            raise USPaperDecisionError("paper decision archive date mismatch")
        for name in (
            "release_id",
            "manifest_sha256",
            "strategy_code_sha256",
            "decision_engine_code_sha256",
            "decision_output_sha256",
        ):
            _sha256_value(manifest.get(name), name)
        if str(value.get("release_id")) != str(manifest.get("release_id")):
            raise USPaperDecisionError("paper decision archive pointer release mismatch")
        if str(value.get("manifest_sha256")) != str(
            manifest.get("manifest_sha256")
        ):
            raise USPaperDecisionError("paper decision archive pointer manifest mismatch")
        if str(manifest.get("decision_output_sha256")) != _hash(
            manifest.get("decision_output")
        ):
            raise USPaperDecisionError("paper decision output hash mismatch")
        for name in ("front_object_sha256", "raw_object_sha256"):
            object_digest = _sha256_value(manifest.get(name), name)
            object_path = self.objects / f"{object_digest}.json"
            object_payload = object_path.read_bytes()
            if hashlib.sha256(object_payload).hexdigest() != object_digest:
                raise USPaperDecisionError(
                    f"paper decision archive object is corrupt: {name}"
                )
        return manifest

    def _put_bars(self, bars: Mapping[str, pd.DataFrame]) -> str:
        value: dict[str, Any] = {}
        for code in sorted(bars):
            frame = bars[code].sort_index(kind="stable")
            value[code] = {
                "index": [pd.Timestamp(item).isoformat() for item in frame.index],
                "columns": [str(item) for item in frame.columns],
                "data": [
                    [_json_safe(item) for item in row]
                    for row in frame.itertuples(index=False, name=None)
                ],
            }
        payload = _canonical_bytes(value)
        digest = hashlib.sha256(payload).hexdigest()
        self._write_once(self.objects / f"{digest}.json", payload)
        return digest

    @staticmethod
    def _write_once(path: Path, payload: bytes) -> None:
        if path.exists():
            if path.read_bytes() != payload:
                raise USPaperDecisionError(
                    f"immutable paper decision archive conflict: {path.name}"
                )
            return
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
        try:
            path.chmod(0o444)
        except OSError:
            pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _source_code_sha256(value: Any) -> str:
    """Hash the exact Python source file that owns a paper decision component."""

    try:
        source = inspect.getsourcefile(value) or inspect.getfile(value)
    except (TypeError, OSError) as exc:
        raise USPaperDecisionError(
            f"cannot locate paper decision source for {value!r}"
        ) from exc
    path = Path(str(source)).resolve()
    if not path.is_file():
        raise USPaperDecisionError(
            f"paper decision source file does not exist: {path}"
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bars_hash(bars: Mapping[str, pd.DataFrame]) -> str:
    digest = hashlib.sha256()
    for code in sorted(bars):
        frame = bars[code].sort_index()
        digest.update(code.encode("utf-8"))
        digest.update("|".join(map(str, frame.columns)).encode("utf-8"))
        digest.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
    return digest.hexdigest()


def _is_retry_slot(current: datetime, earliest: datetime, minutes: int) -> bool:
    delta_minutes = int((current.replace(second=0, microsecond=0) - earliest.replace(
        second=0, microsecond=0
    )).total_seconds() // 60)
    return delta_minutes >= 0 and delta_minutes % minutes == 0


def _next_retry(current: datetime, earliest: datetime, minutes: int) -> datetime:
    anchor = earliest.replace(second=0, microsecond=0)
    probe = current.replace(second=0, microsecond=0)
    elapsed = max(0, int((probe - anchor).total_seconds() // 60))
    slots = elapsed // minutes + 1
    return anchor + timedelta(minutes=slots * minutes)


def _day(value: date | str | pd.Timestamp) -> date:
    if isinstance(value, datetime):
        return _aware(value).date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid decision date: {value!r}") from exc


def _aware(value: Any) -> datetime:
    if isinstance(value, pd.Timestamp):
        result = value.to_pydatetime()
    elif isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid timestamp: {value!r}") from exc
    if result.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return result.astimezone(NY_TZ)


def _us_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if not code.endswith(".US") or len(code) <= 3 or any(char.isspace() for char in code):
        raise ValueError(f"invalid US code: {value!r}")
    return code


def _stable_security_id(value: Any) -> str:
    security_id = str(value or "").strip()
    lowered = security_id.lower()
    if (
        security_id != lowered
        or not lowered.startswith("us_")
        or lowered.endswith(".us")
        or len(lowered) <= 3
        or any(char.isspace() for char in lowered)
    ):
        raise USPaperDecisionError(
            "persisted security_id is not a lowercase stable US identifier"
        )
    return lowered


def _positive(value: Any, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise USPaperDecisionError(f"{name} must be positive")
    return result


def _finite_nonnegative(value: Any, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise USPaperDecisionError(f"{name} must be finite and non-negative")
    return result


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "verified"}


def _sha256_value(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if len(text) != 64 or text != text.lower() or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")
    return text


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


__all__ = [
    "TDXCurrentUSBarSource",
    "USPaperBarBundle",
    "USPaperDecisionConfig",
    "USPaperDecisionCoordinator",
    "USPaperDecisionAuditStore",
    "USPaperDecisionError",
    "USPaperDecisionSource",
]
