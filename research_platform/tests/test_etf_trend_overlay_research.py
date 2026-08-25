from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research_platform.config import PortfolioConfig
from research_platform.equity_etf_reversal_research import EquityEtfAsset
from research_platform.etf_pullback_research import DAY_DTYPE
from research_platform.etf_trend_overlay_research import (
    _annotate_trend_execution,
    assess_replication,
    create_replication_snapshot,
    load_replication_snapshot,
    protocol_manifest,
    simulate_v9_overlay,
)


class EtfTrendOverlayResearchTests(unittest.TestCase):
    def test_snapshot_excludes_holdout_and_verifies_hash(self) -> None:
        asset = EquityEtfAsset("510001.SH", "Industry ETF", "sh", "sh510001")
        records = np.zeros(3, dtype=DAY_DTYPE)
        records["date"] = [20240102, 20250724, 20250725]
        records["open"] = [1000, 1010, 1020]
        records["high"] = [1010, 1020, 1030]
        records["low"] = [990, 1000, 1010]
        records["close"] = [1005, 1015, 1025]
        records["amount"] = [100_000_000.0] * 3
        records["volume"] = [10_000_000] * 3
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "tdx" / "vipdoc" / "sh" / "lday" / "sh510001.day"
            source.parent.mkdir(parents=True)
            source.write_bytes(records.tobytes())
            manifest = create_replication_snapshot(
                tdx_root=root / "tdx",
                output_root=root / "snapshots",
                assets=[asset],
            )
            self.assertEqual(
                manifest["source_prefixes"][0]["maximum_decoded_date"], "2025-07-24"
            )
            snapshot_dir = root / "snapshots" / manifest["snapshot_id"]
            bars = load_replication_snapshot(snapshot_dir)
            self.assertEqual(len(bars), 2)
            self.assertEqual(str(bars["timestamp"].max().date()), "2025-07-24")
            path = snapshot_dir / "bars.parquet"
            path.write_bytes(path.read_bytes() + b"tampered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_replication_snapshot(snapshot_dir)

    def test_ma50_failure_exits_at_next_open_and_future_does_not_change_it(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=75)
        close = np.full(len(dates), 10.0)
        close[58:60] = 9.5
        bars = pd.DataFrame(
            {
                "code": "510001.SH",
                "timestamp": dates,
                "Open": np.full(len(dates), 10.0),
                "High": np.maximum(close, 10.0) * 1.01,
                "Low": np.minimum(close, 10.0) * 0.99,
                "Close": close,
            }
        )
        events = pd.DataFrame(
            {
                "code": ["510001.SH"],
                "signal_date": [dates[55]],
                "score": [1.0],
                "selected": [True],
                "overlay_selected": [True],
                "blocked_correlation": [False],
                "blocked_daily_capacity": [False],
            }
        )
        baseline = _annotate_trend_execution(events, bars, PortfolioConfig(), 1.0)
        row = baseline.iloc[0]
        self.assertEqual(row["exit_reason"], "MA50_TWO_DAY_BREAK")
        self.assertEqual(pd.Timestamp(row["exit_date"]), dates[60])
        self.assertTrue(bool(row["executable"]))

        future_dates = pd.bdate_range(dates[-1] + pd.Timedelta(days=1), periods=5)
        future = pd.DataFrame(
            {
                "code": "510001.SH",
                "timestamp": future_dates,
                "Open": 5.0,
                "High": 5.1,
                "Low": 4.9,
                "Close": 5.0,
            }
        )
        changed = _annotate_trend_execution(
            events, pd.concat([bars, future], ignore_index=True), PortfolioConfig(), 1.0
        )
        columns = ["entry_date", "entry_open", "exit_date", "exit_open", "exit_reason", "net_return"]
        pd.testing.assert_frame_equal(baseline[columns], changed[columns])

    def test_v9_trade_cashflow_is_preserved_without_etf_events(self) -> None:
        dates = pd.bdate_range("2024-07-01", periods=5)
        v9_trades = pd.DataFrame(
            [
                {
                    "timestamp": dates[0],
                    "side": "BUY",
                    "code": "000001.SZ",
                    "quantity": 100,
                    "price": 10.0,
                    "fees": 5.0,
                },
                {
                    "timestamp": dates[2],
                    "side": "SELL",
                    "code": "000001.SZ",
                    "quantity": 100,
                    "price": 11.0,
                    "fees": 5.0,
                },
            ]
        )
        v9_bars = pd.DataFrame(
            {
                "code": "000001.SZ",
                "timestamp": dates,
                "Close": [10.2, 10.5, 11.0, 11.0, 11.0],
            }
        )
        empty_etf = pd.DataFrame(columns=["entry_date", "score", "code"])
        empty_bars = pd.DataFrame(columns=["code", "timestamp", "Close"])
        result = simulate_v9_overlay(
            v9_trades,
            v9_bars,
            empty_etf,
            empty_bars,
            dates,
            initial_cash=50_000.0,
            config=PortfolioConfig(initial_cash=50_000.0),
        )
        self.assertEqual(result["v9_cash_blocked"], 0)
        self.assertEqual(result["v9_trade_rows_processed"], 2)
        self.assertAlmostEqual(result["portfolio_final_equity"], 50_090.0, places=10)
        self.assertAlmostEqual(result["portfolio_total_return"], 0.0018, places=10)

    def test_passing_replication_still_keeps_holdout_closed(self) -> None:
        report = {
            "portfolio_trades": 20,
            "portfolio_annualized_return": 0.08,
            "portfolio_total_return": 0.08,
            "median_trade_return": 0.01,
            "ex_top3_contribution": 0.02,
            "portfolio_max_drawdown": -0.05,
            "fill_rate": 0.90,
        }
        overlay = {
            "incremental_total_return": 0.03,
            "combined_max_drawdown": -0.06,
            "daily_return_correlation": 0.25,
            "v9_reproduction_match": True,
            "v9_cash_blocked": 0,
        }
        decision = assess_replication(report, overlay)
        self.assertEqual(decision["decision"], "REQUIRE_SURVIVOR_AUDIT")
        self.assertTrue(decision["survivor_audit_required"])
        self.assertFalse(decision["holdout_opened"])
        self.assertEqual(
            protocol_manifest()["opening_rule"],
            "replication, survivor audit, then final holdout",
        )

        failed = assess_replication({**report, "portfolio_trades": 14}, overlay)
        self.assertEqual(failed["decision"], "REJECT")
        self.assertFalse(failed["survivor_audit_required"])


if __name__ == "__main__":
    unittest.main()
