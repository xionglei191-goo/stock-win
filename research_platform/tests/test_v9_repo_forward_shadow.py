from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from research_platform.models import (
    PlatformSignal,
    SignalStatus,
    StrategyScanResult,
)
from research_platform.storage import Database
from research_platform.strategies.course49_v9 import Course49V9Strategy
from research_platform.tests.helpers import temporary_config
from research_platform.v9_repo_forward_shadow import (
    BASE_COMMISSION_RATE,
    V9RepoForwardShadowService,
)


SESSIONS = pd.DatetimeIndex(
    [
        "2026-08-07",
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
    ]
)


def stock_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [9.8, 10.0, 10.1, 10.2, 10.3],
            "High": [10.1, 10.3, 10.4, 10.5, 10.6],
            "Low": [9.7, 9.9, 10.0, 10.1, 10.2],
            "Close": [9.9, 10.1, 10.2, 10.3, 10.4],
            "Volume": [1_000_000.0] * 5,
            "Amount": [10_000_000.0] * 5,
        },
        index=SESSIONS,
    )


def repo_bars(code: str) -> pd.DataFrame:
    if code == "131810.SZ":
        close = [1.5, 2.0, 2.1, 2.2, 2.3]
        low = [1.0, 1.2, 1.3, 1.4, 1.5]
    else:
        close = [1.6, 3.0, 3.1, 99.0, 3.3]
        low = [1.1, 1.8, 1.9, 50.0, 2.1]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [value + 0.2 for value in close],
            "Low": low,
            "Close": close,
            "Volume": [10_000.0] * 5,
        },
        index=SESSIONS,
    )


class FakeV9Strategy:
    metadata = Course49V9Strategy.metadata

    def __init__(self) -> None:
        self.visible_ends: list[pd.Timestamp] = []
        self.positions_seen: list[int] = []

    def scan(self, **context: Any) -> StrategyScanResult:
        asof = pd.Timestamp(context["asof"]).normalize()
        self.visible_ends.append(
            max(pd.Timestamp(frame.index[-1]).normalize() for frame in context["raw_bars"].values())
        )
        self.positions_seen.append(len(context["positions"]))
        signals: tuple[PlatformSignal, ...] = ()
        if asof == pd.Timestamp("2026-08-10"):
            generated = datetime(2026, 8, 10, 18, tzinfo=timezone(timedelta(hours=8)))
            signals = (
                PlatformSignal(
                    run_id=str(context["run_id"]),
                    strategy_id="course49_v9",
                    strategy_version="9.0.0",
                    generated_at=generated,
                    available_at=generated,
                    code="000001.SZ",
                    side="BUY",
                    strength=0.9,
                    target_weight=0.24,
                    horizon="daily-short",
                    valid_until=generated + timedelta(days=2),
                    stop_price=9.8,
                    status=SignalStatus.PROPOSED,
                    reason_codes=("TEST_ONLY",),
                    evidence={"entry_price": 10.1, "sector_code": "test"},
                    signal_id="shadow-signal-1",
                ),
            )
        return StrategyScanResult(
            strategy=self.metadata,
            signals=signals,
            candidates=({"code": "000001.SZ", "asof": asof.isoformat()},),
            state={
                "asof": asof.date().isoformat(),
                "runtime_state": {},
                "entry_allowed": bool(signals),
            },
        )


class V9RepoForwardShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = temporary_config(Path(self.temp.name))
        self.database = Database(self.config)
        self.database.initialize()
        self.strategy = FakeV9Strategy()
        self.service = V9RepoForwardShadowService(
            self.config,
            self.database,
            strategy=self.strategy,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def context(self, asof: str) -> dict[str, Any]:
        bars = stock_bars()
        return {
            "asof": asof,
            "front_bars": {"000001.SZ": bars},
            "raw_bars": {"000001.SZ": bars},
            "names": {"000001.SZ": "Test"},
            "sector_members": {},
            "benchmark_bars": {},
            "index_bars": bars,
            "repo_bars": {
                "131810.SZ": repo_bars("131810.SZ"),
                "204001.SH": repo_bars("204001.SH"),
            },
            "limit_snapshot": {},
            "lhb_history": {},
            "market_activity": pd.DataFrame(),
        }

    def table_counts(self) -> dict[str, int]:
        return {
            table: int(
                self.database.query(f"SELECT COUNT(*) AS count FROM {table}")[0]["count"]
            )
            for table in (
                "signals",
                "paper_orders",
                "paper_positions",
                "paper_fills",
            )
        }

    def test_capture_is_point_in_time_idempotent_and_paper_isolated(self) -> None:
        before = self.table_counts()
        first = self.service.capture_session(**self.context("2026-08-10"))
        second = self.service.capture_session(**self.context("2026-08-11"))
        repeated = self.service.capture_session(**self.context("2026-08-11"))

        self.assertEqual(first["status"], "CAPTURED_OBSERVATION_ONLY")
        self.assertEqual(second["virtual_fills"], 1)
        self.assertEqual(repeated["status"], "ALREADY_CAPTURED")
        self.assertEqual(self.strategy.visible_ends, [
            pd.Timestamp("2026-08-10"),
            pd.Timestamp("2026-08-11"),
        ])
        self.assertEqual(self.strategy.positions_seen, [0, 1])
        self.assertEqual(self.table_counts(), before)
        self.assertEqual(
            self.database.query(
                "SELECT COUNT(*) AS count FROM v9_repo_shadow_events"
            )[0]["count"],
            2,
        )
        first_payload = json.loads(
            self.database.query(
                """SELECT payload_json FROM v9_repo_shadow_events
                WHERE session_date='2026-08-10'"""
            )[0]["payload_json"]
        )
        self.assertEqual(first_payload["repo"]["base"]["intent"]["code"], "204001.SH")
        self.assertEqual(
            first_payload["repo"]["base"]["intent"]["quoted_rate_percent"],
            3.0,
        )
        self.assertFalse(first_payload["repo"]["base"]["intent"]["code"] == "99.0")
        self.assertFalse(second["paper_simulation_ready"])

    def test_repo_interest_settles_only_after_two_following_sessions(self) -> None:
        self.service.capture_session(**self.context("2026-08-10"))
        self.service.capture_session(**self.context("2026-08-11"))
        third = self.service.capture_session(**self.context("2026-08-12"))
        payload = json.loads(
            self.database.query(
                """SELECT payload_json FROM v9_repo_shadow_events
                WHERE session_date='2026-08-12'"""
            )[0]["payload_json"]
        )

        expected = 50_000.0 * 3.0 / 100.0 / 365.0 - 50_000.0 * BASE_COMMISSION_RATE
        self.assertEqual(third["status"], "CAPTURED_OBSERVATION_ONLY")
        self.assertAlmostEqual(
            payload["state"]["settled_repo_pnl"]["base"],
            expected,
        )
        self.assertEqual(
            payload["repo"]["base"]["settlement"]["actual_occupied_days"],
            1,
        )
        self.assertEqual(self.service.status()["fresh_sessions"], 3)
        self.assertEqual(self.service.status()["status"], "COLLECTING")
        self.assertFalse(self.service.status()["paper_simulation_ready"])

    def test_forward_gap_is_blocked_instead_of_backfilled(self) -> None:
        self.service.capture_session(**self.context("2026-08-10"))
        result = self.service.capture_session(**self.context("2026-08-12"))

        self.assertEqual(result["status"], "BLOCKED_FORWARD_GAP")
        self.assertEqual(result["missing_sessions"], 1)
        self.assertEqual(
            self.database.query(
                "SELECT COUNT(*) AS count FROM v9_repo_shadow_events"
            )[0]["count"],
            1,
        )

    def test_historical_boundary_is_never_captured(self) -> None:
        result = self.service.capture_session(**self.context("2026-08-07"))

        self.assertEqual(result["status"], "WAITING_FOR_FRESH_SESSION")
        self.assertEqual(
            self.database.query(
                "SELECT COUNT(*) AS count FROM v9_repo_shadow_events"
            )[0]["count"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
