from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd

from strategy_v1.portfolio import price_limit_ratio

from research_platform.lhb import LhbFeatures, latest_lhb_features, latest_limit_features

from .course49 import Course49Market, _consecutive_limit_ups, _daily_return
from .course49_v2 import Course49V2Strategy, MarketStyle, adaptive_target_weight


CORE_CONFIRMATIONS = frozenset({"EARLY_SEAL", "STRONG_SEAL", "AUCTION_STRENGTH"})


def build_course49_v3_candidate_matrix(
    raw_bars: dict[str, pd.DataFrame],
    names: dict[str, str],
    eligibility: pd.DataFrame,
    minimum_streak: int = 2,
) -> pd.DataFrame:
    """Precompute eligible 10% stocks at or above the strategy's board height."""

    codes = [code for code in eligibility.columns if code in raw_bars]
    if not codes or eligibility.empty:
        return pd.DataFrame(index=eligibility.index, columns=codes, dtype=bool)
    close = pd.concat(
        {code: pd.to_numeric(raw_bars[code].get("Close"), errors="coerce") for code in codes},
        axis=1,
    ).reindex(index=eligibility.index, columns=codes)
    returns = close.pct_change(fill_method=None)
    allowed = pd.Series(
        {
            code: price_limit_ratio(code, names.get(code, "")) == 0.10
            and "ST" not in names.get(code, "").upper()
            for code in codes
        }
    )
    limit_up = returns.ge(0.099) & allowed
    running = pd.DataFrame(0, index=limit_up.index, columns=codes, dtype="int16")
    counts = pd.Series(0, index=codes, dtype="int16")
    for timestamp, row in limit_up.iterrows():
        counts = (counts + 1).where(row, 0)
        running.loc[timestamp] = counts
    return eligibility.reindex(index=running.index, columns=codes).fillna(False) & running.ge(
        max(1, int(minimum_streak))
    )


def select_trade_mode_v3(
    market: Course49Market,
    style: MarketStyle,
    leader: dict[str, Any],
) -> tuple[str, float] | None:
    """Select the local acceleration setup without using index style as a hard veto."""

    if market.phase != "ACCELERATION" or style.code == "UNKNOWN":
        return None
    streak = int(leader.get("streak", 0) or 0)
    rank = int(leader.get("leader_rank", 99) or 99)
    role = str(leader.get("role", ""))
    board_score = float(leader.get("board_quality_score", 0.0) or 0.0)
    behavior = leader.get("limit_behavior")
    confirmations = {
        str(item)
        for item in behavior.get("confirmations", [])
    } if isinstance(behavior, dict) else set()
    if rank != 1 or role != "SPACE_LEADER" or not confirmations & CORE_CONFIRMATIONS:
        return None
    if streak >= 4 and board_score >= 0.65:
        return "LOCAL_ACCELERATION_HIGH_BOARD", 0.20
    if streak in {2, 3} and board_score >= 0.75:
        return "LOCAL_ACCELERATION_CORE", 0.15
    return None


class Course49V3Strategy(Course49V2Strategy):
    metadata = replace(
        Course49V2Strategy.metadata,
        strategy_id="course49_v3",
        version="3.0.0",
        name="49课局部加速龙头",
        description=(
            "指数风格调节风险预算但不否决局部投机加速，只参与板块第一空间龙头的"
            "高质量二至三板确认或四板以上核心接力。"
        ),
        strategy_family="course49_v3",
    )

    def entry_allowed(self, market: Course49Market, style: MarketStyle) -> bool:
        return bool(
            market.entry_allowed
            and market.phase == "ACCELERATION"
            and style.code != "UNKNOWN"
        )

    def candidate_minimum_streak(self) -> int:
        return 2

    def candidate_limit(self) -> int:
        return 3

    def select_mode(
        self,
        market: Course49Market,
        style: MarketStyle,
        leader: dict[str, Any],
    ) -> tuple[str, float] | None:
        return select_trade_mode_v3(market, style, leader)

    def candidate_allowed(
        self,
        streak: int,
        board_quality: float,
        confirmations: set[str],
        capital_risk: str,
    ) -> bool:
        quality_ok = (
            streak >= 4 and board_quality >= 0.65
        ) or (
            streak in {2, 3} and board_quality >= 0.75
        )
        return bool(
            quality_ok
            and confirmations & CORE_CONFIRMATIONS
            and not capital_risk
        )

    def candidate_behavior_allowed(self, behavior: LhbFeatures) -> bool:
        return True

    def capital_allowed(self, capital: LhbFeatures | None) -> bool:
        return True

    def candidate_score(
        self,
        *,
        board_quality: float,
        streak: int,
        continuation_rate: float,
        capital_score: float,
        first_limit_score: float,
        historical_premium: float,
    ) -> float:
        height_score = min(1.0, streak / 5.0)
        return float(
            board_quality * 0.40
            + height_score * 0.25
            + continuation_rate * 0.10
            + capital_score * 0.10
            + first_limit_score * 0.10
            + historical_premium * 0.05
        )

    def effective_suitability(self, market: Course49Market, style: MarketStyle) -> float:
        if style.code == "DEFENSIVE" and market.phase == "ACCELERATION":
            return 0.50
        return style.suitability

    def target_weight(
        self,
        base_weight: float,
        suitability: float,
        board_quality: float,
    ) -> float:
        return adaptive_target_weight(base_weight, suitability, board_quality)

    def entry_sector_count(self) -> int:
        return 3

    def entry_sector_reason(self) -> str:
        return "GLOBAL_ACCELERATION_CORE"

    def rank_sectors(
        self,
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        sector_members: dict[str, dict[str, Any]],
        feature_matrix: dict[str, pd.DataFrame] | None = None,
        asof: pd.Timestamp | None = None,
        eligible_codes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        del (
            front_bars,
            raw_bars,
            names,
            sector_members,
            feature_matrix,
            asof,
            eligible_codes,
        )
        return []

    def leader_in_entry_scope(
        self,
        leader: dict[str, Any],
        entry_sector_codes: set[str],
    ) -> bool:
        return True

    def holding_sector_weak(
        self,
        sector_code: str,
        sector: dict[str, Any] | None,
    ) -> bool:
        if sector_code == "GLOBAL_CORE":
            return False
        return super().holding_sector_weak(sector_code, sector)

    def rank_leaders(
        self,
        sectors: list[dict[str, Any]],
        front_bars: dict[str, pd.DataFrame],
        raw_bars: dict[str, pd.DataFrame],
        names: dict[str, str],
        sector_members: dict[str, dict[str, Any]],
        limit_snapshot: dict[str, dict[str, Any]] | None = None,
        lhb_history: dict[str, dict[str, LhbFeatures]] | None = None,
    ) -> list[dict[str, Any]]:
        del limit_snapshot
        lhb_history = lhb_history or {}
        sector_by_code: dict[str, dict[str, Any]] = {}
        for sector in sectors:
            metadata = sector_members.get(str(sector["sector_code"]), {})
            for code in metadata.get("members", []):
                sector_by_code.setdefault(str(code), sector)

        rows: list[dict[str, Any]] = []
        for code, raw in raw_bars.items():
            front = front_bars.get(code)
            name = names.get(code, "")
            if (
                front is None
                or len(raw) < 21
                or price_limit_ratio(code, name) != 0.10
                or "ST" in name.upper()
            ):
                continue
            raw_close = pd.to_numeric(raw.get("Close"), errors="coerce").dropna()
            if len(raw_close) < 2 or float(raw_close.iloc[-1] / raw_close.iloc[-2] - 1.0) < 0.099:
                continue
            streak = _consecutive_limit_ups(raw, code, name)
            if streak < self.candidate_minimum_streak():
                continue
            asof = pd.Timestamp(raw.index[-1])
            behavior = latest_limit_features(lhb_history, code, asof)
            if behavior is None or not behavior.limit_event:
                continue
            capital = latest_lhb_features(lhb_history, code, asof)
            if not self.capital_allowed(capital):
                continue
            confirmations = set(behavior.board_confirmations)
            if not self.candidate_allowed(
                streak,
                behavior.board_quality_score,
                confirmations,
                str(capital.risk) if capital else "",
            ):
                continue
            if not self.candidate_behavior_allowed(behavior):
                continue
            front_close = pd.to_numeric(front.get("Close"), errors="coerce").dropna()
            volume = pd.to_numeric(front.get("Volume"), errors="coerce").reindex(
                front_close.index
            ).fillna(0.0)
            returns = raw_close.pct_change(fill_method=None)
            historical_up = returns >= 0.099
            next_returns = returns.shift(-1)[historical_up].dropna()
            historical_premium = (
                float((next_returns > 0).mean()) if not next_returns.empty else 0.5
            )
            leader_score = self.candidate_score(
                board_quality=behavior.board_quality_score,
                streak=streak,
                continuation_rate=behavior.continuation_rate,
                capital_score=capital.score if capital else 0.5,
                first_limit_score=behavior.first_limit_score,
                historical_premium=historical_premium,
            )
            sector = sector_by_code.get(code)
            rows.append(
                {
                    "code": code,
                    "name": name or code,
                    "streak": streak,
                    "first_limit_score": behavior.first_limit_score,
                    "first_limit_available": bool(behavior.first_limit_time),
                    "return_5d": _daily_return(front, 5),
                    "turnover": float((front_close * volume).tail(20).mean()),
                    "historical_premium": historical_premium,
                    "capital_score": capital.score if capital else 0.5,
                    "capital_risk": capital.risk if capital else "",
                    "lhb": capital.as_dict() if capital else {"listed": False},
                    "board_quality_score": behavior.board_quality_score,
                    "board_risk": behavior.board_risk,
                    "limit_behavior": behavior.behavior_dict(),
                    "price": float(raw_close.iloc[-1]),
                    "leader_score": float(leader_score),
                    "leader_rank": 1,
                    "role": "SPACE_LEADER",
                    "sector_code": str(sector["sector_code"])
                    if sector
                    else "GLOBAL_CORE",
                    "sector_name": str(sector["sector_name"])
                    if sector
                    else "全市场空间核心",
                    "sector_score": float(sector["score"]) if sector else 0.5,
                    "sector_limit_count": int(sector["limit_count"]) if sector else 0,
                    "sector_rank": int(sector["rank"]) if sector else 999,
                    "theme_phase": str(sector["theme_phase"])
                    if sector
                    else "GLOBAL_ACCELERATION",
                }
            )
        rows.sort(
            key=lambda item: (
                -float(item["leader_score"]),
                -int(item["streak"]),
                str(item["code"]),
            )
        )
        return rows[: self.candidate_limit()]
