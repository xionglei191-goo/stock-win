from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.cash_instrument_validation import (
    apply_conservative_publication_rule,
    assess_readiness,
    create_cash_etf_snapshot,
    load_cash_etf_snapshot,
    parse_huabao_income,
    parse_yinhua_nav,
    reconcile_yinhua_dividends,
    verify_file_hash,
)
from research_platform.etf_pullback_research import EtfAsset


def _day_record(
    date_value: int,
    *,
    open_price: int = 100_000,
    high_price: int = 100_100,
    low_price: int = 99_900,
    close_price: int = 100_000,
) -> bytes:
    return struct.pack(
        "<IIIIIfII",
        date_value,
        open_price,
        high_price,
        low_price,
        close_price,
        1_000_000.0,
        10_000,
        0,
    )


class CashInstrumentValidationTests(unittest.TestCase):
    def test_future_day_append_does_not_change_snapshot_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            day_dir = root / "tdx" / "vipdoc" / "sh" / "lday"
            day_dir.mkdir(parents=True)
            day_path = day_dir / "sh511880.day"
            day_path.write_bytes(_day_record(20210401))
            asset = EtfAsset("511880.SH", "test", "sh", "sh511880")
            first = create_cash_etf_snapshot(
                tdx_root=root / "tdx",
                output_root=root / "snapshots",
                assets=(asset,),
            )

            day_path.write_bytes(
                _day_record(20210401)
                + _day_record(20260810, close_price=110_000, high_price=110_100)
            )
            second = create_cash_etf_snapshot(
                tdx_root=root / "tdx",
                output_root=root / "snapshots",
                assets=(asset,),
            )

            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            snapshot = load_cash_etf_snapshot(
                root / "snapshots" / first["snapshot_id"]
            )
            self.assertEqual(snapshot["timestamp"].max(), pd.Timestamp("2021-04-01"))

    def test_frozen_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_file_hash(path, "0" * 64)

    def test_yinhua_parser_allows_nonessential_yield_gap(self) -> None:
        payload = {
            "error_no": "0",
            "results": [
                {
                    "data": [
                        {
                            "nav_date": "20210401",
                            "relate_price": "100.1",
                            "cumulative_net": "125.1",
                            "profit_per_million": "0.2",
                            "seven_days_annual_profit": "",
                        }
                    ]
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "yinhua.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            frame = parse_yinhua_nav(path, start_date="2021-04-01", end_date="2021-04-01")
        self.assertEqual(len(frame), 1)
        self.assertTrue(pd.isna(frame.loc[0, "seven_day_annual_yield_percent"]))
        self.assertTrue(pd.isna(frame.loc[0, "publication_time"]))

    def test_yinhua_parser_rejects_duplicate_dates(self) -> None:
        row = {
            "nav_date": "20210401",
            "relate_price": "100.1",
            "cumulative_net": "125.1",
            "profit_per_million": "0.2",
            "seven_days_annual_profit": "1.5",
        }
        payload = {"error_no": "0", "results": [{"data": [row, row]}]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "yinhua.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate dates"):
                parse_yinhua_nav(path, start_date="2021-04-01", end_date="2021-04-01")

    def test_huabao_parser_preserves_publication_timestamp(self) -> None:
        payload = {
            "code": "0000",
            "data": [
                {
                    "navDate": "2021-04-01",
                    "fundIncome": 0.3,
                    "yield": 0.02,
                    "publishSign": "1",
                    "modifyTime": "20210401193000",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "huabao.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            frame = parse_huabao_income(path, start_date="2021-04-01", end_date="2021-04-01")
        self.assertEqual(frame.loc[0, "publication_time"], pd.Timestamp("2021-04-01 19:30:00"))

    def test_publication_rule_never_exposes_same_day_data(self) -> None:
        frame = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-05"])})
        result = apply_conservative_publication_rule(frame)
        self.assertEqual(
            result.loc[0, "publication_available_at"],
            pd.Timestamp("2024-01-06 23:59:59"),
        )

    def test_dividend_reconciliation_uses_ex_day_price_range(self) -> None:
        nav = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2023-12-28", "2023-12-29"]),
                "unit_nav": [100.05, 100.07],
                "cumulative_nav": [130.50, 130.52],
            }
        )
        bars = pd.DataFrame(
            {
                "code": ["511880.SH", "511880.SH"],
                "timestamp": pd.to_datetime(["2023-12-28", "2023-12-29"]),
                "Open": [101.0, 100.0],
                "High": [101.1, 100.05],
                "Low": [100.9, 99.95],
                "Close": [101.0, 100.02],
            }
        )
        record = {"ex_date": "2023-12-29", "dividend_per_unit": 1.0}
        passed = reconcile_yinhua_dividends(nav, bars, [record])
        self.assertTrue(passed["passed"])
        failed = reconcile_yinhua_dividends(
            nav, bars, [{**record, "dividend_per_unit": 2.0}]
        )
        self.assertFalse(failed["passed"])

    def test_missing_broker_evidence_keeps_results_sealed(self) -> None:
        readiness = assess_readiness(
            {
                "official_data": True,
                "account_broker_fee_schedule": False,
            }
        )
        self.assertEqual(readiness["decision"], "DATA_BLOCKED")
        self.assertFalse(readiness["development_results_unsealed"])
        self.assertFalse(readiness["production_authorized"])


if __name__ == "__main__":
    unittest.main()
