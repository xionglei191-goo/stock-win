from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .indicators import percentile_rank
from .models import LeaderCandidate, MarketState, SectorScore


def _valid_close(frame: pd.DataFrame, minimum: int = 1) -> pd.Series | None:
    if "Close" not in frame.columns:
        return None
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    return close if len(close) >= minimum else None


def filter_universe(
    bars: dict[str, pd.DataFrame],
    names: dict[str, str],
    config: StrategyConfig,
) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for code, frame in bars.items():
        name = names.get(code, "")
        if "ST" in name.upper() or "退" in name:
            continue
        close = _valid_close(frame, config.minimum_listing_bars)
        if close is None or "Volume" not in frame.columns:
            continue
        volume = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0.0)
        if volume.iloc[-1] <= 0:
            continue
        if "Amount" in frame.columns and frame["Amount"].notna().any():
            amount = pd.to_numeric(frame["Amount"], errors="coerce")
            # The platform data hub normalizes Amount to CNY; direct TQ calls use ten-thousand CNY.
            scale = 1.0 if frame.attrs.get("amount_unit") == "CNY" else 10_000.0
            average_turnover = float(amount.tail(20).mean()) * scale
        else:
            average_turnover = float((close * volume.reindex(close.index)).tail(20).mean())
        if not np.isfinite(average_turnover) or average_turnover < config.minimum_average_turnover:
            continue
        result[code] = frame
    return result


def evaluate_market_regime(
    index_bars: pd.DataFrame,
    universe_bars: dict[str, pd.DataFrame],
    config: StrategyConfig,
) -> MarketState:
    index_close = _valid_close(index_bars, 20)
    if index_close is None:
        raise ValueError("At least 20 index bars are required")
    index_condition = bool(index_close.iloc[-1] > index_close.tail(20).mean())

    above_ma: list[bool] = []
    returns: list[float] = []
    for frame in universe_bars.values():
        close = _valid_close(frame, 21)
        if close is None:
            continue
        above_ma.append(bool(close.iloc[-1] > close.tail(20).mean()))
        if close.iloc[-6] > 0:
            returns.append(float(close.iloc[-1] / close.iloc[-6] - 1.0))
    breadth = float(np.mean(above_ma)) if above_ma else 0.0
    average_return = float(np.mean(returns)) if returns else -1.0
    passed = sum(
        (
            index_condition,
            breadth >= config.market_breadth_floor,
            average_return >= config.market_return_floor,
        )
    )
    return MarketState(
        asof=pd.Timestamp(index_close.index[-1]).to_pydatetime(),
        regime="NORMAL" if passed >= 2 else "WEAK",
        index_above_ma20=index_condition,
        breadth=breadth,
        average_return_5d=average_return,
        passed_conditions=passed,
    )


def rank_sectors(
    sector_members: dict[str, dict[str, Any]],
    daily_bars: dict[str, pd.DataFrame],
    config: StrategyConfig,
) -> list[SectorScore]:
    market_returns = []
    for frame in daily_bars.values():
        close = _valid_close(frame, 21)
        if close is not None and close.iloc[-6] > 0:
            market_returns.append(float(close.iloc[-1] / close.iloc[-6] - 1.0))
    market_return = float(np.mean(market_returns)) if market_returns else 0.0

    raw: list[dict[str, Any]] = []
    for sector_code, metadata in sector_members.items():
        member_returns: list[float] = []
        member_breadth: list[bool] = []
        volume_ratios: list[float] = []
        for code in metadata.get("members", []):
            frame = daily_bars.get(code)
            if frame is None:
                continue
            close = _valid_close(frame, 21)
            if close is None or "Volume" not in frame.columns or close.iloc[-6] <= 0:
                continue
            volume = pd.to_numeric(frame["Volume"], errors="coerce").reindex(close.index)
            previous_volume = float(volume.iloc[-20:-5].mean())
            recent_volume = float(volume.iloc[-5:].mean())
            member_returns.append(float(close.iloc[-1] / close.iloc[-6] - 1.0))
            member_breadth.append(bool(close.iloc[-1] > close.tail(20).mean()))
            volume_ratios.append(recent_volume / previous_volume if previous_volume > 0 else 0.0)
        if len(member_returns) < config.min_sector_members:
            continue
        raw.append(
            {
                "code": sector_code,
                "name": str(metadata.get("name", sector_code)),
                "relative_return_5d": float(np.mean(member_returns) - market_return),
                "breadth": float(np.mean(member_breadth)),
                "volume_ratio": float(np.mean(volume_ratios)),
                "valid_members": len(member_returns),
            }
        )
    if not raw:
        return []
    table = pd.DataFrame(raw).set_index("code")
    table["score"] = (
        percentile_rank(table["relative_return_5d"]) * 0.50
        + percentile_rank(table["breadth"]) * 0.30
        + percentile_rank(table["volume_ratio"]) * 0.20
    )
    table = table.sort_values(["score", "relative_return_5d", "code"], ascending=[False, False, True])
    result = []
    for code, row in table.head(config.top_sector_count).iterrows():
        result.append(
            SectorScore(
                code=str(code),
                name=str(row["name"]),
                score=float(row["score"]),
                relative_return_5d=float(row["relative_return_5d"]),
                breadth=float(row["breadth"]),
                volume_ratio=float(row["volume_ratio"]),
                valid_members=int(row["valid_members"]),
            )
        )
    return result


def rank_leaders(
    sectors: list[SectorScore],
    sector_members: dict[str, dict[str, Any]],
    daily_bars: dict[str, pd.DataFrame],
    names: dict[str, str],
    config: StrategyConfig,
) -> list[LeaderCandidate]:
    best_by_code: dict[str, LeaderCandidate] = {}
    for sector in sectors:
        rows: list[dict[str, Any]] = []
        for code in sector_members.get(sector.code, {}).get("members", []):
            frame = daily_bars.get(code)
            if frame is None:
                continue
            close = _valid_close(frame, 21)
            if close is None or close.iloc[-6] <= 0 or close.iloc[-21] <= 0:
                continue
            volume = pd.to_numeric(frame["Volume"], errors="coerce").reindex(close.index).fillna(0.0)
            turnover = close * volume
            rows.append(
                {
                    "code": code,
                    "return_5d": float(close.iloc[-1] / close.iloc[-6] - 1.0),
                    "return_20d": float(close.iloc[-1] / close.iloc[-21] - 1.0),
                    "turnover": float(turnover.tail(20).mean()),
                }
            )
        if not rows:
            continue
        table = pd.DataFrame(rows).set_index("code")
        table["leader_score"] = (
            percentile_rank(table["return_5d"]) * 0.40
            + percentile_rank(table["return_20d"]) * 0.30
            + percentile_rank(table["turnover"]) * 0.30
        )
        table = table.sort_values(["leader_score", "return_5d", "code"], ascending=[False, False, True])
        for rank, (code, row) in enumerate(table.head(config.leaders_per_sector).iterrows(), start=1):
            candidate = LeaderCandidate(
                code=str(code),
                name=names.get(str(code), str(code)),
                sector_code=sector.code,
                sector_name=sector.name,
                sector_score=sector.score,
                leader_score=float(row["leader_score"]),
                leader_rank=rank,
            )
            existing = best_by_code.get(candidate.code)
            if existing is None or (candidate.sector_score, candidate.leader_score) > (
                existing.sector_score,
                existing.leader_score,
            ):
                best_by_code[candidate.code] = candidate
    return sorted(
        best_by_code.values(),
        key=lambda candidate: (-candidate.sector_score, candidate.leader_rank, -candidate.leader_score, candidate.code),
    )
