from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import PlatformConfig
from .early_winner_research import PROJECT_ID
from .storage import Database
from .strategies.early_winner_trade import TRADE_STRATEGY_ID


DEPLOYMENT_ID = "early_winner_trade_v1"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class TradingState(StrEnum):
    VALIDATION_REQUIRED = "VALIDATION_REQUIRED"
    SHADOW = "SHADOW"
    PAPER_QUALIFIED = "PAPER_QUALIFIED"
    LIVE_APPROVAL_REQUIRED = "LIVE_APPROVAL_REQUIRED"
    LIVE_PILOT = "LIVE_PILOT"
    LIVE_ACTIVE = "LIVE_ACTIVE"
    BLOCKED_DATA = "BLOCKED_DATA"
    RISK_HALTED = "RISK_HALTED"
    RECONCILIATION_BLOCKED = "RECONCILIATION_BLOCKED"
    DISABLED = "DISABLED"


ACTIVE_BUY_STATES = {TradingState.LIVE_PILOT.value, TradingState.LIVE_ACTIVE.value}
BROKER_OPEN_STATUSES = {"PENDING_CONFIRMATION", "ACCEPTED", "PARTIALLY_FILLED", "UNKNOWN"}


class TradingSafetyError(RuntimeError):
    pass


class BrokerTransportError(RuntimeError):
    pass


class BrokerClient(Protocol):
    def read_snapshot(self) -> dict[str, Any]: ...

    def submit_limit_order(
        self, *, code: str, side: str, quantity: int, price: float
    ) -> dict[str, Any]: ...

    def cancel_order(self, *, code: str, broker_order_id: str) -> dict[str, Any]: ...


class TdxTradingHttpClient:
    """Minimal TQ-Local adapter; account handles are kept in process memory only."""

    def __init__(
        self,
        config: PlatformConfig,
        *,
        base_url: str = "http://127.0.0.1:17709",
        timeout_seconds: float = 8.0,
    ) -> None:
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._account_id: int | None = None

    def _call(self, method: str, params: Mapping[str, Any]) -> Any:
        payload = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": dict(params), "id": uuid4().hex},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                document = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise BrokerTransportError(f"TDX HTTP unavailable: {exc}") from exc
        if not isinstance(document, dict) or document.get("error"):
            raise BrokerTransportError(f"TDX RPC error: {document!r}")
        result = document.get("result")
        if isinstance(result, dict) and str(result.get("ErrorId", "0")) != "0":
            raise BrokerTransportError(
                f"TDX method {method} failed: {result.get('ErrorInfo') or result.get('Msg') or result}"
            )
        return result

    def _resolve_account_id(self) -> int:
        if self._account_id is not None:
            return self._account_id
        if not self.config.trading_account:
            self._account_id = 0
            return 0
        result = self._call(
            "stock_account",
            {
                "account": self.config.trading_account,
                "account_type": self.config.trading_account_type,
            },
        )
        value = result.get("Value") if isinstance(result, dict) else None
        if value is None:
            raise BrokerTransportError("TDX did not return a temporary account handle")
        self._account_id = int(value)
        return self._account_id

    def read_snapshot(self) -> dict[str, Any]:
        account_id = self._resolve_account_id()
        return {
            "asset": self._call("query_stock_asset", {"account_id": account_id}),
            "positions": self._call("query_stock_positions", {"account_id": account_id}),
            "orders": self._call(
                "query_stock_orders",
                {"account_id": account_id, "stock_code": "", "cancelable_only": False},
            ),
        }

    def submit_limit_order(
        self, *, code: str, side: str, quantity: int, price: float
    ) -> dict[str, Any]:
        raise TradingSafetyError(
            "real broker order submission is not compiled into this paper-only profile"
        )

    def cancel_order(self, *, code: str, broker_order_id: str) -> dict[str, Any]:
        raise TradingSafetyError(
            "real broker order cancellation is not compiled into this paper-only profile"
        )

    def _assert_live_write_enabled(self) -> None:
        if self.base_url not in {"http://127.0.0.1:17709", "http://localhost:17709"}:
            raise TradingSafetyError("trading adapter must use the loopback TDX endpoint")
        if not self.config.live_trading_enabled:
            raise TradingSafetyError("live trading is disabled by local configuration")
        if not self.config.trading_account or self.config.trading_account_type != "STOCK":
            raise TradingSafetyError("a local STOCK cash account must be configured")


class EarlyWinnerTradingService:
    def __init__(
        self,
        config: PlatformConfig,
        database: Database,
        *,
        broker: BrokerClient | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.broker = broker or TdxTradingHttpClient(config)
        self._ensure_deployment()

    def _ensure_deployment(self) -> None:
        now = _now().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO trading_deployments
                (deployment_id, strategy_id, project_id, state, account_alias, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    DEPLOYMENT_ID,
                    TRADE_STRATEGY_ID,
                    PROJECT_ID,
                    TradingState.VALIDATION_REQUIRED.value,
                    self.config.trading_account_alias,
                    now,
                    now,
                ),
            )
        self._sync_strategy_lifecycle(str(self._deployment()["state"]))

    def detail(self) -> dict[str, Any]:
        deployment = self._deployment()
        deployment["champion"] = _json_value(deployment.pop("champion_json", "{}"), {})
        deployment["metrics"] = _json_value(deployment.pop("metrics_json", "{}"), {})
        deployment["funding_complete"] = (
            deployment.get("max_capital_cny") is not None
            and deployment.get("max_account_fraction") is not None
        )
        deployment["live_write_enabled"] = bool(self.config.live_trading_enabled)
        deployment["account_configured"] = bool(self.config.trading_account)
        deployment["account_alias"] = str(deployment.get("account_alias") or "")
        deployment["account_handle_persisted"] = False
        deployment["operator_token_configured"] = bool(self.config.trading_operator_token)
        deployment["scheduler_enabled"] = bool(self.config.trading_scheduler_enabled)
        deployment["order_batches"] = self.list_order_batches(limit=20)
        deployment["latest_reconciliation"] = self._latest_reconciliation()
        deployment["risk_events"] = self._decoded_query(
            """SELECT * FROM trading_risk_events WHERE deployment_id=?
            ORDER BY triggered_at DESC LIMIT 30""",
            (DEPLOYMENT_ID,),
        )
        deployment["qualification"] = self.qualification_metrics()
        deployment["next_rebalance_date"] = _next_weekday(date.today(), 4).isoformat()
        return deployment

    def activate_shadow(self) -> dict[str, Any]:
        deployment = self._deployment()
        if deployment["state"] not in {
            TradingState.VALIDATION_REQUIRED.value,
            TradingState.BLOCKED_DATA.value,
        }:
            return self.detail()
        champion = self._latest_validated_champion()
        now = _now().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE trading_deployments SET state=?, champion_json=?, validation_id=?,
                snapshot_id=?, shadow_started_at=COALESCE(shadow_started_at, ?), updated_at=?
                WHERE deployment_id=?""",
                (
                    TradingState.SHADOW.value,
                    json.dumps(champion, ensure_ascii=False, sort_keys=True),
                    champion["validation_id"],
                    champion["snapshot_id"],
                    now,
                    now,
                    DEPLOYMENT_ID,
                ),
            )
        self._sync_strategy_lifecycle(TradingState.SHADOW.value)
        return self.detail()

    def configure_pilot(
        self,
        *,
        max_capital_cny: float,
        max_account_fraction: float,
        approve_live: bool = False,
        operator_token: str = "",
    ) -> dict[str, Any]:
        if max_capital_cny <= 0 or not 0 < max_account_fraction <= 1:
            raise ValueError("capital limit must be positive and account fraction must be in (0, 1]")
        deployment = self._deployment()
        if deployment["state"] not in {
            TradingState.PAPER_QUALIFIED.value,
            TradingState.LIVE_APPROVAL_REQUIRED.value,
        }:
            raise TradingSafetyError("shadow qualification gates have not passed")
        next_state = TradingState.LIVE_APPROVAL_REQUIRED.value
        pilot_started_at: str | None = None
        if approve_live:
            self._require_operator_token(operator_token)
            if not self.config.live_trading_enabled or not self.config.trading_account:
                raise TradingSafetyError("local account and explicit live-trading enablement are required")
            next_state = TradingState.LIVE_PILOT.value
            pilot_started_at = _now().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE trading_deployments SET max_capital_cny=?, max_account_fraction=?,
                state=?, pilot_started_at=COALESCE(pilot_started_at, ?), updated_at=?
                WHERE deployment_id=?""",
                (
                    float(max_capital_cny),
                    float(max_account_fraction),
                    next_state,
                    pilot_started_at,
                    _now().isoformat(),
                    DEPLOYMENT_ID,
                ),
            )
        self._sync_strategy_lifecycle(next_state)
        return self.detail()

    def create_order_batch(
        self,
        *,
        rebalance_date: str,
        execution_date: str,
        candidates: Iterable[Mapping[str, Any]],
        positions: Iterable[Mapping[str, Any]],
        equity: float,
        account_equity: float,
        market_health: Mapping[str, Any],
        risk_exits: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        deployment = self._deployment()
        state = str(deployment["state"])
        candidate_rows = list(candidates)
        risk_exit_map = dict(risk_exits or {})
        risk_only = bool(risk_exit_map) and not candidate_rows
        if state == TradingState.SHADOW.value:
            mode = "SHADOW"
        elif state in ACTIVE_BUY_STATES or (
            risk_only
            and state
            in {
                TradingState.RISK_HALTED.value,
                TradingState.RECONCILIATION_BLOCKED.value,
            }
        ):
            mode = "LIVE_RISK" if risk_only else "LIVE"
            self._assert_pretrade_health(market_health, buying=not risk_only)
            if deployment.get("max_capital_cny") is None or deployment.get("max_account_fraction") is None:
                raise TradingSafetyError("live capital limits are missing")
        else:
            raise TradingSafetyError(f"deployment state {state} cannot create a rebalance batch")
        existing = self.database.query(
            """SELECT batch_id FROM trading_order_batches
            WHERE deployment_id=? AND rebalance_date=? AND mode=?""",
            (DEPLOYMENT_ID, rebalance_date, mode),
        )
        if existing:
            return self.get_order_batch(str(existing[0]["batch_id"]))
        champion = _json_value(deployment.get("champion_json"), {})
        champion_hash = str(champion.get("artifact_hash") or "")
        if not champion_hash:
            raise TradingSafetyError("validated champion is not frozen")
        funding_limit = float(equity)
        if mode.startswith("LIVE"):
            funding_limit = min(
                funding_limit,
                float(deployment["max_capital_cny"]),
                float(account_equity) * float(deployment["max_account_fraction"]),
            )
        batch_id = f"ewob_{uuid4().hex}"
        intents = self._target_intents(
            batch_key=batch_id,
            candidates=candidate_rows,
            positions=list(positions),
            capital=funding_limit,
            risk_exits=risk_exit_map,
        )
        now = _now()
        execution_day = date.fromisoformat(execution_date)
        approval_deadline = datetime.combine(execution_day, time(9, 20), SHANGHAI_TZ)
        expires_at = datetime.combine(
            execution_day + (timedelta(days=30) if mode.startswith("LIVE") else timedelta()),
            time(14, 57) if mode.startswith("LIVE") else time(9, 35),
            SHANGHAI_TZ,
        )
        confirmation_code = f"{secrets.randbelow(1_000_000):06d}"
        batch_status = "APPROVED" if mode == "LIVE_RISK" else "PENDING_APPROVAL"
        intent_status = "READY" if batch_status == "APPROVED" else "PENDING_APPROVAL"
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO trading_order_batches
                (batch_id, deployment_id, rebalance_date, execution_date, mode, status,
                 confirmation_code, champion_hash, snapshot_id, generated_at,
                 approval_deadline, approved_at, decided_at, decision_note, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    DEPLOYMENT_ID,
                    rebalance_date,
                    execution_date,
                    mode,
                    batch_status,
                    confirmation_code,
                    champion_hash,
                    str(deployment.get("snapshot_id") or ""),
                    now.isoformat(),
                    approval_deadline.isoformat(),
                    now.isoformat() if batch_status == "APPROVED" else None,
                    now.isoformat() if batch_status == "APPROVED" else None,
                    "automatic risk exit" if batch_status == "APPROVED" else "",
                    expires_at.isoformat(),
                ),
            )
            for item in intents:
                connection.execute(
                    """INSERT INTO trading_order_intents
                    (intent_id, batch_id, idempotency_key, code, name, industry, side,
                     reason, target_weight, requested_quantity, limit_price, adv20,
                     status, automatic_risk_exit, evidence_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                    (
                        item["intent_id"],
                        batch_id,
                        item["idempotency_key"],
                        item["code"],
                        item["name"],
                        item["industry"],
                        item["side"],
                        item["reason"],
                        item["target_weight"],
                        item["requested_quantity"],
                        item["adv20"],
                        intent_status,
                        1 if item["automatic_risk_exit"] else 0,
                        json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True),
                    ),
                )
        return self.get_order_batch(batch_id, include_confirmation=True)

    @staticmethod
    def risk_exit_reasons(
        positions: Iterable[Mapping[str, Any]],
    ) -> dict[str, str]:
        hard_negative = {"CLARIFICATION", "REDUCTION", "RISK_WARNING"}
        reasons: dict[str, str] = {}
        for position in positions:
            code = str(position.get("code") or "")
            close = float(position.get("close") or 0)
            ma60 = float(position.get("ma60") or 0)
            peak = float(position.get("holding_peak") or 0)
            event_type = str(position.get("event_type") or "")
            if event_type in hard_negative:
                reasons[code] = "MAJOR_NEGATIVE_EVENT"
            elif close > 0 and ma60 > 0 and close < ma60:
                reasons[code] = "BELOW_MA60"
            elif close > 0 and peak > 0 and close / peak - 1.0 <= -0.25:
                reasons[code] = "DRAWDOWN_25_PERCENT"
        return reasons

    def decide_order_batch(
        self,
        batch_id: str,
        *,
        decision: str,
        confirmation_code: str,
        note: str = "",
        operator_token: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        batch = self._batch_row(batch_id)
        if batch["status"] != "PENDING_APPROVAL":
            return self.get_order_batch(batch_id)
        decision_time = now or _now()
        if decision_time > datetime.fromisoformat(str(batch["approval_deadline"])):
            self._set_batch_status(batch_id, "EXPIRED", note="approval deadline passed")
            return self.get_order_batch(batch_id)
        normalized = decision.upper()
        if normalized not in {"APPROVED", "REJECTED"}:
            raise ValueError("decision must be APPROVED or REJECTED")
        if not hmac.compare_digest(str(batch["confirmation_code"]), str(confirmation_code)):
            raise TradingSafetyError("batch confirmation code is invalid")
        if normalized == "APPROVED" and str(batch["mode"]).startswith("LIVE"):
            self._require_operator_token(operator_token)
        status = normalized
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE trading_order_batches SET status=?, approved_at=?, decided_at=?,
                decision_note=? WHERE batch_id=?""",
                (
                    status,
                    decision_time.isoformat() if status == "APPROVED" else None,
                    decision_time.isoformat(),
                    note,
                    batch_id,
                ),
            )
            connection.execute(
                "UPDATE trading_order_intents SET status=? WHERE batch_id=? AND status='PENDING_APPROVAL'",
                ("READY" if status == "APPROVED" else "REJECTED", batch_id),
            )
        return self.get_order_batch(batch_id)

    def order_batch_confirmation_challenge(
        self,
        batch_id: str,
        *,
        operator_token: str = "",
    ) -> dict[str, Any]:
        batch = self._batch_row(batch_id)
        if batch["status"] != "PENDING_APPROVAL":
            raise TradingSafetyError("only pending batches have a confirmation challenge")
        if str(batch["mode"]).startswith("LIVE"):
            self._require_operator_token(operator_token)
        return {
            "batch_id": batch_id,
            "confirmation_code": str(batch["confirmation_code"]),
            "approval_deadline": str(batch["approval_deadline"]),
            "champion_hash": str(batch["champion_hash"]),
            "snapshot_id": str(batch["snapshot_id"]),
        }

    def submit_intent(
        self,
        intent_id: str,
        *,
        bid: float,
        ask: float,
        limit_up: float,
        limit_down: float,
        quote_age_seconds: float,
        clock_skew_seconds: float,
        calendar_match: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        intent, batch = self._intent_with_batch(intent_id)
        moment = now or _now()
        if batch["status"] != "APPROVED" or intent["status"] not in {"READY", "REPRICE_READY"}:
            raise TradingSafetyError("intent is not in an approved executable state")
        if moment > datetime.fromisoformat(str(batch["expires_at"])):
            self._update_intent_status(intent_id, "EXPIRED")
            raise TradingSafetyError("order intent has expired")
        if str(batch["mode"]).startswith("LIVE"):
            self._assert_execution_window(intent, batch, moment)
        self._assert_pretrade_health(
            {
                "tdx_available": True,
                "quote_age_seconds": quote_age_seconds,
                "clock_skew_seconds": clock_skew_seconds,
                "calendar_match": calendar_match,
            },
            buying=intent["side"] == "BUY",
        )
        price = controlled_limit_price(
            side=str(intent["side"]),
            bid=float(bid),
            ask=float(ask),
            limit_up=float(limit_up),
            limit_down=float(limit_down),
        )
        if batch["mode"] == "SHADOW":
            return self._fill_shadow(intent, price, moment)
        if self._deployment()["state"] not in ACTIVE_BUY_STATES and intent["side"] == "BUY":
            raise TradingSafetyError("new buys are disabled in the current deployment state")
        self._require_live_batch_safety(batch)
        existing = self.database.query(
            """SELECT * FROM trading_broker_orders WHERE intent_id=?
            ORDER BY submitted_at DESC LIMIT 1""",
            (intent_id,),
        )
        if existing and existing[0]["status"] in BROKER_OPEN_STATUSES:
            return self._decode_row(existing[0])
        try:
            response = self.broker.submit_limit_order(
                code=str(intent["code"]),
                side=str(intent["side"]),
                quantity=int(intent["requested_quantity"]),
                price=price,
            )
        except BrokerTransportError as exc:
            recovered = self._recover_after_ambiguous_submit(intent, price)
            if recovered is not None:
                return recovered
            self._record_broker_order(
                intent=intent,
                mode=str(batch["mode"]),
                status="UNKNOWN",
                price=price,
                response={},
                error=str(exc),
                submitted_at=moment,
            )
            self._block_reconciliation("AMBIGUOUS_SUBMIT", {"intent_id": intent_id})
            return self._latest_broker_order(intent_id)
        value = int(response.get("Value") or 0)
        status = {0: "REJECTED", 1: "PENDING_CONFIRMATION", 2: "ACCEPTED"}.get(value, "UNKNOWN")
        stored_response = {
            **response,
            "_arrival_price": float(ask if intent["side"] == "BUY" else bid),
        }
        self._record_broker_order(
            intent=intent,
            mode=str(batch["mode"]),
            status=status,
            price=price,
            response=stored_response,
            error="" if status != "REJECTED" else str(response.get("Msg") or "rejected"),
            submitted_at=moment,
        )
        self._update_intent_status(intent_id, status, price=price, attempted_at=moment)
        return self._latest_broker_order(intent_id)

    def reconcile(self) -> dict[str, Any]:
        deployment = self._deployment()
        if deployment["state"] == TradingState.SHADOW.value:
            return self._save_reconciliation(snapshot={}, differences=[])
        snapshot = self.broker.read_snapshot()
        normalized = {
            "asset": _unwrap_tdx_rows(snapshot.get("asset")),
            "positions": _unwrap_tdx_rows(snapshot.get("positions")),
            "orders": _unwrap_tdx_rows(snapshot.get("orders")),
        }
        self._apply_broker_orders(normalized["orders"])
        differences = self._reconciliation_differences(normalized)
        result = self._save_reconciliation(snapshot=normalized, differences=differences)
        if differences:
            self._block_reconciliation(
                "RECONCILIATION_DIFFERENCE",
                {"difference_count": len(differences)},
            )
        return result

    def scheduler_tick(
        self,
        *,
        quotes: Mapping[str, Mapping[str, Any]],
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run one recoverable scheduler cycle; no background writer starts by default."""
        moment = (now or _now()).astimezone(SHANGHAI_TZ)
        if not force and not self.config.trading_scheduler_enabled:
            return {"status": "DISABLED", "submitted": 0, "canceled": 0}
        scheduler_id = f"ews_{uuid4().hex}"
        self._save_heartbeat(scheduler_id, moment, "START", "RUNNING", "")
        submitted = 0
        canceled = 0
        for batch in self.database.query(
            """SELECT * FROM trading_order_batches WHERE deployment_id=?
            AND status IN ('PENDING_APPROVAL','APPROVED') ORDER BY generated_at""",
            (DEPLOYMENT_ID,),
        ):
            if batch["status"] == "PENDING_APPROVAL" and moment > datetime.fromisoformat(
                str(batch["approval_deadline"])
            ):
                self._set_batch_status(str(batch["batch_id"]), "EXPIRED", note="09:20 approval expired")
                continue
            if batch["status"] != "APPROVED":
                continue
            intents = self.database.query(
                "SELECT * FROM trading_order_intents WHERE batch_id=? ORDER BY side DESC, code",
                (batch["batch_id"],),
            )
            for intent in intents:
                quote = quotes.get(str(intent["code"]))
                if not quote:
                    continue
                current_time = moment.time().replace(tzinfo=None)
                if intent["side"] == "BUY" and current_time >= time(9, 35):
                    canceled += int(self._cancel_intent_order(intent, reason="09:35 buy expiry"))
                    continue
                if (
                    intent["side"] == "BUY"
                    and time(9, 32, 30) <= current_time < time(9, 35)
                    and intent["status"] in {"ACCEPTED", "PENDING_CONFIRMATION", "PARTIALLY_FILLED"}
                    and int(intent.get("attempt_count") or 0) <= 1
                ):
                    if self._cancel_intent_order(intent, reason="single controlled reprice"):
                        self._update_intent_status(str(intent["intent_id"]), "REPRICE_READY")
                if intent["status"] not in {"READY", "REPRICE_READY"}:
                    refreshed = self.database.query(
                        "SELECT * FROM trading_order_intents WHERE intent_id=?",
                        (intent["intent_id"],),
                    )[0]
                    if refreshed["status"] not in {"READY", "REPRICE_READY"}:
                        continue
                try:
                    self.submit_intent(
                        str(intent["intent_id"]),
                        bid=float(quote.get("bid") or 0),
                        ask=float(quote.get("ask") or 0),
                        limit_up=float(quote.get("limit_up") or 0),
                        limit_down=float(quote.get("limit_down") or 0),
                        quote_age_seconds=float(
                            999 if quote.get("age_seconds") is None else quote["age_seconds"]
                        ),
                        clock_skew_seconds=float(
                            999
                            if quote.get("clock_skew_seconds") is None
                            else quote["clock_skew_seconds"]
                        ),
                        calendar_match=bool(quote.get("calendar_match")),
                        now=moment,
                    )
                    submitted += 1
                except TradingSafetyError:
                    continue
        self._save_heartbeat(
            scheduler_id,
            moment,
            "COMPLETED",
            "SUCCEEDED",
            f"submitted={submitted}; canceled={canceled}",
        )
        return {"status": "SUCCEEDED", "submitted": submitted, "canceled": canceled}

    def kill_switch(self, *, note: str, operator_token: str = "") -> dict[str, Any]:
        if self._deployment()["state"] in ACTIVE_BUY_STATES:
            self._require_operator_token(operator_token)
        self._risk_halt("MANUAL_KILL_SWITCH", {"note": note})
        return self.detail()

    def resume_after_review(
        self,
        *,
        note: str,
        operator_token: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        deployment = self._deployment()
        if deployment["state"] not in {
            TradingState.RISK_HALTED.value,
            TradingState.RECONCILIATION_BLOCKED.value,
        }:
            raise TradingSafetyError("deployment is not halted")
        moment = now or _now()
        halted_at = _parse_datetime(deployment.get("last_halt_at"))
        if halted_at is None or moment.astimezone(SHANGHAI_TZ).date() <= halted_at.date():
            raise TradingSafetyError("a halt cannot be cleared on its trigger date")
        latest_reconciliation = self._latest_reconciliation()
        if not latest_reconciliation or latest_reconciliation.get("status") != "MATCHED":
            raise TradingSafetyError("a matched reconciliation is required before resuming")
        if self.database.query(
            "SELECT 1 FROM trading_broker_orders WHERE status IN ('UNKNOWN','CANCEL_UNKNOWN') LIMIT 1"
        ):
            raise TradingSafetyError("unknown broker orders must be resolved before resuming")
        events = self._decoded_query(
            """SELECT * FROM trading_risk_events WHERE deployment_id=? AND status='OPEN'
            ORDER BY triggered_at DESC""",
            (DEPLOYMENT_ID,),
        )
        previous_state = str(
            next(
                (
                    event.get("details", {}).get("previous_state")
                    for event in events
                    if event.get("details", {}).get("previous_state")
                ),
                TradingState.SHADOW.value,
            )
        )
        if previous_state in ACTIVE_BUY_STATES:
            self._require_operator_token(operator_token)
        if previous_state not in {
            TradingState.SHADOW.value,
            TradingState.LIVE_PILOT.value,
            TradingState.LIVE_ACTIVE.value,
        }:
            previous_state = TradingState.SHADOW.value
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE trading_risk_events SET status='RESOLVED', resolved_at=?,
                resolution_note=? WHERE deployment_id=? AND status='OPEN'""",
                (moment.isoformat(), note, DEPLOYMENT_ID),
            )
        self._set_state(previous_state)
        return self.detail()

    def record_sleeve_equity(self, equity: float, *, asof: str | None = None) -> dict[str, Any]:
        if equity < 0:
            raise ValueError("equity must not be negative")
        deployment = self._deployment()
        today = asof or date.today().isoformat()
        previous = float(deployment.get("last_equity") or equity)
        high_water = max(float(deployment.get("high_water_equity") or equity), equity)
        daily_loss = equity / previous - 1.0 if previous > 0 else 0.0
        drawdown = equity / high_water - 1.0 if high_water > 0 else 0.0
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE trading_deployments SET high_water_equity=?, last_equity=?,
                last_equity_date=?, updated_at=? WHERE deployment_id=?""",
                (high_water, equity, today, _now().isoformat(), DEPLOYMENT_ID),
            )
        if daily_loss <= -0.02 or drawdown <= -0.08:
            self._risk_halt(
                "LOSS_LIMIT",
                {"daily_return": daily_loss, "high_water_drawdown": drawdown, "equity": equity},
            )
        return {"equity": equity, "daily_return": daily_loss, "high_water_drawdown": drawdown}

    def qualification_metrics(self) -> dict[str, Any]:
        deployment = self._deployment()
        shadow_start = _parse_datetime(deployment.get("shadow_started_at"))
        pilot_start = _parse_datetime(deployment.get("pilot_started_at"))
        shadow = self._fill_metrics(mode="SHADOW", since=shadow_start)
        live = self._fill_metrics(mode="LIVE", since=pilot_start)
        shadow["weeks"] = _elapsed_weeks(shadow_start)
        live["weeks"] = _elapsed_weeks(pilot_start)
        shadow["passed"] = bool(
            shadow["weeks"] >= 12
            and shadow["fills"] >= 50
            and shadow["exits"] >= 20
            and shadow["execution_rate"] >= 0.80
            and shadow["median_slippage"] <= 0.003
            and shadow["p95_slippage"] <= 0.01
            and shadow["data_success_rate"] >= 0.98
            and shadow["point_in_time_failures"] == 0
        )
        live["passed"] = bool(
            live["weeks"] >= 8
            and live["fills"] >= 20
            and live["duplicate_orders"] == 0
            and live["unauthorized_orders"] == 0
            and live["unresolved_reconciliations"] == 0
            and live["execution_rate"] >= 0.80
            and live["median_slippage"] <= 0.003
            and live["p95_slippage"] <= 0.01
        )
        return {"shadow": shadow, "live_pilot": live}

    def record_data_cycle(
        self,
        *,
        succeeded: bool,
        point_in_time_passed: bool,
        detail: str = "",
        observed_at: datetime | None = None,
    ) -> None:
        moment = observed_at or _now()
        cycle_id = uuid4().hex
        self._save_heartbeat(
            f"data_{cycle_id}",
            moment,
            "DATA_REFRESH",
            "SUCCEEDED" if succeeded else "FAILED",
            detail,
        )
        self._save_heartbeat(
            f"pit_{cycle_id}",
            moment,
            "POINT_IN_TIME_AUDIT",
            "SUCCEEDED" if point_in_time_passed else "FAILED",
            detail,
        )

    def refresh_qualification(self) -> dict[str, Any]:
        state = self._deployment()["state"]
        metrics = self.qualification_metrics()
        if state == TradingState.SHADOW.value and metrics["shadow"]["passed"]:
            self._set_state(TradingState.PAPER_QUALIFIED.value)
        elif state == TradingState.LIVE_PILOT.value and metrics["live_pilot"]["passed"]:
            self._set_state(TradingState.LIVE_ACTIVE.value, live_started_at=_now().isoformat())
        return self.detail()

    def list_order_batches(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.database.query(
            """SELECT * FROM trading_order_batches WHERE deployment_id=?
            ORDER BY execution_date DESC, generated_at DESC LIMIT ?""",
            (DEPLOYMENT_ID, int(limit)),
        )
        output = []
        for row in rows:
            item = dict(row)
            item.pop("confirmation_code", None)
            output.append(item)
        return output

    def get_order_batch(self, batch_id: str, *, include_confirmation: bool = False) -> dict[str, Any]:
        batch = self._batch_row(batch_id)
        if not include_confirmation:
            batch.pop("confirmation_code", None)
        batch["intents"] = self._decoded_query(
            "SELECT * FROM trading_order_intents WHERE batch_id=? ORDER BY side DESC, code",
            (batch_id,),
        )
        return batch

    def _target_intents(
        self,
        *,
        batch_key: str,
        candidates: list[Mapping[str, Any]],
        positions: list[Mapping[str, Any]],
        capital: float,
        risk_exits: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if capital <= 0:
            raise TradingSafetyError("strategy capital is unavailable")
        position_map = {str(item.get("code")): item for item in positions}
        selected: list[Mapping[str, Any]] = []
        industry_counts: dict[str, int] = {}
        for candidate in sorted(candidates, key=lambda item: (int(item.get("rank") or 9999), str(item.get("code")))):
            industry = str(candidate.get("industry") or "未分类")
            if industry_counts.get(industry, 0) >= 5:
                continue
            selected.append(candidate)
            industry_counts[industry] = industry_counts.get(industry, 0) + 1
            if len(selected) >= 20:
                break
        target_codes = {str(item.get("code")) for item in selected}
        intents: list[dict[str, Any]] = []
        for code, position in sorted(position_map.items()):
            quantity = int(float(position.get("can_use_volume") or position.get("quantity") or 0))
            reason = risk_exits.get(code) or ("RANK_EXIT" if code not in target_codes else "")
            if reason and quantity > 0:
                intents.append(
                    self._intent_record(
                        batch_key=batch_key,
                        code=code,
                        name=str(position.get("name") or ""),
                        industry=str(position.get("industry") or ""),
                        side="SELL",
                        reason=reason,
                        target_weight=0.0,
                        quantity=(quantity // 100) * 100,
                        adv20=float(position.get("adv20") or 0),
                        automatic_risk_exit=code in risk_exits,
                        evidence={"t_plus_one_sellable_quantity": quantity},
                    )
                )
        for candidate in selected:
            code = str(candidate.get("code"))
            if code in risk_exits:
                continue
            price = float(candidate.get("execution_price") or candidate.get("close") or 0)
            adv20 = float(candidate.get("adv20") or 0)
            if price <= 0 or adv20 <= 0:
                continue
            current_quantity = int(float(position_map.get(code, {}).get("quantity") or 0))
            target_weight = min(0.05, 1.0 / 20.0)
            target_quantity = math.floor((capital * target_weight / price) / 100) * 100
            adv_quantity = math.floor((adv20 * 0.02 / price) / 100) * 100
            quantity = max(0, min(target_quantity - current_quantity, adv_quantity))
            if quantity <= 0:
                continue
            intents.append(
                self._intent_record(
                    batch_key=batch_key,
                    code=code,
                    name=str(candidate.get("name") or ""),
                    industry=str(candidate.get("industry") or ""),
                    side="BUY",
                    reason="WEEKLY_CHAMPION_TOP20",
                    target_weight=target_weight,
                    quantity=quantity,
                    adv20=adv20,
                    automatic_risk_exit=False,
                    evidence={
                        "rank": candidate.get("rank"),
                        "rule_score": candidate.get("rule_score"),
                        "probability": candidate.get("probability"),
                    },
                )
            )
        return intents

    @staticmethod
    def _intent_record(
        *,
        batch_key: str,
        code: str,
        name: str,
        industry: str,
        side: str,
        reason: str,
        target_weight: float,
        quantity: int,
        adv20: float,
        automatic_risk_exit: bool,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        intent_id = f"ewoi_{uuid4().hex}"
        business = {
            "batch_key": batch_key,
            "strategy_id": TRADE_STRATEGY_ID,
            "code": code,
            "side": side,
            "reason": reason,
            "quantity": int(quantity),
            "evidence": dict(evidence),
        }
        key = hashlib.sha256(
            json.dumps(business, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "intent_id": intent_id,
            "idempotency_key": key,
            "code": code,
            "name": name,
            "industry": industry,
            "side": side,
            "reason": reason,
            "target_weight": target_weight,
            "requested_quantity": int(quantity),
            "adv20": adv20,
            "automatic_risk_exit": automatic_risk_exit,
            "evidence": dict(evidence),
        }

    def _latest_validated_champion(self) -> dict[str, Any]:
        rows = self.database.query(
            """SELECT validation_id, snapshot_id, champion_json FROM research_validations
            WHERE project_id=? AND status='OBSERVATION_ONLY'
            ORDER BY created_at DESC LIMIT 1""",
            (PROJECT_ID,),
        )
        if not rows:
            raise TradingSafetyError("research validation has not passed")
        champion = _json_value(rows[0].get("champion_json"), {})
        if not champion or not champion.get("artifact_hash"):
            raise TradingSafetyError("validation did not freeze a champion artifact")
        champion.setdefault("validation_id", rows[0]["validation_id"])
        champion.setdefault("snapshot_id", rows[0]["snapshot_id"])
        return champion

    def _assert_pretrade_health(self, health: Mapping[str, Any], *, buying: bool) -> None:
        failures = []
        if not bool(health.get("tdx_available")):
            failures.append("TDX_UNAVAILABLE")
        quote_age = health.get("quote_age_seconds")
        if float(999 if quote_age is None else quote_age) > 5:
            failures.append("STALE_QUOTE")
        clock_skew = health.get("clock_skew_seconds")
        if abs(float(999 if clock_skew is None else clock_skew)) > 2:
            failures.append("CLOCK_SKEW")
        if not bool(health.get("calendar_match")):
            failures.append("CALENDAR_MISMATCH")
        reconciliation = self._latest_reconciliation()
        if reconciliation and reconciliation.get("status") != "MATCHED":
            failures.append("RECONCILIATION_DIFFERENCE")
        unknown = self.database.query(
            "SELECT 1 FROM trading_broker_orders WHERE status='UNKNOWN' LIMIT 1"
        )
        if unknown:
            failures.append("UNKNOWN_ORDER")
        if failures:
            if buying:
                self._block_reconciliation("PRETRADE_GATE", {"failures": failures})
            raise TradingSafetyError(f"pre-trade gates failed: {', '.join(failures)}")

    @staticmethod
    def _assert_execution_window(
        intent: Mapping[str, Any], batch: Mapping[str, Any], moment: datetime
    ) -> None:
        local = moment.astimezone(SHANGHAI_TZ)
        execution_day = date.fromisoformat(str(batch["execution_date"]))
        if local.weekday() >= 5 or local.date() < execution_day:
            raise TradingSafetyError("the exchange execution window is closed")
        current_time = local.time().replace(tzinfo=None)
        is_buy = str(intent["side"]) == "BUY"
        if is_buy:
            if local.date() != execution_day or not time(9, 30, 5) <= current_time <= time(9, 35):
                raise TradingSafetyError("buy intents are valid only 09:30:05-09:35 on execution day")
        elif not (
            time(9, 30, 5) <= current_time <= time(11, 30)
            or time(13, 0) <= current_time <= time(14, 57)
        ):
            raise TradingSafetyError("sell intents may execute only during continuous trading")

    def _require_live_batch_safety(self, batch: Mapping[str, Any]) -> None:
        if not str(batch["mode"]).startswith("LIVE"):
            raise TradingSafetyError("broker writes require a LIVE batch")
        if not self.config.live_trading_enabled:
            raise TradingSafetyError("live trading is locally disabled")
        if not self.config.trading_operator_token:
            raise TradingSafetyError("operator token is not configured")

    def _require_operator_token(self, value: str) -> None:
        expected = self.config.trading_operator_token
        if not expected or not hmac.compare_digest(expected, value):
            raise TradingSafetyError("operator token is invalid or not configured")

    def _recover_after_ambiguous_submit(
        self, intent: Mapping[str, Any], price: float
    ) -> dict[str, Any] | None:
        try:
            snapshot = self.broker.read_snapshot()
        except BrokerTransportError:
            return None
        orders = _unwrap_tdx_rows(snapshot.get("orders"))
        matches = [
            item
            for item in orders
            if str(item.get("Code") or item.get("code") or "") == str(intent["code"])
            and int(float(item.get("WtVol") or item.get("order_volume") or 0))
            == int(intent["requested_quantity"])
            and abs(float(item.get("WtPrice") or item.get("price") or 0) - price) < 0.0001
        ]
        if len(matches) != 1:
            return None
        response = {"Value": 2, "Wtbh": matches[0].get("Wtbh"), "recovered": True}
        self._record_broker_order(
            intent=intent,
            mode="LIVE",
            status="ACCEPTED",
            price=price,
            response=response,
            error="",
            submitted_at=_now(),
        )
        self._update_intent_status(str(intent["intent_id"]), "ACCEPTED", price=price)
        return self._latest_broker_order(str(intent["intent_id"]))

    def _fill_shadow(
        self, intent: Mapping[str, Any], price: float, filled_at: datetime
    ) -> dict[str, Any]:
        broker_row_id = self._record_broker_order(
            intent=intent,
            mode="SHADOW",
            status="FILLED",
            price=price,
            response={"simulation": True},
            error="",
            submitted_at=filled_at,
            filled_quantity=int(intent["requested_quantity"]),
            average_fill_price=price,
        )
        arrival = price
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO trading_broker_fills
                (fill_id, broker_order_row_id, intent_id, code, side, filled_at,
                 quantity, price, arrival_price, slippage, fees)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
                (
                    f"ewf_{uuid4().hex}",
                    broker_row_id,
                    intent["intent_id"],
                    intent["code"],
                    intent["side"],
                    filled_at.isoformat(),
                    intent["requested_quantity"],
                    price,
                    arrival,
                ),
            )
        self._update_intent_status(str(intent["intent_id"]), "FILLED", price=price)
        return self._latest_broker_order(str(intent["intent_id"]))

    def _record_broker_order(
        self,
        *,
        intent: Mapping[str, Any],
        mode: str,
        status: str,
        price: float,
        response: Mapping[str, Any],
        error: str,
        submitted_at: datetime,
        filled_quantity: int = 0,
        average_fill_price: float | None = None,
    ) -> str:
        row_id = f"ewbo_{uuid4().hex}"
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO trading_broker_orders
                (broker_order_row_id, intent_id, broker_order_id, mode, status,
                 submitted_at, updated_at, order_quantity, filled_quantity, limit_price,
                 average_fill_price, response_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row_id,
                    intent["intent_id"],
                    str(response.get("Wtbh") or response.get("order_id") or ""),
                    mode,
                    status,
                    submitted_at.isoformat(),
                    submitted_at.isoformat(),
                    int(intent["requested_quantity"]),
                    int(filled_quantity),
                    price,
                    average_fill_price,
                    json.dumps(dict(response), ensure_ascii=False, sort_keys=True),
                    error,
                ),
            )
        return row_id

    def _save_reconciliation(
        self, *, snapshot: Mapping[str, Any], differences: list[dict[str, Any]]
    ) -> dict[str, Any]:
        captured = _now().isoformat()
        snapshot_id = ""
        if snapshot:
            snapshot_id = f"ewps_{uuid4().hex}"
            content = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
            with self.database.connect() as connection:
                connection.execute(
                    """INSERT INTO trading_position_snapshots
                    (snapshot_row_id, deployment_id, captured_at, source, asset_json,
                     positions_json, orders_json, content_hash)
                    VALUES (?, ?, ?, 'TDX', ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        DEPLOYMENT_ID,
                        captured,
                        json.dumps(snapshot.get("asset", []), ensure_ascii=False),
                        json.dumps(snapshot.get("positions", []), ensure_ascii=False),
                        json.dumps(snapshot.get("orders", []), ensure_ascii=False),
                        hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    ),
                )
        reconciliation_id = f"ewr_{uuid4().hex}"
        status = "MATCHED" if not differences else "BLOCKED"
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO trading_reconciliations
                (reconciliation_id, deployment_id, captured_at, status,
                 snapshot_row_id, differences_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    reconciliation_id,
                    DEPLOYMENT_ID,
                    captured,
                    status,
                    snapshot_id,
                    json.dumps(differences, ensure_ascii=False, sort_keys=True),
                ),
            )
        return self._latest_reconciliation() or {}

    def _reconciliation_differences(self, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        known = {
            str(row["broker_order_id"])
            for row in self.database.query(
                "SELECT broker_order_id FROM trading_broker_orders WHERE broker_order_id<>''"
            )
        }
        differences = []
        for order in snapshot.get("orders", []):
            order_id = str(order.get("Wtbh") or order.get("order_id") or "")
            code = str(order.get("Code") or order.get("code") or "")
            if order_id and order_id not in known and _broker_order_is_open(order):
                differences.append({"type": "UNKNOWN_OPEN_ORDER", "order_id": order_id, "code": code})
        for row in self.database.query("SELECT * FROM trading_broker_orders WHERE status='UNKNOWN'"):
            differences.append(
                {"type": "LOCAL_UNKNOWN_ORDER", "intent_id": row["intent_id"], "code": ""}
            )
        return differences

    def _apply_broker_orders(self, orders: Iterable[Mapping[str, Any]]) -> None:
        status_map = {
            0: "REJECTED",
            1: "ACCEPTED",
            2: "PARTIALLY_FILLED",
            3: "FILLED",
            4: "PARTIALLY_CANCELED",
            5: "CANCELED",
        }
        for broker_order in orders:
            broker_id = str(broker_order.get("Wtbh") or broker_order.get("order_id") or "")
            if not broker_id:
                continue
            local_rows = self.database.query(
                """SELECT o.*, i.code, i.side FROM trading_broker_orders o
                JOIN trading_order_intents i ON i.intent_id=o.intent_id
                WHERE o.broker_order_id=? ORDER BY o.submitted_at DESC LIMIT 1""",
                (broker_id,),
            )
            if not local_rows:
                continue
            local = local_rows[0]
            broker_status = int(float(broker_order.get("Status") or 0))
            status = status_map.get(broker_status, "UNKNOWN")
            filled = int(abs(float(broker_order.get("CJVol") or 0)))
            average_price = float(broker_order.get("CjPric") or 0) or None
            previous_filled = int(local.get("filled_quantity") or 0)
            prior_response = _json_value(local.get("response_json"), {})
            merged_response = {
                **prior_response,
                **dict(broker_order),
            }
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE trading_broker_orders SET status=?, filled_quantity=?,
                    average_fill_price=?, updated_at=?, response_json=?
                    WHERE broker_order_row_id=?""",
                    (
                        status,
                        filled,
                        average_price,
                        _now().isoformat(),
                        json.dumps(merged_response, ensure_ascii=False, sort_keys=True),
                        local["broker_order_row_id"],
                    ),
                )
            self._update_intent_status(str(local["intent_id"]), status)
            incremental = max(0, filled - previous_filled)
            if incremental and average_price is not None:
                arrival = float(prior_response.get("_arrival_price") or local["limit_price"])
                side_multiplier = 1.0 if local["side"] == "BUY" else -1.0
                slippage = side_multiplier * (average_price / arrival - 1.0) if arrival > 0 else 0.0
                with self.database.connect() as connection:
                    connection.execute(
                        """INSERT INTO trading_broker_fills
                        (fill_id, broker_order_row_id, intent_id, code, side, filled_at,
                         quantity, price, arrival_price, slippage, fees)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                        (
                            f"ewf_{uuid4().hex}",
                            local["broker_order_row_id"],
                            local["intent_id"],
                            local["code"],
                            local["side"],
                            _now().isoformat(),
                            incremental,
                            average_price,
                            arrival,
                            slippage,
                        ),
                    )

    def _fill_metrics(self, *, mode: str, since: datetime | None) -> dict[str, Any]:
        since_text = since.isoformat() if since else "9999-12-31"
        mode_pattern = "LIVE%" if mode == "LIVE" else mode
        fills = self.database.query(
            """SELECT f.* FROM trading_broker_fills f
            JOIN trading_broker_orders o ON o.broker_order_row_id=f.broker_order_row_id
            WHERE o.mode LIKE ? AND f.filled_at>=? ORDER BY f.filled_at""",
            (mode_pattern, since_text),
        ) if since else []
        intents = self.database.query(
            """SELECT i.* FROM trading_order_intents i
            JOIN trading_order_batches b ON b.batch_id=i.batch_id
            WHERE b.mode LIKE ? AND b.generated_at>=?""",
            (mode_pattern, since_text),
        ) if since else []
        slippage = sorted(abs(float(item.get("slippage") or 0)) for item in fills)
        completed = sum(1 for item in intents if item["status"] in {"FILLED", "PARTIALLY_FILLED"})
        unresolved = self.database.query(
            "SELECT COUNT(*) AS n FROM trading_reconciliations WHERE deployment_id=? AND status<>'MATCHED'",
            (DEPLOYMENT_ID,),
        )[0]["n"]
        return {
            "fills": len(fills),
            "exits": sum(1 for item in fills if item["side"] == "SELL"),
            "execution_rate": completed / len(intents) if intents else 0.0,
            "median_slippage": _percentile(slippage, 0.50),
            "p95_slippage": _percentile(slippage, 0.95),
            "data_success_rate": self._data_task_success_rate(since_text),
            "point_in_time_failures": self._point_in_time_failures(since_text),
            "duplicate_orders": 0,
            "unauthorized_orders": 0,
            "unresolved_reconciliations": int(unresolved),
        }

    def _data_task_success_rate(self, since: str) -> float:
        rows = self.database.query(
            """SELECT status FROM trading_scheduler_heartbeats
            WHERE deployment_id=? AND phase='DATA_REFRESH' AND heartbeat_at>=?""",
            (DEPLOYMENT_ID, since),
        )
        if not rows:
            return 0.0
        return sum(1 for row in rows if row["status"] == "SUCCEEDED") / len(rows)

    def _point_in_time_failures(self, since: str) -> int:
        rows = self.database.query(
            """SELECT COUNT(*) AS total,
            SUM(CASE WHEN status<>'SUCCEEDED' THEN 1 ELSE 0 END) AS failed
            FROM trading_scheduler_heartbeats
            WHERE deployment_id=? AND phase='POINT_IN_TIME_AUDIT' AND heartbeat_at>=?""",
            (DEPLOYMENT_ID, since),
        )
        if not rows or int(rows[0].get("total") or 0) == 0:
            return 1
        return int(rows[0].get("failed") or 0)

    def _risk_halt(self, event_type: str, details: Mapping[str, Any]) -> None:
        now = _now().isoformat()
        previous_state = str(self._deployment()["state"])
        event_details = {"previous_state": previous_state, **dict(details)}
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO trading_risk_events
                (risk_event_id, deployment_id, event_type, severity, status,
                 triggered_at, details_json)
                VALUES (?, ?, ?, 'CRITICAL', 'OPEN', ?, ?)""",
                (
                    f"ewrisk_{uuid4().hex}",
                    DEPLOYMENT_ID,
                    event_type,
                    now,
                    json.dumps(event_details, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.execute(
                """UPDATE trading_deployments SET state='RISK_HALTED', last_halt_at=?,
                updated_at=? WHERE deployment_id=?""",
                (now, now, DEPLOYMENT_ID),
            )
            connection.execute(
                """UPDATE trading_order_intents SET status='CANCEL_REQUIRED'
                WHERE side='BUY' AND status IN ('READY','ACCEPTED','PARTIALLY_FILLED','REPRICE_READY')"""
            )
        self._sync_strategy_lifecycle(TradingState.RISK_HALTED.value)
        if self.config.live_trading_enabled:
            for intent in self.database.query(
                """SELECT i.* FROM trading_order_intents i
                JOIN trading_order_batches b ON b.batch_id=i.batch_id
                WHERE b.mode LIKE 'LIVE%' AND i.side='BUY'
                AND i.status IN ('ACCEPTED','PENDING_CONFIRMATION','PARTIALLY_FILLED')"""
            ):
                try:
                    self._cancel_intent_order(intent, reason=event_type)
                except (BrokerTransportError, TradingSafetyError):
                    self._update_intent_status(str(intent["intent_id"]), "CANCEL_UNKNOWN")

    def _block_reconciliation(self, event_type: str, details: Mapping[str, Any]) -> None:
        self._risk_halt(event_type, details)
        self._set_state(TradingState.RECONCILIATION_BLOCKED.value)

    def _set_state(self, state: str, **columns: Any) -> None:
        allowed = {"live_started_at"}
        updates = {key: value for key, value in columns.items() if key in allowed}
        updates["state"] = state
        updates["updated_at"] = _now().isoformat()
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE trading_deployments SET {assignments} WHERE deployment_id=?",
                [*updates.values(), DEPLOYMENT_ID],
            )
        self._sync_strategy_lifecycle(state)

    def _sync_strategy_lifecycle(self, state: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE strategies SET lifecycle=? WHERE strategy_id=?",
                (state, TRADE_STRATEGY_ID),
            )

    def _set_batch_status(self, batch_id: str, status: str, *, note: str = "") -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE trading_order_batches SET status=?, decided_at=?, decision_note=? WHERE batch_id=?",
                (status, _now().isoformat(), note, batch_id),
            )

    def _cancel_intent_order(self, intent: Mapping[str, Any], *, reason: str) -> bool:
        rows = self.database.query(
            """SELECT * FROM trading_broker_orders WHERE intent_id=?
            AND status IN ('ACCEPTED','PENDING_CONFIRMATION','PARTIALLY_FILLED')
            ORDER BY submitted_at DESC LIMIT 1""",
            (intent["intent_id"],),
        )
        if not rows or not rows[0].get("broker_order_id"):
            return False
        result = self.broker.cancel_order(
            code=str(intent["code"]),
            broker_order_id=str(rows[0]["broker_order_id"]),
        )
        success = int(result.get("Value") or 0) == 1
        status = "CANCEL_REQUESTED" if success else "CANCEL_UNKNOWN"
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE trading_broker_orders SET status=?, updated_at=?, response_json=?,
                error=? WHERE broker_order_row_id=?""",
                (
                    status,
                    _now().isoformat(),
                    json.dumps({**result, "reason": reason}, ensure_ascii=False, sort_keys=True),
                    "" if success else str(result.get("Msg") or "cancel failed"),
                    rows[0]["broker_order_row_id"],
                ),
            )
        self._update_intent_status(str(intent["intent_id"]), status)
        return success

    def _save_heartbeat(
        self,
        scheduler_id: str,
        moment: datetime,
        phase: str,
        status: str,
        detail: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO trading_scheduler_heartbeats
                (scheduler_id, deployment_id, heartbeat_at, phase, status, detail)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (scheduler_id, DEPLOYMENT_ID, moment.isoformat(), phase, status, detail),
            )

    def _update_intent_status(
        self,
        intent_id: str,
        status: str,
        *,
        price: float | None = None,
        attempted_at: datetime | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE trading_order_intents SET status=?,
                limit_price=COALESCE(?, limit_price),
                attempt_count=attempt_count + CASE WHEN ? IS NULL THEN 0 ELSE 1 END,
                last_attempt_at=COALESCE(?, last_attempt_at) WHERE intent_id=?""",
                (
                    status,
                    price,
                    attempted_at.isoformat() if attempted_at else None,
                    attempted_at.isoformat() if attempted_at else None,
                    intent_id,
                ),
            )

    def _latest_broker_order(self, intent_id: str) -> dict[str, Any]:
        rows = self._decoded_query(
            """SELECT * FROM trading_broker_orders WHERE intent_id=?
            ORDER BY submitted_at DESC LIMIT 1""",
            (intent_id,),
        )
        return rows[0] if rows else {}

    def _latest_reconciliation(self) -> dict[str, Any] | None:
        rows = self._decoded_query(
            """SELECT * FROM trading_reconciliations WHERE deployment_id=?
            ORDER BY captured_at DESC LIMIT 1""",
            (DEPLOYMENT_ID,),
        )
        return rows[0] if rows else None

    def _deployment(self) -> dict[str, Any]:
        rows = self.database.query(
            "SELECT * FROM trading_deployments WHERE deployment_id=?", (DEPLOYMENT_ID,)
        )
        if not rows:
            raise RuntimeError("trading deployment is not initialized")
        return dict(rows[0])

    def _batch_row(self, batch_id: str) -> dict[str, Any]:
        rows = self.database.query(
            "SELECT * FROM trading_order_batches WHERE batch_id=? AND deployment_id=?",
            (batch_id, DEPLOYMENT_ID),
        )
        if not rows:
            raise KeyError(batch_id)
        return dict(rows[0])

    def _intent_with_batch(self, intent_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        rows = self.database.query(
            """SELECT i.*, b.mode AS batch_mode, b.status AS batch_status,
            b.expires_at, b.batch_id AS parent_batch_id
            FROM trading_order_intents i JOIN trading_order_batches b ON b.batch_id=i.batch_id
            WHERE i.intent_id=? AND b.deployment_id=?""",
            (intent_id, DEPLOYMENT_ID),
        )
        if not rows:
            raise KeyError(intent_id)
        row = dict(rows[0])
        batch = self._batch_row(str(row["parent_batch_id"]))
        return row, batch

    def _decoded_query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [self._decode_row(row) for row in self.database.query(sql, params)]

    @staticmethod
    def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in list(result):
            if key.endswith("_json"):
                result[key.removesuffix("_json")] = _json_value(result.pop(key), {})
        return result


def controlled_limit_price(
    *,
    side: str,
    bid: float,
    ask: float,
    limit_up: float,
    limit_down: float,
    maximum_offset: float = 0.002,
    tick: float = 0.01,
) -> float:
    normalized = side.upper()
    if normalized not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    reference = ask if normalized == "BUY" else bid
    if reference <= 0 or limit_up <= 0 or limit_down <= 0:
        raise TradingSafetyError("latest executable quote and price limits are required")
    raw = reference * (1 + maximum_offset if normalized == "BUY" else 1 - maximum_offset)
    bounded = min(raw, limit_up) if normalized == "BUY" else max(raw, limit_down)
    ticks = math.floor(bounded / tick + 1e-9) if normalized == "BUY" else math.ceil(bounded / tick - 1e-9)
    return round(ticks * tick, 2)


def _unwrap_tdx_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and "Value" in value:
        value = value["Value"]
    if isinstance(value, dict):
        return [dict(value)]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _broker_order_is_open(order: Mapping[str, Any]) -> bool:
    cancel_flag = int(float(order.get("WtDate") or 0))
    quantity = int(abs(float(order.get("WtVol") or 0)))
    filled = int(abs(float(order.get("CJVol") or 0)))
    return cancel_flag == 0 and filled < quantity


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI_TZ)


def _elapsed_weeks(started_at: datetime | None) -> float:
    if started_at is None:
        return 0.0
    return max(0.0, (_now() - started_at.astimezone(SHANGHAI_TZ)).total_seconds() / 604800)


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, math.ceil(len(values) * probability) - 1))
    return float(values[index])


def _next_weekday(current: date, weekday: int) -> date:
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset)


__all__ = [
    "DEPLOYMENT_ID",
    "EarlyWinnerTradingService",
    "TdxTradingHttpClient",
    "TradingSafetyError",
    "TradingState",
    "controlled_limit_price",
]
