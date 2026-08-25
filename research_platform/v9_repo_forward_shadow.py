from __future__ import annotations

import hashlib
import json
import math
import socket
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Iterable, Mapping

import pandas as pd

from strategy_v1.config import StrategyConfig
from strategy_v1.market import filter_universe
from strategy_v1.portfolio import price_limit_ratio

from .config import PlatformConfig, PortfolioConfig
from .course49_market import normalize_market_activity
from .data import TdxProvider
from .lhb import LhbFeatures, normalize_lhb_history
from .models import PlatformSignal, StrategyScanResult
from .portfolio import can_trade_at_open
from .reverse_repo_sweep_research import (
    BASE_COMMISSION_RATE,
    PRINCIPAL_LOT,
    STRESS_COMMISSION_RATE,
)
from .storage import Database
from .strategies.course49_v9 import Course49V9Strategy


OBSERVER_ID = "course49_v9_cross_market_repo_shadow"
OBSERVER_VERSION = "1.0.0"
STRATEGY_ID = "course49_v9"
STRATEGY_VERSION = "9.0.0"
FORWARD_BOUNDARY_EXCLUSIVE = "2026-08-07"
MINIMUM_FRESH_SESSIONS = 60
INITIAL_CASH = 50_000.0
REPO_CODES = ("131810.SZ", "204001.SH")
SHADOW_PORTFOLIO_CONFIG = PortfolioConfig(
    initial_cash=100_000.0,
    strategy_budget_weight=0.50,
    max_strategy_positions=3,
    max_total_positions=5,
    max_strategy_symbol_weight=0.40,
    max_total_symbol_weight=0.20,
    fixed_stop_loss=0.05,
    commission_rate=0.0003,
    min_commission=5.0,
    stamp_duty_rate=0.0005,
    slippage_rate=0.001,
    board_lot=100,
)


def protocol_manifest() -> dict[str, Any]:
    """Return the immutable, observation-only forward protocol."""

    return {
        "observer_id": OBSERVER_ID,
        "observer_version": OBSERVER_VERSION,
        "research_status": "OBSERVATION_ONLY",
        "strategy": {
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "lifecycle": "HISTORICAL_REJECTED",
            "rule_changes": "none",
            "initial_virtual_cash_cny": INITIAL_CASH,
        },
        "forward_boundary": {
            "exclusive": FORWARD_BOUNDARY_EXCLUSIVE,
            "no_retrospective_backfill": True,
            "minimum_fresh_sessions": MINIMUM_FRESH_SESSIONS,
        },
        "virtual_equity_execution": {
            "signal_time": "current session after close",
            "fill_time": "next session open",
            "raw_open_for_execution": True,
            "front_adjusted_bars_for_analysis": True,
            "portfolio_costs": asdict(SHADOW_PORTFOLIO_CONFIG),
            "stock_orders_have_priority": True,
        },
        "cash_overlay": {
            "instruments": [
                {"code": "131810.SZ", "name": "R-001", "tenor_days": 1},
                {"code": "204001.SH", "name": "GC001", "tenor_days": 1},
            ],
            "selection": "highest eligible rate; ties use code ascending",
            "base": {
                "rate_field": "Close",
                "commission_rate": BASE_COMMISSION_RATE,
            },
            "stress": {
                "rate_field": "Low",
                "commission_rate": STRESS_COMMISSION_RATE,
            },
            "principal_lot_cny": PRINCIPAL_LOT,
            "actual_occupied_days": (
                "calendar days from the next equity session inclusive to the "
                "following equity session exclusive"
            ),
            "principal_assumption": "available before every next equity open",
            "negative_net_quote": "skip",
        },
        "isolation": {
            "append_only_shadow_events": True,
            "write_platform_signals": False,
            "write_paper_orders": False,
            "write_paper_positions": False,
            "unfreeze_archived_account": False,
            "push_tdx": False,
            "real_orders": False,
        },
        "promotion_gate": {
            "automatic_promotion": False,
            "paper_simulation_authorized": False,
            "required_external_evidence": [
                "account-specific reverse-repo commission schedule",
                "broker confirmation that principal is available before the next stock open",
                "near-close executable quote audit",
            ],
            "historical_result": "REJECT",
            "interpretation": (
                "Fresh observations may be collected, but they are not a qualified paper "
                "validation and cannot override the rejected V9 lifecycle."
            ),
        },
    }


def protocol_hash() -> str:
    payload = json.dumps(
        protocol_manifest(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class V9RepoForwardShadowService:
    """Run a forward-only V9/repo shadow book without touching paper trading."""

    def __init__(
        self,
        config: PlatformConfig,
        database: Database,
        strategy: Any | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.strategy = strategy or Course49V9Strategy()
        self._ensure_protocol()

    def capture_live(
        self,
        *,
        refresh_sectors: bool = False,
        refresh_data: bool = False,
        tdx_probe_timeout_seconds: float = 0.0,
        tdx_run_timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Capture one session in a disposable process that cannot strand the CLI."""

        try:
            with socket.create_connection(("127.0.0.1", 17709), timeout=1.0):
                pass
        except OSError:
            return {
                "status": "BLOCKED_TDX_UNAVAILABLE",
                "reason": "The local TongdaXin HTTP service at 127.0.0.1:17709 is unavailable",
                "paper_simulation_ready": False,
                "shadow_status": self.status(),
            }
        if _tdx_channel_busy(self.config):
            return {
                "status": "BLOCKED_TDX_CHANNEL_BUSY",
                "reason": "Another local process currently owns the single TDX data channel",
                "paper_simulation_ready": False,
                "shadow_status": self.status(),
            }
        if tdx_probe_timeout_seconds > 0.0:
            probe = _probe_tdx(self.config, tdx_probe_timeout_seconds)
            if not probe["ready"]:
                return {
                    "status": "BLOCKED_TDX_PROBE",
                    "reason": probe["reason"],
                    "probe_timeout_seconds": tdx_probe_timeout_seconds,
                    "paper_simulation_ready": False,
                    "shadow_status": self.status(),
                }
            probe_asof = str(probe.get("data_asof") or "")
            current_status = self.status()
            if probe_asof and pd.Timestamp(probe_asof) <= pd.Timestamp(
                FORWARD_BOUNDARY_EXCLUSIVE
            ):
                return {
                    "status": "WAITING_FOR_FRESH_SESSION",
                    "data_asof": probe_asof,
                    "required_after": FORWARD_BOUNDARY_EXCLUSIVE,
                    "paper_simulation_ready": False,
                    "shadow_status": current_status,
                }
            if (
                probe_asof
                and current_status.get("latest_session")
                and pd.Timestamp(probe_asof)
                <= pd.Timestamp(str(current_status["latest_session"]))
            ):
                return {
                    "status": "ALREADY_CAPTURED",
                    "session_date": probe_asof,
                    "paper_simulation_ready": False,
                    "shadow_status": current_status,
                }
        return _run_tdx_capture(
            self.config,
            refresh_sectors=refresh_sectors,
            refresh_data=refresh_data,
            timeout_seconds=tdx_run_timeout_seconds,
        )

    def _capture_live_direct(
        self,
        *,
        refresh_sectors: bool = False,
        refresh_data: bool = False,
    ) -> dict[str, Any]:
        with TdxProvider(
            self.config,
            __file__,
            cache_reads=not refresh_data,
        ) as provider:
            index_map = provider.fetch_bars(
                ["999999.SH"],
                "1d",
                180,
                fields=("Open", "High", "Low", "Close", "Volume"),
                dividend_type="front",
            )
            index_bars = index_map.get("999999.SH")
            if index_bars is None or index_bars.empty:
                fallback = provider.fetch_bars(
                    ["000001.SH"],
                    "1d",
                    180,
                    fields=("Open", "High", "Low", "Close", "Volume"),
                    dividend_type="front",
                )
                index_bars = fallback.get("000001.SH")
            if index_bars is None or index_bars.empty:
                raise ValueError("No Shanghai market calendar is available")
            asof = _latest_day(index_bars)
            preflight = self._preflight(asof, index_bars)
            if preflight is not None:
                return preflight

            codes, names = provider.list_a_shares()
            front = provider.fetch_bars(
                codes,
                "1d",
                180,
                fields=("Open", "High", "Low", "Close", "Volume", "Amount"),
                dividend_type="front",
            )
            raw = provider.fetch_bars(
                codes,
                "1d",
                180,
                fields=("Open", "High", "Low", "Close", "Volume", "Amount"),
                dividend_type="none",
            )
            eligible_front = filter_universe(
                front,
                names,
                StrategyConfig(tdx_root=self.config.tdx_root, daily_lookback=180),
            )
            eligible_raw = {
                code: raw[code]
                for code in eligible_front
                if code in raw and not raw[code].empty
            }
            if not eligible_raw:
                raise ValueError("No eligible A-share bars are available")
            latest_coverage = sum(
                _latest_day(frame) == asof for frame in eligible_raw.values()
            ) / len(eligible_raw)
            if latest_coverage < 0.90:
                raise ValueError(
                    f"Latest A-share coverage is incomplete: {latest_coverage:.2%}"
                )

            sectors = provider.load_sectors(
                refresh=refresh_sectors or refresh_data
            )
            benchmark_codes = (
                "000300.CSI",
                "000300.SH",
                "000852.CSI",
                "000852.SH",
                "399006.SZ",
            )
            benchmarks = provider.fetch_bars(
                benchmark_codes,
                "1d",
                180,
                fields=("Open", "High", "Low", "Close", "Volume"),
                dividend_type="front",
            )
            visible_raw = _slice_bars(eligible_raw, asof)
            limit_codes = _latest_limit_codes(visible_raw, names)
            limit_snapshot = (
                provider.fetch_limit_snapshot(limit_codes) if limit_codes else {}
            )
            event_start = (asof - pd.Timedelta(days=30)).strftime("%Y%m%d")
            event_end = asof.strftime("%Y%m%d")
            event_payload = (
                provider.fetch_course49_history(
                    limit_codes,
                    event_start,
                    event_end,
                )
                if limit_codes
                else {}
            )
            lhb_history = normalize_lhb_history(event_payload, visible_raw)
            activity_start = (asof - pd.Timedelta(days=180)).strftime("%Y%m%d")
            market_activity = normalize_market_activity(
                provider.fetch_market_activity(activity_start, event_end)
            )
            repo_bars = provider.fetch_bars(
                REPO_CODES,
                "1d",
                10,
                fields=("Open", "High", "Low", "Close", "Volume"),
                dividend_type="none",
            )

        return self.capture_session(
            asof=asof,
            front_bars=eligible_front,
            raw_bars=eligible_raw,
            names=names,
            sector_members=sectors,
            benchmark_bars=benchmarks,
            index_bars=index_bars,
            repo_bars=repo_bars,
            limit_snapshot=limit_snapshot,
            lhb_history=lhb_history,
            market_activity=market_activity,
        )

    def capture_session(
        self,
        *,
        asof: Any,
        front_bars: Mapping[str, pd.DataFrame],
        raw_bars: Mapping[str, pd.DataFrame],
        names: Mapping[str, str],
        sector_members: Mapping[str, dict[str, Any]],
        benchmark_bars: Mapping[str, pd.DataFrame],
        index_bars: pd.DataFrame,
        repo_bars: Mapping[str, pd.DataFrame],
        limit_snapshot: Mapping[str, dict[str, Any]] | None = None,
        lhb_history: Mapping[str, Mapping[str, LhbFeatures]] | None = None,
        market_activity: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        """Append one immutable session capture from an explicit point-in-time context."""

        session = _day(asof)
        preflight = self._preflight(session, index_bars)
        if preflight is not None:
            return preflight

        visible_front = _slice_bars(front_bars, session)
        visible_raw = _slice_bars(raw_bars, session)
        visible_benchmarks = _slice_bars(benchmark_bars, session)
        visible_repo = _slice_bars(repo_bars, session)
        visible_activity = _slice_frame(market_activity, session)
        if not visible_raw:
            raise ValueError("No point-in-time raw bars are available")
        if any(_latest_day(frame) > session for frame in visible_raw.values()):
            raise AssertionError("Future raw bars reached the shadow strategy")

        events = self._events()
        prior_payload = events[-1]["payload"] if events else None
        state = _initial_state() if prior_payload is None else _copy_state(prior_payload["state"])
        settlements = self._settle_repo(events, session)
        for scenario in ("base", "stress"):
            state["settled_repo_pnl"][scenario] += float(
                settlements[scenario].get("net_interest", 0.0)
            )

        stock_cash, positions, pending, fills = _fill_virtual_intents(
            state["stock_cash"],
            state["positions"],
            state["virtual_stock_intents"],
            session,
            visible_raw,
            names,
            SHADOW_PORTFOLIO_CONFIG,
        )
        for code, position in positions.items():
            frame = visible_raw.get(code)
            if frame is None or frame.empty:
                continue
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
            if not close.empty:
                position["last_price"] = float(close.iloc[-1])

        position_rows = [
            {
                "code": item["code"],
                "stop_price": item["stop_price"],
                "entry_time": item["entry_date"],
                "average_price": item["average_price"],
                "evidence": json.dumps(
                    item.get("evidence") or {}, ensure_ascii=False
                ),
            }
            for item in positions.values()
        ]
        result: StrategyScanResult = self.strategy.scan(
            run_id=f"{OBSERVER_ID}:{session.date().isoformat()}",
            front_bars=visible_front,
            raw_bars=visible_raw,
            names=dict(names),
            sector_members=dict(sector_members),
            positions=position_rows,
            benchmark_bars=visible_benchmarks,
            runtime_state=dict(state.get("runtime_state") or {}),
            limit_snapshot=dict(limit_snapshot or {}),
            lhb_history={
                str(code): dict(history)
                for code, history in dict(lhb_history or {}).items()
            },
            market_activity=visible_activity,
            asof=session,
            eligible_codes=set(visible_front),
        )
        virtual_signals = [_signal_payload(signal) for signal in result.signals]
        pending = _merge_virtual_intents(pending, virtual_signals)

        stock_equity = stock_cash + sum(
            int(item["quantity"]) * float(item["last_price"])
            for item in positions.values()
        )
        repo = {
            "base": {
                "intent": _repo_intent(
                    session,
                    visible_repo,
                    stock_cash + state["settled_repo_pnl"]["base"],
                    rate_field="Close",
                    commission_rate=BASE_COMMISSION_RATE,
                ),
                "settlement": settlements["base"],
            },
            "stress": {
                "intent": _repo_intent(
                    session,
                    visible_repo,
                    stock_cash + state["settled_repo_pnl"]["stress"],
                    rate_field="Low",
                    commission_rate=STRESS_COMMISSION_RATE,
                ),
                "settlement": settlements["stress"],
            },
        }
        state.update(
            {
                "stock_cash": stock_cash,
                "positions": sorted(positions.values(), key=lambda item: item["code"]),
                "virtual_stock_intents": pending,
                "runtime_state": dict(result.state.get("runtime_state") or {}),
            }
        )
        data_hash = _context_hash(
            session,
            visible_front,
            visible_raw,
            visible_benchmarks,
            visible_repo,
            sector_members,
            limit_snapshot or {},
            lhb_history or {},
            visible_activity,
        )
        payload = {
            "observer_id": OBSERVER_ID,
            "observer_version": OBSERVER_VERSION,
            "session_date": session.date().isoformat(),
            "data_boundary": session.date().isoformat(),
            "data_hash": data_hash,
            "state": state,
            "virtual_fills": fills,
            "virtual_signals": virtual_signals,
            "repo": repo,
            "equity": {
                "stock_only": stock_equity,
                "base_with_settled_repo": (
                    stock_equity + state["settled_repo_pnl"]["base"]
                ),
                "stress_with_settled_repo": (
                    stock_equity + state["settled_repo_pnl"]["stress"]
                ),
            },
            "scan_state": {
                key: value
                for key, value in dict(result.state).items()
                if key not in {"runtime_state", "strong_sectors"}
            },
            "candidate_count": len(result.candidates),
            "candidates": [dict(item) for item in result.candidates[:10]],
            "isolation": {
                "platform_signal_rows_written": 0,
                "paper_order_rows_written": 0,
                "paper_position_rows_written": 0,
                "tdx_pushes": 0,
                "real_orders": 0,
            },
        }
        inserted = self._append_event(session, data_hash, payload)
        if not inserted:
            return self._already_captured(session)
        return {
            "status": "CAPTURED_OBSERVATION_ONLY",
            "session_date": session.date().isoformat(),
            "data_hash": data_hash,
            "virtual_fills": len(fills),
            "virtual_signals": len(virtual_signals),
            "positions": len(positions),
            "stock_equity": stock_equity,
            "paper_simulation_ready": False,
            "shadow_status": self.status(),
        }

    def status(self) -> dict[str, Any]:
        events = self._events()
        sessions = len(events)
        latest = events[-1]["payload"] if events else None
        session_gate = sessions >= MINIMUM_FRESH_SESSIONS
        gate_status = (
            "BLOCKED_EXTERNAL_EVIDENCE" if session_gate else "COLLECTING"
        )
        return {
            "observer_id": OBSERVER_ID,
            "observer_version": OBSERVER_VERSION,
            "protocol_hash": protocol_hash(),
            "status": gate_status,
            "observation_only": True,
            "strategy_lifecycle": "HISTORICAL_REJECTED",
            "historical_overlay_decision": "REJECT",
            "fresh_session_boundary_exclusive": FORWARD_BOUNDARY_EXCLUSIVE,
            "fresh_sessions": sessions,
            "minimum_fresh_sessions": MINIMUM_FRESH_SESSIONS,
            "session_gate_passed": session_gate,
            "latest_session": latest.get("session_date") if latest else None,
            "latest_equity": latest.get("equity") if latest else None,
            "external_evidence": {
                "account_fee_schedule_verified": False,
                "next_open_principal_availability_verified": False,
                "near_close_executable_quotes_verified": False,
            },
            "paper_simulation_ready": False,
            "automatic_promotion": False,
            "writes": {
                "shadow_event_ledger": True,
                "platform_signals": False,
                "paper_orders": False,
                "paper_positions": False,
                "tdx_push": False,
                "real_orders": False,
            },
        }

    def _preflight(
        self,
        asof: pd.Timestamp,
        index_bars: pd.DataFrame,
    ) -> dict[str, Any] | None:
        session = _day(asof)
        boundary = pd.Timestamp(FORWARD_BOUNDARY_EXCLUSIVE)
        if session <= boundary:
            return {
                "status": "WAITING_FOR_FRESH_SESSION",
                "data_asof": session.date().isoformat(),
                "required_after": FORWARD_BOUNDARY_EXCLUSIVE,
                "paper_simulation_ready": False,
                "shadow_status": self.status(),
            }
        events = self._events()
        if not events:
            return None
        latest = pd.Timestamp(events[-1]["session_date"])
        if session <= latest:
            return self._already_captured(session)
        sessions = _session_days(index_bars)
        latest_positions = sessions.get_indexer([latest])
        current_positions = sessions.get_indexer([session])
        if latest_positions[0] < 0 or current_positions[0] < 0:
            return {
                "status": "BLOCKED_SESSION_CALENDAR",
                "previous_session": latest.date().isoformat(),
                "data_asof": session.date().isoformat(),
                "paper_simulation_ready": False,
            }
        missing = int(current_positions[0] - latest_positions[0] - 1)
        if missing > 0:
            return {
                "status": "BLOCKED_FORWARD_GAP",
                "previous_session": latest.date().isoformat(),
                "data_asof": session.date().isoformat(),
                "missing_sessions": missing,
                "reason": "Retrospective backfill is forbidden by the frozen protocol",
                "paper_simulation_ready": False,
            }
        return None

    def _ensure_protocol(self) -> None:
        manifest = protocol_manifest()
        payload = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.database.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO v9_repo_shadow_protocols
                (protocol_hash, observer_version, strategy_id, strategy_version,
                 manifest_json, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    protocol_hash(),
                    OBSERVER_VERSION,
                    STRATEGY_ID,
                    STRATEGY_VERSION,
                    payload,
                    datetime.now().astimezone().isoformat(),
                ),
            )
            stored = connection.execute(
                """SELECT manifest_json FROM v9_repo_shadow_protocols
                WHERE protocol_hash=?""",
                (protocol_hash(),),
            ).fetchone()
        if stored is None or str(stored["manifest_json"]) != payload:
            raise ValueError("Stored shadow protocol differs from the current manifest")

    def _append_event(
        self,
        session: pd.Timestamp,
        data_hash: str,
        payload: dict[str, Any],
    ) -> bool:
        identity = "|".join(
            (protocol_hash(), "SESSION_CAPTURE", session.date().isoformat())
        )
        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO v9_repo_shadow_events
                (event_id, protocol_hash, session_date, event_type, data_hash,
                 payload_json, recorded_at) VALUES (?, ?, ?, 'SESSION_CAPTURE', ?, ?, ?)""",
                (
                    event_id,
                    protocol_hash(),
                    session.date().isoformat(),
                    data_hash,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=_json_default,
                    ),
                    datetime.now().astimezone().isoformat(),
                ),
            )
            return int(cursor.rowcount) > 0

    def _events(self) -> list[dict[str, Any]]:
        rows = self.database.query(
            """SELECT * FROM v9_repo_shadow_events
            WHERE protocol_hash=? AND event_type='SESSION_CAPTURE'
            ORDER BY session_date, event_id""",
            (protocol_hash(),),
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            events.append({**row, "payload": payload})
        return events

    def _settle_repo(
        self,
        events: list[dict[str, Any]],
        session: pd.Timestamp,
    ) -> dict[str, dict[str, Any]]:
        result = {
            "base": {"status": "NOT_DUE", "net_interest": 0.0},
            "stress": {"status": "NOT_DUE", "net_interest": 0.0},
        }
        if len(events) < 2:
            return result
        origin = events[-2]
        next_session = pd.Timestamp(events[-1]["session_date"])
        occupied_days = int((session - next_session).days)
        if occupied_days <= 0:
            raise ValueError("Reverse-repo occupied days must be positive")
        for scenario in ("base", "stress"):
            intent = (
                origin["payload"].get("repo", {})
                .get(scenario, {})
                .get("intent", {})
            )
            if not bool(intent.get("executed")):
                result[scenario] = {
                    "status": "SKIPPED_AT_ORIGIN",
                    "origin_session": origin["session_date"],
                    "actual_occupied_days": occupied_days,
                    "net_interest": 0.0,
                }
                continue
            principal = float(intent["principal"])
            quoted_rate = float(intent["quoted_rate_percent"])
            commission = float(intent["commission"])
            gross = principal * quoted_rate / 100.0 * occupied_days / 365.0
            net = gross - commission
            result[scenario] = {
                "status": "SETTLED",
                "origin_session": origin["session_date"],
                "actual_occupied_days": occupied_days,
                "principal": principal,
                "quoted_rate_percent": quoted_rate,
                "gross_interest": gross,
                "commission": commission,
                "net_interest": net,
            }
        return result

    def _already_captured(self, session: pd.Timestamp) -> dict[str, Any]:
        return {
            "status": "ALREADY_CAPTURED",
            "session_date": session.date().isoformat(),
            "paper_simulation_ready": False,
            "shadow_status": self.status(),
        }


def _initial_state() -> dict[str, Any]:
    return {
        "stock_cash": INITIAL_CASH,
        "positions": [],
        "virtual_stock_intents": [],
        "runtime_state": {},
        "settled_repo_pnl": {"base": 0.0, "stress": 0.0},
    }


def _copy_state(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False))


def _fill_virtual_intents(
    cash: float,
    position_rows: Iterable[Mapping[str, Any]],
    intents: Iterable[Mapping[str, Any]],
    session: pd.Timestamp,
    raw_bars: Mapping[str, pd.DataFrame],
    names: Mapping[str, str],
    config: PortfolioConfig,
) -> tuple[float, dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    positions = {
        str(item["code"]): dict(item) for item in position_rows
    }
    remaining: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    ordered = sorted(
        (dict(item) for item in intents),
        key=lambda item: (
            str(item.get("side")) != "SELL",
            -float(item.get("strength", 0.0)),
            str(item.get("code", "")),
        ),
    )
    for intent in ordered:
        code = str(intent.get("code", ""))
        side = str(intent.get("side", ""))
        frame = raw_bars.get(code)
        if frame is None or frame.empty:
            if side == "SELL" and code in positions:
                remaining.append(intent)
            continue
        days = _frame_days(frame)
        row = frame.loc[days == session]
        prior = frame.loc[days < session]
        if row.empty or prior.empty:
            if side == "SELL" and code in positions:
                remaining.append(intent)
            continue
        open_price = float(pd.to_numeric(row["Open"], errors="coerce").iloc[-1])
        previous_close = float(
            pd.to_numeric(prior["Close"], errors="coerce").dropna().iloc[-1]
        )
        if not can_trade_at_open(
            code,
            side,
            open_price,
            previous_close,
            str(names.get(code, "")),
        ):
            if side == "SELL" and code in positions:
                remaining.append(intent)
            continue
        if side == "BUY":
            if code in positions or len(positions) >= config.max_strategy_positions:
                continue
            open_gap = open_price / previous_close - 1.0
            evidence = dict(intent.get("evidence") or {})
            lower = evidence.get("entry_gap_min")
            upper = evidence.get("entry_gap_max")
            if (lower is not None and open_gap < float(lower)) or (
                upper is not None and open_gap > float(upper)
            ):
                continue
            execution = open_price * (1.0 + config.slippage_rate)
            strategy_equity = cash + sum(
                int(item["quantity"]) * float(item["last_price"])
                for item in positions.values()
            )
            budget = min(
                cash,
                strategy_equity
                * min(
                    float(intent.get("target_weight", 0.0)),
                    config.max_strategy_symbol_weight,
                ),
            )
            quantity = int(budget / execution / config.board_lot) * config.board_lot
            while quantity > 0:
                value = quantity * execution
                fee = max(config.min_commission, value * config.commission_rate)
                if value + fee <= cash:
                    break
                quantity -= config.board_lot
            if quantity <= 0:
                continue
            value = quantity * execution
            fee = max(config.min_commission, value * config.commission_rate)
            cash -= value + fee
            positions[code] = {
                "code": code,
                "quantity": quantity,
                "average_price": execution,
                "entry_date": session.date().isoformat(),
                "stop_price": float(
                    intent.get("stop_price") or execution * (1.0 - 0.03)
                ),
                "last_price": execution,
                "evidence": evidence,
                "entry_fees": fee,
            }
            fills.append(
                {
                    "signal_id": intent.get("signal_id"),
                    "side": side,
                    "code": code,
                    "session_date": session.date().isoformat(),
                    "quantity": quantity,
                    "price": execution,
                    "fees": fee,
                    "realized_pnl": None,
                }
            )
        elif side == "SELL":
            position = positions.get(code)
            if position is None:
                continue
            if session.date() <= date.fromisoformat(str(position["entry_date"])):
                remaining.append(intent)
                continue
            execution = open_price * (1.0 - config.slippage_rate)
            quantity = int(position["quantity"])
            value = quantity * execution
            fee = (
                max(config.min_commission, value * config.commission_rate)
                + value * config.stamp_duty_rate
            )
            pnl = (
                (execution - float(position["average_price"])) * quantity
                - float(position.get("entry_fees", 0.0))
                - fee
            )
            cash += value - fee
            del positions[code]
            fills.append(
                {
                    "signal_id": intent.get("signal_id"),
                    "side": side,
                    "code": code,
                    "session_date": session.date().isoformat(),
                    "quantity": quantity,
                    "price": execution,
                    "fees": fee,
                    "realized_pnl": pnl,
                }
            )
    return cash, positions, remaining, fills


def _merge_virtual_intents(
    existing: Iterable[Mapping[str, Any]],
    generated: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result = [dict(item) for item in existing]
    for intent in generated:
        item = dict(intent)
        result = [
            current
            for current in result
            if not (
                current.get("code") == item.get("code")
                and current.get("side") == item.get("side")
            )
        ]
        result.append(item)
    return sorted(
        result,
        key=lambda item: (
            str(item.get("side")) != "SELL",
            -float(item.get("strength", 0.0)),
            str(item.get("code", "")),
        ),
    )


def _signal_payload(signal: PlatformSignal) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "generated_at": signal.generated_at.isoformat(),
        "available_at": signal.available_at.isoformat(),
        "valid_until": signal.valid_until.isoformat(),
        "code": signal.code,
        "side": signal.side,
        "strength": float(signal.strength),
        "target_weight": float(signal.target_weight),
        "stop_price": signal.stop_price,
        "status": signal.status.value,
        "reason_codes": list(signal.reason_codes),
        "evidence": dict(signal.evidence),
        "shadow_only": True,
    }


def _repo_intent(
    session: pd.Timestamp,
    repo_bars: Mapping[str, pd.DataFrame],
    available_cash: float,
    *,
    rate_field: str,
    commission_rate: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for code in REPO_CODES:
        frame = repo_bars.get(code)
        if frame is None or frame.empty:
            missing.append(code)
            continue
        days = _frame_days(frame)
        current = frame.loc[days == session]
        if current.empty:
            missing.append(code)
            continue
        row = current.iloc[-1]
        rows.append(
            {
                "code": code,
                "rate": float(row[rate_field]),
                "volume": float(row.get("Volume", 0.0) or 0.0),
            }
        )
    if missing:
        raise ValueError(
            "Missing same-session reverse-repo quotes: " + ", ".join(missing)
        )
    eligible = [item for item in rows if item["volume"] > 0.0]
    if not eligible:
        raise ValueError("No liquid same-session reverse-repo quote is available")
    selected = sorted(eligible, key=lambda item: (-item["rate"], item["code"]))[0]
    principal = (
        max(0.0, math.floor((available_cash + 1e-9) / PRINCIPAL_LOT))
        * PRINCIPAL_LOT
    )
    gross_one_day = principal * selected["rate"] / 100.0 / 365.0
    commission = principal * commission_rate
    executed = principal > 0.0 and gross_one_day > commission
    return {
        "session_date": session.date().isoformat(),
        "rate_field": rate_field,
        "commission_rate": commission_rate,
        "code": selected["code"],
        "quoted_rate_percent": selected["rate"],
        "available_cash": available_cash,
        "principal": principal,
        "commission": commission if executed else 0.0,
        "minimum_one_day_gross_interest": gross_one_day if executed else 0.0,
        "executed": executed,
        "skip_reason": "" if executed else "NON_POSITIVE_MINIMUM_NET_INTEREST",
        "virtual_only": True,
    }


def _context_hash(
    session: pd.Timestamp,
    front_bars: Mapping[str, pd.DataFrame],
    raw_bars: Mapping[str, pd.DataFrame],
    benchmark_bars: Mapping[str, pd.DataFrame],
    repo_bars: Mapping[str, pd.DataFrame],
    sector_members: Mapping[str, Any],
    limit_snapshot: Mapping[str, Any],
    lhb_history: Mapping[str, Any],
    market_activity: pd.DataFrame,
) -> str:
    digest = hashlib.sha256(session.date().isoformat().encode("utf-8"))
    for label, bars in (
        ("front", front_bars),
        ("raw", raw_bars),
        ("benchmark", benchmark_bars),
        ("repo", repo_bars),
    ):
        digest.update(label.encode("utf-8"))
        for code in sorted(bars):
            frame = bars[code].sort_index()
            digest.update(code.encode("utf-8"))
            digest.update(
                pd.util.hash_pandas_object(frame, index=True)
                .to_numpy()
                .tobytes()
            )
    digest.update(_canonical_bytes(sector_members))
    digest.update(_canonical_bytes(limit_snapshot))
    digest.update(_canonical_bytes(_lhb_payload(lhb_history)))
    if not market_activity.empty:
        digest.update(
            pd.util.hash_pandas_object(market_activity.sort_index(), index=True)
            .to_numpy()
            .tobytes()
        )
    return digest.hexdigest()


def _lhb_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for code, history in value.items():
        if not isinstance(history, Mapping):
            continue
        result[str(code)] = {
            str(day): (
                features.as_dict()
                if hasattr(features, "as_dict")
                else features
            )
            for day, features in history.items()
        }
    return result


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _latest_limit_codes(
    bars: Mapping[str, pd.DataFrame],
    names: Mapping[str, str],
) -> list[str]:
    result: list[str] = []
    for code, frame in bars.items():
        close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
        if len(close) < 2:
            continue
        ratio = price_limit_ratio(code, str(names.get(code, "")))
        if float(close.iloc[-1] / close.iloc[-2] - 1.0) >= ratio - 0.001:
            result.append(code)
    return sorted(result)


def _tdx_channel_busy(config: PlatformConfig) -> bool:
    """Probe the cross-process channel lock without waiting or retaining it."""

    import msvcrt

    config.ensure_runtime_dirs()
    path = config.cache_dir / "tq_channel.lock"
    handle = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    finally:
        handle.close()


def _probe_tdx(config: PlatformConfig, timeout_seconds: float) -> dict[str, Any]:
    """Probe TQ initialization in a disposable process so a dead client cannot hang the CLI."""

    import multiprocessing
    import queue

    timeout = max(1.0, float(timeout_seconds))
    context = multiprocessing.get_context("spawn")
    output = context.Queue(maxsize=1)
    process = context.Process(
        target=_tdx_probe_worker,
        args=(config, output),
        name="v9-repo-shadow-tdx-probe",
    )
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5.0)
        output.close()
        output.join_thread()
        return {
            "ready": False,
            "reason": "TongdaXin initialization or index read timed out",
        }
    try:
        result = output.get_nowait()
    except queue.Empty:
        result = {
            "ready": False,
            "reason": f"TongdaXin probe exited without a result (exit={process.exitcode})",
        }
    output.close()
    output.join_thread()
    return result


def _tdx_probe_worker(config: PlatformConfig, output: Any) -> None:
    try:
        with TdxProvider(config, __file__) as provider:
            bars = provider.fetch_bars(
                ["999999.SH"],
                "1d",
                3,
                fields=("Close",),
                dividend_type="front",
            )
        frame = bars.get("999999.SH")
        if frame is None or frame.empty:
            output.put({"ready": False, "reason": "TongdaXin returned no index bars"})
            return
        output.put(
            {
                "ready": True,
                "reason": "",
                "data_asof": _latest_day(frame).date().isoformat(),
            }
        )
    except Exception as exc:
        output.put(
            {
                "ready": False,
                "reason": f"TongdaXin probe failed: {type(exc).__name__}: {exc}",
            }
        )


def _run_tdx_capture(
    config: PlatformConfig,
    *,
    refresh_sectors: bool,
    refresh_data: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    import multiprocessing
    import queue

    database = Database(config)
    before = database.query(
        """SELECT COUNT(*) AS count FROM v9_repo_shadow_events
        WHERE protocol_hash=? AND event_type='SESSION_CAPTURE'""",
        (protocol_hash(),),
    )[0]["count"]
    timeout = max(10.0, float(timeout_seconds))
    context = multiprocessing.get_context("spawn")
    output = context.Queue(maxsize=1)
    process = context.Process(
        target=_tdx_capture_worker,
        args=(config, refresh_sectors, refresh_data, output),
        name="v9-repo-shadow-capture",
    )
    process.start()
    try:
        result = output.get(timeout=timeout)
    except queue.Empty:
        process.terminate()
        process.join(5.0)
        service = V9RepoForwardShadowService(config, database)
        status = service.status()
        if int(status["fresh_sessions"]) > int(before):
            result = {
                "status": "CAPTURED_OBSERVATION_ONLY_RECOVERED",
                "reason": "The shadow event committed before the disposable TDX worker timed out",
                "paper_simulation_ready": False,
                "shadow_status": status,
            }
        else:
            result = {
                "status": "BLOCKED_TDX_RUN_TIMEOUT",
                "reason": "TongdaXin data collection did not finish before the disposable-worker timeout",
                "run_timeout_seconds": timeout,
                "paper_simulation_ready": False,
                "shadow_status": status,
            }
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5.0)
        output.close()
        output.join_thread()
    return result


def _tdx_capture_worker(
    config: PlatformConfig,
    refresh_sectors: bool,
    refresh_data: bool,
    output: Any,
) -> None:
    try:
        database = Database(config)
        database.initialize()
        service = V9RepoForwardShadowService(config, database)
        result = service._capture_live_direct(
            refresh_sectors=refresh_sectors,
            refresh_data=refresh_data,
        )
        output.put(result)
    except Exception as exc:
        output.put(
            {
                "status": "BLOCKED_TDX_CAPTURE_ERROR",
                "reason": f"{type(exc).__name__}: {exc}",
                "paper_simulation_ready": False,
            }
        )


def _slice_bars(
    bars: Mapping[str, pd.DataFrame],
    asof: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for code, frame in bars.items():
        sliced = _slice_frame(frame, asof)
        if not sliced.empty:
            result[str(code)] = sliced
    return result


def _slice_frame(frame: pd.DataFrame | None, asof: pd.Timestamp) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    days = _frame_days(frame)
    result = frame.loc[days <= _day(asof)].copy()
    result.attrs.update(frame.attrs)
    return result


def _frame_days(frame: pd.DataFrame) -> pd.DatetimeIndex:
    days = pd.DatetimeIndex(frame.index)
    if days.tz is not None:
        days = days.tz_localize(None)
    return days.normalize()


def _session_days(frame: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(_frame_days(frame).unique()).sort_values()


def _latest_day(frame: pd.DataFrame) -> pd.Timestamp:
    if frame.empty:
        raise ValueError("Bar frame is empty")
    return pd.Timestamp(_frame_days(frame).max())


def _day(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp.normalize()


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "as_dict"):
        return value.as_dict()
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")
