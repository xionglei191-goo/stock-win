from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from .config import PlatformConfig
from .data import TdxProvider
from .portfolio import can_trade_at_open
from .storage import Database


STRATEGY_ID = "weekly_triangle_v1"
POLICY_STATUS = "HISTORICAL_REJECTED"
MINIMUM_COMPLETE_BREAKOUTS = 100
MINIMUM_BREAKOUT_COHORTS = 26
MINIMUM_RESOLVED_SETUPS = 100
MINIMUM_SETUP_COHORTS = 26
SETUP_HYPOTHESIS_ID = "price_location_high_v1"
SETUP_CONVERSION_DAYS = 35
SETUP_EPISODE_GAP_DAYS = 14
SETUP_MAXIMUM_ENTRIES = 20


class WeeklyTriangleObservationService:
    """Persist and mature observation-only weekly-triangle candidates."""

    def __init__(self, config: PlatformConfig, database: Database) -> None:
        self.config = config
        self.database = database

    def capture_and_refresh(
        self,
        *,
        run_id: str,
        strategy_version: str,
        observed_at: str | pd.Timestamp,
        candidates: Iterable[dict[str, Any]],
        bars: dict[str, pd.DataFrame],
        target_weight: float,
        maximum_entries: int = 20,
    ) -> dict[str, Any]:
        captured = self.capture(
            run_id=run_id,
            strategy_version=strategy_version,
            observed_at=observed_at,
            candidates=candidates,
            target_weight=target_weight,
            maximum_entries=maximum_entries,
        )
        refreshed = self.refresh(bars=bars)
        return {"status": "READY", "captured": captured, **refreshed}

    def capture(
        self,
        *,
        run_id: str,
        strategy_version: str,
        observed_at: str | pd.Timestamp,
        candidates: Iterable[dict[str, Any]],
        target_weight: float,
        maximum_entries: int = 20,
    ) -> int:
        observed_text = _date_text(observed_at)
        items = [dict(item) for item in candidates if isinstance(item, dict)]
        for item in items:
            stage = str(
                item.get("stage")
                or ("BREAKOUT" if item.get("breakout") else "SETUP")
            ).upper()
            if stage == "SETUP":
                price_location = _candidate_price_location(item)
                if price_location is not None:
                    item["price_location"] = price_location
        now = datetime.now().astimezone().isoformat()
        inserted = 0
        eligible_breakout_rank = 0
        with self.database.connect() as connection:
            for candidate_rank, candidate in enumerate(items, start=1):
                code = str(candidate.get("code") or "").strip()
                signal_asof = _date_text(candidate.get("asof"))
                if not code or not signal_asof:
                    continue
                stage = str(
                    candidate.get("stage")
                    or ("BREAKOUT" if candidate.get("breakout") else "SETUP")
                ).upper()
                policy_rank = None
                if stage == "BREAKOUT" and bool(candidate.get("entry_allowed")):
                    eligible_breakout_rank += 1
                    policy_rank = eligible_breakout_rank
                candidate_payload = dict(candidate)
                candidate_payload["observation_rank"] = candidate_rank
                candidate_payload["policy_rank"] = policy_rank
                candidate_payload["policy_selected"] = bool(
                    policy_rank is not None and policy_rank <= maximum_entries
                )
                candidate_payload["hypothesis_id"] = ""
                candidate_payload["hypothesis_episode_start"] = False
                candidate_payload["hypothesis_rank"] = None
                candidate_payload["hypothesis_selected"] = False
                identity = "|".join(
                    (STRATEGY_ID, strategy_version, code, stage, signal_asof)
                )
                observation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO strategy_observations
                    (observation_id, run_id, strategy_id, strategy_version, code, name,
                     stage, signal_asof, observed_at, score, entry_allowed, target_weight,
                     candidate_json, hypothesis_id, hypothesis_rank, hypothesis_selected,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        observation_id,
                        run_id,
                        STRATEGY_ID,
                        strategy_version,
                        code,
                        str(candidate.get("name") or ""),
                        stage,
                        signal_asof,
                        observed_text,
                        float(candidate.get("score") or 0.0),
                        int(bool(candidate.get("entry_allowed"))),
                        float(target_weight),
                        json.dumps(
                            candidate_payload,
                            ensure_ascii=False,
                            default=_json_default,
                        ),
                        "",
                        None,
                        0,
                        now,
                        now,
                    ),
                )
                inserted += max(0, int(cursor.rowcount))
            self._rebuild_setup_hypotheses(connection, now)
            self._refresh_setup_conversions(connection, items, observed_text)
        return inserted

    def refresh(
        self,
        *,
        bars: dict[str, pd.DataFrame] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now().astimezone().isoformat()
        with self.database.connect() as connection:
            self._rebuild_setup_hypotheses(connection, now)
        pending = self.database.query(
            """SELECT * FROM strategy_observations
            WHERE strategy_id=? AND status IN ('PENDING', 'PARTIAL')
            ORDER BY observed_at, code""",
            (STRATEGY_ID,),
        )
        if not pending:
            return {"evaluated": 0, "remaining": 0, "data_asof": None}

        if bars is None:
            codes = sorted({str(row["code"]) for row in pending})
            with TdxProvider(self.config, __file__) as provider:
                bars = provider.fetch_bars(
                    codes,
                    "1d",
                    180,
                    fields=("Open", "High", "Low", "Close"),
                    dividend_type="none",
                )

        evaluated = 0
        data_days: list[pd.Timestamp] = []
        with self.database.connect() as connection:
            for row in pending:
                frame = bars.get(str(row["code"]))
                if frame is None or frame.empty:
                    continue
                normalized = _normalize_bars(frame)
                if not normalized.empty:
                    data_days.append(pd.Timestamp(normalized.index[-1]).normalize())
                outcome = self._evaluate(row, normalized)
                assignments = [f"{key}=?" for key in outcome]
                connection.execute(
                    f"""UPDATE strategy_observations SET
                    {', '.join(assignments)}, updated_at=? WHERE observation_id=?""",
                    [*outcome.values(), now, row["observation_id"]],
                )
                evaluated += 1

        remaining = self.database.query(
            """SELECT COUNT(*) AS count FROM strategy_observations
            WHERE strategy_id=? AND status IN ('PENDING', 'PARTIAL')""",
            (STRATEGY_ID,),
        )[0]["count"]
        return {
            "evaluated": evaluated,
            "remaining": int(remaining),
            "data_asof": max(data_days).date().isoformat() if data_days else None,
        }

    def summary(self, limit: int = 200) -> dict[str, Any]:
        all_rows = self.database.query(
            """SELECT * FROM strategy_observations WHERE strategy_id=?
            ORDER BY observed_at DESC, score DESC, code LIMIT ?""",
            (STRATEGY_ID, limit),
        )
        rows = [_decode_row(row) for row in all_rows]
        aggregate_rows = self.database.query(
            """SELECT * FROM strategy_observations WHERE strategy_id=?
            ORDER BY signal_asof, code""",
            (STRATEGY_ID,),
        )
        setup_episodes = [
            row
            for row in aggregate_rows
            if row["stage"] == "SETUP"
            and bool(_candidate_payload(row).get("hypothesis_episode_start"))
        ]
        selected_setups = [
            row for row in setup_episodes if bool(row["hypothesis_selected"])
        ]
        baseline_setups: list[dict[str, Any]] = []
        setup_cohorts = sorted({str(row["signal_asof"]) for row in setup_episodes})
        for cohort in setup_cohorts:
            cohort_rows = [
                row for row in setup_episodes if str(row["signal_asof"]) == cohort
            ]
            baseline_setups.extend(
                sorted(
                    cohort_rows,
                    key=lambda row: (-float(row["score"]), str(row["code"])),
                )[:SETUP_MAXIMUM_ENTRIES]
            )
        selected_conversion = _conversion_summary(selected_setups)
        baseline_conversion = _conversion_summary(baseline_setups)
        setup_ready = (
            selected_conversion["resolved_samples"] >= MINIMUM_RESOLVED_SETUPS
            and selected_conversion["resolved_cohorts"] >= MINIMUM_SETUP_COHORTS
        )
        counts = {
            "total": len(aggregate_rows),
            "pending": sum(row["status"] == "PENDING" for row in aggregate_rows),
            "partial": sum(row["status"] == "PARTIAL" for row in aggregate_rows),
            "complete": sum(row["status"] == "COMPLETE" for row in aggregate_rows),
            "unfilled": sum(row["status"] == "UNFILLED" for row in aggregate_rows),
            "blocked": sum(row["status"] == "BLOCKED" for row in aggregate_rows),
            "excluded": sum(row["status"] == "EXCLUDED" for row in aggregate_rows),
            "setup_pending": sum(
                row["conversion_status"] == "PENDING" for row in setup_episodes
            ),
            "setup_converted": sum(
                row["conversion_status"] == "CONVERTED" for row in setup_episodes
            ),
            "setup_not_converted": sum(
                row["conversion_status"] == "NOT_CONVERTED"
                for row in setup_episodes
            ),
        }
        aggregates = []
        for entry_allowed in (False, True):
            selected = [
                row
                for row in aggregate_rows
                if row["stage"] == "BREAKOUT"
                and bool(row["entry_allowed"]) == entry_allowed
                and row["status"] == "COMPLETE"
            ]
            if not selected:
                continue
            aggregates.append(
                _aggregate_summary(
                    selected,
                    stage="BREAKOUT",
                    entry_allowed=entry_allowed,
                    selection="ALL_CANDIDATES",
                )
            )

        qualified_breakouts = [
            row
            for row in aggregate_rows
            if row["stage"] == "BREAKOUT"
            and bool(row["entry_allowed"])
            and row["status"] == "COMPLETE"
            and bool(_candidate_payload(row).get("policy_selected"))
        ]
        breakout_cohorts = len({str(row["signal_asof"]) for row in qualified_breakouts})
        if qualified_breakouts:
            aggregates.append(
                _aggregate_summary(
                    qualified_breakouts,
                    stage="BREAKOUT",
                    entry_allowed=True,
                    selection="POLICY_TOP20",
                )
            )
        ready = (
            len(qualified_breakouts) >= MINIMUM_COMPLETE_BREAKOUTS
            and breakout_cohorts >= MINIMUM_BREAKOUT_COHORTS
        )
        return {
            "strategy_id": STRATEGY_ID,
            "policy_status": POLICY_STATUS,
            "counts": counts,
            "aggregates": aggregates,
            "setup_hypothesis": {
                "hypothesis_id": SETUP_HYPOTHESIS_ID,
                "lifecycle": "RESEARCH_ONLY",
                "status": "READY_FOR_RESEARCH_REVIEW" if setup_ready else "COLLECTING",
                "automatic_live_entry": False,
                "episode_gap_days": SETUP_EPISODE_GAP_DAYS,
                "conversion_days": SETUP_CONVERSION_DAYS,
                "maximum_setups_per_cohort": SETUP_MAXIMUM_ENTRIES,
                "selected_setup_samples": selected_conversion["sample_size"],
                "converted_selected_setup_samples": selected_conversion[
                    "converted_samples"
                ],
                "selected": selected_conversion,
                "score_baseline": baseline_conversion,
                "conversion_rate_lift": (
                    float(
                        selected_conversion["conversion_rate"]
                        - baseline_conversion["conversion_rate"]
                    )
                    if selected_conversion["conversion_rate"] is not None
                    and baseline_conversion["conversion_rate"] is not None
                    else None
                ),
                "minimum_resolved_samples": MINIMUM_RESOLVED_SETUPS,
                "minimum_resolved_cohorts": MINIMUM_SETUP_COHORTS,
            },
            "forward_gate": {
                "status": "READY_FOR_RESEARCH_REVIEW" if ready else "COLLECTING",
                "automatic_live_entry": False,
                "complete_policy_breakouts": len(qualified_breakouts),
                "breakout_cohorts": breakout_cohorts,
                "minimum_complete_policy_breakouts": MINIMUM_COMPLETE_BREAKOUTS,
                "minimum_breakout_cohorts": MINIMUM_BREAKOUT_COHORTS,
            },
            "rows": rows,
        }

    def _evaluate(
        self,
        row: dict[str, Any],
        frame: pd.DataFrame,
    ) -> dict[str, Any]:
        base = {
            "status": "PENDING",
            "executable": 0,
            "block_reason": "",
            "entry_time": None,
            "entry_price": None,
            "return_5d": None,
            "return_20d": None,
            "mae_20d": None,
            "mfe_20d": None,
            "evaluation_json": "{}",
            "conversion_status": "NOT_APPLICABLE",
            "converted_at": None,
            "conversion_days": None,
            "conversion_json": "{}",
        }
        if str(row["stage"]) == "SETUP":
            return self._evaluate_setup(row, frame)

        if frame.empty or any(column not in frame for column in ("Open", "High", "Low", "Close")):
            return base | {"status": "BLOCKED", "block_reason": "MISSING_BARS"}

        observed_day = pd.Timestamp(row["observed_at"])
        if observed_day.tzinfo is not None:
            observed_day = observed_day.tz_localize(None)
        observed_day = observed_day.normalize()
        frame_days = pd.DatetimeIndex(frame.index).normalize()
        visible = frame.loc[frame_days <= observed_day]
        future = frame.loc[frame_days > observed_day]
        if visible.empty:
            return base | {"status": "BLOCKED", "block_reason": "MISSING_OBSERVATION_CLOSE"}
        if future.empty:
            return base | {
                "block_reason": "NEXT_SESSION_NOT_AVAILABLE",
                "evaluation_json": json.dumps(
                    {
                        "available_sessions": 0,
                        "data_asof": pd.Timestamp(frame.index[-1]).date().isoformat(),
                    }
                ),
            }

        previous_close = _finite_float(visible.iloc[-1]["Close"])
        entry_open = _finite_float(future.iloc[0]["Open"])
        if previous_close is None or entry_open is None:
            return base | {"status": "BLOCKED", "block_reason": "INVALID_ENTRY_PRICES"}
        if not can_trade_at_open(
            str(row["code"]), "BUY", entry_open, previous_close, str(row.get("name") or "")
        ):
            return base | {
                "status": "UNFILLED",
                "block_reason": "NEXT_OPEN_NOT_TRADABLE",
                "evaluation_json": json.dumps(
                    {
                        "available_sessions": len(future),
                        "previous_close": previous_close,
                        "entry_open": entry_open,
                        "data_asof": pd.Timestamp(frame.index[-1]).date().isoformat(),
                    }
                ),
            }

        costs = self.config.portfolio
        entry_price = entry_open * (1.0 + costs.slippage_rate)
        target_cash = costs.initial_cash * float(row["target_weight"])
        quantity = int(target_cash // (entry_price * costs.board_lot)) * costs.board_lot
        if quantity <= 0:
            return base | {"status": "UNFILLED", "block_reason": "INSUFFICIENT_TARGET_CASH"}
        entry_value = entry_price * quantity
        entry_fee = max(costs.min_commission, entry_value * costs.commission_rate)
        forward = future.iloc[:20]
        return_5d = self._net_return(forward, 5, entry_value, entry_fee, quantity)
        return_20d = self._net_return(forward, 20, entry_value, entry_fee, quantity)
        complete = len(forward) >= 20
        lows = pd.to_numeric(forward["Low"], errors="coerce").dropna()
        highs = pd.to_numeric(forward["High"], errors="coerce").dropna()
        details: dict[str, Any] = {
            "available_sessions": len(forward),
            "entry_open": entry_open,
            "previous_close": previous_close,
            "quantity": quantity,
            "entry_fee": entry_fee,
            "data_asof": pd.Timestamp(frame.index[-1]).date().isoformat(),
            "cost_model": {
                "slippage_rate": costs.slippage_rate,
                "commission_rate": costs.commission_rate,
                "min_commission": costs.min_commission,
                "stamp_duty_rate": costs.stamp_duty_rate,
            },
        }
        if len(forward) >= 5:
            details["exit_5d"] = pd.Timestamp(forward.index[4]).date().isoformat()
        if complete:
            details["exit_20d"] = pd.Timestamp(forward.index[19]).date().isoformat()
        return base | {
            "status": "COMPLETE" if complete else "PARTIAL",
            "executable": 1,
            "entry_time": pd.Timestamp(forward.index[0]).isoformat(),
            "entry_price": entry_price,
            "return_5d": return_5d,
            "return_20d": return_20d,
            "mae_20d": (
                float(lows.min() / entry_price - 1.0)
                if complete and not lows.empty
                else None
            ),
            "mfe_20d": (
                float(highs.max() / entry_price - 1.0)
                if complete and not highs.empty
                else None
            ),
            "evaluation_json": json.dumps(details, ensure_ascii=False),
        }

    def _net_return(
        self,
        future: pd.DataFrame,
        horizon: int,
        entry_value: float,
        entry_fee: float,
        quantity: int,
    ) -> float | None:
        if len(future) < horizon:
            return None
        close = _finite_float(future.iloc[horizon - 1]["Close"])
        if close is None:
            return None
        costs = self.config.portfolio
        exit_price = close * (1.0 - costs.slippage_rate)
        exit_value = exit_price * quantity
        exit_fee = max(
            costs.min_commission,
            exit_value * costs.commission_rate,
        ) + exit_value * costs.stamp_duty_rate
        return float((exit_value - exit_fee - entry_value - entry_fee) / (entry_value + entry_fee))

    def _rebuild_setup_hypotheses(self, connection: Any, updated_at: str) -> None:
        rows = connection.execute(
            """SELECT observation_id, code, signal_asof, score, status,
            conversion_status, candidate_json, conversion_json
            FROM strategy_observations
            WHERE strategy_id=? AND stage='SETUP'
            ORDER BY code, signal_asof, strategy_version""",
            (STRATEGY_ID,),
        ).fetchall()
        if not rows:
            return

        records: list[dict[str, Any]] = []
        previous_by_code: dict[str, pd.Timestamp] = {}
        for row in rows:
            signal_day = pd.Timestamp(row["signal_asof"])
            code = str(row["code"])
            previous = previous_by_code.get(code)
            episode_start = (
                previous is None
                or signal_day - previous > pd.Timedelta(days=SETUP_EPISODE_GAP_DAYS)
            )
            previous_by_code[code] = signal_day
            try:
                payload = json.loads(str(row["candidate_json"] or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            price_location = _candidate_price_location(payload)
            if price_location is not None:
                payload["price_location"] = price_location
            records.append(
                {
                    "row": row,
                    "payload": payload,
                    "signal_day": signal_day,
                    "episode_start": episode_start,
                    "price_location": price_location,
                    "hypothesis_rank": None,
                    "hypothesis_selected": False,
                }
            )

        cohorts: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            if record["episode_start"] and record["price_location"] is not None:
                cohorts.setdefault(record["signal_day"].date().isoformat(), []).append(record)
        for cohort in cohorts.values():
            ordered = sorted(
                cohort,
                key=lambda record: (
                    -float(record["price_location"]),
                    str(record["row"]["code"]),
                ),
            )
            for rank, record in enumerate(ordered, start=1):
                record["hypothesis_rank"] = rank
                record["hypothesis_selected"] = rank <= SETUP_MAXIMUM_ENTRIES

        for record in records:
            row = record["row"]
            payload = record["payload"]
            episode_start = bool(record["episode_start"])
            hypothesis_id = SETUP_HYPOTHESIS_ID if episode_start else ""
            payload.update(
                {
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_episode_start": episode_start,
                    "hypothesis_rank": record["hypothesis_rank"],
                    "hypothesis_selected": bool(record["hypothesis_selected"]),
                }
            )
            existing_conversion = str(row["conversion_status"] or "")
            resolved = existing_conversion in {"CONVERTED", "NOT_CONVERTED"}
            if episode_start:
                conversion_status = existing_conversion if resolved else "PENDING"
                status = "COMPLETE" if resolved else str(row["status"])
                if status not in {"PENDING", "PARTIAL", "COMPLETE"} or (
                    status == "COMPLETE" and not resolved
                ):
                    status = "PENDING"
                conversion_json = str(row["conversion_json"] or "{}")
            else:
                conversion_status = "NOT_APPLICABLE"
                status = "EXCLUDED"
                conversion_json = "{}"
            connection.execute(
                """UPDATE strategy_observations SET
                status=?, executable=0, block_reason='', entry_time=NULL,
                entry_price=NULL, return_5d=NULL, return_20d=NULL,
                mae_20d=NULL, mfe_20d=NULL, evaluation_json='{}',
                candidate_json=?, hypothesis_id=?, hypothesis_rank=?,
                hypothesis_selected=?, conversion_status=?,
                converted_at=CASE WHEN ? THEN converted_at ELSE NULL END,
                conversion_days=CASE WHEN ? THEN conversion_days ELSE NULL END,
                conversion_json=?, updated_at=? WHERE observation_id=?""",
                (
                    status,
                    json.dumps(payload, ensure_ascii=False, default=_json_default),
                    hypothesis_id,
                    record["hypothesis_rank"],
                    int(record["hypothesis_selected"]),
                    conversion_status,
                    int(resolved and episode_start),
                    int(resolved and episode_start),
                    conversion_json,
                    updated_at,
                    row["observation_id"],
                ),
            )

    def _refresh_setup_conversions(
        self,
        connection: Any,
        candidates: list[dict[str, Any]],
        observed_text: str,
    ) -> None:
        observed_day = pd.Timestamp(observed_text)
        breakout_days_by_code: dict[str, list[pd.Timestamp]] = {}
        for item in candidates:
            stage = str(
                item.get("stage")
                or ("BREAKOUT" if item.get("breakout") else "SETUP")
            ).upper()
            if stage != "BREAKOUT":
                continue
            code = str(item.get("code") or "").strip()
            breakout_text = _date_text(item.get("asof"))
            if code and breakout_text:
                breakout_days_by_code.setdefault(code, []).append(
                    pd.Timestamp(breakout_text)
                )

        for code, breakout_days in breakout_days_by_code.items():
            setup_rows = connection.execute(
                """SELECT observation_id, signal_asof FROM strategy_observations
                WHERE strategy_id=? AND code=? AND stage='SETUP'
                  AND conversion_status='PENDING'
                ORDER BY signal_asof""",
                (STRATEGY_ID, code),
            ).fetchall()
            ordered_breakouts = sorted(set(breakout_days))
            for setup_row in setup_rows:
                setup_day = pd.Timestamp(setup_row["signal_asof"])
                breakout_day = next(
                    (
                        day
                        for day in ordered_breakouts
                        if setup_day < day
                        and day <= setup_day + pd.Timedelta(days=SETUP_CONVERSION_DAYS)
                    ),
                    None,
                )
                if breakout_day is None:
                    continue
                conversion_days = int((breakout_day - setup_day).days)
                connection.execute(
                    """UPDATE strategy_observations SET
                    status='COMPLETE', block_reason='',
                    conversion_status='CONVERTED', converted_at=?, conversion_days=?,
                    conversion_json=?, updated_at=? WHERE observation_id=?""",
                    (
                        breakout_day.date().isoformat(),
                        conversion_days,
                        json.dumps(
                            {
                                "breakout_asof": breakout_day.date().isoformat(),
                                "conversion_days": conversion_days,
                                "hypothesis_id": SETUP_HYPOTHESIS_ID,
                            },
                            ensure_ascii=False,
                        ),
                        datetime.now().astimezone().isoformat(),
                        setup_row["observation_id"],
                    ),
                )

        expiry_day = observed_day - pd.Timedelta(days=SETUP_CONVERSION_DAYS)
        expired_rows = connection.execute(
            """SELECT observation_id FROM strategy_observations
            WHERE strategy_id=? AND stage='SETUP'
              AND conversion_status='PENDING'
              AND signal_asof <= ?""",
            (STRATEGY_ID, expiry_day.date().isoformat()),
        ).fetchall()
        for row in expired_rows:
            connection.execute(
                """UPDATE strategy_observations SET
                status='COMPLETE', block_reason='',
                conversion_status='NOT_CONVERTED', conversion_days=?,
                conversion_json=?, updated_at=?
                WHERE observation_id=?""",
                (
                    SETUP_CONVERSION_DAYS,
                    json.dumps(
                        {
                            "conversion_days": SETUP_CONVERSION_DAYS,
                            "data_asof": observed_text,
                        },
                        ensure_ascii=False,
                    ),
                    datetime.now().astimezone().isoformat(),
                    row["observation_id"],
                ),
            )

    def _evaluate_setup(
        self,
        row: dict[str, Any],
        frame: pd.DataFrame,
    ) -> dict[str, Any]:
        base = {
            "status": "PENDING",
            "executable": 0,
            "block_reason": "",
            "entry_time": None,
            "entry_price": None,
            "return_5d": None,
            "return_20d": None,
            "mae_20d": None,
            "mfe_20d": None,
            "evaluation_json": "{}",
            "conversion_status": "PENDING",
            "converted_at": None,
            "conversion_days": None,
            "conversion_json": "{}",
        }
        if frame.empty:
            return base | {"block_reason": "MISSING_BARS"}
        observed_day = pd.Timestamp(row["observed_at"])
        if observed_day.tzinfo is not None:
            observed_day = observed_day.tz_localize(None)
        observed_day = observed_day.normalize()
        days = pd.DatetimeIndex(frame.index).normalize()
        future = frame.loc[days > observed_day]
        if future.empty:
            return base | {
                "conversion_status": "PENDING",
                "conversion_json": json.dumps(
                    {"available_sessions": 0, "data_asof": pd.Timestamp(frame.index[-1]).date().isoformat()}
                ),
            }
        # A SETUP converts only when a later weekly BREAKOUT is observable.
        # This deliberate placeholder is updated from future scan candidates
        # when the next completed weekly scan sees the same code break out.
        return base | {
            "status": "PARTIAL",
            "conversion_status": "PENDING",
            "conversion_json": json.dumps(
                {
                    "available_sessions": len(future),
                    "data_asof": pd.Timestamp(frame.index[-1]).date().isoformat(),
                }
            ),
        }


def _normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy().sort_index()
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index))
    if result.index.tz is not None:
        result.index = result.index.tz_localize(None)
    return result


def _date_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp.date().isoformat()


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _candidate_price_location(candidate: dict[str, Any]) -> float | None:
    direct = _finite_number(candidate.get("price_location"))
    if direct is not None:
        return direct
    close = _finite_float(candidate.get("close"))
    upper = _finite_float(
        candidate.get("upper_boundary", candidate.get("upper"))
    )
    lower = _finite_float(
        candidate.get("lower_boundary", candidate.get("lower"))
    )
    if close is None or upper is None or lower is None or upper <= lower:
        return None
    return float((close - lower) / (upper - lower))


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("candidate_json", "evaluation_json", "conversion_json"):
        try:
            result[key.removesuffix("_json")] = json.loads(str(result.pop(key) or "{}"))
        except json.JSONDecodeError:
            result[key.removesuffix("_json")] = {}
    result["entry_allowed"] = bool(result.get("entry_allowed"))
    result["executable"] = bool(result.get("executable"))
    result["hypothesis_selected"] = bool(result.get("hypothesis_selected"))
    return result


def _candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(row.get("candidate_json") or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _aggregate_summary(
    rows: list[dict[str, Any]],
    *,
    stage: str,
    entry_allowed: bool,
    selection: str,
) -> dict[str, Any]:
    returns_5d = [
        float(row["return_5d"])
        for row in rows
        if row["return_5d"] is not None
    ]
    returns_20d = [
        float(row["return_20d"])
        for row in rows
        if row["return_20d"] is not None
    ]
    return {
        "stage": stage,
        "entry_allowed": entry_allowed,
        "selection": selection,
        "sample_size": len(rows),
        "cohort_count": len({str(row["signal_asof"]) for row in rows}),
        "average_return_5d": _mean(returns_5d),
        "median_return_5d": _median(returns_5d),
        "win_rate_5d": _win_rate(returns_5d),
        "average_return_20d": _mean(returns_20d),
        "median_return_20d": _median(returns_20d),
        "win_rate_20d": _win_rate(returns_20d),
    }


def _conversion_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [
        row
        for row in rows
        if row["conversion_status"] in {"CONVERTED", "NOT_CONVERTED"}
    ]
    converted = [
        row for row in resolved if row["conversion_status"] == "CONVERTED"
    ]
    return {
        "sample_size": len(rows),
        "cohort_count": len({str(row["signal_asof"]) for row in rows}),
        "resolved_samples": len(resolved),
        "resolved_cohorts": len({str(row["signal_asof"]) for row in resolved}),
        "pending_samples": sum(
            row["conversion_status"] == "PENDING" for row in rows
        ),
        "converted_samples": len(converted),
        "not_converted_samples": sum(
            row["conversion_status"] == "NOT_CONVERTED" for row in resolved
        ),
        "conversion_rate": (
            float(len(converted) / len(resolved)) if resolved else None
        ),
    }


def _mean(values: list[float]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _win_rate(values: list[float]) -> float | None:
    return sum(value > 0 for value in values) / len(values) if values else None
