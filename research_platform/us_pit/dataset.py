from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from research_platform.us_market_time import ny_session_date, ny_session_dates

from .models import (
    QUALITY_CONTRACT_REVISION,
    QUALITY_POLICY_VERSION,
    QualityReport,
    ReleaseStatus,
)
from .store import USPITRelease


def _date(value: object) -> pd.Timestamp:
    return ny_session_date(value)


def _bars_by(frame: pd.DataFrame, group_column: str) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for group, rows in frame.groupby(group_column, sort=False):
        value = rows.drop(columns=[group_column]).copy()
        value["date"] = pd.to_datetime(value["date"], errors="raise")
        value = value.set_index("date").sort_index()
        result[str(group)] = value
    return result


@dataclass(frozen=True)
class USBacktestDataset:
    """Stable-ID data contract consumed by the strict US backtest adapter."""

    release_id: str
    universe_id: str
    quality_report: QualityReport
    membership_by_date: Mapping[pd.Timestamp, frozenset[str]]
    security_master: pd.DataFrame
    identifiers: pd.DataFrame
    listing_aliases: pd.DataFrame
    corporate_actions: pd.DataFrame
    session_exceptions: pd.DataFrame
    calendar: pd.DataFrame
    fee_schedule: pd.DataFrame
    raw_bars: Mapping[str, pd.DataFrame]
    vendor_front_bars: Mapping[str, pd.DataFrame]
    signal_bars_by_decision: Mapping[pd.Timestamp, Mapping[str, pd.DataFrame]]
    benchmark_bars: Mapping[str, pd.DataFrame]

    @property
    def benchmark_signal_bars(self) -> Mapping[str, pd.DataFrame]:
        """Benchmark inputs used for signal formation.

        ``Close`` remains the PIT signal price used by the market-regime rule.
        A separate ``TotalReturnClose`` column is mandatory for promotion
        comparisons; callers must never substitute one for the other.
        """

        return self.benchmark_bars

    @property
    def benchmark_raw_bars(self) -> Mapping[str, pd.DataFrame]:
        """Unadjusted benchmark rows used for execution/valuation checks.

        Version 1 releases store raw OHLCV and a causally frozen total-return
        level side by side.  The strict executor consumes only raw OHLCV while
        qualification consumes only ``TotalReturnClose``.
        """

        return self.benchmark_bars

    @property
    def includes_delisted(self) -> bool:
        return self.quality_report.includes_delisted

    @classmethod
    def from_release(cls, release: USPITRelease) -> "USBacktestDataset":
        release.verify()
        report = release.quality_report
        if (
            release.manifest.quality_policy_version != QUALITY_POLICY_VERSION
            or report.policy_version != QUALITY_POLICY_VERSION
            or report.metrics.get("quality_contract_revision")
            != QUALITY_CONTRACT_REVISION
        ):
            raise ValueError(
                "PIT release quality contract is obsolete: "
                f"{release.manifest.quality_policy_version}/"
                f"{report.metrics.get('quality_contract_revision')}"
            )
        if release.status != ReleaseStatus.DATA_READY or report.status != ReleaseStatus.DATA_READY:
            raise ValueError(f"PIT release is not DATA_READY: {release.release_id}")
        if not report.includes_delisted:
            raise ValueError("PIT release lacks derived delisting coverage")

        membership = release.load_frame("membership_monthly")
        membership["decision_date"] = pd.to_datetime(
            membership["decision_date"], errors="raise"
        ).dt.normalize()
        membership_by_date = {
            pd.Timestamp(decision).normalize(): frozenset(rows["security_id"].astype(str))
            for decision, rows in membership.groupby("decision_date", sort=True)
        }
        raw = release.load_frame("bars_raw")
        front = release.load_frame("bars_vendor_front")
        signal = release.load_frame("bars_pit_signal")
        signal["decision_date"] = pd.to_datetime(
            signal["decision_date"], errors="raise"
        ).dt.normalize()
        signal_by_decision: dict[pd.Timestamp, Mapping[str, pd.DataFrame]] = {}
        for decision, rows in signal.groupby("decision_date", sort=True):
            signal_by_decision[pd.Timestamp(decision).normalize()] = _bars_by(
                rows.drop(columns=["decision_date"]), "security_id"
            )

        aliases = release.load_frame("listing_aliases")
        aliases["valid_from"] = pd.to_datetime(aliases["valid_from"], errors="raise").dt.normalize()
        aliases["valid_to"] = pd.to_datetime(aliases["valid_to"], errors="coerce").dt.normalize()
        fees = release.load_frame("execution_fee_schedule")
        fees["effective_from"] = pd.to_datetime(
            fees["effective_from"], errors="raise"
        ).dt.normalize()
        fees["effective_to"] = pd.to_datetime(
            fees["effective_to"], errors="coerce"
        ).dt.normalize()
        benchmarks = release.load_frame("benchmarks")
        benchmarks["symbol"] = benchmarks["symbol"].astype(str).str.upper().map(
            lambda value: value if value.endswith(".US") else f"{value}.US"
        )
        return cls(
            release_id=release.release_id,
            universe_id=release.universe_id,
            quality_report=report,
            membership_by_date=membership_by_date,
            security_master=release.load_frame("security_master"),
            identifiers=release.load_frame("identifiers"),
            listing_aliases=aliases,
            corporate_actions=release.load_frame("corporate_actions"),
            session_exceptions=release.load_frame("session_exceptions"),
            calendar=release.load_frame("xnys_calendar"),
            fee_schedule=fees,
            raw_bars=_bars_by(raw, "security_id"),
            vendor_front_bars=_bars_by(front, "security_id"),
            signal_bars_by_decision=signal_by_decision,
            benchmark_bars=_bars_by(benchmarks.rename(columns={"symbol": "security_id"}), "security_id"),
        )

    def decision_date(self, asof: object) -> pd.Timestamp:
        cutoff = _date(asof)
        available = [item for item in self.membership_by_date if item <= cutoff]
        if not available:
            raise ValueError(f"no point-in-time membership is available at {cutoff.date()}")
        return max(available)

    def members(self, asof: object) -> frozenset[str]:
        return self.membership_by_date[self.decision_date(asof)]

    def signal_bars(self, asof: object) -> Mapping[str, pd.DataFrame]:
        decision = self.decision_date(asof)
        try:
            return self.signal_bars_by_decision[decision]
        except KeyError as exc:
            raise ValueError(f"signal bars missing for decision {decision.date()}") from exc

    def vendor_code(self, security_id: str, asof: object) -> str:
        day = _date(asof)
        aliases = self.listing_aliases
        active = aliases[
            aliases["security_id"].astype(str).eq(str(security_id))
            & (aliases["valid_from"] <= day)
            & (aliases["valid_to"].isna() | (aliases["valid_to"] >= day))
        ]
        if len(active) != 1:
            raise ValueError(
                f"expected one active vendor alias for {security_id} at {day.date()}, "
                f"found {len(active)}"
            )
        return str(active.iloc[0]["vendor_code"])

    def fee_at(self, asof: object) -> dict[str, float]:
        day = _date(asof)
        active = self.fee_schedule[
            (self.fee_schedule["effective_from"] <= day)
            & (
                self.fee_schedule["effective_to"].isna()
                | (self.fee_schedule["effective_to"] >= day)
            )
        ]
        if len(active) != 1:
            raise ValueError(
                f"expected one effective fee schedule at {day.date()}, found {len(active)}"
            )
        row = active.iloc[0]
        return {
            "commission_rate": float(row["commission_rate"]),
            "min_commission": float(row["min_commission"]),
            "slippage_rate": float(row["slippage_rate"]),
            "sec_sell_fee_rate": float(row["sec_sell_fee_rate"]),
            "finra_taf_per_share": float(row["finra_taf_per_share"]),
            "finra_taf_cap": float(row["finra_taf_cap"]),
        }

    def actions_on(self, session: object) -> pd.DataFrame:
        day = ny_session_date(session)
        effective = ny_session_dates(self.corporate_actions["effective_at"])
        return self.corporate_actions.loc[effective == day].copy()


__all__ = ["USBacktestDataset"]
