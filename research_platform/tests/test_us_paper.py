from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from research_platform.us_paper import (
    USMomentumPaperService,
    USPaperCausalityError,
    USPaperConfig,
    USPaperConflictError,
    USPaperStateError,
)


NY = ZoneInfo("America/New_York")
PIT_RELEASE_ID = "1" * 64
MANIFEST_SHA256 = "2" * 64
ACTION_EVIDENCE_SHA256 = "a" * 64


def at(day: str, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.fromisoformat(day).replace(
        hour=hour,
        minute=minute,
        second=second,
        tzinfo=NY,
    )


def signal(
    code: str = "AAPL.US",
    *,
    side: str = "BUY",
    generated_day: str = "2026-01-30",
    execution_day: str = "2026-02-02",
    signal_id: str | None = None,
    security_id: str | None = None,
    pit_release_id: str = PIT_RELEASE_ID,
    manifest_sha256: str = MANIFEST_SHA256,
) -> dict[str, object]:
    ticker = code.removesuffix(".US").lower().replace(".", "_")
    return {
        "signal_id": signal_id or f"signal-{generated_day}-{code}-{side}",
        "code": code,
        "side": side,
        "target_weight": 0.10 if side == "BUY" else 0.0,
        "generated_at": at(generated_day, 16, 0),
        "available_at": at(execution_day, 9, 30),
        "valid_until": at(execution_day, 16, 0),
        "reason_codes": ("US_RS_ENTRY" if side == "BUY" else "US_RS_EXIT",),
        "evidence": {
            "stop_ratio": 0.08,
            "security_id": security_id or f"us_test_{ticker}",
            "pit_release_id": pit_release_id,
            "manifest_sha256": manifest_sha256,
        },
    }


def open_observation(
    code: str,
    day: str,
    price: float,
    *,
    observation_id: str | None = None,
) -> dict[str, object]:
    return {
        "observation_id": observation_id or f"open-{day}-{code}",
        "idempotency_key": observation_id or f"open-{day}-{code}",
        "code": code,
        "session_date": day,
        "kind": "OPEN",
        "event_at": at(day, 9, 30),
        "available_at": at(day, 9, 30),
        "open": price,
    }


def daily_observation(
    code: str,
    day: str,
    *,
    opening: float,
    high: float,
    low: float,
    close: float,
) -> dict[str, object]:
    return {
        "observation_id": f"daily-{day}-{code}",
        "idempotency_key": f"daily-{day}-{code}",
        "code": code,
        "session_date": day,
        "kind": "DAILY",
        "event_at": at(day, 16, 0),
        "available_at": at(day, 16, 0),
        "open": opening,
        "high": high,
        "low": low,
        "close": close,
    }


def corporate_action(
    action_id: str,
    action_type: str,
    day: str,
    *,
    security_id: str = "us_test_aapl",
    pay_date: str | None = None,
    verified: bool = True,
    **terms: object,
) -> dict[str, object]:
    return {
        "action_id": action_id,
        "action_type": action_type,
        "security_id": security_id,
        "effective_date": day,
        "pay_date": pay_date,
        "verified": verified,
        "verified_at": at(day, 8, 0),
        "evidence_sha256": ACTION_EVIDENCE_SHA256,
        "pit_release_id": PIT_RELEASE_ID,
        "manifest_sha256": MANIFEST_SHA256,
        "terms": terms,
    }


class USMomentumPaperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database_path = Path(self.temporary.name) / "isolated-us-paper.sqlite"
        self.config = USPaperConfig(
            database_path=self.database_path,
            commission_rate=0.0,
            sec_sell_fee_rate=0.0,
            finra_taf_per_share=0.0,
            slippage_rate=0.0,
        )
        self.service = USMomentumPaperService(self.config)

    def buy_aapl(self) -> dict[str, object]:
        self.service.create_period(
            [signal("AAPL.US")], now=at("2026-01-30", 16, 1)
        )
        return self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-02-02", 100.0)],
        )

    def test_schema_is_isolated_and_service_is_unconditionally_paper_only(self) -> None:
        status = self.service.status()
        self.assertEqual(status["mode"], "PAPER")
        self.assertTrue(status["paper_only"])
        self.assertFalse(hasattr(self.service, "broker"))
        connection = sqlite3.connect(self.database_path)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertTrue(
            {
                "us_paper_account",
                "us_paper_periods",
                "us_paper_orders",
                "us_paper_observations",
                "us_paper_positions",
                "us_paper_fills",
                "us_paper_sessions",
                "us_paper_events",
                "us_paper_corporate_actions",
                "us_paper_receivables",
                "us_paper_cash_ledger",
            }.issubset(names)
        )
        self.assertFalse(any(name.startswith("trading_") for name in names))

        connection = sqlite3.connect(self.database_path)
        try:
            position_columns = {
                row[1]: row[5]
                for row in connection.execute(
                    "PRAGMA table_info(us_paper_positions)"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(1, position_columns["security_id"])
        self.assertIn("code", position_columns)
        self.assertEqual("STABLE_SECURITY_ID_V1", status["identity_contract"])
        self.assertTrue(status["binding"]["rolling_releases_allowed"])

    def test_signal_identity_and_release_provenance_are_mandatory(self) -> None:
        missing = signal("AAPL.US")
        missing["evidence"] = {"stop_ratio": 0.08}
        with self.assertRaisesRegex(ValueError, "security_id"):
            self.service.create_period(
                [missing], now=at("2026-01-30", 16, 1)
            )

        mismatch = [
            signal("AAPL.US"),
            signal("MSFT.US", pit_release_id="3" * 64),
        ]
        with self.assertRaisesRegex(USPaperConflictError, "one PIT release"):
            self.service.create_period(
                mismatch, now=at("2026-01-30", 16, 1)
            )

    def test_legacy_signal_shape_requires_an_explicit_test_fixture_identity(self) -> None:
        legacy = signal("AAPL.US")
        legacy["evidence"] = {"stop_ratio": 0.08}
        fixture = {
            "explicit_test_fixture": True,
            "security_id": "us_fixture_aapl",
            "pit_release_id": PIT_RELEASE_ID,
        }
        fixture_service = USMomentumPaperService(
            USPaperConfig(
                database_path=Path(self.temporary.name) / "fixture-enabled.sqlite",
                allow_test_fixture_identity=True,
            )
        )
        period = fixture_service.create_period(
            [legacy],
            manifest_sha256=MANIFEST_SHA256,
            test_fixture_identity=fixture,
            now=at("2026-01-30", 16, 1),
        )
        self.assertEqual("us_fixture_aapl", period["orders"][0]["security_id"])

        other_path = Path(self.temporary.name) / "fixture-guard.sqlite"
        other = USMomentumPaperService(USPaperConfig(database_path=other_path))
        with self.assertRaisesRegex(ValueError, "explicit_test_fixture"):
            other.create_period(
                [legacy],
                manifest_sha256=MANIFEST_SHA256,
                test_fixture_identity={
                    "security_id": "us_fixture_aapl",
                    "pit_release_id": PIT_RELEASE_ID,
                },
                now=at("2026-01-30", 16, 1),
            )

    def test_release_provenance_flows_through_period_order_position_and_fill(self) -> None:
        created = self.service.create_period(
            [signal("AAPL.US")], now=at("2026-01-30", 16, 1)
        )
        self.assertEqual(PIT_RELEASE_ID, created["pit_release_id"])
        self.assertEqual(MANIFEST_SHA256, created["manifest_sha256"])
        self.assertEqual("us_test_aapl", created["orders"][0]["security_id"])

        self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-02-02", 100.0)],
        )
        status = self.service.status()
        for collection in ("positions", "fills"):
            self.assertEqual(PIT_RELEASE_ID, status[collection][0]["pit_release_id"])
            self.assertEqual(
                MANIFEST_SHA256, status[collection][0]["manifest_sha256"]
            )
            self.assertEqual("us_test_aapl", status[collection][0]["security_id"])
        self.assertEqual(PIT_RELEASE_ID, status["binding"]["pit_release_id"])

        observations = self.service.executor._store.rows(
            "SELECT security_id, pit_release_id, manifest_sha256 "
            "FROM us_paper_observations"
        )
        self.assertEqual("us_test_aapl", observations[0]["security_id"])
        self.assertEqual(PIT_RELEASE_ID, observations[0]["pit_release_id"])

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE us_paper_positions SET security_id='us_tampered'"
                )
        finally:
            connection.close()

    def test_alias_rename_is_atomic_has_no_trade_and_allows_rolling_release(self) -> None:
        stable_id = "us_test_meta_security"
        self.service.create_period(
            [signal("FB.US", security_id=stable_id)],
            now=at("2026-01-30", 16, 1),
        )
        self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 5),
            observations=[open_observation("FB.US", "2026-02-02", 100.0)],
        )
        before = self.service.status()
        self.assertEqual(1, len(before["fills"]))

        next_release = "3" * 64
        next_manifest = "4" * 64
        period = self.service.create_period(
            [
                signal(
                    "META.US",
                    side="SELL",
                    generated_day="2026-02-27",
                    execution_day="2026-03-02",
                    security_id=stable_id,
                    pit_release_id=next_release,
                    manifest_sha256=next_manifest,
                )
            ],
            now=at("2026-02-27", 16, 1),
        )
        renamed = self.service.status()
        self.assertEqual("META.US", renamed["positions"][0]["code"])
        self.assertEqual(stable_id, renamed["positions"][0]["security_id"])
        # Entry provenance remains immutable; the period/order use the rolling release.
        self.assertEqual(PIT_RELEASE_ID, renamed["positions"][0]["pit_release_id"])
        self.assertEqual(next_release, period["pit_release_id"])
        self.assertEqual(next_release, period["orders"][0]["pit_release_id"])
        self.assertEqual(next_release, renamed["binding"]["pit_release_id"])
        self.assertEqual(1, len(renamed["fills"]))
        alias_events = [
            row
            for row in renamed["events"]
            if row["event_type"] == "SECURITY_ALIAS_RENAMED"
        ]
        self.assertEqual(1, len(alias_events))

        sold = self.service.tick(
            "2026-03-02",
            now=at("2026-03-02", 9, 30, 5),
            observations=[open_observation("META.US", "2026-03-02", 101.0)],
        )
        self.assertEqual([], sold["positions"])
        self.assertEqual(stable_id, sold["fills"][-1]["security_id"])
        self.assertEqual(next_release, sold["fills"][-1]["pit_release_id"])

    def test_nonempty_legacy_database_without_identity_is_rejected(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy-nonempty.sqlite"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE us_paper_positions (
                code TEXT PRIMARY KEY, quantity INTEGER NOT NULL,
                average_price REAL NOT NULL, stop_price REAL NOT NULL,
                last_price REAL NOT NULL, entry_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)"""
            )
            connection.execute(
                "INSERT INTO us_paper_positions VALUES "
                "('FB.US', 1, 100, 92, 100, '2026-01-01', '2026-01-01')"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(USPaperConflictError, "legacy paper database"):
            USMomentumPaperService(USPaperConfig(database_path=legacy_path))

    def test_empty_legacy_database_is_migrated_to_stable_identity_schema(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy-empty.sqlite"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE us_paper_positions (
                code TEXT PRIMARY KEY, quantity INTEGER NOT NULL,
                average_price REAL NOT NULL, stop_price REAL NOT NULL,
                last_price REAL NOT NULL, entry_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)"""
            )
            connection.commit()
        finally:
            connection.close()
        service = USMomentumPaperService(USPaperConfig(database_path=legacy_path))
        self.assertEqual([], service.status()["positions"])
        connection = sqlite3.connect(legacy_path)
        try:
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(us_paper_positions)"
                ).fetchall()
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertIn("security_id", columns)
        self.assertEqual(5, version)

    def test_split_adjusts_position_and_fractional_cash_once(self) -> None:
        bought = self.buy_aapl()
        quantity = int(bought["positions"][0]["quantity"])
        cash_before = float(bought["cash"])
        action = corporate_action(
            "split-aapl",
            "SPLIT",
            "2026-02-03",
            ratio=1.005,
            cash_in_lieu_price=70.0,
        )
        first = self.service.apply_corporate_actions(
            "2026-02-03",
            [action],
            now=at("2026-02-03", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        restarted = USMomentumPaperService(self.config)
        restarted.apply_corporate_actions(
            "2026-02-03",
            [action],
            now=at("2026-02-03", 8, 31),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        status = restarted.status()
        position = status["positions"][0]
        exact = quantity * 1.005
        self.assertEqual(int(exact), position["quantity"])
        self.assertAlmostEqual(100.0 / 1.005, position["average_price"])
        self.assertAlmostEqual(92.0 / 1.005, position["stop_price"])
        self.assertEqual("APPLIED", first[0]["status"])
        self.assertEqual(1, len(status["corporate_action_cash"]))
        self.assertAlmostEqual(cash_before + (exact - int(exact)) * 70, status["account"]["cash"])

    def test_split_with_new_cusip_moves_the_stable_position_identity(self) -> None:
        bought = self.buy_aapl()
        quantity = int(bought["positions"][0]["quantity"])
        action = corporate_action(
            "split-new-cusip",
            "SPLIT",
            "2026-02-03",
            ratio=10.0,
            successor_security_id="us_test_aapl_new_cusip",
            successor_code="AAPN.US",
        )
        self.service.apply_corporate_actions(
            "2026-02-03",
            [action],
            now=at("2026-02-03", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        position = self.service.status()["positions"][0]
        self.assertEqual("us_test_aapl_new_cusip", position["security_id"])
        self.assertEqual("AAPN.US", position["code"])
        self.assertEqual(quantity * 10, position["quantity"])
        self.assertAlmostEqual(10.0, position["average_price"])
        self.assertAlmostEqual(9.2, position["stop_price"])

    def test_cash_dividend_receivable_is_paid_once_on_pay_date(self) -> None:
        bought = self.buy_aapl()
        quantity = int(bought["positions"][0]["quantity"])
        cash_before = float(bought["cash"])
        action = corporate_action(
            "div-aapl",
            "CASH_DIVIDEND",
            "2026-02-03",
            pay_date="2026-02-05",
            amount_per_share=0.25,
        )
        self.service.apply_corporate_actions(
            "2026-02-03",
            [action],
            now=at("2026-02-03", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        pending = self.service.status()
        self.assertEqual("PENDING", pending["receivables"][0]["status"])
        self.assertAlmostEqual(cash_before, pending["account"]["cash"])
        self.assertAlmostEqual(91.75, pending["positions"][0]["stop_price"])
        self.service.apply_corporate_actions(
            "2026-02-05",
            [],
            now=at("2026-02-05", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.service.apply_corporate_actions(
            "2026-02-05",
            [],
            now=at("2026-02-05", 8, 31),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        paid = self.service.status()
        self.assertEqual("PAID", paid["receivables"][0]["status"])
        self.assertAlmostEqual(cash_before + quantity * 0.25, paid["account"]["cash"])
        self.assertEqual(1, len(paid["corporate_action_cash"]))

    def test_cash_merger_settles_and_stock_merger_moves_stable_identity(self) -> None:
        bought = self.buy_aapl()
        quantity = int(bought["positions"][0]["quantity"])
        cash_before = float(bought["cash"])
        cash_action = corporate_action(
            "cash-merger-aapl", "CASH_MERGER", "2026-02-03", cash_per_share=125.0
        )
        self.service.apply_corporate_actions(
            "2026-02-03",
            [cash_action],
            now=at("2026-02-03", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        settled = self.service.status()
        self.assertEqual([], settled["positions"])
        self.assertAlmostEqual(cash_before + quantity * 125, settled["account"]["cash"])

        other_path = Path(self.temporary.name) / "stock-merger.sqlite"
        other = USMomentumPaperService(
            USPaperConfig(
                database_path=other_path,
                commission_rate=0,
                sec_sell_fee_rate=0,
                finra_taf_per_share=0,
                slippage_rate=0,
            )
        )
        other.create_period([signal("AAPL.US")], now=at("2026-01-30", 16, 1))
        other.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-02-02", 100)],
        )
        stock_action = corporate_action(
            "stock-merger-aapl",
            "STOCK_MERGER",
            "2026-02-03",
            ratio=2.0,
            target_security_id="us_test_newco",
            target_code="NEWC.US",
        )
        other.apply_corporate_actions(
            "2026-02-03",
            [stock_action],
            now=at("2026-02-03", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        merged = other.status()["positions"][0]
        self.assertEqual("us_test_newco", merged["security_id"])
        self.assertEqual("NEWC.US", merged["code"])
        self.assertEqual(quantity * 2, merged["quantity"])
        self.assertAlmostEqual(50.0, merged["average_price"])

    def test_unverified_action_fails_closed_but_verified_spinoff_is_applied(self) -> None:
        self.buy_aapl()
        actions = [
            corporate_action(
                "unverified-aapl", "SPLIT", "2026-02-03", verified=False, ratio=2
            ),
            corporate_action(
                "spinoff-aapl",
                "SPINOFF",
                "2026-02-03",
                child_security_id="us_test_child",
                child_code="CHLD.US",
                share_ratio=0.5,
                cost_basis_fraction=0.2,
            ),
        ]
        result = self.service.apply_corporate_actions(
            "2026-02-03",
            actions,
            now=at("2026-02-03", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual({"BLOCKED", "APPLIED"}, {row["status"] for row in result})
        status = self.service.status()
        self.assertEqual("DATA_DEGRADED", status["account"]["status"])
        self.assertEqual(2, len(status["positions"]))
        positions = {row["security_id"]: row for row in status["positions"]}
        self.assertEqual("CHLD.US", positions["us_test_child"]["code"])
        self.assertAlmostEqual(80.0, positions["us_test_aapl"]["average_price"])
        self.assertAlmostEqual(40.0, positions["us_test_child"]["average_price"])
        reasons = {row["block_reason"] for row in status["corporate_actions"]}
        self.assertTrue(any("NOT_VERIFIED" in reason for reason in reasons))

    def test_release_row_shape_is_admitted_without_guessing_terms(self) -> None:
        self.buy_aapl()
        row = {
            "action_id": "release-split-aapl",
            "security_id": "us_test_aapl",
            "action_type": "SPLIT",
            "announced_at": at("2026-02-02", 16, 30),
            "effective_at": "2026-02-03T00:00:00-05:00",
            "pay_date": float("nan"),
            "terms_verified": 1,
            "evidence_sha256": ACTION_EVIDENCE_SHA256,
            "split_ratio": 2.0,
        }
        result = self.service.apply_corporate_actions(
            "2026-02-03",
            [row],
            now=at("2026-02-03", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual("APPLIED", result[0]["status"])
        self.assertEqual(200, self.service.status()["positions"][0]["quantity"])

    def test_release_rename_is_normalized_to_non_economic_ticker_change(self) -> None:
        self.buy_aapl()
        fills_before = len(self.service.status()["fills"])
        result = self.service.apply_corporate_actions(
            "2026-02-03",
            [
                {
                    "action_id": "rename-aapl",
                    "security_id": "us_test_aapl",
                    "action_type": "RENAME",
                    "announced_at": at("2026-02-02", 16, 30),
                    "effective_at": "2026-02-03T00:00:00-05:00",
                    "terms_verified": True,
                    "evidence_sha256": ACTION_EVIDENCE_SHA256,
                }
            ],
            now=at("2026-02-03", 8, 30),
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
        self.assertEqual("APPLIED", result[0]["status"])
        self.assertEqual("TICKER_CHANGE", result[0]["action_type"])
        status = self.service.status()
        self.assertEqual("AAPL.US", status["positions"][0]["code"])
        self.assertEqual(fills_before, len(status["fills"]))

    def test_period_and_orders_are_auto_approved_and_idempotent(self) -> None:
        signals = [signal("MSFT.US"), signal("AAPL.US")]
        first = self.service.create_period(signals, now=at("2026-01-30", 16, 1))
        duplicate = self.service.create_period(
            list(reversed(signals)), now=at("2026-01-30", 16, 2)
        )
        self.assertEqual(first["period_id"], duplicate["period_id"])
        self.assertEqual(first["status"], "AUTO_APPROVED")
        self.assertEqual(len(first["orders"]), 2)
        self.assertEqual({row["status"] for row in first["orders"]}, {"WAITING_OPEN"})

        changed = [signal("MSFT.US"), signal("GOOG.US")]
        with self.assertRaises(USPaperConflictError):
            self.service.create_period(changed, now=at("2026-01-30", 16, 3))

    def test_empty_monthly_decision_is_recorded_without_inventing_orders(self) -> None:
        first = self.service.create_period(
            [],
            decision_at=at("2026-01-30", 16, 0),
            execution_session="2026-02-02",
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
            now=at("2026-01-30", 16, 1),
        )
        duplicate = self.service.create_period(
            [],
            decision_at=at("2026-01-30", 16, 0),
            execution_session="2026-02-02",
            pit_release_id=PIT_RELEASE_ID,
            manifest_sha256=MANIFEST_SHA256,
            now=at("2026-01-30", 16, 2),
        )
        self.assertEqual(first["period_id"], duplicate["period_id"])
        self.assertEqual(first["status"], "AUTO_APPROVED")
        self.assertEqual(first["orders"], [])

    def test_open_fill_observation_and_fill_are_idempotent(self) -> None:
        self.service.create_period(
            [signal("AAPL.US")], now=at("2026-01-30", 16, 1)
        )
        observation = open_observation("AAPL.US", "2026-02-02", 100.0)
        first = self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 10),
            observations=[observation],
        )
        duplicate = self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 20),
            observations=[observation],
        )
        self.assertEqual(first["state"], "OPEN_CAPTURED")
        self.assertEqual(len(first["positions"]), 1)
        self.assertEqual(len(first["fills"]), 1)
        self.assertEqual(len(duplicate["fills"]), 1)
        self.assertEqual(duplicate["positions"][0]["quantity"], 100)
        self.assertAlmostEqual(duplicate["positions"][0]["stop_price"], 92.0)
        status = self.service.status()
        self.assertEqual(len(status["orders"]), 1)
        self.assertEqual(len(status["fills"]), 1)

    def test_late_open_is_a_permanent_non_fill_and_degrades_data(self) -> None:
        self.service.create_period(
            [signal("AAPL.US")], now=at("2026-01-30", 16, 1)
        )
        observation = open_observation("AAPL.US", "2026-02-02", 100.0)
        result = self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 36),
            observations=[observation],
        )
        self.assertEqual(result["state"], "DATA_DEGRADED")
        self.assertEqual(result["account_status"], "DATA_DEGRADED")
        self.assertFalse(result["fills"])
        status = self.service.status()
        self.assertEqual(status["orders"][0]["status"], "EXPIRED")
        self.assertEqual(
            status["orders"][0]["block_reason"], "LATE_OPEN_NOT_BACKFILLED"
        )
        self.assertEqual(
            self.service.executor._store.rows(
                "SELECT status FROM us_paper_observations"
            )[0]["status"],
            "LATE_IGNORED",
        )

        self.service.acknowledge_data_recovery(
            note="source healthy for future sessions", now=at("2026-02-02", 10, 0)
        )
        replay = self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 10, 1),
            observations=[observation],
        )
        self.assertFalse(replay["fills"])

    def test_missing_one_open_degrades_before_other_buy_can_fill(self) -> None:
        self.service.create_period(
            [signal("AAPL.US"), signal("MSFT.US")],
            now=at("2026-01-30", 16, 1),
        )
        self.service.observe(
            open_observation("AAPL.US", "2026-02-02", 100.0),
            now=at("2026-02-02", 9, 30, 10),
        )
        result = self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 36),
        )
        self.assertEqual(result["account_status"], "DATA_DEGRADED")
        self.assertFalse(result["fills"])
        order_statuses = {
            row["code"]: (row["status"], row["block_reason"])
            for row in self.service.status()["orders"]
        }
        self.assertEqual(order_statuses["MSFT.US"][0], "EXPIRED")
        self.assertEqual(order_statuses["AAPL.US"], ("BLOCKED", "DATA_DEGRADED"))

    def test_gap_stop_and_intraday_stop_use_only_timely_observations(self) -> None:
        self.service.create_period(
            [signal("AAPL.US")], now=at("2026-01-30", 16, 1)
        )
        self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-02-02", 100.0)],
        )
        gap = self.service.tick(
            "2026-02-03",
            now=at("2026-02-03", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-02-03", 90.0)],
        )
        self.assertFalse(gap["positions"])
        self.assertEqual(gap["fills"][-1]["reason"], "US_FIXED_STOP_GAP")
        self.assertEqual(len(gap["fills"]), 2)

    def test_close_low_cannot_backfill_stop_and_missing_mark_degrades(self) -> None:
        self.service.create_period(
            [signal("AAPL.US")], now=at("2026-01-30", 16, 1)
        )
        self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-02-02", 100.0)],
        )
        self.service.tick(
            "2026-02-03",
            now=at("2026-02-03", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-02-03", 100.0)],
        )
        stopped = self.service.tick(
            "2026-02-03",
            now=at("2026-02-03", 16, 0, 5),
            observations=[
                daily_observation(
                    "AAPL.US",
                    "2026-02-03",
                    opening=100.0,
                    high=101.0,
                    low=91.0,
                    close=95.0,
                )
            ],
        )
        self.assertTrue(stopped["positions"])
        self.assertEqual(len(stopped["fills"]), 1)
        self.assertEqual(stopped["state"], "DATA_DEGRADED")
        self.assertIn(
            "MISSED_INTRADAY_STOP:AAPL.US",
            stopped["session"]["degraded_reason"],
        )
        # Re-open the database to prove the next-open obligation is durable,
        # not transient worker memory.
        restarted = USMomentumPaperService(self.config)
        pending = restarted.status()
        self.assertEqual(1, pending["positions"][0]["recovery_exit_pending"])
        self.assertEqual(
            "US_MISSED_INTRADAY_STOP_RECOVERY",
            pending["positions"][0]["recovery_reason"],
        )
        self.assertEqual("2026-02-03", pending["positions"][0]["recovery_detected_session"])
        self.assertEqual(1, len(pending["recovery_exits"]))
        with self.assertRaisesRegex(USPaperStateError, "risk exits are pending"):
            restarted.acknowledge_data_recovery(
                note="cannot waive an economic exit",
                now=at("2026-02-03", 16, 1),
            )

        # The later DAILY low did not fabricate an intraday fill.  The first
        # subsequent timely Open exits unconditionally, even above the stop.
        recovered = restarted.tick(
            "2026-02-04",
            now=at("2026-02-04", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-02-04", 97.0)],
        )
        self.assertEqual([], recovered["positions"])
        self.assertEqual(2, len(recovered["fills"]))
        self.assertEqual(
            "US_MISSED_STOP_RECOVERY_OPEN", recovered["fills"][-1]["reason"]
        )
        self.assertEqual(97.0, recovered["fills"][-1]["price"])
        recovery_events = {row["event_type"] for row in restarted.status()["events"]}
        self.assertIn("NEXT_OPEN_RECOVERY_EXIT_SCHEDULED", recovery_events)
        self.assertIn("NEXT_OPEN_RECOVERY_EXIT_EXECUTED", recovery_events)

        # A fresh sleeve proves a missing closing mark is fail-closed.
        second_path = Path(self.temporary.name) / "missing-mark.sqlite"
        other = USMomentumPaperService(
            USPaperConfig(
                database_path=second_path,
                commission_rate=0,
                sec_sell_fee_rate=0,
                finra_taf_per_share=0,
                slippage_rate=0,
            )
        )
        other.create_period([signal("MSFT.US")], now=at("2026-01-30", 16, 1))
        other.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 5),
            observations=[open_observation("MSFT.US", "2026-02-02", 100.0)],
        )
        degraded = other.tick("2026-02-02", now=at("2026-02-02", 16, 1))
        self.assertEqual(degraded["state"], "DATA_DEGRADED")
        self.assertEqual(degraded["account_status"], "DATA_DEGRADED")

    def test_all_risk_exits_and_rebalance_sells_fill_globally_before_buys(self) -> None:
        self.service.create_period(
            [signal("ZZZ.US"), signal("MSFT.US")],
            now=at("2026-01-30", 16, 1),
        )
        self.service.tick(
            "2026-02-02",
            now=at("2026-02-02", 9, 30, 5),
            observations=[
                open_observation("ZZZ.US", "2026-02-02", 100.0),
                open_observation("MSFT.US", "2026-02-02", 100.0),
            ],
        )
        self.service.create_period(
            [
                signal(
                    "MSFT.US",
                    side="SELL",
                    generated_day="2026-02-27",
                    execution_day="2026-03-02",
                ),
                signal(
                    "AAA.US",
                    generated_day="2026-02-27",
                    execution_day="2026-03-02",
                ),
            ],
            now=at("2026-02-27", 16, 1),
        )

        # Deliberately submit the BUY quote first and give the gap-stop symbol
        # a lexically late ticker. Execution order must still be both SELLs,
        # then BUY.
        self.service.tick(
            "2026-03-02",
            now=at("2026-03-02", 9, 30, 5),
            observations=[
                open_observation("AAA.US", "2026-03-02", 50.0),
                open_observation("MSFT.US", "2026-03-02", 110.0),
                open_observation("ZZZ.US", "2026-03-02", 90.0),
            ],
        )
        fills = self.service.executor._store.rows(
            "SELECT code, side, reason FROM us_paper_fills ORDER BY rowid"
        )
        opening_fills = fills[-3:]
        self.assertEqual(["SELL", "SELL", "BUY"], [row["side"] for row in opening_fills])
        self.assertEqual(
            {"US_FIXED_STOP_GAP", "US_RS_EXIT"},
            {row["reason"] for row in opening_fills[:2]},
        )
        self.assertEqual("AAA.US", opening_fills[-1]["code"])

    def test_causal_time_gates_reject_future_information(self) -> None:
        with self.assertRaises(USPaperCausalityError):
            self.service.create_period(
                [signal("AAPL.US")], now=at("2026-01-30", 15, 59)
            )
        self.service.create_period(
            [signal("AAPL.US")], now=at("2026-01-30", 16, 1)
        )
        future = open_observation("AAPL.US", "2026-02-02", 100.0)
        future["available_at"] = at("2026-02-02", 9, 31)
        with self.assertRaises(USPaperCausalityError):
            self.service.observe(future, now=at("2026-02-02", 9, 30, 30))
        with self.assertRaises(USPaperCausalityError):
            self.service.observe(
                daily_observation(
                    "AAPL.US",
                    "2026-02-02",
                    opening=100,
                    high=101,
                    low=99,
                    close=100,
                ),
                now=at("2026-02-02", 15, 59),
            )

    def test_conflicting_observation_is_rejected(self) -> None:
        first = open_observation(
            "AAPL.US", "2026-02-02", 100.0, observation_id="same-key"
        )
        self.service.observe(first, now=at("2026-02-02", 9, 30, 5))
        changed = dict(first)
        changed["open"] = 101.0
        with self.assertRaises(USPaperConflictError):
            self.service.observe(changed, now=at("2026-02-02", 9, 30, 6))

    def test_kill_is_idempotent_cancels_buys_and_has_no_resume_path(self) -> None:
        self.service.create_period(
            [signal("AAPL.US")], now=at("2026-01-30", 16, 1)
        )
        first = self.service.kill(reason="operator paper kill", now=at("2026-01-30", 17, 0))
        second = self.service.kill(reason="ignored duplicate", now=at("2026-01-30", 17, 1))
        self.assertEqual(first["account"]["status"], "KILLED")
        self.assertEqual(second["account"]["status"], "KILLED")
        self.assertEqual(second["orders"][0]["status"], "CANCELLED")
        self.assertEqual(
            second["orders"][0]["block_reason"], "KILL_SWITCH_BUY_CANCELLED"
        )
        result = self.service.tick(
            "2026-02-02", now=at("2026-02-02", 9, 0)
        )
        self.assertEqual(result["state"], "KILLED")

    def test_kill_preserves_staged_sell_and_risk_exit_execution(self) -> None:
        self.buy_aapl()
        self.service.create_period(
            [
                signal(
                    "AAPL.US",
                    side="SELL",
                    generated_day="2026-02-27",
                    execution_day="2026-03-02",
                )
            ],
            now=at("2026-02-27", 16, 1),
        )
        killed = self.service.kill(
            reason="disable new exposure", now=at("2026-02-27", 17, 0)
        )
        sell = [row for row in killed["orders"] if row["side"] == "SELL"][0]
        self.assertEqual("WAITING_OPEN", sell["status"])
        self.assertEqual("NORMAL_EXIT", sell["risk_class"])

        result = self.service.tick(
            "2026-03-02",
            now=at("2026-03-02", 9, 30, 5),
            observations=[open_observation("AAPL.US", "2026-03-02", 101)],
        )
        self.assertEqual("KILLED", result["account_status"])
        self.assertEqual([], result["positions"])
        self.assertEqual("SELL", result["fills"][-1]["side"])
        with self.assertRaises(USPaperStateError):
            self.service.create_period(
                [
                    signal(
                        "MSFT.US",
                        generated_day="2026-02-27",
                        execution_day="2026-03-02",
                    )
                ],
                now=at("2026-02-27", 16, 1),
            )


if __name__ == "__main__":
    unittest.main()
