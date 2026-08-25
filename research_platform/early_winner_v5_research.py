from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from .config import PlatformConfig
from .early_winner_research import ResearchDataBlockedError
from .early_winner_v4_research import (
    HOLDING_TRADING_DAYS,
    NON_OVERLAP_PHASES,
    PORTFOLIO_SIZE,
    RETURN_COLUMN,
    _assert_v4_pair_alignment,
    _evaluate_v4_pair,
    _worst_phase_excess,
    prepare_v4_labels,
)
from .models import StrategyCategory
from .storage import Database
from .strategies.early_winner import HARD_NEGATIVE_EVENT_TYPES
from .strategies.early_winner_v5 import EarlyWinnerV5Strategy


PROJECT_ID = "early_winner_v5"
STRATEGY_ID = "early_winner_event_quiet_v5"
PROJECT_VERSION = "5.0.0-preregistered"
PROTOCOL_VERSION = "early-winner-v5-event-quiet-v1"
EVENT_REPLAY_SCHEMA_VERSION = "early-winner-v5-event-replay-v1"
DESIGN_YEARS = tuple(range(2018, 2024))
FROZEN_VALIDATION_YEARS = (2024, 2025)
OBSERVATION_YEARS = (2026,)
MINIMUM_PHASE_PERIODS = 3
MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR = 2
MINIMUM_PHASE_INVESTED_PERIODS_COMBINED = 4
MAXIMUM_DRAWDOWN_GAP = 0.03
MAXIMUM_INDUSTRY_CANDIDATES = 5

EVENT_TYPE_SCORES: dict[str, float] = {
    "CLARIFICATION": -3.0,
    "REDUCTION": -2.0,
    "RISK_WARNING": -1.0,
    "NONE": 0.0,
    "UNCLASSIFIED": 0.0,
    "BUYBACK": 1.0,
    "EXPANSION": 1.0,
    "CONTROL_CHANGE": 2.0,
    "MAJOR_ORDER": 2.0,
    "PRICE_INCREASE": 2.0,
    "ACQUISITION": 3.0,
    "EARNINGS_FORECAST": 3.0,
}

EVENT_CLASSIFIER_SPEC: dict[str, Any] = {
    "version": "early-winner-v5-deterministic-event-classifier-v1",
    "scores": EVENT_TYPE_SCORES,
    "hard_negative_types": sorted(HARD_NEGATIVE_EVENT_TYPES),
    "hard_negative_priority": True,
    "selection_order": [
        "hard_negative_first",
        "score",
        "effective_at_desc",
        "event_hash_asc",
    ],
}

REQUIRED_EVENT_RECORD_FIELDS = (
    "event_hash",
    "source_url",
    "event_type",
    "event_score",
    "published_at",
    "effective_at",
)
REQUIRED_EVENT_PROVENANCE_COLUMNS = (
    "selected_event_hash",
    "selected_event_type",
    "selected_event_score",
    "selected_event_published_at",
    "selected_event_effective_at",
    "event_window_start",
    "event_window_end",
    "classifier_rule_hash",
    "all_event_hashes",
    "hard_negative_event_hashes",
    "event_replay_records",
    "event_replay_hash",
)
FROZEN_REQUIRED_GATES = (
    "preregistration",
    "historical_universe_master",
    "event_provenance",
    "trading_calendar",
    "execution_status",
    "label_snapshot",
    "frozen_snapshot",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


CLASSIFIER_RULE_HASH = _hash_payload(EVENT_CLASSIFIER_SPEC)

PROTOCOL_SPEC: dict[str, Any] = {
    "protocol_version": PROTOCOL_VERSION,
    "project_id": PROJECT_ID,
    "lifecycle": "RESEARCH_ONLY",
    "design_years": list(DESIGN_YEARS),
    "frozen_validation_years": list(FROZEN_VALIDATION_YEARS),
    "observation_years": list(OBSERVATION_YEARS),
    "candidate_rule": {
        "selected_event_score_strictly_positive": True,
        "hard_negative_blocks_new_position": True,
        "sort": [
            "selected_event_score_desc",
            "amount_ratio_asc",
            "selected_event_effective_at_desc",
            "code_asc",
        ],
        "portfolio_size": PORTFOLIO_SIZE,
        "maximum_per_industry": MAXIMUM_INDUSTRY_CANDIDATES,
        "unfilled_slots": "CASH_NO_REFILL",
        "rank_before_entry_executable": True,
    },
    "evaluation": {
        "holding_trading_days": HOLDING_TRADING_DAYS,
        "non_overlap_phases": NON_OVERLAP_PHASES,
        "return_policy": "EIGHT_PHASE_NON_OVERLAPPING_FULL_EXIT_REBUILD",
        "paired_cycle_policy": "JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY",
        "cost_policy": "20BPS_ROUND_TRIP_PER_FILLED_SLOT; DOUBLE=40BPS",
        "drawdown_policy": "CYCLE_ENDPOINT_NAV_INCLUDING_INITIAL_1.0",
        "baseline": "RS60",
    },
    "sample_gate": {
        "minimum_phase_periods_per_year": MINIMUM_PHASE_PERIODS,
        "minimum_invested_periods_per_phase_per_year": (
            MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR
        ),
        "minimum_invested_periods_per_phase_combined": (
            MINIMUM_PHASE_INVESTED_PERIODS_COMBINED
        ),
    },
    "performance_gate": {
        "precision_at_20_strictly_above_rs60_each_year": True,
        "pr_auc_strictly_above_rs60_each_year": True,
        "worst_phase_double_cost_return_positive_each_year": True,
        "worst_same_phase_double_cost_excess_positive_each_year": True,
        "maximum_drawdown_gap": MAXIMUM_DRAWDOWN_GAP,
    },
    "event_replay_schema": EVENT_REPLAY_SCHEMA_VERSION,
    "classifier_rule_hash": CLASSIFIER_RULE_HASH,
    "protocol_change_policy": "ANY_CHANGE_REQUIRES_V6",
    "promotion_allowed": False,
}
PROTOCOL_HASH = _hash_payload(PROTOCOL_SPEC)


class FrozenValidationSealedError(ResearchDataBlockedError):
    """Raised before a frozen-year reader is called when any gate is closed."""


class V5ProtocolChangeRequiresV6(ValueError):
    """V5 is immutable; changed rules must be registered as a new project."""


def _as_list(value: Any, field: str) -> list[Any]:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSON") from exc
        if isinstance(decoded, list):
            return decoded
    raise ValueError(f"{field} must be a list")


def _timestamp(value: Any, field: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{field} is missing")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("Asia/Shanghai").tz_localize(None)
    return timestamp


def _timestamp_text(value: Any, field: str) -> str:
    return _timestamp(value, field).isoformat()


def _hash_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip().lower()
    if allow_empty and not text:
        return ""
    if not _SHA256_PATTERN.fullmatch(text):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return text


def _normalize_event_record(
    raw: Mapping[str, Any],
    *,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> dict[str, Any]:
    missing = [field for field in REQUIRED_EVENT_RECORD_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"event record missing fields: {','.join(missing)}")
    event_hash = _hash_text(raw.get("event_hash"), "event_hash")
    source_url = str(raw.get("source_url") or "").strip()
    parsed_url = urlparse(source_url)
    source_host = str(parsed_url.hostname or "").lower()
    if parsed_url.scheme != "https" or not (
        source_host == "cninfo.com.cn" or source_host.endswith(".cninfo.com.cn")
    ):
        raise ValueError("event source_url must be an official HTTPS cninfo URL")
    event_type = str(raw.get("event_type") or "").strip().upper()
    if event_type not in EVENT_TYPE_SCORES:
        raise ValueError(f"unsupported event_type: {event_type}")
    score = float(raw.get("event_score"))
    if not np.isfinite(score) or score != EVENT_TYPE_SCORES[event_type]:
        raise ValueError(f"event score does not match classifier: {event_type}")
    published = _timestamp(raw.get("published_at"), "published_at")
    effective = _timestamp(raw.get("effective_at"), "effective_at")
    if effective < published:
        raise ValueError("event effective_at precedes published_at")
    if effective < window_start or effective > window_end:
        raise ValueError("event effective_at is outside the frozen 30-day window")
    if published > window_end:
        raise ValueError("event was published after the decision boundary")
    return {
        "event_hash": event_hash,
        "source_url": source_url,
        "event_type": event_type,
        "event_score": score,
        "published_at": published.isoformat(),
        "effective_at": effective.isoformat(),
    }


def _selected_event(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not records:
        return None
    hard_negative = [
        item for item in records if str(item["event_type"]) in HARD_NEGATIVE_EVENT_TYPES
    ]
    if hard_negative:
        return sorted(
            hard_negative,
            key=lambda item: (
                float(item["event_score"]),
                -_timestamp(item["effective_at"], "effective_at").value,
                str(item["event_hash"]),
            ),
        )[0]
    return sorted(
        records,
        key=lambda item: (
            -float(item["event_score"]),
            -_timestamp(item["effective_at"], "effective_at").value,
            str(item["event_hash"]),
        ),
    )[0]


def replay_event_provenance(
    events: Iterable[Mapping[str, Any]], asof: str | pd.Timestamp
) -> dict[str, Any]:
    """Build the only accepted row-level event audit payload for V5."""

    decision_day = _timestamp(asof, "asof").normalize()
    window_start = decision_day - pd.Timedelta(days=30)
    window_end = decision_day + pd.Timedelta(hours=15)
    records = [
        _normalize_event_record(
            dict(item), window_start=window_start, window_end=window_end
        )
        for item in events
    ]
    hashes = [str(item["event_hash"]) for item in records]
    if len(hashes) != len(set(hashes)):
        raise ValueError("event hashes are not unique within the decision window")
    records = sorted(records, key=lambda item: str(item["event_hash"]))
    selected = _selected_event(records)
    hard_hashes = sorted(
        str(item["event_hash"])
        for item in records
        if str(item["event_type"]) in HARD_NEGATIVE_EVENT_TYPES
    )
    selected_payload = (
        {
            "selected_event_hash": str(selected["event_hash"]),
            "selected_event_type": str(selected["event_type"]),
            "selected_event_score": float(selected["event_score"]),
            "selected_event_published_at": str(selected["published_at"]),
            "selected_event_effective_at": str(selected["effective_at"]),
        }
        if selected is not None
        else {
            "selected_event_hash": "",
            "selected_event_type": "NONE",
            "selected_event_score": 0.0,
            "selected_event_published_at": "",
            "selected_event_effective_at": "",
        }
    )
    payload: dict[str, Any] = {
        **selected_payload,
        "event_window_start": window_start.isoformat(),
        "event_window_end": window_end.isoformat(),
        "classifier_rule_hash": CLASSIFIER_RULE_HASH,
        "all_event_hashes": sorted(hashes),
        "hard_negative_event_hashes": hard_hashes,
        "event_replay_records": records,
    }
    payload["event_replay_hash"] = _hash_payload(
        {
            "schema": EVENT_REPLAY_SCHEMA_VERSION,
            "classifier_rule_hash": CLASSIFIER_RULE_HASH,
            "event_window_start": payload["event_window_start"],
            "event_window_end": payload["event_window_end"],
            "all_event_hashes": payload["all_event_hashes"],
            "hard_negative_event_hashes": hard_hashes,
            "event_replay_records": records,
            "selected": selected_payload,
        }
    )
    return payload


def validate_event_provenance(frame: pd.DataFrame) -> dict[str, Any]:
    missing = sorted(set(REQUIRED_EVENT_PROVENANCE_COLUMNS) - set(frame.columns))
    if frame.empty:
        return {
            "ready": False,
            "status": "EMPTY",
            "detail": "V5 event provenance contains no decision rows",
            "missing_columns": missing,
            "error_count": 1,
            "errors": ["empty frame"],
        }
    if missing:
        return {
            "ready": False,
            "status": "SCHEMA_INCOMPLETE",
            "detail": "V5 event provenance schema is incomplete",
            "missing_columns": missing,
            "error_count": len(missing),
            "errors": [f"missing column: {column}" for column in missing[:20]],
        }
    duplicate_count = int(frame.duplicated(["asof", "code"]).sum())
    errors: list[str] = []
    replay_hashes: list[tuple[str, str, str]] = []
    for index, row in frame.iterrows():
        grain = f"{row.get('asof')}:{row.get('code')}"
        try:
            classifier_hash = _hash_text(
                row.get("classifier_rule_hash"), "classifier_rule_hash"
            )
            if classifier_hash != CLASSIFIER_RULE_HASH:
                raise ValueError("classifier_rule_hash differs from the V5 preregistration")
            records = _as_list(row.get("event_replay_records"), "event_replay_records")
            replayed = replay_event_provenance(records, row.get("asof"))
            for field in (
                "selected_event_hash",
                "selected_event_type",
                "selected_event_score",
                "selected_event_published_at",
                "selected_event_effective_at",
                "event_window_start",
                "event_window_end",
                "classifier_rule_hash",
                "all_event_hashes",
                "hard_negative_event_hashes",
                "event_replay_hash",
            ):
                actual = row.get(field)
                expected = replayed[field]
                if field in {"all_event_hashes", "hard_negative_event_hashes"}:
                    actual = _as_list(actual, field)
                elif field == "selected_event_score":
                    actual = float(actual)
                elif field.endswith("_at") or field.startswith("event_window_"):
                    if str(actual or "") or str(expected or ""):
                        actual = _timestamp_text(actual, field)
                        expected = _timestamp_text(expected, field)
                else:
                    actual = str(actual or "")
                    expected = str(expected or "")
                if actual != expected:
                    raise ValueError(f"{field} does not reproduce from event records")
            replay_hashes.append(
                (str(row.get("asof")), str(row.get("code")), replayed["event_replay_hash"])
            )
        except (TypeError, ValueError, KeyError) as exc:
            errors.append(f"{grain} row {index}: {exc}")
    if duplicate_count:
        errors.append(f"duplicate (asof, code) rows: {duplicate_count}")
    ready = not errors
    return {
        "ready": ready,
        "status": "READY" if ready else "REPLAY_REJECTED",
        "detail": (
            "Every event selection uniquely replays from frozen content hashes"
            if ready
            else "At least one event selection cannot be uniquely replayed"
        ),
        "schema_version": EVENT_REPLAY_SCHEMA_VERSION,
        "classifier_rule_hash": CLASSIFIER_RULE_HASH,
        "rows": int(len(frame)),
        "duplicate_grain_rows": duplicate_count,
        "error_count": int(len(errors)),
        "errors": errors[:20],
        "replay_snapshot_hash": _hash_payload(replay_hashes) if ready else "",
        "missing_columns": [],
    }


def historical_universe_master_gate(
    value: Mapping[str, Any] | None, *, through_year: int
) -> dict[str, Any]:
    gate = dict(value) if isinstance(value, Mapping) else {}
    required = {
        "ready",
        "status",
        "snapshot_id",
        "manifest_hash",
        "protocol_version",
        "coverage_start",
        "coverage_end",
        "promotion_blocked",
    }
    missing = sorted(required - set(gate))
    errors: list[str] = []
    if missing:
        errors.append(f"missing gate fields: {','.join(missing)}")
    if gate.get("ready") is not True:
        errors.append("master quality gate is not ready")
    if gate.get("promotion_blocked") is True:
        errors.append("master explicitly blocks promotion")
    try:
        _hash_text(gate.get("manifest_hash"), "manifest_hash")
    except ValueError as exc:
        errors.append(str(exc))
    try:
        start = _timestamp(gate.get("coverage_start"), "coverage_start")
        end = _timestamp(gate.get("coverage_end"), "coverage_end")
        if start > pd.Timestamp("2018-01-01"):
            errors.append("master coverage begins after the V5 design boundary")
        if end < pd.Timestamp(through_year, 12, 31):
            errors.append(f"master coverage does not extend through {through_year}")
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
    ready = not errors
    return {
        **gate,
        "ready": ready,
        "status": str(gate.get("status") or ("READY" if ready else "NOT_BUILT")),
        "detail": (
            str(gate.get("detail") or "historical universe master is ready")
            if ready
            else "; ".join(errors)
        ),
        "required_through_year": int(through_year),
        "errors": errors,
        "promotion_blocked": not ready,
    }


def read_historical_universe_master_gate(runtime_dir: Path) -> dict[str, Any]:
    """Use the master store's verified pointer -> manifest -> quality gate chain."""

    try:
        from .historical_security_master import load_historical_universe_master_gate

        payload = load_historical_universe_master_gate(Path(runtime_dir))
        return historical_universe_master_gate(payload, through_year=2023)
    except ImportError:
        pass
    except (OSError, ValueError, ResearchDataBlockedError) as exc:
        payload = {
            "ready": False,
            "status": "MASTER_AUDIT_FAILED",
            "detail": str(exc),
            "snapshot_id": "",
            "manifest_hash": "",
            "protocol_version": "",
            "coverage_start": "",
            "coverage_end": "",
            "promotion_blocked": True,
        }
        return historical_universe_master_gate(payload, through_year=2023)

    # Compatibility during deployment of the platform store. Never trust the
    # pointer as a quality gate; without the verified store, remain blocked.
    path = Path(runtime_dir) / "security_master" / "current.json"
    if not path.exists():
        detail = "data/security_master/current.json does not exist"
        status = "NOT_BUILT"
    else:
        detail = "historical security master store verifier is unavailable"
        status = "MASTER_VERIFIER_UNAVAILABLE"
    return historical_universe_master_gate(
        {
            "ready": False,
            "status": status,
            "detail": detail,
            "snapshot_id": "",
            "manifest_hash": "",
            "protocol_version": "",
            "coverage_start": "",
            "coverage_end": "",
            "promotion_blocked": True,
        },
        through_year=2023,
    )


def _hard_negative_present(row: Mapping[str, Any]) -> bool:
    try:
        return bool(_as_list(row.get("hard_negative_event_hashes"), "hard_negative_event_hashes"))
    except ValueError:
        return True


def _candidate_sort(frame: pd.DataFrame) -> pd.DataFrame:
    ranked = frame.copy()
    ranked["_v5_event_score"] = pd.to_numeric(
        ranked["selected_event_score"], errors="coerce"
    )
    ranked["_v5_amount_ratio"] = pd.to_numeric(
        ranked["amount_ratio"], errors="coerce"
    )
    ranked["_v5_event_effective"] = pd.to_datetime(
        ranked["selected_event_effective_at"], errors="coerce"
    )
    return ranked.sort_values(
        [
            "_v5_event_score",
            "_v5_amount_ratio",
            "_v5_event_effective",
            "code",
        ],
        ascending=[False, True, False, True],
        kind="mergesort",
    )


def select_v5_candidates(
    frame: pd.DataFrame,
    *,
    maximum_candidates: int = PORTFOLIO_SIZE,
    maximum_per_industry: int = MAXIMUM_INDUSTRY_CANDIDATES,
) -> pd.DataFrame:
    """Select from decision-time fields only; entry_executable is intentionally ignored."""

    if frame.empty:
        return frame.copy()
    if frame["asof"].astype(str).nunique() != 1:
        raise ValueError("select_v5_candidates requires exactly one decision date")
    gate = validate_event_provenance(frame)
    if not gate["ready"]:
        raise ResearchDataBlockedError(gate["detail"] + ": " + "; ".join(gate["errors"]))
    score = pd.to_numeric(frame["selected_event_score"], errors="coerce")
    amount = pd.to_numeric(frame["amount_ratio"], errors="coerce")
    hard_negative = frame.apply(_hard_negative_present, axis=1)
    eligible = (score > 0.0) & amount.notna() & np.isfinite(amount) & ~hard_negative
    if "v4_eligible" in frame:
        eligible &= frame["v4_eligible"].fillna(False).astype(bool)
    elif "decision_eligible" in frame:
        eligible &= frame["decision_eligible"].fillna(False).astype(bool)
    ranked = _candidate_sort(frame.loc[eligible])
    positions: list[Any] = []
    industry_counts: dict[str, int] = {}
    for index, row in ranked.iterrows():
        industry = str(row.get("industry") or "UNCLASSIFIED")
        if industry_counts.get(industry, 0) >= maximum_per_industry:
            continue
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        positions.append(index)
        if len(positions) >= maximum_candidates:
            break
    selected = ranked.loc[positions].copy()
    selected["v5_rank"] = np.arange(1, len(selected) + 1, dtype=int)
    return selected.drop(
        columns=["_v5_event_score", "_v5_amount_ratio", "_v5_event_effective"],
        errors="ignore",
    )


def _assert_design_years(frame: pd.DataFrame, *, require_all: bool) -> None:
    years = set(pd.to_datetime(frame["asof"], errors="coerce").dt.year.dropna().astype(int))
    forbidden = sorted(years - set(DESIGN_YEARS))
    if forbidden:
        raise ResearchDataBlockedError(
            f"V5 design code cannot read years outside 2018-2023: {forbidden}"
        )
    if require_all and years != set(DESIGN_YEARS):
        raise ResearchDataBlockedError(
            f"V5 design snapshot must contain exactly 2018-2023; got {sorted(years)}"
        )


def prepare_v5_design_frame(
    frame: pd.DataFrame, *, require_all_design_years: bool = True
) -> pd.DataFrame:
    _assert_design_years(frame, require_all=require_all_design_years)
    provenance = validate_event_provenance(frame)
    if not provenance["ready"]:
        raise ResearchDataBlockedError(
            provenance["detail"] + ": " + "; ".join(provenance["errors"])
        )
    data = frame.copy()
    if "v4_eligible" not in data or "target" not in data:
        data = prepare_v4_labels(data)
    required = {
        "asof",
        "code",
        "industry",
        "amount_ratio",
        "relative_return_60",
        "v4_eligible",
        "target",
        "entry_executable",
        RETURN_COLUMN,
        "planned_entry_time",
        "planned_exit_time",
        "exit_time",
    }
    missing = sorted(required - set(data.columns))
    if missing:
        raise ResearchDataBlockedError(f"V5 design frame missing: {','.join(missing)}")
    data["year"] = pd.to_datetime(data["asof"], errors="coerce").dt.year
    if "evaluation_period" not in data:
        if "label_window_matured" in data:
            mature = data.groupby("asof")["label_window_matured"].transform("any")
        else:
            mature = data.groupby("asof")[RETURN_COLUMN].transform(
                lambda values: values.notna().any()
            )
        data["evaluation_period"] = mature.fillna(False).astype(bool)
    data["v5_evaluation_eligible"] = data["v4_eligible"].fillna(False).astype(bool)
    hard_negative = data.apply(_hard_negative_present, axis=1)
    event_score = pd.to_numeric(data["selected_event_score"], errors="coerce")
    amount = pd.to_numeric(data["amount_ratio"], errors="coerce")
    event_effective = pd.to_datetime(
        data["selected_event_effective_at"], errors="coerce"
    )
    data["v5_candidate_eligible"] = (
        data["v5_evaluation_eligible"]
        & (event_score > 0.0)
        & amount.notna()
        & np.isfinite(amount)
        & event_effective.notna()
        & ~hard_negative
    )
    data["v5_selection_score"] = np.nan
    data["v5_metric_score"] = np.nan
    for _, group in data.groupby("asof", sort=False):
        base = group.loc[group["v5_evaluation_eligible"]].copy()
        if base.empty:
            continue
        candidates = _candidate_sort(base.loc[base["v5_candidate_eligible"]])
        if not candidates.empty:
            values = np.arange(len(candidates), 0, -1, dtype=float)
            data.loc[candidates.index, "v5_selection_score"] = values
            data.loc[candidates.index, "v5_metric_score"] = values
        # Excluded names are all the same cash/non-candidate state. Ranking
        # them by negative events or other factors would silently introduce a
        # second, unregistered strategy into PR-AUC and IC.
        non_candidates = base.index.difference(candidates.index)
        data.loc[non_candidates, "v5_metric_score"] = 0.0
    return data


def _full_pool_metrics(frame: pd.DataFrame, score_column: str) -> dict[str, float]:
    from sklearn.metrics import average_precision_score

    mask = (
        frame["evaluation_period"].fillna(False).astype(bool)
        & frame["v5_evaluation_eligible"].fillna(False).astype(bool)
        & pd.to_numeric(frame["target"], errors="coerce").notna()
        & pd.to_numeric(frame[score_column], errors="coerce").notna()
    )
    labeled = frame.loc[mask]
    target = pd.to_numeric(labeled["target"], errors="coerce").astype(int)
    score = pd.to_numeric(labeled[score_column], errors="coerce")
    pr_auc = (
        float(average_precision_score(target, score))
        if len(labeled) and target.nunique() > 1
        else 0.0
    )
    weekly_ic: list[float] = []
    for _, group in labeled.groupby("asof", sort=True):
        if group[score_column].nunique() > 1 and group[RETURN_COLUMN].nunique() > 1:
            value = group[score_column].corr(group[RETURN_COLUMN], method="spearman")
            if pd.notna(value):
                weekly_ic.append(float(value))
    return {
        "pr_auc": pr_auc,
        "ic": float(np.mean(weekly_ic)) if weekly_ic else 0.0,
        "ranking_rows": int(len(labeled)),
        "ranking_weeks": int(labeled["asof"].nunique()),
    }


def evaluate_v5_pair(frame: pd.DataFrame) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate the locked V5 rule and RS60 on V4's paired eight-phase ledger."""

    prepared = prepare_v5_design_frame(frame, require_all_design_years=False)
    candidate, baseline = _evaluate_v4_pair(
        prepared,
        candidate_score_column="v5_selection_score",
        baseline_score_column="relative_return_60",
        eligibility_column="v5_evaluation_eligible",
    )
    candidate.update(_full_pool_metrics(prepared, "v5_metric_score"))
    candidate["worst_phase_total_return_excess"] = _worst_phase_excess(
        candidate, baseline, "total_return"
    )
    candidate["worst_phase_double_cost_return_excess"] = _worst_phase_excess(
        candidate, baseline, "double_cost_return"
    )
    candidate["worst_phase_drawdown_gap"] = _worst_phase_excess(
        candidate, baseline, "max_drawdown"
    )
    candidate["selection_rule"] = list(PROTOCOL_SPEC["candidate_rule"]["sort"])
    candidate["protocol_hash"] = PROTOCOL_HASH
    candidate["lifecycle"] = "RESEARCH_ONLY"
    baseline["baseline"] = "RS60"
    baseline["protocol_hash"] = PROTOCOL_HASH
    baseline["lifecycle"] = "RESEARCH_ONLY"
    return candidate, baseline


def run_v5_design_audit(
    frame: pd.DataFrame,
    *,
    source_hash: str,
    historical_master: Mapping[str, Any],
) -> dict[str, Any]:
    master = historical_universe_master_gate(historical_master, through_year=2023)
    if not master["ready"]:
        raise ResearchDataBlockedError(master["detail"])
    prepared = prepare_v5_design_frame(frame, require_all_design_years=True)
    provenance = validate_event_provenance(prepared)
    yearly: dict[str, Any] = {}
    for year in DESIGN_YEARS:
        candidate, baseline = evaluate_v5_pair(prepared.loc[prepared["year"] == year])
        yearly[str(year)] = {"candidate": candidate, "baseline": baseline}
    return {
        "project_id": PROJECT_ID,
        "status": "DESIGN_ONLY",
        "lifecycle": "RESEARCH_ONLY",
        "protocol_version": PROTOCOL_VERSION,
        "protocol_hash": PROTOCOL_HASH,
        "source_hash": str(source_hash),
        "design_years": list(DESIGN_YEARS),
        "frozen_validation_years": list(FROZEN_VALIDATION_YEARS),
        "frozen_validation_opened": False,
        "observation_years": list(OBSERVATION_YEARS),
        "promotion_allowed": False,
        "historical_universe_master": master,
        "event_provenance": provenance,
        "yearly": yearly,
    }


def frozen_validation_readiness(
    gates: Mapping[str, Any], *, protocol_hash: str = PROTOCOL_HASH
) -> dict[str, Any]:
    if protocol_hash != PROTOCOL_HASH:
        return {
            "ready": False,
            "status": "V6_REQUIRED",
            "detail": "V5 protocol changed after preregistration; create V6",
            "missing_gates": [],
        }
    missing = [name for name in FROZEN_REQUIRED_GATES if name not in gates]
    failures: list[str] = []
    preregistration = (
        dict(gates["preregistration"])
        if isinstance(gates.get("preregistration"), Mapping)
        else {}
    )
    if preregistration.get("protocol_hash") != PROTOCOL_HASH:
        failures.append("preregistration protocol hash is absent or changed")
    if preregistration.get("protocol_version") != PROTOCOL_VERSION:
        failures.append("preregistration protocol version is absent or changed")
    if preregistration.get("ready") is not True:
        failures.append("preregistration is not ready")
    master = historical_universe_master_gate(
        gates.get("historical_universe_master"), through_year=2025
    )
    if not master["ready"]:
        failures.append("historical_universe_master is not ready through 2025")
    for name in (
        "event_provenance",
        "trading_calendar",
        "execution_status",
        "label_snapshot",
        "frozen_snapshot",
    ):
        gate = gates.get(name)
        if not isinstance(gate, Mapping) or gate.get("ready") is not True:
            failures.append(f"{name} is not ready")
    provenance = (
        dict(gates["event_provenance"])
        if isinstance(gates.get("event_provenance"), Mapping)
        else {}
    )
    if provenance.get("schema_version") != EVENT_REPLAY_SCHEMA_VERSION:
        failures.append("event_provenance schema is absent or changed")
    if provenance.get("classifier_rule_hash") != CLASSIFIER_RULE_HASH:
        failures.append("event_provenance classifier hash is absent or changed")
    immutable_hashes = {
        "event_provenance": (provenance, "snapshot_hash"),
        "trading_calendar": (
            dict(gates["trading_calendar"])
            if isinstance(gates.get("trading_calendar"), Mapping)
            else {},
            "content_hash",
        ),
        "execution_status": (
            dict(gates["execution_status"])
            if isinstance(gates.get("execution_status"), Mapping)
            else {},
            "content_hash",
        ),
        "label_snapshot": (
            dict(gates["label_snapshot"])
            if isinstance(gates.get("label_snapshot"), Mapping)
            else {},
            "snapshot_hash",
        ),
    }
    for name, (gate, field) in immutable_hashes.items():
        try:
            _hash_text(gate.get(field), f"{name}.{field}")
        except ValueError as exc:
            failures.append(str(exc))
    label_snapshot = (
        dict(gates["label_snapshot"])
        if isinstance(gates.get("label_snapshot"), Mapping)
        else {}
    )
    if label_snapshot.get("return_column") != RETURN_COLUMN:
        failures.append(f"label_snapshot must use {RETURN_COLUMN}")
    frozen = (
        dict(gates["frozen_snapshot"])
        if isinstance(gates.get("frozen_snapshot"), Mapping)
        else {}
    )
    try:
        frozen_years = tuple(int(year) for year in frozen.get("years", ()))
    except (TypeError, ValueError):
        frozen_years = ()
    if frozen_years != FROZEN_VALIDATION_YEARS:
        failures.append("frozen snapshot must contain exactly 2024 and 2025")
    if frozen.get("sealed") is not True:
        failures.append("frozen snapshot is not sealed")
    if frozen.get("protocol_hash") != PROTOCOL_HASH:
        failures.append("frozen snapshot protocol hash is absent or changed")
    try:
        _hash_text(frozen.get("manifest_hash"), "frozen_snapshot.manifest_hash")
    except ValueError as exc:
        failures.append(str(exc))
    errors = [*missing, *failures]
    return {
        "ready": not errors,
        "status": "READY_TO_OPEN_ONCE" if not errors else "SEALED",
        "detail": (
            "All immutable V5 gates are ready; an explicit one-time open may proceed"
            if not errors
            else "; ".join(errors)
        ),
        "missing_gates": missing,
        "failures": failures,
        "protocol_hash": PROTOCOL_HASH,
        "frozen_years": list(FROZEN_VALIDATION_YEARS),
        "historical_universe_master": master,
    }


def load_frozen_validation_shards(
    shards: Mapping[int, Any],
    *,
    gates: Mapping[str, Any],
    protocol_hash: str = PROTOCOL_HASH,
    reader: Callable[[Any], pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """The only V5 frozen loader. It checks every gate before calling reader."""

    readiness = frozen_validation_readiness(gates, protocol_hash=protocol_hash)
    if readiness["status"] == "V6_REQUIRED":
        raise V5ProtocolChangeRequiresV6(readiness["detail"])
    if not readiness["ready"]:
        raise FrozenValidationSealedError(readiness["detail"])
    years = tuple(sorted(int(year) for year in shards))
    if years != FROZEN_VALIDATION_YEARS:
        raise FrozenValidationSealedError(
            "V5 frozen reader requires exactly one 2024 and one 2025 shard"
        )
    load = reader or pd.read_parquet
    frames: list[pd.DataFrame] = []
    for year in FROZEN_VALIDATION_YEARS:
        frame = load(shards[year])
        observed = set(
            pd.to_datetime(frame["asof"], errors="coerce").dt.year.dropna().astype(int)
        )
        if observed != {year}:
            raise ResearchDataBlockedError(
                f"frozen shard declared as {year} contains years {sorted(observed)}"
            )
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _phase_map(metrics: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    return {
        int(item["phase"]): item
        for item in metrics.get("phase_metrics", [])
        if isinstance(item, Mapping) and "phase" in item
    }


def assess_v5_frozen_validation(
    candidate_yearly: Mapping[int | str, Mapping[str, Any]],
    baseline_yearly: Mapping[int | str, Mapping[str, Any]],
    *,
    protocol_hash: str = PROTOCOL_HASH,
) -> dict[str, Any]:
    """Apply the immutable V5 sample and performance gates after the one-time run."""

    if protocol_hash != PROTOCOL_HASH:
        raise V5ProtocolChangeRequiresV6(
            "V5 protocol changed after preregistration; create V6"
        )
    candidates = {int(year): value for year, value in candidate_yearly.items()}
    baselines = {int(year): value for year, value in baseline_yearly.items()}
    if set(candidates) != set(FROZEN_VALIDATION_YEARS) or set(baselines) != set(
        FROZEN_VALIDATION_YEARS
    ):
        raise ValueError("V5 validation requires exactly 2024 and 2025 metrics")
    sample_failures: list[str] = []
    performance_failures: list[str] = []
    combined_candidate = {phase: 0 for phase in range(NON_OVERLAP_PHASES)}
    combined_baseline = {phase: 0 for phase in range(NON_OVERLAP_PHASES)}
    for year in FROZEN_VALIDATION_YEARS:
        candidate = candidates[year]
        baseline = baselines[year]
        for label, metrics in (("candidate", candidate), ("baseline", baseline)):
            if metrics.get("protocol_hash") != PROTOCOL_HASH:
                sample_failures.append(
                    f"{year} {label}: protocol hash is absent or changed"
                )
            if metrics.get("return_policy") != (
                "EIGHT_PHASE_NON_OVERLAPPING_FULL_EXIT_REBUILD"
            ):
                sample_failures.append(f"{year} {label}: return policy changed")
            if metrics.get("paired_cycle_policy") != (
                "JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY"
            ):
                sample_failures.append(f"{year} {label}: paired cycle policy changed")
            if metrics.get("unfilled_slot_policy") != "CASH_NO_REFILL":
                sample_failures.append(f"{year} {label}: cash/no-refill policy changed")
            if metrics.get("cost_policy") != (
                "20BPS_ROUND_TRIP_PER_FILLED_SLOT; DOUBLE=40BPS"
            ):
                sample_failures.append(f"{year} {label}: cost policy changed")
            if metrics.get("drawdown_policy") != (
                "CYCLE_ENDPOINT_NAV_INCLUDING_INITIAL_1.0"
            ):
                sample_failures.append(f"{year} {label}: drawdown policy changed")
        try:
            _assert_v4_pair_alignment(candidate, baseline)
        except ResearchDataBlockedError as exc:
            sample_failures.append(f"{year}: paired cycle alignment failed: {exc}")
        candidate_phases = _phase_map(candidate)
        baseline_phases = _phase_map(baseline)
        if set(candidate_phases) != set(range(NON_OVERLAP_PHASES)) or set(
            baseline_phases
        ) != set(range(NON_OVERLAP_PHASES)):
            sample_failures.append(f"{year}: all eight phases are required")
            continue
        for phase in range(NON_OVERLAP_PHASES):
            c_phase = candidate_phases[phase]
            b_phase = baseline_phases[phase]
            for label, values in (("candidate", c_phase), ("baseline", b_phase)):
                periods = int(values.get("periods", 0))
                invested = int(values.get("invested_periods", 0))
                cycles = values.get("cycles", [])
                if not isinstance(cycles, list) or len(cycles) != periods:
                    sample_failures.append(
                        f"{year} phase {phase} {label}: periods do not match frozen cycles"
                    )
                if invested < 0 or invested > periods:
                    sample_failures.append(
                        f"{year} phase {phase} {label}: invested periods are inconsistent"
                    )
                if isinstance(cycles, list) and sum(
                    int(item.get("filled_slots", 0)) > 0
                    for item in cycles
                    if isinstance(item, Mapping)
                ) != invested:
                    sample_failures.append(
                        f"{year} phase {phase} {label}: invested periods do not match cycles"
                    )
                if periods < MINIMUM_PHASE_PERIODS:
                    sample_failures.append(
                        f"{year} phase {phase} {label}: fewer than {MINIMUM_PHASE_PERIODS} periods"
                    )
                if invested < (
                    MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR
                ):
                    sample_failures.append(
                        f"{year} phase {phase} {label}: fewer than "
                        f"{MINIMUM_PHASE_INVESTED_PERIODS_PER_YEAR} invested periods"
                    )
            combined_candidate[phase] += int(c_phase.get("invested_periods", 0))
            combined_baseline[phase] += int(b_phase.get("invested_periods", 0))
            candidate_double = float(c_phase.get("double_cost_return", 0.0))
            baseline_double = float(b_phase.get("double_cost_return", 0.0))
            candidate_drawdown = float(c_phase.get("max_drawdown", -1.0))
            baseline_drawdown = float(b_phase.get("max_drawdown", -1.0))
            if not all(
                np.isfinite(value)
                for value in (
                    candidate_double,
                    baseline_double,
                    candidate_drawdown,
                    baseline_drawdown,
                )
            ):
                performance_failures.append(
                    f"{year} phase {phase}: non-finite portfolio metric"
                )
            elif candidate_double <= 0.0:
                performance_failures.append(
                    f"{year} phase {phase}: double-cost return is not positive"
                )
            if np.isfinite(candidate_double) and np.isfinite(baseline_double) and (
                candidate_double <= baseline_double
            ):
                performance_failures.append(
                    f"{year} phase {phase}: double-cost return did not beat paired RS60"
                )
            if np.isfinite(candidate_drawdown) and np.isfinite(baseline_drawdown) and (
                candidate_drawdown < baseline_drawdown - MAXIMUM_DRAWDOWN_GAP
            ):
                performance_failures.append(
                    f"{year} phase {phase}: drawdown is worse than RS60 by more than 3pp"
                )
        candidate_precision = float(candidate.get("precision_at_20", np.nan))
        baseline_precision = float(baseline.get("precision_at_20", np.nan))
        candidate_pr_auc = float(candidate.get("pr_auc", np.nan))
        baseline_pr_auc = float(baseline.get("pr_auc", np.nan))
        if not all(
            np.isfinite(value) and 0.0 <= value <= 1.0
            for value in (
                candidate_precision,
                baseline_precision,
                candidate_pr_auc,
                baseline_pr_auc,
            )
        ):
            performance_failures.append(f"{year}: invalid ranking metric")
        elif candidate_precision <= baseline_precision:
            performance_failures.append(f"{year}: Precision@20 did not beat RS60")
        if np.isfinite(candidate_pr_auc) and np.isfinite(baseline_pr_auc) and (
            candidate_pr_auc <= baseline_pr_auc
        ):
            performance_failures.append(f"{year}: PR-AUC did not beat RS60")
        derived_summaries = {
            "candidate worst_phase_double_cost_return": min(
                float(item.get("double_cost_return", np.nan))
                for item in candidate_phases.values()
            ),
            "baseline worst_phase_double_cost_return": min(
                float(item.get("double_cost_return", np.nan))
                for item in baseline_phases.values()
            ),
            "candidate worst_phase_max_drawdown": min(
                float(item.get("max_drawdown", np.nan))
                for item in candidate_phases.values()
            ),
            "baseline worst_phase_max_drawdown": min(
                float(item.get("max_drawdown", np.nan))
                for item in baseline_phases.values()
            ),
        }
        for name, derived in derived_summaries.items():
            owner, field = name.split(" ", 1)
            metrics = candidate if owner == "candidate" else baseline
            try:
                reported = float(metrics[field])
            except (KeyError, TypeError, ValueError):
                reported = np.nan
            if not (
                np.isfinite(reported)
                and np.isfinite(derived)
                and np.isclose(reported, derived, rtol=0.0, atol=1e-12)
            ):
                performance_failures.append(f"{year}: inconsistent {name}")
    for phase in range(NON_OVERLAP_PHASES):
        if combined_candidate[phase] < MINIMUM_PHASE_INVESTED_PERIODS_COMBINED:
            sample_failures.append(
                f"combined phase {phase} candidate: fewer than "
                f"{MINIMUM_PHASE_INVESTED_PERIODS_COMBINED} invested periods"
            )
        if combined_baseline[phase] < MINIMUM_PHASE_INVESTED_PERIODS_COMBINED:
            sample_failures.append(
                f"combined phase {phase} baseline: fewer than "
                f"{MINIMUM_PHASE_INVESTED_PERIODS_COMBINED} invested periods"
            )
    if sample_failures:
        status = "INCONCLUSIVE_SAMPLE"
    elif performance_failures:
        status = "VALIDATION_REJECTED"
    else:
        status = "OBSERVATION_ONLY"
    return {
        "project_id": PROJECT_ID,
        "status": status,
        "lifecycle": "RESEARCH_ONLY",
        "protocol_hash": PROTOCOL_HASH,
        "sample_gate_passed": not sample_failures,
        "performance_gate_passed": not performance_failures,
        "sample_failures": sample_failures,
        "performance_failures": performance_failures,
        "combined_candidate_invested_periods": combined_candidate,
        "combined_baseline_invested_periods": combined_baseline,
        "promotion_allowed": False,
        "trade_signals_enabled": False,
        "failure_policy": "ANY_CHANGE_REQUIRES_V6",
    }


class EarlyWinnerV5ResearchService:
    """Project metadata and fail-closed gates; no frozen validation is run here."""

    def __init__(self, config: PlatformConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.strategy = EarlyWinnerV5Strategy()
        current = self.database.query(
            "SELECT status, data_asof, data_gates_json FROM research_projects WHERE project_id=?",
            (PROJECT_ID,),
        )
        status = str(current[0]["status"]) if current else "BLOCKED_DATA"
        data_asof = str(current[0].get("data_asof") or "") or None if current else None
        gates = {}
        if current:
            try:
                gates = json.loads(str(current[0].get("data_gates_json") or "{}"))
            except json.JSONDecodeError:
                gates = {}
        self.database.upsert_research_project(
            project_id=PROJECT_ID,
            version=PROJECT_VERSION,
            name=self.strategy.metadata.name,
            description=self.strategy.metadata.description,
            status=status,
            data_asof=data_asof,
            data_gates=gates,
        )
        # Even a manually altered row cannot turn this preregistration into a
        # deployable strategy. V5 has no promotion transition by design.
        self.database.execute(
            """UPDATE research_projects
            SET category='research_project', lifecycle='RESEARCH_ONLY'
            WHERE project_id=?""",
            (PROJECT_ID,),
        )

    def detail(self) -> dict[str, Any]:
        rows = self.database.query(
            "SELECT * FROM research_projects WHERE project_id=?", (PROJECT_ID,)
        )
        if not rows:
            raise KeyError(PROJECT_ID)
        project = dict(rows[0])
        try:
            stored_gates = json.loads(str(project.pop("data_gates_json", "{}")))
        except json.JSONDecodeError:
            stored_gates = {}
        master = read_historical_universe_master_gate(self.config.runtime_dir)
        gates = {
            **stored_gates,
            "historical_universe_master": master,
            "preregistration": {
                "ready": True,
                "protocol_version": PROTOCOL_VERSION,
                "protocol_hash": PROTOCOL_HASH,
                "change_policy": "ANY_CHANGE_REQUIRES_V6",
            },
        }
        project.update(
            {
                "data_gates": gates,
                "status": (
                    str(project.get("status") or "BLOCKED_DATA")
                    if master["ready"]
                    else "BLOCKED_DATA"
                ),
                "strategy": {
                    "strategy_id": STRATEGY_ID,
                    "version": PROJECT_VERSION,
                    "name": self.strategy.metadata.name,
                    "category": StrategyCategory.RESEARCH_PROJECT.value,
                    "lifecycle": "RESEARCH_ONLY",
                    "scan_enabled": False,
                    "backtest_enabled": False,
                },
                "protocol": PROTOCOL_SPEC,
                "protocol_hash": PROTOCOL_HASH,
                "design_years": list(DESIGN_YEARS),
                "frozen_validation_years": list(FROZEN_VALIDATION_YEARS),
                "observation_years": list(OBSERVATION_YEARS),
                "frozen_validation_opened": False,
                "candidate_generation_enabled": False,
                "trade_signals_enabled": False,
                "promotion_allowed": False,
            }
        )
        return project


__all__ = [
    "CLASSIFIER_RULE_HASH",
    "DESIGN_YEARS",
    "EarlyWinnerV5ResearchService",
    "EarlyWinnerV5Strategy",
    "EVENT_REPLAY_SCHEMA_VERSION",
    "FROZEN_VALIDATION_YEARS",
    "FrozenValidationSealedError",
    "OBSERVATION_YEARS",
    "PROJECT_ID",
    "PROTOCOL_HASH",
    "PROTOCOL_SPEC",
    "PROTOCOL_VERSION",
    "REQUIRED_EVENT_PROVENANCE_COLUMNS",
    "STRATEGY_ID",
    "V5ProtocolChangeRequiresV6",
    "assess_v5_frozen_validation",
    "evaluate_v5_pair",
    "frozen_validation_readiness",
    "historical_universe_master_gate",
    "load_frozen_validation_shards",
    "prepare_v5_design_frame",
    "read_historical_universe_master_gate",
    "replay_event_provenance",
    "run_v5_design_audit",
    "select_v5_candidates",
    "validate_event_provenance",
]
